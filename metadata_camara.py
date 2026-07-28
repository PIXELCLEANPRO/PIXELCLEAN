"""
Lectura de metadata de camara embebida en el archivo de video via ffprobe.
Muestra solo lo que el archivo realmente tiene: no inventa ni completa datos.
"""
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import motores_reparacion as _motores

_SIN_VENTANA = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


MARCAS_CONOCIDAS = {
    "sony": "Sony",
    "ilce": "Sony",
    "canon": "Canon",
    "eos": "Canon",
    "nikon": "Nikon",
    "panasonic": "Panasonic",
    "lumix": "Panasonic",
    "fujifilm": "Fujifilm",
    "fuji": "Fujifilm",
    "blackmagic": "Blackmagic",
    "gopro": "GoPro",
    "dji": "DJI",
    "apple": "Apple",
    "iphone": "Apple",
    "red digital": "RED",
}

# claves de tags (en minuscula, sin puntos ni guiones) donde suele aparecer cada dato
CLAVES_MODELO = ["model", "cameramodelname", "quicktimemodel", "device.model", "com.apple.quicktime.model"]
CLAVES_MARCA = ["make", "manufacturer", "quicktimemake", "com.apple.quicktime.make"]
CLAVES_LENTE = ["lens", "lensmodel", "lensinfo"]
CLAVES_ISO = ["iso", "isospeedratings", "exif:iso"]
CLAVES_PERFIL_COLOR = ["pictureprofile", "picture_profile", "colorprofile", "gamma", "colortransfer"]
CLAVES_ESPACIO_COLOR = ["colorspace", "colorprimaries", "color_space"]
CLAVES_TIMECODE = ["timecode"]
CLAVES_FECHA = ["creation_time", "date", "com.apple.quicktime.creationdate"]

# solo estas claves se tratan como texto libre para buscar patrones tipo "ISO 800" o "Lens: X".
# los tags tecnicos del contenedor (major_brand, compatible_brands, encoder, etc.) quedan afuera
# para no generar falsos positivos (ej. "isom" siendo confundido con "ISO").
CLAVES_TEXTO_LIBRE = ["comment", "description", "notes", "subject"]


def _normalizar_clave(clave):
    return re.sub(r"[^a-z0-9]", "", clave.lower())


def _buscar_en_tags(tags_normalizados, claves_candidatas):
    for candidata in claves_candidatas:
        candidata_norm = _normalizar_clave(candidata)
        if candidata_norm in tags_normalizados:
            return tags_normalizados[candidata_norm]
    return None


def _buscar_por_patron(texto, patron):
    if not texto:
        return None
    m = re.search(patron, texto, re.IGNORECASE)
    return m.group(1).strip() if m else None


def detectar_marca(modelo, marca_tag, todos_los_tags_texto):
    fuente = " ".join(filter(None, [marca_tag, modelo, todos_los_tags_texto])).lower()
    for clave, nombre_bonito in MARCAS_CONOCIDAS.items():
        if clave in fuente:
            return nombre_bonito
    return None


def _tag_local(elemento):
    return elemento.tag.split("}", 1)[-1] if "}" in elemento.tag else elemento.tag


def _ruta_xml_sidecar(ruta_video):
    """Las camaras Sony (formato XAVC) suelen dejar un .XML de metadatos al
    lado de cada clip, con el mismo nombre de archivo mas "M01" (ej.
    C5727M01.XML junto a C5727.MP4). Si no esta, no pasa nada -- se
    devuelve None y el resto del flujo simplemente no agrega esa info."""
    base, _ = os.path.splitext(ruta_video)
    for candidato in (base + "M01.XML", base + "M01.xml", base + ".XML", base + ".xml"):
        if os.path.isfile(candidato):
            return candidato
    return None


def leer_metadata_xml_sony(ruta_video):
    """Lee el .XML de metadatos "NonRealTimeMeta" que graban las camaras
    Sony junto al clip (formato XAVC). Devuelve None si no existe el
    archivo o no se pudo interpretar -- nunca lanza excepcion."""
    ruta_xml = _ruta_xml_sidecar(ruta_video)
    if not ruta_xml:
        return None
    try:
        raiz = ET.parse(ruta_xml).getroot()
    except Exception:
        return None

    datos = {}
    for elem in raiz.iter():
        nombre = _tag_local(elem)
        if nombre == "Device":
            datos["marca"] = elem.get("manufacturer")
            datos["modelo"] = elem.get("modelName")
        elif nombre == "CreationDate":
            datos["fecha"] = elem.get("value")
        elif nombre == "VideoFrame":
            datos["fps_captura"] = elem.get("captureFps")
            datos["codec"] = elem.get("videoCodec")
        elif nombre == "Duration":
            valor = elem.get("value")
            if valor and valor.isdigit():
                datos["fotogramas"] = int(valor)
        elif nombre == "Item" and elem.get("name") == "CaptureGammaEquation":
            datos["perfil_color"] = elem.get("value")
    return datos or None


