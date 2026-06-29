"""Scheduling timezone correctness (Fix #3, diagnosis 2026-06-28).

The model is fed — and emits — LOCAL time (the date directive + the block/clear
tool schema), but the write path interpreted a naive (offset-less) datetime as
UTC, so a 3pm Mexico City block was stored at 15:00Z = 9am local (a 6h shift),
and non-MX doctors were broken by a hardcoded America/Mexico_City constant.

These lock the round-trip: a naive LOCAL time is interpreted in the physician's
zone on write (→ correct UTC) and rendered back in that zone on read; an
offset-aware value is respected as-is; and resolve_physician_tz reads the
doctor's availability zone with a Mexico City fallback (treating the 'UTC'
column default as unset).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from services.cue.calendar_dav import _build_block_calendar, _format_event_dt, _to_utc
from services.cue.tools import executors


MX = "America/Mexico_City"  # UTC-6 (no DST since 2022)


# ---------------------------------------------------------------------------
# _to_utc: naive interpreted in the physician zone; offset-aware respected
# ---------------------------------------------------------------------------


def test_naive_local_time_interpreted_in_physician_zone() -> None:
    # 3pm in Mexico City is 21:00 UTC — NOT 15:00 UTC (the old booking bug).
    assert _to_utc("2026-07-01T15:00:00", MX) == datetime(
        2026, 7, 1, 21, 0, tzinfo=timezone.utc
    )


def test_offset_aware_value_is_respected() -> None:
    assert _to_utc("2026-07-01T15:00:00+00:00", MX) == datetime(
        2026, 7, 1, 15, 0, tzinfo=timezone.utc
    )


def test_default_tz_is_utc_for_storage_origin_values() -> None:
    # Default (no zone passed) keeps the legacy naive=UTC behavior.
    assert _to_utc("2026-07-01T15:00:00") == datetime(
        2026, 7, 1, 15, 0, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# Round-trip: write a 3pm-local block, read it back as 3pm local
# ---------------------------------------------------------------------------


def test_write_then_read_round_trip_is_consistent() -> None:
    ics = _build_block_calendar(
        "cue-x", "2026-07-01T15:00:00", "2026-07-01T15:30:00", "Blocked by Cue", MX
    )
    # Stored as UTC (21:00Z = 15:00 MX), per the iCal DTSTART/DTEND lines.
    assert "DTSTART:20260701T210000Z" in ics
    assert "DTEND:20260701T213000Z" in ics
    # And the read path renders 21:00Z back to the doctor's local 15:00.
    rendered = _format_event_dt(
        datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc), ZoneInfo(MX)
    )
    assert rendered == "2026-07-01 15:00"


# ---------------------------------------------------------------------------
# resolve_physician_tz: availability zone, with 'UTC'/None → MX fallback
# ---------------------------------------------------------------------------


class _Av:
    def __init__(self, tz):
        self.timezone = tz


def _patch_av(monkeypatch, value):
    monkeypatch.setattr(
        "services.physician_dashboard.get_physician_availability",
        lambda physician_id: value,
    )


def test_resolve_uses_real_availability_zone(monkeypatch) -> None:
    _patch_av(monkeypatch, _Av("America/Bogota"))
    assert executors.resolve_physician_tz("p1") == "America/Bogota"


def test_resolve_treats_utc_default_as_unset(monkeypatch) -> None:
    _patch_av(monkeypatch, _Av("UTC"))
    assert executors.resolve_physician_tz("p1") == MX


def test_resolve_falls_back_when_no_availability(monkeypatch) -> None:
    _patch_av(monkeypatch, None)
    assert executors.resolve_physician_tz("p1") == MX


def test_resolve_falls_back_on_bad_zone(monkeypatch) -> None:
    _patch_av(monkeypatch, _Av("Not/AZone"))
    assert executors.resolve_physician_tz("p1") == MX
