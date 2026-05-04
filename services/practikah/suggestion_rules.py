"""Pro-tier pricing matrix for the Phase 13-04 domain-search wizard.

Per D-01 (locked pricing matrix). Per PRO-02 (transparency): the consuming UI
displays the wholesale TLD price (sourced live from Cloudflare Registrar
Availability API) separately from the Práctikah service fee, so the doctor
sees exactly what they're paying for.

All values in CENTS (Stripe convention).

Standard TLD bucket: .com / .mx / .com.mx / .org / .net
Premium TLD bucket:  .doctor / .clinic
(.health is excluded from launch by D-04 — Cloudflare Registrar doesn't carry
it and Phase 13 ships no fallback registrar.)

The actual suggestion generation runs client-side in
``medikah-chat-frontend/lib/domainSuggestions.ts`` (D-19 deterministic rules);
this module exposes only the price lookup the BFF needs to render the PRO-02
breakdown.
"""

from __future__ import annotations

from typing import Literal, TypedDict


TldClass = Literal["standard", "premium"]
Country = Literal["MX", "US"]


class PricingEntry(TypedDict):
    """All amounts in CENTS. Currency follows physician country."""

    annual: int
    monthly: int
    monthly_setup: int
    currency: str


# Per D-01 — locked for launch. Raise on new cohorts post-PMF only (D-02).
PRICING: dict[tuple[TldClass, Country], PricingEntry] = {
    ("standard", "MX"): {
        "annual": 249900,           # MX$2,499 / year
        "monthly": 24900,           # MX$249 / month
        "monthly_setup": 17000,     # MX$170 setup (recoups domain prepay)
        "currency": "MXN",
    },
    ("standard", "US"): {
        "annual": 14700,            # US$147 / year
        "monthly": 1499,            # US$14.99 / month
        "monthly_setup": 1000,      # US$10 setup
        "currency": "USD",
    },
    ("premium", "MX"): {
        "annual": 349900,           # MX$3,499 / year
        "monthly": 34900,           # MX$349 / month
        "monthly_setup": 120000,    # MX$1,200 setup
        "currency": "MXN",
    },
    ("premium", "US"): {
        "annual": 20600,            # US$206 / year
        "monthly": 1999,            # US$19.99 / month
        "monthly_setup": 7000,      # US$70 setup
        "currency": "USD",
    },
}


def get_pricing(tld_class: str, country: str) -> PricingEntry:
    """Return the locked PRICING entry for `(tld_class, country)`.

    Raises ``KeyError`` if either dimension is unknown — caller is responsible
    for normalizing the inputs (uppercasing country, mapping the TLD to
    ``standard`` or ``premium``).
    """
    key = (tld_class, country.upper())
    return PRICING[key]  # type: ignore[index]


# Stable mapping of every Pro-launch TLD to its pricing class. Mirrors the
# weights in ``medikah-chat-frontend/lib/domainSuggestions.ts``. ``.health``
# intentionally absent per D-04.
TLD_CLASS: dict[str, TldClass] = {
    # Standard
    "com": "standard",
    "mx": "standard",
    "com.mx": "standard",
    "org": "standard",
    "net": "standard",
    # Premium
    "doctor": "premium",
    "clinic": "premium",
}


def classify_tld(tld: str) -> TldClass | None:
    """Return ``"standard"``, ``"premium"``, or ``None`` if the TLD isn't in
    the Phase 13 launch scope (D-04)."""

    return TLD_CLASS.get(tld.lower())


def country_weighted_tlds(country: str) -> dict[str, list[str]]:
    """Return the country-weighted TLD list per D-19. Mirrors the frontend
    ``TLD_WEIGHTS`` table in ``lib/domainSuggestions.ts`` so server-side
    enrichment (e.g., the ``/upgrade/domain-search`` BFF response) can validate
    that a client-supplied stem maps to a launch-scope TLD before any registrar
    or DNS work."""

    c = country.upper()
    if c == "MX":
        return {
            "standard": ["mx", "com.mx", "com", "org", "net"],
            "premium": ["doctor", "clinic"],
        }
    if c == "US":
        return {
            "standard": ["com", "org", "net"],
            "premium": ["doctor", "clinic"],
        }
    return {"standard": [], "premium": []}
