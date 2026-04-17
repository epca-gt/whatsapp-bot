from flask import Flask, request, jsonify
import requests
import os
import json
import re
import time
import hmac
import hashlib
import threading
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
import logging

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Variables de entorno ─────────────────────────────────────────────────────
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID   = os.getenv("PHONE_NUMBER_ID")
APP_SECRET        = os.getenv("APP_SECRET")          # Para validar firma de Meta (opcional)

ADMIN_PHONE       = os.getenv("ADMIN_PHONE", "50230306187")
SHEET_URL         = os.getenv("SHEET_URL")
LEADS_WEBHOOK_URL = os.getenv("LEADS_WEBHOOK_URL")

WHATSAPP_API_URL  = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

# ─── Constantes ───────────────────────────────────────────────────────────────
REQUEST_TIMEOUT       = 15
INVENTORY_CACHE_TTL   = 300
PROCESSED_MESSAGE_TTL = 600
USER_SESSION_TTL      = 1800
SEMANTIC_DUPLICATE_TTL = 20
WHATSAPP_TEXT_LIMIT   = 3500
VEHICLE_LIST_LIMIT    = 15    # máximo vehículos a mostrar por lista
RATE_LIMIT_MAX        = 5     # mensajes máximos por ventana
RATE_LIMIT_WINDOW     = 60    # ventana en segundos

GUATEMALA_TZ = ZoneInfo("America/Guatemala")

# ─── Estado en memoria ────────────────────────────────────────────────────────
_inventory_lock = threading.Lock()
inventory_cache = {
    "data": [],
    "timestamp": 0,
    "last_success": 0
}

_state_lock       = threading.Lock()
processed_messages  = {}
recent_user_messages = {}
user_sessions       = {}
user_rate_limits    = {}   # {phone: [timestamp, ...]}


# ─── Helpers ──────────────────────────────────────────────────────────────────
def now_ts():
    return time.time()


def strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = strip_accents(text)
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


# ─── Cleanup en background (cada 2 minutos) ───────────────────────────────────
def _cleanup_loop():
    while True:
        time.sleep(120)
        try:
            current = now_ts()
            with _state_lock:
                for d, ttl in [
                    (processed_messages,   PROCESSED_MESSAGE_TTL),
                    (recent_user_messages, SEMANTIC_DUPLICATE_TTL),
                ]:
                    expired = [k for k, ts in d.items() if current - ts > ttl]
                    for k in expired:
                        d.pop(k, None)

                expired_sessions = [
                    p for p, s in user_sessions.items()
                    if current - s.get("updated_at", 0) > USER_SESSION_TTL
                ]
                for p in expired_sessions:
                    user_sessions.pop(p, None)

                expired_rate = [
                    p for p, ts_list in user_rate_limits.items()
                    if not [t for t in ts_list if current - t < RATE_LIMIT_WINDOW]
                ]
                for p in expired_rate:
                    user_rate_limits.pop(p, None)

            logger.info("Cleanup ejecutado. Sesiones activas: %d", len(user_sessions))
        except Exception as e:
            logger.error("Error en cleanup: %s", e)


threading.Thread(target=_cleanup_loop, daemon=True).start()


# ─── Rate limiting ────────────────────────────────────────────────────────────
def is_rate_limited(phone: str) -> bool:
    current = now_ts()
    with _state_lock:
        ts_list = user_rate_limits.get(phone, [])
        ts_list = [t for t in ts_list if current - t < RATE_LIMIT_WINDOW]
        if len(ts_list) >= RATE_LIMIT_MAX:
            user_rate_limits[phone] = ts_list
            return True
        ts_list.append(current)
        user_rate_limits[phone] = ts_list
        return False


# ─── Sesiones ─────────────────────────────────────────────────────────────────
def set_user_state(phone: str, state: str, extra: dict = None):
    with _state_lock:
        session = user_sessions.get(phone, {})
        session["state"] = state
        session["updated_at"] = now_ts()
        if extra:
            session.update(extra)
        user_sessions[phone] = session


def get_user_session(phone: str) -> dict:
    with _state_lock:
        return dict(user_sessions.get(phone, {}))


def get_user_state(phone: str) -> str:
    return get_user_session(phone).get("state", "")


def clear_user_state(phone: str):
    with _state_lock:
        user_sessions.pop(phone, None)


