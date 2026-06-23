"""
services/cue/voice/voxtral_provider.py
--------------------------------------
Voxtral (Mistral cloud) TTS provider — the WORKING DEFAULT (decided 2026-06-23).

Python port of BeNeXT `lib/cue-api/providers/voxtral.ts`. Calls the Mistral
audio/speech endpoint and returns raw audio bytes.

VOICE-03: this is a VOICE provider only. It never imports the LLM adapter and
never becomes the reasoning brain. Mistral here is voice-only.

Contract (verified against docs.mistral.ai 2026-06-23 + BeNeXT's deployed
voxtral.ts):
  POST https://api.mistral.ai/v1/audio/speech
  Authorization: Bearer $MISTRAL_API_KEY
  body: {model, input, voice_id, response_format}
  → {"audio_data": "<base64>"}
The model id (voxtral-mini-tts-2603) and the request field names are ported
from the deployed BeNeXT client and are env-overridable so a contract change is
a config edit, not a code change.
"""

from __future__ import annotations

import base64
import os

import httpx

from services.cue.voice.providers import CueTtsProvider, VoiceProviderError

_MISTRAL_SPEECH_URL = "https://api.mistral.ai/v1/audio/speech"
# Pin the deployed Voxtral TTS model; override via env if Mistral revs it.
_TTS_MODEL = os.getenv("MISTRAL_TTS_MODEL", "voxtral-mini-tts-2603")
_TIMEOUT_S = float(os.getenv("CUE_TTS_TIMEOUT_S", "60"))


class VoxtralProvider(CueTtsProvider):
    name = "voxtral"

    async def synthesize(self, *, text: str, voice_id: str, locale: str) -> bytes:
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            # Voice-only key. Presence is required for the cloud default path;
            # Hector owns the Render backend env (the one external dependency).
            raise VoiceProviderError("unauthorized", "MISTRAL_API_KEY not set")

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.post(
                    _MISTRAL_SPEECH_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": _TTS_MODEL,
                        "input": text,
                        "voice_id": voice_id,
                        "response_format": "mp3",
                    },
                )
        except httpx.TimeoutException as exc:
            raise VoiceProviderError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError("unknown", f"voxtral request failed: {exc}") from exc

        if resp.status_code != 200:
            # Never log the audio body or the key; status + a trimmed body only.
            raise VoiceProviderError(
                "upstream", f"status={resp.status_code} body={resp.text[:200]!r}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise VoiceProviderError("upstream", "non-JSON upstream response") from exc

        audio_b64 = payload.get("audio_data")
        if not audio_b64:
            raise VoiceProviderError("upstream", "no audio_data in response")

        try:
            return base64.b64decode(audio_b64)
        except (ValueError, TypeError) as exc:
            raise VoiceProviderError("upstream", "audio_data not valid base64") from exc
