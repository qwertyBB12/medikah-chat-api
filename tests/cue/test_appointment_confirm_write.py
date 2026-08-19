"""
tests/cue/test_appointment_confirm_write.py
--------------------------------------------
POST /cue/appointments/confirm-write — the ONLY appointment mutation path.

Gated here:
  - idempotency: a double-clicked Confirm books ONE appointment, not two,
  - the CalDAV mirror really carries X-CUE-MANAGED, end to end through the route
    (this is what keeps clear_range and delete_event_by_uid from ever touching an
    event the doctor authored),
  - move/cancel touch ONLY cue-created rows scoped to the session physician, and
    a refusal writes nothing anywhere,
  - a CalDAV outage flags needs_sync instead of losing an appointment the doctor
    just confirmed out loud,
  - the idempotency ledger and the audit rows hold no PHI.

Like tests/cue/test_confirm_write_and_revoke.py these call the UNDECORATED route
handler via __wrapped__, so the slowapi wrapper is bypassed and the gate,
idempotency, audit and CalDAV logic under test are unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

import routes.cue_routes as routes_mod
import services.cue.appointments_store as store_mod
import services.cue.calendar_dav as caldav_mod
import services.cue.credential_broker as broker_mod
import services.cue.tools.executors as execs_mod
from fastapi import HTTPException

_PHYS = "11111111-1111-1111-1111-111111111111"
_APPT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _FakeRequest:
    def __init__(self, headers: Optional[dict] = None, ip: str = "203.0.113.9") -> None:
        self.state = SimpleNamespace()
        self.headers = headers or {"user-agent": "pytest-agent/1.0"}
        self.client = SimpleNamespace(host=ip)


class _FakeAuth:
    def __init__(self, physician_id: str = _PHYS) -> None:
        self.physician_id = physician_id
        self.verification_status = "verified"


class _FakeLedger:
    """In-memory cue_write_idempotency + workspace_audit_log (same shape as 23-04's)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.audit: list[dict] = []
        self._table: Optional[str] = None
        self._eqs: dict[str, Any] = {}
        self._pending_upsert: Optional[dict] = None
        self._pending_insert: Optional[dict] = None

    def table(self, name: str):
        self._table = name
        self._eqs = {}
        self._pending_upsert = None
        self._pending_insert = None
        return self

    def select(self, *a, **kw):
        return self

    def eq(self, col, val):
        self._eqs[col] = val
        return self

    def limit(self, *a, **kw):
        return self

    def upsert(self, row, *, on_conflict=None, ignore_duplicates=False):
        self._pending_upsert = {"row": row}
        return self

    def insert(self, row):
        self._pending_insert = row
        return self

    def execute(self):
        if self._table == "workspace_audit_log" and self._pending_insert is not None:
            self.audit.append(self._pending_insert)
            return SimpleNamespace(data=[self._pending_insert])
        if self._table == "cue_write_idempotency" and self._pending_upsert is not None:
            row = self._pending_upsert["row"]
            key = (row["physician_id"], row["idempotency_token"])
            self.rows.setdefault(key, row["result_json"])
            return SimpleNamespace(data=[])
        if self._table == "cue_write_idempotency":
            key = (self._eqs.get("physician_id"), self._eqs.get("idempotency_token"))
            if key in self.rows:
                return SimpleNamespace(data=[{"result_json": self.rows[key]}])
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[])


