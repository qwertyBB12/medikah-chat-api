"""
Pro audit P0 (2026-07-02): a payment that recovers after dunning flipped the
subscription active while a provisioning run stranded in partial_finish_later
stayed stranded — silently. The webhook now surfaces stranded runs (audit row +
alert + handler return) WITHOUT auto-resuming (the saga's steps are not yet
independently replayable; an automated re-run could refund/undo a live domain).
"""
import asyncio
from typing import Any

from services.practikah.stripe_webhook import (
    _find_stuck_pro_runs,
    _on_invoice_payment_succeeded,
)


class _Table:
    def __init__(self, db: "_FakeDB", name: str):
        self._db, self._name = db, name

    # chainable query builders — record nothing, return self
    def select(self, *_): return self
    def eq(self, *_): return self
    def in_(self, *_): return self
    def limit(self, *_): return self

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


def _invoice_event(subscription="sub_1", customer="cus_1"):
    return {
        "data": {
            "object": {
                "subscription": subscription,
                "customer": customer,
                "lines": {"data": [{"period": {"end": 1751500800}}]},
            }
        }
    }


def test_find_stuck_pro_runs_returns_rows():
    db = _FakeDB({
        "provisioning_runs": [
            {"run_id": "r1", "status": "partial_finish_later",
             "current_step": "pro.issue_ssl", "domain_name": "drx.mx", "error": {}},
        ],
    })
    runs = _find_stuck_pro_runs(db, "phys-1")
    assert [r["run_id"] for r in runs] == ["r1"]


def test_payment_succeeded_surfaces_stranded_run_and_audits():
    db = _FakeDB({
        "physician_workspace_accounts": [
            {"physician_id": "phys-1", "stripe_subscription_id": "sub_1"},
        ],
        "provisioning_runs": [
            {"run_id": "r1", "status": "partial_finish_later",
             "current_step": "pro.issue_ssl", "domain_name": "drx.mx", "error": {}},
        ],
    })
    result = asyncio.run(_on_invoice_payment_succeeded(_invoice_event(), db))
    assert result["stuck_pro_runs"] == ["r1"]
    # subscription flip still applied
    assert any(t == "physician_workspace_accounts" and p.get("subscription_status") == "active"
               for t, p in db.updates)
    # audit row written, loud action name
    audit = [p for t, p in db.inserts if t == "workspace_audit_log"]
    assert audit and audit[0]["action"] == "pro.stranded_run_on_payment_recovery"
    # and CRITICALLY: no mutation of the stranded run itself (no auto-resume)
    assert not any(t == "provisioning_runs" for t, _ in db.updates)


def test_payment_succeeded_clean_recovery_reports_no_stuck_runs():
    db = _FakeDB({
        "physician_workspace_accounts": [
            {"physician_id": "phys-1", "stripe_subscription_id": "sub_1"},
        ],
        "provisioning_runs": [],
    })
    result = asyncio.run(_on_invoice_payment_succeeded(_invoice_event(), db))
    assert result["stuck_pro_runs"] == []
    assert not any(t == "workspace_audit_log" for t, _ in db.inserts)
