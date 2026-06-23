"""
services/cue/voice/whisper_client.py
------------------------------------
Speech-to-text for Cue voice (VOICE-08) — auto-detect EN/ES.

TWO backends behind ONE `transcribe()` contract, selected by env
`CUE_STT_BACKEND` (default "mistral"):

  - "mistral"  → DEFAULT, cloud, NO VPS. Voxtral cloud transcription via
                 POST https://api.mistral.ai/v1/audio/transcriptions. This is
                 the decided-direction working path (2026-06-23): cloud STT so
                 the voice round-trip needs no VPS. Mistral here is VOICE ONLY
                 (VOICE-03) — it never touches the reasoning brain.

  - "faster-whisper" → DORMANT, self-hosted, DEFERRED. The sovereign STT path
                 for a later upgrade on a DEDICATED box (never the Mailcow VPS).
                 Lazy-imported only when selected, so the (unpinned, not-yet-
                 installed) faster-whisper package being absent never breaks
                 import or the cloud path.

`language=None` → auto-detect EN/ES (VOICE-08). Returns
{"transcript": str, "language": str | None}. The transcript is TRANSIENT — the
route returns it to the client and never persists it backend (T-23-05-02, the
HANDS-02 no-body-persist rule).
"""

from __future__ import annotations

import os

import httpx

_MISTRAL_TRANSCRIBE_URL = "https://api.mistral.ai/v1/audio/transcriptions"
# Voxtral transcription model (verified docs.mistral.ai 2026-06-23). Env-override
# if Mistral revs the id.
_STT_MODEL = os.getenv("MISTRAL_STT_MODEL", "voxtral-mini-latest")
_TIMEOUT_S = float(os.getenv("CUE_STT_TIMEOUT_S", "60"))


class VoiceTranscribeError(Exception):
    """STT failure the route maps onto an HTTP status."""


async def transcribe(
    audio: bytes,
    *,
    language: str | None = None,
    content_type: str = "audio/webm",
) -> dict:
    """Transcribe `audio` bytes. `language=None` → auto-detect EN/ES (VOICE-08).

    Returns {"transcript": str, "language": str | None}.
    """
    # VOICE-08: normalize any unexpected locale hint to auto-detect. A non-EN/ES
    # hint must not pin the wrong language — fall back to language=None (detect).
    if language not in ("en", "es"):
        language = None

    backend = os.getenv("CUE_STT_BACKEND", "mistral")
    if backend == "faster-whisper":
        return await _transcribe_faster_whisper(audio, language=language)
    return await _transcribe_mistral(audio, language=language, content_type=content_type)


async def _transcribe_mistral(
    audio: bytes,
    *,
    language: str | None,
    content_type: str,
) -> dict:
    """Cloud STT via Voxtral (Mistral). DEFAULT path — no VPS required."""
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise VoiceTranscribeError("MISTRAL_API_KEY not set")

    # multipart/form-data: model + file (+ optional language). language omitted
    # entirely for auto-detection (VOICE-08).
    data = {"model": _STT_MODEL}
    if language in ("en", "es"):
        data["language"] = language
    files = {"file": ("audio", audio, content_type or "application/octet-stream")}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                _MISTRAL_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                data=data,
                files=files,
            )
    except httpx.HTTPError as exc:
        raise VoiceTranscribeError(f"mistral transcribe request failed: {exc}") from exc

    if resp.status_code != 200:
        raise VoiceTranscribeError(
            f"mistral transcribe status={resp.status_code} body={resp.text[:200]!r}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise VoiceTranscribeError("non-JSON transcribe response") from exc

    return {
        "transcript": (payload.get("text") or "").strip(),
        "language": payload.get("language"),
    }


async def _transcribe_faster_whisper(audio: bytes, *, language: str | None) -> dict:
    """DORMANT self-hosted STT (deferred sovereign path).

    Lazy-imports faster-whisper so the package's absence never breaks the
    default cloud path. Only runs when CUE_STT_BACKEND=faster-whisper AND the
    package is installed on a dedicated box (never the Mailcow VPS). `language`
    is forwarded as-is; None means faster-whisper auto-detects EN/ES.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore  # noqa: PLC0415
    except ImportError as exc:  # package not installed (dormant by design)
        raise VoiceTranscribeError(
            "faster-whisper backend selected but the package is not installed "
            "(self-hosted STT is deferred — see 23-VPS-VOICE-DEPLOY.md)"
        ) from exc

    import tempfile

    model_size = os.getenv("CUE_WHISPER_MODEL", "base")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    # faster-whisper accepts a file path or file-like; write the blob to a temp
    # file so any container (WAV/WebM/mp3) is decoded by its bundled ffmpeg.
    with tempfile.NamedTemporaryFile(suffix=".audio") as tmp:
        tmp.write(audio)
        tmp.flush()
        # language=None → auto-detect EN/ES (VOICE-08).
        segments, info = model.transcribe(tmp.name, language=language, beam_size=1)
        transcript = "".join(seg.text for seg in segments).strip()
    return {"transcript": transcript, "language": getattr(info, "language", None)}
