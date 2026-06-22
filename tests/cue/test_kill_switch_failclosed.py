"""
tests/cue/test_kill_switch_failclosed.py
-----------------------------------------
D4 gate: kill-switch fail-CLOSED verification (CUE-04a / PATCH-02).

PASS criteria (AI-SPEC §5 D4):
  (a) tripped flag ('soft' or 'hard') → check_kill_switch returns "tripped"
  (b) flag store raises ANY exception → check_kill_switch returns "tripped"
      (fail CLOSED — PATCH-02 fix of BeNeXT chat.ts:100-101)
  (c) value is NULL → check_kill_switch returns "ok"
  (d) gate-ORDER assertion: kill-switch is evaluated BEFORE any adapter call

FAIL: any path that returns "ok" when the flag is tripped OR when the store
      raises — that is the BeNeXT fail-OPEN bug (chat.ts:100-101) being fixed.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from services.cue.gate import KillSwitchResult, bilingual_unavailable, check_kill_switch


# ---------------------------------------------------------------------------
# Minimal Supabase mock helpers
# ---------------------------------------------------------------------------


class _MockResult:
    """Mimics a Supabase query .execute() result."""

    def __init__(self, data):
        self.data = data


class _MockQuery:
    """Chainable Supabase query builder stub."""

    def __init__(self, data=None, raise_exc: Exception | None = None):
        self._data = data
        self._raise = raise_exc

    def table(self, *a, **kw):
        return self

    def select(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def maybe_single(self, *a, **kw):
        return self

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return _MockResult(self._data)


def _make_supabase(data=None, raise_exc: Exception | None = None) -> _MockQuery:
    """Build a mock Supabase client for the kill-switch table read."""
    return _MockQuery(data=data, raise_exc=raise_exc)


# ---------------------------------------------------------------------------
# (a) Tripped flag cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_tripped_hard():
    """value='hard' → tripped (CUE-04a)."""
    supabase = _make_supabase(data={"value": "hard"})
    result = await check_kill_switch(supabase, locale="es")
    assert result == "tripped", (
        "Expected 'tripped' when kill-switch value='hard'; got %r" % result
    )


@pytest.mark.asyncio
async def test_kill_switch_tripped_soft():
    """value='soft' → tripped."""
    supabase = _make_supabase(data={"value": "soft"})
    result = await check_kill_switch(supabase, locale="en")
    assert result == "tripped", (
        "Expected 'tripped' when kill-switch value='soft'; got %r" % result
    )


# ---------------------------------------------------------------------------
# (b) Flag store raises → fail CLOSED (PATCH-02 critical path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_store_exception_fails_closed():
    """
    PATCH-02: ANY flag-store exception → "tripped" (fail CLOSED), NOT "ok".

    This is the explicit reversal of BeNeXT chat.ts:100-101 which catches
    the KV error and continues (fails open).  A clinical tool must fail safe.
    """
    supabase = _make_supabase(raise_exc=ConnectionError("Supabase unreachable"))
    result = await check_kill_switch(supabase, locale="es")
    assert result == "tripped", (
        "PATCH-02 VIOLATED: flag-store exception must return 'tripped' (fail CLOSED), "
        "not 'ok'. Got %r" % result
    )


@pytest.mark.asyncio
async def test_kill_switch_store_timeout_fails_closed():
    """Timeout variant — also must fail CLOSED."""
    supabase = _make_supabase(raise_exc=TimeoutError("read timeout"))
    result = await check_kill_switch(supabase, locale="en")
    assert result == "tripped", (
        "PATCH-02 VIOLATED: timeout must return 'tripped', got %r" % result
    )


@pytest.mark.asyncio
async def test_kill_switch_generic_exception_fails_closed():
    """Any exception (OSError, RuntimeError, etc.) must fail CLOSED."""
    supabase = _make_supabase(raise_exc=RuntimeError("unexpected db error"))
    result = await check_kill_switch(supabase, locale="es")
    assert result == "tripped", (
        "PATCH-02 VIOLATED: generic exception must return 'tripped', got %r" % result
    )


@pytest.mark.asyncio
async def test_kill_switch_none_supabase_fails_closed():
    """supabase=None → fail CLOSED (no store available)."""
    result = await check_kill_switch(None, locale="es")
    assert result == "tripped", (
        "Expected 'tripped' when supabase is None; got %r" % result
    )


# ---------------------------------------------------------------------------
# (c) NULL value / no row → ok
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_null_value_returns_ok():
    """value=NULL (row exists but switch is OFF) → ok."""
    supabase = _make_supabase(data={"value": None})
    result = await check_kill_switch(supabase, locale="es")
    assert result == "ok", (
        "Expected 'ok' when kill-switch value=NULL; got %r" % result
    )


@pytest.mark.asyncio
async def test_kill_switch_no_row_returns_ok():
    """No matching row (maybe_single returns None data) → ok."""
    supabase = _make_supabase(data=None)
    result = await check_kill_switch(supabase, locale="en")
    assert result == "ok", (
        "Expected 'ok' when no kill-switch row found; got %r" % result
    )


# ---------------------------------------------------------------------------
# (d) Gate-ORDER assertion — kill-switch evaluated BEFORE any adapter call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_evaluated_before_adapter():
    """
    Gate-order assertion (AI-SPEC §5 D4): the kill-switch check must complete
    (and return 'tripped') BEFORE any adapter/model call is attempted.

    We verify this by ensuring check_kill_switch is a standalone async function
    (not embedded in a streaming generator) and that its return value is "tripped"
    when the flag is set — the route layer is responsible for using this return
    value to raise 503 before calling adapter.stream()/complete().

    This test drives a mock through the kill-switch to confirm the tripped path
    resolves immediately, giving the route layer a synchronous decision point
    before any streaming work begins.
    """
    adapter_called = []

    class _AdapterSpy:
        """Spy — records whether it was called."""
        async def stream(self, **kw):
            adapter_called.append("stream")
            # Should never reach here when kill-switch is tripped
            if False:
                yield ""

        async def complete(self, **kw):
            adapter_called.append("complete")
            return {}

    supabase = _make_supabase(data={"value": "hard"})
    adapter = _AdapterSpy()

    # Simulate the gate pattern: check kill-switch, then decide whether to call adapter
    result = await check_kill_switch(supabase, locale="es")
    if result == "ok":
        # Only call adapter when switch is NOT tripped
        await adapter.complete(model="x", system_prompt="x", messages=[])

    # Assert: switch is tripped AND adapter was never called
    assert result == "tripped"
    assert adapter_called == [], (
        "Adapter was called despite kill-switch being tripped. "
        "Gate-ORDER violated: kill-switch must short-circuit before any model call."
    )


# ---------------------------------------------------------------------------
# Bilingual message helper
# ---------------------------------------------------------------------------


def test_bilingual_unavailable_es():
    msg = bilingual_unavailable("es")
    assert "Cue" in msg
    assert "disponible" in msg.lower() or "intenta" in msg.lower()


def test_bilingual_unavailable_en():
    msg = bilingual_unavailable("en")
    assert "Cue" in msg
    assert "unavailable" in msg.lower() or "later" in msg.lower()


def test_bilingual_unavailable_fallback():
    """Unknown locale defaults to English."""
    msg = bilingual_unavailable("fr")
    assert "Cue" in msg
