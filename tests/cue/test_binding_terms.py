"""Binding-terms compliance knowledge in Cue's system prompt (terms project).

Before this, self_knowledge carried only COFEPRIS/NOM-024 scope-of-practice and
NO HIPAA / LFPDPPP / cross-border framing — so a doctor's terms/privacy question
hit a knowledge void and Cue deflected. The `binding_terms` addendum injects the
faithful legal frame (counsel docs, Luis Ignacio 2026-04-16) so Cue cites it
instead of dodging. It is STATIC and covers both jurisdictions, so it stays in
the byte-stable cached system prefix (no per-doctor cache fragmentation).
"""
from services.cue.personality.addendums import AssembleContext, binding_terms
from services.cue.personality.assemble import assemble


def test_en_workspace_carries_us_and_mx_framework() -> None:
    out = assemble(locale="en", surface="workspace")
    assert "BINDING TERMS & COMPLIANCE FRAMEWORK" in out
    # US framework
    assert "Business Associate" in out
    assert "Informational Appointment" in out
    assert "Ryan Haight" in out
    assert "MEDIKAH-CB-001" in out
    assert "Delaware" in out
    # MX framework
    assert "LFPDPPP" in out
    assert "cédula profesional" in out
    assert "COFEPRIS" in out
    # privacy + pointer to binding docs
    assert "Privacy Notice" in out
    assert "privacy@medikah.health" in out


def test_es_workspace_carries_framework_in_spanish() -> None:
    out = assemble(locale="es", surface="workspace")
    assert "TÉRMINOS VINCULANTES Y MARCO DE CUMPLIMIENTO" in out
    assert "Business Associate" in out
    assert "Cita Informativa" in out
    assert "responsable" in out
    assert "cédula profesional" in out
    assert "Aviso de Privacidad" in out
    assert "MEDIKAH-CB-001" in out


def test_guardrail_not_a_lawyer_present_both_locales() -> None:
    # Cue explains the frame but defers specific legal questions — mirrors the
    # clinical-deference pattern, does NOT deflect the whole topic.
    en = assemble(locale="en", surface="workspace")
    es = assemble(locale="es", surface="workspace")
    assert "not the doctor's lawyer" in en
    assert "legal@medikah.health" in en
    assert "No eres la abogada del médico" in es
    assert "legal@medikah.health" in es


def test_binding_terms_gated_to_workspace_surface() -> None:
    # Doctor workspace gets it; dev / claude-code sessions do not.
    assert binding_terms(AssembleContext(locale="en", surface="workspace")) is not None
    assert binding_terms(AssembleContext(locale="en", surface="claude-code")) is None


def test_binding_terms_independent_of_mode_for_caching() -> None:
    # Identical in text and voice so the cached system prefix stays byte-stable.
    text = binding_terms(AssembleContext(locale="es", surface="workspace", mode="text"))
    voice = binding_terms(AssembleContext(locale="es", surface="workspace", mode="voice"))
    assert text == voice
