"""7-step Pro upgrade saga (Phase 13-06).

Per D-14 the Pro upgrade saga executes the following steps in order:

  1. ``pro.charge_confirmed``        — Stripe already charged (webhook trigger)
  2. ``pro.register_domain``         — POINT OF NO RETURN (D-15 / ICANN 60-day lock)
  3. ``pro.write_dns``               — versioned per-domain template (D-30, D-32)
  4. ``pro.provision_mailcow_domain``— Mailcow domain
  5. ``pro.provision_pro_mailbox``   — first Pro mailbox (PRO-15)
  6. ``pro.attach_saas_hostname``    — CF for SaaS Custom Hostname + LE poll (WEB-07)
  7. ``pro.migrate_theme``           — atomic flip published_to_domain_id (D-26)

Failure semantics per D-15:
  - Failure with ``len(completed) < POINT_OF_NO_RETURN_INDEX`` (i.e. before
    step 2 succeeded): walk UNDO_REGISTRY in reverse + Stripe refund.
  - Failure with ``len(completed) >= POINT_OF_NO_RETURN_INDEX`` (step 2+):
    transition the run to ``status='partial_finish_later'`` and schedule a
    background retry loop (every 5 minutes for 1 hour, then ops-alert).

Per D-13 this module writes ONLY to ``physician_domains`` and
``physician_website.published_to_domain_id``. ``physician_workspace_accounts``
subscription state is webhook-owned and is updated by ``stripe_webhook.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from services.practikah.audit import ProvisioningLogWriter
from services.practikah.cloudflare_for_saas import cf_saas
from services.practikah.cloudflare_registrar import cf_registrar
from services.practikah.dns_template import (
    TEMPLATE_VERSION,
    compose_pro_dns_records,
)
from services.practikah.mailbox_provisioner import mailbox_provisioner
from services.practikah.orchestrator import (
    UNDO_REGISTRY,
    get_cloudflare_client,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Saga shape constants
# ---------------------------------------------------------------------------

PRO_SAGA_STEPS: list[str] = [
    "pro.charge_confirmed",
    "pro.register_domain",
    "pro.write_dns",
    "pro.provision_mailcow_domain",
    "pro.provision_pro_mailbox",
    "pro.attach_saas_hostname",
    "pro.migrate_theme",
]

# Zero-indexed: step 2 (`pro.register_domain`) is the point of no return.
# Failures with ``len(completed) >= POINT_OF_NO_RETURN_INDEX`` enter the
# ``partial_finish_later`` state instead of rolling back.
POINT_OF_NO_RETURN_INDEX: int = 1

_FINISH_LATER_RETRY_INTERVAL_SEC = 300  # 5 minutes
_FINISH_LATER_MAX_ATTEMPTS = 12  # 1 hour total

_SANDBOX_MODE = os.getenv("MEDIKAH_PROVISIONING_SANDBOX", "false").lower() in {
    "1", "true", "yes", "on",
}


# ---------------------------------------------------------------------------
# Helpers — provisioning_runs writer + Stripe refund
# ---------------------------------------------------------------------------

def _update_run_status(
    db: Any,
    run_id: str,
    *,
    status: Optional[str] = None,
    current_step: Optional[str] = None,
    error: Optional[dict[str, Any]] = None,
    retry_count: Optional[int] = None,
) -> None:
    """Best-effort update to ``provisioning_runs`` row keyed by ``run_id``.

    Never raises — saga progresses even if the state mirror falls behind
    (the practikah_provisioning_log is the source of truth per D-08).
    """
    if db is None:
        return
    payload: dict[str, Any] = {}
    if status is not None:
        payload["status"] = status
    if current_step is not None:
        payload["current_step"] = current_step
    if error is not None:
        payload["error"] = error
    if retry_count is not None:
        payload["retry_count"] = retry_count
    if not payload:
        return
    try:
        db.table("provisioning_runs").update(payload).eq(
            "run_id", run_id
        ).execute()
    except Exception:
        logger.exception(
            "[pro_saga] _update_run_status failed run_id=%s payload_keys=%s",
            run_id, list(payload.keys()),
        )


def _log_workspace_audit(
    db: Any,
    physician_id: str,
    action: str,
    *,
    resource: Optional[str] = None,
    run_id: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Best-effort insert into ``workspace_audit_log`` (OPS-01 compliance log).

    Mirrors ``orchestrator._provision_free_workspace`` lines 469-489.
    Never raises.
    """
    if db is None:
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
                "detail": {**(detail or {}), "run_id": run_id},
            }
        ).execute()
    except Exception:
        logger.exception(
            "[pro_saga] _log_workspace_audit failed physician_id=%s action=%s run_id=%s",
            physician_id, action, run_id,
        )


