"""
tests/cue/test_anchor_parity.py
---------------------------------
MERGE-BLOCKING: EN/ES anchor-parity gate (I18N-05 / D9).

Python port / lift of BeNeXT `cue-personality/parity.test.ts`.

PURPOSE
-------
The clinical-deference anchor (PERS-04) and every other anchor registered in
`anchors.py` with `languages: ("en", "es")` MUST appear in both `core/en.md`
AND `core/es.md` as an HTML-comment tag:

    <!-- anchor: anchor-id-here -->

A safety anchor that exists in only one locale silently disables the
clinical-deference constraint for the other locale (AI-SPEC §1b FM4 / T-22-03-01).

This test is CI merge-blocking — a failing parity test must block merge.

WHAT IS TESTED
--------------
D9-A  extract_anchors_from_file
      Utility: parse `<!-- anchor: ... -->` tags from a .md file.
      Returns a set of anchor IDs found in that file.

D9-B  test_en_anchors_match_registry
      core/en.md must contain EVERY anchor ID registered for "en" in anchors.py.
      No extra unregistered anchors (would indicate a forgotten registry entry).

D9-C  test_es_anchors_match_registry
      core/es.md must contain EVERY anchor ID registered for "es" in anchors.py.

D9-D  test_en_es_bilingual_anchors_match_each_other
      For anchors registered with BOTH ("en", "es") in anchors.py,
      the sets extracted from en.md and es.md must be IDENTICAL.

D9-E  test_clinical_deference_anchor_in_both_locales
      Hard assertion: the "clinical-deference" anchor key MUST be present in
      both en.md and es.md. This is the COFEPRIS-avoidance control.
      A dedicated test for clarity — the parity tests above would also catch it,
      but this makes the failure message unambiguous.

D9-F  test_addendum_surface_blocks_are_bilingual
      The surface addendum must return non-None for both "en" and "es" on the
      "workspace" surface — a missing locale block is a parity failure.

SCAN NOTE (comment-filtering)
------------------------------
The extraction regex targets the `<!-- anchor: ... -->` HTML-comment syntax
specifically. Plain prose that mentions an anchor name in passing (e.g., in a
section header) is NOT matched and does not self-invalidate the gate.
Anchor tags must use the exact `<!-- anchor: id -->` form.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.cue.personality.anchors import ANCHORS, get_anchors_for_locale
from services.cue.personality.addendums import AssembleContext, surface as surface_addendum


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CORE_DIR = Path(__file__).parent.parent.parent / "services" / "cue" / "personality" / "core"
_EN_CORE = _CORE_DIR / "en.md"
_ES_CORE = _CORE_DIR / "es.md"

# Anchor tag pattern — matches <!-- anchor: some-anchor-id -->
_ANCHOR_TAG_PATTERN = re.compile(r"<!--\s*anchor:\s*([\w-]+)\s*-->")


# ---------------------------------------------------------------------------
# Extraction utility
# ---------------------------------------------------------------------------


def extract_anchors_from_file(path: Path) -> set[str]:
    """
    Extract all `<!-- anchor: id -->` tag IDs from a markdown file.

    Case-insensitive anchor name matching; strips whitespace around the ID.
    Returns a set of anchor ID strings.
    Raises FileNotFoundError if the file does not exist.
    """
    text = path.read_text(encoding="utf-8")
    return {m.group(1).strip() for m in _ANCHOR_TAG_PATTERN.finditer(text)}


# ---------------------------------------------------------------------------
# D9-A: Smoke test — core files exist and are non-empty
# ---------------------------------------------------------------------------


def test_core_files_exist() -> None:
    """D9-A: Both locale core files must exist and be non-empty."""
    assert _EN_CORE.exists(), f"core/en.md not found at {_EN_CORE}"
    assert _ES_CORE.exists(), f"core/es.md not found at {_ES_CORE}"
    assert _EN_CORE.stat().st_size > 0, "core/en.md is empty"
    assert _ES_CORE.stat().st_size > 0, "core/es.md is empty"


# ---------------------------------------------------------------------------
# D9-B: EN anchors match registry
# ---------------------------------------------------------------------------


def test_en_anchors_match_registry() -> None:
    """
    D9-B: core/en.md must contain every anchor ID registered for 'en' in anchors.py.

    FAIL = an anchor registered for EN is missing from core/en.md,
    OR core/en.md has an unregistered anchor tag (indicates registry drift).
    """
    found_in_file = extract_anchors_from_file(_EN_CORE)
    expected_for_locale = set(get_anchors_for_locale("en"))

    missing_from_file = expected_for_locale - found_in_file
    extra_in_file = found_in_file - expected_for_locale

    assert not missing_from_file, (
        f"core/en.md is missing anchor tags for: {sorted(missing_from_file)}\n"
        f"Add <!-- anchor: id --> tags above each section in core/en.md."
    )
    assert not extra_in_file, (
        f"core/en.md has unregistered anchor tags: {sorted(extra_in_file)}\n"
        f"Either register them in anchors.py or remove the tags from core/en.md."
    )


# ---------------------------------------------------------------------------
# D9-C: ES anchors match registry
# ---------------------------------------------------------------------------


def test_es_anchors_match_registry() -> None:
    """
    D9-C: core/es.md must contain every anchor ID registered for 'es' in anchors.py.

    FAIL = an anchor registered for ES is missing from core/es.md,
    OR core/es.md has an unregistered anchor tag (indicates registry drift).
    """
    found_in_file = extract_anchors_from_file(_ES_CORE)
    expected_for_locale = set(get_anchors_for_locale("es"))

    missing_from_file = expected_for_locale - found_in_file
    extra_in_file = found_in_file - expected_for_locale

    assert not missing_from_file, (
        f"core/es.md is missing anchor tags for: {sorted(missing_from_file)}\n"
        f"Add <!-- anchor: id --> tags above each section in core/es.md."
    )
    assert not extra_in_file, (
        f"core/es.md has unregistered anchor tags: {sorted(extra_in_file)}\n"
        f"Either register them in anchors.py or remove the tags from core/es.md."
    )


# ---------------------------------------------------------------------------
# D9-D: Bilingual anchors match between locales
# ---------------------------------------------------------------------------


def test_en_es_bilingual_anchors_match_each_other() -> None:
    """
    D9-D: For anchors declared with languages=("en","es"), both locale files
    must have identical bilingual anchor sets.

    FAIL = an anchor tagged in en.md but not es.md (or vice versa) for a
    bilingual anchor — the deference constraint is silently missing in one locale.
    """
    bilingual_anchor_ids = {
        anchor_id
        for anchor_id, meta in ANCHORS.items()
        if "en" in meta.languages and "es" in meta.languages
    }

    en_anchors = extract_anchors_from_file(_EN_CORE)
    es_anchors = extract_anchors_from_file(_ES_CORE)

    # Restrict to bilingual anchors only
    en_bilingual = en_anchors & bilingual_anchor_ids
    es_bilingual = es_anchors & bilingual_anchor_ids

    in_en_not_es = en_bilingual - es_bilingual
    in_es_not_en = es_bilingual - en_bilingual

    assert not in_en_not_es, (
        f"Bilingual anchors found in en.md but MISSING from es.md: {sorted(in_en_not_es)}\n"
        f"Every edit to a bilingual anchor in core/en.md requires a paired edit in core/es.md "
        f"(CONTRIBUTING.md paired-edit discipline, PERS-05)."
    )
    assert not in_es_not_en, (
        f"Bilingual anchors found in es.md but MISSING from en.md: {sorted(in_es_not_en)}\n"
        f"Every edit to a bilingual anchor in core/es.md requires a paired edit in core/en.md "
        f"(CONTRIBUTING.md paired-edit discipline, PERS-05)."
    )


# ---------------------------------------------------------------------------
# D9-E: Clinical deference anchor in BOTH locales (hard gate)
# ---------------------------------------------------------------------------


def test_clinical_deference_anchor_in_both_locales() -> None:
    """
    D9-E: The 'clinical-deference' anchor MUST be present in BOTH core files.

    This is the COFEPRIS-avoidance control (PERS-04 / T-22-03-01).
    A deference anchor in only one locale removes the safety constraint for
    the other locale silently (AI-SPEC §1b FM4).

    Dedicated test for unambiguous failure messaging — the parity tests above
    would also catch it, but this makes the clinical-safety implication explicit.
    """
    deference_key = "clinical-deference"

    en_anchors = extract_anchors_from_file(_EN_CORE)
    es_anchors = extract_anchors_from_file(_ES_CORE)

    assert deference_key in en_anchors, (
        f"CRITICAL: 'clinical-deference' anchor is MISSING from core/en.md.\n"
        f"This removes the COFEPRIS scope-of-practice constraint from English sessions (PERS-04).\n"
        f"Add <!-- anchor: clinical-deference --> above the Clinical Deference section in en.md."
    )
    assert deference_key in es_anchors, (
        f"CRITICAL: 'clinical-deference' anchor is MISSING from core/es.md.\n"
        f"This removes the COFEPRIS scope-of-practice constraint from Spanish sessions (PERS-04).\n"
        f"Add <!-- anchor: clinical-deference --> above the Deferencia Clínica section in es.md."
    )


# ---------------------------------------------------------------------------
# D9-F: Addendum surface blocks are bilingual
# ---------------------------------------------------------------------------


def test_addendum_surface_blocks_are_bilingual() -> None:
    """
    D9-F: The surface addendum must return a non-None string for both locales
    on the 'workspace' surface — a missing locale block is a parity failure.
    """
    ctx_en = AssembleContext(locale="en", surface="workspace")
    ctx_es = AssembleContext(locale="es", surface="workspace")

    block_en = surface_addendum(ctx_en)
    block_es = surface_addendum(ctx_es)

    assert block_en is not None, (
        "surface addendum returned None for locale='en', surface='workspace'. "
        "The workspace surface must have an EN block (addendums.py parity)."
    )
    assert block_es is not None, (
        "surface addendum returned None for locale='es', surface='workspace'. "
        "The workspace surface must have an ES block (addendums.py parity)."
    )
    assert len(block_en) > 20, "EN surface addendum block is suspiciously short."
    assert len(block_es) > 20, "ES surface addendum block is suspiciously short."
