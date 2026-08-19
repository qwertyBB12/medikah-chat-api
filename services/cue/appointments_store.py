"""
services/cue/appointments_store.py
-----------------------------------
DB layer for physician_appointments — the appointment object behind Cue's
doctor-facing appointment vertical (migration 043).

Call style follows services/cue/memory/store.py and services/cue/gate.py:
  supabase.table(name).select(cols).eq(col, val).execute()  -> result.data

SCOPING (CUE-11): every function takes physician_id and filters on it. There is
no unscoped read or write in this module — an appointment id alone is never
enough to reach a row, so a model-supplied (or guessed) id cannot cross the
physician boundary.

FAILURE POSTURE — deliberately different from the memory store:
  memory/store.py fails OPEN (returns [] on error) because a missing memory note
  degrades a turn gracefully. An appointment cannot do that: "you have no
  appointments" when the DB is down is a lie the doctor would act on. So these
  functions RAISE AppointmentStoreError, and the callers decide:
    - appointment_list (read tool) catches it and returns the bilingual
      "could not read" line,
    - the confirm-write route lets it surface as a 500 rather than silently
      confirming an appointment that was never stored.

PHI DISCIPLINE:
  patient_name is minimized to FIRST NAME + LAST INITIAL by
  minimize_patient_name() before it is ever written, because appointment_list
  reads it back into the model's context. patient_contact is written verbatim
  (it is the later notification build's only input) and is never selected by any
  read in this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TABLE = "physician_appointments"

# Statuses that still represent a live appointment. 'moved' is a history marker
# (rescheduled at least once), NOT a terminal state — a moved appointment is
# still on the doctor's book and must keep showing up in their list.
ACTIVE_STATUSES = ("scheduled", "moved")

# The columns any read may select. patient_contact is absent ON PURPOSE: nothing
# in this build reads it, and leaving it out of the projection means it cannot
# leak into a tool result by accident when a new caller is added.
_READ_COLUMNS = (
    "id, patient_name, starts_at, ends_at, status, source, caldav_uid, "
    "needs_sync, created_at, updated_at"
)


class AppointmentStoreError(RuntimeError):
    """Raised when an appointment read/write cannot be completed.

    Callers translate this into a doctor-facing outcome (bilingual read failure
    line, or a 5xx on the write path) — it is never swallowed silently.
    """


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(res: Any) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def minimize_patient_name(raw: str) -> str:
    """Reduce a patient name to FIRST NAME + SURNAME INITIAL ('María González' → 'María G.').

    This is the single choke point for the table's PHI rule. It runs before the
    name reaches the DB, the confirm card, or the model — so even if the doctor
    dictates a full legal name, the stored and echoed form is minimal.

    The initial comes from the SECOND token, not the last. Mexican names run
    nombre + apellido paterno + apellido materno, so 'María González Torres' is
    a González — taking the last token would file her under her mother's family
    and produce a name her own doctor does not recognize.

    - 'María González Torres' → 'María G.'
    - 'Ana Ruiz'              → 'Ana R.'
    - 'María'                 → 'María'      (nothing to minimize)
    - 'María G.'              → 'María G.'   (already minimal; idempotent)
    - ''                      → ''           (the caller decides what to do)

    A compound given name ('María José García') reduces to 'María J.', which is
    less precise than ideal but never MORE revealing — for a PHI rule that is the
    correct direction to be wrong in.
    """
    parts = (raw or "").strip().split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0].upper()}."


def list_upcoming(
    supabase, physician_id: str, limit: int = 10, *, now: Optional[datetime] = None
) -> list[dict]:
    """Active appointments starting from `now`, soonest first (scoped to physician_id).

    Returns rows with status in ACTIVE_STATUSES only — a cancelled appointment is
    not on the doctor's book. Raises AppointmentStoreError if the read fails, so
    a DB outage is never reported to the doctor as an empty schedule.
    """
    if supabase is None:
        raise AppointmentStoreError("Supabase client unavailable")
    moment = now or _utc_now()
    try:
        res = (
            supabase.table(_TABLE)
            .select(_READ_COLUMNS)
            .eq("physician_id", physician_id)
            .in_("status", list(ACTIVE_STATUSES))
            .gte("starts_at", moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
            .order("starts_at", desc=False)
            .limit(limit)
            .execute()
        )
    except Exception as exc:
        logger.exception(
            "[cue:appointments] list_upcoming failed physician=%s", physician_id
        )
        raise AppointmentStoreError("appointment list read failed") from exc
    return _rows(res)


def get_appointment(supabase, physician_id: str, appointment_id: str) -> Optional[dict]:
    """One appointment by id, scoped to physician_id (CUE-11). None if not theirs.

    The physician_id filter is what makes an appointment id safe to accept from
    the model: another doctor's id simply returns no row.
    """
    if supabase is None:
        raise AppointmentStoreError("Supabase client unavailable")
    try:
        res = (
            supabase.table(_TABLE)
            .select(_READ_COLUMNS)
            .eq("physician_id", physician_id)
            .eq("id", appointment_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception(
            "[cue:appointments] get_appointment failed physician=%s", physician_id
        )
        raise AppointmentStoreError("appointment read failed") from exc
    rows = _rows(res)
    return rows[0] if rows else None


def insert_appointment(
    supabase,
    physician_id: str,
    *,
    patient_name: str,
    starts_at: str,
    ends_at: str,
    patient_contact: Optional[str] = None,
    source: str = "cue",
) -> dict:
    """Insert a scheduled appointment and return the stored row.

    starts_at/ends_at are UTC ISO strings (the caller converts the doctor's local
    time). patient_name is minimized here — callers cannot opt out.
    """
    if supabase is None:
        raise AppointmentStoreError("Supabase client unavailable")
    row = {
        "physician_id": physician_id,
        "patient_name": minimize_patient_name(patient_name),
        "patient_contact": patient_contact or None,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "status": "scheduled",
        "source": source,
        "needs_sync": False,
    }
    try:
        res = supabase.table(_TABLE).insert(row).execute()
    except Exception as exc:
        logger.exception(
            "[cue:appointments] insert failed physician=%s", physician_id
        )
        raise AppointmentStoreError("appointment insert failed") from exc
    rows = _rows(res)
    if not rows:
        raise AppointmentStoreError("appointment insert returned no row")
    return rows[0]


def _update(supabase, physician_id: str, appointment_id: str, patch: dict) -> dict:
    """Scoped UPDATE helper — always filters physician_id AND id."""
    if supabase is None:
        raise AppointmentStoreError("Supabase client unavailable")
    patch = dict(patch)
    patch["updated_at"] = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        res = (
            supabase.table(_TABLE)
            .update(patch)
            .eq("physician_id", physician_id)
            .eq("id", appointment_id)
            .execute()
        )
    except Exception as exc:
        logger.exception(
            "[cue:appointments] update failed physician=%s appointment=%s",
            physician_id,
            appointment_id,
        )
        raise AppointmentStoreError("appointment update failed") from exc
    rows = _rows(res)
    return rows[0] if rows else {}


def set_mirror(
    supabase,
    physician_id: str,
    appointment_id: str,
    *,
    caldav_uid: Optional[str],
    needs_sync: bool,
) -> dict:
    """Record the CalDAV mirror state for an appointment.

    caldav_uid=None + needs_sync=True is the "calendar write failed" state: the
    appointment exists and is authoritative, the calendar just has not caught up.
    """
    return _update(
        supabase,
        physician_id,
        appointment_id,
        {"caldav_uid": caldav_uid, "needs_sync": needs_sync},
    )


def apply_move(
    supabase,
    physician_id: str,
    appointment_id: str,
    *,
    starts_at: str,
    ends_at: str,
    previous_starts_at: Optional[str],
    previous_ends_at: Optional[str],
) -> dict:
    """Move an appointment to a new window, keeping the previous one as history.

    Sets status='moved' — still an ACTIVE status, so the appointment stays on the
    doctor's book and keeps appearing in appointment_list.
    """
    return _update(
        supabase,
        physician_id,
        appointment_id,
        {
            "starts_at": starts_at,
            "ends_at": ends_at,
            "previous_starts_at": previous_starts_at,
            "previous_ends_at": previous_ends_at,
            "status": "moved",
        },
    )


def mark_cancelled(supabase, physician_id: str, appointment_id: str) -> dict:
    """Terminal cancel — a status transition, never a row delete (the history stays)."""
    return _update(
        supabase,
        physician_id,
        appointment_id,
        {
            "status": "cancelled",
            "cancelled_at": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
