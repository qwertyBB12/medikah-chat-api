"""
tests/cue/test_voice_providers.py
----------------------------------
Plan 23-05 voice-stack guard suite (VOICE-01/03/04/06/08).

These are SYNCHRONOUS unit tests (no network, no asyncio) — they assert the
registry/catalog/STT CONTRACTS, not live provider I/O:

  - create_tts_provider() returns the right class per name; raises on unknown
    (VOICE-01 registry).
  - providers.py (and the voice package) never import the LLM reasoning adapter
    (VOICE-03 separation) — AST scan, not a substring grep.
  - catalog.resolve() returns a non-empty, namespace-valid {voice_id, provider}
    for EN and ES with ZERO db rows (VOICE-04 — never crashes /cue/tts).
  - whisper_client transcribe auto-detects via language=None (VOICE-08).
  - f5_provider uses the Gradio-6 basic_tts flow, NOT the dead /run/predict.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from services.cue.voice import (
    catalog,
    f5_provider,
    providers,
    voxtral_provider,
    whisper_client,
)
from services.cue.voice.f5_provider import F5TtsProvider
from services.cue.voice.providers import (
    CueTtsProvider,
    VoiceProviderError,
    create_tts_provider,
)
from services.cue.voice.voxtral_provider import VoxtralProvider


# ---------------------------------------------------------------------------
# VOICE-01 — registry / factory
# ---------------------------------------------------------------------------


def test_registry_returns_distinct_classes() -> None:
    vox = create_tts_provider("voxtral")
    f5 = create_tts_provider("f5")
    assert isinstance(vox, VoxtralProvider)
    assert isinstance(f5, F5TtsProvider)
    assert type(vox) is not type(f5)
    assert isinstance(vox, CueTtsProvider) and isinstance(f5, CueTtsProvider)


def test_registry_default_is_voxtral() -> None:
    # Decided direction: voxtral (cloud) is the working default; F5 is dormant.
    assert isinstance(create_tts_provider(), VoxtralProvider)


def test_unknown_provider_raises_value_error() -> None:
    with pytest.raises(ValueError):
        create_tts_provider("bogus")


def test_voice_provider_error_carries_kind() -> None:
    err = VoiceProviderError("unauthorized", "no key")
    assert err.kind == "unauthorized"


# ---------------------------------------------------------------------------
# VOICE-03 — the TTS registry never imports the LLM reasoning adapter
# ---------------------------------------------------------------------------


def _imports_adapter(module) -> bool:
    """True iff the module's source imports services.cue.adapter (AST scan)."""
    src = inspect.getsource(module)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "services.cue.adapter"
        ):
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("services.cue.adapter"):
                    return True
    return False


def test_providers_module_does_not_import_llm_adapter() -> None:
    assert not _imports_adapter(providers)


def test_voice_provider_files_do_not_import_llm_adapter() -> None:
    # Whole voice package stays off the reasoning seam (VOICE-03).
    for module in (providers, voxtral_provider, f5_provider, whisper_client, catalog):
        assert not _imports_adapter(module), f"{module.__name__} imports the LLM adapter"


def test_providers_source_has_zero_adapter_import_lines() -> None:
    # Mirrors the plan's acceptance grep: 0 `from services.cue.adapter` lines.
    src = Path(inspect.getfile(providers)).read_text(encoding="utf-8")
    assert "from services.cue.adapter" not in src


# ---------------------------------------------------------------------------
# VOICE-04 — catalog resolve is provider-aware and zero-DB safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locale", ["en", "es"])
def test_catalog_resolve_zero_db_rows_is_non_empty(locale: str) -> None:
    sel = catalog.resolve("phys-zero-rows", locale)  # supabase=None → no DB
    assert sel["voice_id"], "voice_id must be non-empty with zero DB rows"
    assert sel["provider"] in ("voxtral", "f5")


@pytest.mark.parametrize("locale", ["en", "es"])
def test_catalog_resolve_provider_is_namespace_valid(locale: str) -> None:
    # The returned provider must be a REGISTERED provider so the route can hand
    # the id to create_tts_provider(provider) in that provider's namespace.
    sel = catalog.resolve("phys-ns", locale)
    create_tts_provider(sel["provider"])  # raises if not a registered provider


def test_catalog_default_provider_is_voxtral() -> None:
    # F5 is dormant/not-deployed → the zero-DB default must be the cloud voxtral
    # path so /cue/tts works without an F5 server present.
    assert catalog.resolve("phys-x", "es")["provider"] == "voxtral"


def test_catalog_resolve_survives_broken_supabase() -> None:
    class _Boom:
        def table(self, *_a, **_k):
            raise RuntimeError("no such table: cue_voice_preferences")

    sel = catalog.resolve("phys-err", "en", supabase=_Boom())
    assert sel["voice_id"] and sel["provider"] == "voxtral"


# ---------------------------------------------------------------------------
# VOICE-08 — STT auto-detects EN/ES (language=None)
# ---------------------------------------------------------------------------


def test_whisper_client_uses_language_none_autodetect() -> None:
    src = inspect.getsource(whisper_client)
    assert "language=None" in src or "language = None" in src


# ---------------------------------------------------------------------------
# F5 dormant port — Gradio-6 flow, NOT the dead /run/predict
# ---------------------------------------------------------------------------


def test_f5_uses_gradio6_flow_not_run_predict() -> None:
    src = inspect.getsource(f5_provider)
    assert "call/basic_tts" in src
    assert "/run/predict" not in src
