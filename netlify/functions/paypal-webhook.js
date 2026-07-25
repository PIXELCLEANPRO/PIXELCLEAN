// Recibe el webhook de PayPal cuando se completa un pago, verifica que sea
// autentico, y le manda la clave de licencia Pro al comprador por email.
const PAYPAL_API = "https://api-m.paypal.com";

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

async function avisarVentaAlDueno(emailComprador, monto, moneda) {
  const clave = process.env.PIXELCLEAN_LICENSE_KEY;
  const from = process.env.RESEND_FROM || "PixelClean <onboarding@resend.dev>";
  const destinatario = process.env.NOTIFICATION_EMAIL;
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from,
      to: [destinatario],
      subject: `Nueva venta PixelClean Pro (${monto} ${moneda}) - reenviar clave`,
      html: `
        <p>Se registro un pago nuevo de PixelClean Pro.</p>
        <p><strong>Comprador:</strong> ${emailComprador || "(no se pudo determinar el email, revisar en PayPal)"}</p>
        <p><strong>Monto:</strong> ${monto} ${moneda}</p>
        <p>Reenviale esta clave manualmente:</p>
        <p style="font-family:monospace;font-size:18px;background:#f4f4f8;padding:12px 16px;border-radius:8px;display:inline-block">${clave}</p>
      `,
    }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error("Fallo el envio de email: " + err);
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
      await avisarVentaAlDueno(email, monto, moneda);
      console.log("Aviso de venta enviado, comprador:", email);
    }

    return { statusCode: 200, body: "ok" };
  } catch (e) {
    console.error(e);
    return { statusCode: 500, body: "error: " + e.message };
  }
};