class _FakeAppointments:
    """In-memory physician_appointments, recording every call the route makes."""

    def __init__(self, seed: Optional[dict] = None) -> None:
        self.rows: dict[str, dict] = {}
        if seed:
            self.rows[seed["id"]] = seed
        self.inserts: list[dict] = []
        self.updates: list[tuple[str, dict]] = []

    # -- read -------------------------------------------------------------
    def get_appointment(self, supabase, physician_id, appointment_id):
        row = self.rows.get(appointment_id)
        # The real store filters on physician_id — mirror that here so an IDOR
        # attempt in a test behaves the way the DB would.
        if row is None or row.get("physician_id") != physician_id:
            return None
        return dict(row)

    # -- writes -----------------------------------------------------------
    def insert_appointment(self, supabase, physician_id, **kw):
        row = {
            "id": _APPT,
            "physician_id": physician_id,
            "patient_name": store_mod.minimize_patient_name(kw["patient_name"]),
            "patient_contact": kw.get("patient_contact"),
            "starts_at": kw["starts_at"],
            "ends_at": kw["ends_at"],
            "status": "scheduled",
            "source": "cue",
            "caldav_uid": None,
            "needs_sync": False,
        }
        self.rows[row["id"]] = row
        self.inserts.append(row)
        return dict(row)

    def set_mirror(self, supabase, physician_id, appointment_id, *, caldav_uid, needs_sync):
        patch_ = {"caldav_uid": caldav_uid, "needs_sync": needs_sync}
        self.rows[appointment_id].update(patch_)
        self.updates.append((appointment_id, patch_))
        return dict(self.rows[appointment_id])

    def apply_move(self, supabase, physician_id, appointment_id, **kw):
        patch_ = {
            "starts_at": kw["starts_at"],
            "ends_at": kw["ends_at"],
            "previous_starts_at": kw["previous_starts_at"],
            "previous_ends_at": kw["previous_ends_at"],
            "status": "moved",
        }
        self.rows[appointment_id].update(patch_)
        self.updates.append((appointment_id, patch_))
        return dict(self.rows[appointment_id])

    def mark_cancelled(self, supabase, physician_id, appointment_id):
        patch_ = {"status": "cancelled"}
        self.rows[appointment_id].update(patch_)
        self.updates.append((appointment_id, patch_))
        return dict(self.rows[appointment_id])


def _seed_row(**overrides) -> dict:
    row = {
        "id": _APPT,
        "physician_id": _PHYS,
        "patient_name": "María G.",
        "starts_at": "2026-09-01T15:00:00+00:00",
        "ends_at": "2026-09-01T15:30:00+00:00",
        "status": "scheduled",
        "source": "cue",
        "caldav_uid": "cue-old-uid",
        "needs_sync": False,
    }
    row.update(overrides)
    return row


@pytest.fixture
def wired(monkeypatch):
    """Gates open, workspace verified, credential handed out, tz pinned."""
    ledger = _FakeLedger()
    monkeypatch.setattr(routes_mod, "get_supabase", lambda: ledger)

    async def _ok(*a, **kw):
        return "ok"

    monkeypatch.setattr(routes_mod, "check_kill_switch", _ok)
    monkeypatch.setattr(execs_mod, "_load_workspace_context", lambda pid: ("drtest", "verified"))
    monkeypatch.setattr(execs_mod, "resolve_physician_tz", lambda pid: "America/Mexico_City")

    cred = broker_mod.CueCredential(
        username="drtest@medikah.health", password="secret", app_passwd_id="appid-1"
    )

    async def _fake_cred(physician_id, mailbox_local_part, *a, **kw):
        return cred

    monkeypatch.setattr(broker_mod, "get_cue_cred", _fake_cred)
    return ledger


def _body(**kw):
    defaults = {
        "action": "create",
        "idempotency_token": "tok-1",
        "patient_name": "María González Torres",
        "start_iso": "2026-09-01T09:00:00",
        "end_iso": "2026-09-01T09:30:00",
        "locale": "es",
    }
    defaults.update(kw)
    return routes_mod.CueAppointmentWriteRequest(**defaults)


async def _call(body):
    return await routes_mod.cue_appointment_confirm_write.__wrapped__(
        _FakeRequest(), body, _FakeAuth()
    )


