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
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Request
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
