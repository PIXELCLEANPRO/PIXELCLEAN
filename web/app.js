"use strict";

const PASOS = ["Clip", "Mascara", "Motor y calidad", "Procesar"];
const state = {
  step: 0,
  theme: "dark",
  clips: [],
  frameImg: null,
  frameW: 1280, frameH: 720,
  tool: "brush",
  brushSize: 28,
  hardness: 100,
  opacity: 100,
  angleFine: 0,
  spacePanning: false,
  toolBeforeSpace: null,
  zoom: {mascara: 1, motor: 1},
  pan: {mascara: {x: 0, y: 0}},
  fitScale: {mascara: 1},
  history: [], historyIndex: -1,
  maskBaseSnapshot: null,
  motor: "blur",
  sigma: 15,
  calidad: "bitrate",
  velocidad: "Rapido",
  resolucion: "Original",
  isPainting: false, isPanning: false, lastPt: null, panStart: null,
  debounceTimer: null,
};

/* ---------------- API bridge (real pywebview or mock for browser preview) ---------------- */
const mockFrameDataUrl = (() => {
  const c = document.createElement("canvas");
  c.width = 640; c.height = 360;
  const ctx = c.getContext("2d");
  const cols = ["#e5484d", "#35c58a", "#f5d90a", "#2f6fed", "#e34ba9", "#0ac5c5"];
  cols.forEach((col, i) => { ctx.fillStyle = col; ctx.fillRect(i * (640 / 6), 0, 640 / 6, 360); });
  ctx.strokeStyle = "rgba(255,255,255,0.9)";
  ctx.lineWidth = 6;
  ctx.beginPath(); ctx.moveTo(40, 320); ctx.lineTo(560, 60); ctx.stroke();
  return c.toDataURL("image/png");
})();

const mockApi = {
  elegir_clips: async () => (["C:/videos/clip_prueba.mp4"]),
  obtener_frame_y_metadata: async (ruta) => ({
    frame_b64: mockFrameDataUrl, ancho: 640, alto: 360,
    metadata: {marca: "Sony", modelo: "ILCE-7M3", lente: "FE 24-70mm F2.8 GM", iso: "800", perfil_color: "S-Log3",
      resolucion: "3840x2160", duracion: "00:02:17", fps: 25, codec_video: "H.264", pixel_format: "yuv420p",
      bitrate: "45.2 Mbps", peso: "742 MB", contenedor: "QuickTime / MOV", codec_audio: "AAC", audio_canales: 2, audio_sample_rate: "48 kHz"},
  }),
  render_preview: async (motorId, maskB64, sigma) => ({antes_b64: mockFrameDataUrl, despues_b64: mockFrameDataUrl}),
  procesar_todo: async (payload) => {
    mockApi._progreso = {completados: 0, total: payload.clips.length, por_clip: payload.clips.map(() => 0), logs: [], terminado: false};
    let n = 0;
    const avanzar = () => {
      n++;
      mockApi._progreso.completados = n;
      mockApi._progreso.por_clip = mockApi._progreso.por_clip.map((_, i) => (i < n ? 1 : 0));
      mockApi._progreso.logs.push({mensaje: `OK: clip_${n}.mp4 -> clip_${n}_reparado.mp4`, ok: true});
      if (n < payload.clips.length) setTimeout(avanzar, 700);
      else mockApi._progreso.terminado = true;
    };
    setTimeout(avanzar, 700);
    return {ok: true};
  },
  cancelar_procesamiento: async () => { if (mockApi._progreso) mockApi._progreso.terminado = true; return {ok: true}; },
  estado_licencia: async () => ({pro: false, restantes: 5, limite: 5}),
  activar_licencia: async () => ({ok: false, error: "Modo de prueba en navegador: no se puede validar una clave real aca."}),
  obtener_estado_actualizacion: async () => ({hay_actualizacion: false}),
};

// nota: el flujo real consulta el progreso por fetch('/progreso') servido por el mismo
// servidor Python (ver webview_app.py), no por api.obtener_progreso -- eso evita un pedido
// entre origenes distintos que resulto poco confiable en pywebview+WinForms+WebView2.
let api = (window.pywebview && window.pywebview.api) ? window.pywebview.api : mockApi;
window.addEventListener("pywebviewready", () => { api = window.pywebview.api; refrescarPlanBadge(); revisarActualizacion(); });

/* ---------------- Aviso de actualizacion disponible ---------------- */
async function revisarActualizacion() {
  try {
    const estado = await api.obtener_estado_actualizacion();
    if (estado && estado.hay_actualizacion) {
      const banner = document.getElementById("bannerUpdate");
      document.getElementById("updateVersion").textContent = estado.version_nueva;
      document.getElementById("updateLink").href = estado.url;
      banner.classList.add("visible");
    }
  } catch (err) {
    // sin internet o api no disponible todavia: no molestamos
  }
}
setTimeout(revisarActualizacion, 4000);
document.getElementById("updateClose").addEventListener("click", () => {
  document.getElementById("bannerUpdate").classList.remove("visible");
});

/* ---------------- Plan gratis / Pro ---------------- */
async function refrescarPlanBadge() {
  const btn = document.getElementById("btnPlan");
  if (!btn) return;
  try {
    const estado = await api.estado_licencia();
    btn.classList.remove("is-pro", "is-low");
    if (estado.pro) {
      btn.textContent = "PRO";
      btn.classList.add("is-pro");
    } else {
      btn.textContent = `Gratis · ${estado.restantes}/${estado.limite} hoy`;
      if (estado.restantes <= 1) btn.classList.add("is-low");
    }
  } catch (err) {
    // si no se puede consultar, dejamos el badge por defecto sin romper la app
  }
}