def _install(monkeypatch, appts: _FakeAppointments, *, block=None, delete=None):
    for name in ("get_appointment", "insert_appointment", "set_mirror", "apply_move", "mark_cancelled"):
        monkeypatch.setattr(store_mod, name, getattr(appts, name))
    calls = {"block": [], "delete": []}

    async def _block(username, password, start_iso, end_iso, title, *, physician_id=None, tz_name=None):
        calls["block"].append({"title": title, "start": start_iso, "end": end_iso})
        if block == "fail":
            raise RuntimeError("SOGo unreachable")
        return "cue-new-uid"

    async def _delete(username, password, uid, *, physician_id=None):
        calls["delete"].append(uid)
        if delete == "fail":
            raise RuntimeError("SOGo unreachable")
        return True

    monkeypatch.setattr(caldav_mod, "block_time", _block)
    monkeypatch.setattr(caldav_mod, "delete_event_by_uid", _delete)
    return calls


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    @pytest.mark.asyncio
    async def test_writes_the_row_then_mirrors_it(self, wired, monkeypatch):
        appts = _FakeAppointments()
        calls = _install(monkeypatch, appts)

        result = await _call(_body())

        assert result["created"] is True
        assert result["appointment_id"] == _APPT
        assert result["synced"] is True
        assert result["caldav_uid"] == "cue-new-uid"
        assert len(appts.inserts) == 1
        assert appts.rows[_APPT]["caldav_uid"] == "cue-new-uid"
        assert appts.rows[_APPT]["needs_sync"] is False
        assert len(calls["block"]) == 1

    @pytest.mark.asyncio
    async def test_stores_local_time_as_utc(self, wired, monkeypatch):
        appts = _FakeAppointments()
        _install(monkeypatch, appts)

        await _call(_body())

        # 09:00 America/Mexico_City (UTC-6) is 15:00Z. Storing the naive local
        # string as if it were UTC is the six-hour shift from 2026-06-28.
        assert appts.inserts[0]["starts_at"].startswith("2026-09-01T15:00:00")

    @pytest.mark.asyncio
    async def test_a_replayed_confirm_books_exactly_one_appointment(self, wired, monkeypatch):
        appts = _FakeAppointments()
        calls = _install(monkeypatch, appts)

        first = await _call(_body())
        second = await _call(_body())  # same idempotency_token

        assert second == first
        assert len(appts.inserts) == 1, "A double-clicked Confirm must not double-book."
        assert len(calls["block"]) == 1
        audit = [r for r in wired.audit if r["action"] == "cue.appointment_create"]
        assert len(audit) == 1
        assert audit[0]["detail"]["ip"] == "203.0.113.9"
        assert audit[0]["detail"]["ua"] == "pytest-agent/1.0"

    @pytest.mark.asyncio
    async def test_minimizes_the_name_and_keeps_phi_out_of_the_ledger_and_audit(
        self, wired, monkeypatch
    ):
        appts = _FakeAppointments()
        calls = _install(monkeypatch, appts)

        result = await _call(_body(patient_contact="ana@example.com"))

        assert appts.inserts[0]["patient_name"] == "María G."
        # The contact is stored (later notification build) but goes nowhere else.
        assert appts.inserts[0]["patient_contact"] == "ana@example.com"
        blob = json.dumps({"result": result, "audit": wired.audit, "ledger": list(wired.rows.values())})
        assert "González" not in blob and "Torres" not in blob
        assert "ana@example.com" not in blob
        # The doctor's own calendar is where their schedule belongs — the minimized
        # name is allowed there and nowhere else.
        assert calls["block"][0]["title"] == "Cita: María G."

    @pytest.mark.asyncio
    async def test_caldav_outage_flags_needs_sync_instead_of_losing_the_booking(
        self, wired, monkeypatch
    ):
        appts = _FakeAppointments()
        _install(monkeypatch, appts, block="fail")

        result = await _call(_body())

        assert result["created"] is True, "The appointment must survive a CalDAV outage."
        assert result["synced"] is False
        assert result["caldav_uid"] is None
        assert appts.rows[_APPT]["needs_sync"] is True

    @pytest.mark.asyncio
    async def test_a_bookkeeping_failure_does_not_turn_a_booking_into_an_error(
        self, wired, monkeypatch
    ):
        """The appointment is written before set_mirror runs. If that follow-up
        update fails, returning 5xx would send the doctor back to retry with a
        fresh token and book the same patient twice."""
        appts = _FakeAppointments()
        _install(monkeypatch, appts)

        def _boom(*a, **kw):
            raise store_mod.AppointmentStoreError("db blipped")

        monkeypatch.setattr(store_mod, "set_mirror", _boom)

        result = await _call(_body(idempotency_token="tok-bookkeeping"))

        assert result["created"] is True
        assert len(appts.inserts) == 1

    @pytest.mark.asyncio
    async def test_the_mirror_event_really_carries_x_cue_managed(self, wired, monkeypatch):
        """End-to-end: the route's mirror goes through the tagging write path.

        This is the guard the whole design leans on — clear_range and
        delete_event_by_uid only ever touch events carrying this property, so if
        the appointment mirror lost the tag, Cue would create calendar events it
        could no longer clean up.
        """
        appts = _FakeAppointments()
        for name in ("get_appointment", "insert_appointment", "set_mirror"):
            monkeypatch.setattr(store_mod, name, getattr(appts, name))

        saved: list[str] = []
        mock_calendar = MagicMock()
        mock_calendar.save_event.side_effect = lambda ical: saved.append(ical)
        mock_calendar.search.return_value = []
        mock_calendar.url = "https://practikah.medikah.health/SOGo/dav/drtest/Calendar/personal/"
        mock_principal = MagicMock()
        mock_principal.calendars.return_value = [mock_calendar]
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.principal.return_value = mock_principal

        with patch("caldav.DAVClient", return_value=mock_client):
            result = await _call(_body())

        assert result["synced"] is True
        assert len(saved) == 1
        assert caldav_mod.X_CUE_MANAGED in saved[0], (
            "The appointment mirror must be tagged X-CUE-MANAGED — Cue mutates "
            "only what Cue created."
        )


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------


