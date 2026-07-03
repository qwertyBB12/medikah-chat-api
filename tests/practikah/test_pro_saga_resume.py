"""
Step-replay slice (2026-07-03): the pro saga is now resumable — hydrated from
practikah_provisioning_log (no schema migration), guarded so pre-POR/refunded
runs are never replayed, with idempotent runners. The startup rollback sweeper
EXCLUDES pro runs (P0: a post-POR run in partial_finish_later matches the
orphan criteria while waiting on DNS — sweeping it would tear down a domain
the doctor owns). Payment-recovery auto-resume is env-gated, default OFF.
"""
import asyncio
from typing import Any

import pytest

import services.practikah.audit as audit_module
import services.practikah.orchestrator as orchestrator
import services.practikah.pro_saga as pro_saga
import services.practikah.stripe_webhook as stripe_webhook
from services.practikah.mailbox_provisioner import mailbox_provisioner
from services.practikah.pro_saga import (
    PRO_SAGA_STEPS,
    ProSagaContext,
    _execute_from,
    _load_resume_plan,
    resume_pro_run,
)


# ---------------------------------------------------------------------------
# Fake Supabase client (extends the test_dunning_recovery_reconcile idiom)
# ---------------------------------------------------------------------------

class _Table:
    def __init__(self, db: "_FakeDB", name: str):
        self._db, self._name = db, name

    # chainable query builders — record nothing, return self
    def select(self, *_): return self
    def eq(self, *_): return self
    def in_(self, *_): return self
    def limit(self, *_): return self
    def order(self, *_, **__): return self

    def update(self, payload):
        self._db.updates.append((self._name, payload))
        return self

    def insert(self, payload):
        self._db.inserts.append((self._name, payload))
        return self

    def execute(self):
        class _Res:
            data = self._db.rows.get(self._name, [])
        return _Res()


class _FakeDB:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]):
        self.rows = rows
        self.updates: list[tuple[str, dict]] = []
        self.inserts: list[tuple[str, dict]] = []

    def table(self, name: str) -> _Table:
        return _Table(self, name)


@pytest.fixture(autouse=True)
def _no_real_supabase(monkeypatch):
    """The audit writer resolves its own client — never let a configured shell
    env leak test audit rows into prod."""
    monkeypatch.setattr(audit_module, "get_supabase", lambda: None)


def _run_row(**overrides):
    row = {
        "run_id": "r1",
        "physician_id": "phys-1",
        "saga_type": "pro_upgrade",
        "status": "partial_finish_later",
        "domain_name": "drx.mx",
        "stripe_session_id": "cs_1",
        "retry_count": 0,
    }
    row.update(overrides)
    return row


def _succeeded(step, detail=None):
    return {"step_name": step, "detail": detail or {}, "recorded_at": "2026-07-03T00:00:00Z"}


# ---------------------------------------------------------------------------
# Hydration
# ---------------------------------------------------------------------------

def test_load_resume_plan_hydrates_prefix_and_state():
    db = _FakeDB({
        "provisioning_runs": [_run_row()],
        "practikah_provisioning_log": [
            _succeeded("pro.charge_confirmed", {"stripe_session_id": "cs_1"}),
            _succeeded("pro.register_domain", {"domain": "drx.mx"}),
            _succeeded("pro.write_dns", {"zone_id": "Z1", "record_ids": ["a", "b"]}),
        ],
        "physician_workspace_accounts": [
            {"physician_id": "phys-1", "pro_local_part": "dr-x", "id": "wa1"},
        ],
        "physicians": [{"full_name": "Dr X", "email": "x@drx.mx"}],
    })
    plan, reason = asyncio.run(_load_resume_plan(db, "r1", spawn_finish_later=False))
    assert reason == "" and plan is not None
    assert plan.start_index == 3
    assert plan.ctx.completed == PRO_SAGA_STEPS[:3]
    assert plan.ctx.state["cf_zone_id"] == "Z1"
    assert plan.ctx.state["written_record_ids"] == ["a", "b"]
    assert plan.ctx.local_part == "dr-x"
    # mailbox step still pending → fresh ephemeral password minted
    assert plan.ctx.mailbox_password
    assert plan.ctx.stripe_session_id == "cs_1"


