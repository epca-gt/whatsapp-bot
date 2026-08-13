"""
collage_generator.py
--------------------
Genera imagenes tipo catalogo (collage 2x2) del inventario para usarlas
como anuncios de pago en Facebook. SIN precios (para forzar el contacto
por WhatsApp) y con una banda de promocion configurable.

Reusa la infraestructura del publicador (facebook_publisher.py):
  - leer_inventario()      -> lee el Sheet
  - _drive_service()       -> acceso a Drive
  - _extraer_folder_id()   -> saca el folder de link_fotos
  - ESTADOS_OCULTOS        -> mismos estados que ocultan un vehiculo
  - _formatear_millaje()   -> millaje en millas

Estilo de marca: fondo negro carbon, texto blanco, acento azul electrico
(consistente con la portada de la pagina).

ENDPOINTS (registrados via registrar_rutas_collage(app)):
  /generar-collages?token=...   -> genera todas las imagenes, las deja en
                                   memoria y devuelve links para descargarlas
  /collage/<indice>?token=...   -> descarga la imagen N ya generada
  /debug-collages?token=...     -> lista que vehiculos entrarian, sin generar

VARIABLE DE ENTORNO OPCIONAL:
  LOGO_URL   -> URL publica del logo (PNG con fondo negro/transparente).
                Si no esta, se usa un fallback con el nombre estilizado.
"""

