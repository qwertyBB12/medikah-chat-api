# Wave 0 RED scaffold — implemented by Plan 23-03
"""
tests/cue/test_mail_reader.py
-------------------------------
Wave 0 failing scaffold for the Cue IMAP read-only client (HANDS-02).

These tests MUST fail (ImportError) until Plan 23-03 writes
services/cue/mail_reader.py.  The failing import is the intended RED state.

Requirements gated:
  HANDS-02: IMAP READ-ONLY client: no Seen mutation (mark_seen=False), TLS 993,
            transient bodies, never persist message content.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# RED import — this module does not exist yet (Plan 23-03 creates it).
# The collection error here is the EXPECTED red-state for Wave 0.
# ---------------------------------------------------------------------------
from services.cue.mail_reader import read_recent  # noqa: E402


# ---------------------------------------------------------------------------
# Test 1 — HANDS-02: read_recent must use mark_seen=False
# ---------------------------------------------------------------------------

class TestReadRecentMarkSeenFalse:
    """read_recent must fetch with mark_seen=False — never mutate the IMAP Seen flag."""

    def test_read_recent_is_async_or_sync(self):
        """read_recent may be sync (imap-tools MailBox is synchronous) or async.
        Either form is acceptable; we only gate that it is callable.
        """
        assert callable(read_recent), "read_recent must be callable"

    def test_read_recent_signature(self):
        """read_recent must accept username and password as its first two params."""
        sig = inspect.signature(read_recent)
        params = list(sig.parameters.keys())
        assert "username" in params, "read_recent must accept 'username'"
        assert "password" in params, "read_recent must accept 'password'"

    def test_read_recent_has_limit_param(self):
        """read_recent must accept a 'limit' param to bound the result set."""
        sig = inspect.signature(read_recent)
        assert "limit" in sig.parameters, (
            "read_recent must accept 'limit' to bound result count"
        )

    def test_read_recent_calls_fetch_with_mark_seen_false(self, monkeypatch):
        """HANDS-02: the imap-tools MailBox.fetch() call must use mark_seen=False.

        Capturing the fetch() kwargs proves the implementation does not
        silently mutate the IMAP Seen flag on the physician's inbox.
        """
        fetch_kwargs_seen: list[dict] = []

        class MockMsg:
            subject = "Test subject"
            from_   = "patient@example.com"
            date    = "2026-07-01"
            uid     = "42"

        class MockMailbox:
            def login(self, *args, **kwargs):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def fetch(self, criteria, **kwargs):
                fetch_kwargs_seen.append(kwargs)
                return iter([MockMsg()])

        # Patch the imap_tools.MailBox class
        with patch("imap_tools.MailBox", return_value=MockMailbox()):
            try:
                result = read_recent(
                    username="testdoctor@medikah.health",
                    password="app-pass-value",
                    limit=5,
                )
                # If async, unwrap it
                import asyncio
                if asyncio.iscoroutine(result):
                    asyncio.get_event_loop().run_until_complete(result)
            except Exception:
                # We only care about the fetch kwargs — an exception on other
                # parts (e.g. missing env var) is acceptable here
                pass

        assert len(fetch_kwargs_seen) >= 1, (
            "HANDS-02: read_recent must call MailBox.fetch() at least once"
        )

        for call_kwargs in fetch_kwargs_seen:
            mark_seen = call_kwargs.get("mark_seen")
            assert mark_seen is False, (
                f"HANDS-02 VIOLATION: MailBox.fetch() called with mark_seen={mark_seen!r}. "
                f"It MUST be False — any other value mutates the IMAP Seen flag on the "
                f"physician's inbox, violating the read-only contract."
            )


# ---------------------------------------------------------------------------
# Test 2 — HANDS-02: read_recent must never persist message bodies
# ---------------------------------------------------------------------------

class TestReadRecentNoPersistence:
    """read_recent must return a lightweight summary dict; bodies must be transient."""

    def test_read_recent_returns_list(self, monkeypatch):
        """read_recent must return a list of message summary dicts."""

        class MockMsg:
            subject = "Hello"
            from_   = "a@b.com"
            date    = "2026-01-01"
            uid     = "1"

        class MockMailbox:
            def login(self, *a, **kw): return self
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def fetch(self, *a, **kw): return iter([MockMsg()])

        with patch("imap_tools.MailBox", return_value=MockMailbox()):
            result = read_recent(
                username="testdoctor@medikah.health",
                password="app-pass",
            )
            import asyncio
            if asyncio.iscoroutine(result):
                result = asyncio.get_event_loop().run_until_complete(result)

        assert isinstance(result, list), (
            f"read_recent must return a list; got {type(result)}"
        )

    def test_read_recent_result_has_no_body_field(self, monkeypatch):
        """HANDS-02: result dicts must NOT contain a 'body' or 'text' or 'html' key.

        Message bodies must never be persisted — only subject, from, date, uid
        are acceptable as summary fields for inbox context.
        """

        class MockMsg:
            subject = "Important"
            from_   = "patient@test.com"
            date    = "2026-02-15"
            uid     = "99"
            text    = "Secret patient message body"
            html    = "<p>Secret patient message body</p>"
            headers = {}

        class MockMailbox:
            def login(self, *a, **kw): return self
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def fetch(self, *a, **kw): return iter([MockMsg()])

        with patch("imap_tools.MailBox", return_value=MockMailbox()):
            result = read_recent(
                username="testdoctor@medikah.health",
                password="app-pass",
                limit=1,
            )
            import asyncio
            if asyncio.iscoroutine(result):
                result = asyncio.get_event_loop().run_until_complete(result)

        assert len(result) >= 1, "Expected at least one result item"

        for item in result:
            assert isinstance(item, dict), f"Each item must be a dict; got {type(item)}"
            for forbidden_key in ("body", "text", "html", "payload", "content"):
                assert forbidden_key not in item, (
                    f"HANDS-02 VIOLATION: read_recent result contains '{forbidden_key}' key. "
                    f"Message bodies must be transient — never stored in the return value. "
                    f"Only subject, from_, date, uid are acceptable summary fields."
                )

    def test_read_recent_uses_headers_only_or_minimal_fetch(self, monkeypatch):
        """HANDS-02: to avoid fetching bodies, read_recent should use headers_only=True
        or an equivalent minimal fetch strategy (e.g. BODY[HEADER.FIELDS]).

        This test checks that the fetch keyword argument set does NOT omit
        headers_only when calling imap-tools MailBox.fetch().
        """
        fetch_kwargs_seen: list[dict] = []

        class MockMsg:
            subject = "S"
            from_   = "f@g.com"
            date    = "2026-01-01"
            uid     = "1"

        class MockMailbox:
            def login(self, *a, **kw): return self
            def __enter__(self): return self
            def __exit__(self, *a): pass

            def fetch(self, *a, **kw):
                fetch_kwargs_seen.append(kw)
                return iter([MockMsg()])

        with patch("imap_tools.MailBox", return_value=MockMailbox()):
            try:
                result = read_recent(
                    username="doc@medikah.health",
                    password="pass",
                    limit=3,
                )
                import asyncio
                if asyncio.iscoroutine(result):
                    asyncio.get_event_loop().run_until_complete(result)
            except Exception:
                pass

        assert fetch_kwargs_seen, "read_recent must call MailBox.fetch()"

        for kw in fetch_kwargs_seen:
            headers_only = kw.get("headers_only")
            assert headers_only is True, (
                f"HANDS-02: read_recent should call fetch(headers_only=True) to avoid "
                f"fetching message bodies; got headers_only={headers_only!r}. "
                f"Without headers_only=True, the full body is transferred over the "
                f"IMAP connection and enters Python memory even if not returned to caller."
            )