/* ---------------- Modal: activar licencia ---------------- */
const licenciaOverlay = document.getElementById("licenciaOverlay");
const licenciaInput = document.getElementById("licenciaInput");
const licenciaMsg = document.getElementById("licenciaMsg");
const licenciaStatus = document.getElementById("licenciaStatus");

async function abrirModalLicencia() {
  licenciaMsg.textContent = "";
  licenciaMsg.className = "licencia-msg";
  licenciaInput.value = "";
  licenciaStatus.textContent = "Consultando tu plan...";
  licenciaOverlay.classList.add("open");
  licenciaInput.focus();

  const estado = await api.estado_licencia().catch(() => null);
  if (!estado) {
    licenciaStatus.textContent = "";
  } else if (estado.pro) {
    licenciaStatus.textContent = "Ya tenes PixelClean Pro activado. Gracias por bancar el proyecto!";
    licenciaInput.style.display = "none";
    document.getElementById("licenciaActivar").style.display = "none";
  } else {
    licenciaStatus.textContent = `Version gratis: te quedan ${estado.restantes} de ${estado.limite} clips hoy. Si comprastes Pro, pega tu clave aca.`;
    licenciaInput.style.display = "";
    document.getElementById("licenciaActivar").style.display = "";
  }
}

function cerrarModalLicencia() {
  licenciaOverlay.classList.remove("open");
}

document.getElementById("btnLicencia").addEventListener("click", abrirModalLicencia);
document.getElementById("licenciaClose").addEventListener("click", cerrarModalLicencia);
document.getElementById("licenciaCancelar").addEventListener("click", cerrarModalLicencia);
licenciaOverlay.addEventListener("click", (e) => { if (e.target === licenciaOverlay) cerrarModalLicencia(); });
window.addEventListener("keydown", (e) => { if (e.key === "Escape" && licenciaOverlay.classList.contains("open")) cerrarModalLicencia(); });

async function activarLicenciaDesdeModal() {
  const clave = licenciaInput.value.trim();
  if (!clave) return;
  const btnActivar = document.getElementById("licenciaActivar");
  btnActivar.disabled = true;
  const resultado = await api.activar_licencia(clave).catch((err) => ({ok: false, error: String(err)}));
  btnActivar.disabled = false;
  if (resultado.ok) {
    licenciaMsg.textContent = "Listo, PixelClean Pro activado. Gracias!";
    licenciaMsg.className = "licencia-msg ok";
    licenciaStatus.textContent = "";
    licenciaInput.style.display = "none";
    btnActivar.style.display = "none";
    refrescarPlanBadge();
  } else {
    licenciaMsg.textContent = resultado.error || "Esa clave no es valida.";
    licenciaMsg.className = "licencia-msg err";
  }
}
document.getElementById("licenciaActivar").addEventListener("click", activarLicenciaDesdeModal);
licenciaInput.addEventListener("keydown", (e) => { if (e.key === "Enter") activarLicenciaDesdeModal(); });

refrescarPlanBadge();

/* ---------------- Topbar: pasos + tema ---------------- */
function renderSteps() {
  const cont = document.getElementById("steps");
  cont.innerHTML = "";
  PASOS.forEach((nombre, i) => {
    const pill = document.createElement("div");
    pill.className = "step-pill" + (i === state.step ? " active" : "") + (i < state.step ? " done" : "");
    pill.innerHTML = `<span class="num">${i < state.step ? "" : i + 1}</span><span>${nombre}</span>`;
    cont.appendChild(pill);
  });
}

document.getElementById("themeToggle").addEventListener("click", () => {
  state.theme = state.theme === "light" ? "dark" : "light";
  document.body.setAttribute("data-theme", state.theme);
});

/* ---------------- Navegacion entre pasos ---------------- */
function mostrarPaso(nuevo) {
  state.step = nuevo;
  document.querySelectorAll(".panel").forEach(p => {
    p.classList.toggle("active", Number(p.dataset.step) === nuevo);
  });
  renderSteps();
  document.getElementById("btnAtras").disabled = nuevo === 0;
  document.getElementById("btnSiguiente").textContent = nuevo === PASOS.length - 1 ? "Procesar" : "Siguiente";
  document.getElementById("navInfo").textContent = "";
  if (nuevo === 1) setTimeout(fitCanvasToViewport, 30);
  if (nuevo === 2) actualizarPreview("motor");
}

document.getElementById("btnAtras").addEventListener("click", () => {
  if (state.step > 0) mostrarPaso(state.step - 1);
});

document.getElementById("btnSiguiente").addEventListener("click", () => {
  const info = document.getElementById("navInfo");
  if (state.step === 0 && state.clips.length === 0) { info.textContent = "Carga al menos un clip primero"; info.classList.add("warn"); return; }
  if (state.step === 1 && !hayMascara()) { info.textContent = "Pinta o carga una mascara primero"; info.classList.add("warn"); return; }
  info.classList.remove("warn");
  if (state.step < PASOS.length - 1) mostrarPaso(state.step + 1);
  else procesarTodo();
});

/* ---------------- Paso 1: Clip ---------------- */
async function establecerClips(archivos) {
  if (!archivos || !archivos.length) return;
  state.clips = archivos;
  document.getElementById("lblClips").textContent = `${archivos.length} clip(s) cargado(s)`;
  const list = document.getElementById("clipList");
  list.innerHTML = "";
  archivos.forEach(ruta => {
    const row = document.createElement("div");
    row.className = "clip-row";
    const nombre = ruta.split(/[\\/]/).pop();
    row.innerHTML = `<span class="name">${nombre}</span><div class="bar"><div style="width:0%"></div></div>`;
    list.appendChild(row);
  });
  const datos = await api.obtener_frame_y_metadata(archivos[0]);
  cargarFrameMuestra(datos);
  mostrarMetadata(datos.metadata);
}

