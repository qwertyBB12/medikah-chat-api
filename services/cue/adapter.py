"""
services/cue/adapter.py
-----------------------
Provider-agnostic model-adapter seam for Medikah Cue.

Python port of BeNeXT `lib/companion/adapters/{types,index,anthropic}.ts`.

CUE-02 CONTRACT — ZERO LEAK RULE
---------------------------------
This file is the ONLY place in the codebase where:
  - `anthropic.types.*` (Message, ContentBlock, ToolUseBlock, etc.) may be imported
  - `cache_control` may appear as a dict key
  - The string literal `"ephemeral"` may appear as a cache strategy value
  - Any other Anthropic-SDK-specific type or shape may appear

The engine, tools, gates, and callers interact ONLY with:
  - CueNeutralTool  — vendor-neutral tool definition
  - CueModelAdapter — vendor-neutral adapter contract
  - create_adapter() — registry factory

Swapping or adding a provider = one new adapter class + one `create_adapter` case.
Zero engine/tool/gate edits required (CUE-07 extension point).

PORT NOTE
---------
BeNeXT adapters/types.ts had two leaks that are fixed here (CUE-02):
  1. `CueTool = Anthropic.Tool`  → replaced by CueNeutralTool (Pydantic; neutral shape)
  2. `systemCacheControl: 'ephemeral'` in CueStreamParams
     → replaced by `system_cache_strategy: Literal["ephemeral"] | None`
     The literal "ephemeral" and the `cache_control` dict key live ONLY inside
     AnthropicAdapter._build_system_param(), never in callers.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Literal

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import Message
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Neutral types (CUE-02: no Anthropic-shaped types exported from this file
# to the engine/tools layer — callers import only what is defined below)
# ---------------------------------------------------------------------------


class CueNeutralTool(BaseModel):
    """
    Vendor-neutral tool definition.

    Port of BeNeXT `CueTool = Anthropic.Tool` — the leak is fixed: callers
    define tools as CueNeutralTool; the AnthropicAdapter translates to the
    SDK dict shape via to_anthropic_dict().

    The engine and tool executors ONLY import CueNeutralTool, never
    `anthropic.types.*` or any other SDK-specific type.
    """

    name: str
    description: str
    input_schema: dict  # JSON Schema object ({"type":"object","properties":{},...})

    def to_anthropic_dict(self) -> dict[str, Any]:
        """Translate to the Anthropic SDK tool shape.

        This translation MUST stay inside the adapter module (CUE-02).
        The dict shape is an Anthropic concern; callers never see it.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ---------------------------------------------------------------------------
# Neutral stream + complete params (replaces CueStreamParams from types.ts)
# ---------------------------------------------------------------------------

# system_cache_strategy is the neutral name for what BeNeXT called
# systemCacheControl: 'ephemeral' — callers set this field; the
# AnthropicAdapter translates it to cache_control INTERNALLY only.
SystemCacheStrategy = Literal["ephemeral"] | None


# ---------------------------------------------------------------------------
# Vendor-neutral adapter contract
# ---------------------------------------------------------------------------


class CueModelAdapter(ABC):
    """
    Vendor-neutral adapter interface (port of BeNeXT CueModelAdapter).

    All engine/tool/gate code depends on this ABC only.
    No SDK type may appear in any concrete method signature visible to callers.

    Two call modes:
      stream()    — yields text deltas; used for streamed user-facing responses
      complete()  — returns an opaque provider response object; used for
                    tool-use loop turns (where callers inspect stop_reason +
                    content blocks) and for background judges
    """

    @abstractmethod
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
        """Yield text deltas. Caller drives the agentic loop."""
        ...  # pragma: no cover

    @abstractmethod
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
        """
        Non-streaming call. Returns the raw provider response object.

        Used by:
          - The tool-use loop to inspect stop_reason + content blocks
          - Background judges (Haiku tier; non-blocking)

        IMPORTANT: Callers must not import or type-annotate the return value
        with any provider-specific type (e.g. anthropic.types.Message).
        Access content via duck-typing or treat as Any. The adapter contract
        intentionally returns Any here so callers stay provider-neutral.
        """
        ...  # pragma: no cover

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
        """
        Stream ONE model turn, yielding neutral events (CUE-03 TTFT path).

        Yields, in order:
          • zero or more {"type": "text", "text": <delta>} — text deltas as the
            model generates them (Time-To-First-Token streaming)
          • exactly one {"type": "message", "stop_reason": str, "content": list,
            "usage": {"input_tokens": int, "output_tokens": int}} — the terminal
            message, carrying the tool_use/stop_reason/usage the engine inspects

        This is the single primitive the engine's tool loop drives: it gives BOTH
        live text deltas AND the full final message (incl. tool_use blocks +
        usage) from a single model call — no double round-trip (the deferred
        Phase-23 TTFT optimization, done correctly).

        DEFAULT (this method): a non-streaming fallback built on complete() — it
        completes the turn, surfaces the assembled text as a single delta, then
        the message event. Adapters with true token streaming (AnthropicAdapter)
        OVERRIDE this. Keeping a working default means every existing adapter /
        test double gets stream_turn for free with identical loop semantics.
        """
        response = await self.complete(
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            system_cache_strategy=system_cache_strategy,
        )
        stop_reason, content, usage = _coerce_response_fields(response)
        text = _coerce_text(content)
        if text:
            yield {"type": "text", "text": text}
        yield {
            "type": "message",
            "stop_reason": stop_reason,
            "content": content,
            "usage": usage,
        }