# ─── Inventario ───────────────────────────────────────────────────────────────
def refrescar_inventario():
    if not SHEET_URL:
        logger.error("SHEET_URL no configurada.")
        return inventory_cache["data"]

    try:
        response = requests.get(SHEET_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            with _inventory_lock:
                inventory_cache["data"] = data
                inventory_cache["timestamp"] = now_ts()
                inventory_cache["last_success"] = now_ts()
            logger.info("Inventario actualizado. Registros: %d", len(data))
            return data

        logger.warning("Respuesta de inventario inválida.")
        return inventory_cache["data"]

    except Exception as e:
        logger.error("Error refrescando inventario: %s", e)
        return inventory_cache["data"]


def obtener_inventario(force_refresh=False):
    current = now_ts()

    if force_refresh:
        return refrescar_inventario()

    with _inventory_lock:
        has_data = bool(inventory_cache["data"])
        age = current - inventory_cache["timestamp"]

    if not has_data or age > INVENTORY_CACHE_TTL:
        return refrescar_inventario()

    with _inventory_lock:
        return list(inventory_cache["data"])


def obtener_marcas_disponibles():
    carros = obtener_inventario()
    marcas_map = {}

    for carro in carros:
        marca_original = (carro.get("marca") or "").strip()
        marca_normalizada = normalize_text(marca_original)
        if marca_original and marca_normalizada not in marcas_map:
            marcas_map[marca_normalizada] = marca_original

    return sorted(marcas_map.values(), key=lambda x: normalize_text(x))


def buscar_marca_en_texto(user_text: str):
    user_text_norm = normalize_text(user_text)
    if not user_text_norm:
        return None

    marcas = obtener_marcas_disponibles()

    for marca in marcas:
        if normalize_text(marca) == user_text_norm:
            return marca

    for marca in marcas:
        if normalize_text(marca) in user_text_norm:
            return marca

    return None


def obtener_carros_por_marca(marca_buscada: str):
    carros = obtener_inventario()
    marca_norm = normalize_text(marca_buscada)
    return [c for c in carros if normalize_text(c.get("marca", "")) == marca_norm]


def buscar_carro_por_id(vehicle_id: str):
    carros = obtener_inventario()
    vehicle_id = str(vehicle_id).strip().lower()
    for carro in carros:
        if str(carro.get("id", "")).strip().lower() == vehicle_id:
            return carro
    return None


def parse_price_value(price_text):
    if price_text is None:
        return None

    text = str(price_text).strip().lower()
    if not text:
        return None

    text = (text
            .replace("gtq", "").replace("quetzales", "").replace("q", "")
            .replace("usd", "").replace("$", "")
            .replace(",", "").replace(" ", ""))

    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None

    try:
        return float(match.group(1))
    except (ValueError, TypeError):
        return None


def extraer_presupuesto(texto: str):
    if not texto:
        return None

    texto = texto.strip().lower()

    patrones = [
        r"presupuesto[:\s]*q?\$?\s*([\d,]+(?:\.\d+)?)",
        r"maximo[:\s]*q?\$?\s*([\d,]+(?:\.\d+)?)",
        r"máximo[:\s]*q?\$?\s*([\d,]+(?:\.\d+)?)",
        r"hasta[:\s]*q?\$?\s*([\d,]+(?:\.\d+)?)",
        r"\bq\s*([\d,]+(?:\.\d+)?)\b",
        r"\$\s*([\d,]+(?:\.\d+)?)\b",
    ]

    for patron in patrones:
        match = re.search(patron, texto, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except (ValueError, TypeError):
                pass

    solo_numero = re.fullmatch(r"[\d,]+(?:\.\d+)?", texto)
    if solo_numero:
        try:
            valor = float(texto.replace(",", ""))
            if valor >= 1000:
                return valor
        except (ValueError, TypeError):
            pass

    return None


def obtener_carros_por_presupuesto(presupuesto_max: float):
    carros = obtener_inventario()
    coincidencias = []
    for c in carros:
        precio_valor = parse_price_value(c.get("precio", ""))
        if precio_valor is not None and precio_valor <= presupuesto_max:
            coincidencias.append(c)
    return coincidencias


def extraer_vehicle_id(texto: str):
    if not texto:
        return None

    texto = texto.strip()

    if buscar_carro_por_id(texto):
        return texto

    patrones = [
        r"\bid[:\s#-]*([a-zA-Z0-9_-]+)\b",
        r"\bvehiculo[:\s#-]*([a-zA-Z0-9_-]+)\b",
        r"\bvehículo[:\s#-]*([a-zA-Z0-9_-]+)\b",
        r"\bcodigo[:\s#-]*([a-zA-Z0-9_-]+)\b",
        r"\bcódigo[:\s#-]*([a-zA-Z0-9_-]+)\b",
    ]

    for patron in patrones:
        match = re.search(patron, texto, re.IGNORECASE)
        if match:
            posible_id = match.group(1).strip()
            if buscar_carro_por_id(posible_id):
                return posible_id

    return None


# ─── Leads (asíncrono, no bloquea el response) ────────────────────────────────
def _guardar_lead_async(telefono: str, mensaje: str, tipo: str):
    if not LEADS_WEBHOOK_URL:
        logger.warning("LEADS_WEBHOOK_URL no configurada. Lead no guardado.")
        return
    try:
        payload = {
            "fecha": datetime.now(GUATEMALA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "telefono": telefono,
            "mensaje": mensaje,
            "tipo": tipo
        }
        response = requests.post(
            LEADS_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT
        )
        logger.info("Lead guardado [%s]: %s", response.status_code, tipo)
    except Exception as e:
        logger.error("Error guardando lead: %s", e)


def guardar_lead(telefono: str, mensaje: str, tipo: str):
    threading.Thread(
        target=_guardar_lead_async,
        args=(telefono, mensaje, tipo),
        daemon=True
    ).start()


# ─── WhatsApp API ─────────────────────────────────────────────────────────────
def send_whatsapp_payload(payload: dict):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(
            WHATSAPP_API_URL,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        if response.status_code != 200:
            logger.warning("WhatsApp API error %s: %s", response.status_code, response.text)
        return response
    except Exception as e:
        logger.error("Error enviando mensaje WhatsApp: %s", e)
        return None


def split_message(text: str, limit: int = WHATSAPP_TEXT_LIMIT):
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []

    for block in text.split("\n"):
        candidate = ("\n".join(current + [block])).strip()
        if len(candidate) <= limit:
            current.append(block)
        else:
            if current:
                chunks.append("\n".join(current).strip())
            current = [block]

    if current:
        chunks.append("\n".join(current).strip())

    return chunks


def send_whatsapp_message(to_number: str, message_text: str):
    parts = split_message(message_text)
    last_response = None

    for part in parts:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": part}
        }
        last_response = send_whatsapp_payload(payload)

    return last_response


def send_whatsapp_list_menu(to_number: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": "Bienvenido a Importadora Los Gemelos y Fer 🚗\n\nSelecciona una opción:"
            },
            "footer": {"text": "Atención automatizada"},
            "action": {
                "button": "Selecciona la opción",
                "sections": [
                    {
                        "title": "Menú principal",
                        "rows": [
                            {"id": "ver_vehiculos",      "title": "Ver vehículos",         "description": "Inventario disponible"},
                            {"id": "buscar_marca",       "title": "Buscar marca",           "description": "Toyota, Mazda, Nissan y más"},
                            {"id": "buscar_presupuesto", "title": "Buscar por presupuesto", "description": "Ej. Q150,000"},
                            {"id": "cotizar_importacion","title": "Cotizar importación",    "description": "Solicita una cotización"},
                            {"id": "hablar_asesor",      "title": "Hablar con asesor",      "description": "Atención personalizada"}
                        ]
                    }
                ]
            }
        }
    }
    return send_whatsapp_payload(payload)


def send_import_interest_buttons(to_number: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": "¿Te gustaría que un asesor te ayude a buscar opciones según el vehículo que tienes en mente?"
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "import_yes", "title": "Sí, quiero asesor"}},
                    {"type": "reply", "reply": {"id": "import_no",  "title": "No por ahora"}}
                ]
            }
        }
    }
    send_whatsapp_payload(payload)


