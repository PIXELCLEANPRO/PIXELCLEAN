"""
Motores de reparacion de pixeles quemados. Cada motor tiene la misma firma:
    motor_x(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
            parametros, callback_progreso) -> (exito: bool, resultado_o_error: str)

La mascara (ruta_mascara_bn) es una imagen PNG en escala de grises, ya con
grosor/calado aplicados: 255 = zona a reparar, 0 = zona intacta.
"""
import functools
import json
import os
import subprocess

import numpy as np
from PIL import Image

try:
    import cv2
except ImportError:
    cv2 = None

_SIN_VENTANA = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Candidatos de encoder por hardware, en orden de preferencia. Cada uno se
# prueba de verdad (no alcanza con que ffmpeg lo liste como compilado -- eso
# no garantiza que el driver/la placa realmente lo soporten).
_CANDIDATOS_GPU = [
    ("nvenc", "h264_nvenc", ["-preset", "p4"]),
    ("qsv", "h264_qsv", []),
    ("amf", "h264_amf", ["-quality", "speed"]),
    ("videotoolbox", "h264_videotoolbox", []),  # Mac (Apple Silicon e Intel)
]
_cache_deteccion_gpu = {}


def _listar_encoders(ffmpeg_bin):
    try:
        r = subprocess.run([ffmpeg_bin, "-hide_banner", "-encoders"], capture_output=True,
                            text=True, timeout=10, creationflags=_SIN_VENTANA)
        return r.stdout
    except Exception:
        return ""


def _probar_encoder(ffmpeg_bin, codec_name, extra_args):
    try:
        cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
               "-frames:v", "1", "-c:v", codec_name, *extra_args, "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=10, creationflags=_SIN_VENTANA)
        return r.returncode == 0
    except Exception:
        return False


def detectar_motor_gpu(ffmpeg_bin):
    """Devuelve 'nvenc', 'qsv', 'amf', 'videotoolbox' o None (sin GPU utilizable -> CPU).
    Prueba cada encoder con un mini-render de verdad, no solo si ffmpeg lo
    lista como compilado. El resultado se cachea por ejecutable de ffmpeg
    para no repetir la prueba en cada clip."""
    if ffmpeg_bin in _cache_deteccion_gpu:
        return _cache_deteccion_gpu[ffmpeg_bin]
    listado = _listar_encoders(ffmpeg_bin)
    resultado = None
    for nombre, codec, extra in _CANDIDATOS_GPU:
        if codec in listado and _probar_encoder(ffmpeg_bin, codec, extra):
            resultado = nombre
            break
    _cache_deteccion_gpu[ffmpeg_bin] = resultado
    return resultado


def _codec_args(motor_gpu, preset_info, modo_calidad, bitrate_objetivo_bps):
    """Arma los argumentos de codec de ffmpeg para el encoder detectado
    (GPU) o libx264 (CPU) como ultimo recurso, en el modo de calidad
    (bitrate fijo o CRF/calidad) que haya elegido el usuario."""
    if motor_gpu == "nvenc":
        base = ["-c:v", "h264_nvenc", "-preset", "p4"]
    elif motor_gpu == "qsv":
        base = ["-c:v", "h264_qsv"]
    elif motor_gpu == "amf":
        base = ["-c:v", "h264_amf", "-quality", "speed"]
    elif motor_gpu == "videotoolbox":
        base = ["-c:v", "h264_videotoolbox"]
    else:
        base = ["-c:v", "libx264", "-preset", preset_info["preset"]]

    if modo_calidad == "bitrate" and bitrate_objetivo_bps:
        kbps = max(1000, int(bitrate_objetivo_bps / 1000))
        return base + ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps*1.5)}k", "-bufsize", f"{kbps*2}k"]

    crf = str(preset_info["crf"])
    if motor_gpu == "nvenc":
        return base + ["-cq", crf]
    if motor_gpu == "qsv":
        return base + ["-global_quality", crf]
    if motor_gpu == "amf":
        return base + ["-qp_i", crf, "-qp_p", crf]
    if motor_gpu == "videotoolbox":
        # h264_videotoolbox no soporta CRF -- usa -q:v (escala 1-100,
        # mayor = mejor calidad), asi que se invierte el numero de CRF
        # (mas bajo = mejor) a esa escala en vez de pasarlo directo.
        return base + ["-q:v", str(max(1, 100 - int(crf) * 2))]
    return base + ["-crf", crf]


