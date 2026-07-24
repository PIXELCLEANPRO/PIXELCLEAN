"""
REPARADOR DE PIXELES QUEMADOS - Sony A7III (v3)
==============================================================================
Novedades sobre v2:
  - Vista previa de la mascara, con sliders de "Grosor" (dilata la zona
    marcada) y "Calado" (suaviza el borde para una transicion mas natural).
  - Vista previa del desenfoque: recorta y agranda la zona de la mascara
    sobre un frame real del video, mostrando Antes/Despues antes de generar
    nada.
  - Panel de calidad: analiza el clip original (resolucion, codec, bitrate,
    peso) y deja elegir entre igualar el bitrate original (mas parecido
    posible al original), fijar una calidad (CRF) o comprimir mas /
    reducir resolucion — explicando el trade-off de cada opcion.

Requiere:
  pip install pillow numpy
  ffmpeg.exe y ffprobe.exe (junto a este script, o en el PATH del sistema)
==============================================================================
"""

import os
import sys
import re
import json
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

try:
    import numpy as np
    from PIL import Image, ImageTk, ImageFilter
except ImportError as e:
    print(f"Falta instalar una dependencia: {e}")
    print("Corre: pip install pillow numpy")
    sys.exit(1)


# ============================================================
# CONFIGURACION
# ============================================================
def _carpeta_base():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _ruta_local(nombre):
    candidato = os.path.join(_carpeta_base(), nombre)
    return candidato if os.path.isfile(candidato) else nombre


FFMPEG_BIN = _ruta_local("ffmpeg.exe") if os.name == "nt" else _ruta_local("ffmpeg")
FFPROBE_BIN = _ruta_local("ffprobe.exe") if os.name == "nt" else _ruta_local("ffprobe")

GREEN_THRESHOLD = 40
OUTPUT_SUFFIX = "_reparado"
MAX_WORKERS_DEFAULT = 2

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

COLOR_FONDO_1 = "#dce8f7"
COLOR_FONDO_2 = "#eef5fb"
COLOR_PANEL_SOLIDO = "#f2f7fc"
COLOR_ACENTO = "#0a84ff"
COLOR_ACENTO_HOVER = "#0068d6"
COLOR_TEXTO = "#1c1c1e"
COLOR_TEXTO_SUAVE = "#6e6e73"
COLOR_OK = "#2e7d32"
COLOR_ERROR = "#c62828"
COLOR_AVISO = "#b8860b"


