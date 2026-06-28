"""
services/cue/memory/store.py
----------------------------
DB layer for Cue memory notes. All calls scoped to physician_id (CUE-11).
Read/consent helpers fail OPEN (return empty/False) so a DB hiccup never breaks
a turn; insert fails OPEN (logs, never raises) per CUE-04b.

Follows the supabase-py call style in services/cue/gate.py:
  supabase.table(name).select(cols).eq(col, val).execute()  -> result.data
"""
from __future__ import annotations

import logging

from .recall import RecallNote

logger = logging.getLogger(__name__)


def has_aviso_ack(supabase, physician_id: str) -> bool:
    """True iff the physician has acknowledged the memory aviso (PATCH-03 gate)."""
    if supabase is None:
        return False
    try:
        res = (
            supabase.table("cue_memory_consent")
            .select("physician_id")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as exc:  # fail-closed gate: no ack proof → no memory
        logger.warning("[cue-memory] aviso-ack check failed for %s: %s", physician_id, exc)
        return False


def load_recent_notes(supabase, physician_id: str, limit: int = 10) -> list[RecallNote]:
    """Newest-first notes for this physician. Never raises — returns [] on any error."""
    if supabase is None:
        return []
    try:
        res = (
            supabase.table("cue_memory_notes")
            .select("note, appended_at, category")
            .eq("physician_id", physician_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [
            {"note": r["note"], "appended_at": r.get("appended_at", ""), "category": r.get("category", "general")}
            for r in (res.data or [])
        ]
    except Exception as exc:
        logger.warning("[cue-memory] load_recent_notes failed for %s: %s", physician_id, exc)
        return []


def insert_note(supabase, physician_id: str, note: str, category: str, locale: str) -> None:
    """Insert one memory note, scoped to physician_id. Never raises (CUE-04b)."""
    if supabase is None:
        return
    try:
        supabase.table("cue_memory_notes").insert({
            "physician_id": physician_id,
            "note": note,
            "category": category,
            "source_tag": "judge-inferred",
            "locale": locale,
        }).execute()
    except Exception as exc:
        logger.error("[cue-memory] insert_note failed for %s: %s", physician_id, exc)
