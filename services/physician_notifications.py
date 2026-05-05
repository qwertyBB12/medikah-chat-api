"""Physician notification service for welcome and onboarding emails."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from services.email_chrome import (
    TOKENS,
    email_footer,
    email_head,
    email_header,
)
from services.notifications import NotificationMessage, NotificationService

logger = logging.getLogger(__name__)

# Locked-token shorthand (mirror of frontend tokens.ts).
_C = TOKENS["colors"]
_F = TOKENS["fonts"]
_R = TOKENS["radii"]
_PAGE_BG = TOKENS["pageBg"]

# Base URL for dashboard links in emails
_BASE_URL = os.getenv("NEXT_PUBLIC_BASE_URL", "https://medikah.health")


def _build_welcome_html(physician_data: dict, locale: str = "en") -> str:
    """Build the HTML email body for physician welcome email."""
    name = physician_data.get("full_name", "Doctor")
    dashboard_url = f"{_BASE_URL}/physicians/dashboard"

    if locale == "es":
        subject_line = "Bienvenido/a a la red de Medikah"
        greeting = f"Estimado/a Dr. {name},"
        intro = (
            "Nos complace darle la bienvenida a la red de medicos de Medikah. "
            "Su perfil ha sido recibido y esta actualmente bajo revision."
        )
        status_title = "Estado de su perfil"
        status_text = "En revision"
        status_detail = (
            "Nuestro equipo de verificacion esta revisando sus credenciales. "
            "Este proceso generalmente toma de 1 a 3 dias habiles."
        )
        next_steps_title = "Proximos pasos"
        next_steps = [
            "Revisaremos su licencia medica y credenciales",
            "Recibira una notificacion cuando su perfil sea aprobado",
            "Una vez verificado, los pacientes podran encontrarlo en nuestra plataforma",
        ]
        cta_text = "Acceder a su panel"
        support_text = (
            "Si tiene preguntas, no dude en contactarnos en "
            '<a href="mailto:hello@medikah.health" style="color: #2C7A8C; '
            'text-decoration: none; font-weight: 600;">hello@medikah.health</a>'
        )
        sign_off = "Cordialmente,"
        team_name = "El equipo de Medikah"
        tagline = "Cuidado Sin Distancia.<br/>Coordinacion medica unida a traves de las Americas."
    else:
        subject_line = "Welcome to the Medikah physician network"
        greeting = f"Dear Dr. {name},"
        intro = (
            "Welcome to the Medikah physician network. Your profile has been "
            "received and is currently under review."
        )
        status_title = "Profile status"
        status_text = "Under review"
        status_detail = (
            "Our verification team is reviewing your credentials. "
            "This process typically takes 1-3 business days."
        )
        next_steps_title = "What to expect"
        next_steps = [
            "We will verify your medical license and credentials",
            "You will receive a notification when your profile is approved",
            "Once verified, patients will be able to find you on our platform",
        ]
        cta_text = "Access Your Dashboard"
        support_text = (
            "If you have any questions, don't hesitate to reach out at "
            '<a href="mailto:hello@medikah.health" style="color: #2C7A8C; '
            'text-decoration: none; font-weight: 600;">hello@medikah.health</a>'
        )
        sign_off = "Warmly,"
        team_name = "The Medikah Team"
        tagline = (
            "Care Without Distance.<br/>Healthcare coordination across the Americas."
        )

    next_steps_html = "".join(
        f'<li style="margin-bottom: 8px;">{step}</li>' for step in next_steps
    )

    head = email_head()
    header = email_header("linen", locale, "medikah")
    footer = email_footer(locale)
    return f"""\
