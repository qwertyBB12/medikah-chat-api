"""Trigger Resend transactional emails by calling back into the Next.js BFF (Phase 12-02).

Why HTTP-back to Next.js:
  The Resend HTML template + bilingual content map lives in
  medikah-chat-frontend/lib/practikahEmail.ts (mirrors lib/physicianEmail.ts).
  FastAPI doesn't duplicate the template — it signals Next.js via an internal-only
  HTTP endpoint protected by INTERNAL_API_SHARED_SECRET.

Trust boundary:
  FastAPI → Next.js /api/internal/practikah-email-trigger
  Authenticated by 'X-Internal-Secret' header matching INTERNAL_API_SHARED_SECRET env var.
  Per T-12-02-03: the Next.js endpoint uses crypto.timingSafeEqual for constant-time compare.

Best-effort semantics (T-12-02-10):
  send_practikah_live_email() NEVER raises. Email failure is logged as WARNING and the
  caller (wizard_complete endpoint) treats it as non-fatal. The doctor's mailbox is live
  regardless of whether the welcome email was delivered.

Environment variables:
  INTERNAL_API_SHARED_SECRET  — shared secret for FastAPI → Next.js internal API calls
                                 (must match the value set in Netlify + Render envs)
  NEXT_INTERNAL_URL           — internal base URL for Next.js (default: NEXT_PUBLIC_BASE_URL
                                 or http://localhost:3000 for local dev)
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def send_practikah_live_email(
    to: str,
    lang: str,
    mailbox_address: str,
    slug: str,
    first_name: str,
    last_name: str,
) -> None:
    """Fire a best-effort Resend 'Práctikah is live' transactional email via Next.js BFF.

    Calls POST /api/internal/practikah-email-trigger on the Next.js BFF with the
    X-Internal-Secret header. On any failure (missing secret, network error, non-2xx
    from Next.js), logs a WARNING and returns — NEVER raises.

    Args:
        to:              Recipient email address (physician's Medikah-login email).
        lang:            Language code ('en' or 'es'). Falls back to 'en' if unrecognized.
        mailbox_address: The newly provisioned Práctikah mailbox address.
        slug:            The physician's URL slug (e.g. 'dr-lopez').
        first_name:      Physician's first name for personalized greeting.
        last_name:       Physician's last name for personalized greeting.
    """
    # Resolve Next.js internal base URL
    next_url = os.environ.get(
        "NEXT_INTERNAL_URL",
        os.environ.get("NEXT_PUBLIC_BASE_URL", "http://localhost:3000"),
    )
    secret = os.environ.get("INTERNAL_API_SHARED_SECRET")

    if not secret:
        logger.warning(
            "[notifications] INTERNAL_API_SHARED_SECRET not set — "
            "skipping Práctikah-live email for recipient=%s. "
            "Set this env var in both Render and Netlify dashboards (identical value).",
            to,
        )
        return

    # Normalize lang
    normalized_lang = lang if lang in ("en", "es") else "en"

    payload = {
        "kind": "practikah_live",
        "to": to,
        "lang": normalized_lang,
        "mailbox_address": mailbox_address,
        "slug": slug,
        "first_name": first_name,
        "last_name": last_name,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{next_url.rstrip('/')}/api/internal/practikah-email-trigger",
                headers={
                    "X-Internal-Secret": secret,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 300:
                logger.warning(
                    "[notifications] practikah-live email trigger returned non-2xx "
                    "status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                logger.info(
                    "[notifications] practikah-live email queued for recipient=%s lang=%s",
                    to, normalized_lang,
                )
    except Exception as exc:
        # Best-effort — never raises (T-12-02-10)
        logger.warning(
            "[notifications] practikah-live email trigger failed: %s", exc
        )
