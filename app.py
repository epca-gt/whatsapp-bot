from flask import Flask, request, jsonify
import requests
import os
import json
from datetime import datetime

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

ADMIN_PHONE = "50230306187"

SHEET_URL = "https://opensheet.elk.sh/1opEhxT7aat4GnVAEBcPqze84TSZMO3W-ji2jyHP8HZc/Sheet1"
LEADS_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbwN7uG2ft37ALx9A736YhsBs039czPJCA40YZU1RDIcj5g7viirf3BOVznS1TsgCxoh-w/exec"


def obtener_inventario():
    try:
        response = requests.get(SHEET_URL, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print("Error obteniendo inventario:", e)
        return []


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
            timeout=15
        )

        print("Lead guardado:", response.status_code)
        print("Respuesta Apps Script:", response.text)

    except Exception as e:
        print("Error guardando lead:", e)


def send_whatsapp_message(to_number, message_text):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {
            "body": message_text
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    print("TEXT:", response.status_code, response.text)


def send_whatsapp_list_menu(to_number):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": "Bienvenido a Importadora Los Gemelos 🚗\n\nSelecciona una opción:"
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
        "description": "Toyota, Mazda, Nissan"
    },
    {
        "id": "cotizar_importacion",
        "title": "Cotizar importación",
        "description": "Solicita cotización"
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

    response = requests.post(url, headers=headers, json=payload)
    print("LIST:", response.status_code, response.text)


def notificar_asesor(telefono_cliente: str, mensaje_cliente: str):
    aviso = (
        "🚨 Nuevo cliente quiere hablar con asesor\n\n"
        f"📞 Cliente: {telefono_cliente}\n"
        f"💬 Mensaje: {mensaje_cliente}\n"
        f"🕒 Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_whatsapp_message(ADMIN_PHONE, aviso)


@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200


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
    data = request.get_json()

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return jsonify({"status": "ok"}), 200

        message = value["messages"][0]
        from_number = message["from"]

        # Mensajes de texto
        if message["type"] == "text":
            user_text = message["text"]["body"].strip().lower()

            saludos = [
                "hola", "buenas", "buenos dias", "buenas tardes",
                "buenas noches", "menu", "menú", "inicio", "start"
            ]

            if user_text in saludos:
                guardar_lead(from_number, user_text, "saludo")
                send_whatsapp_list_menu(from_number)
                return jsonify({"status": "ok"}), 200

            reply_text = get_bot_response(user_text, from_number)
            if reply_text:
                send_whatsapp_message(from_number, reply_text)
            return jsonify({"status": "ok"}), 200

        # Respuesta de lista interactiva
        if message["type"] == "interactive":
            interactive = message.get("interactive", {})
            interactive_type = interactive.get("type")

            if interactive_type == "list_reply":
                selected_id = interactive["list_reply"]["id"]

                if selected_id == "ver_vehiculos":
                    reply_text = get_bot_response("1", from_number)
                    if reply_text:
                        send_whatsapp_message(from_number, reply_text)
                    return jsonify({"status": "ok"}), 200

                if selected_id == "buscar_marca":
                    reply_text = get_bot_response("2", from_number)
                    if reply_text:
                        send_whatsapp_message(from_number, reply_text)
                    return jsonify({"status": "ok"}), 200

                if selected_id == "cotizar_importacion":
                    reply_text = get_bot_response("3", from_number)
                    if reply_text:
                        send_whatsapp_message(from_number, reply_text)
                    return jsonify({"status": "ok"}), 200

                if selected_id == "hablar_asesor":
                    reply_text = get_bot_response("4", from_number)
                    if reply_text:
                        send_whatsapp_message(from_number, reply_text)
                    return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("Error procesando mensaje:", e)

    return jsonify({"status": "ok"}), 200


def get_bot_response(user_text: str, from_number: str):
    if user_text == "1":
        guardar_lead(from_number, user_text, "ver_vehiculos")
        carros = obtener_inventario()

        if not carros:
            return "No hay vehículos disponibles en este momento."

        mensaje = "🚗 Vehículos disponibles:\n\n"

        for carro in carros[:5]:
            marca = carro.get("marca", "")
            modelo = carro.get("modelo", "")
            anio = carro.get("anio", "")
            precio = carro.get("precio", "")

            mensaje += f"• {marca} {modelo} {anio}\n💰 {precio}\n\n"

        mensaje += "Escribe la marca que buscas para ver más detalles."
        return mensaje

    if user_text == "2":
        guardar_lead(from_number, user_text, "buscar_marca")
        return (
            "Escribe la marca que buscas.\n\n"
            "Ejemplos:\n"
            "• Toyota\n"
            "• Mazda\n"
            "• Ford\n"
            "• Nissan\n\n"
            "Escribe *menu* para volver al menú principal."
        )

    if user_text == "3":
        guardar_lead(from_number, user_text, "cotizar_importacion")
        return (
            "Para cotizar importación, envíanos:\n\n"
            "• Marca\n"
            "• Modelo\n"
            "• Año aproximado\n"
            "• Presupuesto\n\n"
            "Ejemplo:\n"
            "Toyota Tacoma 2021, presupuesto Q180,000"
        )

    if user_text == "4":
        guardar_lead(from_number, user_text, "quiere_asesor")
        notificar_asesor(from_number, "Cliente solicitó hablar con asesor")
        return (
            "Un asesor te atenderá en breve. 👨‍💼\n\n"
            "Ya notificamos a un asesor para que te contacte."
        )

    carros = obtener_inventario()
    coincidencias = []

    for carro in carros:
        marca = carro.get("marca", "").strip().lower()
        if marca == user_text:
            coincidencias.append(carro)

    if coincidencias:
        guardar_lead(from_number, user_text, "busqueda_marca")
        send_whatsapp_message(from_number, f"Resultados para {user_text.title()}:")

        for carro in coincidencias[:5]:
            marca = carro.get("marca", "")
            modelo = carro.get("modelo", "")
            anio = carro.get("anio", "")
            precio = carro.get("precio", "")
            motor = carro.get("motor", "")
            transmision = carro.get("transmision", "")
            millaje = carro.get("millaje", "")
            link_fotos = carro.get("link_fotos", "")
            descripcion = carro.get("descripcion", "").strip()

            descripcion_formateada = ""
            if descripcion:
                lineas = [line.strip() for line in descripcion.split("\n") if line.strip()]
                if lineas:
                    descripcion_formateada = "\n".join(lineas)

            mensaje = (
                f"🚗 {marca} {modelo} {anio}\n"
                f"💰 Precio: {precio}\n"
                f"⚙️ Motor: {motor}\n"
                f"🔄 Transmisión: {transmision}\n"
                f"📏 Millaje: {millaje}\n"
                f"{'📋 Descripción:\n' + descripcion_formateada + '\n' if descripcion_formateada else ''}"
                f"📸 Ver fotos del vehículo:\n{link_fotos}"
            )

            send_whatsapp_message(from_number, mensaje)

        return "Escribe *menu* para volver al menú principal."

    return "No entendí tu mensaje.\n\nEscribe *menu* para ver las opciones disponibles."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
