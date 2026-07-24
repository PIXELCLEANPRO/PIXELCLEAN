"""
Lectura de metadata de camara embebida en el archivo de video via ffprobe.
Muestra solo lo que el archivo realmente tiene: no inventa ni completa datos.
"""
import json
import re
import subprocess


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


def leer_metadata_camara(ffprobe_bin, ruta_video):
    """Devuelve un dict con los campos detectados (solo los presentes) y 'marca' para el icono.
    No lanza excepcion si no encuentra nada: devuelve un dict con todo en None salvo que ffprobe falle."""
    cmd = [
        ffprobe_bin, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", ruta_video,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
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

    return {
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
        "fps": fps,
        "codec_video": video_stream.get("codec_long_name") or video_stream.get("codec_name"),
        "pixel_format": video_stream.get("pix_fmt"),
        "bitrate": bitrate,
        "peso": peso,
        "contenedor": formato.get("format_long_name") or formato.get("format_name"),
        "codec_audio": audio_stream.get("codec_long_name") or audio_stream.get("codec_name"),
        "audio_canales": audio_stream.get("channels"),
        "audio_sample_rate": (f"{int(audio_stream['sample_rate'])/1000:g} kHz" if audio_stream.get("sample_rate") else None),
        "tags_crudos": tags_crudos,
    }
