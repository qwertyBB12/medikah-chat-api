"""tests/cue/memory/test_read_seam.py — recall envelope prepended; semantic when a query is present."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import routes.cue_routes as cr


@pytest.mark.asyncio
async def test_recall_prepended_when_notes_exist():
    notes = [{"note": "The doctor is preparing the CDMX launch",
              "appended_at": "2026-06-27T10:00:00Z", "category": "project"}]
    with patch.object(cr, "load_relevant_notes", return_value=notes):
        prompt = await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
        )
    assert "<cue-session-recall>" in prompt
    assert "preparing the CDMX launch" in prompt
    # recall comes BEFORE the clinical core's language directive
    assert prompt.index("<cue-session-recall>") < prompt.index("LANGUAGE DIRECTIVE")


@pytest.mark.asyncio
async def test_no_envelope_when_no_notes():
    with patch.object(cr, "load_relevant_notes", return_value=[]):
        prompt = await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
        )
    assert "<cue-session-recall>" not in prompt


@pytest.mark.asyncio
async def test_query_text_drives_semantic_recall():
    """When a query is present, embed it and pass the vector to load_relevant_notes."""
    captured = {}

    def _capture(supabase, physician_id, query_embedding, limit=10):
        captured["embedding"] = query_embedding
        return []

    with patch.object(cr, "embed_text", AsyncMock(return_value=[0.5] * 8)), \
         patch.object(cr, "load_relevant_notes", side_effect=_capture):
        await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
            query_text="what's on my calendar for the launch",
        )
    assert captured["embedding"] == [0.5] * 8


@pytest.mark.asyncio
async def test_no_query_uses_recency_no_embed():
    """No query (opening greeting) → embed is not called; recency path (None embedding)."""
    with patch.object(cr, "embed_text", AsyncMock()) as emb, \
         patch.object(cr, "load_relevant_notes", return_value=[]) as lrn:
        await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(), query_text=None,
        )
    emb.assert_not_called()
    assert lrn.call_args[0][2] is None  # query_embedding is None → recency fallback
