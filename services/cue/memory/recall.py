"""
services/cue/memory/recall.py
-----------------------------
MEM-01 recall envelope formatter (Python port of BeNeXT recall-envelope.ts) +
MEM-07 prompt-injection fence.

PURE function — no fetch, no Supabase. Transforms recalled notes into the
<cue-session-recall>…</cue-session-recall> block that _build_system_prompt
prepends to the assembled clinical system prompt.

SECURITY: note text is user-influenced and lands in the system prompt. We
neutralize any forged <cue-session-recall> boundary and cap length so a note
cannot break out of the data block or stuff the context. Self-scoped (a doctor
only affects their own recall) — defense in depth.
"""
from __future__ import annotations

import re
from typing import TypedDict


class RecallNote(TypedDict):
    note: str
    appended_at: str
    category: str


_RECALL_SENTINEL = re.compile(r"<\/?\s*cue-session-recall\s*>", re.IGNORECASE)

_HEADERS = {
    "en": {"recent": "Recent notes (newest first):", "none": "(none yet)"},
    "es": {"recent": "Notas recientes (más recientes primero):", "none": "(aún sin notas)"},
}


def _sanitize_note(text: str, max_len: int = 600) -> str:
    cleaned = _RECALL_SENTINEL.sub("[fenced]", text or "")
    return cleaned[:max_len] + "…" if len(cleaned) > max_len else cleaned


def assemble_recall_envelope(notes: list[RecallNote], locale: str) -> str:
    """Assemble the session-start recall envelope. Pure; never raises for list input."""
    h = _HEADERS.get(locale, _HEADERS["en"])

    if not notes:
        body = f"  {h['none']}"
    else:
        lines = []
        for n in notes:
            raw_date = n.get("appended_at") or ""
            date_str = raw_date[:10] if raw_date else "(unknown date)"
            category = n.get("category") or "general"
            lines.append(f"- {date_str} — {_sanitize_note(n.get('note', ''))}  [{category}]")
        body = "\n".join(lines)

    return "\n".join([
        "<cue-session-recall>",
        h["recent"],
        body,
        "</cue-session-recall>",
    ])
