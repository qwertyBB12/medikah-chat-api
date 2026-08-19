"""
tests/cue/test_tool_executors_reads.py
--------------------------------------
availability_read (HANDS-03) and inquiry_list_recent (HANDS-04) — the two Cue
read executors backed by Medikah's OWN tables via services/physician_dashboard.

Gated here:
  - the grid/queue is really read and shaped for the model (no placeholder),
  - PHI discipline: the inquiry list carries FIRST NAME, status and date only —
    never symptoms, never the patient email,
  - a DB failure degrades to the bilingual "could not read" line instead of
    raising an is_error tool_result,
  - the per-action audit row carries counts only (HANDS-08a).

Both executors import their service lazily, so the patch target is the service
module, not a name bound in executors.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import services.cue.tools.executors as ex
from models.physician import (
    DayAvailability,
    InquiryStatus,
    PaginatedInquiries,
    PatientInquiry,
    PhysicianAvailability,
    TimeSlot,
)

_PHYS = "session-physician-abc"

_AVAILABILITY_TARGET = "services.physician_dashboard.get_physician_availability"
_INQUIRIES_TARGET = "services.physician_dashboard.get_physician_inquiries"


def _grid() -> PhysicianAvailability:
    return PhysicianAvailability(
        physician_id=_PHYS,
        timezone="America/Mexico_City",
        schedule=[
            DayAvailability(
                day="monday",
                slots=[
                    TimeSlot(start_time="09:00", end_time="13:00"),
                    TimeSlot(start_time="15:00", end_time="18:00"),
                ],
                enabled=True,
            ),
            DayAvailability(
                day="tuesday",
                slots=[TimeSlot(start_time="09:00", end_time="13:00")],
                enabled=False,
            ),
            DayAvailability(day="wednesday", slots=[], enabled=True),
        ],
    )


class TestAvailabilityRead:
    @pytest.mark.asyncio
    async def test_lists_only_days_the_doctor_offers(self):
        with patch(_AVAILABILITY_TARGET, return_value=_grid()), \
             patch.object(ex, "_write_action_audit"):
            result = await ex.availability_read(physician_id=_PHYS)

        assert "America/Mexico_City" in result
        assert "monday: 09:00–13:00, 15:00–18:00" in result
        # disabled day and slotless day are noise the model would read back as grid
        assert "tuesday" not in result
        assert "wednesday" not in result

    @pytest.mark.asyncio
    async def test_empty_grid_says_so_in_both_languages(self):
        empty = PhysicianAvailability(physician_id=_PHYS, timezone="UTC", schedule=[])
        with patch(_AVAILABILITY_TARGET, return_value=empty), \
             patch.object(ex, "_write_action_audit"):
            result = await ex.availability_read(physician_id=_PHYS)

        assert "disponibilidad" in result.lower()
        assert "availability" in result.lower()

    @pytest.mark.asyncio
    async def test_db_failure_degrades_instead_of_raising(self):
        with patch(_AVAILABILITY_TARGET, side_effect=RuntimeError("Database not configured")), \
             patch.object(ex, "_write_action_audit"):
            result = await ex.availability_read(physician_id=_PHYS)

        assert result == ex._read_unavailable_message()

    @pytest.mark.asyncio
    async def test_audit_row_carries_shape_only(self):
        with patch(_AVAILABILITY_TARGET, return_value=_grid()), \
             patch.object(ex, "_write_action_audit") as audit:
            await ex.availability_read(physician_id=_PHYS)

        physician_id, action, detail = audit.call_args[0]
        assert physician_id == _PHYS
        assert action == "cue.availability_read"
        assert detail == {"day_count": 1}


def _inquiries(total: int = 7) -> PaginatedInquiries:
    return PaginatedInquiries(
        items=[
            PatientInquiry(
                inquiry_id="inq-1",
                patient_name="María Fernanda Ruiz Gómez",
                patient_email="maria.ruiz@example.com",
                symptoms="dolor abdominal persistente desde hace tres días",
                status=InquiryStatus.PENDING,
                created_at=datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc),
            ),
        ],
        total=total,
        page=1,
        page_size=5,
    )


class TestInquiryListRecent:
    @pytest.mark.asyncio
    async def test_lists_first_name_status_and_date(self):
        with patch(_INQUIRIES_TARGET, return_value=_inquiries()), \
             patch.object(ex, "_write_action_audit"):
            result = await ex.inquiry_list_recent(physician_id=_PHYS, limit=5)

        assert "2026-06-22" in result
        assert "María" in result
        assert "pending" in result
        assert "inq-1" in result
        assert "1 of 7" in result

    @pytest.mark.asyncio
    async def test_never_leaks_symptoms_surname_or_email(self):
        """Registry contract: first name only, no PHI in the model context."""
        with patch(_INQUIRIES_TARGET, return_value=_inquiries()), \
             patch.object(ex, "_write_action_audit"):
            result = await ex.inquiry_list_recent(physician_id=_PHYS, limit=5)

        assert "dolor abdominal" not in result
        assert "example.com" not in result
        assert "Ruiz" not in result
        assert "Fernanda" not in result

    @pytest.mark.asyncio
    async def test_empty_queue_says_so_in_both_languages(self):
        empty = PaginatedInquiries(items=[], total=0, page=1, page_size=5)
        with patch(_INQUIRIES_TARGET, return_value=empty), \
             patch.object(ex, "_write_action_audit"):
            result = await ex.inquiry_list_recent(physician_id=_PHYS, limit=5)

        assert "consultas" in result.lower()
        assert "inquiries" in result.lower()

    @pytest.mark.asyncio
    async def test_db_failure_degrades_instead_of_raising(self):
        with patch(_INQUIRIES_TARGET, side_effect=RuntimeError("Database not configured")), \
             patch.object(ex, "_write_action_audit"):
            result = await ex.inquiry_list_recent(physician_id=_PHYS, limit=5)

        assert result == ex._read_unavailable_message()

    @pytest.mark.asyncio
    async def test_scoped_to_session_physician_with_page_size_floor(self):
        """CUE-11 scope, and a model-supplied limit of 0 must not make an empty page."""
        with patch(_INQUIRIES_TARGET, return_value=_inquiries()) as svc, \
             patch.object(ex, "_write_action_audit"):
            await ex.inquiry_list_recent(physician_id=_PHYS, limit=0)

        assert svc.call_args[0][0] == _PHYS
        assert svc.call_args[1]["page_size"] == 1

    @pytest.mark.asyncio
    async def test_audit_row_carries_counts_only(self):
        with patch(_INQUIRIES_TARGET, return_value=_inquiries()), \
             patch.object(ex, "_write_action_audit") as audit:
            await ex.inquiry_list_recent(physician_id=_PHYS, limit=5)

        physician_id, action, detail = audit.call_args[0]
        assert physician_id == _PHYS
        assert action == "cue.inquiry_list_recent"
        assert detail == {"limit": 5, "inquiry_count": 1}
