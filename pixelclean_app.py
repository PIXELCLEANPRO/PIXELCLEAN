"""
PixelClean - Reparador de pixeles quemados
Interfaz por pasos (Clip -> Mascara -> Motor y calidad -> Procesar) con
vista previa en tiempo real con zoom, editor de mascara con pincel, y
tema claro/oscuro con estilo "vidrio" (tarjetas redondeadas, acentos y
anillo de progreso circular).
"""
import os
import sys
import threading
import traceback

import customtkinter as ctk
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageTk

import metadata_camara
import motores_reparacion as motores

try:
    import cv2
except ImportError:
    cv2 = None


# ============================================================
# CONFIGURACION / RUTAS
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
RUTA_ICONO = _ruta_local(os.path.join("assets", "logo.ico"))

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

MOTORES_INFO = {
    "blur": {"nombre": "Blur gaussiano", "detalle": "Rapido, difumina la zona marcada."},
    "opencv": {"nombre": "OpenCV inpainting", "detalle": "Recomendado: reconstruye siguiendo bordes y textura."},
}

MARCAS_COLOR = {
    "Sony": "#FF6600", "Canon": "#CC0000", "Nikon": "#FFCC00", "Panasonic": "#003087",
    "Fujifilm": "#00A651", "Blackmagic": "#E4002B", "GoPro": "#00B5E2", "DJI": "#000000", "Apple": "#555555",
}

PASOS = ["Clip", "Mascara", "Motor y calidad", "Procesar"]

# Paleta (light, dark) - CustomTkinter resuelve el par automaticamente segun el modo activo.
COLOR = {
    "bg_page":       ("#eef2f9", "#0b0d12"),
    "bg_card":       ("#ffffff", "#151822"),
    "bg_card_alt":   ("#f3f6fb", "#1b1f2b"),
    "accent":        ("#2f7bde", "#3aa0ff"),
    "accent_hover":  ("#2566bd", "#4db1ff"),
    "text_primary":  ("#161a23", "#f2f4f8"),
    "text_secondary": ("#5b6473", "#9aa3b8"),
    "border":        ("#e1e6f0", "#262b3a"),
    "success":       ("#1f9d63", "#34d399"),
    "pill_inactive": ("#e4e9f2", "#1c2030"),
    "canvas_bg":     ("#11131a", "#0c0e14"),
}


