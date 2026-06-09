from __future__ import annotations

import logging
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from services import email_chrome
from services.appointments import SecureAppointmentStore
from services.conversation_state import (
    ConversationStage,
    ConversationState,
    ConversationStateStore,
    IntakeHistory,
)
from services.ai_triage import AITriageResponseGenerator, TriagePromptBuilder
from services.notifications import EmailAttachment, NotificationMessage, NotificationService
from services.triage import TriageAction, TriageConversationEngine
from utils.scheduling import (
    build_google_calendar_link,
    build_ics_content,
    generate_doxy_link,
)
from routes.physician_routes import router as physician_router
from routes.ai_routes import router as ai_router
from routes.practikah_routes import router as practikah_router
from utils.openai_client import get_openai_client
from db.client import is_production

load_dotenv()

# NEXTAUTH_SECRET — shared HS256 signing key with the Next.js NextAuth config.
# Required in production so utils/auth.py can verify physician JWTs against
# the same secret the frontend uses to issue them. In development, a missing
# secret only logs a warning so local-only flows that don't hit auth-gated
# routes can still run.
NEXTAUTH_SECRET = os.getenv("NEXTAUTH_SECRET")
if NEXTAUTH_SECRET is None:
    if is_production():
        raise RuntimeError(
            "NEXTAUTH_SECRET is required in production. "
            "Set it in the Render dashboard. Same value as the frontend NextAuth signing secret."
        )
    logging.warning("NEXTAUTH_SECRET not set; auth dependency will reject all requests.")

limiter = Limiter(key_func=get_remote_address)
# Disable interactive API docs (/docs, /redoc, /openapi.json) in production —
# they advertise the full endpoint surface (provisioning, billing, webhooks).
_DOCS_ENABLED = not is_production()
app = FastAPI(
    title="Medikah Chat API",
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again shortly."},
    )

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(physician_router)
app.include_router(ai_router)
app.include_router(practikah_router)


@app.on_event("startup")
async def _resume_orphan_provisioning_runs():
    """On startup: detect abandoned provisioning runs and roll them back.

    Per Phase 11 D-09: the log is the source of truth. If the FastAPI process
    died mid-provision, the log preserves what completed; this hook resumes rollback.
    Failure is non-fatal — logged and swallowed so FastAPI continues starting up.
    """
    from services.practikah.orchestrator import resume_orphan_runs
    try:
        cleaned = await resume_orphan_runs()
        if cleaned:
            logger.info("[practikah] resumed %d orphan provisioning runs on startup", cleaned)
    except Exception:
        logger.exception("[practikah] resume_orphan_runs failed; continuing")

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")
logging.info(f"Using LOG_LEVEL={log_level_str}")
logger = logging.getLogger("medikah.api")

openai_client = get_openai_client()


class ChatRequest(BaseModel):
    """Schema for a chat request."""

    message: str
    session_id: Optional[str] = Field(default=None, max_length=120)
    locale: Optional[str] = Field(default=None, max_length=16)
    timezone: Optional[str] = Field(default=None, max_length=64)
    patient_name: Optional[str] = Field(default=None, max_length=255)
    patient_email: Optional[EmailStr] = None


class ChatResponse(BaseModel):
    """Schema for a chat response."""

    reply: str
    session_id: str
    stage: ConversationStage
    actions: List[dict[str, str]] = Field(default_factory=list)
    appointment_confirmed: bool = False
    emergency_noted: bool = False
    response: Optional[str] = None


class ScheduleRequest(BaseModel):
    """Schema for scheduling a telehealth appointment."""

    model_config = ConfigDict(str_strip_whitespace=True)

    patient_name: str = Field(..., min_length=1, max_length=255)
    patient_contact: EmailStr
    appointment_time: datetime
    symptoms: Optional[str] = Field(default=None, max_length=2000)
    locale_preference: Optional[str] = Field(default=None, max_length=120)
    patient_timezone: Optional[str] = Field(default=None, max_length=64)


