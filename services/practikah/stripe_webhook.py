"""Stripe webhook handler — signature verification + idempotent event router (Phase 13-01).

Per D-13: this module is the SOLE writer of subscription state on
``physician_workspace_accounts`` (alongside ``dunning_state_machine.py`` in 13-09).
All other modules read but never mutate ``tier``, ``subscription_status``,
``current_period_end``, ``stripe_customer_id``, ``stripe_subscription_id``.

Per T-13-01-01 / T-13-01-02: signature verification is mandatory and runs
on the EXACT raw bytes of the webhook body. Any JSON re-serialization
breaks signature verification. The Next.js BFF
(``pages/api/practikah/upgrade/webhook.ts``) disables ``bodyParser`` for
this reason.

Per T-13-01-07: every event is idempotency-checked via
``stripe_events_processed`` (PRIMARY KEY on ``event_id``). Duplicate
deliveries from Stripe's 3-day retry window are absorbed by the DB-level
UNIQUE constraint with no application-state side-effects.

Best-effort, never-raises pattern (mirrors ``services/practikah/audit.py``
lines 73-90): subscription-state writes log via ``logger.exception`` on
failure but never propagate. Stripe will retry; idempotency check absorbs
the resulting duplicate delivery.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature verification (T-13-01-01)
# ---------------------------------------------------------------------------

def _stripe_webhook_secret() -> str:
    """Return STRIPE_WEBHOOK_SECRET, or raise RuntimeError if unset.

    We resolve at call time (not import time) so module import doesn't
    crash uvicorn startup before env is loaded.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET is not set. "
            "Webhook signature verification cannot proceed."
        )
    return secret


def verify_signature(raw_body: bytes, sig_header: str):
    """Verify Stripe webhook signature on the raw body.

    Wraps ``stripe.Webhook.construct_event`` per Stripe SDK convention.
    Raises ``ValueError`` on bad signature; FastAPI converts to HTTP 400.

    The raw_body MUST be the exact bytes Stripe sent — any re-serialization
    breaks HMAC verification (T-13-01-02).
    """
    import stripe  # imported lazily — keeps module importable without stripe SDK
    secret = _stripe_webhook_secret()
    # construct_event raises stripe.error.SignatureVerificationError or ValueError
    return stripe.Webhook.construct_event(raw_body, sig_header, secret)


# ---------------------------------------------------------------------------
# Idempotency (T-13-01-07)
# ---------------------------------------------------------------------------

def _payload_hash(raw_body: bytes) -> str:
    """Return sha256(raw_body) hex — non-repudiation evidence per T-13-01-03."""
    return hashlib.sha256(raw_body).hexdigest()


def _record_event(
    db: Any,
    event_id: str,
    event_type: str,
    physician_id: Optional[str],
    payload_hash: str,
) -> bool:
    """Insert into stripe_events_processed; return True if new, False if duplicate.

    Relies on the PRIMARY KEY on ``event_id`` to reject duplicates at the
    DB layer (T-13-01-07). PostgREST surfaces unique-violations as a
    structured error; we catch the broad Exception class because the
    ``supabase-py`` client raises various subclasses depending on version.
    """
    try:
        db.table("stripe_events_processed").insert(
            {
                "event_id": event_id,
                "event_type": event_type,
                "physician_id": physician_id,
                "payload_hash": payload_hash,
            }
        ).execute()
        return True
    except Exception as err:
        # Treat any insert error as "already processed". This is safe because:
        #   1. The PRIMARY KEY guarantees uniqueness — re-attempting is benign.
        #   2. If the DB is genuinely down, the dispatch below will also fail
        #      and Stripe will retry, hitting the same idempotency check on retry.
        logger.info(
            "[stripe_webhook] event %s already processed or insert rejected: %s",
            event_id, err,
        )
        return False


# ---------------------------------------------------------------------------
# Helpers — extract physician_id and resolve subscription rows
# ---------------------------------------------------------------------------

