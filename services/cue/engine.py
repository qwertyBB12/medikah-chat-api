"""
services/cue/engine.py
-----------------------
Medikah Cue — hand-rolled multi-step tool_use / tool_result agentic loop.

Python port of BeNeXT lib/companion/engine.ts — with the CRITICAL addition of
the tool-use loop.  The BeNeXT engine at line 434 is single-shot: it calls
adapter.stream() with NO tools key and has no tool_use detection.  Phase 22
adds the loop.

CUE-03: run_cue_turn() — the tool_use / tool_result loop
---------------------------------------------------------
Drives the multi-step exchange:
  1. Call adapter.complete() with NEUTRAL_TOOLS (non-streaming — tool_use blocks
     do NOT appear in the delta stream; complete() is required for detection,
     per AI-SPEC §3 Pitfall #4).
  2. stop_reason == "end_turn"  → assemble final text and return.
  3. stop_reason == "tool_use"  → execute each tool_use block via dispatch_tool()
     (physician_id is ALWAYS from the session, never from tool_input — CUE-11),
     append assistant content + tool_result user message, loop.
  4. Any other stop_reason      → return gracefully with whatever text exists.
  5. max_tool_rounds cap        → return the safe limit message (no runaway).

TOOL_RESULT ORDERING (AI-SPEC §3 Pitfall #3 — HARD API REQUIREMENT)
---------------------------------------------------------------------
tool_result blocks MUST be first in the user-reply content array.  Any text
block placed before a tool_result causes an Anthropic API 400 error.
This is enforced by construction: tool_results are built as a list and sent as
the entire content of the user reply (no mixed-in text blocks).

EXECUTOR EXCEPTION HANDLING
----------------------------
An exception in an executor returns an is_error=True tool_result to the model
(the model adapts and continues the conversation).  The loop does NOT crash.

PROVIDER NEUTRALITY (CUE-02)
------------------------------
engine.py imports ONLY the CueModelAdapter contract, the CueNeutralTool neutral
type, and the NEUTRAL_TOOLS + dispatch_tool registry.  No provider-SDK types
appear here.  The adapter translates internally.

RETURN CONTRACT
---------------
run_cue_turn() returns (final_text: str, usage_totals: dict, pending_confirm: dict | None).
The caller (cue_routes.py) streams final_text to the client via adapter.stream()
on the final text-only turn (AI-SPEC §4b.2: stream for UX on the final turn).

D-03 SURFACING MECHANISM (Plan 23-04 — the H1 fix)
---------------------------------------------------
The block/clear model tools are PURE PROPOSERS: their executor returns ONLY a
json.dumps confirm-card payload {kind:'confirm', action, title, summary,
start_iso, end_iso} as the tool_result content. When run_cue_turn sees a
tool_result that parses to {kind:'confirm'}, it STOPS the loop IMMEDIATELY and
returns that dict as the THIRD value pending_confirm — it does NOT append the
confirm tool_result back into working_messages (that re-entry is exactly what
made the card re-emerge as model prose). /cue/chat then emits pending_confirm as
a structured `\x1e` sentinel line so CueSurface renders the confirm card off the
parsed payload, never off model prose. When no confirm payload appears,
pending_confirm is None and behavior is identical to Phase 22.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

from services.cue.adapter import CueModelAdapter, CueNeutralTool, SystemCacheStrategy
from services.cue.tools.registry import NEUTRAL_TOOLS, dispatch_tool

logger = logging.getLogger(__name__)

# Safety cap — prevents runaway agentic loops (AI-SPEC §6 round-cap guardrail).
_DEFAULT_MAX_TOOL_ROUNDS = 5

# Message shown to the physician when the round cap is hit.
_ROUND_CAP_MESSAGE = "[Cue reached the tool-call limit for this turn.]"


# ---------------------------------------------------------------------------
# run_cue_turn — the tool_use / tool_result loop (CUE-03)
# ---------------------------------------------------------------------------


async def run_cue_turn_streaming(
    adapter: CueModelAdapter,
    *,
    model: str,
    system_prompt: str,
    messages: list[dict],
    physician_id: str,               # from verified session — NEVER from tool args (CUE-11)
    locale: str = "es",
    max_tokens: int = 1024,
    max_tool_rounds: int = _DEFAULT_MAX_TOOL_ROUNDS,
    tools: Optional[list[CueNeutralTool]] = None,
    system_cache_strategy: SystemCacheStrategy = None,
) -> AsyncIterator[dict]:
    """
    Drive the tool_use / tool_result agentic loop, STREAMING text deltas (CUE-03
    TTFT). This is the Phase-23 TTFT optimization done correctly: each round is a
    single `adapter.stream_turn()` call that yields live text deltas AND the full
    terminal message (incl. tool_use + usage) — so the final turn streams with NO
    second model round-trip (the double-call trap the naive "re-stream
    final_messages" approach would have hit).

    Parameters mirror the legacy run_cue_turn(), plus:
    tools : the tool set offered to the model. Defaults to NEUTRAL_TOOLS. Pass an
            empty list to DISABLE tools entirely (the opening greeting never
            proposes a write, so it streams tool-free).

    Yields
    ------
    {"type": "delta", "text": str}
        A user-facing text delta, as the model generates it. The route forwards
        these to the client immediately (TTFT) and accumulates them for the judge.
    {"type": "tool", "phase": "start"|"end", "tool": str, ...}
        A "thinking trace" frame, emitted as the agentic loop STARTS and FINISHES
        each tool call (PURELY ADDITIVE — see THINKING TRACE below). The route
        forwards these as \x1f-framed lines so the client can render cascading
        terminal-style steps before the spoken answer.
    {"type": "done", "final_text": str, "usage": dict, "pending_confirm": dict|None}
        Exactly once, terminal. final_text is the assembled terminal text (== the
        concatenation of the streamed deltas in the common case); usage is the
        accumulated {"input_tokens","output_tokens"} across all rounds; and
        pending_confirm carries the D-03 {kind:'confirm', ...} payload when a
        block/clear PROPOSER stopped the loop (else None).

    THINKING TRACE (tool-event frames — wire-spec v2, additive)
    ------------------------------------------------------------
    Immediately BEFORE a dispatch_tool() call, the loop yields
    {"type":"tool","phase":"start","tool":<name>}; immediately AFTER a SUCCESSFUL
    dispatch it yields {"type":"tool","phase":"end","tool":<name>,"ok":True} plus
    an "items":n key ONLY when n>0 (n = number of result lines starting with "- ",
    per _count_items). On an executor exception it yields
    {"type":"tool","phase":"end","tool":<name>,"ok":False} (no items). The end
    frame for a D-03 proposer is emitted BEFORE the loop break, so a proposer
    still gets its end frame. These yields are ADDITIVE: a turn with NO tool calls
    is byte-identical to before (delta/done only). The non-streaming wrapper
    run_cue_turn() reads only type=="done", so it ignores these transparently.

    D-03 / voice-parity note: every text delta the model emits is streamed live
    (including any short lead-in on a write-proposer turn). The actual calendar
    mutation NEVER happens here — block/clear tools are PURE PROPOSERS and the
    sole write path is the explicit /cue/calendar/confirm-write endpoint (the
    doctor's Confirm tap). On a pending_confirm, the client stops TTS on the
    sentinel and speaks a controlled, templated proposal line.
    """
    active_tools = NEUTRAL_TOOLS if tools is None else tools
    usage_totals: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    working_messages: list[dict] = list(messages)

    for round_idx in range(max_tool_rounds):
        logger.debug(
            "[cue:engine] tool round %d/%d physician=%s",
            round_idx + 1,
            max_tool_rounds,
            physician_id,
        )

        # ONE streaming model call per round. stream_turn yields live text deltas
        # AND the terminal message (with tool_use blocks + usage) — so we get TTFT
        # while still being able to inspect tool_use afterwards (AI-SPEC §3
        # Pitfall #4 is satisfied: tool_use is read from the final message, not
        # from the delta stream). Higher max_tokens for tool-detection rounds
        # (tools may emit structured reasoning — AI-SPEC §4b.3).
        tool_detection_max_tokens = max(max_tokens, 2048)

        round_text_parts: list[str] = []
        message_event: Optional[dict] = None
        async for ev in adapter.stream_turn(
            model=model,
            system_prompt=system_prompt,
            messages=working_messages,
            tools=active_tools,
            max_tokens=tool_detection_max_tokens,
            system_cache_strategy=system_cache_strategy,
        ):
            etype = ev.get("type")
            if etype == "text":
                delta = ev.get("text", "")
                if delta:
                    round_text_parts.append(delta)
                    yield {"type": "delta", "text": delta}
            elif etype == "message":
                message_event = ev

        # Defensive: a well-behaved stream_turn always yields a terminal message.
        if message_event is None:
            yield {
                "type": "done",
                "final_text": "".join(round_text_parts),
                "usage": usage_totals,
                "pending_confirm": None,
            }
            return

        # Accumulate usage (stream_turn normalizes to a dict — CUE-02 neutrality).
        usage = message_event.get("usage", {}) or {}
        usage_totals["input_tokens"] += usage.get("input_tokens", 0)
        usage_totals["output_tokens"] += usage.get("output_tokens", 0)

        stop_reason: str = message_event.get("stop_reason", "end_turn") or "end_turn"
        content: list[Any] = message_event.get("content", []) or []

        # ------------------------------------------------------------------
        # TERMINAL: end_turn — no more tool calls
        # ------------------------------------------------------------------
        if stop_reason == "end_turn":
            final_text = _assemble_text(content) or "".join(round_text_parts)
            logger.debug(
                "[cue:engine] end_turn after %d round(s) physician=%s chars=%d",
                round_idx + 1,
                physician_id,
                len(final_text),
            )
            yield {
                "type": "done",
                "final_text": final_text,
                "usage": usage_totals,
                "pending_confirm": None,
            }
            return

        # ------------------------------------------------------------------
        # GRACEFUL: unexpected stop_reason (e.g. "max_tokens", "stop_sequence")
        # ------------------------------------------------------------------
        if stop_reason != "tool_use":
            logger.warning(
                "[cue:engine] unexpected stop_reason=%r round=%d physician=%s — returning gracefully",
                stop_reason,
                round_idx + 1,
                physician_id,
            )
            final_text = _assemble_text(content) or "".join(round_text_parts)
            yield {
                "type": "done",
                "final_text": final_text,
                "usage": usage_totals,
                "pending_confirm": None,
            }
            return

        # ------------------------------------------------------------------
        # TOOL USE: execute each tool_use block and build tool_result replies
        # ------------------------------------------------------------------

        # 1. Append the assistant's full response (including tool_use blocks)
        #    to the working message history.
        working_messages.append({
            "role": "assistant",
            "content": content,   # includes both text and tool_use blocks
        })

        # 2. Execute each tool — scope is ALWAYS auth.physician_id (CUE-11).
        tool_results: list[dict] = []
        pending_confirm: Optional[dict] = None
        # best-available text for D-03 stop (== the streamed deltas this round).
        assistant_text = _assemble_text(content) or "".join(round_text_parts)
        for block in content:
            block_type = _get_block_type(block)
            if block_type != "tool_use":
                continue

            tool_name = _get_block_attr(block, "name")
            tool_input = _get_block_attr(block, "input") or {}
            tool_use_id = _get_block_attr(block, "id")

            # THINKING TRACE: announce the tool call STARTING (additive frame).
            yield {"type": "tool", "phase": "start", "tool": tool_name}

            try:
                result_text = await dispatch_tool(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    physician_id=physician_id,  # session-derived; model cannot override
                    locale=locale,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_text,
                })
                logger.debug(
                    "[cue:engine] tool %r OK physician=%s chars=%d",
                    tool_name,
                    physician_id,
                    len(result_text),
                )

                # THINKING TRACE: announce the SUCCESSFUL finish (additive frame).
                # items=n only when n>0 (n = "- " result lines). This MUST run
                # before the D-03 break below so a proposer still gets its end frame.
                end_frame: dict[str, Any] = {
                    "type": "tool", "phase": "end", "tool": tool_name, "ok": True,
                }
                n_items = _count_items(result_text)
                if n_items:
                    end_frame["items"] = n_items
                yield end_frame

                # D-03 SURFACING (Plan 23-04): a block/clear PROPOSER returns a
                # json.dumps({kind:'confirm', ...}) tool_result. Detect it, capture
                # it as pending_confirm, and STOP the loop — do NOT feed the confirm
                # JSON back to the model (that re-entry made the card become prose).
                parsed = _try_parse_confirm(result_text)
                if parsed is not None:
                    pending_confirm = parsed
                    logger.debug(
                        "[cue:engine] confirm card from tool %r — stopping loop physician=%s",
                        tool_name,
                        physician_id,
                    )
                    break
            except Exception as exc:
                # Executor exception → is_error tool_result (AI-SPEC §3 Pitfall #2).
                # The loop continues; the model adapts.
                logger.warning(
                    "[cue:engine] tool %r raised — returning is_error physician=%s: %s",
                    tool_name,
                    physician_id,
                    exc,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"Error: {exc}",
                    "is_error": True,
                })

                # THINKING TRACE: announce the FAILED finish (additive frame).
                # ok=False, no items — a failed tool produced no result list.
                yield {"type": "tool", "phase": "end", "tool": tool_name, "ok": False}

        # 2b. D-03 STOP: a confirm card was proposed — return it structurally as
        #     pending_confirm WITHOUT appending the confirm tool_result back into
        #     working_messages (no model re-entry → it can never re-emerge as prose).
        if pending_confirm is not None:
            yield {
                "type": "done",
                "final_text": assistant_text,
                "usage": usage_totals,
                "pending_confirm": pending_confirm,
            }
            return

        # 3. tool_result blocks MUST come FIRST in the content array
        #    (AI-SPEC §3 Pitfall #3 — Anthropic hard API requirement).
        #    No text blocks mixed in before tool_results.
        working_messages.append({
            "role": "user",
            "content": tool_results,  # tool_results only — satisfies ordering requirement
        })

    # ------------------------------------------------------------------
    # ROUND CAP: max_tool_rounds exhausted (AI-SPEC §6 guardrail)
    # ------------------------------------------------------------------
    logger.warning(
        "[cue:engine] round cap (%d) reached physician=%s", max_tool_rounds, physician_id
    )
    yield {
        "type": "done",
        "final_text": _ROUND_CAP_MESSAGE,
        "usage": usage_totals,
        "pending_confirm": None,
    }


async def run_cue_turn(
    adapter: CueModelAdapter,
    *,
    model: str,
    system_prompt: str,
    messages: list[dict],
    physician_id: str,               # from verified session — NEVER from tool args (CUE-11)
    max_tokens: int = 1024,
    max_tool_rounds: int = _DEFAULT_MAX_TOOL_ROUNDS,
    system_cache_strategy: SystemCacheStrategy = None,
) -> tuple[str, dict, Optional[dict]]:
    """
    Non-streaming wrapper over run_cue_turn_streaming() — preserves the original
    Phase-22 return contract for callers/tests that want the assembled result.

    Returns
    -------
    (final_text: str, usage_totals: dict, pending_confirm: dict | None)
        Identical semantics to the pre-streaming implementation: the assembled
        terminal text (or proposer text when a confirm card stops the loop), the
        accumulated usage, and the D-03 pending_confirm payload (else None).

    The streaming caller (cue_routes.py) consumes run_cue_turn_streaming()
    directly to forward `delta` events to the client as they arrive; this wrapper
    simply drains the same generator and returns the terminal `done` event.
    """
    final_text = ""
    usage_totals: dict = {"input_tokens": 0, "output_tokens": 0}
    pending_confirm: Optional[dict] = None
    async for ev in run_cue_turn_streaming(
        adapter,
        model=model,
        system_prompt=system_prompt,
        messages=messages,
        physician_id=physician_id,
        max_tokens=max_tokens,
        max_tool_rounds=max_tool_rounds,
        system_cache_strategy=system_cache_strategy,
    ):
        if ev.get("type") == "done":
            final_text = ev.get("final_text", "")
            usage_totals = ev.get("usage", usage_totals)
            pending_confirm = ev.get("pending_confirm")
    return final_text, usage_totals, pending_confirm


def _count_items(result_text: Any) -> Optional[int]:
    """Return the number of result lines that start with "- ", or None when 0.

    The read executors (calendar_read_day, inbox_read_recent) format their
    results as a header line followed by one "- {item}" line per row. This counts
    those rows so the "thinking trace" end frame can show "✓ 14 eventos". A header
    line or an empty/"no results" string yields no "- " lines → None (so the
    caller OMITS the items key, per the wire spec). Non-string tool_results (the
    D-03 {kind:'confirm'} JSON payload) return None without crashing.
    """
    if not isinstance(result_text, str):
        return None
    n = sum(1 for line in result_text.splitlines() if line.startswith("- "))
    return n if n > 0 else None


def _try_parse_confirm(result_text: Any) -> Optional[dict]:
    """Return the confirm-card dict if result_text is a {kind:'confirm'} JSON string.

    The block/clear proposer executors are the ONLY tool_results that are
    JSON-encoded (the read executors return plain prose). A tool_result that
    json.loads to a dict with kind=='confirm' is a D-03 confirm card; anything
    else (prose, malformed JSON, a JSON object without kind=='confirm') returns
    None so the loop proceeds normally.
    """
    if not isinstance(result_text, str):
        return None
    stripped = result_text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get("kind") == "confirm":
        return parsed
    return None


# ---------------------------------------------------------------------------
# Duck-typed response field accessors (CUE-02 provider neutrality)
# ---------------------------------------------------------------------------
# These helpers abstract over both the real AnthropicAdapter response (SDK
# Message object with attribute access) and the DummyAdapter response (plain
# dict).  All access is duck-typed — no provider SDK types are imported here.


def _get_stop_reason(response: Any) -> str:
    """Extract stop_reason from a provider response (duck-typed)."""
    if isinstance(response, dict):
        return response.get("stop_reason", "end_turn")
    return getattr(response, "stop_reason", "end_turn") or "end_turn"


def _get_content(response: Any) -> list[Any]:
    """Extract content list from a provider response (duck-typed)."""
    if isinstance(response, dict):
        return response.get("content", [])
    return getattr(response, "content", []) or []


def _get_usage(response: Any) -> dict[str, int]:
    """Extract usage dict from a provider response (duck-typed).

    Handles both dict responses (DummyAdapter) and SDK Message objects
    (which expose a usage attribute with input_tokens / output_tokens).
    """
    if isinstance(response, dict):
        return response.get("usage", {})
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    # SDK usage object — access as attributes (duck-typed; no SDK type import)
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
    }


def _get_block_type(block: Any) -> str:
    """Extract block.type (duck-typed — works for dict or SDK object)."""
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "")


def _get_block_attr(block: Any, attr: str) -> Any:
    """Extract a named attribute from a content block (duck-typed)."""
    if isinstance(block, dict):
        return block.get(attr)
    return getattr(block, attr, None)


def _assemble_text(content: list[Any]) -> str:
    """Concatenate all text blocks from a content list."""
    parts: list[str] = []
    for block in content:
        if _get_block_type(block) == "text":
            text = _get_block_attr(block, "text") or ""
            parts.append(text)
    return "".join(parts)