def _rotacion_normalizada(video_stream):
    """Angulo (0/90/180/270) que ffmpeg aplica solo al decodificar este
    stream, por el tag "rotate" clasico o el "Display Matrix" side-data
    que usan camaras como la Sony A7III al filmar en vertical (el archivo
    queda codificado "acostado" con una bandera de rotacion). ffmpeg
    autorota al decodificar, asi que el ancho/alto real -- como se ve, y
    como sale del pipe de rawvideo -- puede ser el inverso del que
    reporta el stream si no se corrige esto."""
    rotate_tag = video_stream.get("tags", {}).get("rotate")
    if rotate_tag is not None:
        try:
            return int(rotate_tag) % 360
        except (TypeError, ValueError):
            pass
    for sd in video_stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(sd["rotation"]) % 360
            except (TypeError, ValueError):
                pass
    return 0


def _info_basica(ffprobe_bin, ruta_video):
    cmd = [
        ffprobe_bin, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", ruta_video,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=_SIN_VENTANA)
    datos = json.loads(r.stdout)
    video_stream = next((s for s in datos.get("streams", []) if s.get("codec_type") == "video"), {})
    formato = datos.get("format", {})
    fps_txt = video_stream.get("r_frame_rate", "25/1")
    num, den = fps_txt.split("/")
    fps = float(num) / float(den) if float(den) else 25.0
    ancho = int(video_stream.get("width", 0))
    alto = int(video_stream.get("height", 0))
    if _rotacion_normalizada(video_stream) in (90, 270):
        ancho, alto = alto, ancho
    return {
        "ancho": ancho,
        "alto": alto,
        "fps": fps,
        "duracion_seg": float(formato.get("duration", 0)) or None,
        "tiene_audio": any(s.get("codec_type") == "audio" for s in datos.get("streams", [])),
    }


# ============================================================
# MOTOR 1: BLUR GAUSSIANO (via filtro de ffmpeg, todo el video de una)
# ============================================================
def motor_blur(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
                parametros, callback_progreso):
    sigma_blur = parametros.get("sigma_blur", 15)
    preset_info = parametros["preset_info"]
    modo_calidad = parametros.get("modo_calidad", "crf")
    bitrate_objetivo_bps = parametros.get("bitrate_objetivo_bps")
    resolucion_objetivo = parametros.get("resolucion_objetivo")
    motor_gpu = parametros.get("motor_gpu")

    filtro = (
        "[0:v]split=2[base][toblur];"
        f"[toblur]gblur=sigma={sigma_blur}[blurred];"
        "[base][blurred][1:v]maskedmerge[out]"
    )
    if resolucion_objetivo:
        w, h = resolucion_objetivo
        filtro += f";[out]scale={w}:{h}:force_original_aspect_ratio=decrease[outs]"
        etiqueta_salida = "[outs]"
    else:
        etiqueta_salida = "[out]"
    if motor_gpu:
        # Camaras profesionales (Sony XAVC, etc.) suelen grabar en 10-bit
        # 4:2:2 (yuv422p10le) -- los encoders de GPU de consumo (NVENC,
        # Quick Sync, AMF) no soportan eso por H.264, solo 8-bit 4:2:0, y
        # rechazan el encode entero si se les manda tal cual. libx264 (CPU)
        # si soporta alta profundidad de color, asi que esta conversion
        # solo se fuerza cuando se va a codificar por GPU.
        filtro += f";{etiqueta_salida}format=yuv420p[outgpu]"
        etiqueta_salida = "[outgpu]"

    codec_args = _codec_args(motor_gpu, preset_info, modo_calidad, bitrate_objetivo_bps)

    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", ruta_video, "-loop", "1", "-i", ruta_mascara_bn,
        "-filter_complex", filtro,
        "-map", etiqueta_salida, "-map", "0:a?",
        *codec_args, "-threads", "0", "-c:a", "aac", "-b:a", "192k", "-shortest",
        "-progress", "pipe:1", "-nostats",
        ruta_salida,
    ]
    evento_cancelar = parametros.get("evento_cancelar")
    return _correr_ffmpeg_con_progreso(cmd, ffprobe_bin, ruta_video, ruta_salida, callback_progreso, evento_cancelar)


