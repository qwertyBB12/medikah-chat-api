"""Dunning + auto-downgrade state machine (Phase 13-09 / OPS-12 / D-27 / D-28 / D-29).

Per D-11: Stripe Smart Retries owns the 3 retry emails over 14 days. We listen
for ``invoice.payment_failed`` events. Non-final attempts increment a retry
counter and write an audit row. The final attempt (``next_payment_attempt is
None``) transitions the workspace into the 7-day grace period. Grace expiration
(or ``customer.subscription.deleted``) triggers ``auto_downgrade``.

Per D-28: auto_downgrade does the following atomically (best-effort, audited
per D-29):
  1. Freeze the Pro custom-domain mailbox via Mailcow ACL — IMAP login + mbox
     export remain available, SMTP send/receive blocked. The free
     ``@medikah.health`` mailbox is NEVER touched (PRO-17).
  2. Detach the Cloudflare for SaaS Custom Hostname (cert + DNS removed).
  3. NULL out ``physician_website.published_to_domain_id`` so the Plan 13-08
     middleware redirect disappears (Try Pro preview at
     ``<slug>.medikah.health`` resumes serving directly within 60s).
  4. Set ``physician_workspace_accounts.tier='free'``,
     ``subscription_status='canceled'`` and stamp ``frozen_until`` for the
     30-day mailbox hold (D-28).
  5. Schedule mailbox purge after FROZEN_HOLD_DAYS unless the doctor requests
     transfer-out (PRO-11) which collapses the hold.

Per PRO-11: ``request_transfer_out`` returns the EPP code synchronously by
delegating to ``cloudflare_registrar.do_transfer_out``. EPP codes are NEVER
written to the audit log — only the ``epp_issued: True`` flag (T-13-09-06).

Per D-29 the following workspace_audit_log actions are written:
``billing.payment_failed``, ``billing.dunning_retry_{1,2,3}``,
``billing.grace_started``, ``billing.downgraded_to_free``,
``billing.mailbox_frozen``, ``billing.mailbox_purged``,
``billing.domain_released``, ``billing.transfer_out_requested``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .cloudflare_for_saas import cf_saas
from .cloudflare_registrar import cf_registrar
from .mailbox_provisioner import mailbox_provisioner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — tuned per D-27/D-28
# ---------------------------------------------------------------------------

GRACE_DAYS = 7
FROZEN_HOLD_DAYS = 30


# ---------------------------------------------------------------------------
# Helpers — workspace_audit_log writer (best-effort; never raises)
# ---------------------------------------------------------------------------

def _log_workspace_audit(
    db: Any,
    physician_id: Optional[str],
    action: str,
    *,
    resource: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Best-effort insert into ``workspace_audit_log`` (OPS-01 / D-29).

    Mirrors ``pro_saga._log_workspace_audit``. Never raises — audit failures
    log via ``logger.exception`` and return.

    Per T-13-09-06 the EPP code is NOT written to ``detail``; callers pass
    only the ``epp_issued`` flag.
    """
    if db is None or not physician_id:
        return
    try:
        db.table("workspace_audit_log").insert(
            {
                "physician_id": physician_id,
                "actor_id": physician_id,
                "actor_role": "system",
                "action": action,
                "resource_type": "workspace",
                "resource_id": resource,
                "detail": detail or {},
            }
        ).execute()
    except Exception:
        logger.exception(
            "[dunning] _log_workspace_audit failed physician_id=%s action=%s",
            physician_id, action,
        )


def _physician_id_from_event(event: Any) -> Optional[str]:
    """Pull a physician_id off a Stripe event-shaped dict (or None)."""
    if not isinstance(event, dict):
        return None
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    pid = meta.get("physician_id") if isinstance(meta, dict) else None
    return pid if isinstance(pid, str) and pid else None


