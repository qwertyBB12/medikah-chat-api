# Wave 0 RED scaffold — implemented by Plan 23-03
"""
tests/cue/test_calendar_dav.py
--------------------------------
Wave 0 failing scaffold for the Cue CalDAV client (HANDS-03).

These tests MUST fail (ImportError) until Plan 23-03 writes
services/cue/calendar_dav.py.  The failing import is the intended RED state.

Requirements gated:
  HANDS-03: CalDAV client — read_day, block_time, clear_range; X-CUE-MANAGED tag;
            UTC storage.
  HANDS-10: SOGo collection slug resolved per physician (not hardcoded).
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# RED import — this module does not exist yet (Plan 23-03 creates it).
# The collection error here is the EXPECTED red-state for Wave 0.
# ---------------------------------------------------------------------------
from services.cue.calendar_dav import (  # noqa: E402
    block_time,
    clear_range,
    read_day,
    X_CUE_MANAGED,
)


# ---------------------------------------------------------------------------
# Test 1 — HANDS-03: block_time must tag the event X-CUE-MANAGED
# ---------------------------------------------------------------------------

class TestBlockTimeTagsXCueManaged:
    """block_time must add X-CUE-MANAGED:true to every VEVENT it creates.

    This tag is the sole blast-radius guard that lets clear_range distinguish
    Cue-authored events from doctor-authored events.
    """

    def test_x_cue_managed_constant_is_defined(self):
        """X_CUE_MANAGED must be exported as a module-level constant."""
        assert isinstance(X_CUE_MANAGED, str), (
            "X_CUE_MANAGED must be a string constant (the iCal property name)"
        )
        assert "CUE" in X_CUE_MANAGED.upper() or "CUE-MANAGED" in X_CUE_MANAGED.upper(), (
            f"X_CUE_MANAGED must include 'CUE' in its name; got: {X_CUE_MANAGED!r}"
        )

    def test_block_time_is_async(self):
        """block_time must be an async function (CalDAV calls are blocking-network)."""
        assert inspect.iscoroutinefunction(block_time), (
            "block_time must be async"
        )

    def test_block_time_signature(self):
        """block_time must accept physician_id, start_iso, end_iso, title."""
        sig = inspect.signature(block_time)
        params = set(sig.parameters.keys())
        for required in ("physician_id", "start_iso", "end_iso", "title"):
            assert required in params, (
                f"block_time missing required parameter: {required}"
            )

    @pytest.mark.asyncio
    async def test_block_time_saves_event_with_x_cue_managed(self, monkeypatch):
        """block_time must write a VEVENT that includes X-CUE-MANAGED:true.

        Captures the iCal data passed to caldav calendar.save_event() and
        asserts that the X-CUE-MANAGED property is present. (Plan 23-04: real
        write body — credentials are username/password, never module-level.)
        """
        from icalendar import Calendar as ICalendar

        saved_ical_data: list[str] = []

        # Minimal caldav mock: captures the save_event call
        mock_calendar = MagicMock()

        def capture_save_event(ical_str: str):
            saved_ical_data.append(ical_str)

        mock_calendar.save_event = capture_save_event

        mock_principal = MagicMock()
        mock_principal.calendars.return_value = [mock_calendar]
        mock_calendar.url = "https://practikah.medikah.health/SOGo/dav/testdoc/Calendar/personal/"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.principal.return_value = mock_principal

        # Patch the caldav.DAVClient constructor
        with patch("caldav.DAVClient", return_value=mock_client):
            uid = await block_time(
                "testdoc@medikah.health",
                "app-passwd-secret",
                "2026-07-01T10:00:00+00:00",
                "2026-07-01T12:00:00+00:00",
                "Blocked by Cue",
                physician_id="test-phys-005",
            )

        # block_time must return a stable Cue-namespaced uid (route wraps as
        # {"blocked": true, "uid": uid}).
        assert isinstance(uid, str) and uid.startswith("cue-"), (
            f"block_time must return a 'cue-'-prefixed uid; got {uid!r}"
        )

        assert len(saved_ical_data) >= 1, (
            "block_time must call calendar.save_event() to write the VEVENT"
        )

        # Parse the saved iCal and check for X-CUE-MANAGED
        found_x_cue_managed = False
        for ical_str in saved_ical_data:
            ical_bytes = ical_str.encode("utf-8") if isinstance(ical_str, str) else ical_str
            cal = ICalendar.from_ical(ical_bytes)
            for component in cal.walk():
                if component.name == "VEVENT":
                    prop_val = component.get(X_CUE_MANAGED)
                    if prop_val is not None:
                        found_x_cue_managed = True
                        break

        assert found_x_cue_managed, (
            f"HANDS-03 VIOLATION: block_time did not set {X_CUE_MANAGED} on the VEVENT. "
            f"This property is the sole blast-radius guard for clear_range — without it, "
            f"clear_range would delete doctor-authored events."
        )


# ---------------------------------------------------------------------------
# Test 2 — HANDS-03: clear_range must ONLY delete X-CUE-MANAGED events
# ---------------------------------------------------------------------------

class TestClearRangeBlastRadius:
    """clear_range must delete ONLY events tagged X-CUE-MANAGED:true.

    It must leave doctor-authored events (no X-CUE-MANAGED property) untouched.
    This is the hardest requirement of HANDS-03 — a clear_range that deletes
    untagged events could wipe a physician's patient appointments.
    """

    def test_clear_range_is_async(self):
        assert inspect.iscoroutinefunction(clear_range), "clear_range must be async"

    def test_clear_range_signature(self):
        sig = inspect.signature(clear_range)
        params = set(sig.parameters.keys())
        for required in ("physician_id", "start_iso", "end_iso"):
            assert required in params, (
                f"clear_range missing required parameter: {required}"
            )

    @pytest.mark.asyncio
    async def test_clear_range_deletes_only_cue_managed_events(self, monkeypatch):
        """clear_range must filter on X-CUE-MANAGED and leave untagged events intact.

        Sets up two mock events: one Cue-managed, one doctor-authored.
        Asserts that delete() is called exactly once on the Cue-managed event
        and never on the doctor-authored event — the BLAST-RADIUS test. The
        return shape is the canonical {deleted, skipped}.
        """
        from icalendar import Calendar as ICalendar, Event, vText

        def _make_event(with_cue_managed: bool, uid: str) -> MagicMock:
            """Build a mock caldav event whose icalendar_component has/lacks X-CUE-MANAGED."""
            vevent = Event()
            vevent.add("uid", uid)
            vevent.add("summary", "Doctor event" if not with_cue_managed else "Cue block")
            vevent.add("dtstart", datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc))
            vevent.add("dtend",   datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc))
            if with_cue_managed:
                vevent.add(X_CUE_MANAGED, vText("true"))

            mock_event = MagicMock()
            mock_event.icalendar_component = vevent
            mock_event.delete = MagicMock()
            return mock_event

        cue_event    = _make_event(with_cue_managed=True,  uid="cue-abc-123")
        doctor_event = _make_event(with_cue_managed=False, uid="doctor-xyz-456")

        mock_calendar = MagicMock()
        mock_calendar.search.return_value = [cue_event, doctor_event]
        mock_calendar.url = (
            "https://practikah.medikah.health/SOGo/dav/testdoc/Calendar/personal/"
        )

        mock_principal = MagicMock()
        mock_principal.calendars.return_value = [mock_calendar]

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.principal.return_value = mock_principal

        with patch("caldav.DAVClient", return_value=mock_client):
            result = await clear_range(
                "testdoc@medikah.health",
                "app-passwd-secret",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T23:59:59+00:00",
                physician_id="test-phys-006",
            )

        # The Cue-managed event must have been deleted
        cue_event.delete.assert_called_once()

        # The doctor-authored event must NOT have been deleted
        doctor_event.delete.assert_not_called(), (
            "HANDS-03 VIOLATION: clear_range deleted a doctor-authored event "
            "(no X-CUE-MANAGED property). Blast-radius protection failed."
        )

        # Canonical return shape: exactly one deleted, one skipped (the doctor's).
        assert result == {"deleted": 1, "skipped": 1}, (
            f"clear_range must return {{deleted:1, skipped:1}} for a mixed range; got {result}"
        )

    @pytest.mark.asyncio
    async def test_clear_range_returns_zero_when_no_cue_events(self, monkeypatch):
        """clear_range on a range with only doctor-authored events deletes nothing.

        Returns {deleted:0, skipped:N} — a zero-Cue-event range must touch
        nothing on the doctor's calendar.
        """
        from icalendar import Event

        def _make_doctor_event(uid: str) -> MagicMock:
            vevent = Event()
            vevent.add("uid", uid)
            vevent.add("summary", "Patient appointment")
            vevent.add("dtstart", datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone.utc))
            vevent.add("dtend",   datetime(2026, 7, 1, 15, 0, 0, tzinfo=timezone.utc))
            # No X-CUE-MANAGED — doctor-authored event
            mock_event = MagicMock()
            mock_event.icalendar_component = vevent
            mock_event.delete = MagicMock()
            return mock_event

        doctor_event = _make_doctor_event("doctor-patient-appt-001")

        mock_calendar = MagicMock()
        mock_calendar.search.return_value = [doctor_event]
        mock_calendar.url = (
            "https://practikah.medikah.health/SOGo/dav/testdoc/Calendar/personal/"
        )
        mock_principal = MagicMock()
        mock_principal.calendars.return_value = [mock_calendar]
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.principal.return_value = mock_principal

        with patch("caldav.DAVClient", return_value=mock_client):
            result = await clear_range(
                "testdoc@medikah.health",
                "app-passwd-secret",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T23:59:59+00:00",
                physician_id="test-phys-007",
            )

        doctor_event.delete.assert_not_called()
        assert result == {"deleted": 0, "skipped": 1}, (
            f"clear_range must return {{deleted:0, skipped:1}} when no Cue-managed "
            f"events exist; got {result}"
        )
