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

# Anti-duplicados simple en memoria
processed_message_ids = set()


def obtener_inventario():
    try:
        response = requests.get(SHEET_URL, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print("Error obteniendo inventario:", e)
        return []


def obtener_marcas_disponibles():
    carros = obtener_inventario()
    marcas = []

    for carro in carros:
        marca = carro.get("marca", "").strip()
        if marca and marca.lower() not in [m.lower() for m in marcas]:
            marcas.append(marca)

    return marcas


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


def send_brand_buttons(to_number):
    marcas = obtener_marcas_disponibles()

    if not marcas:
        send_whatsapp_message(
            to_number,
            "No encontré marcas disponibles en este momento."
        )
        return

    # WhatsApp permite máximo 3 reply buttons
    marcas_para_botones = marcas[:3]

    buttons = []
    for marca in marcas_para_botones:
        buttons.append({
            "type": "reply",
            "reply": {
                "id": f"marca_{marca.lower()}",
                "title": marca[:20]
            }
        })

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
            "type": "button",
            "body": {
                "text": "Selecciona una marca disponible:"
            },
            "footer": {
                "text": "Si no ves tu marca, escríbela manualmente"
            },
            "action": {
                "buttons": buttons
            }
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    print("BRAND_BUTTONS:", response.status_code, response.text)

    # Si hay más marcas, mandamos texto adicional
    if len(marcas) > 3:
        restantes = ", ".join(marcas[3:])
        send_whatsapp_message(
            to_number,
            f"También puedes escribir manualmente otras marcas disponibles: {restantes}"
        )


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
        message_id = message.get("id")

        # Evitar duplicados por reintentos de Meta
        if message_id in processed_message_ids:
            print("Mensaje duplicado ignorado:", message_id)
            return jsonify({"status": "duplicate_ignored"}), 200

        if message_id:
            processed_message_ids.add(message_id)

            # Limpieza simple para que no crezca demasiado
            if len(processed_message_ids) > 1000:
                processed_message_ids.clear()

        # Mensajes de texto normales
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

        # Mensajes interactivos
        if message["type"] == "interactive":
            interactive = message.get("interactive", {})
            interactive_type = interactive.get("type")

            # Respuesta de lista principal
            if interactive_type == "list_reply":
                selected_id = interactive["list_reply"]["id"]

                if selected_id == "ver_vehiculos":
                    reply_text = get_bot_response("1", from_number)
                    if reply_text:
                        send_whatsapp_message(from_number, reply_text)
                    return jsonify({"status": "ok"}), 200

                if selected_id == "buscar_marca":
                    guardar_lead(from_number, selected_id, "buscar_marca")
                    send_brand_buttons(from_number)
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

            # Respuesta de botones de marcas
            if interactive_type == "button_reply":
                selected_id = interactive["button_reply"]["id"]

                if selected_id.startswith("marca_"):
                    marca = selected_id.replace("marca_", "").strip().lower()
                    reply_text = get_bot_response(marca, from_number)
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
        send_brand_buttons(from_number)
        return None

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
        return (
            "Perfecto 👍\n\n"
            "Habla directamente con nuestro asesor:\n\n"
            "👨‍💼 Paolo\n"
            "https://wa.me/50230306187?text=Hola%20vengo%20del%20bot%20de%20Importadora%20Los%20Gemelos"
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
