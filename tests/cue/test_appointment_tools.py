"""
tests/cue/test_appointment_tools.py
------------------------------------
The four appointment model tools: appointment_list (READ) and
appointment_create / appointment_move / appointment_cancel (PURE PROPOSERS).

The load-bearing property is D-03 PROPOSER PURITY: the write tools must return a
confirm card and NOTHING must have changed. A misheard utterance or an injected
tool_use has to be incapable of booking, moving, or cancelling a real patient's
appointment — only the doctor's Confirm click, through the route, can do that.

Also gated:
  - PHI minimization: a full legal name dictated to the tool never reaches the
    card, and no raw identifier has anywhere to land,
  - the blast-radius refusals (another physician's appointment, an appointment
    Cue did not create) happen at PROPOSAL time, before a card is ever offered,
  - CUE-11: no identity argument in any schema, and dispatch drops stray keys.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.cue.appointments_store as store_mod
import services.cue.calendar_dav as caldav_mod
import services.cue.tools.executors as ex
from services.cue.tools.registry import NEUTRAL_TOOLS, dispatch_tool

_PHYS = "11111111-1111-1111-1111-111111111111"
_APPT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_TZ = "America/Mexico_City"

_APPOINTMENT_TOOLS = (
    "appointment_list",
    "appointment_create",
    "appointment_move",
    "appointment_cancel",
)


def _row(**overrides) -> dict:
    row = {
        "id": _APPT,
        "patient_name": "María G.",
        # 15:00Z == 09:00 America/Mexico_City
        "starts_at": "2026-09-01T15:00:00+00:00",
        "ends_at": "2026-09-01T15:30:00+00:00",
        "status": "scheduled",
        "source": "cue",
        "caldav_uid": "cue-uid-1",
        "needs_sync": False,
    }
    row.update(overrides)
    return row


@pytest.fixture
def no_writes(monkeypatch):
    """Arm every write path in the store and the calendar to fail the test if called.

    This is the fixture that makes 'pure proposer' a checked claim rather than a
    comment: if any proposer ever grows a write branch, these blow up.
    """
    tripwires = {}
    for name in ("insert_appointment", "apply_move", "mark_cancelled", "set_mirror", "_update"):
        mock = MagicMock(side_effect=AssertionError(f"proposer wrote via store.{name}"))
        monkeypatch.setattr(store_mod, name, mock)
        tripwires[name] = mock
    for name in ("block_time", "clear_range", "delete_event_by_uid"):
        mock = AsyncMock(side_effect=AssertionError(f"proposer wrote via calendar_dav.{name}"))
        monkeypatch.setattr(caldav_mod, name, mock)
        tripwires[name] = mock
    # Audits and tz lookups are side channels, not writes to the doctor's data.
    monkeypatch.setattr(ex, "_write_action_audit", MagicMock())
    monkeypatch.setattr(ex, "resolve_physician_tz", lambda pid: _TZ)
    monkeypatch.setattr(ex, "_get_db", lambda: MagicMock())
    return tripwires


# ---------------------------------------------------------------------------
# D-03 proposer purity
# ---------------------------------------------------------------------------


class TestProposersNeverWrite:
    @pytest.mark.asyncio
    async def test_create_returns_a_card_and_writes_nothing(self, no_writes):
        raw = await ex.appointment_create(
            physician_id=_PHYS,
            patient_name="María González Torres",
            start_iso="2026-09-01T09:00:00",
            end_iso="2026-09-01T09:30:00",
            locale="es",
        )
        card = json.loads(raw)
        assert card["kind"] == "confirm"
        assert card["action"] == "appointment_create"
        for mock in no_writes.values():
            mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_returns_a_card_and_writes_nothing(self, no_writes, monkeypatch):
        monkeypatch.setattr(store_mod, "get_appointment", lambda *a, **kw: _row())
        raw = await ex.appointment_move(
            physician_id=_PHYS,
            appointment_id=_APPT,
            start_iso="2026-09-02T09:00:00",
            end_iso="2026-09-02T09:30:00",
            locale="es",
        )
        card = json.loads(raw)
        assert card["kind"] == "confirm"
        assert card["action"] == "appointment_move"
        assert card["appointment_id"] == _APPT
        for mock in no_writes.values():
            mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_returns_a_card_and_writes_nothing(self, no_writes, monkeypatch):
        monkeypatch.setattr(store_mod, "get_appointment", lambda *a, **kw: _row())
        raw = await ex.appointment_cancel(
            physician_id=_PHYS, appointment_id=_APPT, locale="es"
        )
        card = json.loads(raw)
        assert card["kind"] == "confirm"
        assert card["action"] == "appointment_cancel"
        # The card carries the CURRENT window so the doctor sees what disappears.
        assert card["start_iso"] == "2026-09-01T15:00:00+00:00"
        for mock in no_writes.values():
            mock.assert_not_called()

    def test_no_proposer_takes_a_confirmed_parameter(self):
        for fn in (ex.appointment_create, ex.appointment_move, ex.appointment_cancel):
            params = inspect.signature(fn).parameters
            assert "confirmed" not in params, (
                f"{fn.__name__} must have no confirmed parameter — a one-shot "
                "tool_use can never authorize a write."
            )
            assert "physician_id" in params, (
                f"{fn.__name__} must take physician_id from the dispatcher (CUE-11)."
            )


# ---------------------------------------------------------------------------
# PHI minimization
# ---------------------------------------------------------------------------


class TestPhiMinimization:
    @pytest.mark.asyncio
    async def test_full_name_never_reaches_the_card(self, no_writes):
        raw = await ex.appointment_create(
            physician_id=_PHYS,
            patient_name="María González Torres",
            start_iso="2026-09-01T09:00:00",
            end_iso="2026-09-01T09:30:00",
            locale="es",
        )
        assert "González" not in raw
        assert "Torres" not in raw
        card = json.loads(raw)
        assert card["patient_name"] == "María G."
        assert "María G." in card["summary"]

    def test_no_appointment_tool_accepts_a_patient_contact(self):
        # The column exists for the later notification build, but a raw email or
        # phone must never travel through the model's context to reach it.
        forbidden = ("patient_contact", "email", "phone", "patient_email", "contact")
        for tool in NEUTRAL_TOOLS:
            if tool.name not in _APPOINTMENT_TOOLS:
                continue
            props = set(tool.input_schema.get("properties", {}))
            assert not (props & set(forbidden)), (
                f"{tool.name} exposes a contact field: {props & set(forbidden)}"
            )

    def test_no_appointment_tool_accepts_an_identity_argument(self):
        for tool in NEUTRAL_TOOLS:
            if tool.name not in _APPOINTMENT_TOOLS:
                continue
            props = set(tool.input_schema.get("properties", {}))
            assert not (props & {"physician_id", "slug", "doctor_id", "confirmed"}), (
                f"CUE-11: {tool.name} declares an identity/confirmed property"
            )

    @pytest.mark.asyncio
    async def test_dispatch_drops_a_hallucinated_contact_key(self, no_writes):
        # An unexpected key must be dropped, not become a TypeError the model has
        # to interpret — and it must not survive into the card.
        raw = await dispatch_tool(
            tool_name="appointment_create",
            tool_input={
                "patient_name": "Ana Ruiz",
                "start_iso": "2026-09-01T09:00:00",
                "end_iso": "2026-09-01T09:30:00",
                "patient_email": "ana@example.com",
                "physician_id": "someone-elses-id",
            },
            physician_id=_PHYS,
            locale="es",
        )
        assert "ana@example.com" not in raw
        assert "someone-elses-id" not in raw
        assert json.loads(raw)["patient_name"] == "Ana R."


# ---------------------------------------------------------------------------
# Blast-radius refusals at proposal time
# ---------------------------------------------------------------------------


class TestProposalTimeRefusals:
    @pytest.mark.asyncio
    async def test_another_physicians_appointment_gets_no_card(self, no_writes, monkeypatch):
        # get_appointment is physician-scoped, so a foreign id returns None.
        monkeypatch.setattr(store_mod, "get_appointment", lambda *a, **kw: None)
        raw = await ex.appointment_move(
            physician_id=_PHYS, appointment_id="not-mine",
            start_iso="2026-09-02T09:00:00", end_iso="2026-09-02T09:30:00",
        )
        assert "kind" not in raw
        assert "no encontré" in raw.lower() and "could not find" in raw.lower()

    @pytest.mark.asyncio
    async def test_an_appointment_cue_did_not_create_gets_no_card(self, no_writes, monkeypatch):
        monkeypatch.setattr(store_mod, "get_appointment", lambda *a, **kw: _row(source="manual"))
        raw = await ex.appointment_cancel(physician_id=_PHYS, appointment_id=_APPT)
        assert "kind" not in raw
        assert "cue" in raw.lower()
        assert "dashboard" in raw.lower()

    @pytest.mark.asyncio
    async def test_an_already_cancelled_appointment_gets_no_card(self, no_writes, monkeypatch):
        monkeypatch.setattr(store_mod, "get_appointment", lambda *a, **kw: _row(status="cancelled"))
        raw = await ex.appointment_cancel(physician_id=_PHYS, appointment_id=_APPT)
        assert "kind" not in raw

    @pytest.mark.asyncio
    async def test_a_lookup_failure_degrades_instead_of_raising(self, no_writes, monkeypatch):
        def _boom(*a, **kw):
            raise store_mod.AppointmentStoreError("db down")

        monkeypatch.setattr(store_mod, "get_appointment", _boom)
        raw = await ex.appointment_cancel(physician_id=_PHYS, appointment_id=_APPT)
        assert "kind" not in raw
        assert "could not read" in raw.lower()


# ---------------------------------------------------------------------------
# appointment_list (READ)
# ---------------------------------------------------------------------------


class TestAppointmentList:
    @pytest.mark.asyncio
    async def test_renders_local_time_not_utc(self, no_writes, monkeypatch):
        monkeypatch.setattr(store_mod, "list_upcoming", lambda *a, **kw: [_row()])
        out = await ex.appointment_list(physician_id=_PHYS, limit=10)

        # 15:00Z is 09:00 in Mexico City — the doctor's wall-clock, never the UTC value.
        assert "09:00" in out
        assert "15:00" not in out
        assert "María G." in out
        assert _APPT in out

    @pytest.mark.asyncio
    async def test_empty_book_says_so_in_both_languages(self, no_writes, monkeypatch):
        monkeypatch.setattr(store_mod, "list_upcoming", lambda *a, **kw: [])
        out = await ex.appointment_list(physician_id=_PHYS)
        assert "citas" in out.lower()
        assert "appointments" in out.lower()

    @pytest.mark.asyncio
    async def test_db_failure_is_reported_not_disguised_as_an_empty_book(
        self, no_writes, monkeypatch
    ):
        def _boom(*a, **kw):
            raise store_mod.AppointmentStoreError("db down")

        monkeypatch.setattr(store_mod, "list_upcoming", _boom)
        out = await ex.appointment_list(physician_id=_PHYS)

        assert "could not read" in out.lower()
        assert "no upcoming appointments" not in out.lower(), (
            "An outage reported as an empty schedule is a lie the doctor acts on."
        )

    @pytest.mark.asyncio
    async def test_audit_row_carries_counts_only(self, monkeypatch):
        monkeypatch.setattr(ex, "resolve_physician_tz", lambda pid: _TZ)
        monkeypatch.setattr(ex, "_get_db", lambda: MagicMock())
        monkeypatch.setattr(store_mod, "list_upcoming", lambda *a, **kw: [_row()])
        audit = MagicMock()
        monkeypatch.setattr(ex, "_write_action_audit", audit)

        await ex.appointment_list(physician_id=_PHYS, limit=10)

        _, action, detail = audit.call_args[0]
        assert action == "cue.appointment_list"
        assert detail == {"limit": 10, "appointment_count": 1}
        assert "María" not in json.dumps(detail)

    @pytest.mark.asyncio
    async def test_dispatch_caps_the_limit_at_twenty(self, no_writes, monkeypatch):
        seen = {}

        def _capture(supabase, physician_id, limit=10, **kw):
            seen["limit"] = limit
            return []

        monkeypatch.setattr(store_mod, "list_upcoming", _capture)
        await dispatch_tool(
            tool_name="appointment_list",
            tool_input={"limit": 500},
            physician_id=_PHYS,
        )
        assert seen["limit"] == 20


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestRegistryWiring:
    def test_all_four_tools_are_registered(self):
        names = {t.name for t in NEUTRAL_TOOLS}
        assert set(_APPOINTMENT_TOOLS) <= names

    @pytest.mark.parametrize("locale", ["en", "es"])
    def test_self_knowledge_does_not_contradict_the_appointment_tools(self, locale):
        """The prompt must not tell Cue it cannot do what the tools now let it do.

        Before the appointment vertical, the self-knowledge block said Cue stores
        no patient-identifiable information at all and does not do appointment
        scheduling. Leaving that in would have Cue refuse its own tools, or use
        them and then contradict itself out loud to the doctor.
        """
        from services.cue.personality.self_knowledge import build_self_knowledge

        block = build_self_knowledge(locale).lower()

        assert "cita" in block or "appointment book" in block
        # The patient-notification boundary is the one the doctor could be
        # burned by: Cue must never imply the patient was told.
        assert "not notified" in block or "no recibe ningún aviso" in block
        # The old absolute PHI denial is gone; the narrow exception replaced it.
        assert "phase 22" not in block and "fase 22" not in block
        assert "last initial" in block or "inicial de su apellido" in block

    def test_write_tool_descriptions_tell_the_model_it_cannot_write(self):
        for tool in NEUTRAL_TOOLS:
            if tool.name not in ("appointment_create", "appointment_move", "appointment_cancel"):
                continue
            desc = tool.description.lower()
            assert "never writes" in desc
            assert "confirm" in desc
            # Doctor-facing only: no patient notification exists in this build,
            # and Cue must not imply one happened.
            assert "not notif" in desc or "does not notify" in desc, (
                f"{tool.name} must tell the model the patient is NOT notified"
            )
