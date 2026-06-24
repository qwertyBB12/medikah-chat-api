"""
tests/cue/test_streaming_turn.py
--------------------------------
Phase-23 TTFT streaming: adapter.stream_turn() + engine.run_cue_turn_streaming().

These guard the streaming path that lets the route forward token deltas to the
client as they arrive (instead of buffering the whole reply), WITHOUT a second
model round-trip and WITHOUT changing the D-03 confirm contract.

Coverage
--------
S-A  adapter default stream_turn() wraps complete() → text event(s) + message event
S-B  streaming a no-tool turn yields deltas whose concat == final_text, then done
S-C  deltas are emitted BEFORE the done event (true progressive streaming)
S-D  a tool round + end_turn streams only the terminal text; done carries final_text
S-E  a block PROPOSER stops the loop and surfaces pending_confirm on the done event
S-F  run_cue_turn() (tuple wrapper) returns identical results to draining the stream
S-G  usage accumulates across rounds on the done event
S-H  the route imports run_cue_turn_streaming (wiring)
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from services.cue.adapter import CueModelAdapter, CueNeutralTool, SystemCacheStrategy
from services.cue.engine import run_cue_turn, run_cue_turn_streaming
from tests.cue.test_tool_loop import (
    ToolUseAdapter,
    _dispatch_echo,
    ECHO_TOOLS,
)


async def _collect(agen: AsyncIterator[dict]) -> list[dict]:
    return [ev async for ev in agen]


def _deltas(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e.get("type") == "delta")


def _done(events: list[dict]) -> dict:
    dones = [e for e in events if e.get("type") == "done"]
    assert len(dones) == 1, f"expected exactly one done event, got {len(dones)}"
    return dones[0]


# ---------------------------------------------------------------------------
# S-A: the ABC default stream_turn() wraps complete()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_stream_turn_wraps_complete() -> None:
    """A non-streaming adapter (dict complete()) gets stream_turn for free:
    text event(s) carrying the assembled text, then one terminal message event."""
    adapter = ToolUseAdapter(rounds_with_tools=[], final_text="Hola doctora.")
    events = await _collect(
        adapter.stream_turn(
            model="m", system_prompt="s", messages=[{"role": "user", "content": "hi"}]
        )
    )
    text_events = [e for e in events if e.get("type") == "text"]
    msg_events = [e for e in events if e.get("type") == "message"]

    assert "".join(e["text"] for e in text_events) == "Hola doctora."
    assert len(msg_events) == 1
    msg = msg_events[0]
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"] == {"input_tokens": 80, "output_tokens": 40}
    # message event is LAST (text deltas precede the terminal message)
    assert events[-1]["type"] == "message"


# ---------------------------------------------------------------------------
# S-B / S-C: no-tool turn streams deltas then done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tool_turn_streams_deltas_then_done() -> None:
    adapter = ToolUseAdapter(rounds_with_tools=[], final_text="Listo para ayudarte.")
    events = await _collect(
        run_cue_turn_streaming(
            adapter,
            model="m",
            system_prompt="s",
            messages=[{"role": "user", "content": "hola"}],
            physician_id="doc-1",
        )
    )
    done = _done(events)
    assert _deltas(events) == "Listo para ayudarte."
    assert done["final_text"] == "Listo para ayudarte."
    assert done["pending_confirm"] is None
    # progressive: at least one delta arrives BEFORE the done event
    first_delta_idx = next(i for i, e in enumerate(events) if e["type"] == "delta")
    done_idx = next(i for i, e in enumerate(events) if e["type"] == "done")
    assert first_delta_idx < done_idx


# ---------------------------------------------------------------------------
# S-D: a tool round then end_turn — only the terminal text streams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_round_then_end_turn_streams_terminal_text() -> None:
    adapter = ToolUseAdapter(
        rounds_with_tools=[[("echo", {"message": "x"}, "t1")]],  # round 0: tool_use
        final_text="Aquí está tu resumen.",
    )
    import services.cue.engine as engine_mod
    import services.cue.tools.registry as registry_mod

    orig_dispatch = registry_mod.dispatch_tool
    orig_tools = engine_mod.NEUTRAL_TOOLS
    registry_mod.dispatch_tool = _dispatch_echo  # type: ignore[assignment]
    engine_mod.NEUTRAL_TOOLS = ECHO_TOOLS  # type: ignore[assignment]
    try:
        events = await _collect(
            run_cue_turn_streaming(
                adapter,
                model="m",
                system_prompt="s",
                messages=[{"role": "user", "content": "resume"}],
                physician_id="doc-1",
            )
        )
    finally:
        registry_mod.dispatch_tool = orig_dispatch  # type: ignore[assignment]
        engine_mod.NEUTRAL_TOOLS = orig_tools  # type: ignore[assignment]

    done = _done(events)
    # The tool round (round 0) emits no text; only the end_turn round streams.
    assert _deltas(events) == "Aquí está tu resumen."
    assert done["final_text"] == "Aquí está tu resumen."
    assert done["pending_confirm"] is None
    assert adapter._call_count == 2  # tool round + end_turn round


# ---------------------------------------------------------------------------
# S-E: a block PROPOSER stops the loop and surfaces pending_confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_proposer_surfaces_pending_confirm_on_done() -> None:
    """D-03 over the streaming path: a calendar_block_time tool_use STOPS the loop
    and the terminal done event carries the structured pending_confirm. The model
    is NEVER re-invoked (single round) — the confirm JSON never becomes prose."""
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
        ],
        final_text="should not be reached",
    )
    events = await _collect(
        run_cue_turn_streaming(
            adapter,
            model="m",
            system_prompt="s",
            messages=[{"role": "user", "content": "block 2-4pm"}],
            physician_id="doc-1",
        )
    )
    done = _done(events)
    pc = done["pending_confirm"]
    assert pc is not None and pc.get("kind") == "confirm"
    assert pc.get("action") == "block"
    assert pc.get("start_iso") == "2026-07-01T14:00:00+00:00"
    # loop stopped on the confirm — exactly one model round
    assert adapter._call_count == 1
    assert len(adapter.captured_messages) == 1


# ---------------------------------------------------------------------------
# S-F: run_cue_turn() tuple wrapper parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cue_turn_tuple_matches_stream_done() -> None:
    adapter_stream = ToolUseAdapter(rounds_with_tools=[], final_text="Igual.")
    adapter_tuple = ToolUseAdapter(rounds_with_tools=[], final_text="Igual.")

    events = await _collect(
        run_cue_turn_streaming(
            adapter_stream, model="m", system_prompt="s",
            messages=[{"role": "user", "content": "x"}], physician_id="doc-1",
        )
    )
    done = _done(events)

    final_text, usage, pc = await run_cue_turn(
        adapter_tuple, model="m", system_prompt="s",
        messages=[{"role": "user", "content": "x"}], physician_id="doc-1",
    )
    assert final_text == done["final_text"] == "Igual."
    assert usage == done["usage"]
    assert pc == done["pending_confirm"] is None


# ---------------------------------------------------------------------------
# S-G: usage accumulates across rounds on the done event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_accumulates_on_done() -> None:
    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [("echo", {"message": "a"}, "t1")],
            [("echo", {"message": "b"}, "t2")],
        ],
        final_text="Done.",
    )
    import services.cue.engine as engine_mod
    import services.cue.tools.registry as registry_mod

    orig_dispatch = registry_mod.dispatch_tool
    orig_tools = engine_mod.NEUTRAL_TOOLS
    registry_mod.dispatch_tool = _dispatch_echo  # type: ignore[assignment]
    engine_mod.NEUTRAL_TOOLS = ECHO_TOOLS  # type: ignore[assignment]
    try:
        events = await _collect(
            run_cue_turn_streaming(
                adapter, model="m", system_prompt="s",
                messages=[{"role": "user", "content": "two rounds"}],
                physician_id="doc-1",
            )
        )
    finally:
        registry_mod.dispatch_tool = orig_dispatch  # type: ignore[assignment]
        engine_mod.NEUTRAL_TOOLS = orig_tools  # type: ignore[assignment]

    done = _done(events)
    assert done["usage"]["input_tokens"] == 100 + 100 + 80
    assert done["usage"]["output_tokens"] == 50 + 50 + 40


# ---------------------------------------------------------------------------
# S-H: route wiring
# ---------------------------------------------------------------------------


def test_route_imports_streaming_generator() -> None:
    import routes.cue_routes as cue_routes_mod

    assert hasattr(cue_routes_mod, "run_cue_turn_streaming"), (
        "cue_routes.py must import run_cue_turn_streaming for the TTFT path."
    )


# ---------------------------------------------------------------------------
# S-I: tools=[] disables tools (the opening-greeting path offers no tools)
# ---------------------------------------------------------------------------


class _ToolSpyAdapter(CueModelAdapter):
    """Records the `tools` arg passed to stream_turn; always returns end_turn."""

    def __init__(self) -> None:
        self.seen_tools: list[Any] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[str]:  # pragma: no cover
        yield "x"

    async def complete(self, **kwargs: Any) -> Any:  # pragma: no cover
        return {"stop_reason": "end_turn", "content": [], "usage": {}}

    async def stream_turn(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[CueNeutralTool] | None = None,
        max_tokens: int = 1024,
        system_cache_strategy: SystemCacheStrategy = None,
    ) -> AsyncIterator[dict]:
        self.seen_tools.append(tools)
        yield {"type": "text", "text": "Hola."}
        yield {"type": "message", "stop_reason": "end_turn", "content": [], "usage": {}}


@pytest.mark.asyncio
async def test_empty_tools_list_disables_tools() -> None:
    adapter = _ToolSpyAdapter()
    await _collect(
        run_cue_turn_streaming(
            adapter, model="m", system_prompt="s",
            messages=[{"role": "user", "content": "greet"}],
            physician_id="doc-1", tools=[],
        )
    )
    assert adapter.seen_tools == [[]], "tools=[] must be forwarded verbatim (no NEUTRAL_TOOLS)."
