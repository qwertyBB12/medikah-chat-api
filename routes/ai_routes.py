"""AI clinical decision-support route (legacy dashboard surface).

NAMING / LEGAL (Hector, 2026-06-29): nothing on this surface may be named or
inferable as an "(official) diagnosis." The route, models, and fields speak of
clinical decision SUPPORT and ranked CONSIDERATIONS; the only "diagnosis" token
anywhere is the disclaimer's explicit denial.

CONSOLIDATION (2026-07-02, sprint cleanup): this endpoint previously carried its
own copy of the system prompt + prose parser. It now ADOPTS the single-source
generator in services/cue/clinical_support.py — the same engine behind the Cue
conversational card — per that module's stated design ("used by BOTH"). The old
/ai/diagnosis path is retired along with the duplicated logic; the surface is
flag-hidden in the dashboard (CLINICAL_SUPPORT_IN_DASH=false) and unreachable in
prod, so the rename carries no live-traffic risk.

Runs on the Opus reasoning tier via the provider-neutral wrapper (CUE-09).
No provider-specific types appear here (D1 rule — tests/cue/test_no_provider_leak.py).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from services.cue.clinical_support import (
    ClinicalSupportUnavailable,
    generate_clinical_support,
)
from utils.auth import AuthenticatedPhysician, authenticated_physician

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/ai", tags=["ai"])


class ClinicalSupportRequest(BaseModel):
    """Request model for AI clinical decision support (de-identified input only)."""

    symptoms: str = Field(
        ..., min_length=5, max_length=3000, description="De-identified clinical presentation"
    )
    age_range: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Age range (e.g., '30-40', 'pediatric', 'elderly')",
    )
    sex: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Biological sex if clinically relevant",
    )


class ConsiderationItem(BaseModel):
    """A single ranked clinical consideration."""

    condition: str
    rationale: str
    confidence: str = Field(description="HIGH, MODERATE, or LOW")
    distinguishing_factors: str


class ClinicalSupportResponse(BaseModel):
    """Response model for AI clinical decision support."""

    considerations: List[ConsiderationItem]
    red_flags: List[str]
    disclaimer: str
    raw_text: str = Field(description="Full AI response text")


@router.post("/clinical-support", response_model=ClinicalSupportResponse)
@limiter.limit("10/minute")
async def ai_clinical_support(
    request: Request,
    body: ClinicalSupportRequest,
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> ClinicalSupportResponse:
    """Generate ranked clinical considerations for decision support.

    Stateless, stores nothing, never logs the presentation. Restricted to
    authenticated physicians (any verification status) — same gate as the rest
    of the dashboard surface.
    """
    try:
        result = await generate_clinical_support(
            presentation=body.symptoms,
            age_range=body.age_range,
            sex=body.sex,
        )
    except ClinicalSupportUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="AI service is not configured or returned an empty response.",
        ) from exc
    except Exception as exc:
        logger.exception("Clinical support generation failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Unable to generate clinical support at this time. Please try again.",
        ) from exc

    return ClinicalSupportResponse(
        considerations=[ConsiderationItem(**c) for c in result["considerations"]],
        red_flags=result["red_flags"],
        disclaimer=result["disclaimer"],
        raw_text=result["summary"],
    )
