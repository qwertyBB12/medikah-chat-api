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
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from services.appointments import SecureAppointmentStore
from services.conversation_state import (
    ConversationStage,
    ConversationState,
    ConversationStateStore,
    IntakeHistory,
)
from services.ai_triage import AITriageResponseGenerator, TriagePromptBuilder
from services.notifications import NotificationMessage, NotificationService
from services.triage import TriageAction, TriageConversationEngine
from utils.scheduling import (
    build_google_calendar_link,
    generate_doxy_link,
)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Medikah Chat API")
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
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

load_dotenv()

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")
logging.info(f"Using LOG_LEVEL={log_level_str}")
logger = logging.getLogger("medikah.api")

try:
    openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    logger.info("OpenAI client initialised successfully.")
except KeyError:
    openai_client = None
    logger.warning("OPENAI_API_KEY not set; OpenAI client disabled.")


class ChatRequest(BaseModel):
    """Schema for a chat request."""

    message: str
    session_id: Optional[str] = Field(default=None, max_length=120)
    locale: Optional[str] = Field(default=None, max_length=16)


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

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDGRID_SENDER_EMAIL = os.getenv("SENDGRID_SENDER_EMAIL")
APPOINTMENT_HASH_KEY = os.getenv("APPOINTMENT_HASH_KEY")

missing_envs = [
    name
    for name, value in (
        ("SENDGRID_API_KEY", SENDGRID_API_KEY),
        ("SENDGRID_SENDER_EMAIL", SENDGRID_SENDER_EMAIL),
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

SENDGRID_SANDBOX_MODE_RAW = os.getenv("SENDGRID_SANDBOX_MODE", "false").lower()
SENDGRID_SANDBOX_MODE = SENDGRID_SANDBOX_MODE_RAW in {"1", "true", "yes", "on"}
APPOINTMENT_DURATION_MINUTES = _resolve_duration_minutes()

appointment_store: Optional[SecureAppointmentStore] = SecureAppointmentStore(
    APPOINTMENT_HASH_KEY
)
notification_service: NotificationService = NotificationService(
    SENDGRID_API_KEY,
    SENDGRID_SENDER_EMAIL,
    sandbox_mode=SENDGRID_SANDBOX_MODE,
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

    patient_plain_body = (
        f"Hello {req.patient_name},\n\n"
        f"Your telehealth appointment with {assigned_doctor} is scheduled for "
        f"{appointment_time.isoformat()}.\n"
        f"Join using this secure Medikah link: {doxy_link}\n\n"
        f"Add the appointment to your calendar: {calendar_link}\n\n"
        "If you did not request this appointment, please contact us immediately.\n\n"
        "Thank you,\nMedikah Care Team"
    )
    patient_html_body = (
        f"<p>Hello {req.patient_name},</p>"
        f"<p>Your telehealth appointment with {assigned_doctor} is scheduled for "
        f"<strong>{appointment_time.isoformat()}</strong>.</p>"
        f"<p>Join using this secure Medikah link: "
        f'<a href="{doxy_link}">{doxy_link}</a></p>'
        f'<p>Add the appointment to your calendar: <a href="{calendar_link}">'
        "Calendar Link</a></p>"
        "<p>If you did not request this appointment, please contact us immediately.</p>"
        "<p>Thank you,<br/>Medikah Care Team</p>"
    )
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
        f"Telehealth appointment scheduled.\n"
        f"Assigned doctor: {assigned_doctor}\n"
        f"Patient: {req.patient_name}\n"
        f"When: {appointment_time.isoformat()}\n"
        f"{symptoms_line}"
        f"{locale_line}"
        f"{notes_line}"
        f"Medikah link: {doxy_link}\n"
        f"Calendar: {calendar_link}\n"
    )

    messages = [
        NotificationMessage(
            recipient=req.patient_contact,
            subject="Your upcoming Medikah telehealth appointment",
            plain_body=patient_plain_body,
            html_body=patient_html_body,
        ),
        NotificationMessage(
            recipient=DOCTOR_NOTIFICATION_EMAIL,
            subject="New Medikah telehealth appointment scheduled",
            plain_body=doctor_plain_body,
            html_body=doctor_plain_body.replace("\n", "<br/>"),
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
    )

    sandbox_mode = os.getenv("SENDGRID_SANDBOX_MODE", "false").lower() == "true"
    intake_notes = "\n".join(intake.summary_lines() + intake.notes)

    outcome = await _perform_scheduling(
        schedule_payload,
        sandbox_mode=sandbox_mode,
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

    appointment_time = (
        intake.preferred_time_utc.isoformat()
        if intake.preferred_time_utc
        else "your scheduled time"
    )

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
            req.session_id, message, locale=req.locale
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

    if triage_result.should_schedule and state is not None:
        schedule_message, schedule_actions, appointment_confirmed = await finalize_chat_scheduling(
            state
        )
        if schedule_message:
            reply_text = f"{reply_text}\n\n{schedule_message}"
        if schedule_actions:
            actions.extend(schedule_actions)

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

        sandbox_mode = os.getenv("SENDGRID_SANDBOX_MODE", "false").lower() == "true"
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
    """Health check endpoint with diagnostics."""
    ai_test = None
    if openai_client:
        try:
            completion = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "Say hello in 3 words"}],
                max_tokens=20,
            )
            ai_test = completion.choices[0].message.content.strip() if completion.choices else "empty"
        except Exception as exc:
            ai_test = f"ERROR: {exc}"
    return {
        "status": "ok",
        "openai_client": openai_client is not None,
        "ai_responder": ai_responder is not None,
        "supabase": conversation_store._use_db,
        "sandbox_mode": SENDGRID_SANDBOX_MODE,
        "ai_test": ai_test,
    }

