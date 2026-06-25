"""
services/cue/voice/catalog.py
-----------------------------
Voice-catalog resolver (VOICE-04) — turns (physician_id, locale) into a
provider-aware voice selection.

Python port of BeNeXT `lib/cue-api/voice-catalog.ts`, with the BeNeXT
alumni / `is_ecosystem_admin` dev branch DROPPED (VOICE-04).

resolve(physician_id, locale) -> {"voice_id": str, "provider": str}

Fallback chain (first hit wins):
  1. per-physician preference   — OPTIONAL `cue_voice_preferences` row (override)
  2. catalog default for locale — OPTIONAL `cue_voice_catalog` row (override)
  3. env override               — MISTRAL_CUE_VOICE_ID[_ES]
  4. in-code default map        — MANDATORY; works with ZERO db rows

CRITICAL (never crash /cue/tts): the final fallback `DEFAULT_VOICES` ALWAYS
returns a non-empty {voice_id, provider}. The provider travels WITH the id so
the route hands the id to create_tts_provider(provider) and the id is valid IN
THAT PROVIDER'S NAMESPACE (F5 Gradio ref-audio paths vs Voxtral Mistral voice
ids are different namespaces — never conflate them).

DECIDED DIRECTION (2026-06-23): the working default provider is `voxtral`
(cloud), because F5 is dormant/not-deployed. So DEFAULT_VOICES uses
provider="voxtral". When self-hosted F5 is later flipped on, change this map (or
seed catalog rows) — a one-line edit, no route change (VOICE-01).

There is NO migration for a voice table in this slice. The DB override layers
(1/2) are best-effort: any missing table / absent row / driver error is caught
and falls through to the env + in-code defaults. resolve() NEVER depends on a
table existing.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("cue.voice.catalog")

# In-code PROVIDER-AWARE default voice map (the mandatory final fallback).
# Voxtral voice ids are env-overridable so deploy config CAN pin the voice
# (MISTRAL_CUE_VOICE_ID alongside MISTRAL_API_KEY in Render). The in-code default
# is now Cue's REAL Voxtral voice (Hector-supplied 2026-06-24) instead of the old
# "Cue-EN"/"Cue-ES" placeholders, so the voice works with zero env config. One id
# covers EN + ES (bilingual voice).
# NOTE: if the Render env var MISTRAL_CUE_VOICE_ID is still set to a PREVIOUS
# voice, IT WINS over this default — update or clear it to use this one.
_CUE_VOICE_ID = "2d06246f-1c89-4d2f-9c2f-18e307dc3367"
_DEFAULT_VOICE_EN = os.getenv("MISTRAL_CUE_VOICE_ID", _CUE_VOICE_ID)
_DEFAULT_VOICE_ES = os.getenv("MISTRAL_CUE_VOICE_ID_ES", os.getenv("MISTRAL_CUE_VOICE_ID", _CUE_VOICE_ID))

DEFAULT_VOICES: dict[str, dict[str, str]] = {
    "en": {"voice_id": _DEFAULT_VOICE_EN, "provider": "voxtral"},
    "es": {"voice_id": _DEFAULT_VOICE_ES, "provider": "voxtral"},
}


def _in_code_default(locale: str) -> dict[str, str]:
    """The mandatory final fallback — always non-empty {voice_id, provider}."""
    return DEFAULT_VOICES.get(locale, DEFAULT_VOICES["es"])  # physicians Spanish-first


def resolve(physician_id: str, locale: str, supabase=None) -> dict[str, str]:
    """Resolve a {"voice_id", "provider"} for the physician + locale.

    Always returns a non-empty mapping (never raises on zero DB rows). `supabase`
    is OPTIONAL; when absent, only the env + in-code defaults are consulted.
    """
    loc = "en" if locale == "en" else "es"

    # Steps 1–2: OPTIONAL DB overrides. Best-effort; any error → fall through.
    if supabase is not None:
        override = _db_override(physician_id, loc, supabase)
        if override is not None:
            return override

    # Step 3: env override carrying its own provider (defaults to voxtral — the
    # working cloud default). Only used if an env voice id is explicitly set.
    env_voice = os.getenv("MISTRAL_CUE_VOICE_ID_ES" if loc == "es" else "MISTRAL_CUE_VOICE_ID")
    if env_voice:
        return {"voice_id": env_voice, "provider": os.getenv("CUE_DEFAULT_TTS_PROVIDER", "voxtral")}

    # Step 4: in-code default map (mandatory; zero-DB safe).
    return _in_code_default(loc)


def _db_override(physician_id: str, locale: str, supabase) -> dict[str, str] | None:
    """Best-effort per-physician voice override.

    Reads an OPTIONAL `cue_voice_preferences` row (columns: physician_id,
    voice_id, provider). The table may not exist (no migration in this slice) —
    any error is swallowed and the caller falls through to env/in-code defaults.
    An override row carries its OWN provider (namespace travels with the id).
    """
    try:
        res = (
            supabase.table("cue_voice_preferences")
            .select("voice_id, provider")
            .eq("physician_id", physician_id)
            .eq("locale", locale)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if rows:
            row = rows[0]
            voice_id = row.get("voice_id")
            provider = row.get("provider")
            if voice_id and provider:
                return {"voice_id": voice_id, "provider": provider}
    except Exception:  # noqa: BLE001 — table absent / driver error → fall through
        logger.debug(
            "[cue.voice.catalog] no cue_voice_preferences override for physician "
            "(table may be absent — using env/in-code default)"
        )
    return None
