"""Secure appointment storage utilities."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Dict
from uuid import uuid4

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
            "patient_name": self.patient_name,
            "appointment_time": self.appointment_time.isoformat(),
        }


class SecureAppointmentStore:
    """
    In-memory store that keeps only minimal appointment metadata.

    The contact field is HMAC-SHA256 hashed to avoid storing clear-text PII,
    supporting compliance requirements by retaining only what is strictly needed.
    """

    def __init__(self, secret_key: str) -> None:
        if not secret_key:
            raise ValueError("Secret key is required for SecureAppointmentStore")
        self._secret = secret_key.encode("utf-8")
        self._records: Dict[str, AppointmentRecord] = {}
        self._lock = Lock()

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

        with self._lock:
            self._records[appointment_id] = record
        logger.info("Appointment stored: %s", record.to_public_dict())
        return record

    def get(self, appointment_id: str) -> AppointmentRecord | None:
        """Retrieve an appointment by its identifier."""
        with self._lock:
            return self._records.get(appointment_id)
