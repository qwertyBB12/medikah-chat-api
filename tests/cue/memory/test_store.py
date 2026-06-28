"""tests/cue/memory/test_store.py — DB layer for memory notes (scoping + fail-open)."""
from unittest.mock import MagicMock

from services.cue.memory.store import (
    has_aviso_ack, load_recent_notes, insert_note,
)


def _chain(execute_data):
    """Build a supabase MagicMock whose .table(...)....execute() returns .data."""
    sb = MagicMock()
    result = MagicMock()
    result.data = execute_data
    # Every chained call returns the same builder; .execute() returns result.
    builder = sb.table.return_value
    for attr in ("select", "insert", "eq", "order", "limit"):
        getattr(builder, attr).return_value = builder
    builder.execute.return_value = result
    return sb


class TestAvisoAck:
    def test_true_when_row_exists(self):
        sb = _chain([{"physician_id": "phys-1"}])
        assert has_aviso_ack(sb, "phys-1") is True

    def test_false_when_no_row(self):
        sb = _chain([])
        assert has_aviso_ack(sb, "phys-1") is False

    def test_false_when_client_none(self):
        assert has_aviso_ack(None, "phys-1") is False


class TestLoadRecentNotes:
    def test_returns_notes_newest_first(self):
        sb = _chain([
            {"note": "b", "appended_at": "2026-06-27T10:00:00Z", "category": "project"},
            {"note": "a", "appended_at": "2026-06-26T10:00:00Z", "category": "general"},
        ])
        notes = load_recent_notes(sb, "phys-1", limit=10)
        assert [n["note"] for n in notes] == ["b", "a"]
        # scoped to physician_id
        sb.table.return_value.eq.assert_any_call("physician_id", "phys-1")

    def test_empty_on_none_client(self):
        assert load_recent_notes(None, "phys-1") == []

    def test_empty_on_error(self):
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("db down")
        assert load_recent_notes(sb, "phys-1") == []


class TestInsertNote:
    def test_inserts_scoped_row(self):
        sb = _chain([{"id": "note-1"}])
        insert_note(sb, "phys-1", "the doctor is preparing the launch", "project", "en")
        args = sb.table.return_value.insert.call_args[0][0]
        assert args["physician_id"] == "phys-1"
        assert args["note"] == "the doctor is preparing the launch"
        assert args["category"] == "project"

    def test_insert_never_raises_on_error(self):
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("db down")
        insert_note(sb, "phys-1", "note", "general", "en")  # must not raise
