"""
services/cue/tools/executors.py
---------------------------------
Cue tool executors (CUE-03 contract / CUE-11 IDOR discipline).

Phase 22 shipped no-op stubs. Phase 23 (Plan 23-02 — READ increment) makes the
hands executors real:

  - calendar_read_day  (HANDS-03/04): reads the doctor's OWN SOGo calendar via
                                      CalDAV, backed by a lazily-minted, no-send,
                                      kill-switch-gated Cue credential.
  - inbox_read_recent  (HANDS-02/04): reads recent inbox HEADERS read-only via
                                      IMAP (mark_seen=False).

Plan 23-04 (WRITE increment) makes calendar_block_time / calendar_clear_range
PURE PROPOSERS: each ALWAYS returns ONLY a confirm-card payload (json.dumps
STRING) and NEVER writes. The actual mutation happens at the route-level
POST /cue/calendar/confirm-write, OUTSIDE the model loop, after the doctor
clicks Confirm (D-03). The model tool has no write path at all.

CUE-11 IDOR DISCIPLINE — MANDATORY FOR ALL EXECUTORS
------------------------------------------------------
Every executor:
  - Accepts physician_id ONLY as an explicit keyword argument from dispatch_tool()
    (which sources it from the verified FastAPI session, auth.physician_id).
  - Does NOT accept an identity key (physician_id / slug) anywhere in its
    model-supplied keyword arguments.
  - NEVER reads an identity key from the model input dict — dispatch_tool's
    _safe_tool_input strips identity keys defence-in-depth before unpacking.

VERIFIED-GATE (Plan 23-02 gate resolution)
-------------------------------------------
The reasoning surface (/cue/chat) stays on authenticated_physician (pending
physicians can chat). But the HANDS executors mint/use a real Mailcow credential,
so they mint ONLY when verification_status == 'verified' AND mailbox_local_part is
set. A record with a mailbox_local_part but an unverified status must NOT mint —
the executor returns the bilingual "connect workspace" message (NO 403, NO mint).

PER-ACTION AUDIT (HANDS-08a scoping)
------------------------------------
Each hands action writes a workspace_audit_log row {physician_id, action, range}
with NO bodies/secrets. These in-loop read executors have NO Request object, so
they CANNOT and DO NOT capture IP+UA — per-action IP+UA is captured only at the
ROUTE-level actions (revoke in 23-04; confirm-write in 23-04). Read-action rows
OMIT IP+UA.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers (verified-gate workspace lookup + per-action audit)
# ---------------------------------------------------------------------------


def _get_db():
    """Service-role Supabase client (server-side only). May be None in dev."""
    from db.client import get_supabase

    return get_supabase()


def _connect_workspace_message() -> str:
    """Bilingual 'connect your workspace' message (no PHI, locale-agnostic).

    Returned when the physician is not verified OR has no mailbox_local_part.
    The surface renders EN/ES; STT auto-detect is honored downstream (VOICE-08).
    """
    return (
        "Conecta tu espacio de trabajo de Medikah para que Cue pueda leer tu "
        "calendario y bandeja. / Connect your Medikah workspace to let Cue read "
        "your calendar and inbox."
    )


def _load_workspace_context(physician_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (mailbox_local_part, verification_status) for the session physician.

    mailbox_local_part comes from physician_workspace_accounts; verification_status
    comes from physicians. Both are read with the service-role client, scoped to
    the session-derived physician_id (CUE-11 — sourced from the dispatcher kwarg,
    never from the model input dict).
    Returns (None, None) when the DB is unavailable.
    """
    db = _get_db()
    if db is None:
        return None, None

    mailbox_local_part: Optional[str] = None
    verification_status: Optional[str] = None

    try:
        ws = (
            db.table("physician_workspace_accounts")
            .select("mailbox_local_part")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        rows = getattr(ws, "data", None) or []
        if rows:
            mailbox_local_part = rows[0].get("mailbox_local_part")
    except Exception:
        logger.exception(
            "[cue:tools] workspace lookup failed physician=%s", physician_id
        )

    try:
        ph = (
            db.table("physicians")
            .select("verification_status")
            .eq("id", physician_id)
            .limit(1)
            .execute()
        )
        prows = getattr(ph, "data", None) or []
        if prows:
            verification_status = prows[0].get("verification_status")
    except Exception:
        logger.exception(
            "[cue:tools] verification_status lookup failed physician=%s", physician_id
        )

    return mailbox_local_part, verification_status


def _write_action_audit(physician_id: str, action: str, detail: dict) -> None:
    """Best-effort per-action audit row (HANDS-08a).

    Writes {physician_id, action, detail(range only)} — NO bodies, NO secrets,
    and NO IP+UA (in-loop read executors have no Request; HANDS-08a scoping).
    """
    db = _get_db()
    if db is None:
        return
    try:
        db.table("workspace_audit_log").insert(
            {
                "physician_id": physician_id,
                "actor_id": physician_id,
                "actor_role": "physician",
                "action": action,
                "resource_type": "cue_hands",
                "resource_id": None,
                "detail": detail,  # range/action only — never bodies/secrets, never IP+UA
            }
        ).execute()
    except Exception:
        logger.exception(
            "[cue:tools] action audit insert failed action=%s physician=%s (non-fatal)",
            action,
            physician_id,
        )


# ---------------------------------------------------------------------------
# Physician scheduling timezone (HANDS-03 — diagnosis 2026-06-28)
# ---------------------------------------------------------------------------

_DEFAULT_PHYSICIAN_TZ = "America/Mexico_City"


def resolve_physician_tz(physician_id: str) -> str:
    """The physician's IANA scheduling timezone, with a Mexico City fallback.

    Source of truth = physician_availability.timezone (their practice zone).
    That column defaults to 'UTC' on rows that never set it, so 'UTC' is treated
    as UNSET (→ fallback) — no LatAm doctor schedules in UTC, and storing local
    blocks as if UTC was the booking-time bug. Threads a real per-doctor zone
    into the date directive + calendar read/write so 'today/tomorrow' and block
    times resolve in the doctor's zone (hemispheric scope), not a hardcoded
    constant. Never raises (fail-safe to the Mexico City default).
    """
    try:
        from zoneinfo import ZoneInfo
        from services.physician_dashboard import get_physician_availability

        av = get_physician_availability(physician_id)
        tz = (getattr(av, "timezone", None) or "").strip()
        if tz and tz.upper() != "UTC":
            ZoneInfo(tz)  # validate; unknown zone raises → fallback
            return tz
    except Exception:
        logger.debug("[cue:tools] tz resolve fell back for physician=%s", physician_id)
    return _DEFAULT_PHYSICIAN_TZ


# ---------------------------------------------------------------------------
# calendar_read_day executor (Phase 23 HANDS-03/04 — REAL)
# ---------------------------------------------------------------------------


async def calendar_read_day(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
    date: str,          # functional arg from tool_input only
) -> str:
    """Read the physician's OWN calendar for `date` via CalDAV (HANDS-03/04).

    Verified-gate: only a physician with verification_status == 'verified' AND a
    mailbox_local_part proceeds; otherwise returns the bilingual "connect
    workspace" message (NO 403, NO mint). Fetches the Cue credential (lazy mint,
    kill-switch-gated), reads the day, writes a per-action audit row (range only,
    no IP+UA), and returns a structured summary.
    """
    logger.debug(
        "[cue:tools] calendar_read_day: physician=%s date=%s", physician_id, date
    )

    mailbox_local_part, verification_status = _load_workspace_context(physician_id)
    if verification_status != "verified" or not mailbox_local_part:
        return _connect_workspace_message()

    from services.cue.credential_broker import get_cue_cred
    from services.cue import calendar_dav

    cred = await get_cue_cred(physician_id, mailbox_local_part)
    events = await calendar_dav.read_day(
        cred.username, cred.password, date, tz_name=resolve_physician_tz(physician_id)
    )

    # Per-action audit — range/action only, NO IP+UA (no Request here; HANDS-08a).
    _write_action_audit(
        physician_id,
        "cue.calendar_read_day",
        {"date": date, "event_count": len(events)},
    )

    if not events:
        return (
            f"No hay eventos en tu calendario para {date}. / "
            f"You have no calendar events on {date}."
        )

    lines = [f"{date}:"]
    for ev in events:
        summary = ev.get("summary") or "(sin título / untitled)"
        start = ev.get("dtstart", "")
        end = ev.get("dtend", "")
        tag = " [Cue]" if ev.get("cue_managed") else ""
        lines.append(f"- {start} → {end}: {summary}{tag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# inbox_read_recent executor (Phase 23 HANDS-02/04 — REAL)
# ---------------------------------------------------------------------------


async def inbox_read_recent(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
    limit: int = 10,    # functional arg from tool_input, capped by dispatcher
) -> str:
    """Read recent inbox HEADERS read-only via IMAP (HANDS-02/04).

    Same verified-gate as calendar_read_day. Reads headers only (mark_seen=False),
    bodies transient/never persisted, writes a per-action audit row (count only,
    no IP+UA), and returns a structured bilingual summary.
    """
    logger.debug(
        "[cue:tools] inbox_read_recent: physician=%s limit=%d", physician_id, limit
    )

    mailbox_local_part, verification_status = _load_workspace_context(physician_id)
    if verification_status != "verified" or not mailbox_local_part:
        return _connect_workspace_message()

    import asyncio

    from services.cue.credential_broker import get_cue_cred
    from services.cue import mail_reader

    cred = await get_cue_cred(physician_id, mailbox_local_part)
    # read_recent is synchronous (blocking imap-tools); offload to a worker
    # thread so the event loop is never blocked on the IMAP round-trip.
    messages = await asyncio.to_thread(
        mail_reader.read_recent, cred.username, cred.password, limit=limit
    )

    # Per-action audit — count only, NO bodies/secrets, NO IP+UA (HANDS-08a).
    _write_action_audit(
        physician_id,
        "cue.inbox_read_recent",
        {"limit": limit, "message_count": len(messages)},
    )

    if not messages:
        return (
            "No hay mensajes recientes en tu bandeja. / "
            "You have no recent inbox messages."
        )

    lines = ["Mensajes recientes / Recent messages:"]
    for m in messages:
        subject = m.get("subject") or "(sin asunto / no subject)"
        sender = m.get("from_", "")
        when = m.get("date", "")
        lines.append(f"- {when} — {sender}: {subject}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# calendar_block_time / calendar_clear_range executors (Plan 23-04 — WRITE)
#
# D-03 TWO-COMPONENT DESIGN — these are PURE PROPOSERS. They NEVER write.
# Each executor ALWAYS returns ONLY a confirm-card payload (json.dumps STRING in
# the tool_result content) and NEVER calls calendar_dav, NEVER writes an audit
# row, and has NO `confirmed` parameter and NO write branch. The actual mutation
# happens ONLY at the route-level POST /cue/calendar/confirm-write, OUTSIDE the
# model loop, after the doctor clicks Confirm. A single misheard/injected
# tool_use therefore CANNOT mutate the calendar.
#
# SERIALIZATION CONTRACT (pinned — producer/parser must agree): the read
# executors above return plain prose strings; THIS confirm payload is the only
# JSON-encoded tool_result. run_cue_turn json.loads the tool_result and detects
# kind=='confirm' to STOP the loop and surface pending_confirm.
# ---------------------------------------------------------------------------


def _range_summary(start_iso: str, end_iso: str) -> str:
    """Human-readable bilingual range string for the confirm card (no PHI).

    Best-effort: parses the ISO datetimes and formats a compact EN/ES range.
    Falls back to the raw ISO strings if parsing fails (never raises — a
    proposer must always produce a card).
    """
    from datetime import datetime

    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        same_day = s.date() == e.date()
        day = s.strftime("%Y-%m-%d")
        s_t = s.strftime("%H:%M")
        e_t = e.strftime("%H:%M")
        if same_day:
            return f"{day} {s_t}–{e_t}"
        return f"{day} {s_t} → {e.strftime('%Y-%m-%d')} {e_t}"
    except Exception:
        return f"{start_iso} → {end_iso}"


async def calendar_block_time(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
    start_iso: str,     # functional arg
    end_iso: str,       # functional arg
    title: str,         # functional arg
    locale: str = "es", # session-derived (dispatcher kwarg)
) -> str:
    """PROPOSE a calendar block (D-03). NEVER writes — returns a confirm card only.

    Returns ONLY the confirm-card payload as a JSON string. There is NO write
    branch and NO `confirmed` parameter: even a model that emits confirmed=true
    (stripped by _safe_tool_input anyway) cannot mutate the calendar from here.
    The route-level confirm-write endpoint is the sole mutation path.
    """
    import json

    rng = _range_summary(start_iso, end_iso)
    if locale == "es":
        summary = f"¿Bloquear {rng} «{title}»?"
    else:
        summary = f'Block {rng} "{title}"?'
    payload = {
        "kind": "confirm",
        "action": "block",
        "title": title,
        "summary": summary,
        "start_iso": start_iso,
        "end_iso": end_iso,
    }
    return json.dumps(payload)


async def calendar_clear_range(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
    start_iso: str,     # functional arg
    end_iso: str,       # functional arg
    locale: str = "es", # session-derived (dispatcher kwarg)
) -> str:
    """PROPOSE clearing Cue blocks in a range (D-03). NEVER writes — confirm card only.

    Returns ONLY the confirm-card payload as a JSON string. No write branch, no
    `confirmed` parameter. The route-level confirm-write endpoint performs the
    actual (X-CUE-MANAGED-guarded) delete only after the doctor clicks Confirm.
    """
    import json

    rng = _range_summary(start_iso, end_iso)
    if locale == "es":
        summary = f"¿Liberar los bloques de Cue en {rng}?"
    else:
        summary = f"Clear Cue blocks in {rng}?"
    payload = {
        "kind": "confirm",
        "action": "clear",
        "title": "",
        "summary": summary,
        "start_iso": start_iso,
        "end_iso": end_iso,
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# availability_read executor stub (Phase 22 — UNCHANGED; Phase 23 HANDS-03)
# ---------------------------------------------------------------------------


async def availability_read(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
) -> str:
    """
    Return the physician's weekly availability grid.

    Phase 22: no-op stub — returns a benign placeholder.
    Phase 23 HANDS-03: reads physician_availability scoped to physician_id.

    physician_id is sourced exclusively from dispatch_tool() (session-derived).
    No functional args accepted from tool_input for this tool.
    """
    logger.debug(
        "[cue:tools] availability_read stub: physician=%s", physician_id
    )
    # Phase 22 stub — real implementation wired in a later HANDS-03 increment.
    # Return a clean, jargon-free, bilingual "not connected yet" message: the
    # grounding spine tells Cue to relay tool emptiness honestly, and the model
    # may surface this string to the doctor (no internal phase markers).
    return (
        "La cuadrícula de disponibilidad aún no está conectada a tu espacio de "
        "trabajo. / Your availability grid isn't connected to your workspace yet."
    )


# ---------------------------------------------------------------------------
# inquiry_list_recent executor stub (Phase 22 — UNCHANGED; Phase 23 HANDS-04)
# ---------------------------------------------------------------------------


async def inquiry_list_recent(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
    limit: int = 5,     # functional arg from tool_input, capped by dispatcher
) -> str:
    """
    Return the most recent patient inquiries for the physician.

    Phase 22: no-op stub — returns a benign placeholder.
    Phase 23 HANDS-04: reads patient_inquiries scoped to physician_id.

    physician_id is sourced exclusively from dispatch_tool() (session-derived).
    'limit' is the ONLY functional arg accepted from tool_input (capped at 20
    by dispatch_tool before it arrives here).
    """
    logger.debug(
        "[cue:tools] inquiry_list_recent stub: physician=%s limit=%d",
        physician_id,
        limit,
    )
    # Phase 22 stub — real implementation wired in a later HANDS-04 increment.
    # Clean, jargon-free, bilingual "not connected yet" message (see
    # availability_read): the grounding spine has Cue relay this honestly rather
    # than inventing a count of pending inquiries.
    return (
        "La cola de consultas de pacientes aún no está conectada a tu espacio de "
        "trabajo. / Your patient-inquiry queue isn't connected to your workspace yet."
    )