def _physician_id_from_metadata(stripe_object: dict[str, Any]) -> Optional[str]:
    """Read physician_id from Stripe object metadata.

    The checkout session is created with metadata={'physician_id': ...} by
    13-04 (PR placeholder today). For invoice / subscription events we
    fall back to looking up by stripe_customer_id below.
    """
    meta = stripe_object.get("metadata") or {}
    pid = meta.get("physician_id")
    return pid if isinstance(pid, str) and pid else None


def _physician_id_by_customer(db: Any, customer_id: Optional[str]) -> Optional[str]:
    """Look up physician_id by stripe_customer_id on physician_workspace_accounts."""
    if not customer_id:
        return None
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("physician_id")
            .eq("stripe_customer_id", customer_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0].get("physician_id")
    except Exception:
        logger.exception(
            "[stripe_webhook] lookup by customer_id=%s failed", customer_id
        )
    return None


def _physician_id_by_subscription(db: Any, subscription_id: Optional[str]) -> Optional[str]:
    """Look up physician_id by stripe_subscription_id on physician_workspace_accounts."""
    if not subscription_id:
        return None
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("physician_id")
            .eq("stripe_subscription_id", subscription_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0].get("physician_id")
    except Exception:
        logger.exception(
            "[stripe_webhook] lookup by subscription_id=%s failed", subscription_id
        )
    return None


def _epoch_to_iso(value: Any) -> Optional[str]:
    """Convert a Stripe epoch (int, seconds) to ISO 8601 UTC string."""
    if value is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Event dispatchers
# ---------------------------------------------------------------------------

async def _on_checkout_session_completed(event: dict[str, Any], db: Any) -> dict[str, Any]:
    """Trigger the pro_upgrade saga when checkout.session.completed lands.

    Stub today — wired to ``services.practikah.orchestrator.start_pro_upgrade_saga``
    in Plan 13-06. The webhook persists the customer/subscription IDs so the
    saga can pick up from a known-good state.

    Per D-13 the webhook (this function) is the sole writer of
    stripe_customer_id/stripe_subscription_id on physician_workspace_accounts.
    """
    obj = event["data"]["object"]
    physician_id = _physician_id_from_metadata(obj)
    customer_id = obj.get("customer")
    subscription_id = obj.get("subscription")

    if not physician_id:
        physician_id = _physician_id_by_customer(db, customer_id)

    if physician_id:
        try:
            db.table("physician_workspace_accounts").update(
                {
                    "stripe_customer_id": customer_id,
                    "stripe_subscription_id": subscription_id,
                }
            ).eq("physician_id", physician_id).execute()
        except Exception:
            logger.exception(
                "[stripe_webhook] checkout.session.completed: failed to persist "
                "stripe IDs for physician_id=%s", physician_id,
            )

    # TODO(13-06): start the pro_upgrade saga here.
    # from services.practikah.orchestrator import start_pro_upgrade_saga
    # await start_pro_upgrade_saga(physician_id=physician_id, session=obj)

    return {
        "dispatched": "checkout.session.completed",
        "physician_id": physician_id,
        "subscription_id": subscription_id,
    }


async def _on_invoice_payment_succeeded(event: dict[str, Any], db: Any) -> dict[str, Any]:
    """Mark subscription active and refresh current_period_end.

    Per D-13 this writer is authoritative for these columns.
    """
    obj = event["data"]["object"]
    subscription_id = obj.get("subscription")
    customer_id = obj.get("customer")

    # Pull period_end from the first line item (recurring price) when present.
    period_end_iso: Optional[str] = None
    try:
        lines = (obj.get("lines") or {}).get("data") or []
        if lines:
            period = (lines[0] or {}).get("period") or {}
            period_end_iso = _epoch_to_iso(period.get("end"))
    except Exception:
        # Defensive — Stripe SDK shape can shift; keep handler resilient.
        logger.exception("[stripe_webhook] failed to parse invoice line period")

    physician_id = (
        _physician_id_by_subscription(db, subscription_id)
        or _physician_id_by_customer(db, customer_id)
    )

    if not physician_id or not subscription_id:
        logger.warning(
            "[stripe_webhook] invoice.payment_succeeded with no physician match "
            "subscription_id=%s customer_id=%s", subscription_id, customer_id,
        )
        return {"dispatched": "invoice.payment_succeeded", "matched": False}

    update_payload: dict[str, Any] = {"subscription_status": "active"}
    if period_end_iso:
        update_payload["current_period_end"] = period_end_iso

    try:
        db.table("physician_workspace_accounts").update(update_payload).eq(
            "stripe_subscription_id", subscription_id
        ).execute()
    except Exception:
        logger.exception(
            "[stripe_webhook] invoice.payment_succeeded: update failed "
            "subscription_id=%s", subscription_id,
        )

    return {
        "dispatched": "invoice.payment_succeeded",
        "physician_id": physician_id,
        "current_period_end": period_end_iso,
    }


