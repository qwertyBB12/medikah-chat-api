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
from datetime import datetime, timedelta, timezone

from .recall import RecallNote

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PATCH-03 retention TTL
# ---------------------------------------------------------------------------
# Migration 036 gave cue_memory_notes an expires_at column and nothing ever
# wrote it, so "notes expire" was a promise with no mechanism behind it.
#
# The aviso de privacidad (frontend lib/cueAvisoContent.ts §6 Retención) states
# the period as "[TTL_VALUE_DAYS] days from the creation date of each note" —
# the number was left for legal review and never filled in. 24 months is the
# default carried here until counsel pins it; changing it is a one-line change
# plus the matching aviso text.
#
# ANCHORED TO CREATION, NEVER REFRESHED. update_note (consolidation) deliberately
# leaves expires_at alone: the aviso promises a clock that starts when the note
# is created, and rolling it forward on every near-duplicate merge would let a
# frequently-reinforced note outlive the retention promise indefinitely.
MEMORY_RETENTION_DAYS = 730


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _filter_ts(moment: datetime) -> str:
    """Timestamp formatted for a PostgREST filter value.

    Seconds precision with a literal Z rather than .isoformat(): the '+00:00'
    offset would have to survive query-string encoding, and Z will not.
    """
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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
            # PATCH-03: an expired note must never reach the prompt, whether or
            # not the purge sweep has caught up with it yet. A null expires_at is
            # a legacy row written before the TTL landed — migration 036 reads
            # null as "default policy", so those stay recallable.
            .or_(f"expires_at.is.null,expires_at.gt.{_filter_ts(_utc_now())}")
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

    PATCH-03: stamps expires_at at MEMORY_RETENTION_DAYS from creation, so the
    retention promise in the aviso has a value behind it from the first note on.
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
            "expires_at": (
                _utc_now() + timedelta(days=MEMORY_RETENTION_DAYS)
            ).isoformat(),
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

    PATCH-03 TTL GAP (known, bounded): the semantic branch selects rows inside the
    match_cue_memory_notes RPC (migration 037), which has no expires_at predicate
    and returns neither the id nor expires_at — so an expired note cannot be
    filtered out from here, in SQL or in Python. purge_expired_notes() is the
    enforcement for this path: once an expired row is deleted the RPC stops
    returning it. Closing the gap properly needs a migration adding
    `and (n.expires_at is null or n.expires_at > now())` to the RPC's where
    clause; that SQL lives in medikah-chat-frontend/supabase/migrations, outside
    this service. The recency branch below is filtered at the query.
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

# NOTE: there is deliberately NO correct_note / edit op. Doctors view + delete only;
# rewriting a note would silently skew Cue's reasoning (product decision 2026-06-28).


def purge_expired_notes(supabase) -> int:
    """Hard-delete every memory note past its retention TTL (PATCH-03 enforcement).

    Deliberately NOT physician-scoped: this is the retention promise itself, a
    maintenance sweep across the table, not a doctor-facing read. Rows with a
    null expires_at (legacy, written before the TTL landed) are left alone —
    `lt` never matches null.

    Idempotent and safe to run repeatedly. Returns the number of rows deleted, or
    -1 on error (never raises — the caller decides whether a failed sweep is
    worth surfacing; the internal route maps -1 to a 500 so a silent cron does
    not report a green sweep that deleted nothing).
    """
    if supabase is None:
        return 0
    try:
        res = (
            supabase.table("cue_memory_notes")
            .delete()
            .lt("expires_at", _filter_ts(_utc_now()))
            .execute()
        )
        deleted = len(res.data or [])
        logger.info("[cue-memory] purge_expired_notes deleted %d expired note(s)", deleted)
        return deleted
    except Exception as exc:
        logger.error("[cue-memory] purge_expired_notes failed: %s", exc)
        return -1


def update_note(supabase, note_id: str, note: str, embedding, salience: int) -> None:
    """Update a note in place (consolidation): refresh text, embedding, salience,
    and updated_at so the living profile replaces a near-duplicate. Never raises.

    expires_at is deliberately NOT refreshed — see MEMORY_RETENTION_DAYS: the
    aviso anchors the clock to creation, so consolidation extends what a note
    says, never how long it lives."""
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