document.getElementById("btnCargarClips").addEventListener("click", async () => {
  const btnCargar = document.getElementById("btnCargarClips");
  btnCargar.disabled = true;
  try {
    const archivos = await api.elegir_clips();
    if (archivos && archivos.length) establecerClips(archivos);
  } finally {
    btnCargar.disabled = false;
  }
});

const EXTENSIONES_VIDEO = [".mp4", ".mov", ".mxf", ".avi", ".m4v", ".mkv"];
const zonaClip = document.querySelector(".panel-clip");
zonaClip.addEventListener("dragenter", (e) => { e.preventDefault(); zonaClip.classList.add("dragover"); });
zonaClip.addEventListener("dragover", (e) => { e.preventDefault(); zonaClip.classList.add("dragover"); });
zonaClip.addEventListener("dragleave", () => zonaClip.classList.remove("dragover"));
zonaClip.addEventListener("drop", (e) => {
  e.preventDefault();
  zonaClip.classList.remove("dragover");
  const archivos = [...e.dataTransfer.files]
    .map((f) => f.pywebviewFullPath || f.path)
    .filter((p) => p && EXTENSIONES_VIDEO.some((ext) => p.toLowerCase().endsWith(ext)));
  if (archivos.length) {
    establecerClips(archivos);
  } else {
    document.getElementById("lblClips").textContent = "No se pudo leer la ruta del archivo soltado -- proba con el boton";
  }
});

function mostrarMetadata(meta) {
  const badge = document.getElementById("brandBadge");
  const span = badge.querySelector("span");
  span.textContent = (meta && meta.marca) ? meta.marca : "Marca no detectada";

  const grupos = [
    {titulo: "Camara y lente", campos: [["Modelo", "modelo"], ["Lente", "lente"], ["ISO", "iso"],
      ["Perfil de color", "perfil_color"], ["Espacio de color", "espacio_color"], ["Timecode", "timecode"], ["Fecha de grabacion", "fecha"]]},
    {titulo: "Archivo y codificacion", campos: [["Resolucion", "resolucion"], ["Duracion", "duracion"], ["FPS", "fps"],
      ["Codec de video", "codec_video"], ["Formato de pixel", "pixel_format"], ["Bitrate", "bitrate"], ["Peso", "peso"],
      ["Contenedor", "contenedor"], ["Codec de audio", "codec_audio"], ["Canales de audio", "audio_canales"], ["Frecuencia de audio", "audio_sample_rate"]]},
  ];
  const fields = document.getElementById("metaFields");
  fields.innerHTML = "";
  let huboAlgo = false;
  grupos.forEach(grupo => {
    const presentes = grupo.campos.filter(([, key]) => meta && meta[key]);
    if (!presentes.length) return;
    huboAlgo = true;
    const titulo = document.createElement("div");
    titulo.className = "section-title";
    titulo.style.marginTop = "10px";
    titulo.textContent = grupo.titulo;
    fields.appendChild(titulo);
    presentes.forEach(([label, key]) => {
      const row = document.createElement("div");
      row.className = "meta-field";
      row.innerHTML = `<span class="k">${label}</span><span class="v">${meta[key]}</span>`;
      fields.appendChild(row);
    });
  });
  if (!huboAlgo) fields.innerHTML = `<div class="meta-field"><span class="k">El archivo no trae datos tecnicos embebidos.</span></div>`;
}

/* ---------------- Editor de mascara: canvas, pincel, zoom, pan, historial ---------------- */
const cnvFrame = document.getElementById("cnvFrame");
const cnvMask = document.getElementById("cnvMask");
const ctxFrame = cnvFrame.getContext("2d");
const ctxMask = cnvMask.getContext("2d");
const viewport = document.getElementById("viewportMascara");
const stage = document.getElementById("stageMascara");
const brushCursor = document.getElementById("brushCursor");

function cargarFrameMuestra(datos) {
  state.frameW = datos.ancho; state.frameH = datos.alto;
  const img = new Image();
  img.onload = () => {
    state.frameImg = img;
    [cnvFrame, cnvMask].forEach(c => { c.width = state.frameW; c.height = state.frameH; });
    ctxFrame.drawImage(img, 0, 0);
    ctxMask.clearRect(0, 0, state.frameW, state.frameH);
    pushHistory();
    fitCanvasToViewport();
  };
  img.src = datos.frame_b64;
}

function fitCanvasToViewport() {
  if (!state.frameImg) return;
  const vw = viewport.clientWidth - 24, vh = viewport.clientHeight - 24;
  state.fitScale.mascara = Math.min(vw / state.frameW, vh / state.frameH);
  state.zoom.mascara = 1;
  state.pan.mascara = {x: 0, y: 0};
  aplicarTransformEscenario();
}

// Ojo: a proposito NO se usa transform:scale() aca. Un solo sistema de
// posicionamiento (ancho/alto/left/top puestos directo por JS, sin transform
// de por medio) hace que la conversion de coordenadas del mouse sea directa
// y confiable -- es el mismo esquema que ya funciona bien en las vistas previas.
function aplicarTransformEscenario() {
  const vw = viewport.clientWidth, vh = viewport.clientHeight;
  const escala = state.fitScale.mascara * state.zoom.mascara;
  const dispW = state.frameW * escala, dispH = state.frameH * escala;
  const left = (vw - dispW) / 2 + state.pan.mascara.x;
  const top = (vh - dispH) / 2 + state.pan.mascara.y;
  [cnvFrame, cnvMask].forEach(c => {
    c.style.width = dispW + "px";
    c.style.height = dispH + "px";
    c.style.left = left + "px";
    c.style.top = top + "px";
  });
  document.getElementById("zoomPct").textContent = Math.round(state.zoom.mascara * 100) + "%";
}