def send_brand_list_menu(to_number: str):
    marcas = obtener_marcas_disponibles()

    if not marcas:
        send_whatsapp_message(to_number, "No encontré marcas disponibles en este momento.")
        return

    rows = [
        {
            "id": f"marca_{normalize_text(marca).replace(' ', '_')}",
            "title": marca[:24],
            "description": "Ver vehículos disponibles"
        }
        for marca in marcas[:10]
    ]

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "Selecciona una marca disponible:"},
            "footer": {"text": "Si no ves tu marca, escríbela manualmente"},
            "action": {
                "button": "Ver marcas",
                "sections": [{"title": "Marcas disponibles", "rows": rows}]
            }
        }
    }

    send_whatsapp_payload(payload)

    if len(marcas) > 10:
        restantes = ", ".join(marcas[10:])
        send_whatsapp_message(
            to_number,
            f"También puedes escribir manualmente estas marcas:\n{restantes}"
        )


def send_vehicle_messages(to_number: str, carros: list, marca_mostrada: str):
    if not carros:
        send_whatsapp_message(
            to_number,
            f"No encontré vehículos de {marca_mostrada} en este momento."
        )
        return

    mensaje = f"🚗 Vehículos disponibles de {marca_mostrada}:\n\n"

    for carro in carros[:VEHICLE_LIST_LIMIT]:
        carro_id = str(carro.get("id", "")).strip()
        marca    = (carro.get("marca")  or "").strip()
        modelo   = (carro.get("modelo") or "").strip()
        anio     = (carro.get("anio")   or "").strip()

        mensaje += f"• {marca} {modelo} {anio}\n"
        if carro_id:
            mensaje += f"🆔 ID: {carro_id}\n"
        mensaje += "\n"

    if len(carros) > VEHICLE_LIST_LIMIT:
        mensaje += f"_(Mostrando {VEHICLE_LIST_LIMIT} de {len(carros)}. Puedes refinar con presupuesto.)_\n\n"

    mensaje += (
        "Escribe el *ID* del vehículo para consultar información y precio, "
        "o escribe *menu* para volver al menú principal."
    )

    send_whatsapp_message(to_number, mensaje)


