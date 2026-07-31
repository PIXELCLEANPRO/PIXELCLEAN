// Recibe el webhook de PayPal cuando se completa un pago, verifica que sea
// autentico, y liga la compra a la cuenta de Google del comprador para que
// se active sola cuando inicie sesion (ver public.iniciar_sesion en Supabase).
const PAYPAL_API = "https://api-m.paypal.com";
const SUPABASE_URL = "https://ujuibmpvicuibidkbdrq.supabase.co";

async function obtenerAccessToken() {
  const auth = Buffer.from(
    `${process.env.PAYPAL_CLIENT_ID}:${process.env.PAYPAL_CLIENT_SECRET}`
  ).toString("base64");
  const res = await fetch(`${PAYPAL_API}/v1/oauth2/token`, {
    method: "POST",
    headers: {
      Authorization: `Basic ${auth}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: "grant_type=client_credentials",
  });
  const datos = await res.json();
  if (!res.ok) throw new Error("No se pudo obtener token de PayPal: " + JSON.stringify(datos));
  return datos.access_token;
}

async function verificarFirmaWebhook(headers, body, accessToken) {
  const res = await fetch(`${PAYPAL_API}/v1/notifications/verify-webhook-signature`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      transmission_id: headers["paypal-transmission-id"],
      transmission_time: headers["paypal-transmission-time"],
      cert_url: headers["paypal-cert-url"],
      auth_algo: headers["paypal-auth-algo"],
      transmission_sig: headers["paypal-transmission-sig"],
      webhook_id: process.env.PAYPAL_WEBHOOK_ID,
      webhook_event: body,
    }),
  });
  const datos = await res.json();
  return datos.verification_status === "SUCCESS";
}

async function obtenerEmailComprador(resource, accessToken) {
  if (resource.payer && resource.payer.email_address) return resource.payer.email_address;
  const orderId =
    resource.supplementary_data &&
    resource.supplementary_data.related_ids &&
    resource.supplementary_data.related_ids.order_id;
  if (!orderId) return null;
  const res = await fetch(`${PAYPAL_API}/v2/checkout/orders/${orderId}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  const orden = await res.json();
  return (orden.payer && orden.payer.email_address) || null;
}

async function enviarEmail({ to, subject, html }) {
  const from = process.env.RESEND_FROM || "CLIPXEL <onboarding@resend.dev>";
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ from, to, subject, html }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error("Fallo el envio de email: " + err);
  }
}

function generarClaveDeRegistro() {
  // Solo para tener un identificador legible de la compra en soporte /
  // PayPal; la activacion real no depende de que el comprador la copie a
  // ningun lado, pasa sola al iniciar sesion con el mismo Google.
  const bytes = require("crypto").randomBytes(10).toString("hex").toUpperCase();
  return `CLIPXEL-${bytes.slice(0, 5)}-${bytes.slice(5, 10)}-${bytes.slice(10, 15)}-${bytes.slice(15, 20)}`;
}

async function registrarCompra(emailComprador, monto, moneda, orderId) {
  const claveRegistro = generarClaveDeRegistro();
  const res = await fetch(`${SUPABASE_URL}/rest/v1/licenses`, {
    method: "POST",
    headers: {
      apikey: process.env.SUPABASE_SERVICE_KEY,
      Authorization: `Bearer ${process.env.SUPABASE_SERVICE_KEY}`,
      "Content-Type": "application/json",
      Prefer: "return=minimal",
    },
    body: JSON.stringify({
      license_key: claveRegistro,
      buyer_email: emailComprador,
      paypal_order_id: orderId || null,
      monto: monto || null,
      moneda: moneda || null,
    }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error("No se pudo registrar la compra en Supabase: " + err);
  }
  return claveRegistro;
}

// Esto solo se llama despues de verificar la firma criptografica del
// webhook de PayPal (mas arriba), asi que no hay forma de activar una
// cuenta gratis a Pro sin un pago real y confirmado.
async function entregarAlComprador(emailComprador, monto, moneda, orderId) {
  if (!emailComprador) {
    const destinatario = process.env.NOTIFICATION_EMAIL;
    if (destinatario) {
      await enviarEmail({
        to: [destinatario],
        subject: `Venta CLIPXEL Pro sin email de comprador (${monto} ${moneda})`,
        html: `<p>Se registro un pago pero no se pudo determinar el email del comprador. Revisar en PayPal y activar la cuenta a mano en Supabase (tabla licenses).</p>`,
      });
    }
    return;
  }

  await registrarCompra(emailComprador, monto, moneda, orderId);

  await enviarEmail({
    to: [emailComprador],
    subject: "Tu compra de CLIPXEL Pro",
    html: `
      <p>¡Gracias por tu compra!</p>
      <p>Para activar <strong>CLIPXEL Pro</strong> solo tenés que iniciar sesión con esta misma cuenta de Google (${emailComprador}):</p>
      <ul>
        <li>Dentro del programa Clipxel, tocá "Iniciar sesión con Google".</li>
        <li>O entrá a <a href="https://clipxel.github.io">clipxel.github.io</a> e iniciá sesión ahí.</li>
      </ul>
      <p>Se activa solo, sin claves que copiar. Cualquier problema, respondé este email.</p>
    `,
  });

  const destinatario = process.env.NOTIFICATION_EMAIL;
  if (destinatario) {
    await enviarEmail({
      to: [destinatario],
      subject: `Nueva venta CLIPXEL Pro (${monto} ${moneda})`,
      html: `
        <p>Se registro un pago nuevo de CLIPXEL Pro.</p>
        <p><strong>Comprador:</strong> ${emailComprador}</p>
        <p><strong>Monto:</strong> ${monto} ${moneda}</p>
        <p>Se le aviso que active iniciando sesion con Google.</p>
      `,
    });
  }
}

exports.handler = async (event) => {
  try {
    if (event.httpMethod !== "POST") {
      return { statusCode: 405, body: "Method not allowed" };
    }

    const body = JSON.parse(event.body);
    const accessToken = await obtenerAccessToken();

    const headersMin = {};
    Object.keys(event.headers || {}).forEach((k) => {
      headersMin[k.toLowerCase()] = event.headers[k];
    });

    const valido = await verificarFirmaWebhook(headersMin, body, accessToken);
    if (!valido) {
      console.error("Webhook con firma invalida", body.event_type);
      return { statusCode: 400, body: "Firma invalida" };
    }

    if (body.event_type === "PAYMENT.CAPTURE.COMPLETED") {
      const email = await obtenerEmailComprador(body.resource, accessToken);
      const monto = body.resource.amount && body.resource.amount.value;
      const moneda = body.resource.amount && body.resource.amount.currency_code;
      const orderId =
        body.resource.supplementary_data &&
        body.resource.supplementary_data.related_ids &&
        body.resource.supplementary_data.related_ids.order_id;
      await entregarAlComprador(email, monto, moneda, orderId);
      console.log("Compra registrada, comprador:", email);
    }

    return { statusCode: 200, body: "ok" };
  } catch (e) {
    console.error(e);
    return { statusCode: 500, body: "error: " + e.message };
  }
};