class ScheduleResponse(BaseModel):
    """Response payload for the scheduling endpoint."""

    appointment_id: str
    doxy_link: str
    calendar_link: str
    message: str


class SandboxScheduleResponse(BaseModel):
    """Response payload when sandbox mode skips notifications."""

    status: str
    doxy: str
    calendar: str
    note: str


def _resolve_duration_minutes() -> int:
    raw_value = os.getenv("APPOINTMENT_DURATION_MINUTES", "30")
    try:
        duration = int(raw_value)
        if duration <= 0:
            raise ValueError
        return duration
    except ValueError:
        logger.warning(
            "Invalid APPOINTMENT_DURATION_MINUTES=%s; defaulting to 30", raw_value
        )
        return 30


DOXY_BASE_URL = os.getenv("DOXY_BASE_URL")
DOXY_ROOM_URL = os.getenv("DOXY_ROOM_URL")
DOCTOR_NOTIFICATION_EMAIL = os.getenv("DOCTOR_NOTIFICATION_EMAIL")
ON_CALL_DOCTOR_NAME = os.getenv("ON_CALL_DOCTOR_NAME", "Medikah On-Call Doctor")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_SENDER_EMAIL = os.getenv("RESEND_SENDER_EMAIL", "Medikah <onboarding@resend.dev>")
APPOINTMENT_HASH_KEY = os.getenv("APPOINTMENT_HASH_KEY")

missing_envs = [
    name
    for name, value in (
        ("RESEND_API_KEY", RESEND_API_KEY),
        ("APPOINTMENT_HASH_KEY", APPOINTMENT_HASH_KEY),
    )
    if not value
]
if missing_envs:
    logging.error(
        "Missing required notification configuration: %s",
        ", ".join(missing_envs),
    )
    raise RuntimeError("Notification service is not configured")

EMAIL_SANDBOX_MODE_RAW = os.getenv("EMAIL_SANDBOX_MODE", "false").lower()
EMAIL_SANDBOX_MODE = EMAIL_SANDBOX_MODE_RAW in {"1", "true", "yes", "on"}
APPOINTMENT_DURATION_MINUTES = _resolve_duration_minutes()

appointment_store: Optional[SecureAppointmentStore] = SecureAppointmentStore(
    APPOINTMENT_HASH_KEY
)
notification_service: NotificationService = NotificationService(
    RESEND_API_KEY,
    RESEND_SENDER_EMAIL,
    sandbox_mode=EMAIL_SANDBOX_MODE,
)

conversation_store = ConversationStateStore()

# AI response generation (graceful: None if OpenAI unavailable)
ai_responder = None
if openai_client:
    _prompt_builder = TriagePromptBuilder(
        on_call_doctor_name=ON_CALL_DOCTOR_NAME,
        doxy_room_url=DOXY_ROOM_URL or (DOXY_BASE_URL or ""),
    )
    ai_responder = AITriageResponseGenerator(
        openai_client=openai_client,
        prompt_builder=_prompt_builder,
    )
    logger.info("AI triage response generation enabled.")
else:
    logger.warning("AI triage disabled; using hardcoded responses.")

triage_engine = TriageConversationEngine(
    conversation_store,
    on_call_doctor_name=ON_CALL_DOCTOR_NAME,
    doxy_room_url=DOXY_ROOM_URL or (DOXY_BASE_URL or ""),
    ai_responder=ai_responder,
)


@dataclass(slots=True)
class SchedulingOutcome:
    response: Union[ScheduleResponse, SandboxScheduleResponse]
    appointment_id: str
    doxy_link: str
    calendar_link: str


