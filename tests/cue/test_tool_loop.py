"""
tests/cue/test_tool_loop.py
-----------------------------
Phase 22 tool-use loop correctness tests (CUE-03 / D2 eval dimension).

MERGE-BLOCKING: these tests must pass before the /cue/chat route is deployed
with the tool loop active.

Test inventory
--------------
D2-A  test_single_tool_round_terminates_in_end_turn
      A single tool_use block is issued then resolved; the loop terminates
      with a final end_turn text turn.

D2-B  test_multi_round_terminates_within_cap
      A sequence of tool_use rounds forces the loop to iterate; it terminates
      before exceeding the cap and returns the final text.

D2-C  test_round_cap_hit_returns_safe_message
      The loop is forced to exceed max_tool_rounds; it returns the safe
      "reached the tool-call limit" message (no runaway, no crash).

D2-D  test_executor_exception_returns_is_error_tool_result
      An executor that raises returns an is_error tool_result content block;
      the loop continues (does NOT crash) and terminates in end_turn.

D2-E  test_tool_result_blocks_first_in_content_array
      After a tool_use round, the working_messages user reply has
      tool_result blocks as the FIRST items in content (Pitfall #3 —
      prevents Anthropic API 400).

D2-F  test_no_tool_call_single_shot_end_turn
      A message that requires no tool produces a direct end_turn response
      (the loop exits on round 0).

D2-G  test_graceful_unexpected_stop_reason
      A stop_reason that is neither "end_turn" nor "tool_use" (e.g.
      "max_tokens") is handled gracefully — the loop returns the
      available text without crashing.

D2-H  test_usage_totals_accumulated_across_rounds
      usage_totals accumulates input_tokens + output_tokens from each
      complete() call across all rounds.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from services.cue.adapter import CueModelAdapter, CueNeutralTool, SystemCacheStrategy
from services.cue.engine import run_cue_turn, _ROUND_CAP_MESSAGE
from services.cue.tools.registry import NEUTRAL_TOOLS


# ---------------------------------------------------------------------------
# Echo tool (trivial tool for testing the loop — NOT a real Phase 23 hand)
# ---------------------------------------------------------------------------

ECHO_TOOL = CueNeutralTool(
    name="echo",
    description="Echoes the input message back. Test tool only.",
    input_schema={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to echo."}
        },
        "required": ["message"],
    },
)

ECHO_TOOLS = [ECHO_TOOL]


async def _echo_executor(physician_id: str, message: str) -> str:
    """Trivial echo tool executor — no data access, no PHI."""
    return f"[echo] {message}"


# ---------------------------------------------------------------------------
# ToolUseAdapter — test adapter that simulates tool_use / end_turn sequence
# ---------------------------------------------------------------------------


class ToolUseAdapter(CueModelAdapter):
    """
    Test double that simulates a controlled tool_use → end_turn sequence.

    Behaviour is configured via `rounds_with_tools`: a list of lists of
    (tool_name, tool_input, tool_id) tuples.  On each round_idx call:
      - If round_idx < len(rounds_with_tools) and the entry is non-empty,
        return stop_reason="tool_use" with tool_use blocks.
      - Otherwise return stop_reason="end_turn" with a text block.

    complete() returns plain dicts (duck-typed — CUE-02 provider neutrality).
    stream() yields the final text character-by-character.
    """

    def __init__(
        self,
        rounds_with_tools: list[list[tuple[str, dict, str]]] | None = None,
        final_text: str = "All done.",
        executor_raises: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        rounds_with_tools : Each entry is one "round". A non-empty list at index i
                            means round i returns tool_use; an empty list returns end_turn.
        final_text        : The text returned on the final end_turn round.
        executor_raises   : If True, simulate an executor error (the executor itself
                            raises — tested via ToolUseAdapter in combination with a
                            raising echo executor in the test).
        """
        self._rounds = rounds_with_tools or []
        self._final_text = final_text
        self._call_count = 0
        self.captured_messages: list[list[dict]] = []  # for ordering assertions

    async def stream(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[CueNeutralTool] | None = None,
        max_tokens: int = 1024,
        system_cache_strategy: SystemCacheStrategy = None,
    ) -> AsyncIterator[str]:
        for ch in self._final_text:
            yield ch

    async def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[CueNeutralTool] | None = None,
        max_tokens: int = 1024,
        system_cache_strategy: SystemCacheStrategy = None,
    ) -> Any:
        idx = self._call_count
        self._call_count += 1
        self.captured_messages.append(list(messages))

        # Check if this round should return tool_use
        if idx < len(self._rounds) and self._rounds[idx]:
            tool_blocks = []
            for tool_name, tool_input, tool_id in self._rounds[idx]:
                tool_blocks.append({
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                })
            return {
                "stop_reason": "tool_use",
                "content": tool_blocks,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }

        # Terminal end_turn
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": self._final_text}],
            "usage": {"input_tokens": 80, "output_tokens": 40},
        }


