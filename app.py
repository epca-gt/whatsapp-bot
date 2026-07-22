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
from openai import OpenAI

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
APP_SECRET        = os.getenv("APP_SECRET")

ADMIN_PHONE       = os.getenv("ADMIN_PHONE", "50230306187")
SHEET_URL         = os.getenv("SHEET_URL")
LEADS_WEBHOOK_URL = os.getenv("LEADS_WEBHOOK_URL")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")

WHATSAPP_API_URL  = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
GUATEMALA_TZ      = ZoneInfo("America/Guatemala")

# Inicializar cliente OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY NO configurada. El agente no podrá responder.")

# ─── Constantes ───────────────────────────────────────────────────────────────
REQUEST_TIMEOUT       = 15
INVENTORY_CACHE_TTL   = 300
PROCESSED_MESSAGE_TTL = 600
USER_SESSION_TTL      = 3600  # 1 hora para memoria del agente
SEMANTIC_DUPLICATE_TTL = 20
RATE_LIMIT_MAX        = 10
RATE_LIMIT_WINDOW     = 60

MAX_RESULTADOS_LISTA  = 12   # tope de vehículos que se mandan al modelo al listar
MAX_ITEMS_RESUMEN     = 6    # cuántas viñetas de 'descripcion' van en el listado

# La columna 'estado' del Sheet marca disponibilidad (Disponible / Vendido / etc.)
FILTRAR_POR_ESTADO     = True
ESTADOS_NO_DISPONIBLES = ("vendido", "apartado", "reservado", "entregado", "no disponible")

# ─── Estado en memoria ────────────────────────────────────────────────────────
_inventory_lock = threading.Lock()
inventory_cache = {
    "data": [],
    "timestamp": 0,
    "last_success": 0
}

_state_lock       = threading.Lock()
processed_messages  = {}
known_users         = set()
recent_user_messages = {}
user_sessions       = {}
user_chat_histories = {}  # Memoria del Agente AI
user_rate_limits    = {}

# ─── Estadísticas en memoria ──────────────────────────────────────────────────
stats = {
    "consultas_hoy":     0,
    "vehiculos_vistos":  {},
    "asesores_hoy":      [],
    "fecha":             None
}

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

def parse_price_value(price_text):
    """Extrae únicamente los números y el punto decimal, ignorando Q, comas, etc."""
    if price_text is None:
        return None
    text = str(price_text).strip().lower()
    if not text:
        return None

    text = re.sub(r'[^\d.]', '', text.replace(',', ''))

    try:
        return float(text)
    except (ValueError, TypeError):
        return None

def _normalize_row(row: dict) -> dict:
    """Convierte llaves a minúsculas sin acentos: 'Precio Q' -> 'precio_q'."""
    out = {}
    for k, v in row.items():
        key = normalize_text(str(k)).replace(" ", "_")
        out[key] = v.strip() if isinstance(v, str) else v

    alias = {
        "precio":      ["precio_q", "precio_quetzales", "valor", "precio_venta"],
        "anio":        ["ano", "year", "modelo_anio"],
        "link_fotos":  ["fotos", "link", "enlace", "url_fotos", "galeria"],
        "marca":       ["brand"],
        "modelo":      ["model", "linea"],
        "id":          ["codigo", "no", "num", "id_vehiculo"],
        "millaje":     ["kilometraje", "km", "millas", "mileage"],
        "transmision": ["caja", "transmission"],
        "combustible": ["gasolina", "fuel", "tipo_combustible"],
        "descripcion": ["detalles", "equipamiento", "extras", "notas"],
    }
    for destino, posibles in alias.items():
        if not out.get(destino):
            for p in posibles:
                if out.get(p):
                    out[destino] = out[p]
                    break
    return out

def _limpiar_vinieta(linea: str) -> str:
    """Quita emojis/bullets del inicio de cada línea de la descripción."""
    return re.sub(r"^[\s\-•*\u2000-\u3300\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]+", "", linea).strip()