# ---------------------------------------------------------------------------
# Anthropic adapter (the only concrete adapter in Phase 22)
# ---------------------------------------------------------------------------


class AnthropicAdapter(CueModelAdapter):
    """
    Anthropic Claude adapter (port of BeNeXT adapters/anthropic.ts).

    This is the SOLE place in the codebase where:
      - anthropic.types.* is used
      - cache_control appears as a dict key
      - "ephemeral" appears as a cache-strategy value

    Zero of these implementation details may leak to the engine or tools layer.
    """

    def __init__(self, client: AsyncAnthropic) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_system_param(
        self,
        system_prompt: str,
        system_cache_strategy: SystemCacheStrategy,
    ) -> str | list[dict]:
        """
        Translate the neutral system_cache_strategy into the Anthropic SDK
        `system` parameter shape.

        CUE-02: cache_control and "ephemeral" live ONLY here — never in callers.

        Port of BeNeXT adapters/anthropic.ts lines 8-16:
            const system = params.systemCacheControl
              ? [{ type: 'text', text: params.systemPrompt,
                   cache_control: { type: params.systemCacheControl } }]
              : params.systemPrompt
        """
        if system_cache_strategy == "ephemeral":
            # Anthropic-specific prompt caching shape — internal only
            return [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},  # Anthropic SDK shape
                }
            ]
        return system_prompt

    def _translate_tools(
        self, tools: list[CueNeutralTool] | None
    ) -> list[dict] | None:
        """Translate CueNeutralTool list to Anthropic SDK tool dicts."""
        if not tools:
            return None
        return [t.to_anthropic_dict() for t in tools]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

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
        """
        Yield text deltas.

        Port of BeNeXT adapters/anthropic.ts `stream()` method.
        Anthropic SDK `.messages.stream()` is used; only text_delta events
        are yielded. Tool-use blocks do NOT appear in the delta stream
        (confirmed in BeNeXT adapters/anthropic.ts:29-31 + Anthropic docs) —
        callers needing tool-use detection must use complete() instead.
        """
        system = self._build_system_param(system_prompt, system_cache_strategy)
        sdk_tools = self._translate_tools(tools)

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if sdk_tools:
            kwargs["tools"] = sdk_tools

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text

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
        """
        True token-streaming turn (CUE-03 TTFT). Overrides the ABC default.

        Uses the SDK `.messages.stream()` helper, which surfaces BOTH the live
        text deltas (`stream.text_stream`) AND the fully-assembled terminal
        message (`get_final_message()` — incl. tool_use blocks + usage). This is
        why streaming the FINAL turn no longer needs a second model call: one
        stream yields the user-facing deltas while still letting the engine
        inspect stop_reason/tool_use afterwards (AI-SPEC §4b.2, done right).

        Yields {"type":"text", ...} deltas, then one {"type":"message", ...}.
        """
        system = self._build_system_param(system_prompt, system_cache_strategy)
        sdk_tools = self._translate_tools(tools)

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if sdk_tools:
            kwargs["tools"] = sdk_tools

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    yield {"type": "text", "text": text}
            final = await stream.get_final_message()

        usage = getattr(final, "usage", None)
        yield {
            "type": "message",
            "stop_reason": getattr(final, "stop_reason", "end_turn") or "end_turn",
            "content": getattr(final, "content", []) or [],
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            },
        }

    async def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[CueNeutralTool] | None = None,
        max_tokens: int = 1024,
        system_cache_strategy: SystemCacheStrategy = None,
    ) -> Message:
        """
        Non-streaming call. Returns the raw Anthropic Message object.

        Used by:
          - Tool-use loop turns (inspect stop_reason + content blocks)
          - Background judges (Haiku; non-blocking, CUE-04b)

        NOTE: The return type is anthropic.types.Message — this annotation
        is ONLY in the concrete class. The ABC declares `Any` so callers
        that type-annotate against CueModelAdapter stay provider-neutral.
        """
        system = self._build_system_param(system_prompt, system_cache_strategy)
        sdk_tools = self._translate_tools(tools)

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if sdk_tools:
            kwargs["tools"] = sdk_tools

        return await self._client.messages.create(**kwargs)


