"""tests/cue/memory/test_judge.py — MEM-02 memory judge (gate, redact, persist, never-throw)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cue.memory.judge import build_judge_prompt, run_memory_judge


class TestJudgePrompt:
    def test_pins_physician_name(self):
        p = build_judge_prompt("Aguirre")
        assert "Aguirre" in p

    def test_forbids_hector_for_unknown(self):
        p = build_judge_prompt(None)
        assert "Hector" in p  # the guard names the forbidden value
        assert "the doctor" in p


def _anthropic_returning(judgement: dict):
    """Mock AsyncAnthropic whose messages.create returns a Haiku-shaped text block."""
    client = MagicMock()
    msg = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(judgement)
    msg.content = [block]
    client.messages.create = AsyncMock(return_value=msg)
    return client


@pytest.mark.asyncio
class TestRunMemoryJudge:
    async def test_skips_when_no_aviso(self):
        sb = MagicMock()
        with patch("services.cue.memory.judge.has_aviso_ack", return_value=False), \
             patch("services.cue.memory.judge.insert_note") as ins:
            await run_memory_judge(sb, "phys-1", {"user": "hi", "assistant": "hello"}, "en", "Aguirre")
            ins.assert_not_called()

    async def test_keeps_and_redacts_salient_turn(self):
        sb = MagicMock()
        judgement = {"kept": True, "summary": "The doctor will follow up with Sr. Juan Pérez", "category": "follow_up"}
        with patch("services.cue.memory.judge.has_aviso_ack", return_value=True), \
             patch("services.cue.memory.judge._judge_client", return_value=_anthropic_returning(judgement)), \
             patch("services.cue.memory.judge.insert_note") as ins:
            await run_memory_judge(sb, "phys-1", {"user": "x", "assistant": "y"}, "en", "Aguirre")
            ins.assert_called_once()
            stored_note = ins.call_args[0][2]
            assert "Juan Pérez" not in stored_note   # PATCH-01 applied
            assert "[paciente]" in stored_note

    async def test_drops_small_talk(self):
        sb = MagicMock()
        judgement = {"kept": False, "summary": None, "category": None}
        with patch("services.cue.memory.judge.has_aviso_ack", return_value=True), \
             patch("services.cue.memory.judge._judge_client", return_value=_anthropic_returning(judgement)), \
             patch("services.cue.memory.judge.insert_note") as ins:
            await run_memory_judge(sb, "phys-1", {"user": "thanks", "assistant": "de nada"}, "es", "Aguirre")
            ins.assert_not_called()

    async def test_never_raises_on_judge_error(self):
        sb = MagicMock()
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("anthropic down"))
        with patch("services.cue.memory.judge.has_aviso_ack", return_value=True), \
             patch("services.cue.memory.judge._judge_client", return_value=client), \
             patch("services.cue.memory.judge.insert_note") as ins:
            await run_memory_judge(sb, "phys-1", {"user": "x", "assistant": "y"}, "en", "Aguirre")
            ins.assert_not_called()  # no crash, no insert
