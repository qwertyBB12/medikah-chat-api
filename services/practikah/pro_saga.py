"""8-step Pro upgrade saga with step-replay resume (Phase 13-06 + step-replay slice).

Per D-14 the Pro upgrade saga executes the following steps in order:

  1. ``pro.charge_confirmed``        — Stripe already charged (webhook trigger)
  2. ``pro.register_domain``         — POINT OF NO RETURN (D-15 / ICANN 60-day lock)
  3. ``pro.write_dns``               — versioned per-domain template (D-30, D-32)
  4. ``pro.provision_mailcow_domain``— Mailcow domain
  5. ``pro.provision_pro_mailbox``   — first Pro mailbox (PRO-15)
  6. ``pro.attach_saas_hostname``    — CF for SaaS Custom Hostname + LE poll (WEB-07)
  7. ``pro.migrate_theme``           — atomic flip published_to_domain_id (D-26)
  8. ``pro.verify_live``             — HTTPS GET on the doctor's domain (step-replay slice)

Failure semantics per D-15:
  - Failure with ``len(completed) < POINT_OF_NO_RETURN_INDEX`` (i.e. before
    step 2 succeeded): walk UNDO_REGISTRY in reverse + Stripe refund.
  - Failure with ``len(completed) >= POINT_OF_NO_RETURN_INDEX`` (step 2+):
    transition the run to ``status='partial_finish_later'`` and schedule a
    background retry loop (every 5 minutes for 1 hour, then ops-alert).

Step-replay resume (this slice): every step's outputs are persisted in
``practikah_provisioning_log.detail``, so ``resume_pro_run`` re-hydrates a
stranded post-POR run from the log (no schema migration) and re-executes
from the first incomplete step. The finish-later loop now calls it on each
attempt, and ``rearm_finish_later_runs`` re-arms the loop on startup (the
loop is an in-process asyncio task and dies on every deploy). Pre-POR runs
(no ``pro.register_domain`` succeeded row) are NEVER resumed — they were
undone + refunded.

Per D-13 this module writes ONLY to ``physician_domains`` and
``physician_website.published_to_domain_id``. ``physician_workspace_accounts``
subscription state is webhook-owned and is updated by ``stripe_webhook.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

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
    "pro.verify_live",
]

# Zero-indexed: step 2 (`pro.register_domain`) is the point of no return.
# Failures with ``len(completed) >= POINT_OF_NO_RETURN_INDEX`` enter the
# ``partial_finish_later`` state instead of rolling back.
POINT_OF_NO_RETURN_INDEX: int = 1

_FINISH_LATER_RETRY_INTERVAL_SEC = 300  # 5 minutes
_FINISH_LATER_MAX_ATTEMPTS = 12  # 1 hour total

_VERIFY_LIVE_ATTEMPTS = 3
_VERIFY_LIVE_RETRY_DELAY_SEC = 10

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
    clear_progress: bool = False,
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
    if clear_progress:
        # Terminal success: a stale error envelope / current_step from an
        # earlier failed attempt misleads ops (proven in the day-5 proof).
        payload["current_step"] = None
        payload["error"] = None
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
        # workspace_audit_log.resource_id is a UUID column; pro-saga resources
        # are usually domain names. Non-UUID resources ride in detail instead —
        # otherwise Postgres rejects the row and the audit event silently drops
        # (proven live in the day-5 sandbox resume proof).
        payload_detail = {**(detail or {}), "run_id": run_id}
        resource_id: Optional[str] = None
        if resource:
            try:
                from uuid import UUID
                UUID(str(resource))
                resource_id = resource
            except ValueError:
                payload_detail["resource"] = resource
        db.table("workspace_audit_log").insert(
            {
                "physician_id": physician_id,
                "actor_id": physician_id,
                "actor_role": "system",
                "action": action,
                "resource_type": "workspace",
                "resource_id": resource_id,
                "detail": payload_detail,
            }
        ).execute()
    except Exception:
        logger.exception(
            "[pro_saga] _log_workspace_audit failed physician_id=%s action=%s run_id=%s",
            physician_id, action, run_id,
        )


async def _trigger_practikah_email(
    *,
    db: Any,
    kind: str,
    physician_id: str,
    domain: str,
    run_id: str,
) -> None:
    """Best-effort BFF call to the Next.js practikah-email-trigger.

    Bilingual content + the Resend send live in ``lib/practikahEmail.ts``
    (frontend) so the Resend API key never leaks server-side. Looks up the
    recipient off ``physician_workspace_accounts.physician_email``.
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
            "[pro_saga] _trigger_practikah_email lookup failed kind=%s physician_id=%s run_id=%s",
            kind, physician_id, run_id,
        )
        return

    base = os.environ.get("FRONTEND_BASE_URL") or os.environ.get(
        "NEXT_PUBLIC_BASE_URL", "https://medikah.health"
    )
    secret = os.environ.get("INTERNAL_API_SHARED_SECRET", "")
    if not secret:
        logger.info(
            "[pro_saga] _trigger_practikah_email skipped (no INTERNAL_API_SHARED_SECRET) kind=%s",
            kind,
        )
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            await client.post(
                f"{base.rstrip('/')}/api/internal/practikah-email-trigger",
                json={
                    "kind": kind,
                    "to": physician_email,
                    "domain": domain,
                    "physician_id": physician_id,
                    "run_id": run_id,
                },
                headers={"X-Internal-Secret": secret},
            )
    except Exception:
        logger.exception(
            "[pro_saga] _trigger_practikah_email failed kind=%s physician_id=%s run_id=%s",
            kind, physician_id, run_id,
        )


