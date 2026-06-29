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
import os
import urllib.request

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


def notify_schema_problems(status: dict) -> None:
    """Push a one-shot phone alert via ntfy when schema objects are missing.

    Set ``NTFY_ALERT_URL`` in the environment (Render) to your ntfy topic URL,
    e.g. ``https://ntfy.sh/<your-unguessable-topic>`` — the same topic the
    mail-ops Kuma stack already pushes to. Optional ``NTFY_ALERT_TOKEN`` adds a
    Bearer token for reserved/self-hosted topics. No-op when schema is OK or the
    URL isn't configured. Uses stdlib urllib so it adds no dependency.
    """
    if not status.get("checked") or status.get("ok"):
        return
    url = os.getenv("NTFY_ALERT_URL")
    if not url:
        logger.warning(
            "[schema-check] schema is broken but NTFY_ALERT_URL is unset — "
            "no push alert sent (set it in Render to your ntfy topic URL)"
        )
        return

    problems = status.get("problems", [])
    body = (
        "Medikah API booted with MISSING DB schema — apply pending backend "
        "migrations to prod.\n\n" + "\n".join(problems)
    ).encode("utf-8")
    headers = {
        "Title": "Medikah API: schema-check FAILED",
        "Priority": "urgent",
        "Tags": "rotating_light,database",
    }
    token = os.getenv("NTFY_ALERT_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=8)  # noqa: S310 — operator-set URL
        logger.info("[schema-check] pushed schema-failure alert to ntfy")
    except Exception:
        logger.exception("[schema-check] failed to push ntfy alert")
