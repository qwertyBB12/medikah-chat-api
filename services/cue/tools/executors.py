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
  - availability_read  (HANDS-03):    reads physician_availability.
  - inquiry_list_recent(HANDS-04):    reads patient_inquiries (first name only).

  - appointment_list   (appointments vertical): reads physician_appointments.
  - appointment_create/move/cancel: PURE PROPOSERS over the same table (see the
                                    appointment section near the bottom).

The last two read Medikah's OWN tables through services/physician_dashboard.py
and mint no credential, so the verified-gate below does NOT apply to them (same
posture as clinical_decision_support). They still write a per-action audit row.

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

from services.cue.clinical_support import generate_clinical_support

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


def _read_unavailable_message() -> str:
    """Bilingual 'could not read that right now' message (no PHI, no internals).

    Returned by the Medikah-table read executors when the DB is unavailable or a
    read fails. These executors relay the gap honestly rather than raising: a
    raised executor becomes an is_error tool_result the model has to guess at,
    and the grounding spine already tells Cue to report tool emptiness plainly.
    """
    return (
        "No pude leer esa información ahora mismo. Vuelve a intentarlo en un "
        "momento. / I could not read that information right now. Please try "
        "again in a moment."
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
# availability_read executor (Phase 23 HANDS-03 — REAL)
# ---------------------------------------------------------------------------


async def availability_read(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
) -> str:
    """Return the physician's weekly availability grid (HANDS-03).

    Reads physician_availability through services.physician_dashboard, scoped to
    the session-derived physician_id (CUE-11).

    NO verified-gate: unlike calendar_read_day / inbox_read_recent, this reads
    Medikah's OWN table and never mints a Mailcow credential, so it matches the
    clinical_decision_support posture — any authenticated physician may use it.

    physician_id is sourced exclusively from dispatch_tool() (session-derived).
    No functional args accepted from tool_input for this tool.
    """
    logger.debug("[cue:tools] availability_read: physician=%s", physician_id)

    from services.physician_dashboard import get_physician_availability

    try:
        availability = get_physician_availability(physician_id)
    except Exception:
        logger.exception(
            "[cue:tools] availability_read failed physician=%s", physician_id
        )
        return _read_unavailable_message()

    # Only days the doctor actually offers: a disabled or slotless day is noise
    # the model would otherwise read back as part of the grid.
    days = [
        d
        for d in (getattr(availability, "schedule", None) or [])
        if d.enabled and d.slots
    ]

    # Per-action audit — shape only, NO slot times, NO IP+UA (HANDS-08a).
    _write_action_audit(
        physician_id, "cue.availability_read", {"day_count": len(days)}
    )

    if not days:
        return (
            "Aún no has definido tu disponibilidad semanal. / "
            "You have not set your weekly availability yet."
        )

    tz = getattr(availability, "timezone", None) or "UTC"
    lines = [f"Disponibilidad semanal / Weekly availability ({tz}):"]
    for day in days:
        slots = ", ".join(f"{s.start_time}–{s.end_time}" for s in day.slots)
        lines.append(f"- {day.day}: {slots}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# inquiry_list_recent executor (Phase 23 HANDS-04 — REAL)
# ---------------------------------------------------------------------------


async def inquiry_list_recent(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
    limit: int = 5,     # functional arg from tool_input, capped by dispatcher
) -> str:
    """Return the most recent patient inquiries for the physician (HANDS-04).

    Reads patient_inquiries through services.physician_dashboard (the same
    service backing GET /physicians/{id}/inquiries), scoped to the
    session-derived physician_id (CUE-11). Pure read — accept/decline stay on
    the dashboard, out of the model loop.

    NO verified-gate, for the same reason as availability_read: Medikah's own
    table, no Mailcow credential minted.

    PHI DISCIPLINE (registry contract): patient FIRST NAME only, plus status and
    date. Symptoms and patient email are deliberately NEVER placed in the tool
    result — they would land in the model context and, from there, in a
    transcript the doctor did not ask for.

    physician_id is sourced exclusively from dispatch_tool() (session-derived).
    'limit' is the ONLY functional arg accepted from tool_input (capped at 20
    by dispatch_tool before it arrives here).
    """
    logger.debug(
        "[cue:tools] inquiry_list_recent: physician=%s limit=%d",
        physician_id,
        limit,
    )

    from services.physician_dashboard import get_physician_inquiries

    try:
        # Lower-bound the page size too: dispatch_tool caps the top end, but a
        # model-supplied limit of 0 or a negative would make an empty page range.
        page = get_physician_inquiries(
            physician_id, page=1, page_size=max(1, limit)
        )
    except Exception:
        logger.exception(
            "[cue:tools] inquiry_list_recent failed physician=%s", physician_id
        )
        return _read_unavailable_message()

    # Per-action audit — counts only, NO names/symptoms, NO IP+UA (HANDS-08a).
    _write_action_audit(
        physician_id,
        "cue.inquiry_list_recent",
        {"limit": limit, "inquiry_count": len(page.items)},
    )

    if not page.items:
        return (
            "No tienes consultas de pacientes recientes. / "
            "You have no recent patient inquiries."
        )

    lines = [
        f"Consultas recientes / Recent inquiries "
        f"({len(page.items)} de {page.total} / {len(page.items)} of {page.total}):"
    ]
    for inq in page.items:
        when = inq.created_at.strftime("%Y-%m-%d") if inq.created_at else "(sin fecha / no date)"
        full_name = (inq.patient_name or "").strip()
        first_name = full_name.split()[0] if full_name else "(sin nombre / no name)"
        status = getattr(inq.status, "value", inq.status)
        lines.append(f"- {when} — {first_name} [{status}] (id: {inq.inquiry_id})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Appointment vertical — appointment_list (READ) + appointment_create/move/cancel
# (PURE PROPOSERS).
#
# Same D-03 two-component design as calendar_block_time/calendar_clear_range: the
# three write tools NEVER touch the DB or the calendar. Each returns ONLY a
# confirm-card payload; the mutation happens at POST /cue/appointments/confirm-write
# after the doctor clicks Confirm.
#
# The move/cancel proposers DO read the appointment row — a card that says
# "cancel the appointment" without naming which one is not a confirmation, it is
# a coin flip. Reading is not writing: there is still no mutation path here. That
# read is also the first IDOR/blast-radius check, so an appointment that is not
# the session physician's, or that Cue did not create, never even gets a card.
#
# NO verified-gate on any of these: like availability_read and inquiry_list_recent
# they read Medikah's OWN table and mint no Mailcow credential. The confirm-write
# route applies the verified-gate before it touches CalDAV.
# ---------------------------------------------------------------------------

_APPOINTMENTS_CONFIRM_ENDPOINT = "/cue/appointments/confirm-write"


def _appointment_not_found_message() -> str:
    """Bilingual 'no such appointment' line (no PHI, no ids echoed back)."""
    return (
        "No encontré esa cita en tu agenda. / "
        "I could not find that appointment on your schedule."
    )


def _appointment_not_cue_managed_message() -> str:
    """Bilingual refusal for an appointment Cue did not create.

    The DB-side half of the blast-radius rule (the X-CUE-MANAGED tag is the
    calendar half): Cue moves and cancels only what Cue created.
    """
    return (
        "Esa cita no la creó Cue, así que no puedo moverla ni cancelarla. "
        "Puedes hacerlo desde tu panel. / "
        "That appointment was not created by Cue, so I cannot move or cancel it. "
        "You can do that from your dashboard."
    )


def _render_local(ts: Optional[str], tz_name: str) -> str:
    """Render a stored UTC timestamp in the physician's local zone ('YYYY-MM-DD HH:MM').

    Appointments are stored in UTC; the doctor thinks in their own wall-clock.
    Handing the model the UTC string is the same bug that made Cue misreport
    calendar times (Issue 3, 2026-06-28). Falls back to the raw value rather than
    raising — a rendering hiccup must not take down the whole listing.
    """
    if not ts:
        return ""
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo

    try:
        parsed = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_tz.utc)
        return parsed.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _appointment_window_summary(start_iso: str, end_iso: str) -> str:
    """Human-readable window for a confirm card — same shape as _range_summary."""
    return _range_summary(start_iso, end_iso)


async def appointment_list(
    physician_id: str,  # session-derived (dispatcher kwarg) — never model-supplied
    limit: int = 10,    # functional arg from tool_input, capped by dispatcher
) -> str:
    """List the physician's upcoming appointments (soonest first).

    Reads physician_appointments scoped to the session-derived physician_id
    (CUE-11). Cancelled appointments are excluded; 'moved' ones are included
    (a moved appointment is still on the book).

    PHI DISCIPLINE: the stored patient_name is already minimized to first name +
    last initial, and patient_contact is never selected — so the model context
    gets a name fragment, a time, a status and an id, and nothing else.

    A DB failure returns the bilingual "could not read" line rather than raising:
    an is_error tool_result would leave the model guessing, and reporting an
    empty schedule during an outage would be a lie the doctor could act on.
    """
    logger.debug(
        "[cue:tools] appointment_list: physician=%s limit=%d", physician_id, limit
    )

    from services.cue.appointments_store import list_upcoming

    tz_name = resolve_physician_tz(physician_id)
    try:
        rows = list_upcoming(_get_db(), physician_id, limit=max(1, limit))
    except Exception:
        logger.exception(
            "[cue:tools] appointment_list failed physician=%s", physician_id
        )
        return _read_unavailable_message()

    # Per-action audit — count only, NO patient names, NO IP+UA (HANDS-08a).
    _write_action_audit(
        physician_id,
        "cue.appointment_list",
        {"limit": limit, "appointment_count": len(rows)},
    )

    if not rows:
        return (
            "No tienes citas próximas. / You have no upcoming appointments."
        )

    lines = [f"Próximas citas / Upcoming appointments ({len(rows)}):"]
    for row in rows:
        when = _render_local(row.get("starts_at"), tz_name)
        until = _render_local(row.get("ends_at"), tz_name)
        end_time = until.split(" ")[-1] if until else ""
        name = row.get("patient_name") or "(sin nombre / no name)"
        status = row.get("status", "scheduled")
        lines.append(
            f"- {when}–{end_time} — {name} [{status}] (id: {row.get('id')})"
        )
    return "\n".join(lines)


async def appointment_create(
    physician_id: str,   # session-derived (dispatcher kwarg) — never model-supplied
    patient_name: str,   # functional arg
    start_iso: str,      # functional arg (physician-LOCAL time)
    end_iso: str,        # functional arg (physician-LOCAL time)
    locale: str = "es",  # session-derived (dispatcher kwarg)
) -> str:
    """PROPOSE a new appointment (D-03). NEVER writes — returns a confirm card only.

    The patient name is minimized to first name + last initial HERE, before it
    reaches the card, so the surface, the route, and the DB all see the same
    minimal form and there is no path by which a full legal name gets stored.

    There is deliberately NO patient_contact argument: the column exists for the
    later notification build, but accepting an email or phone through a model
    tool would put a raw identifier in the model's context for no present gain.
    The confirm-write route accepts it, so a non-model surface can supply it.
    """
    import json

    from services.cue.appointments_store import minimize_patient_name

    display_name = minimize_patient_name(patient_name)
    rng = _appointment_window_summary(start_iso, end_iso)
    if locale == "es":
        summary = f"¿Agendar cita con {display_name} el {rng}?"
    else:
        summary = f"Schedule an appointment with {display_name} on {rng}?"
    payload = {
        "kind": "confirm",
        "action": "appointment_create",
        # Tells the surface which confirm endpoint to POST to. Absent on the
        # older calendar cards, which keep going to /cue/calendar/confirm-write.
        "endpoint": _APPOINTMENTS_CONFIRM_ENDPOINT,
        "title": display_name,
        "summary": summary,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "patient_name": display_name,
    }
    return json.dumps(payload)


async def appointment_move(
    physician_id: str,    # session-derived (dispatcher kwarg) — never model-supplied
    appointment_id: str,  # functional arg
    start_iso: str,       # functional arg (new start, physician-LOCAL time)
    end_iso: str,         # functional arg (new end, physician-LOCAL time)
    locale: str = "es",   # session-derived (dispatcher kwarg)
) -> str:
    """PROPOSE moving an appointment (D-03). NEVER writes — confirm card only.

    Reads the appointment first (scoped to physician_id) so the card can name the
    patient and the time it is moving FROM. A row that is not this physician's,
    or that Cue did not create, returns a plain refusal line instead of a card —
    the proposal never even gets offered.
    """
    import json

    from services.cue.appointments_store import get_appointment

    tz_name = resolve_physician_tz(physician_id)
    try:
        row = get_appointment(_get_db(), physician_id, appointment_id)
    except Exception:
        logger.exception(
            "[cue:tools] appointment_move lookup failed physician=%s", physician_id
        )
        return _read_unavailable_message()

    if row is None or row.get("status") == "cancelled":
        return _appointment_not_found_message()
    if row.get("source") != "cue":
        return _appointment_not_cue_managed_message()

    name = row.get("patient_name") or ""
    was = _render_local(row.get("starts_at"), tz_name)
    rng = _appointment_window_summary(start_iso, end_iso)
    if locale == "es":
        summary = f"¿Mover la cita con {name} del {was} al {rng}?"
    else:
        summary = f"Move the appointment with {name} from {was} to {rng}?"
    payload = {
        "kind": "confirm",
        "action": "appointment_move",
        "endpoint": _APPOINTMENTS_CONFIRM_ENDPOINT,
        "title": name,
        "summary": summary,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "appointment_id": appointment_id,
    }
    return json.dumps(payload)


async def appointment_cancel(
    physician_id: str,    # session-derived (dispatcher kwarg) — never model-supplied
    appointment_id: str,  # functional arg
    locale: str = "es",   # session-derived (dispatcher kwarg)
) -> str:
    """PROPOSE cancelling an appointment (D-03). NEVER writes — confirm card only.

    Same scoped read + cue-managed check as appointment_move. The card carries
    the appointment's CURRENT window so the surface can show the doctor exactly
    what is about to disappear.
    """
    import json

    from services.cue.appointments_store import get_appointment

    tz_name = resolve_physician_tz(physician_id)
    try:
        row = get_appointment(_get_db(), physician_id, appointment_id)
    except Exception:
        logger.exception(
            "[cue:tools] appointment_cancel lookup failed physician=%s", physician_id
        )
        return _read_unavailable_message()

    if row is None or row.get("status") == "cancelled":
        return _appointment_not_found_message()
    if row.get("source") != "cue":
        return _appointment_not_cue_managed_message()

    name = row.get("patient_name") or ""
    when = _render_local(row.get("starts_at"), tz_name)
    if locale == "es":
        summary = f"¿Cancelar la cita con {name} del {when}?"
    else:
        summary = f"Cancel the appointment with {name} on {when}?"
    payload = {
        "kind": "confirm",
        "action": "appointment_cancel",
        "endpoint": _APPOINTMENTS_CONFIRM_ENDPOINT,
        "title": name,
        "summary": summary,
        "start_iso": row.get("starts_at") or "",
        "end_iso": row.get("ends_at") or "",
        "appointment_id": appointment_id,
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# clinical_decision_support executor (Phase 24 — Cue clinical support surface)
#
# NAMING / LEGAL (Hector, 2026-06-29): a doctor-support tool. NOTHING here is named
# or framed as an "(official) diagnosis" — it returns ranked clinical CONSIDERATIONS
# for the physician to weigh. The only "diagnosis" token is the disclaimer's denial.
# ---------------------------------------------------------------------------


async def clinical_decision_support(
    physician_id: str,                 # session-derived (dispatcher kwarg) — never model-supplied
    presentation: str,                 # functional arg: DE-IDENTIFIED clinical presentation
    age_range: Optional[str] = None,   # functional arg
    sex: Optional[str] = None,         # functional arg
) -> str:
    """Generate ranked clinical considerations from a DE-IDENTIFIED presentation (Phase 24).

    Returns a {kind:'clinical_support', considerations, red_flags, disclaimer, summary}
    JSON card payload. The engine surfaces the structured card to the UI AND feeds the
    readable `summary` prose back to the model so Cue narrates a walkthrough and the
    doctor can keep conversing about it — the loop CONTINUES (this is NOT a terminal
    confirm card).

    No verified-gate: this is a stateless LLM call (it never mints a Mailcow
    credential like the hands executors), so any authenticated physician may use it
    — matching the legacy clinical-support endpoint's auth posture.

    Stateless / no-PHI: the presentation is never logged or stored. The per-action
    audit row records the action + consideration count ONLY (never the presentation,
    never IP+UA — in-loop executor, HANDS-08a scoping).
    """
    import json

    result = await generate_clinical_support(presentation, age_range=age_range, sex=sex)

    _write_action_audit(
        physician_id,
        "cue.clinical_decision_support",
        {"consideration_count": len(result.get("considerations", []))},
    )

    payload = {
        "kind": "clinical_support",
        "considerations": result["considerations"],
        "red_flags": result["red_flags"],
        "disclaimer": result["disclaimer"],
        "summary": result["summary"],
    }
    return json.dumps(payload)
