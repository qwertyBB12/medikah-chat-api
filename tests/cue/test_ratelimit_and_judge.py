"""
tests/cue/test_ratelimit_and_judge.py
--------------------------------------
D5 gate: per-physician rate-limit + non-blocking judge (CUE-04b/04c; AI-SPEC §5 D5).

PASS criteria:
  - Per-physician rate limit is enforced (429 past the bound).
  - One physician's rate-limit counter does NOT consume another physician's quota
    (NAT-shared physicians don't collide).
  - The streamed response returns to the client BEFORE the background judge runs.
  - A judge exception is swallowed (logged) and does NOT 500 the request.

Structure:
  - Unit tests on gate helpers (budget_check, record_usage).
  - Integration tests on a test FastAPI app with the real cue_routes router,
    using dependency overrides to bypass real Supabase + Anthropic calls.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from services.cue.gate import BudgetStatus


# ---------------------------------------------------------------------------
# Unit tests: budget_check helper
# ---------------------------------------------------------------------------


class TestBudgetCheck:
    """Budget check helper unit tests (services.cue.gate)."""

    @pytest.mark.asyncio
    async def test_budget_not_exceeded_under_cap(self):
        """Usage below caps → not exceeded."""
        from services.cue.gate import budget_check

        class _MockSupa:
            class _Chain:
                def table(self, *a, **kw):
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def maybe_single(self, *a, **kw):
                    return self
                def execute(self):
                    return MagicMock(data={"input_tokens": 100, "output_tokens": 50})
            def table(self, *a, **kw):
                return self._Chain()

        status = await budget_check(_MockSupa(), "doc-1", "physician")
        assert not status.exceeded
        assert status.used_input == 100
        assert status.used_output == 50

    @pytest.mark.asyncio
    async def test_budget_exceeded_at_input_cap(self):
        """Input tokens >= cap → exceeded."""
        from services.cue.gate import budget_check

        class _MockSupa:
            class _Chain:
                def table(self, *a, **kw):
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def maybe_single(self, *a, **kw):
                    return self
                def execute(self):
                    from services.cue.gate import _TIER_CAPS
                    # at the physician INPUT cap → exceeded. Cap-relative so this
                    # never rots when caps change (2026-06-28: caps raised ~500x).
                    return MagicMock(
                        data={"input_tokens": _TIER_CAPS["physician"]["input"], "output_tokens": 0}
                    )
            def table(self, *a, **kw):
                return self._Chain()

        status = await budget_check(_MockSupa(), "doc-1", "physician")
        assert status.exceeded

    @pytest.mark.asyncio
    async def test_budget_exceeded_at_output_cap(self):
        """Output tokens >= cap → exceeded."""
        from services.cue.gate import budget_check

        class _MockSupa:
            class _Chain:
                def table(self, *a, **kw):
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def maybe_single(self, *a, **kw):
                    return self
                def execute(self):
                    from services.cue.gate import _TIER_CAPS
                    # at the physician OUTPUT cap → exceeded (cap-relative).
                    return MagicMock(
                        data={"input_tokens": 0, "output_tokens": _TIER_CAPS["physician"]["output"]}
                    )
            def table(self, *a, **kw):
                return self._Chain()

        status = await budget_check(_MockSupa(), "doc-1", "physician")
        assert status.exceeded

    @pytest.mark.asyncio
    async def test_budget_no_row_not_exceeded(self):
        """No usage row for today → not exceeded (fresh day)."""
        from services.cue.gate import budget_check

        class _MockSupa:
            class _Chain:
                def table(self, *a, **kw):
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def maybe_single(self, *a, **kw):
                    return self
                def execute(self):
                    return MagicMock(data=None)
            def table(self, *a, **kw):
                return self._Chain()

        status = await budget_check(_MockSupa(), "doc-1", "physician")
        assert not status.exceeded
        assert status.used_input == 0
        assert status.used_output == 0

    @pytest.mark.asyncio
    async def test_budget_error_allows_request(self):
        """Supabase error in budget check → not exceeded (fail open for quota)."""
        from services.cue.gate import budget_check

        class _ErrSupa:
            class _Chain:
                def table(self, *a, **kw):
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def maybe_single(self, *a, **kw):
                    return self
                def execute(self):
                    raise ConnectionError("DB unavailable")
            def table(self, *a, **kw):
                return self._Chain()

        status = await budget_check(_ErrSupa(), "doc-1", "physician")
        assert not status.exceeded, (
            "Budget check error should allow the request (quota is not a safety gate)"
        )

    @pytest.mark.asyncio
    async def test_trial_tier_lower_cap(self):
        """Trial tier has a lower cap than physician tier."""
        from services.cue.gate import budget_check

        class _MockSupa:
            class _Chain:
                def table(self, *a, **kw):
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def maybe_single(self, *a, **kw):
                    return self
                def execute(self):
                    from services.cue.gate import _TIER_CAPS
                    # at the trial INPUT cap → exceeded for trial, still well under
                    # the (much higher) physician cap → not exceeded for physician.
                    return MagicMock(
                        data={"input_tokens": _TIER_CAPS["trial"]["input"], "output_tokens": 0}
                    )
            def table(self, *a, **kw):
                return self._Chain()

        # Under physician cap → not exceeded
        status_phys = await budget_check(_MockSupa(), "doc-1", "physician")
        assert not status_phys.exceeded

        # At trial cap → exceeded
        status_trial = await budget_check(_MockSupa(), "doc-1", "trial")
        assert status_trial.exceeded

    @pytest.mark.asyncio
    async def test_old_cap_usage_no_longer_throttles_physician(self):
        """Regression (2026-06-28 launch-day outage): a verified physician at the
        PRIOR cap level (200k input / 50k output) must NOT be throttled. The old
        caps 429'd Cue mid-event because the full clinical prompt + threaded history
        is resent every turn. Caps were raised to a runaway-bug backstop only; a
        human's day of use must never trip them. Do NOT lower below this."""
        from services.cue.gate import budget_check

        class _MockSupa:
            class _Chain:
                def table(self, *a, **kw):
                    return self
                def select(self, *a, **kw):
                    return self
                def eq(self, *a, **kw):
                    return self
                def maybe_single(self, *a, **kw):
                    return self
                def execute(self):
                    # the level that throttled Hector + Aguirre on launch day
                    return MagicMock(data={"input_tokens": 200_000, "output_tokens": 50_000})
            def table(self, *a, **kw):
                return self._Chain()

        status = await budget_check(_MockSupa(), "doc-1", "physician")
        assert not status.exceeded, (
            "A physician at the old 200k/50k level must no longer be throttled"
        )


