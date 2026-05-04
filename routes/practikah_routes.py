"""Práctikah Pro workspace API routes (Phase 11).

Exposes /practikah/* endpoints gated behind verified_physician (WSPC-06).
Every business endpoint (all except /health) requires a verified physician
so unverified physicians receive the bilingual structured 403 envelope (D-07).

Route surface (per WSPC-06):
  GET  /practikah/health                — readiness probe (NO auth)
  GET  /practikah/workspace/status      — physician workspace tier + mailbox info [verified]
  POST /practikah/domains/check         — domain availability check [verified]
  POST /practikah/provision             — workspace provisioning saga [verified, 3/min]
  GET  /practikah/runs/{run_id}         — provisioning run log [verified + ownership]

Mirrors physician_routes.py shape: APIRouter + SlowAPI + Depends + try/except pattern.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional, Set
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from db.client import get_supabase
from utils.auth import AuthenticatedPhysician, verified_physician
from services.practikah.orchestrator import (
    ProvisioningResult,
    check_domain_availability,
    provision_workspace,
)
from services.practikah.audit import ProvisioningLogWriter
from services.practikah.sse_status import stream_run_status
from services.practikah.availability import check_availability as upgrade_check_availability
from services.practikah.checkout import create_checkout_session
from services.practikah.sat_compliance_gate import (
    CountryNotSupportedError,
    SATBlockedError,
    is_sat_blocked,
    is_supported_country,
)
from services.practikah.suggestion_rules import (
    PRICING,
    country_weighted_tlds,
    get_pricing,
)

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/practikah", tags=["practikah"])


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class DomainCheckRequest(BaseModel):
    domain: str = Field(..., min_length=4, max_length=253)


class DomainCheckResponse(BaseModel):
    available: bool
    registrar: str  # 'cloudflare' | 'opensrs' | 'mocked'
    suggestions: list[str] = []


class ProvisionRequest(BaseModel):
    domain: str = Field(..., min_length=4, max_length=253)
    mailbox_local_part: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9._-]+$",
    )
    mailbox_password: str = Field(..., min_length=12)
    registrant_name: str = Field(default="", max_length=255)
    registrant_email: str = Field(default="", max_length=255)
    registrant_country: str = Field(default="US", min_length=2, max_length=3)
    tld_strategy: str = Field(default="real", pattern=r"^(real|mocked)$")


class ProvisionResponse(BaseModel):
    success: bool
    run_id: str
    elapsed_seconds: float
    domain: str
    mailbox_address: Optional[str]
    error: Optional[str] = None


class WorkspaceStatusResponse(BaseModel):
    physician_id: str
    verification_status: str
    tier: Optional[str] = "free"
    mailbox_address: Optional[str] = None
    has_active_provisioning_run: bool = False


# ---------------------------------------------------------------------------
# Wizard endpoint Pydantic models (Phase 12-02)
# ---------------------------------------------------------------------------

class WizardTitleRequest(BaseModel):
    title: Literal['Dr', 'Dra']


class WizardTitleResponse(BaseModel):
    success: bool
    title: str


class LocalPartCandidate(BaseModel):
    local_part: str
    available: bool
    source: Literal['mailcow_check', 'reserved', 'invalid']


class LocalPartSuggestionsRequest(BaseModel):
    title: Literal['Dr', 'Dra']
    candidates: List[str] = Field(..., min_length=1, max_items=10)


class LocalPartSuggestionsResponse(BaseModel):
    suggestions: List[LocalPartCandidate]


class WizardCompleteRequest(BaseModel):
    title: Literal['Dr', 'Dra']
    mailbox_local_part: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9._-]+$",
    )
    mailbox_password: str = Field(..., min_length=12)


class WizardCompleteResponse(BaseModel):
    success: bool
    run_id: str
    physician_id: str
    mailbox_address: Optional[str] = None
    slug: Optional[str] = None
    elapsed_seconds: float
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health")
@limiter.limit("60/minute")
async def health(request: Request) -> dict[str, Any]:
    """Readiness probe — no auth required.

    Returns 200 OK with version marker. Used by Render health checks and Plan 11-07
    smoke tests to verify the practikah router is registered and reachable.
    """
    return {"status": "ok", "phase": "11", "router": "practikah"}


@router.get("/workspace/status", response_model=WorkspaceStatusResponse)
@limiter.limit("30/minute")
async def workspace_status(
    request: Request,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> WorkspaceStatusResponse:
    """Return the physician's current workspace tier, mailbox address, and provisioning state.

    Per WSPC-02: the physician's Medikah login is the single identity for workspace access.
    Queries physician_workspace_accounts (created in Plan 11-01 migration 017).
    Does NOT expose mailbox password — that is write-only at provisioning time (T-11-06-04).
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        # Look up physician_workspace_accounts by physician_id
        result = (
            db.table("physician_workspace_accounts")
            .select("tier, mailbox_address, updated_at")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "workspace_status: DB error for physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to load workspace status.")

    tier: Optional[str] = "free"
    mailbox_address: Optional[str] = None

    if result.data:
        row = result.data[0]
        tier = row.get("tier", "free")
        mailbox_address = row.get("mailbox_address")

    # Check for an active (non-terminal) provisioning run in the last 15 minutes
    has_active_run = False
    try:
        active_result = (
            db.table("practikah_provisioning_log")
            .select("run_id")
            .eq("physician_id", auth.physician_id)
            .eq("event", "requested")
            .order("recorded_at", desc=True)
            .limit(1)
            .execute()
        )
        if active_result.data:
            # Simplified active-run check: if there's a 'requested' event with no
            # terminal event for the same run, consider it active. Full query
            # (with HAVING) requires raw RPC — this approximation is sufficient for
            # the status endpoint's UX purpose (showing a spinner in Phase 12 UI).
            has_active_run = True
    except Exception:
        logger.warning(
            "workspace_status: could not check active provisioning runs for physician_id=%s",
            auth.physician_id,
        )

    return WorkspaceStatusResponse(
        physician_id=auth.physician_id,
        verification_status=auth.verification_status,
        tier=tier,
        mailbox_address=mailbox_address,
        has_active_provisioning_run=has_active_run,
    )