function cambiarZoom(delta) {
  state.zoom.mascara = Math.max(0.3, Math.min(6, state.zoom.mascara + delta));
  aplicarTransformEscenario();
}
document.getElementById("btnZoomIn").addEventListener("click", () => cambiarZoom(0.2));
document.getElementById("btnZoomOut").addEventListener("click", () => cambiarZoom(-0.2));
document.getElementById("hudZoomIn").addEventListener("click", () => cambiarZoom(0.2));
document.getElementById("hudZoomOut").addEventListener("click", () => cambiarZoom(-0.2));

viewport.addEventListener("wheel", (e) => {
  e.preventDefault();
  cambiarZoom(e.deltaY < 0 ? 0.15 : -0.15);
}, {passive: false});

/* --- herramientas --- */
function setTool(tool) {
  state.tool = tool;
  document.querySelectorAll("#toolbarMascara .tool-btn[data-tool]").forEach(b => {
    b.classList.toggle("active", b.dataset.tool === tool);
  });
  viewport.classList.toggle("pan-mode", tool === "hand");
  actualizarCursorPincel();
}
document.querySelectorAll("#toolbarMascara .tool-btn[data-tool]").forEach(b => {
  b.addEventListener("click", () => setTool(b.dataset.tool));
});

function actualizarCursorPincel() {
  const visible = state.tool === "brush" || state.tool === "eraser";
  brushCursor.style.display = visible ? "block" : "none";
  brushCursor.style.borderColor = state.tool === "eraser" ? "#ff6666" : "#ffffff";
}

function coordsAImagen(clientX, clientY) {
  const rect = cnvMask.getBoundingClientRect();
  const escalaX = rect.width / state.frameW;
  const escalaY = rect.height / state.frameH;
  return {x: (clientX - rect.left) / escalaX, y: (clientY - rect.top) / escalaY};
}

viewport.addEventListener("mousemove", (e) => {
  if (state.isResizingBrush) {
    const dx = e.clientX - state.resizeStart.x, dy = e.clientY - state.resizeStart.y;
    setBrushSize(state.resizeStart.size + dx * 0.4);
    setHardness(state.resizeStart.hardness - dy * 0.5);
    const diametro = state.brushSize * 2;
    brushCursor.style.width = diametro + "px";
    brushCursor.style.height = diametro + "px";
    brushCursor.style.left = (state.resizeStart.x - diametro / 2) + "px";
    brushCursor.style.top = (state.resizeStart.y - diametro / 2) + "px";
    return;
  }

  const rectCursor = cnvMask.getBoundingClientRect();
  const escala = state.frameW ? (rectCursor.width / state.frameW) : 1;
  const diametro = state.brushSize * 2 * escala;
  brushCursor.style.width = diametro + "px";
  brushCursor.style.height = diametro + "px";
  brushCursor.style.left = (e.clientX - diametro / 2) + "px";
  brushCursor.style.top = (e.clientY - diametro / 2) + "px";

  if (state.isPanning) {
    state.pan.mascara.x = state.panStart.px + (e.clientX - state.panStart.x);
    state.pan.mascara.y = state.panStart.py + (e.clientY - state.panStart.y);
    aplicarTransformEscenario();
    return;
  }
  if (state.isPainting) pintarEn(e.clientX, e.clientY);
});
viewport.addEventListener("mouseenter", () => actualizarCursorPincel());
viewport.addEventListener("mouseleave", () => { brushCursor.style.display = "none"; });

viewport.addEventListener("contextmenu", (e) => { if (e.altKey || state.isResizingBrush) e.preventDefault(); });

viewport.addEventListener("mousedown", (e) => {
  if (e.altKey && e.button === 2) {
    e.preventDefault();
    state.isResizingBrush = true;
    state.resizeStart = {x: e.clientX, y: e.clientY, size: state.brushSize, hardness: state.hardness};
    brushCursor.style.display = "block";
    brushCursor.style.left = (e.clientX - state.brushSize) + "px";
    brushCursor.style.top = (e.clientY - state.brushSize) + "px";
    brushCursor.style.width = brushCursor.style.height = (state.brushSize * 2) + "px";
    return;
  }
  if (state.tool === "hand") {
    state.isPanning = true;
    state.panStart = {x: e.clientX, y: e.clientY, px: state.pan.mascara.x, py: state.pan.mascara.y};
    viewport.classList.add("panning");
  } else if (state.frameImg) {
    state.isPainting = true;
    state.lastPt = coordsAImagen(e.clientX, e.clientY);
    pintarEn(e.clientX, e.clientY);
  }
});
window.addEventListener("mouseup", () => {
  if (state.isPainting) { state.isPainting = false; state.lastPt = null; pushHistory(); actualizarPreview("mascara"); }
  if (state.isPanning) { state.isPanning = false; viewport.classList.remove("panning"); }
  if (state.isResizingBrush) { state.isResizingBrush = false; actualizarCursorPincel(); }
});

function estamparDab(x, y) {
  const radio = state.brushSize;
  const dureza = state.hardness / 100;
  const opacidad = state.opacity / 100;
  ctxMask.globalCompositeOperation = state.tool === "eraser" ? "destination-out" : "source-over";
  // el radio interior tiene que ser siempre menor al exterior: si son iguales (dureza 100%)
  // el gradiente queda degenerado y algunos motores de renderizado no pintan nada.
  const radioInterior = Math.max(0, Math.min(radio * dureza, radio - 0.5));
  const grad = ctxMask.createRadialGradient(x, y, radioInterior, x, y, radio);
  grad.addColorStop(0, `rgba(58,160,255,${opacidad})`);
  grad.addColorStop(1, "rgba(58,160,255,0)");
  ctxMask.fillStyle = grad;
  ctxMask.beginPath();
  ctxMask.arc(x, y, radio, 0, Math.PI * 2);
  ctxMask.fill();
}

