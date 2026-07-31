// Widget de cuenta para el nav del sitio: si hay sesion, muestra un avatar
// con dropdown (email, plan, link a cuenta.html, cerrar sesion). Si no,
// muestra un link a cuenta.html para iniciar sesion / registrarse.
(function () {
  var SUPABASE_URL = "https://ujuibmpvicuibidkbdrq.supabase.co";
  var SUPABASE_ANON_KEY = "sb_publishable_x3mWkJJdqcimXIVkgbHEUA_-TWxzxtf";

  function iniciar() {
    var mount = document.getElementById("navCtaAccount");
    if (!mount || typeof supabase === "undefined") return;

    var sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

    function pintarLogueado(email, plan) {
      var inicial = email ? email[0].toUpperCase() : "?";
      var chipClase = plan === "pro" ? "cuenta-nav-chip is-pro" : "cuenta-nav-chip";
      var chipTexto = plan === "pro" ? "Pro" : "Gratis";
      mount.innerHTML =
        '<div class="cuenta-nav-wrap">' +
          '<button class="cuenta-nav-avatar" id="cnavBtn">' + inicial + '</button>' +
          '<div class="cuenta-nav-pop" id="cnavPop">' +
            '<div class="cuenta-nav-head">' +
              '<div class="cuenta-nav-avatar" style="width:38px;height:38px;font-size:15px">' + inicial + '</div>' +
              '<div>' +
                '<div class="cuenta-nav-email">' + email + '</div>' +
                '<span class="' + chipClase + '">' + chipTexto + '</span>' +
              '</div>' +
            '</div>' +
            '<a class="cuenta-nav-item" href="cuenta.html">Administrar cuenta</a>' +
            '<div class="cuenta-nav-sep"></div>' +
            '<button class="cuenta-nav-item cuenta-nav-item-danger" id="cnavSalir">Cerrar sesión</button>' +
          '</div>' +
        '</div>';

      document.getElementById("cnavBtn").addEventListener("click", function (e) {
        e.stopPropagation();
        document.getElementById("cnavPop").classList.toggle("open");
      });
      document.addEventListener("click", function (e) {
        var pop = document.getElementById("cnavPop");
        if (pop && pop.classList.contains("open") && !e.target.closest(".cuenta-nav-wrap")) {
          pop.classList.remove("open");
        }
      });
      document.getElementById("cnavSalir").addEventListener("click", async function () {
        await sb.auth.signOut();
        window.location.reload();
      });
    }

    function pintarDeslogueado() {
      mount.innerHTML = '<a class="btn btn-ghost" href="cuenta.html">Mi cuenta</a>';
    }

    async function refrescar() {
      var { data } = await sb.auth.getSession();
      if (!data.session) {
        pintarDeslogueado();
        return;
      }
      var { data: perfilRows } = await sb.rpc("mi_perfil");
      var perfil = (perfilRows && perfilRows[0]) || { plan: "free" };
      pintarLogueado(data.session.user.email, perfil.plan);
    }

    sb.auth.onAuthStateChange(refrescar);
    refrescar();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();