def resumir_descripcion(desc, max_items: int = MAX_ITEMS_RESUMEN) -> str:
    """Convierte la descripción larga en una línea corta para el listado."""
    if not desc:
        return ""
    lineas = [_limpiar_vinieta(l) for l in str(desc).split("\n")]
    lineas = [l for l in lineas if l]
    if not lineas:
        return ""
    resumen = " • ".join(lineas[:max_items])
    restantes = len(lineas) - max_items
    if restantes > 0:
        resumen += f" (+{restantes} detalles más)"
    return resumen

def formatear_descripcion(desc) -> list:
    """Devuelve la descripción completa como lista limpia de características."""
    if not desc:
        return []
    lineas = [_limpiar_vinieta(l) for l in str(desc).split("\n")]
    return [l for l in lineas if l]

def esta_disponible(carro: dict) -> bool:
    if not FILTRAR_POR_ESTADO:
        return True
    estado = normalize_text(str(carro.get("estado") or ""))
    if not estado:
        return True  # sin dato = asumimos disponible
    return estado not in ESTADOS_NO_DISPONIBLES

def limpiar_markdown_whatsapp(texto: str) -> str:
    """Red de seguridad: convierte Markdown al formato que sí renderiza WhatsApp."""
    if not texto:
        return texto
    texto = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1: \2", texto)
    texto = re.sub(r"\*\*([^\*]+)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", texto, flags=re.MULTILINE)
    return texto.strip()

# ─── Memoria Conversacional del Agente ────────────────────────────────────────
def append_to_history(phone: str, role: str, content: str):
    with _state_lock:
        if phone not in user_chat_histories:
            user_chat_histories[phone] = {
                "messages": [],
                "updated_at": now_ts()
            }
        user_chat_histories[phone]["messages"].append({"role": role, "content": content})
        user_chat_histories[phone]["updated_at"] = now_ts()

        if len(user_chat_histories[phone]["messages"]) > 10:
            user_chat_histories[phone]["messages"] = user_chat_histories[phone]["messages"][-10:]

def get_history(phone: str):
    with _state_lock:
        history = user_chat_histories.get(phone, {}).get("messages", [])
        return list(history)