import os
import io
import logging

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# Reusar todo lo que ya existe en el publicador
from facebook_publisher import (
    leer_inventario,
    _drive_service,
    _extraer_folder_id,
    _formatear_millaje,
    ESTADOS_OCULTOS,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# CONFIGURACION VISUAL
# --------------------------------------------------------------------------

# Lienzo cuadrado 1080x1080 (formato ideal para feed y anuncios de Facebook/IG)
LIENZO = 1080

# Paleta de marca
COL_FONDO = (10, 10, 12)
COL_FONDO_CLARO = (22, 22, 26)
COL_TEXTO = (255, 255, 255)
COL_TEXTO_TENUE = (180, 180, 186)
COL_ACENTO = (46, 110, 245)      # azul electrico
COL_TARJETA = (26, 26, 30)

# Cuantos vehiculos por imagen
VEHICULOS_POR_COLLAGE = 4

# Texto de la promocion (banda inferior)
PROMO_TEXTO = "Menciónanos que vienes de Facebook y el traspaso va GRATIS"

# Datos de contacto
WHATSAPP_DISPLAY = "+502 4170 6199"

FONT_DIR = "/mnt/skills/examples/canvas-design/canvas-fonts/"

# Fallback si el modulo corre en Render y las fuentes no estan (se usan las
# del sistema); en Render conviene incluir las .ttf en el repo. Ver nota abajo.
FONT_DIR_FALLBACK = os.environ.get("FONT_DIR", FONT_DIR)


def _font(nombre, tam):
    """Carga una fuente, probando primero el dir de skills y luego el fallback."""
    for base in (FONT_DIR, FONT_DIR_FALLBACK):
        ruta = os.path.join(base, nombre)
        if os.path.exists(ruta):
            return ImageFont.truetype(ruta, tam)
    # Ultimo recurso: fuente por defecto de PIL
    return ImageFont.load_default()


# --------------------------------------------------------------------------
# UTILIDADES DE IMAGEN
# --------------------------------------------------------------------------

def _descargar_imagen(url, timeout=30):
    """Descarga una imagen desde una URL y la devuelve como objeto PIL."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def _primera_foto_url(vehiculo):
    """
    Devuelve la URL de la PRIMERA foto de la carpeta de Drive del vehiculo
    (la de portada, por orden de nombre). Cae a foto_principal si no hay carpeta.
    """
    folder_id = _extraer_folder_id(vehiculo.get("link_fotos", ""))
    if folder_id:
        try:
            svc = _drive_service()
            resultado = svc.files().list(
                q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
                fields="files(id, name, mimeType)",
                orderBy="name",
                pageSize=5,
            ).execute()
            for archivo in resultado.get("files", []):
                if "heic" in archivo.get("mimeType", "").lower():
                    continue
                return f"https://lh3.googleusercontent.com/d/{archivo['id']}"
        except Exception as e:
            log.warning("No se pudo listar carpeta para foto de portada: %s", e)

    principal = vehiculo.get("foto_principal", "").strip()
    return principal or None


def _recortar_llenar(img, ancho, alto):
    """Recorta y escala una imagen para llenar exactamente ancho x alto (cover)."""
    return ImageOps.fit(img, (ancho, alto), method=Image.LANCZOS, centering=(0.5, 0.5))


def _esquinas_redondeadas(img, radio):
    """Devuelve la imagen con esquinas redondeadas (con canal alfa)."""
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, img.size[0], img.size[1]], radius=radio, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def _cargar_logo():
    """
    Carga el logo desde LOGO_URL (si esta configurada) y lo deja listo
    con fondo transparente. Devuelve None si no hay logo disponible.
    """
    url = os.environ.get("LOGO_URL", "").strip()
    if not url:
        return None
    try:
        logo = _descargar_imagen(url).convert("RGBA")
    except Exception as e:
        log.warning("No se pudo cargar el logo desde LOGO_URL: %s", e)
        return None

    # Hacer transparente el fondo negro del logo
    datos = logo.getdata()
    nuevos = []
    for r, g, b, a in datos:
        if r < 25 and g < 25 and b < 25:
            nuevos.append((r, g, b, 0))
        else:
            boost = lambda c: min(255, int(c * 1.3))
            nuevos.append((boost(r), boost(g), boost(b), a))
    logo.putdata(nuevos)
    return logo


# --------------------------------------------------------------------------
# CONSTRUCCION DE UNA TARJETA DE VEHICULO
# --------------------------------------------------------------------------

def _dibujar_tarjeta(base, x, y, ancho, alto, vehiculo, foto):
    """
    Dibuja una tarjeta de vehiculo en (x, y) sobre `base`.
    foto: objeto PIL ya descargado (o None).
    """
    draw = ImageDraw.Draw(base, "RGBA")
    radio = 24

    # Fondo de la tarjeta
    draw.rounded_rectangle([x, y, x + ancho, y + alto], radius=radio, fill=COL_TARJETA)

    # --- Foto (mitad superior de la tarjeta) ---
    foto_h = int(alto * 0.54)
    margen = 0
    if foto is not None:
        foto_ajustada = _recortar_llenar(foto, ancho, foto_h)
        foto_red = _esquinas_redondeadas(foto_ajustada, radio)
        # Recortar solo esquinas superiores redondeadas: pegar y luego tapar abajo
        base.paste(foto_red, (x, y), foto_red)
        # Tapar las esquinas inferiores de la foto para que no queden redondeadas
        draw.rectangle([x, y + foto_h - radio, x + ancho, y + foto_h], fill=COL_TARJETA)
    else:
        draw.rounded_rectangle(
            [x, y, x + ancho, y + foto_h], radius=radio,
            fill=COL_FONDO_CLARO,
        )
        f = _font("WorkSans-Regular.ttf", 22)
        draw.text((x + ancho // 2, y + foto_h // 2), "Sin foto",
                  font=f, fill=COL_TEXTO_TENUE, anchor="mm")

    # --- Barra de acento bajo la foto ---
    draw.rectangle([x, y + foto_h, x + ancho, y + foto_h + 4], fill=COL_ACENTO)

    # --- Texto (mitad inferior) ---
    tx = x + 24
    ty = y + foto_h + 20

    marca = vehiculo.get("marca", "").strip()
    modelo = vehiculo.get("modelo", "").strip()
    anio = vehiculo.get("anio", "").strip()

    titulo = f"{marca} {modelo}".strip().upper()
    f_titulo = _font("BigShoulders-Bold.ttf", 32)

    # Ajustar el titulo si es muy largo (reducir tamano hasta que quepa)
    max_ancho_texto = ancho - 48
    tam = 32
    while tam > 20:
        f_titulo = _font("BigShoulders-Bold.ttf", tam)
        w = draw.textlength(titulo, font=f_titulo)
        if w <= max_ancho_texto:
            break
        tam -= 2

    draw.text((tx, ty), titulo, font=f_titulo, fill=COL_TEXTO)
    ty += tam + 4

    # Año como chip de acento
    if anio:
        f_anio = _font("WorkSans-Bold.ttf", 18)
        chip_w = draw.textlength(anio, font=f_anio) + 18
        draw.rounded_rectangle([tx, ty, tx + chip_w, ty + 27], radius=7, fill=COL_ACENTO)
        draw.text((tx + 9, ty + 3), anio, font=f_anio, fill=COL_TEXTO)
        ty += 37

    # Datos: transmision y millaje (SIN precio)
    f_dato = _font("WorkSans-Regular.ttf", 20)
    transmision = vehiculo.get("transmision", "").strip()
    millaje = vehiculo.get("millaje", "").strip()

    lineas = []
    if transmision:
        lineas.append(transmision)
    if millaje:
        lineas.append(_formatear_millaje(millaje))

    for linea in lineas:
        # Bullet dibujado a mano (evita depender de glifos Unicode/emoji)
        by = ty + 10
        draw.ellipse([tx, by, tx + 6, by + 6], fill=COL_ACENTO)
        draw.text((tx + 16, ty), linea, font=f_dato, fill=COL_TEXTO_TENUE)
        ty += 27


# --------------------------------------------------------------------------
# CONSTRUCCION DE UN COLLAGE COMPLETO (2x2)
# --------------------------------------------------------------------------

def _construir_collage(grupo, logo, indice, total):
    """
    Construye un collage 1080x1080 con hasta 4 vehiculos (2x2).
    grupo: lista de dicts vehiculo (max 4).
    logo: objeto PIL o None.
    """
    base = Image.new("RGB", (LIENZO, LIENZO), COL_FONDO)

    # Fondo con degradado sutil
    for yy in range(LIENZO):
        t = yy / LIENZO
        c = tuple(int(COL_FONDO[i] + (COL_FONDO_CLARO[i] - COL_FONDO[i]) * t) for i in range(3))
        ImageDraw.Draw(base).line([(0, yy), (LIENZO, yy)], fill=c)

    draw = ImageDraw.Draw(base, "RGBA")

    # --- Encabezado ---
    header_h = 150
    if logo is not None:
        logo_size = 110
        logo_r = logo.resize((logo_size, logo_size), Image.LANCZOS)
        base.paste(logo_r, (40, 20), logo_r)
        texto_x = 40 + logo_size + 20
    else:
        texto_x = 44

    f_marca = _font("BigShoulders-Bold.ttf", 44)
    draw.text((texto_x, 38), "LOS GEMELOS Y FER", font=f_marca, fill=COL_TEXTO)
    f_sub = _font("WorkSans-Regular.ttf", 22)
    draw.text((texto_x, 92), "VEHÍCULOS DISPONIBLES", font=f_sub, fill=COL_ACENTO)

    # Contador de pagina (arriba a la derecha)
    if total > 1:
        f_pag = _font("DMMono-Regular.ttf", 20)
        pag_txt = f"{indice}/{total}"
        w = draw.textlength(pag_txt, font=f_pag)
        draw.text((LIENZO - 44 - w, 60), pag_txt, font=f_pag, fill=COL_TEXTO_TENUE)

    # --- Rejilla 2x2 de tarjetas ---
    promo_h = 130
    zona_top = header_h
    zona_alto = LIENZO - header_h - promo_h
    gap = 24
    margen_lat = 40

    celda_w = (LIENZO - 2 * margen_lat - gap) // 2
    celda_h = (zona_alto - gap) // 2 - 6

    posiciones = [
        (margen_lat, zona_top),
        (margen_lat + celda_w + gap, zona_top),
        (margen_lat, zona_top + celda_h + gap),
        (margen_lat + celda_w + gap, zona_top + celda_h + gap),
    ]

    for i, vehiculo in enumerate(grupo[:4]):
        foto = None
        url = _primera_foto_url(vehiculo)
        if url:
            try:
                foto = _descargar_imagen(url)
            except Exception as e:
                log.warning("No se pudo descargar foto de %s: %s", vehiculo.get("id"), e)
        px, py = posiciones[i]
        _dibujar_tarjeta(base, px, py, celda_w, celda_h, vehiculo, foto)

    # --- Banda de promocion (inferior) ---
    py0 = LIENZO - promo_h
    draw.rectangle([0, py0, LIENZO, LIENZO], fill=COL_ACENTO)

    # Texto de promo (envuelto en 2 lineas)
    f_promo = _font("BigShoulders-Bold.ttf", 34)
    _texto_centrado_multilinea(
        draw, PROMO_TEXTO, f_promo, LIENZO, py0 + 24, COL_TEXTO, max_ancho=LIENZO - 80
    )

    # WhatsApp
    f_wa = _font("WorkSans-Bold.ttf", 26)
    wa_txt = f"WhatsApp  {WHATSAPP_DISPLAY}"
    w = draw.textlength(wa_txt, font=f_wa)
    draw.text(((LIENZO - w) // 2, py0 + promo_h - 40), wa_txt, font=f_wa, fill=COL_TEXTO)

    return base


def _texto_centrado_multilinea(draw, texto, font, ancho_lienzo, y, color, max_ancho):
    """Dibuja texto centrado, partiendolo en varias lineas si no cabe."""
    palabras = texto.split()
    lineas, actual = [], ""
    for p in palabras:
        prueba = (actual + " " + p).strip()
        if draw.textlength(prueba, font=font) <= max_ancho:
            actual = prueba
        else:
            if actual:
                lineas.append(actual)
            actual = p
    if actual:
        lineas.append(actual)

    alto_linea = font.size + 6
    for i, linea in enumerate(lineas):
        w = draw.textlength(linea, font=font)
        draw.text(((ancho_lienzo - w) // 2, y + i * alto_linea), linea, font=font, fill=color)


# --------------------------------------------------------------------------
# ORQUESTADOR
# --------------------------------------------------------------------------

# Cache en RAM de los collages generados (bytes PNG)
_collages_cache = {"imagenes": [], "info": []}


def generar_todos_los_collages():
    """
    Lee el inventario, agrupa los vehiculos disponibles de 4 en 4 y genera
    un collage por grupo. Deja los PNG en _collages_cache y devuelve un resumen.
    """
    inventario, _, _ = leer_inventario()

    disponibles = [
        v for v in inventario
        if v.get("estado", "").strip().lower() not in ESTADOS_OCULTOS
        and v.get("id", "").strip()
    ]

    if not disponibles:
        return {"ok": True, "mensaje": "No hay vehiculos disponibles", "collages": 0}

    grupos = [
        disponibles[i:i + VEHICULOS_POR_COLLAGE]
        for i in range(0, len(disponibles), VEHICULOS_POR_COLLAGE)
    ]
    total = len(grupos)

    logo = _cargar_logo()

    _collages_cache["imagenes"] = []
    _collages_cache["info"] = []

    for idx, grupo in enumerate(grupos, start=1):
        img = _construir_collage(grupo, logo, idx, total)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _collages_cache["imagenes"].append(buf.getvalue())
        _collages_cache["info"].append({
            "indice": idx,
            "vehiculos": [
                f"{v.get('marca','')} {v.get('modelo','')} {v.get('anio','')}".strip()
                for v in grupo
            ],
        })

    return {
        "ok": True,
        "collages_generados": total,
        "vehiculos_totales": len(disponibles),
        "detalle": _collages_cache["info"],
    }


# --------------------------------------------------------------------------
# ENDPOINTS FLASK
# --------------------------------------------------------------------------

def registrar_rutas_collage(app):
    """
    Registra los endpoints de collages. En app.py:

        from collage_generator import registrar_rutas_collage
        registrar_rutas_collage(app)
    """
    from flask import request, jsonify, Response, url_for

    def _token_valido():
        esperado = os.environ.get("FB_PUBLISH_TOKEN", "")
        return esperado and request.args.get("token") == esperado

    @app.route("/debug-collages")
    def _debug_collages():
        """Muestra que vehiculos entrarian en cada collage, sin generar imagenes."""
        if not _token_valido():
            return jsonify({"error": "no autorizado"}), 403
        try:
            inventario, _, _ = leer_inventario()
            disponibles = [
                v for v in inventario
                if v.get("estado", "").strip().lower() not in ESTADOS_OCULTOS
                and v.get("id", "").strip()
            ]
            grupos = [
                [f"{v.get('marca','')} {v.get('modelo','')} {v.get('anio','')}".strip()
                 for v in disponibles[i:i + VEHICULOS_POR_COLLAGE]]
                for i in range(0, len(disponibles), VEHICULOS_POR_COLLAGE)
            ]
            return jsonify({
                "ok": True,
                "vehiculos_disponibles": len(disponibles),
                "collages_que_se_generarian": len(grupos),
                "grupos": grupos,
            })
        except Exception as e:
            log.exception("Error en debug-collages")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/generar-collages")
    def _generar_collages():
        """Genera todas las imagenes y devuelve links para descargarlas."""
        if not _token_valido():
            return jsonify({"error": "no autorizado"}), 403
        try:
            resumen = generar_todos_los_collages()
            token = request.args.get("token", "")
            base = request.host_url.rstrip("/")
            resumen["descargas"] = [
                f"{base}/collage/{i}?token={token}"
                for i in range(len(_collages_cache["imagenes"]))
            ]
            return jsonify(resumen)
        except Exception as e:
            log.exception("Error generando collages")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/collage/<int:indice>")
    def _ver_collage(indice):
        """Devuelve la imagen PNG N ya generada."""
        if not _token_valido():
            return jsonify({"error": "no autorizado"}), 403
        imagenes = _collages_cache["imagenes"]
        if indice < 0 or indice >= len(imagenes):
            return jsonify({"error": "indice fuera de rango. Corre /generar-collages primero."}), 404
        return Response(imagenes[indice], mimetype="image/png")
