"""tests/cue/memory/test_store_vector.py — Slice 2 store: semantic recall + consolidation."""
from unittest.mock import MagicMock

from services.cue.memory.store import (
    load_relevant_notes, find_similar_note, update_note, insert_note,
)


def _rpc(data):
    """supabase mock whose .rpc(name, params).execute() returns .data = data."""
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(data=data)
    return sb


def _table(data):
    """supabase mock whose .table(...).select().eq().order().limit().execute() returns data."""
    sb = MagicMock()
    builder = sb.table.return_value
    for attr in ("select", "insert", "update", "eq", "order", "limit"):
        getattr(builder, attr).return_value = builder
    builder.execute.return_value = MagicMock(data=data)
    return sb


class TestLoadRelevantNotes:
    def test_semantic_path_used_when_embedding_given(self):
        sb = _rpc([{"note": "n1", "appended_at": "2026-06-27T10:00:00Z", "category": "project", "distance": 0.02}])
        notes = load_relevant_notes(sb, "phys-1", [0.1] * 8, limit=10)
        assert notes[0]["note"] == "n1"
        # called the RPC, scoped to physician
        name, params = sb.rpc.call_args[0]
        assert name == "match_cue_memory_notes"
        assert params["p_physician_id"] == "phys-1"

    def test_recency_fallback_when_no_embedding(self):
        sb = _table([{"note": "recent", "appended_at": "2026-06-27T10:00:00Z", "category": "general"}])
        notes = load_relevant_notes(sb, "phys-1", None, limit=10)
        assert notes[0]["note"] == "recent"
        sb.rpc.assert_not_called()

    def test_recency_fallback_when_rpc_empty(self):
        sb = _table([{"note": "recent", "appended_at": "2026-06-27T10:00:00Z", "category": "general"}])
        # make rpc return empty so it falls back to the table path
        sb.rpc.return_value.execute.return_value = MagicMock(data=[])
        notes = load_relevant_notes(sb, "phys-1", [0.1] * 8, limit=10)
        assert notes[0]["note"] == "recent"


class TestFindSimilarNote:
    def test_returns_id_and_salience(self):
        sb = _rpc([{"id": "note-9", "salience": 3, "distance": 0.05}])
        hit = find_similar_note(sb, "phys-1", [0.1] * 8, "project")
        assert hit == {"id": "note-9", "salience": 3}

    def test_none_when_no_match(self):
        sb = _rpc([])
        assert find_similar_note(sb, "phys-1", [0.1] * 8, "project") is None

    def test_none_when_no_embedding(self):
        sb = MagicMock()
        assert find_similar_note(sb, "phys-1", None, "project") is None
        sb.rpc.assert_not_called()


class TestUpdateAndInsert:
    def test_update_note_bumps_salience_and_embedding(self):
        sb = _table([{"id": "note-9"}])
        update_note(sb, "note-9", "the refreshed note", [0.2] * 8, salience=4)
        payload = sb.table.return_value.update.call_args[0][0]
        assert payload["note"] == "the refreshed note"
        assert payload["salience"] == 4
        assert payload["embedding"] == [0.2] * 8
        sb.table.return_value.eq.assert_any_call("id", "note-9")

    def test_insert_note_includes_embedding(self):
        sb = _table([{"id": "note-1"}])
        insert_note(sb, "phys-1", "a note", "project", "en", embedding=[0.3] * 8)
        payload = sb.table.return_value.insert.call_args[0][0]
        assert payload["embedding"] == [0.3] * 8
        assert payload["physician_id"] == "phys-1"

    def test_insert_note_omits_embedding_when_none(self):
        sb = _table([{"id": "note-1"}])
        insert_note(sb, "phys-1", "a note", "project", "en", embedding=None)
        payload = sb.table.return_value.insert.call_args[0][0]
        assert "embedding" not in payload