# ─── Inventario (Google Sheets) ───────────────────────────────────────────────
def refrescar_inventario():
    if not SHEET_URL:
        logger.error("SHEET_URL NO configurada en Render")
        return inventory_cache["data"]
    try:
        r = requests.get(SHEET_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        logger.info("Sheet -> status=%s ct=%s bytes=%s",
                    r.status_code, r.headers.get("Content-Type"), len(r.content))
        r.raise_for_status()

        try:
            data = r.json()
        except Exception:
            logger.error("Respuesta NO es JSON. Primeros 300: %s", r.text[:300])
            return inventory_cache["data"]

        if isinstance(data, dict):
            for k in ("data", "items", "rows", "inventario", "result", "values"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break

        if not isinstance(data, list):
            logger.error("Formato inesperado del Sheet: %s", type(data))
            return inventory_cache["data"]

        limpio = [_normalize_row(x) for x in data if isinstance(x, dict)]

        with _inventory_lock:
            inventory_cache["data"] = limpio
            inventory_cache["timestamp"] = now_ts()
            inventory_cache["last_success"] = now_ts()

        logger.info("Inventario OK: %d registros. Llaves: %s",
                    len(limpio), list(limpio[0].keys()) if limpio else [])
        return limpio

    except Exception as e:
        logger.error("Error refrescando inventario: %s", e)
    return inventory_cache["data"]

def obtener_inventario():
    with _inventory_lock:
        data = list(inventory_cache["data"])
        edad = now_ts() - inventory_cache["timestamp"]

    if not data or edad > INVENTORY_CACHE_TTL:
        data = refrescar_inventario()

    return list(data)

def buscar_carro_por_id(vehicle_id: str):
    carros = obtener_inventario()
    objetivo = normalize_text(str(vehicle_id))
    for carro in carros:
        if normalize_text(str(carro.get("id", ""))) == objetivo:
            return carro
    return None

def buscar_carro_por_texto(texto: str):
    """Fallback: encuentra un carro por 'Nissan 350z' si no dieron el ID."""
    carros = obtener_inventario()
    consulta = normalize_text(texto)
    if not consulta:
        return None
    for carro in carros:
        etiqueta = normalize_text(
            f"{carro.get('marca','')} {carro.get('modelo','')} {carro.get('anio','')}"
        )
        if not etiqueta.strip():
            continue
        if consulta in etiqueta or etiqueta in consulta:
            return carro
    # Coincidencia parcial por palabras
    palabras = [p for p in consulta.split() if len(p) > 2]
    for carro in carros:
        etiqueta = normalize_text(f"{carro.get('marca','')} {carro.get('modelo','')}")
        if palabras and all(p in etiqueta for p in palabras):
            return carro
    return None

# ─── Cleanup e inventario en background ──────────────────────────────────────
def _inventory_refresh_loop():
    time.sleep(10)
    while True:
        try:
            refrescar_inventario()
        except Exception as e:
            logger.error("Error en refresh automático de inventario: %s", e)
        time.sleep(240)

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

                expired_histories = [
                    p for p, data in user_chat_histories.items()
                    if current - data.get("updated_at", 0) > USER_SESSION_TTL
                ]
                for p in expired_histories:
                    user_chat_histories.pop(p, None)

            logger.info("Cleanup ejecutado. Sesiones AI activas: %d", len(user_chat_histories))
        except Exception as e:
            logger.error("Error en cleanup: %s", e)

threading.Thread(target=_inventory_refresh_loop, daemon=True).start()
threading.Thread(target=_cleanup_loop, daemon=True).start()

# ─── Tools (Herramientas para el Agente) ──────────────────────────────────────
INVENTORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "consultar_inventario",
            "description": (
                "Lista vehículos disponibles según filtros (marca, modelo, rango de precio, año). "
                "Devuelve datos resumidos de cada auto. Usar para búsquedas y comparaciones. "
                "Si el cliente pide TODOS los detalles o el equipamiento de un auto puntual, "
                "usar 'detalle_vehiculo' en lugar de esta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "marca": {
                        "type": "string",
                        "description": "Marca del auto, ej: Nissan, Chevrolet, Toyota"
                    },
                    "modelo": {
                        "type": "string",
                        "description": "Modelo del auto, ej: 350z, Corvette"
                    },
                    "precio_max": {
                        "type": "number",
                        "description": (
                            "Precio MÁXIMO en Quetzales. Usar cuando el cliente dice "
                            "'hasta', 'menos de', 'máximo', 'no más de', 'presupuesto de'"
                        )
                    },
                    "precio_min": {
                        "type": "number",
                        "description": (
                            "Precio MÍNIMO en Quetzales. Usar cuando el cliente dice "
                            "'más de', 'arriba de', 'mayor a', 'desde', 'superior a'"
                        )
                    },
                    "anio": {
                        "type": "integer",
                        "description": "Año específico del vehículo"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detalle_vehiculo",
            "description": (
                "Devuelve la ficha COMPLETA de un vehículo: motor, transmisión, millaje, "
                "combustible, color, estado y la lista completa de equipamiento y extras. "
                "Usar siempre que el cliente pregunte por características, equipamiento, "
                "millaje, motor o 'qué trae' un auto específico."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "ID exacto del vehículo según el inventario, ej: 1"
                    },
                    "descripcion_vehiculo": {
                        "type": "string",
                        "description": (
                            "Alternativa si no se conoce el ID: marca y modelo tal como los "
                            "mencionó el cliente, ej: 'Nissan 350z' o 'Mazda 6 Touring'"
                        )
                    }
                },
                "required": []
            }
        }
    }
]