def _correr_ffmpeg_con_progreso(cmd, ffprobe_bin, ruta_video, ruta_salida, callback_progreso, evento_cancelar=None):
    import re
    import threading
    duracion = _info_basica(ffprobe_bin, ruta_video)["duracion_seg"]
    try:
        proceso = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1, universal_newlines=True,
                                    creationflags=_SIN_VENTANA)
    except Exception as e:
        return False, f"No se pudo iniciar ffmpeg: {e}"

    stderr_lineas = []
    threading.Thread(target=lambda: [stderr_lineas.append(l.rstrip()) for l in proceso.stderr], daemon=True).start()
    patron = re.compile(r"out_time_ms=(\d+)")
    for linea in proceso.stdout:
        if evento_cancelar is not None and evento_cancelar.is_set():
            proceso.terminate()
            proceso.wait()
            if os.path.isfile(ruta_salida):
                try:
                    os.remove(ruta_salida)
                except OSError:
                    pass
            return False, "Cancelado por el usuario."
        m = patron.search(linea)
        if m and duracion:
            callback_progreso(min(int(m.group(1)) / 1_000_000 / duracion, 1.0))
        elif "progress=end" in linea:
            callback_progreso(1.0)
    proceso.wait()
    if proceso.returncode != 0 or not os.path.isfile(ruta_salida):
        detalle = "\n".join(stderr_lineas[-8:]) if stderr_lineas else "(sin detalle)"
        return False, f"ffmpeg devolvio error (codigo {proceso.returncode}):\n{detalle}"
    callback_progreso(1.0)
    return True, ruta_salida


# ============================================================
# MOTOR 2: OPENCV INPAINTING (Telea) - por frame, solo en el recorte de la mascara
# ============================================================
def _bbox_desde_mascara(mascara_arr, padding, ancho_frame, alto_frame):
    ys, xs = np.where(mascara_arr > 20)
    if len(xs) == 0:
        raise ValueError("La mascara no marca ninguna zona.")
    x0 = max(int(xs.min()) - padding, 0)
    x1 = min(int(xs.max()) + padding, ancho_frame)
    y0 = max(int(ys.min()) - padding, 0)
    y1 = min(int(ys.max()) + padding, alto_frame)
    return x0, y0, x1, y1


