"""
Deteccion automatica de pixel/cluster quemado comparando varios cuadros del
mismo clip: un pixel de sensor quemado no se mueve ni cambia de valor con el
tiempo, a diferencia del resto de la escena. Se buscan pixeles con muy poca
variacion temporal que ademas contrastan fuerte contra su entorno espacial
(para no marcar zonas uniformes como cielo o paredes, que tambien son
estables en el tiempo pero no son un defecto).
"""
import os
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageFilter

try:
    import cv2
except ImportError:
    cv2 = None

_SIN_VENTANA = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

PERCENTIL_ESTABILIDAD = 20   # el 20% de pixeles mas estables de la zona entran en la ronda
FACTOR_CONTRASTE = 1.3       # cuantos desvios por encima del contraste promedio hace falta para ser "anomalo"
PISO_CONTRASTE = 6.0         # minimo absoluto para no marcar nada si la zona es lisa/pareja
MIN_PIXELES = 2            # candidatos minimos para no devolver ruido como si fuera deteccion
AREA_MAXIMA_BLOB = 2500    # px^2: un punto/cluster quemado real es chico; descarta manchas grandes
FRACCION_MAXIMA_CUADRO = 0.015  # si los candidatos superan ~1.5% del cuadro, es ruido/patron, no un defecto


