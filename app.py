from flask import Flask, request, jsonify
import requests
import os
import json
import re
import time
import unicodedata
from datetime import datetime
from urllib.parse import quote

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

ADMIN_PHONE = "50230306187"

SHEET_URL = "https://opensheet.elk.sh/1opEhxT7aat4GnVAEBcPqze84TSZMO3W-ji2jyHP8HZc/Sheet1"
LEADS_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbwN7uG2ft37ALx9A736YhsBs039czPJCA40YZU1RDIcj5g7viirf3BOVznS1TsgCxoh-w/exec"

WHATSAPP_API_URL = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

REQUEST_TIMEOUT = 15
INVENTORY_CACHE_TTL = 300
PROCESSED_MESSAGE_TTL = 600
USER_SESSION_TTL = 1800
SEMANTIC_DUPLICATE_TTL = 20
WHATSAPP_TEXT_LIMIT = 3500  # margen conservador

inventory_cache = {
    "data": [],
    "timestamp": 0,
    "last_success": 0
}

processed_messages = {}
recent_user_messages = {}
user_sessions = {}


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


def cleanup_processed_messages():
    current = now_ts()

    expired_ids = [
        msg_id for msg_id, ts in processed_messages.items()
        if current - ts > PROCESSED_MESSAGE_TTL
    ]
    for msg_id in expired_ids:
        processed_messages.pop(msg_id, None)

    expired_semantic = [
        key for key, ts in recent_user_messages.items()
        if current - ts > SEMANTIC_DUPLICATE_TTL
    ]
    for key in expired_semantic:
        recent_user_messages.pop(key, None)


def cleanup_user_sessions():
    current = now_ts()
    expired = [
        phone for phone, session in user_sessions.items()
        if current - session.get("updated_at", 0) > USER_SESSION_TTL
    ]
    for phone in expired:
        user_sessions.pop(phone, None)


def set_user_state(phone: str, state: str, extra: dict = None):
    session = user_sessions.get(phone, {})
    session["state"] = state
    session["updated_at"] = now_ts()

    if extra:
        session.update(extra)

    user_sessions[phone] = session


def get_user_session(phone: str) -> dict:
    cleanup_user_sessions()
    return user_sessions.get(phone, {})


def get_user_state(phone: str) -> str:
    return get_user_session(phone).get("state", "")


def clear_user_state(phone: str):
    user_sessions.pop(phone, None)


def refrescar_inventario():
    try:
        response = requests.get(SHEET_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            inventory_cache["data"] = data
            inventory_cache["timestamp"] = now_ts()
            inventory_cache["last_success"] = now_ts()
            print(f"[INFO] Inventario actualizado. Registros: {len(data)}")
            return data

        print("[WARN] Respuesta de inventario inválida.")
        return inventory_cache["data"]

    except Exception as e:
        print("[ERROR] Error refrescando inventario:", e)
        return inventory_cache["data"]


def obtener_inventario(force_refresh=False):
    current = now_ts()

    if force_refresh:
        return refrescar_inventario()

    if not inventory_cache["data"]:
        return refrescar_inventario()

    if current - inventory_cache["timestamp"] > INVENTORY_CACHE_TTL:
        return refrescar_inventario()

    return inventory_cache["data"]


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
    marcas = obtener_marcas_disponibles()

    if not user_text_norm:
        return None

    for marca in marcas:
        if normalize_text(marca) == user_text_norm:
            return marca

    for marca in marcas:
        marca_norm = normalize_text(marca)
        if marca_norm in user_text_norm:
            return marca

    return None


def obtener_carros_por_marca(marca_buscada: str):
    carros = obtener_inventario()
    marca_norm = normalize_text(marca_buscada)
    coincidencias = []

    for carro in carros:
        marca = normalize_text(carro.get("marca", ""))
        if marca == marca_norm:
            coincidencias.append(carro)

    return coincidencias


def buscar_carro_por_id(vehicle_id: str):
    carros = obtener_inventario()
    vehicle_id = str(vehicle_id).strip()

    for carro in carros:
        carro_id = str(carro.get("id", "")).strip()
        if carro_id.lower() == vehicle_id.lower():
            return carro

    return None


def extraer_vehicle_id(texto: str):
    if not texto:
        return None

    texto = texto.strip()

    # Exacto
    carro = buscar_carro_por_id(texto)
    if carro:
        return texto

    # Patrones tipo: ID 123 / id: 123 / vehículo 123
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


def guardar_lead(telefono: str, mensaje: str, tipo: str):
    try:
        payload = {
            "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "telefono": telefono,
            "mensaje": mensaje,
            "tipo": tipo
        }

        headers = {
            "Content-Type": "application/json"
        }

        response = requests.post(
            LEADS_WEBHOOK_URL,
            headers=headers,
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT
        )

        print(f"[INFO] Lead guardado: {response.status_code}")
        print("[INFO] Respuesta Apps Script:", response.text)

    except Exception as e:
        print("[ERROR] Error guardando lead:", e)


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
        print("[WA]", response.status_code, response.text)
        return response
    except Exception as e:
        print("[ERROR] Error enviando mensaje WhatsApp:", e)
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
            "text": {
                "body": part
            }
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
            "footer": {
                "text": "Atención automatizada"
            },
            "action": {
                "button": "Selecciona la opción",
                "sections": [
                    {
                        "title": "Menú principal",
                        "rows": [
                            {
                                "id": "ver_vehiculos",
                                "title": "Ver vehículos",
                                "description": "Inventario disponible"
                            },
                            {
                                "id": "buscar_marca",
                                "title": "Buscar marca",
                                "description": "Toyota, Mazda, Nissan y más"
                            },
                            {
                                "id": "cotizar_importacion",
                                "title": "Cotizar importación",
                                "description": "Solicita una cotización"
                            },
                            {
                                "id": "hablar_asesor",
                                "title": "Hablar con asesor",
                                "description": "Atención personalizada"
                            }
                        ]
                    }
                ]
            }
        }
    }
    return send_whatsapp_payload(payload)


