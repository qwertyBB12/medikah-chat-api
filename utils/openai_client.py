"""Shared OpenAI client singleton."""

from __future__ import annotations

import logging
import os
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None
_initialized = False


def get_openai_client() -> Optional[AsyncOpenAI]:
    """Return the shared AsyncOpenAI client, or None if not configured."""
    global _client, _initialized
    if _initialized:
        return _client

    _initialized = True
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set; OpenAI client disabled.")
        return None

    try:
        _client = AsyncOpenAI(api_key=api_key)
        logger.info("Shared OpenAI client initialised successfully.")
    except Exception:
        logger.exception("Failed to create OpenAI client.")
        _client = None

    return _client
