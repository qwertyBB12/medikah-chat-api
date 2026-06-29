"""Startup guardrail: verify required DB schema actually exists in prod.

Backend migrations in ``db/migrations/`` are applied by hand and are NOT wired
into any deploy step (see the 2026-06-28 incident: migrations 002 and 003 had
silently never reached prod, so the physician availability Save 500'd into a
missing ``physician_availability`` table and appointments couldn't link to a
physician). This module turns that class of silent gap into a loud signal at
boot and a field on ``/health``.

It probes each required table by selecting a representative column with
``limit(1)`` through the Supabase (PostgREST) client — chosen so one probe
covers both the table AND the migration that added that column:

    conversation_sessions.patient_timezone -> migration 004
    appointments.physician_id              -> migration 003
    patient_inquiries.id                   -> migration 002
    physician_availability.physician_id    -> migration 002

The check is non-fatal: a missing table is logged at CRITICAL and reported on
``/health`` so the rest of the API keeps serving. Extend ``REQUIRED_SCHEMA`` as
new backend-owned tables/columns are added.
"""

from __future__ import annotations

import logging

from db.client import get_supabase

logger = logging.getLogger(__name__)

# table -> representative column that must be queryable. The column is picked to
# also assert the migration that introduced it (see module docstring).
REQUIRED_SCHEMA: dict[str, str] = {
    "physicians": "id",
    "conversation_sessions": "patient_timezone",
    "appointments": "physician_id",
    "patient_inquiries": "id",
    "physician_availability": "physician_id",
}


def check_schema() -> dict:
    """Probe required tables/columns. Returns a status dict for logging + /health.

    Shape::

        {"ok": bool, "checked": bool, "problems": [str, ...]}

    ``checked`` is False when no DB is configured (dev / in-memory mode), so the
    caller can tell "all good" apart from "couldn't look".
    """
    db = get_supabase()
    if db is None:
        return {"ok": True, "checked": False, "problems": []}

    problems: list[str] = []
    for table, column in REQUIRED_SCHEMA.items():
        try:
            db.table(table).select(column).limit(1).execute()
        except Exception as exc:  # PostgREST raises on missing relation/column
            reason = str(exc).replace("\n", " ").strip()
            problems.append(f"{table}.{column} not queryable: {reason[:200]}")

    return {"ok": not problems, "checked": True, "problems": problems}


def log_schema_status(status: dict) -> None:
    """Emit the schema-check result at the appropriate level."""
    if not status.get("checked"):
        logger.info("[schema-check] skipped — no database configured")
        return
    if status.get("ok"):
        logger.info(
            "[schema-check] OK — all %d required tables/columns present",
            len(REQUIRED_SCHEMA),
        )
        return
    for problem in status.get("problems", []):
        logger.critical("[schema-check] MISSING: %s", problem)
    logger.critical(
        "[schema-check] %d required schema object(s) missing — apply pending "
        "migrations in medikah-chat-api/db/migrations/ to prod",
        len(status.get("problems", [])),
    )
