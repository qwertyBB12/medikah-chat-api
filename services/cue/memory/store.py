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
from datetime import datetime, timezone

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


def insert_note(
    supabase,
    physician_id: str,
    note: str,
    category: str,
    locale: str,
    embedding: list[float] | None = None,
) -> None:
    """Insert one memory note, scoped to physician_id. Never raises (CUE-04b).

    Slice 2: stores the embedding when present; a null embedding is reached by
    the recency fallback in load_relevant_notes.
    """
    if supabase is None:
        return
    try:
        row = {
            "physician_id": physician_id,
            "note": note,
            "category": category,
            "source_tag": "judge-inferred",
            "locale": locale,
        }
        if embedding is not None:
            row["embedding"] = embedding
        supabase.table("cue_memory_notes").insert(row).execute()
    except Exception as exc:
        logger.error("[cue-memory] insert_note failed for %s: %s", physician_id, exc)


# ---------------------------------------------------------------------------
# Slice 2 — semantic recall + consolidation (via migration 037 RPCs)
# ---------------------------------------------------------------------------


def load_relevant_notes(supabase, physician_id: str, query_embedding, limit: int = 10) -> list[RecallNote]:
    """Semantic recall: nearest notes to query_embedding (CUE-11 scoped).

    Falls back to recency (load_recent_notes) when no query embedding is given,
    when the semantic search returns nothing (e.g. notes not embedded yet), or on
    any error. Never raises.
    """
    if supabase is None:
        return []
    if query_embedding:
        try:
            res = supabase.rpc("match_cue_memory_notes", {
                "p_physician_id": physician_id,
                "p_query_embedding": query_embedding,
                "p_match_count": limit,
            }).execute()
            rows = res.data or []
            if rows:
                return [
                    {"note": r["note"], "appended_at": r.get("appended_at", ""), "category": r.get("category", "general")}
                    for r in rows
                ]
        except Exception as exc:
            logger.warning("[cue-memory] semantic recall failed for %s — recency fallback: %s", physician_id, exc)
    return load_recent_notes(supabase, physician_id, limit)


def find_similar_note(supabase, physician_id: str, embedding, category: str, max_distance: float = 0.15):
    """Return {"id","salience"} of the nearest same-category near-duplicate, or None.

    Used for consolidation — the judge updates this note instead of inserting a
    duplicate. Never raises.
    """
    if supabase is None or not embedding:
        return None
    try:
        res = supabase.rpc("find_similar_cue_note", {
            "p_physician_id": physician_id,
            "p_embedding": embedding,
            "p_category": category,
            "p_max_distance": max_distance,
        }).execute()
        rows = res.data or []
        if rows:
            return {"id": rows[0]["id"], "salience": rows[0].get("salience", 1)}
        return None
    except Exception as exc:
        logger.warning("[cue-memory] find_similar_note failed for %s: %s", physician_id, exc)
        return None


def list_notes(supabase, physician_id: str) -> list[dict]:
    """Full note rows for the doctor-visible management UI (CUE-11 scoped).
    Never raises — returns [] on any error."""
    if supabase is None:
        return []
    try:
        res = (
            supabase.table("cue_memory_notes")
            .select("id, note, category, source_tag, salience, appended_at, updated_at")
            .eq("physician_id", physician_id)
            .order("updated_at", desc=True)
            .execute()
        )
        return list(res.data or [])
    except Exception as exc:
        logger.warning("[cue-memory] list_notes failed for %s: %s", physician_id, exc)
        return []


def delete_note(supabase, physician_id: str, note_id: str) -> bool:
    """Delete a note the doctor owns. Scoped by BOTH id AND physician_id (IDOR guard).
    Returns True on success, False on error."""
    if supabase is None:
        return False
    try:
        (
            supabase.table("cue_memory_notes")
            .delete()
            .eq("id", note_id)
            .eq("physician_id", physician_id)
            .execute()
        )
        return True
    except Exception as exc:
        logger.error("[cue-memory] delete_note failed for %s/%s: %s", physician_id, note_id, exc)
        return False


def correct_note(supabase, physician_id: str, note_id: str, note: str, embedding) -> bool:
    """Doctor edits a note's text (re-embedded by the caller). Scoped by id AND
    physician_id (IDOR guard). Returns True on success, False on error."""
    if supabase is None:
        return False
    try:
        payload = {"note": note, "updated_at": datetime.now(timezone.utc).isoformat()}
        if embedding is not None:
            payload["embedding"] = embedding
        (
            supabase.table("cue_memory_notes")
            .update(payload)
            .eq("id", note_id)
            .eq("physician_id", physician_id)
            .execute()
        )
        return True
    except Exception as exc:
        logger.error("[cue-memory] correct_note failed for %s/%s: %s", physician_id, note_id, exc)
        return False


def update_note(supabase, note_id: str, note: str, embedding, salience: int) -> None:
    """Update a note in place (consolidation): refresh text, embedding, salience,
    and updated_at so the living profile replaces a near-duplicate. Never raises."""
    if supabase is None:
        return
    try:
        payload = {
            "note": note,
            "salience": salience,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if embedding is not None:
            payload["embedding"] = embedding
        supabase.table("cue_memory_notes").update(payload).eq("id", note_id).execute()
    except Exception as exc:
        logger.error("[cue-memory] update_note failed for %s: %s", note_id, exc)