class TestMove:
    @pytest.mark.asyncio
    async def test_repoints_the_mirror_and_keeps_the_previous_window(self, wired, monkeypatch):
        appts = _FakeAppointments(seed=_seed_row())
        calls = _install(monkeypatch, appts)

        result = await _call(_body(
            action="move", appointment_id=_APPT, patient_name=None,
            start_iso="2026-09-02T09:00:00", end_iso="2026-09-02T09:30:00",
            idempotency_token="tok-move",
        ))

        assert result["moved"] is True
        assert result["synced"] is True
        assert calls["delete"] == ["cue-old-uid"], "The stale mirror must be removed."
        assert len(calls["block"]) == 1
        row = appts.rows[_APPT]
        assert row["status"] == "moved"
        assert row["previous_starts_at"] == "2026-09-01T15:00:00+00:00"
        assert row["caldav_uid"] == "cue-new-uid"

    @pytest.mark.asyncio
    async def test_another_physicians_appointment_is_a_404_and_writes_nothing(
        self, wired, monkeypatch
    ):
        appts = _FakeAppointments(seed=_seed_row(physician_id="someone-else"))
        calls = _install(monkeypatch, appts)

        with pytest.raises(HTTPException) as exc:
            await _call(_body(
                action="move", appointment_id=_APPT, patient_name=None,
                start_iso="2026-09-02T09:00:00", end_iso="2026-09-02T09:30:00",
                idempotency_token="tok-idor",
            ))

        assert exc.value.status_code == 404
        assert appts.updates == []
        assert calls["block"] == [] and calls["delete"] == []

    @pytest.mark.asyncio
    async def test_a_failed_mirror_delete_flags_needs_sync(self, wired, monkeypatch):
        appts = _FakeAppointments(seed=_seed_row())
        _install(monkeypatch, appts, delete="fail")

        result = await _call(_body(
            action="move", appointment_id=_APPT, patient_name=None,
            start_iso="2026-09-02T09:00:00", end_iso="2026-09-02T09:30:00",
            idempotency_token="tok-move-2",
        ))

        assert result["moved"] is True
        assert result["synced"] is False
        assert appts.rows[_APPT]["needs_sync"] is True


# ---------------------------------------------------------------------------
# Cancel — the blast-radius case
# ---------------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancels_a_cue_row_and_drops_its_mirror(self, wired, monkeypatch):
        appts = _FakeAppointments(seed=_seed_row())
        calls = _install(monkeypatch, appts)

        result = await _call(_body(
            action="cancel", appointment_id=_APPT, patient_name=None,
            start_iso=None, end_iso=None, idempotency_token="tok-cancel",
        ))

        assert result["cancelled"] is True
        assert result["synced"] is True
        assert appts.rows[_APPT]["status"] == "cancelled"
        assert calls["delete"] == ["cue-old-uid"]
        assert calls["block"] == [], "Cancel must never create a calendar event."

    @pytest.mark.asyncio
    async def test_refuses_a_row_cue_did_not_create_and_touches_nothing(
        self, wired, monkeypatch
    ):
        appts = _FakeAppointments(seed=_seed_row(source="manual"))
        calls = _install(monkeypatch, appts)

        with pytest.raises(HTTPException) as exc:
            await _call(_body(
                action="cancel", appointment_id=_APPT, patient_name=None,
                start_iso=None, end_iso=None, idempotency_token="tok-manual",
            ))

        assert exc.value.status_code == 409
        assert appts.rows[_APPT]["status"] == "scheduled", "The row must be untouched."
        assert appts.updates == []
        assert calls["delete"] == [], (
            "Cue must not delete the calendar side of an appointment it did not create."
        )

    @pytest.mark.asyncio
    async def test_a_replayed_cancel_cancels_once(self, wired, monkeypatch):
        appts = _FakeAppointments(seed=_seed_row())
        calls = _install(monkeypatch, appts)
        body = _body(
            action="cancel", appointment_id=_APPT, patient_name=None,
            start_iso=None, end_iso=None, idempotency_token="tok-cancel-2",
        )

        first = await _call(body)
        second = await _call(body)

        assert second == first
        assert len(calls["delete"]) == 1


