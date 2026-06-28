"""
services/cue/memory/embeddings.py
---------------------------------
Slice 2 — embedding provider seam for semantic memory recall.

Turns a note (or a query) into a vector so pgvector can rank memories by meaning,
not by recency. This is the ONE swap point: today it calls a hosted multilingual
model (OpenAI text-embedding-3-small, 1536-d — the dim the migration's vector(N)
column expects); a future sovereign/local model drops in by replacing embed()
alone, with no call-site changes (read seam + judge call embed() only).

The note is already redacted and deliberately thin before it reaches here
(PATCH-01), so the provider only ever sees a cleaned, low-sensitivity sentence.

Fail-open: returns None on any error / missing key. Callers fall back to
recency-ordered recall and store the note with a null embedding.
"""
from __future__ import annotations

import logging

from utils.openai_client import get_openai_client

logger = logging.getLogger(__name__)

# Model + dim. EMBED_DIM MUST match the vector(N) column in migration 036.
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536

# Cap input so a pathological note can't run up the embedding call.
_MAX_CHARS = 8000


async def embed(text: str | None) -> list[float] | None:
    """Return the embedding vector for `text`, or None (fail-open) on any miss.

    Never raises. Empty/blank text returns None without calling the API.
    """
    if not text or not text.strip():
        return None
    client = get_openai_client()
    if client is None:
        return None
    try:
        resp = await client.embeddings.create(model=EMBED_MODEL, input=text[:_MAX_CHARS])
        return list(resp.data[0].embedding)
    except Exception:
        logger.exception("[cue-memory] embed failed (model=%s)", EMBED_MODEL)
        return None
