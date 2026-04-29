"""Práctikah Pro workspace provisioning orchestrator (Phase 11).

Implements the DB-backed saga pattern per D-08/D-09/D-10:
- provision_workspace() runs the 8-stage saga (registrar → CF zone → DKIM fetch →
  DNS records → Mailcow domain → Mailcow mailbox → CF custom hostname).
- Each step writes practikah_provisioning_log rows (requested/succeeded/failed).
- On any step failure, run_rollback() walks the log in reverse and calls each
  module's undo_ method.
- resume_orphan_runs() detects abandoned runs on FastAPI startup and resumes rollback.

Vendor-client singletons are constructed lazily (at first call) from env vars.
Missing env vars fail loudly at call time in production, log a warning and return
a 503-shaped ProvisioningResult in dev (never raise at import time — keeps tests fast).

Per D-19: MEDIKAH_PROVISIONING_SANDBOX=true scopes Mailcow domains to 'sandbox-'
prefix and tags Cloudflare zones with purpose=sandbox. Sandbox uses real APIs against
the live VPS — only the data layer differs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional
from uuid import uuid4

import httpx

from services.practikah.audit import ProvisioningLogWriter
from services.practikah.cloudflare_client import CloudflareClient, CloudflareResult
from services.practikah.dns_writer import compose_dns_records
from services.practikah.domain_registrar import DomainRegistrar, RegistrarResult
from services.practikah.mailbox_provisioner import MailboxProvisioner, MailcowResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable reads (module-level, no raise at import time per D-10)
# ---------------------------------------------------------------------------

_CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
_CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
_MAILCOW_API_URL = os.getenv("MAILCOW_API_URL", "https://practikah.medikah.health")
_MAILCOW_API_KEY = os.getenv("MAILCOW_API_KEY")
_OPENSRS_USER = os.getenv("OPENSRS_USERNAME", "")
_OPENSRS_KEY = os.getenv("OPENSRS_API_KEY", "")
_MAILCOW_VPS_IP = os.getenv("MAILCOW_VPS_IP", "")
_RESEND_API_KEY = os.getenv("RESEND_API_KEY")
_SANDBOX_MODE = os.getenv("MEDIKAH_PROVISIONING_SANDBOX", "false").lower() in {
    "1", "true", "yes", "on"
}

# ---------------------------------------------------------------------------
# Lazy vendor-client singletons (constructed at first call per D-10)
# ---------------------------------------------------------------------------

_cf_client: Optional[CloudflareClient] = None
_mailbox: Optional[MailboxProvisioner] = None
_registrar: Optional[DomainRegistrar] = None


def get_cloudflare_client() -> CloudflareClient:
    """Return (or create) the module-level CloudflareClient singleton.

    Raises RuntimeError in production if CLOUDFLARE_API_TOKEN is missing.
    In dev, allows the caller to handle a None-less approach — we raise here too
    to keep the contract clear, but provision_workspace catches and wraps it.
    """
    global _cf_client
    if _cf_client is None:
        if not _CLOUDFLARE_API_TOKEN:
            raise RuntimeError(
                "CLOUDFLARE_API_TOKEN is not configured. "
                "Set it in the Render dashboard or local .env."
            )
        _cf_client = CloudflareClient(
            _CLOUDFLARE_API_TOKEN,
            sandbox_mode=_SANDBOX_MODE,
        )
    return _cf_client


def get_mailbox_provisioner() -> MailboxProvisioner:
    """Return (or create) the module-level MailboxProvisioner singleton."""
    global _mailbox
    if _mailbox is None:
        if not _MAILCOW_API_KEY:
            raise RuntimeError(
                "MAILCOW_API_KEY is not configured. "
                "Set it in the Render dashboard or local .env. "
                "NOTE: The current key may be returning 401 — see Phase 11 D-17 carry-item."
            )
        _mailbox = MailboxProvisioner(
            _MAILCOW_API_URL,
            _MAILCOW_API_KEY,
            sandbox_mode=_SANDBOX_MODE,
        )
    return _mailbox


def get_domain_registrar() -> DomainRegistrar:
    """Return (or create) the module-level DomainRegistrar singleton."""
    global _registrar
    if _registrar is None:
        if not _CLOUDFLARE_API_TOKEN:
            raise RuntimeError(
                "CLOUDFLARE_API_TOKEN is not configured — required for DomainRegistrar."
            )
        if not _CLOUDFLARE_ACCOUNT_ID:
            raise RuntimeError(
                "CLOUDFLARE_ACCOUNT_ID is not configured — required for DomainRegistrar."
            )
        _registrar = DomainRegistrar(
            cloudflare_api_token=_CLOUDFLARE_API_TOKEN,
            cloudflare_account_id=_CLOUDFLARE_ACCOUNT_ID,
            opensrs_username=_OPENSRS_USER,
            opensrs_api_key=_OPENSRS_KEY,
            sandbox_mode=_SANDBOX_MODE,
        )
    return _registrar


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------

@dataclass
class ProvisioningResult:
    """Final outcome of a provision_workspace call."""

    success: bool
    run_id: str
    elapsed_seconds: float
    domain: str
    mailbox_address: Optional[str]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# UNDO_REGISTRY — one entry per SAGA_STEPS step
# ---------------------------------------------------------------------------
# Each value is an async callable(step_record: dict, run_id: str) -> None.
# `step_record` is the 'succeeded' log row dict (has 'step_name', 'resource_type', 'detail').
# The callables delegate to the appropriate module's undo_ method.
# All undo_ methods are non-raising per D-10.

UNDO_REGISTRY: dict[str, Callable[[dict[str, Any], str], Awaitable[None]]] = {
    "registrar.register": lambda step, run_id: get_domain_registrar().undo_register(
        domain=step["detail"].get("domain", ""),
        run_id=run_id,
        prior_result=RegistrarResult(
            success=True,
            registrar=step["detail"].get("registrar", "mocked"),
            registrar_domain_id=step["detail"].get("registrar_domain_id"),
            raw_response={},
        ),
    ),
    "mailcow.get_dkim": lambda step, run_id: _noop_undo("mailcow.get_dkim", run_id),
    "cloudflare.create_zone": lambda step, run_id: get_cloudflare_client().undo_create_zone(
        domain=step["detail"].get("domain", ""),
        run_id=run_id,
        prior_result=CloudflareResult(
            success=True,
            resource_id=step["detail"].get("zone_id") or step["detail"].get("resource_id"),
            raw_response={},
        ),
    ),
    "cloudflare.write_dns_record.MX": lambda step, run_id: get_cloudflare_client().undo_write_dns_record(
        zone_id=step["detail"].get("zone_id", ""),
        record_id=step["detail"].get("resource_id", ""),
        run_id=run_id,
    ),
    "cloudflare.write_dns_record.A": lambda step, run_id: get_cloudflare_client().undo_write_dns_record(
        zone_id=step["detail"].get("zone_id", ""),
        record_id=step["detail"].get("resource_id", ""),
        run_id=run_id,
    ),
    "cloudflare.write_dns_record.SPF": lambda step, run_id: get_cloudflare_client().undo_write_dns_record(
        zone_id=step["detail"].get("zone_id", ""),
        record_id=step["detail"].get("resource_id", ""),
        run_id=run_id,
    ),
    "cloudflare.write_dns_record.DKIM_RESEND": lambda step, run_id: get_cloudflare_client().undo_write_dns_record(
        zone_id=step["detail"].get("zone_id", ""),
        record_id=step["detail"].get("resource_id", ""),
        run_id=run_id,
    ),
    "cloudflare.write_dns_record.DKIM_MAILCOW": lambda step, run_id: get_cloudflare_client().undo_write_dns_record(
        zone_id=step["detail"].get("zone_id", ""),
        record_id=step["detail"].get("resource_id", ""),
        run_id=run_id,
    ),
    "cloudflare.write_dns_record.DMARC": lambda step, run_id: get_cloudflare_client().undo_write_dns_record(
        zone_id=step["detail"].get("zone_id", ""),
        record_id=step["detail"].get("resource_id", ""),
        run_id=run_id,
    ),
    "mailcow.add_domain": lambda step, run_id: get_mailbox_provisioner().undo_add_domain(
        domain=step["detail"].get("domain", ""),
        run_id=run_id,
        prior_result=MailcowResult(
            success=True,
            resource_id=step["detail"].get("resource_id"),
            raw_response={},
        ),
    ),
    "mailcow.add_mailbox": lambda step, run_id: get_mailbox_provisioner().undo_add_mailbox(
        local_part=step["detail"].get("local_part", ""),
        domain=step["detail"].get("domain", ""),
        run_id=run_id,
        prior_result=MailcowResult(
            success=True,
            resource_id=step["detail"].get("resource_id"),
            raw_response={},
        ),
    ),
    "cloudflare.create_custom_hostname": lambda step, run_id: get_cloudflare_client().undo_create_custom_hostname(
        zone_id=step["detail"].get("zone_id", ""),
        hostname_id=step["detail"].get("resource_id", ""),
        run_id=run_id,
        prior_result=CloudflareResult(
            success=True,
            resource_id=step["detail"].get("resource_id"),
            raw_response={},
        ),
    ),
}


async def _noop_undo(step_name: str, run_id: str) -> None:
    """No-op undo for steps that have no compensating action (e.g. pure reads)."""
    logger.info("[orchestrator] _noop_undo step=%s run_id=%s (no undo needed)", step_name, run_id)


# ---------------------------------------------------------------------------
# Resend DKIM helper (Step 3 in saga)
# ---------------------------------------------------------------------------

async def _fetch_resend_dkim(domain: str) -> str:
    """Fetch or create the Resend DKIM value for `domain`.

    Returns the DKIM TXT record value string, e.g. 'v=DKIM1; k=rsa; p=AAAA...'.
    On any failure, returns an empty string (caller logs and continues — DNS record
    will be written with empty value and flagged for operator review).
    """
    if not _RESEND_API_KEY:
        logger.warning(
            "[orchestrator] RESEND_API_KEY not set — Resend DKIM will be empty for domain=%s",
            domain,
        )
        return ""

    headers = {
        "Authorization": f"Bearer {_RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(10.0, connect=3.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Step 1: List domains to find existing one
            resp = await client.get("https://api.resend.com/domains", headers=headers)
            resp.raise_for_status()
            domains_data = resp.json()

            existing_domain = None
            for d in domains_data.get("data", []):
                if d.get("name") == domain:
                    existing_domain = d
                    break

            if existing_domain is None:
                # Domain not in Resend — add it
                logger.info("[orchestrator] adding domain=%s to Resend", domain)
                add_resp = await client.post(
                    "https://api.resend.com/domains",
                    json={"name": domain},
                    headers=headers,
                )
                add_resp.raise_for_status()
                existing_domain = add_resp.json()

            # Step 2: Extract DKIM value from domain records
            for record in existing_domain.get("records", []):
                if record.get("type") == "TXT" and "resend._domainkey" in record.get("name", ""):
                    value = record.get("value", "")
                    if value:
                        logger.info(
                            "[orchestrator] Resend DKIM fetched domain=%s", domain
                        )
                        return value

            logger.warning(
                "[orchestrator] Resend DKIM record not found for domain=%s "
                "(domain may need verification in Resend dashboard)",
                domain,
            )
            return ""

    except Exception:
        logger.exception(
            "[orchestrator] Failed to fetch/create Resend DKIM for domain=%s", domain
        )
        return ""


# ---------------------------------------------------------------------------
# Free-tier provisioning saga (Phase 12-02)
# ---------------------------------------------------------------------------

async def _provision_free_workspace(
    physician_id: str,
    mailbox_local_part: str,
    mailbox_password: str,
    run_id: str,
    title: Optional[str] = None,
) -> ProvisioningResult:
    """Single-step free-tier provisioning saga.

    The medikah.health Mailcow domain is pre-provisioned (Phase 10). This function
    only adds the physician's mailbox to that existing domain.

    Saga step:
      1. mailcow.add_mailbox(domain='medikah.health', local_part, password, quota_mb=10240)

    UNDO: undo_add_mailbox on failure (UNDO_REGISTRY already covers this step).

    State machine transitions (physician_workspace_accounts.state):
      The CALLER (wizard_complete route) is responsible for:
        - Setting state='provisioning' BEFORE calling this function.
        - Setting state='free_active' + mailbox fields on success.
        - Setting state='free_failed' on failure.
      This function updates the DB directly AS WELL for defense-in-depth — the
      route-level update is the primary path; this is a fallback for orphan-run
      crash recovery.

    Per T-12-02-02: mailbox_password is NEVER logged.
    """
    started_at = time.monotonic()
    log = ProvisioningLogWriter(physician_id, run_id)
    domain = "medikah.health"
    mailbox_address = f"{mailbox_local_part}@{domain}"

    logger.info(
        "[orchestrator] _provision_free_workspace start physician_id=%s "
        "mailbox=%s run_id=%s sandbox=%s",
        physician_id, mailbox_address, run_id, _SANDBOX_MODE,
    )

    await log.requested(
        step="free_tier.start",
        detail={"physician_id": physician_id, "tier": "free"},
        resource_type="workspace",
    )

    try:
        mailbox_prov = get_mailbox_provisioner()
    except RuntimeError as err:
        elapsed = time.monotonic() - started_at
        await log.failed(
            step="free_tier.start",
            detail={"error": str(err)},
            resource_type="workspace",
        )
        return ProvisioningResult(
            success=False,
            run_id=run_id,
            elapsed_seconds=elapsed,
            domain=domain,
            mailbox_address=None,
            error=f"Mailbox provisioner misconfigured: {err}",
        )

    # Step 1: add_mailbox — the only saga step for free tier
    # NOTE: password never in log (T-12-02-02 / T-12-02-11)
    await log.requested(
        step="mailcow.add_mailbox",
        detail={"local_part": mailbox_local_part, "domain": domain},
        resource_type="mailbox",
    )

    try:
        mailbox_result = await mailbox_prov.do_add_mailbox(
            local_part=mailbox_local_part,
            domain=domain,
            password=mailbox_password,
            run_id=run_id,
            quota_mb=10240,  # MAIL-08: 10 GB default
        )
    except Exception as err:
        elapsed = time.monotonic() - started_at
        await log.failed(
            step="mailcow.add_mailbox",
            detail={"local_part": mailbox_local_part, "domain": domain, "error": str(err)},
            resource_type="mailbox",
        )
        logger.exception(
            "[orchestrator] _provision_free_workspace mailcow.add_mailbox exception "
            "physician_id=%s run_id=%s",
            physician_id, run_id,
        )
        return ProvisioningResult(
            success=False,
            run_id=run_id,
            elapsed_seconds=elapsed,
            domain=domain,
            mailbox_address=None,
            error=f"Mailbox creation failed: {err}",
        )

    if not mailbox_result.success:
        elapsed = time.monotonic() - started_at
        await log.failed(
            step="mailcow.add_mailbox",
            detail={
                "local_part": mailbox_local_part,
                "domain": domain,
                "error": mailbox_result.error,
            },
            resource_type="mailbox",
        )
        # Attempt undo (best-effort) — mailbox may have been partially created
        try:
            await mailbox_prov.undo_add_mailbox(
                local_part=mailbox_local_part,
                domain=domain,
                run_id=run_id,
                prior_result=mailbox_result,
            )
        except Exception:
            logger.warning(
                "[orchestrator] _provision_free_workspace: undo_add_mailbox failed "
                "physician_id=%s run_id=%s (best-effort, continuing)",
                physician_id, run_id,
            )
        error_msg = mailbox_result.error or "Mailcow add_mailbox returned failure"
        logger.warning(
            "[orchestrator] _provision_free_workspace FAILED physician_id=%s "
            "run_id=%s error=%s elapsed=%.1fs",
            physician_id, run_id, error_msg, elapsed,
        )
        return ProvisioningResult(
            success=False,
            run_id=run_id,
            elapsed_seconds=elapsed,
            domain=domain,
            mailbox_address=None,
            error=error_msg,
        )

    # Mailbox created successfully
    await log.succeeded(
        step="mailcow.add_mailbox",
        detail={
            "mailbox_address": mailbox_address,
            "resource_id": mailbox_result.resource_id,
        },
        resource_type="mailbox",
    )

    # Write workspace_audit_log (OPS-01 / T-12-02-05)
    from db.client import get_supabase
    db = get_supabase()
    if db is not None:
        try:
            db.table("workspace_audit_log").insert(
                {
                    "physician_id": physician_id,
                    "actor_id": physician_id,
                    "actor_role": "physician",
                    "action": "workspace.setup_completed",
                    "resource_type": "workspace",
                    "resource_id": None,
                    "detail": {
                        "mailbox_address": mailbox_address,
                        "tier": "free",
                        "run_id": run_id,
                    },
                }
            ).execute()
        except Exception:
            logger.exception(
                "[orchestrator] _provision_free_workspace: workspace_audit_log insert "
                "failed physician_id=%s run_id=%s (non-fatal)",
                physician_id, run_id,
            )

    elapsed = time.monotonic() - started_at
    await log.succeeded(
        step="free_tier.completed",
        detail={
            "mailbox_address": mailbox_address,
            "tier": "free",
            "title": title,
        },
        resource_type="workspace",
    )

    logger.info(
        "[orchestrator] _provision_free_workspace SUCCESS physician_id=%s "
        "mailbox=%s run_id=%s elapsed=%.1fs",
        physician_id, mailbox_address, run_id, elapsed,
    )

    return ProvisioningResult(
        success=True,
        run_id=run_id,
        elapsed_seconds=elapsed,
        domain=domain,
        mailbox_address=mailbox_address,
    )


# ---------------------------------------------------------------------------
# Main saga: provision_workspace
# ---------------------------------------------------------------------------

async def provision_workspace(
    physician_id: str,
    domain: str,
    mailbox_local_part: str,
    mailbox_password: str,
    *,
    registrant_name: str = "",
    registrant_email: str = "",
    registrant_country: str = "US",
    run_id: Optional[str] = None,
    tld_strategy: Literal["real", "mocked"] = "real",
    tier: Literal["free", "pro"] = "pro",
    title: Optional[str] = None,
) -> ProvisioningResult:
    """Run the Práctikah workspace provisioning saga.

    For tier='free': runs a single-step saga (Mailcow add_mailbox only) against
    the existing medikah.health domain. Skips registrar, Cloudflare zone/DNS, and
    custom hostname steps — these only apply for Pro (custom domain) workspaces.

    For tier='pro' (default): runs the full 8-step saga:
    1.  registrar.register         — register the custom domain
    2.  mailcow.get_dkim           — fetch/create Mailcow DKIM key for DNS template
    3.  cloudflare.create_zone     — create CF DNS zone for the domain
    4a. cloudflare.write_dns_record.MX
    4b. cloudflare.write_dns_record.A
    4c. cloudflare.write_dns_record.SPF
    4d. cloudflare.write_dns_record.DKIM_RESEND
    4e. cloudflare.write_dns_record.DKIM_MAILCOW
    4f. cloudflare.write_dns_record.DMARC
    5.  mailcow.add_domain         — add domain to Mailcow
    6.  mailcow.add_mailbox        — create the physician's mailbox
    7.  cloudflare.create_custom_hostname — attach CF for SaaS hostname

    On any step failure: run_rollback() walks the log in reverse and undoes
    completed steps. Returns ProvisioningResult(success=False, ...) with error message.

    On success: returns ProvisioningResult(success=True, ...) with elapsed time,
    domain, and mailbox address.

    Per D-19: MEDIKAH_PROVISIONING_SANDBOX=true prefixes Mailcow domains with
    'sandbox-' and tags CF zones with purpose=sandbox.

    Args:
        physician_id:       UUID of the physician record.
        domain:             For free tier: 'medikah.health'. For pro: custom domain (e.g. 'drsmith.health').
        mailbox_local_part: The mailbox local part (e.g. 'dr.smith').
        mailbox_password:   Mailbox password — NEVER logged.
        registrant_name:    Doctor's full name for WHOIS (pro tier only).
        registrant_email:   Doctor's email for WHOIS (pro tier only).
        registrant_country: ISO 3166-1 alpha-2 country code (pro tier only).
        run_id:             Optional saga run ID (generated if not provided).
        tld_strategy:       'real' (real registrar) or 'mocked' (skip ~$10/run cost).
        tier:               'free' (skip registrar/CF) or 'pro' (full saga). Default 'pro'.
        title:              Physician honorific ('Dr' or 'Dra') — stored on free-tier completion.
    """
    run_id = run_id or str(uuid4())

    # ------------------------------------------------------------------
    # FREE-TIER BRANCH (Phase 12-02)
    # Skips registrar, Cloudflare zone/DNS/custom-hostname.
    # The medikah.health Mailcow domain is pre-provisioned (Phase 10).
    # Only adds the physician's mailbox to the existing medikah.health domain.
    # ------------------------------------------------------------------
    if tier == "free":
        return await _provision_free_workspace(
            physician_id=physician_id,
            mailbox_local_part=mailbox_local_part,
            mailbox_password=mailbox_password,
            run_id=run_id,
            title=title,
        )

    # ------------------------------------------------------------------
    # PRO-TIER SAGA (original Phase 11 full saga — unchanged)
    # ------------------------------------------------------------------
    started_at = time.monotonic()
    log = ProvisioningLogWriter(physician_id, run_id)

    logger.info(
        "[orchestrator] provision_workspace start physician_id=%s domain=%s run_id=%s "
        "tld_strategy=%s sandbox=%s",
        physician_id, domain, run_id, tld_strategy, _SANDBOX_MODE,
    )

    current_step = "init"
    zone_id: Optional[str] = None
    mailbox_address: Optional[str] = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Register the domain
        # ------------------------------------------------------------------
        current_step = "registrar.register"
        await log.requested(
            step=current_step,
            detail={"domain": domain, "tld_strategy": tld_strategy},
            resource_type="domain",
        )

        try:
            registrar = get_domain_registrar()
        except RuntimeError as err:
            await log.failed(step=current_step, detail={"error": str(err)}, resource_type="domain")
            elapsed = time.monotonic() - started_at
            return ProvisioningResult(
                success=False, run_id=run_id, elapsed_seconds=elapsed,
                domain=domain, mailbox_address=None,
                error=f"Registrar misconfigured: {err}",
            )

        reg_result = await registrar.do_register(
            domain=domain,
            run_id=run_id,
            registrant_name=registrant_name,
            registrant_email=registrant_email,
            registrant_country=registrant_country,
            mocked=(tld_strategy == "mocked"),
            whois_privacy=True,
        )

        if not reg_result.success:
            await log.failed(
                step=current_step,
                detail={**reg_result.summary(), "domain": domain},
                resource_type="domain",
            )
            raise RuntimeError(f"Registrar failed: {reg_result.error}")

        await log.succeeded(
            step=current_step,
            detail={**reg_result.summary(), "domain": domain},
            resource_type="domain",
        )
        logger.info("[orchestrator] Step 1 done: registrar domain=%s run_id=%s", domain, run_id)

        # ------------------------------------------------------------------
        # Step 2: Fetch Mailcow DKIM key (pure read — creates key if missing)
        # ------------------------------------------------------------------
        current_step = "mailcow.get_dkim"
        await log.requested(
            step=current_step,
            detail={"domain": domain},
            resource_type="domain",
        )

        try:
            mailbox_prov = get_mailbox_provisioner()
        except RuntimeError as err:
            await log.failed(step=current_step, detail={"error": str(err)}, resource_type="domain")
            raise RuntimeError(f"Mailbox provisioner misconfigured: {err}")

        dkim_result = await mailbox_prov.do_get_dkim(domain=domain, run_id=run_id)
        mailcow_dkim_value = dkim_result.resource_id or ""

        if not dkim_result.success:
            # Non-fatal — log warning but continue; DNS record will be empty
            logger.warning(
                "[orchestrator] Mailcow DKIM fetch failed domain=%s — DNS DKIM_MAILCOW record "
                "will have empty value. error=%s",
                domain, dkim_result.error,
            )
            await log.failed(
                step=current_step,
                detail={"error": dkim_result.error or "DKIM fetch failed", "domain": domain},
                resource_type="domain",
            )
            # Continue — we can still write an empty DKIM record and fix later
        else:
            await log.succeeded(
                step=current_step,
                detail={"domain": domain, "dkim_value_len": len(mailcow_dkim_value)},
                resource_type="domain",
            )
        logger.info("[orchestrator] Step 2 done: mailcow_dkim domain=%s run_id=%s", domain, run_id)

        # ------------------------------------------------------------------
        # Step 3: Fetch Resend DKIM value (for DNS template)
        # ------------------------------------------------------------------
        resend_dkim_value = await _fetch_resend_dkim(domain)
        logger.info(
            "[orchestrator] Step 3 done: resend_dkim domain=%s resend_dkim_len=%d run_id=%s",
            domain, len(resend_dkim_value), run_id,
        )

        # ------------------------------------------------------------------
        # Step 4: Create Cloudflare zone
        # ------------------------------------------------------------------
        current_step = "cloudflare.create_zone"
        await log.requested(
            step=current_step,
            detail={"domain": domain},
            resource_type="cloudflare_zone",
        )

        try:
            cf = get_cloudflare_client()
        except RuntimeError as err:
            await log.failed(step=current_step, detail={"error": str(err)}, resource_type="cloudflare_zone")
            raise RuntimeError(f"Cloudflare client misconfigured: {err}")

        zone_result = await cf.do_create_zone(domain=domain, run_id=run_id)

        if not zone_result.success:
            await log.failed(
                step=current_step,
                detail={**zone_result.summary(), "domain": domain},
                resource_type="cloudflare_zone",
            )
            raise RuntimeError(f"Cloudflare zone creation failed: {zone_result.error}")

        zone_id = zone_result.resource_id
        await log.succeeded(
            step=current_step,
            detail={**zone_result.summary(), "domain": domain, "zone_id": zone_id},
            resource_type="cloudflare_zone",
        )
        logger.info(
            "[orchestrator] Step 4 done: cf_zone domain=%s zone_id=%s run_id=%s",
            domain, zone_id, run_id,
        )

        # ------------------------------------------------------------------
        # Step 5: Write DNS records
        # ------------------------------------------------------------------
        dns_records = compose_dns_records(
            domain=domain,
            mailcow_host="practikah.medikah.health",
            mailcow_vps_ip=_MAILCOW_VPS_IP,
            resend_dkim_value=resend_dkim_value,
            mailcow_dkim_value=mailcow_dkim_value,
        )

        # Map record position → step name suffix
        DNS_STEP_NAMES = ["MX", "A", "SPF", "DKIM_RESEND", "DKIM_MAILCOW", "DMARC"]

        for dns_record, step_suffix in zip(dns_records, DNS_STEP_NAMES):
            current_step = f"cloudflare.write_dns_record.{step_suffix}"
            await log.requested(
                step=current_step,
                detail={
                    "domain": domain,
                    "zone_id": zone_id,
                    "record_type": dns_record.record_type,
                    "record_name": dns_record.name,
                },
                resource_type="dns",
            )

            dns_result = await cf.do_write_dns_record(
                zone_id=zone_id,  # type: ignore[arg-type]
                record=dns_record,
                run_id=run_id,
            )

            if not dns_result.success:
                await log.failed(
                    step=current_step,
                    detail={
                        **dns_result.summary(),
                        "zone_id": zone_id,
                        "record_type": dns_record.record_type,
                        "record_name": dns_record.name,
                    },
                    resource_type="dns",
                )
                raise RuntimeError(
                    f"DNS record write failed ({step_suffix}): {dns_result.error}"
                )

            await log.succeeded(
                step=current_step,
                detail={
                    **dns_result.summary(),
                    "zone_id": zone_id,
                    "record_type": dns_record.record_type,
                    "record_name": dns_record.name,
                    "resource_id": dns_result.resource_id,
                },
                resource_type="dns",
            )
            logger.info(
                "[orchestrator] DNS record %s written domain=%s run_id=%s",
                step_suffix, domain, run_id,
            )

        logger.info("[orchestrator] Step 5 done: all DNS records domain=%s run_id=%s", domain, run_id)

        # ------------------------------------------------------------------
        # Step 6: Mailcow add_domain
        # ------------------------------------------------------------------
        current_step = "mailcow.add_domain"
        await log.requested(
            step=current_step,
            detail={"domain": domain},
            resource_type="mailbox",
        )

        domain_result = await mailbox_prov.do_add_domain(domain=domain, run_id=run_id)

        if not domain_result.success:
            await log.failed(
                step=current_step,
                detail={**domain_result.summary(), "domain": domain},
                resource_type="mailbox",
            )
            raise RuntimeError(f"Mailcow add_domain failed: {domain_result.error}")

        await log.succeeded(
            step=current_step,
            detail={**domain_result.summary(), "domain": domain, "resource_id": domain_result.resource_id},
            resource_type="mailbox",
        )
        logger.info("[orchestrator] Step 6 done: mailcow_domain domain=%s run_id=%s", domain, run_id)

        # ------------------------------------------------------------------
        # Step 7: Mailcow add_mailbox
        # ------------------------------------------------------------------
        current_step = "mailcow.add_mailbox"
        mailbox_address = f"{mailbox_local_part}@{domain}"
        await log.requested(
            step=current_step,
            # NOTE: password is NEVER logged (threat model T-11-06-04)
            detail={"local_part": mailbox_local_part, "domain": domain},
            resource_type="mailbox",
        )

        mailbox_result = await mailbox_prov.do_add_mailbox(
            local_part=mailbox_local_part,
            domain=domain,
            password=mailbox_password,
            run_id=run_id,
        )

        if not mailbox_result.success:
            await log.failed(
                step=current_step,
                detail={
                    "local_part": mailbox_local_part,
                    "domain": domain,
                    "error": mailbox_result.error,
                },
                resource_type="mailbox",
            )
            raise RuntimeError(f"Mailcow add_mailbox failed: {mailbox_result.error}")

        await log.succeeded(
            step=current_step,
            detail={
                "local_part": mailbox_local_part,
                "domain": domain,
                "resource_id": mailbox_result.resource_id,
            },
            resource_type="mailbox",
        )
        logger.info(
            "[orchestrator] Step 7 done: mailbox address=%s run_id=%s",
            mailbox_address, run_id,
        )

        # ------------------------------------------------------------------
        # Step 8: Cloudflare create_custom_hostname
        # ------------------------------------------------------------------
        current_step = "cloudflare.create_custom_hostname"

        # In sandbox mode, use a slug hostname under medikah.health;
        # in real mode, use the physician's own domain.
        slug = domain.replace(".", "-")
        hostname = f"{slug}.medikah.health" if _SANDBOX_MODE else domain

        await log.requested(
            step=current_step,
            detail={"zone_id": zone_id, "hostname": hostname},
            resource_type="cloudflare_hostname",
        )

        hostname_result = await cf.do_create_custom_hostname(
            zone_id=zone_id,  # type: ignore[arg-type]
            hostname=hostname,
            run_id=run_id,
        )

        if not hostname_result.success:
            await log.failed(
                step=current_step,
                detail={**hostname_result.summary(), "zone_id": zone_id, "hostname": hostname},
                resource_type="cloudflare_hostname",
            )
            raise RuntimeError(
                f"Cloudflare custom hostname failed: {hostname_result.error}"
            )

        await log.succeeded(
            step=current_step,
            detail={
                **hostname_result.summary(),
                "zone_id": zone_id,
                "hostname": hostname,
                "resource_id": hostname_result.resource_id,
            },
            resource_type="cloudflare_hostname",
        )
        logger.info(
            "[orchestrator] Step 8 done: custom_hostname=%s run_id=%s", hostname, run_id
        )

        # ------------------------------------------------------------------
        # All steps succeeded
        # ------------------------------------------------------------------
        elapsed = time.monotonic() - started_at
        logger.info(
            "[orchestrator] provision_workspace SUCCESS physician_id=%s domain=%s "
            "run_id=%s elapsed=%.1fs",
            physician_id, domain, run_id, elapsed,
        )
        return ProvisioningResult(
            success=True,
            run_id=run_id,
            elapsed_seconds=elapsed,
            domain=domain,
            mailbox_address=mailbox_address,
        )

    except Exception as exc:
        elapsed = time.monotonic() - started_at
        error_msg = str(exc)
        logger.exception(
            "[orchestrator] provision_workspace FAILED physician_id=%s domain=%s "
            "run_id=%s step=%s elapsed=%.1fs error=%s",
            physician_id, domain, run_id, current_step, elapsed, error_msg,
        )

        # Attempt rollback — best-effort, never raises
        try:
            await run_rollback(physician_id=physician_id, run_id=run_id)
        except Exception:
            logger.exception(
                "[orchestrator] run_rollback itself failed physician_id=%s run_id=%s",
                physician_id, run_id,
            )

        return ProvisioningResult(
            success=False,
            run_id=run_id,
            elapsed_seconds=elapsed,
            domain=domain,
            mailbox_address=None,
            error=error_msg,
        )


# ---------------------------------------------------------------------------
# Rollback runner
# ---------------------------------------------------------------------------

async def run_rollback(physician_id: str, run_id: str) -> None:
    """Walk practikah_provisioning_log for run_id in reverse step order.

    For each completed step, invokes UNDO_REGISTRY[step_name] with the step's
    detail dict (which carries the resource IDs needed for undo). Rollback is
    best-effort: one failed undo does not abort the remaining undos.

    Per D-08/D-09: the log is the source of truth. This function can be called
    after a crash-resume (resume_orphan_runs) or after a fresh provision failure.
    """
    log = ProvisioningLogWriter(physician_id, run_id)
    completed = await log.list_completed_steps_for_run()

    logger.info(
        "[orchestrator] run_rollback start physician_id=%s run_id=%s "
        "completed_steps=%d",
        physician_id, run_id, len(completed),
    )

    for step in reversed(completed):
        step_name = step.get("step_name", "unknown")
        rollback_step = f"{step_name}.rollback"

        await log.rollback_started(
            step=rollback_step,
            detail={"rollback_for_step": step_name, "run_id": run_id},
        )

        undo_callable = UNDO_REGISTRY.get(step_name)
        if undo_callable is None:
            logger.warning(
                "[orchestrator] run_rollback no undo handler for step=%s run_id=%s — skipping",
                step_name, run_id,
            )
            await log.rollback_succeeded(
                step=rollback_step,
                detail={"note": "no undo handler registered — treated as no-op"},
            )
            continue

        try:
            await undo_callable(step, run_id)
            await log.rollback_succeeded(
                step=rollback_step,
                detail={"undone_step": step_name},
            )
            logger.info(
                "[orchestrator] run_rollback step=%s succeeded run_id=%s",
                step_name, run_id,
            )
        except Exception as exc:
            # Per D-08: rollback runner is best-effort. Log and continue.
            logger.exception(
                "[orchestrator] run_rollback step=%s FAILED run_id=%s — continuing with remaining undos",
                step_name, run_id,
            )
            await log.rollback_failed(
                step=rollback_step,
                detail={"error": str(exc), "undone_step": step_name},
            )

    logger.info(
        "[orchestrator] run_rollback complete physician_id=%s run_id=%s",
        physician_id, run_id,
    )


# ---------------------------------------------------------------------------
# Crash-resume: detect and clean up orphaned runs
# ---------------------------------------------------------------------------

async def resume_orphan_runs() -> int:
    """Detect orphaned provisioning runs and run rollback for each.

    An orphaned run has a 'requested' event but no terminal event
    ('rollback_succeeded' or 'rollback_failed') and last activity >5 minutes ago.
    Called once on FastAPI startup (via @app.on_event("startup") in main.py)
    and also callable from scripts/provision_test_doctor.py.

    Per D-09: the log is the source of truth. If the FastAPI process died mid-provision,
    the log preserves what completed; this function resumes rollback.

    Returns the number of orphaned runs cleaned up (may be 0).
    """
    orphans = await ProvisioningLogWriter.list_orphan_runs()

    if not orphans:
        logger.debug("[orchestrator] resume_orphan_runs: no orphaned runs found")
        return 0

    logger.warning(
        "[orchestrator] resume_orphan_runs: found %d orphaned run(s) — rolling back",
        len(orphans),
    )

    for physician_id, run_id in orphans:
        try:
            logger.info(
                "[orchestrator] resume_orphan_runs rolling back physician_id=%s run_id=%s",
                physician_id, run_id,
            )
            await run_rollback(physician_id=physician_id, run_id=run_id)
        except Exception:
            # One bad orphan should not block the rest
            logger.exception(
                "[orchestrator] resume_orphan_runs: rollback failed for run_id=%s — continuing",
                run_id,
            )

    return len(orphans)


# ---------------------------------------------------------------------------
# Domain availability check (read-only, no side effects, no log entries)
# ---------------------------------------------------------------------------

async def check_domain_availability(domain: str, run_id: str) -> dict[str, Any]:
    """Check whether `domain` is available for registration.

    Read-only — no side effects, no log entries. Returns:
    {
        "available": bool,
        "registrar": "cloudflare" | "opensrs" | "mocked",
        "suggestions": []   # Phase 13 adds smart suggestions per PRO-01
    }

    In sandbox mode, always returns available=True (per D-19).

    In real mode:
    - Checks if the domain already exists in Cloudflare zones (if it does, it's
      already provisioned — not available for a new workspace).
    - Cloudflare Registrar availability check is deferred to Phase 13 (beta API
      access not yet granted as of 2026-04-28 — see runbooks/cf-registrar-api-beta-request.md).

    No suggestions in Phase 11 — suggestions logic ships in Phase 13 per PRO-01.
    """
    from typing import Any as _Any

    if _SANDBOX_MODE:
        logger.info(
            "[orchestrator] check_domain_availability sandbox=True → available=True domain=%s run_id=%s",
            domain, run_id,
        )
        return {"available": True, "registrar": "mocked", "suggestions": []}

    # In real mode, check if domain already has a CF zone (implying already provisioned)
    try:
        cf = get_cloudflare_client()
        # Use the internal _get_zone_by_name method — it's a retried idempotent GET
        existing_zone = await cf._get_zone_by_name(domain)
        if existing_zone:
            logger.info(
                "[orchestrator] check_domain_availability domain=%s already has CF zone — not available",
                domain,
            )
            return {"available": False, "registrar": "cloudflare", "suggestions": []}
    except RuntimeError:
        # CF client not configured — can't check
        logger.warning(
            "[orchestrator] check_domain_availability: Cloudflare not configured domain=%s",
            domain,
        )

    # Determine which registrar would handle this domain
    try:
        registrar = get_domain_registrar()
        registrar_name = (
            "cloudflare"
            if DomainRegistrar.CLOUDFLARE_SUPPORTED_TLDS  # type: ignore[attr-defined]
            and domain.rsplit(".", 1)[-1].lower() in DomainRegistrar.CLOUDFLARE_SUPPORTED_TLDS  # type: ignore[attr-defined]
            else "opensrs"
        )
    except RuntimeError:
        registrar_name = "cloudflare"  # default fallback

    # Phase 13 will add actual registrar availability API call
    # For now, return available=True (optimistic) when the domain has no CF zone
    logger.info(
        "[orchestrator] check_domain_availability domain=%s → available=True (optimistic) run_id=%s",
        domain, run_id,
    )
    return {"available": True, "registrar": registrar_name, "suggestions": []}
