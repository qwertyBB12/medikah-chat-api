"""
tests/cue/test_capability_directives.py
-----------------------------------------
Guard the two prompt directives that keep Cue useful on the dashboard:

1. PROPOSER AWARENESS — the assembled prompt must tell Cue it can PROPOSE blocking
   time (block / clear), so the model tees up a D-03 confirm card instead of giving
   a read-only "I can't write to your calendar" refusal. Regression source: the
   self-knowledge + surface blocks once listed only READ capabilities, so the model
   refused "schedule a meeting tomorrow at 9am" rather than calling
   calendar_block_time. (Live bug 2026-06-25 — "talky refusal, no card".)

2. NO-MARKDOWN — the answer line is plain text (no Markdown renderer) AND it is the
   speakable TTS feed, so Markdown emphasis surfaces as literal "*asterisks*". The
   "no markdown" rule used to live ONLY in the voice addendum, but /cue/chat always
   assembles with mode='text' → it never fired. The directive now lives in the
   always-applied self-knowledge Output-format block.

These are string-presence guards over assemble() — the same gate style as
test_no_brand_bleed.py / test_anchor_parity.py.
"""

from __future__ import annotations

from services.cue.personality.assemble import assemble


def _assembled(locale: str) -> str:
    return assemble(locale=locale, surface="workspace")


def test_en_prompt_advertises_calendar_block_proposer() -> None:
    out = _assembled("en").lower()
    # Cue must know it can propose blocking the doctor's own time...
    assert "propose" in out
    assert "block" in out
    # ...and be told explicitly NOT to refuse a scheduling request.
    assert "schedule, block, hold" in out  # the directive verb list
    assert "confirm" in out                # propose-and-confirm framing


def test_es_prompt_advertises_calendar_block_proposer() -> None:
    out = _assembled("es").lower()
    assert "propón el bloqueo" in out or "proponer" in out
    assert "agendar, bloquear" in out      # the directive verb list
    assert "confirmar" in out


def test_en_prompt_forbids_markdown() -> None:
    out = _assembled("en")
    assert "No Markdown" in out


def test_es_prompt_forbids_markdown() -> None:
    out = _assembled("es")
    assert "Sin Markdown" in out