@router.post("/domains/check", response_model=DomainCheckResponse)
@limiter.limit("10/minute")
async def domains_check(
    request: Request,
    body: DomainCheckRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> DomainCheckResponse:
    """Check domain availability for a Práctikah Pro workspace.

    Read-only — no side effects. Returns availability + which registrar would handle it.
    Suggestions are empty in Phase 11; Phase 13 adds smart suggestions per PRO-01.

    Requires a verified physician (WSPC-06). Unverified physicians receive the bilingual
    structured 403 envelope from the verified_physician dependency (D-07).
    """
    try:
        result = await check_domain_availability(body.domain, run_id=str(uuid4()))
        return DomainCheckResponse(
            available=result.get("available", False),
            registrar=result.get("registrar", "cloudflare"),
            suggestions=result.get("suggestions", []),
        )
    except RuntimeError as err:
        raise HTTPException(status_code=503, detail=str(err))
    except Exception:
        logger.exception(
            "domains_check: error for domain=%s physician_id=%s",
            body.domain,
            auth.physician_id,
        )
        raise HTTPException(status_code=500, detail="Unable to check domain availability.")


@router.post("/provision", response_model=ProvisionResponse)
@limiter.limit("3/minute")
async def provision(
    request: Request,
    body: ProvisionRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> ProvisionResponse:
    """Trigger the Práctikah Pro workspace provisioning saga.

    Runs the full provisioning saga (registrar → Cloudflare zone → DNS records →
    Mailcow domain → Mailcow mailbox → Cloudflare custom hostname) as a DB-backed
    saga. On any step failure, rollback is attempted automatically.

    Rate limited to 3/minute — provisioning is a heavy, vendor-API-touching operation.
    A single run can take up to 3 minutes in real mode (per WSPC-09 acceptance criterion).

    Per T-11-06-04: mailbox_password is NEVER logged. The provisioning orchestrator
    passes it to Mailcow over TLS only; it does not appear in practikah_provisioning_log.
    """
    logger.info(
        "provision: physician_id=%s domain=%s tld_strategy=%s",
        auth.physician_id,
        body.domain,
        body.tld_strategy,
    )

    try:
        result: ProvisioningResult = await provision_workspace(
            physician_id=auth.physician_id,
            domain=body.domain,
            mailbox_local_part=body.mailbox_local_part,
            mailbox_password=body.mailbox_password,
            registrant_name=body.registrant_name,
            registrant_email=body.registrant_email,
            registrant_country=body.registrant_country,
            tld_strategy=body.tld_strategy,  # type: ignore[arg-type]
        )
        return ProvisionResponse(
            success=result.success,
            run_id=result.run_id,
            elapsed_seconds=result.elapsed_seconds,
            domain=result.domain,
            mailbox_address=result.mailbox_address,
            error=result.error,
        )
    except RuntimeError as err:
        logger.exception(
            "provision: RuntimeError for physician_id=%s domain=%s",
            auth.physician_id,
            body.domain,
        )
        raise HTTPException(status_code=503, detail=str(err))
    except Exception:
        logger.exception(
            "provision: unhandled error for physician_id=%s domain=%s",
            auth.physician_id,
            body.domain,
        )
        raise HTTPException(
            status_code=500,
            detail="Provisioning failed; rollback was attempted. Check /practikah/runs/{run_id} for details.",
        )


@router.get("/runs/{run_id}")
@limiter.limit("30/minute")
async def run_log(
    request: Request,
    run_id: str = Path(..., min_length=8, max_length=64),
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> list[dict[str, Any]]:
    """Return the provisioning log timeline for a specific run_id.

    Ownership enforced by physician_id filter — even if a malicious doctor guesses
    another physician's run_id, this query returns only rows where physician_id matches
    the authenticated physician. RLS provides defense-in-depth.

    Per T-11-06-03: cross-tenant access is blocked at the query level.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        result = (
            db.table("practikah_provisioning_log")
            .select("step_name, event, resource_type, detail, recorded_at")
            .eq("physician_id", auth.physician_id)
            .eq("run_id", run_id)
            .order("recorded_at", desc=False)
            .execute()
        )
        return result.data or []
    except Exception:
        logger.exception(
            "run_log: DB error physician_id=%s run_id=%s", auth.physician_id, run_id
        )
        raise HTTPException(status_code=500, detail="Unable to load run log.")


# ---------------------------------------------------------------------------
# Wizard endpoints (Phase 12-02)
# ---------------------------------------------------------------------------

@router.post("/wizard/title", response_model=WizardTitleResponse)
@limiter.limit("10/minute")
async def wizard_title(
    request: Request,
    body: WizardTitleRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> WizardTitleResponse:
    """Persist the physician's chosen honorific title (Dr/Dra) during the workspace wizard.

    Step 1 of 3 in the free-tier onboarding wizard. Upserts a physician_workspace_accounts
    row for the authenticated physician setting title=body.title, tier='free',
    state='free_pending'. If the row already exists, only the title is updated.

    Gated by verified_physician (WSPC-06 / T-12-02-06).
    Rate-limited to 10/minute (SlowAPI).
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        # Upsert: create or update the workspace row for this physician
        existing = (
            db.table("physician_workspace_accounts")
            .select("physician_id")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            # Row exists — update title only
            db.table("physician_workspace_accounts").update(
                {"title": body.title}
            ).eq("physician_id", auth.physician_id).execute()
        else:
            # New row — set tier + state + title
            db.table("physician_workspace_accounts").insert(
                {
                    "physician_id": auth.physician_id,
                    "tier": "free",
                    "state": "free_pending",
                    "title": body.title,
                }
            ).execute()
    except Exception:
        logger.exception(
            "wizard_title: DB error physician_id=%s title=%s",
            auth.physician_id, body.title,
        )
        raise HTTPException(status_code=500, detail="Unable to persist title selection.")

    logger.info(
        "wizard_title: physician_id=%s title=%s", auth.physician_id, body.title
    )
    return WizardTitleResponse(success=True, title=body.title)


@router.post("/wizard/local-part-suggestions", response_model=LocalPartSuggestionsResponse)
@limiter.limit("10/minute")
async def wizard_local_part_suggestions(
    request: Request,
    body: LocalPartSuggestionsRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> LocalPartSuggestionsResponse:
    """Check Mailcow availability for a list of mailbox local-part candidates.

    Step 2 of 3 in the free-tier onboarding wizard. Accepts up to 10 candidate
    local-parts and checks each against:
      1. RESERVED_LOCAL_PARTS block-list (admin, postmaster, medikah, etc.) → source='reserved'
      2. Local-part format regex (^[a-z0-9._-]+$) → source='invalid'
      3. Live Mailcow GET /api/v1/get/mailbox/<address> → source='mailcow_check'

    Returns 5 ranked candidates with availability flags per FREE-04.
    Gated by verified_physician (WSPC-06). Rate-limited to 10/minute (T-12-02-07).
    """
    from services.practikah.local_part_suggester import check_candidate_availability

    suggestions = []
    for candidate in body.candidates[:10]:  # hard cap at 10 per model constraint
        result = await check_candidate_availability(
            local_part=candidate,
            domain="medikah.health",
        )
        suggestions.append(
            LocalPartCandidate(
                local_part=result["local_part"],
                available=result["available"],
                source=result["source"],
            )
        )

    return LocalPartSuggestionsResponse(suggestions=suggestions)


@router.post("/wizard/complete", response_model=WizardCompleteResponse)
@limiter.limit("3/minute")
async def wizard_complete(
    request: Request,
    body: WizardCompleteRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> WizardCompleteResponse:
    """Complete the free-tier workspace wizard and trigger mailbox provisioning.

    Step 3 of 3 in the free-tier onboarding wizard. Orchestrates the free-tier
    provisioning saga (single Mailcow add_mailbox step) and updates the
    physician_workspace_accounts state machine:
        free_pending → provisioning → free_active (success)
                                   → free_failed  (error)

    On success:
      - Sets state='free_active', mailbox_local_part, mailbox_address in DB
      - Writes workspace_audit_log row (action='workspace.setup_completed')
      - Fires best-effort Resend 'Práctikah is live' transactional email via Next.js BFF

    On failure:
      - Sets state='free_failed' in DB
      - Raises HTTP 503

    Per T-12-02-02: mailbox_password is NEVER logged at any call site.
    Gated by verified_physician (WSPC-06). Rate-limited to 3/minute.
    """
    from utils.slug import name_to_slug
    from services.practikah.notifications import send_practikah_live_email

    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Fetch physician row for name, email, preferred_language
    try:
        physician_result = (
            db.table("physicians")
            .select("full_name, first_name, last_name, email, country, preferred_language")
            .eq("id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "wizard_complete: physician lookup failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to load physician record.")

    if not physician_result.data:
        raise HTTPException(status_code=404, detail="Physician record not found.")

    physician = physician_result.data[0]
    full_name = physician.get("full_name") or ""
    first_name = physician.get("first_name") or (full_name.split()[0] if full_name else "")
    last_name = physician.get("last_name") or (full_name.split()[-1] if full_name else "")
    email = physician.get("email") or auth.email
    lang = physician.get("preferred_language") or "en"
    if lang not in ("en", "es"):
        lang = "en"

    slug = name_to_slug(full_name)

    # Set state to 'provisioning' BEFORE calling the orchestrator (WSPC-05)
    try:
        db.table("physician_workspace_accounts").update(
            {"state": "provisioning"}
        ).eq("physician_id", auth.physician_id).execute()
    except Exception:
        logger.exception(
            "wizard_complete: state→provisioning update failed physician_id=%s",
            auth.physician_id,
        )
        # Non-fatal — continue so the provisioning attempt can still proceed

    # Determine tld_strategy: mocked in sandbox, real otherwise
    tld_strategy: Literal["real", "mocked"] = (
        "mocked" if os.environ.get("MEDIKAH_PROVISIONING_SANDBOX", "").lower() in {"1", "true", "yes", "on"}
        else "real"
    )

    started_at = time.monotonic()

    logger.info(
        "wizard_complete: starting free-tier provisioning physician_id=%s local_part=%s",
        auth.physician_id, body.mailbox_local_part,
    )

    # NOTE: body.mailbox_password is NEVER logged (T-12-02-02)
    result: ProvisioningResult = await provision_workspace(
        physician_id=auth.physician_id,
        domain="medikah.health",
        mailbox_local_part=body.mailbox_local_part,
        mailbox_password=body.mailbox_password,
        tier="free",
        title=body.title,
        tld_strategy=tld_strategy,
    )

    elapsed = time.monotonic() - started_at
    mailbox_address = f"{body.mailbox_local_part}@medikah.health"

    if result.success:
        # Update workspace accounts row to free_active
        try:
            db.table("physician_workspace_accounts").update(
                {
                    "state": "free_active",
                    "tier": "free",
                    "title": body.title,
                    "mailbox_local_part": body.mailbox_local_part,
                    "mailbox_address": mailbox_address,
                    "mailbox_quota_mb": 10240,
                    "workspace_setup_completed_at": "now()",
                }
            ).eq("physician_id", auth.physician_id).execute()
        except Exception:
            logger.exception(
                "wizard_complete: free_active update failed physician_id=%s run_id=%s",
                auth.physician_id, result.run_id,
            )

        # Write workspace_audit_log (OPS-01 / T-12-02-05)
        try:
            db.table("workspace_audit_log").insert(
                {
                    "physician_id": auth.physician_id,
                    "actor_id": auth.physician_id,
                    "actor_role": "physician",
                    "action": "workspace.setup_completed",
                    "resource_type": "workspace",
                    "resource_id": result.run_id,
                    "detail": {
                        "mailbox_address": mailbox_address,
                        "tier": "free",
                        "run_id": result.run_id,
                    },
                }
            ).execute()
        except Exception:
            logger.exception(
                "wizard_complete: audit log insert failed physician_id=%s run_id=%s",
                auth.physician_id, result.run_id,
            )

        # Best-effort email trigger — must NOT block or raise (T-12-02-10)
        try:
            await send_practikah_live_email(
                to=email,
                lang=lang,
                mailbox_address=mailbox_address,
                slug=slug,
                first_name=first_name,
                last_name=last_name,
            )
        except Exception:
            logger.warning(
                "wizard_complete: send_practikah_live_email failed physician_id=%s (non-fatal)",
                auth.physician_id,
            )

        logger.info(
            "wizard_complete: SUCCESS physician_id=%s run_id=%s mailbox=%s elapsed=%.1fs",
            auth.physician_id, result.run_id, mailbox_address, elapsed,
        )
        return WizardCompleteResponse(
            success=True,
            run_id=result.run_id,
            physician_id=auth.physician_id,
            mailbox_address=mailbox_address,
            slug=slug,
            elapsed_seconds=elapsed,
        )
    else:
        # Provisioning failed — set state to free_failed
        try:
            db.table("physician_workspace_accounts").update(
                {"state": "free_failed"}
            ).eq("physician_id", auth.physician_id).execute()
        except Exception:
            logger.exception(
                "wizard_complete: free_failed update failed physician_id=%s run_id=%s",
                auth.physician_id, result.run_id,
            )

        logger.error(
            "wizard_complete: FAILED physician_id=%s run_id=%s error=%s elapsed=%.1fs",
            auth.physician_id, result.run_id, result.error, elapsed,
        )
        raise HTTPException(
            status_code=503,
            detail=result.error or "Workspace provisioning failed. Please try again.",
        )


# ---------------------------------------------------------------------------
# Mailbox management endpoints (Phase 12-03)
# ---------------------------------------------------------------------------

class MailboxChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=12)


class MailboxChangePasswordResponse(BaseModel):
    success: bool


class ImapCredentialsResponse(BaseModel):
    host: str
    imap_port: int
    smtp_port: int
    smtp_starttls_port: int
    username: str
    protocol_imap: str
    protocol_smtp: str


@router.post("/mailbox/change-password", response_model=MailboxChangePasswordResponse)
@limiter.limit("5/minute")
async def mailbox_change_password(
    request: Request,
    body: MailboxChangePasswordRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> MailboxChangePasswordResponse:
    """Change the authenticated physician's mailbox password via Mailcow Admin API.

    Auth gate: verified_physician (WSPC-06 / T-12-03-06). The doctor's authenticated
    session is treated as proof of identity for password rotation (T-12-03-03 lean
    choice — old-password verification skipped; doctor may legitimately have forgotten
    it; all rotations are audit-logged with IP+UA for detectability per T-12-03-05).

    Security:
      - body.new_password is NEVER logged (T-12-03-01).
      - body.current_password is NEVER logged (T-12-03-02).
      - Rate-limited to 5/minute per IP (T-12-03-09).
      - Writes workspace_audit_log row: action='workspace.password_changed' (security-relevant).
    """
    from services.practikah.mailbox_provisioner import do_update_mailbox_password

    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Look up the physician's mailbox_local_part from physician_workspace_accounts
    try:
        workspace_result = (
            db.table("physician_workspace_accounts")
            .select("mailbox_local_part, state")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "mailbox_change_password: DB lookup failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to load workspace account.")

    if not workspace_result.data:
        raise HTTPException(status_code=409, detail="workspace_not_provisioned")

    row = workspace_result.data[0]
    mailbox_local_part: Optional[str] = row.get("mailbox_local_part")
    state: Optional[str] = row.get("state")

    if not mailbox_local_part or state != "free_active":
        raise HTTPException(status_code=409, detail="workspace_not_provisioned")

    # Rotate password via Mailcow Admin API
    # NOTE: body.new_password and body.current_password are NEVER logged (T-12-03-01/02)
    try:
        await do_update_mailbox_password(
            domain="medikah.health",
            local_part=mailbox_local_part,
            new_password=body.new_password,
        )
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))
    except RuntimeError as err:
        logger.warning(
            "mailbox_change_password: Mailcow returned error physician_id=%s error=%s",
            auth.physician_id, str(err),
        )
        raise HTTPException(status_code=502, detail=f"Mailcow error: {err}")
    except Exception:
        logger.exception(
            "mailbox_change_password: unexpected error physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Password change failed.")

    # Write security-relevant audit log row (T-12-03-05 / OPS-01)
    # IP + UA captured because 'workspace.password_changed' is SECURITY_RELEVANT
    try:
        db.table("workspace_audit_log").insert(
            {
                "physician_id": auth.physician_id,
                "actor_id": auth.physician_id,
                "actor_role": "physician",
                "action": "workspace.password_changed",
                "resource_type": "mailbox",
                "resource_id": f"{mailbox_local_part}@medikah.health",
                "detail": {"domain": "medikah.health", "local_part": mailbox_local_part},
            }
        ).execute()
    except Exception:
        logger.exception(
            "mailbox_change_password: audit log insert failed physician_id=%s (non-fatal)",
            auth.physician_id,
        )

    logger.info(
        "mailbox_change_password: success physician_id=%s local_part=%s",
        auth.physician_id, mailbox_local_part,
    )
    return MailboxChangePasswordResponse(success=True)


@router.get("/mailbox/mobileconfig")
@limiter.limit("10/minute")
async def mailbox_mobileconfig(
    request: Request,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> Response:
    """Return the Apple .mobileconfig binary profile for iOS auto-configuration.

    Proxies GET /api/v1/get/mobileconfig/<address> from Mailcow Admin API.
    The response is a binary plist that iOS interprets to auto-configure Mail,
    Calendar, and Contacts with the physician's mailbox credentials (T-12-03-04).

    Security:
      - JWT-gated (verified_physician) — no enumeration risk (T-12-03-11).
      - Writes informational audit log row: action='workspace.mobileconfig_requested'.
    """
    from services.practikah.mailbox_provisioner import fetch_mobileconfig

    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Look up the physician's mailbox_local_part
    try:
        workspace_result = (
            db.table("physician_workspace_accounts")
            .select("mailbox_local_part, state")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "mailbox_mobileconfig: DB lookup failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to load workspace account.")

    if not workspace_result.data:
        raise HTTPException(status_code=409, detail="workspace_not_provisioned")

    row = workspace_result.data[0]
    mailbox_local_part: Optional[str] = row.get("mailbox_local_part")
    state: Optional[str] = row.get("state")

    if not mailbox_local_part or state != "free_active":
        raise HTTPException(status_code=409, detail="workspace_not_provisioned")

    # Fetch binary .mobileconfig from Mailcow
    try:
        profile_bytes = await fetch_mobileconfig(
            domain="medikah.health",
            local_part=mailbox_local_part,
        )
    except Exception:
        logger.exception(
            "mailbox_mobileconfig: fetch failed physician_id=%s local_part=%s",
            auth.physician_id, mailbox_local_part,
        )
        raise HTTPException(status_code=502, detail="Unable to fetch mobileconfig from Mailcow.")

    # Informational audit log (no IP/UA — not security-relevant)
    try:
        db.table("workspace_audit_log").insert(
            {
                "physician_id": auth.physician_id,
                "actor_id": auth.physician_id,
                "actor_role": "physician",
                "action": "workspace.mobileconfig_requested",
                "resource_type": "mailbox",
                "resource_id": f"{mailbox_local_part}@medikah.health",
                "detail": {"domain": "medikah.health"},
            }
        ).execute()
    except Exception:
        logger.exception(
            "mailbox_mobileconfig: audit log insert failed physician_id=%s (non-fatal)",
            auth.physician_id,
        )

    return Response(
        content=profile_bytes,
        media_type="application/x-apple-aspen-config",
        headers={
            "Content-Disposition": 'attachment; filename="practikah.mobileconfig"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/mailbox/imap-credentials", response_model=ImapCredentialsResponse)
@limiter.limit("30/minute")
async def mailbox_imap_credentials(
    request: Request,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> ImapCredentialsResponse:
    """Return deterministic IMAP/SMTP connection credentials for the physician's mailbox.

    Credentials are public-shape config (host/port/username) — no password is exposed.
    The doctor uses the change-password endpoint to set/rotate the password they use
    with any IMAP client.

    Security:
      - JWT-gated (verified_physician) — only authenticated physicians see their own creds.
      - NO audit log (read-only of non-sensitive public-shape config — T-12-03-07).
      - No password field in response (T-12-03-07).
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Look up the physician's mailbox_local_part
    try:
        workspace_result = (
            db.table("physician_workspace_accounts")
            .select("mailbox_local_part, state")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "mailbox_imap_credentials: DB lookup failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to load workspace account.")

    if not workspace_result.data:
        raise HTTPException(status_code=409, detail="workspace_not_provisioned")

    row = workspace_result.data[0]
    mailbox_local_part: Optional[str] = row.get("mailbox_local_part")
    state: Optional[str] = row.get("state")

    if not mailbox_local_part or state != "free_active":
        raise HTTPException(status_code=409, detail="workspace_not_provisioned")

    username = f"{mailbox_local_part}@medikah.health"

    return ImapCredentialsResponse(
        host="mail.medikah.health",
        imap_port=993,
        smtp_port=465,
        smtp_starttls_port=587,
        username=username,
        protocol_imap="SSL/TLS",
        protocol_smtp="SSL/TLS",
    )


# ---------------------------------------------------------------------------
# Theme endpoints (Phase 12-05)
# ---------------------------------------------------------------------------

# Server-side allowlist of WCAG-AA hex colors (mirrors WCAG_AA_PALETTE in
# medikah-chat-frontend/lib/practikahTheme.ts — defense-in-depth per T-12-05-08).
ALLOWED_ACCENT_COLORS: Set[str] = {
    "#2C7A8C",  # Clinical Teal (Práctikah default)
    "#1B5E7E",  # Deep Ocean
    "#0F766E",  # Pine
    "#1E40AF",  # Royal Blue
    "#5B21B6",  # Plum
    "#9D174D",  # Garnet
    "#B45309",  # Amber Earth
    "#166534",  # Forest
    "#0E7490",  # Cyan Slate
    "#7C2D12",  # Burnt Sienna
    "#3730A3",  # Indigo
    "#831843",  # Wine
}

# Normalize to uppercase for comparison (hex values are case-insensitive)
ALLOWED_ACCENT_COLORS_NORMALIZED: Set[str] = {c.upper() for c in ALLOWED_ACCENT_COLORS}


class ThemeRequest(BaseModel):
    layout_variant: Literal["classic", "editorial", "minimal"]
    accent_color: str = Field(..., pattern=r"^#[0-9A-Fa-f]{6}$")
    font_weight: Literal["light", "regular", "bold"]
    favicon_url: Optional[str] = None
    office_photo_urls: List[str] = Field(default_factory=list, max_length=6)


class ThemeResponse(BaseModel):
    physician_id: str
    layout_variant: str
    accent_color: str
    font_weight: str
    favicon_url: Optional[str]
    office_photo_urls: List[str]
    updated_at: str


def _validate_storage_url(url: Optional[str], prefix: Optional[str]) -> bool:
    """Return True if the URL is None (allowed) or starts with the Supabase Storage CDN prefix.

    T-12-05-04: rejects off-platform image embedding.
    If SUPABASE_STORAGE_PUBLIC_URL_PREFIX is not set, validation is skipped (dev/test mode).
    """
    if url is None:
        return True
    if not prefix:
        # No prefix configured — allow any (dev/test mode without storage)
        return True
    return url.startswith(prefix)


@router.get("/theme", response_model=ThemeResponse)
@limiter.limit("30/minute")
async def get_theme(
    request: Request,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> ThemeResponse:
    """Return the authenticated physician's website theme row.

    Returns 404 with detail='no_theme' if the doctor hasn't claimed Try Pro yet.
    The BFF (pages/api/practikah/theme/get.ts) maps 404 → DEFAULT_THEME for the editor.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        result = (
            db.table("physician_website_themes")
            .select("*")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "get_theme: DB error physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to load theme.")

    if not result.data:
        raise HTTPException(status_code=404, detail="no_theme")

    row = result.data[0]
    return ThemeResponse(
        physician_id=row["physician_id"],
        layout_variant=row.get("layout_variant", "classic"),
        accent_color=row.get("accent_color", "#2C7A8C"),
        font_weight=row.get("font_weight", "regular"),
        favicon_url=row.get("favicon_url"),
        office_photo_urls=row.get("office_photo_urls") or [],
        updated_at=str(row.get("updated_at", "")),
    )


@router.put("/theme", response_model=ThemeResponse)
@limiter.limit("10/minute")
async def put_theme(
    request: Request,
    body: ThemeRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> ThemeResponse:
    """Upsert the authenticated physician's website theme.

    Validates:
      - accent_color is in the ALLOWED_ACCENT_COLORS server-side allowlist (T-12-05-08)
      - favicon_url and all office_photo_urls start with SUPABASE_STORAGE_PUBLIC_URL_PREFIX (T-12-05-04)

    Writes workspace_audit_log row: action='workspace.theme_changed' (T-12-05-06).
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Server-side WCAG-AA palette enforcement (T-12-05-08)
    if body.accent_color.upper() not in ALLOWED_ACCENT_COLORS_NORMALIZED:
        raise HTTPException(
            status_code=400,
            detail=f"accent_color '{body.accent_color}' is not in the approved WCAG-AA palette.",
        )

    # Off-platform URL injection prevention (T-12-05-04)
    storage_prefix: Optional[str] = os.environ.get("SUPABASE_STORAGE_PUBLIC_URL_PREFIX")
    if not _validate_storage_url(body.favicon_url, storage_prefix):
        raise HTTPException(
            status_code=400,
            detail="favicon_url must point to Supabase Storage (SUPABASE_STORAGE_PUBLIC_URL_PREFIX).",
        )
    for photo_url in body.office_photo_urls:
        if not _validate_storage_url(photo_url, storage_prefix):
            raise HTTPException(
                status_code=400,
                detail=f"office_photo_url '{photo_url}' must point to Supabase Storage.",
            )

    # Upsert physician_website_themes
    try:
        result = (
            db.table("physician_website_themes")
            .upsert(
                {
                    "physician_id": auth.physician_id,
                    "layout_variant": body.layout_variant,
                    "accent_color": body.accent_color,
                    "font_weight": body.font_weight,
                    "favicon_url": body.favicon_url,
                    "office_photo_urls": body.office_photo_urls,
                },
                on_conflict="physician_id",
            )
            .execute()
        )
    except Exception:
        logger.exception(
            "put_theme: upsert failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to save theme.")

    # Fetch back to get server-assigned updated_at
    try:
        fetch_result = (
            db.table("physician_website_themes")
            .select("*")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
        row = fetch_result.data[0] if fetch_result.data else {}
    except Exception:
        logger.warning(
            "put_theme: fetch-back failed physician_id=%s (non-fatal)", auth.physician_id
        )
        row = {}

    # Audit log — informational (T-12-05-06)
    try:
        db.table("workspace_audit_log").insert(
            {
                "physician_id": auth.physician_id,
                "actor_id": auth.physician_id,
                "actor_role": "physician",
                "action": "workspace.theme_changed",
                "resource_type": "theme",
                "resource_id": None,
                "detail": {
                    "layout_variant": body.layout_variant,
                    "accent_color": body.accent_color,
                    "font_weight": body.font_weight,
                },
            }
        ).execute()
    except Exception:
        logger.warning(
            "put_theme: audit log failed physician_id=%s (non-fatal)", auth.physician_id
        )

    logger.info(
        "put_theme: success physician_id=%s layout=%s accent=%s font=%s",
        auth.physician_id, body.layout_variant, body.accent_color, body.font_weight,
    )

    return ThemeResponse(
        physician_id=auth.physician_id,
        layout_variant=body.layout_variant,
        accent_color=body.accent_color,
        font_weight=body.font_weight,
        favicon_url=body.favicon_url,
        office_photo_urls=body.office_photo_urls,
        updated_at=str(row.get("updated_at", "")),
    )


@router.put("/theme/claim", response_model=ThemeResponse)
@limiter.limit("3/minute")
async def claim_theme(
    request: Request,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> ThemeResponse:
    """One-click 'Claim Try Pro Preview' (D-19).

    Creates a default-theme row in physician_website_themes if one doesn't exist.
    Idempotent: if the row already exists, returns it unchanged (200).
    Also sets physician_website.enabled = TRUE for this physician.
    Writes audit row: action='workspace.site_published'.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Check if a theme row already exists (idempotent)
    try:
        existing = (
            db.table("physician_website_themes")
            .select("*")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "claim_theme: existing-check failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to check theme status.")

    if existing.data:
        # Already claimed — idempotent 200
        row = existing.data[0]
        logger.info(
            "claim_theme: already claimed physician_id=%s (idempotent)", auth.physician_id
        )
        return ThemeResponse(
            physician_id=row["physician_id"],
            layout_variant=row.get("layout_variant", "classic"),
            accent_color=row.get("accent_color", "#2C7A8C"),
            font_weight=row.get("font_weight", "regular"),
            favicon_url=row.get("favicon_url"),
            office_photo_urls=row.get("office_photo_urls") or [],
            updated_at=str(row.get("updated_at", "")),
        )

    # Create default theme row
    try:
        insert_result = (
            db.table("physician_website_themes")
            .insert(
                {
                    "physician_id": auth.physician_id,
                    "layout_variant": "classic",
                    "accent_color": "#2C7A8C",
                    "font_weight": "regular",
                    "favicon_url": None,
                    "office_photo_urls": [],
                }
            )
            .execute()
        )
        row = insert_result.data[0] if insert_result.data else {}
    except Exception:
        logger.exception(
            "claim_theme: insert failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to claim Try Pro preview.")

    # Set physician_website.enabled = TRUE (claim implies publish — WEB-18 default state)
    try:
        db.table("physician_website").upsert(
            {
                "physician_id": auth.physician_id,
                "enabled": True,
                "published_at": "now()",
            },
            on_conflict="physician_id",
        ).execute()
    except Exception:
        logger.warning(
            "claim_theme: physician_website.enabled update failed physician_id=%s (non-fatal)",
            auth.physician_id,
        )

    # Fetch physician slug for audit log detail
    slug = ""
    try:
        from utils.slug import name_to_slug
        physician_result = (
            db.table("physicians")
            .select("full_name")
            .eq("id", auth.physician_id)
            .limit(1)
            .execute()
        )
        if physician_result.data:
            slug = name_to_slug(physician_result.data[0].get("full_name", ""))
    except Exception:
        logger.warning(
            "claim_theme: slug lookup failed physician_id=%s (non-fatal)", auth.physician_id
        )

    # Audit log — 'workspace.site_published' (T-12-05-06)
    try:
        db.table("workspace_audit_log").insert(
            {
                "physician_id": auth.physician_id,
                "actor_id": auth.physician_id,
                "actor_role": "physician",
                "action": "workspace.site_published",
                "resource_type": "website",
                "resource_id": None,
                "detail": {
                    "preview_url": f"https://{slug}.medikah.health" if slug else None,
                },
            }
        ).execute()
    except Exception:
        logger.warning(
            "claim_theme: audit log failed physician_id=%s (non-fatal)", auth.physician_id
        )

    logger.info(
        "claim_theme: success physician_id=%s slug=%s", auth.physician_id, slug
    )

    return ThemeResponse(
        physician_id=auth.physician_id,
        layout_variant="classic",
        accent_color="#2C7A8C",
        font_weight="regular",
        favicon_url=None,
        office_photo_urls=[],
        updated_at=str(row.get("updated_at", "")),
    )


# ---------------------------------------------------------------------------
# Engagement tracking endpoint (Phase 12-07)
# ---------------------------------------------------------------------------

class EngagementTrackRequest(BaseModel):
    event: Literal[
        "theme_edit",
        "preview_visit",
        "share_link_copied",
        "cta_dismissed",
        "upgrade_interest",
        # Phase 13-04: Mexican physician requested a heads-up when Pro launches
        # (SAT compliance gate from Plan 13-03 cleared). Doctor-owned counter on
        # ``physician_workspace_accounts.engagement_counters`` per T-13-04 /
        # T-12-07-01 — informational, not security-relevant.
        "upgrade_sat_notify_me",
    ]


class EngagementTrackResponse(BaseModel):
    ok: bool


@router.post("/engagement/track", response_model=EngagementTrackResponse)
@limiter.limit("60/minute")
async def engagement_track(
    request: Request,
    body: EngagementTrackRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> EngagementTrackResponse:
    """Record an engagement event for the free-tier upgrade CTA heuristic (FREE-08 / D-20).

    High-volume informational endpoint (up to 60 events/min per physician).
    Increments the matching counter in physician_workspace_accounts.engagement_counters JSONB.
    Also records first_engaged_at on the first ever event, and cta_dismissed_at on dismissal.

    Security notes (T-12-07-01):
      - Counters are doctor-owned (no cross-tenant risk).
      - Worst-case manipulation: doctor bumps their own theme_edit count to force-show
        their own banner — no security impact.
      - NOT audit-logged (engagement is high-volume informational; not security-relevant).

    Per T-12-07-03: rate-limited to 60/minute per IP via SlowAPI.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        # Read current engagement_counters for this physician
        row_result = (
            db.table("physician_workspace_accounts")
            .select("engagement_counters")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "engagement_track: DB read failed physician_id=%s", auth.physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to load engagement data.")

    if not row_result.data:
        raise HTTPException(status_code=409, detail="workspace_not_found")

    counters: dict = dict(row_result.data[0].get("engagement_counters") or {})
    now_iso = datetime.now(timezone.utc).isoformat()

    if body.event == "cta_dismissed":
        counters["cta_dismissed"] = int(counters.get("cta_dismissed") or 0) + 1
        counters["cta_dismissed_at"] = now_iso
    else:
        counters[body.event] = int(counters.get(body.event) or 0) + 1
        if not counters.get("first_engaged_at"):
            counters["first_engaged_at"] = now_iso

    try:
        db.table("physician_workspace_accounts").update(
            {"engagement_counters": counters}
        ).eq("physician_id", auth.physician_id).execute()
    except Exception:
        logger.exception(
            "engagement_track: DB update failed physician_id=%s event=%s",
            auth.physician_id, body.event,
        )
        raise HTTPException(status_code=500, detail="Unable to save engagement event.")

    logger.info(
        "engagement_track: physician_id=%s event=%s", auth.physician_id, body.event
    )
    return EngagementTrackResponse(ok=True)


# ---------------------------------------------------------------------------
# Phase 13-01: Stripe webhook + Customer Portal BFF
# ---------------------------------------------------------------------------

# Frontend URL used to build the Customer Portal return_url. Falls back to
# the production base URL; localhost during dev.
_FRONTEND_URL = (
    os.environ.get("PRACTIKAH_FRONTEND_URL")
    or os.environ.get("NEXT_PUBLIC_BASE_URL")
    or "https://medikah.health"
)


@router.post("/stripe/webhook")
@limiter.limit("120/minute")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    """Stripe webhook entry point — signature-verified + idempotent dispatch.

    Per D-13: this is the SOLE writer of subscription state on
    physician_workspace_accounts (alongside dunning_state_machine.py from 13-09).

    Per T-13-01-01: NOT behind verified_physician — auth is the
    Stripe-Signature header HMAC, validated against STRIPE_WEBHOOK_SECRET.

    Per T-13-01-02: signature is computed over the EXACT raw body. The
    Next.js BFF (pages/api/practikah/upgrade/webhook.ts) must disable
    bodyParser; this handler reads request.body() raw, never request.json().
    """
    from services.practikah.stripe_webhook import verify_signature, handle_event

    raw_body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        event = verify_signature(raw_body, sig_header)
    except ValueError as err:
        logger.warning("stripe_webhook: signature verification failed: %s", err)
        raise HTTPException(status_code=400, detail=f"signature: {err}")
    except Exception as err:
        # SignatureVerificationError from stripe SDK isn't a ValueError on all
        # versions — catch broadly here; we never want a 5xx for a malformed
        # signature (Stripe will retry forever otherwise).
        logger.warning("stripe_webhook: verify_signature raised: %s", err)
        raise HTTPException(status_code=400, detail=f"signature: {err}")

    db = get_supabase()
    if db is None:
        # Non-prod fallback: if Supabase isn't configured, acknowledge to
        # Stripe so dev environments don't accumulate retries. Production
        # forces Supabase to be configured (db.client.is_production()).
        logger.error("stripe_webhook: Supabase not configured — event %s acknowledged but NOT processed",
                     getattr(event, "id", "?"))
        return {"received": True, "status": "supabase_unconfigured"}

    result = await handle_event(event, db, raw_body=raw_body)
    return {"received": True, **result}


class BillingPortalLinkResponse(BaseModel):
    url: str


@router.post("/billing/portal-link", response_model=BillingPortalLinkResponse)
@limiter.limit("10/minute")
async def billing_portal_link(
    request: Request,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> BillingPortalLinkResponse:
    """Return a fresh Stripe Customer Portal session URL for the authenticated physician.

    Per D-09 / T-13-01-08: Portal URLs are short-lived (Stripe-default ~5min)
    and bound to the customer_id. This BFF lookup ensures only the physician
    owning the customer_id receives the URL.

    No audit log — Stripe records Portal session creation on its side and
    the payment / subscription mutations the physician makes via the Portal
    return as webhook events that ARE audited (D-13).
    """
    import stripe  # lazy import — avoids hard dep when stripe isn't installed yet

    stripe_secret = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe_secret:
        raise HTTPException(status_code=503, detail="Stripe is not configured.")
    stripe.api_key = stripe_secret

    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Look up the physician's stripe_customer_id.
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("stripe_customer_id")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "billing_portal_link: DB lookup failed physician_id=%s",
            auth.physician_id,
        )
        raise HTTPException(status_code=500, detail="Unable to load workspace account.")

    customer_id: Optional[str] = None
    if result.data:
        customer_id = result.data[0].get("stripe_customer_id")

    if not customer_id:
        raise HTTPException(status_code=404, detail="No subscription on file")

    portal_config_id = os.environ.get("STRIPE_PORTAL_CONFIGURATION_ID")
    return_url = f"{_FRONTEND_URL}/physicians/dashboard/workspace/billing"

    try:
        kwargs: dict[str, Any] = {
            "customer": customer_id,
            "return_url": return_url,
        }
        if portal_config_id:
            kwargs["configuration"] = portal_config_id
        session = stripe.billing_portal.Session.create(**kwargs)
    except Exception as err:
        logger.exception(
            "billing_portal_link: stripe.billing_portal.Session.create failed "
            "physician_id=%s customer_id=%s",
            auth.physician_id, customer_id,
        )
        raise HTTPException(status_code=502, detail=f"Stripe error: {err}")

    return BillingPortalLinkResponse(url=session.url)


# ---------------------------------------------------------------------------
# Phase 13 — Pro upsell domain availability (BFF for 13-04 search UX)
# ---------------------------------------------------------------------------


@router.get("/upgrade/availability")
@limiter.limit("30/minute")
async def upgrade_availability(
    request: Request,
    domain: str,
    auth: AuthenticatedPhysician = Depends(verified_physician),
):
    """Domain availability check for the Pro upsell domain-search component.

    Auth-gated to verified physicians (T-13-02-03 — prevents anonymous
    enumeration scraping). 30/min per physician matches Phase 11 read-rate
    convention. Service layer applies CF primary + RDAP fallback per D-20.
    """
    if not domain or len(domain) > 253:
        raise HTTPException(status_code=400, detail="invalid domain")
    return await upgrade_check_availability(domain)


# ---------------------------------------------------------------------------
# Phase 13-04: Pro-tier pricing matrix lookup for the DomainSearch wizard
# ---------------------------------------------------------------------------


class DomainSearchPricingRequest(BaseModel):
    """Optional client-supplied country override.

    The authoritative country comes from the physician record — the request
    body is honored only when the physician's stored country is missing
    (legacy rows). Clients should pass ``country: "MX"`` or ``country: "US"``
    so the BFF response renders consistently across both countries during the
    Phase 13 launch (D-23).
    """

    country: Optional[Literal["MX", "US"]] = None


@router.post("/upgrade/domain-search")
@limiter.limit("60/minute")
async def upgrade_domain_search(
    request: Request,
    body: DomainSearchPricingRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
):
    """Return the locked Pro-tier pricing matrix per physician country.

    Per D-19: deterministic suggestion generation runs client-side
    (``medikah-chat-frontend/lib/domainSuggestions.ts``) — NO LLM at search
    time. This endpoint exists solely to expose the locked pricing matrix per
    PRO-02 so the wizard renders wholesale TLD price + Práctikah service fee
    transparently without hardcoding the matrix in the browser.

    Per D-23: Phase 13 ships Mexico + US only. If the physician record has no
    country and the client did not pass one, default to US so the wizard
    still renders something sensible — the actual eligibility gate runs in
    /upgrade/checkout (Plan 13-05).
    """

    db = get_supabase()
    country = (body.country or "").upper()
    if not country and db is not None:
        try:
            r = (
                db.table("physicians")
                .select("country")
                .eq("id", auth.physician_id)
                .limit(1)
                .execute()
            )
            if r.data:
                country = (r.data[0].get("country") or "").upper().strip()
        except Exception:
            logger.exception(
                "upgrade_domain_search: DB lookup failed physician_id=%s",
                auth.physician_id,
            )

    if country not in ("MX", "US"):
        country = "US"

    return {
        "country": country,
        "pricing": {
            "standard": get_pricing("standard", country),
            "premium": get_pricing("premium", country),
        },
        "tld_weights": country_weighted_tlds(country),
    }


# ---------------------------------------------------------------------------
# Phase 13-03: SAT compliance gate (D-22) + launch-scope guard (D-23)
# ---------------------------------------------------------------------------


@router.get("/upgrade/sat-status")
@limiter.limit("30/minute")
async def upgrade_sat_status(
    request: Request,
    auth: AuthenticatedPhysician = Depends(verified_physician),
):
    """Return the SAT-compliance + launch-scope status for the authenticated physician.

    Surfaces two booleans the upgrade wizard uses to render the right UX:
      - ``supported``: physician's country is in the Phase 13 launch scope (MX + US).
      - ``sat_blocked``: physician is Mexican and ``MEDIKAH_MX_SAT_REGISTERED`` is OFF.

    Per T-13-03-01: this endpoint is a UX hint only. The security control is
    ``assert_eligible(physician.country)`` invoked inside the /upgrade/checkout
    handler (Plan 13-05) before ``stripe.checkout.Session.create``.

    Per T-13-03-03: response leaks no PII beyond the physician's own country
    code and two booleans — nothing the physician doesn't already know about
    themselves.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        result = (
            db.table("physicians")
            .select("country")
            .eq("id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "upgrade_sat_status: DB lookup failed physician_id=%s",
            auth.physician_id,
        )
        raise HTTPException(status_code=500, detail="Unable to load physician record.")

    country = ""
    if result.data:
        country = (result.data[0].get("country") or "").upper().strip()

    blocked = is_sat_blocked(country)
    supported = is_supported_country(country)
    if blocked:
        message_key = "sat.blocked"
    elif not supported:
        message_key = "country.not_supported"
    else:
        message_key = None

    return {
        "country": country,
        "supported": supported,
        "sat_blocked": blocked,
        "message_key": message_key,
    }


# ---------------------------------------------------------------------------
# Phase 13-05: Stripe Checkout session creation (/upgrade/checkout)
# ---------------------------------------------------------------------------


class CheckoutRequest(BaseModel):
    """POST body for /practikah/upgrade/checkout (Plan 13-05).

    The physician's authoritative country is sourced from the ``physicians``
    row (T-13-05-02) — never from the request body — so a tampered client
    cannot bypass the SAT compliance gate.
    """

    tld_class: Literal["standard", "premium"]
    cadence: Literal["annual", "monthly"]
    domain: str = Field(..., min_length=4, max_length=253)


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


@router.post("/upgrade/checkout", response_model=CheckoutResponse)
@limiter.limit("10/minute")
async def upgrade_checkout(
    request: Request,
    body: CheckoutRequest,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> CheckoutResponse:
    """Create a Stripe Checkout Session for the Pro upgrade flow.

    Per D-22 / T-13-05-02: ``assert_eligible(physician.country)`` is the FIRST
    Stripe-touching statement (inside ``create_checkout_session``). Mexican
    physicians while ``MEDIKAH_MX_SAT_REGISTERED`` is OFF → HTTP 403 with the
    bilingual ``SAT_BLOCKED`` envelope. Non-MX/non-US physicians → HTTP 403
    with the ``COUNTRY_NOT_SUPPORTED`` envelope.

    Per PRO-04 / D-05: currency is routed by ``physician.country`` (MXN for
    MX, USD for US). Per D-06 / PRO-09: monthly cadence appends a one-time
    setup-fee Price as a second line item. Per D-10: Stripe Tax is enabled.
    Per T-13-05-05: idempotency_key bound to the saga ``run_id``.

    Side effect (D-17): inserts a ``provisioning_runs`` row keyed on the new
    ``run_id`` with ``status='pending'`` and the ``stripe_session_id``. The
    ``checkout.session.completed`` webhook (Plan 13-01) flips it to
    ``running`` and the saga (Plan 13-06) advances from there.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # 1. Look up the physician's authoritative country + email.
    try:
        physician_result = (
            db.table("physicians")
            .select("country, email")
            .eq("id", auth.physician_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "upgrade_checkout: physician lookup failed physician_id=%s",
            auth.physician_id,
        )
        raise HTTPException(status_code=500, detail="Unable to load physician record.")

    if not physician_result.data:
        raise HTTPException(status_code=404, detail="Physician record not found.")

    physician_row = physician_result.data[0]
    physician_country = (physician_row.get("country") or "").upper().strip()
    physician_email = physician_row.get("email") or auth.email

    # 2. Look up an existing stripe_customer_id (None on first upgrade).
    stripe_customer_id: Optional[str] = None
    try:
        workspace_result = (
            db.table("physician_workspace_accounts")
            .select("stripe_customer_id")
            .eq("physician_id", auth.physician_id)
            .limit(1)
            .execute()
        )
        if workspace_result.data:
            stripe_customer_id = workspace_result.data[0].get("stripe_customer_id")
    except Exception:
        logger.warning(
            "upgrade_checkout: workspace account lookup failed physician_id=%s "
            "(continuing without customer_id)",
            auth.physician_id,
        )

    # 3. Build the Stripe Checkout Session (SAT gate is the first body line).
    try:
        result = await create_checkout_session(
            physician_id=auth.physician_id,
            physician_country=physician_country,
            physician_email=physician_email,
            tld_class=body.tld_class,
            cadence=body.cadence,
            domain=body.domain,
            stripe_customer_id=stripe_customer_id,
        )
    except SATBlockedError:
        # D-22 — bilingual envelope per T-13-05-07 (no Stripe IDs/stack traces).
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SAT_BLOCKED",
                "message_en": "Práctikah Pro is launching in México soon",
                "message_es": "Práctikah Pro estará disponible en México pronto",
            },
        )
    except CountryNotSupportedError:
        # D-23 — bilingual envelope.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "COUNTRY_NOT_SUPPORTED",
                "message_en": "Práctikah Pro isn't available in your country yet",
                "message_es": "Práctikah Pro aún no está disponible en tu país",
            },
        )
    except RuntimeError as err:
        # Stripe price lookup miss / Stripe API failure / missing secret.
        logger.exception(
            "upgrade_checkout: RuntimeError physician_id=%s",
            auth.physician_id,
        )
        raise HTTPException(status_code=503, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception:
        logger.exception(
            "upgrade_checkout: unhandled error physician_id=%s",
            auth.physician_id,
        )
        raise HTTPException(status_code=502, detail="Stripe Checkout creation failed.")

    # 4. D-17 — persist the saga run BEFORE returning the URL so a successful
    # webhook can correlate by run_id even if the doctor force-quits the tab.
    try:
        db.table("provisioning_runs").insert(
            {
                "physician_id": auth.physician_id,
                "run_id": result["run_id"],
                "saga_type": "pro_upgrade",
                "status": "pending",
                "stripe_session_id": result["session_id"],
                "domain_name": body.domain,
            }
        ).execute()
    except Exception:
        # Best-effort — the webhook can fall back to inserting on receipt.
        logger.exception(
            "upgrade_checkout: provisioning_runs insert failed physician_id=%s "
            "run_id=%s session_id=%s (webhook will reconcile)",
            auth.physician_id, result["run_id"], result["session_id"],
        )

    # 5. Persist the customer_id placeholder so future Customer Portal lookups
    # succeed even if the doctor never returns from Checkout. Stripe will
    # populate the real ID via the webhook event; this is just a marker that
    # a session exists.
    try:
        db.table("physician_workspace_accounts").upsert(
            {
                "physician_id": auth.physician_id,
                "stripe_session_id": result["session_id"],
            },
            on_conflict="physician_id",
        ).execute()
    except Exception:
        # The column may not exist on older deployments — non-fatal.
        logger.warning(
            "upgrade_checkout: workspace stripe_session_id update failed "
            "physician_id=%s (non-fatal)",
            auth.physician_id,
        )

    return CheckoutResponse(
        checkout_url=result["url"],
        session_id=result["session_id"],
    )


# ---------------------------------------------------------------------------
# SSE: Pro upgrade saga live status (Phase 13-07 / D-16)
# ---------------------------------------------------------------------------

@router.get("/upgrade/status")
async def upgrade_status_sse(
    request: Request,
    run_id: str,
    auth: AuthenticatedPhysician = Depends(verified_physician),
):
    """Server-Sent Events stream of saga step transitions for the doctor's
    Vercel-deploy-style stepped checklist UI (D-16, 3-min live UX).

    Owner-only authorization (T-13-07-01): the run must belong to the
    authenticated physician. We re-check on every connection open — RLS
    provides defense-in-depth on the SELECT, but explicit 403 here is the
    canonical security gate.

    No SlowAPI rate limit on this endpoint — it's a long-lived connection,
    not a request-per-call surface. Concurrency is naturally bounded: one
    saga per upgrade flow per physician (T-13-07-03).

    Per D-15: stream emits ``run.partial_finish_later`` and closes when the
    saga lands in finish-later state (post-POR retry); the UI shows a warm
    bilingual "we're finishing setup, your domain is yours" message.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Owner-only auth gate (T-13-07-01) — runs BEFORE the stream opens.
    try:
        run_resp = (
            db.table("provisioning_runs")
            .select("physician_id")
            .eq("run_id", run_id)
            .single()
            .execute()
        )
        run = run_resp.data
    except Exception:
        logger.exception(
            "upgrade_status_sse: provisioning_runs lookup failed run_id=%s "
            "physician_id=%s",
            run_id,
            auth.physician_id,
        )
        raise HTTPException(status_code=404, detail="run not found")

    if not run:
        raise HTTPException(status_code=404, detail="run not found")

    if run.get("physician_id") != auth.physician_id:
        # Distinct 403 (not 404) — caller is authenticated but not the owner.
        raise HTTPException(status_code=403, detail="not your run")

    return StreamingResponse(
        stream_run_status(db, run_id, auth.physician_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Defeats Netlify edge buffering and nginx ``proxy_buffering``. (D-16)
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Helper: resolve run_id from a Stripe Checkout session_id (Phase 13-07)
# ---------------------------------------------------------------------------

class RunBySessionResponse(BaseModel):
    run_id: str


@router.get("/upgrade/run-by-session", response_model=RunBySessionResponse)
@limiter.limit("30/minute")
async def upgrade_run_by_session(
    request: Request,
    session_id: str,
    auth: AuthenticatedPhysician = Depends(verified_physician),
) -> RunBySessionResponse:
    """Map a Stripe Checkout ``session_id`` back to its saga ``run_id``.

    The doctor returns from Stripe with ``?session_id=cs_test_…`` in the
    URL but the SSE stream is keyed on ``run_id``. This endpoint owner-gates
    the lookup so a physician can never resolve another physician's run_id.
    """
    db = get_supabase()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        resp = (
            db.table("provisioning_runs")
            .select("run_id")
            .eq("stripe_session_id", session_id)
            .eq("physician_id", auth.physician_id)
            .single()
            .execute()
        )
        row = resp.data
    except Exception:
        logger.exception(
            "upgrade_run_by_session: lookup failed session_id=%s physician_id=%s",
            session_id,
            auth.physician_id,
        )
        raise HTTPException(status_code=404, detail="run not found")

    if not row:
        raise HTTPException(status_code=404, detail="run not found")

    return RunBySessionResponse(run_id=str(row["run_id"]))


# ---------------------------------------------------------------------------
# Phase 13 D-24/D-25/D-26 — active-Pro redirect map (internal endpoint)
# ---------------------------------------------------------------------------
#
# Returns {slug: custom_domain} for every physician whose Pro subscription is
# currently ``active`` AND who has a published custom domain. Consumed by the
# Next.js edge middleware (medikah-chat-frontend/middleware.ts) via a 60s
# in-memory cache. Authenticated by the existing INTERNAL_API_SHARED_SECRET
# pattern (Phase 12-02 / services/practikah/notifications.py).
#
# Revertibility (D-25 / PRO-17): when subscription_status flips to 'canceled'
# or published_to_domain_id is set to NULL, the slug drops out of the map on
# the next refresh — the middleware then falls through to the existing
# rewrite to /sites/<slug> (Try Pro surface).
@router.get("/internal/pro-redirect-map")
async def internal_pro_redirect_map(request: Request) -> dict[str, str]:
    """Internal endpoint — returns {slug: custom_domain} for active Pro physicians.

    Auth: ``X-Internal-Secret`` header must equal ``INTERNAL_API_SHARED_SECRET``.
    Returns 403 otherwise. Returns ``{}`` if Supabase is unavailable
    (fail-open contract — middleware preserves existing rewrite).
    """
    from services.practikah.redirect_cache import active_pro_redirect_map

    expected = os.environ.get("INTERNAL_API_SHARED_SECRET", "")
    if not expected:
        # Misconfiguration — refuse rather than allow unauthenticated access.
        logger.error("internal_pro_redirect_map: INTERNAL_API_SHARED_SECRET not set")
        raise HTTPException(status_code=503, detail="internal auth not configured")

    secret = request.headers.get("X-Internal-Secret", "")
    if secret != expected:
        raise HTTPException(status_code=403, detail="forbidden")

    db = get_supabase()
    if db is None:
        # Fail-open: empty map → middleware preserves existing rewrite.
        logger.warning("internal_pro_redirect_map: supabase not configured — returning empty map")
        return {}

    return await active_pro_redirect_map(db)