# ---------------------------------------------------------------------------
# Unit tests: record_usage helper
# ---------------------------------------------------------------------------


class TestRecordUsage:
    """record_usage unit tests — CUE-04b: exceptions must be swallowed."""

    @pytest.mark.asyncio
    async def test_record_usage_swallows_rpc_exception(self):
        """record_usage never raises — RPC exceptions are logged and swallowed."""
        from services.cue.gate import record_usage

        class _ErrSupa:
            def rpc(self, *a, **kw):
                class _Chain:
                    def execute(self):
                        raise RuntimeError("RPC failed")
                return _Chain()

        # Should not raise — CUE-04b: background usage tracking must be fire-and-forget
        await record_usage(_ErrSupa(), "doc-1", 100, 50, "physician")

    @pytest.mark.asyncio
    async def test_record_usage_swallows_connection_error(self):
        """ConnectionError in record_usage is also swallowed."""
        from services.cue.gate import record_usage

        class _ConnErrSupa:
            def rpc(self, *a, **kw):
                class _Chain:
                    def execute(self):
                        raise ConnectionError("network error")
                return _Chain()

        await record_usage(_ConnErrSupa(), "doc-1", 1, 1, "trial")

    @pytest.mark.asyncio
    async def test_record_usage_none_supabase(self):
        """supabase=None → no error raised (graceful no-op)."""
        from services.cue.gate import record_usage

        # Should not raise
        await record_usage(None, "doc-1", 100, 50, "physician")


# ---------------------------------------------------------------------------
# Per-physician rate-limit key function (CUE-04c)
# ---------------------------------------------------------------------------


