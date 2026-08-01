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

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY NO configurada. El agente no podrá responder.")

# ─── Constantes ───────────────────────────────────────────────────────────────
REQUEST_TIMEOUT        = 15
INVENTORY_CACHE_TTL    = 300
PROCESSED_MESSAGE_TTL  = 600
USER_SESSION_TTL       = 3600
SEMANTIC_DUPLICATE_TTL = 20

RATE_LIMIT_MAX         = 10    # mensajes permitidos...
RATE_LIMIT_WINDOW      = 60    # ...por esta ventana en segundos
RATE_LIMIT_AVISO_TTL   = 300   # no repetir el aviso antes de 5 min

WHATSAPP_MAX_LEN       = 3900  # margen bajo el límite real de 4096
WHATSAPP_CAPTION_MAX   = 1000  # límite de caption en imágenes (real: 1024)

MAX_RESULTADOS_LISTA   = 25    # con inventario chico no hay razón para cortar antes
MAX_ITEMS_RESUMEN      = 6
MAX_FOTOS_POR_RESPUESTA = 3
FOTOS_SI_COINCIDENCIAS_MENOR_A = 4

FILTRAR_POR_ESTADO     = True
ESTADOS_NO_DISPONIBLES = ("vendido", "apartado", "reservado", "entregado", "no disponible")

# ─── Ubicación del negocio ─────────────────────────────────────────────────────
NEGOCIO_NOMBRE     = "Importadora Los Gemelos y Fer"
NEGOCIO_DIRECCION  = "35 Avenida 16-33 Zona 7, Villa Linda 2, Ciudad de Guatemala"
NEGOCIO_LAT        = 14.6432439
NEGOCIO_LNG        = -90.5547868
NEGOCIO_MAPS_URL   = f"https://www.google.com/maps/search/?api=1&query={NEGOCIO_LAT},{NEGOCIO_LNG}"
NEGOCIO_WAZE_URL   = f"https://waze.com/ul?ll={NEGOCIO_LAT}%2C{NEGOCIO_LNG}&navigate=yes"

# ─── Visa Cuotas ──────────────────────────────────────────────────────────────
# Recargo sobre el monto que se pasa por Visa Cuotas (no sobre el pago al contado).
VISA_CUOTAS_RECARGO = {
    3:  0.10,
    6:  0.10,
    9:  0.11,
    12: 0.12,
    18: 0.18,
    24: 0.24,
    36: 0.27,
    48: 0.36,
}
VISA_CUOTAS_PLANES = sorted(VISA_CUOTAS_RECARGO.keys())
VISA_PLANES_SUGERIDOS = [12, 24, 36, 48]  # los que se muestran si no piden plazo

# ─── Estado en memoria ────────────────────────────────────────────────────────
_inventory_lock = threading.Lock()
inventory_cache = {"data": [], "timestamp": 0, "last_success": 0}

_state_lock          = threading.Lock()
processed_messages   = {}
known_users          = set()
recent_user_messages = {}
user_chat_histories  = {}
user_rate_limits     = {}
rate_limit_avisados  = {}

stats = {"consultas_hoy": 0, "vehiculos_vistos": {}, "asesores_hoy": [], "fecha": None}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def now_ts():
    return time.time()

def hoy_str():
    return datetime.now(GUATEMALA_TZ).strftime("%Y-%m-%d")

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
    return re.sub(r"\s+", " ", text)

def normalize_match(text: str) -> str:
    """Para comparar marca/modelo: quita guiones, espacios y todo lo no alfanumérico.
    Así 'hrv' == 'HR-V', 'cr v' == 'CR-V', 'f150' == 'F-150'."""
    return re.sub(r"[^a-z0-9]", "", normalize_text(text))

def parse_price_value(price_text):
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

def formato_quetzales(valor: float) -> str:
    return f"Q{valor:,.2f}"

