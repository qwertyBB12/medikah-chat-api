"""
services/cue/personality
-------------------------
Clinical personality package for Medikah Cue.

Python port of BeNeXT `@hector-ecosystem/cue-personality` — re-authored for
a doctor-facing clinical workspace context (PERS-01..06, I18N-05).

Public API
----------
assemble(locale, surface, mode, tier, voice_gender) -> str
    The single entry point. Returns the full clinical system prompt assembled
    from: version header + clinical core markdown + self-knowledge block +
    addendums (surface, tier, voice_mode, voice_register) + language directive.

    Called by the engine's buildSystemPrompt() equivalent before each Cue turn.
    The assembled prompt includes the clinical-deference anchor (PERS-04) in
    every call regardless of locale, surface, or addendum combination.

Merge-blocking gates (AI-SPEC §6)
----------------------------------
- tests/cue/test_anchor_parity.py  — asserts EN/ES anchor-set parity (I18N-05 / D9)
- tests/cue/test_no_brand_bleed.py — asserts zero BeNeXT brand tokens in assembled
                                     prompts across all locales + surfaces (D10)

Design notes
------------
- ZERO BeNeXT brand language in assembled output (D10 forbidden list enforced by test)
- ZERO PHI in core files — examples are synthetic/anonymous
- clinical-deference anchor (PERS-04) is a required anchor KEY in both locale
  core files, enforced by the parity gate
- Locale default: "es" (Spanish-first for Medikah's physician base)
"""

from .assemble import assemble, load_core
from .anchors import ANCHORS, get_anchor_ids, get_anchors_for_locale, is_anchor_id
from .addendums import AssembleContext, ADDENDUM_ORDER
from .self_knowledge import build_self_knowledge

__all__ = [
    "assemble",
    "load_core",
    "ANCHORS",
    "get_anchor_ids",
    "get_anchors_for_locale",
    "is_anchor_id",
    "AssembleContext",
    "ADDENDUM_ORDER",
    "build_self_knowledge",
]
