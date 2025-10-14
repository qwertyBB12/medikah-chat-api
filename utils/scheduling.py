"""Scheduling helper utilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus


def generate_doxy_link(base_url: str, appointment_id: str) -> str:
    """Return a unique Doxy.me room link for an appointment."""
    if not base_url:
        raise ValueError("Doxy.me base URL must be configured")
    base = base_url.rstrip("/")
    return f"{base}/{appointment_id}"


def build_google_calendar_link(
    *,
    title: str,
    description: str,
    start: datetime,
    duration_minutes: int = 30,
    location: str | None = None,
) -> str:
    """Generate a Google Calendar link embedding appointment metadata."""
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    start_utc = _to_google_timestamp(start)
    end_utc = _to_google_timestamp(start + timedelta(minutes=duration_minutes))

    params = {
        "action": "TEMPLATE",
        "text": title,
        "details": description,
        "dates": f"{start_utc}/{end_utc}",
    }
    if location:
        params["location"] = location

    query = "&".join(f"{key}={quote_plus(value)}" for key, value in params.items())
    return f"https://calendar.google.com/calendar/render?{query}"


def _to_google_timestamp(value: datetime) -> str:
    """Convert a datetime to Google calendar timestamp format."""
    value_utc = value.astimezone(timezone.utc)
    return value_utc.strftime("%Y%m%dT%H%M%SZ")