# ---------------------------------------------------------------------------
# Duck-typed response coercion for the default stream_turn() fallback
# ---------------------------------------------------------------------------
# These mirror the engine's duck-typed accessors but live here so the ABC's
# default stream_turn() (the non-streaming complete()-wrapper) stays
# self-contained — engine.py imports adapter.py, so adapter.py must not import
# engine.py (no circular import). Used ONLY by the default fallback; the real
# AnthropicAdapter.stream_turn() override never touches these.


def _coerce_response_fields(response: Any) -> tuple[str, list[Any], dict[str, int]]:
    """Extract (stop_reason, content, usage) from a dict OR SDK Message."""
    if isinstance(response, dict):
        stop_reason = response.get("stop_reason", "end_turn") or "end_turn"
        content = response.get("content", []) or []
        usage = response.get("usage", {}) or {}
        usage_dict = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
        return stop_reason, content, usage_dict
    stop_reason = getattr(response, "stop_reason", "end_turn") or "end_turn"
    content = getattr(response, "content", []) or []
    usage_obj = getattr(response, "usage", None)
    usage_dict = {
        "input_tokens": getattr(usage_obj, "input_tokens", 0) if usage_obj else 0,
        "output_tokens": getattr(usage_obj, "output_tokens", 0) if usage_obj else 0,
    }
    return stop_reason, content, usage_dict


def _coerce_text(content: list[Any]) -> str:
    """Concatenate all text blocks from a content list (dict or SDK block)."""
    parts: list[str] = []
    for block in content:
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", "")
        if btype == "text":
            text = (block.get("text") if isinstance(block, dict) else getattr(block, "text", "")) or ""
            parts.append(text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Provider registry + factory (port of BeNeXT adapters/index.ts)
# ---------------------------------------------------------------------------

# Extend this literal when a new provider adapter is added (CUE-07).
# Zero engine/tool/gate edits are required — only a new class above and
# a new case in create_adapter() below.
Provider = Literal["anthropic"]  # | "openai" | "deepseek" in future

# Process-level singleton cache keyed by provider string.
# Mirrors BeNeXT createAdapter() — one client per process.
_adapter_cache: dict[str, CueModelAdapter] = {}


def create_adapter(provider: Provider = "anthropic") -> CueModelAdapter:
    """
    Adapter registry / factory (port of BeNeXT adapters/index.ts createAdapter()).

    Returns a cached adapter instance. Anthropic is the only wired provider
    in Phase 22 (CUE-07: the registry extension point is here; engine never
    changes when a new provider is added).

    Raises ValueError for unknown providers (exhaustive — port of the
    TypeScript `never`-typed default branch).
    """
    cached = _adapter_cache.get(provider)
    if cached is not None:
        return cached

    if provider == "anthropic":
        api_key = os.environ["ANTHROPIC_API_KEY"]  # CUE-05: host shim (import.meta.env → os.environ)
        adapter: CueModelAdapter = AnthropicAdapter(AsyncAnthropic(api_key=api_key))
        _adapter_cache[provider] = adapter
        return adapter

    # Exhaustive default — port of TypeScript `const _exhaustive: never = provider`
    raise ValueError(f"Unknown provider: {provider!r}. Add a new adapter class and a case here (CUE-07).")


# ---------------------------------------------------------------------------
# Tier → model ID routing (from AI-SPEC §4)
# ---------------------------------------------------------------------------

# Model IDs verified against Anthropic Models Overview 2026-06-21.
# Do NOT change without re-verifying against the live docs.
_TIER_MODELS: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",   # background memory/flag judges; free/trial tier
    "sonnet": "claude-sonnet-4-6",           # default reasoning brain (all active physicians)
    "opus": "claude-opus-4-8",               # highest-stakes clinical (Phase 24 diagnosis surface)
}
_DEFAULT_TIER = "sonnet"


def select_model(tier: str = _DEFAULT_TIER) -> str:
    """
    Return the dated model ID for a reasoning tier.

    Physicians are NEVER charged. Tiers gate model quality and daily token
    quota only (CUE-06).

    Port of BeNeXT engine.ts:406-408 tier→model routing.
    """
    return _TIER_MODELS.get(tier, _TIER_MODELS[_DEFAULT_TIER])
