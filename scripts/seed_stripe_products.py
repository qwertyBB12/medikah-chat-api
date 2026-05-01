"""Stripe Products + Prices seeder for Práctikah Pro (Phase 13-01 / D-05 / D-06).

Idempotent — safe to re-run. Uses Stripe ``lookup_key`` for de-duplication:
on each Price we set a stable lookup_key like ``practikah_pro_standard_annual_mxn``;
re-running the script does ``stripe.Price.list(lookup_keys=[...])`` first and
skips creation if the price already exists.

Materializes:
  - 4 Products: practikah_pro_{tld_class}_{cadence}
      with metadata.tld_class ∈ {standard, premium}
      and  metadata.cadence   ∈ {annual, monthly}
  - 8 recurring Prices (one MXN + one USD per Product) with EXACT D-01 amounts
  - 4 one-time setup-fee Prices (per D-06 — attached as a checkout line item
    for monthly cadence Pro tiers)
  - One Customer Portal Configuration (D-09): allows subscription cancel +
    plan switch within the Pro family + invoice history. The configuration_id
    must be persisted into STRIPE_PORTAL_CONFIGURATION_ID via .env (Render
    dashboard); this script logs it on stdout for the operator.

Pricing matrix (per D-01 — keep in sync with content.ts pricing copy):

  | tld_class | cadence | currency | amount (cents) | display          |
  |-----------|---------|----------|----------------|------------------|
  | standard  | annual  | mxn      | 249900         | MX$2,499 / yr   |
  | standard  | annual  | usd      | 14700          | US$147 / yr     |
  | standard  | monthly | mxn      | 24900          | MX$249 / mo     |
  | standard  | monthly | usd      | 1499           | US$14.99 / mo   |
  | premium   | annual  | mxn      | 349900         | MX$3,499 / yr   |
  | premium   | annual  | usd      | 20600          | US$206 / yr     |
  | premium   | monthly | mxn      | 34900          | MX$349 / mo     |
  | premium   | monthly | usd      | 1999           | US$19.99 / mo   |

Setup fees (D-06, monthly cadence only — annual cadence amortizes setup):

  | tld_class | currency | amount (cents) |
  |-----------|----------|----------------|
  | standard  | mxn      | 17000          |
  | standard  | usd      | 1000           |
  | premium   | mxn      | 120000         |
  | premium   | usd      | 7000           |

Stripe Tax: enabled per Product via ``tax_behavior="exclusive"`` on each Price
(per D-05 — Mexican IVA 16% on MXN customers, US sales-tax on US customers).

Usage:
  STRIPE_SECRET_KEY=sk_test_... python medikah-chat-api/scripts/seed_stripe_products.py

Operator note: live Stripe API calls are an operator checkpoint. This script
writes the code and the matrix; it does NOT run during plan execution.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

# We import stripe at module top to fail fast in this script-context. The
# webhook handler imports lazily because it must remain importable without
# the SDK during dev.
try:
    import stripe  # type: ignore
except ImportError:
    print(
        "ERROR: stripe SDK is not installed. Install via "
        "`pip install -r medikah-chat-api/requirements.txt`.",
        file=sys.stderr,
    )
    sys.exit(2)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed_stripe_products")


# ---------------------------------------------------------------------------
# D-01 pricing matrix — exact source of truth
# ---------------------------------------------------------------------------

# Each entry: (tld_class, cadence, currency, amount_cents, interval)
RECURRING_PRICES: list[tuple[str, str, str, int, str]] = [
    ("standard", "annual",  "mxn", 249900, "year"),
    ("standard", "annual",  "usd",  14700, "year"),
    ("standard", "monthly", "mxn",  24900, "month"),
    ("standard", "monthly", "usd",   1499, "month"),
    ("premium",  "annual",  "mxn", 349900, "year"),
    ("premium",  "annual",  "usd",  20600, "year"),
    ("premium",  "monthly", "mxn",  34900, "month"),
    ("premium",  "monthly", "usd",   1999, "month"),
]

# Each entry: (tld_class, currency, amount_cents)
SETUP_PRICES: list[tuple[str, str, int]] = [
    ("standard", "mxn",  17000),
    ("standard", "usd",   1000),
    ("premium",  "mxn", 120000),
    ("premium",  "usd",   7000),
]


def _product_lookup_id(tld_class: str, cadence: str) -> str:
    """Stable, deterministic Stripe product ID per D-05."""
    return f"practikah_pro_{tld_class}_{cadence}"


def _recurring_price_lookup(tld_class: str, cadence: str, currency: str) -> str:
    return f"practikah_pro_{tld_class}_{cadence}_{currency}"


def _setup_price_lookup(tld_class: str, currency: str) -> str:
    return f"practikah_pro_{tld_class}_setup_{currency}"


# ---------------------------------------------------------------------------
# Idempotent helpers
# ---------------------------------------------------------------------------

def _ensure_product(tld_class: str, cadence: str) -> Any:
    """Idempotently create or fetch a Product by deterministic id."""
    pid = _product_lookup_id(tld_class, cadence)
    try:
        existing = stripe.Product.retrieve(pid)
        logger.info("product exists: %s", pid)
        return existing
    except stripe.error.InvalidRequestError:
        # Not found — create.
        pass

    product = stripe.Product.create(
        id=pid,
        name=f"Práctikah Pro {tld_class.title()} ({cadence})",
        description=f"Práctikah Pro {tld_class} tier, billed {cadence}.",
        metadata={
            "tld_class": tld_class,
            "cadence": cadence,
            "phase": "13",
        },
    )
    logger.info("product created: %s", pid)
    return product


def _find_price_by_lookup(lookup_key: str) -> Optional[Any]:
    """Return the active Price with this lookup_key, or None."""
    result = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1)
    if result.data:
        return result.data[0]
    return None


def _ensure_recurring_price(
    product_id: str,
    tld_class: str,
    cadence: str,
    currency: str,
    amount_cents: int,
    interval: str,
) -> Any:
    lookup_key = _recurring_price_lookup(tld_class, cadence, currency)
    existing = _find_price_by_lookup(lookup_key)
    if existing is not None:
        logger.info("price exists: %s (%s)", lookup_key, existing.id)
        return existing

    price = stripe.Price.create(
        product=product_id,
        unit_amount=amount_cents,
        currency=currency,
        recurring={"interval": interval},
        lookup_key=lookup_key,
        # Stripe Tax (D-05): customer's tax-residency determines whether tax is
        # added on top. "exclusive" means our amount excludes tax — Stripe
        # computes & adds it at checkout when Tax is enabled at the account level.
        tax_behavior="exclusive",
        metadata={
            "tld_class": tld_class,
            "cadence": cadence,
            "currency": currency,
            "kind": "recurring",
        },
    )
    logger.info("price created: %s (%s)", lookup_key, price.id)
    return price


def _ensure_setup_price(
    product_id: str,
    tld_class: str,
    currency: str,
    amount_cents: int,
) -> Any:
    lookup_key = _setup_price_lookup(tld_class, currency)
    existing = _find_price_by_lookup(lookup_key)
    if existing is not None:
        logger.info("setup price exists: %s (%s)", lookup_key, existing.id)
        return existing

    price = stripe.Price.create(
        product=product_id,
        unit_amount=amount_cents,
        currency=currency,
        # No `recurring` — one-time charge per D-06.
        lookup_key=lookup_key,
        tax_behavior="exclusive",
        metadata={
            "tld_class": tld_class,
            "currency": currency,
            "kind": "setup",
        },
    )
    logger.info("setup price created: %s (%s)", lookup_key, price.id)
    return price


def _ensure_portal_configuration() -> Optional[str]:
    """Create the Customer Portal configuration once (D-09).

    Allows:
      - subscription_cancel (immediate cancel; webhook fires
        customer.subscription.deleted → dunning auto-downgrade saga)
      - subscription_update with switching between Pro standard/premium
        within the Pro family (downgrade to free is NOT a Portal action —
        downgrade-to-free is gated behind our own UI per D-09 / D-29 to
        ensure the doctor sees the data-loss warnings)
      - invoice_history (always read-only)

    Idempotency: Stripe Portal configuration objects don't have lookup_keys.
    We list active configurations and skip creation if one with the same
    business_profile.headline already exists.
    """
    HEADLINE = "Práctikah Pro Billing"
    try:
        existing = stripe.billing_portal.Configuration.list(active=True, limit=100)
        for cfg in existing.data:
            bp = getattr(cfg, "business_profile", None) or {}
            if isinstance(bp, dict):
                headline = bp.get("headline")
            else:
                headline = getattr(bp, "headline", None)
            if headline == HEADLINE:
                logger.info("portal configuration exists: %s", cfg.id)
                return cfg.id
    except Exception:
        logger.exception("failed to list portal configurations; will attempt create")

    # Build the list of Pro recurring price IDs eligible for plan switching.
    pro_recurring_ids: list[str] = []
    for tld_class, cadence, currency, _amount, _interval in RECURRING_PRICES:
        lookup_key = _recurring_price_lookup(tld_class, cadence, currency)
        p = _find_price_by_lookup(lookup_key)
        if p is not None:
            pro_recurring_ids.append(p.id)

    products_for_update: list[dict[str, Any]] = []
    # Group price IDs by product for the Portal "products" allowlist.
    by_product: dict[str, list[str]] = {}
    for tld_class, cadence, currency, _a, _i in RECURRING_PRICES:
        product_id = _product_lookup_id(tld_class, cadence)
        lookup_key = _recurring_price_lookup(tld_class, cadence, currency)
        p = _find_price_by_lookup(lookup_key)
        if p is None:
            continue
        by_product.setdefault(product_id, []).append(p.id)
    for product_id, price_ids in by_product.items():
        products_for_update.append({"product": product_id, "prices": price_ids})

    cfg = stripe.billing_portal.Configuration.create(
        business_profile={
            "headline": HEADLINE,
        },
        features={
            "customer_update": {
                "enabled": True,
                "allowed_updates": ["email", "address", "tax_id"],
            },
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {
                "enabled": True,
                "mode": "immediately",
                "proration_behavior": "none",
            },
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["price"],
                "proration_behavior": "create_prorations",
                "products": products_for_update,
            } if products_for_update else {"enabled": False},
        },
    )
    logger.info("portal configuration created: %s", cfg.id)
    return cfg.id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        print(
            "ERROR: STRIPE_SECRET_KEY is not set. "
            "Export your test-mode secret (sk_test_...) and re-run.",
            file=sys.stderr,
        )
        return 2

    if not (secret.startswith("sk_test_") or secret.startswith("sk_live_")):
        print(
            "ERROR: STRIPE_SECRET_KEY does not look like a Stripe secret key "
            "(expected sk_test_... or sk_live_...).",
            file=sys.stderr,
        )
        return 2

    if secret.startswith("sk_live_"):
        logger.warning(
            "Running against LIVE Stripe. Press Ctrl-C within 5 seconds to abort."
        )
        import time
        time.sleep(5)

    stripe.api_key = secret

    # --- 1. Products ---
    products: dict[tuple[str, str], Any] = {}
    for (tld_class, cadence, _c, _a, _i) in RECURRING_PRICES:
        key = (tld_class, cadence)
        if key not in products:
            products[key] = _ensure_product(tld_class, cadence)

    # --- 2. Recurring prices ---
    for tld_class, cadence, currency, amount_cents, interval in RECURRING_PRICES:
        product = products[(tld_class, cadence)]
        _ensure_recurring_price(
            product_id=product.id,
            tld_class=tld_class,
            cadence=cadence,
            currency=currency,
            amount_cents=amount_cents,
            interval=interval,
        )

    # --- 3. Setup prices (attach to the *_monthly Product per D-06) ---
    for tld_class, currency, amount_cents in SETUP_PRICES:
        product = products[(tld_class, "monthly")]
        _ensure_setup_price(
            product_id=product.id,
            tld_class=tld_class,
            currency=currency,
            amount_cents=amount_cents,
        )

    # --- 4. Customer Portal configuration ---
    cfg_id = _ensure_portal_configuration()
    if cfg_id:
        print()
        print("=" * 72)
        print(f"STRIPE_PORTAL_CONFIGURATION_ID={cfg_id}")
        print("Persist this in your Render dashboard env (and .env.example note).")
        print("=" * 72)

    logger.info("seed_stripe_products: done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
