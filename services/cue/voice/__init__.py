"""
services/cue/voice/
-------------------
Cue voice stack (VOICE-01..08): provider-agnostic TTS registry, STT client,
and the per-doctor voice-catalog resolver.

DECIDED DIRECTION (2026-06-23, Hector): Cue voice ships on Voxtral (Mistral
cloud) as the working default. Self-hosted F5 (TTS) + faster-whisper (STT) are
DEFERRED to a later sovereignty upgrade on a DEDICATED box — never the Mailcow
VPS. F5 is ported here as a DORMANT provider (built to the verified Gradio-6
9-input contract) so it can be flipped on later with one registry/default edit.

VOICE-03 separation: voice providers are NEVER the reasoning brain. This package
MUST NOT import services.cue.adapter (the LLM seam). A test asserts the
zero-import (tests/cue/test_voice_providers.py).
"""