function pintarEn(clientX, clientY) {
  const pt = coordsAImagen(clientX, clientY);
  if (state.lastPt) {
    const dx = pt.x - state.lastPt.x, dy = pt.y - state.lastPt.y;
    const dist = Math.hypot(dx, dy);
    const paso = Math.max(1, state.brushSize * 0.22);
    const pasos = Math.max(1, Math.floor(dist / paso));
    for (let i = 1; i <= pasos; i++) {
      estamparDab(state.lastPt.x + (dx * i) / pasos, state.lastPt.y + (dy * i) / pasos);
    }
  } else {
    estamparDab(pt.x, pt.y);
  }
  state.lastPt = pt;
  cnvMask.style.opacity = "0.7";
  debounceActualizarPreview("mascara");
}

function hayMascara() {
  if (!state.frameImg) return false;
  const data = ctxMask.getImageData(0, 0, cnvMask.width, cnvMask.height).data;
  for (let i = 3; i < data.length; i += 4 * 97) if (data[i] > 10) return true;
  return false;
}

/* --- historial --- */
function pushHistory() {
  state.history = state.history.slice(0, state.historyIndex + 1);
  state.history.push(cnvMask.toDataURL());
  state.historyIndex = state.history.length - 1;
  if (state.history.length > 25) { state.history.shift(); state.historyIndex--; }
  state.maskBaseSnapshot = state.history[state.historyIndex];
}
function restaurarDesde(dataUrl) {
  const img = new Image();
  img.onload = () => { ctxMask.clearRect(0, 0, cnvMask.width, cnvMask.height); ctxMask.drawImage(img, 0, 0); actualizarPreview("mascara"); };
  img.src = dataUrl;
}
document.getElementById("btnUndo").addEventListener("click", () => {
  if (state.historyIndex > 0) { state.historyIndex--; restaurarDesde(state.history[state.historyIndex]); state.maskBaseSnapshot = state.history[state.historyIndex]; }
});
document.getElementById("btnRedo").addEventListener("click", () => {
  if (state.historyIndex < state.history.length - 1) { state.historyIndex++; restaurarDesde(state.history[state.historyIndex]); state.maskBaseSnapshot = state.history[state.historyIndex]; }
});
document.getElementById("btnRotate").addEventListener("click", () => {
  const tmp = document.createElement("canvas");
  tmp.width = cnvMask.width; tmp.height = cnvMask.height;
  const tctx = tmp.getContext("2d");
  tctx.translate(tmp.width / 2, tmp.height / 2);
  tctx.rotate(Math.PI / 2);
  tctx.drawImage(cnvMask, -tmp.width / 2, -tmp.height / 2);
  ctxMask.clearRect(0, 0, cnvMask.width, cnvMask.height);
  ctxMask.drawImage(tmp, 0, 0);
  pushHistory();
  actualizarPreview("mascara");
});

/* --- campos "scrub" estilo Photoshop: arrastras el numero, doble click para escribir --- */
function crearCampoScrub(id, {min, max, valorInicial, sensibilidad = 1, onChange}) {
  const el = document.getElementById(id);
  const spanValor = el.querySelector(".sf-value");
  let valor = valorInicial;

  function fijar(v, notificar = true) {
    valor = Math.max(min, Math.min(max, Math.round(v)));
    spanValor.textContent = valor;
    if (notificar && onChange) onChange(valor);
  }

  let arrastrando = false, startX = 0, startVal = 0, huboArrastre = false;
  el.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    arrastrando = true; huboArrastre = false; startX = e.clientX; startVal = valor;
    el.classList.add("scrubbing");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!arrastrando) return;
    if (Math.abs(e.clientX - startX) > 2) huboArrastre = true;
    fijar(startVal + (e.clientX - startX) * sensibilidad);
  });
  window.addEventListener("mouseup", () => {
    if (arrastrando) { arrastrando = false; el.classList.remove("scrubbing"); }
  });
  el.addEventListener("dblclick", () => {
    const input = document.createElement("input");
    input.type = "text"; input.className = "sf-edit"; input.value = valor;
    spanValor.replaceWith(input);
    input.focus(); input.select();
    const confirmar = () => {
      const n = parseFloat(input.value);
      input.replaceWith(spanValor);
      if (!isNaN(n)) fijar(n);
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") confirmar(); if (e.key === "Escape") { input.replaceWith(spanValor); } });
    input.addEventListener("blur", confirmar);
  });

  fijar(valorInicial, false);
  return {set: (v) => fijar(v, false)};
}

const campoTamano = crearCampoScrub("scrubSize", {min: 1, max: 300, valorInicial: 28, sensibilidad: 0.6,
  onChange: (v) => { state.brushSize = v; }});
const campoDureza = crearCampoScrub("scrubHardness", {min: 0, max: 100, valorInicial: 100, sensibilidad: 0.7,
  onChange: (v) => { state.hardness = v; }});
const campoOpacidad = crearCampoScrub("scrubOpacity", {min: 1, max: 100, valorInicial: 100, sensibilidad: 0.7,
  onChange: (v) => { state.opacity = v; }});
const campoAngulo = crearCampoScrub("scrubAngle", {min: -180, max: 180, valorInicial: 0, sensibilidad: 0.5,
  onChange: (v) => {
    state.angleFine = v;
    if (!state.maskBaseSnapshot) return;
    const img = new Image();
    img.onload = () => {
      ctxMask.clearRect(0, 0, cnvMask.width, cnvMask.height);
      ctxMask.save();
      ctxMask.translate(cnvMask.width / 2, cnvMask.height / 2);
      ctxMask.rotate(state.angleFine * Math.PI / 180);
      ctxMask.drawImage(img, -cnvMask.width / 2, -cnvMask.height / 2);
      ctxMask.restore();
      debounceActualizarPreview("mascara");
    };
    img.src = state.maskBaseSnapshot;
  }});