async def _perform_scheduling(
    req: ScheduleRequest,
    *,
    sandbox_mode: bool,
    intake_notes: Optional[str] = None,
) -> SchedulingOutcome:
    if appointment_store is None or (not DOXY_BASE_URL and not DOXY_ROOM_URL):
        logger.error("Schedule attempted without proper storage or Doxy configuration.")
        raise HTTPException(
            status_code=503, detail="Scheduling service is not configured."
        )

    if not DOCTOR_NOTIFICATION_EMAIL:
        logger.error("Doctor notification email not configured.")
        raise HTTPException(
            status_code=503, detail="Notification service is not configured."
        )

    appointment_time = req.appointment_time
    if (
        appointment_time.tzinfo is None
        or appointment_time.tzinfo.utcoffset(appointment_time) is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Appointment time must include timezone information.",
        )

    try:
        record = appointment_store.save(
            patient_name=req.patient_name,
            patient_contact=req.patient_contact,
            appointment_time=appointment_time,
        )
    except Exception:
        logger.exception("Failed to persist appointment for session")
        raise HTTPException(
            status_code=500, detail="Unable to store the appointment."
        )

    assigned_doctor = ON_CALL_DOCTOR_NAME

    if DOXY_ROOM_URL:
        doxy_link = DOXY_ROOM_URL
    else:
        doxy_link = generate_doxy_link(DOXY_BASE_URL, record.appointment_id)
    calendar_description = (
        f"Telehealth visit with {assigned_doctor} for {req.patient_name}. "
        f"Join via Medikah: {doxy_link}"
    )
    calendar_link = build_google_calendar_link(
        title="Medikah Telehealth Appointment",
        description=calendar_description,
        start=appointment_time,
        duration_minutes=APPOINTMENT_DURATION_MINUTES,
        location=doxy_link,
    )

    logger.info(
        "Assigned doctor %s to appointment %s",
        assigned_doctor,
        record.appointment_id,
    )

    # Format appointment time for display in patient's local timezone
    import base64
    from zoneinfo import ZoneInfo
    patient_tz = ZoneInfo("UTC")
    if req.patient_timezone:
        try:
            patient_tz = ZoneInfo(req.patient_timezone)
        except (KeyError, ValueError):
            pass
    local_time = appointment_time.astimezone(patient_tz)
    tz_abbr = local_time.strftime("%Z") or "UTC"
    time_display = local_time.strftime(f"%B %d, %Y at %I:%M %p {tz_abbr}")

    # Build ICS calendar attachment
    ics_content = build_ics_content(
        title=f"Medikah Visit with {assigned_doctor}",
        description=(
            f"Secure video consultation with {assigned_doctor}.\n"
            f"Join here: {doxy_link}"
        ),
        start=appointment_time,
        duration_minutes=APPOINTMENT_DURATION_MINUTES,
        location=doxy_link,
    )
    ics_attachment = EmailAttachment(
        filename="medikah-appointment.ics",
        content=base64.b64encode(ics_content.encode("utf-8")).decode("ascii"),
        content_type="text/calendar",
    )

    # Patient email — warm, professional, reassuring
    patient_plain_body = (
        f"Hi {req.patient_name},\n\n"
        f"Great news — your Medikah visit is confirmed!\n\n"
        f"Here are your appointment details:\n"
        f"  Doctor: {assigned_doctor}\n"
        f"  Date & Time: {time_display}\n"
        f"  How to join: {doxy_link}\n\n"
        "We've attached a calendar invite to this email so it's right on your "
        "schedule — just open it and it'll be added automatically.\n\n"
        "A few things to know before your visit:\n"
        "  - No downloads needed — just click the link above when it's time\n"
        "  - Find a quiet, private spot with good internet\n"
        "  - Have any medications or health records handy if possible\n\n"
        "If you need to reschedule or have any questions, simply reply to this email "
        "and our care team will be happy to help.\n\n"
        "We're looking forward to taking care of you.\n\n"
        "Warmly,\n"
        "The Medikah Care Team\n"
    )
    _ec_locale = (req.locale_preference or "en").lower()
    if _ec_locale not in ("en", "es"):
        _ec_locale = "en"
    _ec_head = email_chrome.email_head()
    _ec_header = email_chrome.email_header("linen", _ec_locale, "medikah")  # type: ignore[arg-type]
    _ec_footer = email_chrome.email_footer(_ec_locale)  # type: ignore[arg-type]
    _ec_C = email_chrome.TOKENS["colors"]
    _ec_F = email_chrome.TOKENS["fonts"]
    _ec_R = email_chrome.TOKENS["radii"]
    _ec_BG = email_chrome.TOKENS["pageBg"]
    patient_html_body = f"""\
<!DOCTYPE html>
<html lang="{_ec_locale}">
{_ec_head}
<body style="margin:0;padding:0;background-color:{_ec_BG};font-family:{_ec_F['body']};color:{_ec_C['bodySlate']};">
{_ec_header}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_ec_BG};padding:40px 20px;">
  <tr><td align="center">
    <table role="presentation" class="email-container" width="600" cellpadding="0" cellspacing="0" style="background-color:{_ec_C['white']};border-radius:{_ec_R['md']};overflow:hidden;">
      <tr>
        <td class="email-pad" style="padding:40px 48px 0 48px;">
          <p style="font-family:{_ec_F['ui']};font-size:13px;color:{_ec_C['clinicalTeal']};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 16px 0;">Visit Confirmed</p>
          <p style="font-family:{_ec_F['body']};font-size:20px;font-weight:600;line-height:1.4;color:{_ec_C['deepCharcoal']};margin:0 0 24px 0;">Hi {req.patient_name},</p>
          <p style="font-family:{_ec_F['ui']};font-size:16px;line-height:1.7;color:{_ec_C['bodySlate']};margin:0 0 24px 0;">
            Great news — your Medikah visit is confirmed. Here are your appointment details:
          </p>
        </td>
      </tr>

      <tr>
        <td class="email-pad" style="padding:0 48px 28px 48px;">
          <div style="background-color:{_ec_C['linen']};border-left:4px solid {_ec_C['instBlue']};padding:24px;border-radius:{_ec_R['sm']};">
            <table role="presentation" style="width:100%;border-collapse:collapse;">
              <tr>
                <td style="font-family:{_ec_F['ui']};padding:10px 0;color:{_ec_C['bodySlate']};font-size:12px;text-transform:uppercase;letter-spacing:0.05em;width:110px;font-weight:600;">Doctor</td>
                <td style="font-family:{_ec_F['ui']};padding:10px 0;color:{_ec_C['instBlue']};font-size:16px;font-weight:700;">{assigned_doctor}</td>
              </tr>
              <tr>
                <td style="font-family:{_ec_F['ui']};padding:10px 0;color:{_ec_C['bodySlate']};font-size:12px;text-transform:uppercase;letter-spacing:0.05em;font-weight:600;">Date &amp; Time</td>
                <td style="font-family:{_ec_F['ui']};padding:10px 0;color:{_ec_C['instBlue']};font-size:16px;font-weight:700;">{time_display}</td>
              </tr>
            </table>
          </div>
        </td>
      </tr>

      <tr>
        <td class="email-pad" style="padding:0 48px 28px 48px;text-align:center;">
          <a href="{doxy_link}" style="display:inline-block;background-color:{_ec_C['instBlue']};color:{_ec_C['white']};font-family:{_ec_F['ui']};text-decoration:none;padding:16px 40px;border-radius:{_ec_R['sm']};font-size:16px;font-weight:700;letter-spacing:0.02em;">Join Your Visit</a>
        </td>
      </tr>

      <tr>
        <td class="email-pad" style="padding:0 48px 28px 48px;">
          <p style="font-family:{_ec_F['ui']};font-size:13px;line-height:1.6;color:{_ec_C['bodySlate']};text-align:center;margin:0 0 24px 0;">
            We've attached a calendar invite — open it to add this appointment to your calendar automatically.
          </p>
          <div style="border-top:1px solid {_ec_C['borderLine']};padding-top:24px;">
            <p style="font-family:{_ec_F['body']};font-size:14px;font-weight:700;color:{_ec_C['instBlue']};margin:0 0 12px 0;">Before your visit:</p>
            <ul style="font-family:{_ec_F['ui']};font-size:14px;line-height:1.8;color:{_ec_C['bodySlate']};padding-left:20px;margin:0;">
              <li>No downloads needed — just click the link when it's time</li>
              <li>Find a quiet, private spot with a good connection</li>
              <li>Have any medications or health records handy</li>
            </ul>
          </div>
        </td>
      </tr>

      <tr>
        <td class="email-pad" style="padding:0 48px 32px 48px;border-top:1px solid {_ec_C['borderLine']};">
          <p style="font-family:{_ec_F['ui']};font-size:14px;line-height:1.6;color:{_ec_C['bodySlate']};margin:24px 0 16px 0;">
            Need to reschedule or have questions? Simply reply to this email — our care team is here for you.
          </p>
          <p style="font-family:{_ec_F['ui']};font-size:15px;color:{_ec_C['bodySlate']};line-height:1.6;font-style:italic;margin:16px 0 8px 0;">
            Care Without Distance.<br/>Healthcare coordination across the Americas.
          </p>
          <p style="font-family:{_ec_F['body']};font-size:16px;font-weight:700;color:{_ec_C['instBlue']};margin:0;">— Medikah Care Team</p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
{_ec_footer}
</body>
</html>"""

    # Doctor notification
    symptoms_line = (
        f"Primary concern: {req.symptoms.strip()}\n"
        if req.symptoms and req.symptoms.strip()
        else ""
    )
    locale_line = (
        f"Locale preference: {req.locale_preference.strip()}\n"
        if req.locale_preference and req.locale_preference.strip()
        else ""
    )
    notes_line = f"Intake notes:\n{intake_notes}\n" if intake_notes else ""
    doctor_plain_body = (
        f"New Medikah appointment\n"
        f"Doctor: {assigned_doctor}\n"
        f"Patient: {req.patient_name}\n"
        f"When: {time_display}\n"
        f"{symptoms_line}"
        f"{locale_line}"
        f"{notes_line}"
        f"Visit link: {doxy_link}\n"
    )

    messages = [
        NotificationMessage(
            recipient=req.patient_contact,
            subject=f"Your Medikah visit is confirmed — {time_display}",
            plain_body=patient_plain_body,
            html_body=patient_html_body,
            attachments=[ics_attachment],
        ),
        NotificationMessage(
            recipient=DOCTOR_NOTIFICATION_EMAIL,
            subject=f"New appointment: {req.patient_name} — {time_display}",
            plain_body=doctor_plain_body,
            html_body=doctor_plain_body.replace("\n", "<br/>"),
            attachments=[ics_attachment],
        ),
    ]

    if sandbox_mode:
        logger.info(
            "Sandbox mode enabled; skipping notification dispatch for appointment %s",
            record.appointment_id,
        )
        sandbox_response = SandboxScheduleResponse(
            status="ok",
            doxy=doxy_link,
            calendar=calendar_link,
            note="sandbox mode: email not sent",
        )
        return SchedulingOutcome(
            response=sandbox_response,
            appointment_id=record.appointment_id,
            doxy_link=doxy_link,
            calendar_link=calendar_link,
        )

    try:
        await notification_service.send_bulk(messages)
    except Exception as exc:
        logger.exception(
            "Failed to send notifications for appointment %s", record.appointment_id
        )
        raise HTTPException(
            status_code=502, detail="Failed to deliver appointment notifications."
        ) from exc

    return SchedulingOutcome(
        response=ScheduleResponse(
            appointment_id=record.appointment_id,
            doxy_link=doxy_link,
            calendar_link=calendar_link,
            message="Appointment scheduled and notifications dispatched.",
        ),
        appointment_id=record.appointment_id,
        doxy_link=doxy_link,
        calendar_link=calendar_link,
    )


