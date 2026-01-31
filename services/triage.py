"""Conversation engine that orchestrates the intake triage dialogue."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from dateutil import parser as dt_parser
from email_validator import EmailNotValidError, validate_email

from services.conversation_state import (
    ConversationStage,
    ConversationState,
    ConversationStateStore,
)

logger = logging.getLogger(__name__)

EMERGENCY_KEYWORDS = (
    "chest pain",
    "shortness of breath",
    "difficulty breathing",
    "trouble breathing",
    "bleeding",
    "unconscious",
    "can't breathe",
    "cannot breathe",
    "suicidal",
    "overdose",
    "stroke",
    "heart attack",
    "severe pain",
    "numbness",
)

AFFIRMATIVE_WORDS = ("yes", "yep", "yeah", "affirmative", "please", "sure", "ok", "okay")
NEGATIVE_WORDS = ("no", "nope", "nah", "not now", "later")


def _detect_emergency(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in EMERGENCY_KEYWORDS)


def _has_word(text: str, words: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def _parse_preferred_time(raw: str) -> Optional[datetime]:
    text = raw.strip()
    if not text:
        return None
    try:
        dt = dt_parser.parse(text, fuzzy=True)
    except (ValueError, dt_parser.ParserError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sanitize_name(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.title()


@dataclass(slots=True)
class TriageAction:
    """Represents an optional action (CTA) to show alongside a response."""

    label: str
    url: str


@dataclass(slots=True)
class TriageResult:
    """Computed response for a conversation turn."""

    reply: str
    stage: ConversationStage
    session_id: str
    actions: List[TriageAction] = field(default_factory=list)
    appointment_confirmed: bool = False
    emergency_noted: bool = False
    should_schedule: bool = False


class TriageConversationEngine:
    """Encapsulates the state machine logic for the intake conversation."""

    def __init__(
        self,
        store: ConversationStateStore,
        *,
        on_call_doctor_name: str,
        doxy_room_url: str,
    ) -> None:
        self._store = store
        self._on_call_doctor_name = on_call_doctor_name
        self._doxy_room_url = doxy_room_url

    def begin_or_resume(
        self, session_id: Optional[str]
    ) -> ConversationState:
        state = self._store.get_or_create(session_id)
        if state.intake.patient_name:
            logger.debug(
                "Resuming intake session %s at stage %s",
                state.session_id,
                state.stage,
            )
        return state

    def build_summary(self, state: ConversationState) -> str:
        lines = [
            "Here is what I've gathered so far:",
            f"• Name: {state.intake.patient_name or '—'}",
            f"• Contact email: {state.intake.patient_email or '—'}",
            f"• Primary concern: {state.intake.symptom_overview or '—'}",
            f"• Symptom details: {state.intake.symptom_history or '—'}",
        ]
        if state.intake.preferred_time_utc:
            lines.append(
                f"• Preferred appointment time (UTC): "
                f"{state.intake.preferred_time_utc.isoformat()}"
            )
        if state.intake.locale_preference:
            lines.append(
                f"• Language preference: {state.intake.locale_preference}"
            )
        return "\n".join(lines)

    def process_message(
        self, session_id: Optional[str], message: str, *, locale: Optional[str] = None
    ) -> TriageResult:
        state = self.begin_or_resume(session_id)
        intake = state.intake
        text = message.strip()
        should_schedule = False

        if not text:
            return TriageResult(
                reply=(
                    "I'm ready when you are. Could you share a bit more so I "
                    "can prepare the doctor?"
                ),
                stage=state.stage,
                session_id=state.session_id,
            )

        if _detect_emergency(text):
            intake.emergency_flag = True
            intake.notes.append(f"emergency_flagged: {text}")
            state.stage = ConversationStage.EMERGENCY_ESCALATED
            state.touch()
            self._store.update(state)
            return TriageResult(
                reply=(
                    "Your symptoms sound urgent. Please call your local "
                    "emergency number or go to the nearest emergency room "
                    "immediately. I'll pause scheduling and remain here if "
                    "you need non-urgent information."
                ),
                stage=state.stage,
                session_id=state.session_id,
                emergency_noted=True,
            )

        if locale and not intake.locale_preference:
            intake.locale_preference = locale

        if state.stage == ConversationStage.WELCOME:
            state.stage = ConversationStage.COLLECT_NAME
            response = (
                "Welcome to Medikah! I'm here to help you connect with a doctor. "
                "To get started, could you share your name?"
            )
        elif state.stage == ConversationStage.COLLECT_NAME:
            intake.patient_name = _sanitize_name(text)
            intake.notes.append(f"name_raw: {text}")
            state.stage = ConversationStage.COLLECT_EMAIL
            response = (
                f"Thank you, {intake.patient_name}. What's the best email to "
                "send appointment details to?"
            )
        elif state.stage == ConversationStage.COLLECT_EMAIL:
            try:
                validation = validate_email(text, check_deliverability=False)
                intake.patient_email = validation.normalized
                intake.notes.append(f"email_raw: {text}")
            except EmailNotValidError:
                return TriageResult(
                    reply=(
                        "I want to be sure the doctor can reach you. Could you "
                        "enter a valid email address?"
                    ),
                    stage=state.stage,
                    session_id=state.session_id,
                )
            state.stage = ConversationStage.COLLECT_SYMPTOMS
            response = (
                "Got it. Could you tell me what you're feeling and what you'd "
                "like the doctor to help with today?"
            )
        elif state.stage == ConversationStage.COLLECT_SYMPTOMS:
            intake.symptom_overview = text
            intake.notes.append(f"symptom_overview: {text}")
            state.stage = ConversationStage.COLLECT_HISTORY
            response = (
                "Thanks for sharing that. When did these symptoms begin, and "
                "have they been getting better, worse, or about the same?"
            )
        elif state.stage == ConversationStage.COLLECT_HISTORY:
            existing = intake.symptom_history or ""
            combined = f"{existing}\n{text}".strip() if existing else text
            intake.symptom_history = combined
            intake.notes.append(f"symptom_history: {text}")
            state.stage = ConversationStage.COLLECT_TIMING
            response = (
                "Understood. When would you like to connect with our on-call "
                "doctor via telemedicine? You can share a date and time."
            )
        elif state.stage == ConversationStage.COLLECT_TIMING:
            appointment_dt = _parse_preferred_time(text)
            if appointment_dt is None:
                return TriageResult(
                    reply=(
                        "Thanks. Could you share the date and time in a format "
                        "like '2025-10-23 17:00' so I can lock it in?"
                    ),
                    stage=state.stage,
                    session_id=state.session_id,
                )
            intake.preferred_time_utc = appointment_dt
            intake.notes.append(f"preferred_time_input: {text}")
            state.stage = ConversationStage.CONFIRM_SUMMARY
            summary = self.build_summary(state)
            response = (
                f"{summary}\n\nDoes that summary look right? Let me know if "
                "anything needs an edit."
            )
        elif state.stage == ConversationStage.CONFIRM_SUMMARY:
            intake.notes.append(f"summary_feedback: {text}")
            if _has_word(text, AFFIRMATIVE_WORDS):
                state.stage = ConversationStage.CONFIRM_APPOINTMENT
                response = (
                    "Perfect. Would you like me to book a telemedicine visit "
                    f"with {self._on_call_doctor_name}? It's a secure Doxy.me "
                    "call."
                )
            elif "name" in text.lower():
                state.stage = ConversationStage.COLLECT_NAME
                response = "No problem—what name should I use instead?"
            elif "email" in text.lower():
                state.stage = ConversationStage.COLLECT_EMAIL
                response = "Understood. What's the correct email address?"
                intake.patient_email = None
            elif "time" in text.lower() or "date" in text.lower():
                state.stage = ConversationStage.COLLECT_TIMING
                response = (
                    "Sure thing. What date and time works best for you?"
                )
                intake.preferred_time_utc = None
            else:
                response = (
                    "Thanks for clarifying. Tell me what you'd like to update "
                    "— name, email, symptoms, or timing — and I'll adjust."
                )
        elif state.stage == ConversationStage.CONFIRM_APPOINTMENT:
            intake.notes.append(f"appointment_decision: {text}")
            if _has_word(text, AFFIRMATIVE_WORDS):
                if intake.appointment_id:
                    response = (
                        "You're already booked. If you need to make changes, "
                        "let me know."
                    )
                else:
                    response = (
                        "Great! Let me finalize that booking. I'll share the "
                        "appointment details in just a moment."
                    )
                state.stage = ConversationStage.SCHEDULED
                should_schedule = intake.appointment_id is None
            elif _has_word(text, NEGATIVE_WORDS):
                state.stage = ConversationStage.FOLLOW_UP
                response = (
                    "All right—I'll hold off on scheduling. Let me know if "
                    "you'd like more guidance or resources."
                )
            else:
                response = (
                    "I'll wait for a yes or no. Would you like me to schedule "
                    "the telemedicine visit?"
                )
        elif state.stage == ConversationStage.SCHEDULED:
            if intake.appointment_id:
                response = (
                    "You're set! Feel free to ask any other questions while "
                    "you wait for the visit."
                )
            else:
                response = (
                    "I'm still wrapping up the scheduling details. Give me a "
                    "moment and I'll confirm everything shortly."
                )
        else:  # FOLLOW_UP, COMPLETED, EMERGENCY_ESCALATED
            response = (
                "I'm here if you have more questions about your symptoms or "
                "next steps."
            )

        state.touch()
        self._store.update(state)

        return TriageResult(
            reply=response,
            stage=state.stage,
            session_id=state.session_id,
            emergency_noted=intake.emergency_flag,
            appointment_confirmed=bool(intake.appointment_id),
            should_schedule=should_schedule,
        )

    @property
    def doxy_room_url(self) -> str:
        return self._doxy_room_url

    @property
    def on_call_doctor_name(self) -> str:
        return self._on_call_doctor_name
