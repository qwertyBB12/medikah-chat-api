"""NextAuth HS256 JWT verification + verification-gate dependencies for FastAPI routes."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Header, HTTPException, Request

from db.client import get_supabase

logger = logging.getLogger(__name__)

NEXTAUTH_SECRET = os.getenv("NEXTAUTH_SECRET")
JWT_ALGORITHM = "HS256"


@dataclass(frozen=True, slots=True)
class AuthenticatedPhysician:
    """Identity + verification status of a physician authenticated via NextAuth."""

    physician_id: str
    auth_user_id: str
    email: str
    role: str
    verification_status: str


async def _decode_and_lookup(
    authorization: Optional[str],
    path_physician_id: Optional[str],
) -> dict:
    """Decode the NextAuth HS256 JWT and look up the matching physician row.

    Returns a dict with the physician_row, userId, email, and role on success.
    Raises HTTPException on any failure mode (401 missing/invalid token,
    403 role/ownership mismatch, 503 misconfiguration).
    """
    # 1. Bearer header present and well-formed
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")

    # 2. Strip prefix; reject empty token
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    # 3. NEXTAUTH_SECRET must be configured
    if NEXTAUTH_SECRET is None:
        logger.error("NEXTAUTH_SECRET is not configured; cannot verify JWT")
        raise HTTPException(status_code=503, detail="Auth not configured")

    # 4. Verify HS256 signature; reject expired or invalid tokens.
    #    `algorithms=["HS256"]` MUST be a list — passing a string would let an
    #    attacker pass `alg: none` and bypass signature verification entirely.
    try:
        claims = jwt.decode(token, NEXTAUTH_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        logger.exception("NextAuth JWT expired")
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        logger.exception("NextAuth JWT invalid")
        raise HTTPException(status_code=401, detail="Invalid token")

    # 5. Required claim shape
    user_id = claims.get("userId")
    role = claims.get("role")
    email = claims.get("email")
    if not user_id or not role or not email:
        raise HTTPException(status_code=401, detail="Token claims missing required fields")

    # 6. Role gate — patient JWTs may not access physician resources
    if role != "physician":
        raise HTTPException(status_code=403, detail="Physician role required")

    # 7. Supabase client must be available for the row lookup
    supabase = get_supabase()
    if supabase is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # 8. Find the physician row owned by this auth user
    try:
        result = (
            supabase.table("physicians")
            .select("id, email, verification_status")
            .eq("auth_user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to look up physician row for auth_user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Unable to verify physician access")

    if not result.data:
        raise HTTPException(
            status_code=403,
            detail="No physician profile linked to this account",
        )

    physician_row = result.data[0]

    # 9. Path-parameter ownership check (when present)
    if path_physician_id is not None and physician_row["id"] != path_physician_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this physician's resources",
        )

    # 10. Hand back the bundle; caller decides whether to enforce verification
    return {
        "physician_row": physician_row,
        "userId": user_id,
        "email": email,
        "role": role,
    }


async def authenticated_physician(
    request: Request,
    physician_id: Optional[str] = None,
    authorization: Optional[str] = Header(default=None),
) -> AuthenticatedPhysician:
    """D-03 retrofit dependency: real auth without verification-gate.

    Used by existing /physicians/{id}/* routes that should remain accessible
    to physicians whose verification_status is still 'pending' or 'in_review'
    (e.g. dashboard, status checks, onboarding).

    `physician_id` is resolved by FastAPI from the route's path parameter
    when the parent route declares `/{physician_id}/...`. Declared as
    `Optional[str] = None` rather than `Path(default=None)` because FastAPI
    rejects path parameters with default values at module-import time
    (`assert default is ..., "Path parameters cannot have a default value"`).
    """
    bundle = await _decode_and_lookup(authorization, physician_id)
    row = bundle["physician_row"]
    return AuthenticatedPhysician(
        physician_id=row["id"],
        auth_user_id=bundle["userId"],
        email=row["email"],
        role=bundle["role"],
        verification_status=row["verification_status"],
    )


async def verified_physician(
    request: Request,
    physician_id: Optional[str] = None,
    authorization: Optional[str] = Header(default=None),
) -> AuthenticatedPhysician:
    """WSPC-06 verification-gate. Used by all /practikah/* routes.

    Wraps `authenticated_physician` and additionally enforces that
    `verification_status == 'verified'`. On gate failure, raises 403 with
    the bilingual structured envelope locked in CONTEXT.md D-07.
    """
    auth = await authenticated_physician(request, physician_id, authorization)
    if auth.verification_status != "verified":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "WSPC_NOT_VERIFIED",
                "verification_status": auth.verification_status,
                "message_en": "Your physician credentials have not been verified yet. Please complete verification before accessing your workspace.",
                "message_es": "Tus credenciales médicas aún no se han verificado. Completa la verificación antes de acceder a tu espacio de trabajo.",
            },
        )
    return auth
