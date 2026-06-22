"""
utils/openai_client.py
----------------------
Shared OpenAI client singleton + a provider-neutral completion wrapper.

CUE-09: The two hardcoded gpt-4o call sites (routes/ai_routes.py and
services/ai_triage.py) now call `openai_complete()` instead of
`client.chat.completions.create(...)` directly. This wrapper is the
migration seam: it wraps the OpenAI client in a `complete()`-shaped
interface so callers stay provider-swappable (a future US-jurisdiction
BAA provider or Claude can drop in with zero call-site edits).

This module deliberately stays OpenAI-only — it is NOT the Anthropic
adapter (that lives in services/cue/adapter.py). The two gpt-4o
call sites stay on OpenAI for now; they route through this wrapper.

No provider-specific types leak to callers from this module.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

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


async def openai_complete(
    *,
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    """
    Provider-neutral completion call over the OpenAI client.

    CUE-09 migration seam: call sites use this instead of
    `client.chat.completions.create(...)` directly. The interface
    matches the `complete(messages, model, max_tokens, temperature)`
    contract so a provider swap (Claude or a BAA provider) needs zero
    call-site edits — only this wrapper is replaced.

    Returns the assistant message text, or None on failure so the
    caller can apply its own graceful-fallback logic (matching the
    existing pattern in ai_triage.py and ai_routes.py).

    No Anthropic types / provider-specific objects are returned here.
    """
    client = get_openai_client()
    if client is None:
        return None

    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = completion.choices[0] if completion.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()
        logger.warning("openai_complete: empty response from model %s", model)
        return None
    except Exception:
        logger.exception("openai_complete: call failed (model=%s)", model)
        return None
