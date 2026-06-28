"""
services/cue/calendar_dav.py
------------------------------
Cue CalDAV client over the doctor's OWN SOGo calendar (HANDS-03 / HANDS-10).

READ increment (Plan 23-02):
  - read_day(...)    : list the events on a date, with the X-CUE-MANAGED flag.
  - get_calendar(...): resolve the physician's "personal" collection by probing
                       principal().calendars() (HANDS-10 — never a hardcoded slug).

WRITE increment (Plan 23-04 — REAL):
  - block_time(...)  : create a busy VEVENT tagged X-CUE-MANAGED (UTC), return uid.
  - clear_range(...) : delete ONLY X-CUE-MANAGED events in range; doctor-authored
                       (untagged) events are NEVER deleted; return {deleted, skipped}.
  The SOLE caller is the route-level POST /cue/calendar/confirm-write (OUTSIDE the
  model loop, after the doctor clicks Confirm — D-03). The model tools are PURE
  PROPOSERS and never reach these functions.

Credential discipline: the CalDAV client is built PER REQUEST from the Cue
app-password (username/password) handed out by credential_broker.get_cue_cred.
The credential is NEVER stored at module level (it is physician-specific and
short-lived). All times are handled in UTC (HANDS-03).

23-PROBE-FINDINGS (live 2026-06-23):
  - Principal connect URL : https://practikah.medikah.health/SOGo/dav/<local_part>/
  - Resolved collection   : .../SOGo/dav/<full_email>/Calendar/personal/
  - The `caldav` lib follows the principal home-set, so we resolve the calendar
    via principal().calendars() and prefer the collection whose URL contains
    'personal' (with a documented fallback to the first calendar).
  - X-CUE-MANAGED survives the SOGo CalDAV round-trip intact (A7 PASS) — it is
    the sole blast-radius guard for clear_range in Plan 23-04.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timezone, tzinfo
from zoneinfo import ZoneInfo
from typing import Any, Optional

# Launch market default. SOGo day boundaries are the physician's LOCAL day; an
# evening event in America/Mexico_City rolls into the next UTC day, so a naive
# UTC day window would miss it. We build the window in this zone, then convert.
_DEFAULT_CAL_TIMEZONE = "America/Mexico_City"

logger = logging.getLogger(__name__)

# CalDAV base origin (HANDS-06 apex — never the fragile mail.medikah.health CNAME).
CALDAV_BASE = os.environ.get(
    "CALDAV_BASE", "https://practikah.medikah.health/SOGo/dav"
)

# Custom iCal property tag. The sole blast-radius guard that lets clear_range
# (Plan 23-04) distinguish Cue-authored events from doctor-authored events.
X_CUE_MANAGED = "X-CUE-MANAGED"


def _cue_client(username: str, password: str) -> Any:
    """Build a per-request authenticated CalDAV client (credential never module-level).

    Imported lazily so the module imports cleanly even if `caldav` is absent in
    a minimal test environment, and so tests can patch `caldav.DAVClient`.
    """
    import caldav

    return caldav.DAVClient(
        url=f"{CALDAV_BASE}/{username}/",
        username=username,
        password=password,  # the minted app-password — never persisted/logged
    )


def _resolve_calendar(client: Any) -> Any:
    """Resolve the physician's 'personal' calendar collection (HANDS-10).

    Probes principal().calendars() and prefers the collection whose URL contains
    'personal' (the probe-confirmed SOGo slug). Falls back to the first calendar
    if the slug differs (locale rename / custom collection) rather than
    hardcoding the path. Raises ValueError if no calendar exists.
    """
    principal = client.principal()
    calendars = principal.calendars()
    for cal in calendars:
        if "personal" in str(cal.url).lower():
            return cal
    if calendars:
        logger.warning(
            "[cue:caldav] no 'personal' collection found — falling back to first "
            "calendar (HANDS-10 slug differs); url=%s",
            str(calendars[0].url),
        )
        return calendars[0]
    raise ValueError("No CalDAV calendar collection found for this physician")


def _format_event_dt(value: Any, tz: tzinfo) -> str:
    """Render an iCal DTSTART/DTEND value in the physician's LOCAL timezone.

    SOGo stores events in UTC and caldav `expand=True` normalizes to UTC, so the
    raw value Cue would otherwise surface is a UTC wall-clock — a 9:00 AM
    America/Mexico_City event reads as 15:00. Handing the model that string made
    Cue report the wrong time (Dr. José report 2026-06-28 Issue 3). We convert to
    `tz` here so the model always receives the doctor's local time.

    - tz-aware datetime → converted to `tz`, rendered "YYYY-MM-DD HH:MM".
    - naive datetime    → assumed UTC (HANDS-03 storage rule), then converted.
    - all-day `date`    → "YYYY-MM-DD (all day)" (no time component).
    - None / other      → "".
    """
    if value is None:
        return ""
    # datetime is a subclass of date — test datetime first.
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return f"{value.isoformat()} (all day)"
    return str(value)


def _event_to_dict(event: Any, tz: tzinfo) -> dict:
    """Project a caldav event into a transient summary dict (LOCAL time).

    Returns summary/dtstart/dtend/uid plus the cue_managed bool derived from the
    X-CUE-MANAGED property. dtstart/dtend are rendered in the physician's local
    zone `tz` (not UTC) — see _format_event_dt. Bodies/attendees are never
    extracted.
    """
    comp = event.icalendar_component
    dtstart = comp.get("DTSTART")
    dtend = comp.get("DTEND")
    return {
        "summary": str(comp.get("SUMMARY", "")),
        "dtstart": _format_event_dt(dtstart.dt if dtstart is not None else None, tz),
        "dtend": _format_event_dt(dtend.dt if dtend is not None else None, tz),
        "uid": str(comp.get("UID", "")),
        "cue_managed": comp.get(X_CUE_MANAGED) is not None,
    }


async def read_day(
    username: str,
    password: str,
    date_str: str,
    tz_name: str = _DEFAULT_CAL_TIMEZONE,
) -> list[dict]:
    """List the physician's events on `date_str` (HANDS-03).

    Resolves the per-physician 'personal' collection (HANDS-10), searches the
    physician's LOCAL day window (converted to UTC for the query), and returns
    transient summary dicts including the cue_managed flag. Building the window
    in the local zone ensures evening events (which roll into the next UTC day)
    are included — the Aguirre 2026-06-27 calendar bug.
    """
    d = date.fromisoformat(date_str)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
    end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz).astimezone(timezone.utc)

    client = _cue_client(username, password)
    # caldav DAVClient supports the context-manager protocol; use it when present.
    # Events are rendered in `tz` (the physician's local zone) so Cue surfaces the
    # doctor's wall-clock, never the UTC value (Issue 3 fix).
    if hasattr(client, "__enter__"):
        with client as c:
            cal = _resolve_calendar(c)
            events = cal.search(start=start, end=end, event=True, expand=True)
            return [_event_to_dict(e, tz) for e in events]
    cal = _resolve_calendar(client)
    events = cal.search(start=start, end=end, event=True, expand=True)
    return [_event_to_dict(e, tz) for e in events]


# ---------------------------------------------------------------------------
# WRITE increment (Plan 23-04). The ONLY caller is the route-level
# POST /cue/calendar/confirm-write (OUTSIDE the model loop, after the doctor
# clicks Confirm — D-03). The in-loop model tools are PURE PROPOSERS and NEVER
# reach these functions. Credentials (username/password) are the per-request
# Cue app-password handed out by credential_broker.get_cue_cred — never stored
# at module level, never logged. All times are stored in UTC (HANDS-03).
# ---------------------------------------------------------------------------


def _to_utc(iso: str) -> datetime:
    """Parse an ISO 8601 string and convert to a tz-aware UTC datetime.

    A naive datetime (no offset) is assumed to already be UTC. Storage is always
    UTC (HANDS-03); the surface renders in the physician's local timezone.
    """
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_cue_managed(comp: Any) -> bool:
    """True iff the iCal component carries the X-CUE-MANAGED tag.

    This is the SOLE blast-radius guard (HANDS-03 / RESEARCH Pitfall 4): only an
    event Cue itself authored (and tagged) may be deleted by clear_range. A
    doctor-authored event has no such property and must NEVER reach delete().
    """
    return comp.get(X_CUE_MANAGED) is not None


def _build_block_calendar(uid: str, start_iso: str, end_iso: str, title: str) -> str:
    """Serialize a single X-CUE-MANAGED VEVENT to an iCal string (UTC).

    Uses the icalendar library (never hand-rolled .ics concatenation) so line
    folding, escaping, and VEVENT structure are RFC-correct.
    """
    from icalendar import Calendar, Event, vText

    vcal = Calendar()
    vcal.add("prodid", "-//Medikah Cue//EN")
    vcal.add("version", "2.0")

    vevent = Event()
    vevent.add("uid", uid)
    vevent.add("summary", title)
    vevent.add("dtstart", _to_utc(start_iso))
    vevent.add("dtend", _to_utc(end_iso))
    # HANDS-03 data-safety tag — confirmed to survive the SOGo round-trip
    # (23-PROBE-FINDINGS §3, A7 PASS). This is what clear_range filters on.
    vevent.add(X_CUE_MANAGED, vText("true"))
    vcal.add_component(vevent)

    return vcal.to_ical().decode("utf-8")


async def block_time(
    username: str,
    password: str,
    start_iso: str,
    end_iso: str,
    title: str,
    *,
    physician_id: Optional[str] = None,
) -> str:
    """Create a busy VEVENT tagged X-CUE-MANAGED and return its UID (HANDS-03).

    Stored in UTC. The route (confirm-write) wraps the returned uid as
    { "blocked": true, "uid": <uid> }. physician_id is accepted only for
    log/trace correlation — it is NEVER part of the CalDAV identity (that is the
    username/password credential).
    """
    uid = f"cue-{uuid.uuid4()}"
    ical_str = _build_block_calendar(uid, start_iso, end_iso, title)

    client = _cue_client(username, password)
    if hasattr(client, "__enter__"):
        with client as c:
            cal = _resolve_calendar(c)
            cal.save_event(ical_str)
    else:
        cal = _resolve_calendar(client)
        cal.save_event(ical_str)

    logger.info(
        "[cue:caldav] block_time wrote uid=%s physician=%s", uid, physician_id
    )
    return uid


async def clear_range(
    username: str,
    password: str,
    start_iso: str,
    end_iso: str,
    *,
    physician_id: Optional[str] = None,
) -> dict:
    """Delete ONLY X-CUE-MANAGED events in [start,end] (HANDS-03 blast-radius).

    Returns EXACTLY {"deleted": <int>, "skipped": <int>}. A doctor-authored
    (untagged) event is NEVER passed to delete() under any code path — the
    delete() call is reachable only inside the `if _is_cue_managed(...)` branch.
    A range with zero Cue-managed events deletes nothing and returns
    {"deleted": 0, "skipped": <N>}.
    """
    start = _to_utc(start_iso)
    end = _to_utc(end_iso)

    def _sweep(cal: Any) -> dict:
        deleted = 0
        skipped = 0
        events = cal.search(start=start, end=end, event=True)
        for event in events:
            comp = event.icalendar_component
            if _is_cue_managed(comp):
                # Reachable ONLY for Cue-tagged events (blast-radius guard).
                event.delete()
                deleted += 1
            else:
                # Doctor-authored event — left untouched, counted as skipped.
                skipped += 1
        return {"deleted": deleted, "skipped": skipped}

    client = _cue_client(username, password)
    if hasattr(client, "__enter__"):
        with client as c:
            cal = _resolve_calendar(c)
            result = _sweep(cal)
    else:
        cal = _resolve_calendar(client)
        result = _sweep(cal)

    logger.info(
        "[cue:caldav] clear_range deleted=%d skipped=%d physician=%s",
        result["deleted"],
        result["skipped"],
        physician_id,
    )
    return result
