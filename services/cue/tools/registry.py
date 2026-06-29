"""
services/cue/tools/registry.py
--------------------------------
Neutral tool registry for Medikah Cue (CUE-03 / CUE-11).

NEUTRAL_TOOLS — the three Phase 22 contract stubs (calendar_read_day,
availability_read, inquiry_list_recent).  These define the API surface the
Phase-23 HANDS plans will implement.

dispatch_tool(tool_name, tool_input, physician_id) — the ONLY path through
which a tool executor is reached.  physician_id is a dispatcher parameter
sourced from the verified FastAPI session; it is NEVER read from tool_input.

CUE-11 IDOR GUARD — BY CONSTRUCTION
-------------------------------------
None of the tool input_schemas below declares a 'physician_id' or 'slug'
property.  A model-supplied identity arg has no field to land in — there is
no code path that reads it.  The IDOR guard is structural, not validation-based.

Key rule (AI-SPEC §4 "Key rule for all executors"):
  The function signature of each executor accepts physician_id from the
  dispatcher only, never from tool_input.  Any tool definition that includes
  a physician_id parameter is an IDOR and MUST be rejected at code review.
"""

from __future__ import annotations

from services.cue.adapter import CueNeutralTool

# ---------------------------------------------------------------------------
# Phase 22 tool contract stubs (AI-SPEC §4 Tool Use Configuration)
# Phase 23 HANDS plans fill the real executor bodies.
# ---------------------------------------------------------------------------

NEUTRAL_TOOLS: list[CueNeutralTool] = [
    CueNeutralTool(
        name="calendar_read_day",
        description=(
            "Reads the authenticated physician's calendar for a given date. "
            "Returns a list of events with time, title, and duration. "
            "Use when the doctor asks what is on their schedule. "
            "Never accepts a physician_id argument — scope is always the "
            "authenticated session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": (
                        "ISO 8601 date (YYYY-MM-DD) in the physician's local timezone."
                    ),
                }
            },
            "required": ["date"],
        },
    ),
    CueNeutralTool(
        name="availability_read",
        description=(
            "Returns the authenticated physician's weekly availability grid "
            "(days and hours they have set as available for appointments). "
            "Use when the doctor asks about their schedule or open slots."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    CueNeutralTool(
        name="inquiry_list_recent",
        description=(
            "Returns the most recent patient inquiries pending for the authenticated "
            "physician.  Returns inquiry IDs, patient first-name only (no PHI), "
            "status, and date. "
            "Use when the doctor asks how many patients are waiting or who is in "
            "their queue."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max inquiries to return.  Default 5, max 20.",
                }
            },
            "required": [],
        },
    ),
    # ----- Phase 23 HANDS-03/04 (Plan 23-04) — calendar block/clear PROPOSERS -----
    # D-03: these tools NEVER write. The executor ALWAYS returns ONLY a confirm-card
    # payload; the human approves in the UI and the route-level confirm-write
    # endpoint performs the actual mutation. NO physician_id, NO confirmed property.
    CueNeutralTool(
        name="calendar_block_time",
        description=(
            "PROPOSES blocking a time range on the authenticated physician's calendar. "
            "This tool NEVER writes — it returns a confirm card for the human to "
            "approve. The actual block is written only AFTER the physician clicks "
            "Confirm in the UI (a separate authenticated route). Do NOT claim or "
            "assume the block is done. "
            "Never accepts a physician_id or confirmed argument — scope is always the "
            "authenticated session and confirmation is a UI action."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the block start, in the physician's "
                        "LOCAL timezone (the same zone as the current-time reference "
                        "above), e.g. '2026-07-01T15:00:00'. No offset/'Z' needed."
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the block end, in the physician's "
                        "LOCAL timezone, e.g. '2026-07-01T15:30:00'. No offset/'Z' needed."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Event title (e.g. 'Blocked by Cue').",
                },
            },
            "required": ["start_iso", "end_iso", "title"],
        },
    ),
    CueNeutralTool(
        name="calendar_clear_range",
        description=(
            "PROPOSES clearing Cue-created blocks in a time range on the authenticated "
            "physician's calendar. This tool NEVER writes — it returns a confirm card "
            "for the human to approve. The actual clear runs only AFTER the physician "
            "clicks Confirm in the UI; it deletes ONLY Cue-created events and never "
            "the physician's own appointments. Do NOT claim or assume the clear is done. "
            "Never accepts a physician_id or confirmed argument."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the range start, in the physician's "
                        "LOCAL timezone (no offset/'Z' needed)."
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the range end, in the physician's "
                        "LOCAL timezone (no offset/'Z' needed)."
                    ),
                },
            },
            "required": ["start_iso", "end_iso"],
        },
    ),
    # ----- Phase 23 HANDS-02/04 (Plan 23-02) — read-only inbox headers -----
    CueNeutralTool(
        name="inbox_read_recent",
        description=(
            "Reads the authenticated physician's most recent inbox message HEADERS "
            "(subject, sender, date) — READ-ONLY: it never marks mail as read and "
            "never reads message bodies. "
            "Use when the doctor asks what is new in their inbox or who has emailed "
            "them recently. "
            "Never accepts a physician_id argument — scope is always the "
            "authenticated session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max messages to return.  Default 10, max 20.",
                }
            },
            "required": [],
        },
    ),
]

