"""tests/cue/memory/test_store_manage.py — Slice 3 doctor-visible/editable store ops.

delete/correct MUST be scoped by BOTH note id AND physician_id (IDOR: a doctor can
never touch another doctor's note).
"""
from unittest.mock import MagicMock

from services.cue.memory.store import list_notes, delete_note, correct_note


def _table(data):
    sb = MagicMock()
    builder = sb.table.return_value
    for attr in ("select", "insert", "update", "delete", "eq", "order", "limit"):
        getattr(builder, attr).return_value = builder
    builder.execute.return_value = MagicMock(data=data)
    return sb


class TestListNotes:
    def test_lists_full_fields_scoped(self):
        sb = _table([
            {"id": "n1", "note": "preparing launch", "category": "project",
             "salience": 2, "appended_at": "2026-06-27T10:00:00Z", "updated_at": "2026-06-27T11:00:00Z"},
        ])
        notes = list_notes(sb, "phys-1")
        assert notes[0]["id"] == "n1"
        assert notes[0]["category"] == "project"
        sb.table.return_value.eq.assert_any_call("physician_id", "phys-1")

    def test_empty_on_none_client(self):
        assert list_notes(None, "phys-1") == []


class TestDeleteNote:
    def test_scoped_by_id_and_physician(self):
        sb = _table([{"id": "n1"}])
        ok = delete_note(sb, "phys-1", "n1")
        assert ok is True
        calls = sb.table.return_value.eq.call_args_list
        pairs = {(c[0][0], c[0][1]) for c in calls}
        assert ("id", "n1") in pairs
        assert ("physician_id", "phys-1") in pairs  # IDOR guard

    def test_false_on_error(self):
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("db down")
        assert delete_note(sb, "phys-1", "n1") is False


class TestCorrectNote:
    def test_updates_text_and_embedding_scoped(self):
        sb = _table([{"id": "n1"}])
        ok = correct_note(sb, "phys-1", "n1", "the corrected note", [0.4] * 8)
        assert ok is True
        payload = sb.table.return_value.update.call_args[0][0]
        assert payload["note"] == "the corrected note"
        assert payload["embedding"] == [0.4] * 8
        pairs = {(c[0][0], c[0][1]) for c in sb.table.return_value.eq.call_args_list}
        assert ("id", "n1") in pairs and ("physician_id", "phys-1") in pairs

    def test_false_on_error(self):
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("db down")
        assert correct_note(sb, "phys-1", "n1", "x", None) is False
