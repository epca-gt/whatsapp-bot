from flask import Flask, request, jsonify
import requests
import os
import requests

SHEET_URL = "https://opensheet.elk.sh/1opEhxT7aat4GnVAEBcPqze84TSZMO3W-ji2jyHP8HZc/Sheet1"


def obtener_inventario():
    response = requests.get(SHEET_URL)
    return response.json()

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")


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

        if "messages" in value:
            message = value["messages"][0]
            from_number = message["from"]

            if message["type"] == "text":
                user_text = message["text"]["body"].strip().lower()
                reply_text = get_bot_response(user_text)
                send_whatsapp_message(from_number, reply_text)

    except Exception as e:
        print("Error procesando mensaje:", e)

    return jsonify({"status": "ok"}), 200


def get_bot_response(user_text: str) -> str:
    saludos = ["hola", "buenas", "buenos dias", "buenas tardes", "buenas noches", "menu", "menú", "inicio", "start"]

    if user_text in saludos:
        return (
            "Bienvenido a Importadora Los Gemelos 🚗\n\n"
            "Escribe una opción:\n"
            "1️⃣ Ver vehículos disponibles\n"
            "2️⃣ Buscar por marca\n"
            "3️⃣ Cotizar importación\n"
            "4️⃣ Hablar con asesor"
        )

    if user_text == "1":
    carros = obtener_inventario()

    if not carros:
        return "No hay vehículos disponibles en este momento."

    mensaje = "🚗 Vehículos disponibles:\n\n"

    for carro in carros[:5]:
        mensaje += (
            f"• {carro['marca']} {carro['modelo']} {carro['anio']}\n"
            f"💰 {carro['precio']}\n\n"
        )

    mensaje += "Escribe la marca para ver más detalles."

    return mensaje

    for carro in obtener_inventario():
    if user_text in carro["marca"].lower():
        return (
            f"🚗 {carro['marca']} {carro['modelo']} {carro['anio']}\n\n"
            f"💰 Precio: {carro['precio']}\n"
            f"⚙️ Motor: {carro['motor']}\n"
            f"🔄 Transmisión: {carro['transmision']}\n"
            f"📏 Millaje: {carro['millaje']}\n\n"
            f"📸 Fotos:\n{carro['link_fotos']}"
        )

    if user_text == "2":
        return (
            "Escribe la marca que buscas.\n\n"
            "Ejemplos:\n"
            "• Toyota\n"
            "• Mazda\n"
            "• Ford\n\n"
            "Escribe *menu* para volver al menú principal."
        )

    if user_text in ["toyota", "mazda", "ford", "honda", "nissan", "chevrolet"]:
        return (
            f"Resultados para la marca *{user_text.title()}*:\n\n"
            f"Por ahora esta búsqueda está en modo demo.\n"
            f"En el siguiente paso la conectaremos al inventario real.\n\n"
            f"Escribe *menu* para volver al menú principal."
        )

    if user_text == "3":
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
        return (
            "Un asesor te atenderá en breve. 👨‍💼\n\n"
            "Mientras tanto, puedes escribir:\n"
            "• 1 para ver vehículos disponibles\n"
            "• 2 para buscar por marca\n"
            "• 3 para cotizar importación"
        )

    return (
        "No entendí tu mensaje.\n\n"
        "Escribe *menu* para ver las opciones disponibles."
    )


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
    print(response.status_code, response.text)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