function setBrushSize(v) { state.brushSize = Math.max(1, Math.min(300, v)); campoTamano.set(state.brushSize); }
function setHardness(v) { state.hardness = Math.max(0, Math.min(100, v)); campoDureza.set(state.hardness); }
function setOpacity(v) { state.opacity = Math.max(1, Math.min(100, v)); campoOpacidad.set(state.opacity); }

document.getElementById("btnLoadPng").addEventListener("click", async () => {
  if (!api.elegir_mascara_png) return;
  const resultado = await api.elegir_mascara_png(state.frameW, state.frameH);
  if (!resultado) return;
  const img = new Image();
  img.onload = () => { ctxMask.clearRect(0, 0, cnvMask.width, cnvMask.height); ctxMask.drawImage(img, 0, 0, cnvMask.width, cnvMask.height); cnvMask.style.opacity = "0.55"; pushHistory(); actualizarPreview("mascara"); };
  img.src = resultado;
});

window.addEventListener("resize", () => { if (state.step === 1) fitCanvasToViewport(); });

/* ---------------- Paneles redimensionables ---------------- */
function habilitarResizer(resizerEl, panelEl, {min = 220, max = 1400} = {}) {
  let arrastrando = false, startX = 0, startWidth = 0;
  resizerEl.addEventListener("mousedown", (e) => {
    arrastrando = true; startX = e.clientX; startWidth = panelEl.getBoundingClientRect().width;
    resizerEl.classList.add("dragging");
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!arrastrando) return;
    const nuevo = Math.max(min, Math.min(max, startWidth - (e.clientX - startX)));
    panelEl.style.flex = `0 0 ${nuevo}px`;
    panelEl.style.width = nuevo + "px";
    if (state.step === 1) fitCanvasToViewport();
  });
  window.addEventListener("mouseup", () => {
    if (arrastrando) { arrastrando = false; resizerEl.classList.remove("dragging"); document.body.style.userSelect = ""; }
  });
}
habilitarResizer(document.getElementById("resizerMascara"), document.getElementById("previewColMascara"));
habilitarResizer(document.getElementById("resizerMotor"), document.getElementById("previewColMotor"));

/* ---------------- Paneles de vista previa (Antes/Despues) reutilizables ---------------- */
const panEstadoPorPanel = {};

const zoomEstadoPorPanel = {};

function crearPanelPreview(titulo, id) {
  const wrap = document.createElement("div");
  wrap.className = "preview-panel";
  wrap.innerHTML = `
    <div class="preview-head">
      <span>${titulo}</span>
      <div class="mini-tools">
        <button class="mini-btn" data-act="out" title="Alejar"><svg width="13" height="13"><use href="#i-zoomout"/></svg></button>
        <button class="mini-btn" data-act="in" title="Acercar"><svg width="13" height="13"><use href="#i-zoomin"/></svg></button>
        <button class="mini-btn" data-act="reset" title="Ajustar"><svg width="13" height="13"><use href="#i-move"/></svg></button>
        <button class="mini-btn" data-act="expand" title="Expandir"><svg width="13" height="13"><use href="#i-expand"/></svg></button>
      </div>
    </div>
    <div class="preview-canvas-wrap" id="${id}-wrap"><canvas id="${id}"></canvas></div>`;
  zoomEstadoPorPanel[id] = 1;
  panEstadoPorPanel[id] = {x: 0, y: 0};
  wrap.querySelector('[data-act="in"]').addEventListener("click", () => { zoomEstadoPorPanel[id] = Math.min(4, zoomEstadoPorPanel[id] + 0.25); aplicarZoomPreview(id); });
  wrap.querySelector('[data-act="out"]').addEventListener("click", () => { zoomEstadoPorPanel[id] = Math.max(0.4, zoomEstadoPorPanel[id] - 0.25); aplicarZoomPreview(id); });
  wrap.querySelector('[data-act="reset"]').addEventListener("click", () => { zoomEstadoPorPanel[id] = 1; panEstadoPorPanel[id] = {x: 0, y: 0}; aplicarZoomPreview(id); });
  wrap.querySelector('[data-act="expand"]').addEventListener("click", () => expandirPreview(id, titulo));

  const contenedor = wrap.querySelector(".preview-canvas-wrap");
  contenedor.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomEstadoPorPanel[id] = Math.max(0.4, Math.min(4, zoomEstadoPorPanel[id] + (e.deltaY < 0 ? 0.15 : -0.15)));
    aplicarZoomPreview(id);
  }, {passive: false});

  let arrastrando = false, startX = 0, startY = 0, panStart = {x: 0, y: 0};
  contenedor.addEventListener("mousedown", (e) => {
    arrastrando = true; startX = e.clientX; startY = e.clientY;
    panStart = {...panEstadoPorPanel[id]};
    contenedor.classList.add("panning");
  });
  window.addEventListener("mousemove", (e) => {
    if (!arrastrando) return;
    panEstadoPorPanel[id] = {x: panStart.x + (e.clientX - startX), y: panStart.y + (e.clientY - startY)};
    aplicarZoomPreview(id);
  });
  window.addEventListener("mouseup", () => { arrastrando = false; contenedor.classList.remove("panning"); });

  // como en el visor de Premiere: mientras este en modo "Ajustar" (zoom == 1, el
  // default), el encuadre se recalcula solo cada vez que el panel cambia de tamano
  // (al arrastrar el separador, al redimensionar la ventana, etc.), no solo cuando
  // llega una imagen nueva.
  new ResizeObserver(() => {
    const cnv = document.getElementById(id);
    if (zoomEstadoPorPanel[id] === 1 && cnv && cnv.dataset.pintado) recalcularEncuadre(id);
  }).observe(contenedor);

  return wrap;
}
function recalcularEncuadre(canvasId) {
  const c = document.getElementById(canvasId);
  if (!c || !c.width || !c.height || !c.dataset.pintado) return;
  const wrapEl = c.parentElement;
  const maxW = Math.max(40, wrapEl.clientWidth - 16), maxH = Math.max(40, wrapEl.clientHeight - 16);
  const escala = Math.max(0.05, Math.min(maxW / c.width, maxH / c.height, 1) || 1);
  c.dataset.baseW = c.width * escala;
  c.dataset.ratio = c.height / c.width;
  aplicarZoomPreview(canvasId);
}
function aplicarZoomPreview(canvasId) {
  const c = document.getElementById(canvasId);
  const base = Number(c.dataset.baseW || 0);
  if (!base) return;
  const z = zoomEstadoPorPanel[canvasId] || 1;
  const pan = panEstadoPorPanel[canvasId] || {x: 0, y: 0};
  const ratio = Number(c.dataset.ratio || 1);
  c.style.width = (base * z) + "px";
  c.style.height = (base * z * ratio) + "px";
  c.style.transform = `translate(calc(-50% + ${pan.x}px), calc(-50% + ${pan.y}px))`;
}
function pintarImagenEnCanvas(canvasId, dataUrl) {
  const c = document.getElementById(canvasId);
  if (!c) return;
  const img = new Image();
  img.onload = () => {
    requestAnimationFrame(() => {
      c.width = img.width; c.height = img.height;
      c.dataset.pintado = "1";
      c.getContext("2d").drawImage(img, 0, 0);
      recalcularEncuadre(canvasId);
      void c.offsetHeight; // fuerza un reflow: en esta combinacion pywebview/WebView2
                            // a veces el canvas no se repinta solo tras dibujar por codigo.
    });
  };
  img.src = dataUrl;
}
function expandirPreview(canvasId, titulo) {
  const src = document.getElementById(canvasId).toDataURL();
  const overlay = document.createElement("div");
  overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.75);display:flex;align-items:center;justify-content:center;z-index:100;animation:fadein .2s ease";
  overlay.innerHTML = `<div style="background:var(--bg-card);border-radius:20px;padding:20px;width:92vw;height:90vh;display:flex;flex-direction:column;gap:12px;align-items:center;box-sizing:border-box">
    <div style="font-weight:700;flex-shrink:0">${titulo} — click afuera de la imagen para cerrar</div>
    <div style="flex:1;min-height:0;width:100%;display:flex;align-items:center;justify-content:center;background:var(--bg-canvas);border-radius:12px">
      <img src="${src}" style="max-width:100%;max-height:100%;object-fit:contain">
    </div>
  </div>`;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}