def test_load_resume_plan_hydrates_hostname_and_domain_ids():
    db = _FakeDB({
        "provisioning_runs": [_run_row()],
        "practikah_provisioning_log": [
            _succeeded(s) for s in PRO_SAGA_STEPS[:5]
        ] + [
            _succeeded("pro.attach_saas_hostname", {"resource_id": "ch-9"}),
            _succeeded("pro.migrate_theme", {"domain_id": "dom-7"}),
        ],
        "physician_workspace_accounts": [
            {"physician_id": "phys-1", "pro_local_part": "dr-x"},
        ],
        "physicians": [],
    })
    plan, reason = asyncio.run(_load_resume_plan(db, "r1", spawn_finish_later=False))
    assert reason == "" and plan is not None
    assert plan.start_index == 7  # only pro.verify_live left
    assert plan.ctx.state["saas_hostname_id"] == "ch-9"
    assert plan.ctx.state["migrated_domain_id"] == "dom-7"
    # mailbox already provisioned → no fresh password minted
    assert plan.ctx.mailbox_password == ""


# ---------------------------------------------------------------------------
# Refusal guards
# ---------------------------------------------------------------------------

def test_resume_refuses_pre_por_run():
    db = _FakeDB({
        "provisioning_runs": [_run_row(status="failed")],
        "practikah_provisioning_log": [
            _succeeded("pro.charge_confirmed"),
            # no pro.register_domain succeeded row → pre-POR, was refunded
        ],
    })
    result = asyncio.run(resume_pro_run(db, "r1", trigger="test"))
    assert result == {"resumed": False, "reason": "pre_por_not_resumable"}


def test_resume_refuses_non_resumable_status_and_wrong_saga():
    db = _FakeDB({"provisioning_runs": [_run_row(status="succeeded")]})
    result = asyncio.run(resume_pro_run(db, "r1", trigger="test"))
    assert result["reason"] == "status_succeeded_not_resumable"

    db = _FakeDB({"provisioning_runs": [_run_row(saga_type="pro_downgrade")]})
    result = asyncio.run(resume_pro_run(db, "r1", trigger="test"))
    assert result["reason"] == "not_pro_saga"

    db = _FakeDB({"provisioning_runs": []})
    result = asyncio.run(resume_pro_run(db, "r1", trigger="test"))
    assert result["reason"] == "run_not_found"


# ---------------------------------------------------------------------------
# Resume executes from the correct index
# ---------------------------------------------------------------------------

def test_resume_starts_at_correct_index(monkeypatch):
    db = _FakeDB({
        "provisioning_runs": [_run_row()],
        "practikah_provisioning_log": [
            _succeeded(s) for s in PRO_SAGA_STEPS[:3]
        ],
        "physician_workspace_accounts": [
            {"physician_id": "phys-1", "pro_local_part": "dr-x"},
        ],
        "physicians": [],
    })

    ran: list[str] = []

    def _recorder(name):
        async def _run(ctx):
            ran.append(name)
        return _run

    for step in PRO_SAGA_STEPS[3:]:
        monkeypatch.setitem(pro_saga.STEP_RUNNERS, step, _recorder(step))

    emails: list[str] = []

    async def _fake_live_email(**kwargs):
        emails.append(kwargs["run_id"])
    monkeypatch.setattr(pro_saga, "_trigger_pro_live_email", _fake_live_email)

    result = asyncio.run(resume_pro_run(db, "r1", trigger="manual_ops"))
    assert result["resumed"] is True
    assert result["start_step"] == "pro.provision_mailcow_domain"
    assert result["final_status"] == "succeeded"
    assert ran == PRO_SAGA_STEPS[3:]
    # run flipped to running, then succeeded
    statuses = [p.get("status") for t, p in db.updates if t == "provisioning_runs" and "status" in p]
    assert statuses[0] == "running" and statuses[-1] == "succeeded"
    # audit trail: resumed + succeeded
    actions = [p["action"] for t, p in db.inserts if t == "workspace_audit_log"]
    assert "pro.upgrade_resumed" in actions and "pro.upgrade_succeeded" in actions
    assert emails == ["r1"]


