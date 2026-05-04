"""Active-Pro redirect map (Phase 13 D-24/D-25/D-26).

Returns a {slug: custom_domain} mapping of every physician whose Pro
subscription is currently active AND who has a published custom domain.
Consumed by Next.js edge middleware via FastAPI internal endpoint and a
60s in-memory cache (lib/proRedirectLookup.ts).

Filter (must apply ALL three to qualify for redirect):
  1. physician_workspace_accounts.tier == 'pro'
  2. physician_workspace_accounts.subscription_status == 'active'
  3. physician_website.published_to_domain_id IS NOT NULL

When any of these flips back, the row drops out of the map and the
existing rewrite to /sites/<slug> is restored within ≤60s — this is the
revertibility contract from D-25 / PRO-17.

Slug derivation mirrors medikah-chat-frontend/lib/slug.ts:nameToSlug
(see utils/slug.py:name_to_slug).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from utils.slug import name_to_slug

logger = logging.getLogger(__name__)


async def active_pro_redirect_map(db) -> Dict[str, str]:
    """Return {slug: custom_domain} for active Pro physicians with a published domain.

    Uses three sequential, focused queries instead of a single nested join so
    we never depend on supabase-py's PostgREST FK-graph inference (which is
    fragile across schema migrations).

    Args:
        db: supabase Client (admin/service-role).

    Returns:
        dict mapping ``name_to_slug(full_name)`` → ``physician_domains.domain_name``.
        Empty dict on any failure (fail-open downstream — middleware falls back
        to the existing rewrite to /sites/<slug>).
    """
    if db is None:
        logger.warning("[redirect_cache] supabase client is None — returning empty map")
        return {}

    try:
        # Step 1 — active Pro workspace accounts
        ws_resp = (
            db.table("physician_workspace_accounts")
            .select("physician_id, tier, subscription_status")
            .eq("tier", "pro")
            .eq("subscription_status", "active")
            .execute()
        )
        ws_rows = ws_resp.data or []
        if not ws_rows:
            return {}

        active_ids = [r["physician_id"] for r in ws_rows if r.get("physician_id")]
        if not active_ids:
            return {}

        # Step 2 — published websites among those physicians (must have domain id)
        site_resp = (
            db.table("physician_website")
            .select("physician_id, published_to_domain_id")
            .in_("physician_id", active_ids)
            .not_.is_("published_to_domain_id", "null")
            .execute()
        )
        site_rows = site_resp.data or []
        if not site_rows:
            return {}

        domain_ids = [r["published_to_domain_id"] for r in site_rows if r.get("published_to_domain_id")]
        physician_to_domain_id = {
            r["physician_id"]: r["published_to_domain_id"]
            for r in site_rows
            if r.get("physician_id") and r.get("published_to_domain_id")
        }

        # Step 3a — resolve domain names
        dom_resp = (
            db.table("physician_domains")
            .select("id, domain_name")
            .in_("id", domain_ids)
            .execute()
        )
        domain_id_to_name = {
            r["id"]: r["domain_name"]
            for r in (dom_resp.data or [])
            if r.get("id") and r.get("domain_name")
        }

        # Step 3b — resolve physician full_name → slug
        phys_ids_with_domain = list(physician_to_domain_id.keys())
        phys_resp = (
            db.table("physicians")
            .select("id, full_name")
            .in_("id", phys_ids_with_domain)
            .execute()
        )
        phys_rows = phys_resp.data or []

        out: Dict[str, str] = {}
        for p in phys_rows:
            pid = p.get("id")
            full_name = p.get("full_name") or ""
            if not pid or not full_name:
                continue
            domain_id = physician_to_domain_id.get(pid)
            domain_name = domain_id_to_name.get(domain_id) if domain_id else None
            if not domain_name:
                continue
            slug = name_to_slug(full_name)
            if slug:
                out[slug] = domain_name

        return out

    except Exception:
        # Fail-open: empty map → middleware preserves existing rewrite (PRO-17 bias).
        logger.exception("[redirect_cache] active_pro_redirect_map failed — returning empty map")
        return {}
