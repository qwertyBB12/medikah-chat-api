"""
tests/cue/conftest.py
---------------------
Shared fixtures for Phase 22 Cue adapter tests.

Key fixture: DummyAdapter (EchoAdapter) — implements the full CueModelAdapter
contract with no Anthropic SDK dependency. Used by:
  - test_no_provider_leak.py: proves that a second adapter can drive
    complete()/stream() with zero engine/call-site edits (CUE-07 extension
    point proof; D1 swap test).
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from services.cue.adapter import CueModelAdapter, CueNeutralTool, SystemCacheStrategy


# ---------------------------------------------------------------------------
# DummyAdapter (EchoAdapter)
# ---------------------------------------------------------------------------


class DummyAdapter(CueModelAdapter):
    """
    A test double that implements the full CueModelAdapter contract.
    Uses NO Anthropic SDK, NO network calls.

    stream()    — yields the user's last message content character-by-character
    complete()  — returns a plain dict shaped like the minimal fields the
                  engine/caller inspects (stop_reason, content, usage).
                  Note: it returns a dict, not anthropic.types.Message — proving
                  that callers must not depend on the concrete SDK type.
    """

    def __init__(self, echo_prefix: str = "[DUMMY] ") -> None:
        self._prefix = echo_prefix

    async def stream(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[CueNeutralTool] | None = None,
        max_tokens: int = 1024,
        system_cache_strategy: SystemCacheStrategy = None,
    ) -> AsyncIterator[str]:
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "hello",
        )
        reply = f"{self._prefix}{last_user}"
        for ch in reply:
            yield ch

    async def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[CueNeutralTool] | None = None,
        max_tokens: int = 1024,
        system_cache_strategy: SystemCacheStrategy = None,
    ) -> Any:
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "hello",
        )
        reply = f"{self._prefix}{last_user}"
        # Returns a plain dict — callers must not require anthropic.types.Message
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": reply}],
            "usage": {"input_tokens": len(system_prompt), "output_tokens": len(reply)},
        }


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dummy_adapter() -> DummyAdapter:
    """A fresh DummyAdapter instance for each test."""
    return DummyAdapter()


@pytest.fixture
def sample_tool() -> CueNeutralTool:
    """A minimal CueNeutralTool for adapter contract tests."""
    return CueNeutralTool(
        name="test_tool",
        description="A tool for testing",
        input_schema={"type": "object", "properties": {}, "required": []},
    )


@pytest.fixture
def sample_messages() -> list[dict]:
    """Minimal message history for a single-turn test."""
    return [{"role": "user", "content": "Hello, Cue."}]
