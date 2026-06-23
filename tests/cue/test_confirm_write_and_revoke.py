"""
tests/cue/test_confirm_write_and_revoke.py
-------------------------------------------
Plan 23-04 route-level safety tests for the WRITE increment:

  - POST /cue/calendar/confirm-write idempotency (HANDS-04, T-23-04-08):
    a replayed (physician_id, idempotency_token) returns the cached result and
    creates EXACTLY ONE block VEVENT with a stable uid (the calendar write runs
    only on the first call; the replay is served from cue_write_idempotency).

  - DELETE /cue/credential during a TRIPPED kill-switch (HANDS-09a, T-23-04-05):
    revoke is NOT fail-closed — it must succeed even when the global kill-switch
    is tripped, so a doctor is never trapped with Cue connected during an
    incident. The revoke audit row carries IP+UA derived from the route's own
    Request.

These call the UNDECORATED route handlers (via __wrapped__) so the slowapi
rate-limit wrapper / app.state.limiter is bypassed — the gate, idempotency,
audit, and CalDAV logic under test are unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

import routes.cue_routes as routes_mod


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal Request stand-in exposing .state, .headers, .client."""

    def __init__(self, headers: Optional[dict] = None, ip: str = "203.0.113.9") -> None:
        self.state = SimpleNamespace()
        self.headers = headers or {"user-agent": "pytest-agent/1.0"}
        self.client = SimpleNamespace(host=ip)


class _FakeAuth:
    def __init__(self, physician_id: str, status: str = "verified") -> None:
        self.physician_id = physician_id
        self.verification_status = status


