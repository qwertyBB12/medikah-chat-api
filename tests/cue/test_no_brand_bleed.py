"""
tests/cue/test_no_brand_bleed.py
----------------------------------
MERGE-BLOCKING: Brand-bleed gate over assemble() output (D10).

PURPOSE
-------
The BeNeXT personality core being ported contained brand-specific framing
("project author", "ecosystem vessels", "Author × AI", "Arkah", "Futuro",
"NeXT", "BeNeXT"). If any of this survives the re-author, a physician reads
language that is contextually bizarre and undermines clinical trust.

More critically: the BeNeXT persona does NOT include a clinical-deference anchor.
A brand bleed is therefore not just a cosmetic issue — it REMOVES the safety
constraint entirely (AI-SPEC §1b FM4 / T-22-03-02).

This test is CI merge-blocking — a failing brand-bleed test must block merge.

WHAT IS TESTED
--------------
D10-A  test_no_brand_tokens_in_assembled_en
       assemble(locale='en', ...) output across all surface hints must contain
       ZERO tokens from the D10 forbidden list.

D10-B  test_no_brand_tokens_in_assembled_es
       assemble(locale='es', ...) output across all surface hints must contain
       ZERO tokens from the D10 forbidden list.

D10-C  test_no_brand_tokens_in_all_surfaces_and_modes
       Parametrized sweep: every (locale, surface, mode, voice_gender) combination
       in the test matrix must produce zero forbidden tokens.

D10-D  test_no_brand_tokens_in_core_files_directly
       Direct scan of core/en.md and core/es.md — checks the source before assembly.
       Catches a bleed in the raw markdown before it propagates to the assembled prompt.

D10-E  test_no_brand_tokens_in_self_knowledge
       Direct scan of self_knowledge.py output — the rebuilt block is the most
       likely site of a vessel-naming bleed (it replaced engine.ts lines 314-339
       which listed Arkah, Futuro, NeXT, BeNeXT explicitly).

FORBIDDEN TOKENS (D10 list — from 22-03 PLAN.md and AI-SPEC §1b FM4)
----------------------------------------------------------------------
Case-insensitive matching. Exact string presence. No word-boundary requirement
(erring on the side of caution — "NeXT" as a word fragment would still be caught).

    "project author"    — BeNeXT role framing
    "ecosystem vessels" — BeNeXT vessel concept
    "Author × AI"       — BeNeXT product name
    "Author x AI"       — ASCII variant of the above
    "Arkah"             — BeNeXT ecosystem vessel
    "Futuro"            — BeNeXT ecosystem vessel
    "NeXT"              — BeNeXT ecosystem vessel (note: "next" as a common word
                          is NOT in the list — only the capitalized brand form)
    "BeNeXT"            — BeNeXT brand name

SCAN NOTE
---------
Most tokens are checked case-insensitively (FORBIDDEN_TOKENS list).
"NeXT" is checked case-SENSITIVELY (FORBIDDEN_TOKENS_CASE_SENSITIVE list) because
"next" is a common English/Spanish word that would produce false positives in clinical
prose (e.g., "redirect toward the next actionable step"). The brand form "NeXT"
(mixed-case) is the target; lowercase "next" is not a brand bleed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.cue.personality.assemble import assemble
from services.cue.personality.self_knowledge import build_self_knowledge


# ---------------------------------------------------------------------------
# D10 forbidden token list (exact from PLAN.md / AI-SPEC §1b FM4)
# ---------------------------------------------------------------------------

FORBIDDEN_TOKENS: list[str] = [
    "project author",
    "ecosystem vessels",
    "Author × AI",
    "Author x AI",
    "Arkah",
    "Futuro",
    "BeNeXT",
]

# Tokens that must be matched case-SENSITIVELY.
# "NeXT" is a brand token but "next" is a common English/Spanish word —
# case-insensitive matching would cause false positives in clinical prose.
# We match the exact brand form (mixed case) to avoid false alarms.
FORBIDDEN_TOKENS_CASE_SENSITIVE: list[str] = [
    "NeXT",
]


# ---------------------------------------------------------------------------
# Paths for direct file scan
# ---------------------------------------------------------------------------

_CORE_DIR = Path(__file__).parent.parent.parent / "services" / "cue" / "personality" / "core"
_EN_CORE = _CORE_DIR / "en.md"
_ES_CORE = _CORE_DIR / "es.md"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def find_forbidden_tokens(text: str) -> list[str]:
    """
    Return a list of forbidden tokens found in `text`.

    FORBIDDEN_TOKENS are checked case-insensitively (e.g., "Arkah", "Futuro",
    "BeNeXT" in any capitalization).

    FORBIDDEN_TOKENS_CASE_SENSITIVE are checked case-sensitively (e.g., "NeXT"
    as a brand token — "next" as a common English/Spanish word must not trigger
    false positives in clinical prose like "redirect toward the next step").

    Returns a list of matching tokens (empty if none found).
    """
    hits: list[str] = []
    text_lower = text.lower()
    for token in FORBIDDEN_TOKENS:
        if token.lower() in text_lower:
            hits.append(token)
    for token in FORBIDDEN_TOKENS_CASE_SENSITIVE:
        if token in text:  # case-sensitive: exact form must match
            hits.append(token)
    return hits


# ---------------------------------------------------------------------------
# Surface and mode combinations for sweep
# ---------------------------------------------------------------------------

_TEST_MATRIX = [
    # (locale, surface, mode, voice_gender)
    ("en", "workspace", "text", None),
    ("en", "workspace", "voice", None),
    ("en", "workspace", "voice", "male"),
    ("en", "workspace", "voice", "female"),
    ("en", "claude-code", "text", None),
    ("es", "workspace", "text", None),
    ("es", "workspace", "voice", None),
    ("es", "workspace", "voice", "male"),
    ("es", "workspace", "voice", "female"),
    ("es", "claude-code", "text", None),
]


# ---------------------------------------------------------------------------
# D10-A: No brand tokens in EN assembled output
# ---------------------------------------------------------------------------


def test_no_brand_tokens_in_assembled_en() -> None:
    """
    D10-A: assemble(locale='en') must contain zero D10 forbidden tokens.

    Tests the workspace surface (primary Medikah surface for physicians).
    """
    output = assemble(locale="en", surface="workspace")
    hits = find_forbidden_tokens(output)
    assert not hits, (
        f"Brand bleed detected in assemble(locale='en', surface='workspace').\n"
        f"Forbidden tokens found: {hits}\n"
        f"Locate and remove these tokens from core/en.md, self_knowledge.py, "
        f"or addendums.py (D10 / T-22-03-02)."
    )


# ---------------------------------------------------------------------------
# D10-B: No brand tokens in ES assembled output
# ---------------------------------------------------------------------------


def test_no_brand_tokens_in_assembled_es() -> None:
    """
    D10-B: assemble(locale='es') must contain zero D10 forbidden tokens.

    Tests the workspace surface (primary Medikah surface for physicians).
    """
    output = assemble(locale="es", surface="workspace")
    hits = find_forbidden_tokens(output)
    assert not hits, (
        f"Brand bleed detected in assemble(locale='es', surface='workspace').\n"
        f"Forbidden tokens found: {hits}\n"
        f"Locate and remove these tokens from core/es.md, self_knowledge.py, "
        f"or addendums.py (D10 / T-22-03-02)."
    )


# ---------------------------------------------------------------------------
# D10-C: Parametrized sweep across all surfaces/modes/locales
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locale,surface,mode,voice_gender",
    _TEST_MATRIX,
    ids=[f"{r[0]}-{r[1]}-{r[2]}-voice{r[3] or 'none'}" for r in _TEST_MATRIX],
)
def test_no_brand_tokens_in_all_surfaces_and_modes(
    locale: str,
    surface: str,
    mode: str,
    voice_gender: str | None,
) -> None:
    """
    D10-C: No forbidden token must appear in any assembled prompt across the
    full (locale, surface, mode, voice_gender) test matrix.

    Covers every addendum combination — voice_register addendum in particular
    must not introduce brand language for any gendered variant.
    """
    output = assemble(
        locale=locale,
        surface=surface,
        mode=mode,
        voice_gender=voice_gender,
    )
    hits = find_forbidden_tokens(output)
    assert not hits, (
        f"Brand bleed in assemble("
        f"locale={locale!r}, surface={surface!r}, mode={mode!r}, "
        f"voice_gender={voice_gender!r}).\n"
        f"Forbidden tokens found: {hits}\n"
        f"Locate and remove from core/.md files, self_knowledge.py, or addendums.py (D10)."
    )


# ---------------------------------------------------------------------------
# D10-D: Direct scan of core files (source-level check)
# ---------------------------------------------------------------------------


def test_no_brand_tokens_in_core_files_directly() -> None:
    """
    D10-D: core/en.md and core/es.md must contain zero D10 forbidden tokens.

    Scans the raw markdown source before assembly. A bleed here propagates to
    every assembled prompt variant — catching it at source is more informative.
    """
    en_text = _EN_CORE.read_text(encoding="utf-8")
    es_text = _ES_CORE.read_text(encoding="utf-8")

    en_hits = find_forbidden_tokens(en_text)
    es_hits = find_forbidden_tokens(es_text)

    assert not en_hits, (
        f"Brand bleed in core/en.md source.\n"
        f"Forbidden tokens found: {en_hits}\n"
        f"These are BeNeXT-specific terms that must not appear in the clinical core (D10)."
    )
    assert not es_hits, (
        f"Brand bleed in core/es.md source.\n"
        f"Forbidden tokens found: {es_hits}\n"
        f"These are BeNeXT-specific terms that must not appear in the clinical core (D10)."
    )


# ---------------------------------------------------------------------------
# D10-E: Direct scan of self_knowledge output
# ---------------------------------------------------------------------------


def test_no_brand_tokens_in_self_knowledge() -> None:
    """
    D10-E: build_self_knowledge() output for both locales must contain zero
    D10 forbidden tokens.

    The self_knowledge block is the rebuilt replacement for BeNeXT engine.ts
    lines 314-339 (which explicitly named Arkah, BeNeXT Global, Futuro, NeXT,
    Medikah, Mítikah Co.). This test ensures the rebuild contains zero vessel names.
    """
    sk_en = build_self_knowledge("en")
    sk_es = build_self_knowledge("es")

    en_hits = find_forbidden_tokens(sk_en)
    es_hits = find_forbidden_tokens(sk_es)

    assert not en_hits, (
        f"Brand bleed in build_self_knowledge('en') output.\n"
        f"Forbidden tokens found: {en_hits}\n"
        f"Remove BeNeXT ecosystem vessel names from self_knowledge.py (PERS-06 / D10)."
    )
    assert not es_hits, (
        f"Brand bleed in build_self_knowledge('es') output.\n"
        f"Forbidden tokens found: {es_hits}\n"
        f"Remove BeNeXT ecosystem vessel names from self_knowledge.py (PERS-06 / D10)."
    )