<!DOCTYPE html>
<html lang="{locale}">
{head}
<body style="margin:0;padding:0;background-color:{_PAGE_BG};font-family:{_F['body']};color:{_C['bodySlate']};">
{header}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_PAGE_BG};padding:40px 20px;">
  <tr>
    <td align="center">
      <table role="presentation" class="email-container" width="600" cellpadding="0" cellspacing="0" style="background-color:{_C['white']};border-radius:{_R['md']};overflow:hidden;">
        <tr>
          <td class="email-pad" style="padding:40px 48px 0 48px;">
            <p style="font-family:{_F['ui']};font-size:13px;color:{_C['clinicalTeal']};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 16px 0;">{subject_line}</p>
            <p style="font-family:{_F['body']};font-size:20px;font-weight:600;line-height:1.4;color:{_C['deepCharcoal']};margin:0 0 24px 0;">{greeting}</p>
            <p style="font-family:{_F['ui']};font-size:16px;line-height:1.7;color:{_C['bodySlate']};margin:0 0 28px 0;">
              {intro}
            </p>
          </td>
        </tr>

        <tr>
          <td class="email-pad" style="padding:0 48px 28px 48px;">
            <div style="background-color:{_C['linen']};border-left:4px solid {_C['instBlue']};padding:24px;border-radius:{_R['sm']};">
              <table role="presentation" style="width:100%;border-collapse:collapse;">
                <tr>
                  <td style="font-family:{_F['ui']};padding:10px 0;color:{_C['bodySlate']};font-size:12px;text-transform:uppercase;letter-spacing:0.05em;width:140px;font-weight:600;">{status_title}</td>
                  <td style="font-family:{_F['ui']};padding:10px 0;color:{_C['clinicalTeal']};font-size:16px;font-weight:700;">{status_text}</td>
                </tr>
              </table>
              <p style="font-family:{_F['ui']};font-size:14px;line-height:1.6;color:{_C['bodySlate']};margin:12px 0 0 0;">
                {status_detail}
              </p>
            </div>
          </td>
        </tr>

        <tr>
          <td class="email-pad" style="padding:0 48px 28px 48px;">
            <p style="font-family:{_F['body']};font-size:14px;font-weight:700;color:{_C['instBlue']};margin:0 0 12px 0;">{next_steps_title}</p>
            <ol style="font-family:{_F['ui']};font-size:14px;line-height:1.8;color:{_C['bodySlate']};padding-left:20px;margin:0;">
              {next_steps_html}
            </ol>
          </td>
        </tr>

        <tr>
          <td class="email-pad" style="padding:0 48px 28px 48px;text-align:center;">
            <a href="{dashboard_url}" style="display:inline-block;background-color:{_C['clinicalTeal']};color:{_C['white']};font-family:{_F['ui']};text-decoration:none;padding:16px 40px;border-radius:{_R['sm']};font-size:16px;font-weight:700;letter-spacing:0.02em;">{cta_text}</a>
          </td>
        </tr>

        <tr>
          <td class="email-pad" style="padding:0 48px 32px 48px;">
            <p style="font-family:{_F['ui']};font-size:14px;line-height:1.6;color:{_C['bodySlate']};margin:0 0 24px 0;">
              {support_text}
            </p>
            <div style="margin-top:8px;padding-top:24px;border-top:1px solid {_C['borderLine']};">
              <p style="font-family:{_F['ui']};font-size:15px;color:{_C['bodySlate']};line-height:1.6;font-style:italic;margin:0 0 8px 0;">
                {tagline}
              </p>
              <p style="font-family:{_F['ui']};font-size:14px;color:{_C['bodySlate']};margin:0 0 4px 0;">{sign_off}</p>
              <p style="font-family:{_F['body']};font-size:16px;font-weight:700;color:{_C['instBlue']};margin:0;">{team_name}</p>
            </div>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
{footer}
</body>
</html>"""


def _build_welcome_plain(physician_data: dict, locale: str = "en") -> str:
    """Build the plain text email body for physician welcome email."""
    name = physician_data.get("full_name", "Doctor")
    dashboard_url = f"{_BASE_URL}/physicians/dashboard"

    if locale == "es":
        return (
            f"Estimado/a Dr. {name},\n\n"
            "Nos complace darle la bienvenida a la red de medicos de Medikah. "
            "Su perfil ha sido recibido y esta actualmente bajo revision.\n\n"
            "Estado de su perfil: En revision\n"
            "Nuestro equipo de verificacion esta revisando sus credenciales. "
            "Este proceso generalmente toma de 1 a 3 dias habiles.\n\n"
            "Proximos pasos:\n"
            "1. Revisaremos su licencia medica y credenciales\n"
            "2. Recibira una notificacion cuando su perfil sea aprobado\n"
            "3. Una vez verificado, los pacientes podran encontrarlo en nuestra plataforma\n\n"
            f"Acceda a su panel: {dashboard_url}\n\n"
            "Si tiene preguntas, contactenos en hello@medikah.health\n\n"
            "Cordialmente,\n"
            "El equipo de Medikah\n"
        )

    return (
        f"Dear Dr. {name},\n\n"
        "Welcome to the Medikah physician network. Your profile has been "
        "received and is currently under review.\n\n"
        "Profile status: Under review\n"
        "Our verification team is reviewing your credentials. "
        "This process typically takes 1-3 business days.\n\n"
        "What to expect:\n"
        "1. We will verify your medical license and credentials\n"
        "2. You will receive a notification when your profile is approved\n"
        "3. Once verified, patients will be able to find you on our platform\n\n"
        f"Access your dashboard: {dashboard_url}\n\n"
        "If you have any questions, reach out at hello@medikah.health\n\n"
        "Warmly,\n"
        "The Medikah Team\n"
    )


async def send_inquiry_accepted_email(
    patient_email: str,
    patient_name: str,
    physician_name: str,
    notification_service: NotificationService,
    locale: str = "en",
) -> None:
    """Send a notification to the patient that their inquiry was accepted.

    Args:
        patient_email: Patient's email address.
        patient_name: Patient's name.
        physician_name: Physician's display name.
        notification_service: The configured NotificationService instance.
        locale: 'en' or 'es' for bilingual support.
    """
    if not patient_email:
        logger.error("Cannot send accepted email: no patient email provided")
        return

    dashboard_url = f"{_BASE_URL}/patients"

    if locale == "es":
        subject = f"Su consulta con Dr. {physician_name} ha sido aceptada"
        plain_body = (
            f"Estimado/a {patient_name},\n\n"
            f"Buenas noticias: Dr. {physician_name} ha aceptado su solicitud de consulta "
            "a traves de Medikah.\n\n"
            "Proximos pasos:\n"
            "- Recibira informacion adicional sobre como agendar su cita\n"
            "- Puede acceder a su portal de paciente para mas detalles\n\n"
            f"Acceder a su portal: {dashboard_url}\n\n"
            "Si tiene preguntas, contactenos en hello@medikah.health\n\n"
            "Cordialmente,\n"
            "El equipo de Medikah\n"
        )
        header_text = "Consulta Aceptada"
        greeting = f"Estimado/a {patient_name},"
        intro = (
            f"Buenas noticias: <strong>Dr. {physician_name}</strong> ha aceptado "
            "su solicitud de consulta a traves de Medikah."
        )
        next_title = "Proximos pasos"
        next_items = [
            "Recibira informacion adicional sobre como agendar su cita",
            "Puede acceder a su portal de paciente para mas detalles",
        ]
        cta_text = "Acceder a su portal"
        sign_off = "Cordialmente,"
        team_name = "El equipo de Medikah"
    else:
        subject = f"Your consultation with Dr. {physician_name} has been accepted"
        plain_body = (
            f"Dear {patient_name},\n\n"
            f"Great news: Dr. {physician_name} has accepted your consultation request "
            "through Medikah.\n\n"
            "Next steps:\n"
            "- You will receive additional information about scheduling your appointment\n"
            "- You can access your patient portal for more details\n\n"
            f"Access your portal: {dashboard_url}\n\n"
            "If you have questions, reach out at hello@medikah.health\n\n"
            "Warmly,\n"
            "The Medikah Team\n"
        )
        header_text = "Consultation Accepted"
        greeting = f"Dear {patient_name},"
        intro = (
            f"Great news: <strong>Dr. {physician_name}</strong> has accepted your "
            "consultation request through Medikah."
        )
        next_title = "Next steps"
        next_items = [
            "You will receive additional information about scheduling your appointment",
            "You can access your patient portal for more details",
        ]
        cta_text = "Access Your Portal"
        sign_off = "Warmly,"
        team_name = "The Medikah Team"

    next_items_html = "".join(
        f'<li style="margin-bottom: 8px;">{item}</li>' for item in next_items
    )

    head = email_head()
    header = email_header("linen", locale, "medikah")
    footer = email_footer(locale)
    html_body = f"""\