class _FakeIdempotencyStore:
    """In-memory stand-in for the cue_write_idempotency + workspace_audit_log tables.

    Supports the chained calls the route uses:
      table(...).select(...).eq(...).eq(...).limit(...).execute()  -> lookup
      table(...).upsert(..., ignore_duplicates=True).execute()     -> store (no-dup)
      table(...).insert(...).execute()                             -> audit append
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.audit: list[dict] = []
        # query-building scratch state
        self._table: Optional[str] = None
        self._eqs: dict[str, Any] = {}
        self._pending_upsert: Optional[dict] = None
        self._pending_insert: Optional[dict] = None

    # chain entrypoint
    def table(self, name: str):
        self._table = name
        self._eqs = {}
        self._pending_upsert = None
        self._pending_insert = None
        return self

    def select(self, *a, **kw):
        return self

    def eq(self, col, val):
        self._eqs[col] = val
        return self

    def limit(self, *a, **kw):
        return self

    def upsert(self, row, *, on_conflict=None, ignore_duplicates=False):
        self._pending_upsert = {
            "row": row,
            "ignore_duplicates": ignore_duplicates,
        }
        return self

    def insert(self, row):
        self._pending_insert = row
        return self

    def execute(self):
        # AUDIT insert
        if self._table == "workspace_audit_log" and self._pending_insert is not None:
            self.audit.append(self._pending_insert)
            return SimpleNamespace(data=[self._pending_insert])

        # IDEMPOTENCY upsert (ON CONFLICT DO NOTHING)
        if self._table == "cue_write_idempotency" and self._pending_upsert is not None:
            row = self._pending_upsert["row"]
            key = (row["physician_id"], row["idempotency_token"])
            if key not in self.rows:  # ignore_duplicates → no overwrite
                self.rows[key] = row["result_json"]
            return SimpleNamespace(data=[])

        # IDEMPOTENCY lookup
        if self._table == "cue_write_idempotency":
            key = (self._eqs.get("physician_id"), self._eqs.get("idempotency_token"))
            if key in self.rows:
                return SimpleNamespace(data=[{"result_json": self.rows[key]}])
            return SimpleNamespace(data=[])

        return SimpleNamespace(data=[])


# ---------------------------------------------------------------------------
# Idempotency — a replayed token writes exactly ONE VEVENT, stable uid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_write_block_is_idempotent_on_replayed_token(monkeypatch):
    """T-23-04-08: a double-clicked Confirm (same token) creates ONE block, stable uid."""
    store = _FakeIdempotencyStore()
    monkeypatch.setattr(routes_mod, "get_supabase", lambda: store)

    # kill-switch ok
    async def _ok(*a, **kw):
        return "ok"

    monkeypatch.setattr(routes_mod, "check_kill_switch", _ok)
    # no origin header → origin guard passes

    # Workspace context: verified + a local part
    import services.cue.tools.executors as execs_mod
    monkeypatch.setattr(
        execs_mod, "_load_workspace_context", lambda pid: ("drtest", "verified")
    )

    # Credential broker hands out a (fake) credential without touching Mailcow.
    import services.cue.credential_broker as broker_mod
    cred = broker_mod.CueCredential(
        username="drtest@medikah.health", password="secret", app_passwd_id="appid-1"
    )

    async def _fake_get_cred(physician_id, mailbox_local_part, *a, **kw):
        return cred

    monkeypatch.setattr(broker_mod, "get_cue_cred", _fake_get_cred)
    monkeypatch.setattr(broker_mod, "get_cue_credential", _fake_get_cred)

    # calendar_dav.block_time: count calls; return a stable uid per call.
    import services.cue.calendar_dav as caldav_mod
    calls = {"n": 0}

    async def _fake_block(username, password, start_iso, end_iso, title, *, physician_id=None):
        calls["n"] += 1
        return f"cue-fixed-uid-{calls['n']}"

    monkeypatch.setattr(caldav_mod, "block_time", _fake_block)

    body = routes_mod.CueConfirmWriteRequest(
        action="block",
        start_iso="2026-07-01T14:00:00+00:00",
        end_iso="2026-07-01T16:00:00+00:00",
        title="Blocked by Cue",
        idempotency_token="tok-abc-123",
        locale="es",
    )
    auth = _FakeAuth("11111111-1111-1111-1111-111111111111")

    handler = routes_mod.cue_confirm_write.__wrapped__

    # First call → real write (uid cue-fixed-uid-1).
    r1 = await handler(_FakeRequest(), body, auth)
    # Second call, SAME token → cached result; NO second write.
    r2 = await handler(_FakeRequest(), body, auth)

    assert r1 == {"blocked": True, "uid": "cue-fixed-uid-1"}
    assert r2 == r1, "Replayed token must return the cached result (stable uid)."
    assert calls["n"] == 1, (
        "A replayed Confirm must create EXACTLY ONE block VEVENT — "
        f"calendar_dav.block_time was called {calls['n']} times."
    )
    # Exactly one audit row was written (only the first, real write is audited).
    block_rows = [r for r in store.audit if r["action"] == "cue.calendar_block_time"]
    assert len(block_rows) == 1
    # The audit row carries IP + UA from the route's own Request (HANDS-08a).
    assert block_rows[0]["detail"].get("ip") == "203.0.113.9"
    assert block_rows[0]["detail"].get("ua") == "pytest-agent/1.0"


# ---------------------------------------------------------------------------
# Revoke is NOT fail-closed: succeeds during a tripped kill-switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_succeeds_during_tripped_kill_switch(monkeypatch):
    """T-23-04-05: DELETE /cue/credential proceeds even when the kill-switch is tripped."""
    # If the revoke route checked the kill-switch, this would block it. It must NOT.
    async def _tripped(*a, **kw):
        return "tripped"

    monkeypatch.setattr(routes_mod, "check_kill_switch", _tripped, raising=False)

    captured: dict = {}

    import services.cue.credential_broker as broker_mod

    async def _fake_revoke(physician_id, *, ip=None, ua=None):
        captured["physician_id"] = physician_id
        captured["ip"] = ip
        captured["ua"] = ua
        return True

    monkeypatch.setattr(broker_mod, "revoke_cue_credential", _fake_revoke)

    auth = _FakeAuth("22222222-2222-2222-2222-222222222222")
    req = _FakeRequest(
        headers={
            "user-agent": "pytest-agent/2.0",
            "X-Forwarded-For": "198.51.100.7, 10.0.0.1",
        }
    )

    handler = routes_mod.cue_revoke_credential.__wrapped__
    result = await handler(req, auth)

    assert result == {"revoked": True}, (
        "Revoke must succeed during a tripped kill-switch (NOT fail-closed)."
    )
    assert captured["physician_id"] == auth.physician_id, (
        "Revoke must use auth.physician_id (CUE-11), never a body value."
    )
    # Audit IP+UA come from the route's own Request: XFF first hop + UA header.
    assert captured["ip"] == "198.51.100.7"
    assert captured["ua"] == "pytest-agent/2.0"
