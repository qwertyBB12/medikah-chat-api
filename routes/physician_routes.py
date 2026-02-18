"""Physician dashboard API routes."""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from utils.auth import verify_physician_access

from models.physician import (
    InquiryAction,
    PaginatedInquiries,
    PatientInquiry,
    PhysicianAvailability,
    PhysicianProfile,
)
from services.physician_dashboard import (
    accept_inquiry,
    decline_inquiry,
    get_physician_availability,
    get_physician_inquiries,
    get_physician_profile,
    update_physician_availability,
)
from services.physician_notifications import (
    send_inquiry_accepted_email,
    send_inquiry_declined_email,
)
from services.notifications import NotificationService

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/physicians", tags=["physicians"])

_RESEND_API_KEY = os.getenv("RESEND_API_KEY")
_RESEND_SENDER_EMAIL = os.getenv("RESEND_SENDER_EMAIL", "Medikah <onboarding@resend.dev>")
_EMAIL_SANDBOX_MODE_RAW = os.getenv("EMAIL_SANDBOX_MODE", "false").lower()
_EMAIL_SANDBOX_MODE = _EMAIL_SANDBOX_MODE_RAW in {"1", "true", "yes", "on"}
_notification_service: Optional[NotificationService] = None
if _RESEND_API_KEY:
    _notification_service = NotificationService(
        _RESEND_API_KEY, _RESEND_SENDER_EMAIL, sandbox_mode=_EMAIL_SANDBOX_MODE,
    )


def _get_physician_name(physician_id: str) -> str:
    """Fetch the physician's display name from the database."""
    profile = get_physician_profile(physician_id)
    if profile:
        return profile.full_name
    return "Your Medikah Physician"


@router.get("/{physician_id}/dashboard", response_model=PhysicianProfile)
@limiter.limit("30/minute")
async def physician_dashboard(request: Request, physician_id: str = Depends(verify_physician_access)) -> PhysicianProfile:
    """Return physician profile, verification status, and dashboard stats."""
    try:
        profile = get_physician_profile(physician_id)
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Database not configured")
    except Exception:
        logger.exception("Error fetching dashboard for physician %s", physician_id)
        raise HTTPException(status_code=500, detail="Unable to load dashboard data.")

    if profile is None:
        raise HTTPException(status_code=404, detail="Physician not found")

    return profile


@router.get("/{physician_id}/inquiries", response_model=PaginatedInquiries)
@limiter.limit("30/minute")
async def list_inquiries(
    request: Request,
    physician_id: str = Depends(verify_physician_access),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = Query(default=None),
) -> PaginatedInquiries:
    """List incoming patient inquiries for a physician (paginated)."""
    if status and status not in ("pending", "accepted", "declined"):
        raise HTTPException(
            status_code=400,
            detail="Invalid status filter. Must be: pending, accepted, or declined.",
        )

    try:
        result = get_physician_inquiries(
            physician_id,
            page=page,
            page_size=page_size,
            status_filter=status,
        )
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Database not configured")
    except Exception:
        logger.exception("Error fetching inquiries for physician %s", physician_id)
        raise HTTPException(status_code=500, detail="Unable to load inquiries.")

    return result


@router.post("/{physician_id}/inquiries/{inquiry_id}/accept", response_model=PatientInquiry)
@limiter.limit("10/minute")
async def accept_patient_inquiry(
    request: Request,
    physician_id: str = Depends(verify_physician_access),
    inquiry_id: str = Path(...),
) -> PatientInquiry:
    """Accept a patient inquiry and trigger patient notification."""
    try:
        inquiry = accept_inquiry(physician_id, inquiry_id)
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Database not configured")
    except Exception:
        logger.exception(
            "Error accepting inquiry %s for physician %s", inquiry_id, physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to process inquiry.")

    if inquiry is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")

    if _notification_service and inquiry.patient_email:
        try:
            physician_name = _get_physician_name(physician_id)
            await send_inquiry_accepted_email(
                patient_email=inquiry.patient_email,
                patient_name=inquiry.patient_name,
                physician_name=physician_name,
                notification_service=_notification_service,
                locale=inquiry.locale or "en",
            )
        except Exception:
            logger.exception("Failed to send accepted email for inquiry %s", inquiry_id)

    return inquiry


@router.post("/{physician_id}/inquiries/{inquiry_id}/decline", response_model=PatientInquiry)
@limiter.limit("10/minute")
async def decline_patient_inquiry(
    request: Request,
    body: InquiryAction,
    physician_id: str = Depends(verify_physician_access),
    inquiry_id: str = Path(...),
) -> PatientInquiry:
    """Decline a patient inquiry with an optional reason."""
    try:
        inquiry = decline_inquiry(physician_id, inquiry_id, reason=body.reason)
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Database not configured")
    except Exception:
        logger.exception(
            "Error declining inquiry %s for physician %s", inquiry_id, physician_id
        )
        raise HTTPException(status_code=500, detail="Unable to process inquiry.")

    if inquiry is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")

    if _notification_service and inquiry.patient_email:
        try:
            physician_name = _get_physician_name(physician_id)
            await send_inquiry_declined_email(
                patient_email=inquiry.patient_email,
                patient_name=inquiry.patient_name,
                physician_name=physician_name,
                notification_service=_notification_service,
                reason=body.reason,
                locale=inquiry.locale or "en",
            )
        except Exception:
            logger.exception("Failed to send declined email for inquiry %s", inquiry_id)

    return inquiry


@router.get("/{physician_id}/availability", response_model=PhysicianAvailability)
@limiter.limit("30/minute")
async def get_availability(
    request: Request,
    physician_id: str = Depends(verify_physician_access),
) -> PhysicianAvailability:
    """Get the physician's current availability schedule."""
    try:
        availability = get_physician_availability(physician_id)
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Database not configured")
    except Exception:
        logger.exception("Error fetching availability for physician %s", physician_id)
        raise HTTPException(status_code=500, detail="Unable to load availability.")

    if availability is None:
        raise HTTPException(status_code=404, detail="Physician not found")

    return availability


@router.put("/{physician_id}/availability", response_model=PhysicianAvailability)
@limiter.limit("10/minute")
async def set_availability(
    request: Request,
    body: PhysicianAvailability,
    physician_id: str = Depends(verify_physician_access),
) -> PhysicianAvailability:
    """Update the physician's availability schedule."""
    try:
        result = update_physician_availability(physician_id, body)
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Database not configured")
    except Exception:
        logger.exception("Error updating availability for physician %s", physician_id)
        raise HTTPException(status_code=500, detail="Unable to update availability.")

    return result
