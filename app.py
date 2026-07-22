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

    # Eliminar todo lo que no sea dígito o punto decimal (elimina la 'q', comas, espacios)
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

    # Alias comunes por si el Sheet usa otros encabezados
    alias = {
        "precio":     ["precio_q", "precio_quetzales", "valor", "precio_venta"],
        "anio":       ["ano", "año", "year", "modelo_anio"],
        "link_fotos": ["fotos", "link", "enlace", "url_fotos", "galeria"],
        "marca":      ["brand"],
        "modelo":     ["model", "linea"],
        "id":         ["codigo", "no", "num", "id_vehiculo"],
    }
    for destino, posibles in alias.items():
        if not out.get(destino):
            for p in posibles:
                if out.get(p):
                    out[destino] = out[p]
                    break
    return out

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

        # Mantener solo los últimos 10 mensajes para ahorrar tokens
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

        # Apps Script a veces envuelve el array dentro de un objeto
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

    # Carga sincrónica si está vacío o vencido
    if not data or edad > INVENTORY_CACHE_TTL:
        data = refrescar_inventario()

    return list(data)

def buscar_carro_por_id(vehicle_id: str):
    carros = obtener_inventario()
    vehicle_id = str(vehicle_id).strip().lower()
    for carro in carros:
        if str(carro.get("id", "")).strip().lower() == vehicle_id:
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
                "Busca vehículos en el inventario disponibles según filtros como marca, "
                "modelo, presupuesto máximo o año. Si se llama sin filtros devuelve el "
                "inventario general disponible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "marca": {"type": "string", "description": "Marca del auto, ej: Nissan, Chevrolet, Toyota"},
                    "modelo": {"type": "string", "description": "Modelo del auto, ej: 350z, Corvette"},
                    "precio_max": {"type": "number", "description": "Presupuesto máximo en Quetzales"},
                    "anio": {"type": "integer", "description": "Año específico del vehículo"}
                },
                "required": []
            }
        }
    }
]

def ejecutar_tool_inventario(marca=None, modelo=None, precio_max=None, anio=None):
    carros = obtener_inventario()
    resultados = []

    for c in carros:
        # Ignorar filas donde la marca esté vacía (elimina filas basura/fantasmas)
        marca_auto = str(c.get("marca") or "").strip()
        if not marca_auto:
            continue

        if marca and normalize_text(marca) not in normalize_text(marca_auto):
            continue
        if modelo and normalize_text(modelo) not in normalize_text(str(c.get("modelo") or "")):
            continue
        if anio and str(c.get("anio") or "").strip() != str(anio):
            continue
        if precio_max:
            val_precio = parse_price_value(c.get("precio"))
            if val_precio is None or val_precio > precio_max:
                continue

        resultados.append({
            "id": c.get("id"),
            "marca": marca_auto,
            "modelo": c.get("modelo"),
            "anio": c.get("anio"),
            "precio": c.get("precio"),
            "color": c.get("color") or "No especificado",
            "link_fotos": c.get("link_fotos") or ""
        })

    logger.info("TOOL inventario -> total=%d filtrado=%d (marca=%s modelo=%s max=%s anio=%s)",
                len(carros), len(resultados), marca, modelo, precio_max, anio)

    return {
        "total_inventario": len(carros),
        "coincidencias": len(resultados),
        "vehiculos": resultados[:15]
    }

# ─── Agente AI Principal ──────────────────────────────────────────────────────
def procesar_mensaje_con_agente(from_number: str, user_text_raw: str) -> str:
    if not openai_client:
        return "Servicio temporalmente en mantenimiento."

    historial = get_history(from_number)

    system_prompt = {
        "role": "system",
        "content": (
            "Eres el asesor virtual inteligente de Importadora Los Gemelos y Fer en Guatemala. "
            "Tu objetivo es ayudar a los clientes a encontrar vehículos en nuestro inventario, resolver dudas y agendar citas.\n\n"
            "REGLAS OBLIGATORIAS:\n"
            "1. Sé amable, persuasivo y muy conciso (máximo 2 a 3 párrafos cortos por mensaje). Usa emojis sutilmente.\n"
            "2. SIEMPRE usa la herramienta 'consultar_inventario' cuando te pregunten por marcas, modelos, precios, disponibilidad o presupuesto.\n"
            "3. NUNCA inventes autos, precios o características técnicas que no provengan del resultado de la herramienta.\n"
            "3b. Si la herramienta devuelve coincidencias = 0, decí claramente que no hay ese vehículo "
            "disponible en este momento y ofrecé alternativas reales del inventario. Nunca inventes.\n"
            "4. Cuando muestres autos de la herramienta, incluye su ID, Marca, Modelo, Año y Precio.\n"
            "5. Ubicación física: 35 Avenida 16-33 Zona 7, Villa Linda 2. Horarios: Lunes a Sábado 8:00 AM – 6:00 PM.\n"
            "6. Formas de pago: Contado y Visa Cuotas (No damos crédito propio ni prestamos).\n"
            "7. Si el cliente quiere hablar con un humano o asesor, indícale que un asesor tomará el chat en breve."
        )
    }

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
                if tool_call.function.name == "consultar_inventario":
                    try:
                        args = json.loads(tool_call.function.arguments or "{}")
                    except Exception:
                        args = {}

                    res_inventario = ejecutar_tool_inventario(
                        marca=args.get("marca"),
                        modelo=args.get("modelo"),
                        precio_max=args.get("precio_max"),
                        anio=args.get("anio")
                    )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": "consultar_inventario",
                        "content": json.dumps(res_inventario, ensure_ascii=False)
                    })

            segunda_respuesta = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.3
            )
            texto_final = segunda_respuesta.choices[0].message.content
        else:
            texto_final = response_message.content

        texto_final = (texto_final or "").strip() or "¿En qué más te puedo ayudar?"
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
        msg = (
            f"📊 *Estadísticas de Hoy*\n"
            f"Consultas Totales: {stats['consultas_hoy']}\n"
            f"Vehículos en inventario: {len(inv)}\n"
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
    return jsonify({
        "sheet_url_configurada": bool(SHEET_URL),
        "openai_configurado": bool(openai_client),
        "registros": len(data),
        "llaves_detectadas": list(data[0].keys()) if data else [],
        "muestra": data[:3]
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