# ============================================================
# UTILIDADES DE MASCARA (compartidas con el editor)
# ============================================================
def mascara_desde_pintado_color(ruta_archivo):
    img = Image.open(ruta_archivo).convert("RGB")
    arr = np.asarray(img).astype(np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    verde = (g > r + GREEN_THRESHOLD) & (g > b + GREEN_THRESHOLD)
    if verde.any():
        return Image.fromarray((verde.astype(np.uint8)) * 255, mode="L")
    return Image.open(ruta_archivo).convert("L")


def reparar_crop_preview(motor_id, crop_rgb, mascara_crop_u8, sigma_blur=15):
    """Repara un recorte (numpy RGB) usando el motor elegido, para vista previa rapida."""
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


# ============================================================
# WIDGETS PROPIOS: anillo de progreso y panel de vista previa con zoom
# ============================================================
class AnilloProgreso(ctk.CTkFrame):
    def __init__(self, parent, tam=150, grosor=14, **kwargs):
        super().__init__(parent, fg_color="transparent", width=tam, height=tam, **kwargs)
        self.tam = tam
        self.grosor = grosor
        self.valor = 0.0
        self.canvas = ctk.CTkCanvas(self, width=tam, height=tam, highlightthickness=0)
        self.canvas.place(x=0, y=0)
        self.lbl = ctk.CTkLabel(self, text="0%", font=ctk.CTkFont(size=24, weight="bold"),
                                 text_color=COLOR["text_primary"])
        self.lbl.place(relx=0.5, rely=0.5, anchor="center")
        self._dibujar()

    def set(self, valor):
        self.valor = max(0.0, min(1.0, valor))
        self.lbl.configure(text=f"{int(self.valor * 100)}%")
        self._dibujar()

    def _dibujar(self):
        self.canvas.delete("all")
        self.canvas.configure(bg=self._apply_appearance_mode(COLOR["bg_page"]))
        pad = self.grosor
        color_track = self._apply_appearance_mode(COLOR["border"])
        color_fill = self._apply_appearance_mode(COLOR["accent"])
        self.canvas.create_oval(pad, pad, self.tam - pad, self.tam - pad, outline=color_track, width=self.grosor)
        if self.valor > 0:
            extent = -360 * self.valor
            self.canvas.create_arc(pad, pad, self.tam - pad, self.tam - pad, start=90, extent=extent,
                                    style="arc", outline=color_fill, width=self.grosor)


class PanelVistaPrevia(ctk.CTkFrame):
    """Tarjeta con titulo, imagen con zoom (+/-, rueda del mouse) y boton para expandir."""

    def __init__(self, parent, titulo, tam_base=210, **kwargs):
        super().__init__(parent, corner_radius=18, fg_color=COLOR["bg_card_alt"], **kwargs)
        self.titulo = titulo
        self.imagen_pil = None
        self.zoom = 1.0
        self.tam_base = tam_base

        cabecera = ctk.CTkFrame(self, fg_color="transparent")
        cabecera.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(cabecera, text=titulo, font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=COLOR["text_secondary"]).pack(side="left")
        ctk.CTkButton(cabecera, text="⤢", width=28, height=26, corner_radius=8,
                      fg_color=COLOR["bg_card"], text_color=COLOR["text_primary"],
                      hover_color=COLOR["border"], command=self._expandir).pack(side="right", padx=(4, 0))
        ctk.CTkButton(cabecera, text="+", width=26, height=26, corner_radius=8,
                      fg_color=COLOR["bg_card"], text_color=COLOR["text_primary"],
                      hover_color=COLOR["border"], command=lambda: self._cambiar_zoom(0.2)).pack(side="right", padx=2)
        ctk.CTkButton(cabecera, text="-", width=26, height=26, corner_radius=8,
                      fg_color=COLOR["bg_card"], text_color=COLOR["text_primary"],
                      hover_color=COLOR["border"], command=lambda: self._cambiar_zoom(-0.2)).pack(side="right", padx=2)

        self.marco_img = ctk.CTkFrame(self, fg_color=COLOR["canvas_bg"], corner_radius=14)
        self.marco_img.pack(padx=14, pady=(0, 14), fill="both", expand=True)
        self.lbl_img = ctk.CTkLabel(self.marco_img, text="", fg_color="transparent")
        self.lbl_img.pack(padx=8, pady=8)
        self.lbl_img.bind("<MouseWheel>", lambda e: self._cambiar_zoom(0.15 if e.delta > 0 else -0.15))

    def set_imagen(self, imagen_pil):
        self.imagen_pil = imagen_pil
        self._redibujar()

    def _cambiar_zoom(self, delta):
        self.zoom = max(0.6, min(3.0, round(self.zoom + delta, 2)))
        self._redibujar()

    def _redibujar(self):
        if self.imagen_pil is None:
            return
        tam = max(40, int(self.tam_base * self.zoom))
        self._ctk_img = ctk.CTkImage(light_image=self.imagen_pil, dark_image=self.imagen_pil, size=(tam, tam))
        self.lbl_img.configure(image=self._ctk_img, text="")

    def _expandir(self):
        if self.imagen_pil is None:
            return
        top = ctk.CTkToplevel(self)
        top.title(self.titulo)
        top.geometry("720x760")
        top.configure(fg_color=COLOR["bg_page"])
        top.after(50, top.lift)
        top.after(60, top.focus_force)
        ctk.CTkLabel(top, text=self.titulo, font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=COLOR["text_primary"]).pack(pady=(18, 10))
        tam = 660
        ctk_img = ctk.CTkImage(light_image=self.imagen_pil, dark_image=self.imagen_pil, size=(tam, tam))
        marco = ctk.CTkFrame(top, fg_color=COLOR["canvas_bg"], corner_radius=16)
        marco.pack(padx=20, pady=10)
        lbl = ctk.CTkLabel(marco, image=ctk_img, text="")
        lbl.image = ctk_img
        lbl.pack(padx=10, pady=10)


# ============================================================
# EDITOR DE MASCARA (pincel, goma, deshacer, rotar, zoom)
# ============================================================
class EditorMascara(ctk.CTkFrame):
    TAM_LIENZO = 480

    def __init__(self, parent, on_cambio=None, **kwargs):
        super().__init__(parent, corner_radius=18, fg_color=COLOR["bg_card"], **kwargs)
        self.on_cambio = on_cambio
        self.frame_video = None
        self.mascara = None
        self.historial = []
        self.indice_historial = -1
        self.modo = "pincel"
        self.grosor_pincel = ctk.IntVar(value=28)
        self.zoom = 1.0
        self._ultimo_punto = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        barra = ctk.CTkFrame(self, fg_color="transparent")
        barra.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 6))
        self.btn_pincel = ctk.CTkButton(barra, text="Pincel", width=74, corner_radius=10,
                                         fg_color=COLOR["accent"], hover_color=COLOR["accent_hover"],
                                         command=lambda: self._set_modo("pincel"))
        self.btn_pincel.pack(side="left", padx=(0, 6))
        self.btn_goma = ctk.CTkButton(barra, text="Goma", width=74, corner_radius=10,
                                       fg_color=COLOR["pill_inactive"], text_color=COLOR["text_primary"],
                                       hover_color=COLOR["border"], command=lambda: self._set_modo("goma"))
        self.btn_goma.pack(side="left", padx=(0, 6))
        for texto, ancho, cmd in [
            ("Deshacer", 84, self.deshacer), ("Rehacer", 84, self.rehacer),
            ("Rotar 90°", 90, lambda: self.rotar(90)),
        ]:
            ctk.CTkButton(barra, text=texto, width=ancho, corner_radius=10,
                          fg_color=COLOR["pill_inactive"], text_color=COLOR["text_primary"],
                          hover_color=COLOR["border"], command=cmd).pack(side="left", padx=(0, 6))
        ctk.CTkButton(barra, text="Cargar PNG...", width=110, corner_radius=10,
                      fg_color=COLOR["pill_inactive"], text_color=COLOR["text_primary"],
                      hover_color=COLOR["border"], command=self._cargar_png).pack(side="left", padx=(0, 6))
        ctk.CTkButton(barra, text="+", width=30, corner_radius=10,
                      fg_color=COLOR["pill_inactive"], text_color=COLOR["text_primary"],
                      hover_color=COLOR["border"], command=lambda: self._cambiar_zoom(0.15)).pack(side="right", padx=2)
        ctk.CTkButton(barra, text="-", width=30, corner_radius=10,
                      fg_color=COLOR["pill_inactive"], text_color=COLOR["text_primary"],
                      hover_color=COLOR["border"], command=lambda: self._cambiar_zoom(-0.15)).pack(side="right", padx=2)

        marco_lienzo = ctk.CTkFrame(self, fg_color=COLOR["canvas_bg"], corner_radius=14)
        marco_lienzo.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 6))
        marco_lienzo.columnconfigure(0, weight=1)
        marco_lienzo.rowconfigure(0, weight=1)
        self.lienzo = ctk.CTkCanvas(marco_lienzo, bg=self._apply_appearance_mode(COLOR["canvas_bg"]), highlightthickness=0)
        self.lienzo.grid(row=0, column=0, sticky="nsew")
        self.lienzo.bind("<Button-1>", self._on_click)
        self.lienzo.bind("<B1-Motion>", self._on_drag)
        self.lienzo.bind("<ButtonRelease-1>", self._on_release)
        self.lienzo.bind("<MouseWheel>", self._on_rueda)
        self.lienzo.bind("<Configure>", lambda e: self._redibujar())

        panel_inferior = ctk.CTkFrame(self, fg_color="transparent")
        panel_inferior.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))
        panel_inferior.columnconfigure(1, weight=1)
        ctk.CTkLabel(panel_inferior, text="Grosor del pincel", text_color=COLOR["text_secondary"]).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkSlider(panel_inferior, from_=4, to=80, variable=self.grosor_pincel,
                       progress_color=COLOR["accent"], button_color=COLOR["accent"],
                       button_hover_color=COLOR["accent_hover"]).grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(panel_inferior, text="Angulo fino", text_color=COLOR["text_secondary"]).grid(row=1, column=0, padx=(0, 8), pady=(6, 0))
        self.angulo_var = ctk.DoubleVar(value=0)
        ctk.CTkSlider(panel_inferior, from_=-45, to=45, variable=self.angulo_var,
                       progress_color=COLOR["accent"], button_color=COLOR["accent"],
                       button_hover_color=COLOR["accent_hover"],
                       command=lambda v: self._rotar_a(float(v))).grid(row=1, column=1, sticky="ew", pady=(6, 0))

    def _set_modo(self, modo):
        self.modo = modo
        self.btn_pincel.configure(fg_color=COLOR["accent"] if modo == "pincel" else COLOR["pill_inactive"],
                                   text_color="white" if modo == "pincel" else COLOR["text_primary"])
        self.btn_goma.configure(fg_color=COLOR["accent"] if modo == "goma" else COLOR["pill_inactive"],
                                 text_color="white" if modo == "goma" else COLOR["text_primary"])

    def cargar_frame(self, frame_pil_rgb, mascara_inicial_l=None):
        self.frame_video = frame_pil_rgb
        self.mascara = mascara_inicial_l if mascara_inicial_l is not None else Image.new("L", frame_pil_rgb.size, 0)
        self.historial = [self.mascara.copy()]
        self.indice_historial = 0
        self._redibujar()

    def _cargar_png(self):
        from tkinter import filedialog
        archivo = filedialog.askopenfilename(title="Elegi la mascara PNG",
                                              filetypes=[("Imagenes", "*.png *.jpg *.jpeg"), ("Todos", "*.*")])
        if not archivo or self.frame_video is None:
            return
        try:
            cargada = mascara_desde_pintado_color(archivo).resize(self.frame_video.size)
        except Exception:
            return
        self.mascara = cargada
        self._push_historial()
        self._redibujar()
        if self.on_cambio:
            self.on_cambio()

    def _cambiar_zoom(self, delta):
        self.zoom = max(0.5, min(4.0, round(self.zoom + delta, 2)))
        self._redibujar()

    def _coords_a_imagen(self, x, y):
        w_lienzo = self.lienzo.winfo_width() or self.TAM_LIENZO
        h_lienzo = self.lienzo.winfo_height() or self.TAM_LIENZO
        escala = min(w_lienzo / self.frame_video.width, h_lienzo / self.frame_video.height) * self.zoom
        off_x = (w_lienzo - self.frame_video.width * escala) / 2
        off_y = (h_lienzo - self.frame_video.height * escala) / 2
        return (x - off_x) / escala, (y - off_y) / escala

    def _on_click(self, evento):
        self._ultimo_punto = self._coords_a_imagen(evento.x, evento.y)
        self._pintar_en(evento)

    def _on_drag(self, evento):
        self._pintar_en(evento)

    def _on_release(self, _evento):
        self._ultimo_punto = None
        self._push_historial()
        if self.on_cambio:
            self.on_cambio()

    def _on_rueda(self, evento):
        self._cambiar_zoom(0.1 if evento.delta > 0 else -0.1)

    def _pintar_en(self, evento):
        if self.frame_video is None:
            return
        x_img, y_img = self._coords_a_imagen(evento.x, evento.y)
        draw = ImageDraw.Draw(self.mascara)
        color = 255 if self.modo == "pincel" else 0
        r = self.grosor_pincel.get()
        if self._ultimo_punto:
            draw.line([self._ultimo_punto, (x_img, y_img)], fill=color, width=r * 2)
        draw.ellipse([x_img - r, y_img - r, x_img + r, y_img + r], fill=color)
        self._ultimo_punto = (x_img, y_img)
        self._redibujar()
        if self.on_cambio:
            self.on_cambio()

    def rotar(self, grados):
        if self.mascara is None:
            return
        self.mascara = self.mascara.rotate(grados, expand=False, fillcolor=0)
        self._push_historial()
        self._redibujar()
        if self.on_cambio:
            self.on_cambio()

    def _rotar_a(self, grados):
        if self.mascara is None or not self.historial:
            return
        base = self.historial[self.indice_historial]
        self.mascara = base.rotate(grados, expand=False, fillcolor=0)
        self._redibujar()
        if self.on_cambio:
            self.on_cambio()

    def _push_historial(self):
        self.historial = self.historial[: self.indice_historial + 1]
        self.historial.append(self.mascara.copy())
        self.indice_historial = len(self.historial) - 1
        if len(self.historial) > 25:
            self.historial.pop(0)
            self.indice_historial -= 1

    def deshacer(self):
        if self.indice_historial > 0:
            self.indice_historial -= 1
            self.mascara = self.historial[self.indice_historial].copy()
            self._redibujar()
            if self.on_cambio:
                self.on_cambio()

    def rehacer(self):
        if self.indice_historial < len(self.historial) - 1:
            self.indice_historial += 1
            self.mascara = self.historial[self.indice_historial].copy()
            self._redibujar()
            if self.on_cambio:
                self.on_cambio()

    def _redibujar(self):
        self.lienzo.delete("all")
        self.lienzo.configure(bg=self._apply_appearance_mode(COLOR["canvas_bg"]))
        if self.frame_video is None:
            return
        w_lienzo = self.lienzo.winfo_width() or self.TAM_LIENZO
        h_lienzo = self.lienzo.winfo_height() or self.TAM_LIENZO
        escala = min(w_lienzo / self.frame_video.width, h_lienzo / self.frame_video.height) * self.zoom
        tam = (max(1, int(self.frame_video.width * escala)), max(1, int(self.frame_video.height * escala)))

        overlay = Image.new("RGBA", self.frame_video.size, (0, 0, 0, 0))
        capa_verde = Image.new("RGBA", self.frame_video.size, (58, 160, 255, 130))
        overlay.paste(capa_verde, (0, 0), self.mascara)
        compuesto = Image.alpha_composite(self.frame_video.convert("RGBA"), overlay).convert("RGB").resize(tam)

        self._tk_img = ImageTk.PhotoImage(compuesto)
        self.lienzo.create_image(w_lienzo / 2, h_lienzo / 2, image=self._tk_img)