<!DOCTYPE html>
<html lang="{locale}">
{head}
<body style="margin:0;padding:0;background-color:{_PAGE_BG};font-family:{_F['body']};color:{_C['bodySlate']};">
{header}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_PAGE_BG};padding:40px 20px;">
  <tr><td align="center">
    <table role="presentation" class="email-container" width="600" cellpadding="0" cellspacing="0" style="background-color:{_C['white']};border-radius:{_R['md']};overflow:hidden;">
      <tr>
        <td class="email-pad" style="padding:40px 48px 0 48px;">
          <p style="font-family:{_F['ui']};font-size:13px;color:{_C['clinicalTeal']};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 16px 0;">{header_text}</p>
          <p style="font-family:{_F['body']};font-size:20px;font-weight:600;color:{_C['deepCharcoal']};margin:0 0 24px 0;">{greeting}</p>
          <p style="font-family:{_F['ui']};font-size:16px;line-height:1.7;color:{_C['bodySlate']};margin:0 0 28px 0;">{intro}</p>
        </td>
      </tr>
      <tr>
        <td class="email-pad" style="padding:0 48px 28px 48px;">
          <p style="font-family:{_F['body']};font-size:14px;font-weight:700;color:{_C['instBlue']};margin:0 0 12px 0;">{next_title}</p>
          <ul style="font-family:{_F['ui']};font-size:14px;line-height:1.8;color:{_C['bodySlate']};padding-left:20px;margin:0;">{next_items_html}</ul>
        </td>
      </tr>
      <tr>
        <td class="email-pad" style="padding:0 48px 28px 48px;text-align:center;">
          <a href="{dashboard_url}" style="display:inline-block;background-color:{_C['clinicalTeal']};color:{_C['white']};font-family:{_F['ui']};text-decoration:none;padding:16px 40px;border-radius:{_R['sm']};font-size:16px;font-weight:700;">{cta_text}</a>
        </td>
      </tr>
      <tr>
        <td class="email-pad" style="padding:0 48px 32px 48px;border-top:1px solid {_C['borderLine']};">
          <p style="font-family:{_F['ui']};font-size:14px;color:{_C['bodySlate']};margin:24px 0 4px 0;">{sign_off}</p>
          <p style="font-family:{_F['body']};font-size:16px;font-weight:700;color:{_C['instBlue']};margin:0;">{team_name}</p>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
{footer}
</body>
</html>"""

    message = NotificationMessage(
        recipient=patient_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
    )

    try:
        await notification_service.send_bulk([message])
        logger.info("Inquiry accepted email sent to %s (locale=%s)", patient_email, locale)
    except Exception:
        logger.exception("Failed to send inquiry accepted email to %s", patient_email)
        raise


async def send_inquiry_declined_email(
    patient_email: str,
    patient_name: str,
    physician_name: str,
    notification_service: NotificationService,
    reason: Optional[str] = None,
    locale: str = "en",
) -> None:
    """Send a notification to the patient that their inquiry was declined.

    Args:
        patient_email: Patient's email address.
        patient_name: Patient's name.
        physician_name: Physician's display name.
        notification_service: The configured NotificationService instance.
        reason: Optional reason for declining.
        locale: 'en' or 'es' for bilingual support.
    """
    if not patient_email:
        logger.error("Cannot send declined email: no patient email provided")
        return

    dashboard_url = f"{_BASE_URL}/patients"

    if locale == "es":
        subject = "Actualizacion sobre su solicitud de consulta en Medikah"
        reason_text = f"\nMotivo: {reason}\n" if reason else ""
        plain_body = (
            f"Estimado/a {patient_name},\n\n"
            f"Lamentamos informarle que Dr. {physician_name} no puede atender su solicitud "
            "de consulta en este momento.\n"
            f"{reason_text}\n"
            "Esto no significa que no pueda recibir atencion. Le recomendamos:\n"
            "- Buscar otro medico disponible en nuestra plataforma\n"
            "- Contactar a nuestro equipo de cuidado para ayudarle a encontrar un especialista\n\n"
            f"Acceder a su portal: {dashboard_url}\n\n"
            "Si tiene preguntas, contactenos en hello@medikah.health\n\n"
            "Cordialmente,\n"
            "El equipo de Medikah\n"
        )
        header_text = "Actualizacion de Consulta"
        greeting = f"Estimado/a {patient_name},"
        intro = (
            f"Lamentamos informarle que <strong>Dr. {physician_name}</strong> no puede "
            "atender su solicitud de consulta en este momento."
        )
        reason_html = (
            f'<div style="background-color:{_C["linen"]};border-left:4px solid {_C["error"]};padding:16px;margin:0 0 24px 0;border-radius:{_R["sm"]};">'
            f'<p style="font-size: 14px; color: #4A5568; margin: 0;"><strong>Motivo:</strong> {reason}</p></div>'
            if reason else ""
        )
        next_title = "Le recomendamos"
        next_items = [
            "Buscar otro medico disponible en nuestra plataforma",
            "Contactar a nuestro equipo de cuidado para ayudarle a encontrar un especialista",
        ]
        cta_text = "Buscar otro medico"
        sign_off = "Cordialmente,"
        team_name = "El equipo de Medikah"
    else:
        subject = "Update on your Medikah consultation request"
        reason_text = f"\nReason: {reason}\n" if reason else ""
        plain_body = (
            f"Dear {patient_name},\n\n"
            f"We regret to inform you that Dr. {physician_name} is unable to take your "
            "consultation request at this time.\n"
            f"{reason_text}\n"
            "This doesn't mean you can't receive care. We recommend:\n"
            "- Searching for another available physician on our platform\n"
            "- Contacting our care team to help you find a specialist\n\n"
            f"Access your portal: {dashboard_url}\n\n"
            "If you have questions, reach out at hello@medikah.health\n\n"
            "Warmly,\n"
            "The Medikah Team\n"
        )
        header_text = "Consultation Update"
        greeting = f"Dear {patient_name},"
        intro = (
            f"We regret to inform you that <strong>Dr. {physician_name}</strong> is "
            "unable to take your consultation request at this time."
        )
        reason_html = (
            f'<div style="background-color:{_C["linen"]};border-left:4px solid {_C["error"]};padding:16px;margin:0 0 24px 0;border-radius:{_R["sm"]};">'
            f'<p style="font-size: 14px; color: #4A5568; margin: 0;"><strong>Reason:</strong> {reason}</p></div>'
            if reason else ""
        )
        next_title = "We recommend"
        next_items = [
            "Searching for another available physician on our platform",
            "Contacting our care team to help you find a specialist",
        ]
        cta_text = "Find Another Physician"
        sign_off = "Warmly,"
        team_name = "The Medikah Team"

    next_items_html = "".join(
        f'<li style="margin-bottom: 8px;">{item}</li>' for item in next_items
    )

    head = email_head()
    header = email_header("linen", locale, "medikah")
    footer = email_footer(locale)
    html_body = f"""\
