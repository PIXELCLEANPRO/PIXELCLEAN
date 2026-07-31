// Supabase Edge Function: recibe reportes de soporte desde la app de
// escritorio (mensaje del usuario + diagnostico + log + adjunto opcional),
// los guarda en public.soporte_reportes y avisa por mail.
// Secrets necesarios: GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFICATION_EMAIL.

import { SMTPClient } from "https://deno.land/x/denomailer@1.6.0/mod.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

async function enviarEmail(
  { to, subject, html, attachments }:
  { to: string[]; subject: string; html: string; attachments?: { filename: string; content: string }[] }
) {
  const gmailUser = Deno.env.get("GMAIL_USER");
  const gmailPass = Deno.env.get("GMAIL_APP_PASSWORD");
  if (!gmailUser || !gmailPass) return;

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
      attachments: (attachments || []).map((a) => ({
        filename: a.filename,
        content: a.content,
        encoding: "base64",
      })),
    });
  } finally {
    await client.close();
  }
}

function escapeHtml(s: string) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

Deno.serve(async (req) => {
  try {
    if (req.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const body = await req.json();
    const email = body.email || null;
    const mensaje = (body.mensaje || "").toString().slice(0, 5000);
    const diagnostico = body.diagnostico || null;
    const log = body.log ? String(body.log).slice(0, 20000) : null;
    const adjunto = body.adjunto || null; // { nombre, datos_b64 }

    if (!mensaje.trim()) {
      return new Response(JSON.stringify({ ok: false, error: "Falta el mensaje." }), { status: 400 });
    }

    const insertRes = await fetch(`${SUPABASE_URL}/rest/v1/soporte_reportes`, {
      method: "POST",
      headers: {
        apikey: SERVICE_ROLE_KEY,
        Authorization: `Bearer ${SERVICE_ROLE_KEY}`,
        "Content-Type": "application/json",
        Prefer: "return=minimal",
      },
      body: JSON.stringify({
        email,
        mensaje,
        diagnostico,
        log_excerpt: log,
        adjunto_nombre: adjunto?.nombre || null,
      }),
    });
    if (!insertRes.ok) {
      console.error("No se pudo guardar el reporte:", await insertRes.text());
    }

    const destinatario = Deno.env.get("NOTIFICATION_EMAIL");
    if (destinatario) {
      const diagHtml = diagnostico
        ? `<ul>${Object.entries(diagnostico).map(([k, v]) => `<li><strong>${escapeHtml(k)}:</strong> ${escapeHtml(String(v))}</li>`).join("")}</ul>`
        : "<p>(sin diagnostico)</p>";
      const attachments = [];
      if (adjunto?.datos_b64 && adjunto?.nombre) {
        attachments.push({ filename: adjunto.nombre, content: adjunto.datos_b64 });
      }
      if (log) {
        attachments.push({ filename: "clipxel-debug.log", content: btoa(unescape(encodeURIComponent(log))) });
      }
      await enviarEmail({
        to: [destinatario],
        subject: `Reporte de soporte Clipxel${email ? " - " + email : ""}`,
        html: `
          <p><strong>De:</strong> ${escapeHtml(email || "(sin cuenta)")}</p>
          <p><strong>Mensaje:</strong></p>
          <p>${escapeHtml(mensaje).replace(/\n/g, "<br>")}</p>
          <p><strong>Diagnostico:</strong></p>
          ${diagHtml}
        `,
        attachments,
      });
    }

    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
  } catch (e) {
    console.error(e);
    return new Response(JSON.stringify({ ok: false, error: (e as Error).message }), { status: 500 });
  }
});
