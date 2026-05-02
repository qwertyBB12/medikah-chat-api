"""Domain availability service for Práctikah Pro upsell (Phase 13).

Per D-20: Cloudflare Registrar Availability API is the primary source. On any
CF error (4xx/5xx/network), we fall back to RDAP (rdap.org bootstrap) which
returns 404 for unregistered domains and 200 for registered ones.

This service is consumed by:
  - 13-04 frontend domain-search component (debounced lookup as user types).
  - The /practikah/upgrade/availability BFF route (auth-gated, 30/min).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .cloudflare_registrar import cf_registrar

logger = logging.getLogger(__name__)

RDAP_URL = "https://rdap.org/domain/{domain}"


async def check_availability(domain: str) -> dict[str, Any]:
    """Return availability for `domain` with CF primary + RDAP fallback.

    Response shape (always includes a `source` key for observability):
        {
          "available": bool,
          "tld": str,
          "wholesale_price_usd": Optional[float],
          "source": "cf" | "rdap",
        }

    Strategy per D-20:
      1. Cloudflare Registrar Availability API first (richest data — pricing).
      2. RDAP fallback on CF error or non-success envelope.

    RDAP semantics: HTTP 404 ⇒ domain not in registry ⇒ available;
    HTTP 200 ⇒ domain is registered ⇒ not available.
    """
    try:
        cf = await cf_registrar.check_availability(domain)
        if cf.success:
            return {**cf.raw_response, "source": "cf"}
        logger.info(
            "[availability] CF returned non-success for %s; falling back to RDAP",
            domain,
        )
    except Exception:
        logger.exception(
            "[availability] CF availability raised for %s; falling back to RDAP",
            domain,
        )

    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(RDAP_URL.format(domain=domain))
        available = r.status_code == 404
        return {
            "available": available,
            "tld": tld,
            "wholesale_price_usd": None,
            "source": "rdap",
        }
    except Exception:
        logger.exception("[availability] RDAP fallback failed for %s", domain)
        # Conservative default — surface "unknown" as not-available rather than
        # promising the physician a domain we couldn't actually verify.
        return {
            "available": False,
            "tld": tld,
            "wholesale_price_usd": None,
            "source": "rdap",
            "error": "availability_check_failed",
        }