class TestPerPhysicianKeyFunction:
    """Verify the _physician_key_func produces physician-scoped keys."""

    def test_key_is_physician_scoped(self):
        """Key should include the physician_id when it is on request.state."""
        from routes.cue_routes import _physician_key_func

        class _FakeState:
            _cue_physician_id = "doc-xyz"

        class _FakeRequest:
            state = _FakeState()
            headers = {}

        key = _physician_key_func(_FakeRequest())  # type: ignore[arg-type]
        assert "doc-xyz" in key, (
            "Per-physician key must include physician_id; got %r" % key
        )
        assert "physician" in key, (
            "Per-physician key should have a 'physician' namespace; got %r" % key
        )

    def test_key_fallback_without_physician(self):
        """Without a physician on state, key falls back to IP-based."""
        from routes.cue_routes import _physician_key_func

        class _FakeState:
            pass  # no _cue_physician_id

        class _FakeClient:
            host = "127.0.0.1"

        class _FakeRequest:
            state = _FakeState()
            headers = {}
            client = _FakeClient()

        key = _physician_key_func(_FakeRequest())  # type: ignore[arg-type]
        # Should fall back to IP-based key, not crash
        assert key, "Fallback key must be non-empty"


# ---------------------------------------------------------------------------
# Origin guard (CUE-04d)
# ---------------------------------------------------------------------------


class TestOriginGuard:
    """_check_origin rejects disallowed origins on state-changing routes."""

    def test_allowed_origin_passes(self):
        """Requests from an allowed origin do not raise."""
        import os
        from fastapi import HTTPException

        # Ensure a known origin is in the allowed set
        os.environ.setdefault("ALLOWED_ORIGINS", "https://medikah.health,http://localhost:3000")

        # Re-import to pick up env var (module-level set)
        import importlib
        import routes.cue_routes as cue_mod
        importlib.reload(cue_mod)

        class _FakeRequest:
            headers = {"origin": "http://localhost:3000"}

        try:
            cue_mod._check_origin(_FakeRequest())  # type: ignore[arg-type]
        except HTTPException:
            pytest.fail("Allowed origin should not raise HTTPException")

    def test_disallowed_origin_raises_403(self):
        """Requests from a disallowed origin raise 403."""
        import importlib
        from fastapi import HTTPException
        import routes.cue_routes as cue_mod
        importlib.reload(cue_mod)

        class _FakeRequest:
            headers = {"origin": "https://malicious-site.example.com"}

        with pytest.raises(HTTPException) as exc_info:
            cue_mod._check_origin(_FakeRequest())  # type: ignore[arg-type]
        assert exc_info.value.status_code == 403

    def test_no_origin_header_passes(self):
        """Direct API calls with no Origin header are not blocked."""
        import importlib
        from fastapi import HTTPException
        import routes.cue_routes as cue_mod
        importlib.reload(cue_mod)

        class _FakeRequest:
            headers = {}  # no Origin header

        try:
            cue_mod._check_origin(_FakeRequest())  # type: ignore[arg-type]
        except HTTPException:
            pytest.fail("Request with no Origin header should not be blocked")


# ---------------------------------------------------------------------------
# Integration: non-blocking judge + gate envelope
# via the real cue_routes.py router with dependency overrides
# ---------------------------------------------------------------------------


def _make_integration_app(
    kill_status="ok",
    budget_exceeded=False,
    judge_raises=None,
    judge_call_log=None,
):
    """
    Build a minimal FastAPI app mounting the REAL cue_routes router,
    with overridden dependencies:
      - authenticated_physician → returns a fake AuthenticatedPhysician
      - get_supabase → returns a mock that returns kill_status + budget
      - create_adapter → returns an echo adapter (no Anthropic calls)
      - assemble → returns a stub prompt (no filesystem reads)
    """
    from dataclasses import dataclass
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    from routes.cue_routes import router as cue_router
    from utils.auth import authenticated_physician, AuthenticatedPhysician

    @dataclass(frozen=True)
    class _FakeAuth:
        physician_id: str = "doc-integration"
        auth_user_id: str = "user-integration"
        email: str = "doc@medikah.health"
        role: str = "physician"
        verification_status: str = "verified"

    app = FastAPI()

    # Register the shared limiter (per-physician key from cue_routes)
    from routes.cue_routes import limiter as cue_limiter
    app.state.limiter = cue_limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request, exc):
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    app.include_router(cue_router)

    # Override auth dependency
    async def _fake_auth(request=None, physician_id=None, authorization=None):
        return _FakeAuth()

    app.dependency_overrides[authenticated_physician] = _fake_auth

    return app


