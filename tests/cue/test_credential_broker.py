# Wave 0 RED scaffold — implemented by Plan 23-02
"""
tests/cue/test_credential_broker.py
-------------------------------------
Wave 0 failing scaffold for the Cue credential broker (HANDS-01/05/05a/08/09/09a).

These tests MUST fail (ImportError / AttributeError) until Plan 23-02 writes
services/cue/credential_broker.py.  The failing import is the intended RED state.

Requirements gated:
  HANDS-01: Mailcow app-passwd broker with imap+dav, smtp_access=0, one-call revoke.
  HANDS-05: No-send enforced at CREDENTIAL: smtp_access=0 at mint time.
  HANDS-05a: Mint with active="1" NEVER active="2" (active=2 blocks inbound mail).
  HANDS-08: Audit credential mint/revoke; NEVER log the minted secret value.
  HANDS-09a: Kill-switch gate consulted BEFORE broker returns a credential.
"""

from __future__ import annotations

import inspect
import pytest

# ---------------------------------------------------------------------------
# RED import — this module does not exist yet (Plan 23-02 creates it).
# The collection error here is the EXPECTED red-state for Wave 0.
# ---------------------------------------------------------------------------
from services.cue.credential_broker import (  # noqa: E402
    mint_cue_credential,
    revoke_cue_credential,
    get_cue_credential,
)


# ---------------------------------------------------------------------------
# Test 1 — HANDS-05 / HANDS-05a: mint payload must set smtp_access=0 and active=1
# ---------------------------------------------------------------------------

class TestMintPayloadConstraints:
    """The credential broker must enforce no-send + no-inbound-block at mint time."""

    def test_mint_cue_credential_is_async(self):
        """mint_cue_credential must be an async function (httpx-based Mailcow call)."""
        assert inspect.iscoroutinefunction(mint_cue_credential), (
            "mint_cue_credential must be async (Mailcow Admin API calls use httpx.AsyncClient)"
        )

    def test_revoke_cue_credential_is_async(self):
        """revoke_cue_credential must be async."""
        assert inspect.iscoroutinefunction(revoke_cue_credential), (
            "revoke_cue_credential must be async"
        )

    def test_mint_signature_accepts_physician_id_and_mailbox(self):
        """mint_cue_credential must accept physician_id and mailbox_local_part."""
        sig = inspect.signature(mint_cue_credential)
        params = set(sig.parameters.keys())
        assert "physician_id" in params, (
            "mint_cue_credential must accept physician_id (used for audit row + app_name)"
        )
        assert "mailbox_local_part" in params, (
            "mint_cue_credential must accept mailbox_local_part (the Mailcow mailbox prefix)"
        )

    @pytest.mark.asyncio
    async def test_mint_payload_never_contains_active_2(self, monkeypatch):
        """HANDS-05a: the payload sent to Mailcow must NEVER contain active='2'.

        active='2' tells Postfix to block inbound delivery on the doctor's mailbox.
        The Cue credential must use active='1' (credential valid, inbound allowed).
        """
        captured_payloads: list[dict] = []

        async def mock_post(url: str, json: dict, **kwargs) -> object:
            captured_payloads.append(json)

            class MockResponse:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return [{"type": "success", "msg": ["cue-app-passwd-id"]}]

            return MockResponse()

        # Monkeypatch the httpx.AsyncClient so we capture the actual POST payload.
        # Plan 23-02 must accept this monkeypatch pattern — i.e. httpx calls must be
        # injectable (no hard-coded client at module level).
        import httpx

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url: str, **kwargs):
                return await mock_post(url, kwargs.get("json", {}))

            async def request(self, method: str, url: str, **kwargs):
                return await mock_post(url, kwargs.get("json", {}))

            async def get(self, url: str, **kwargs):
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {}
                return R()

        monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

        try:
            await mint_cue_credential(
                physician_id="test-physician-001",
                mailbox_local_part="testdoctor",
            )
        except Exception:
            # If mint raises (e.g. Supabase write), that is fine for this unit test —
            # we only care that the Mailcow payload was sent correctly.
            pass

        # At least one POST payload must have been captured.
        assert len(captured_payloads) >= 1, (
            "mint_cue_credential must POST to the Mailcow Admin API"
        )

        # Check ALL captured payloads — none may contain active="2".
        for payload in captured_payloads:
            assert payload.get("active") != "2", (
                "HANDS-05a VIOLATION: mint payload contains active='2' which blocks "
                "inbound mail on the physician's mailbox. Use active='1' instead."
            )
            assert payload.get("active") != 2, (
                "HANDS-05a VIOLATION: mint payload contains active=2 (integer). "
                "Mailcow API expects strings; either form is forbidden for Cue."
            )

    @pytest.mark.asyncio
    async def test_mint_payload_sets_smtp_access_zero(self, monkeypatch):
        """HANDS-05: the payload sent to Mailcow must set smtp_access='0'.

        smtp_access='0' tells Mailcow to reject any SMTP AUTH from this credential,
        preventing the doctor from sending mail via Cue's app-password.
        """
        captured_payloads: list[dict] = []

        import httpx

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url: str, **kwargs):
                captured_payloads.append(kwargs.get("json", {}))

                class R:
                    def raise_for_status(self): pass
                    def json(self): return [{"type": "success", "msg": ["id-abc"]}]

                return R()

            async def request(self, method: str, url: str, **kwargs):
                captured_payloads.append(kwargs.get("json", {}))

                class R:
                    def raise_for_status(self): pass
                    def json(self): return [{"type": "success", "msg": ["id-abc"]}]

                return R()

            async def get(self, url: str, **kwargs):
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {}

                return R()

        monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

        try:
            await mint_cue_credential(
                physician_id="test-physician-002",
                mailbox_local_part="testdoctor2",
            )
        except Exception:
            pass

        assert len(captured_payloads) >= 1, (
            "mint_cue_credential must POST to the Mailcow Admin API"
        )

        # At least one payload must explicitly set smtp_access to "0" or 0.
        smtp_values = [p.get("smtp_access") for p in captured_payloads]
        assert any(v in ("0", 0) for v in smtp_values), (
            "HANDS-05 VIOLATION: no captured Mailcow payload sets smtp_access='0'. "
            f"Observed smtp_access values: {smtp_values}"
        )


