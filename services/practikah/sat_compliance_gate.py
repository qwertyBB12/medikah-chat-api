"""SAT compliance gate for the Práctikah Pro upgrade flow (Phase 13-03).

Implements D-22 (Mexico SAT digital-services-provider gate) and D-23 (launch
country scope = MX + US only). Two layers of protection:

  1. SERVER-SIDE: ``assert_eligible(country)`` is the security control. It MUST
     be called inside any FastAPI handler that creates a Stripe Checkout session
     (Plan 13-05). Even if the client-side UX gate is bypassed, this raises
     ``SATBlockedError`` for Mexican physicians while the env flag is OFF.

  2. UX: ``is_sat_blocked(country)`` is the read-only flag exposed via
     ``GET /practikah/upgrade/sat-status``. The frontend renders SATBlockedNotice
     when this returns True.

Per WSPC-09 (zero-human-interaction NFR): when ``MEDIKAH_MX_SAT_REGISTERED`` is
flipped to ``true`` (operator action in Render dashboard), gating disappears
automatically — no code deploy required.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# D-23 launch scope: only Mexico and the United States are supported in
# Phase 13. AR/BR/CL/CO/PE are deferred to a future milestone — Stripe
# Checkout creation refuses non-MX/non-US country codes outright.
LAUNCH_COUNTRIES: frozenset[str] = frozenset({"MX", "US"})

# D-22: Mexican physicians require SAT compliance before they can be billed
# (IVA collection on Mexican consumers). When the env flag is OFF, checkout
# is blocked.
SAT_GATED_COUNTRIES: frozenset[str] = frozenset({"MX"})


def _flag() -> bool:
    """Read MEDIKAH_MX_SAT_REGISTERED at call time (env-driven, not cached)."""
    return os.getenv("MEDIKAH_MX_SAT_REGISTERED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_sat_blocked(physician_country: str) -> bool:
    """Return True iff the physician's country is SAT-gated and the flag is OFF.

    D-22: Mexican physicians blocked from Stripe Checkout until SAT registration
    completes. Non-MX countries are never blocked by this gate (they may be
    blocked by ``is_supported_country`` instead — D-23).
    """
    country = (physician_country or "").upper().strip()
    if country not in SAT_GATED_COUNTRIES:
        return False
    return not _flag()


def is_supported_country(physician_country: str) -> bool:
    """Return True iff the physician's country is in the Phase 13 launch scope.

    D-23: Phase 13 launches MX + US only. Any other country is unsupported.
    """
    return (physician_country or "").upper().strip() in LAUNCH_COUNTRIES


class SATBlockedError(Exception):
    """Mexican physician attempted checkout while the SAT flag is OFF (D-22)."""


class CountryNotSupportedError(Exception):
    """Non-MX/non-US physician attempted checkout (D-23)."""


def assert_eligible(physician_country: str) -> None:
    """Single guard for ``/upgrade/checkout`` (Plan 13-05).

    Raises:
        CountryNotSupportedError: if the country is outside the Phase 13 launch
            scope (D-23). Caller should map to HTTP 403.
        SATBlockedError: if the country is MX and the SAT flag is OFF (D-22).
            Caller should map to HTTP 403.
    """
    if not is_supported_country(physician_country):
        raise CountryNotSupportedError(physician_country)
    if is_sat_blocked(physician_country):
        logger.info(
            "sat_compliance_gate: blocked checkout for country=%s (flag OFF)",
            physician_country,
        )
        raise SATBlockedError(physician_country)