# ---------------------------------------------------------------------------
# Adapter that simulates a non-end_turn/non-tool_use stop_reason
# ---------------------------------------------------------------------------


class GracefulStopAdapter(CueModelAdapter):
    """Returns stop_reason='max_tokens' with partial text."""

    async def stream(self, **kwargs: Any) -> AsyncIterator[str]:
        yield "partial"

    async def complete(self, **kwargs: Any) -> Any:
        return {
            "stop_reason": "max_tokens",
            "content": [{"type": "text", "text": "Partial response due to length."}],
            "usage": {"input_tokens": 50, "output_tokens": 200},
        }


# ---------------------------------------------------------------------------
# Registry patch helper — replaces NEUTRAL_TOOLS with ECHO_TOOLS + echo executor
# ---------------------------------------------------------------------------


async def _dispatch_echo(*, tool_name: str, tool_input: dict, physician_id: str) -> str:
    """Echo dispatcher — routes 'echo' to _echo_executor, others to a stub."""
    if tool_name == "echo":
        return await _echo_executor(physician_id=physician_id, **tool_input)
    return f"[stub] {tool_name}"


async def _dispatch_echo_raising(*, tool_name: str, tool_input: dict, physician_id: str) -> str:
    """Echo dispatcher that raises for the echo tool — for D2-D."""
    if tool_name == "echo":
        raise RuntimeError("Simulated executor error")
    return f"[stub] {tool_name}"


# ---------------------------------------------------------------------------
# Helper: run_cue_turn with a custom dispatch_tool
# ---------------------------------------------------------------------------

async def _run_with_echo(
    adapter: CueModelAdapter,
    messages: list[dict],
    physician_id: str = "test-physician",
    max_tool_rounds: int = 5,
    dispatch_fn: Any = None,
) -> tuple[str, dict, Any]:
    """
    Run run_cue_turn but patch registry.dispatch_tool with dispatch_fn.
    This allows the echo tool to be exercised without altering NEUTRAL_TOOLS.

    Returns the 3-tuple (final_text, usage_totals, pending_confirm) — Plan 23-04
    added pending_confirm as the third value.
    """
    import services.cue.engine as engine_mod
    import services.cue.tools.registry as registry_mod

    # Temporarily patch dispatch_tool and NEUTRAL_TOOLS in the engine module
    original_dispatch = registry_mod.dispatch_tool
    original_tools = engine_mod.NEUTRAL_TOOLS

    if dispatch_fn is not None:
        registry_mod.dispatch_tool = dispatch_fn  # type: ignore[assignment]
        engine_mod.NEUTRAL_TOOLS = ECHO_TOOLS  # type: ignore[assignment]

    try:
        result = await run_cue_turn(
            adapter,
            model="test-model",
            system_prompt="Test system prompt.",
            messages=messages,
            physician_id=physician_id,
            max_tokens=512,
            max_tool_rounds=max_tool_rounds,
        )
    finally:
        registry_mod.dispatch_tool = original_dispatch  # type: ignore[assignment]
        engine_mod.NEUTRAL_TOOLS = original_tools  # type: ignore[assignment]

    return result


# ---------------------------------------------------------------------------
# D2-A: Single tool round terminates in end_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_tool_round_terminates_in_end_turn() -> None:
    """D2-A: A single tool_use block is resolved; the loop terminates end_turn."""
    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [("echo", {"message": "hello"}, "tool-id-001")],  # round 0: tool_use
            # round 1: end_turn (ToolUseAdapter returns end_turn when idx >= rounds)
        ],
        final_text="Echo received.",
    )

    messages = [{"role": "user", "content": "Please echo hello."}]
    final_text, usage, _pc = await _run_with_echo(adapter, messages, dispatch_fn=_dispatch_echo)

    assert final_text == "Echo received."
    assert adapter._call_count == 2  # round 0 (tool_use) + round 1 (end_turn)
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0