def _pipeline_por_frame(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
                         parametros, callback_progreso, funcion_reparar_crop):
    """Comparte extraccion/recorte/reconstruccion entre los motores que reparan
    frame a frame (OpenCV inpainting e IA local). Solo cambia como se repara
    el recorte: funcion_reparar_crop(crop_bgr, mascara_crop_u8) -> crop_reparado_bgr."""
    padding = parametros.get("zoom_padding", 40)
    preset_info = parametros["preset_info"]
    modo_calidad = parametros.get("modo_calidad", "crf")
    bitrate_objetivo_bps = parametros.get("bitrate_objetivo_bps")
    evento_cancelar = parametros.get("evento_cancelar")

    info = _info_basica(ffprobe_bin, ruta_video)
    ancho, alto, fps, duracion = info["ancho"], info["alto"], info["fps"], info["duracion_seg"]
    if not ancho or not alto:
        return False, "No se pudo leer la resolucion del video."

    mascara_img = Image.open(ruta_mascara_bn).convert("L").resize((ancho, alto))
    mascara_arr = np.asarray(mascara_img)
    try:
        x0, y0, x1, y1 = _bbox_desde_mascara(mascara_arr, padding, ancho, alto)
    except ValueError as e:
        return False, str(e)
    mascara_recorte = mascara_arr[y0:y1, x0:x1]

    # Se probo tambien decodificar con NVDEC (-hwaccel cuda) ademas de
    # codificar con GPU. Medido: no ayuda -- el frame igual tiene que bajar
    # a memoria de sistema para pasar por el pipe hacia python, asi que no se
    # ahorra nada del lado de python, y el frame decodificado no se reusa en
    # la GPU para nada mas. Solo queda el encode en GPU, que es donde si se
    # midio una mejora real (~3x mas rapido que libx264 en la misma maquina).
    cmd_decode = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", ruta_video, "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    motor_gpu = parametros.get("motor_gpu")
    codec_args = _codec_args(motor_gpu, preset_info, modo_calidad, bitrate_objetivo_bps)

    # El video se codifica primero SIN audio, a un archivo temporal. El audio
    # se agrega despues en un segundo paso (remux, ver _remuxear_con_audio),
    # en vez de mandarlo todo junto con "-shortest" en el mismo comando --
    # con "-shortest" el video (que viene de un pipe sin duracion conocida de
    # antemano) y el audio (que si tiene una duracion real, del archivo
    # original) no se reconciliaban bien y el video terminaba mas largo que
    # el audio. Haciendolo en dos pasos, para cuando se junta el audio el
    # video temporal ya es un archivo real con duracion conocida, y ahi
    # "-shortest" corta parejo de verdad.
    ruta_video_tmp = ruta_salida + ".video_tmp.mp4"
    cmd_encode = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ancho}x{alto}", "-r", str(fps), "-i", "pipe:0",
        *codec_args, "-threads", "0",
        ruta_video_tmp,
    ]

    try:
        proc_decode = subprocess.Popen(cmd_decode, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        creationflags=_SIN_VENTANA)
        proc_encode = subprocess.Popen(cmd_encode, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=_SIN_VENTANA)
    except Exception as e:
        return False, f"No se pudo iniciar ffmpeg: {e}"

    tam_frame = ancho * alto * 3
    total_frames_estimado = int(duracion * fps) if duracion else None
    frames_leidos = 0

    # Se probo repartir el recorte+inpaint de cada frame entre varios procesos
    # (ProcessPoolExecutor). Medido contra un clip real: fue mas LENTO, no mas
    # rapido -- el crop que se repara es chico (solo la zona de la mascara),
    # asi que cv2.inpaint ya es barato por frame, y el costo de arrancar los
    # procesos + mandar cada frame de un lado al otro (pickling) termina
    # pesando mas que lo que se gana paralelizando. Se descarto esa idea.
    cancelado = False
    try:
        while True:
            if evento_cancelar is not None and evento_cancelar.is_set():
                cancelado = True
                break
            crudo = proc_decode.stdout.read(tam_frame)
            if len(crudo) < tam_frame:
                break
            frame = np.frombuffer(crudo, dtype=np.uint8).reshape((alto, ancho, 3)).copy()
            recorte = frame[y0:y1, x0:x1]
            reparado = funcion_reparar_crop(recorte, mascara_recorte)
            frame[y0:y1, x0:x1] = reparado
            proc_encode.stdin.write(frame.tobytes())
            frames_leidos += 1
            if total_frames_estimado:
                callback_progreso(min(frames_leidos / total_frames_estimado, 1.0))
    except BrokenPipeError:
        pass
    finally:
        proc_decode.terminate()
        proc_decode.stdout.close()
        proc_decode.wait()
        if cancelado:
            proc_encode.kill()
            proc_encode.stderr.close()
            stderr_encode = ""
        else:
            proc_encode.stdin.close()
            stderr_encode = proc_encode.stderr.read().decode(errors="ignore")
        proc_encode.wait()

    if cancelado:
        if os.path.isfile(ruta_video_tmp):
            try:
                os.remove(ruta_video_tmp)
            except OSError:
                pass
        return False, "Cancelado por el usuario."

    if proc_encode.returncode != 0 or not os.path.isfile(ruta_video_tmp):
        return False, f"ffmpeg (encode) devolvio error (codigo {proc_encode.returncode}):\n{stderr_encode[-600:]}"

    callback_progreso(1.0)
    ok_remux, error_remux = _remuxear_con_audio(ffmpeg_bin, ffprobe_bin, ruta_video_tmp, ruta_video, ruta_salida, duracion)
    try:
        os.remove(ruta_video_tmp)
    except OSError:
        pass
    if not ok_remux:
        return False, f"No se pudo agregar el audio original:\n{error_remux[-600:]}"
    return True, ruta_salida


