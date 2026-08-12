"""
facebook_publisher.py
---------------------
Genera borradores de publicaciones en la pagina de Facebook de
Importadora Los Gemelos y Fer, a partir del inventario en Google Sheets.

PRINCIPIOS DE DISENO (consistentes con el bot de WhatsApp):
  - El caption lo redacta Python de forma determinista desde el Sheet.
    La IA NO escribe precios ni especificaciones.
  - Todo se publica como BORRADOR (published=false). Un humano aprueba
    y publica desde Meta Business Suite.
  - Nunca se inventan datos: si una columna viene vacia, simplemente
    no aparece en el caption.
  - Rotacion controlada por la columna `ultima_publicacion_fb` del Sheet.

VARIABLES DE ENTORNO REQUERIDAS EN RENDER:
  FB_PAGE_ID                   ID numerico de la pagina de Facebook
  FB_PAGE_TOKEN                Page Access Token de larga duracion
  GOOGLE_SERVICE_ACCOUNT_JSON  Credenciales de la cuenta de servicio (JSON en una linea)
  SHEET_ID                     ID del Google Sheet de inventario
  SHEET_NAME                   Nombre de la hoja (default: "Hoja 1")
  FB_PUBLISH_TOKEN             Token propio para proteger el endpoint
  WHATSAPP_LINK                Link wa.me que se incluye en el caption

DEPENDENCIAS (agregar a requirements.txt):
  google-api-python-client
  google-auth
  requests
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# CONFIGURACION
# --------------------------------------------------------------------------

GRAPH_VERSION = "v20.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Cuantos vehiculos se publican por corrida
VEHICULOS_POR_CORRIDA = 6

# Facebook maneja hasta 10 fotos de forma confiable en attached_media
MAX_FOTOS_POR_POST = 10

# Estados del Sheet que ocultan un vehiculo (mismos que usa el bot)
ESTADOS_OCULTOS = {
    "vendido", "apartado", "reservado", "entregado", "no disponible",
}

# Zona horaria de Guatemala (UTC-6, sin horario de verano)
TZ_GT = timezone(timedelta(hours=-6))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Nombre exacto de la columna nueva que hay que agregar al Sheet
COL_ULTIMA_PUB = "ultima_publicacion_fb"


# --------------------------------------------------------------------------
# AUTENTICACION CON GOOGLE
# --------------------------------------------------------------------------

def _google_credentials():
    """Construye credenciales desde la env var GOOGLE_SERVICE_ACCOUNT_JSON."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("Falta GOOGLE_SERVICE_ACCOUNT_JSON en las variables de entorno")
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _sheets_service():
    return build("sheets", "v4", credentials=_google_credentials(), cache_discovery=False)


def _drive_service():
    return build("drive", "v3", credentials=_google_credentials(), cache_discovery=False)


# --------------------------------------------------------------------------
# LECTURA / ESCRITURA DEL SHEET
# --------------------------------------------------------------------------

def leer_inventario():
    """
    Lee el Sheet completo y devuelve (lista_de_dicts, headers, sheet_name).
    Cada dict incluye `_fila` con el numero de fila real (1-indexed) para
    poder escribir de vuelta despues.
    """
    sheet_id = os.environ["SHEET_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "Hoja 1")

    svc = _sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=sheet_name,
    ).execute()

    valores = resp.get("values", [])
    if not valores:
        return [], [], sheet_name

    headers = [h.strip().lower() for h in valores[0]]

    if COL_ULTIMA_PUB not in headers:
        raise RuntimeError(
            f"El Sheet no tiene la columna '{COL_ULTIMA_PUB}'. "
            "Agregala antes de correr el publicador."
        )

    filas = []
    for idx, fila in enumerate(valores[1:], start=2):  # fila 1 = headers
        # Rellenar celdas faltantes al final
        fila = fila + [""] * (len(headers) - len(fila))
        registro = dict(zip(headers, fila))
        registro["_fila"] = idx
        filas.append(registro)

    return filas, headers, sheet_name


def marcar_publicado(fila_num, headers, sheet_name, fecha_iso):
    """Escribe la fecha de publicacion en la columna ultima_publicacion_fb."""
    sheet_id = os.environ["SHEET_ID"]
    col_idx = headers.index(COL_ULTIMA_PUB)
    col_letra = _num_a_letra_columna(col_idx + 1)

    svc = _sheets_service()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!{col_letra}{fila_num}",
        valueInputOption="RAW",
        body={"values": [[fecha_iso]]},
    ).execute()


