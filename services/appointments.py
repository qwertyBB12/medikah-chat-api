"""Secure appointment storage utilities."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional
from uuid import uuid4

from db.client import get_supabase

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppointmentRecord:
    """Immutable representation of a stored appointment."""

    appointment_id: str
    patient_name: str
    patient_contact_hash: str
    appointment_time: datetime

    def to_public_dict(self) -> Dict[str, str]:
        """Return non-sensitive fields for logging/debugging."""
        return {
            "appointment_id": self.appointment_id,
            "appointment_time": self.appointment_time.isoformat(),
        }


class SecureAppointmentStore:
    """
    Appointment store with Supabase persistence.
    Falls back to in-memory storage if Supabase is not configured.

    The contact field is HMAC-SHA256 hashed to avoid storing clear-text PII.
    """

    def __init__(self, secret_key: str) -> None:
        if not secret_key:
            raise ValueError("Secret key is required for SecureAppointmentStore")
        self._secret = secret_key.encode("utf-8")
        self._lock = Lock()
        self._memory_store: Dict[str, AppointmentRecord] = {}
        self._supabase = get_supabase()
        if self._supabase:
            logger.info("SecureAppointmentStore using Supabase persistence.")
        else:
            logger.info("SecureAppointmentStore using in-memory storage.")

    @property
    def _use_db(self) -> bool:
        return self._supabase is not None

    def save(
        self, *, patient_name: str, patient_contact: str, appointment_time: datetime
    ) -> AppointmentRecord:
        """Persist a new appointment record with hashed contact details."""
        appointment_time = appointment_time.astimezone(timezone.utc)
        appointment_id = uuid4().hex
        hashed_contact = hmac.new(
            self._secret, patient_contact.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        record = AppointmentRecord(
            appointment_id=appointment_id,
            patient_name=patient_name,
            patient_contact_hash=hashed_contact,
            appointment_time=appointment_time,
        )

        if self._use_db:
            try:
                self._supabase.table("appointments").insert({
                    "appointment_id": record.appointment_id,
                    "patient_name": record.patient_name,
                    "patient_contact_hash": record.patient_contact_hash,
                    "appointment_time": record.appointment_time.isoformat(),
                }).execute()
            except Exception:
                logger.exception("Failed to store appointment in Supabase")
                raise
        else:
            with self._lock:
                self._memory_store[appointment_id] = record

        logger.info("Appointment stored: %s", record.to_public_dict())
        return record

    def get(self, appointment_id: str) -> Optional[AppointmentRecord]:
        """Retrieve an appointment by its identifier."""
        if self._use_db:
            try:
                result = (
                    self._supabase.table("appointments")
                    .select("*")
                    .eq("appointment_id", appointment_id)
                    .single()
                    .execute()
                )
                if result.data:
                    row = result.data
                    return AppointmentRecord(
                        appointment_id=row["appointment_id"],
                        patient_name=row["patient_name"],
                        patient_contact_hash=row["patient_contact_hash"],
                        appointment_time=datetime.fromisoformat(row["appointment_time"]),
                    )
            except Exception:
                logger.exception("Failed to fetch appointment from Supabase")
            return None

        with self._lock:
            return self._memory_store.get(appointment_id)