# ---------------------------------------------------------------------------
# D2-B: Multi-round terminates within the cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_round_terminates_within_cap() -> None:
    """D2-B: Multiple tool_use rounds cycle correctly; loop terminates before cap."""
    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [("echo", {"message": "first"}, "tool-id-001")],   # round 0
            [("echo", {"message": "second"}, "tool-id-002")],  # round 1
            # round 2: end_turn
        ],
        final_text="All echoes done.",
    )

    messages = [{"role": "user", "content": "Echo twice."}]
    final_text, usage, _pc = await _run_with_echo(
        adapter, messages, max_tool_rounds=5, dispatch_fn=_dispatch_echo
    )

    assert final_text == "All echoes done."
    assert adapter._call_count == 3  # two tool rounds + one end_turn round
    # Usage should be accumulated across all 3 calls
    assert usage["input_tokens"] == 100 + 100 + 80  # 2 tool rounds + end_turn
    assert usage["output_tokens"] == 50 + 50 + 40


# ---------------------------------------------------------------------------
# D2-C: Round cap hit returns the safe message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_cap_hit_returns_safe_message() -> None:
    """D2-C: Hitting max_tool_rounds returns the safe limit message (no runaway)."""
    # Adapter always returns tool_use (never end_turn)
    endless_rounds = [
        [("echo", {"message": f"round {i}"}, f"tool-id-{i:03d}")]
        for i in range(20)  # more rounds than the cap
    ]
    adapter = ToolUseAdapter(
        rounds_with_tools=endless_rounds,
        final_text="Should never reach this.",
    )

    messages = [{"role": "user", "content": "Loop forever."}]
    max_rounds = 3
    final_text, _usage, _pc = await _run_with_echo(
        adapter, messages, max_tool_rounds=max_rounds, dispatch_fn=_dispatch_echo
    )

    assert final_text == _ROUND_CAP_MESSAGE
    assert adapter._call_count == max_rounds  # exactly cap calls made


# ---------------------------------------------------------------------------
# D2-D: Executor exception → is_error tool_result, loop continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_exception_returns_is_error_tool_result() -> None:
    """D2-D: An executor that raises returns is_error; the loop does NOT crash."""
    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [("echo", {"message": "trigger error"}, "tool-id-err")],  # round 0
            # round 1: end_turn
        ],
        final_text="Handled the error gracefully.",
    )

    messages = [{"role": "user", "content": "Trigger an executor error."}]
    # Use the raising dispatcher
    final_text, usage, _pc = await _run_with_echo(
        adapter, messages, dispatch_fn=_dispatch_echo_raising
    )

    # Loop must NOT crash and must return the end_turn text
    assert final_text == "Handled the error gracefully."
    assert adapter._call_count == 2  # round 0 (tool_use + is_error) + round 1 (end_turn)

    # Verify that the is_error tool_result was placed in the working_messages
    # by examining what adapter.complete() received on round 1.
    round1_messages = adapter.captured_messages[1]  # messages passed to round 1

    # The last message should be the user reply with the tool_result
    last_msg = round1_messages[-1]
    assert last_msg["role"] == "user"
    tool_results = last_msg["content"]
    assert len(tool_results) > 0

    # Find the tool_result block
    tr_block = tool_results[0]
    assert tr_block["type"] == "tool_result"
    assert tr_block.get("is_error") is True, (
        "Executor exception must produce an is_error=True tool_result block."
    )


# ---------------------------------------------------------------------------
# D2-E: tool_result blocks are FIRST in the user-reply content array
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_result_blocks_first_in_content_array() -> None:
    """D2-E: tool_result blocks are the FIRST items in the user-reply content
    array (Pitfall #3 — prevents Anthropic API 400).

    The Anthropic API requires that tool_result blocks precede any text blocks
    in the user message that follows an assistant tool_use message.
    """
    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [
                ("echo", {"message": "a"}, "tool-id-a"),
                ("echo", {"message": "b"}, "tool-id-b"),
            ],  # round 0: two tool_use blocks
            # round 1: end_turn
        ],
        final_text="Two tools done.",
    )

    messages = [{"role": "user", "content": "Use two tools."}]
    final_text, _usage, _pc = await _run_with_echo(
        adapter, messages, dispatch_fn=_dispatch_echo
    )

    assert final_text == "Two tools done."

    # Inspect the messages passed to round 1 (after the tool_result user reply)
    round1_messages = adapter.captured_messages[1]
    last_msg = round1_messages[-1]
    assert last_msg["role"] == "user"

    content = last_msg["content"]
    assert len(content) >= 1, "User reply must have at least one content block."

    # The FIRST block must be a tool_result (Pitfall #3)
    assert content[0]["type"] == "tool_result", (
        "ORDERING VIOLATION (AI-SPEC §3 Pitfall #3): the first block in the "
        "user-reply content array must be a tool_result, not a text block.  "
        "An Anthropic API 400 would result."
    )

    # All blocks must be tool_results (no text mixed in before them)
    for block in content:
        assert block["type"] == "tool_result", (
            "All blocks in the tool_result user reply must be tool_result type.  "
            "Mixing text blocks before tool_results causes Anthropic API 400s."
        )