def _num_a_letra_columna(n):
    """1 -> A, 27 -> AA, etc."""
    letras = ""
    while n > 0:
        n, resto = divmod(n - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


# --------------------------------------------------------------------------
# SELECCION DE VEHICULOS (ROTACION)
# --------------------------------------------------------------------------

def seleccionar_vehiculos(inventario, cantidad=VEHICULOS_POR_CORRIDA):
    """
    Filtra vehiculos disponibles y los ordena por antiguedad de publicacion.
    Los que nunca se han publicado (celda vacia) tienen prioridad maxima.
    """
    disponibles = [
        v for v in inventario
        if v.get("estado", "").strip().lower() not in ESTADOS_OCULTOS
        and v.get("id", "").strip()
    ]

    def clave_orden(v):
        fecha = v.get(COL_ULTIMA_PUB, "").strip()
        # Cadena vacia ordena primero -> nunca publicados van al frente
        return fecha if fecha else ""

    disponibles.sort(key=clave_orden)
    return disponibles[:cantidad]


# --------------------------------------------------------------------------
# GENERACION DEL CAPTION (100% DETERMINISTA, SIN IA)
# --------------------------------------------------------------------------

def _formatear_precio(valor):
    """Convierte '85000' o '85,000' en 'Q85,000'. Si no es numero, devuelve tal cual."""
    limpio = str(valor).replace("Q", "").replace(",", "").replace(" ", "").strip()
    try:
        return f"Q{int(float(limpio)):,}"
    except (ValueError, TypeError):
        return str(valor).strip()


def _formatear_millaje(valor):
    limpio = str(valor).replace(",", "").replace(" ", "").strip()
    limpio = limpio.lower().replace("km", "").replace("millas", "").strip()
    try:
        return f"{int(float(limpio)):,} km"
    except (ValueError, TypeError):
        return str(valor).strip()


def generar_caption(vehiculo):
    """
    Arma el texto del post desde las columnas del Sheet.
    Si un campo viene vacio, simplemente se omite: nunca se inventa nada.
    """
    marca = vehiculo.get("marca", "").strip()
    modelo = vehiculo.get("modelo", "").strip()
    anio = vehiculo.get("anio", "").strip()

    titulo_partes = [p for p in (marca, modelo, anio) if p]
    titulo = " ".join(titulo_partes).upper()

    lineas = [titulo, ""]

    precio = vehiculo.get("precio", "").strip()
    if precio:
        lineas.append(f"💰 {_formatear_precio(precio)}")

    # Especificaciones, solo las que existan
    specs = [
        ("🔧", vehiculo.get("motor", "").strip()),
        ("⚙️", vehiculo.get("transmision", "").strip()),
        ("⛽", vehiculo.get("combustible", "").strip()),
        ("🎨", vehiculo.get("color", "").strip()),
    ]
    for icono, valor in specs:
        if valor:
            lineas.append(f"{icono} {valor}")

    millaje = vehiculo.get("millaje", "").strip()
    if millaje:
        lineas.append(f"📊 {_formatear_millaje(millaje)}")

    descripcion = vehiculo.get("descripcion", "").strip()
    if descripcion:
        lineas.extend(["", descripcion])

    # Bloque fijo de cierre: mismas reglas de negocio que usa el bot
    whatsapp = os.environ.get("WHATSAPP_LINK", "").strip()

    lineas.extend([
        "",
        "━━━━━━━━━━━━━━━",
        "✅ Visitas SIN cita previa",
        "🚗 Recibimos tu vehículo como parte de pago",
        "💳 Aceptamos Visa Cuotas",
        "",
        "📍 35 Avenida 16-33 Zona 7, Villa Linda 2, Ciudad de Guatemala",
        "🕐 Lunes a Sábado, 8:00 AM – 6:00 PM",
    ])

    if whatsapp:
        lineas.extend(["", f"💬 Escríbenos: {whatsapp}"])

    return "\n".join(lineas)


# --------------------------------------------------------------------------
# FOTOS DESDE GOOGLE DRIVE
# --------------------------------------------------------------------------

def _extraer_folder_id(link):
    """Saca el ID de carpeta de un link de Drive tipo /folders/<ID>."""
    link = (link or "").strip()
    if not link:
        return None
    if "/folders/" in link:
        return link.split("/folders/")[1].split("?")[0].split("/")[0]
    return None


def obtener_fotos(vehiculo, limite=MAX_FOTOS_POR_POST):
    """
    Lista las imagenes publicas de la carpeta de Drive del vehiculo y
    devuelve URLs servidas por el CDN de Google (mismo patron que el bot).
    Si no hay carpeta, cae de vuelta a foto_principal.
    """
    folder_id = _extraer_folder_id(vehiculo.get("link_fotos", ""))

    if not folder_id:
        principal = vehiculo.get("foto_principal", "").strip()
        return [principal] if principal else []

    try:
        svc = _drive_service()
        resultado = svc.files().list(
            q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
            fields="files(id, name, mimeType)",
            orderBy="name",
            pageSize=limite,
        ).execute()
    except Exception as e:
        log.warning("No se pudo listar la carpeta %s: %s", folder_id, e)
        principal = vehiculo.get("foto_principal", "").strip()
        return [principal] if principal else []

    archivos = resultado.get("files", [])

    urls = []
    for archivo in archivos:
        # HEIC no lo procesa Facebook; se descarta igual que en el bot
        if "heic" in archivo.get("mimeType", "").lower():
            continue
        urls.append(f"https://lh3.googleusercontent.com/d/{archivo['id']}")

    if not urls:
        principal = vehiculo.get("foto_principal", "").strip()
        return [principal] if principal else []

    return urls[:limite]


# --------------------------------------------------------------------------
# PUBLICACION A FACEBOOK
# --------------------------------------------------------------------------

def _subir_foto_sin_publicar(url_foto):
    """
    Sube una foto a la pagina SIN publicarla y devuelve su ID,
    para poder adjuntarla despues a un post multi-foto.
    Lanza RuntimeError con el detalle exacto que devuelve Facebook si falla.
    """
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_TOKEN"]

    resp = requests.post(
        f"{GRAPH_BASE}/{page_id}/photos",
        data={
            "url": url_foto,
            "published": "false",
            "access_token": token,
        },
        timeout=60,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Facebook respondio {resp.status_code} para '{url_foto}': {resp.text[:500]}"
        )

    data = resp.json()
    if "id" not in data:
        raise RuntimeError(f"Respuesta sin 'id' para '{url_foto}': {resp.text[:500]}")

    return data["id"]


def crear_borrador(vehiculo):
    """
    Crea UN borrador de publicacion en Facebook para un vehiculo.
    Devuelve un dict con el resultado.
    """
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_TOKEN"]

    caption = generar_caption(vehiculo)
    fotos = obtener_fotos(vehiculo)

    if not fotos:
        return {
            "id": vehiculo.get("id"),
            "ok": False,
            "error": "El vehiculo no tiene fotos disponibles",
        }

    # Subir cada foto sin publicar
    media_ids = []
    errores_fotos = []
    for url in fotos:
        try:
            media_ids.append(_subir_foto_sin_publicar(url))
        except Exception as e:
            log.warning("Fallo al subir foto %s: %s", url, e)
            errores_fotos.append({"url": url, "error": str(e)})

    if not media_ids:
        return {
            "id": vehiculo.get("id"),
            "ok": False,
            "error": "Ninguna foto se pudo subir a Facebook",
            "detalle_errores": errores_fotos,
        }

    # Crear el post como BORRADOR con todas las fotos adjuntas
    payload = {
        "message": caption,
        "published": "false",
        "access_token": token,
    }
    for i, media_id in enumerate(media_ids):
        payload[f"attached_media[{i}]"] = json.dumps({"media_fbid": media_id})

    resp = requests.post(f"{GRAPH_BASE}/{page_id}/feed", data=payload, timeout=60)

    if resp.status_code != 200:
        return {
            "id": vehiculo.get("id"),
            "ok": False,
            "error": f"Facebook respondio {resp.status_code}: {resp.text[:300]}",
        }

    return {
        "id": vehiculo.get("id"),
        "ok": True,
        "post_id": resp.json().get("id"),
        "fotos": len(media_ids),
        "_fila": vehiculo["_fila"],
    }


# --------------------------------------------------------------------------
# ORQUESTADOR
# --------------------------------------------------------------------------

def generar_borradores(cantidad=VEHICULOS_POR_CORRIDA, dry_run=False):
    """
    Punto de entrada principal.
    Lee el Sheet, elige los N vehiculos con publicacion mas antigua,
    crea un borrador por cada uno y marca la fecha de vuelta en el Sheet.

    dry_run=True devuelve lo que HARIA sin tocar Facebook ni el Sheet.
    """
    inventario, headers, sheet_name = leer_inventario()
    seleccionados = seleccionar_vehiculos(inventario, cantidad)

    if not seleccionados:
        return {"ok": True, "mensaje": "No hay vehiculos disponibles para publicar", "resultados": []}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "seleccionados": [
                {
                    "id": v.get("id"),
                    "vehiculo": f"{v.get('marca','')} {v.get('modelo','')} {v.get('anio','')}".strip(),
                    "ultima_publicacion": v.get(COL_ULTIMA_PUB) or "nunca",
                    "fotos_encontradas": len(obtener_fotos(v)),
                    "caption": generar_caption(v),
                }
                for v in seleccionados
            ],
        }

    hoy = datetime.now(TZ_GT).strftime("%Y-%m-%d")
    resultados = []

    for vehiculo in seleccionados:
        resultado = crear_borrador(vehiculo)
        resultados.append(resultado)

        if resultado["ok"]:
            try:
                marcar_publicado(resultado["_fila"], headers, sheet_name, hoy)
            except Exception as e:
                log.error("Borrador creado pero fallo al marcar el Sheet: %s", e)
                resultado["aviso"] = "Borrador creado, pero no se marco en el Sheet"

    exitosos = sum(1 for r in resultados if r["ok"])

    return {
        "ok": True,
        "fecha": hoy,
        "borradores_creados": exitosos,
        "fallidos": len(resultados) - exitosos,
        "resultados": resultados,
    }