const panelAntesMascara = crearPanelPreview("Antes (en vivo)", "cnvAntesMascara");
const panelDespuesMascara = crearPanelPreview("Despues (en vivo)", "cnvDespuesMascara");
document.getElementById("previewColMascara").append(panelAntesMascara, panelDespuesMascara);

const panelAntesMotor = crearPanelPreview("Antes (en vivo)", "cnvAntesMotor");
const panelDespuesMotor = crearPanelPreview("Despues (en vivo)", "cnvDespuesMotor");
document.getElementById("previewColMotor").append(panelAntesMotor, panelDespuesMotor);

function debounceActualizarPreview(contexto) {
  clearTimeout(state.debounceTimer);
  state.debounceTimer = setTimeout(() => actualizarPreview(contexto), 160);
}
async function actualizarPreview(contexto) {
  if (!state.frameImg || !hayMascara()) return;
  const maskB64 = cnvMask.toDataURL();
  const resultado = await api.render_preview(state.motor, maskB64, state.sigma);
  if (!resultado) return;
  if (contexto === "mascara") {
    pintarImagenEnCanvas("cnvAntesMascara", resultado.antes_b64);
    pintarImagenEnCanvas("cnvDespuesMascara", resultado.despues_b64);
  } else {
    pintarImagenEnCanvas("cnvAntesMotor", resultado.antes_b64);
    pintarImagenEnCanvas("cnvDespuesMotor", resultado.despues_b64);
  }
}

/* ---------------- Paso 3: Motor y calidad ---------------- */
document.querySelectorAll(".engine-card").forEach(card => {
  card.addEventListener("click", () => {
    document.querySelectorAll(".engine-card").forEach(c => c.classList.remove("selected"));
    card.classList.add("selected");
    state.motor = card.dataset.engine;
    actualizarPreview("motor");
  });
});
document.getElementById("sliderSigma").addEventListener("input", (e) => { state.sigma = Number(e.target.value); debounceActualizarPreview("motor"); });
document.querySelectorAll('input[name="calidad"]').forEach(r => r.addEventListener("change", (e) => { state.calidad = e.target.value; }));
document.querySelectorAll("#pillVelocidad .pill").forEach(p => {
  p.addEventListener("click", () => {
    document.querySelectorAll("#pillVelocidad .pill").forEach(x => x.classList.remove("selected"));
    p.classList.add("selected");
    state.velocidad = p.dataset.vel;
  });
});
document.getElementById("selectResolucion").addEventListener("change", (e) => { state.resolucion = e.target.value; });

/* ---------------- Paso 4: Procesar ---------------- */
const CIRC = 2 * Math.PI * 72;
function setAnillo(frac) {
  const off = CIRC - frac * CIRC;
  document.getElementById("ringFill").style.strokeDashoffset = off;
  document.getElementById("ringLabel").textContent = Math.round(frac * 100) + "%";
}
function logLinea(texto, tipo) {
  const box = document.getElementById("logBox");
  const div = document.createElement("div");
  if (tipo) div.className = tipo;
  div.textContent = texto;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}
function actualizarProgresoClip(index, frac) {
  const rows = document.querySelectorAll("#clipList .clip-row .bar > div");
  if (rows[index]) rows[index].style.width = Math.round(frac * 100) + "%";
}

let _pollTimer = null;
let _logsMostrados = 0;

