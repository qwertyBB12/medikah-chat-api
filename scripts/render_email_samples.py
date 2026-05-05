"""One-off renderer for Plan 13.1-01 Task 7.

Builds representative HTML samples by composing the email_chrome helper
with stub bodies that mirror what main.py / physician_notifications.py
emit. Run from medikah-chat-api/:

    python3 scripts/render_email_samples.py

Avoids importing main.py / physician_notifications.py directly so the
script doesn't pull in resend / Supabase / OpenAI deps that aren't
installed in every dev env. Helper output is byte-identical to what
the production templates use.

Reusable for future brand-alignment audits.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.email_chrome import (  # noqa: E402
    TOKENS,
    email_footer,
    email_head,
    email_header,
)

OUT_DIR = (
    ROOT.parent
    / ".planning"
    / "phases"
    / "13.1-brand-alignment-email-calendar-surfaces"
    / "render-samples"
)
OUT_DIR.mkdir(parents=True, exist_ok=True)

C = TOKENS["colors"]
F = TOKENS["fonts"]
R = TOKENS["radii"]
PAGE_BG = TOKENS["pageBg"]


def write(name: str, html: str) -> None:
    path = OUT_DIR / name
    path.write_text(html, encoding="utf-8")
    print(f"✓ {name} ({len(html.encode('utf-8'))} bytes)")


def shell(locale: str, body_html: str) -> str:
    head = email_head()
    header = email_header("navy", locale, "medikah")  # type: ignore[arg-type]
    footer = email_footer(locale)  # type: ignore[arg-type]
    return f"""\
<!DOCTYPE html>
<html lang="{locale}">
{head}
<body style="margin:0;padding:0;background-color:{PAGE_BG};font-family:{F['body']};color:{C['bodySlate']};">
{header}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{PAGE_BG};padding:40px 20px;">
  <tr><td align="center">
    <table role="presentation" class="email-container" width="600" cellpadding="0" cellspacing="0" style="background-color:{C['white']};border-radius:{R['md']};overflow:hidden;">
      <tr><td class="email-pad" style="padding:40px 48px;">{body_html}</td></tr>
    </table>
  </td></tr>
</table>
{footer}
</body>
</html>"""


# ---- Physician welcome (mirrors physician_notifications._build_welcome_html) ----
def render_physician_welcome(locale: str) -> str:
    if locale == "es":
        sub = "Bienvenido/a a la red de Medikah"
        greeting = "Estimado/a Dr. Demo,"
        intro = "Su perfil ha sido recibido y esta actualmente bajo revision."
        cta = "Acceder a su panel"
    else:
        sub = "Welcome to the Medikah physician network"
        greeting = "Dear Dr. Demo,"
        intro = "Your profile has been received and is currently under review."
        cta = "Access Your Dashboard"
    body = f"""\
<p style="font-family:{F['ui']};font-size:13px;color:{C['clinicalTeal']};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 16px 0;">{sub}</p>
<p style="font-family:{F['body']};font-size:20px;font-weight:600;color:{C['deepCharcoal']};margin:0 0 24px 0;">{greeting}</p>
<p style="font-family:{F['ui']};font-size:16px;line-height:1.7;color:{C['bodySlate']};margin:0 0 28px 0;">{intro}</p>
<div style="background-color:{C['linen']};border-left:4px solid {C['instBlue']};padding:24px;border-radius:{R['sm']};margin:0 0 28px 0;">
  <p style="font-family:{F['ui']};font-size:14px;color:{C['clinicalTeal']};font-weight:700;margin:0;">Profile under review (1-3 days)</p>
</div>
<p style="text-align:center;"><a href="https://medikah.health/physicians/dashboard" style="display:inline-block;background-color:{C['clinicalTeal']};color:{C['white']};font-family:{F['ui']};text-decoration:none;padding:16px 40px;border-radius:{R['sm']};font-weight:700;">{cta}</a></p>
"""
    return shell(locale, body)


write("backend__physicianWelcome__en.html", render_physician_welcome("en"))
write("backend__physicianWelcome__es.html", render_physician_welcome("es"))


# ---- Patient appointment confirmation (mirrors main.py block) ----
def render_appointment_confirmed(locale: str = "en") -> str:
    body = f"""\
