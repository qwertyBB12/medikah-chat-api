"""Pydantic models for physician-related API endpoints."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    """Physician verification status."""

    PENDING = "pending"
    IN_REVIEW = "in_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class InquiryStatus(str, Enum):
    """Patient inquiry status."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class TimeSlot(BaseModel):
    """A single availability time slot."""

    start_time: str = Field(..., description="Start time in HH:MM format (24h)")
    end_time: str = Field(..., description="End time in HH:MM format (24h)")


class DayAvailability(BaseModel):
    """Availability for a single day of the week."""

    day: str = Field(..., description="Day of the week (monday, tuesday, ...)")
    slots: List[TimeSlot] = Field(default_factory=list)
    enabled: bool = True


class PhysicianProfile(BaseModel):
    """Response model for physician profile/dashboard data."""

    physician_id: str
    full_name: str
    email: str
    photo_url: Optional[str] = None
    specialty: Optional[str] = None
    license_country: Optional[str] = None
    license_number: Optional[str] = None
    verification_status: VerificationStatus = VerificationStatus.PENDING
    bio: Optional[str] = None
    languages: List[str] = Field(default_factory=list)
    timezone: Optional[str] = None
    created_at: Optional[datetime] = None
    inquiry_count: int = 0
    upcoming_appointments: int = 0


class PhysicianAvailability(BaseModel):
    """Request/response model for physician availability schedule."""

    physician_id: Optional[str] = None
    timezone: str = Field(default="UTC", description="IANA timezone for the schedule")
    schedule: List[DayAvailability] = Field(default_factory=list)
    updated_at: Optional[datetime] = None


class PatientInquiry(BaseModel):
    """Response model for a patient inquiry."""

    inquiry_id: str
    patient_name: str
    patient_email: Optional[str] = None
    symptoms: Optional[str] = None
    preferred_time: Optional[datetime] = None
    status: InquiryStatus = InquiryStatus.PENDING
    created_at: Optional[datetime] = None
    locale: Optional[str] = None


class InquiryAction(BaseModel):
    """Request model for accepting or declining an inquiry."""

    reason: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional reason (used when declining)",
    )


class PaginatedInquiries(BaseModel):
    """Paginated list of patient inquiries."""

    items: List[PatientInquiry] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