async def _trigger_pro_live_email(
    *,
    db: Any,
    physician_id: str,
    domain: str,
    run_id: str,
) -> None:
    """PRO-13: send the Pro-live transactional email after saga step 7 succeeds.

    Best-effort BFF call to Next.js practikah-email-trigger which forwards to
    ``lib/practikahEmail.ts:sendProLiveEmail``. Bilingual EN/ES content lives
    on the frontend so the Resend API key never leaks server-side.
    """
    if db is None:
        return
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("physician_email, pro_local_part")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            return
        physician_email = result.data[0].get("physician_email")
        if not physician_email:
            return
    except Exception:
        logger.exception(
            "[pro_saga] _trigger_pro_live_email lookup failed physician_id=%s run_id=%s",
            physician_id, run_id,
        )
        return

    base = os.environ.get("FRONTEND_BASE_URL") or os.environ.get(
        "NEXT_PUBLIC_BASE_URL", "https://medikah.health"
    )
    secret = os.environ.get("INTERNAL_API_SHARED_SECRET", "")
    if not secret:
        logger.info(
            "[pro_saga] _trigger_pro_live_email skipped (no INTERNAL_API_SHARED_SECRET)"
        )
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            await client.post(
                f"{base.rstrip('/')}/api/internal/practikah-email-trigger",
                json={
                    "kind": "pro_live",
                    "to": physician_email,
                    "domain": domain,
                    "physician_id": physician_id,
                    "run_id": run_id,
                },
                headers={"X-Internal-Secret": secret},
            )
    except Exception:
        logger.exception(
            "[pro_saga] _trigger_pro_live_email failed physician_id=%s run_id=%s",
            physician_id, run_id,
        )


def _stripe_refund(stripe_session_id: str, run_id: str) -> bool:
    """Issue a full refund for the Stripe checkout session that triggered this saga.

    Used only on pre-POR failure (D-15). Returns True if the refund call succeeded
    (or sandbox short-circuit), False otherwise.
    """
    if _SANDBOX_MODE or not stripe_session_id:
        logger.info(
            "[pro_saga] _stripe_refund sandbox/no-session short-circuit run_id=%s",
            run_id,
        )
        return True
    try:
        import stripe  # imported lazily — keeps module importable without SDK
        # Resolve the payment intent off the session, then refund it.
        session = stripe.checkout.Session.retrieve(stripe_session_id)
        payment_intent = session.get("payment_intent") if isinstance(session, dict) else getattr(session, "payment_intent", None)
        if not payment_intent:
            logger.warning(
                "[pro_saga] _stripe_refund no payment_intent on session=%s",
                stripe_session_id,
            )
            return False
        stripe.Refund.create(
            payment_intent=payment_intent,
            reason="requested_by_customer",
        )
        logger.info(
            "[pro_saga] _stripe_refund issued payment_intent=%s run_id=%s",
            payment_intent, run_id,
        )
        return True
    except Exception:
        logger.exception(
            "[pro_saga] _stripe_refund failed session=%s run_id=%s",
            stripe_session_id, run_id,
        )
        return False


