"""
services/cue/personality/assemble.py
--------------------------------------
Clinical personality assembler — Python port of BeNeXT cue-personality/src/assemble.ts.

`assemble()` is the single entry point the engine calls to get the full clinical
system prompt. It:

  1. Emits a version/context header
  2. Loads the locale-keyed clinical core markdown via filesystem read
     (CUE-05 shim: Vite `?raw` import → Python `pathlib` / `open()`)
  3. Appends the self-knowledge block (clinical Cue identity + capabilities)
  4. Iterates ADDENDUM_ORDER, appending each non-None block
  5. Appends an explicit per-locale language directive
     (AI-SPEC §4b.3 mandate: "Respond ONLY in SPANISH/ENGLISH.")
  6. Joins all parts with double newlines

COMPOSITION ORDER (mirrors BeNeXT ADDENDUM_ORDER from assemble.ts):
  1. version header
  2. core (canonical clinical instrument)
  3. self-knowledge block (clinical Cue identity)
  4. surface addendum
  5. tier addendum
  6. voice_mode addendum
  7. voice_register addendum
  8. language directive (always last)

SAFETY NOTE (PERS-04 / T-22-03-01):
The clinical-deference anchor is embedded in the core .md files and is
therefore present in EVERY assembled prompt. It is NOT gated by any addendum.
This prevents scope-creep drift across a long conversation.

The test_anchor_parity.py merge gate asserts the deference anchor is in both
locale core files. The test_no_brand_bleed.py gate asserts no BeNeXT tokens
survive in any assembled prompt across both locales + all surface hints.
"""

from __future__ import annotations

import os
from pathlib import Path

from .addendums import ADDENDUM_ORDER, AssembleContext, Locale
from .self_knowledge import build_self_knowledge

# Version token — bump when semantic changes land (mirrors BeNeXT VERSION)
_VERSION = "1.0.0-medikah"

# Path to the clinical core markdown files
_CORE_DIR = Path(__file__).parent / "core"

# Supported locales (mirrors BeNeXT CORE dict)
_SUPPORTED_LOCALES: frozenset[Locale] = frozenset(("en", "es"))


# ---------------------------------------------------------------------------
# Core loader (CUE-05 shim: Vite ?raw import → Python fs read)
# ---------------------------------------------------------------------------


def _load_core(locale: Locale) -> str:
    """
    Load the clinical core markdown for a given locale.

    CUE-05 HOST SHIM: BeNeXT uses Vite `import esCore from './core/es.md?raw'`
    which inlines the markdown content at build time. Python has no equivalent —
    we read from disk at import-load time (cached per process via module-level
    dict after first load).

    Raises ValueError for unsupported locales.
    Raises FileNotFoundError if the core file is missing (should never happen
    in a correct deployment — surfaces immediately in dev).
    """
    if locale not in _SUPPORTED_LOCALES:
        raise ValueError(
            f"Unsupported locale {locale!r}. "
            f"Supported: {sorted(_SUPPORTED_LOCALES)}"
        )
    core_path = _CORE_DIR / f"{locale}.md"
    if not core_path.exists():
        raise FileNotFoundError(
            f"Clinical core file not found: {core_path}. "
            "Run the Phase 22 plan to generate core/en.md and core/es.md."
        )
    return core_path.read_text(encoding="utf-8")


# Module-level cache (equivalent to Vite static import inlining)
_CORE_CACHE: dict[Locale, str] = {}


def load_core(locale: Locale) -> str:
    """Return the clinical core markdown, cached after first read."""
    if locale not in _CORE_CACHE:
        _CORE_CACHE[locale] = _load_core(locale)
    return _CORE_CACHE[locale]


# ---------------------------------------------------------------------------
# Header formatter
# ---------------------------------------------------------------------------


def _format_header(ctx: AssembleContext) -> str:
    """
    Emit a version/context comment header.

    Port of BeNeXT `formatHeader()`. Callers can grep for the version string
    in assembled prompts to verify the assembly ran.
    """
    voice = ctx.voice_gender if ctx.voice_gender else "null"
    return (
        f"<!-- cue-personality v{_VERSION} "
        f"— surface={ctx.surface} mode={ctx.mode} "
        f"tier={ctx.tier} locale={ctx.locale} voice={voice} -->"
    )


# ---------------------------------------------------------------------------
# Language directive (AI-SPEC §4b.3 mandate)
# ---------------------------------------------------------------------------


def _language_directive(locale: Locale) -> str:
    """
    Emit the bilingual language directive (EN + ES).

    Cue is bilingual: it MIRRORS the doctor's language turn-by-turn rather than
    locking to a single locale (BeNeXT-parity — physicians switch freely between
    Spanish and English mid-conversation). `locale` only sets the default for the
    opening greeting, before the doctor has said anything.

    Appears LAST in the assembled prompt so it is not buried by addendums.
    """
    default_lang = "Spanish" if locale == "es" else "English"
    return (
        "--- LANGUAGE DIRECTIVE ---\n\n"
        "Respond in the SAME language the doctor uses. If they write or speak to "
        "you in Spanish, respond in Spanish; if in English, respond in English. "
        "Mirror their language turn by turn — if they switch, you switch. "
        f"When their language is not yet known (e.g. the opening greeting), default to {default_lang}. "
        "Never tell the doctor you can only speak one language. "
        "Technical terms, brand names, and drug names may stay in their original language."
    )


# ---------------------------------------------------------------------------
# Public assembler
# ---------------------------------------------------------------------------


def assemble(
    locale: Locale = "es",
    surface: str = "workspace",
    mode: str = "text",
    tier: str | None = "standard",
    voice_gender: str | None = None,
) -> str:
    """
    Assemble the full clinical system prompt for a Cue turn.

    Python port of BeNeXT `assemble(ctx: AssembleContext): string`.

    Parameters
    ----------
    locale       : "en" | "es" — physician locale. Spanish-first for Medikah.
    surface      : "workspace" | "claude-code" | "voice" — surface hint for addendums.
    mode         : "text" | "voice" — conversation mode.
    tier         : clinical tier string — gates model quality, not cost.
    voice_gender : "male" | "female" | None — governs voice register addendum.

    Returns
    -------
    str
        The assembled system prompt, ready to pass as the `system` parameter
        to the AnthropicAdapter (or any future CueModelAdapter).

    Safety guarantee
    ----------------
    The clinical-deference anchor (PERS-04) is embedded in the core .md files
    and appears in every assembled prompt regardless of locale, surface, or
    addendum combination. The parity gate enforces its presence in both locales.
    """
    # Coerce str surface/mode/voice_gender to typed literals
    _locale: Locale = "es" if locale == "es" else "en"  # default to es for unsupported
    _surface = surface if surface in ("workspace", "claude-code", "voice") else "workspace"
    _mode = mode if mode in ("text", "voice") else "text"
    _tier = tier
    _voice_gender = voice_gender if voice_gender in ("male", "female") else None

    ctx = AssembleContext(
        locale=_locale,
        surface=_surface,  # type: ignore[arg-type]
        mode=_mode,  # type: ignore[arg-type]
        tier=_tier,  # type: ignore[arg-type]
        voice_gender=_voice_gender,  # type: ignore[arg-type]
    )

    parts: list[str] = [
        _format_header(ctx),
        load_core(_locale),
        build_self_knowledge(_locale),
    ]

    for addendum_fn in ADDENDUM_ORDER:
        block = addendum_fn(ctx)
        if block is not None:
            parts.append(block)

    # Language directive always last (AI-SPEC §4b.3)
    parts.append(_language_directive(_locale))

    return "\n\n".join(parts)
