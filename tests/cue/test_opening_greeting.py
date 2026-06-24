"""Opening-turn greeting branch — Phase 23 voice slice.

The greeting is a brain turn (compass-aware, clinical register), addressing the
doctor by honorific from physician_workspace_accounts.title with a name-only
fallback when title IS NULL.
"""
import asyncio

from routes.cue_routes import _resolve_doctor_address, _build_system_prompt


# ---------------------------------------------------------------------------
# Regression: _build_system_prompt must use the REAL assembled clinical prompt,
# NOT the emergency fallback. The route used to call `await assemble(...,
# physician_id=..., supabase=...)` — but assemble() is SYNC and takes neither
# arg, so it raised TypeError on EVERY turn and Cue ran on the generic English
# fallback (English-only, said "How can I help?" which the core forbids).
# ---------------------------------------------------------------------------


def _build(locale: str) -> str:
    return asyncio.run(_build_system_prompt("phys-test", locale, None))


def test_system_prompt_is_real_not_fallback_es():
    prompt = _build("es")
    # The fallback is a ~4-line string; the real assembled prompt is ~20k chars.
    assert len(prompt) > 2000, "ES prompt fell back to the tiny emergency prompt"
    assert "Eres Cue, un asistente de apoyo clínico para el médico autenticado." not in prompt


def test_system_prompt_is_real_not_fallback_en():
    prompt = _build("en")
    assert len(prompt) > 2000, "EN prompt fell back to the tiny emergency prompt"
    assert "You are Cue, a clinical decision-support assistant for the authenticated physician." not in prompt


def test_system_prompt_language_directive_is_bilingual():
    # Cue mirrors the doctor's language (EN+ES), not locked to one locale.
    prompt = _build("es")
    assert "SAME language the doctor uses" in prompt
    assert "Respond ONLY IN SPANISH" not in prompt  # the old locale-lock is gone


class _Resp:
    def __init__(self, data): self.data = data


class _Table:
    def __init__(self, store, name): self._store, self._name = store, name
    def select(self, *_): return self
    def eq(self, *_): return self
    def limit(self, *_): return self
    def execute(self): return _Resp(self._store.get(self._name, []))


class _FakeDB:
    def __init__(self, store): self._store = store
    def table(self, name): return _Table(self._store, name)


def test_address_uses_dra_honorific():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": "Dra"}],
        "physicians": [{"full_name": "Erika Aguirre"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Doctora Aguirre"


def test_address_uses_dr_honorific():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": "Dr"}],
        "physicians": [{"full_name": "Juan Perez Lopez"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Doctor Lopez"


def test_address_name_only_fallback_when_title_null():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": None}],
        "physicians": [{"full_name": "Erika Aguirre"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Aguirre"


def test_address_empty_when_no_records():
    db = _FakeDB({})
    assert _resolve_doctor_address(db, "p1") == ""
