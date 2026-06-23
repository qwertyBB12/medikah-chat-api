"""
services/cue/voice/f5_provider.py
---------------------------------
F5-TTS self-hosted (Gradio) provider — DORMANT (decided 2026-06-23).

This is the sovereign-default TTS provider, ported to the VERIFIED Gradio-6
contract so it can be flipped on later. It is NOT deployed in this slice:
self-hosted F5 (+ Whisper) move to a DEDICATED box in a later sovereignty
upgrade — never the Mailcow VPS. Until then `voxtral` is the working default
and this class is registered but unused at runtime. Constructing it does NO
network I/O, so it is safe to import/register while dormant.

Python port of BeNeXT `lib/cue-api/providers/f5.ts`.

EMPIRICAL CONTRACT (BeNeXT Phase 66 probe, 2026-05-27, Gradio 6.10 + f5-tts
1.1.20) — recorded in 23-VPS-VOICE-DEPLOY.md:
  - API prefix is `/gradio_api/call/<name>` (Gradio 6 namespaced — NOT `/call/`).
  - The reference audio must be pre-uploaded via POST /gradio_api/upload
    (multipart) BEFORE being referenced in data[0]; `voice_id` IS the Gradio
    upload path. Direct server-local paths are rejected (InvalidPathError).
  - `basic_tts` requires NINE inputs (the info endpoint reports 6 — three hidden
    sliders cross_fade_duration / nfe_step / speed are omitted from the schema
    but still required). The dead legacy predict shape is NOT used — only the
    Gradio-6 `call/basic_tts` event-stream flow below.
  - SSE emits `event: heartbeat` periodically before `event: complete`.
  - File retrieval is `/gradio_api/file=<path>` (Gradio 6 namespaced).

3 round-trips (caller pre-uploads the reference audio):
  0. (deploy harness) POST /gradio_api/upload (multipart) → ["<uploaded-path>"]
  1. POST /gradio_api/call/basic_tts (json, 9 inputs) → {event_id}
  2. GET  /gradio_api/call/basic_tts/<event_id> (SSE) → `complete` carries path
  3. GET  /gradio_api/file=<path> → raw WAV bytes

VOICE-03: voice-only. Never imports the LLM adapter.
"""

from __future__ import annotations

import json
import os

import httpx

from services.cue.voice.providers import CueTtsProvider, VoiceProviderError

_DEFAULT_BASE_URL = "http://127.0.0.1:7860"  # bind 127.0.0.1 only (never 0.0.0.0)
_API_PREFIX = "/gradio_api"
_ENDPOINT_PATH = f"{_API_PREFIX}/call/basic_tts"
_TIMEOUT_S = float(os.getenv("F5_TTS_TIMEOUT_S", "120"))  # CPU-mode F5 can take minutes

# basic_tts default param values (Gradio UI defaults at f5-tts 1.1.20).
_DEFAULT_REMOVE_SILENCE = True
_DEFAULT_RANDOMIZE_SEED = False
_DEFAULT_SEED = 0
_DEFAULT_CROSS_FADE_DURATION = 0.15
_DEFAULT_NFE_STEP = 32
_DEFAULT_SPEED = 1.0


def _parse_sse_complete(sse_text: str) -> str | None:
    """Return the output file path from the SSE `complete` event, or None.

    `basic_tts` returns an array on complete:
    [Synthesized Audio FileData, Spectrogram FileData, Reference Text, Seed].
    Heartbeat events are filtered out (we read only `event: complete`).
    """
    for block in sse_text.split("\n\n"):
        lines = block.split("\n")
        event = next(
            (l[len("event:"):].strip() for l in lines if l.startswith("event:")),
            None,
        )
        if event != "complete":
            continue
        data_line = next(
            (l[len("data:"):].strip() for l in lines if l.startswith("data:")),
            None,
        )
        if not data_line:
            continue
        try:
            parsed = json.loads(data_line)
        except ValueError:
            return None
        first = parsed[0] if isinstance(parsed, list) and parsed else parsed
        if isinstance(first, dict) and isinstance(first.get("path"), str):
            return first["path"]
        return None
    return None


class F5TtsProvider(CueTtsProvider):
    name = "f5"

    async def synthesize(self, *, text: str, voice_id: str, locale: str) -> bytes:
        base_url = os.getenv("F5_TTS_URL", _DEFAULT_BASE_URL)
        # `voice_id` MUST be a Gradio-cached upload path (from a prior
        # POST /gradio_api/upload). The deploy harness pre-uploads ref audio and
        # stores the path as the catalog voice_id when F5 is flipped on.
        uploaded_path = voice_id

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                # Step 1: submit the synthesis job — 9 inputs via the Gradio-6
                # call/basic_tts flow (NOT the dead legacy predict endpoint).
                # data[0]=ref_audio, [1]=ref_text (empty = Whisper auto),
                # [2]=gen_text, [3]=remove_silence, [4]=randomize_seed, [5]=seed,
                # [6]=cross_fade_duration, [7]=nfe_step, [8]=speed.
                submit = await client.post(
                    f"{base_url}{_ENDPOINT_PATH}",
                    json={
                        "data": [
                            {"path": uploaded_path, "meta": {"_type": "gradio.FileData"}},
                            "",
                            text,
                            _DEFAULT_REMOVE_SILENCE,
                            _DEFAULT_RANDOMIZE_SEED,
                            _DEFAULT_SEED,
                            _DEFAULT_CROSS_FADE_DURATION,
                            _DEFAULT_NFE_STEP,
                            _DEFAULT_SPEED,
                        ]
                    },
                )
                if submit.status_code != 200:
                    body = submit.text
                    if _looks_like_missing_voice(body):
                        raise VoiceProviderError("invalid_voice", voice_id)
                    raise VoiceProviderError("upstream", f"submit status={submit.status_code}")
                event_id = (submit.json() or {}).get("event_id")
                if not event_id:
                    raise VoiceProviderError("upstream", "no event_id in submit response")

                # Step 2: poll the SSE stream for `complete` (heartbeats interleave).
                poll = await client.get(f"{base_url}{_ENDPOINT_PATH}/{event_id}")
                if poll.status_code != 200:
                    raise VoiceProviderError("upstream", f"poll status={poll.status_code}")
                out_path = _parse_sse_complete(poll.text)
                if not out_path:
                    raise VoiceProviderError("upstream", "no complete event with file path")

                # Step 3: fetch the raw WAV bytes.
                file_res = await client.get(f"{base_url}{_API_PREFIX}/file={out_path}")
                if file_res.status_code != 200:
                    raise VoiceProviderError("upstream", f"file status={file_res.status_code}")
                return file_res.content
        except httpx.TimeoutException as exc:
            raise VoiceProviderError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError(
                "unknown", f"f5 local server unreachable: {exc}"
            ) from exc


def _looks_like_missing_voice(body: str) -> bool:
    lower = body.lower()
    return (
        ("reference audio" in lower and ("not found" in lower or "missing" in lower))
        or ("ref_audio" in lower and "not found" in lower)
        or "voice not found" in lower
        # Gradio 6 rejects un-uploaded server-local paths with this phrasing.
        or ("cannot move" in lower and "not uploaded" in lower)
    )