def build_advisor_link():
    text = quote("Hola, vengo del bot de Importadora Los Gemelos y El Fer")
    return f"https://wa.me/{ADMIN_PHONE}?text={text}"


# ─── Flujos de conversación ───────────────────────────────────────────────────
def mostrar_vehiculos(from_number: str):
    guardar_lead(from_number, "ver_vehiculos", "ver_vehiculos")
    carros = obtener_inventario()

    if not carros:
        send_whatsapp_message(from_number, "No hay vehículos disponibles en este momento.")
        return

    mensaje = "🚗 Vehículos disponibles:\n\n"

    for carro in carros[:VEHICLE_LIST_LIMIT]:
        carro_id = str(carro.get("id", "")).strip()
        marca    = (carro.get("marca")  or "").strip()
        modelo   = (carro.get("modelo") or "").strip()
        anio     = (carro.get("anio")   or "").strip()

        mensaje += f"• {marca} {modelo} {anio}\n"
        if carro_id:
            mensaje += f"🆔 ID: {carro_id}\n"
        mensaje += "\n"

    if len(carros) > VEHICLE_LIST_LIMIT:
        mensaje += f"_(Mostrando {VEHICLE_LIST_LIMIT} de {len(carros)}. Busca por marca o presupuesto para ver más.)_\n\n"

    mensaje += (
        "Puedes escribir una *marca* para filtrar resultados "
        "o escribir el *ID* para consultar información y precio."
    )

    send_whatsapp_message(from_number, mensaje)
    set_user_state(from_number, "awaiting_brand_or_id")


def iniciar_busqueda_marca(from_number: str):
    guardar_lead(from_number, "buscar_marca", "buscar_marca")
    set_user_state(from_number, "awaiting_brand_or_id")
    send_brand_list_menu(from_number)


def iniciar_busqueda_presupuesto(from_number: str):
    guardar_lead(from_number, "buscar_presupuesto", "buscar_presupuesto")
    set_user_state(from_number, "awaiting_budget")
    send_whatsapp_message(
        from_number,
        "Envíanos tu presupuesto máximo.\n\n"
        "Ejemplos:\n"
        "• Q150000\n"
        "• presupuesto 180000\n"
        "• máximo 200000"
    )


