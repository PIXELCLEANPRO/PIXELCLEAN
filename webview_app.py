"""
PixelClean - punto de entrada de la version con interfaz web (pywebview).
El backend (motores de reparacion, lectura de metadata) es el mismo que
usaba la version CustomTkinter; solo cambia la capa visual.
"""
import base64
import datetime
import hashlib
import io
import json
import logging
import mimetypes
import os
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import webview
from PIL import Image, ImageFilter

import deteccion_automatica
import metadata_camara
import motores_reparacion as motores
from webview.dom import DOMEventHandler

try:
    import cv2
except ImportError:
    cv2 = None


def _carpeta_base():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _ruta_local(nombre):
    candidato = os.path.join(_carpeta_base(), nombre)
    return candidato if os.path.isfile(candidato) else nombre


FFMPEG_BIN = _ruta_local("ffmpeg.exe") if os.name == "nt" else _ruta_local("ffmpeg")
FFPROBE_BIN = _ruta_local("ffprobe.exe") if os.name == "nt" else _ruta_local("ffprobe")
CARPETA_WEB = os.path.join(_carpeta_base(), "web")

GREEN_THRESHOLD = 40
OUTPUT_SUFFIX = "_reparado"

PRESETS_VELOCIDAD = {
    "Rapido":      {"preset": "ultrafast", "crf": 18},
    "Equilibrado": {"preset": "veryfast",  "crf": 16},
    "Calidad":     {"preset": "slow",      "crf": 12},
}
RESOLUCIONES = {
    "Original": None,
    "1080p (1920x1080)": (1920, 1080),
    "720p (1280x720)": (1280, 720),
}

# Hash SHA-256 de la clave Pro (nunca se guarda la clave en texto plano).
LICENCIA_HASH_PRO = "ab45d00dee70232649d49b004f952ecb6c0ae0a00663c15f5d7b4c2a3929d071"
LIMITE_GRATIS_DIARIO = 5

VERSION_APP = "1.8.0"
URL_ULTIMA_VERSION = "https://api.github.com/repos/PIXELCLEANPRO/PIXELCLEAN/releases/latest"
URL_PAGINA_DESCARGA = "https://pixelclean-app.netlify.app"


def _version_a_tupla(texto):
    limpio = (texto or "").strip().lstrip("vV")
    partes = []
    for trozo in limpio.split("."):
        digitos = "".join(c for c in trozo if c.isdigit())
        partes.append(int(digitos) if digitos else 0)
    return tuple(partes) or (0,)


def _carpeta_datos_app():
    if os.name == "nt":
        base = os.environ["LOCALAPPDATA"]
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))
    carpeta = os.path.join(base, "PixelClean")
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def _ruta_datos_usuario(nombre_archivo):
    return os.path.join(_carpeta_datos_app(), nombre_archivo)


def _carpeta_perfiles_usuario():
    # A diferencia del resto de los datos de la app (config tecnica, cache de
    # WebView2, etc.), los perfiles de camara son algo que el usuario arma a
    # mano y espera encontrar/hacer backup facil -- se guardan en Documentos
    # en vez de la carpeta tecnica escondida (%LOCALAPPDATA%), que a nadie se
    # le ocurre revisar.
    carpeta = os.path.join(os.path.expanduser("~"), "Documents", "PixelClean")
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def _ruta_perfiles_camara():
    return os.path.join(_carpeta_perfiles_usuario(), "perfiles_camara.json")


def _pil_a_b64(img, formato="PNG"):
    buf = io.BytesIO()
    img.save(buf, format=formato)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _b64_a_pil(data_url):
    _, _, datos = data_url.partition(",")
    return Image.open(io.BytesIO(base64.b64decode(datos)))


def reparar_crop_preview(motor_id, crop_rgb, mascara_crop_u8, sigma_blur=15):
    if motor_id == "blur":
        base = Image.fromarray(crop_rgb)
        blur_img = base.filter(ImageFilter.GaussianBlur(radius=sigma_blur))
        mask_norm = (mascara_crop_u8.astype(np.float32) / 255.0)[..., None]
        resultado = np.asarray(base).astype(np.float32) * (1 - mask_norm) + np.asarray(blur_img).astype(np.float32) * mask_norm
        return resultado.astype(np.uint8)
    if motor_id == "opencv" and cv2 is not None:
        bgr = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR)
        reparado = cv2.inpaint(bgr, mascara_crop_u8, 5, cv2.INPAINT_TELEA)
        return cv2.cvtColor(reparado, cv2.COLOR_BGR2RGB)
    return crop_rgb


