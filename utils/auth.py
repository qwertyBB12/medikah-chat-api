"""Authentication dependency for physician routes."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Header, HTTPException, Path

from db.client import get_supabase

logger = logging.getLogger(__name__)


async def verify_physician_access(
    physician_id: str = Path(...),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Verify the caller owns the physician_id via Supabase JWT.

    Extracts the Bearer token from the Authorization header, validates it
    against Supabase Auth, then checks the physicians table to confirm
    the authenticated user owns the requested physician_id.

    Returns the physician_id on success; raises 401/403 on failure.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    supabase = get_supabase()
    if supabase is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Validate the token via Supabase Auth
    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        logger.exception("Failed to validate Supabase token")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response or not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    auth_user_id = user_response.user.id

    # Check that this auth user owns the requested physician_id
    try:
        result = (
            supabase.table("physicians")
            .select("id")
            .eq("auth_user_id", auth_user_id)
            .eq("id", physician_id)
            .execute()
        )
    except Exception:
        logger.exception("Failed to verify physician ownership for %s", physician_id)
        raise HTTPException(status_code=500, detail="Unable to verify physician access")

    if not result.data:
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this physician's resources",
        )

    return physician_id