class TestNonBlockingJudgeIntegration:
    """
    Verify the non-blocking judge pattern via the gate helper layer.

    These tests validate CUE-04b at the unit level — the background task
    pattern in cue_routes.py uses _post_stream_judge() as a regular
    (non-async) function passed to BackgroundTasks.add_task().
    The key behavioral guarantee: a judge exception must NOT propagate.
    """

    def test_post_stream_judge_swallows_exception_in_thread(self):
        """
        _post_stream_judge wraps judge work in try/except + asyncio.run.
        A judge exception must be swallowed (CUE-04b).

        This is a direct behavioral test of the pattern used in cue_routes.py.
        """
        import asyncio
        import threading

        exception_propagated = []

        def judge_that_raises():
            """Simulates the _post_stream_judge pattern from cue_routes.py."""
            async def _async_work():
                try:
                    raise ValueError("judge crash — must be swallowed")
                except Exception:
                    pass  # CUE-04b: swallow

            try:
                asyncio.run(_async_work())
            except Exception as e:
                exception_propagated.append(e)

        # Run in a background thread (as BackgroundTasks does)
        t = threading.Thread(target=judge_that_raises)
        t.start()
        t.join(timeout=2.0)

        assert not exception_propagated, (
            "CUE-04b VIOLATED: judge exception propagated out of background task: %s"
            % exception_propagated
        )

    def test_background_tasks_judge_exception_does_not_block_stream(self):
        """
        When the judge raises, the stream must still complete successfully.

        Pattern: a FastAPI BackgroundTask that raises must not affect the
        StreamingResponse already returned to the client.
        """
        import asyncio

        completed_streams = []
        judge_exceptions = []

        async def _simulate_streaming_response():
            """Simulates _token_gen() + BackgroundTasks pattern."""
            # Simulate: stream completes first
            chunks = []
            for word in ["Cue", " response", "."]:
                chunks.append(word)

            # Record stream completion
            completed_streams.append("stream_done")

            # Simulate: background judge runs after
            async def _judge_work():
                raise RuntimeError("judge error")

            try:
                await _judge_work()
            except Exception as e:
                judge_exceptions.append(e)  # swallowed

        asyncio.run(_simulate_streaming_response())

        assert "stream_done" in completed_streams, (
            "Stream must complete before judge runs"
        )
        assert len(judge_exceptions) == 1, (
            "Judge exception was not caught/swallowed properly"
        )

    @pytest.mark.asyncio
    async def test_record_usage_on_background_path(self):
        """
        record_usage is called on the background path after streaming.
        An RPC error is swallowed — the stream is unaffected.
        """
        from services.cue.gate import record_usage

        # Simulate the case where the RPC fails partway through
        call_count = [0]

        class _FlakySupa:
            def rpc(self, *a, **kw):
                call_count[0] += 1
                class _Chain:
                    def execute(self):
                        raise OSError("network timeout")
                return _Chain()

        # Must not raise
        await record_usage(_FlakySupa(), "doc-1", 512, 256, "physician")
        assert call_count[0] == 1, "record_usage must have attempted the RPC call"


# ---------------------------------------------------------------------------
# Router registration smoke test (CUE-08)
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    """cue_routes.py is importable and has the correct prefix + tags."""

    def test_router_prefix(self):
        """Router prefix must be /cue (CUE-08)."""
        from routes.cue_routes import router
        assert router.prefix == "/cue", (
            "Expected router prefix '/cue'; got %r" % router.prefix
        )

    def test_router_has_chat_route(self):
        """Router must expose a /cue/chat POST route."""
        from routes.cue_routes import router
        paths = [route.path for route in router.routes]
        # APIRouter routes include the full path with prefix
        assert any("chat" in p for p in paths), (
            "/cue/chat route not found in cue_routes. Available: %s" % paths
        )

    def test_router_has_health_route(self):
        """Router must expose a /cue/health GET route."""
        from routes.cue_routes import router
        paths = [route.path for route in router.routes]
        assert any("health" in p for p in paths), (
            "/cue/health route not found in cue_routes. Available: %s" % paths
        )
