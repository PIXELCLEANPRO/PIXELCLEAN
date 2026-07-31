/* Selector de idioma + sugerencia por idioma del navegador — compartido por todo el sitio.
   Para sumar un idioma nuevo: agregarlo a SUPPORTED y NAMES. Nada mas hay que tocar aca. */
(function () {
  var SUPPORTED = ["es", "en"];
  var NAMES = {
    es: "Español", en: "English", pt: "Português", fr: "Français",
    de: "Deutsch", zh: "中文", ru: "Русский", ja: "日本語"
  };
  var SUGGEST_TEXT = {
    es: "Esta página también está disponible en",
    en: "This page is also available in"
  };

  var path = location.pathname;
  var m = path.match(/^\/(es|en|pt|fr|de|zh|ru|ja)\/(.*)$/);
  var curLang, file;
  if (m) { curLang = m[1]; file = m[2] || "index.html"; }
  else { curLang = "es"; file = path.replace(/^\//, "") || "index.html"; }
  if (file === "") file = "index.html";

  function urlFor(lang) {
    return lang === "es" ? "/" + file : "/" + lang + "/" + file;
  }

  function setLang(lang) {
    try { localStorage.setItem("pc_lang", lang); } catch (e) {}
  }

  /* ---- dropdown selector manual ---- */
  function buildSwitcher() {
    var mount = document.getElementById("langSwitchMount");
    if (!mount) return;
    var wrap = document.createElement("div");
    wrap.className = "lang-switch";
    var btn = document.createElement("button");
    btn.className = "lang-btn";
    btn.type = "button";
    btn.setAttribute("aria-haspopup", "true");
    btn.innerHTML = '<span class="lbl">' + NAMES[curLang] + "</span><span class=\"car\">▾</span>";
    var menu = document.createElement("div");
    menu.className = "lang-menu";
    SUPPORTED.forEach(function (lang) {
      var a = document.createElement("a");
      a.href = urlFor(lang);
      a.textContent = NAMES[lang];
      if (lang === curLang) a.className = "current";
      a.addEventListener("click", function () { setLang(lang); });
      menu.appendChild(a);
    });
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      menu.classList.toggle("open");
    });
    document.addEventListener("click", function () { menu.classList.remove("open"); });
    wrap.appendChild(btn);
    wrap.appendChild(menu);
    mount.appendChild(wrap);
  }

  /* ---- banner de sugerencia segun idioma del navegador ---- */
  function buildSuggestBanner() {
    var dismissed = false;
    try { dismissed = localStorage.getItem("pc_lang_dismissed") === "1"; } catch (e) {}
    var chosen = null;
    try { chosen = localStorage.getItem("pc_lang"); } catch (e) {}
    if (chosen) return; // el usuario ya eligio idioma a mano, no lo molestamos mas
    if (dismissed) return;

    var browserLangs = navigator.languages || [navigator.language || "es"];
    var suggested = null;
    for (var i = 0; i < browserLangs.length; i++) {
      var code = (browserLangs[i] || "").slice(0, 2).toLowerCase();
      if (SUPPORTED.indexOf(code) !== -1 && code !== curLang) { suggested = code; break; }
    }
    if (!suggested) return;

    var bar = document.createElement("div");
    bar.className = "lang-suggest";
    var text = SUGGEST_TEXT[curLang] || SUGGEST_TEXT.es;
    var a = document.createElement("a");
    a.href = urlFor(suggested);
    a.textContent = NAMES[suggested];
    a.addEventListener("click", function () { setLang(suggested); });
    var span = document.createElement("span");
    span.textContent = text + " ";
    var closeBtn = document.createElement("button");
    closeBtn.setAttribute("aria-label", "Cerrar");
    closeBtn.textContent = "✕";
    closeBtn.addEventListener("click", function () {
      try { localStorage.setItem("pc_lang_dismissed", "1"); } catch (e) {}
      bar.remove();
    });
    bar.appendChild(span);
    bar.appendChild(a);
    bar.appendChild(closeBtn);
    document.body.insertBefore(bar, document.body.firstChild);
  }

  document.addEventListener("DOMContentLoaded", function () {
    buildSwitcher();
    buildSuggestBanner();
  });
})();
