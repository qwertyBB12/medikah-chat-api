"""Conversation engine that orchestrates the intake triage dialogue."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, List, Optional

from dateutil import parser as dt_parser
from email_validator import EmailNotValidError, validate_email

from services.conversation_state import (
    ConversationStage,
    ConversationState,
    ConversationStateStore,
)

if TYPE_CHECKING:
    from services.ai_triage import AITriageResponseGenerator

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
    # Spanish equivalents
    "dolor de pecho",
    "falta de aire",
    "dificultad para respirar",
    "sangrado",
    "inconsciente",
    "no puedo respirar",
    "sobredosis",
    "derrame",
    "infarto",
    "dolor severo",
    "dolor intenso",
    "entumecimiento",
)

AFFIRMATIVE_WORDS = (
    "yes", "yep", "yeah", "affirmative", "please", "sure", "ok", "okay",
    "correct", "right", "good", "great", "perfect", "looks good", "that's right",
    "that's correct", "confirm", "go ahead", "do it", "book", "schedule",
    "let's do it", "sounds good", "all good", "fine", "absolutely", "of course",
    "thanks", "thank you", "that works", "works for me", "let's go", "yup",
    "sí", "si", "claro", "por favor", "dale", "de acuerdo", "está bien",
    "correcto", "bien", "perfecto", "todo bien", "adelante", "confírmalo",
    "agéndalo", "listo", "eso es", "confirmado", "confirmo", "todo listo",
    "gracias", "eso", "va", "vamos", "sale",
)
NEGATIVE_WORDS = (
    "no", "nope", "nah", "not now", "later",
    "no gracias", "ahora no", "después", "luego",
)


def _detect_emergency(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in EMERGENCY_KEYWORDS)


def _has_word(text: str, words: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


_RELATIVE_TIME_MAP = {
    "tomorrow": 1, "mañana": 1,
    "today": 0, "hoy": 0,
    "next week": 7, "la próxima semana": 7, "proxima semana": 7,
    "in two days": 2, "en dos días": 2, "en dos dias": 2,
    "in three days": 3, "en tres días": 3, "en tres dias": 3,
}


def _parse_preferred_time(raw: str) -> Optional[datetime]:
    text = raw.strip()
    if not text:
        return None

    lowered = text.lower()

    # Handle relative time expressions
    from datetime import timedelta
    for phrase, days_offset in _RELATIVE_TIME_MAP.items():
        if phrase in lowered:
            base_date = datetime.now(timezone.utc) + timedelta(days=days_offset)
            # Try to extract a time from the rest of the text
            remaining = lowered.replace(phrase, "").strip()
            hour = 10  # default to 10am UTC
            if remaining:
                try:
                    parsed_time = dt_parser.parse(remaining, fuzzy=True)
                    hour = parsed_time.hour
                except (ValueError, dt_parser.ParserError):
                    pass
            dt = base_date.replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            logger.info("Parsed relative time %r → %s", text, dt.isoformat())
            return dt

    try:
        dt = dt_parser.parse(text, fuzzy=True)
    except (ValueError, dt_parser.ParserError):
        logger.info("Failed to parse time from: %r", text)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    logger.info("Parsed time %r → %s", text, dt.isoformat())
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
        ai_responder: Optional[AITriageResponseGenerator] = None,
    ) -> None:
        self._store = store
        self._on_call_doctor_name = on_call_doctor_name
        self._doxy_room_url = doxy_room_url
        self._ai_responder = ai_responder

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

    # ------------------------------------------------------------------
    # Fallback responses (used when AI is unavailable)
    # ------------------------------------------------------------------

    def _fallback_response(
        self, stage: ConversationStage, state: ConversationState
    ) -> str:
        intake = state.intake
        if stage == ConversationStage.WELCOME:
            return (
                "Welcome to Medikah! I'm here to help you connect with a doctor. "
                "What brings you in today? How are you feeling?"
            )
        elif stage == ConversationStage.COLLECT_SYMPTOMS:
            return (
                "Thank you for sharing that. Could you tell me a bit more about "
                "what you're experiencing?"
            )
        elif stage == ConversationStage.COLLECT_HISTORY:
            return (
                "Thanks for sharing that. When did these symptoms begin, and "
                "have they been getting better, worse, or about the same?"
            )
        elif stage == ConversationStage.COLLECT_NAME:
            return (
                "Thank you for telling me about that. To help connect you with "
                "our doctor, could I get your name?"
            )
        elif stage == ConversationStage.COLLECT_EMAIL:
            return (
                f"Thank you, {intake.patient_name}. What's the best email to "
                "send your appointment details to?"
            )
        elif stage == ConversationStage.COLLECT_TIMING:
            return (
                "Great. When would you like to schedule your Medikah visit? "
                "You can share a date and time."
            )
        elif stage == ConversationStage.CONFIRM_SUMMARY:
            summary = self.build_summary(state)
            return (
                f"Here is what I've gathered so far:\n{summary}\n\n"
                "Does that summary look right? Let me know if anything needs an edit."
            )
        elif stage == ConversationStage.CONFIRM_APPOINTMENT:
            return (
                "Perfect. Would you like me to book your Medikah visit "
                f"with {self._on_call_doctor_name}?"
            )
        elif stage == ConversationStage.SCHEDULED:
            return (
                "You're all set! You'll receive an email with your appointment "
                "details. Feel free to ask any other questions."
            )
        else:
            return (
                "I'm here if you have more questions about your symptoms or "
                "next steps."
            )

    # ------------------------------------------------------------------
    # Main conversation processing
    # ------------------------------------------------------------------

    async def process_message(
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

        # Emergency detection (keyword-based safety net — always runs)
        if _detect_emergency(text):
            intake.emergency_flag = True
            intake.notes.append(f"emergency_flagged: {text}")
            state.stage = ConversationStage.EMERGENCY_ESCALATED
            # Generate AI emergency response or use fallback
            response = None
            if self._ai_responder:
                response = await self._ai_responder.generate_response(
                    text, state.stage, intake, locale
                )
            if not response:
                response = (
                    "Your symptoms sound urgent. Please call your local "
                    "emergency number or go to the nearest emergency room "
                    "immediately. I'll pause scheduling and remain here if "
                    "you need non-urgent information."
                )
            intake.add_message("user", text)
            intake.add_message("assistant", response)
            state.touch()
            self._store.update(state)
            return TriageResult(
                reply=response,
                stage=state.stage,
                session_id=state.session_id,
                emergency_noted=True,
            )

        # Auto-detect language from user text if not yet set
        if not intake.locale_preference:
            if locale:
                intake.locale_preference = locale
            else:
                # Simple heuristic: common Spanish words → es
                _spanish_markers = (
                    "hola", "buenos", "tengo", "estoy", "quiero", "necesito",
                    "dolor", "siento", "ayuda", "cómo", "qué", "por favor",
                    "gracias", "médico", "cita", "salud",
                )
                if any(w in text.lower() for w in _spanish_markers):
                    intake.locale_preference = "es"

        # ---- State machine: extract data and advance stage ----
        # Flow: WELCOME → COLLECT_SYMPTOMS → COLLECT_HISTORY → COLLECT_NAME
        #       → COLLECT_EMAIL → COLLECT_TIMING → CONFIRM → SCHEDULE

        if state.stage == ConversationStage.WELCOME:
            state.stage = ConversationStage.COLLECT_SYMPTOMS

        elif state.stage == ConversationStage.COLLECT_SYMPTOMS:
            intake.symptom_overview = text
            intake.notes.append(f"symptom_overview: {text}")
            state.stage = ConversationStage.COLLECT_HISTORY

        elif state.stage == ConversationStage.COLLECT_HISTORY:
            existing = intake.symptom_history or ""
            combined = f"{existing}\n{text}".strip() if existing else text
            intake.symptom_history = combined
            intake.notes.append(f"symptom_history: {text}")
            state.stage = ConversationStage.COLLECT_NAME

        elif state.stage == ConversationStage.COLLECT_NAME:
            intake.patient_name = _sanitize_name(text)
            intake.notes.append(f"name_raw: {text}")
            state.stage = ConversationStage.COLLECT_EMAIL

        elif state.stage == ConversationStage.COLLECT_EMAIL:
            try:
                validation = validate_email(text, check_deliverability=False)
                intake.patient_email = validation.normalized
                intake.notes.append(f"email_raw: {text}")
                state.stage = ConversationStage.COLLECT_TIMING
            except EmailNotValidError:
                # Stay at COLLECT_EMAIL — AI will ask nicely to retry
                pass

        elif state.stage == ConversationStage.COLLECT_TIMING:
            appointment_dt = _parse_preferred_time(text)
            if appointment_dt is not None:
                intake.preferred_time_utc = appointment_dt
                intake.notes.append(f"preferred_time_input: {text}")
                state.stage = ConversationStage.CONFIRM_SUMMARY
            # If parse fails, stay at COLLECT_TIMING — AI will ask to retry

        elif state.stage == ConversationStage.CONFIRM_SUMMARY:
            intake.notes.append(f"summary_feedback: {text}")
            logger.info(
                "CONFIRM_SUMMARY: text=%r, affirmative=%s",
                text, _has_word(text, AFFIRMATIVE_WORDS),
            )
            if _has_word(text, AFFIRMATIVE_WORDS):
                state.stage = ConversationStage.SCHEDULED
                should_schedule = intake.appointment_id is None
            elif _has_word(text, NEGATIVE_WORDS):
                state.stage = ConversationStage.FOLLOW_UP
            elif "name" in text.lower() or "nombre" in text.lower():
                state.stage = ConversationStage.COLLECT_NAME
            elif "email" in text.lower() or "correo" in text.lower():
                state.stage = ConversationStage.COLLECT_EMAIL
                intake.patient_email = None
            elif any(w in text.lower() for w in ("time", "date", "hora", "fecha")):
                state.stage = ConversationStage.COLLECT_TIMING
                intake.preferred_time_utc = None
            # Otherwise stay at CONFIRM_SUMMARY — AI will ask what to update

        elif state.stage == ConversationStage.CONFIRM_APPOINTMENT:
            intake.notes.append(f"appointment_decision: {text}")
            logger.info(
                "CONFIRM_APPOINTMENT: text=%r, affirmative=%s, negative=%s",
                text, _has_word(text, AFFIRMATIVE_WORDS), _has_word(text, NEGATIVE_WORDS),
            )
            if _has_word(text, AFFIRMATIVE_WORDS):
                state.stage = ConversationStage.SCHEDULED
                should_schedule = intake.appointment_id is None
            elif _has_word(text, NEGATIVE_WORDS):
                state.stage = ConversationStage.FOLLOW_UP
            # Otherwise stay — AI will re-ask

        # ---- Generate response ----

        # For CONFIRM_SUMMARY, inject the summary data so the AI can present it
        ai_context = text
        if state.stage == ConversationStage.CONFIRM_SUMMARY and intake.preferred_time_utc:
            summary = self.build_summary(state)
            ai_context = f"{text}\n\n[SYSTEM: Here is the intake summary to present to the patient:\n{summary}]"

        response = None
        if self._ai_responder:
            logger.info(
                "Calling AI responder for session %s, stage %s",
                state.session_id, state.stage,
            )
            response = await self._ai_responder.generate_response(
                ai_context, state.stage, intake, locale
            )
            if response:
                logger.info("AI response received (%d chars)", len(response))
            else:
                logger.warning("AI responder returned None for stage %s", state.stage)
        else:
            logger.warning("No AI responder available")

        if not response:
            logger.info("Using fallback response for stage %s", state.stage)
            response = self._fallback_response(state.stage, state)

        # Track message history
        intake.add_message("user", text)
        intake.add_message("assistant", response)

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