def ejecutar_tool_inventario(marca=None, modelo=None, precio_max=None, anio=None, precio_min=None):
    carros = obtener_inventario()
    resultados = []

    for c in carros:
        marca_auto = str(c.get("marca") or "").strip()
        if not marca_auto:
            continue
        if not esta_disponible(c):
            continue

        if marca and normalize_text(marca) not in normalize_text(marca_auto):
            continue
        if modelo and normalize_text(modelo) not in normalize_text(str(c.get("modelo") or "")):
            continue
        if anio and str(c.get("anio") or "").strip() != str(anio):
            continue

        if precio_max or precio_min:
            val_precio = parse_price_value(c.get("precio"))
            if val_precio is None:
                continue
            if precio_max and val_precio > precio_max:
                continue
            if precio_min and val_precio < precio_min:
                continue

        resultados.append({
            "id":          c.get("id"),
            "marca":       marca_auto,
            "modelo":      c.get("modelo"),
            "anio":        c.get("anio"),
            "precio":      c.get("precio"),
            "motor":       c.get("motor") or "",
            "transmision": c.get("transmision") or "",
            "millaje":     c.get("millaje") or "",
            "combustible": c.get("combustible") or "",
            "color":       c.get("color") or "",
            "extras_resumen": resumir_descripcion(c.get("descripcion")),
            "link_fotos":  c.get("link_fotos") or ""
        })

    logger.info(
        "TOOL lista -> total=%d filtrado=%d (marca=%s modelo=%s min=%s max=%s anio=%s)",
        len(carros), len(resultados), marca, modelo, precio_min, precio_max, anio
    )

    return {
        "total_inventario": len(carros),
        "coincidencias": len(resultados),
        "nota": "Los extras vienen resumidos. Para la ficha completa usá detalle_vehiculo con el id.",
        "vehiculos": resultados[:MAX_RESULTADOS_LISTA]
    }

def ejecutar_tool_detalle(id=None, descripcion_vehiculo=None):
    carro = None
    if id:
        carro = buscar_carro_por_id(id)
    if not carro and descripcion_vehiculo:
        carro = buscar_carro_por_texto(descripcion_vehiculo)

    if not carro:
        logger.info("TOOL detalle -> NO encontrado (id=%s texto=%s)", id, descripcion_vehiculo)
        return {
            "encontrado": False,
            "mensaje": "No se encontró ese vehículo en el inventario."
        }

    disponible = esta_disponible(carro)
    logger.info("TOOL detalle -> id=%s %s %s disponible=%s",
                carro.get("id"), carro.get("marca"), carro.get("modelo"), disponible)

    return {
        "encontrado": True,
        "disponible": disponible,
        "id":          carro.get("id"),
        "marca":       carro.get("marca"),
        "modelo":      carro.get("modelo"),
        "anio":        carro.get("anio"),
        "precio":      carro.get("precio"),
        "estado":      carro.get("estado") or "",
        "motor":       carro.get("motor") or "No especificado",
        "transmision": carro.get("transmision") or "No especificado",
        "millaje":     carro.get("millaje") or "No especificado",
        "combustible": carro.get("combustible") or "No especificado",
        "color":       carro.get("color") or "No especificado",
        "equipamiento": formatear_descripcion(carro.get("descripcion")),
        "link_fotos":  carro.get("link_fotos") or ""
    }

def despachar_tool(nombre: str, args: dict):
    if nombre == "consultar_inventario":
        return ejecutar_tool_inventario(
            marca=args.get("marca"),
            modelo=args.get("modelo"),
            precio_max=args.get("precio_max"),
            precio_min=args.get("precio_min"),
            anio=args.get("anio")
        )
    if nombre == "detalle_vehiculo":
        return ejecutar_tool_detalle(
            id=args.get("id"),
            descripcion_vehiculo=args.get("descripcion_vehiculo")
        )
    return {"error": f"Herramienta desconocida: {nombre}"}