# ---------------------------------------------------------------------------
# Gate envelope + validation
# ---------------------------------------------------------------------------


class TestGates:
    @pytest.mark.asyncio
    async def test_tripped_kill_switch_blocks_the_write(self, wired, monkeypatch):
        appts = _FakeAppointments()
        _install(monkeypatch, appts)

        async def _tripped(*a, **kw):
            return "tripped"

        monkeypatch.setattr(routes_mod, "check_kill_switch", _tripped)

        with pytest.raises(HTTPException) as exc:
            await _call(_body(idempotency_token="tok-kill"))
        assert exc.value.status_code == 503
        assert appts.inserts == []

    @pytest.mark.asyncio
    async def test_an_unverified_workspace_cannot_write(self, wired, monkeypatch):
        appts = _FakeAppointments()
        _install(monkeypatch, appts)
        monkeypatch.setattr(execs_mod, "_load_workspace_context", lambda pid: ("drtest", "pending"))

        with pytest.raises(HTTPException) as exc:
            await _call(_body(idempotency_token="tok-unverified"))
        assert exc.value.status_code == 403
        assert appts.inserts == []

    @pytest.mark.asyncio
    async def test_unknown_action_and_missing_fields_are_rejected(self, wired, monkeypatch):
        _install(monkeypatch, _FakeAppointments())

        for bad in (
            _body(action="delete_everything", idempotency_token="t1"),
            _body(action="create", patient_name="", idempotency_token="t2"),
            _body(action="move", appointment_id=None, idempotency_token="t3"),
            _body(action="create", start_iso=None, idempotency_token="t4"),
            _body(idempotency_token=""),
        ):
            with pytest.raises(HTTPException) as exc:
                await _call(bad)
            assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# calendar_dav.delete_event_by_uid — the calendar half of the blast-radius guard
# ---------------------------------------------------------------------------


class TestDeleteEventByUid:
    def _calendar_with(self, component):
        event = MagicMock()
        event.icalendar_component = component
        cal = MagicMock()
        cal.event_by_uid.return_value = event
        cal.url = "https://practikah.medikah.health/SOGo/dav/drtest/Calendar/personal/"
        principal = MagicMock()
        principal.calendars.return_value = [cal]
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.principal.return_value = principal
        return client, event

    @pytest.mark.asyncio
    async def test_deletes_a_cue_managed_event(self):
        from icalendar import Event, vText

        comp = Event()
        comp.add("uid", "cue-1")
        comp.add(caldav_mod.X_CUE_MANAGED, vText("true"))
        client, event = self._calendar_with(comp)

        with patch("caldav.DAVClient", return_value=client):
            assert await caldav_mod.delete_event_by_uid("u", "p", "cue-1") is True
        event.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_refuses_an_event_the_doctor_authored(self):
        from icalendar import Event

        comp = Event()
        comp.add("uid", "doctor-1")
        comp.add("summary", "Consulta personal")
        client, event = self._calendar_with(comp)

        with patch("caldav.DAVClient", return_value=client):
            assert await caldav_mod.delete_event_by_uid("u", "p", "doctor-1") is False
        event.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_missing_uid_is_not_an_error(self):
        cal = MagicMock()
        cal.event_by_uid.side_effect = RuntimeError("404 Not Found")
        cal.url = "https://practikah.medikah.health/SOGo/dav/drtest/Calendar/personal/"
        principal = MagicMock()
        principal.calendars.return_value = [cal]
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.principal.return_value = principal

        with patch("caldav.DAVClient", return_value=client):
            assert await caldav_mod.delete_event_by_uid("u", "p", "gone") is False
