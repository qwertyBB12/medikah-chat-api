"""
services/cue/calendar_dav.py
------------------------------
Cue CalDAV client over the doctor's OWN SOGo calendar (HANDS-03 / HANDS-10).

READ increment (Plan 23-02):
  - read_day(...)    : list the events on a date, with the X-CUE-MANAGED flag.
  - get_calendar(...): resolve the physician's "personal" collection by probing
                       principal().calendars() (HANDS-10 — never a hardcoded slug).

WRITE increment (Plan 23-04 — STUBBED here so the module imports cleanly):
  - block_time(...)  : raise NotImplementedError("Plan 23-04")
  - clear_range(...) : raise NotImplementedError("Plan 23-04")

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
from datetime import date, datetime, timezone
from typing import Any

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


def _event_to_dict(event: Any) -> dict:
    """Project a caldav event into a transient summary dict (UTC).

    Returns summary/dtstart/dtend/uid plus the cue_managed bool derived from the
    X-CUE-MANAGED property. Bodies/attendees are never extracted.
    """
    comp = event.icalendar_component
    dtstart = comp.get("DTSTART")
    dtend = comp.get("DTEND")
    return {
        "summary": str(comp.get("SUMMARY", "")),
        "dtstart": str(dtstart.dt) if dtstart is not None else "",
        "dtend": str(dtend.dt) if dtend is not None else "",
        "uid": str(comp.get("UID", "")),
        "cue_managed": comp.get(X_CUE_MANAGED) is not None,
    }


async def read_day(
    username: str,
    password: str,
    date_str: str,
) -> list[dict]:
    """List the physician's events on `date_str` (HANDS-03).

    Resolves the per-physician 'personal' collection (HANDS-10), searches the
    UTC day window, and returns transient summary dicts including the
    cue_managed flag. Storage/search are UTC; the surface renders in the
    physician's local timezone.
    """
    d = date.fromisoformat(date_str)
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)

    client = _cue_client(username, password)
    # caldav DAVClient supports the context-manager protocol; use it when present.
    if hasattr(client, "__enter__"):
        with client as c:
            cal = _resolve_calendar(c)
            events = cal.search(start=start, end=end, event=True, expand=True)
            return [_event_to_dict(e) for e in events]
    cal = _resolve_calendar(client)
    events = cal.search(start=start, end=end, event=True, expand=True)
    return [_event_to_dict(e) for e in events]


# ---------------------------------------------------------------------------
# WRITE increment — STUBBED (Plan 23-04). Present so the module imports cleanly.
# The real bodies (tagged X-CUE-MANAGED VEVENT create + blast-radius-guarded
# delete) land in Plan 23-04 with the confirm-before-write envelope (D-03).
# ---------------------------------------------------------------------------


async def block_time(
    physician_id: str,
    start_iso: str,
    end_iso: str,
    title: str,
) -> str:
    """STUB (Plan 23-04): create a busy VEVENT tagged X-CUE-MANAGED after confirm."""
    raise NotImplementedError("Plan 23-04")


async def clear_range(
    physician_id: str,
    start_iso: str,
    end_iso: str,
) -> int:
    """STUB (Plan 23-04): delete ONLY X-CUE-MANAGED events in range after confirm."""
    raise NotImplementedError("Plan 23-04")
