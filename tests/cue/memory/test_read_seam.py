"""tests/cue/memory/test_read_seam.py — recall envelope is prepended when notes exist."""
from unittest.mock import MagicMock, patch

import pytest

import routes.cue_routes as cr


@pytest.mark.asyncio
async def test_recall_prepended_when_notes_exist():
    notes = [{"note": "The doctor is preparing the CDMX launch",
              "appended_at": "2026-06-27T10:00:00Z", "category": "project"}]
    with patch.object(cr, "load_recent_notes", return_value=notes):
        prompt = await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
        )
    assert "<cue-session-recall>" in prompt
    assert "preparing the CDMX launch" in prompt
    # recall comes BEFORE the clinical core's language directive
    assert prompt.index("<cue-session-recall>") < prompt.index("LANGUAGE DIRECTIVE")


@pytest.mark.asyncio
async def test_no_envelope_when_no_notes():
    with patch.object(cr, "load_recent_notes", return_value=[]):
        prompt = await cr._build_system_prompt(
            physician_id="phys-1", locale="en", supabase=MagicMock(),
        )
    assert "<cue-session-recall>" not in prompt