# ============================================================
# APP PRINCIPAL
# ============================================================
class PixelCleanApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PixelClean - Reparador de pixeles quemados")
        self.geometry("1220x820")
        self.minsize(1000, 700)
        self.configure(fg_color=COLOR["bg_page"])
        if os.path.isfile(RUTA_ICONO):
            try:
                self.iconbitmap(RUTA_ICONO)
            except Exception:
                pass

        self.clips = []
        self.frame_muestra = None
        self.paso_actual = 0
        self.motor_id = ctk.StringVar(value="opencv")
        self.modo_calidad_var = ctk.StringVar(value="bitrate")
        self.velocidad_var = ctk.StringVar(value="Equilibrado")
        self.resolucion_var = ctk.StringVar(value="Original")
        self.sigma_var = ctk.IntVar(value=15)
        self.tarjetas_clips = {}
        self._debounce_id = None

        self._construir_layout()
        self._mostrar_paso(0)

    # ---------- layout general ----------
    def _construir_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        barra_superior = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        barra_superior.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        barra_superior.columnconfigure(1, weight=1)

        marca = ctk.CTkFrame(barra_superior, fg_color="transparent")
        marca.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(marca, text="▶", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=COLOR["accent"]).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(marca, text="PixelClean", font=ctk.CTkFont(size=19, weight="bold"),
                     text_color=COLOR["text_primary"]).pack(side="left")

        self.frame_pasos = ctk.CTkFrame(barra_superior, fg_color="transparent")
        self.frame_pasos.grid(row=0, column=1)
        self.labels_pasos = []
        for i, nombre in enumerate(PASOS):
            lbl = ctk.CTkLabel(self.frame_pasos, text=f"  {i+1}  {nombre}  ", corner_radius=999,
                                fg_color=COLOR["pill_inactive"], text_color=COLOR["text_secondary"],
                                font=ctk.CTkFont(size=12, weight="bold"), padx=6, pady=6)
            lbl.grid(row=0, column=i, padx=4)
            self.labels_pasos.append(lbl)

        self.switch_tema = ctk.CTkSwitch(barra_superior, text="Modo oscuro", command=self._toggle_tema,
                                          progress_color=COLOR["accent"], text_color=COLOR["text_secondary"])
        self.switch_tema.grid(row=0, column=2, sticky="e")

        self.contenedor_pasos = ctk.CTkFrame(self, fg_color="transparent")
        self.contenedor_pasos.grid(row=1, column=0, sticky="nsew", padx=20, pady=6)
        self.contenedor_pasos.columnconfigure(0, weight=1)
        self.contenedor_pasos.rowconfigure(0, weight=1)

        self.paneles = [
            self._crear_paso_clip(),
            self._crear_paso_mascara(),
            self._crear_paso_motor(),
            self._crear_paso_procesar(),
        ]
        for panel in self.paneles:
            panel.grid(row=0, column=0, sticky="nsew")

        barra_nav = ctk.CTkFrame(self, fg_color="transparent")
        barra_nav.grid(row=2, column=0, sticky="ew", padx=20, pady=(6, 18))
        barra_nav.columnconfigure(1, weight=1)
        self.btn_atras = ctk.CTkButton(barra_nav, text="Atras", corner_radius=999, width=110,
                                        fg_color=COLOR["pill_inactive"], text_color=COLOR["text_primary"],
                                        hover_color=COLOR["border"], command=self._paso_anterior)
        self.btn_atras.grid(row=0, column=0, sticky="w")
        self.lbl_nav_info = ctk.CTkLabel(barra_nav, text="", text_color=COLOR["text_secondary"])
        self.lbl_nav_info.grid(row=0, column=1)
        self.btn_siguiente = ctk.CTkButton(barra_nav, text="Siguiente", corner_radius=999, width=140,
                                            fg_color=COLOR["accent"], hover_color=COLOR["accent_hover"],
                                            font=ctk.CTkFont(weight="bold"), command=self._paso_siguiente)
        self.btn_siguiente.grid(row=0, column=2, sticky="e")

    def _toggle_tema(self):
        ctk.set_appearance_mode("dark" if self.switch_tema.get() else "light")
        self.after(50, self._refrescar_canvas)

    def _refrescar_canvas(self):
        if hasattr(self, "editor"):
            self.editor._redibujar()
        for panel in (getattr(self, "panel_preview_mascara", None), getattr(self, "panel_preview_despues_mascara", None),
                      getattr(self, "panel_preview_motor", None), getattr(self, "panel_preview_despues_motor", None)):
            if panel is not None:
                panel._redibujar()
        if hasattr(self, "anillo_general"):
            self.anillo_general._dibujar()

    def _mostrar_paso(self, indice):
        self.paso_actual = indice
        self.paneles[indice].tkraise()
        for i, lbl in enumerate(self.labels_pasos):
            if i == indice:
                lbl.configure(fg_color=COLOR["accent"], text_color="white")
            elif i < indice:
                lbl.configure(fg_color=COLOR["success"], text_color="white")
            else:
                lbl.configure(fg_color=COLOR["pill_inactive"], text_color=COLOR["text_secondary"])
        self.btn_atras.configure(state="normal" if indice > 0 else "disabled")
        self.btn_siguiente.configure(text="Procesar" if indice == len(PASOS) - 1 else "Siguiente")

    def _paso_anterior(self):
        if self.paso_actual > 0:
            self._mostrar_paso(self.paso_actual - 1)

    def _paso_siguiente(self):
        if self.paso_actual == 0 and not self.clips:
            self.lbl_nav_info.configure(text="Carga al menos un clip primero")
            return
        if self.paso_actual == 1 and self.editor.mascara is None:
            self.lbl_nav_info.configure(text="Pinta o carga una mascara primero")
            return
        self.lbl_nav_info.configure(text="")
        if self.paso_actual < len(PASOS) - 1:
            self._mostrar_paso(self.paso_actual + 1)
            if self.paso_actual == 2:
                self._actualizar_preview_motor()
        else:
            self._procesar_todo()

    # ---------- paso 1: clip ----------
    def _crear_paso_clip(self):
        panel = ctk.CTkFrame(self.contenedor_pasos, corner_radius=20, fg_color=COLOR["bg_card"])
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)

        fila = ctk.CTkFrame(panel, fg_color="transparent")
        fila.grid(row=0, column=0, sticky="ew", padx=22, pady=22)
        ctk.CTkButton(fila, text="Cargar clips...", corner_radius=999, fg_color=COLOR["accent"],
                      hover_color=COLOR["accent_hover"], font=ctk.CTkFont(weight="bold"),
                      command=self._cargar_clips).pack(side="left")
        self.lbl_clips = ctk.CTkLabel(fila, text="Ningun clip cargado", text_color=COLOR["text_secondary"])
        self.lbl_clips.pack(side="left", padx=14)

        cuerpo = ctk.CTkFrame(panel, fg_color="transparent")
        cuerpo.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 22))
        cuerpo.columnconfigure(0, weight=1)
        cuerpo.columnconfigure(1, weight=1)
        cuerpo.rowconfigure(0, weight=1)

        self.lista_clips_frame = ctk.CTkScrollableFrame(cuerpo, corner_radius=16, label_text="Clips cargados",
                                                          fg_color=COLOR["bg_card_alt"])
        self.lista_clips_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        panel_metadata = ctk.CTkFrame(cuerpo, corner_radius=16, fg_color=COLOR["bg_card_alt"])
        panel_metadata.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        panel_metadata.columnconfigure(0, weight=1)
        self.badge_marca = ctk.CTkLabel(panel_metadata, text="Sin datos de camara", corner_radius=999,
                                         fg_color=COLOR["pill_inactive"], text_color=COLOR["text_secondary"],
                                         font=ctk.CTkFont(weight="bold"), padx=16, pady=8)
        self.badge_marca.grid(row=0, column=0, padx=18, pady=(18, 10), sticky="w")
        self.lbl_metadata = ctk.CTkLabel(panel_metadata, text="Carga un clip para ver su metadata.",
                                          justify="left", anchor="nw", wraplength=380, text_color=COLOR["text_primary"])
        self.lbl_metadata.grid(row=1, column=0, sticky="nw", padx=18, pady=(0, 18))
        return panel

    def _cargar_clips(self):
        from tkinter import filedialog
        archivos = filedialog.askopenfilenames(title="Elegi uno o varios clips",
                                                filetypes=[("Videos", "*.mp4 *.mov *.mxf *.avi"), ("Todos", "*.*")])
        if not archivos:
            return
        self._establecer_clips(archivos)

    def _establecer_clips(self, archivos):
        self.clips = list(archivos)
        self.lbl_clips.configure(text=f"{len(self.clips)} clip(s) cargado(s)")
        for w in self.lista_clips_frame.winfo_children():
            w.destroy()
        self.tarjetas_clips = {}
        for clip in self.clips:
            fila = ctk.CTkFrame(self.lista_clips_frame, corner_radius=12, fg_color=COLOR["bg_card"])
            fila.pack(fill="x", pady=4)
            ctk.CTkLabel(fila, text=os.path.basename(clip), text_color=COLOR["text_primary"]).pack(side="left", padx=10, pady=10)
            barra = ctk.CTkProgressBar(fila, corner_radius=999, progress_color=COLOR["accent"])
            barra.set(0)
            barra.pack(side="right", padx=10, fill="x", expand=True)
            self.tarjetas_clips[clip] = barra
        threading.Thread(target=self._analizar_metadata_worker, daemon=True).start()
        threading.Thread(target=self._preparar_frame_muestra, daemon=True).start()

    def _analizar_metadata_worker(self):
        try:
            info = metadata_camara.leer_metadata_camara(FFPROBE_BIN, self.clips[0])
        except Exception:
            info = None
        self.after(0, lambda: self._mostrar_metadata(info))

    def _mostrar_metadata(self, info):
        if not info:
            self.lbl_metadata.configure(text="No se pudo leer la metadata de este clip.")
            return
        marca = info.get("marca")
        color = MARCAS_COLOR.get(marca)
        self.badge_marca.configure(text=marca or "Marca no detectada",
                                    fg_color=color if color else COLOR["pill_inactive"],
                                    text_color="white" if color else COLOR["text_secondary"])
        campos = [
            ("Modelo", info.get("modelo")),
            ("Lente", info.get("lente")),
            ("ISO", info.get("iso")),
            ("Perfil de color", info.get("perfil_color")),
            ("Espacio de color", info.get("espacio_color")),
            ("Timecode", info.get("timecode")),
            ("Fecha", info.get("fecha")),
        ]
        texto = "\n".join(f"{k}: {v}" for k, v in campos if v)
        self.lbl_metadata.configure(text=texto or "El archivo no trae datos tecnicos embebidos.")

    def _preparar_frame_muestra(self):
        try:
            ruta_tmp = os.path.join(os.path.dirname(self.clips[0]), "_pixelclean_frame.png")
            info = motores._info_basica(FFPROBE_BIN, self.clips[0])
            segundo = (info["duracion_seg"] / 2) if info["duracion_seg"] else 1.0
            import subprocess
            subprocess.run([FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                             "-ss", str(segundo), "-i", self.clips[0], "-frames:v", "1", ruta_tmp],
                            capture_output=True, timeout=30)
            self.frame_muestra = Image.open(ruta_tmp).convert("RGB")
            self.after(0, self._on_frame_muestra_listo)
        except Exception:
            pass

    def _on_frame_muestra_listo(self):
        self.editor.cargar_frame(self.frame_muestra)

    # ---------- paso 2: mascara ----------
    def _crear_paso_mascara(self):
        panel = ctk.CTkFrame(self.contenedor_pasos, corner_radius=20, fg_color="transparent")
        panel.columnconfigure(0, weight=3)
        panel.columnconfigure(1, weight=2)
        panel.rowconfigure(0, weight=1)

        self.editor = EditorMascara(panel, on_cambio=self._on_mascara_cambio)
        self.editor.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        col_preview = ctk.CTkFrame(panel, fg_color="transparent")
        col_preview.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        col_preview.columnconfigure(0, weight=1)
        col_preview.rowconfigure(0, weight=1)
        col_preview.rowconfigure(1, weight=1)

        self.panel_preview_mascara = PanelVistaPrevia(col_preview, "Antes (en vivo)")
        self.panel_preview_mascara.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.panel_preview_despues_mascara = PanelVistaPrevia(col_preview, "Despues (en vivo)")
        self.panel_preview_despues_mascara.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        return panel

    def _on_mascara_cambio(self):
        if self._debounce_id:
            self.after_cancel(self._debounce_id)
        self._debounce_id = self.after(150, self._actualizar_preview_mascara)

    def _actualizar_preview_mascara(self):
        self._actualizar_preview_generico(self.panel_preview_mascara, self.panel_preview_despues_mascara)

    def _actualizar_preview_motor(self):
        self._actualizar_preview_generico(self.panel_preview_motor, self.panel_preview_despues_motor)

    def _actualizar_preview_generico(self, panel_antes, panel_despues):
        if self.frame_muestra is None or self.editor.mascara is None:
            return
        frame = np.asarray(self.frame_muestra)
        mascara = np.asarray(self.editor.mascara.resize(self.frame_muestra.size))
        ys, xs = np.where(mascara > 20)
        if len(xs) == 0:
            return
        pad = 60
        x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + pad, frame.shape[1])
        y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + pad, frame.shape[0])
        crop = frame[y0:y1, x0:x1]
        mascara_crop = mascara[y0:y1, x0:x1]
        try:
            reparado = reparar_crop_preview(self.motor_id.get(), crop, mascara_crop, self.sigma_var.get())
        except Exception:
            return
        panel_antes.set_imagen(Image.fromarray(crop))
        panel_despues.set_imagen(Image.fromarray(reparado))

    # ---------- paso 3: motor y calidad ----------
    def _crear_paso_motor(self):
        panel = ctk.CTkFrame(self.contenedor_pasos, corner_radius=20, fg_color="transparent")
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(0, weight=1)

        col_izq = ctk.CTkFrame(panel, fg_color="transparent")
        col_izq.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        col_izq.columnconfigure(0, weight=1)

        panel_motor = ctk.CTkFrame(col_izq, corner_radius=18, fg_color=COLOR["bg_card"])
        panel_motor.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(panel_motor, text="Motor de reparacion", font=ctk.CTkFont(weight="bold"),
                     text_color=COLOR["text_primary"]).pack(anchor="w", padx=18, pady=(16, 8))
        for motor_id, info in MOTORES_INFO.items():
            ctk.CTkRadioButton(panel_motor, text=f"{info['nombre']} - {info['detalle']}", value=motor_id,
                                variable=self.motor_id, fg_color=COLOR["accent"], hover_color=COLOR["accent_hover"],
                                text_color=COLOR["text_primary"],
                                command=self._actualizar_preview_motor).pack(anchor="w", padx=18, pady=5)
        ctk.CTkLabel(panel_motor, text="Intensidad del blur (si aplica)",
                     text_color=COLOR["text_secondary"]).pack(anchor="w", padx=18, pady=(12, 0))
        ctk.CTkSlider(panel_motor, from_=1, to=40, variable=self.sigma_var,
                       progress_color=COLOR["accent"], button_color=COLOR["accent"],
                       button_hover_color=COLOR["accent_hover"],
                       command=lambda v: self._actualizar_preview_motor()).pack(fill="x", padx=18, pady=(4, 16))

        panel_calidad = ctk.CTkFrame(col_izq, corner_radius=18, fg_color=COLOR["bg_card"])
        panel_calidad.pack(fill="x")
        ctk.CTkLabel(panel_calidad, text="Calidad y peso", font=ctk.CTkFont(weight="bold"),
                     text_color=COLOR["text_primary"]).pack(anchor="w", padx=18, pady=(16, 8))
        ctk.CTkRadioButton(panel_calidad, text="Igualar bitrate original", value="bitrate",
                            variable=self.modo_calidad_var, fg_color=COLOR["accent"],
                            hover_color=COLOR["accent_hover"], text_color=COLOR["text_primary"]).pack(anchor="w", padx=18, pady=5)
        ctk.CTkRadioButton(panel_calidad, text="Calidad fija (CRF)", value="crf",
                            variable=self.modo_calidad_var, fg_color=COLOR["accent"],
                            hover_color=COLOR["accent_hover"], text_color=COLOR["text_primary"]).pack(anchor="w", padx=18, pady=5)
        fila_vel = ctk.CTkFrame(panel_calidad, fg_color="transparent")
        fila_vel.pack(fill="x", padx=18, pady=(6, 4))
        for nombre in PRESETS_VELOCIDAD:
            ctk.CTkRadioButton(fila_vel, text=nombre, value=nombre, variable=self.velocidad_var,
                                fg_color=COLOR["accent"], hover_color=COLOR["accent_hover"],
                                text_color=COLOR["text_primary"]).pack(side="left", padx=(0, 14))
        fila_res = ctk.CTkFrame(panel_calidad, fg_color="transparent")
        fila_res.pack(fill="x", padx=18, pady=(6, 16))
        ctk.CTkLabel(fila_res, text="Resolucion:", text_color=COLOR["text_secondary"]).pack(side="left")
        ctk.CTkOptionMenu(fila_res, values=list(RESOLUCIONES.keys()), variable=self.resolucion_var,
                           fg_color=COLOR["pill_inactive"], text_color=COLOR["text_primary"],
                           button_color=COLOR["accent"], button_hover_color=COLOR["accent_hover"]).pack(side="left", padx=8)

        col_preview = ctk.CTkFrame(panel, fg_color="transparent")
        col_preview.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        col_preview.columnconfigure(0, weight=1)
        col_preview.rowconfigure(0, weight=1)
        col_preview.rowconfigure(1, weight=1)

        self.panel_preview_motor = PanelVistaPrevia(col_preview, "Antes (en vivo)")
        self.panel_preview_motor.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.panel_preview_despues_motor = PanelVistaPrevia(col_preview, "Despues (en vivo)")
        self.panel_preview_despues_motor.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        return panel

    # ---------- paso 4: procesar ----------
    def _crear_paso_procesar(self):
        panel = ctk.CTkFrame(self.contenedor_pasos, corner_radius=20, fg_color=COLOR["bg_card"])
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=2)
        panel.rowconfigure(0, weight=1)

        col_anillo = ctk.CTkFrame(panel, fg_color="transparent")
        col_anillo.grid(row=0, column=0, sticky="nsew", padx=22, pady=22)
        ctk.CTkLabel(col_anillo, text="Progreso general", font=ctk.CTkFont(weight="bold"),
                     text_color=COLOR["text_primary"]).pack(anchor="w", pady=(0, 14))
        self.anillo_general = AnilloProgreso(col_anillo, tam=150, grosor=14)
        self.anillo_general.pack(pady=6)
        self.lbl_general_info = ctk.CTkLabel(col_anillo, text="0 / 0 clips", text_color=COLOR["text_secondary"])
        self.lbl_general_info.pack(pady=(10, 0))

        col_log = ctk.CTkFrame(panel, fg_color="transparent")
        col_log.grid(row=0, column=1, sticky="nsew", padx=(0, 22), pady=22)
        col_log.columnconfigure(0, weight=1)
        col_log.rowconfigure(0, weight=1)
        self.caja_log = ctk.CTkTextbox(col_log, corner_radius=14, fg_color=COLOR["bg_card_alt"],
                                        text_color=COLOR["text_primary"])
        self.caja_log.grid(row=0, column=0, sticky="nsew")
        return panel

    def log(self, texto):
        def _agregar():
            self.caja_log.insert("end", texto + "\n")
            self.caja_log.see("end")
        self.after(0, _agregar)

    def _procesar_todo(self):
        self.btn_siguiente.configure(state="disabled")
        threading.Thread(target=self._procesar_todo_worker, daemon=True).start()

    def _procesar_todo_worker(self):
        try:
            carpeta_salida = os.path.join(os.path.dirname(self.clips[0]), "reparados")
            os.makedirs(carpeta_salida, exist_ok=True)
            mascara_bn_path = os.path.join(carpeta_salida, "_mascara_bn.png")
            self.editor.mascara.save(mascara_bn_path)

            preset_info = PRESETS_VELOCIDAD[self.velocidad_var.get()]
            parametros = {
                "sigma_blur": self.sigma_var.get(),
                "preset_info": preset_info,
                "modo_calidad": self.modo_calidad_var.get(),
                "resolucion_objetivo": RESOLUCIONES[self.resolucion_var.get()],
                "usar_nvenc": False,
                "radio_inpaint": 5,
                "metodo": "telea",
                "zoom_padding": 40,
            }
            motor_fn = motores.MOTORES[self.motor_id.get()]
            total = len(self.clips)
            completados = 0

            for clip in self.clips:
                barra = self.tarjetas_clips.get(clip)
                nombre = os.path.splitext(os.path.basename(clip))[0]
                ext = os.path.splitext(clip)[1] or ".mp4"
                ruta_salida = os.path.join(carpeta_salida, nombre + OUTPUT_SUFFIX + ext)

                def callback(frac, b=barra):
                    if b:
                        self.after(0, b.set, frac)

                exito, resultado = motor_fn(FFMPEG_BIN, FFPROBE_BIN, clip, mascara_bn_path, ruta_salida,
                                             parametros, callback)
                completados += 1
                frac_general = completados / total
                self.after(0, self.anillo_general.set, frac_general)
                self.after(0, lambda c=completados: self.lbl_general_info.configure(text=f"{c} / {total} clips"))
                if exito:
                    self.log(f"OK: {os.path.basename(clip)} -> {os.path.basename(resultado)}")
                else:
                    self.log(f"ERROR: {os.path.basename(clip)} -> {resultado}")

            self.log(f"\nListo. Carpeta de salida: {carpeta_salida}")
        except Exception:
            self.log("ERROR inesperado:")
            self.log(traceback.format_exc())
        finally:
            self.after(0, lambda: self.btn_siguiente.configure(state="normal"))


if __name__ == "__main__":
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    app = PixelCleanApp()
    app.mainloop()