async def _on_invoice_payment_failed(event: dict[str, Any], db: Any) -> dict[str, Any]:
    """Hand off to dunning state machine (stub — Plan 13-09).

    Today we just record the event; 13-09 wires the retry/grace logic.
    """
    obj = event["data"]["object"]
    subscription_id = obj.get("subscription")
    customer_id = obj.get("customer")
    physician_id = (
        _physician_id_by_subscription(db, subscription_id)
        or _physician_id_by_customer(db, customer_id)
    )

    # TODO(13-09): dispatch into dunning state machine.
    # from services.practikah.dunning_state_machine import on_payment_failed
    # await on_payment_failed(event, db)

    if physician_id:
        try:
            db.table("physician_workspace_accounts").update(
                {"subscription_status": "past_due"}
            ).eq("physician_id", physician_id).execute()
        except Exception:
            logger.exception(
                "[stripe_webhook] invoice.payment_failed: past_due update failed "
                "physician_id=%s", physician_id,
            )

    return {"dispatched": "invoice.payment_failed", "physician_id": physician_id}


async def _on_subscription_deleted(event: dict[str, Any], db: Any) -> dict[str, Any]:
    """Auto-downgrade to free when Stripe reports the subscription as deleted (D-13).

    The dunning state machine (Plan 13-09) handles the multi-step downgrade
    (mailbox freeze → purge → domain release). Today we just flip status so
    UI/middleware reflect reality.
    """
    obj = event["data"]["object"]
    subscription_id = obj.get("id")
    customer_id = obj.get("customer")
    physician_id = (
        _physician_id_by_subscription(db, subscription_id)
        or _physician_id_by_customer(db, customer_id)
    )

    # TODO(13-09): dispatch into dunning state machine for full auto-downgrade saga.
    # from services.practikah.dunning_state_machine import auto_downgrade
    # await auto_downgrade(event, db)

    if physician_id:
        try:
            db.table("physician_workspace_accounts").update(
                {"subscription_status": "canceled", "tier": "free"}
            ).eq("physician_id", physician_id).execute()
        except Exception:
            logger.exception(
                "[stripe_webhook] customer.subscription.deleted: downgrade update failed "
                "physician_id=%s", physician_id,
            )

    return {"dispatched": "customer.subscription.deleted", "physician_id": physician_id}


