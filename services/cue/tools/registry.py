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
    # ----- Appointments vertical — the appointment object (doctor-facing) -----
    # appointment_list is a normal READ. The other three are PURE PROPOSERS on the
    # same D-03 contract as calendar_block_time/clear_range: they NEVER write; the
    # doctor's Confirm click drives POST /cue/appointments/confirm-write.
    # NO physician_id, NO confirmed, and NO patient contact property anywhere.
    CueNeutralTool(
        name="appointment_list",
        description=(
            "Returns the authenticated physician's UPCOMING appointments, soonest "
            "first: date, time, patient first name + last initial, status, and the "
            "appointment id. Cancelled appointments are excluded. "
            "Use when the doctor asks who is coming in, what their day or week "
            "looks like, or before proposing a move or cancellation — the "
            "appointment id you need for those comes from this tool. "
            "Never accepts a physician_id argument — scope is always the "
            "authenticated session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max appointments to return.  Default 10, max 20.",
                }
            },
            "required": [],
        },
    ),
    CueNeutralTool(
        name="appointment_create",
        description=(
            "PROPOSES booking a new appointment for the authenticated physician. "
            "This tool NEVER writes — it returns a confirm card for the human to "
            "approve. The appointment is created, and mirrored to the doctor's "
            "calendar, only AFTER the physician clicks Confirm in the UI. Do NOT "
            "claim or assume the appointment is booked. "
            "The patient is NOT notified by this build — do not tell the doctor "
            "the patient has been informed. "
            "Give the patient's name as first name plus last initial (e.g. 'María "
            "G.'); it is stored minimized either way. Do NOT pass a patient email, "
            "phone number, or any other identifier — there is no field for one. "
            "Never accepts a physician_id or confirmed argument."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "patient_name": {
                    "type": "string",
                    "description": (
                        "Patient's first name plus last initial (e.g. 'María G.'). "
                        "No full legal names, no other identifiers."
                    ),
                },
                "start_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the appointment start, in the "
                        "physician's LOCAL timezone (the same zone as the "
                        "current-time reference above), e.g. '2026-09-01T09:00:00'. "
                        "No offset/'Z' needed."
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the appointment end, in the "
                        "physician's LOCAL timezone. No offset/'Z' needed."
                    ),
                },
            },
            "required": ["patient_name", "start_iso", "end_iso"],
        },
    ),
    CueNeutralTool(
        name="appointment_move",
        description=(
            "PROPOSES moving an existing appointment to a new time. This tool NEVER "
            "writes — it returns a confirm card for the human to approve; the move "
            "happens only AFTER the physician clicks Confirm. Do NOT claim or assume "
            "the appointment has moved, and do NOT tell the doctor the patient has "
            "been notified — this build does not notify patients. "
            "Get appointment_id from appointment_list; never invent one. Cue can "
            "only move appointments Cue created. "
            "Never accepts a physician_id or confirmed argument."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "appointment_id": {
                    "type": "string",
                    "description": "The appointment id, as returned by appointment_list.",
                },
                "start_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the NEW start, in the physician's "
                        "LOCAL timezone (no offset/'Z' needed)."
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime for the NEW end, in the physician's "
                        "LOCAL timezone (no offset/'Z' needed)."
                    ),
                },
            },
            "required": ["appointment_id", "start_iso", "end_iso"],
        },
    ),
    CueNeutralTool(
        name="appointment_cancel",
        description=(
            "PROPOSES cancelling an appointment. This tool NEVER writes — it returns "
            "a confirm card for the human to approve; the cancellation happens only "
            "AFTER the physician clicks Confirm. Do NOT claim or assume the "
            "appointment is cancelled, and do NOT tell the doctor the patient has "
            "been notified — this build does not notify patients. "
            "Get appointment_id from appointment_list; never invent one. Cue can "
            "only cancel appointments Cue created. "
            "Never accepts a physician_id or confirmed argument."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "appointment_id": {
                    "type": "string",
                    "description": "The appointment id, as returned by appointment_list.",
                }
            },
            "required": ["appointment_id"],
        },
    ),
    # ----- Phase 24 — Cue clinical DECISION-SUPPORT surface -----
    # NAMING / LEGAL (Hector, 2026-06-29): a doctor-support tool. It is NEVER named or
    # framed as an "(official) diagnosis" — it returns ranked clinical CONSIDERATIONS
    # for the physician to weigh. A NORMAL tool (NOT a confirm proposer): its result is
    # additively surfaced to the UI as a structured card AND the loop continues so Cue
    # narrates a walkthrough and the doctor can keep conversing. NO physician_id arg.
    CueNeutralTool(
        name="clinical_decision_support",
        description=(
            "Generates ranked clinical CONSIDERATIONS (possible conditions to weigh) from a "
            "DE-IDENTIFIED clinical presentation the physician provides. Use ONLY when a "
            "physician explicitly asks for clinical decision support, considerations, or 'what "
            "could this be' for a case. "
            "The presentation must contain NO patient-identifying information (no names, contact "
            "details, dates, or identifiers) — only symptoms, history, and findings. "
            "After the tool returns, give a brief conversational walkthrough of the considerations "
            "(highlight the leading ones and any red flags) and invite follow-up questions — the "
            "full ranked list is already shown to the physician as a card, so summarize rather than "
            "re-reading every item. This is clinical DECISION SUPPORT only: present it as "
            "considerations to support the physician's judgment — never state or imply it is the "
            "patient's diagnosis. "
            "Never accepts a physician_id argument — scope is always the authenticated session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "presentation": {
                    "type": "string",
                    "description": (
                        "The DE-IDENTIFIED clinical presentation: symptoms, history, and "
                        "exam/lab findings. Must contain NO patient identifiers."
                    ),
                },
                "age_range": {
                    "type": "string",
                    "description": "Optional age range if clinically relevant (e.g. '30-40', 'pediatric', 'elderly').",
                },
                "sex": {
                    "type": "string",
                    "description": "Optional biological sex if clinically relevant.",
                },
            },
            "required": ["presentation"],
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

    if tool_name == "appointment_list":
        # Appointments vertical — READ. limit hard-capped at 20 like the other lists.
        from services.cue.tools.executors import appointment_list
        limit = int(safe_input.get("limit", 10))
        return await appointment_list(physician_id=physician_id, limit=min(limit, 20))

    if tool_name in ("appointment_create", "appointment_move", "appointment_cancel"):
        # Appointments vertical — PURE PROPOSERS (D-03). Each returns ONLY a
        # confirm-card payload; never writes. Args are pulled by name rather than
        # **safe_input so an unexpected model key (a hallucinated patient_email,
        # say) is dropped here instead of becoming a TypeError the model has to
        # interpret — and can never reach an executor parameter.
        from services.cue.tools import executors as _appt
        if tool_name == "appointment_create":
            return await _appt.appointment_create(
                physician_id=physician_id,
                patient_name=str(safe_input.get("patient_name", "")).strip(),
                start_iso=str(safe_input.get("start_iso", "")).strip(),
                end_iso=str(safe_input.get("end_iso", "")).strip(),
                locale=locale,
            )
        if tool_name == "appointment_move":
            return await _appt.appointment_move(
                physician_id=physician_id,
                appointment_id=str(safe_input.get("appointment_id", "")).strip(),
                start_iso=str(safe_input.get("start_iso", "")).strip(),
                end_iso=str(safe_input.get("end_iso", "")).strip(),
                locale=locale,
            )
        return await _appt.appointment_cancel(
            physician_id=physician_id,
            appointment_id=str(safe_input.get("appointment_id", "")).strip(),
            locale=locale,
        )

    if tool_name == "clinical_decision_support":
        # Phase 24 — Cue clinical decision-support surface. presentation is the only
        # functional input (DE-IDENTIFIED); age_range/sex are optional. NO physician_id
        # from tool_input (stripped above + not in schema); scope is the session id.
        from services.cue.tools.executors import clinical_decision_support
        presentation = str(safe_input.get("presentation", "")).strip()
        age_range = safe_input.get("age_range")
        sex = safe_input.get("sex")
        return await clinical_decision_support(
            physician_id=physician_id,
            presentation=presentation,
            age_range=age_range,
            sex=sex,
        )

    # Unknown tool — raise so the engine returns an is_error tool_result
    raise ValueError(f"Unknown tool: {tool_name!r}")