# ---------------------------------------------------------------------------
# Test 2 — HANDS-08: audit rows must NOT contain the app-passwd secret
# ---------------------------------------------------------------------------

class TestAuditNeverLogsSecret:
    """Credential mint/revoke must write a workspace_audit_log row whose detail
    dict NEVER contains the app-passwd secret value.
    """

    @pytest.mark.asyncio
    async def test_mint_audit_row_excludes_password(self, monkeypatch):
        """HANDS-08: the workspace_audit_log detail must not contain any 'password'
        or 'secret' key, and must not contain the app-passwd secret string.
        """
        audit_rows: list[dict] = []

        # Minimal mock for Supabase table().insert().execute()
        class MockTable:
            def __init__(self, name):
                self._name = name

            def insert(self, row: dict):
                if self._name == "workspace_audit_log":
                    audit_rows.append(row)

                class Chain:
                    def execute(self_inner):
                        return None

                return Chain()

            def select(self, *a, **kw):
                class Chain:
                    def eq(self, *a, **kw): return self
                    def execute(self_inner):
                        return type("R", (), {"data": [], "error": None})()
                return Chain()

            def update(self, *a, **kw):
                class Chain:
                    def eq(self, *a, **kw): return self
                    def execute(self): return None
                return Chain()

        class MockSupabase:
            def table(self, name: str):
                return MockTable(name)

        # Stub out httpx too so the Mailcow call doesn't fail
        import httpx

        FAKE_SECRET = "super-secret-app-passwd-value-should-never-be-logged"

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url: str, **kwargs):
                class R:
                    def raise_for_status(self): pass

                    def json(self):
                        return [{"type": "success", "msg": [f"id-xyz|{FAKE_SECRET}"]}]

                return R()

            async def request(self, method: str, url: str, **kwargs):
                class R:
                    def raise_for_status(self): pass

                    def json(self):
                        return [{"type": "success", "msg": [f"id-xyz|{FAKE_SECRET}"]}]

                return R()

            async def get(self, url: str, **kwargs):
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {}

                return R()

        monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

        # Inject the mock Supabase into the broker module
        import services.cue.credential_broker as broker_mod
        if hasattr(broker_mod, "get_supabase"):
            monkeypatch.setattr(broker_mod, "get_supabase", lambda: MockSupabase())
        elif hasattr(broker_mod, "_get_db"):
            monkeypatch.setattr(broker_mod, "_get_db", lambda: MockSupabase())

        try:
            await mint_cue_credential(
                physician_id="test-physician-003",
                mailbox_local_part="testdoctor3",
            )
        except Exception:
            pass

        # An audit row must have been written
        assert len(audit_rows) >= 1, (
            "HANDS-08: mint_cue_credential must write a workspace_audit_log row"
        )

        for row in audit_rows:
            detail = row.get("detail", {}) or {}
            row_str = str(row)
            # No key named 'password', 'secret', 'passwd', 'app_passwd_secret'
            for forbidden_key in ("password", "secret", "passwd", "app_passwd_secret"):
                assert forbidden_key not in detail, (
                    f"HANDS-08 VIOLATION: audit row detail contains forbidden key '{forbidden_key}'"
                )
            # The secret value itself must never appear in the serialized row
            assert FAKE_SECRET not in row_str, (
                "HANDS-08 VIOLATION: the minted app-passwd secret value appears in the audit row"
            )


