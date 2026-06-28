"""
services/cue/memory/judge.py
----------------------------
MEM-02 memory judge — a Haiku call decides whether a turn is worth remembering and
writes ONE short third-person, clinically-salient, PHI-minimized note.

Port of BeNeXT memory-judge-helper.ts + judge-prompt.ts, re-authored for the
DOCTOR's practice/state and adapted to:
  - physician_id keying (CUE-11), not supabase_user_id + install token,
  - the aviso-ack gate (PATCH-03),
  - mandatory free-text redaction of the summary (PATCH-01),
  - never-throws / fail-open (CUE-04b).

The judge runs on Haiku via the existing tier→model routing (select_model).
"""
from __future__ import annotations

import json
import logging
import os
import re

from anthropic import AsyncAnthropic

from services.cue.adapter import select_model
from .redact import redact_free_text
from .embeddings import embed
from .store import has_aviso_ack, insert_note, find_similar_note, update_note

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = {"identity", "practice", "project", "follow_up", "preference", "general"}


def _judge_client() -> AsyncAnthropic:
    """Isolated Anthropic client for the side judge call (mirrors create_adapter)."""
    return AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def build_judge_prompt(physician_name: str | None) -> str:
    """Clinical-salience judge system prompt, subject pinned to the physician."""
    who = physician_name.strip() if physician_name and physician_name.strip() else None
    subject = who or "the doctor"
    name_guard = (
        f'Refer to the doctor as "{who}" in the third person. Never substitute another name.'
        if who else
        'Refer to the subject as "the doctor" or "they". Never invent a name — in particular, '
        'never call the doctor "Hector" unless they explicitly identify themselves as Hector in this turn.'
    )
    return f"""You decide whether a single turn between {subject} and Cue is worth saving as
one short third-person note for cross-session recall.

This is a DOCTOR-facing clinical assistant in Mexico. Save a note ONLY if it carries:
  - a fact about the doctor's practice or state (specialty focus, how they work, workload),
  - a project or initiative the doctor is pursuing (a launch, a study, a hire),
  - a follow-up commitment the doctor made (something to do next session),
  - a stated preference about how Cue should work with them.

DO NOT save:
  - small talk or generic acknowledgment,
  - things Cue did (its own outputs) unless they encode a commitment,
  - sensitive patient clinical detail (diagnoses, conditions, identifiers) — MINIMIZE: keep
    notes about the DOCTOR's practice, not the patient's medical record.

Faithfulness: summarize faithfully in third person; preserve the doctor's own words for
project and concept names; never invent a claim the turn does not support. {name_guard}

Return STRICT JSON only:
{{ "kept": boolean,
   "summary": string | null,   // third-person, ONE short sentence; null if kept=false
   "category": string | null }} // one of: identity, practice, project, follow_up, preference, general
If kept=false, summary and category must be null."""


def _parse_judgement(text: str) -> dict | None:
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed.get("kept"), bool):
        return None
    return parsed


async def run_memory_judge(
    supabase,
    physician_id: str,
    turn: dict,
    locale: str,
    physician_name: str | None,
) -> None:
    """Gate → judge → redact (fail-closed) → insert. Never raises (CUE-04b)."""
    try:
        if not has_aviso_ack(supabase, physician_id):
            return  # PATCH-03: no memory until the doctor acknowledges the aviso

        system_prompt = build_judge_prompt(physician_name)
        user_content = (
            f"user: {str(turn.get('user', ''))[:4000]}\n"
            f"assistant: {str(turn.get('assistant', ''))[:4000]}"
        )

        client = _judge_client()
        msg = await client.messages.create(
            model=select_model(tier="haiku"),
            max_tokens=200,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

        decision = _parse_judgement(text)
        if not decision or not decision.get("kept") or not decision.get("summary"):
            return

        category = decision.get("category")
        if category not in _VALID_CATEGORIES:
            category = "general"

        # PATCH-01 — redact the free-text body; fail-closed (no store if redaction errors).
        try:
            redacted = redact_free_text(decision["summary"])
        except Exception as exc:
            logger.error("[cue-memory] redaction failed for %s — note dropped: %s", physician_id, exc)
            return
        if not redacted.strip():
            return

        # Slice 2: embed (fail-open) → consolidate a near-duplicate if one exists,
        # else insert. A null embedding (no provider) stores a recency-only note.
        embedding = await embed(redacted)
        if embedding is not None:
            similar = find_similar_note(supabase, physician_id, embedding, category)
            if similar is not None:
                update_note(
                    supabase,
                    similar["id"],
                    redacted,
                    embedding,
                    (similar.get("salience", 1) or 1) + 1,
                )
                return
        insert_note(supabase, physician_id, redacted, category, locale, embedding)

    except Exception as exc:  # CUE-04b: swallow everything
        logger.error("[cue-memory] run_memory_judge failed for %s — swallowed: %s", physician_id, exc)
