"""
tests/cue/test_tool_event_frames.py
-----------------------------------
Wire-spec v2 "thinking trace": tool-event frames on the /cue/chat stream.

The agentic loop now emits a PURELY ADDITIVE event each time it STARTS and
FINISHES a tool call, so the frontend can render cascading terminal-style steps
("leyendo tu calendario ✓ 14 eventos") before the spoken answer. These tests
guard BOTH layers:

Engine (run_cue_turn_streaming) — new {"type":"tool", ...} events
--------------------------------------------------------------------
TE-A  a tool round yields, IN ORDER: tool/start → tool/end(ok=True) → done,
      and any leading text delta precedes them.
TE-B  the end frame carries items=n when the tool_result has n "- " lines,
      and OMITS items when there are 0.
TE-C  an executor that RAISES yields tool/end(ok=False) with no items, and the
      loop continues to a terminal done.
TE-D  a D-03 PROPOSER ({kind:'confirm'}) still yields its start+end frames AND
      still stops the loop with pending_confirm on the done event.
TE-E  a tool-free turn yields NO {"type":"tool"} events (shape-identical to
      before), and run_cue_turn() (the tuple wrapper) is unchanged.

Route (/cue/chat _token_gen) — \\x1f-framed tool events on the wire
--------------------------------------------------------------------
TE-F  a tool turn's streamed bytes contain \\x1f{...}\\n frames with the right
      JSON, and the \\x1e confirm tail (when present) still appears AFTER the
      text. Tool frames are NOT folded into the spoken `captured` text.
TE-G  a tool-free turn's bytes are byte-identical to the pre-change behavior
      (no \\x1f frame, optional \\x1e tail only).

Wire shape (text/plain stream):
  • text deltas — raw UTF-8, no framing
  • tool event  — \\x1f + compact JSON {"phase":..., "tool":..., [ "ok", "items" ]} + \\n
  • confirm tail (UNCHANGED) — \\x1e + {"pending_confirm":{...}} + \\n
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest

from services.cue.engine import (
    run_cue_turn,
    run_cue_turn_streaming,
    _count_items,
)
from tests.cue.test_tool_loop import (
    ToolUseAdapter,
    ECHO_TOOLS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect(agen: AsyncIterator[dict]) -> list[dict]:
    return [ev async for ev in agen]


def _types(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


def _tool_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "tool"]


def _done(events: list[dict]) -> dict:
    dones = [e for e in events if e.get("type") == "done"]
    assert len(dones) == 1, f"expected exactly one done event, got {len(dones)}"
    return dones[0]


# A dispatcher whose result text has n "- " lines (the executors' list format).
def _make_list_dispatch(n_items: int):
    async def _dispatch(*, tool_name: str, tool_input: dict, physician_id: str, locale: str = "es") -> str:
        header = "2026-07-01:"
        items = [f"- 09:00 → 09:30: event {i}" for i in range(n_items)]
        return "\n".join([header, *items]) if items else "No events today."
    return _dispatch


async def _dispatch_raises(*, tool_name: str, tool_input: dict, physician_id: str, locale: str = "es") -> str:
    raise RuntimeError("Simulated executor error")


def _patch_registry(dispatch_fn: Any):
    """Patch the dispatch_tool the engine actually calls + NEUTRAL_TOOLS.

    engine.py does `from ...registry import dispatch_tool`, so the live binding is
    `engine_mod.dispatch_tool` (its own module attribute) — patching only
    `registry_mod.dispatch_tool` would NOT affect the call site. We patch both so
    the engine resolves our scripted dispatcher. Returns a restore fn.
    """
    import services.cue.engine as engine_mod
    import services.cue.tools.registry as registry_mod

    orig_engine_dispatch = engine_mod.dispatch_tool
    orig_registry_dispatch = registry_mod.dispatch_tool
    orig_tools = engine_mod.NEUTRAL_TOOLS
    engine_mod.dispatch_tool = dispatch_fn  # type: ignore[assignment]
    registry_mod.dispatch_tool = dispatch_fn  # type: ignore[assignment]
    engine_mod.NEUTRAL_TOOLS = ECHO_TOOLS  # type: ignore[assignment]

    def _restore() -> None:
        engine_mod.dispatch_tool = orig_engine_dispatch  # type: ignore[assignment]
        registry_mod.dispatch_tool = orig_registry_dispatch  # type: ignore[assignment]
        engine_mod.NEUTRAL_TOOLS = orig_tools  # type: ignore[assignment]

    return _restore


# ===========================================================================
# Engine-level tests
# ===========================================================================


# ---------------------------------------------------------------------------
# TE-A: tool/start precedes tool/end precedes done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_round_yields_start_then_end_then_done() -> None:
    adapter = ToolUseAdapter(
        rounds_with_tools=[[("echo", {"message": "x"}, "t1")]],  # round 0: tool_use
        final_text="Aquí está tu resumen.",
    )
    restore = _patch_registry(_make_list_dispatch(0))  # no items → end without items
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
        restore()

    tools = _tool_events(events)
    assert len(tools) == 2, f"expected start+end, got {tools}"

    start, end = tools
    assert start == {"type": "tool", "phase": "start", "tool": "echo"}
    assert end["phase"] == "end"
    assert end["tool"] == "echo"
    assert end["ok"] is True
    assert "items" not in end, "items must be OMITTED when there are zero '- ' lines"

    # Ordering: start precedes end; both precede the single done.
    idx = {id(e): i for i, e in enumerate(events)}
    start_i = idx[id(start)]
    end_i = idx[id(end)]
    done_i = next(i for i, e in enumerate(events) if e.get("type") == "done")
    assert start_i < end_i < done_i


# ---------------------------------------------------------------------------
# TE-B: items count == number of "- " lines; omitted when 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_frame_items_count_matches_dashed_lines() -> None:
    adapter = ToolUseAdapter(
        rounds_with_tools=[[("echo", {"message": "x"}, "t1")]],
        final_text="ok.",
    )
    restore = _patch_registry(_make_list_dispatch(3))  # 3 "- " lines
    try:
        events = await _collect(
            run_cue_turn_streaming(
                adapter, model="m", system_prompt="s",
                messages=[{"role": "user", "content": "read"}], physician_id="doc-1",
            )
        )
    finally:
        restore()

    end = [e for e in _tool_events(events) if e["phase"] == "end"][0]
    assert end["ok"] is True
    assert end.get("items") == 3


@pytest.mark.asyncio
async def test_end_frame_omits_items_when_zero() -> None:
    adapter = ToolUseAdapter(
        rounds_with_tools=[[("echo", {"message": "x"}, "t1")]],
        final_text="ok.",
    )
    restore = _patch_registry(_make_list_dispatch(0))
    try:
        events = await _collect(
            run_cue_turn_streaming(
                adapter, model="m", system_prompt="s",
                messages=[{"role": "user", "content": "read"}], physician_id="doc-1",
            )
        )
    finally:
        restore()

    end = [e for e in _tool_events(events) if e["phase"] == "end"][0]
    assert "items" not in end


def test_count_items_helper_directly() -> None:
    # Pure-function contract for _count_items: count of lines starting with "- ".
    assert _count_items("header:\n- a\n- b\n- c") == 3
    assert _count_items("No events today.") is None
    assert _count_items("") is None
    assert _count_items("- only") == 1
    # A non-string (e.g. a structured tool_result) → None (no crash).
    assert _count_items(None) is None
    assert _count_items({"kind": "confirm"}) is None
    # Lines that merely contain "- " but don't START with it are not counted.
    assert _count_items("a - b\n- real") == 1


# ---------------------------------------------------------------------------
# TE-C: executor exception yields end(ok=False), loop continues to done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_exception_yields_end_ok_false_and_continues() -> None:
    adapter = ToolUseAdapter(
        rounds_with_tools=[[("echo", {"message": "boom"}, "t1")]],  # round 0: tool_use
        final_text="Handled gracefully.",  # round 1: end_turn
    )
    restore = _patch_registry(_dispatch_raises)
    try:
        events = await _collect(
            run_cue_turn_streaming(
                adapter, model="m", system_prompt="s",
                messages=[{"role": "user", "content": "go"}], physician_id="doc-1",
            )
        )
    finally:
        restore()

    tools = _tool_events(events)
    assert len(tools) == 2
    start, end = tools
    assert start["phase"] == "start"
    assert end["phase"] == "end"
    assert end["ok"] is False
    assert "items" not in end, "a failed tool reports no items"

    # The loop continued past the error and reached the terminal end_turn.
    done = _done(events)
    assert done["final_text"] == "Handled gracefully."
    assert done["pending_confirm"] is None
    assert adapter._call_count == 2  # tool round + end_turn round


# ---------------------------------------------------------------------------
# TE-D: a D-03 proposer still emits start+end AND stops with pending_confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d03_proposer_emits_frames_and_stops_with_pending_confirm() -> None:
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
    # Uses the REAL NEUTRAL_TOOLS / dispatch_tool (calendar_block_time is a pure
    # proposer returning a {kind:'confirm'} JSON tool_result).
    events = await _collect(
        run_cue_turn_streaming(
            adapter, model="m", system_prompt="s",
            messages=[{"role": "user", "content": "block 2-4pm"}], physician_id="doc-1",
        )
    )

    tools = _tool_events(events)
    assert [t["phase"] for t in tools] == ["start", "end"], (
        "the proposer must STILL get its start+end frames before the loop stops"
    )
    start, end = tools
    assert start["tool"] == "calendar_block_time"
    assert end["tool"] == "calendar_block_time"
    assert end["ok"] is True
    # The confirm JSON is a single object, not a "- " list → no items.
    assert "items" not in end

    done = _done(events)
    pc = done["pending_confirm"]
    assert pc is not None and pc.get("kind") == "confirm"
    assert pc.get("action") == "block"
    # Loop stopped on the confirm — exactly one model round.
    assert adapter._call_count == 1

    # The end frame must precede the done (D-03 end frame runs BEFORE the break).
    end_i = next(i for i, e in enumerate(events) if e.get("type") == "tool" and e["phase"] == "end")
    done_i = next(i for i, e in enumerate(events) if e.get("type") == "done")
    assert end_i < done_i


# ---------------------------------------------------------------------------
# TE-E: a tool-free turn yields NO tool events; tuple wrapper unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_free_turn_yields_no_tool_events() -> None:
    adapter = ToolUseAdapter(rounds_with_tools=[], final_text="Listo para ayudarte.")
    events = await _collect(
        run_cue_turn_streaming(
            adapter, model="m", system_prompt="s",
            messages=[{"role": "user", "content": "hola"}], physician_id="doc-1",
        )
    )
    assert _tool_events(events) == [], "a tool-free turn must emit ZERO tool events"
    # Shape is unchanged: only delta(s) then exactly one done.
    assert set(_types(events)) <= {"delta", "done"}
    done = _done(events)
    assert done["final_text"] == "Listo para ayudarte."


@pytest.mark.asyncio
async def test_run_cue_turn_wrapper_unchanged_with_tool_events() -> None:
    """The non-streaming wrapper drains the generator reading only `done`; the new
    `tool` events must be transparently ignored, leaving the 3-tuple intact."""
    adapter = ToolUseAdapter(
        rounds_with_tools=[[("echo", {"message": "x"}, "t1")]],
        final_text="Echo received.",
    )
    restore = _patch_registry(_make_list_dispatch(2))
    try:
        result = await run_cue_turn(
            adapter, model="m", system_prompt="s",
            messages=[{"role": "user", "content": "echo"}], physician_id="doc-1",
        )
    finally:
        restore()

    assert isinstance(result, tuple) and len(result) == 3
    final_text, usage, pending_confirm = result
    assert final_text == "Echo received."
    assert isinstance(usage, dict)
    assert pending_confirm is None


# ===========================================================================
# Route-level tests — /cue/chat _token_gen \x1f framing
# ===========================================================================


def _stub_route_gates(monkeypatch):
    """Stub the gate + context + adapter dependencies of /cue/chat so the route
    runs end-to-end with NO Supabase / Anthropic / filesystem access."""
    import routes.cue_routes as cue_mod

    async def _ok_kill(*a, **kw):
        return "ok"

    class _Budget:
        exceeded = False

    async def _ok_budget(*a, **kw):
        return _Budget()

    async def _prompt(*a, **kw):
        return "SYSTEM PROMPT"

    async def _noop_record(*a, **kw):
        return None

    monkeypatch.setattr(cue_mod, "get_supabase", lambda: None)
    monkeypatch.setattr(cue_mod, "check_kill_switch", _ok_kill)
    monkeypatch.setattr(cue_mod, "budget_check", _ok_budget)
    monkeypatch.setattr(cue_mod, "_build_system_prompt", _prompt)
    monkeypatch.setattr(cue_mod, "record_usage", _noop_record)
    monkeypatch.setattr(cue_mod, "create_adapter", lambda *a, **kw: object())
    # Disable the rate limiter: slowapi's @limiter.limit decorator mangles the
    # endpoint signature under TestClient (FastAPI then reads `body` as a query
    # param → 422). We exercise the REAL _token_gen framing by invoking cue_chat()
    # directly (below) instead of going over HTTP; the limiter is off so the
    # decorator passes through. The origin guard allows no-Origin direct calls.
    # monkeypatch.setattr restores `enabled` after the test (no cross-test leak).
    monkeypatch.setattr(cue_mod.limiter, "enabled", False)


async def _run_chat_raw(body_dict: dict) -> bytes:
    """Invoke cue_chat() directly and drain the StreamingResponse to raw bytes.

    This exercises the REAL route code path — the gate envelope, the opening/
    conversational branch selection, and the _token_gen framing (text deltas,
    \\x1f tool frames, \\x1e confirm tail) — without slowapi's HTTP-layer
    signature mangling. The gates/adapter/context are stubbed by _stub_route_gates.
    """
    from dataclasses import dataclass
    from types import SimpleNamespace

    from fastapi import BackgroundTasks
    from routes.cue_routes import cue_chat, CueChatRequest

    @dataclass(frozen=True)
    class _FakeAuth:
        physician_id: str = "doc-route"
        auth_user_id: str = "user-route"
        email: str = "doc@medikah.health"
        role: str = "physician"
        verification_status: str = "verified"

    # Minimal Request stand-in: the route reads .state (rate-limit key sink),
    # .headers (origin guard), and .client (IP fallback).
    request = SimpleNamespace(
        state=SimpleNamespace(), headers={}, client=SimpleNamespace(host="127.0.0.1")
    )
    body = CueChatRequest(**body_dict)
    background_tasks = BackgroundTasks()

    resp = await cue_chat(request, body, background_tasks, _FakeAuth())  # type: ignore[arg-type]
    chunks: list[bytes] = []
    async for chunk in resp.body_iterator:  # type: ignore[attr-defined]
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# TE-F: a tool turn's bytes carry \x1f frames; \x1e confirm tail still trails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_emits_tool_frames_and_confirm_tail(monkeypatch) -> None:
    import routes.cue_routes as cue_mod

    captured_seen: list[str] = []

    async def _fake_stream(*args, **kwargs) -> AsyncIterator[dict]:
        # leading text, a tool start+end (with items), more text, then a D-03 done.
        yield {"type": "delta", "text": "Un momento. "}
        yield {"type": "tool", "phase": "start", "tool": "calendar_read_day"}
        yield {"type": "tool", "phase": "end", "tool": "calendar_read_day", "ok": True, "items": 14}
        yield {"type": "delta", "text": "Listo."}
        yield {
            "type": "done",
            "final_text": "Un momento. Listo.",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "pending_confirm": {
                "kind": "confirm",
                "action": "block",
                "start_iso": "2026-07-01T14:00:00+00:00",
                "end_iso": "2026-07-01T16:00:00+00:00",
            },
        }

    monkeypatch.setattr(cue_mod, "run_cue_turn_streaming", _fake_stream)
    _stub_route_gates(monkeypatch)

    raw = await _run_chat_raw(
        {"messages": [{"role": "user", "content": "read my calendar"}], "locale": "es"}
    )

    # --- tool frames present and well-formed (\x1f + JSON + \n) ---
    assert b"\x1f" in raw
    frames = []
    for chunk in raw.split(b"\x1f"):
        if b"\n" in chunk:
            head = chunk.split(b"\n", 1)[0]
            try:
                frames.append(json.loads(head.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                pass
    start_frames = [f for f in frames if isinstance(f, dict) and f.get("phase") == "start"]
    end_frames = [f for f in frames if isinstance(f, dict) and f.get("phase") == "end"]
    assert {"phase": "start", "tool": "calendar_read_day"} in start_frames
    assert {"phase": "end", "tool": "calendar_read_day", "ok": True, "items": 14} in end_frames

    # --- spoken text deltas are on the wire, unframed ---
    assert b"Un momento. " in raw
    assert b"Listo." in raw

    # --- the \x1e confirm tail STILL appears, AFTER the last text delta ---
    assert b"\x1e" in raw
    confirm_idx = raw.index(b"\x1e")
    listo_idx = raw.rindex("Listo.".encode("utf-8"))
    assert listo_idx < confirm_idx, "the confirm sentinel must trail the spoken text"
    tail = raw[confirm_idx + 1 :]
    payload = json.loads(tail.split(b"\n", 1)[0].decode("utf-8"))
    assert payload["pending_confirm"]["kind"] == "confirm"

    # --- tool frames are NOT folded into the spoken text: the JSON of a frame
    #     must not appear inside the plain-text region before the first \x1f.
    text_region = raw.split(b"\x1f", 1)[0]
    assert b"phase" not in text_region


# ---------------------------------------------------------------------------
# TE-G: a tool-free turn's bytes are byte-identical to pre-change behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_tool_free_turn_is_byte_identical(monkeypatch) -> None:
    import routes.cue_routes as cue_mod

    async def _fake_stream(*args, **kwargs) -> AsyncIterator[dict]:
        yield {"type": "delta", "text": "Hola, "}
        yield {"type": "delta", "text": "doctora."}
        yield {
            "type": "done",
            "final_text": "Hola, doctora.",
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "pending_confirm": None,
        }

    monkeypatch.setattr(cue_mod, "run_cue_turn_streaming", _fake_stream)
    _stub_route_gates(monkeypatch)

    raw = await _run_chat_raw({"messages": [{"role": "user", "content": "hola"}], "locale": "es"})

    # No tool framing, no confirm tail — exactly the spoken text bytes.
    assert raw == "Hola, doctora.".encode("utf-8")
    assert b"\x1f" not in raw
    assert b"\x1e" not in raw
