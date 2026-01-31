"""Supabase database client singleton."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_supabase_client = None


def get_supabase():
    """Return the Supabase client, or None if not configured."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set; using in-memory storage.")
        return None

    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        logger.info("Supabase client initialized.")
        return _supabase_client
    except Exception:
        logger.exception("Failed to initialize Supabase client; falling back to in-memory.")
        return None