# ============================================================
# UTILIDADES DE BAJO NIVEL
# ============================================================
def verificar_binario(ruta):
    try:
        r = subprocess.run([ruta, "-version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def detectar_nvenc():
    try:
        r = subprocess.run([FFMPEG_BIN, "-hide_banner", "-encoders"],
                            capture_output=True, text=True, timeout=10)
        return "h264_nvenc" in r.stdout
    except Exception:
        return False


def analizar_clip(ruta_video):
    """Devuelve dict con info del clip original via ffprobe, o None si falla."""
    try:
        cmd = [
            FFPROBE_BIN, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", ruta_video,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        datos = json.loads(r.stdout)
        video_stream = next((s for s in datos.get("streams", []) if s.get("codec_type") == "video"), {})
        formato = datos.get("format", {})

        bitrate = formato.get("bit_rate") or video_stream.get("bit_rate")
        peso_bytes = formato.get("size")
        duracion = formato.get("duration")

        return {
            "ancho": int(video_stream.get("width", 0)),
            "alto": int(video_stream.get("height", 0)),
            "codec": video_stream.get("codec_name", "?"),
            "fps": eval(video_stream.get("r_frame_rate", "0/1")) if "/" in str(video_stream.get("r_frame_rate", "")) else 0,
            "bitrate_bps": int(bitrate) if bitrate else None,
            "peso_mb": (int(peso_bytes) / (1024 * 1024)) if peso_bytes else None,
            "duracion_seg": float(duracion) if duracion else None,
        }
    except Exception:
        return None


def obtener_duracion_segundos(ruta_video):
    info = analizar_clip(ruta_video)
    return info["duracion_seg"] if info else None


def extraer_frame(ruta_video, ruta_salida_png, segundo=None):
    """Extrae un frame (a mitad del video si no se especifica) como PNG."""
    if segundo is None:
        info = analizar_clip(ruta_video)
        segundo = (info["duracion_seg"] / 2) if (info and info["duracion_seg"]) else 1.0
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(segundo), "-i", ruta_video,
        "-frames:v", "1", ruta_salida_png,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not os.path.isfile(ruta_salida_png):
        raise RuntimeError(f"No se pudo extraer un frame de prueba: {r.stderr.strip()[-300:]}")
    return ruta_salida_png


# ---------- mascara: deteccion de verde + grosor + calado ----------
def mascara_base_desde_color(ruta_mascara_color):
    """Devuelve un array numpy 0/255 (uint8) detectando la zona verde."""
    if not os.path.isfile(ruta_mascara_color):
        raise FileNotFoundError(f"No se encuentra el archivo de mascara: {ruta_mascara_color}")
    img = Image.open(ruta_mascara_color).convert("RGB")
    arr = np.asarray(img).astype(np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mascara_bool = (g > r + GREEN_THRESHOLD) & (g > b + GREEN_THRESHOLD)
    if not mascara_bool.any():
        raise ValueError(
            "No se detecto ninguna zona verde en la mascara. "
            "Revisa que el color pintado sea un verde puro."
        )
    return (mascara_bool.astype(np.uint8)) * 255


def aplicar_grosor_y_calado(mascara_arr_u8, grosor_px, calado_px):
    """Aplica dilatacion (grosor) y luego suavizado de borde (calado)."""
    img = Image.fromarray(mascara_arr_u8, mode="L")
    if grosor_px > 0:
        k = grosor_px * 2 + 1
        img = img.filter(ImageFilter.MaxFilter(k))
    if calado_px > 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=calado_px))
    return img  # PIL Image modo "L"


def generar_vista_previa_blur(frame_img, mascara_img_l, sigma_blur, zoom_padding=80, tam_salida=320):
    """Recorta y agranda la zona marcada, devuelve (antes, despues) como imagenes PIL."""
    frame = np.asarray(frame_img.convert("RGB")).astype(np.float32)
    mask = np.asarray(mascara_img_l.resize(frame_img.size)).astype(np.float32) / 255.0

    ys, xs = np.where(mask > 0.05)
    if len(xs) == 0:
        raise ValueError("La mascara (con los ajustes actuales) no marca ninguna zona.")

    x0, x1 = max(int(xs.min()) - zoom_padding, 0), min(int(xs.max()) + zoom_padding, frame.shape[1])
    y0, y1 = max(int(ys.min()) - zoom_padding, 0), min(int(ys.max()) + zoom_padding, frame.shape[0])

    recorte_base = Image.fromarray(frame[y0:y1, x0:x1].astype(np.uint8))
    recorte_blur = recorte_base.filter(ImageFilter.GaussianBlur(radius=sigma_blur))
    recorte_mask = mask[y0:y1, x0:x1][..., None]

    base_arr = np.asarray(recorte_base).astype(np.float32)
    blur_arr = np.asarray(recorte_blur).astype(np.float32)
    resultado = base_arr * (1 - recorte_mask) + blur_arr * recorte_mask
    resultado_img = Image.fromarray(resultado.astype(np.uint8))

    antes = recorte_base.resize((tam_salida, tam_salida))
    despues = resultado_img.resize((tam_salida, tam_salida))
    return antes, despues


_RE_TIME = re.compile(r"out_time_ms=(\d+)")


def construir_comando_ffmpeg(ruta_video, ruta_mascara_bn, ruta_salida, sigma_blur,
                              modo_calidad, preset_info, bitrate_objetivo_bps,
                              resolucion_objetivo, usar_nvenc):
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
        objetivo_kbps = max(1000, int(bitrate_objetivo_bps / 1000))
        codec_args = base_codec + [
            "-b:v", f"{objetivo_kbps}k",
            "-maxrate", f"{int(objetivo_kbps*1.5)}k",
            "-bufsize", f"{objetivo_kbps*2}k",
        ]
    else:
        crf_arg = "-cq" if usar_nvenc else "-crf"
        codec_args = base_codec + [crf_arg, str(preset_info["crf"])]

    return [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-i", ruta_video,
        "-loop", "1", "-i", ruta_mascara_bn,
        "-filter_complex", filtro,
        "-map", etiqueta_salida, "-map", "0:a?",
        *codec_args,
        "-threads", "0",
        "-c:a", "copy",
        "-shortest",
        "-progress", "pipe:1", "-nostats",
        ruta_salida,
    ]


def procesar_clip(ruta_video, ruta_mascara_bn, carpeta_salida, sigma_blur,
                   modo_calidad, preset_info, resolucion_objetivo,
                   usar_nvenc, callback_progreso):
    if not os.path.isfile(ruta_video):
        return False, "El archivo de video no existe (¿se movio o se borro?)."

    nombre = os.path.splitext(os.path.basename(ruta_video))[0]
    ext = os.path.splitext(ruta_video)[1] or ".mp4"
    ruta_salida = os.path.join(carpeta_salida, nombre + OUTPUT_SUFFIX + ext)

    duracion = obtener_duracion_segundos(ruta_video)
    bitrate_objetivo = None
    if modo_calidad == "bitrate":
        info = analizar_clip(ruta_video)
        bitrate_objetivo = info["bitrate_bps"] if info else None
        if not bitrate_objetivo:
            modo_calidad = "crf"  # sin dato de bitrate, caemos a CRF

    cmd = construir_comando_ffmpeg(
        ruta_video, ruta_mascara_bn, ruta_salida, sigma_blur,
        modo_calidad, preset_info, bitrate_objetivo, resolucion_objetivo, usar_nvenc,
    )

    try:
        proceso = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1, universal_newlines=True)
    except FileNotFoundError:
        return False, "No se encontro ffmpeg."
    except Exception as e:
        return False, f"No se pudo iniciar ffmpeg: {e}"

    stderr_lineas = []

    def leer_stderr():
        for linea in proceso.stderr:
            stderr_lineas.append(linea.rstrip())

    hilo = threading.Thread(target=leer_stderr, daemon=True)
    hilo.start()

    try:
        for linea in proceso.stdout:
            m = _RE_TIME.search(linea)
            if m and duracion:
                seg = int(m.group(1)) / 1_000_000
                callback_progreso(min(seg / duracion, 1.0))
            elif "progress=end" in linea:
                callback_progreso(1.0)
    except Exception:
        pass

    proceso.wait()
    hilo.join(timeout=5)

    if proceso.returncode != 0 or not os.path.isfile(ruta_salida):
        detalle = "\n".join(stderr_lineas[-8:]) if stderr_lineas else "(sin detalle de ffmpeg)"
        return False, f"ffmpeg devolvio error (codigo {proceso.returncode}):\n{detalle}"

    callback_progreso(1.0)
    return True, ruta_salida


# ============================================================
# INTERFAZ GRAFICA
# ============================================================
class TarjetaClip(ttk.Frame):
    def __init__(self, parent, nombre_archivo):
        super().__init__(parent, style="Card.TFrame", padding=(12, 8))
        self.columnconfigure(0, weight=1)
        self.lbl_nombre = ttk.Label(self, text=nombre_archivo, style="CardText.TLabel")
        self.lbl_nombre.grid(row=0, column=0, sticky="w")
        self.lbl_estado = ttk.Label(self, text="En espera...", style="CardSub.TLabel")
        self.lbl_estado.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.barra = ttk.Progressbar(self, style="Glass.Horizontal.TProgressbar",
                                      orient="horizontal", mode="determinate", maximum=100)
        self.barra.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def actualizar(self, fraccion, estado_texto=None, color_estado=None):
        self.barra["value"] = max(0, min(100, fraccion * 100))
        if estado_texto is not None:
            self.lbl_estado.config(text=estado_texto)
        if color_estado is not None:
            self.lbl_estado.config(foreground=color_estado)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Reparador de pixeles quemados")
        self.root.geometry("720x920")
        self.root.minsize(680, 680)

        self.clips = []
        self.mascara = None
        self.mascara_arr_base = None      # numpy 0/255 sin procesar
        self.mascara_procesada_img = None  # PIL "L" con grosor+calado aplicados
        self.frame_prueba_img = None
        self.tarjetas = {}
        self.usar_nvenc = detectar_nvenc()
        self._debounce_id = None

        self._configurar_estilos()
        self._construir_fondo()
        self._construir_ui()
        self._verificar_dependencias_inicio()

    # ---------- estilos ----------
    def _configurar_estilos(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Card.TFrame", background=COLOR_PANEL_SOLIDO)
        style.configure("CardText.TLabel", background=COLOR_PANEL_SOLIDO, foreground=COLOR_TEXTO, font=("Segoe UI", 10))
        style.configure("CardSub.TLabel", background=COLOR_PANEL_SOLIDO, foreground=COLOR_TEXTO_SUAVE, font=("Segoe UI", 9))
        style.configure("Titulo.TLabel", background=COLOR_FONDO_2, foreground=COLOR_TEXTO, font=("Segoe UI", 15, "bold"))
        style.configure("Seccion.TLabel", background=COLOR_FONDO_2, foreground=COLOR_TEXTO, font=("Segoe UI", 10, "bold"))
        style.configure("Info.TLabel", background=COLOR_FONDO_2, foreground=COLOR_TEXTO_SUAVE, font=("Segoe UI", 9))
        style.configure("Aviso.TLabel", background=COLOR_FONDO_2, foreground=COLOR_AVISO, font=("Segoe UI", 9, "italic"))
        style.configure("Glass.TButton", font=("Segoe UI", 10), padding=(14, 8), background="#ffffff", foreground=COLOR_TEXTO, borderwidth=0)
        style.map("Glass.TButton", background=[("active", "#e4edf7")])
        style.configure("Accento.TButton", font=("Segoe UI", 11, "bold"), padding=(16, 10), background=COLOR_ACENTO, foreground="white", borderwidth=0)
        style.map("Accento.TButton", background=[("active", COLOR_ACENTO_HOVER)])
        style.configure("Glass.Horizontal.TProgressbar", troughcolor="#e4ecf5", background=COLOR_ACENTO, thickness=8, borderwidth=0)
        style.configure("General.Horizontal.TProgressbar", troughcolor="#dbe6f2", background=COLOR_OK, thickness=12, borderwidth=0)

    def _construir_fondo(self):
        self.canvas_fondo = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas_fondo.pack(fill="both", expand=True)
        self.canvas_fondo.bind("<Configure>", self._redibujar_gradiente)

    def _redibujar_gradiente(self, event):
        self.canvas_fondo.delete("gradiente")
        w, h = event.width, event.height
        pasos = 60
        c1 = self.root.winfo_rgb(COLOR_FONDO_1)
        c2 = self.root.winfo_rgb(COLOR_FONDO_2)
        for i in range(pasos):
            t = i / pasos
            r = int((c1[0] + (c2[0] - c1[0]) * t) / 256)
            g = int((c1[1] + (c2[1] - c1[1]) * t) / 256)
            b = int((c1[2] + (c2[2] - c1[2]) * t) / 256)
            color = f"#{r:02x}{g:02x}{b:02x}"
            y0, y1 = int(h * i / pasos), int(h * (i + 1) / pasos)
            self.canvas_fondo.create_rectangle(0, y0, w, y1, fill=color, outline="", tags="gradiente")
        self.canvas_fondo.tag_lower("gradiente")
        if hasattr(self, "panel_id"):
            self.canvas_fondo.tag_raise(self.panel_id)

    # ---------- UI ----------
    def _construir_ui(self):
        contenedor_scroll = ttk.Frame(self.canvas_fondo, style="Card.TFrame")
        self.panel_id = self.canvas_fondo.create_window(20, 20, anchor="nw", window=contenedor_scroll, tags="panel_widgets")

        canvas_scroll = tk.Canvas(contenedor_scroll, bg=COLOR_PANEL_SOLIDO, highlightthickness=0, width=660, height=880)
        scrollbar = ttk.Scrollbar(contenedor_scroll, orient="vertical", command=canvas_scroll.yview)
        cont = ttk.Frame(canvas_scroll, style="Card.TFrame", padding=20)
        cont.bind("<Configure>", lambda e: canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all")))
        canvas_scroll.create_window((0, 0), window=cont, anchor="nw")
        canvas_scroll.configure(yscrollcommand=scrollbar.set)
        canvas_scroll.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(cont, text="Reparador de pixeles quemados", style="Titulo.TLabel").pack(anchor="w")
        estado_gpu = "GPU (NVENC) detectada" if self.usar_nvenc else "Usando CPU (no se detecto GPU NVIDIA)"
        ttk.Label(cont, text=estado_gpu, style="Info.TLabel").pack(anchor="w", pady=(2, 14))

        # 1) clips
        ttk.Label(cont, text="1) Clips de video", style="Seccion.TLabel").pack(anchor="w")
        fila1 = ttk.Frame(cont, style="Card.TFrame")
        fila1.pack(fill="x", pady=(4, 14))
        ttk.Button(fila1, text="Cargar clips...", style="Glass.TButton", command=self.cargar_clips).pack(side="left")
        self.lbl_clips = ttk.Label(fila1, text="Ningun clip cargado", style="CardText.TLabel")
        self.lbl_clips.pack(side="left", padx=12)

        # 2) mascara + grosor/calado + preview
        ttk.Label(cont, text="2) Mascara: cargar y ajustar", style="Seccion.TLabel").pack(anchor="w")
        fila2 = ttk.Frame(cont, style="Card.TFrame")
        fila2.pack(fill="x", pady=(4, 6))
        ttk.Button(fila2, text="Cargar mascara...", style="Glass.TButton", command=self.cargar_mascara).pack(side="left")
        self.lbl_mask = ttk.Label(fila2, text="Ninguna mascara cargada", style="CardText.TLabel")
        self.lbl_mask.pack(side="left", padx=12)

        fila_mascara_editor = ttk.Frame(cont, style="Card.TFrame")
        fila_mascara_editor.pack(fill="x", pady=(6, 4))

        self.canvas_mascara = tk.Canvas(fila_mascara_editor, width=180, height=180, bg="#111318", highlightthickness=1, highlightbackground="#c7d6e8")
        self.canvas_mascara.pack(side="left", padx=(0, 16))

        panel_sliders = ttk.Frame(fila_mascara_editor, style="Card.TFrame")
        panel_sliders.pack(side="left", fill="x", expand=True)

        ttk.Label(panel_sliders, text="Grosor (agranda la zona marcada)", style="CardText.TLabel").pack(anchor="w")
        self.grosor_var = tk.IntVar(value=0)
        ttk.Scale(panel_sliders, from_=0, to=20, orient="horizontal", variable=self.grosor_var,
                  command=self._on_slider_mascara_cambio).pack(fill="x")

        ttk.Label(panel_sliders, text="Calado (suaviza el borde)", style="CardText.TLabel").pack(anchor="w", pady=(8, 0))
        self.calado_var = tk.IntVar(value=3)
        ttk.Scale(panel_sliders, from_=0, to=20, orient="horizontal", variable=self.calado_var,
                  command=self._on_slider_mascara_cambio).pack(fill="x")

        ttk.Label(panel_sliders, text="Intensidad del desenfoque", style="CardText.TLabel").pack(anchor="w", pady=(8, 0))
        self.sigma_var = tk.IntVar(value=15)
        ttk.Scale(panel_sliders, from_=1, to=40, orient="horizontal", variable=self.sigma_var,
                  command=self._on_slider_mascara_cambio).pack(fill="x")

        # 3) vista previa del desenfoque
        ttk.Label(cont, text="3) Vista previa del resultado (sobre un frame real)", style="Seccion.TLabel").pack(anchor="w", pady=(14, 0))
        fila3 = ttk.Frame(cont, style="Card.TFrame")
        fila3.pack(fill="x", pady=(4, 4))
        ttk.Button(fila3, text="Generar vista previa", style="Glass.TButton", command=self.generar_preview_click).pack(side="left")
        self.lbl_preview_estado = ttk.Label(fila3, text="", style="CardSub.TLabel")
        self.lbl_preview_estado.pack(side="left", padx=12)

        fila_preview_img = ttk.Frame(cont, style="Card.TFrame")
        fila_preview_img.pack(fill="x", pady=(6, 4))
        marco_antes = ttk.Frame(fila_preview_img, style="Card.TFrame")
        marco_antes.pack(side="left", padx=(0, 14))
        ttk.Label(marco_antes, text="Antes", style="CardSub.TLabel").pack()
        self.canvas_antes = tk.Canvas(marco_antes, width=220, height=220, bg="#111318", highlightthickness=1, highlightbackground="#c7d6e8")
        self.canvas_antes.pack()
        marco_despues = ttk.Frame(fila_preview_img, style="Card.TFrame")
        marco_despues.pack(side="left")
        ttk.Label(marco_despues, text="Despues", style="CardSub.TLabel").pack()
        self.canvas_despues = tk.Canvas(marco_despues, width=220, height=220, bg="#111318", highlightthickness=1, highlightbackground="#c7d6e8")
        self.canvas_despues.pack()

        # 4) calidad / peso
        ttk.Label(cont, text="4) Calidad y peso del archivo final", style="Seccion.TLabel").pack(anchor="w", pady=(14, 0))
        fila4a = ttk.Frame(cont, style="Card.TFrame")
        fila4a.pack(fill="x", pady=(4, 4))
        ttk.Button(fila4a, text="Analizar clip original", style="Glass.TButton", command=self.analizar_click).pack(side="left")
        self.lbl_analisis = ttk.Label(fila4a, text="Sin analizar todavia", style="CardSub.TLabel")
        self.lbl_analisis.pack(side="left", padx=12)

        fila4b = ttk.Frame(cont, style="Card.TFrame")
        fila4b.pack(fill="x", pady=(8, 4))
        self.modo_calidad_var = tk.StringVar(value="bitrate")
        ttk.Radiobutton(fila4b, text="Igualar bitrate original (mas parecido posible)",
                        value="bitrate", variable=self.modo_calidad_var,
                        command=self._actualizar_texto_calidad).pack(anchor="w")
        ttk.Radiobutton(fila4b, text="Calidad fija (CRF) — velocidad configurable",
                        value="crf", variable=self.modo_calidad_var,
                        command=self._actualizar_texto_calidad).pack(anchor="w")

        fila4c = ttk.Frame(cont, style="Card.TFrame")
        fila4c.pack(fill="x", pady=(4, 4))
        self.velocidad_var = tk.StringVar(value="Equilibrado")
        for nombre in PRESETS_VELOCIDAD:
            ttk.Radiobutton(fila4c, text=nombre, value=nombre, variable=self.velocidad_var,
                            command=self._actualizar_texto_calidad).pack(side="left", padx=(0, 14))

        fila4d = ttk.Frame(cont, style="Card.TFrame")
        fila4d.pack(fill="x", pady=(8, 4))
        ttk.Label(fila4d, text="Resolucion de salida:", style="CardText.TLabel").pack(side="left")
        self.resolucion_var = tk.StringVar(value="Original")
        combo = ttk.Combobox(fila4d, textvariable=self.resolucion_var, values=list(RESOLUCIONES.keys()), state="readonly", width=22)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", lambda e: self._actualizar_texto_calidad())

        ttk.Label(fila4d, text="Clips en paralelo:", style="CardText.TLabel").pack(side="left", padx=(20, 4))
        self.paralelo_var = tk.IntVar(value=MAX_WORKERS_DEFAULT)
        ttk.Spinbox(fila4d, from_=1, to=6, width=3, textvariable=self.paralelo_var).pack(side="left")

        self.lbl_explicacion_calidad = ttk.Label(cont, text="", style="Aviso.TLabel", wraplength=600, justify="left")
        self.lbl_explicacion_calidad.pack(anchor="w", pady=(8, 4))
        self._actualizar_texto_calidad()

        # boton procesar
        self.btn_procesar = ttk.Button(cont, text="Procesar", style="Accento.TButton", command=self.procesar_todo)
        self.btn_procesar.pack(pady=(10, 16))

        # progreso general
        ttk.Label(cont, text="Progreso general", style="Seccion.TLabel").pack(anchor="w")
        self.barra_general = ttk.Progressbar(cont, style="General.Horizontal.TProgressbar", orient="horizontal", mode="determinate", maximum=100)
        self.barra_general.pack(fill="x", pady=(4, 4))
        self.lbl_general = ttk.Label(cont, text="0 / 0 clips", style="Info.TLabel")
        self.lbl_general.pack(anchor="w", pady=(0, 12))

        self.frame_tarjetas = ttk.Frame(cont, style="Card.TFrame")
        self.frame_tarjetas.pack(fill="both", expand=False, pady=(0, 10))

        ttk.Label(cont, text="Registro", style="Seccion.TLabel").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(cont, height=8, font=("Consolas", 9), bg="#0f1115", fg="#d7e3f4", borderwidth=0)
        self.log_box.pack(fill="both", expand=True, pady=(4, 0))

    def _verificar_dependencias_inicio(self):
        if not verificar_binario(FFMPEG_BIN):
            self.log("AVISO: no se encontro ffmpeg.")
        if not verificar_binario(FFPROBE_BIN):
            self.log("AVISO: no se encontro ffprobe.")

    def log(self, texto):
        def _agregar():
            self.log_box.insert("end", texto + "\n")
            self.log_box.see("end")
        self.root.after(0, _agregar)

    # ---------- calidad: texto explicativo dinamico ----------
    def _actualizar_texto_calidad(self):
        modo = self.modo_calidad_var.get()
        resolucion = self.resolucion_var.get()
        partes = []
        if modo == "bitrate":
            partes.append(
                "Se va a intentar igualar el bitrate del video original para que el peso y la calidad "
                "queden lo mas parecido posible. No va a ser un archivo idéntico bit a bit (eso no es "
                "posible al modificar píxeles), pero sí muy cercano visualmente."
            )
        else:
            crf = PRESETS_VELOCIDAD[self.velocidad_var.get()]["crf"]
            partes.append(
                f"Se va a usar calidad fija (CRF {crf}) en vez de igualar el peso original — el archivo "
                "resultante puede pesar más o menos que el original según la complejidad de la imagen, "
                "pero la calidad visual va a ser consistente."
            )
        if resolucion != "Original":
            partes.append(
                f"Ademas vas a reducir la resolucion a {resolucion}: esto baja bastante el peso final y "
                "acelera la codificacion, pero se pierde detalle respecto al original — no recomendado "
                "si vas a hacer zoom o correcciones de color despues."
            )
        else:
            partes.append("La resolucion se mantiene igual a la del original.")
        self.lbl_explicacion_calidad.config(text=" ".join(partes))

    # ---------- mascara: preview con grosor/calado ----------
    def cargar_mascara(self):
        archivo = filedialog.askopenfilename(title="Elegi la mascara PNG",
                                              filetypes=[("Imagenes", "*.png *.jpg *.jpeg"), ("Todos", "*.*")])
        if not archivo:
            return
        try:
            self.mascara_arr_base = mascara_base_desde_color(archivo)
        except Exception as e:
            messagebox.showerror("Mascara invalida", str(e))
            return
        self.mascara = archivo
        self.lbl_mask.config(text=os.path.basename(archivo))
        self._recalcular_mascara_procesada()

    def _on_slider_mascara_cambio(self, _valor=None):
        # debounce para no recalcular en cada micro-movimiento del slider
        if self._debounce_id:
            self.root.after_cancel(self._debounce_id)
        self._debounce_id = self.root.after(150, self._recalcular_mascara_procesada)

    def _recalcular_mascara_procesada(self):
        if self.mascara_arr_base is None:
            return
        try:
            self.mascara_procesada_img = aplicar_grosor_y_calado(
                self.mascara_arr_base, self.grosor_var.get(), self.calado_var.get()
            )
        except Exception as e:
            self.log(f"ERROR ajustando la mascara: {e}")
            return
        self._dibujar_thumbnail_mascara()

    def _dibujar_thumbnail_mascara(self):
        img = self.mascara_procesada_img.copy()
        img.thumbnail((180, 180))
        self._tk_img_mascara = ImageTk.PhotoImage(img)
        self.canvas_mascara.delete("all")
        self.canvas_mascara.create_image(90, 90, image=self._tk_img_mascara)

    # ---------- vista previa del desenfoque ----------
    def generar_preview_click(self):
        if not self.clips:
            messagebox.showwarning("Falta info", "Carga al menos un clip primero.")
            return
        if self.mascara_procesada_img is None:
            messagebox.showwarning("Falta info", "Carga y ajusta la mascara primero.")
            return
        self.lbl_preview_estado.config(text="Extrayendo frame de prueba...")
        threading.Thread(target=self._generar_preview_worker, daemon=True).start()

    def _generar_preview_worker(self):
        try:
            tmp_dir = os.path.join(os.path.dirname(self.clips[0]), "reparados")
            os.makedirs(tmp_dir, exist_ok=True)
            ruta_frame = os.path.join(tmp_dir, "_frame_prueba.png")
            extraer_frame(self.clips[0], ruta_frame)
            frame_img = Image.open(ruta_frame)

            antes, despues = generar_vista_previa_blur(
                frame_img, self.mascara_procesada_img, self.sigma_var.get()
            )

            self._tk_antes = ImageTk.PhotoImage(antes)
            self._tk_despues = ImageTk.PhotoImage(despues)

            def _mostrar():
                self.canvas_antes.delete("all")
                self.canvas_antes.create_image(110, 110, image=self._tk_antes)
                self.canvas_despues.delete("all")
                self.canvas_despues.create_image(110, 110, image=self._tk_despues)
                self.lbl_preview_estado.config(text="Vista previa lista (recorte ampliado de la zona marcada)")
            self.root.after(0, _mostrar)
        except Exception as e:
            self.root.after(0, lambda: self.lbl_preview_estado.config(text=f"Error: {e}"))

    # ---------- calidad: analizar ----------
    def analizar_click(self):
        if not self.clips:
            messagebox.showwarning("Falta info", "Carga al menos un clip primero.")
            return
        self.lbl_analisis.config(text="Analizando...")
        threading.Thread(target=self._analizar_worker, daemon=True).start()

    def _analizar_worker(self):
        info = analizar_clip(self.clips[0])
        if not info:
            self.root.after(0, lambda: self.lbl_analisis.config(text="No se pudo analizar el clip."))
            return
        mbps = (info["bitrate_bps"] / 1_000_000) if info["bitrate_bps"] else None
        texto = (
            f"{info['ancho']}x{info['alto']} · {info['codec']} · {info['fps']:.0f}fps · "
            f"{mbps:.1f} Mbps · {info['peso_mb']:.0f} MB" if mbps and info["peso_mb"]
            else f"{info['ancho']}x{info['alto']} · {info['codec']}"
        )
        self.root.after(0, lambda: self.lbl_analisis.config(text=texto))

    # ---------- clips ----------
    def cargar_clips(self):
        archivos = filedialog.askopenfilenames(title="Elegi uno o varios clips",
                                                filetypes=[("Videos", "*.mp4 *.mov *.mxf *.avi"), ("Todos", "*.*")])
        if archivos:
            self.clips = list(archivos)
            self.lbl_clips.config(text=f"{len(self.clips)} clip(s) cargado(s)")
            self._reconstruir_tarjetas()

    def _reconstruir_tarjetas(self):
        for w in self.frame_tarjetas.winfo_children():
            w.destroy()
        self.tarjetas = {}
        for clip in self.clips:
            t = TarjetaClip(self.frame_tarjetas, os.path.basename(clip))
            t.pack(fill="x", pady=4)
            self.tarjetas[clip] = t

    # ---------- procesar ----------
    def procesar_todo(self):
        if not self.clips:
            messagebox.showwarning("Falta info", "Carga al menos un clip.")
            return
        if self.mascara_procesada_img is None:
            messagebox.showwarning("Falta info", "Carga la mascara.")
            return
        if not verificar_binario(FFMPEG_BIN):
            messagebox.showerror("Falta ffmpeg", "No se encontro ffmpeg.")
            return

        self.btn_procesar.config(state="disabled", text="Procesando...")
        self.barra_general["value"] = 0
        self.lbl_general.config(text=f"0 / {len(self.clips)} clips")
        threading.Thread(target=self._procesar_todo_worker, daemon=True).start()

    def _procesar_todo_worker(self):
        try:
            carpeta_salida = os.path.join(os.path.dirname(self.clips[0]), "reparados")
            os.makedirs(carpeta_salida, exist_ok=True)

            mascara_bn_path = os.path.join(carpeta_salida, "_mascara_bn.png")
            self.mascara_procesada_img.save(mascara_bn_path)

            modo_calidad = self.modo_calidad_var.get()
            preset_info = PRESETS_VELOCIDAD[self.velocidad_var.get()]
            resolucion_objetivo = RESOLUCIONES[self.resolucion_var.get()]
            sigma_blur = self.sigma_var.get()
            n_paralelo = max(1, min(6, self.paralelo_var.get()))

            completados = {"n": 0}
            total = len(self.clips)

            def tarea(clip):
                tarjeta = self.tarjetas.get(clip)

                def callback(frac):
                    if tarjeta:
                        self.root.after(0, tarjeta.actualizar, frac, f"{int(frac*100)}%", None)

                if tarjeta:
                    self.root.after(0, tarjeta.actualizar, 0, "Procesando...", COLOR_ACENTO)

                exito, resultado = procesar_clip(
                    clip, mascara_bn_path, carpeta_salida, sigma_blur,
                    modo_calidad, preset_info, resolucion_objetivo,
                    self.usar_nvenc, callback,
                )

                if tarjeta:
                    self.root.after(0, tarjeta.actualizar, 1.0 if exito else 0,
                                    "Listo" if exito else "Error", COLOR_OK if exito else COLOR_ERROR)

                completados["n"] += 1
                pct = completados["n"] / total * 100
                self.root.after(0, lambda: self.barra_general.configure(value=pct))
                self.root.after(0, lambda: self.lbl_general.configure(text=f"{completados['n']} / {total} clips"))

                if exito:
                    self.log(f"OK: {os.path.basename(clip)} -> {os.path.basename(resultado)}")
                else:
                    self.log(f"ERROR: {os.path.basename(clip)} -> {resultado}")
                return exito

            resultados = []
            with ThreadPoolExecutor(max_workers=n_paralelo) as ejecutor:
                futuros = {ejecutor.submit(tarea, clip): clip for clip in self.clips}
                for fut in as_completed(futuros):
                    try:
                        resultados.append(fut.result())
                    except Exception as e:
                        self.log(f"ERROR inesperado: {e}")
                        resultados.append(False)

            ok = sum(1 for r in resultados if r)
            self.log(f"\nListo. {ok} clip(s) reparado(s), {total-ok} con error.")
            self.log(f"Carpeta de salida: {carpeta_salida}")
        except Exception:
            self.log("ERROR inesperado:")
            self.log(traceback.format_exc())
        finally:
            self.root.after(0, lambda: self.btn_procesar.config(state="normal", text="Procesar"))


if __name__ == "__main__":
    root = tk.Tk()
    try:
        root.attributes("-alpha", 0.99)
    except Exception:
        pass
    App(root)
    root.mainloop()