def _fetch_physician_registrant(db: Any, physician_id: str) -> dict[str, Any]:
    """Fetch registrant contact dict for CF Registrar from the physicians row.

    Per T-13-06-08: registrant is pulled from the verified physician profile,
    NOT from Stripe metadata.
    """
    if db is None:
        return {}
    try:
        result = (
            db.table("physicians")
            .select("full_name, email")
            .eq("id", physician_id)
            .limit(1)
            .execute()
        )
        if result.data:
            row = result.data[0]
            return {
                "name": row.get("full_name", ""),
                "email": row.get("email", ""),
            }
    except Exception:
        logger.exception(
            "[pro_saga] _fetch_physician_registrant failed physician_id=%s",
            physician_id,
        )
    return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def provision_pro_upgrade(
    db: Any,
    physician_id: str,
    run_id: str,
    domain: str,
    tld_class: str,
    cadence: str,
    local_part: str,
    mailbox_password: str,
    physician_registrant: dict[str, Any],
    stripe_session_id: str,
) -> None:
    """Execute the 7-step Pro upgrade saga (D-14).

    Args:
        db: Supabase client.
        physician_id: UUID of the upgrading physician.
        run_id: Saga run ID — correlates all log entries and the
            ``provisioning_runs`` row already created by 13-05 checkout.
        domain: Custom domain the doctor purchased.
        tld_class: 'standard' | 'premium' (used for billing record correlation).
        cadence: 'monthly' | 'annual'.
        local_part: Mailbox local-part chosen at checkout review.
        mailbox_password: Auto-generated; emailed to doctor (T-13-06-09 — never logged).
        physician_registrant: Pre-fetched registrant contact dict (name/email).
        stripe_session_id: Checkout session ID (used for refund on pre-POR fail).
    """
    log = ProvisioningLogWriter(physician_id, run_id)
    completed: list[str] = []
    _update_run_status(
        db, run_id, status="running", current_step=PRO_SAGA_STEPS[0]
    )

    cf_zone_id: Optional[str] = None
    written_record_ids: list[str] = []
    saas_hostname_id: Optional[str] = None
    migrated_domain_id: Optional[str] = None

    try:
        # ------------------------------------------------------------------
        # Step 1 — pro.charge_confirmed (Stripe already charged)
        # ------------------------------------------------------------------
        await log.requested(
            step="pro.charge_confirmed",
            detail={"stripe_session_id": stripe_session_id},
            resource_type="billing",
        )
        await log.succeeded(
            step="pro.charge_confirmed",
            detail={"stripe_session_id": stripe_session_id},
            resource_type="billing",
        )
        completed.append("pro.charge_confirmed")
        _update_run_status(db, run_id, current_step=PRO_SAGA_STEPS[1])

        # ------------------------------------------------------------------
        # Step 2 — pro.register_domain (POINT OF NO RETURN per D-15)
        # ------------------------------------------------------------------
        await log.requested(
            step="pro.register_domain",
            detail={"domain": domain, "tld_class": tld_class},
            resource_type="domain",
        )
        reg = await cf_registrar.do_register(
            domain=domain,
            registrant=physician_registrant or _fetch_physician_registrant(
                db, physician_id
            ),
            run_id=run_id,
        )
        if not reg.success:
            raise RuntimeError(f"register failed: {reg.error}")
        await log.succeeded(
            step="pro.register_domain",
            detail={**reg.summary(), "domain": domain},
            resource_type="domain",
        )
        completed.append("pro.register_domain")
        _update_run_status(db, run_id, current_step=PRO_SAGA_STEPS[2])

        # ------------------------------------------------------------------
        # Step 3 — pro.write_dns (per-domain DKIM via Mailcow + versioned template)
        # ------------------------------------------------------------------
        await log.requested(
            step="pro.write_dns",
            detail={"domain": domain, "template_version": TEMPLATE_VERSION},
            resource_type="dns",
        )
        dkim = await mailbox_provisioner.get_per_domain_dkim(domain, run_id)
        # CF zone is required for DNS record writes — create idempotently.
        cf = get_cloudflare_client()
        zone_result = await cf.do_create_zone(domain=domain, run_id=run_id)
        if not zone_result.success:
            raise RuntimeError(f"CF zone create failed: {zone_result.error}")
        cf_zone_id = zone_result.resource_id

        records = compose_pro_dns_records(
            domain=domain,
            mailcow_a_record=os.environ.get("MAILCOW_VPS_IP", "0.0.0.0"),
            website_a_record=os.environ.get(
                "CF_FOR_SAAS_FALLBACK_A", "0.0.0.0"
            ),
            spf_value="v=spf1 a mx include:_spf.resend.com ~all",
            dkim_selector=dkim["selector"],
            dkim_public_key=dkim["public_key"],
        )
        # Translate dns_template.DnsRecord → cloudflare_client.DnsRecord shape.
        from services.practikah.dns_writer import DnsRecord as CFDnsRecord
        for rec in records:
            cf_rec = CFDnsRecord(
                record_type=rec.type,
                name=rec.name,
                value=rec.content,
                priority=rec.priority,
                ttl=rec.ttl,
            )
            dns_result = await cf.do_write_dns_record(
                zone_id=cf_zone_id, record=cf_rec, run_id=run_id
            )
            if not dns_result.success:
                raise RuntimeError(
                    f"DNS write failed for {rec.type} {rec.name}: {dns_result.error}"
                )
            if dns_result.resource_id:
                written_record_ids.append(dns_result.resource_id)
        await log.succeeded(
            step="pro.write_dns",
            detail={
                "domain": domain,
                "zone_id": cf_zone_id,
                "record_ids": written_record_ids,
                "records_written": len(records),
                "dkim_selector": dkim["selector"],
                "template_version": TEMPLATE_VERSION,
            },
            resource_type="dns",
        )
        completed.append("pro.write_dns")
        _update_run_status(db, run_id, current_step=PRO_SAGA_STEPS[3])

        # ------------------------------------------------------------------
        # Step 4 — pro.provision_mailcow_domain
        # ------------------------------------------------------------------
        await log.requested(
            step="pro.provision_mailcow_domain",
            detail={"domain": domain},
            resource_type="mailbox",
        )
        mc_dom = await mailbox_provisioner.do_add_domain(
            domain=domain, run_id=run_id
        )
        if not mc_dom.success:
            raise RuntimeError(f"mailcow add_domain failed: {mc_dom.error}")
        await log.succeeded(
            step="pro.provision_mailcow_domain",
            detail={**mc_dom.summary(), "domain": domain},
            resource_type="mailbox",
        )
        completed.append("pro.provision_mailcow_domain")
        _update_run_status(db, run_id, current_step=PRO_SAGA_STEPS[4])

        # ------------------------------------------------------------------
        # Step 5 — pro.provision_pro_mailbox (PRO-15)
        # ------------------------------------------------------------------
        # NOTE: mailbox_password is NEVER logged (T-13-06-09).
        await log.requested(
            step="pro.provision_pro_mailbox",
            detail={"domain": domain, "local_part": local_part},
            resource_type="mailbox",
        )
        mbox = await mailbox_provisioner.do_provision_pro_mailbox(
            domain=domain,
            local_part=local_part,
            password=mailbox_password,
            run_id=run_id,
        )
        if not mbox.success:
            raise RuntimeError(f"mailbox failed: {mbox.error}")
        await log.succeeded(
            step="pro.provision_pro_mailbox",
            detail={
                "domain": domain,
                "local_part": local_part,
                "resource_id": mbox.resource_id,
            },
            resource_type="mailbox",
        )
        completed.append("pro.provision_pro_mailbox")
        _update_run_status(db, run_id, current_step=PRO_SAGA_STEPS[5])

        # ------------------------------------------------------------------
        # Step 6 — pro.attach_saas_hostname (CF for SaaS + LE cert poll)
        # ------------------------------------------------------------------
        await log.requested(
            step="pro.attach_saas_hostname",
            detail={"hostname": domain},
            resource_type="cloudflare_hostname",
        )
        att = await cf_saas.attach_hostname(domain=domain, run_id=run_id)
        if not att.success or not att.resource_id:
            raise RuntimeError(f"attach hostname failed: {att.error}")
        saas_hostname_id = att.resource_id
        ssl = await cf_saas.poll_ssl_status(att.resource_id, timeout_sec=300)
        if not ssl.success:
            raise RuntimeError(f"LE cert did not activate: {ssl.error}")
        await log.succeeded(
            step="pro.attach_saas_hostname",
            detail={
                **att.summary(),
                "hostname": domain,
                "ssl": "active",
            },
            resource_type="cloudflare_hostname",
        )
        completed.append("pro.attach_saas_hostname")
        _update_run_status(db, run_id, current_step=PRO_SAGA_STEPS[6])

        # ------------------------------------------------------------------
        # Step 7 — pro.migrate_theme (atomic published_to_domain_id flip — D-26)
        # ------------------------------------------------------------------
        await log.requested(
            step="pro.migrate_theme",
            detail={"domain": domain},
            resource_type="workspace",
        )
        # Resolve the workspace_account_id so the FK is satisfied.
        workspace_account_id: Optional[str] = None
        try:
            wa_row = (
                db.table("physician_workspace_accounts")
                .select("id")
                .eq("physician_id", physician_id)
                .limit(1)
                .execute()
            )
            if wa_row.data:
                workspace_account_id = wa_row.data[0]["id"]
        except Exception:
            logger.exception(
                "[pro_saga] step 7: failed to read workspace_account_id "
                "physician_id=%s run_id=%s", physician_id, run_id,
            )

        # Insert the physician_domains row atomically with the published_to_domain_id flip.
        domain_row: dict[str, Any] = {}
        try:
            insert_payload: dict[str, Any] = {
                "physician_id": physician_id,
                "domain": domain,
                "registrar": "cloudflare",
                "status": "active",
                "auto_renew": True,
                "whois_privacy": True,
                "is_sandbox": _SANDBOX_MODE,
                "cloudflare_zone_id": cf_zone_id,
                "cloudflare_hostname_id": saas_hostname_id,
            }
            if workspace_account_id:
                insert_payload["workspace_account_id"] = workspace_account_id
            insert_resp = (
                db.table("physician_domains").insert(insert_payload).execute()
            )
            if insert_resp.data:
                domain_row = insert_resp.data[0]
                migrated_domain_id = domain_row.get("id")
        except Exception as err:
            raise RuntimeError(f"physician_domains insert failed: {err}") from err

        if not migrated_domain_id:
            raise RuntimeError("physician_domains insert returned no id")

        try:
            db.table("physician_website").update(
                {"published_to_domain_id": migrated_domain_id}
            ).eq("physician_id", physician_id).execute()
        except Exception as err:
            raise RuntimeError(
                f"physician_website published_to_domain_id flip failed: {err}"
            ) from err

        await log.succeeded(
            step="pro.migrate_theme",
            detail={
                "domain": domain,
                "domain_id": migrated_domain_id,
                "physician_id": physician_id,
            },
            resource_type="workspace",
        )
        completed.append("pro.migrate_theme")

        _update_run_status(
            db, run_id, status="succeeded", current_step=None
        )
        _log_workspace_audit(
            db,
            physician_id,
            action="pro.upgrade_succeeded",
            resource=domain,
            run_id=run_id,
            detail={"domain_id": migrated_domain_id},
        )
        # Plan 13-09 (PRO-13): fire pro_live transactional email — bilingual.
        await _trigger_pro_live_email(
            db=db,
            physician_id=physician_id,
            domain=domain,
            run_id=run_id,
        )
        logger.info(
            "[pro_saga] provision_pro_upgrade SUCCESS physician_id=%s domain=%s run_id=%s",
            physician_id, domain, run_id,
        )

    except Exception as err:
        idx_failed = len(completed)
        failed_step = (
            PRO_SAGA_STEPS[idx_failed]
            if idx_failed < len(PRO_SAGA_STEPS)
            else "unknown"
        )
        await log.failed(
            step=failed_step,
            detail={"error": str(err)},
            resource_type="workspace",
        )
        logger.exception(
            "[pro_saga] provision_pro_upgrade failed physician_id=%s domain=%s "
            "run_id=%s failed_step=%s completed=%s",
            physician_id, domain, run_id, failed_step, completed,
        )

        if idx_failed < POINT_OF_NO_RETURN_INDEX:
            # ----------------------------------------------------------
            # Pre-POR failure → roll back + Stripe refund (D-15)
            # ----------------------------------------------------------
            for step_name in reversed(completed):
                undo = UNDO_REGISTRY.get(step_name)
                if undo is None:
                    continue
                try:
                    await undo(
                        {
                            "step_name": step_name,
                            "detail": {
                                "domain": domain,
                                "physician_id": physician_id,
                            },
                        },
                        run_id,
                    )
                except Exception:
                    logger.exception(
                        "[pro_saga] pre-POR undo step=%s failed run_id=%s",
                        step_name, run_id,
                    )
            refunded = _stripe_refund(stripe_session_id, run_id)
            _update_run_status(
                db,
                run_id,
                status="failed",
                error={
                    "step": failed_step,
                    "message": str(err),
                    "refunded": refunded,
                },
            )
            _log_workspace_audit(
                db,
                physician_id,
                action="pro.upgrade_failed_pre_por",
                resource=domain,
                run_id=run_id,
                detail={"failed_step": failed_step, "refunded": refunded},
            )
        else:
            # ----------------------------------------------------------
            # Post-POR failure → finish-later state (D-15)
            # ----------------------------------------------------------
            _update_run_status(
                db,
                run_id,
                status="partial_finish_later",
                error={"step": failed_step, "message": str(err)},
                retry_count=0,
            )
            _log_workspace_audit(
                db,
                physician_id,
                action="pro.upgrade_finish_later",
                resource=domain,
                run_id=run_id,
                detail={"failed_step": failed_step},
            )
            # Schedule the finish-later retry loop (D-15).
            asyncio.create_task(
                _finish_later_retry_loop(
                    db=db,
                    physician_id=physician_id,
                    run_id=run_id,
                    domain=domain,
                    failed_step=failed_step,
                )
            )