# ---------------------------------------------------------------------------
# Test 3 — HANDS-09a: kill-switch gate must be consulted before credential issue
# ---------------------------------------------------------------------------

class TestKillSwitchGateConsulted:
    """The kill-switch gate must run BEFORE the broker hands out any credential.

    When the kill switch is tripped, get_cue_credential must raise or return
    an error sentinel — it must NOT proceed to mint or return a credential.
    """

    @pytest.mark.asyncio
    async def test_kill_switch_tripped_blocks_credential_issue(self, monkeypatch):
        """HANDS-09a: if the kill switch is tripped, get_cue_credential must NOT
        return a credential, and must NOT call the Mailcow mint endpoint.
        """
        mint_calls: list = []

        import httpx

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url: str, **kwargs):
                if "app-passwd" in url:
                    mint_calls.append(url)

                class R:
                    def raise_for_status(self): pass
                    def json(self): return [{"type": "success", "msg": ["id-blocked"]}]

                return R()

            async def request(self, method: str, url: str, **kwargs):
                return await self.post(url, **kwargs)

            async def get(self, url: str, **kwargs):
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {}

                return R()

        monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

        # Simulate a tripped kill switch via the broker's internal check mechanism.
        # The broker must expose a way for tests to inject a kill-switch state.
        # Acceptable patterns (Plan 23-02 chooses one):
        #   A. broker imports _check_kill_switch() and the test monkeypatches it.
        #   B. broker accepts a kill_switch_fn parameter.
        #   C. broker reads from Supabase and the test patches the Supabase call.
        import services.cue.credential_broker as broker_mod

        # Try pattern A (most likely given Phase-22 precedent in cue_routes.py)
        if hasattr(broker_mod, "_check_kill_switch"):
            async def tripped_kill_switch(*args, **kwargs):
                return "tripped"

            monkeypatch.setattr(broker_mod, "_check_kill_switch", tripped_kill_switch)
        elif hasattr(broker_mod, "check_kill_switch"):
            async def tripped_kill_switch(*args, **kwargs):
                return "tripped"

            monkeypatch.setattr(broker_mod, "check_kill_switch", tripped_kill_switch)
        else:
            pytest.skip(
                "HANDS-09a: broker module has no _check_kill_switch / check_kill_switch "
                "attribute to monkeypatch — Plan 23-02 must expose this for testability."
            )

        with pytest.raises(Exception) as exc_info:
            await get_cue_credential(
                physician_id="test-physician-004",
                mailbox_local_part="testdoctor4",
            )

        # The exception must indicate kill-switch / unavailable, not a Mailcow error
        exc_str = str(exc_info.value).lower()
        assert any(
            kw in exc_str
            for kw in ("kill", "switch", "unavailable", "503", "tripped", "disabled")
        ), (
            f"HANDS-09a: expected kill-switch error, got: {exc_info.value!r}"
        )

        # The Mailcow mint endpoint must NOT have been called
        assert len(mint_calls) == 0, (
            "HANDS-09a VIOLATION: Mailcow mint endpoint was called despite tripped kill switch. "
            "The gate must run BEFORE the broker issues a credential."
        )