def _extraer_frames(ffmpeg_bin, ruta_video, duracion_seg, cantidad):
    margen = max((duracion_seg or 3) * 0.1, 0.3)
    inicio = margen
    fin = max((duracion_seg or 3) - margen, inicio + 0.1)
    posiciones = np.linspace(inicio, fin, cantidad) if duracion_seg else [0.5] * cantidad

    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, seg in enumerate(posiciones):
            ruta_tmp = os.path.join(tmp, f"f{i}.png")
            subprocess.run(
                [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", str(float(seg)), "-i", ruta_video, "-frames:v", "1", ruta_tmp],
                capture_output=True, timeout=30, creationflags=_SIN_VENTANA,
            )
            if os.path.isfile(ruta_tmp):
                frames.append(np.asarray(Image.open(ruta_tmp).convert("RGB")))
    return frames


def _mapa_anomalias(frames):
    """A partir de una lista de cuadros del mismo tamano (numpy HxWx3), devuelve
    el booleano (H,W) de pixeles que son estables en el tiempo Y contrastan
    fuerte contra su entorno -- sin asumir ningun color en particular, sirve
    igual para un defecto rojo, verde, azul o cualquier otro.

    Los umbrales son relativos a la propia zona/clip (percentiles y desvios
    sobre la distribucion real), no numeros fijos: un numero fijo calibrado
    sin ruido de compresion terminaba rechazando pixeles quemados reales en
    video comprimido, donde hasta un pixel "fijo" del sensor varia un poco
    entre cuadros por el codec."""
    pila = np.stack(frames, axis=0).astype(np.float32)  # (N, H, W, 3)

    # 1) estabilidad temporal: un pixel quemado cambia mucho menos que el resto
    # de la escena -- se queda con el percentil mas estable de la zona misma,
    # en vez de pedir un desvio absoluto que no sabe cuanto ruido de compresion hay.
    desvio_temporal = pila.std(axis=0).mean(axis=-1)  # (H, W)
    umbral_estab = max(np.percentile(desvio_temporal, PERCENTIL_ESTABILIDAD), 1.5)
    estable = desvio_temporal <= umbral_estab

    # 2) contraste espacial contra el entorno (evita marcar cielos/paredes uniformes,
    # que tambien son estables en el tiempo pero no resaltan contra si mismas).
    # Umbral adaptativo: promedio + N desvios de la propia zona, con un piso minimo.
    contraste_acumulado = np.zeros(pila.shape[1:3], dtype=np.float32)
    for frame in frames:
        suavizado = np.asarray(Image.fromarray(frame).filter(ImageFilter.GaussianBlur(radius=6))).astype(np.float32)
        contraste_acumulado += np.abs(frame.astype(np.float32) - suavizado).mean(axis=-1)
    contraste_promedio = contraste_acumulado / len(frames)
    umbral_contraste = max(
        contraste_promedio.mean() + FACTOR_CONTRASTE * contraste_promedio.std(),
        PISO_CONTRASTE,
    )
    anomalo = contraste_promedio >= umbral_contraste

    return estable & anomalo


def detectar_mascara_automatica(ffmpeg_bin, ruta_video, duracion_seg, cantidad_frames=5):
    """Devuelve (ok: bool, mascara_rgba_uint8_o_None, mensaje: str)."""
    frames = _extraer_frames(ffmpeg_bin, ruta_video, duracion_seg, cantidad_frames)
    if len(frames) < 3:
        return False, None, "No se pudieron extraer suficientes cuadros del clip para comparar."

    tamanos = {f.shape[:2] for f in frames}
    if len(tamanos) > 1:
        alto, ancho = min(tamanos)
        frames = [np.asarray(Image.fromarray(f).resize((ancho, alto))) for f in frames]

    candidato = _mapa_anomalias(frames)
    total_px = candidato.size
    if int(candidato.sum()) < MIN_PIXELES:
        return False, None, "No se detecto ningun punto quemado claro en este clip. Marcalo a mano en el paso Mascara."
    if candidato.sum() / total_px > FRACCION_MAXIMA_CUADRO:
        # cubre demasiado del cuadro para ser un defecto puntual real (patron de
        # prueba, escena con mucho contraste, etc.) -- mejor no adivinar
        return False, None, "El clip tiene demasiado contraste/patron para detectar un punto puntual solo. Marcalo a mano en el paso Mascara."

    candidato_filtrado = _filtrar_manchas_chicas(candidato)
    if candidato_filtrado is None or int(candidato_filtrado.sum()) < MIN_PIXELES:
        return False, None, "No se detecto ningun punto quemado claro en este clip. Marcalo a mano en el paso Mascara."

    # dilata un par de pixeles para dar margen, igual que un trazo de pincel minimo
    mascara_l = Image.fromarray((candidato_filtrado * 255).astype(np.uint8), mode="L").filter(ImageFilter.MaxFilter(5))
    dilatado = np.asarray(mascara_l) > 0

    alto, ancho = dilatado.shape
    rgba = np.zeros((alto, ancho, 4), dtype=np.uint8)
    rgba[dilatado] = [58, 160, 255, 255]

    cantidad_px = int(dilatado.sum())
    return True, rgba, f"Detectado automaticamente: {cantidad_px} pixeles candidatos."


def refinar_seleccion_por_contraste(ffmpeg_bin, ruta_video, duracion_seg, bbox, cantidad_frames=5):
    """Pincel inteligente: el usuario ya marco mas o menos donde esta el defecto
    (bbox = x0,y0,x1,y1 en coordenadas del cuadro completo); esto se queda solo
    con los pixeles de esa zona que son anomalos de verdad (estables en el
    tiempo + contrastan contra su entorno), sin asumir ningun color.
    Devuelve (ok, mascara_rgba_del_tamano_del_bbox_o_None, mensaje)."""
    x0, y0, x1, y1 = bbox
    if x1 - x0 < 2 or y1 - y0 < 2:
        return False, None, "Marca una zona un poco mas grande."

    # padding para que el calculo de contraste tenga entorno real alrededor del
    # borde del pincelazo (si no, los bordes del bbox se leen como "contraste").
    pad = 20
    frames_completos = _extraer_frames(ffmpeg_bin, ruta_video, duracion_seg, cantidad_frames)
    if len(frames_completos) < 3:
        return False, None, "No se pudieron extraer suficientes cuadros del clip para comparar."

    tamanos = {f.shape[:2] for f in frames_completos}
    if len(tamanos) > 1:
        alto_min, ancho_min = min(tamanos)
        frames_completos = [np.asarray(Image.fromarray(f).resize((ancho_min, alto_min))) for f in frames_completos]
    alto_frame, ancho_frame = frames_completos[0].shape[:2]

    xp0, yp0 = max(x0 - pad, 0), max(y0 - pad, 0)
    xp1, yp1 = min(x1 + pad, ancho_frame), min(y1 + pad, alto_frame)
    recortes = [f[yp0:yp1, xp0:xp1] for f in frames_completos]

    candidato_recorte = _mapa_anomalias(recortes)
    # vuelve a coordenadas del recorte sin el padding (el pincelazo original)
    ry0, ry1 = y0 - yp0, y1 - yp0
    rx0, rx1 = x0 - xp0, x1 - xp0
    candidato = candidato_recorte[ry0:ry1, rx0:rx1]

    if int(candidato.sum()) < MIN_PIXELES:
        return False, None, "No se encontro ningun pixel anomalo claro en esa zona. Probá marcar mas ajustado al defecto."

    mascara_l = Image.fromarray((candidato * 255).astype(np.uint8), mode="L").filter(ImageFilter.MaxFilter(3))
    dilatado = np.asarray(mascara_l) > 0

    alto, ancho = dilatado.shape
    rgba = np.zeros((alto, ancho, 4), dtype=np.uint8)
    rgba[dilatado] = [58, 160, 255, 255]
    cantidad_px = int(dilatado.sum())
    return True, rgba, f"Seleccion afinada: {cantidad_px} pixeles."


def _filtrar_manchas_chicas(candidato):
    """Se queda solo con manchas conectadas chicas y compactas (un defecto de
    sensor real es puntual): descarta regiones grandes que un patron de prueba
    o una escena de alto contraste puedan generar por error."""
    if cv2 is None:
        return candidato  # sin opencv no se puede filtrar por componentes; se usa tal cual
    cantidad, etiquetas, stats, _ = cv2.connectedComponentsWithStats(candidato.astype(np.uint8), connectivity=8)
    resultado = np.zeros_like(candidato, dtype=bool)
    for i in range(1, cantidad):  # etiqueta 0 es el fondo
        area = stats[i, cv2.CC_STAT_AREA]
        if area <= AREA_MAXIMA_BLOB:
            resultado |= (etiquetas == i)
    return resultado if resultado.any() else None