async def finalize_chat_scheduling(
    state: ConversationState,
) -> tuple[str, List[dict[str, str]], bool]:
    intake = state.intake
    missing: List[str] = []

    if not intake.patient_name:
        missing.append("name")
    if not intake.patient_email:
        missing.append("email")
    if not intake.preferred_time_utc:
        missing.append("preferred time")

    if missing:
        missing_str = ", ".join(missing)
        return (
            f"I still need your {missing_str} before I can schedule."
            " Let me know when you're ready to add it.",
            [],
            False,
        )

    symptom_lines = [
        intake.symptom_overview or "",
        intake.symptom_history or "",
    ]
    symptoms_text = "\n".join(line for line in symptom_lines if line).strip() or None

    schedule_payload = ScheduleRequest(
        patient_name=intake.patient_name or "Medikah Patient",
        patient_contact=intake.patient_email,
        appointment_time=intake.preferred_time_utc,
        symptoms=symptoms_text,
        locale_preference=intake.locale_preference,
        patient_timezone=intake.patient_timezone,
    )

    logger.info(
        "finalize_chat_scheduling: session=%s, name=%s, email=%s, time=%s",
        state.session_id, intake.patient_name, intake.patient_email,
        intake.preferred_time_utc,
    )
    intake_notes = "\n".join(intake.summary_lines() + intake.notes)

    logger.info(
        "finalize_chat_scheduling: calling _perform_scheduling sandbox_mode=%s",
        EMAIL_SANDBOX_MODE,
    )
    outcome = await _perform_scheduling(
        schedule_payload,
        sandbox_mode=EMAIL_SANDBOX_MODE,
        intake_notes=intake_notes or None,
    )

    intake.appointment_id = outcome.appointment_id
    intake.appointment_confirmed_at = datetime.now(timezone.utc)
    state.stage = ConversationStage.SCHEDULED
    conversation_store.update(state)
    logger.info(
        "Triage chat session %s scheduled appointment %s",
        state.session_id,
        outcome.appointment_id,
    )

    # Format appointment time in patient's local timezone for display
    from zoneinfo import ZoneInfo
    if intake.preferred_time_utc:
        patient_tz = ZoneInfo("UTC")
        if intake.patient_timezone:
            try:
                patient_tz = ZoneInfo(intake.patient_timezone)
            except (KeyError, ValueError):
                pass
        local_time = intake.preferred_time_utc.astimezone(patient_tz)
        tz_abbr = local_time.strftime("%Z") or "UTC"
        appointment_time = local_time.strftime(f"%B %d, %Y at %I:%M %p {tz_abbr}")
    else:
        appointment_time = "your scheduled time"

    if isinstance(outcome.response, SandboxScheduleResponse):
        message = (
            "Sandbox mode is enabled, so emails were skipped, but your telemedicine "
            f"visit with {triage_engine.on_call_doctor_name} is staged for {appointment_time}. "
            "You can join using the secure link below."
        )
    else:
        message = (
            f"All set! I've scheduled your telemedicine visit with {triage_engine.on_call_doctor_name} "
            f"for {appointment_time}. You'll receive an email with the details."
        )

    doxy_url = triage_engine.doxy_room_url or outcome.doxy_link
    actions = (
        [{"label": "Join telemedicine visit", "url": doxy_url}] if doxy_url else []
    )

    return message, actions, True


