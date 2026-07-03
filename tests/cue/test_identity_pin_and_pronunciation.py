"""
Cue QA fixes from Hector's live voice session (2026-07-02 → 03 UTC):

1. Identity pin — the doctor's address (honorific + surname) rides EVERY turn's
   system prompt, with an explicit never-guess directive when no honorific is
   on file (the "doctora" incident: workspace title said Dr, physicians.title
   was NULL, and mid-conversation Spanish turns drifted to a guessed feminine
   honorific).
2. Address fallback — physicians.title backs up the workspace title.
3. Pronunciation — "Cue" is a name; the Spanish TTS path swaps the grapheme so
   it sounds like the English letter Q. Audio only, transcript untouched.
4. Memory hygiene — the judge refuses demo/hypothetical cases; the recall
   envelope tells the model notes are hints, droppable on non-recognition.
"""
from routes.cue_routes import _build_identity_directive, _resolve_doctor_address
from services.cue.memory.judge import build_judge_prompt
from services.cue.memory.recall import assemble_recall_envelope
from services.cue.voice.providers import normalize_for_tts

from tests.cue.test_opening_greeting import _FakeDB


# ---------------------------------------------------------------------------
# 1. Identity directive
# ---------------------------------------------------------------------------

def test_identity_directive_es_pins_dra():
    d = _build_identity_directive("es", "Doctora Aguirre")
    assert "«Doctora Aguirre»" in d
    assert "femenino" in d
    assert "NUNCA cambies el honorífico" in d


def test_identity_directive_es_pins_dr():
    d = _build_identity_directive("es", "Doctor Lopez")
    assert "«Doctor Lopez»" in d
    assert "masculino" in d


def test_identity_directive_es_name_only_forbids_guessing():
    d = _build_identity_directive("es", "Aguirre")
    assert "Aguirre" in d
    assert "adivinar el género está prohibido" in d.lower() or "adivinar" in d.lower()
    assert "«Doctor»" in d and "«Doctora»" in d  # explicitly named as forbidden


def test_identity_directive_es_empty_address_is_neutral():
    d = _build_identity_directive("es", "")
    assert "neutra" in d


def test_identity_directive_en_variants():
    dra = _build_identity_directive("en", "Doctora Aguirre")
    assert "she/her" in dra and "Doctora Aguirre" in dra
    dr = _build_identity_directive("en", "Doctor Lopez")
    assert "he/him" in dr
    name_only = _build_identity_directive("en", "Lopez")
    assert "guessing" in name_only.lower()


# ---------------------------------------------------------------------------
# 2. Address fallback to physicians.title
# ---------------------------------------------------------------------------

def test_address_falls_back_to_physicians_title():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": None}],
        "physicians": [{"full_name": "Erika Aguirre", "title": "Dra"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Doctora Aguirre"


def test_address_workspace_title_wins_over_physicians_title():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": "Dr"}],
        "physicians": [{"full_name": "Sam Rios", "title": "Dra"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Doctor Rios"


# ---------------------------------------------------------------------------
# 3. TTS pronunciation normalization
# ---------------------------------------------------------------------------

def test_tts_es_swaps_cue_to_kiu_case_preserving():
    assert normalize_for_tts("Cue está listo. Pregúntale a cue.", "es") == (
        "Kiú está listo. Pregúntale a kiú."
    )


def test_tts_es_does_not_touch_words_containing_cue():
    # No substring rewrites — only the standalone name.
    assert normalize_for_tts("El documento y su cuestionario", "es") == (
        "El documento y su cuestionario"
    )


def test_tts_en_passthrough():
    assert normalize_for_tts("Cue is ready.", "en") == "Cue is ready."


# ---------------------------------------------------------------------------
# 4. Memory hygiene
# ---------------------------------------------------------------------------

def test_judge_prompt_refuses_demo_and_hypothetical_cases():
    p = build_judge_prompt("Doctor Lopez")
    assert "hypothetical" in p
    assert "role-played" in p
    assert "kept=false" in p


def test_recall_envelope_carries_hints_not_facts_caveat():
    notes = [{"note": "x", "appended_at": "2026-07-01T00:00:00Z", "category": "follow_up"}]
    es = assemble_recall_envelope(notes, "es")
    assert "pistas, no como hechos" in es
    en = assemble_recall_envelope(notes, "en")
    assert "hints, not facts" in en