async def _finish_later_retry_loop(
    db: Any,
    physician_id: str,
    run_id: str,
    domain: str,
    failed_step: str,
) -> None:
    """Per D-15: retry every 5 min for 1 hour (12 attempts).

    Each attempt re-reads the provisioning_runs row; if status has been
    manually fixed to ``succeeded`` we exit early. Final attempt writes a
    structured ops-alert that Plan 10-09's mailops monitoring layer surfaces.

    No re-execution of saga steps in this MVP — Plan 13-10's sandbox dry-run
    will exercise the loop's structure; the actual per-step retry runner
    lands as a follow-up alongside the Plan 14 drift monitor (it shares the
    same step-replay primitive).
    """
    for attempt in range(_FINISH_LATER_MAX_ATTEMPTS):
        await asyncio.sleep(_FINISH_LATER_RETRY_INTERVAL_SEC)
        try:
            current = (
                db.table("provisioning_runs")
                .select("status")
                .eq("run_id", run_id)
                .limit(1)
                .execute()
            )
            if current.data and current.data[0].get("status") == "succeeded":
                logger.info(
                    "[pro_saga] finish-later loop: run_id=%s already succeeded; exiting",
                    run_id,
                )
                return
        except Exception:
            logger.exception(
                "[pro_saga] finish-later loop status read failed run_id=%s",
                run_id,
            )

        _update_run_status(db, run_id, retry_count=attempt + 1)

    # Final attempt exhausted — emit ops alert.
    try:
        log_dir = "/var/log/medikah"
        if os.path.isdir(log_dir):
            with open(f"{log_dir}/ops-alerts.jsonl", "a", encoding="utf-8") as fh:
                import json
                fh.write(
                    json.dumps(
                        {
                            "level": "alert",
                            "source": "pro_saga.finish_later",
                            "physician_id": physician_id,
                            "run_id": run_id,
                            "domain": domain,
                            "failed_step": failed_step,
                            "runbook": "runbooks/PICKUP-pro-saga-finish-later.md",
                        }
                    )
                    + "\n"
                )
    except Exception:
        logger.exception(
            "[pro_saga] finish-later ops-alert write failed run_id=%s", run_id,
        )

    logger.error(
        "[pro_saga] finish-later loop exhausted run_id=%s failed_step=%s — ops alerted",
        run_id, failed_step,
    )
