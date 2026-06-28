"""
utils/anthropic_client.py
-------------------------
Provider-neutral completion over the shared Cue Claude adapter (CUE-09 seam,
Claude path).

Mirrors utils/openai_client.openai_complete(), but routes through the Cue model
adapter (services/cue/adapter.py) so a call site runs on Claude — the Opus tier,
the model reserved for the high-stakes clinical / diagnosis surface — while
staying provider-switchable: adding or swapping a provider is one new adapter
class + a create_adapter() case, with zero call-site edits.

Two provider facts shape this wrapper, both handled here so callers stay neutral:
  * Claude separates the system prompt from the message list, so this wrapper
    takes `system_prompt` explicitly rather than a system-role message.
  * The Opus 4.x family rejects sampling parameters (temperature/top_p/top_k),
    so this wrapper forwards none.

It returns plain assistant text (or None on failure, matching the openai_complete
contract so callers keep their graceful-fallback logic). No provider-specific
types are returned to or required by callers.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from services.cue.adapter import create_adapter, select_model

logger = logging.getLogger(__name__)


async def anthropic_complete(
    *,
    system_prompt: str,
    messages: list[dict],
    tier: str = "opus",
    max_tokens: int = 1024,
) -> Optional[str]:
    """One-shot text completion on Claude via the Cue adapter.

    `tier` selects the model quality (opus = highest-stakes clinical). Returns
    the assistant text, or None if the provider is unconfigured or the call
    fails — so the caller keeps its own graceful-fallback behaviour.
    """
    try:
        adapter = create_adapter("anthropic")
    except KeyError:
        # ANTHROPIC_API_KEY not set — mirror openai_complete's "return None
        # when unconfigured" so the route degrades to a 503 rather than a 500.
        logger.warning("ANTHROPIC_API_KEY not set; Claude client disabled.")
        return None
    except Exception:
        logger.exception("Failed to create the Claude adapter.")
        return None

    try:
        response: Any = await adapter.complete(
            model=select_model(tier=tier),
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
        )
        # Duck-typed text extraction (no provider types imported): concatenate
        # the text blocks of the returned message.
        parts: list[str] = []
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        text_out = "".join(parts).strip()
        return text_out or None
    except Exception:
        logger.exception("Claude completion failed.")
        return None