class Api:
    def __init__(self):
        self._ventana = None
        self._frame_actual = None  # PIL RGB del frame de muestra del clip activo
        self.progreso = {"completados": 0, "total": 0, "por_clip": [], "logs": [], "terminado": True}
        self._progreso_lock = threading.Lock()
        self._evento_cancelar = threading.Event()
        self._actualizacion_lock = threading.Lock()
        self._actualizacion = {"hay_actualizacion": False}
        self._preview_lock = threading.Lock()
        self._preview_render_b64 = None
        self._preview_render_ultima = 0.0
        self._motor_gpu_cache = "sin_probar"

    def _detectar_motor_gpu(self):
        """Prueba una sola vez por sesion que encoder de video acelerado por
        GPU funciona de verdad en esta PC (NVENC/QuickSync/AMF) y devuelve
        ese nombre, o None si no hay ninguno utilizable (cae a CPU/libx264)."""
        if self._motor_gpu_cache == "sin_probar":
            self._motor_gpu_cache = motores.detectar_motor_gpu(FFMPEG_BIN)
        return self._motor_gpu_cache

    def set_ventana(self, ventana):
        self._ventana = ventana

    # ---------- licencia / limite gratis ----------
    def _es_pro(self):
        try:
            with open(_ruta_datos_usuario("licencia.json"), "r", encoding="utf-8") as f:
                datos = json.load(f)
            return datos.get("hash") == LICENCIA_HASH_PRO
        except Exception:
            return False

    def _leer_uso(self):
        hoy = datetime.date.today().isoformat()
        try:
            with open(_ruta_datos_usuario("uso.json"), "r", encoding="utf-8") as f:
                datos = json.load(f)
            if datos.get("fecha") != hoy:
                return {"fecha": hoy, "clips": 0}
            return datos
        except Exception:
            return {"fecha": hoy, "clips": 0}

    def _incrementar_uso(self, cantidad=1):
        datos = self._leer_uso()
        datos["clips"] = datos.get("clips", 0) + cantidad
        with open(_ruta_datos_usuario("uso.json"), "w", encoding="utf-8") as f:
            json.dump(datos, f)

    def estado_licencia(self):
        if self._es_pro():
            return {"pro": True, "restantes": None, "limite": None}
        uso = self._leer_uso()
        restantes = max(LIMITE_GRATIS_DIARIO - uso.get("clips", 0), 0)
        return {"pro": False, "restantes": restantes, "limite": LIMITE_GRATIS_DIARIO}

    def activar_licencia(self, clave):
        clave = (clave or "").strip().upper()
        h = hashlib.sha256(clave.encode("utf-8")).hexdigest()
        if h == LICENCIA_HASH_PRO:
            with open(_ruta_datos_usuario("licencia.json"), "w", encoding="utf-8") as f:
                json.dump({"hash": h}, f)
            return {"ok": True}
        return {"ok": False, "error": "Esa clave no es valida."}

    # ---------- actualizaciones ----------
    def obtener_estado_actualizacion(self):
        """El frontend consulta esto (polling liviano); el chequeo real corre
        en un hilo aparte al arrancar para no bloquear el inicio de la app."""
        with self._actualizacion_lock:
            return dict(self._actualizacion)

    def _revisar_actualizacion_en_fondo(self):
        if os.name != "nt":
            return  # la auto-instalacion solo esta implementada para Windows por ahora
        import urllib.request
        try:
            req = urllib.request.Request(
                URL_ULTIMA_VERSION,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "PixelClean"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                datos = json.loads(resp.read().decode("utf-8"))
            version_remota = datos.get("tag_name", "")
            if _version_a_tupla(version_remota) > _version_a_tupla(VERSION_APP):
                descarga_url = None
                for asset in datos.get("assets", []):
                    if asset.get("name") == "PixelClean_Setup.exe":
                        descarga_url = asset.get("browser_download_url")
                        break
                with self._actualizacion_lock:
                    self._actualizacion = {
                        "hay_actualizacion": True,
                        "version_actual": VERSION_APP,
                        "version_nueva": version_remota,
                        "url": datos.get("html_url") or URL_PAGINA_DESCARGA,
                        "descarga_url": descarga_url,
                        "instalando": False,
                    }
                # Se instala sola, sin que el usuario tenga que apretar nada -- salvo
                # que haya un procesamiento en curso en ese momento, para no perder
                # trabajo. Si esta procesando, se reintenta cuando termine el lote
                # (ver el finally de _procesar_todo_worker).
                if descarga_url:
                    with self._progreso_lock:
                        procesando = not self.progreso.get("terminado", True)
                    if not procesando:
                        self._descargar_e_instalar(descarga_url)
        except Exception:
            pass  # sin internet o GitHub caido: no molestamos al usuario

    def _descargar_e_instalar(self, descarga_url):
        """Descarga el instalador nuevo y lo lanza en modo silencioso; el
        instalador reemplaza esta misma instalacion (mismo AppId => actualizacion
        in-place) y vuelve a abrir la app solo. La licencia no se pierde porque
        vive en %LOCALAPPDATA%\\PixelClean, fuera de la carpeta de instalacion.
        Devuelve {"ok": bool, "error": str|None} -- el llamador debe propagarlo,
        nunca asumir que salio bien."""
        try:
            with self._actualizacion_lock:
                self._actualizacion["instalando"] = True

            import urllib.request
            import tempfile
            ruta_temp = os.path.join(tempfile.gettempdir(), "PixelClean_Setup_actualizacion.exe")
            urllib.request.urlretrieve(descarga_url, ruta_temp)

            import subprocess
            subprocess.Popen(
                [ruta_temp, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            threading.Thread(target=self._cerrar_para_actualizar, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            with self._actualizacion_lock:
                self._actualizacion["instalando"] = False
                self._actualizacion["error"] = str(e)
            return {"ok": False, "error": str(e)}

    def instalar_actualizacion(self):
        """Disparador manual (botón de la campanita): normalmente la
        actualizacion ya se instalo sola en segundo plano, pero esto sirve
        para forzarla de nuevo si quedo pendiente por un procesamiento activo."""
        with self._actualizacion_lock:
            descarga_url = self._actualizacion.get("descarga_url")
            ya_instalando = self._actualizacion.get("instalando")
        if not descarga_url:
            return {"ok": False, "error": "No hay una URL de descarga disponible todavia."}
        if ya_instalando:
            return {"ok": True}
        return self._descargar_e_instalar(descarga_url)

    def _cerrar_para_actualizar(self):
        import time
        time.sleep(1.2)  # le da tiempo al frontend a mostrar el mensaje antes de cerrar
        os._exit(0)

    # ---------- paso clip ----------
    # Los dialogos nativos de Windows necesitan correr en el mismo hilo que
    # pywebview ya usa para las llamadas js_api (por temas de threading de COM).
    # Se probo moverlo a un hilo propio para evitar colgarse y broque el
    # dialogo silenciosamente -- se vuelve a la forma simple, que es la que
    # ya se confirmo funcionando antes.
    def elegir_clips(self):
        archivos = self._ventana.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("Videos (*.mp4;*.mov;*.mxf;*.avi)", "Todos los archivos (*.*)"),
        )
        return list(archivos) if archivos else []

    def obtener_frame_y_metadata(self, ruta_clip):
        try:
            info_meta = metadata_camara.leer_metadata_camara(FFPROBE_BIN, ruta_clip)
        except Exception:
            info_meta = None

        try:
            import subprocess
            ruta_tmp = os.path.join(os.path.dirname(ruta_clip), "_pixelclean_frame.png")
            info = motores._info_basica(FFPROBE_BIN, ruta_clip)
            segundo = (info["duracion_seg"] / 2) if info["duracion_seg"] else 1.0
            subprocess.run([FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                             "-ss", str(segundo), "-i", ruta_clip, "-frames:v", "1", ruta_tmp],
                            capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            img = Image.open(ruta_tmp).convert("RGB")
            self._frame_actual = img
            os.remove(ruta_tmp)
            return {
                "frame_b64": _pil_a_b64(img), "ancho": img.width, "alto": img.height,
                "metadata": info_meta or {}, "duracion_seg": info.get("duracion_seg") or 0,
            }
        except Exception as e:
            return {"frame_b64": None, "ancho": 0, "alto": 0, "metadata": info_meta or {}, "duracion_seg": 0, "error": str(e)}

    def obtener_frame_en(self, ruta_clip, segundo):
        """Pide el cuadro de un instante puntual del clip (para el scrubber del
        paso Mascara): sirve para buscar a mano un momento donde el defecto se
        vea mejor, en vez de quedarse siempre con el cuadro del medio del clip."""
        try:
            import subprocess
            import tempfile
            ruta_tmp = os.path.join(tempfile.gettempdir(), f"_pixelclean_scrub_{os.getpid()}.png")
            subprocess.run([FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                             "-ss", str(max(float(segundo), 0)), "-i", ruta_clip, "-frames:v", "1", ruta_tmp],
                            capture_output=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if not os.path.isfile(ruta_tmp):
                return {"ok": False, "error": "No se pudo leer ese instante del clip."}
            img = Image.open(ruta_tmp).convert("RGB")
            self._frame_actual = img  # el resto del flujo (mascara, preview) usa este mismo cuadro
            os.remove(ruta_tmp)
            return {"ok": True, "frame_b64": _pil_a_b64(img)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def elegir_mascara_png(self, ancho, alto):
        archivo = self._ventana.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Imagenes (*.png;*.jpg;*.jpeg)", "Todos los archivos (*.*)"),
        )
        if not archivo:
            return None
        try:
            img = Image.open(archivo[0]).convert("RGB")
            arr = np.asarray(img).astype(np.int16)
            r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
            verde = (g > r + GREEN_THRESHOLD) & (g > b + GREEN_THRESHOLD)
            if not verde.any():
                gris = np.asarray(Image.open(archivo[0]).convert("L"))
                verde = gris > 127
            rgba = np.zeros((verde.shape[0], verde.shape[1], 4), dtype=np.uint8)
            rgba[verde] = [58, 160, 255, 255]
            salida = Image.fromarray(rgba, mode="RGBA").resize((ancho, alto))
            return _pil_a_b64(salida)
        except Exception:
            return None

    # ---------- batch inteligente: deteccion automatica ----------
    def detectar_mascara_auto(self, ruta_clip):
        try:
            info = motores._info_basica(FFPROBE_BIN, ruta_clip)
            ok, rgba, mensaje = deteccion_automatica.detectar_mascara_automatica(
                FFMPEG_BIN, ruta_clip, info.get("duracion_seg"))
            if not ok:
                return {"ok": False, "error": mensaje}
            tamano = self._frame_actual.size if self._frame_actual is not None else (rgba.shape[1], rgba.shape[0])
            salida = Image.fromarray(rgba, mode="RGBA").resize(tamano)
            return {"ok": True, "mascara_b64": _pil_a_b64(salida), "mensaje": mensaje}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- pincel inteligente: el usuario pinta mas o menos, se afina solo ----------
    def refinar_pincel_inteligente(self, ruta_clip, x0, y0, x1, y1):
        try:
            info = motores._info_basica(FFPROBE_BIN, ruta_clip)
            bbox = (int(x0), int(y0), int(x1), int(y1))
            ok, rgba, mensaje = deteccion_automatica.refinar_seleccion_por_contraste(
                FFMPEG_BIN, ruta_clip, info.get("duracion_seg"), bbox)
            if not ok:
                return {"ok": False, "error": mensaje}
            return {
                "ok": True, "mascara_b64": _pil_a_b64(Image.fromarray(rgba, mode="RGBA")),
                "x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3], "mensaje": mensaje,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- perfiles guardados por camara (el usuario les pone nombre a mano) ----------
    def _leer_perfiles_camara(self):
        # migra el archivo viejo (guardado por error en la carpeta tecnica
        # escondida en versiones anteriores) la primera vez que lo encuentra.
        ruta_nueva = _ruta_perfiles_camara()
        if not os.path.isfile(ruta_nueva):
            ruta_vieja = _ruta_datos_usuario("perfiles_camara.json")
            if os.path.isfile(ruta_vieja):
                try:
                    with open(ruta_vieja, "r", encoding="utf-8") as f:
                        datos_viejos = f.read()
                    with open(ruta_nueva, "w", encoding="utf-8") as f:
                        f.write(datos_viejos)
                except Exception:
                    pass
        try:
            with open(ruta_nueva, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _guardar_perfiles_camara(self, perfiles):
        with open(_ruta_perfiles_camara(), "w", encoding="utf-8") as f:
            json.dump(perfiles, f)

    def listar_perfiles_camara(self):
        return sorted(self._leer_perfiles_camara().keys())

    def guardar_perfil_camara(self, nombre, mascara_b64, motor, sigma):
        nombre = (nombre or "").strip()
        if not nombre:
            return {"ok": False, "error": "Poné un nombre para el perfil."}
        try:
            perfiles = self._leer_perfiles_camara()
            perfiles[nombre] = {
                "mascara_b64": mascara_b64, "motor": motor, "sigma": sigma,
                "creado": datetime.date.today().isoformat(),
            }
            self._guardar_perfiles_camara(perfiles)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def cargar_perfil_camara(self, nombre):
        perfil = self._leer_perfiles_camara().get(nombre)
        if not perfil:
            return {"ok": False, "error": "No existe ese perfil."}
        return {"ok": True, **perfil}

    def borrar_perfil_camara(self, nombre):
        try:
            perfiles = self._leer_perfiles_camara()
            perfiles.pop(nombre, None)
            self._guardar_perfiles_camara(perfiles)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- vista previa en vivo ----------
    def render_preview(self, motor_id, mascara_b64, sigma):
        if self._frame_actual is None:
            return None
        try:
            mascara_rgba = np.asarray(_b64_a_pil(mascara_b64).convert("RGBA").resize(self._frame_actual.size))
            mascara_arr = mascara_rgba[..., 3]
            ys, xs = np.where(mascara_arr > 20)
            if len(xs) == 0:
                return None
            pad = 60
            frame = np.asarray(self._frame_actual)
            x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + pad, frame.shape[1])
            y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + pad, frame.shape[0])
            crop = frame[y0:y1, x0:x1]
            mascara_crop = mascara_arr[y0:y1, x0:x1]
            reparado = reparar_crop_preview(motor_id, crop, mascara_crop, sigma)
            return {
                "antes_b64": _pil_a_b64(Image.fromarray(crop)),
                "despues_b64": _pil_a_b64(Image.fromarray(reparado)),
            }
        except Exception:
            traceback.print_exc()
            return None

    # ---------- procesar ----------
    # Nota: las actualizaciones de progreso se guardan aca y el frontend las consulta
    # activamente (polling) en vez de que Python se las empuje via evaluate_js desde un
    # hilo en segundo plano -- eso ultimo resulto poco confiable en esta combinacion
    # concreta de pywebview + WinForms + WebView2 (errores de threading en el log).
    def procesar_todo(self, payload):
        try:
            total = len(payload["clips"])
            if not self._es_pro():
                uso = self._leer_uso()
                restantes = max(LIMITE_GRATIS_DIARIO - uso.get("clips", 0), 0)
                if total > restantes:
                    return {"ok": False, "error": "limite_gratis", "restantes": restantes, "limite": LIMITE_GRATIS_DIARIO}
            self._evento_cancelar.clear()
            with self._progreso_lock:
                self.progreso = {"completados": 0, "total": total, "por_clip": [0.0] * total,
                                  "logs": [], "terminado": False}
            threading.Thread(target=self._procesar_todo_worker, args=(payload,), daemon=True).start()
            return {"ok": True}
        except Exception as e:
            with self._progreso_lock:
                self.progreso = {"completados": 0, "total": 0, "por_clip": [], "terminado": True,
                                  "logs": [{"mensaje": f"ERROR al iniciar: {e}", "ok": False}]}
            return {"ok": False, "error": str(e)}

    def cancelar_procesamiento(self):
        self._evento_cancelar.set()
        self._agregar_log("Cancelando... (se corta al terminar el paso actual del clip en curso)", False)
        return {"ok": True}

    def _agregar_log(self, mensaje, ok):
        with self._progreso_lock:
            self.progreso["logs"].append({"mensaje": mensaje, "ok": ok})

    # ---------- vista previa en vivo durante el render (de prueba) ----------
    # Muestreo periodico y en baja resolucion, solo para ver "por donde viene"
    # el lote -- no es el cuadro exacto que esta escribiendo ffmpeg (eso requeriria
    # leer el archivo de salida a medio escribir, fragil), sino el mismo calculo
    # de reparar_crop_preview aplicado al cuadro de origen mas cercano al avance
    # actual. Nunca debe interrumpir el procesamiento real si algo falla.
    def _actualizar_preview_render(self, ruta_clip, frac, motor_id, mascara_rgba_arr, sigma):
        ahora = time.time()
        with self._preview_lock:
            if ahora - self._preview_render_ultima < 2.0:
                return
            self._preview_render_ultima = ahora
        # OJO: esto tiene que correr en un hilo aparte, nunca en linea. Esta
        # funcion la llama el callback de progreso, que corre en el mismo hilo
        # que va leyendo la salida de ffmpeg del render real -- si acá adentro
        # se bloquea 1-2s haciendo su propio ffmpeg + proceso de imagen, se deja
        # de leer el stdout del ffmpeg real, su pipe se llena y HACE QUE EL
        # RENDER REAL SE FRENE esperando que alguien lea. Un render que antes
        # tardaba X terminaba tardando el doble o mas por esto.
        threading.Thread(
            target=self._generar_preview_render, args=(ruta_clip, frac, motor_id, mascara_rgba_arr, sigma),
            daemon=True,
        ).start()

    def _generar_preview_render(self, ruta_clip, frac, motor_id, mascara_rgba_arr, sigma):
        try:
            import subprocess
            import tempfile
            info = motores._info_basica(FFPROBE_BIN, ruta_clip)
            duracion = info.get("duracion_seg") or 1.0
            segundo = max(min(frac, 0.98), 0.0) * duracion
            ruta_tmp = os.path.join(
                tempfile.gettempdir(), f"_pixelclean_preview_{os.getpid()}_{threading.get_ident()}.png")
            subprocess.run([FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                             "-ss", str(segundo), "-i", ruta_clip, "-frames:v", "1", ruta_tmp],
                            capture_output=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if not os.path.isfile(ruta_tmp):
                return
            frame = np.asarray(Image.open(ruta_tmp).convert("RGB"))
            os.remove(ruta_tmp)
            mascara_rs = np.asarray(Image.fromarray(mascara_rgba_arr, mode="RGBA")
                                     .resize((frame.shape[1], frame.shape[0])))
            mascara_alpha = mascara_rs[..., 3]
            # cuadro completo, no solo el recorte de la mascara -- ahora que
            # esto corre en su propio hilo (ver _actualizar_preview_render) ya
            # no hay costo de bloquear el render real por hacerlo mas grande.
            reparado = reparar_crop_preview(motor_id, frame, mascara_alpha, sigma)
            img_out = Image.fromarray(reparado)
            img_out.thumbnail((720, 720))  # mantiene relacion de aspecto original, solo limita el lado mas largo
            with self._preview_lock:
                self._preview_render_b64 = _pil_a_b64(img_out)
        except Exception:
            pass

    def _procesar_todo_worker(self, payload):
        clips = payload["clips"]
        try:
            carpeta_salida = os.path.join(os.path.dirname(clips[0]), "reparados")
            os.makedirs(carpeta_salida, exist_ok=True)

            mascara_rgba = _b64_a_pil(payload["mascara_b64"]).convert("RGBA").resize(self._frame_actual.size)
            mascara_l = Image.fromarray(np.asarray(mascara_rgba)[..., 3], mode="L")
            mascara_bn_path = os.path.join(carpeta_salida, "_mascara_bn.png")
            mascara_l.save(mascara_bn_path)

            motor_gpu = self._detectar_motor_gpu()
            nombres_gpu = {"nvenc": "NVIDIA (NVENC)", "qsv": "Intel Quick Sync", "amf": "AMD (AMF)",
                           "videotoolbox": "Apple (VideoToolbox)"}
            self._agregar_log(
                f"Codificando con GPU: {nombres_gpu.get(motor_gpu, motor_gpu)}." if motor_gpu
                else "Codificando con CPU (no se detecto GPU compatible).", True)

            preset_info = PRESETS_VELOCIDAD[payload["velocidad"]]
            parametros = {
                "sigma_blur": payload["sigma"], "preset_info": preset_info,
                "modo_calidad": payload["calidad"], "resolucion_objetivo": RESOLUCIONES[payload["resolucion"]],
                "motor_gpu": motor_gpu, "radio_inpaint": 5, "metodo": "telea", "zoom_padding": 40,
                "evento_cancelar": self._evento_cancelar,
            }
            motor_fn = motores.MOTORES[payload["motor"]]
            total = len(clips)

            for idx, clip in enumerate(clips):
                if self._evento_cancelar.is_set():
                    self._agregar_log("Cancelado por el usuario.", False)
                    break
                nombre = os.path.splitext(os.path.basename(clip))[0]
                ext = os.path.splitext(clip)[1] or ".mp4"
                ruta_salida = os.path.join(carpeta_salida, nombre + OUTPUT_SUFFIX + ext)

                mascara_rgba_arr = np.asarray(mascara_rgba)

                def callback(frac, i=idx, clip=clip):
                    with self._progreso_lock:
                        self.progreso["por_clip"][i] = frac
                    self._actualizar_preview_render(clip, frac, payload["motor"], mascara_rgba_arr, payload["sigma"])

                exito, resultado = motor_fn(FFMPEG_BIN, FFPROBE_BIN, clip, mascara_bn_path, ruta_salida,
                                             parametros, callback)
                mensaje = (f"OK: {os.path.basename(clip)} -> {os.path.basename(resultado)}" if exito
                           else f"ERROR: {os.path.basename(clip)} -> {resultado}")
                self._agregar_log(mensaje, exito)
                if exito and not self._es_pro():
                    self._incrementar_uso(1)
                with self._progreso_lock:
                    self.progreso["completados"] = idx + 1

            self._agregar_log(f"Listo. Carpeta de salida: {carpeta_salida}", True)
        except Exception:
            self._agregar_log("ERROR inesperado:\n" + traceback.format_exc(), False)
        finally:
            with self._progreso_lock:
                self.progreso["terminado"] = True
            with self._actualizacion_lock:
                pendiente = (
                    self._actualizacion.get("hay_actualizacion")
                    and self._actualizacion.get("descarga_url")
                    and not self._actualizacion.get("instalando")
                )
                descarga_url = self._actualizacion.get("descarga_url")
            if pendiente:
                self._descargar_e_instalar(descarga_url)


def _iniciar_servidor(api):
    """Un unico servidor HTTP que sirve los archivos estaticos (web/) y el endpoint
    de progreso desde el mismo origen. El puente js_api de pywebview demostro ser
    poco confiable para llamadas repetidas/rapidas en esta combinacion de tecnologias;
    y usar un segundo servidor en otro puerto para el progreso metia de vuelta un
    pedido cruzado (distinto puerto = distinto origen) que tambien fallaba. Con todo
    en el mismo servidor, el fetch del frontend queda siempre same-origin."""
    from http.server import SimpleHTTPRequestHandler

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=CARPETA_WEB, **kwargs)

        def end_headers(self):
            # WebView2/Chromium a veces sirve el index.html, app.js o style.css
            # cacheados aunque el archivo en disco ya cambio (sobre todo entre
            # reinicios de la app durante desarrollo). Sin esto, un Ctrl+R puede
            # seguir mostrando una version vieja del frontend.
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _responder_json(self, datos):
            cuerpo = json.dumps(datos).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_GET(self):
            ruta = self.path.split("?")[0]
            if ruta == "/progreso":
                with api._progreso_lock:
                    self._responder_json(api.progreso)
            elif ruta == "/preview_render":
                with api._preview_lock:
                    self._responder_json({"preview_b64": api._preview_render_b64})
            elif ruta == "/clip_video":
                self._servir_video()
            else:
                super().do_GET()

        def _servir_video(self):
            """Streamea el clip original directo desde disco (con soporte de
            Range) para que el <video> del paso Mascara pueda reproducir y
            buscar posiciones al instante, sin pasar por Python en cada
            movimiento -- eso era lo que hacia lento al scrubber anterior."""
            query = urllib.parse.urlsplit(self.path).query
            ruta_video = urllib.parse.parse_qs(query).get("path", [None])[0]
            if ruta_video:
                ruta_video = urllib.parse.unquote(ruta_video)
            if not ruta_video or not os.path.isfile(ruta_video):
                self.send_error(404, "Clip no encontrado")
                return

            tamano = os.path.getsize(ruta_video)
            tipo = mimetypes.guess_type(ruta_video)[0] or "video/mp4"
            rango = self.headers.get("Range")

            if rango:
                unidad, _, valores = rango.partition("=")
                inicio_txt, _, fin_txt = valores.partition("-")
                inicio = int(inicio_txt) if inicio_txt else 0
                fin = int(fin_txt) if fin_txt else tamano - 1
                fin = min(fin, tamano - 1)
                largo = max(fin - inicio + 1, 0)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {inicio}-{fin}/{tamano}")
            else:
                inicio, largo = 0, tamano
                self.send_response(200)

            self.send_header("Content-Type", tipo)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(largo))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            try:
                with open(ruta_video, "rb") as f:
                    f.seek(inicio)
                    restante = largo
                    while restante > 0:
                        trozo = f.read(min(262144, restante))
                        if not trozo:
                            break
                        self.wfile.write(trozo)
                        restante -= len(trozo)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass  # el usuario siguio moviendo el scrubber y corto el pedido anterior; normal

        def log_message(self, formato, *args):
            pass

    servidor = HTTPServer(("127.0.0.1", 0), Handler)
    puerto = servidor.server_address[1]
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return puerto


def main():
    carpeta_log = _carpeta_datos_app()
    logging.basicConfig(
        filename=os.path.join(carpeta_log, "debug.log"), filemode="w",
        level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger().info("arranque: cwd=%s frozen=%s meipass=%s exe=%s",
                              os.getcwd(), getattr(sys, "frozen", False),
                              getattr(sys, "_MEIPASS", None), sys.executable)
    # ojo: NO forzar DPI-awareness (SetProcessDpiAwareness) a nivel de proceso aca.
    # WebView2/Chromium ya maneja el escalado de pantalla por su cuenta de forma
    # correcta (via devicePixelRatio); si el proceso host tambien se declara
    # DPI-aware, las dos capas terminan compensando el escalado por separado y
    # las coordenadas del mouse le llegan mal a la pagina (pintar en el lugar
    # equivocado). Se probo explicitamente y empeoraba las cosas.
    api = Api()
    threading.Thread(target=api._revisar_actualizacion_en_fondo, daemon=True).start()
    puerto = _iniciar_servidor(api)
    ventana = webview.create_window(
        "PixelClean - Reparador de pixeles quemados",
        f"http://127.0.0.1:{puerto}/index.html",
        js_api=api, width=1280, height=820, min_size=(1040, 700),
        background_color="#0b0d12",
    )
    api.set_ventana(ventana)

    def _al_soltar_archivos(e):
        # El navegador nunca expone la ruta real de un archivo soltado por
        # seguridad; pywebview la inyecta como "pywebviewFullPath" pero SOLO
        # para listeners de drop registrados por esta via (window.dom), no
        # para un addEventListener comun hecho en el JS de la pagina -- por
        # eso el drag&drop hecho solo en JS nunca funcionaba.
        try:
            archivos = (e.get("dataTransfer") or {}).get("files", [])
            rutas = [f["pywebviewFullPath"] for f in archivos if f.get("pywebviewFullPath")]
            rutas = [r for r in rutas if r.lower().endswith((".mp4", ".mov", ".mxf", ".avi", ".m4v", ".mkv"))]
            if rutas:
                ventana.evaluate_js(f"window.agregarClipsDesdeDrop({json.dumps(rutas)})")
        except Exception:
            logging.getLogger().exception("Error al procesar archivos soltados")

    def _configurar_drag_and_drop():
        try:
            ventana.dom.document.events.dragover += DOMEventHandler(lambda e: None, prevent_default=True)
            ventana.dom.document.events.drop += DOMEventHandler(_al_soltar_archivos, prevent_default=True)
        except Exception:
            logging.getLogger().exception("No se pudo registrar drag and drop nativo")

    ventana.events.loaded += _configurar_drag_and_drop
    if os.name == "nt":
        # Instalado en Program Files, la carpeta del .exe queda de solo-lectura para
        # usuarios sin admin. Si no se fija storage_path, WebView2 intenta crear su
        # carpeta de perfil ahi mismo (o en un temp dir que pywebview borra al toque),
        # falla en silencio (build sin consola) y la ventana se queda en negro para
        # siempre en el background_color de arriba. Se fuerza una carpeta escribible
        # por-usuario en LOCALAPPDATA. En macOS (backend Cocoa/WebKit) esto no aplica.
        carpeta_datos = os.path.join(_carpeta_datos_app(), "webview2_data")
        os.makedirs(carpeta_datos, exist_ok=True)
        webview.start(private_mode=False, storage_path=carpeta_datos)
    else:
        webview.start(private_mode=False)


if __name__ == "__main__":
    main()
