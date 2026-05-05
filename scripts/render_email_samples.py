"""Backend email-sample renderer — Plan 13.1-01 Task 7 (Option A: pure editorial).

Body content sits directly on the sand-tone page bg between the two wave
dividers — no outer white card. Mirrors the homepage's section architecture.

Run from medikah-chat-api/:
    python3 scripts/render_email_samples.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.email_chrome import (  # noqa: E402
    TOKENS,
    email_button,
    email_eyebrow,
    email_footer,
    email_head,
    email_header,
    email_heading,
    email_section_label,
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
NAVY_GRAD = TOKENS["gradients"]["navy"]


def write(name: str, html: str) -> None:
    path = OUT_DIR / name
    path.write_text(html, encoding="utf-8")
    print(f"✓ {name} ({len(html.encode('utf-8'))} bytes)")


def shell(locale: str, body_html: str, wordmark: str = "medikah") -> str:
    return f"""\
<!DOCTYPE html>
<html lang="{locale}">
{email_head()}
<body style="margin:0;padding:0;background-color:{PAGE_BG};font-family:{F['body']};color:{C['bodySlate']};">
{email_header("linen", locale, wordmark)}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{PAGE_BG};">
  <tr><td align="center" style="padding:48px 32px 56px 32px;">
    <table role="presentation" class="email-container" width="520" cellpadding="0" cellspacing="0" border="0" style="max-width:520px;">
      <tr><td>{body_html}</td></tr>
    </table>
  </td></tr>
</table>
{email_footer(locale)}
</body>
</html>"""


# ---- Physician welcome ----
def render_physician_welcome(locale: str) -> str:
    if locale == "es":
        eyebrow = "Red de Medikah · En revisión"
        heading = "BIENVENIDO,\nDR. DEMO"
        intro = (
            "Su perfil ha sido recibido y está bajo revisión. Verificamos sus "
            "credenciales y le notificaremos cuando su perfil esté activo."
        )
        cta = "Acceder a su panel"
    else:
        eyebrow = "Medikah Network · Under review"
        heading = "WELCOME,\nDR. DEMO"
        intro = (
            "Your profile has been received and is currently under review. We're "
            "verifying your credentials and will notify you when your profile is live."
        )
        cta = "Access your dashboard"
    body = f"""\
{email_section_label(eyebrow, "light")}
{email_heading(heading, "light", 36)}
<p style="font-family:{F['body']};font-size:16px;line-height:1.7;color:{C['bodySlate']};margin:32px 0 32px 0;">{intro}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{C['white']};border:1px solid {C['hairlineDark']};border-radius:{R['md']};margin:0 0 36px 0;">
  <tr><td style="padding:24px 28px;">
    {email_eyebrow("Verification typically 1-3 days" if locale == "en" else "Verificación: 1-3 días", "light", "0")}
  </td></tr>
</table>
{email_button(cta, "https://medikah.health/physicians/dashboard", "primary")}
"""
    return shell(locale, body)


write("backend__physicianWelcome__en.html", render_physician_welcome("en"))
write("backend__physicianWelcome__es.html", render_physician_welcome("es"))


# ---- Patient appointment confirmation ----
def render_appointment_confirmed(locale: str = "en") -> str:
    body = f"""\
{email_section_label("Visit Confirmed", "light")}
{email_heading("YOUR VISIT\nIS BOOKED", "light", 36)}
<p style="font-family:{F['body']};font-size:16px;font-weight:500;color:{C['deepCharcoal']};margin:32px 0 16px 0;">Hi Maria,</p>
<p style="font-family:{F['body']};font-size:16px;line-height:1.7;color:{C['bodySlate']};margin:0 0 28px 0;">Great news — your Medikah visit is confirmed.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{C['white']};border:1px solid {C['hairlineDark']};border-radius:{R['md']};margin:0 0 36px 0;">
  <tr><td style="padding:24px 28px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td style="font-family:{F['body']};font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:0.25em;color:{C['teal500']};padding:6px 0;width:120px;">Doctor</td>
        <td style="font-family:{F['body']};font-size:15px;font-weight:600;color:{C['instBlue']};padding:6px 0;">Dr. on call</td>
      </tr>
      <tr>
        <td style="font-family:{F['body']};font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:0.25em;color:{C['teal500']};padding:6px 0;">Date &amp; Time</td>
        <td style="font-family:{F['body']};font-size:15px;font-weight:600;color:{C['instBlue']};padding:6px 0;">May 10, 2026 · 10:00 AM CDT</td>
      </tr>
    </table>
  </td></tr>
</table>
{email_button("Join your visit", "https://doxy.me/medikahhealth/", "primary")}
<p style="font-family:{F['body']};font-size:13px;line-height:1.6;color:{C['textMuted']};margin:32px 0 8px 0;">Care Without Distance.</p>
<p style="font-family:{F['body']};font-size:14px;font-weight:600;color:{C['instBlue']};margin:0;">— Medikah Care Team</p>
"""
    return shell(locale, body)


write("backend__appointmentConfirmed__en.html", render_appointment_confirmed("en"))


# ---- Inquiry accepted ----
def render_inquiry_accepted(locale: str) -> str:
    if locale == "es":
        eyebrow = "Consulta · Aceptada"
        heading = "SU CONSULTA\nFUE ACEPTADA"
        intro = "<strong style='color:{}'>Dr. Demo</strong> ha aceptado su solicitud.".format(C["instBlue"])
        cta = "Acceder a su portal"
    else:
        eyebrow = "Consultation · Accepted"
        heading = "YOUR CONSULTATION\nIS ACCEPTED"
        intro = "<strong style='color:{}'>Dr. Demo</strong> has accepted your consultation request.".format(C["instBlue"])
        cta = "Access your portal"
    body = f"""\
{email_section_label(eyebrow, "light")}
{email_heading(heading, "light", 36)}
<p style="font-family:{F['body']};font-size:16px;line-height:1.7;color:{C['bodySlate']};margin:32px 0 32px 0;">{intro}</p>
{email_button(cta, "https://medikah.health/patients", "primary")}
"""
    return shell(locale, body)


write("backend__inquiryAccepted__en.html", render_inquiry_accepted("en"))
write("backend__inquiryAccepted__es.html", render_inquiry_accepted("es"))

print()
print(f"All samples written to: {OUT_DIR}")
print(f"BASE_URL={os.environ.get('BASE_URL', 'https://medikah.health (default)')}")
