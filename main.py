from __future__ import annotations

import logging
import os
import random
import traceback
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()
raw_level = os.getenv("LOG_LEVEL", "INFO")
level = getattr(logging, raw_level.upper(), logging.INFO)
logging.basicConfig(level=level)
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
NOTIFICATION_SENDER_EMAIL = os.getenv("NOTIFICATION_SENDER_EMAIL")
APPOINTMENT_HASH_KEY = os.getenv("APPOINTMENT_HASH_KEY")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDGRID_SANDBOX_MODE = os.getenv("SENDGRID_SANDBOX_MODE", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
APPOINTMENT_DURATION_MINUTES = _resolve_duration_minutes()
DOCTOR_POOL = tuple(
    name.strip()
    for name in os.getenv(
        "SCHEDULER_DOCTORS",
        "Dr. Alvarez,Dr. Gutierrez,Dr. Lopez",
    ).split(",")
    if name.strip()
)

appointment_store: Optional[SecureAppointmentStore]
notification_service: Optional[NotificationService]

if APPOINTMENT_HASH_KEY:
    appointment_store = SecureAppointmentStore(APPOINTMENT_HASH_KEY)
else:
    appointment_store = None
    logger.error(
        "APPOINTMENT_HASH_KEY not configured; /schedule endpoint will be unavailable."
    )

if SENDGRID_API_KEY and NOTIFICATION_SENDER_EMAIL:
    notification_service = NotificationService(
        SENDGRID_API_KEY,
        NOTIFICATION_SENDER_EMAIL,
        sandbox_mode=SENDGRID_SANDBOX_MODE,
    )
else:
    notification_service = None
    logger.error(
        "SendGrid credentials missing; notifications cannot be delivered until "
        "SENDGRID_API_KEY and NOTIFICATION_SENDER_EMAIL are configured."
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


@app.post("/schedule", response_model=ScheduleResponse)
async def schedule_endpoint(req: ScheduleRequest) -> ScheduleResponse:
    """
    Create a telehealth appointment, send notifications, and return the session link.
    """
    if appointment_store is None or not DOXY_BASE_URL:
        logger.error(
            "Schedule attempted without proper storage or Doxy configuration."
        )
        raise HTTPException(
            status_code=503, detail="Scheduling service is not configured."
        )
    if notification_service is None or not DOCTOR_NOTIFICATION_EMAIL:
        logger.error("Notification service not configured for scheduling.")
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
    except Exception as exc:  # noqa: BLE001 - log and rewrap for client clarity.
        logger.exception("Failed to persist appointment for %s", req.patient_contact)
        raise HTTPException(
            status_code=500, detail="Unable to store the appointment."
        ) from exc

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

    assigned_doctor = random.choice(DOCTOR_POOL) if DOCTOR_POOL else "Medikah Primary Care"
    logger.info(
        "Assigned doctor %s to appointment %s",
        assigned_doctor,
        record.appointment_id,
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

    try:
        await notification_service.send_bulk(messages)
    except Exception as exc:
        logger.exception("Failed to send notifications for appointment %s", record.appointment_id)
        raise HTTPException(
            status_code=502, detail="Failed to deliver appointment notifications."
        ) from exc

    return ScheduleResponse(
        appointment_id=record.appointment_id,
        doxy_link=doxy_link,
        calendar_link=calendar_link,
        message="Appointment scheduled and notifications dispatched.",
    )


@app.get("/")
async def read_root():
    """Root endpoint providing basic info."""
    return {"message": "Medikah Chat API is running"}


@app.get("/ping")
def ping() -> dict[str, str]:
    """Lightweight health check endpoint."""
    return {"message": "pong"}
