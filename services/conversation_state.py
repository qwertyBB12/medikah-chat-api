"""Conversation state tracking for the intake triage flow."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Dict, List, Optional

from db.client import get_supabase

logger = logging.getLogger(__name__)

SESSION_TTL_MINUTES = 90


class ConversationStage(str, Enum):
    """Lifecycle stages for the intake conversation."""

    WELCOME = "welcome"
    COLLECT_NAME = "collect_name"
    COLLECT_EMAIL = "collect_email"
    COLLECT_SYMPTOMS = "collect_symptoms"
    COLLECT_HISTORY = "collect_history"
    COLLECT_TIMING = "collect_timing"
    CONFIRM_SUMMARY = "confirm_summary"
    CONFIRM_APPOINTMENT = "confirm_appointment"
    SCHEDULED = "scheduled"
    FOLLOW_UP = "follow_up"
    COMPLETED = "completed"
    EMERGENCY_ESCALATED = "emergency_escalated"


@dataclass(slots=True)
class IntakeHistory:
    """Summary of the information we collect before scheduling."""

    patient_name: Optional[str] = None
    patient_email: Optional[str] = None
    symptom_overview: Optional[str] = None
    symptom_history: Optional[str] = None
    preferred_time_utc: Optional[datetime] = None
    locale_preference: Optional[str] = None
    emergency_flag: bool = False
    appointment_id: Optional[str] = None
    appointment_confirmed_at: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)
    education_shared: bool = False
    message_history: List[dict] = field(default_factory=list)

    def add_message(self, role: str, content: str, max_history: int = 20) -> None:
        """Append a message to conversation history, trimming to max length."""
        self.message_history.append({"role": role, "content": content})
        if len(self.message_history) > max_history:
            self.message_history = self.message_history[-max_history:]

    def summary_lines(self) -> List[str]:
        """Render the captured details for emails or logging."""
        lines: List[str] = []
        if self.patient_name:
            lines.append(f"Patient name: {self.patient_name}")
        if self.patient_email:
            lines.append(f"Contact email: {self.patient_email}")
        if self.symptom_overview:
            lines.append(f"Primary concern: {self.symptom_overview}")
        if self.symptom_history:
            lines.append(f"Symptom history: {self.symptom_history}")
        if self.preferred_time_utc:
            lines.append(
                f"Preferred time (UTC): {self.preferred_time_utc.isoformat()}"
            )
        if self.locale_preference:
            lines.append(f"Language preference: {self.locale_preference}")
        if self.emergency_flag:
            lines.append("Emergency escalation: patient advised to seek urgent care")
        return lines


@dataclass(slots=True)
class ConversationState:
    """Mutable per-session state for triage conversations."""

    session_id: str
    stage: ConversationStage
    created_at: datetime
    updated_at: datetime
    intake: IntakeHistory = field(default_factory=IntakeHistory)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class ConversationStateStore:
    """
    Conversation state store with Supabase persistence.
    Falls back to in-memory storage if Supabase is not configured.
    """

    def __init__(self, ttl_minutes: int = SESSION_TTL_MINUTES) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._lock = Lock()
        self._memory_store: Dict[str, ConversationState] = {}
        self._supabase = get_supabase()
        if self._supabase:
            logger.info("ConversationStateStore using Supabase persistence.")
        else:
            logger.info("ConversationStateStore using in-memory storage.")

    @property
    def _use_db(self) -> bool:
        return self._supabase is not None

    # ---- Supabase helpers ----

    def _state_to_row(self, state: ConversationState) -> dict:
        intake = state.intake
        return {
            "session_id": state.session_id,
            "stage": state.stage.value,
            "patient_name": intake.patient_name,
            "patient_email": intake.patient_email,
            "symptom_overview": intake.symptom_overview,
            "symptom_history": intake.symptom_history,
            "preferred_time_utc": intake.preferred_time_utc.isoformat() if intake.preferred_time_utc else None,
            "locale_preference": intake.locale_preference,
            "emergency_flag": intake.emergency_flag,
            "appointment_id": intake.appointment_id,
            "appointment_confirmed_at": intake.appointment_confirmed_at.isoformat() if intake.appointment_confirmed_at else None,
            "notes": intake.notes,
            "education_shared": intake.education_shared,
            "message_history": intake.message_history,
            "created_at": state.created_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
        }

    def _row_to_state(self, row: dict) -> ConversationState:
        preferred_time = None
        if row.get("preferred_time_utc"):
            preferred_time = datetime.fromisoformat(row["preferred_time_utc"])

        confirmed_at = None
        if row.get("appointment_confirmed_at"):
            confirmed_at = datetime.fromisoformat(row["appointment_confirmed_at"])

        notes = row.get("notes", "[]")
        if isinstance(notes, str):
            notes = json.loads(notes)

        intake = IntakeHistory(
            patient_name=row.get("patient_name"),
            patient_email=row.get("patient_email"),
            symptom_overview=row.get("symptom_overview"),
            symptom_history=row.get("symptom_history"),
            preferred_time_utc=preferred_time,
            locale_preference=row.get("locale_preference"),
            emergency_flag=row.get("emergency_flag", False),
            appointment_id=row.get("appointment_id"),
            appointment_confirmed_at=confirmed_at,
            notes=notes,
            education_shared=row.get("education_shared", False),
            message_history=row.get("message_history") or [],
        )

        return ConversationState(
            session_id=row["session_id"],
            stage=ConversationStage(row["stage"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            intake=intake,
        )

    # ---- In-memory helpers ----

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            session_id
            for session_id, state in self._memory_store.items()
            if now - state.updated_at > self._ttl
        ]
        for session_id in expired:
            self._memory_store.pop(session_id, None)

    # ---- Public API ----

    def get(self, session_id: str) -> Optional[ConversationState]:
        if self._use_db:
            try:
                result = (
                    self._supabase.table("conversation_sessions")
                    .select("*")
                    .eq("session_id", session_id)
                    .single()
                    .execute()
                )
                if result.data:
                    state = self._row_to_state(result.data)
                    # Check TTL
                    if datetime.now(timezone.utc) - state.updated_at > self._ttl:
                        return None
                    state.touch()
                    self._supabase.table("conversation_sessions").update(
                        {"updated_at": state.updated_at.isoformat()}
                    ).eq("session_id", session_id).execute()
                    return state
            except Exception:
                logger.exception("Failed to fetch session %s from Supabase", session_id)
            return None

        with self._lock:
            self._prune()
            state = self._memory_store.get(session_id)
            if state:
                state.touch()
            return state

    def create(self, session_id: Optional[str] = None) -> ConversationState:
        new_id = session_id or secrets.token_urlsafe(16)
        now = datetime.now(timezone.utc)
        state = ConversationState(
            session_id=new_id,
            stage=ConversationStage.WELCOME,
            created_at=now,
            updated_at=now,
        )

        if self._use_db:
            try:
                self._supabase.table("conversation_sessions").upsert(
                    self._state_to_row(state)
                ).execute()
            except Exception:
                logger.exception("Failed to create session in Supabase")
        else:
            with self._lock:
                self._prune()
                self._memory_store[new_id] = state

        return state

    def get_or_create(self, session_id: Optional[str]) -> ConversationState:
        if session_id:
            existing = self.get(session_id)
            if existing is not None:
                return existing
        return self.create(session_id=session_id)

    def update(self, state: ConversationState) -> ConversationState:
        state.touch()

        if self._use_db:
            try:
                self._supabase.table("conversation_sessions").upsert(
                    self._state_to_row(state)
                ).execute()
            except Exception:
                logger.exception("Failed to update session in Supabase")
        else:
            with self._lock:
                self._memory_store[state.session_id] = state

        return state

    def mark_completed(self, session_id: str) -> None:
        if self._use_db:
            try:
                self._supabase.table("conversation_sessions").update({
                    "stage": ConversationStage.COMPLETED.value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("session_id", session_id).execute()
            except Exception:
                logger.exception("Failed to mark session completed in Supabase")
            return

        with self._lock:
            existing = self._memory_store.get(session_id)
            if existing:
                existing.stage = ConversationStage.COMPLETED
                existing.touch()

    def snapshot(self, session_id: str) -> Optional[ConversationState]:
        if self._use_db:
            state = self.get(session_id)
            return state

        with self._lock:
            state = self._memory_store.get(session_id)
            if state:
                return replace(state)
            return None