<p style="font-family:{F['ui']};font-size:13px;color:{C['clinicalTeal']};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 16px 0;">Visit Confirmed</p>
<p style="font-family:{F['body']};font-size:20px;font-weight:600;color:{C['deepCharcoal']};margin:0 0 24px 0;">Hi Maria,</p>
<p style="font-family:{F['ui']};font-size:16px;line-height:1.7;color:{C['bodySlate']};margin:0 0 24px 0;">Great news — your Medikah visit is confirmed. Here are your appointment details:</p>
<div style="background-color:{C['linen']};border-left:4px solid {C['instBlue']};padding:24px;border-radius:{R['sm']};margin:0 0 28px 0;">
  <table role="presentation" style="width:100%;border-collapse:collapse;">
    <tr><td style="font-family:{F['ui']};padding:10px 0;color:{C['bodySlate']};font-size:12px;text-transform:uppercase;letter-spacing:0.05em;width:110px;font-weight:600;">Doctor</td>
        <td style="font-family:{F['ui']};padding:10px 0;color:{C['instBlue']};font-size:16px;font-weight:700;">Dr. on call</td></tr>
    <tr><td style="font-family:{F['ui']};padding:10px 0;color:{C['bodySlate']};font-size:12px;text-transform:uppercase;letter-spacing:0.05em;font-weight:600;">Date &amp; Time</td>
        <td style="font-family:{F['ui']};padding:10px 0;color:{C['instBlue']};font-size:16px;font-weight:700;">May 10, 2026 at 10:00 AM CDT</td></tr>
  </table>
</div>
<p style="text-align:center;"><a href="https://doxy.me/medikahhealth/" style="display:inline-block;background-color:{C['instBlue']};color:{C['white']};font-family:{F['ui']};text-decoration:none;padding:16px 40px;border-radius:{R['sm']};font-weight:700;">Join Your Visit</a></p>
<p style="font-family:{F['ui']};font-size:15px;color:{C['bodySlate']};line-height:1.6;font-style:italic;margin:24px 0 8px 0;">Care Without Distance.<br/>Healthcare coordination across the Americas.</p>
<p style="font-family:{F['body']};font-size:16px;font-weight:700;color:{C['instBlue']};margin:0;">— Medikah Care Team</p>
"""
    return shell(locale, body)


write("backend__appointmentConfirmed__en.html", render_appointment_confirmed("en"))


# ---- Inquiry accepted ----
def render_inquiry_accepted(locale: str) -> str:
    if locale == "es":
        header_text = "Consulta Aceptada"
        greeting = "Estimada Maria,"
        intro = "<strong>Dr. Demo</strong> ha aceptado su solicitud de consulta."
        cta = "Acceder a su portal"
    else:
        header_text = "Consultation Accepted"
        greeting = "Dear Maria,"
        intro = "<strong>Dr. Demo</strong> has accepted your consultation request."
        cta = "Access Your Portal"
    body = f"""\
<p style="font-family:{F['ui']};font-size:13px;color:{C['clinicalTeal']};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 16px 0;">{header_text}</p>
<p style="font-family:{F['body']};font-size:20px;font-weight:600;color:{C['deepCharcoal']};margin:0 0 24px 0;">{greeting}</p>
<p style="font-family:{F['ui']};font-size:16px;line-height:1.7;color:{C['bodySlate']};margin:0 0 28px 0;">{intro}</p>
<p style="text-align:center;"><a href="https://medikah.health/patients" style="display:inline-block;background-color:{C['clinicalTeal']};color:{C['white']};font-family:{F['ui']};text-decoration:none;padding:16px 40px;border-radius:{R['sm']};font-weight:700;">{cta}</a></p>
"""
    return shell(locale, body)


write("backend__inquiryAccepted__en.html", render_inquiry_accepted("en"))
write("backend__inquiryAccepted__es.html", render_inquiry_accepted("es"))

print()
print(f"All samples written to: {OUT_DIR}")
print(f"BASE_URL={os.environ.get('BASE_URL', 'https://medikah.health (default)')}")