document.getElementById("btnCancelar").addEventListener("click", () => {
  const btnCancelar = document.getElementById("btnCancelar");
  btnCancelar.disabled = true;
  btnCancelar.textContent = "Cancelando...";
  api.cancelar_procesamiento().catch(() => {});
});

async function procesarTodo() {
  const btn = document.getElementById("btnSiguiente");
  const btnCancelar = document.getElementById("btnCancelar");

  const estadoPrevio = await api.estado_licencia().catch(() => null);
  if (estadoPrevio && !estadoPrevio.pro && state.clips.length > estadoPrevio.restantes) {
    document.getElementById("logBox").innerHTML = "";
    _logsMostrados = 0;
    logLinea(
      `Llegaste al limite de la version gratis: ${estadoPrevio.limite} clips por dia (te quedan ${estadoPrevio.restantes}).`,
      "err"
    );
    logLinea("Activa PixelClean Pro (boton con la llave, arriba) para procesar sin limite diario.", "err");
    abrirModalLicencia();
    return;
  }

  btn.disabled = true;
  btnCancelar.style.display = "inline-flex";
  btnCancelar.disabled = false;
  btnCancelar.textContent = "Cancelar";
  document.getElementById("logBox").innerHTML = "";
  _logsMostrados = 0;
  logLinea("Iniciando procesamiento...");
  setAnillo(0);
  document.querySelector(".ring-wrap").classList.add("procesando");
  document.querySelectorAll("#clipList .clip-row .bar > div").forEach((b) => { b.style.width = "0%"; });

  const payload = {
    mascara_b64: cnvMask.toDataURL(),
    motor: state.motor, sigma: state.sigma, calidad: state.calidad,
    velocidad: state.velocidad, resolucion: state.resolucion, clips: state.clips,
  };

  // No esperamos la respuesta de esta llamada: en esta combinacion de tecnologias
  // (pywebview + WinForms + WebView2) la promesa a veces se queda colgada aunque
  // el trabajo en Python arranque bien igual. Arrancamos a consultar el progreso
  // en paralelo, que es una llamada mas chica y confiable.
  api.procesar_todo(payload).catch((err) => {
    logLinea("Aviso al iniciar (puede ser normal): " + err.message);
  });

  let intentosSinRespuesta = 0;
  let vimosArranque = false;
  clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    let p;
    try {
      const resp = await fetch(`/progreso?_=${Date.now()}`);
      p = await resp.json();
      intentosSinRespuesta = 0;
    } catch (err) {
      intentosSinRespuesta++;
      if (intentosSinRespuesta === 6) {
        logLinea("No se pudo consultar el progreso todavia, sigo intentando...");
      }
      return;
    }
    if (p.total > 0 || (p.logs && p.logs.length > 0)) vimosArranque = true;
    const porClip = p.por_clip || [];
    const fracGeneral = p.total ? porClip.reduce((a, b) => a + b, 0) / p.total : 0;
    setAnillo(fracGeneral);
    document.getElementById("lblProcesoInfo").textContent = `${p.completados} / ${p.total} clips`;
    porClip.forEach((frac, i) => actualizarProgresoClip(i, frac));
    (p.logs || []).slice(_logsMostrados).forEach((l) => logLinea(l.mensaje, l.ok ? "ok" : "err"));
    _logsMostrados = (p.logs || []).length;
    document.querySelector(".ring-wrap").classList.toggle("procesando", !p.terminado);
    if (p.terminado && vimosArranque) {
      clearInterval(_pollTimer);
      btn.disabled = false;
      btnCancelar.style.display = "none";
      document.querySelector(".ring-wrap").classList.remove("procesando");
      refrescarPlanBadge();
    }
  }, 500);
}

/* ---------------- Atajos de teclado estilo Photoshop (activos en el paso Mascara) ---------------- */
window.addEventListener("keydown", (e) => {
  if (state.step !== 1) return;
  const ctrl = e.ctrlKey || e.metaKey;

  if (e.code === "Space" && !state.spacePanning) {
    state.spacePanning = true;
    state.toolBeforeSpace = state.tool;
    setTool("hand");
    e.preventDefault();
    return;
  }
  if (ctrl && e.key.toLowerCase() === "z") {
    e.preventDefault();
    if (e.shiftKey) document.getElementById("btnRedo").click();
    else document.getElementById("btnUndo").click();
    return;
  }
  if (ctrl && e.key.toLowerCase() === "y") { e.preventDefault(); document.getElementById("btnRedo").click(); return; }
  if (ctrl && (e.key === "=" || e.key === "+")) { e.preventDefault(); cambiarZoom(0.2); return; }
  if (ctrl && e.key === "-") { e.preventDefault(); cambiarZoom(-0.2); return; }
  if (ctrl && e.key === "0") { e.preventDefault(); fitCanvasToViewport(); return; }
  if (ctrl) return;

  switch (e.key.toLowerCase()) {
    case "b": setTool("brush"); break;
    case "e": setTool("eraser"); break;
    case "h": setTool("hand"); break;
    case "[": e.shiftKey ? setHardness(state.hardness - 10) : setBrushSize(state.brushSize - 4); break;
    case "]": e.shiftKey ? setHardness(state.hardness + 10) : setBrushSize(state.brushSize + 4); break;
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space" && state.spacePanning) {
    state.spacePanning = false;
    setTool(state.toolBeforeSpace || "brush");
  }
});

window.addEventListener("keydown", (e) => {
  if (e.key === "F9") {
    const info = {
      windowPywebview: !!window.pywebview,
      apiExists: !!(window.pywebview && window.pywebview.api),
      metodos: window.pywebview && window.pywebview.api ? Object.keys(window.pywebview.api) : [],
      apiEsMock: api === mockApi,
    };
    console.log("DIAG_F9", JSON.stringify(info));
    document.getElementById("lblClips").textContent = JSON.stringify(info);
  }
});

renderSteps();
mostrarPaso(0);