def responder_cotizacion(from_number: str):
    guardar_lead(from_number, "orientacion_importacion", "orientacion_importacion")

    mensaje = (
        "🚗 *Importación de vehículos por encargo*\n\n"
        "Para que tengas una idea clara, el costo de importar un vehículo se compone de:\n\n"
        "• Compra del vehículo en subasta de USA\n"
        "• Impuestos en Guatemala: *32% sobre el valor de compra*\n"
        "• Flete aproximado desde USA: *US$1,200 a US$1,600*\n"
        "• Grúa desde puerto hacia nuestro predio: *Q800*\n"
        "• Honorarios de gestión: *Q4,000*\n\n"
        "*El único valor que realmente varía es el precio de compra del vehículo en USA.*\n\n"
        "*Ejemplos orientativos:*\n\n"
        "🔹 *Ejemplo 1*\n"
        "Compra en subasta: Q35,000\n"
        "Impuestos (32%): Q11,200\n"
        "Flete estimado: Q9,000 a Q12,000\n"
        "Grúa: Q800\n"
        "Honorarios: Q4,000\n"
        "*Total aproximado:* Q60,000 a Q63,000\n\n"
        "🔹 *Ejemplo 2*\n"
        "Compra en subasta: Q50,000\n"
        "Impuestos (32%): Q16,000\n"
        "Flete estimado: Q9,000 a Q12,000\n"
        "Grúa: Q800\n"
        "Honorarios: Q4,000\n"
        "*Total aproximado:* Q79,800 a Q82,800\n\n"
        "🔹 *Ejemplo 3*\n"
        "Compra en subasta: Q70,000\n"
        "Impuestos (32%): Q22,400\n"
        "Flete estimado: Q9,000 a Q12,000\n"
        "Grúa: Q800\n"
        "Honorarios: Q4,000\n"
        "*Total aproximado:* Q106,200 a Q109,200\n\n"
        "Cada vehículo puede variar dependiendo de modelo, año, condición y precio en subasta.\n\n"
        "Tú eliges el carro y el presupuesto, nosotros nos encargamos de buscarlo, comprarlo e importarlo por ti.\n\n"
        "Si ya tienes un vehículo en mente, escríbenos cuál buscas y un asesor te orienta."
    )

    send_whatsapp_message(from_number, mensaje)
    send_import_interest_buttons(from_number)


def responder_asesor(from_number: str):
    guardar_lead(from_number, "asesor", "quiere_asesor")
    clear_user_state(from_number)
    send_whatsapp_message(
        from_number,
        "Perfecto 👍\n\n"
        f"Habla directamente con nuestro asesor:\n\n👨‍💼 Paolo\n{build_advisor_link()}"
    )


def responder_precio_por_id(from_number: str, vehicle_id: str):
    carro = buscar_carro_por_id(vehicle_id)

    if not carro:
        send_whatsapp_message(
            from_number,
            "No encontré un vehículo con ese ID.\n\nRevisa el código y vuelve a intentarlo o escribe *menu*."
        )
        return

    marca       = (carro.get("marca")       or "").strip()
    modelo      = (carro.get("modelo")      or "").strip()
    anio        = (carro.get("anio")        or "").strip()
    precio      = (carro.get("precio")      or "").strip()
    descripcion = (carro.get("descripcion") or "").strip()
    link_fotos  = (carro.get("link_fotos")  or "").strip()

    guardar_lead(from_number, f"id:{vehicle_id}", "consulta_precio_por_id")

    partes = [
        "💰ℹ️ Detalles y Precio del vehículo solicitado:\n",
        f"🚗 {marca} {modelo} {anio}",
        f"🆔 ID: {vehicle_id}",
        f"📋 Descripción:\n{descripcion}" if descripcion else "",
        f"📸 Ver fotos del vehículo:\n{link_fotos}" if link_fotos else "",
        f"💵 Precio: {precio if precio else 'No disponible en este momento'}",
        "\nEscribe *asesor* si deseas continuar con este vehículo, o *menu* para volver a ver las opciones."
    ]

    mensaje = "\n".join([p for p in partes if p])
    send_whatsapp_message(from_number, mensaje)


def manejar_marca(from_number: str, marca_detectada: str):
    coincidencias = obtener_carros_por_marca(marca_detectada)

    if not coincidencias:
        send_whatsapp_message(
            from_number,
            f"No encontré vehículos de {marca_detectada} en este momento.\n\nEscribe *menu* para volver al menú principal."
        )
        return

    guardar_lead(from_number, marca_detectada, "busqueda_marca")
    set_user_state(from_number, "awaiting_vehicle_id", {"last_brand": marca_detectada})
    send_vehicle_messages(from_number, coincidencias, marca_detectada)