def leer_metadata_camara(ffprobe_bin, ruta_video):
    """Devuelve un dict con los campos detectados (solo los presentes) y 'marca' para el icono.
    No lanza excepcion si no encuentra nada: devuelve un dict con todo en None salvo que ffprobe falle."""
    cmd = [
        ffprobe_bin, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", ruta_video,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=_SIN_VENTANA)
    datos = json.loads(r.stdout)

    tags_crudos = {}
    tags_crudos.update(datos.get("format", {}).get("tags", {}) or {})
    for stream in datos.get("streams", []):
        tags_crudos.update(stream.get("tags", {}) or {})

    tags_normalizados = {_normalizar_clave(k): v for k, v in tags_crudos.items()}
    texto_libre = " | ".join(
        str(v) for k, v in tags_crudos.items() if _normalizar_clave(k) in {_normalizar_clave(c) for c in CLAVES_TEXTO_LIBRE}
    )

    modelo = _buscar_en_tags(tags_normalizados, CLAVES_MODELO)
    marca_tag = _buscar_en_tags(tags_normalizados, CLAVES_MARCA)
    lente = _buscar_en_tags(tags_normalizados, CLAVES_LENTE) or _buscar_por_patron(texto_libre, r"lens:?\s*([^,|]+)")
    iso = _buscar_en_tags(tags_normalizados, CLAVES_ISO) or _buscar_por_patron(texto_libre, r"\bISO\s*:?\s*(\d{2,6})\b")
    perfil_color = _buscar_en_tags(tags_normalizados, CLAVES_PERFIL_COLOR) or _buscar_por_patron(
        texto_libre, r"picture\s*profile:?\s*([^,|]+)"
    )
    espacio_color = _buscar_en_tags(tags_normalizados, CLAVES_ESPACIO_COLOR)
    timecode = _buscar_en_tags(tags_normalizados, CLAVES_TIMECODE)
    fecha = _buscar_en_tags(tags_normalizados, CLAVES_FECHA)

    texto_para_marca = " | ".join(f"{k}: {v}" for k, v in tags_crudos.items())
    marca = detectar_marca(modelo, marca_tag, texto_para_marca)

    formato = datos.get("format", {})
    video_stream = next((s for s in datos.get("streams", []) if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in datos.get("streams", []) if s.get("codec_type") == "audio"), {})

    ancho, alto = video_stream.get("width"), video_stream.get("height")
    if ancho and alto and _motores._rotacion_normalizada(video_stream) in (90, 270):
        ancho, alto = alto, ancho  # camara filmada en vertical (ver motores_reparacion._info_basica)
    resolucion = f"{ancho}x{alto}" if ancho and alto else None

    fps = None
    r_frame_rate = video_stream.get("r_frame_rate")
    if r_frame_rate and "/" in r_frame_rate:
        num, den = r_frame_rate.split("/")
        if float(den or 0):
            fps = round(float(num) / float(den), 2)

    duracion_seg = formato.get("duration") or video_stream.get("duration")
    duracion = None
    if duracion_seg:
        segundos_totales = int(float(duracion_seg))
        m, s = divmod(segundos_totales, 60)
        h, m = divmod(m, 60)
        duracion = f"{h:02d}:{m:02d}:{s:02d}"

    bitrate_bps = formato.get("bit_rate") or video_stream.get("bit_rate")
    bitrate = f"{round(int(bitrate_bps) / 1_000_000, 1)} Mbps" if bitrate_bps else None

    peso_bytes = formato.get("size")
    peso = f"{round(int(peso_bytes) / (1024 * 1024))} MB" if peso_bytes else None

    nb_frames = video_stream.get("nb_frames")
    fotogramas = int(nb_frames) if nb_frames and str(nb_frames).isdigit() else None
    if fotogramas is None and duracion_seg and fps:
        fotogramas = round(float(duracion_seg) * fps)

    pixel_format = video_stream.get("pix_fmt") or ""
    m_bits = re.search(r"(\d+)(?:le|be)$", pixel_format)
    bits_color = f"{m_bits.group(1)}-bit" if m_bits else ("8-bit" if pixel_format else None)

    formato_contenedor = formato.get("format_long_name") or formato.get("format_name")

    # El .XML que dejan las camaras Sony al lado del clip (ver
    # leer_metadata_xml_sony) es mas confiable para marca/modelo/perfil de
    # color en formatos XAVC, donde ffprobe no siempre encuentra esos tags
    # dentro del propio video. Si no hay .XML, no cambia nada (sigue como
    # estaba, solo con lo que trae el propio archivo de video).
    info_xml = leer_metadata_xml_sony(ruta_video)
    if info_xml:
        marca = marca or info_xml.get("marca")
        modelo = modelo or info_xml.get("modelo")
        perfil_color = perfil_color or info_xml.get("perfil_color")
        if info_xml.get("fotogramas"):
            fotogramas = info_xml["fotogramas"]
        if info_xml.get("fecha"):
            # el CreationDate del XML es el inicio real de la grabacion; el
            # creation_time que a veces trae el propio contenedor de video
            # puede reflejar otro momento (ej. cuando se termino de escribir
            # el archivo), asi que el XML tiene prioridad para este campo.
            fecha = info_xml["fecha"]

    return {
        "nombre_archivo": os.path.basename(ruta_video),
        "modelo": modelo,
        "marca": marca,
        "lente": lente,
        "iso": iso,
        "perfil_color": perfil_color,
        "espacio_color": espacio_color,
        "timecode": timecode,
        "fecha": fecha,
        "resolucion": resolucion,
        "duracion": duracion,
        "fotogramas": fotogramas,
        "bits_color": bits_color,
        "formato": formato_contenedor,
        "fps": fps,
        "codec_video": video_stream.get("codec_long_name") or video_stream.get("codec_name"),
        "pixel_format": video_stream.get("pix_fmt"),
        "bitrate": bitrate,
        "peso": peso,
        "contenedor": formato_contenedor,
        "codec_audio": audio_stream.get("codec_long_name") or audio_stream.get("codec_name"),
        "audio_canales": audio_stream.get("channels"),
        "audio_sample_rate": (f"{int(audio_stream['sample_rate'])/1000:g} kHz" if audio_stream.get("sample_rate") else None),
        "tags_crudos": tags_crudos,
    }
