"""Supabase database client singleton."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_supabase_client = None


def is_production() -> bool:
    """Return True when running on Render (production).

    Render sets ``RENDER=true`` automatically on all deployed services.
    """
    return bool(os.getenv("RENDER"))


def get_supabase():
    """Return the Supabase client, or None if not configured.

    In production (Render), raises ``RuntimeError`` when credentials are
    missing or the client fails to initialise.  In development, returns
    ``None`` so the caller can fall back to in-memory storage.
    """
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        if is_production():
            raise RuntimeError(
                "Supabase credentials (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY) "
                "are required in production. Set these environment variables in "
                "your Render dashboard."
            )
        logger.warning("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set; using in-memory storage.")
        return None

    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        logger.info("Supabase client initialized.")
        return _supabase_client
    except Exception as exc:
        if is_production():
            raise RuntimeError(
                "Failed to initialize Supabase client in production"
            ) from exc
        logger.exception("Failed to initialize Supabase client; falling back to in-memory.")
        return None


def require_supabase():
    """Return the Supabase client or raise ``RuntimeError``.

    Convenience wrapper for code paths that must have a working Supabase
    connection (e.g. production-only operations).
    """
    client = get_supabase()
    if client is None:
        raise RuntimeError(
            "Supabase client is required but not available. "
            "Ensure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set."
        )
    return client
