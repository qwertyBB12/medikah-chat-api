#!/usr/bin/env python3
"""
probe_sogo_calendar_slug.py
---------------------------
Wave 0 / HANDS-10 + A7: Live probe of the SOGo CalDAV collection slug
and X-CUE-MANAGED property round-trip survival.

PURPOSE
  1. HANDS-10: Confirm the real CalDAV collection URL slug for a live physician
     principal (assumed "Calendar/personal/" but SOGo may localize the name).
  2. A7 / HANDS-03: Prove that an X-CUE-MANAGED custom iCal property survives
     a SOGo CalDAV round-trip (write → fetch → property still present).
     If SOGo strips it, clear_range blast-radius protection fails and an
     alternative tagging mechanism (CATEGORIES or UID prefix) must be chosen
     before Plan 23-04 proceeds.

WHAT IT DOES
  1. Connects to SOGo/dav/<local_part>/ using a temporary app-passwd credential.
  2. Lists principal().calendars() — prints every URL to confirm the real slug.
  3. Writes ONE test VEVENT carrying X-CUE-MANAGED:true via icalendar.
  4. Fetches the event back and checks whether X-CUE-MANAGED is still present.
  5. Deletes the test event (no residue left — T-23-01-02).

DO NOT RUN AUTOMATICALLY
  This script requires a live SOGo/CalDAV credential (a temporary app-passwd
  minted by probe_app_passwd_api.py or provided by Hector).  Run only under
  per-command human approval.

USAGE (Hector, on the VPS or Render shell):
  pip install caldav==3.2.1 icalendar==7.1.3   # if not yet in venv
  python scripts/probe_sogo_calendar_slug.py <local_part> <temp-app-passwd>
  e.g.: python scripts/probe_sogo_calendar_slug.py hlopez s3cr3t-app-pass

THREAT MITIGATIONS (T-23-01-01, T-23-01-02)
  - Credential passed as CLI arg, never embedded in this file.
  - Test event is always deleted in step 5 (even if step 4 fails).
  - No secret value is printed; only the X-CUE-MANAGED property value is recorded.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

# These imports will fail until caldav==3.2.1 + icalendar==7.1.3 are installed
# from requirements.txt — that is the expected RED state for Wave 0.
try:
    import caldav
    from icalendar import Calendar, Event, vText
    CALDAV_AVAILABLE = True
except ImportError:
    CALDAV_AVAILABLE = False


CALDAV_BASE = "https://practikah.medikah.health/SOGo/dav"
X_CUE_MANAGED = "X-CUE-MANAGED"

# Probe event constants
PROBE_SUMMARY = "cue-probe-DELETE-ME"
PROBE_UID_PREFIX = "cue-probe-"


def _build_probe_event(uid: str) -> bytes:
    """Build a minimal VEVENT with X-CUE-MANAGED:true for the round-trip probe."""
    vcal = Calendar()
    vcal.add("prodid", "-//Medikah Cue Probe//EN")
    vcal.add("version", "2.0")

    vevent = Event()
    vevent.add("uid", uid)
    vevent.add("summary", PROBE_SUMMARY)
    # 2 hours from now (UTC) — specific time does not matter for this probe
    start = datetime(2099, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
    end   = datetime(2099, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
    vevent.add("dtstart", start)
    vevent.add("dtend", end)
    vevent.add("dtstamp", datetime.now(tz=timezone.utc))

    # HANDS-03 / A7: the custom property that clear_range uses as its blast-radius
    # guard.  SOGo should preserve X- properties per RFC 5545 §3.8.8.2.
    vevent.add(X_CUE_MANAGED, vText("true"))

    vcal.add_component(vevent)
    return vcal.to_ical()


def step_list_calendars(principal: "caldav.Principal") -> list:
    """Step 1: List all calendar collections for this principal."""
    print(f"\n[STEP 1] Listing calendar collections for principal")
    calendars = principal.calendars()
    print(f"  Found {len(calendars)} calendar(s):")
    for cal in calendars:
        print(f"    URL: {cal.url}")
        try:
            print(f"    displayname: {cal.get_supported_components()}")
        except Exception:
            pass
    return calendars


def step_write_probe_event(calendar: "caldav.Calendar", uid: str) -> None:
    """Step 2: Write a VEVENT with X-CUE-MANAGED:true."""
    print(f"\n[STEP 2] Writing probe VEVENT (uid={uid})")
    ical_bytes = _build_probe_event(uid)
    calendar.save_event(ical_bytes.decode("utf-8"))
    print(f"  Event written successfully.")


def step_readback_event(calendar: "caldav.Calendar", uid: str) -> bool:
    """Step 3: Fetch the event back and check X-CUE-MANAGED is preserved.

    Returns True (PASS) if the property survives; False (FAIL) if SOGo stripped it.
    """
    print(f"\n[STEP 3] Fetching event back (A7 round-trip check)")
    # Fetch event by UID
    try:
        event = calendar.event_by_uid(uid)
    except Exception as exc:
        print(f"  ERROR fetching by UID: {exc}")
        print("  Falling back to search by date range...")
        from datetime import date
        start = datetime(2099, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
        end   = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        events = calendar.search(start=start, end=end, event=True)
        event = next(
            (e for e in events
             if str(e.icalendar_component.get("UID", "")) == uid),
            None,
        )

    if event is None:
        print(f"  FAIL: event not found on read-back — cannot check X-CUE-MANAGED.")
        return False

    comp = event.icalendar_component
    cue_prop = comp.get(X_CUE_MANAGED)
    summary  = str(comp.get("SUMMARY", ""))

    print(f"  Summary: {summary}")
    print(f"  {X_CUE_MANAGED}: {cue_prop!r}")

    if cue_prop is not None:
        print(f"\n  *** X-CUE-MANAGED ROUND-TRIP: PASS ***")
        print(f"      SOGo preserved the property — clear_range blast-radius protection is viable.")
        return True
    else:
        print(f"\n  *** X-CUE-MANAGED ROUND-TRIP: FAIL ***")
        print(f"      SOGo STRIPPED the X-CUE-MANAGED property.")
        print(f"      FALLBACK OPTIONS (choose one before Plan 23-04 proceeds):")
        print(f"        1. Use CATEGORIES:CUE-MANAGED instead (SOGo preserves CATEGORIES).")
        print(f"        2. Use a UID prefix ('cue-' prefix) as the clear_range guard.")
        print(f"      Document the chosen fallback in 23-PROBE-FINDINGS.md.")
        return False


def step_delete_probe_event(calendar: "caldav.Calendar", uid: str) -> None:
    """Step 4: Delete the test event — no residue left (T-23-01-02)."""
    print(f"\n[STEP 4] Deleting probe event (cleanup — uid={uid})")
    try:
        event = calendar.event_by_uid(uid)
        event.delete()
        print(f"  *** CLEANUP: test event DELETED (T-23-01-02 satisfied) ***")
    except Exception as exc:
        print(f"  WARNING: could not delete event by UID ({exc}).")
        print(f"  Attempting date-range search and delete...")
        try:
            start = datetime(2099, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
            end   = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
            events = calendar.search(start=start, end=end, event=True)
            for e in events:
                if str(e.icalendar_component.get("UID", "")) == uid:
                    e.delete()
                    print(f"  *** CLEANUP: test event DELETED via search (T-23-01-02 satisfied) ***")
                    return
            print(f"  *** CLEANUP FAILED — manually verify 'cue-probe-DELETE-ME' event removed ***")
        except Exception as exc2:
            print(f"  *** CLEANUP FAILED ({exc2}) — manually verify event removed ***")


def _pick_calendar(calendars: list) -> "caldav.Calendar":
    """Pick the best candidate calendar.

    Prefers the one whose URL contains 'personal' (assumed HANDS-10 default).
    Falls back to the first calendar if no 'personal' slug is found.
    """
    for cal in calendars:
        if "personal" in str(cal.url).lower():
            print(f"  Using calendar with 'personal' slug: {cal.url}")
            return cal
    if calendars:
        print(f"  No 'personal' slug found — using first calendar: {calendars[0].url}")
        print(f"  *** RECORD THIS URL in 23-PROBE-FINDINGS.md as the real slug! ***")
        return calendars[0]
    raise RuntimeError("No calendar collections found for this principal.")


def main(local_part: str, app_passwd: str) -> None:
    if not CALDAV_AVAILABLE:
        print("ERROR: caldav or icalendar not installed.")
        print("  Run: pip install caldav==3.2.1 icalendar==7.1.3")
        sys.exit(1)

    username = local_part if "@" in local_part else f"{local_part}@medikah.health"
    principal_url = f"{CALDAV_BASE}/{local_part}/"

    print("=" * 70)
    print("HANDS-10 / A7 PROBE: SOGo CalDAV slug + X-CUE-MANAGED round-trip")
    print(f"  Principal URL: {principal_url}")
    print(f"  Username: {username}")
    print(f"  Credential: [PROVIDED — not printed]")
    print("=" * 70)

    uid = f"{PROBE_UID_PREFIX}{uuid.uuid4()}"
    roundtrip_pass = False

    with caldav.DAVClient(
        url=principal_url,
        username=username,
        password=app_passwd,  # the temp app-passwd — never logged
    ) as client:
        principal = client.principal()
        calendars = step_list_calendars(principal)

        calendar = _pick_calendar(calendars)

        try:
            step_write_probe_event(calendar, uid)
            roundtrip_pass = step_readback_event(calendar, uid)
        finally:
            # Cleanup runs even if readback raised
            step_delete_probe_event(calendar, uid)

    print("\n" + "=" * 70)
    print("PROBE COMPLETE")
    print(f"  X-CUE-MANAGED round-trip: {'PASS' if roundtrip_pass else 'FAIL'}")
    print(f"\nNext: fill in 23-PROBE-FINDINGS.md with:")
    print(f"  - The exact calendar slug URL(s) printed in STEP 1")
    print(f"  - X-CUE-MANAGED: {'PASS' if roundtrip_pass else 'FAIL + chosen fallback'}")
    print("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <local_part> <temp-app-passwd>")
        print(f"  e.g.: {sys.argv[0]} hlopez s3cr3t-app-pass")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
