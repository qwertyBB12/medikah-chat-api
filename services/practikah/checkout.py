"""Stripe Checkout session builder for the Práctikah Pro upgrade flow (Phase 13-05).

Implements D-05/D-06/D-08/D-10 — hosted Stripe Checkout, currency routed by
``physician.country`` (PRO-04), monthly cadence attaches a one-time setup-fee
line item (PRO-09 / D-06), Stripe Tax enabled (D-10), idempotency keys bound
to the saga ``run_id``.

Per D-22/D-23 the SAT compliance gate (``sat_compliance_gate.assert_eligible``)
is the FIRST statement of ``create_checkout_session`` — even if a hostile
client bypasses the wizard's UX gate, Stripe Checkout creation refuses with
``SATBlockedError`` for Mexican physicians while the env flag is OFF.

Per T-13-05-04: ``STRIPE_SECRET_KEY`` is server-only — never imported into
the Next.js client bundle.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Literal, Optional

import stripe

from .sat_compliance_gate import assert_eligible

logger = logging.getLogger(__name__)


# Stripe API key is read at call time (not module import) so test environments
# without the env var can still import the module.
def _ensure_stripe_configured() -> None:
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not configured. The /upgrade/checkout endpoint "
            "is unavailable until the operator sets it in the runtime env."
        )
    stripe.api_key = secret


# Frontend URL used for Stripe Checkout success/cancel redirects.
def _frontend_url() -> str:
    return (
        os.environ.get("PRACTIKAH_FRONTEND_URL")
        or os.environ.get("NEXT_PUBLIC_BASE_URL")
        or os.environ.get("FRONTEND_URL")
        or "https://practikah.medikah.health"
    )


TldClass = Literal["standard", "premium"]
Cadence = Literal["annual", "monthly"]


# Stripe Price lookup_keys seeded by Plan 13-01's ``seed_stripe_products.py``.
# 8 recurring Prices (4 products × 2 currencies) per D-05 / PRO-04.
LOOKUP_RECURRING: dict[tuple[str, str, str], str] = {
    ("standard", "annual", "MX"): "practikah_pro_standard_annual_mxn",
    ("standard", "annual", "US"): "practikah_pro_standard_annual_usd",
    ("standard", "monthly", "MX"): "practikah_pro_standard_monthly_mxn",
    ("standard", "monthly", "US"): "practikah_pro_standard_monthly_usd",
    ("premium", "annual", "MX"): "practikah_pro_premium_annual_mxn",
    ("premium", "annual", "US"): "practikah_pro_premium_annual_usd",
    ("premium", "monthly", "MX"): "practikah_pro_premium_monthly_mxn",
    ("premium", "monthly", "US"): "practikah_pro_premium_monthly_usd",
}

# 4 one-time setup-fee Prices per D-06 / PRO-09 — only attached on monthly cadence.
LOOKUP_SETUP: dict[tuple[str, str], str] = {
    ("standard", "MX"): "practikah_pro_standard_setup_mxn",
    ("standard", "US"): "practikah_pro_standard_setup_usd",
    ("premium", "MX"): "practikah_pro_premium_setup_mxn",
    ("premium", "US"): "practikah_pro_premium_setup_usd",
}


def _resolve_price(lookup_key: str) -> str:
    """Resolve a Stripe ``Price`` ID from its ``lookup_key``.

    Plan 13-01's seed script creates each Price with a stable ``lookup_key``
    so this layer never hardcodes Stripe IDs (which differ between
    test/live mode + reseeds).
    """
    prices = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
    if not prices.data:
        raise RuntimeError(
            f"Stripe price not found: {lookup_key!r}. "
            "Run scripts/seed_stripe_products.py to create the locked Pro pricing matrix."
        )
    return prices.data[0].id


async def create_checkout_session(
    physician_id: str,
    physician_country: str,
    physician_email: str,
    tld_class: TldClass,
    cadence: Cadence,
    domain: str,
    stripe_customer_id: Optional[str],
) -> dict[str, Any]:
    """Build a Stripe Checkout Session for a Pro upgrade.

    Returns ``{"session_id": ..., "url": ..., "run_id": ...}``. The ``run_id``
    is what the FastAPI handler persists to ``provisioning_runs`` so the
    later ``checkout.session.completed`` webhook can find the matching saga.

    Raises:
        SATBlockedError: D-22 — MX physician while the env flag is OFF.
        CountryNotSupportedError: D-23 — physician outside MX/US.
        RuntimeError: Stripe price lookup failed (seed script not run).
        ValueError: invalid input.
    """
    # --- D-22 / D-23 — SECURITY CONTROL --------------------------------------
    # MUST be the first statement of the function body. Even if the wizard's
    # client-side gate is bypassed, this guard refuses to create the session.
    assert_eligible(physician_country)

    country = (physician_country or "").upper().strip()
    if country not in {"MX", "US"}:
        # assert_eligible should have already rejected this, but defense-in-depth.
        raise ValueError(f"Unsupported country: {country!r}")

    if tld_class not in {"standard", "premium"}:
        raise ValueError(f"Invalid tld_class: {tld_class!r}")
    if cadence not in {"annual", "monthly"}:
        raise ValueError(f"Invalid cadence: {cadence!r}")

    _ensure_stripe_configured()

    run_id = str(uuid.uuid4())
    front = _frontend_url()

    # --- Line items ----------------------------------------------------------
    line_items: list[dict[str, Any]] = [
        {
            "price": _resolve_price(LOOKUP_RECURRING[(tld_class, cadence, country)]),
            "quantity": 1,
        }
    ]
    # D-06 / PRO-09 — monthly cadence attaches the one-time setup-fee line item
    # (recoups the annually-prepaid wholesale domain cost).
    if cadence == "monthly":
        line_items.append(
            {
                "price": _resolve_price(LOOKUP_SETUP[(tld_class, country)]),
                "quantity": 1,
            }
        )

    # --- Session args --------------------------------------------------------
    session_args: dict[str, Any] = {
        "mode": "subscription",  # D-08
        "line_items": line_items,
        # D-10 — Stripe Tax handles 16% IVA for Mexican consumers automatically
        # once the Mexican tax registration is connected (D-22 prerequisite).
        "automatic_tax": {"enabled": True},
        "tax_id_collection": {"enabled": True},
        "billing_address_collection": "required",
        "client_reference_id": physician_id,
        # T-13-05-03 — metadata carried into the Subscription so disputed
        # charges trace back to the saga run.
        "metadata": {
            "physician_id": physician_id,
            "run_id": run_id,
            "tld_class": tld_class,
            "cadence": cadence,
            "domain": domain,
            "country": country,
        },
        "subscription_data": {
            "metadata": {
                "physician_id": physician_id,
                "run_id": run_id,
                "tld_class": tld_class,
                "cadence": cadence,
                "domain": domain,
            },
        },
        "success_url": (
            f"{front}/physicians/dashboard/workspace/upgrade"
            "?session_id={CHECKOUT_SESSION_ID}"
        ),
        "cancel_url": f"{front}/physicians/dashboard/workspace/upgrade?cancelled=1",
        "locale": "auto",
    }

    if stripe_customer_id:
        session_args["customer"] = stripe_customer_id
    else:
        session_args["customer_email"] = physician_email
        session_args["customer_creation"] = "always"

    # T-13-05-05 — idempotency_key bound to the saga run_id so retries (e.g.
    # network blip on the BFF) return the same Checkout Session instead of
    # creating duplicates the doctor would see flicker.
    session = stripe.checkout.Session.create(
        **session_args,
        idempotency_key=f"checkout_{run_id}",
    )

    logger.info(
        "create_checkout_session: physician_id=%s country=%s tld_class=%s "
        "cadence=%s domain=%s run_id=%s session_id=%s",
        physician_id, country, tld_class, cadence, domain, run_id, session.id,
    )

    return {
        "session_id": session.id,
        "url": session.url,
        "run_id": run_id,
    }