# --------------------------------------------------------------------------
# ENDPOINTS FLASK (registrar en la app existente)
# --------------------------------------------------------------------------

def registrar_rutas(app):
    """
    Registra los endpoints en la app de Flask existente.
    En tu archivo principal:

        from facebook_publisher import registrar_rutas
        registrar_rutas(app)
    """
    from flask import request, jsonify

    def _token_valido():
        esperado = os.environ.get("FB_PUBLISH_TOKEN", "")
        return esperado and request.args.get("token") == esperado

    @app.route("/publicar-facebook")
    def _publicar_facebook():
        """Endpoint que dispara UptimeRobot una vez al dia."""
        if not _token_valido():
            return jsonify({"error": "no autorizado"}), 403
        try:
            cantidad = int(request.args.get("cantidad", VEHICULOS_POR_CORRIDA))
        except ValueError:
            cantidad = VEHICULOS_POR_CORRIDA
        try:
            return jsonify(generar_borradores(cantidad=cantidad))
        except Exception as e:
            log.exception("Error generando borradores")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/debug-foto")
    def _debug_foto():
        """Prueba subir UNA foto sola y muestra el error exacto de Facebook."""
        if not _token_valido():
            return jsonify({"error": "no autorizado"}), 403
        url_foto = request.args.get("url", "").strip()
        if not url_foto:
            return jsonify({"error": "falta el parametro ?url="}), 400
        try:
            media_id = _subir_foto_sin_publicar(url_foto)
            return jsonify({"ok": True, "media_id": media_id, "url": url_foto})
        except Exception as e:
            return jsonify({"ok": False, "url": url_foto, "error": str(e)}), 200

    @app.route("/debug-facebook")
    def _debug_facebook():
        """Prueba en seco: muestra que publicaria, sin tocar Facebook ni el Sheet."""
        if not _token_valido():
            return jsonify({"error": "no autorizado"}), 403
        try:
            cantidad = int(request.args.get("cantidad", VEHICULOS_POR_CORRIDA))
        except ValueError:
            cantidad = VEHICULOS_POR_CORRIDA
        try:
            return jsonify(generar_borradores(cantidad=cantidad, dry_run=True))
        except Exception as e:
            log.exception("Error en dry run")
            return jsonify({"ok": False, "error": str(e)}), 500