def send_brand_list_menu(to_number: str):
    marcas = obtener_marcas_disponibles()

    if not marcas:
        send_whatsapp_message(to_number, "No encontré marcas disponibles en este momento.")
        return

    rows = []
    for marca in marcas[:10]:
        rows.append({
            "id": f"marca_{normalize_text(marca).replace(' ', '_')}",
            "title": marca[:24],
            "description": "Ver vehículos disponibles"
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": "Selecciona una marca disponible:"
            },
            "footer": {
                "text": "Si no ves tu marca, escríbela manualmente"
            },
            "action": {
                "button": "Ver marcas",
                "sections": [
                    {
                        "title": "Marcas disponibles",
                        "rows": rows
                    }
                ]
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

    for carro in carros:
        carro_id = str(carro.get("id", "")).strip()
        marca = (carro.get("marca") or "").strip()
        modelo = (carro.get("modelo") or "").strip()
        anio = (carro.get("anio") or "").strip()

        mensaje += f"• {marca} {modelo} {anio}\n"
        if carro_id:
            mensaje += f"🆔 ID: {carro_id}\n"
        mensaje += "\n"

    mensaje += (
        "Escribe el *ID* del vehículo para consultar precio o disponibilidad, "
        "o escribe *menu* para volver al menú principal."
    )

    send_whatsapp_message(to_number, mensaje)


def build_advisor_link():
    text = quote("Hola, vengo del bot de Importadora Los Gemelos y El Fer")
    return f"https://wa.me/{ADMIN_PHONE}?text={text}"


def mostrar_vehiculos(from_number: str):
    guardar_lead(from_number, "ver_vehiculos", "ver_vehiculos")
    carros = obtener_inventario()

    if not carros:
        send_whatsapp_message(from_number, "No hay vehículos disponibles en este momento.")
        return

    mensaje = "🚗 Vehículos disponibles:\n\n"

    for carro in carros:
        carro_id = str(carro.get("id", "")).strip()
        marca = (carro.get("marca") or "").strip()
        modelo = (carro.get("modelo") or "").strip()
        anio = (carro.get("anio") or "").strip()

        mensaje += f"• {marca} {modelo} {anio}\n"
        if carro_id:
            mensaje += f"🆔 ID: {carro_id}\n"
        mensaje += "\n"

    mensaje += (
        "Puedes escribir una *marca* para filtrar resultados "
        "o escribir el *ID* para consultar precio y disponibilidad."
    )

    send_whatsapp_message(from_number, mensaje)
    set_user_state(from_number, "awaiting_brand_or_id")


def iniciar_busqueda_marca(from_number: str):
    guardar_lead(from_number, "buscar_marca", "buscar_marca")
    set_user_state(from_number, "awaiting_brand_or_id")
    send_brand_list_menu(from_number)


def responder_cotizacion(from_number: str):
    guardar_lead(from_number, "cotizar_importacion", "cotizar_importacion")
    set_user_state(from_number, "awaiting_import_quote")
    send_whatsapp_message(
        from_number,
        "Para cotizar importación, envíanos:\n\n"
        "• Marca\n"
        "• Modelo\n"
        "• Año aproximado\n"
        "• Presupuesto\n\n"
        "Ejemplo:\n"
        "Toyota Tacoma 2021, presupuesto Q180,000"
    )


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

    marca = (carro.get("marca") or "").strip()
    modelo = (carro.get("modelo") or "").strip()
    anio = (carro.get("anio") or "").strip()
    precio = (carro.get("precio") or "").strip()
    descripcion = (carro.get("descripcion") or "").strip()
    link_fotos = (carro.get("link_fotos") or "").strip()

    guardar_lead(from_number, f"id:{vehicle_id}", "consulta_precio_por_id")

    partes = [
        "💰 Precio del vehículo solicitado:\n",
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


def is_semantic_duplicate(from_number: str, user_text_raw: str) -> bool:
    normalized = normalize_text(user_text_raw)
    if not normalized:
        return False

    key = f"{from_number}|{normalized}"
    current = now_ts()

    previous = recent_user_messages.get(key)
    if previous and (current - previous) <= SEMANTIC_DUPLICATE_TTL:
        return True

    recent_user_messages[key] = current
    return False


def handle_text_message(from_number: str, user_text_raw: str):
    user_text = normalize_text(user_text_raw)
    state = get_user_state(from_number)

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

    vehicle_id = extraer_vehicle_id(user_text_raw.strip())
    if vehicle_id:
        responder_precio_por_id(from_number, vehicle_id)
        return

    if state in {"awaiting_brand", "awaiting_brand_or_id", "awaiting_vehicle_id"}:
        marca_detectada = buscar_marca_en_texto(user_text)
        if marca_detectada:
            manejar_marca(from_number, marca_detectada)
            return

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
        list_reply = interactive.get("list_reply", {})
        selected_id = list_reply.get("id", "")

        if selected_id == "ver_vehiculos":
            mostrar_vehiculos(from_number)
            return

        if selected_id == "buscar_marca":
            iniciar_busqueda_marca(from_number)
            return

        if selected_id == "cotizar_importacion":
            responder_cotizacion(from_number)
            return

        if selected_id == "hablar_asesor":
            responder_asesor(from_number)
            return

        if selected_id.startswith("marca_"):
            marca_slug = selected_id.replace("marca_", "").replace("_", " ").strip()
            marcas = obtener_marcas_disponibles()

            for marca in marcas:
                if normalize_text(marca) == normalize_text(marca_slug):
                    manejar_marca(from_number, marca)
                    return

    if interactive_type == "button_reply":
        button_reply = interactive.get("button_reply", {})
        selected_id = button_reply.get("id", "")

        if selected_id.startswith("marca_"):
            marca_slug = selected_id.replace("marca_", "").replace("_", " ").strip()
            marcas = obtener_marcas_disponibles()

            for marca in marcas:
                if normalize_text(marca) == normalize_text(marca_slug):
                    manejar_marca(from_number, marca)
                    return


def process_single_message(message: dict):
    from_number = message.get("from")
    message_id = message.get("id")
    message_type = message.get("type")

    if not from_number:
        return "ok_no_from"

    if message_id:
        if message_id in processed_messages:
            print("[INFO] Mensaje duplicado por ID ignorado:", message_id)
            return "duplicate_ignored"
        processed_messages[message_id] = now_ts()

    if message_type == "text":
        user_text_raw = message.get("text", {}).get("body", "").strip()

        if is_semantic_duplicate(from_number, user_text_raw):
            print("[INFO] Mensaje duplicado semántico ignorado:", from_number, user_text_raw)
            return "semantic_duplicate_ignored"

        handle_text_message(from_number, user_text_raw)
        return "ok_text"

    if message_type == "interactive":
        interactive = message.get("interactive", {})
        handle_interactive_message(from_number, interactive)
        return "ok_interactive"

    print("[INFO] Tipo de mensaje ignorado:", message_type)
    return f"ignored_{message_type}"


@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "inventory_items": len(inventory_cache["data"]),
        "inventory_last_success": inventory_cache["last_success"]
    }), 200


@app.route("/refresh-inventory", methods=["GET"])
def refresh_inventory_route():
    data = refrescar_inventario()
    return jsonify({
        "status": "ok",
        "items": len(data)
    }), 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Token inválido", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    cleanup_processed_messages()
    cleanup_user_sessions()

    data = request.get_json(silent=True) or {}
    results = []

    try:
        entries = data.get("entry", [])

        for entry in entries:
            changes = entry.get("changes", [])

            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])

                if not messages:
                    continue

                for message in messages:
                    result = process_single_message(message)
                    results.append(result)

        if not results:
            return jsonify({"status": "ok_no_messages"}), 200

        return jsonify({
            "status": "ok",
            "results": results
        }), 200

    except Exception as e:
        print("[ERROR] Error procesando webhook:", e)
        return jsonify({"status": "ok_error_handled"}), 200


if __name__ == "__main__":
    try:
        refrescar_inventario()
    except Exception as e:
        print("[ERROR] No se pudo precargar inventario al iniciar:", e)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
