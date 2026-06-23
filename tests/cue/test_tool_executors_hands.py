# Wave 0 RED scaffold — implemented by Plan 23-03 / 23-04
"""
tests/cue/test_tool_executors_hands.py
----------------------------------------
Wave 0 failing scaffold for the Phase 23 Cue tool executors (HANDS-04 / CUE-11).

These tests MUST fail (ImportError or AttributeError) until Plan 23-03/04 implement
the calendar_block_time, calendar_clear_range, and inbox_read_recent executors
in services/cue/tools/executors.py.  The failing import is the intended RED state.

Requirements gated:
  HANDS-04: calendar_block_time, calendar_clear_range, inbox_read_recent are
            physician_id-scoped (session-derived, NEVER from tool_input).
  CUE-11:   IDOR discipline — physician_id as explicit keyword arg only.

IMPORTANT: This scaffold does NOT modify or replace the existing Phase-22 stubs
(calendar_read_day, availability_read, inquiry_list_recent).  It ONLY asserts
on the THREE NEW Phase-23 executors.
"""

from __future__ import annotations

import inspect

import pytest

# ---------------------------------------------------------------------------
# RED import — calendar_block_time, calendar_clear_range, inbox_read_recent
# do not exist in executors.py yet (Phase 22 only has stubs for read_day,
# availability_read, inquiry_list_recent).
# The ImportError here is the EXPECTED red-state for Wave 0.
# ---------------------------------------------------------------------------
from services.cue.tools.executors import (  # noqa: E402
    calendar_block_time,
    calendar_clear_range,
    inbox_read_recent,
)


# ---------------------------------------------------------------------------
# Test 1 — CUE-11 / HANDS-04: physician_id must be a keyword-only arg
# (never accepted from tool_input)
# ---------------------------------------------------------------------------

class TestPhysicianIdAsKeywordOnlyArg:
    """All three new executors must accept physician_id ONLY as an explicit
    keyword argument — never as part of **tool_input kwargs.

    This is the CUE-11 IDOR discipline established in Phase 22.  The dispatcher
    (registry.py dispatch_tool) strips identity keys from tool_input using
    _IDENTITY_KEYS before passing to executors — so if an executor tried to read
    physician_id from **kwargs, it would get None or KeyError, not the real value.

    The correct pattern: physician_id is an explicit positional-or-keyword param
    that dispatch_tool() always passes by keyword: executor(physician_id=pid, ...)
    """

    def test_calendar_block_time_accepts_physician_id_as_kwarg(self):
        """calendar_block_time signature must include physician_id as an explicit param."""
        sig = inspect.signature(calendar_block_time)
        params = sig.parameters
        assert "physician_id" in params, (
            "CUE-11: calendar_block_time must accept physician_id as an explicit "
            "keyword argument (not via **kwargs)"
        )

    def test_calendar_clear_range_accepts_physician_id_as_kwarg(self):
        """calendar_clear_range signature must include physician_id as an explicit param."""
        sig = inspect.signature(calendar_clear_range)
        params = sig.parameters
        assert "physician_id" in params, (
            "CUE-11: calendar_clear_range must accept physician_id as an explicit "
            "keyword argument (not via **kwargs)"
        )

    def test_inbox_read_recent_accepts_physician_id_as_kwarg(self):
        """inbox_read_recent signature must include physician_id as an explicit param."""
        sig = inspect.signature(inbox_read_recent)
        params = sig.parameters
        assert "physician_id" in params, (
            "CUE-11: inbox_read_recent must accept physician_id as an explicit "
            "keyword argument (not via **kwargs)"
        )

    def test_calendar_block_time_does_not_accept_var_kwargs_as_physician_id_path(self):
        """calendar_block_time must not use **kwargs as the primary physician_id path.

        Having **kwargs is acceptable only if physician_id is ALSO an explicit param.
        The explicit param is what dispatch_tool() targets with physician_id=pid.
        """
        sig = inspect.signature(calendar_block_time)
        # physician_id must be an explicit parameter (POSITIONAL_OR_KEYWORD or KEYWORD_ONLY)
        pid_param = sig.parameters.get("physician_id")
        assert pid_param is not None, (
            "CUE-11: calendar_block_time has no explicit 'physician_id' parameter"
        )
        assert pid_param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ), (
            f"CUE-11: calendar_block_time.physician_id must be POSITIONAL_OR_KEYWORD or "
            f"KEYWORD_ONLY; got {pid_param.kind.name}"
        )

    def test_calendar_clear_range_does_not_accept_var_kwargs_as_physician_id_path(self):
        sig = inspect.signature(calendar_clear_range)
        pid_param = sig.parameters.get("physician_id")
        assert pid_param is not None
        assert pid_param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ), (
            f"CUE-11: calendar_clear_range.physician_id kind: {pid_param.kind.name}"
        )

    def test_inbox_read_recent_does_not_accept_var_kwargs_as_physician_id_path(self):
        sig = inspect.signature(inbox_read_recent)
        pid_param = sig.parameters.get("physician_id")
        assert pid_param is not None
        assert pid_param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ), (
            f"CUE-11: inbox_read_recent.physician_id kind: {pid_param.kind.name}"
        )


