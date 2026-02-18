"""Business logic for physician dashboard operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from db.client import get_supabase
from models.physician import (
    DayAvailability,
    InquiryStatus,
    PaginatedInquiries,
    PatientInquiry,
    PhysicianAvailability,
    PhysicianProfile,
    VerificationStatus,
)

logger = logging.getLogger(__name__)


def _get_db():
    """Get the Supabase client, raising if unavailable."""
    client = get_supabase()
    if client is None:
        raise RuntimeError("Database not configured")
    return client


def get_physician_profile(physician_id: str) -> Optional[PhysicianProfile]:
    """Fetch physician profile and compute dashboard stats."""
    db = _get_db()

    result = (
        db.table("physicians")
        .select("*")
        .eq("id", physician_id)
        .single()
        .execute()
    )
    if not result.data:
        return None

    row = result.data

    # Count pending inquiries
    inquiry_count = 0
    try:
        inquiry_result = (
            db.table("patient_inquiries")
            .select("id", count="exact")
            .eq("physician_id", physician_id)
            .eq("status", "pending")
            .execute()
        )
        inquiry_count = inquiry_result.count or 0
    except Exception:
        logger.warning("Could not fetch inquiry count for physician %s", physician_id)

    # Count upcoming appointments
    upcoming_appointments = 0
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        appt_result = (
            db.table("appointments")
            .select("appointment_id", count="exact")
            .eq("physician_id", physician_id)
            .gte("appointment_time", now_iso)
            .execute()
        )
        upcoming_appointments = appt_result.count or 0
    except Exception:
        logger.warning(
            "Could not fetch appointment count for physician %s", physician_id
        )

    languages = row.get("languages") or []
    if isinstance(languages, str):
        try:
            languages = json.loads(languages)
        except (json.JSONDecodeError, TypeError):
            languages = [languages]

    created_at = None
    if row.get("created_at"):
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except (ValueError, TypeError):
            pass

    return PhysicianProfile(
        physician_id=row.get("id", physician_id),
        full_name=row.get("full_name", ""),
        email=row.get("email", ""),
        photo_url=row.get("photo_url"),
        specialty=row.get("specialty"),
        license_country=row.get("license_country"),
        license_number=row.get("license_number"),
        verification_status=VerificationStatus(
            row.get("verification_status", "pending")
        ),
        bio=row.get("bio"),
        languages=languages,
        timezone=row.get("timezone"),
        created_at=created_at,
        inquiry_count=inquiry_count,
        upcoming_appointments=upcoming_appointments,
    )


def get_physician_inquiries(
    physician_id: str,
    page: int = 1,
    page_size: int = 20,
    status_filter: Optional[str] = None,
) -> PaginatedInquiries:
    """Fetch paginated patient inquiries for a physician."""
    db = _get_db()

    query = (
        db.table("patient_inquiries")
        .select("*", count="exact")
        .eq("physician_id", physician_id)
        .order("created_at", desc=True)
    )

    if status_filter:
        query = query.eq("status", status_filter)

    offset = (page - 1) * page_size
    query = query.range(offset, offset + page_size - 1)

    result = query.execute()

    items = []
    for row in result.data or []:
        preferred_time = None
        if row.get("preferred_time"):
            try:
                preferred_time = datetime.fromisoformat(row["preferred_time"])
            except (ValueError, TypeError):
                pass

        created_at = None
        if row.get("created_at"):
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except (ValueError, TypeError):
                pass

        items.append(
            PatientInquiry(
                inquiry_id=row.get("id", ""),
                patient_name=row.get("patient_name", ""),
                patient_email=row.get("patient_email"),
                symptoms=row.get("symptoms"),
                preferred_time=preferred_time,
                status=InquiryStatus(row.get("status", "pending")),
                created_at=created_at,
                locale=row.get("locale"),
            )
        )

    return PaginatedInquiries(
        items=items,
        total=result.count or 0,
        page=page,
        page_size=page_size,
    )


def accept_inquiry(physician_id: str, inquiry_id: str) -> Optional[PatientInquiry]:
    """Accept a patient inquiry and return the updated record."""
    db = _get_db()

    # Verify the inquiry belongs to this physician
    result = (
        db.table("patient_inquiries")
        .select("*")
        .eq("id", inquiry_id)
        .eq("physician_id", physician_id)
        .single()
        .execute()
    )
    if not result.data:
        return None

    # Update status
    db.table("patient_inquiries").update(
        {
            "status": "accepted",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", inquiry_id).execute()

    row = result.data
    return PatientInquiry(
        inquiry_id=row.get("id", ""),
        patient_name=row.get("patient_name", ""),
        patient_email=row.get("patient_email"),
        symptoms=row.get("symptoms"),
        status=InquiryStatus.ACCEPTED,
        created_at=(
            datetime.fromisoformat(row["created_at"])
            if row.get("created_at")
            else None
        ),
        locale=row.get("locale"),
    )


def decline_inquiry(
    physician_id: str, inquiry_id: str, reason: Optional[str] = None
) -> Optional[PatientInquiry]:
    """Decline a patient inquiry with an optional reason."""
    db = _get_db()

    # Verify the inquiry belongs to this physician
    result = (
        db.table("patient_inquiries")
        .select("*")
        .eq("id", inquiry_id)
        .eq("physician_id", physician_id)
        .single()
        .execute()
    )
    if not result.data:
        return None

    update_data = {
        "status": "declined",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if reason:
        update_data["decline_reason"] = reason

    db.table("patient_inquiries").update(update_data).eq("id", inquiry_id).execute()

    row = result.data
    return PatientInquiry(
        inquiry_id=row.get("id", ""),
        patient_name=row.get("patient_name", ""),
        patient_email=row.get("patient_email"),
        symptoms=row.get("symptoms"),
        status=InquiryStatus.DECLINED,
        created_at=(
            datetime.fromisoformat(row["created_at"])
            if row.get("created_at")
            else None
        ),
        locale=row.get("locale"),
    )


def get_physician_availability(physician_id: str) -> Optional[PhysicianAvailability]:
    """Fetch the physician's availability schedule."""
    db = _get_db()

    result = (
        db.table("physician_availability")
        .select("*")
        .eq("physician_id", physician_id)
        .single()
        .execute()
    )
    if not result.data:
        return PhysicianAvailability(
            physician_id=physician_id,
            timezone="UTC",
            schedule=[],
        )

    row = result.data
    schedule_data = row.get("schedule") or []
    if isinstance(schedule_data, str):
        try:
            schedule_data = json.loads(schedule_data)
        except (json.JSONDecodeError, TypeError):
            schedule_data = []

    schedule = []
    for day_data in schedule_data:
        schedule.append(
            DayAvailability(
                day=day_data.get("day", ""),
                slots=day_data.get("slots", []),
                enabled=day_data.get("enabled", True),
            )
        )

    updated_at = None
    if row.get("updated_at"):
        try:
            updated_at = datetime.fromisoformat(row["updated_at"])
        except (ValueError, TypeError):
            pass

    return PhysicianAvailability(
        physician_id=physician_id,
        timezone=row.get("timezone", "UTC"),
        schedule=schedule,
        updated_at=updated_at,
    )


def update_physician_availability(
    physician_id: str, availability: PhysicianAvailability
) -> PhysicianAvailability:
    """Create or update the physician's availability schedule."""
    db = _get_db()

    now = datetime.now(timezone.utc)
    schedule_json = [day.model_dump() for day in availability.schedule]

    db.table("physician_availability").upsert(
        {
            "physician_id": physician_id,
            "timezone": availability.timezone,
            "schedule": schedule_json,
            "updated_at": now.isoformat(),
        }
    ).execute()

    availability.physician_id = physician_id
    availability.updated_at = now
    return availability