def _remuxear_con_audio(ffmpeg_bin, ffprobe_bin, ruta_video_sin_audio, ruta_video_original, ruta_salida, duracion_original):
    """Junta el video ya codificado (sin audio) con el audio del clip
    original, en un archivo aparte que ya tiene duracion real y conocida --
    ver el comentario en _pipeline_por_frame de por que no se hace todo en
    un solo paso. El video se copia tal cual (ya esta codificado, no hace
    falta re-codificarlo) y solo el audio se re-codifica a AAC (compatible
    con MP4/MOV; el audio original de camara suele ser PCM sin comprimir).

    Reconstruir el video cuadro a cuadro a un framerate fijo nominal (ver
    _pipeline_por_frame) puede quedar con una duracion total levemente
    distinta a la del clip original (decimales del framerate real, algun
    cuadro de mas o de menos al decodificar), y esa pequeña diferencia se
    va notando cada vez mas cuanto mas largo es el clip -- terminaba
    desfasando bastante el audio en clips largos. Antes de mezclar el
    audio, se mide la duracion real del video ya codificado contra la del
    original y, si no coinciden, se corrige con "-itsscale": estira o
    encoge levemente la linea de tiempo del video (una fraccion de
    porcentaje, imperceptible) para que la duracion final quede exacta."""
    duracion_tmp = _info_basica(ffprobe_bin, ruta_video_sin_audio)["duracion_seg"]
    itsscale_args = []
    if duracion_tmp and duracion_original and duracion_tmp > 0:
        factor = duracion_original / duracion_tmp
        if abs(factor - 1) > 0.0005:
            itsscale_args = ["-itsscale", f"{factor:.6f}"]
    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        *itsscale_args, "-i", ruta_video_sin_audio, "-i", ruta_video_original,
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
        ruta_salida,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, creationflags=_SIN_VENTANA)
    except Exception as e:
        return False, str(e)
    if r.returncode != 0 or not os.path.isfile(ruta_salida):
        return False, r.stderr.decode(errors="ignore")
    return True, ""


def _reparar_crop_opencv(crop_bgr, mascara_crop, radio, metodo_str):
    metodo = cv2.INPAINT_TELEA if metodo_str == "telea" else cv2.INPAINT_NS
    return cv2.inpaint(crop_bgr, mascara_crop, radio, metodo)


def motor_opencv_inpaint(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
                          parametros, callback_progreso):
    if cv2 is None:
        return False, "Falta instalar opencv-python."

    radio = parametros.get("radio_inpaint", 5)
    metodo_str = parametros.get("metodo", "telea")
    reparar_crop = functools.partial(_reparar_crop_opencv, radio=radio, metodo_str=metodo_str)

    return _pipeline_por_frame(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
                                parametros, callback_progreso, reparar_crop)


MOTORES = {
    "blur": motor_blur,
    "opencv": motor_opencv_inpaint,
}