# ---------------------------------------------------------------------------
# Session-scoped dispatcher (CUE-11 IDOR guard)
# ---------------------------------------------------------------------------


def _safe_tool_input(tool_input: dict) -> dict:
    """
    Strip any identity keys the model may have hallucinated into tool_input.

    CUE-11: No NEUTRAL_TOOLS schema declares 'physician_id' or 'slug', but a
    model can still put any key in tool_input.  We strip identity keys here
    as a defence-in-depth measure so they can never reach an executor via
    **tool_input unpacking — even if a future schema accidentally adds one.

    D-03 defence-in-depth (Plan 23-04): 'confirmed' is ALSO stripped here. The
    block/clear proposer executors have no write branch and no `confirmed`
    parameter, so a hallucinated confirmed=true has nowhere to land — but we
    strip it anyway so a one-shot tool_use can never even appear to authorize a
    write (the sole mutation path is the route-level confirm-write endpoint).
    """
    _IDENTITY_KEYS = frozenset(
        {"physician_id", "slug", "doctor_id", "user_id", "confirmed"}
    )
    return {k: v for k, v in tool_input.items() if k not in _IDENTITY_KEYS}


async def dispatch_tool(
    *,
    tool_name: str,
    tool_input: dict,
    physician_id: str,  # ALWAYS from the verified session — never from tool_input
    locale: str = "es",
) -> str:
    """
    Route a tool_use block to the appropriate executor.

    physician_id is a dispatcher parameter (session-derived by the engine from
    auth.physician_id) — it is NEVER read from tool_input.  Identity keys are
    stripped from tool_input before expansion (defence-in-depth: if a model
    hallucinates a physician_id key into tool_input, it is removed here so it
    cannot reach an executor via **tool_input unpacking).

    Returns a plain string result to be placed in a tool_result content block.
    Raises exceptions on unknown tools or executor errors — the caller
    (engine.run_cue_turn) catches these and returns an is_error tool_result.
    """
    # Strip identity keys from tool_input (CUE-11 defence-in-depth)
    safe_input = _safe_tool_input(tool_input)

    if tool_name == "calendar_read_day":
        from services.cue.tools.executors import calendar_read_day
        return await calendar_read_day(physician_id=physician_id, **safe_input)

    if tool_name == "availability_read":
        from services.cue.tools.executors import availability_read
        return await availability_read(physician_id=physician_id)

    if tool_name == "inquiry_list_recent":
        from services.cue.tools.executors import inquiry_list_recent
        limit = int(safe_input.get("limit", 5))
        return await inquiry_list_recent(physician_id=physician_id, limit=min(limit, 20))

    if tool_name == "inbox_read_recent":
        # Phase 23 HANDS-02/04 — read-only inbox headers; limit hard-capped at 20.
        from services.cue.tools.executors import inbox_read_recent
        limit = int(safe_input.get("limit", 10))
        return await inbox_read_recent(physician_id=physician_id, limit=min(limit, 20))

    if tool_name == "calendar_block_time":
        # Phase 23 HANDS-03/04 — PURE PROPOSER (D-03). Returns ONLY a confirm-card
        # payload (JSON string); never writes. 'confirmed' is stripped above.
        from services.cue.tools.executors import calendar_block_time
        return await calendar_block_time(physician_id=physician_id, locale=locale, **safe_input)

    if tool_name == "calendar_clear_range":
        # Phase 23 HANDS-03/04 — PURE PROPOSER (D-03). Returns ONLY a confirm-card
        # payload (JSON string); never writes. 'confirmed' is stripped above.
        from services.cue.tools.executors import calendar_clear_range
        return await calendar_clear_range(physician_id=physician_id, locale=locale, **safe_input)

    # Unknown tool — raise so the engine returns an is_error tool_result
    raise ValueError(f"Unknown tool: {tool_name!r}")