# ─── Agente AI Principal ──────────────────────────────────────────────────────
SYSTEM_PROMPT_TEXT = (
    "Eres el asesor virtual inteligente de Importadora Los Gemelos y Fer en Guatemala. "
    "Tu objetivo es ayudar a los clientes a encontrar vehículos en nuestro inventario, "
    "resolver dudas y agendar citas.\n\n"

    "REGLAS OBLIGATORIAS:\n"

    "1. Sé amable, persuasivo y conciso. Usa emojis sutilmente.\n"

    "2. Usá 'consultar_inventario' para buscar o listar autos (marcas, modelos, precios, "
    "presupuesto, disponibilidad general).\n"

    "3. Usá 'detalle_vehiculo' cuando el cliente pregunte por UN auto específico: "
    "equipamiento, qué trae, motor, millaje, transmisión, color o cualquier detalle técnico. "
    "Pasá el id si lo conocés; si no, pasá marca y modelo en 'descripcion_vehiculo'.\n"

    "4. NUNCA inventes autos, precios, millaje ni características. Todo dato debe venir de "
    "una herramienta. Si un campo dice 'No especificado', decí que lo confirmás con un asesor.\n"

    "5. Si 'coincidencias' es 0, decí claramente que no hay ese vehículo disponible y ofrecé "
    "alternativas reales del inventario.\n"

    "6. Respetá el sentido del rango de precio: 'más de X' es precio_min=X, "
    "'hasta X' o 'menos de X' es precio_max=X. Jamás afirmes que no hay autos en un rango "
    "sin haber consultado ese rango exacto.\n"

    "7. FORMATO WHATSAPP OBLIGATORIO: prohibido Markdown. Nada de [texto](url), nada de "
    "## títulos, nada de **doble asterisco**. Para negrita usá UN solo asterisco: *así*. "
    "Los links van pelados, sin paréntesis ni corchetes.\n"

    "8. Al LISTAR vehículos: máximo 4 por mensaje, y por auto solo esto:\n"
    "*Marca Modelo* (Año)\n"
    "Q00,000 | ID: xx\n"
    "URL_DEL_LINK\n\n"
    "No pongas el equipamiento en los listados. Si hay más resultados, decí cuántos faltan "
    "y preguntá si quiere verlos o afinar la búsqueda.\n"

    "9. Al dar el DETALLE de un auto: encabezado con *Marca Modelo (Año)* y precio, luego "
    "motor, transmisión, millaje, combustible y color en líneas cortas, y después máximo 10 "
    "puntos del equipamiento con guiones. Si hay más, ofrecé mandar el resto o el link de fotos.\n"

    "10. Ubicación: 35 Avenida 16-33 Zona 7, Villa Linda 2. "
    "Horarios: Lunes a Sábado 8:00 AM – 6:00 PM.\n"

    "11. Formas de pago: Contado y Visa Cuotas (no damos crédito propio ni prestamos).\n"

    "12. Si el cliente quiere hablar con un humano o asesor, indicá que un asesor "
    "tomará el chat en breve."
)

def procesar_mensaje_con_agente(from_number: str, user_text_raw: str) -> str:
    if not openai_client:
        return "Servicio temporalmente en mantenimiento."

    historial = get_history(from_number)
    system_prompt = {"role": "system", "content": SYSTEM_PROMPT_TEXT}

    messages = [system_prompt] + historial + [{"role": "user", "content": user_text_raw}]
    append_to_history(from_number, "user", user_text_raw)

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=INVENTORY_TOOLS,
            tool_choice="auto",
            temperature=0.3
        )

        response_message = response.choices[0].message

        if response_message.tool_calls:
            messages.append(response_message)

            for tool_call in response_message.tool_calls:
                nombre = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except Exception:
                    args = {}

                resultado = despachar_tool(nombre, args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": nombre,
                    "content": json.dumps(resultado, ensure_ascii=False)
                })

            segunda_respuesta = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.3
            )
            texto_final = segunda_respuesta.choices[0].message.content
        else:
            texto_final = response_message.content

        texto_final = limpiar_markdown_whatsapp(texto_final or "")
        texto_final = texto_final or "¿En qué más te puedo ayudar?"

        append_to_history(from_number, "assistant", texto_final)
        return texto_final

    except Exception as e:
        logger.error("Error en OpenAI: %s", e)
        return "Disculpa, estoy procesando mucha información. ¿Puedes repetir tu pregunta en unos segundos?"

