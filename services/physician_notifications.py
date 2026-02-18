"""Physician notification service for welcome and onboarding emails."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from services.notifications import NotificationMessage, NotificationService

logger = logging.getLogger(__name__)

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
        tagline = "Coordinacion medica que cruza fronteras.<br/>El cuidado, nunca."
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
            "Healthcare coordination that crosses borders.<br/>Care that never does."
        )

    next_steps_html = "".join(
        f'<li style="margin-bottom: 8px;">{step}</li>' for step in next_steps
    )

    return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
</head>
<body style="margin: 0; padding: 0; background-color: #FAFAFB;">
<div style="font-family: 'Mulish', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; background-color: #FFFFFF; border-radius: 12px; overflow: hidden;">
  <!-- Header -->
  <div style="background-color: #FFFFFF; padding: 0; text-align: center; border-bottom: 4px solid #1B2A41;">
    <div style="padding: 32px 48px;">
      <p style="font-family: 'Mulish', -apple-system, BlinkMacSystemFont, sans-serif; font-size: 32px; font-weight: 800; color: #1B2A41; letter-spacing: -0.01em; margin: 0;">medikah</p>
      <p style="font-size: 13px; color: #2C7A8C; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; margin: 12px 0 0 0;">{subject_line}</p>
    </div>
  </div>

  <div style="padding: 48px; background: #FFFFFF;">
    <p style="font-size: 20px; font-weight: 600; line-height: 1.4; color: #1B2A41; margin: 0 0 24px 0;">{greeting}</p>

    <p style="font-size: 16px; line-height: 1.7; color: #4A5568; margin: 0 0 28px 0;">
      {intro}
    </p>

    <!-- Status box -->
    <div style="background: linear-gradient(135deg, #F8FAFB 0%, #F0F4F5 100%); border-left: 4px solid #1B2A41; padding: 24px; margin: 0 0 28px 0; border-radius: 0 8px 8px 0;">
      <table style="width: 100%; border-collapse: collapse;">
        <tr>
          <td style="padding: 10px 0; color: #6B7280; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; width: 140px; font-weight: 600;">{status_title}</td>
          <td style="padding: 10px 0; color: #2C7A8C; font-size: 16px; font-weight: 700;">{status_text}</td>
        </tr>
      </table>
      <p style="font-size: 14px; line-height: 1.6; color: #4A5568; margin: 12px 0 0 0;">
        {status_detail}
      </p>
    </div>

    <!-- Next steps -->
    <div style="margin: 0 0 28px 0;">
      <p style="font-size: 14px; font-weight: 700; color: #1B2A41; margin: 0 0 12px 0;">{next_steps_title}</p>
      <ol style="font-size: 14px; line-height: 1.8; color: #4A5568; padding-left: 20px; margin: 0;">
        {next_steps_html}
      </ol>
    </div>

    <!-- CTA button -->
    <div style="text-align: center; margin: 0 0 28px 0;">
      <a href="{dashboard_url}" style="display: inline-block; background: #2C7A8C; color: #FFFFFF; text-decoration: none; padding: 18px 40px; border-radius: 8px; font-size: 16px; font-weight: 700; letter-spacing: 0.02em; box-shadow: 0 4px 12px rgba(44,122,140,0.25);">{cta_text}</a>
    </div>

    <p style="font-size: 14px; line-height: 1.6; color: #4A5568; margin: 0 0 24px 0;">
      {support_text}
    </p>

    <div style="margin-top: 32px; padding-top: 24px; border-top: 2px solid #F0F4F5;">
      <p style="font-size: 15px; color: #6B7280; line-height: 1.6; font-style: italic; margin: 0 0 8px 0;">
        {tagline}
      </p>
      <p style="font-size: 14px; color: #4A5568; margin: 0 0 4px 0;">{sign_off}</p>
      <p style="font-size: 16px; font-weight: 700; color: #1B2A41; margin: 0;">{team_name}</p>
    </div>
  </div>

  <!-- Footer -->
  <div style="background-color: #F5F7F8; padding: 28px 48px; text-align: center; border-top: 4px solid #1B2A41;">
    <p style="font-size: 12px; line-height: 1.6; color: #6B7280; margin: 0 0 12px 0;">
      Your information is handled with care and stored securely.<br/>
      Our team reviews credentials to ensure safe, quality care.
    </p>
    <p style="font-size: 12px; line-height: 1.6; color: #9CA3AF; margin: 0 0 16px 0;">
      Medikah Corporation &middot; Incorporated in Delaware, USA
    </p>
    <p style="font-size: 12px; margin: 0;">
      <a href="https://medikah.health/privacy" style="color: #1B2A41; text-decoration: none; font-weight: 600;">Privacy Policy</a>
      <span style="color: #D1D5DB; margin: 0 8px;">|</span>
      <a href="https://medikah.health/terms" style="color: #1B2A41; text-decoration: none; font-weight: 600;">Terms of Service</a>
      <span style="color: #D1D5DB; margin: 0 8px;">|</span>
      <a href="mailto:hello@medikah.health" style="color: #1B2A41; text-decoration: none; font-weight: 600;">Contact</a>
    </p>
  </div>
</div>
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

    html_body = f"""\