# ---------------------------------------------------------------------------
# Test 2 — HANDS-04: functional params must NOT include identity keys
# ---------------------------------------------------------------------------

class TestNoIdentityKeysInFunctionalParams:
    """The executors must not define any of the IDOR-strip identity keys
    (physician_id, slug, doctor_id, user_id) as parameters that could be
    populated from tool_input.

    The _IDENTITY_KEYS strip in dispatch_tool() removes these before the
    executor call — so an executor that declares them as plain positional
    params (without the explicit physician_id kwarg) would get None.
    This test ensures the executor contract stays aligned with the dispatcher.
    """

    IDENTITY_KEYS = frozenset({"slug", "doctor_id", "user_id"})

    def _check_no_identity_params_besides_physician_id(
        self, func: object, func_name: str
    ) -> None:
        sig = inspect.signature(func)
        params = set(sig.parameters.keys())
        forbidden_in_params = params & self.IDENTITY_KEYS
        assert not forbidden_in_params, (
            f"CUE-11: {func_name} declares identity-key params that would be "
            f"stripped by dispatch_tool's _safe_tool_input before reaching the executor: "
            f"{forbidden_in_params}. Remove them — physician_id is the only allowed "
            f"identity param and must be explicitly declared."
        )

    def test_calendar_block_time_no_slug_or_user_id(self):
        self._check_no_identity_params_besides_physician_id(
            calendar_block_time, "calendar_block_time"
        )

    def test_calendar_clear_range_no_slug_or_user_id(self):
        self._check_no_identity_params_besides_physician_id(
            calendar_clear_range, "calendar_clear_range"
        )

    def test_inbox_read_recent_no_slug_or_user_id(self):
        self._check_no_identity_params_besides_physician_id(
            inbox_read_recent, "inbox_read_recent"
        )


# ---------------------------------------------------------------------------
# Test 3 — HANDS-04: executor signatures match the documented contracts
# ---------------------------------------------------------------------------

class TestExecutorSignatureContracts:
    """Verify the full parameter set for each executor matches the Phase-23
    plan specification (23-PATTERNS.md executor signature patterns).
    """

    def test_calendar_block_time_functional_params(self):
        """calendar_block_time must accept start_iso, end_iso, title as functional args."""
        sig = inspect.signature(calendar_block_time)
        params = set(sig.parameters.keys())
        for required in ("start_iso", "end_iso", "title"):
            assert required in params, (
                f"HANDS-04: calendar_block_time missing functional param '{required}'"
            )

    def test_calendar_clear_range_functional_params(self):
        """calendar_clear_range must accept start_iso and end_iso."""
        sig = inspect.signature(calendar_clear_range)
        params = set(sig.parameters.keys())
        for required in ("start_iso", "end_iso"):
            assert required in params, (
                f"HANDS-04: calendar_clear_range missing functional param '{required}'"
            )

    def test_inbox_read_recent_limit_param(self):
        """inbox_read_recent must accept 'limit' as a functional arg (capped by dispatcher)."""
        sig = inspect.signature(inbox_read_recent)
        assert "limit" in sig.parameters, (
            "HANDS-04: inbox_read_recent must accept 'limit' (functional arg from tool_input)"
        )
        # limit must have a default (dispatcher caps it at 20 but executor must default)
        limit_param = sig.parameters["limit"]
        assert limit_param.default is not inspect.Parameter.empty, (
            "inbox_read_recent 'limit' must have a default value (e.g. 10)"
        )

    def test_all_three_executors_are_async(self):
        """All three new executors must be async functions (they call async services)."""
        for name, func in [
            ("calendar_block_time", calendar_block_time),
            ("calendar_clear_range", calendar_clear_range),
            ("inbox_read_recent", inbox_read_recent),
        ]:
            assert inspect.iscoroutinefunction(func), (
                f"HANDS-04: {name} must be async (calls async CalDAV/IMAP services)"
            )


# ---------------------------------------------------------------------------
# Test 4 — Phase 22 existing executors are unchanged (non-regression guard)
# ---------------------------------------------------------------------------

class TestPhase22ExecutorsUnmodified:
    """Verify that the Phase-22 executor stubs are still importable and their
    signatures are unchanged.  Plan 23-01 must not touch these.
    """

    def test_calendar_read_day_still_importable(self):
        from services.cue.tools.executors import calendar_read_day
        assert callable(calendar_read_day)

    def test_availability_read_still_importable(self):
        from services.cue.tools.executors import availability_read
        assert callable(availability_read)

    def test_inquiry_list_recent_still_importable(self):
        from services.cue.tools.executors import inquiry_list_recent
        assert callable(inquiry_list_recent)

    def test_calendar_read_day_signature_unchanged(self):
        from services.cue.tools.executors import calendar_read_day
        sig = inspect.signature(calendar_read_day)
        params = set(sig.parameters.keys())
        assert "physician_id" in params, "calendar_read_day must still have physician_id"
        assert "date" in params, "calendar_read_day must still have date"

    def test_inquiry_list_recent_signature_unchanged(self):
        from services.cue.tools.executors import inquiry_list_recent
        sig = inspect.signature(inquiry_list_recent)
        params = set(sig.parameters.keys())
        assert "physician_id" in params
        assert "limit" in params