def _physician_by_subscription(db: Any, subscription_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Look up the workspace row by stripe_subscription_id."""
    if not subscription_id or db is None:
        return None
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("*")
            .eq("stripe_subscription_id", subscription_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
    except Exception:
        logger.exception(
            "[dunning] _physician_by_subscription failed subscription_id=%s",
            subscription_id,
        )
    return None


def _physician_by_customer(db: Any, customer_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not customer_id or db is None:
        return None
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("*")
            .eq("stripe_customer_id", customer_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
    except Exception:
        logger.exception(
            "[dunning] _physician_by_customer failed customer_id=%s", customer_id
        )
    return None


def _resolve_workspace(db: Any, event: dict[str, Any]) -> Optional[dict[str, Any]]:
    obj = (event.get("data") or {}).get("object") or {}
    sub_id = obj.get("subscription") or (obj.get("id") if event.get("type") == "customer.subscription.deleted" else None)
    cust_id = obj.get("customer")
    return (
        _physician_by_subscription(db, sub_id)
        or _physician_by_customer(db, cust_id)
    )


def _domain_row_for(db: Any, physician_id: str) -> Optional[dict[str, Any]]:
    if db is None or not physician_id:
        return None
    try:
        result = (
            db.table("physician_domains")
            .select("*")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
    except Exception:
        logger.exception(
            "[dunning] _domain_row_for failed physician_id=%s", physician_id
        )
    return None


# ---------------------------------------------------------------------------
# Email trigger (best-effort BFF call)
# ---------------------------------------------------------------------------

async def _trigger_email(kind: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget call to the Next.js practikah-email-trigger BFF.

    Bilingual content + Resend send live in lib/practikahEmail.ts (frontend).
    The internal BFF route forwards to that lib so the Resend API key stays
    server-side only.
    """
    import os

    base = os.environ.get("FRONTEND_BASE_URL") or os.environ.get(
        "NEXT_PUBLIC_BASE_URL", "https://medikah.health"
    )
    secret = os.environ.get("INTERNAL_API_SHARED_SECRET", "")
    if not secret:
        logger.info(
            "[dunning] _trigger_email skipped (no INTERNAL_API_SHARED_SECRET) kind=%s",
            kind,
        )
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            await client.post(
                f"{base.rstrip('/')}/api/internal/practikah-email-trigger",
                json={"kind": kind, **payload},
                headers={"X-Internal-Secret": secret},
            )
    except Exception:
        logger.exception("[dunning] _trigger_email failed kind=%s", kind)


# ---------------------------------------------------------------------------
# Public API — Stripe webhook entry points (called by stripe_webhook.handle_event)
# ---------------------------------------------------------------------------

async def on_payment_failed(db: Any, event: dict[str, Any]) -> dict[str, Any]:
    """Handle ``invoice.payment_failed`` (D-11 + D-27).

    For non-final attempts (``next_payment_attempt is not None``) we just
    increment the retry counter and write an audit row. For the final
    attempt we flow into ``on_payment_failed_final`` which starts the
    grace period.
    """
    obj = (event.get("data") or {}).get("object") or {}
    workspace = _resolve_workspace(db, event)
    if not workspace:
        logger.warning(
            "[dunning] on_payment_failed could not resolve workspace event=%s",
            event.get("id"),
        )
        return {"dispatched": "invoice.payment_failed", "matched": False}

    physician_id = workspace.get("physician_id")
    invoice_id = obj.get("id")
    # D-11 / Stripe convention: next_payment_attempt is None on the final attempt.
    is_final = obj.get("next_payment_attempt") is None

    _log_workspace_audit(
        db,
        physician_id,
        "billing.payment_failed",
        resource=invoice_id,
        detail={"is_final": is_final},
    )

    if not is_final:
        # Increment retry counter (1-based, capped at 3 per D-11 Smart Retries config).
        retry_n = (workspace.get("dunning_retry_count") or 0) + 1
        try:
            db.table("physician_workspace_accounts").update(
                {"dunning_retry_count": retry_n}
            ).eq("physician_id", physician_id).execute()
        except Exception:
            # Column may not exist on older deployments — log but continue.
            logger.warning(
                "[dunning] dunning_retry_count update failed physician_id=%s "
                "(column may be missing — additive migration pending)", physician_id,
            )
        bucket = max(1, min(retry_n, 3))
        _log_workspace_audit(
            db,
            physician_id,
            f"billing.dunning_retry_{bucket}",
            resource=invoice_id,
            detail={"retry_n": retry_n},
        )
        return {
            "dispatched": "invoice.payment_failed",
            "physician_id": physician_id,
            "retry_n": retry_n,
            "is_final": False,
        }

    # Final attempt failed → start grace period
    return await on_payment_failed_final(db, event)


async def on_payment_failed_final(db: Any, event: dict[str, Any]) -> dict[str, Any]:
    """Transition workspace to 7-day grace period (D-27 / D-28)."""
    obj = (event.get("data") or {}).get("object") or {}
    workspace = _resolve_workspace(db, event)
    if not workspace:
        return {"dispatched": "invoice.payment_failed_final", "matched": False}
    physician_id = workspace.get("physician_id")
    invoice_id = obj.get("id")

    grace_until = datetime.now(timezone.utc) + timedelta(days=GRACE_DAYS)
    try:
        db.table("physician_workspace_accounts").update(
            {
                "subscription_status": "past_due",
                "grace_until": grace_until.isoformat(),
            }
        ).eq("physician_id", physician_id).execute()
    except Exception:
        logger.exception(
            "[dunning] on_payment_failed_final: update past_due failed physician_id=%s",
            physician_id,
        )

    _log_workspace_audit(
        db,
        physician_id,
        "billing.grace_started",
        resource=invoice_id,
        detail={"grace_until": grace_until.isoformat(), "grace_days": GRACE_DAYS},
    )

    # Schedule auto-downgrade after GRACE_DAYS unless Stripe Smart Retries
    # recovers payment in the meantime (we re-check status before downgrading).
    asyncio.create_task(
        _schedule_auto_downgrade(db, physician_id, delay_seconds=GRACE_DAYS * 86400)
    )

    # Bilingual supplement (Stripe sent the canonical retry email already).
    physician_email = workspace.get("physician_email") or ""
    if physician_email:
        await _trigger_email(
            "dunning_grace_started",
            {
                "to": physician_email,
                "grace_days": GRACE_DAYS,
                "physician_id": physician_id,
            },
        )

    return {
        "dispatched": "invoice.payment_failed_final",
        "physician_id": physician_id,
        "grace_until": grace_until.isoformat(),
    }


async def auto_downgrade(db: Any, event_or_physician_id: Any) -> dict[str, Any]:
    """Atomic downgrade flow (D-28).

    Accepts either a Stripe-event dict (``customer.subscription.deleted``)
    or a raw physician_id string (called from the grace-expiration timer).

    Steps (audited per D-29, best-effort — failures log but don't unwind):
      1. ``billing.mailbox_frozen`` — Mailcow read-only freeze on the Pro
         custom-domain mailbox. Free ``@medikah.health`` mailbox NEVER touched.
      2. CF for SaaS hostname detached (cert + DNS removed).
      3. ``physician_website.published_to_domain_id`` set to NULL — Plan 13-08
         middleware redirect disappears within 60s, Try Pro preview at
         ``<slug>.medikah.health`` resumes (PRO-17).
      4. ``physician_workspace_accounts`` tier→free, subscription_status→
         canceled, ``frozen_until`` stamped FROZEN_HOLD_DAYS in the future.
      5. ``billing.downgraded_to_free`` audit row.
      6. Schedule mailbox purge after FROZEN_HOLD_DAYS.
    """
    if isinstance(event_or_physician_id, str):
        physician_id = event_or_physician_id
    else:
        # Event-shaped — try metadata first, then resolve via subscription/customer.
        physician_id = _physician_id_from_event(event_or_physician_id)
        if not physician_id and isinstance(event_or_physician_id, dict):
            ws = _resolve_workspace(db, event_or_physician_id)
            if ws:
                physician_id = ws.get("physician_id")

    if not physician_id:
        logger.warning("[dunning] auto_downgrade: no physician_id resolved")
        return {"dispatched": "auto_downgrade", "matched": False}

    # Pull the workspace row (may already be free if user previously cancelled).
    ws_row = None
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("*")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        if result.data:
            ws_row = result.data[0]
    except Exception:
        logger.exception(
            "[dunning] auto_downgrade workspace fetch failed physician_id=%s",
            physician_id,
        )

    domain_row = _domain_row_for(db, physician_id)
    if not domain_row:
        # Already free (no Pro domain on file). Nothing to downgrade.
        logger.info(
            "[dunning] auto_downgrade: no physician_domains row physician_id=%s — skipping",
            physician_id,
        )
        return {"dispatched": "auto_downgrade", "physician_id": physician_id, "skipped": "no_domain"}

    custom_domain = domain_row.get("domain_name") or ""
    local_part = (ws_row or {}).get("pro_local_part") or domain_row.get("local_part") or ""

    # Step 1: freeze Pro mailbox (NEVER touches @medikah.health — T-13-09-07)
    if custom_domain and local_part:
        try:
            await mailbox_provisioner.freeze_pro_mailbox(custom_domain, local_part)
            _log_workspace_audit(
                db, physician_id, "billing.mailbox_frozen",
                resource=f"{local_part}@{custom_domain}",
                detail={"domain": custom_domain},
            )
        except Exception:
            logger.exception(
                "[dunning] freeze_pro_mailbox failed domain=%s local_part=%s",
                custom_domain, local_part,
            )

    # Step 2: detach CF for SaaS hostname (best-effort)
    cf_hostname_id = domain_row.get("cf_saas_hostname_id")
    if cf_hostname_id:
        try:
            await cf_saas.undo_attach_hostname(
                cf_hostname_id, f"downgrade-{physician_id}", None
            )
        except Exception:
            logger.exception(
                "[dunning] cf_saas.undo_attach_hostname failed hostname_id=%s",
                cf_hostname_id,
            )

    # Step 3: NULL out physician_website.published_to_domain_id (PRO-17 — middleware
    # redirect disappears within 60s; Try Pro preview resumes serving directly).
    try:
        db.table("physician_website").update(
            {"published_to_domain_id": None}
        ).eq("physician_id", physician_id).execute()
    except Exception:
        logger.exception(
            "[dunning] published_to_domain_id NULL failed physician_id=%s",
            physician_id,
        )

    # Step 4: flip tier + status
    now_iso = datetime.now(timezone.utc).isoformat()
    frozen_until = (datetime.now(timezone.utc) + timedelta(days=FROZEN_HOLD_DAYS)).isoformat()
    try:
        db.table("physician_workspace_accounts").update(
            {
                "tier": "free",
                "subscription_status": "canceled",
                "downgraded_at": now_iso,
                "frozen_until": frozen_until,
            }
        ).eq("physician_id", physician_id).execute()
    except Exception:
        logger.exception(
            "[dunning] auto_downgrade tier=free update failed physician_id=%s",
            physician_id,
        )

    # Step 5: audit
    _log_workspace_audit(
        db,
        physician_id,
        "billing.downgraded_to_free",
        resource=custom_domain,
        detail={"frozen_until": frozen_until, "frozen_hold_days": FROZEN_HOLD_DAYS},
    )

    # Step 6: notify + schedule purge
    physician_email = (ws_row or {}).get("physician_email")
    if physician_email and custom_domain:
        await _trigger_email(
            "downgrade_notice",
            {
                "to": physician_email,
                "domain": custom_domain,
                "frozen_hold_days": FROZEN_HOLD_DAYS,
                "physician_id": physician_id,
            },
        )

    asyncio.create_task(
        _schedule_mailbox_purge(
            db, physician_id, custom_domain, local_part,
            delay_seconds=FROZEN_HOLD_DAYS * 86400,
        )
    )

    return {
        "dispatched": "auto_downgrade",
        "physician_id": physician_id,
        "domain": custom_domain,
        "frozen_until": frozen_until,
    }


# ---------------------------------------------------------------------------
# Public API — Doctor-initiated transfer-out (PRO-11)
# ---------------------------------------------------------------------------

async def request_transfer_out(db: Any, physician_id: str) -> dict[str, Any]:
    """Request CF Registrar transfer-out and return the EPP code synchronously.

    Per PRO-11: doctors own their domain. EPP code is returned in the response
    body of the BFF call (not via email round-trip) so the dashboard renders
    it immediately for copy-to-clipboard.

    Per T-13-09-06: the EPP code is NEVER written to ``workspace_audit_log``;
    only the ``epp_issued: True`` flag is recorded.
    """
    domain_row = _domain_row_for(db, physician_id)
    if not domain_row:
        raise ValueError("no Pro domain on file")

    domain_name = domain_row.get("domain_name") or ""
    if not domain_name:
        raise ValueError("domain_name missing on physician_domains row")

    result = await cf_registrar.do_transfer_out(
        domain_name, run_id=f"transfer-{physician_id}"
    )
    if not result.success:
        raise RuntimeError(result.error or "transfer-out failed")

    epp_code = result.resource_id or ""

    # Audit: epp_issued flag only — NEVER the code itself (T-13-09-06)
    _log_workspace_audit(
        db,
        physician_id,
        "billing.transfer_out_requested",
        resource=domain_name,
        detail={"epp_issued": bool(epp_code)},
    )

    # Deliver the EPP code via authenticated email channel (PRO-11 — within 24h)
    workspace = None
    try:
        ws_q = (
            db.table("physician_workspace_accounts")
            .select("physician_email")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        if ws_q.data:
            workspace = ws_q.data[0]
    except Exception:
        logger.exception(
            "[dunning] request_transfer_out workspace email lookup failed physician_id=%s",
            physician_id,
        )

    if workspace and workspace.get("physician_email") and epp_code:
        await _trigger_email(
            "epp_code_delivery",
            {
                "to": workspace["physician_email"],
                "domain": domain_name,
                "epp_code": epp_code,
                "physician_id": physician_id,
            },
        )

    return {"epp_code": epp_code, "domain": domain_name}


# ---------------------------------------------------------------------------
# Schedulers — internal
# ---------------------------------------------------------------------------

async def _schedule_auto_downgrade(db: Any, physician_id: str, *, delay_seconds: int) -> None:
    """After grace expires, downgrade unless Smart Retries recovered payment."""
    try:
        await asyncio.sleep(delay_seconds)
    except Exception:
        logger.exception(
            "[dunning] _schedule_auto_downgrade sleep interrupted physician_id=%s",
            physician_id,
        )
        return

    # Re-check current subscription status — Smart Retries may have recovered.
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("subscription_status")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        status = (result.data[0].get("subscription_status") if result.data else "")
    except Exception:
        logger.exception(
            "[dunning] _schedule_auto_downgrade status re-check failed physician_id=%s",
            physician_id,
        )
        status = ""

    if status == "active":
        logger.info(
            "[dunning] _schedule_auto_downgrade aborted — payment recovered physician_id=%s",
            physician_id,
        )
        return

    await auto_downgrade(db, physician_id)


async def _schedule_mailbox_purge(
    db: Any,
    physician_id: str,
    domain: str,
    local_part: str,
    *,
    delay_seconds: int,
) -> None:
    """After 30-day frozen hold, purge mailbox + release domain (D-28)."""
    try:
        await asyncio.sleep(delay_seconds)
    except Exception:
        logger.exception(
            "[dunning] _schedule_mailbox_purge sleep interrupted physician_id=%s",
            physician_id,
        )
        return

    # If transfer-out collapsed the hold, the physician_domains row may be gone.
    domain_row = _domain_row_for(db, physician_id)
    if not domain_row:
        logger.info(
            "[dunning] _schedule_mailbox_purge: domain row gone (transferred out?) "
            "physician_id=%s domain=%s — skipping purge", physician_id, domain,
        )
        return

    if domain and local_part:
        try:
            await mailbox_provisioner.purge_pro_mailbox(domain, local_part)
            _log_workspace_audit(
                db, physician_id, "billing.mailbox_purged",
                resource=f"{local_part}@{domain}",
                detail={"domain": domain},
            )
        except Exception:
            logger.exception(
                "[dunning] purge_pro_mailbox failed domain=%s local_part=%s",
                domain, local_part,
            )

    _log_workspace_audit(
        db, physician_id, "billing.domain_released",
        resource=domain,
        detail={"hold_days": FROZEN_HOLD_DAYS},
    )
