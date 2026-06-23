"""
services/cue/voice/providers.py
-------------------------------
Provider-agnostic TTS seam for Medikah Cue voice (VOICE-01 / VOICE-03).

Python port of BeNeXT `lib/cue-api/providers/{types,index}.ts`.

VOICE-01 — one registry entry per provider
------------------------------------------
Adding a TTS provider is a SINGLE `_REGISTRY` entry + a concrete class, exactly
mirroring `services/cue/adapter.create_adapter()` for the LLM side. The engine,
routes, and catalog never change when a provider is added or swapped.

VOICE-03 — STRICT separation from the reasoning brain
-----------------------------------------------------
The TTS registry is strictly separate from the LLM adapter
(`services/cue/adapter.py`). This module — and every provider it imports — MUST
NOT import `services.cue.adapter`. A voice provider may never become the
reasoning brain. `tests/cue/test_voice_providers.py` asserts the zero-import.

DECIDED DIRECTION (2026-06-23)
------------------------------
`voxtral` (Mistral cloud) is the WORKING DEFAULT. `f5` (self-hosted Gradio) is
registered but DORMANT — built to the verified contract, not deployed. Flipping
F5 on later is a one-line default/catalog change, no route edits.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class VoiceProviderError(Exception):
    """Typed TTS failure the route maps onto an HTTP status.

    `kind` mirrors BeNeXT's TtsError discriminated union:
      "unauthorized" → 503 (provider key missing/invalid)
      "upstream"     → 502 (provider returned a non-OK response)
      "timeout"      → 504
      "invalid_voice"→ 502 (voice id not valid in the provider namespace)
      "unknown"      → 502
    """

    def __init__(self, kind: str, message: str = "") -> None:
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}" if message else kind)


class CueTtsProvider(ABC):
    """Vendor-neutral TTS provider contract (port of BeNeXT `TtsProvider`).

    `synthesize` takes a `voice_id` that is valid ONLY in this provider's
    namespace (F5 Gradio ref-audio paths and Voxtral Mistral voice ids are
    different namespaces — the catalog resolver returns the provider ALONGSIDE
    the id so the route never conflates them).
    """

    name: str

    @abstractmethod
    async def synthesize(self, *, text: str, voice_id: str, locale: str) -> bytes:
        """Return raw audio bytes for `text`. `locale` in {"en", "es"}."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Registry / factory (VOICE-01 — one entry per provider; mirror create_adapter)
# ---------------------------------------------------------------------------

# Process-level singleton cache keyed by provider name (mirrors create_adapter's
# _adapter_cache — one instance per process). Concrete providers are imported
# LAZILY inside the factory: they import CueTtsProvider/VoiceProviderError from
# THIS module, so a top-level import here would be circular.
_provider_cache: dict[str, CueTtsProvider] = {}


def create_tts_provider(name: str = "voxtral") -> CueTtsProvider:
    """Return the provider impl for `name` (VOICE-01 extension point).

    Default is `voxtral` — the working cloud default (decided 2026-06-23); `f5`
    is registered but DORMANT (not deployed). Adding a provider = one new class +
    one elif case here; zero engine/route edits. Raises ValueError for an unknown
    provider (exhaustive — port of the TS `never`-typed default branch), exactly
    like `services/cue/adapter.create_adapter`.
    """
    cached = _provider_cache.get(name)
    if cached is not None:
        return cached

    if name == "voxtral":
        from services.cue.voice.voxtral_provider import VoxtralProvider

        provider: CueTtsProvider = VoxtralProvider()  # WORKING DEFAULT — Mistral cloud
    elif name == "f5":
        from services.cue.voice.f5_provider import F5TtsProvider

        provider = F5TtsProvider()  # DORMANT — sovereign self-host, deferred
    else:
        raise ValueError(
            f"Unknown TTS provider: {name!r}. "
            "Add a provider class and an elif case here (VOICE-01)."
        )

    _provider_cache[name] = provider
    return provider