# ─── Leads y Mensajería WhatsApp ──────────────────────────────────────────────
def guardar_lead(telefono: str, mensaje: str, tipo: str):
    if not LEADS_WEBHOOK_URL:
        return
    def _guardar():
        try:
            payload = {
                "fecha": datetime.now(GUATEMALA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "telefono": telefono,
                "mensaje": mensaje,
                "tipo": tipo
            }
            requests.post(LEADS_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        except Exception:
            pass
    threading.Thread(target=_guardar, daemon=True).start()

def send_whatsapp_payload(payload: dict):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        res = requests.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        return res
    except Exception as e:
        logger.error("Error WhatsApp: %s", e)
        return None

def send_whatsapp_message(to_number: str, message_text: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text}
    }
    return send_whatsapp_payload(payload)

def build_advisor_link():
    return f"https://wa.me/{ADMIN_PHONE}?text={quote('Hola, vengo del bot')}"

# ─── Controladores Principales de Mensajes ────────────────────────────────────
def handle_text_message(from_number: str, user_text_raw: str):
    user_text = normalize_text(user_text_raw)

    if user_text == "adminstats" and from_number == ADMIN_PHONE:
        inv = obtener_inventario()
        disponibles = [c for c in inv if str(c.get("marca") or "").strip() and esta_disponible(c)]
        msg = (
            f"📊 *Estadísticas de Hoy*\n"
            f"Consultas Totales: {stats['consultas_hoy']}\n"
            f"Vehículos en Sheet: {len(inv)}\n"
            f"Disponibles: {len(disponibles)}\n"
            f"Sesiones AI activas: {len(user_chat_histories)}"
        )
        send_whatsapp_message(from_number, msg)
        return

    with _state_lock:
        stats["consultas_hoy"] += 1
        if from_number not in known_users:
            known_users.add(from_number)
            guardar_lead(from_number, "Usuario Nuevo", "usuario_nuevo")

    respuesta_agente = procesar_mensaje_con_agente(from_number, user_text_raw)
    send_whatsapp_message(from_number, respuesta_agente)

def handle_interactive_message(from_number: str, interactive: dict):
    interactive_type = interactive.get("type")
    user_text = ""

    if interactive_type == "list_reply":
        user_text = interactive.get("list_reply", {}).get("title", "")
    elif interactive_type == "button_reply":
        user_text = interactive.get("button_reply", {}).get("title", "")

    if user_text:
        handle_text_message(from_number, f"El usuario seleccionó la opción: {user_text}")

def process_single_message(message: dict):
    from_number  = message.get("from")
    message_id   = message.get("id")
    message_type = message.get("type")

    if not from_number:
        return "ok_no_from"

    if message_id:
        with _state_lock:
            if message_id in processed_messages:
                return "duplicate_ignored"
            processed_messages[message_id] = now_ts()

    if message_type == "text":
        handle_text_message(from_number, message.get("text", {}).get("body", "").strip())
        return "ok_text"

    if message_type == "interactive":
        handle_interactive_message(from_number, message.get("interactive", {}))
        return "ok_interactive"

    return f"ignored_{message_type}"

# ─── Rutas Webhook y Flask ────────────────────────────────────────────────────
def verify_meta_signature(request_data: bytes, signature_header: str) -> bool:
    if not APP_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode("utf-8"), request_data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)

@app.route("/", methods=["GET"])
def home():
    return "Agente AI Activo", 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ai_sessions": len(user_chat_histories),
        "inventario": len(inventory_cache["data"])
    }), 200

@app.route("/debug-inventario", methods=["GET"])
def debug_inventario():
    data = obtener_inventario()
    validos = [c for c in data if str(c.get("marca") or "").strip()]
    disponibles = [c for c in validos if esta_disponible(c)]
    return jsonify({
        "sheet_url_configurada": bool(SHEET_URL),
        "openai_configurado": bool(openai_client),
        "registros": len(data),
        "con_marca": len(validos),
        "disponibles": len(disponibles),
        "estados_encontrados": sorted({str(c.get("estado") or "(vacio)") for c in validos}),
        "llaves_detectadas": list(data[0].keys()) if data else [],
        "muestra": data[:2]
    }), 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Token inválido", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_meta_signature(request.data, signature):
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for message in change.get("value", {}).get("messages", []):
                    process_single_message(message)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("Error Webhook: %s", e)
        return jsonify({"status": "ok_error_handled"}), 200

# ─── Carga inicial (también aplica bajo gunicorn en Render) ───────────────────
try:
    refrescar_inventario()
except Exception as e:
    logger.error("Carga inicial de inventario falló: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