async def _trigger_pro_live_email(
    *,
    db: Any,
    physician_id: str,
    domain: str,
    run_id: str,
) -> None:
    """PRO-13: send the Pro-live transactional email after the saga succeeds."""
    await _trigger_practikah_email(
        db=db, kind="pro_live", physician_id=physician_id,
        domain=domain, run_id=run_id,
    )


async def _trigger_pro_stalled_email(
    *,
    db: Any,
    physician_id: str,
    domain: str,
    run_id: str,
) -> None:
    """Step-replay slice: honest 'setup is delayed' email on retry exhaustion.

    Fired exactly once, when the finish-later loop exhausts its 12 attempts —
    the doctor's payment is safe, the domain is theirs, and ops has been
    alerted; the email says so plainly with no compliance claims.
    """
    await _trigger_practikah_email(
        db=db, kind="pro_stalled", physician_id=physician_id,
        domain=domain, run_id=run_id,
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
# Saga context + step runners (step-replay slice)
# ---------------------------------------------------------------------------

@dataclass
class ProSagaContext:
    """Everything one saga execution needs, shared across step runners.

    ``completed`` carries the hydrated prefix on resume, so the failure
    handler's ``len(completed)`` index math is identical for fresh runs and
    replays. ``state`` carries cross-step resource ids (zone/record/hostname/
    domain ids) — hydrated from the provisioning log on resume.
    ``spawn_finish_later`` is False when the finish-later loop itself is the
    caller, so a failed retry never spawns a nested loop.
    """

    db: Any
    physician_id: str
    run_id: str
    domain: str
    tld_class: str
    cadence: str
    local_part: str
    mailbox_password: str
    physician_registrant: dict[str, Any]
    stripe_session_id: str
    log: ProvisioningLogWriter
    completed: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    spawn_finish_later: bool = True


async def _step_charge_confirmed(ctx: ProSagaContext) -> None:
    """Step 1 — pro.charge_confirmed (Stripe already charged)."""
    await ctx.log.requested(
        step="pro.charge_confirmed",
        detail={"stripe_session_id": ctx.stripe_session_id},
        resource_type="billing",
    )
    await ctx.log.succeeded(
        step="pro.charge_confirmed",
        detail={"stripe_session_id": ctx.stripe_session_id},
        resource_type="billing",
    )


async def _step_register_domain(ctx: ProSagaContext) -> None:
    """Step 2 — pro.register_domain (POINT OF NO RETURN per D-15).

    Replay-safe: cf_registrar sends X-Idempotency-Key and maps
    409 already_registered_to_account → success.
    """
    await ctx.log.requested(
        step="pro.register_domain",
        detail={"domain": ctx.domain, "tld_class": ctx.tld_class},
        resource_type="domain",
    )
    reg = await cf_registrar.do_register(
        domain=ctx.domain,
        registrant=ctx.physician_registrant or _fetch_physician_registrant(
            ctx.db, ctx.physician_id
        ),
        run_id=ctx.run_id,
    )
    if not reg.success:
        raise RuntimeError(f"register failed: {reg.error}")
    await ctx.log.succeeded(
        step="pro.register_domain",
        detail={**reg.summary(), "domain": ctx.domain},
        resource_type="domain",
    )


async def _step_write_dns(ctx: ProSagaContext) -> None:
    """Step 3 — pro.write_dns (per-domain DKIM via Mailcow + versioned template).

    Replay-safe: CF zone create and record writes are upserts by name+type.
    """
    await ctx.log.requested(
        step="pro.write_dns",
        detail={"domain": ctx.domain, "template_version": TEMPLATE_VERSION},
        resource_type="dns",
    )
    dkim = await mailbox_provisioner.get_per_domain_dkim(ctx.domain, ctx.run_id)
    # CF zone is required for DNS record writes — create idempotently.
    cf = get_cloudflare_client()
    zone_result = await cf.do_create_zone(domain=ctx.domain, run_id=ctx.run_id)
    if not zone_result.success:
        raise RuntimeError(f"CF zone create failed: {zone_result.error}")
    ctx.state["cf_zone_id"] = zone_result.resource_id

    records = compose_pro_dns_records(
        domain=ctx.domain,
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
    written_record_ids: list[str] = ctx.state.setdefault("written_record_ids", [])
    for rec in records:
        cf_rec = CFDnsRecord(
            record_type=rec.type,
            name=rec.name,
            value=rec.content,
            priority=rec.priority,
            ttl=rec.ttl,
        )
        dns_result = await cf.do_write_dns_record(
            zone_id=ctx.state["cf_zone_id"], record=cf_rec, run_id=ctx.run_id
        )
        if not dns_result.success:
            raise RuntimeError(
                f"DNS write failed for {rec.type} {rec.name}: {dns_result.error}"
            )
        if dns_result.resource_id:
            written_record_ids.append(dns_result.resource_id)
    await ctx.log.succeeded(
        step="pro.write_dns",
        detail={
            "domain": ctx.domain,
            "zone_id": ctx.state["cf_zone_id"],
            "record_ids": written_record_ids,
            "records_written": len(records),
            "dkim_selector": dkim["selector"],
            "template_version": TEMPLATE_VERSION,
        },
        resource_type="dns",
    )


async def _step_provision_mailcow_domain(ctx: ProSagaContext) -> None:
    """Step 4 — pro.provision_mailcow_domain.

    Replay-safe: do_add_domain GETs before POST and returns success for an
    existing domain.
    """
    await ctx.log.requested(
        step="pro.provision_mailcow_domain",
        detail={"domain": ctx.domain},
        resource_type="mailbox",
    )
    mc_dom = await mailbox_provisioner.do_add_domain(
        domain=ctx.domain, run_id=ctx.run_id
    )
    if not mc_dom.success:
        raise RuntimeError(f"mailcow add_domain failed: {mc_dom.error}")
    await ctx.log.succeeded(
        step="pro.provision_mailcow_domain",
        detail={**mc_dom.summary(), "domain": ctx.domain},
        resource_type="mailbox",
    )


async def _step_provision_pro_mailbox(ctx: ProSagaContext) -> None:
    """Step 5 — pro.provision_pro_mailbox (PRO-15).

    NOTE: mailbox_password is NEVER logged (T-13-06-09). Replay-safe:
    do_add_mailbox GETs before POST; if a prior partial attempt created the
    mailbox, its original password stands (doctor access is via portal SSO).
    """
    await ctx.log.requested(
        step="pro.provision_pro_mailbox",
        detail={"domain": ctx.domain, "local_part": ctx.local_part},
        resource_type="mailbox",
    )
    mbox = await mailbox_provisioner.do_provision_pro_mailbox(
        domain=ctx.domain,
        local_part=ctx.local_part,
        password=ctx.mailbox_password,
        run_id=ctx.run_id,
    )
    if not mbox.success:
        raise RuntimeError(f"mailbox failed: {mbox.error}")
    await ctx.log.succeeded(
        step="pro.provision_pro_mailbox",
        detail={
            "domain": ctx.domain,
            "local_part": ctx.local_part,
            "resource_id": mbox.resource_id,
        },
        resource_type="mailbox",
    )


async def _step_attach_saas_hostname(ctx: ProSagaContext) -> None:
    """Step 6 — pro.attach_saas_hostname (CF for SaaS + LE cert poll).

    Replay hardening: a 409 already-attached response may omit the hostname
    id — fall back to the id hydrated from a prior succeeded log row.
    """
    await ctx.log.requested(
        step="pro.attach_saas_hostname",
        detail={"hostname": ctx.domain},
        resource_type="cloudflare_hostname",
    )
    att = await cf_saas.attach_hostname(domain=ctx.domain, run_id=ctx.run_id)
    if not att.success:
        raise RuntimeError(f"attach hostname failed: {att.error}")
    hostname_id = att.resource_id or ctx.state.get("saas_hostname_id")
    if not hostname_id:
        raise RuntimeError(
            f"attach hostname returned no id for {ctx.domain} and none hydrated from log"
        )
    ctx.state["saas_hostname_id"] = hostname_id
    ssl = await cf_saas.poll_ssl_status(hostname_id, timeout_sec=300)
    if not ssl.success:
        raise RuntimeError(f"LE cert did not activate: {ssl.error}")
    await ctx.log.succeeded(
        step="pro.attach_saas_hostname",
        detail={
            **att.summary(),
            "resource_id": hostname_id,
            "hostname": ctx.domain,
            "ssl": "active",
        },
        resource_type="cloudflare_hostname",
    )


async def _step_migrate_theme(ctx: ProSagaContext) -> None:
    """Step 7 — pro.migrate_theme (atomic published_to_domain_id flip — D-26).

    Replay hardening: physician_domains is insert-not-upsert, so a prior
    partial attempt may have inserted the row already — select before insert.
    """
    await ctx.log.requested(
        step="pro.migrate_theme",
        detail={"domain": ctx.domain},
        resource_type="workspace",
    )
    db = ctx.db
    # Resolve the workspace_account_id so the FK is satisfied.
    workspace_account_id: Optional[str] = None
    try:
        wa_row = (
            db.table("physician_workspace_accounts")
            .select("id")
            .eq("physician_id", ctx.physician_id)
            .limit(1)
            .execute()
        )
        if wa_row.data:
            workspace_account_id = wa_row.data[0]["id"]
    except Exception:
        logger.exception(
            "[pro_saga] step 7: failed to read workspace_account_id "
            "physician_id=%s run_id=%s", ctx.physician_id, ctx.run_id,
        )

    migrated_domain_id: Optional[str] = None
    try:
        existing = (
            db.table("physician_domains")
            .select("id")
            .eq("physician_id", ctx.physician_id)
            .eq("domain", ctx.domain)
            .limit(1)
            .execute()
        )
        if existing.data:
            migrated_domain_id = existing.data[0].get("id")
    except Exception:
        logger.exception(
            "[pro_saga] step 7: physician_domains pre-check failed "
            "physician_id=%s run_id=%s", ctx.physician_id, ctx.run_id,
        )

    if not migrated_domain_id:
        try:
            insert_payload: dict[str, Any] = {
                "physician_id": ctx.physician_id,
                "domain": ctx.domain,
                "registrar": "cloudflare",
                "status": "active",
                "auto_renew": True,
                "whois_privacy": True,
                "is_sandbox": _SANDBOX_MODE,
                "cloudflare_zone_id": ctx.state.get("cf_zone_id"),
                "cloudflare_hostname_id": ctx.state.get("saas_hostname_id"),
            }
            if workspace_account_id:
                insert_payload["workspace_account_id"] = workspace_account_id
            insert_resp = (
                db.table("physician_domains").insert(insert_payload).execute()
            )
            if insert_resp.data:
                migrated_domain_id = insert_resp.data[0].get("id")
        except Exception as err:
            raise RuntimeError(f"physician_domains insert failed: {err}") from err

    if not migrated_domain_id:
        raise RuntimeError("physician_domains insert returned no id")

    try:
        db.table("physician_website").update(
            {"published_to_domain_id": migrated_domain_id}
        ).eq("physician_id", ctx.physician_id).execute()
    except Exception as err:
        raise RuntimeError(
            f"physician_website published_to_domain_id flip failed: {err}"
        ) from err

    ctx.state["migrated_domain_id"] = migrated_domain_id
    await ctx.log.succeeded(
        step="pro.migrate_theme",
        detail={
            "domain": ctx.domain,
            "domain_id": migrated_domain_id,
            "physician_id": ctx.physician_id,
        },
        resource_type="workspace",
    )


async def _verify_live_once(domain: str) -> int:
    """One HTTPS GET against the doctor's new domain (TLS verification ON).

    Returns the status code; raises on transport/TLS errors. Factored out so
    tests can monkeypatch the network call.
    """
    import httpx
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=True
    ) as client:
        resp = await client.get(f"https://{domain}")
        return resp.status_code


async def _step_verify_live(ctx: ProSagaContext) -> None:
    """Step 8 — pro.verify_live: prove the doctor promise end-to-end.

    Any status < 500 counts — a 401/404 still proves DNS + TLS + routing all
    resolve to us. Failure lands the run in partial_finish_later, where
    DNS/cert propagation delays get retried naturally by the finish-later
    loop instead of being declared success blind.
    """
    await ctx.log.requested(
        step="pro.verify_live",
        detail={"domain": ctx.domain},
        resource_type="workspace",
    )
    if _SANDBOX_MODE:
        # Sandbox domains never resolve publicly — short-circuit like every
        # other adapter does in sandbox mode.
        await ctx.log.succeeded(
            step="pro.verify_live",
            detail={"domain": ctx.domain, "sandbox": True},
            resource_type="workspace",
        )
        return
    last_error: Optional[str] = None
    for attempt in range(_VERIFY_LIVE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_VERIFY_LIVE_RETRY_DELAY_SEC)
        try:
            status_code = await _verify_live_once(ctx.domain)
            if status_code < 500:
                await ctx.log.succeeded(
                    step="pro.verify_live",
                    detail={
                        "domain": ctx.domain,
                        "status_code": status_code,
                        "attempts": attempt + 1,
                    },
                    resource_type="workspace",
                )
                return
            last_error = f"HTTP {status_code}"
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(
        f"verify_live failed after {_VERIFY_LIVE_ATTEMPTS} attempts: {last_error}"
    )


STEP_RUNNERS: dict[str, Callable[[ProSagaContext], Awaitable[None]]] = {
    "pro.charge_confirmed": _step_charge_confirmed,
    "pro.register_domain": _step_register_domain,
    "pro.write_dns": _step_write_dns,
    "pro.provision_mailcow_domain": _step_provision_mailcow_domain,
    "pro.provision_pro_mailbox": _step_provision_pro_mailbox,
    "pro.attach_saas_hostname": _step_attach_saas_hostname,
    "pro.migrate_theme": _step_migrate_theme,
    "pro.verify_live": _step_verify_live,
}


# ---------------------------------------------------------------------------
# Execution engine (shared by fresh dispatch and resume)
# ---------------------------------------------------------------------------

async def _execute_from(ctx: ProSagaContext, start_index: int) -> bool:
    """Run PRO_SAGA_STEPS from ``start_index``; returns True on full success.

    Failure semantics are D-15 verbatim: pre-POR → undo + refund; post-POR →
    partial_finish_later (+ finish-later loop iff ctx.spawn_finish_later).
    """
    first_step = (
        PRO_SAGA_STEPS[start_index]
        if start_index < len(PRO_SAGA_STEPS)
        else None
    )
    _update_run_status(
        ctx.db, ctx.run_id, status="running", current_step=first_step
    )

    try:
        for idx in range(start_index, len(PRO_SAGA_STEPS)):
            step_name = PRO_SAGA_STEPS[idx]
            await STEP_RUNNERS[step_name](ctx)
            ctx.completed.append(step_name)
            if idx + 1 < len(PRO_SAGA_STEPS):
                _update_run_status(
                    ctx.db, ctx.run_id, current_step=PRO_SAGA_STEPS[idx + 1]
                )

        _update_run_status(
            ctx.db, ctx.run_id, status="succeeded", clear_progress=True
        )
        _log_workspace_audit(
            ctx.db,
            ctx.physician_id,
            action="pro.upgrade_succeeded",
            resource=ctx.domain,
            run_id=ctx.run_id,
            detail={"domain_id": ctx.state.get("migrated_domain_id")},
        )
        # Plan 13-09 (PRO-13): fire pro_live transactional email — bilingual.
        await _trigger_pro_live_email(
            db=ctx.db,
            physician_id=ctx.physician_id,
            domain=ctx.domain,
            run_id=ctx.run_id,
        )
        logger.info(
            "[pro_saga] provision_pro_upgrade SUCCESS physician_id=%s domain=%s run_id=%s",
            ctx.physician_id, ctx.domain, ctx.run_id,
        )
        return True

    except Exception as err:
        idx_failed = len(ctx.completed)
        failed_step = (
            PRO_SAGA_STEPS[idx_failed]
            if idx_failed < len(PRO_SAGA_STEPS)
            else "unknown"
        )
        await ctx.log.failed(
            step=failed_step,
            detail={"error": str(err)},
            resource_type="workspace",
        )
        logger.exception(
            "[pro_saga] provision_pro_upgrade failed physician_id=%s domain=%s "
            "run_id=%s failed_step=%s completed=%s",
            ctx.physician_id, ctx.domain, ctx.run_id, failed_step, ctx.completed,
        )

        if idx_failed < POINT_OF_NO_RETURN_INDEX:
            # ----------------------------------------------------------
            # Pre-POR failure → roll back + Stripe refund (D-15)
            # ----------------------------------------------------------
            for step_name in reversed(ctx.completed):
                undo = UNDO_REGISTRY.get(step_name)
                if undo is None:
                    continue
                try:
                    await undo(
                        {
                            "step_name": step_name,
                            "detail": {
                                "domain": ctx.domain,
                                "physician_id": ctx.physician_id,
                            },
                        },
                        ctx.run_id,
                    )
                except Exception:
                    logger.exception(
                        "[pro_saga] pre-POR undo step=%s failed run_id=%s",
                        step_name, ctx.run_id,
                    )
            refunded = _stripe_refund(ctx.stripe_session_id, ctx.run_id)
            _update_run_status(
                ctx.db,
                ctx.run_id,
                status="failed",
                error={
                    "step": failed_step,
                    "message": str(err),
                    "refunded": refunded,
                },
            )
            _log_workspace_audit(
                ctx.db,
                ctx.physician_id,
                action="pro.upgrade_failed_pre_por",
                resource=ctx.domain,
                run_id=ctx.run_id,
                detail={"failed_step": failed_step, "refunded": refunded},
            )
        else:
            # ----------------------------------------------------------
            # Post-POR failure → finish-later state (D-15)
            # ----------------------------------------------------------
            # retry_count is only reset on the ORIGINAL dispatch; when the
            # finish-later loop is the caller it owns the counter (so a
            # failed retry can't reset the budget and loop forever).
            _update_run_status(
                ctx.db,
                ctx.run_id,
                status="partial_finish_later",
                error={"step": failed_step, "message": str(err)},
                retry_count=0 if ctx.spawn_finish_later else None,
            )
            _log_workspace_audit(
                ctx.db,
                ctx.physician_id,
                action="pro.upgrade_finish_later",
                resource=ctx.domain,
                run_id=ctx.run_id,
                detail={"failed_step": failed_step},
            )
            if ctx.spawn_finish_later:
                # Schedule the finish-later retry loop (D-15).
                asyncio.create_task(
                    _finish_later_retry_loop(
                        db=ctx.db,
                        physician_id=ctx.physician_id,
                        run_id=ctx.run_id,
                        domain=ctx.domain,
                        failed_step=failed_step,
                    )
                )
        return False


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
    """Execute the 8-step Pro upgrade saga (D-14).

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
    ctx = ProSagaContext(
        db=db,
        physician_id=physician_id,
        run_id=run_id,
        domain=domain,
        tld_class=tld_class,
        cadence=cadence,
        local_part=local_part,
        mailbox_password=mailbox_password,
        physician_registrant=physician_registrant,
        stripe_session_id=stripe_session_id,
        log=ProvisioningLogWriter(physician_id, run_id),
        spawn_finish_later=True,
    )
    await _execute_from(ctx, 0)


# ---------------------------------------------------------------------------
# Step-replay resume (this slice)
# ---------------------------------------------------------------------------

@dataclass
class _ResumePlan:
    ctx: ProSagaContext
    start_index: int


async def _load_resume_plan(
    db: Any, run_id: str, *, spawn_finish_later: bool
) -> tuple[Optional[_ResumePlan], str]:
    """Guard + hydrate a resume; returns (plan, "") or (None, refusal_reason).

    Guards: saga_type='pro_upgrade'; status ∈ {partial_finish_later, failed};
    'pro.register_domain' has a succeeded log row (post-POR proof — pre-POR
    runs were undone + refunded and are NEVER resumed).
    """
    if db is None:
        return None, "db_unavailable"
    try:
        res = (
            db.table("provisioning_runs")
            .select(
                "run_id, physician_id, saga_type, status, domain_name, "
                "stripe_session_id, retry_count"
            )
            .eq("run_id", run_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
    except Exception:
        logger.exception(
            "[pro_saga] _load_resume_plan run lookup failed run_id=%s", run_id
        )
        return None, "run_lookup_failed"
    if not rows:
        return None, "run_not_found"
    row = rows[0]
    if row.get("saga_type") != "pro_upgrade":
        return None, "not_pro_saga"
    status = row.get("status")
    if status not in {"partial_finish_later", "failed"}:
        return None, f"status_{status}_not_resumable"
    physician_id = str(row.get("physician_id") or "")
    domain = str(row.get("domain_name") or "")
    if not physician_id or not domain:
        return None, "run_missing_identity"

    # Hydrate the completed prefix + step outputs from the provisioning log —
    # the log persists every step's output ids (D-08: log is source of truth).
    try:
        log_res = (
            db.table("practikah_provisioning_log")
            .select("step_name, detail, recorded_at")
            .eq("run_id", run_id)
            .eq("event", "succeeded")
            .order("recorded_at", desc=False)
            .execute()
        )
        succeeded_rows = list(log_res.data or [])
    except Exception:
        logger.exception(
            "[pro_saga] _load_resume_plan log read failed run_id=%s", run_id
        )
        return None, "log_read_failed"

    succeeded_steps = {r.get("step_name") for r in succeeded_rows}
    if "pro.register_domain" not in succeeded_steps:
        return None, "pre_por_not_resumable"

    completed: list[str] = []
    for step in PRO_SAGA_STEPS:
        if step in succeeded_steps:
            completed.append(step)
        else:
            break
    start_index = len(completed)

    state: dict[str, Any] = {}
    for r in succeeded_rows:
        detail = r.get("detail") or {}
        name = r.get("step_name")
        if name == "pro.write_dns":
            state["cf_zone_id"] = detail.get("zone_id")
            state["written_record_ids"] = list(detail.get("record_ids") or [])
        elif name == "pro.attach_saas_hostname":
            state["saas_hostname_id"] = detail.get("resource_id")
        elif name == "pro.migrate_theme":
            state["migrated_domain_id"] = detail.get("domain_id")

    # Rebuild dispatch inputs — checkout metadata is gone; the run row +
    # workspace account carry what a replay needs.
    local_part = ""
    try:
        wa = (
            db.table("physician_workspace_accounts")
            .select("pro_local_part")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        if wa.data:
            local_part = str(wa.data[0].get("pro_local_part") or "")
    except Exception:
        logger.exception(
            "[pro_saga] _load_resume_plan local_part lookup failed run_id=%s",
            run_id,
        )
    if not local_part:
        from services.practikah.stripe_webhook import _default_local_part
        local_part = _default_local_part(physician_id)

    mailbox_password = ""
    if "pro.provision_pro_mailbox" not in succeeded_steps:
        # The original password was ephemeral and never delivered if the step
        # never completed — mint a fresh one for the replay.
        from services.practikah.stripe_webhook import _generate_mailbox_password
        mailbox_password = _generate_mailbox_password()

    ctx = ProSagaContext(
        db=db,
        physician_id=physician_id,
        run_id=str(run_id),
        domain=domain,
        tld_class="",
        cadence="",
        local_part=local_part,
        mailbox_password=mailbox_password,
        physician_registrant=_fetch_physician_registrant(db, physician_id),
        stripe_session_id=str(row.get("stripe_session_id") or ""),
        log=ProvisioningLogWriter(physician_id, str(run_id)),
        completed=completed,
        state=state,
        spawn_finish_later=spawn_finish_later,
    )
    return _ResumePlan(ctx=ctx, start_index=start_index), ""


async def resume_pro_run(
    db: Any,
    run_id: str,
    *,
    trigger: str,
    spawn_finish_later: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """Resume a stranded post-POR pro run from its first incomplete step.

    Args:
        db: Supabase client.
        run_id: provisioning_runs.run_id.
        trigger: audit label — 'finish_later_retry' | 'payment_recovery' |
            'manual_ops' | 'startup_rearm'.
        spawn_finish_later: whether a FAILED resume may spawn a fresh
            finish-later loop. False when the loop itself is the caller.
        background: run the saga in a task and return immediately (guards
            still run synchronously) — used by the ops endpoint.

    Returns a dict: {"resumed": False, "reason": ...} on refusal, otherwise
    {"resumed": True, "start_index", "start_step", "final_status"|"background"}.
    """
    plan, reason = await _load_resume_plan(
        db, run_id, spawn_finish_later=spawn_finish_later
    )
    if plan is None:
        logger.warning(
            "[pro_saga] resume_pro_run REFUSED run_id=%s trigger=%s reason=%s",
            run_id, trigger, reason,
        )
        return {"resumed": False, "reason": reason}

    start_step = (
        PRO_SAGA_STEPS[plan.start_index]
        if plan.start_index < len(PRO_SAGA_STEPS)
        else None
    )
    _log_workspace_audit(
        db,
        plan.ctx.physician_id,
        action="pro.upgrade_resumed",
        resource=plan.ctx.domain,
        run_id=run_id,
        detail={
            "trigger": trigger,
            "start_index": plan.start_index,
            "start_step": start_step,
        },
    )
    logger.info(
        "[pro_saga] resume_pro_run run_id=%s trigger=%s start_step=%s",
        run_id, trigger, start_step,
    )

    if background:
        asyncio.create_task(_execute_from(plan.ctx, plan.start_index))
        return {
            "resumed": True,
            "background": True,
            "start_index": plan.start_index,
            "start_step": start_step,
        }

    ok = await _execute_from(plan.ctx, plan.start_index)
    return {
        "resumed": True,
        "start_index": plan.start_index,
        "start_step": start_step,
        "final_status": "succeeded" if ok else "not_succeeded",
    }


# Refusal reasons that can never clear on their own — the loop stops retrying
# and falls through to the exhaustion alert instead of burning attempts.
_PERMANENT_REFUSALS = {"run_not_found", "not_pro_saga", "pre_por_not_resumable"}


async def _finish_later_retry_loop(
    db: Any,
    physician_id: str,
    run_id: str,
    domain: str,
    failed_step: str,
    *,
    start_attempt: int = 0,
) -> None:
    """Per D-15: retry every 5 min for up to 1 hour (12 attempts total).

    v2 (step-replay slice): each attempt actually RE-EXECUTES the saga from
    the first incomplete step via ``resume_pro_run`` (the resumed execution
    never spawns a nested loop). This task dies on every deploy —
    ``rearm_finish_later_runs`` re-arms it on startup with ``start_attempt``
    picking the count back up from ``provisioning_runs.retry_count``.

    Exhaustion → structured ops-alert (Plan 10-09 mailops surface) + an
    honest 'pro_stalled' email to the doctor.
    """
    for attempt in range(start_attempt, _FINISH_LATER_MAX_ATTEMPTS):
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

        result = await resume_pro_run(
            db, run_id, trigger="finish_later_retry", spawn_finish_later=False
        )
        if result.get("resumed") and result.get("final_status") == "succeeded":
            logger.info(
                "[pro_saga] finish-later loop: run_id=%s completed on retry %d",
                run_id, attempt + 1,
            )
            return
        if not result.get("resumed"):
            reason = result.get("reason")
            if reason == "status_succeeded_not_resumable":
                return
            if reason in _PERMANENT_REFUSALS:
                logger.error(
                    "[pro_saga] finish-later loop: run_id=%s permanent refusal "
                    "reason=%s — abandoning retries", run_id, reason,
                )
                break

    # Attempts exhausted (or permanently refused) — emit ops alert.
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

    await _trigger_pro_stalled_email(
        db=db, physician_id=physician_id, domain=domain, run_id=run_id,
    )

    logger.error(
        "[pro_saga] finish-later loop exhausted run_id=%s failed_step=%s — ops alerted",
        run_id, failed_step,
    )


async def rearm_finish_later_runs(db: Any = None) -> int:
    """Startup re-arm for pro runs stranded in ``partial_finish_later``.

    The finish-later loop is an in-process asyncio task — every Render
    deploy/restart kills it, silently stranding any post-POR run mid-retry.
    Called once on FastAPI startup (alongside — and independent of — the
    basic-saga orphan sweeper, which deliberately EXCLUDES pro runs).

    Returns the number of loops re-armed.
    """
    if db is None:
        from db.client import get_supabase
        db = get_supabase()
    if db is None:
        logger.warning("[pro_saga] rearm_finish_later_runs: supabase not configured")
        return 0
    try:
        res = (
            db.table("provisioning_runs")
            .select("run_id, physician_id, domain_name, current_step, retry_count")
            .eq("saga_type", "pro_upgrade")
            .eq("status", "partial_finish_later")
            .execute()
        )
        rows = list(res.data or [])
    except Exception:
        logger.exception("[pro_saga] rearm_finish_later_runs scan failed")
        return 0

    armed = 0
    for row in rows:
        run_id = row.get("run_id")
        if not run_id:
            continue
        retry_count = int(row.get("retry_count") or 0)
        if retry_count >= _FINISH_LATER_MAX_ATTEMPTS:
            # Budget already exhausted before the restart — ops alert fired
            # (or will need manual resume). Don't re-arm; just stay loud.
            logger.warning(
                "[pro_saga] rearm: run_id=%s already exhausted (retry_count=%d) "
                "— needs manual resume (POST /practikah/internal/pro-resume-run)",
                run_id, retry_count,
            )
            continue
        physician_id = str(row.get("physician_id") or "")
        domain = str(row.get("domain_name") or "")
        asyncio.create_task(
            _finish_later_retry_loop(
                db=db,
                physician_id=physician_id,
                run_id=str(run_id),
                domain=domain,
                failed_step=str(row.get("current_step") or "unknown"),
                start_attempt=retry_count,
            )
        )
        _log_workspace_audit(
            db,
            physician_id,
            action="pro.finish_later_rearmed",
            resource=domain,
            run_id=str(run_id),
            detail={"retry_count": retry_count},
        )
        armed += 1

    if armed:
        logger.info(
            "[pro_saga] rearmed %d finish-later loop(s) on startup", armed
        )
    return armed
