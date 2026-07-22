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

# ─── Constantes ───────────────────────────────────────────────────────────────
REQUEST_TIMEOUT       = 15
INVENTORY_CACHE_TTL   = 300
PROCESSED_MESSAGE_TTL = 600
USER_SESSION_TTL      = 3600  # Aumentado a 1 hora para memoria del agente
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

                # Limpiar sesiones antiguas de AI
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

# ─── Inventario (Google Sheets) ───────────────────────────────────────────────
def refrescar_inventario():
    if not SHEET_URL:
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
    except Exception as e:
        logger.error("Error refrescando inventario: %s", e)
    return inventory_cache["data"]

def obtener_inventario():
    with _inventory_lock:
        if not inventory_cache["data"]:
            pass # Si está vacío, intentará en background, usamos lo que haya
        return list(inventory_cache["data"])

def buscar_carro_por_id(vehicle_id: str):
    carros = obtener_inventario()
    vehicle_id = str(vehicle_id).strip().lower()
    for carro in carros:
        if str(carro.get("id", "")).strip().lower() == vehicle_id:
            return carro
    return None

# ─── Tools (Herramientas para el Agente) ──────────────────────────────────────
INVENTORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "consultar_inventario",
            "description": "Busca vehículos en el inventario disponibles según filtros como marca, modelo, presupuesto máximo o año.",
            "parameters": {
                "type": "object",
                "properties": {
                    "marca": {"type": "string", "description": "Marca del auto, ej: Toyota, Mazda"},
                    "modelo": {"type": "string", "description": "Modelo del auto, ej: Civic, RAV4"},
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
        if marca and normalize_text(marca) not in normalize_text(c.get("marca", "")):
            continue
        if modelo and normalize_text(modelo) not in normalize_text(c.get("modelo", "")):
            continue
        if anio and str(c.get("anio", "")).strip() != str(anio):
            continue
        if precio_max:
            val_precio = parse_price_value(c.get("precio", ""))
            if val_precio is None or val_precio > precio_max:
                continue
                
        resultados.append({
            "id": c.get("id"),
            "marca": c.get("marca"),
            "modelo": c.get("modelo"),
            "anio": c.get("anio"),
            "precio": c.get("precio"),
            "color": c.get("color", "No especificado"),
            "link_fotos": c.get("link_fotos", "Sin link disponible")
        })

    # Limitamos a 15 para no exceder los tokens del modelo
    return resultados[:15]

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
            "4. Cuando muestres autos de la herramienta, incluye su ID, Marca, Modelo, Año y Precio.\n"
            "5. Ubicación física: 35 Avenida 16-33 Zona 7, Villa Linda 2. Horarios: Lunes a Sábado 8:00 AM – 6:00 PM.\n"
            "6. Formas de pago: Contado y Visa Cuotas (No damos crédito propio ni prestamos).\n"
            "7. Si el cliente quiere hablar con un humano o asesor, indícale que un asesor tomará el chat en breve."
        )
    }

    messages = [system_prompt] + historial + [{"role": "user", "content": user_text_raw}]
    append_to_history(from_number, "user", user_text_raw)

    try:
        # Llamada inicial al LLM
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=INVENTORY_TOOLS,
            tool_choice="auto",
            temperature=0.3
        )

        response_message = response.choices[0].message

        # Validar si el LLM decidió usar una herramienta
        if response_message.tool_calls:
            messages.append(response_message)
            
            for tool_call in response_message.tool_calls:
                if tool_call.function.name == "consultar_inventario":
                    args = json.loads(tool_call.function.arguments)
                    
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

            # Segunda llamada para redactar la respuesta con los datos
            segunda_respuesta = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.3
            )
            texto_final = segunda_respuesta.choices[0].message.content
        else:
            texto_final = response_message.content

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
    
    # Comando de Administración
    if user_text == "adminstats" and from_number == ADMIN_PHONE:
        msg = f"📊 *Estadísticas de Hoy*\nConsultas Totales: {stats['consultas_hoy']}\n(Módulo AI Activo)"
        send_whatsapp_message(from_number, msg)
        return

    # Registrar estadísticas y leads
    with _state_lock:
        stats["consultas_hoy"] += 1
        if from_number not in known_users:
            known_users.add(from_number)
            guardar_lead(from_number, "Usuario Nuevo", "usuario_nuevo")

    # Procesar todo el lenguaje natural con OpenAI
    respuesta_agente = procesar_mensaje_con_agente(from_number, user_text_raw)
    
    # Enviar la respuesta construida por la AI al usuario
    send_whatsapp_message(from_number, respuesta_agente)

def handle_interactive_message(from_number: str, interactive: dict):
    """
    Si el usuario presiona un botón de un mensaje antiguo, 
    le pasamos el título del botón a la IA como si lo hubiera escrito.
    """
    interactive_type = interactive.get("type")
    user_text = ""
    
    if interactive_type == "list_reply":
        user_text = interactive.get("list_reply", {}).get("title", "")
    elif interactive_type == "button_reply":
        user_text = interactive.get("button_reply", {}).get("title", "")

    if user_text:
        # Enviar el texto del botón al agente
        handle_text_message(from_number, f"El usuario seleccionó la opción: {user_text}")


def process_single_message(message: dict):
    from_number  = message.get("from")
    message_id   = message.get("id")
    message_type = message.get("type")

    if not from_number: return "ok_no_from"

    if message_id:
        with _state_lock:
            if message_id in processed_messages: return "duplicate_ignored"
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
    if not APP_SECRET: return True
    if not signature_header or not signature_header.startswith("sha256="): return False
    expected = hmac.new(APP_SECRET.encode("utf-8"), request_data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)

@app.route("/", methods=["GET"])
def home(): return "Agente AI Activo", 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ai_sessions": len(user_chat_histories)}), 200

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

if __name__ == "__main__":
    try:
        refrescar_inventario()
    except:
        pass
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
