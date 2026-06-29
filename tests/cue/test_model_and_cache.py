"""Model-tier routing + prompt caching on the live /cue/chat path.

Diagnosis 2026-06-28: every live doctor turn was pinned to Haiku — the tier the
adapter reserves for background memory/flag judges — which read to doctors as
"not intelligent / confused" and fumbled the scheduling date/tool-arg math. The
conversational / clinical + tool turn must use the documented Sonnet reasoning
brain; only the trivial opening greeting stays on the fast Haiku brain. Prompt
caching is threaded engine -> adapter so Sonnet's added prefill does not regress
TTFT (it was a built-but-unused seam — adapter.py had cache_control support that
no caller requested).
"""
from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from routes.cue_routes import _select_turn_model
from services.cue.adapter import (
    CueModelAdapter,
    CueNeutralTool,
    SystemCacheStrategy,
    select_model,
)
from services.cue.engine import run_cue_turn, run_cue_turn_streaming


# ---------------------------------------------------------------------------
# Model tier per turn type
# ---------------------------------------------------------------------------


def test_conversational_turn_uses_sonnet_reasoning_brain() -> None:
    model = _select_turn_model(opening=False)
    assert model == select_model(tier="sonnet")
    # Regression guard: the live doctor turn must NOT run on the weak/background brain.
    assert model != select_model(tier="haiku")


def test_opening_greeting_stays_on_fast_haiku_brain() -> None:
    # The greeting is a single warm sentence — the fast brain keeps the first
    # impression instant; quality matters far less for one templated sentence.
    assert _select_turn_model(opening=True) == select_model(tier="haiku")


# ---------------------------------------------------------------------------
# Prompt caching is threaded engine -> adapter
# ---------------------------------------------------------------------------


class _CacheSpyAdapter(CueModelAdapter):
    """Records the system_cache_strategy passed to stream_turn."""

    def __init__(self) -> None:
        self.seen_cache: list[Any] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[str]:  # pragma: no cover
        yield "x"

    async def complete(self, **kwargs: Any) -> Any:  # pragma: no cover
        return {"stop_reason": "end_turn", "content": [], "usage": {}}

    async def stream_turn(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[CueNeutralTool] | None = None,
        max_tokens: int = 1024,
        system_cache_strategy: SystemCacheStrategy = None,
    ) -> AsyncIterator[dict]:
        self.seen_cache.append(system_cache_strategy)
        yield {"type": "text", "text": "Hola."}
        yield {"type": "message", "stop_reason": "end_turn", "content": [], "usage": {}}


@pytest.mark.asyncio
async def test_engine_forwards_cache_strategy_to_adapter() -> None:
    adapter = _CacheSpyAdapter()
    async for _ in run_cue_turn_streaming(
        adapter,
        model="m",
        system_prompt="s",
        messages=[{"role": "user", "content": "hola"}],
        physician_id="doc-1",
        system_cache_strategy="ephemeral",
        tools=[],
    ):
        pass
    assert adapter.seen_cache == ["ephemeral"], (
        "run_cue_turn_streaming must forward system_cache_strategy to "
        "adapter.stream_turn so the large static clinical system prompt is cached."
    )


@pytest.mark.asyncio
async def test_engine_cache_strategy_defaults_off() -> None:
    # Default None = no behavior change for existing callers/tests.
    adapter = _CacheSpyAdapter()
    async for _ in run_cue_turn_streaming(
        adapter,
        model="m",
        system_prompt="s",
        messages=[{"role": "user", "content": "hola"}],
        physician_id="doc-1",
        tools=[],
    ):
        pass
    assert adapter.seen_cache == [None]


@pytest.mark.asyncio
async def test_run_cue_turn_wrapper_forwards_cache_strategy() -> None:
    adapter = _CacheSpyAdapter()
    await run_cue_turn(
        adapter,
        model="m",
        system_prompt="s",
        messages=[{"role": "user", "content": "hola"}],
        physician_id="doc-1",
        system_cache_strategy="ephemeral",
    )
    assert adapter.seen_cache == ["ephemeral"]