def _normalize_row(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        key = normalize_text(str(k)).replace(" ", "_")
        out[key] = v.strip() if isinstance(v, str) else v

    alias = {
        "precio":          ["precio_q", "precio_quetzales", "valor", "precio_venta"],
        "anio":            ["ano", "year", "modelo_anio"],
        "link_fotos":      ["fotos", "link", "enlace", "url_fotos", "galeria"],
        "foto_principal":  ["foto", "imagen", "foto_url", "img", "imagen_principal"],
        "marca":           ["brand"],
        "modelo":          ["model", "linea"],
        "id":              ["codigo", "no", "num", "id_vehiculo"],
        "millaje":         ["kilometraje", "km", "millas", "mileage"],
        "transmision":     ["caja", "transmission"],
        "combustible":     ["gasolina", "fuel", "tipo_combustible"],
        "descripcion":     ["detalles", "equipamiento", "extras", "notas"],
    }
    for destino, posibles in alias.items():
        if not out.get(destino):
            for p in posibles:
                if out.get(p):
                    out[destino] = out[p]
                    break
    return out

def _limpiar_vinieta(linea: str) -> str:
    return re.sub(
        r"^[\s\-•*\u2000-\u3300\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]+", "", linea
    ).strip()

def resumir_descripcion(desc, max_items: int = MAX_ITEMS_RESUMEN) -> str:
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
    if not desc:
        return []
    lineas = [_limpiar_vinieta(l) for l in str(desc).split("\n")]
    return [l for l in lineas if l]

def construir_caption_foto(carro: dict) -> str:
    """
    Arma el caption de WhatsApp con los datos reales del Sheet.
    La foto (marco/branding) se sube UNA sola vez por carro; el texto se
    actualiza solo en cada consulta, así que un cambio de precio no
    requiere tocar la imagen. Esta info NO debe repetirse en el mensaje
    de texto que acompaña al detalle.
    """
    partes = [
        f"*{carro.get('marca','')} {carro.get('modelo','')}* ({carro.get('anio','')})",
        f"💰 {carro.get('precio','')}"
    ]
    if carro.get("millaje"):
        partes.append(f"🛣️ {carro.get('millaje')} millas")
    motor_trans = f"{carro.get('motor','')} {carro.get('transmision','')}".strip()
    if motor_trans:
        partes.append(f"⚙️ {motor_trans}")
    if carro.get("color"):
        partes.append(f"🎨 {carro.get('color')}")
    return "\n".join(partes)[:WHATSAPP_CAPTION_MAX]

def esta_disponible(carro: dict) -> bool:
    if not FILTRAR_POR_ESTADO:
        return True
    estado = normalize_text(str(carro.get("estado") or ""))
    if not estado:
        return True
    return estado not in ESTADOS_NO_DISPONIBLES

def limpiar_markdown_whatsapp(texto: str) -> str:
    if not texto:
        return texto
    texto = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1: \2", texto)
    texto = re.sub(r"\*\*([^\*]+)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", texto, flags=re.MULTILINE)
    return texto.strip()

def url_imagen_directa(url: str) -> str:
    """
    Convierte links de Google Drive al CDN de imágenes (lh3.googleusercontent.com),
    que entrega el archivo crudo. WhatsApp necesita una URL que devuelva la imagen
    directa: un link de carpeta o de vista previa NO funciona.
    El archivo debe estar en 'Cualquier persona con el enlace' y ser JPG o PNG.
    """
    if not url:
        return ""
    url = str(url).strip()
    if not url.startswith("http"):
        return ""

    # https://drive.google.com/file/d/FILE_ID/view
    m = re.search(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)", url)
    if m:
        return f"https://lh3.googleusercontent.com/d/{m.group(1)}"

    # https://drive.google.com/open?id=FILE_ID  |  /uc?export=download&id=FILE_ID
    m = re.search(r"drive\.google\.com/(?:open|uc)\?(?:[^&]*&)*id=([A-Za-z0-9_-]+)", url)
    if m:
        return f"https://lh3.googleusercontent.com/d/{m.group(1)}"

    # Los links de carpeta no sirven como imagen
    if "drive.google.com/drive/folders" in url:
        return ""

    return url

# ─── Rate limiting ────────────────────────────────────────────────────────────
def rate_limit_excedido(phone: str) -> bool:
    ahora = now_ts()
    with _state_lock:
        marcas = [t for t in user_rate_limits.get(phone, []) if ahora - t < RATE_LIMIT_WINDOW]
        if len(marcas) >= RATE_LIMIT_MAX:
            user_rate_limits[phone] = marcas
            return True
        marcas.append(ahora)
        user_rate_limits[phone] = marcas
        return False

def debe_avisar_rate_limit(phone: str) -> bool:
    ahora = now_ts()
    with _state_lock:
        if ahora - rate_limit_avisados.get(phone, 0) < RATE_LIMIT_AVISO_TTL:
            return False
        rate_limit_avisados[phone] = ahora
        return True

# ─── Memoria Conversacional ───────────────────────────────────────────────────
def append_to_history(phone: str, role: str, content: str):
    with _state_lock:
        if phone not in user_chat_histories:
            user_chat_histories[phone] = {"messages": [], "updated_at": now_ts()}
        user_chat_histories[phone]["messages"].append({"role": role, "content": content})
        user_chat_histories[phone]["updated_at"] = now_ts()
        if len(user_chat_histories[phone]["messages"]) > 10:
            user_chat_histories[phone]["messages"] = user_chat_histories[phone]["messages"][-10:]

def get_history(phone: str):
    with _state_lock:
        return list(user_chat_histories.get(phone, {}).get("messages", []))

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
    objetivo = normalize_text(str(vehicle_id)).rstrip(".")
    for carro in obtener_inventario():
        if normalize_text(str(carro.get("id", ""))) == objetivo:
            return carro
    return None

def buscar_carro_por_texto(texto: str):
    """
    Búsqueda flexible e insensible a guiones/espacios ('hrv' encuentra 'HR-V').
    Primero subcadena compactada; si falla, gana el carro con MÁS palabras
    coincidentes (no exige que todas coincidan, que era la causa de que
    'no reconociera' carros con nombres parciales).
    """
    carros = obtener_inventario()
    consulta = normalize_text(texto)
    consulta_compacta = normalize_match(texto)
    if not consulta_compacta:
        return None

    for carro in carros:
        etiqueta_compacta = normalize_match(
            f"{carro.get('marca','')} {carro.get('modelo','')} {carro.get('anio','')}"
        )
        if not etiqueta_compacta:
            continue
        if consulta_compacta in etiqueta_compacta or etiqueta_compacta in consulta_compacta:
            return carro

    palabras = [normalize_match(p) for p in consulta.split() if len(p) > 2]
    palabras = [p for p in palabras if p]
    if not palabras:
        return None

    mejor_match, mejor_score = None, 0
    for carro in carros:
        etiqueta = normalize_match(f"{carro.get('marca','')} {carro.get('modelo','')}")
        if not etiqueta:
            continue
        score = sum(1 for p in palabras if p in etiqueta)
        if score > mejor_score:
            mejor_score, mejor_match = score, carro

    if mejor_match and mejor_score >= 1:
        return mejor_match
    return None

# ─── Loops en background ──────────────────────────────────────────────────────
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
                    (rate_limit_avisados,  RATE_LIMIT_AVISO_TTL),
                ]:
                    for k in [k for k, ts in d.items() if current - ts > ttl]:
                        d.pop(k, None)

                for p in [p for p, v in user_rate_limits.items()
                          if not v or current - max(v) > RATE_LIMIT_WINDOW * 2]:
                    user_rate_limits.pop(p, None)

                for p in [p for p, data in user_chat_histories.items()
                          if current - data.get("updated_at", 0) > USER_SESSION_TTL]:
                    user_chat_histories.pop(p, None)

            logger.info("Cleanup ejecutado. Sesiones AI activas: %d", len(user_chat_histories))
        except Exception as e:
            logger.error("Error en cleanup: %s", e)