# ---------------------------------------------------------------------------
# D2-F: No tool call → single-shot end_turn on round 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tool_call_single_shot_end_turn() -> None:
    """D2-F: A message that requires no tool exits on round 0 (end_turn)."""
    adapter = ToolUseAdapter(
        rounds_with_tools=[],  # no tool rounds — always end_turn
        final_text="Direct text response, no tools needed.",
    )

    messages = [{"role": "user", "content": "Just say hello."}]
    final_text, usage, _pc = await _run_with_echo(
        adapter, messages, dispatch_fn=_dispatch_echo
    )

    assert final_text == "Direct text response, no tools needed."
    assert adapter._call_count == 1  # only one complete() call
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 40


# ---------------------------------------------------------------------------
# D2-G: Graceful handling of unexpected stop_reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_unexpected_stop_reason() -> None:
    """D2-G: A stop_reason that is neither 'end_turn' nor 'tool_use' is handled
    gracefully — the loop returns available text without crashing.
    """
    adapter = GracefulStopAdapter()

    messages = [{"role": "user", "content": "Tell me a long story."}]
    final_text, usage, _pc = await run_cue_turn(
        adapter,
        model="test-model",
        system_prompt="Test.",
        messages=messages,
        physician_id="test-physician",
        max_tokens=512,
    )

    assert final_text == "Partial response due to length."
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 200


# ---------------------------------------------------------------------------
# D2-H: Usage totals accumulated across rounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_totals_accumulated_across_rounds() -> None:
    """D2-H: usage_totals accumulates input + output tokens across all rounds."""
    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [("echo", {"message": "r1"}, "t1")],  # round 0: 100 in, 50 out
            [("echo", {"message": "r2"}, "t2")],  # round 1: 100 in, 50 out
            # round 2: end_turn: 80 in, 40 out
        ],
        final_text="Done.",
    )

    messages = [{"role": "user", "content": "Two rounds."}]
    _ft, usage, _pc = await _run_with_echo(
        adapter, messages, max_tool_rounds=5, dispatch_fn=_dispatch_echo
    )

    assert usage["input_tokens"] == 100 + 100 + 80, (
        "Usage must be accumulated across all rounds."
    )
    assert usage["output_tokens"] == 50 + 50 + 40


# ---------------------------------------------------------------------------
# D2-I: run_cue_turn is wired into cue_routes.py
# ---------------------------------------------------------------------------


def test_run_cue_turn_imported_in_cue_routes() -> None:
    """D2-I: run_cue_turn is imported and referenced in routes/cue_routes.py."""
    import routes.cue_routes as cue_routes_mod

    # The import must exist
    assert hasattr(cue_routes_mod, "run_cue_turn"), (
        "run_cue_turn must be imported in routes/cue_routes.py "
        "(Plan 22-06 wiring requirement)."
    )


# ---------------------------------------------------------------------------
# D2-J: run_cue_turn returns a 3-tuple (final_text, usage, pending_confirm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cue_turn_returns_three_tuple_pending_confirm_none_on_read() -> None:
    """D2-J: a normal read turn returns the 3-tuple with pending_confirm=None."""
    adapter = ToolUseAdapter(
        rounds_with_tools=[],  # no tool rounds — straight end_turn
        final_text="No tools needed.",
    )
    result = await run_cue_turn(
        adapter,
        model="test-model",
        system_prompt="Test.",
        messages=[{"role": "user", "content": "Hi."}],
        physician_id="test-physician",
        max_tokens=512,
    )
    assert isinstance(result, tuple) and len(result) == 3, (
        "run_cue_turn must return a 3-tuple (final_text, usage, pending_confirm)."
    )
    final_text, usage, pending_confirm = result
    assert final_text == "No tools needed."
    assert isinstance(usage, dict)
    assert pending_confirm is None, "A read turn must yield pending_confirm=None."