def manejar_presupuesto(from_number: str, presupuesto: float):
    coincidencias = obtener_carros_por_presupuesto(presupuesto)

    if not coincidencias:
        send_whatsapp_message(
            from_number,
            f"No encontré vehículos dentro de un presupuesto de Q{presupuesto:,.0f}.\n\n"
            "Puedes probar con otro monto o escribir *menu*."
        )
        return

    guardar_lead(from_number, f"presupuesto:{presupuesto}", "busqueda_presupuesto")
    set_user_state(from_number, "awaiting_vehicle_id", {"last_budget": presupuesto})

    mensaje = f"💰 Vehículos dentro de tu presupuesto de Q{presupuesto:,.0f}:\n\n"

    for carro in coincidencias[:VEHICLE_LIST_LIMIT]:
        carro_id = str(carro.get("id", "")).strip()
        marca    = (carro.get("marca")  or "").strip()
        modelo   = (carro.get("modelo") or "").strip()
        anio     = (carro.get("anio")   or "").strip()
        precio   = (carro.get("precio") or "").strip()

        mensaje += f"• {marca} {modelo} {anio}\n"
        if carro_id:
            mensaje += f"🆔 ID: {carro_id}\n"
        if precio:
            mensaje += f"💵 {precio}\n"
        mensaje += "\n"

    if len(coincidencias) > VEHICLE_LIST_LIMIT:
        mensaje += f"_(Mostrando {VEHICLE_LIST_LIMIT} de {len(coincidencias)}.)_\n\n"

    mensaje += (
        "Escribe el *ID* del vehículo que te interese para ver detalles, "
        "o escribe una *marca* para filtrar más."
    )

    send_whatsapp_message(from_number, mensaje)


# ─── Deduplicación semántica ──────────────────────────────────────────────────
def is_semantic_duplicate(from_number: str, user_text_raw: str) -> bool:
    normalized = normalize_text(user_text_raw)
    if not normalized:
        return False

    key = f"{from_number}|{normalized}"
    current = now_ts()

    with _state_lock:
        previous = recent_user_messages.get(key)
        if previous and (current - previous) <= SEMANTIC_DUPLICATE_TTL:
            return True
        recent_user_messages[key] = current

    return False


# ─── Handlers de mensajes ─────────────────────────────────────────────────────
def handle_text_message(from_number: str, user_text_raw: str):
    user_text  = normalize_text(user_text_raw)
    state      = get_user_state(from_number)
    presupuesto = extraer_presupuesto(user_text_raw)

    saludos = {
        "hola", "buenas", "buenos dias", "buenas tardes",
        "buenas noches", "menu", "menú", "inicio", "start"
    }

    if user_text in saludos:
        guardar_lead(from_number, user_text, "saludo")
        clear_user_state(from_number)
        send_whatsapp_list_menu(from_number)
        return

    if user_text in {"asesor", "hablar con asesor"}:
        responder_asesor(from_number)
        return

    if state == "awaiting_budget" and presupuesto:
        manejar_presupuesto(from_number, presupuesto)
        return

    vehicle_id = extraer_vehicle_id(user_text_raw.strip())
    if vehicle_id:
        responder_precio_por_id(from_number, vehicle_id)
        return

    if presupuesto:
        manejar_presupuesto(from_number, presupuesto)
        return

    # Detección de marca (unificada — sin duplicar lógica)
    marca_detectada = buscar_marca_en_texto(user_text)
    if marca_detectada:
        manejar_marca(from_number, marca_detectada)
        return

    if state == "awaiting_import_quote":
        guardar_lead(from_number, user_text_raw, "detalle_cotizacion")
        clear_user_state(from_number)
        send_whatsapp_message(
            from_number,
            "Gracias, ya recibimos tu solicitud ✅\n\nUn asesor revisará tu información y te contactará."
        )
        return

    send_whatsapp_message(
        from_number,
        "No entendí tu mensaje.\n\n"
        "Escribe *menu* para ver las opciones disponibles o envía una *marca* o un *ID* de vehículo."
    )