threading.Thread(target=_inventory_refresh_loop, daemon=True).start()
threading.Thread(target=_cleanup_loop, daemon=True).start()

# ─── Tools ────────────────────────────────────────────────────────────────────
INVENTORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "consultar_inventario",
            "description": (
                "Lista vehículos disponibles según filtros (marca, modelo, rango de precio, año). "
                "Devuelve datos resumidos. Para el equipamiento completo de un auto puntual "
                "usar 'detalle_vehiculo'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "marca": {"type": "string", "description": "Marca del auto, ej: Nissan, Toyota"},
                    "modelo": {"type": "string", "description": "Modelo del auto, ej: 350z, Corvette"},
                    "precio_max": {
                        "type": "number",
                        "description": "Precio MÁXIMO en Quetzales. Para 'hasta', 'menos de', 'máximo', 'presupuesto de'"
                    },
                    "precio_min": {
                        "type": "number",
                        "description": "Precio MÍNIMO en Quetzales. Para 'más de', 'arriba de', 'mayor a', 'desde'"
                    },
                    "anio": {"type": "integer", "description": "Año específico del vehículo"}
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
                "Ficha COMPLETA de un vehículo: motor, transmisión, millaje, combustible, color, "
                "estado y todo el equipamiento. Usar cuando pregunten qué trae un auto específico."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "ID exacto del vehículo, ej: 1"},
                    "descripcion_vehiculo": {
                        "type": "string",
                        "description": "Si no se conoce el ID: marca y modelo, ej: 'Nissan 350z'"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calcular_visa_cuotas",
            "description": (
                "Calcula el pago mensual con Visa Cuotas. Usar SIEMPRE que pregunten por cuotas, "
                "mensualidades, financiamiento o pagos mixtos (parte al contado y parte con tarjeta). "
                "Nunca calcules cuotas ni recargos de cabeza: esta herramienta ya aplica el recargo "
                "correcto solo sobre la porción que va a la tarjeta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id_vehiculo": {
                        "type": "string",
                        "description": "ID del vehículo del inventario. Preferí esto sobre 'monto'."
                    },
                    "monto": {
                        "type": "number",
                        "description": "Precio total en Quetzales, solo si el cliente da una cifra propia sin referir a un auto del inventario"
                    },
                    "cuotas": {
                        "type": "integer",
                        "description": f"Plazo en meses. Planes válidos: {VISA_CUOTAS_PLANES}. Si el cliente no dice plazo, omitir este campo."
                    },
                    "pago_contado": {
                        "type": "number",
                        "description": (
                            "Monto en Quetzales que el cliente paga en efectivo, prima o enganche. "
                            "Este monto NO lleva recargo. Ej: si dice 'doy Q20,000 de prima y el resto a cuotas', acá va 20000."
                        )
                    },
                    "monto_a_tarjeta": {
                        "type": "number",
                        "description": (
                            "Monto exacto que el cliente quiere pasar por la tarjeta, si lo especifica. "
                            "Ej: 'quiero pasar solo Q30,000 por Visa'. Si se omite, se financia todo el resto tras el pago al contado."
                        )
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "enviar_ubicacion",
            "description": (
                "Envía la ubicación del negocio (mapa nativo de WhatsApp + links de Waze y Google "
                "Maps). Usar SIEMPRE que el cliente pregunte dónde están ubicados, cómo llegar, "
                "la dirección, o pida el Waze/Maps del local."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "contactar_asesor",
            "description": (
                "Conecta al cliente con un asesor humano. Usar cuando el cliente pida hablar con "
                "una persona, un vendedor, un humano, quiera negociar precio, o el bot no pueda "
                "resolver su consulta. Incluí un resumen breve de lo que el cliente busca para "
                "que el asesor tenga contexto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resumen": {
                        "type": "string",
                        "description": (
                            "Resumen en una frase de qué busca el cliente, con el vehículo e ID si "
                            "aplica. Ej: 'Interesado en Nissan 350z (ID 1), preguntó por 24 cuotas "
                            "con Q20,000 de prima'. Si no hay contexto, dejar vacío."
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
    resultados, fotos = [], []

    for c in carros:
        marca_auto = str(c.get("marca") or "").strip()
        if not marca_auto or not esta_disponible(c):
            continue
        if marca and normalize_match(marca) not in normalize_match(marca_auto):
            continue
        if modelo and normalize_match(modelo) not in normalize_match(str(c.get("modelo") or "")):
            continue
        if anio and str(c.get("anio") or "").strip() != str(anio):
            continue
        if precio_max or precio_min:
            val = parse_price_value(c.get("precio"))
            if val is None:
                continue
            if precio_max and val > precio_max:
                continue
            if precio_min and val < precio_min:
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
        fotos.append({
            "url": url_imagen_directa(c.get("foto_principal")),
            "caption": construir_caption_foto(c)
        })

    logger.info("TOOL lista -> total=%d filtrado=%d (marca=%s modelo=%s min=%s max=%s anio=%s)",
                len(carros), len(resultados), marca, modelo, precio_min, precio_max, anio)

    fotos_validas = []
    if 0 < len(resultados) < FOTOS_SI_COINCIDENCIAS_MENOR_A:
        fotos_validas = [f for f in fotos if f["url"]][:MAX_FOTOS_POR_RESPUESTA]

    return {
        "total_inventario": len(carros),
        "coincidencias": len(resultados),
        "nota": "Extras resumidos. Para la ficha completa usá detalle_vehiculo con el id.",
        "vehiculos": resultados[:MAX_RESULTADOS_LISTA]
    }, fotos_validas

def ejecutar_tool_detalle(id=None, descripcion_vehiculo=None):
    carro = None
    if id:
        carro = buscar_carro_por_id(id)
    if not carro and descripcion_vehiculo:
        carro = buscar_carro_por_texto(descripcion_vehiculo)

    if not carro:
        logger.info("TOOL detalle -> NO encontrado (id=%s texto=%s)", id, descripcion_vehiculo)
        return {"encontrado": False, "mensaje": "No se encontró ese vehículo en el inventario."}, []

    disponible = esta_disponible(carro)
    logger.info("TOOL detalle -> id=%s %s %s disponible=%s",
                carro.get("id"), carro.get("marca"), carro.get("modelo"), disponible)

    foto_url = url_imagen_directa(carro.get("foto_principal"))
    fotos = []
    if foto_url:
        fotos.append({
            "url": foto_url,
            "caption": construir_caption_foto(carro)
        })

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
        "link_fotos":  carro.get("link_fotos") or "",
        "nota": (
            "IMPORTANTE: la foto que se envía junto con esta respuesta ya muestra precio, motor, "
            "transmisión, millaje y color en su caption. NO repitas esos datos en tu texto: enfocate "
            "solo en el equipamiento (lista de abajo) y en invitar a visitarnos, sin necesidad "
            "de agendar cita previa."
        )
    }, fotos

def ejecutar_tool_visa_cuotas(id_vehiculo=None, monto=None, cuotas=None,
                              pago_contado=None, monto_a_tarjeta=None):
    referencia, base = None, None

    if id_vehiculo:
        carro = buscar_carro_por_id(id_vehiculo)
        if not carro:
            return {"error": f"No existe el vehículo con id {id_vehiculo}."}, []
        base = parse_price_value(carro.get("precio"))
        referencia = f"{carro.get('marca','')} {carro.get('modelo','')} ({carro.get('anio','')})"
        if base is None:
            return {"error": "Ese vehículo no tiene precio registrado."}, []

    if base is None and monto:
        base = float(monto)

    if base is None:
        return {"error": "Falta el vehículo o el monto para calcular."}, []

    contado = max(float(pago_contado or 0), 0)
    if contado > base:
        return {"error": "El pago al contado no puede ser mayor que el precio."}, []

    # Cuánto se pasa efectivamente por la tarjeta
    if monto_a_tarjeta:
        tarjeta = float(monto_a_tarjeta)
        if tarjeta <= 0:
            return {"error": "El monto a tarjeta debe ser mayor a cero."}, []
        if contado + tarjeta > base + 0.01:
            return {"error": "El contado más el monto a tarjeta superan el precio del vehículo."}, []
        # Lo que no va a la tarjeta se cubre al contado
        contado = base - tarjeta
    else:
        tarjeta = base - contado

    if tarjeta <= 0:
        return {
            "vehiculo": referencia,
            "precio": formato_quetzales(base),
            "mensaje": "Con ese pago al contado se cubre el vehículo completo. No se necesita financiamiento."
        }, []

    def calcular(n):
        recargo_pct = VISA_CUOTAS_RECARGO.get(n)
        if recargo_pct is None:
            return None
        total_tarjeta = tarjeta * (1 + recargo_pct)
        return {
            "cuotas": n,
            "recargo_pct": f"{int(recargo_pct * 100)}%",
            "recargo_monto": formato_quetzales(total_tarjeta - tarjeta),
            "total_en_tarjeta": formato_quetzales(total_tarjeta),
            "pago_mensual": formato_quetzales(total_tarjeta / n),
            "total_a_pagar": formato_quetzales(contado + total_tarjeta)
        }

    resultado = {
        "vehiculo": referencia,
        "precio_vehiculo": formato_quetzales(base),
        "pago_contado": formato_quetzales(contado),
        "monto_por_tarjeta": formato_quetzales(tarjeta),
        "nota_recargo": "El recargo aplica ÚNICAMENTE sobre el monto que pasa por la tarjeta.",
        "aviso": "Montos referenciales. La aprobación y el cupo dependen del banco emisor."
    }

    if cuotas:
        plan = calcular(int(cuotas))
        if not plan:
            resultado["error_plan"] = (
                f"No manejamos plan a {cuotas} cuotas. Planes disponibles: {VISA_CUOTAS_PLANES}"
            )
            resultado["planes"] = [calcular(n) for n in VISA_PLANES_SUGERIDOS]
        else:
            resultado["plan_solicitado"] = plan
    else:
        resultado["planes"] = [calcular(n) for n in VISA_PLANES_SUGERIDOS]
        resultado["otros_plazos_disponibles"] = [n for n in VISA_CUOTAS_PLANES
                                                 if n not in VISA_PLANES_SUGERIDOS]

    logger.info("TOOL visa_cuotas -> base=%s contado=%s tarjeta=%s cuotas=%s",
                base, contado, tarjeta, cuotas)
    return resultado, []

def formatear_bloque_cuotas(resultado: dict) -> str:
    """
    Arma el texto EXACTO de la respuesta de cuotas a partir del cálculo en Python.
    No se deja que el modelo redacte los números: un LLM puede transcribir mal una
    cifra o mezclar el recargo de un plazo con el pago de otro. Esta función
    garantiza que lo que ve el cliente es exactamente lo que calculó la fórmula.
    """
    if "error" in resultado:
        return resultado["error"]
    if "mensaje" in resultado and "planes" not in resultado and "plan_solicitado" not in resultado:
        # Caso: el contado ya cubre todo el vehículo.
        partes = []
        if resultado.get("vehiculo"):
            partes.append(f"*{resultado['vehiculo']}*")
        partes.append(resultado["mensaje"])
        return "\n".join(partes)

    lineas = []
    if resultado.get("vehiculo"):
        lineas.append(f"*{resultado['vehiculo']}*")
    lineas.append(f"💰 Precio: {resultado.get('precio_vehiculo', '')}")
    if resultado.get("pago_contado") and resultado["pago_contado"] not in ("Q0.00", None):
        lineas.append(f"💵 Pago al contado: {resultado['pago_contado']}")
    lineas.append(f"💳 Monto financiado por tarjeta: {resultado.get('monto_por_tarjeta', '')}")
    lineas.append("")

    def linea_plan(p):
        return (f"*{p['cuotas']} cuotas*: {p['pago_mensual']}/mes "
                f"(recargo {p['recargo_pct']}, total a pagar {p['total_a_pagar']})")

    if "plan_solicitado" in resultado:
        lineas.append(linea_plan(resultado["plan_solicitado"]))
    elif "planes" in resultado:
        for p in resultado["planes"][:4]:
            lineas.append(linea_plan(p))
        otros = resultado.get("otros_plazos_disponibles")
        if otros:
            lineas.append(f"\nTambién manejamos plazos de {', '.join(str(o) for o in otros)} meses.")

    if resultado.get("error_plan"):
        lineas.append(f"\n{resultado['error_plan']}")

    lineas.append(
        f"\n_{resultado.get('aviso', 'Montos referenciales, sujetos a aprobación del banco.')}_"
    )
    return "\n".join(lineas)

def ejecutar_tool_ubicacion():
    logger.info("TOOL ubicacion -> enviada")
    return {
        "nombre": NEGOCIO_NOMBRE,
        "direccion": NEGOCIO_DIRECCION,
        "google_maps": NEGOCIO_MAPS_URL,
        "waze": NEGOCIO_WAZE_URL,
        "nota": (
            "El pin de ubicación ya se envió como mensaje nativo de WhatsApp. "
            "Solo mencioná brevemente la dirección y horarios; no repitas los links, "
            "ya se muestran en el mapa. Recordá que no se necesita cita previa para visitar."
        )
    }, []

def construir_link_asesor(cliente: str, resumen: str) -> str:
    """Link wa.me al asesor con el contexto precargado en el texto."""
    texto = "Hola, vengo del bot de Los Gemelos y Fer."
    if resumen:
        texto += f" {resumen}"
    return f"https://wa.me/{ADMIN_PHONE}?text={quote(texto)}"

def ejecutar_tool_asesor(cliente: str, resumen: str = ""):
    resumen = (resumen or "").strip()
    link = construir_link_asesor(cliente, resumen)

    # Alerta al negocio: queda en el Sheet de leads (y en logs) con el contexto,
    # para que puedan escribirle al cliente proactivamente.
    detalle_lead = resumen if resumen else "Cliente pidió hablar con un asesor"
    guardar_lead(cliente, detalle_lead, "solicita_asesor")
    logger.info("TOOL asesor -> cliente=%s resumen=%s", cliente, resumen or "(sin contexto)")

    return {
        "link_asesor": link,
        "nota": (
            "Compartí este link con el cliente para que escriba directo al asesor; el mensaje "
            "ya lleva su contexto precargado, así no tiene que repetir nada. Avisale también "
            "que ya notificamos al equipo y que si prefiere, un asesor puede contactarlo a él. "
            "El link va pelado, sin formato Markdown."
        )
    }, []

def despachar_tool(nombre: str, args: dict, cliente: str = ""):
    """Devuelve (resultado_json, fotos_a_enviar)."""
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
    if nombre == "calcular_visa_cuotas":
        return ejecutar_tool_visa_cuotas(
            id_vehiculo=args.get("id_vehiculo"),
            monto=args.get("monto"),
            cuotas=args.get("cuotas"),
            pago_contado=args.get("pago_contado"),
            monto_a_tarjeta=args.get("monto_a_tarjeta")
        )
    if nombre == "enviar_ubicacion":
        return ejecutar_tool_ubicacion()
    if nombre == "contactar_asesor":
        return ejecutar_tool_asesor(cliente, args.get("resumen", ""))
    return {"error": f"Herramienta desconocida: {nombre}"}, []

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT_TEXT = (
    "Eres el asesor virtual inteligente de Importadora Los Gemelos y Fer en Guatemala. "
    "Tu objetivo es ayudar a los clientes a encontrar vehículos y resolver dudas.\n\n"

    "REGLAS OBLIGATORIAS:\n"

    "1. Sé amable, persuasivo y conciso. Usa emojis sutilmente.\n"

    "2. Usá 'consultar_inventario' para buscar o listar autos (marcas, modelos, precios, "
    "presupuesto, disponibilidad general).\n"

    "3. Usá 'detalle_vehiculo' cuando pregunten por UN auto específico: equipamiento, qué trae, "
    "motor, millaje, transmisión o color. Pasá el id si lo conocés; si no, marca y modelo en "
    "'descripcion_vehiculo'.\n"

    "4. Usá 'calcular_visa_cuotas' para TODA pregunta de cuotas, mensualidades, financiamiento o "
    "pagos mixtos. Jamás calcules montos, recargos ni divisiones de cabeza.\n"

    "4b. PAGOS MIXTOS: es muy común que el cliente pague una parte al contado y solo el resto con "
    "Visa Cuotas. Si dice 'doy X de prima', mandá X en 'pago_contado'. Si dice 'quiero pasar solo X "
    "por la tarjeta', mandá X en 'monto_a_tarjeta'. El recargo solo aplica a la parte de tarjeta, "
    "así que conviene mencionarle que entre más pague al contado, menos recargo paga.\n"

    "4c. Usá 'enviar_ubicacion' cuando pregunten dónde están, cómo llegar, la dirección, o pidan "
    "el Waze/Maps del local.\n"

    "4d. Usá 'contactar_asesor' cuando el cliente pida hablar con una persona, un vendedor o un "
    "humano, quiera negociar el precio, o tengas una consulta que no podés resolver. SIEMPRE "
    "incluí en 'resumen' qué busca el cliente (vehículo, ID, qué preguntó) para que el asesor "
    "tenga el contexto sin que el cliente repita nada.\n"

    "5. NUNCA inventes autos, precios, millaje ni características. Todo dato viene de una "
    "herramienta. Si un campo dice 'No especificado', decí que lo confirmás con un asesor.\n"

    "6. Si 'coincidencias' es 0, decí claramente que no hay ese vehículo y ofrecé alternativas "
    "reales del inventario.\n"

    "7. Respetá el rango de precio: 'más de X' es precio_min=X, 'hasta X' es precio_max=X. "
    "Jamás afirmes que no hay autos en un rango sin haber consultado ese rango exacto.\n"

    "8. FORMATO WHATSAPP: prohibido Markdown. Nada de [texto](url), ## títulos ni **doble "
    "asterisco**. Negrita con UN asterisco: *así*. Links pelados, sin paréntesis ni corchetes.\n"

    "9. Al LISTAR: máximo 4 autos por mensaje, y por auto solo:\n"
    "*Marca Modelo* (Año)\n"
    "Q00,000 | ID: xx\n"
    "URL_DEL_LINK\n\n"
    "No pongas equipamiento en los listados. Si hay más resultados, decí cuántos faltan.\n"

    "10. Al dar DETALLE: la foto que se envía ya trae precio, motor, transmisión, millaje y color "
    "en el caption — NO los repitas en el texto. En el texto poné solo un encabezado corto "
    "*Marca Modelo (Año)*, luego máximo 10 puntos de equipamiento con guiones, y el link de fotos "
    "adicionales si existe. Si hay más equipamiento, ofrecé mandar el resto. Cerrá invitando a "
    "visitarnos a verlo en persona, sin necesidad de agendar cita previa.\n"

    "11. Al dar CUOTAS: mostrá máximo 4 plazos por mensaje con el pago mensual de cada uno. "
    "Aclará el recargo de ese plazo y que el monto es referencial y sujeto a aprobación del banco. "
    "Nunca prometas aprobación. Si hay más plazos disponibles, mencionalos sin desglosarlos.\n"

    "12. Si el sistema envió fotos o ubicación, no digas 'no puedo mandar fotos/ubicación'. "
    "Ya se mandaron aparte como mensajes nativos.\n"

    "13. Ubicación: 35 Avenida 16-33 Zona 7, Villa Linda 2. "
    "Horarios: Lunes a Sábado 8:00 AM – 6:00 PM. NO se necesita cita previa para visitarnos: "
    "los clientes pueden llegar directo en horario de atención.\n"

    "14. Formas de pago: Contado y Visa Cuotas (no damos crédito propio ni prestamos).\n"

    "15. Si el cliente pregunta por opciones de pago, promociones, descuentos o formas de "
    "financiamiento que NO sean Visa Cuotas, responde honestamente que solo manejas Contado y "
    "Visa Cuotas. No inventes crédito, leasing, o planes que no existen.\n"

    "16. Si el cliente pide 'más fotos' de un carro, menciona el link_fotos (la carpeta de Drive) "
    "directamente sin intentar mandárselas por WhatsApp. Decí: 'Aquí están todas las fotos: [link]'.\n"

    "17. Si el cliente ya preguntó por un carro en este chat, recordá esa consulta. Si pregunta de "
    "nuevo, no repitas el detalle completo a menos que pida 'de nuevo'.\n"

    "18. Si el cliente pregunta 'cuál es el precio final', 'hay margen', o 'puedo negociar', "
    "responde honestamente que los precios son los de la base de datos de la importadora, pero que "
    "puede hablar con un asesor sobre opciones. NO prometas descuentos ni ofertas que no existen.\n"

    "19. Recepción de vehículos: Los Gemelos y Fer recibe vehículos SOLO como parte de pago. Si el "
    "cliente pregunta sobre vender/canjear su auto, explica que lo aceptamos en parte de pago por "
    "otro carro y invitalo a contactar con un asesor para evaluar la propuesta. No prometas valores "
    "ni procesos específicos—eso lo maneja el equipo de ventas.\n"

    "20. Para preguntas de CUOTAS: el sistema ya va a mandar el bloque con los montos exactos "
    "calculados. No necesitás redactar cifras en tu respuesta a menos que también estés "
    "respondiendo otra cosa en el mismo mensaje.\n"
)

# ─── Agente AI ────────────────────────────────────────────────────────────────
def procesar_mensaje_con_agente(from_number: str, user_text_raw: str) -> dict:
    if not openai_client:
        return {"texto": "Servicio temporalmente en mantenimiento.", "fotos": [], "enviar_ubicacion": False}

    historial = get_history(from_number)
    messages = ([{"role": "system", "content": SYSTEM_PROMPT_TEXT}]
                + historial
                + [{"role": "user", "content": user_text_raw}])
    append_to_history(from_number, "user", user_text_raw)

    fotos_pendientes = []
    ubicacion_pedida = False
    tools_llamadas = []
    resultado_cuotas = None

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

                if nombre == "enviar_ubicacion":
                    ubicacion_pedida = True

                resultado, fotos = despachar_tool(nombre, args, cliente=from_number)
                fotos_pendientes.extend(fotos)
                tools_llamadas.append(nombre)
                if nombre == "calcular_visa_cuotas":
                    resultado_cuotas = resultado

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": nombre,
                    "content": json.dumps(resultado, ensure_ascii=False)
                })

            # Si la ÚNICA herramienta llamada fue cuotas, no dejamos que el modelo
            # redacte los números: los arma Python directo desde el cálculo, así
            # nunca hay riesgo de que transcriba mal una cifra o un recargo.
            if tools_llamadas == ["calcular_visa_cuotas"] and resultado_cuotas is not None:
                texto_final = formatear_bloque_cuotas(resultado_cuotas)
            else:
                segunda = openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=messages,
                    temperature=0.3
                )
                texto_final = segunda.choices[0].message.content
        else:
            texto_final = response_message.content

        texto_final = limpiar_markdown_whatsapp(texto_final or "") or "¿En qué más te puedo ayudar?"
        append_to_history(from_number, "assistant", texto_final)

        vistas, fotos_unicas = set(), []
        for f in fotos_pendientes:
            if f["url"] and f["url"] not in vistas:
                vistas.add(f["url"])
                fotos_unicas.append(f)

        return {
            "texto": texto_final,
            "fotos": fotos_unicas[:MAX_FOTOS_POR_RESPUESTA],
            "enviar_ubicacion": ubicacion_pedida
        }

    except Exception as e:
        logger.error("Error en OpenAI: %s", e)
        return {
            "texto": "Disculpa, estoy procesando mucha información. ¿Puedes repetir tu pregunta en unos segundos?",
            "fotos": [],
            "enviar_ubicacion": False
        }

# ─── Leads y Mensajería WhatsApp ──────────────────────────────────────────────
def guardar_lead(telefono: str, mensaje: str, tipo: str):
    if not LEADS_WEBHOOK_URL:
        return
    def _guardar():
        try:
            requests.post(LEADS_WEBHOOK_URL, json={
                "fecha": datetime.now(GUATEMALA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "telefono": telefono,
                "mensaje": mensaje,
                "tipo": tipo
            }, timeout=REQUEST_TIMEOUT)
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
        if res is not None and res.status_code >= 400:
            logger.error("WhatsApp %s: %s", res.status_code, res.text[:300])
        return res
    except Exception as e:
        logger.error("Error WhatsApp: %s", e)
        return None

def dividir_mensaje(texto: str, limite: int = WHATSAPP_MAX_LEN) -> list:
    """Parte un texto largo respetando párrafos, luego líneas, luego corte duro."""
    texto = (texto or "").strip()
    if len(texto) <= limite:
        return [texto] if texto else []

    partes, actual = [], ""
    for bloque in texto.split("\n\n"):
        candidato = f"{actual}\n\n{bloque}" if actual else bloque
        if len(candidato) <= limite:
            actual = candidato
            continue

        if actual:
            partes.append(actual)
            actual = ""

        if len(bloque) <= limite:
            actual = bloque
            continue

        for linea in bloque.split("\n"):
            candidato = f"{actual}\n{linea}" if actual else linea
            if len(candidato) <= limite:
                actual = candidato
            else:
                if actual:
                    partes.append(actual)
                    actual = ""
                while len(linea) > limite:
                    partes.append(linea[:limite])
                    linea = linea[limite:]
                actual = linea

    if actual:
        partes.append(actual)

    total = len(partes)
    if total > 1:
        partes = [f"{p}\n\n({i}/{total})" for i, p in enumerate(partes, 1)]
    return partes

def send_whatsapp_message(to_number: str, message_text: str):
    partes = dividir_mensaje(message_text)
    if not partes:
        return None
    respuesta = None
    for i, parte in enumerate(partes):
        respuesta = send_whatsapp_payload({
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": parte}
        })
        if i < len(partes) - 1:
            time.sleep(0.6)
    if len(partes) > 1:
        logger.info("Mensaje dividido en %d partes para %s", len(partes), to_number)
    return respuesta

def send_whatsapp_image(to_number: str, image_url: str, caption: str = ""):
    if not image_url:
        return None
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "image",
        "image": {"link": image_url}
    }
    if caption:
        payload["image"]["caption"] = caption[:WHATSAPP_CAPTION_MAX]
    res = send_whatsapp_payload(payload)
    if res is not None and res.status_code >= 400:
        logger.error("Falló envío de imagen: %s", image_url)
    return res

def send_whatsapp_location(to_number: str):
    """Manda el pin nativo de WhatsApp con mapa embebido."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "location",
        "location": {
            "latitude": NEGOCIO_LAT,
            "longitude": NEGOCIO_LNG,
            "name": NEGOCIO_NOMBRE,
            "address": NEGOCIO_DIRECCION
        }
    }
    res = send_whatsapp_payload(payload)
    if res is not None and res.status_code >= 400:
        logger.error("Falló envío de ubicación nativa")
    return res

def build_advisor_link():
    return f"https://wa.me/{ADMIN_PHONE}?text={quote('Hola, vengo del bot')}"

# ─── Controladores de Mensajes ────────────────────────────────────────────────
def handle_text_message(from_number: str, user_text_raw: str):
    user_text = normalize_text(user_text_raw)

    if user_text == "adminstats" and from_number == ADMIN_PHONE:
        inv = obtener_inventario()
        disponibles = [c for c in inv if str(c.get("marca") or "").strip() and esta_disponible(c)]
        con_foto = [c for c in disponibles if url_imagen_directa(c.get("foto_principal"))]
        send_whatsapp_message(from_number, (
            f"📊 *Estadísticas*\n"
            f"Consultas hoy: {stats['consultas_hoy']}\n"
            f"Usuarios en sesión: {len(known_users)}\n"
            f"Vehículos en Sheet: {len(inv)}\n"
            f"Disponibles: {len(disponibles)}\n"
            f"Con foto válida: {len(con_foto)}\n"
            f"Sesiones AI activas: {len(user_chat_histories)}"
        ))
        return

    # Rate limit (el admin queda exento)
    if from_number != ADMIN_PHONE and rate_limit_excedido(from_number):
        logger.warning("Rate limit alcanzado por %s", from_number)
        if debe_avisar_rate_limit(from_number):
            send_whatsapp_message(from_number, (
                "Estás enviando mensajes muy rápido 😅 Dame un minuto y seguimos. "
                "Si es urgente, escribinos directo: " + build_advisor_link()
            ))
        return

    with _state_lock:
        if stats["fecha"] != hoy_str():
            stats["fecha"] = hoy_str()
            stats["consultas_hoy"] = 0
        stats["consultas_hoy"] += 1
        es_nuevo = from_number not in known_users
        known_users.add(from_number)

    if es_nuevo:
        guardar_lead(from_number, "Usuario Nuevo", "usuario_nuevo")

    resultado = procesar_mensaje_con_agente(from_number, user_text_raw)

    # Orden: primero lo visual (foto/ubicación), después el texto que las complementa.
    for foto in resultado.get("fotos", []):
        send_whatsapp_image(from_number, foto["url"], foto.get("caption", ""))
        time.sleep(0.4)

    if resultado.get("enviar_ubicacion"):
        send_whatsapp_location(from_number)
        time.sleep(0.4)

    send_whatsapp_message(from_number, resultado["texto"])

def handle_interactive_message(from_number: str, interactive: dict):
    tipo = interactive.get("type")
    user_text = ""
    if tipo == "list_reply":
        user_text = interactive.get("list_reply", {}).get("title", "")
    elif tipo == "button_reply":
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

# ─── Rutas ────────────────────────────────────────────────────────────────────
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
    con_foto = [c for c in validos if url_imagen_directa(c.get("foto_principal"))]
    return jsonify({
        "sheet_url_configurada": bool(SHEET_URL),
        "openai_configurado": bool(openai_client),
        "registros": len(data),
        "con_marca": len(validos),
        "disponibles": len(disponibles),
        "con_foto_valida": len(con_foto),
        "estados_encontrados": sorted({str(c.get("estado") or "(vacio)") for c in validos}),
        "llaves_detectadas": list(data[0].keys()) if data else [],
        "fotos_convertidas": [
            {"id": c.get("id"), "url": url_imagen_directa(c.get("foto_principal")),
             "caption": construir_caption_foto(c)}
            for c in con_foto[:5]
        ],
        "muestra": data[:2]
    }), 200

@app.route("/debug-cuotas", methods=["GET"])
def debug_cuotas():
    """Prueba rápida: /debug-cuotas?id=1&contado=20000&cuotas=24"""
    resultado, _ = ejecutar_tool_visa_cuotas(
        id_vehiculo=request.args.get("id"),
        monto=request.args.get("monto", type=float),
        cuotas=request.args.get("cuotas", type=int),
        pago_contado=request.args.get("contado", type=float),
        monto_a_tarjeta=request.args.get("tarjeta", type=float)
    )
    return jsonify(resultado), 200

@app.route("/debug-ubicacion", methods=["GET"])
def debug_ubicacion():
    resultado, _ = ejecutar_tool_ubicacion()
    return jsonify(resultado), 200

@app.route("/debug-buscar", methods=["GET"])
def debug_buscar():
    """Prueba la búsqueda flexible: /debug-buscar?q=nissan 350"""
    q = request.args.get("q", "")
    carro = buscar_carro_por_texto(q)
    if not carro:
        return jsonify({"consulta": q, "encontrado": False}), 200
    return jsonify({
        "consulta": q,
        "encontrado": True,
        "id": carro.get("id"),
        "marca": carro.get("marca"),
        "modelo": carro.get("modelo"),
        "anio": carro.get("anio")
    }), 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge"), 200
    return "Token inválido", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    if not verify_meta_signature(request.data, request.headers.get("X-Hub-Signature-256", "")):
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

# ─── Arranque (aplica también bajo gunicorn en Render) ────────────────────────
try:
    refrescar_inventario()
except Exception as e:
    logger.error("Carga inicial de inventario falló: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