<!DOCTYPE html>
<html lang="{locale}">
{head}
<body style="margin:0;padding:0;background-color:{_PAGE_BG};font-family:{_F['body']};color:{_C['bodySlate']};">
{header}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_PAGE_BG};padding:40px 20px;">
  <tr><td align="center">
    <table role="presentation" class="email-container" width="600" cellpadding="0" cellspacing="0" style="background-color:{_C['white']};border-radius:{_R['md']};overflow:hidden;">
      <tr>
        <td class="email-pad" style="padding:40px 48px 0 48px;">
          <p style="font-family:{_F['ui']};font-size:13px;color:{_C['clinicalTeal']};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 16px 0;">{header_text}</p>
          <p style="font-family:{_F['body']};font-size:20px;font-weight:600;color:{_C['deepCharcoal']};margin:0 0 24px 0;">{greeting}</p>
          <p style="font-family:{_F['ui']};font-size:16px;line-height:1.7;color:{_C['bodySlate']};margin:0 0 24px 0;">{intro}</p>
          {reason_html}
        </td>
      </tr>
      <tr>
        <td class="email-pad" style="padding:0 48px 28px 48px;">
          <p style="font-family:{_F['body']};font-size:14px;font-weight:700;color:{_C['instBlue']};margin:0 0 12px 0;">{next_title}</p>
          <ul style="font-family:{_F['ui']};font-size:14px;line-height:1.8;color:{_C['bodySlate']};padding-left:20px;margin:0;">{next_items_html}</ul>
        </td>
      </tr>
      <tr>
        <td class="email-pad" style="padding:0 48px 28px 48px;text-align:center;">
          <a href="{dashboard_url}" style="display:inline-block;background-color:{_C['clinicalTeal']};color:{_C['white']};font-family:{_F['ui']};text-decoration:none;padding:16px 40px;border-radius:{_R['sm']};font-size:16px;font-weight:700;">{cta_text}</a>
        </td>
      </tr>
      <tr>
        <td class="email-pad" style="padding:0 48px 32px 48px;border-top:1px solid {_C['borderLine']};">
          <p style="font-family:{_F['ui']};font-size:14px;color:{_C['bodySlate']};margin:24px 0 4px 0;">{sign_off}</p>
          <p style="font-family:{_F['body']};font-size:16px;font-weight:700;color:{_C['instBlue']};margin:0;">{team_name}</p>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
{footer}
</body>
</html>"""

    message = NotificationMessage(
        recipient=patient_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
    )

    try:
        await notification_service.send_bulk([message])
        logger.info("Inquiry declined email sent to %s (locale=%s)", patient_email, locale)
    except Exception:
        logger.exception("Failed to send inquiry declined email to %s", patient_email)
        raise


async def send_physician_welcome_email(
    physician_data: dict,
    notification_service: NotificationService,
    locale: str = "en",
) -> None:
    """Send a welcome email to a newly onboarded physician.

    Args:
        physician_data: Dict with at least 'full_name' and 'email' keys.
        notification_service: The configured NotificationService instance.
        locale: 'en' or 'es' for bilingual support.
    """
    email = physician_data.get("email")
    name = physician_data.get("full_name", "Doctor")

    if not email:
        logger.error("Cannot send welcome email: no email address provided")
        return

    if locale == "es":
        subject = f"Bienvenido/a a Medikah, Dr. {name}"
    else:
        subject = f"Welcome to Medikah, Dr. {name}"

    html_body = _build_welcome_html(physician_data, locale)
    plain_body = _build_welcome_plain(physician_data, locale)

    message = NotificationMessage(
        recipient=email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
    )

    try:
        await notification_service.send_bulk([message])
        logger.info("Physician welcome email sent to %s (locale=%s)", email, locale)
    except Exception:
        logger.exception("Failed to send physician welcome email to %s", email)
        raise
