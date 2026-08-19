"""
tests/cue/test_appointments_store.py
-------------------------------------
The DB layer behind the appointment vertical (migration 043).

Gated here:
  - PHI minimization at the choke point: minimize_patient_name is what keeps a
    full legal name out of a column the model reads back,
  - every read and write is scoped to physician_id (CUE-11) — an appointment id
    alone must never be enough to reach a row,
  - the failure posture: this store RAISES where the memory store returns empty.
    "You have no appointments" during a DB outage is a lie the doctor would act
    on, so the read tool has to be able to tell empty from broken.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.cue.appointments_store import (
    ACTIVE_STATUSES,
    AppointmentStoreError,
    apply_move,
    get_appointment,
    insert_appointment,
    list_upcoming,
    mark_cancelled,
    minimize_patient_name,
    set_mirror,
)

_PHYS = "11111111-1111-1111-1111-111111111111"
_APPT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _chain(execute_data):
    """supabase MagicMock whose .table(...)....execute() returns .data."""
    sb = MagicMock()
    result = MagicMock()
    result.data = execute_data
    builder = sb.table.return_value
    for attr in ("select", "insert", "update", "eq", "in_", "gte", "order", "limit"):
        getattr(builder, attr).return_value = builder
    builder.execute.return_value = result
    return sb


class TestMinimizePatientName:
    def test_initial_comes_from_the_apellido_paterno_not_the_last_token(self):
        # Mexican names are nombre + apellido paterno + apellido materno. Taking
        # the LAST token would file María González Torres under 'T.' — her
        # mother's family, not the name her doctor knows her by.
        assert minimize_patient_name("María González Torres") == "María G."

    def test_two_part_name_keeps_only_the_initial(self):
        assert minimize_patient_name("Ana Ruiz") == "Ana R."

    def test_single_name_is_left_alone(self):
        assert minimize_patient_name("Ana") == "Ana"

    def test_already_minimal_name_is_idempotent(self):
        assert minimize_patient_name("María G.") == "María G."
        assert minimize_patient_name(minimize_patient_name("María González")) == "María G."

    def test_blank_stays_blank(self):
        assert minimize_patient_name("") == ""
        assert minimize_patient_name("   ") == ""


class TestListUpcoming:
    def test_scoped_to_physician_and_active_statuses_only(self):
        sb = _chain([
            {"id": _APPT, "patient_name": "Ana R.", "starts_at": "2026-09-01T15:00:00+00:00"},
        ])
        rows = list_upcoming(sb, _PHYS, limit=5)

        assert len(rows) == 1
        builder = sb.table.return_value
        builder.eq.assert_any_call("physician_id", _PHYS)
        # A cancelled appointment is off the doctor's book; 'moved' is still on it.
        builder.in_.assert_any_call("status", list(ACTIVE_STATUSES))
        assert set(ACTIVE_STATUSES) == {"scheduled", "moved"}

    def test_orders_soonest_first(self):
        sb = _chain([])
        list_upcoming(sb, _PHYS)
        sb.table.return_value.order.assert_any_call("starts_at", desc=False)

    def test_db_failure_raises_instead_of_looking_empty(self):
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("db down")
        with pytest.raises(AppointmentStoreError):
            list_upcoming(sb, _PHYS)

    def test_missing_client_raises(self):
        with pytest.raises(AppointmentStoreError):
            list_upcoming(None, _PHYS)

    def test_patient_contact_is_never_selected(self):
        sb = _chain([])
        list_upcoming(sb, _PHYS)
        columns = sb.table.return_value.select.call_args[0][0]
        assert "patient_contact" not in columns, (
            "patient_contact must stay out of every read projection — it is the "
            "one field that can hold a raw identifier."
        )


class TestGetAppointment:
    def test_filters_on_both_physician_and_id(self):
        sb = _chain([{"id": _APPT, "source": "cue"}])
        row = get_appointment(sb, _PHYS, _APPT)

        assert row["id"] == _APPT
        builder = sb.table.return_value
        builder.eq.assert_any_call("physician_id", _PHYS)
        builder.eq.assert_any_call("id", _APPT)

    def test_returns_none_when_the_row_is_not_this_physicians(self):
        # Another doctor's appointment id simply returns no row — that is the IDOR guard.
        assert get_appointment(_chain([]), _PHYS, _APPT) is None


class TestInsertAppointment:
    def test_minimizes_the_name_before_writing(self):
        sb = _chain([{"id": _APPT}])
        insert_appointment(
            sb, _PHYS,
            patient_name="María González Torres",
            starts_at="2026-09-01T15:00:00+00:00",
            ends_at="2026-09-01T15:30:00+00:00",
        )
        written = sb.table.return_value.insert.call_args[0][0]
        assert written["patient_name"] == "María G."
        assert "González" not in written["patient_name"]
        assert written["physician_id"] == _PHYS
        assert written["status"] == "scheduled"
        assert written["source"] == "cue"

    def test_contact_is_stored_when_supplied_and_null_otherwise(self):
        sb = _chain([{"id": _APPT}])
        insert_appointment(
            sb, _PHYS, patient_name="Ana R.",
            starts_at="2026-09-01T15:00:00+00:00",
            ends_at="2026-09-01T15:30:00+00:00",
            patient_contact="ana@example.com",
        )
        assert sb.table.return_value.insert.call_args[0][0]["patient_contact"] == "ana@example.com"

        sb2 = _chain([{"id": _APPT}])
        insert_appointment(
            sb2, _PHYS, patient_name="Ana R.",
            starts_at="2026-09-01T15:00:00+00:00",
            ends_at="2026-09-01T15:30:00+00:00",
        )
        assert sb2.table.return_value.insert.call_args[0][0]["patient_contact"] is None

    def test_raises_when_the_insert_returns_nothing(self):
        with pytest.raises(AppointmentStoreError):
            insert_appointment(
                _chain([]), _PHYS, patient_name="Ana R.",
                starts_at="2026-09-01T15:00:00+00:00",
                ends_at="2026-09-01T15:30:00+00:00",
            )


class TestUpdates:
    def test_move_keeps_the_previous_window_and_stays_active(self):
        sb = _chain([{"id": _APPT}])
        apply_move(
            sb, _PHYS, _APPT,
            starts_at="2026-09-02T15:00:00+00:00",
            ends_at="2026-09-02T15:30:00+00:00",
            previous_starts_at="2026-09-01T15:00:00+00:00",
            previous_ends_at="2026-09-01T15:30:00+00:00",
        )
        patch = sb.table.return_value.update.call_args[0][0]
        assert patch["status"] == "moved"
        assert patch["status"] in ACTIVE_STATUSES, (
            "'moved' is a history marker, not a terminal state — a moved "
            "appointment is still on the doctor's book."
        )
        assert patch["previous_starts_at"] == "2026-09-01T15:00:00+00:00"
        sb.table.return_value.eq.assert_any_call("physician_id", _PHYS)

    def test_cancel_is_a_status_transition_never_a_delete(self):
        sb = _chain([{"id": _APPT}])
        mark_cancelled(sb, _PHYS, _APPT)
        patch = sb.table.return_value.update.call_args[0][0]
        assert patch["status"] == "cancelled"
        assert patch["cancelled_at"]
        sb.table.return_value.delete.assert_not_called()

    def test_set_mirror_records_the_sync_gap(self):
        sb = _chain([{"id": _APPT}])
        set_mirror(sb, _PHYS, _APPT, caldav_uid=None, needs_sync=True)
        patch = sb.table.return_value.update.call_args[0][0]
        assert patch == {
            "caldav_uid": None,
            "needs_sync": True,
            "updated_at": patch["updated_at"],
        }

    def test_update_failure_raises(self):
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("db down")
        with pytest.raises(AppointmentStoreError):
            mark_cancelled(sb, _PHYS, _APPT)
