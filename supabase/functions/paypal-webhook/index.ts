// Supabase Edge Function: recibe el webhook de PayPal cuando se completa un
// pago, verifica que sea autentico, y liga la compra a la cuenta de Google
// del comprador (tabla public.licenses) para que se active sola cuando
// inicie sesion (ver public.iniciar_sesion en la base).
//
// Deploy: Dashboard de Supabase -> Edge Functions -> paypal-webhook -> pegar
// este archivo como index.ts. Secrets necesarios (Edge Functions -> Secrets):
//   PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_WEBHOOK_ID,
//   GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFICATION_EMAIL (opcional)
// SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY los provee Supabase solo, no hace
// falta configurarlos a mano.

import { SMTPClient } from "https://deno.land/x/denomailer@1.6.0/mod.ts";

const PAYPAL_API = "https://api-m.paypal.com";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

async function obtenerAccessTokenPayPal() {
  const clientId = Deno.env.get("PAYPAL_CLIENT_ID");
  const clientSecret = Deno.env.get("PAYPAL_CLIENT_SECRET");
  const auth = btoa(`${clientId}:${clientSecret}`);
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
  return datos.access_token as string;
}

async function verificarFirmaWebhook(headers: Headers, body: unknown, accessToken: string) {
  const res = await fetch(`${PAYPAL_API}/v1/notifications/verify-webhook-signature`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      transmission_id: headers.get("paypal-transmission-id"),
      transmission_time: headers.get("paypal-transmission-time"),
      cert_url: headers.get("paypal-cert-url"),
      auth_algo: headers.get("paypal-auth-algo"),
      transmission_sig: headers.get("paypal-transmission-sig"),
      webhook_id: Deno.env.get("PAYPAL_WEBHOOK_ID"),
      webhook_event: body,
    }),
  });
  const datos = await res.json();
  return datos.verification_status === "SUCCESS";
}

async function obtenerEmailComprador(resource: any, accessToken: string) {
  if (resource.payer?.email_address) return resource.payer.email_address as string;
  const orderId = resource.supplementary_data?.related_ids?.order_id;
  if (!orderId) return null;
  const res = await fetch(`${PAYPAL_API}/v2/checkout/orders/${orderId}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  const orden = await res.json();
  return orden.payer?.email_address ?? null;
}

async function enviarEmail({ to, subject, html }: { to: string[]; subject: string; html: string }) {
  const gmailUser = Deno.env.get("GMAIL_USER");
  const gmailPass = Deno.env.get("GMAIL_APP_PASSWORD");
  if (!gmailUser || !gmailPass) return; // sin credenciales configuradas, no rompe el webhook

  const client = new SMTPClient({
    connection: {
      hostname: "smtp.gmail.com",
      port: 465,
      tls: true,
      auth: { username: gmailUser, password: gmailPass },
    },
  });
  try {
    await client.send({
      from: `CLIPXEL Studio <${gmailUser}>`,
      to,
      subject,
      html,
      content: "auto",
    });
  } finally {
    await client.close();
  }
}

function generarClaveDeRegistro() {
  // Solo un identificador legible de la compra para soporte / PayPal; la
  // activacion real no depende de que el comprador la copie a ningun lado.
  const bytes = crypto.getRandomValues(new Uint8Array(10));
  const hex = Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
  return `CLIPXEL-${hex.slice(0, 5)}-${hex.slice(5, 10)}-${hex.slice(10, 15)}-${hex.slice(15, 20)}`;
}

async function registrarCompra(emailComprador: string, monto: string | null, moneda: string | null, orderId: string | null) {
  const claveRegistro = generarClaveDeRegistro();
  const res = await fetch(`${SUPABASE_URL}/rest/v1/licenses`, {
    method: "POST",
    headers: {
      apikey: SERVICE_ROLE_KEY,
      Authorization: `Bearer ${SERVICE_ROLE_KEY}`,
      "Content-Type": "application/json",
      Prefer: "return=minimal",
    },
    body: JSON.stringify({
      license_key: claveRegistro,
      buyer_email: emailComprador,
      paypal_order_id: orderId,
      monto,
      moneda,
    }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error("No se pudo registrar la compra en Supabase: " + err);
  }
}

// Esto solo se llama despues de verificar la firma criptografica del
// webhook de PayPal (mas arriba), asi que no hay forma de activar una
// cuenta gratis a Pro sin un pago real y confirmado.
async function entregarAlComprador(emailComprador: string | null, monto: string | null, moneda: string | null, orderId: string | null) {
  const destinatario = Deno.env.get("NOTIFICATION_EMAIL");

  if (!emailComprador) {
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

Deno.serve(async (req) => {
  try {
    if (req.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const body = await req.json();
    const accessToken = await obtenerAccessTokenPayPal();

    const valido = await verificarFirmaWebhook(req.headers, body, accessToken);
    if (!valido) {
      console.error("Webhook con firma invalida", body.event_type);
      return new Response("Firma invalida", { status: 400 });
    }

    if (body.event_type === "PAYMENT.CAPTURE.COMPLETED") {
      const email = await obtenerEmailComprador(body.resource, accessToken);
      const monto = body.resource?.amount?.value ?? null;
      const moneda = body.resource?.amount?.currency_code ?? null;
      const orderId = body.resource?.supplementary_data?.related_ids?.order_id ?? null;
      await entregarAlComprador(email, monto, moneda, orderId);
      console.log("Compra registrada, comprador:", email);
    }

    return new Response("ok", { status: 200 });
  } catch (e) {
    console.error(e);
    return new Response("error: " + (e as Error).message, { status: 500 });
  }
});