# ---------------------------------------------------------------------------
# Mailcow check-first idempotency (GET-before-POST short-circuit)
# ---------------------------------------------------------------------------

def test_mailcow_add_domain_short_circuits_when_domain_exists(monkeypatch):
    async def _existing(domain):
        return {"domain_name": domain, "active": "1"}

    async def _boom(*args, **kwargs):
        raise AssertionError("write must not run when the domain already exists")

    monkeypatch.setattr(mailbox_provisioner, "_get_domain", _existing)
    monkeypatch.setattr(mailbox_provisioner, "_request_write", _boom)

    result = asyncio.run(mailbox_provisioner.do_add_domain("drx.mx", run_id="r1"))
    assert result.success is True
    assert result.resource_id == mailbox_provisioner._maybe_sandbox_prefix("drx.mx")


# ---------------------------------------------------------------------------
# verify_live failure → partial_finish_later (post-POR semantics)
# ---------------------------------------------------------------------------

def test_verify_live_failure_lands_in_partial_finish_later(monkeypatch):
    monkeypatch.setattr(pro_saga, "_SANDBOX_MODE", False)
    monkeypatch.setattr(pro_saga, "_VERIFY_LIVE_RETRY_DELAY_SEC", 0)

    async def _dead(domain):
        raise RuntimeError("TLS handshake failed")
    monkeypatch.setattr(pro_saga, "_verify_live_once", _dead)

    db = _FakeDB({"provisioning_runs": [_run_row()]})
    ctx = ProSagaContext(
        db=db, physician_id="phys-1", run_id="r1", domain="drx.mx",
        tld_class="", cadence="", local_part="dr-x", mailbox_password="",
        physician_registrant={}, stripe_session_id="cs_1",
        log=audit_module.ProvisioningLogWriter("phys-1", "r1"),
        completed=list(PRO_SAGA_STEPS[:7]),
        state={}, spawn_finish_later=False,
    )
    ok = asyncio.run(_execute_from(ctx, 7))
    assert ok is False
    stall = [p for t, p in db.updates
             if t == "provisioning_runs" and p.get("status") == "partial_finish_later"]
    assert stall and stall[0]["error"]["step"] == "pro.verify_live"
    # loop-owned counter: a failed RESUME must not reset the retry budget
    assert "retry_count" not in stall[0]


def test_verify_live_accepts_sub_500_status(monkeypatch):
    monkeypatch.setattr(pro_saga, "_SANDBOX_MODE", False)
    monkeypatch.setattr(pro_saga, "_VERIFY_LIVE_RETRY_DELAY_SEC", 0)

    async def _up(domain):
        return 404  # DNS + TLS + routing all resolve to us — that's the promise
    monkeypatch.setattr(pro_saga, "_verify_live_once", _up)

    ctx = ProSagaContext(
        db=_FakeDB({}), physician_id="phys-1", run_id="r1", domain="drx.mx",
        tld_class="", cadence="", local_part="dr-x", mailbox_password="",
        physician_registrant={}, stripe_session_id="cs_1",
        log=audit_module.ProvisioningLogWriter("phys-1", "r1"),
        completed=list(PRO_SAGA_STEPS[:7]),
        state={}, spawn_finish_later=False,
    )
    asyncio.run(pro_saga._step_verify_live(ctx))  # must not raise


# ---------------------------------------------------------------------------
# Payment-recovery auto-resume — env-gated, default OFF
# ---------------------------------------------------------------------------

def test_auto_resume_default_off(monkeypatch):
    monkeypatch.delenv("PRO_AUTO_RESUME_ON_RECOVERY", raising=False)
    called: list[str] = []

    async def _fake_resume(db, run_id, **kwargs):
        called.append(run_id)
    monkeypatch.setattr(pro_saga, "resume_pro_run", _fake_resume)

    async def _main():
        spawned = stripe_webhook._spawn_auto_resume(
            _FakeDB({}), "phys-1", [{"run_id": "r1"}]
        )
        await asyncio.sleep(0)
        return spawned

    assert asyncio.run(_main()) == []
    assert called == []