<!DOCTYPE html>
<html>
<head><meta name="color-scheme" content="light"></head>
<body style="margin: 0; padding: 0; background-color: #FAFAFB;">
<div style="font-family: 'Mulish', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; background-color: #FFFFFF; border-radius: 12px; overflow: hidden;">
  <div style="background-color: #FFFFFF; text-align: center; border-bottom: 4px solid #1B2A41;">
    <div style="padding: 32px 48px;">
      <p style="font-size: 32px; font-weight: 800; color: #1B2A41; margin: 0;">medikah</p>
      <p style="font-size: 13px; color: #2C7A8C; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; margin: 12px 0 0 0;">{header_text}</p>
    </div>
  </div>
  <div style="padding: 48px; background: #FFFFFF;">
    <p style="font-size: 20px; font-weight: 600; color: #1B2A41; margin: 0 0 24px 0;">{greeting}</p>
    <p style="font-size: 16px; line-height: 1.7; color: #4A5568; margin: 0 0 28px 0;">{intro}</p>
    <div style="margin: 0 0 28px 0;">
      <p style="font-size: 14px; font-weight: 700; color: #1B2A41; margin: 0 0 12px 0;">{next_title}</p>
      <ul style="font-size: 14px; line-height: 1.8; color: #4A5568; padding-left: 20px; margin: 0;">{next_items_html}</ul>
    </div>
    <div style="text-align: center; margin: 0 0 28px 0;">
      <a href="{dashboard_url}" style="display: inline-block; background: #2C7A8C; color: #FFFFFF; text-decoration: none; padding: 18px 40px; border-radius: 8px; font-size: 16px; font-weight: 700;">{cta_text}</a>
    </div>
    <div style="margin-top: 32px; padding-top: 24px; border-top: 2px solid #F0F4F5;">
      <p style="font-size: 14px; color: #4A5568; margin: 0 0 4px 0;">{sign_off}</p>
      <p style="font-size: 16px; font-weight: 700; color: #1B2A41; margin: 0;">{team_name}</p>
    </div>
  </div>
  <div style="background-color: #F5F7F8; padding: 28px 48px; text-align: center; border-top: 4px solid #1B2A41;">
    <p style="font-size: 12px; color: #9CA3AF; margin: 0;">Medikah Corporation &middot; Incorporated in Delaware, USA</p>
  </div>
</div>
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
            f'<div style="background: #FEF3F2; border-left: 4px solid #B83D3D; padding: 16px; margin: 0 0 24px 0; border-radius: 0 8px 8px 0;">'
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
            f'<div style="background: #FEF3F2; border-left: 4px solid #B83D3D; padding: 16px; margin: 0 0 24px 0; border-radius: 0 8px 8px 0;">'
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

    html_body = f"""\
<!DOCTYPE html>
<html>
<head><meta name="color-scheme" content="light"></head>
<body style="margin: 0; padding: 0; background-color: #FAFAFB;">
<div style="font-family: 'Mulish', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; background-color: #FFFFFF; border-radius: 12px; overflow: hidden;">
  <div style="background-color: #FFFFFF; text-align: center; border-bottom: 4px solid #1B2A41;">
    <div style="padding: 32px 48px;">
      <p style="font-size: 32px; font-weight: 800; color: #1B2A41; margin: 0;">medikah</p>
      <p style="font-size: 13px; color: #2C7A8C; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; margin: 12px 0 0 0;">{header_text}</p>
    </div>
  </div>
  <div style="padding: 48px; background: #FFFFFF;">
    <p style="font-size: 20px; font-weight: 600; color: #1B2A41; margin: 0 0 24px 0;">{greeting}</p>
    <p style="font-size: 16px; line-height: 1.7; color: #4A5568; margin: 0 0 24px 0;">{intro}</p>
    {reason_html}
    <div style="margin: 0 0 28px 0;">
      <p style="font-size: 14px; font-weight: 700; color: #1B2A41; margin: 0 0 12px 0;">{next_title}</p>
      <ul style="font-size: 14px; line-height: 1.8; color: #4A5568; padding-left: 20px; margin: 0;">{next_items_html}</ul>
    </div>
    <div style="text-align: center; margin: 0 0 28px 0;">
      <a href="{dashboard_url}" style="display: inline-block; background: #2C7A8C; color: #FFFFFF; text-decoration: none; padding: 18px 40px; border-radius: 8px; font-size: 16px; font-weight: 700;">{cta_text}</a>
    </div>
    <div style="margin-top: 32px; padding-top: 24px; border-top: 2px solid #F0F4F5;">
      <p style="font-size: 14px; color: #4A5568; margin: 0 0 4px 0;">{sign_off}</p>
      <p style="font-size: 16px; font-weight: 700; color: #1B2A41; margin: 0;">{team_name}</p>
    </div>
  </div>
  <div style="background-color: #F5F7F8; padding: 28px 48px; text-align: center; border-top: 4px solid #1B2A41;">
    <p style="font-size: 12px; color: #9CA3AF; margin: 0;">Medikah Corporation &middot; Incorporated in Delaware, USA</p>
  </div>
</div>
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