async def _build_symptom_brief(intake: IntakeHistory) -> Optional[str]:
    if openai_client is None:
        return None

    if not intake.symptom_overview:
        return None

    details = [
        f"Primary concern: {intake.symptom_overview}",
    ]
    if intake.symptom_history:
        details.append(f"Symptom history: {intake.symptom_history}")

    summary = "\n".join(details)

    try:
        completion = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Medikah's virtual nurse. Offer a concise,"
                        " compassionate summary of possible considerations"
                        " based on the patient's description. Do not"
                        " diagnose or provide definitive treatment. Encourage"
                        " speaking with a clinician for confirmation. Limit"
                        " the response to 3-4 sentences."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Patient intake summary:\n"
                        f"{summary}\n\n"
                        "Provide general context and suggested next steps"
                        " the doctor might explore."
                    ),
                },
            ],
        )
        choice = completion.choices[0] if completion.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()
    except Exception:
        logger.exception("Failed to generate symptom brief via OpenAI")
    return None
@app.post("/chat", response_model=ChatResponse)
@limiter.limit("30/minute")
async def chat_endpoint(request: Request, req: ChatRequest) -> ChatResponse:
    """
    Orchestrate the triage intake conversation, capturing required data for scheduling.
    """
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        triage_result = await triage_engine.process_message(
            req.session_id, message, locale=req.locale, timezone=req.timezone,
            patient_name=req.patient_name, patient_email=req.patient_email,
        )
    except Exception as exc:  # noqa: BLE001 - want a clean HTTP error for clients
        logger.exception("Triage engine failed for session %s", req.session_id)
        raise HTTPException(
            status_code=500,
            detail="Unable to process your message right now. Please try again.",
        ) from exc

    actions = [{"label": action.label, "url": action.url} for action in triage_result.actions]
    reply_text = triage_result.reply
    appointment_confirmed = triage_result.appointment_confirmed

    state = conversation_store.get(triage_result.session_id)

    logger.info(
        "Chat endpoint: session=%s stage=%s should_schedule=%s state_found=%s",
        triage_result.session_id, triage_result.stage,
        triage_result.should_schedule, state is not None,
    )
    if triage_result.should_schedule and state is not None:
        try:
            schedule_message, schedule_actions, appointment_confirmed = await finalize_chat_scheduling(
                state
            )
            # Don't append schedule_message — the AI response already
            # confirms the booking in the patient's language
            if schedule_actions:
                actions.extend(schedule_actions)
        except Exception as exc:
            logger.exception(
                "finalize_chat_scheduling FAILED for session %s: %s",
                triage_result.session_id, exc,
            )
            reply_text = (
                f"{reply_text}\n\nI wasn't able to complete the scheduling just now. "
                "Please try again or contact us directly."
            )

    if (
        state is not None
        and state.intake.appointment_id
        and triage_result.stage == ConversationStage.SCHEDULED
    ):
        appointment_confirmed = True
        doxy_url = triage_engine.doxy_room_url or generate_doxy_link(
            DOXY_BASE_URL, state.intake.appointment_id
        )
        if doxy_url and not any(action.get("url") == doxy_url for action in actions):
            actions.append(
                {
                    "label": "Join telemedicine visit",
                    "url": doxy_url,
                }
            )

    return ChatResponse(
        reply=reply_text,
        response=reply_text,
        session_id=triage_result.session_id,
        stage=triage_result.stage,
        actions=actions,
        appointment_confirmed=appointment_confirmed,
        emergency_noted=triage_result.emergency_noted,
    )


