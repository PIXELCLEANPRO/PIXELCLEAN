"""
Motores de reparacion de pixeles quemados. Cada motor tiene la misma firma:
    motor_x(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
            parametros, callback_progreso) -> (exito: bool, resultado_o_error: str)

La mascara (ruta_mascara_bn) es una imagen PNG en escala de grises, ya con
grosor/calado aplicados: 255 = zona a reparar, 0 = zona intacta.
"""
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
    return {
        "ancho": int(video_stream.get("width", 0)),
        "alto": int(video_stream.get("height", 0)),
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
    usar_nvenc = parametros.get("usar_nvenc", False)

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

    if usar_nvenc:
        base_codec = ["-c:v", "h264_nvenc", "-preset", "p4"]
    else:
        base_codec = ["-c:v", "libx264", "-preset", preset_info["preset"]]

    if modo_calidad == "bitrate" and bitrate_objetivo_bps:
        kbps = max(1000, int(bitrate_objetivo_bps / 1000))
        codec_args = base_codec + ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps*1.5)}k", "-bufsize", f"{kbps*2}k"]
    else:
        crf_arg = "-cq" if usar_nvenc else "-crf"
        codec_args = base_codec + [crf_arg, str(preset_info["crf"])]

    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", ruta_video, "-loop", "1", "-i", ruta_mascara_bn,
        "-filter_complex", filtro,
        "-map", etiqueta_salida, "-map", "0:a?",
        *codec_args, "-threads", "0", "-c:a", "copy", "-shortest",
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

    cmd_decode = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", ruta_video, "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    codec_args = ["-c:v", "libx264", "-preset", preset_info["preset"]]
    if modo_calidad == "bitrate" and bitrate_objetivo_bps:
        kbps = max(1000, int(bitrate_objetivo_bps / 1000))
        codec_args += ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps*1.5)}k", "-bufsize", f"{kbps*2}k"]
    else:
        codec_args += ["-crf", str(preset_info["crf"])]

    cmd_encode = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ancho}x{alto}", "-r", str(fps), "-i", "pipe:0",
        "-i", ruta_video,
        "-map", "0:v", "-map", "1:a?",
        *codec_args, "-threads", "0", "-c:a", "copy", "-shortest",
        ruta_salida,
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
        if os.path.isfile(ruta_salida):
            try:
                os.remove(ruta_salida)
            except OSError:
                pass
        return False, "Cancelado por el usuario."

    if proc_encode.returncode != 0 or not os.path.isfile(ruta_salida):
        return False, f"ffmpeg (encode) devolvio error (codigo {proc_encode.returncode}):\n{stderr_encode[-600:]}"

    callback_progreso(1.0)
    return True, ruta_salida


def motor_opencv_inpaint(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
                          parametros, callback_progreso):
    if cv2 is None:
        return False, "Falta instalar opencv-python."

    radio = parametros.get("radio_inpaint", 5)
    metodo = cv2.INPAINT_TELEA if parametros.get("metodo", "telea") == "telea" else cv2.INPAINT_NS

    def reparar_crop(crop_bgr, mascara_crop):
        return cv2.inpaint(crop_bgr, mascara_crop, radio, metodo)

    return _pipeline_por_frame(ffmpeg_bin, ffprobe_bin, ruta_video, ruta_mascara_bn, ruta_salida,
                                parametros, callback_progreso, reparar_crop)


MOTORES = {
    "blur": motor_blur,
    "opencv": motor_opencv_inpaint,
}