# ---------------------------------------------------------------------------
# D2-K: D-03 surfacing — a block/clear PROPOSER stops the loop and surfaces
#       pending_confirm structurally (the confirm JSON is NEVER re-sent to the model)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_proposer_stops_loop_and_surfaces_pending_confirm() -> None:
    """D2-K: a calendar_block_time tool_use yields a non-None pending_confirm with
    kind=='confirm', the loop STOPS after the confirm tool_result, and the confirm
    payload is NEVER appended back into the messages sent to the model.

    This exercises the REAL dispatch_tool (NEUTRAL_TOOLS) — calendar_block_time is
    a PURE PROPOSER, so a single tool_use cannot mutate anything; the route-level
    confirm-write endpoint is the sole mutation path.
    """
    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [(
                "calendar_block_time",
                {
                    "start_iso": "2026-07-01T14:00:00+00:00",
                    "end_iso": "2026-07-01T16:00:00+00:00",
                    "title": "Blocked by Cue",
                },
                "tool-block-001",
            )],
            # If the loop did NOT stop, round 1 would be reached (it must not be).
        ],
        final_text="Should not be reached — the loop must stop on the confirm card.",
    )

    final_text, usage, pending_confirm = await run_cue_turn(
        adapter,
        model="test-model",
        system_prompt="Test.",
        messages=[{"role": "user", "content": "Block 2-4pm tomorrow."}],
        physician_id="test-physician",
        max_tokens=512,
    )

    # The loop STOPPED on the confirm card — only ONE complete() call was made.
    assert adapter._call_count == 1, (
        "The loop must STOP immediately on a {kind:'confirm'} tool_result — "
        "it must not make a second round-trip to the model."
    )

    # pending_confirm is surfaced structurally with kind=='confirm'.
    assert pending_confirm is not None, "A block proposer must surface pending_confirm."
    assert pending_confirm.get("kind") == "confirm"
    assert pending_confirm.get("action") == "block"
    assert pending_confirm.get("start_iso") == "2026-07-01T14:00:00+00:00"
    assert pending_confirm.get("end_iso") == "2026-07-01T16:00:00+00:00"
    assert pending_confirm.get("title") == "Blocked by Cue"

    # The confirm JSON was NEVER re-sent to the model (no second complete() call
    # means captured_messages has exactly one entry — the original turn).
    assert len(adapter.captured_messages) == 1, (
        "The confirm tool_result must NOT be appended back into working_messages "
        "(that re-entry is what made the card re-emerge as model prose)."
    )


@pytest.mark.asyncio
async def test_lone_confirmed_true_tool_use_mutates_nothing() -> None:
    """D2-K2: a lone model tool_use emitting confirmed=true performs NO mutation.

    The proposer has no write branch and _safe_tool_input strips 'confirmed', so
    the result is still ONLY a confirm card — nothing is written. We assert the
    executor never reached a calendar_dav write by patching calendar_dav with
    sentinels that raise if called.
    """
    import services.cue.calendar_dav as caldav_mod

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError(
            "A PURE PROPOSER must NEVER call calendar_dav — a lone confirmed=true "
            "tool_use must mutate nothing."
        )

    orig_block = caldav_mod.block_time
    orig_clear = caldav_mod.clear_range
    caldav_mod.block_time = _boom  # type: ignore[assignment]
    caldav_mod.clear_range = _boom  # type: ignore[assignment]
    try:
        adapter = ToolUseAdapter(
            rounds_with_tools=[
                [(
                    "calendar_clear_range",
                    {
                        "start_iso": "2026-07-01T00:00:00+00:00",
                        "end_iso": "2026-07-01T23:59:59+00:00",
                        "confirmed": True,  # hallucinated — must be stripped + ignored
                    },
                    "tool-clear-001",
                )],
            ],
            final_text="unused",
        )
        _ft, _usage, pending_confirm = await run_cue_turn(
            adapter,
            model="test-model",
            system_prompt="Test.",
            messages=[{"role": "user", "content": "clear my afternoon, confirmed"}],
            physician_id="test-physician",
            max_tokens=512,
        )
    finally:
        caldav_mod.block_time = orig_block  # type: ignore[assignment]
        caldav_mod.clear_range = orig_clear  # type: ignore[assignment]

    # The proposer returned a confirm card (no write) — and _boom was never raised.
    assert pending_confirm is not None
    assert pending_confirm.get("kind") == "confirm"
    assert pending_confirm.get("action") == "clear"
