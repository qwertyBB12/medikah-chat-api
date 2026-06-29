"""Voice-mode prompt threading on the live path (Fix #2, diagnosis 2026-06-28).

The brevity + no-markdown voice directives live in the voice_mode addendum, which
gates on mode == 'voice'. The live path assembled mode='text' unconditionally
(the client never sent a mode, CueChatRequest had no field to receive one), so
spoken Cue inherited the verbose text persona and read long, register-less
answers aloud. These tests lock the end-to-end threading: _build_system_prompt
must surface the VOICE MODE DIRECTIVES header on voice turns and omit it on text
turns, and CueChatRequest must carry a mode field (default text, backwards-safe).
"""
import asyncio

from routes.cue_routes import CueChatRequest, _build_system_prompt


def _build(locale: str, mode: str) -> str:
    return asyncio.run(_build_system_prompt("phys-test", locale, None, mode=mode))


def test_voice_turn_loads_brevity_directives_es() -> None:
    prompt = _build("es", "voice")
    assert "DIRECTIVAS DE MODO DE VOZ" in prompt
    assert "Dos o tres oraciones" in prompt  # the 2-3 sentence brevity cap
    assert "Sin markdown" in prompt


def test_voice_turn_loads_brevity_directives_en() -> None:
    prompt = _build("en", "voice")
    assert "VOICE MODE DIRECTIVES" in prompt
    assert "Two to three sentences" in prompt
    assert "No markdown." in prompt


def test_text_turn_has_no_voice_directives() -> None:
    prompt = _build("es", "text")
    assert "DIRECTIVAS DE MODO DE VOZ" not in prompt
    assert "VOICE MODE DIRECTIVES" not in prompt


def test_chat_request_carries_mode_defaulting_text() -> None:
    # Backwards-safe: an old client that omits mode keeps the text behavior.
    assert CueChatRequest(messages=[]).mode == "text"
    assert CueChatRequest(messages=[], mode="voice").mode == "voice"