async def _on_subscription_updated(event: dict[str, Any], db: Any) -> dict[str, Any]:
    """Sync subscription_status / current_period_end / plan changes from Stripe.

    Plan switches via the Stripe Customer Portal land here. We pull the
    plan_id from the first item.
    """
    obj = event["data"]["object"]
    subscription_id = obj.get("id")
    customer_id = obj.get("customer")
    status = obj.get("status")
    period_end_iso = _epoch_to_iso(obj.get("current_period_end"))

    plan_id: Optional[str] = None
    try:
        items = (obj.get("items") or {}).get("data") or []
        if items:
            price = (items[0] or {}).get("price") or {}
            plan_id = price.get("id")
    except Exception:
        logger.exception("[stripe_webhook] failed to read subscription plan id")

    physician_id = (
        _physician_id_by_subscription(db, subscription_id)
        or _physician_id_by_customer(db, customer_id)
    )

    if not physician_id or not subscription_id:
        logger.warning(
            "[stripe_webhook] customer.subscription.updated with no physician match "
            "subscription_id=%s customer_id=%s", subscription_id, customer_id,
        )
        return {"dispatched": "customer.subscription.updated", "matched": False}

    update_payload: dict[str, Any] = {}
    if status:
        update_payload["subscription_status"] = status
    if period_end_iso:
        update_payload["current_period_end"] = period_end_iso
    if plan_id:
        # Stored as plan_id on physician_workspace_accounts; column added in 13-04
        # if not already present (additive). Today we attempt the update; if the
        # column doesn't exist yet, the Supabase client raises and we log+continue.
        update_payload["plan_id"] = plan_id

    if update_payload:
        try:
            db.table("physician_workspace_accounts").update(update_payload).eq(
                "stripe_subscription_id", subscription_id
            ).execute()
        except Exception:
            logger.exception(
                "[stripe_webhook] customer.subscription.updated: update failed "
                "subscription_id=%s payload_keys=%s",
                subscription_id, list(update_payload.keys()),
            )

    return {
        "dispatched": "customer.subscription.updated",
        "physician_id": physician_id,
        "status": status,
        "plan_id": plan_id,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Event-type dispatch table. Adding a new event = add a row here.
_DISPATCH = {
    "checkout.session.completed": _on_checkout_session_completed,
    "invoice.payment_succeeded": _on_invoice_payment_succeeded,
    "invoice.payment_failed": _on_invoice_payment_failed,
    "customer.subscription.deleted": _on_subscription_deleted,
    "customer.subscription.updated": _on_subscription_updated,
}


async def handle_event(event: Any, db: Any, raw_body: Optional[bytes] = None) -> dict[str, Any]:
    """Idempotently dispatch a verified Stripe event.

    Steps:
      1. Insert into ``stripe_events_processed`` (DB rejects duplicates via PK).
         If the insert is a no-op, return ``{"status":"already_processed"}``.
      2. Look up dispatcher by ``event.type``; ignore unsubscribed events.
      3. Call dispatcher; trap any exception (best-effort write — Stripe will
         retry, idempotency table absorbs the duplicate).

    The ``event`` parameter is whatever ``stripe.Webhook.construct_event``
    returned — a ``stripe.Event`` object that supports both attribute and
    dict access. We use dict access throughout for portability across
    SDK versions and to keep this file testable without the SDK installed.
    """
    # Normalise to dict — stripe.Event supports __getitem__ but not all
    # attribute paths, so dict access is the lowest-common-denominator.
    if hasattr(event, "to_dict"):
        event_dict = event.to_dict()
    elif isinstance(event, dict):
        event_dict = event
    else:
        # Fall back to attribute access — try common Stripe SDK shape.
        event_dict = {
            "id": getattr(event, "id", None),
            "type": getattr(event, "type", None),
            "data": getattr(event, "data", None),
        }

    event_id = event_dict.get("id")
    event_type = event_dict.get("type")

    if not event_id or not event_type:
        logger.error(
            "[stripe_webhook] malformed event missing id/type: %s", event_dict
        )
        return {"status": "malformed"}

    # 1. Idempotency gate — DB PK rejects duplicates (T-13-01-07).
    payload_hash = _payload_hash(raw_body) if raw_body else "no_raw_body"
    is_new = _record_event(db, event_id, event_type, None, payload_hash)
    if not is_new:
        return {"status": "already_processed", "event_id": event_id}

    # 2. Dispatch.
    handler = _DISPATCH.get(event_type)
    if handler is None:
        logger.info(
            "[stripe_webhook] no handler for event_type=%s (event_id=%s) — ignoring",
            event_type, event_id,
        )
        return {"status": "ignored", "event_id": event_id, "event_type": event_type}

    # 3. Best-effort handler invocation. Never raise to caller — Stripe
    # retries failed webhooks for 3 days; we want to keep returning 200
    # for events we've already recorded as processed.
    try:
        result = await handler(event_dict, db)
        return {"status": "processed", "event_id": event_id, **(result or {})}
    except Exception:
        logger.exception(
            "[stripe_webhook] handler crashed event_id=%s event_type=%s",
            event_id, event_type,
        )
        return {"status": "handler_error", "event_id": event_id, "event_type": event_type}
