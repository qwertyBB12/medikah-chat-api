"""tests/cue/memory/test_read_seam.py — recall: aviso-gated, semantic when a query is present."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import routes.cue_routes as cr


@pytest.mark.asyncio
async def test_recall_prepended_when_notes_exist():
    notes = [{"note": "The doctor is preparing the CDMX launch",
              "appended_at": "2026-06-27T10:00:00Z", "category": "project"}]
    with patch.object(cr, "has_aviso_ack", return_value=True), \
         patch.object(cr, "load_relevant_notes", return_value=notes):
        prompt = await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
        )
    assert "<cue-session-recall>" in prompt
    assert "preparing the CDMX launch" in prompt
    assert prompt.index("<cue-session-recall>") < prompt.index("LANGUAGE DIRECTIVE")


@pytest.mark.asyncio
async def test_no_envelope_when_no_notes():
    with patch.object(cr, "has_aviso_ack", return_value=True), \
         patch.object(cr, "load_relevant_notes", return_value=[]):
        prompt = await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
        )
    assert "<cue-session-recall>" not in prompt


@pytest.mark.asyncio
async def test_no_aviso_skips_recall_and_embed():
    """No consent → no recall attempt and no embedding API call."""
    with patch.object(cr, "has_aviso_ack", return_value=False), \
         patch.object(cr, "embed_text", AsyncMock()) as emb, \
         patch.object(cr, "load_relevant_notes") as lrn:
        prompt = await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
            query_text="something",
        )
    emb.assert_not_called()
    lrn.assert_not_called()
    assert "<cue-session-recall>" not in prompt


@pytest.mark.asyncio
async def test_query_text_drives_semantic_recall():
    """When acked and a query is present, embed it and pass the vector to recall."""
    captured = {}

    def _capture(supabase, physician_id, query_embedding, limit=10):
        captured["embedding"] = query_embedding
        return []

    with patch.object(cr, "has_aviso_ack", return_value=True), \
         patch.object(cr, "embed_text", AsyncMock(return_value=[0.5] * 8)), \
         patch.object(cr, "load_relevant_notes", side_effect=_capture):
        await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
            query_text="what's on my calendar for the launch",
        )
    assert captured["embedding"] == [0.5] * 8


@pytest.mark.asyncio
async def test_no_query_uses_recency_no_embed():
    """Acked but no query (opening greeting) → embed not called; recency (None embedding)."""
    with patch.object(cr, "has_aviso_ack", return_value=True), \
         patch.object(cr, "embed_text", AsyncMock()) as emb, \
         patch.object(cr, "load_relevant_notes", return_value=[]) as lrn:
        await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(), query_text=None,
        )
    emb.assert_not_called()
    assert lrn.call_args[0][2] is None