def test_auto_resume_spawns_when_armed(monkeypatch):
    monkeypatch.setenv("PRO_AUTO_RESUME_ON_RECOVERY", "true")
    called: list[tuple[str, str]] = []

    async def _fake_resume(db, run_id, **kwargs):
        called.append((run_id, kwargs.get("trigger")))
    monkeypatch.setattr(pro_saga, "resume_pro_run", _fake_resume)

    async def _main():
        spawned = stripe_webhook._spawn_auto_resume(
            _FakeDB({}), "phys-1", [{"run_id": "r1"}, {"run_id": "r2"}]
        )
        await asyncio.sleep(0)
        return spawned

    assert asyncio.run(_main()) == ["r1", "r2"]
    assert called == [("r1", "payment_recovery"), ("r2", "payment_recovery")]


# ---------------------------------------------------------------------------
# Orphan-sweeper exclusion (P0): pro runs never reach rollback
# ---------------------------------------------------------------------------

def test_sweeper_excludes_pro_runs(monkeypatch):
    async def _orphans():
        return [("phys-1", "r-pro"), ("phys-2", "r-basic")]
    monkeypatch.setattr(
        orchestrator.ProvisioningLogWriter, "list_orphan_runs", _orphans
    )
    # provisioning_runs only ever holds pro_upgrade rows (021 CHECK) — the
    # fake returns r-pro for the saga_type lookup.
    monkeypatch.setattr(
        orchestrator, "get_supabase",
        lambda: _FakeDB({"provisioning_runs": [{"run_id": "r-pro"}]}),
    )

    rolled: list[tuple[str, str]] = []

    async def _fake_rollback(*, physician_id, run_id):
        rolled.append((physician_id, run_id))
    monkeypatch.setattr(orchestrator, "run_rollback", _fake_rollback)

    cleaned = asyncio.run(orchestrator.resume_orphan_runs())
    assert cleaned == 1
    assert rolled == [("phys-2", "r-basic")]


def test_sweeper_fails_closed_without_db(monkeypatch):
    async def _orphans():
        return [("phys-1", "r-pro"), ("phys-2", "r-basic")]
    monkeypatch.setattr(
        orchestrator.ProvisioningLogWriter, "list_orphan_runs", _orphans
    )
    monkeypatch.setattr(orchestrator, "get_supabase", lambda: None)

    rolled: list[tuple[str, str]] = []

    async def _fake_rollback(*, physician_id, run_id):
        rolled.append((physician_id, run_id))
    monkeypatch.setattr(orchestrator, "run_rollback", _fake_rollback)

    cleaned = asyncio.run(orchestrator.resume_orphan_runs())
    assert cleaned == 0
    assert rolled == []


# ---------------------------------------------------------------------------
# Startup re-arm
# ---------------------------------------------------------------------------

def test_rearm_skips_exhausted_and_arms_fresh(monkeypatch):
    armed: list[tuple[str, int]] = []

    async def _fake_loop(*, db, physician_id, run_id, domain, failed_step, start_attempt=0):
        armed.append((run_id, start_attempt))
    monkeypatch.setattr(pro_saga, "_finish_later_retry_loop", _fake_loop)

    db = _FakeDB({
        "provisioning_runs": [
            {"run_id": "r-fresh", "physician_id": "p1", "domain_name": "a.mx",
             "current_step": "pro.write_dns", "retry_count": 4},
            {"run_id": "r-done", "physician_id": "p2", "domain_name": "b.mx",
             "current_step": "pro.verify_live",
             "retry_count": pro_saga._FINISH_LATER_MAX_ATTEMPTS},
        ],
    })

    async def _main():
        n = await pro_saga.rearm_finish_later_runs(db)
        await asyncio.sleep(0)
        return n

    assert asyncio.run(_main()) == 1
    assert armed == [("r-fresh", 4)]
    actions = [p["action"] for t, p in db.inserts if t == "workspace_audit_log"]
    assert actions == ["pro.finish_later_rearmed"]
