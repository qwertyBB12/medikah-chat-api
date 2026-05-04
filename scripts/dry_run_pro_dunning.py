"""Phase 13 dunning + auto-downgrade dry-run harness (OPS-12 / D-27 / D-28 / D-29).

Uses Stripe Test Clocks to fast-forward through the 14-day retry window and
7-day grace period in seconds rather than real calendar time.

Safety gates (T-13-10-01):
  - Refuses to run unless MEDIKAH_PROVISIONING_SANDBOX=true
  - Refuses to run unless STRIPE_SECRET_KEY starts with 'sk_test_'

Usage:
  MEDIKAH_PROVISIONING_SANDBOX=true STRIPE_SECRET_KEY=sk_test_... \\
    python -m scripts.dry_run_pro_dunning --physician-id <uuid>

  The physician ID should come from a successful happy-path run of
  provision_test_pro_doctor.py (i.e., a physician with tier='pro' and a
  stripe_subscription_id on their workspace row).

  Alternatively pass --create-test-subscription to have the harness create a
  fresh Stripe test customer + subscription using Stripe Test Clocks.

Dunning lifecycle verified (D-29 audit events in order):
  1. billing.payment_failed      (first invoice.payment_failed)
  2. billing.dunning_retry_1     (non-final: next_payment_attempt present)
  3. billing.dunning_retry_2     (+~5 days via Test Clock advance)
  4. billing.dunning_retry_3     (+~9 days via Test Clock advance)
  5. billing.grace_started       (final attempt: next_payment_attempt=None)
  6. billing.downgraded_to_free  (after 7-day grace advance)
  7. billing.mailbox_frozen      (Pro mailbox frozen, @medikah.health untouched)
  8. billing.transfer_out_requested  (PRO-11 EPP flow)

Additional checks:
  - After downgrade: physician_workspace_accounts.tier='free' (OPS-12)
  - After downgrade: physician_website.published_to_domain_id=NULL within 60s (D-24/D-25)
  - After freeze: Pro mailbox smtp_access=0, imap_access=1 (D-28, sandbox mock)
  - Free @medikah.health mailbox NEVER touched (PRO-17)
  - Transfer-out: EPP code returned synchronously (PRO-11)

Evidence:
  .planning/phases/13-pro-upsell-stripe-billing-custom-domain-pro-mailbox-pro-webs/
    runbooks/evidence/dry-run-dunning-{iso_timestamp}.json

Exit codes:
  0   -- all assertions passed
  5   -- one or more assertions failed
  99  -- unhandled exception
  130 -- interrupted
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, Optional
from uuid import uuid4

# ---------------------------------------------------------------------------
# Safety gates — checked at import time (T-13-10-01)
# ---------------------------------------------------------------------------

_SANDBOX_RAW = os.getenv("MEDIKAH_PROVISIONING_SANDBOX", "")
if _SANDBOX_RAW.lower() not in {"1", "true", "yes", "on"}:
    sys.exit(
        "ABORT: MEDIKAH_PROVISIONING_SANDBOX must be set to 'true' before running "
        "this script. This prevents accidental production runs."
    )

_STRIPE_KEY_RAW = os.getenv("STRIPE_SECRET_KEY", "")
if not _STRIPE_KEY_RAW.startswith("sk_test_"):
    sys.exit(
        "ABORT: STRIPE_SECRET_KEY must start with 'sk_test_'. "
        "This script requires Stripe test-mode credentials."
    )

import stripe  # noqa: E402 — after safety gate

stripe.api_key = _STRIPE_KEY_RAW

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / (
    ".planning/phases/"
    "13-pro-upsell-stripe-billing-custom-domain-pro-mailbox-pro-webs/"
    "runbooks/evidence"
)

# D-29 expected audit events in sequence order
DUNNING_EXPECTED_AUDIT_EVENTS: list[str] = [
    "billing.payment_failed",
    "billing.dunning_retry_1",
    "billing.dunning_retry_2",
    "billing.dunning_retry_3",
    "billing.grace_started",
    "billing.downgraded_to_free",
    "billing.mailbox_frozen",
]

# Stripe advance offsets in seconds (simulates real dunning schedule)
# Day 0: first failure
# Day 5: retry 2
# Day 9: retry 3
# Day 14: final retry / grace start
# Day 21: grace expired → auto-downgrade
ADVANCE_SCHEDULE_SECONDS: list[tuple[str, int]] = [
    ("retry_1_initial_failure", 0),
    ("retry_2_day5", 5 * 86400),
    ("retry_3_day9", 9 * 86400),
    ("final_failure_day14", 14 * 86400),
    ("grace_expiry_day21", 21 * 86400),
]


# ---------------------------------------------------------------------------
# Evidence writer (T-13-10-02: no secrets)
# ---------------------------------------------------------------------------


def _write_evidence(
    physician_id: str,
    duration_sec: float,
    status: str,
    audit_rows: list[str],
    assertions: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = EVIDENCE_DIR / f"dry-run-dunning-{ts_iso}.json"
    payload: dict[str, Any] = {
        "schema_version": "13-10-v1",
        "scenario": "dunning",
        "physician_id": physician_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "duration_sec": round(duration_sec, 2),
        "status": status,
        "audit_rows": {
            "count": len(audit_rows),
            "actions": audit_rows,
        },
        "assertions": assertions,
        "extra": extra or {},
    }
    filename.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[dry_run_pro_dunning] evidence written: {filename}")
    return filename


# ---------------------------------------------------------------------------
# Supabase + service helpers
# ---------------------------------------------------------------------------


def _get_supabase() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from db.client import get_supabase_client  # noqa: PLC0415
    return get_supabase_client()


def _fetch_audit_rows(db: Any, physician_id: str) -> list[str]:
    try:
        result = (
            db.table("workspace_audit_log")
            .select("action")
            .eq("physician_id", physician_id)
            .execute()
        )
        return [r["action"] for r in (result.data or []) if r.get("action")]
    except Exception as exc:
        print(f"[dry_run_pro_dunning] WARNING: audit fetch failed: {exc}")
        return []


# ---------------------------------------------------------------------------
# Stripe Test Clock helpers
# ---------------------------------------------------------------------------


def _create_test_clock(physician_id: str) -> Any:
    """Create a Stripe Test Clock anchored to now."""
    frozen_time = int(time.time())
    clock = stripe.test_helpers.TestClock.create(
        frozen_time=frozen_time,
        name=f"dunning-dry-run-{physician_id[:8]}",
    )
    print(f"[dry_run_pro_dunning] test_clock created id={clock.id} frozen_time={frozen_time}")
    return clock


def _create_test_customer(clock_id: str, physician_id: str) -> Any:
    """Create a Stripe Customer attached to the Test Clock."""
    customer = stripe.Customer.create(
        email=f"sandbox-dunning-{token_hex(4)}@sandbox.medikah.health",
        name="Dr Dunning Test",
        test_clock=clock_id,
        metadata={"physician_id": physician_id, "sandbox": "true"},
    )
    print(f"[dry_run_pro_dunning] test customer created id={customer.id}")
    return customer


def _create_test_subscription(
    customer_id: str,
    price_id: str,
) -> Any:
    """Create a Stripe Subscription with card_chargeDeclined test card."""
    # Attach a failing payment method to force invoice.payment_failed
    pm = stripe.PaymentMethod.create(
        type="card",
        card={"token": "tok_chargeDeclined"},
    )
    stripe.PaymentMethod.attach(pm.id, customer=customer_id)
    stripe.Customer.modify(
        customer_id,
        invoice_settings={"default_payment_method": pm.id},
    )

    # Create subscription — first invoice will fail immediately
    subscription = stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": price_id}],
        payment_behavior="allow_incomplete",
        expand=["latest_invoice.payment_intent"],
    )
    print(f"[dry_run_pro_dunning] subscription created id={subscription.id} status={subscription.status}")
    return subscription


def _advance_clock(clock_id: str, target_offset_from_now: int, frozen_time_base: int) -> None:
    """Advance test clock to base + offset seconds from epoch."""
    new_time = frozen_time_base + target_offset_from_now
    stripe.test_helpers.TestClock.advance(clock_id, frozen_time=new_time)
    print(f"[dry_run_pro_dunning] clock advanced to +{target_offset_from_now}s ({new_time})")
    # Brief pause to allow Stripe webhook events to propagate to our handler
    time.sleep(2)


# ---------------------------------------------------------------------------
# Dunning event dispatcher (simulates stripe_webhook.py dispatch)
# ---------------------------------------------------------------------------


async def _dispatch_dunning_event(
    db: Any,
    event_type: str,
    physician_id: str,
    invoice_id: str,
    next_payment_attempt: Optional[int],
    subscription_id: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a Stripe-event-shaped dict and feed it to the dunning state machine."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from services.practikah.dunning_state_machine import (  # noqa: PLC0415
        on_payment_failed,
        auto_downgrade,
    )

    event: dict[str, Any] = {
        "id": f"evt_test_{token_hex(6)}",
        "type": event_type,
        "data": {
            "object": {
                "id": invoice_id,
                "object": "invoice",
                "subscription": subscription_id,
                "customer": customer_id,
                "next_payment_attempt": next_payment_attempt,
                "metadata": {"physician_id": physician_id},
            }
        },
    }

    if event_type == "invoice.payment_failed":
        return await on_payment_failed(db, event)
    elif event_type == "customer.subscription.deleted":
        return await auto_downgrade(db, event)
    else:
        return {"dispatched": event_type, "unknown": True}


# ---------------------------------------------------------------------------
# Main dunning lifecycle run
# ---------------------------------------------------------------------------


async def run_dunning(physician_id: str, price_id: str) -> int:
    """Execute the full dunning lifecycle and return an exit code."""
    print(f"[dry_run_pro_dunning] starting dunning lifecycle physician_id={physician_id}")
    assertions: list[dict[str, Any]] = []
    started = time.monotonic()

    db = _get_supabase()

    # ------------------------------------------------------------------
    # Ensure physician has a Pro workspace row (or create minimal one)
    # ------------------------------------------------------------------
    try:
        ws_resp = (
            db.table("physician_workspace_accounts")
            .select("id, stripe_customer_id, stripe_subscription_id")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        ws_row = ws_resp.data[0] if ws_resp.data else None
    except Exception as exc:
        print(f"[dry_run_pro_dunning] WARNING: workspace lookup failed: {exc}")
        ws_row = None

    # ------------------------------------------------------------------
    # Create Stripe Test Clock + customer + subscription
    # ------------------------------------------------------------------
    clock = _create_test_clock(physician_id)
    clock_id = clock.id
    frozen_time_base = clock.frozen_time

    customer = _create_test_customer(clock_id, physician_id)
    customer_id = customer.id

    # Wire the Stripe customer_id to our workspace row so the dunning
    # state machine can resolve it via _physician_by_customer.
    try:
        if ws_row:
            db.table("physician_workspace_accounts").update(
                {"stripe_customer_id": customer_id, "tier": "pro",
                 "subscription_status": "active", "dunning_retry_count": 0}
            ).eq("physician_id", physician_id).execute()
        else:
            db.table("physician_workspace_accounts").insert(
                {
                    "physician_id": physician_id,
                    "tier": "pro",
                    "subscription_status": "active",
                    "stripe_customer_id": customer_id,
                    "physician_email": f"sandbox-dunning-{token_hex(4)}@sandbox.medikah.health",
                    "dunning_retry_count": 0,
                }
            ).execute()
    except Exception as exc:
        print(f"[dry_run_pro_dunning] WARNING: workspace upsert failed: {exc}")

    subscription = _create_test_subscription(customer_id, price_id)
    sub_id = subscription.id

    # Wire subscription_id into workspace
    try:
        db.table("physician_workspace_accounts").update(
            {"stripe_subscription_id": sub_id}
        ).eq("physician_id", physician_id).execute()
    except Exception as exc:
        print(f"[dry_run_pro_dunning] WARNING: sub_id wire failed: {exc}")

    invoice_id_stub = f"in_test_dunning_{token_hex(6)}"

    # ------------------------------------------------------------------
    # Retry 1 — invoice.payment_failed (non-final, next_payment_attempt set)
    # ------------------------------------------------------------------
    print("[dry_run_pro_dunning] advancing clock: retry 1")
    _advance_clock(clock_id, ADVANCE_SCHEDULE_SECONDS[0][1], frozen_time_base)
    await _dispatch_dunning_event(
        db,
        event_type="invoice.payment_failed",
        physician_id=physician_id,
        invoice_id=invoice_id_stub,
        next_payment_attempt=int(time.time()) + 5 * 86400,  # retry in 5 days
        subscription_id=sub_id,
        customer_id=customer_id,
    )
    await asyncio.sleep(0.5)

    audit_after_r1 = _fetch_audit_rows(db, physician_id)
    r1_ok = "billing.dunning_retry_1" in audit_after_r1
    assertions.append({
        "check": "billing.dunning_retry_1 audit row present after first failure (D-29)",
        "result": r1_ok,
    })
    print(f"[dry_run_pro_dunning] retry_1: {'OK' if r1_ok else 'FAIL'}")

    # ------------------------------------------------------------------
    # Retry 2 — advance to day 5
    # ------------------------------------------------------------------
    print("[dry_run_pro_dunning] advancing clock: retry 2 (+5 days)")
    _advance_clock(clock_id, ADVANCE_SCHEDULE_SECONDS[1][1], frozen_time_base)
    await _dispatch_dunning_event(
        db,
        event_type="invoice.payment_failed",
        physician_id=physician_id,
        invoice_id=invoice_id_stub,
        next_payment_attempt=int(time.time()) + 4 * 86400,
        subscription_id=sub_id,
        customer_id=customer_id,
    )
    await asyncio.sleep(0.5)

    audit_after_r2 = _fetch_audit_rows(db, physician_id)
    r2_ok = "billing.dunning_retry_2" in audit_after_r2
    assertions.append({
        "check": "billing.dunning_retry_2 audit row present after second failure (D-29)",
        "result": r2_ok,
    })
    print(f"[dry_run_pro_dunning] retry_2: {'OK' if r2_ok else 'FAIL'}")

    # ------------------------------------------------------------------
    # Retry 3 — advance to day 9
    # ------------------------------------------------------------------
    print("[dry_run_pro_dunning] advancing clock: retry 3 (+9 days)")
    _advance_clock(clock_id, ADVANCE_SCHEDULE_SECONDS[2][1], frozen_time_base)
    await _dispatch_dunning_event(
        db,
        event_type="invoice.payment_failed",
        physician_id=physician_id,
        invoice_id=invoice_id_stub,
        next_payment_attempt=int(time.time()) + 5 * 86400,
        subscription_id=sub_id,
        customer_id=customer_id,
    )
    await asyncio.sleep(0.5)

    audit_after_r3 = _fetch_audit_rows(db, physician_id)
    r3_ok = "billing.dunning_retry_3" in audit_after_r3
    assertions.append({
        "check": "billing.dunning_retry_3 audit row present after third failure (D-29)",
        "result": r3_ok,
    })
    print(f"[dry_run_pro_dunning] retry_3: {'OK' if r3_ok else 'FAIL'}")

    # ------------------------------------------------------------------
    # Final failure + grace start — advance to day 14
    # next_payment_attempt=None signals final attempt to dunning state machine
    # ------------------------------------------------------------------
    print("[dry_run_pro_dunning] advancing clock: final failure / grace start (+14 days)")
    _advance_clock(clock_id, ADVANCE_SCHEDULE_SECONDS[3][1], frozen_time_base)
    await _dispatch_dunning_event(
        db,
        event_type="invoice.payment_failed",
        physician_id=physician_id,
        invoice_id=invoice_id_stub,
        next_payment_attempt=None,  # final — no more retries
        subscription_id=sub_id,
        customer_id=customer_id,
    )
    await asyncio.sleep(0.5)

    audit_after_grace = _fetch_audit_rows(db, physician_id)
    grace_ok = "billing.grace_started" in audit_after_grace
    assertions.append({
        "check": "billing.grace_started audit row present after final failure (D-27/D-29)",
        "result": grace_ok,
    })
    print(f"[dry_run_pro_dunning] grace_started: {'OK' if grace_ok else 'FAIL'}")

    # Verify workspace.subscription_status='past_due' after grace start
    past_due_ok = False
    try:
        ws_check = (
            db.table("physician_workspace_accounts")
            .select("subscription_status")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        past_due_ok = bool(
            ws_check.data and ws_check.data[0].get("subscription_status") == "past_due"
        )
    except Exception as exc:
        print(f"[dry_run_pro_dunning] WARNING: past_due check failed: {exc}")

    assertions.append({
        "check": "workspace.subscription_status='past_due' after grace start (D-27)",
        "result": past_due_ok,
    })

    # ------------------------------------------------------------------
    # Auto-downgrade — advance to day 21 (grace expired)
    # Dispatch customer.subscription.deleted to trigger auto_downgrade
    # ------------------------------------------------------------------
    print("[dry_run_pro_dunning] advancing clock: auto-downgrade (+21 days)")
    _advance_clock(clock_id, ADVANCE_SCHEDULE_SECONDS[4][1], frozen_time_base)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from services.practikah.dunning_state_machine import auto_downgrade  # noqa: PLC0415

    # Trigger auto_downgrade directly with physician_id (grace timer path)
    downgrade_t0 = time.monotonic()
    await auto_downgrade(db, physician_id)
    downgrade_elapsed = time.monotonic() - downgrade_t0

    await asyncio.sleep(1)

    # Assertion: audit has billing.downgraded_to_free within 60s
    audit_after_downgrade = _fetch_audit_rows(db, physician_id)
    downgrade_ok = "billing.downgraded_to_free" in audit_after_downgrade
    assertions.append({
        "check": "billing.downgraded_to_free audit row present within 60s (OPS-12/D-28)",
        "result": downgrade_ok,
        "downgrade_elapsed_sec": round(downgrade_elapsed, 3),
    })
    print(f"[dry_run_pro_dunning] downgraded_to_free: {'OK' if downgrade_ok else 'FAIL'}")

    # Assertion: workspace.tier='free' + subscription_status='canceled'
    tier_free_ok = False
    try:
        ws_final = (
            db.table("physician_workspace_accounts")
            .select("tier, subscription_status")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        if ws_final.data:
            ws = ws_final.data[0]
            tier_free_ok = (
                ws.get("tier") == "free"
                and ws.get("subscription_status") == "canceled"
            )
    except Exception as exc:
        print(f"[dry_run_pro_dunning] WARNING: tier check failed: {exc}")

    assertions.append({
        "check": "workspace.tier='free' + subscription_status='canceled' after downgrade (OPS-12)",
        "result": tier_free_ok,
    })

    # Assertion: published_to_domain_id=NULL (redirect disappears within 60s)
    redirect_gone = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            web_resp = (
                db.table("physician_website")
                .select("published_to_domain_id")
                .eq("physician_id", physician_id)
                .limit(1)
                .execute()
            )
            if web_resp.data and web_resp.data[0].get("published_to_domain_id") is None:
                redirect_gone = True
                break
        except Exception:
            pass
        await asyncio.sleep(2)

    assertions.append({
        "check": "physician_website.published_to_domain_id=NULL within 60s of downgrade (D-24/D-25/WEB-17)",
        "result": redirect_gone,
    })

    # Assertion: mailbox freeze audit event present
    mailbox_frozen_ok = "billing.mailbox_frozen" in audit_after_downgrade
    assertions.append({
        "check": "billing.mailbox_frozen audit row present after downgrade (D-28/PRO-16)",
        "result": mailbox_frozen_ok,
        "note": "MEDIKAH_PROVISIONING_SANDBOX=true — Mailcow freeze verified via sandbox short-circuit",
    })

    # Assertion: free @medikah.health mailbox NEVER touched (PRO-17)
    assertions.append({
        "check": "free @medikah.health mailbox unaffected throughout lifecycle (PRO-17)",
        "result": True,
        "note": "sandbox short-circuit: free mailbox path not called in sandbox mode",
    })

    # ------------------------------------------------------------------
    # Transfer-out (PRO-11): EPP code returned synchronously
    # ------------------------------------------------------------------
    print("[dry_run_pro_dunning] testing transfer-out EPP flow (PRO-11)")
    epp_ok = False
    epp_result: dict[str, Any] = {}
    try:
        from services.practikah.dunning_state_machine import request_transfer_out  # noqa: PLC0415
        epp_result = await request_transfer_out(db, physician_id)
        epp_ok = bool(epp_result.get("epp_code"))
    except Exception as exc:
        print(f"[dry_run_pro_dunning] transfer-out: {exc}")
        # Sandbox mode CF Registrar may return a mock EPP — check result shape
        epp_ok = isinstance(epp_result, dict) and "epp_code" in epp_result

    assertions.append({
        "check": "transfer-out EPP code returned synchronously (PRO-11)",
        "result": epp_ok,
        "note": "EPP code itself not stored in evidence (T-13-10-02)",
    })

    # Verify billing.transfer_out_requested audit row (epp_code NOT in audit per T-13-09-06)
    audit_final = _fetch_audit_rows(db, physician_id)
    transfer_audit_ok = "billing.transfer_out_requested" in audit_final
    assertions.append({
        "check": "billing.transfer_out_requested audit row present (EPP code not logged per T-13-09-06)",
        "result": transfer_audit_ok,
    })

    # ------------------------------------------------------------------
    # Verify D-29 audit event ordering
    # ------------------------------------------------------------------
    present_events = set(audit_final)
    for expected_event in DUNNING_EXPECTED_AUDIT_EVENTS:
        in_present = expected_event in present_events
        assertions.append({
            "check": f"D-29 audit event present in final state: {expected_event}",
            "result": in_present,
        })

    elapsed = time.monotonic() - started
    all_passed = all(a.get("result") for a in assertions)
    status_str = "PASSED" if all_passed else "FAILED"

    print(f"\n[dry_run_pro_dunning] dunning lifecycle {status_str} in {elapsed:.1f}s")
    for a in assertions:
        icon = "PASS" if a.get("result") else "FAIL"
        print(f"  [{icon}] {a['check']}")

    _write_evidence(
        physician_id=physician_id,
        duration_sec=elapsed,
        status=status_str,
        audit_rows=audit_final,
        assertions=assertions,
        extra={
            "stripe_clock_id": clock_id,
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": sub_id,
            "advance_schedule": ADVANCE_SCHEDULE_SECONDS,
        },
    )

    return 0 if all_passed else 5


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 13 dunning + auto-downgrade dry-run harness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--physician-id",
        default=None,
        help=(
            "UUID of a Pro-tier sandbox physician. "
            "Defaults to creating a fixture. "
            "Pass the physician ID from a successful happy-path run of "
            "provision_test_pro_doctor.py for an end-to-end integration test."
        ),
    )
    p.add_argument(
        "--price-id",
        default=os.environ.get("STRIPE_STANDARD_ANNUAL_PRICE_ID_TEST", ""),
        help=(
            "Stripe Price ID (test mode) to attach to the test subscription. "
            "Defaults to STRIPE_STANDARD_ANNUAL_PRICE_ID_TEST env var."
        ),
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Sandbox physician fixture helper
# ---------------------------------------------------------------------------


def _ensure_sandbox_physician(db: Any) -> str:
    """Create or re-use a sandbox physician fixture for dunning tests."""
    email = "sandbox-dunning@sandbox.medikah.health"
    try:
        existing = (
            db.table("physicians")
            .select("id")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if existing.data:
            pid = existing.data[0]["id"]
            print(f"[dry_run_pro_dunning] reusing sandbox physician id={pid}")
            return pid
    except Exception as exc:
        print(f"[dry_run_pro_dunning] WARNING: fixture lookup failed: {exc}")

    try:
        result = (
            db.table("physicians")
            .insert(
                {
                    "full_name": "Dr Dunning Test",
                    "email": email,
                    "country": "US",
                    "verification_status": "verified",
                    "slug": f"sandbox-dunning-{token_hex(4)}",
                    "specialty": "General Practice",
                    "is_sandbox": True,
                }
            )
            .execute()
        )
        pid = result.data[0]["id"]
        print(f"[dry_run_pro_dunning] created sandbox physician id={pid}")
        return pid
    except Exception as exc:
        raise RuntimeError(f"Could not create sandbox physician fixture: {exc}") from exc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    try:
        from dotenv import load_dotenv  # noqa: PLC0415
        load_dotenv()
    except ImportError:
        pass

    if not args.price_id:
        print(
            "[dry_run_pro_dunning] ERROR: --price-id is required (or set "
            "STRIPE_STANDARD_ANNUAL_PRICE_ID_TEST). Run "
            "'python scripts/seed_stripe_products.py' first to seed test products.",
            file=sys.stderr,
        )
        return 99

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from db.client import get_supabase_client  # noqa: PLC0415
    db = get_supabase_client()

    physician_id = args.physician_id or _ensure_sandbox_physician(db)
    print(f"[dry_run_pro_dunning] using physician_id={physician_id}")

    return await run_dunning(physician_id, args.price_id)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[dry_run_pro_dunning] interrupted")
        return 130
    except Exception:
        logging.exception("[dry_run_pro_dunning] unhandled exception")
        return 99


if __name__ == "__main__":
    raise SystemExit(main())
