"""
services/cue/tools/executors.py
---------------------------------
No-op executor stubs for Phase 22 Cue tools (CUE-03 contract).

Phase 23 HANDS plans fill these with real implementations:
  - HANDS-03: calendar_read_day + availability_read (Supabase reads)
  - HANDS-04: inquiry_list_recent (Supabase reads)

CUE-11 IDOR DISCIPLINE — MANDATORY FOR ALL EXECUTORS
------------------------------------------------------
Every executor:
  - Accepts physician_id ONLY as an explicit keyword argument from dispatch_tool()
    (which sources it from the verified FastAPI session, auth.physician_id).
  - Does NOT accept physician_id (or slug) anywhere in its **tool_input kwargs.
  - The no-op stubs here enforce this contract by design — they echo back the
    session-derived physician_id in the response, not any model-supplied value.

Phase 23 implementations MUST maintain this contract unchanged.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# calendar_read_day executor stub (Phase 23 HANDS-03)
# ---------------------------------------------------------------------------


async def calendar_read_day(
    physician_id: str,  # session-derived — NEVER from tool_input
    date: str,          # functional arg from tool_input only
) -> str:
    """
    Return the physician's calendar events for `date`.

    Phase 22: no-op stub — returns a benign placeholder.
    Phase 23 HANDS-03: reads physician_availability / appointments scoped to physician_id.

    physician_id is sourced exclusively from dispatch_tool() (session-derived).
    The 'date' parameter is the ONLY functional arg accepted from tool_input.
    """
    logger.debug(
        "[cue:tools] calendar_read_day stub: physician=%s date=%s", physician_id, date
    )
    # Phase 22 stub — real implementation wired in Phase 23 HANDS-03
    return (
        f"[Phase 22 stub] No calendar data available yet for {date}. "
        f"Calendar integration will be available in the next release."
    )


# ---------------------------------------------------------------------------
# availability_read executor stub (Phase 23 HANDS-03)
# ---------------------------------------------------------------------------


async def availability_read(
    physician_id: str,  # session-derived — NEVER from tool_input
) -> str:
    """
    Return the physician's weekly availability grid.

    Phase 22: no-op stub — returns a benign placeholder.
    Phase 23 HANDS-03: reads physician_availability scoped to physician_id.

    physician_id is sourced exclusively from dispatch_tool() (session-derived).
    No functional args accepted from tool_input for this tool.
    """
    logger.debug(
        "[cue:tools] availability_read stub: physician=%s", physician_id
    )
    # Phase 22 stub — real implementation wired in Phase 23 HANDS-03
    return (
        "[Phase 22 stub] Availability data not yet integrated. "
        "Availability integration will be available in the next release."
    )


# ---------------------------------------------------------------------------
# inquiry_list_recent executor stub (Phase 23 HANDS-04)
# ---------------------------------------------------------------------------


async def inquiry_list_recent(
    physician_id: str,  # session-derived — NEVER from tool_input
    limit: int = 5,     # functional arg from tool_input, capped by dispatcher
) -> str:
    """
    Return the most recent patient inquiries for the physician.

    Phase 22: no-op stub — returns a benign placeholder.
    Phase 23 HANDS-04: reads patient_inquiries scoped to physician_id.

    physician_id is sourced exclusively from dispatch_tool() (session-derived).
    'limit' is the ONLY functional arg accepted from tool_input (capped at 20
    by dispatch_tool before it arrives here).
    """
    logger.debug(
        "[cue:tools] inquiry_list_recent stub: physician=%s limit=%d",
        physician_id,
        limit,
    )
    # Phase 22 stub — real implementation wired in Phase 23 HANDS-04
    return (
        f"[Phase 22 stub] No inquiry data available yet (requested up to {limit}). "
        f"Inquiry integration will be available in the next release."
    )