@app.post(
    "/schedule", response_model=Union[ScheduleResponse, SandboxScheduleResponse]
)
@limiter.limit("10/minute")
async def schedule_endpoint(
    request: Request, req: ScheduleRequest
) -> Union[ScheduleResponse, SandboxScheduleResponse]:
    """
    Create a telehealth appointment, send notifications, and return the session link.
    """
    try:
        try:
            body = await request.json()
        except Exception:
            body = req.model_dump()
        logging.info("Incoming /schedule request for appointment at %s", req.appointment_time)

        sandbox_mode = EMAIL_SANDBOX_MODE
        outcome = await _perform_scheduling(
            req,
            sandbox_mode=sandbox_mode,
            intake_notes=body.get("intake_notes"),
        )
        return outcome.response
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error during /schedule processing.")
        raise HTTPException(
            status_code=500, detail="Unexpected error while scheduling appointment."
        )


@app.get("/")
async def read_root():
    """Root endpoint providing basic info."""
    return {"message": "Medikah Chat API is running"}


@app.get("/ping")
def ping() -> dict[str, str]:
    """Lightweight health check endpoint."""
    return {"message": "pong"}


@app.get("/health")
async def health() -> dict:
    """Health check endpoint with diagnostics (no external API calls)."""
    return {
        "status": "ok",
        "openai_client": openai_client is not None,
        "ai_responder": ai_responder is not None,
        "supabase": conversation_store._use_db,
        "sandbox_mode": EMAIL_SANDBOX_MODE,
        "ai_test": "configured" if openai_client else "not_configured",
        "doxy_room_url": DOXY_ROOM_URL or "(not set)",
        "doxy_base_url": DOXY_BASE_URL or "(not set)",
    }

