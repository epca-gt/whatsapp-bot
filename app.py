from flask import Flask, request, jsonify
import requests
import os
import json
import re
import time
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

inventory_cache = {
    "data": [],
    "timestamp": 0,
    "last_success": 0
}

processed_messages = {}
user_sessions = {}


def now_ts():
    return time.time()


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def cleanup_processed_messages():
    current = now_ts()
    expired = [
        msg_id for msg_id, ts in processed_messages.items()
        if current - ts > PROCESSED_MESSAGE_TTL
    ]
    for msg_id in expired:
        processed_messages.pop(msg_id, None)


def cleanup_user_sessions():
    current = now_ts()
    expired = [
        phone for phone, session in user_sessions.items()
        if current - session.get("updated_at", 0) > USER_SESSION_TTL
    ]
    for phone in expired:
        user_sessions.pop(phone, None)


def set_user_state(phone: str, state: str):
    user_sessions[phone] = {
        "state": state,
        "updated_at": now_ts()
    }


def get_user_state(phone: str) -> str:
    cleanup_user_sessions()
    session = user_sessions.get(phone, {})
    return session.get("state", "")


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
            print(f"Inventario actualizado. Registros: {len(data)}")
            return data

        print("Respuesta de inventario inválida.")
        return inventory_cache["data"]

    except Exception as e:
        print("Error refrescando inventario:", e)
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

    return sorted(marcas_map.values(), key=lambda x: x.lower())


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
        if carro_id == vehicle_id:
            return carro

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

        print("Lead guardado:", response.status_code)
        print("Respuesta Apps Script:", response.text)

    except Exception as e:
        print("Error guardando lead:", e)


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
        print("WA:", response.status_code, response.text)
        return response
    except Exception as e:
        print("Error enviando mensaje WhatsApp:", e)
        return None


def send_whatsapp_message(to_number: str, message_text: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {
            "body": message_text
        }
    }
    return send_whatsapp_payload(payload)


def send_whatsapp_list_menu(to_number: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": "Bienvenido a Importadora Los Gemelos y El Fer 🚗\n\nSelecciona una opción:"
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
            f"También puedes escribir manualmente estas marcas: {restantes}"
        )


def send_vehicle_messages(to_number: str, carros: list, marca_mostrada: str):
    send_whatsapp_message(
        to_number,
        f"Resultados para {marca_mostrada}:\n\n"
        f"Te mostramos vehículos disponibles sin publicar el precio.\n"
        f"Escribe el *ID* del vehículo que te interesa para consultar precio o disponibilidad."
    )

    for carro in carros:
        carro_id = str(carro.get("id", "")).strip()
        marca = (carro.get("marca") or "").strip()
        modelo = (carro.get("modelo") or "").strip()
        anio = (carro.get("anio") or "").strip()
        motor = (carro.get("motor") or "").strip()
        transmision = (carro.get("transmision") or "").strip()
        millaje = (carro.get("millaje") or "").strip()
        link_fotos = (carro.get("link_fotos") or "").strip()
        descripcion = (carro.get("descripcion") or "").strip()

        descripcion_formateada = ""
        if descripcion:
            lineas = [line.strip() for line in descripcion.split("\n") if line.strip()]
            if lineas:
                descripcion_formateada = "\n".join(lineas)

        partes = [
            f"🚗 {marca} {modelo} {anio}".strip(),
            f"🆔 ID: {carro_id}" if carro_id else "",
            f"⚙️ Motor: {motor}" if motor else "",
            f"🔄 Transmisión: {transmision}" if transmision else "",
            f"📏 Millaje: {millaje}" if millaje else "",
            "💰 Precio: consultar por este medio",
            f"📋 Descripción:\n{descripcion_formateada}" if descripcion_formateada else "",
            f"📸 Ver fotos del vehículo:\n{link_fotos}" if link_fotos else ""
        ]

        mensaje = "\n".join([p for p in partes if p])
        send_whatsapp_message(to_number, mensaje)

    send_whatsapp_message(
        to_number,
        "Escribe el *ID* del vehículo para consultar precio, o escribe *asesor* para hablar con un vendedor."
    )


def build_advisor_link():
    text = quote("Hola vengo del bot de Importadora Los Gemelos")
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

    mensaje += "Escribe la marca que buscas para ver más detalles, o escribe el *ID* para consultar un vehículo específico."
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

    guardar_lead(from_number, f"id:{vehicle_id}", "consulta_precio_por_id")

    mensaje = (
        f"💰 Precio del vehículo solicitado:\n\n"
        f"🚗 {marca} {modelo} {anio}\n"
        f"🆔 ID: {vehicle_id}\n"
        f"💵 Precio: {precio if precio else 'No disponible en este momento'}\n\n"
        f"Escribe *asesor* si deseas continuar con este vehículo."
    )

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
    set_user_state(from_number, "awaiting_vehicle_id")
    send_vehicle_messages(from_number, coincidencias, marca_detectada)


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

    if user_text in {"1", "ver vehiculos", "ver vehículos"}:
        mostrar_vehiculos(from_number)
        return

    if user_text in {"2", "buscar marca"}:
        iniciar_busqueda_marca(from_number)
        return

    if user_text in {"3", "cotizar importacion", "cotizar importación"}:
        responder_cotizacion(from_number)
        return

    if user_text in {"4", "asesor", "hablar con asesor"}:
        responder_asesor(from_number)
        return

    # Intentar interpretar el mensaje como ID de vehículo
    carro = buscar_carro_por_id(user_text_raw.strip())
    if carro:
        responder_precio_por_id(from_number, user_text_raw.strip())
    return

    if state in {"awaiting_brand", "awaiting_brand_or_id"}:
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
        "No entendí tu mensaje.\n\nEscribe *menu* para ver las opciones disponibles."
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


@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200


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

    try:
        entry = (data.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value", {})

        if "messages" not in value:
            return jsonify({"status": "ok_no_messages"}), 200

        message = value["messages"][0]
        from_number = message.get("from")
        message_id = message.get("id")
        message_type = message.get("type")

        if not from_number:
            return jsonify({"status": "ok_no_from"}), 200

        if message_id:
            if message_id in processed_messages:
                print("Mensaje duplicado ignorado:", message_id)
                return jsonify({"status": "duplicate_ignored"}), 200
            processed_messages[message_id] = now_ts()

        if message_type == "text":
            user_text_raw = message.get("text", {}).get("body", "").strip()
            handle_text_message(from_number, user_text_raw)
            return jsonify({"status": "ok_text"}), 200

        if message_type == "interactive":
            interactive = message.get("interactive", {})
            handle_interactive_message(from_number, interactive)
            return jsonify({"status": "ok_interactive"}), 200

        return jsonify({"status": f"ignored_{message_type}"}), 200

    except Exception as e:
        print("Error procesando mensaje:", e)
        return jsonify({"status": "ok_error_handled"}), 200


if __name__ == "__main__":
    try:
        refrescar_inventario()
    except Exception as e:
        print("No se pudo precargar inventario al iniciar:", e)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
