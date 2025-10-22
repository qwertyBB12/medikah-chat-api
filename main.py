from __future__ import annotations

import logging
import os
import random
import traceback
from datetime import datetime
from typing import Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from services.appointments import SecureAppointmentStore
from services.notifications import NotificationMessage, NotificationService
from utils.scheduling import (
    build_google_calendar_link,
    generate_doxy_link,
)

app = FastAPI(title="Medikah Chat API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 🔐 TODO: Replace with actual Netlify domain for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


class ChatResponse(BaseModel):
    """Schema for a chat response."""

    response: str


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
DOCTOR_NOTIFICATION_EMAIL = os.getenv("DOCTOR_NOTIFICATION_EMAIL")

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDGRID_SENDER_EMAIL = os.getenv("SENDGRID_SENDER_EMAIL", "noreply@medikah.org")
NOTIFICATION_SENDER_EMAIL = os.getenv(
    "NOTIFICATION_SENDER_EMAIL", SENDGRID_SENDER_EMAIL
)

APPOINTMENT_HASH_KEY = os.getenv("APPOINTMENT_HASH_KEY")
if not APPOINTMENT_HASH_KEY:
    raise RuntimeError("❌ Missing APPOINTMENT_HASH_KEY")

SENDGRID_SANDBOX_MODE_RAW = os.getenv("SENDGRID_SANDBOX_MODE", "false").lower()
SENDGRID_SANDBOX_MODE = SENDGRID_SANDBOX_MODE_RAW in {"1", "true", "yes", "on"}
APPOINTMENT_DURATION_MINUTES = _resolve_duration_minutes()
DOCTOR_POOL = tuple(
    name.strip()
    for name in os.getenv(
        "SCHEDULER_DOCTORS",
        "Dr. Alvarez,Dr. Gutierrez,Dr. Lopez",
    ).split(",")
    if name.strip()
)

appointment_store: Optional[SecureAppointmentStore] = SecureAppointmentStore(
    APPOINTMENT_HASH_KEY
)
notification_service: Optional[NotificationService] = None
if SENDGRID_API_KEY and NOTIFICATION_SENDER_EMAIL:
    notification_service = NotificationService(
        SENDGRID_API_KEY,
        NOTIFICATION_SENDER_EMAIL,
        sandbox_mode=SENDGRID_SANDBOX_MODE,
    )
else:
    if not SENDGRID_API_KEY:
        logger.warning("SENDGRID_API_KEY not provided; notification service disabled.")
    if not NOTIFICATION_SENDER_EMAIL:
        logger.warning(
            "Notification sender email not provided; notification service disabled."
        )


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    """
    Accepts a user message, forwards it to the configured LLM provider, and returns
    the response. This implementation calls OpenAI's GPT-4o model.
    """
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    answer = "OpenAI call failed"

    if openai_client is not None:
        try:
            logger.info("Dispatching request to OpenAI GPT-4o: %s", message)
            completion = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": "You are Medikah's assistant. Provide concise, helpful medical guidance without offering diagnoses.",
                    },
                    {"role": "user", "content": message},
                ],
            )
            logger.debug("OpenAI raw completion received: %s", completion)
            choice = completion.choices[0] if completion.choices else None
            answer_text = ""
            if choice and choice.message and choice.message.content:
                answer_text = choice.message.content.strip()
            if answer_text:
                answer = answer_text
            else:
                logger.warning("OpenAI response missing content; returning fallback.")
        except Exception:
            logger.exception("OpenAI request failed.")
            answer = "OpenAI call failed"

    return ChatResponse(response=answer)


@app.post(
    "/schedule", response_model=Union[ScheduleResponse, SandboxScheduleResponse]
)
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
        logging.info("Incoming /schedule payload: %s", body)

        sandbox_mode_env = os.getenv("SENDGRID_SANDBOX_MODE", "false").lower()
        sandbox_mode = sandbox_mode_env == "true"

        if appointment_store is None or not DOXY_BASE_URL:
            logger.error(
                "Schedule attempted without proper storage or Doxy configuration."
            )
            raise HTTPException(
                status_code=503, detail="Scheduling service is not configured."
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

        sendgrid_ready = (
            bool(SENDGRID_API_KEY)
            and notification_service is not None
            and bool(DOCTOR_NOTIFICATION_EMAIL)
        )
        if not sandbox_mode and not sendgrid_ready:
            logger.error(
                "Notification service configuration incomplete. "
                "SENDGRID_API_KEY=%s, service=%s, doctor_email=%s",
                bool(SENDGRID_API_KEY),
                notification_service is not None,
                bool(DOCTOR_NOTIFICATION_EMAIL),
            )
            raise HTTPException(
                status_code=503, detail="Notification service is not configured."
            )

        try:
            record = appointment_store.save(
                patient_name=req.patient_name,
                patient_contact=req.patient_contact,
                appointment_time=appointment_time,
            )
        except Exception:
            logger.exception("Failed to persist appointment for %s", req.patient_contact)
            raise HTTPException(
                status_code=500, detail="Unable to store the appointment."
            )

        doxy_link = generate_doxy_link(DOXY_BASE_URL, record.appointment_id)
        calendar_description = (
            f"Telehealth visit for {req.patient_name}. Join via Doxy.me: {doxy_link}"
        )
        calendar_link = build_google_calendar_link(
            title="Medikah Telehealth Appointment",
            description=calendar_description,
            start=appointment_time,
            duration_minutes=APPOINTMENT_DURATION_MINUTES,
            location=doxy_link,
        )

        assigned_doctor = (
            random.choice(DOCTOR_POOL) if DOCTOR_POOL else "Medikah Primary Care"
        )
        logger.info(
            "Assigned doctor %s to appointment %s",
            assigned_doctor,
            record.appointment_id,
        )

        if sandbox_mode:
            logger.info(
                "Sandbox mode enabled; skipping notification dispatch for appointment %s",
                record.appointment_id,
            )
            return SandboxScheduleResponse(
                status="ok",
                doxy=doxy_link,
                calendar=calendar_link,
                note="sandbox mode: email not sent",
            )

        patient_plain_body = (
            f"Hello {req.patient_name},\n\n"
            f"Your telehealth appointment is scheduled for "
            f"{appointment_time.isoformat()}.\n"
            f"Join using this secure Doxy.me link: {doxy_link}\n\n"
            f"Add the appointment to your calendar: {calendar_link}\n\n"
            "If you did not request this appointment, please contact us immediately.\n\n"
            "Thank you,\nMedikah Care Team"
        )
        patient_html_body = (
            f"<p>Hello {req.patient_name},</p>"
            f"<p>Your telehealth appointment is scheduled for "
            f"<strong>{appointment_time.isoformat()}</strong>.</p>"
            f"<p>Join using this secure Doxy.me link: "
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
        doctor_plain_body = (
            f"Telehealth appointment scheduled.\n"
            f"Assigned doctor: {assigned_doctor}\n"
            f"Patient: {req.patient_name}\n"
            f"When: {appointment_time.isoformat()}\n"
            f"{symptoms_line}"
            f"{locale_line}"
            f"Doxy.me link: {doxy_link}\n"
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

        await notification_service.send_bulk(messages)

        return ScheduleResponse(
            appointment_id=record.appointment_id,
            doxy_link=doxy_link,
            calendar_link=calendar_link,
            message="Appointment scheduled and notifications dispatched.",
        )
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

# Temporary diagnostic to inspect registered routes; remove once verified.
logging.info("Routes: %s", [route.path for route in app.routes])