def handle_interactive_message(from_number: str, interactive: dict):
    interactive_type = interactive.get("type")

    if interactive_type == "list_reply":
        selected_id = interactive.get("list_reply", {}).get("id", "")

        route_map = {
            "ver_vehiculos":      lambda: mostrar_vehiculos(from_number),
            "buscar_marca":       lambda: iniciar_busqueda_marca(from_number),
            "buscar_presupuesto": lambda: iniciar_busqueda_presupuesto(from_number),
            "cotizar_importacion":lambda: responder_cotizacion(from_number),
            "hablar_asesor":      lambda: responder_asesor(from_number),
        }

        if selected_id in route_map:
            route_map[selected_id]()
            return

        if selected_id.startswith("marca_"):
            marca_slug = selected_id.replace("marca_", "").replace("_", " ").strip()
            for marca in obtener_marcas_disponibles():
                if normalize_text(marca) == normalize_text(marca_slug):
                    manejar_marca(from_number, marca)
                    return

    if interactive_type == "button_reply":
        selected_id = interactive.get("button_reply", {}).get("id", "")

        if selected_id == "import_yes":
            guardar_lead(from_number, "quiere_asesor_importacion", "asesor_importacion")
            responder_asesor(from_number)
            return

        if selected_id == "import_no":
            send_whatsapp_message(
                from_number,
                "Perfecto 👍\n\nSi en algún momento deseas explorar opciones de importación, escríbenos y con gusto te orientamos."
            )
            return

        if selected_id.startswith("marca_"):
            marca_slug = selected_id.replace("marca_", "").replace("_", " ").strip()
            for marca in obtener_marcas_disponibles():
                if normalize_text(marca) == normalize_text(marca_slug):
                    manejar_marca(from_number, marca)
                    return


def process_single_message(message: dict):
    from_number  = message.get("from")
    message_id   = message.get("id")
    message_type = message.get("type")

    if not from_number:
        return "ok_no_from"

    # Rate limiting por usuario
    if is_rate_limited(from_number):
        logger.warning("Rate limit alcanzado para %s", from_number)
        return "rate_limited"

    # Deduplicación por ID
    if message_id:
        with _state_lock:
            if message_id in processed_messages:
                logger.info("Mensaje duplicado por ID ignorado: %s", message_id)
                return "duplicate_ignored"
            processed_messages[message_id] = now_ts()

    if message_type == "text":
        user_text_raw = message.get("text", {}).get("body", "").strip()

        if is_semantic_duplicate(from_number, user_text_raw):
            logger.info("Mensaje duplicado semántico ignorado: %s", from_number)
            return "semantic_duplicate_ignored"

        handle_text_message(from_number, user_text_raw)
        return "ok_text"

    if message_type == "interactive":
        handle_interactive_message(from_number, message.get("interactive", {}))
        return "ok_interactive"

    logger.info("Tipo de mensaje ignorado: %s", message_type)
    return f"ignored_{message_type}"


# ─── Validación de firma Meta ─────────────────────────────────────────────────
def verify_meta_signature(request_data: bytes, signature_header: str) -> bool:
    if not APP_SECRET:
        # Si no está configurado, se omite la validación (modo desarrollo)
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        APP_SECRET.encode("utf-8"),
        request_data,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature_header)


# ─── Rutas Flask ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200


@app.route("/health", methods=["GET"])
def health():
    with _inventory_lock:
        items       = len(inventory_cache["data"])
        last_success = inventory_cache["last_success"]

    return jsonify({
        "status": "ok",
        "inventory_items": items,
        "inventory_last_success": last_success,
        "active_sessions": len(user_sessions)
    }), 200


@app.route("/refresh-inventory", methods=["GET"])
def refresh_inventory_route():
    data = refrescar_inventario()
    return jsonify({"status": "ok", "items": len(data)}), 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Token inválido", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    # Validar firma de Meta
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_meta_signature(request.data, signature):
        logger.warning("Firma inválida en webhook.")
        return jsonify({"status": "unauthorized"}), 401

    data    = request.get_json(silent=True) or {}
    results = []

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                messages = change.get("value", {}).get("messages", [])
                for message in messages:
                    result = process_single_message(message)
                    results.append(result)

        if not results:
            return jsonify({"status": "ok_no_messages"}), 200

        return jsonify({"status": "ok", "results": results}), 200

    except Exception as e:
        logger.error("Error procesando webhook: %s", e)
        return jsonify({"status": "ok_error_handled"}), 200


# ─── Inicio ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        refrescar_inventario()
    except Exception as e:
        logger.error("No se pudo precargar inventario al iniciar: %s", e)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
