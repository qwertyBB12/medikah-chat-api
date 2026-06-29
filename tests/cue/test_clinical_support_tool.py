"""
tests/cue/test_clinical_support_tool.py (file kept as test_diagnosis_tool.py path)
-----------------------------------------------------------------------------------
Phase 24 — Cue clinical DECISION-SUPPORT surface.

NAMING / LEGAL (Hector, 2026-06-29): a doctor-support tool. NOTHING is named or
framed as an "(official) diagnosis" — the tool returns ranked clinical
CONSIDERATIONS for the physician to weigh. The only "diagnosis" token allowed is
the disclaimer's explicit DENIAL ("not a diagnosis").

These tests pin the CORRECTED design (Hector, 2026-06-29):
  - clinical_decision_support is a NORMAL Cue tool (NOT a terminal confirm card).
  - Its tool_result is ADDITIVELY surfaced to the UI as a structured `card`
    event AND the loop CONTINUES so Cue narrates a walkthrough and the doctor can
    keep conversing (the confirm path is unchanged — it still STOPS the loop).
  - The model is fed the readable `summary` prose (not raw JSON) as the
    tool_result so its walkthrough reads naturally.

Slice 1 (backend) inventory
---------------------------
DX-A  shared service parse: parse_support_response → structured dicts + flags
DX-B  disclaimer constant is decision-support framed + bilingual
DX-C  generate_clinical_support returns {considerations, red_flags, disclaimer, summary}
DX-D  clinical_decision_support tool registered (presentation required; NO identity arg)
DX-E  executor returns a {kind:'clinical_support', ...} JSON card payload
DX-F  executor declares physician_id as an explicit kwarg (CUE-11 IDOR)
DX-G  dispatch_tool routes clinical_decision_support (foreign physician_id ignored)
DX-H  engine emits an additive `card` event, CONTINUES the loop, feeds back summary
"""

from __future__ import annotations

import inspect
import json

import pytest
import pytest_asyncio  # noqa: F401  (registers the asyncio marker, mirrors sibling tests)

from tests.cue.test_tool_loop import ToolUseAdapter


# A representative raw response exactly in the format the system prompt asks the
# model to produce (numbered conditions + Rationale/Distinguishing lines, then a
# Red Flags section).
SAMPLE_RAW = """\
1. **Acute viral pharyngitis** (HIGH)
   Rationale: Sore throat with low-grade fever and absence of tonsillar exudate suggests a viral etiology.
   Distinguishing factors: Centor criteria; rapid strep antigen to rule out bacterial cause.
2. **Streptococcal pharyngitis** (MODERATE)
   Rationale: Fever with tonsillar findings can indicate group A streptococcus.
   Distinguishing factors: Positive rapid antigen test or throat culture.

Red Flags:
- Difficulty breathing or drooling (possible epiglottitis)
- Unilateral severe swelling (possible peritonsillar abscess)
"""


# ---------------------------------------------------------------------------
# DX-A: shared service parse
# ---------------------------------------------------------------------------


def test_parse_support_response_extracts_structured_considerations() -> None:
    """DX-A: parse_support_response returns structured consideration dicts + flags."""
    from services.cue.clinical_support import parse_support_response

    considerations, red_flags = parse_support_response(SAMPLE_RAW)

    assert len(considerations) == 2
    assert considerations[0]["condition"] == "Acute viral pharyngitis"
    assert considerations[0]["confidence"] == "HIGH"
    assert considerations[1]["condition"] == "Streptococcal pharyngitis"
    assert considerations[1]["confidence"] == "MODERATE"
    # Each consideration is a plain JSON-serializable dict (for the card payload).
    for item in considerations:
        assert set(item) == {"condition", "rationale", "confidence", "distinguishing_factors"}

    assert len(red_flags) == 2
    assert any("epiglottitis" in f for f in red_flags)


# ---------------------------------------------------------------------------
# DX-B: disclaimer constant
# ---------------------------------------------------------------------------


def test_disclaimer_is_decision_support_and_bilingual() -> None:
    """DX-B: the on-card disclaimer DENIES being a diagnosis, in EN + ES."""
    from services.cue.clinical_support import CLINICAL_SUPPORT_DISCLAIMER

    low = CLINICAL_SUPPORT_DISCLAIMER.lower()
    assert "not a diagnosis" in low          # EN decision-support denial
    assert "no es un diagnóstico" in low      # ES decision-support denial


# ---------------------------------------------------------------------------
# DX-C: generate_clinical_support shape (anthropic_complete stubbed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_clinical_support_returns_structured(monkeypatch) -> None:
    """DX-C: generate_clinical_support returns structured card fields + summary prose."""
    import services.cue.clinical_support as cs

    async def fake_complete(**kwargs):
        return SAMPLE_RAW

    monkeypatch.setattr(cs, "anthropic_complete", fake_complete)

    result = await cs.generate_clinical_support("sore throat with fever for 3 days")

    assert set(result) >= {"considerations", "red_flags", "disclaimer", "summary"}
    assert len(result["considerations"]) == 2
    assert result["disclaimer"] == cs.CLINICAL_SUPPORT_DISCLAIMER
    # `summary` is the readable LLM prose fed back to the model for its walkthrough.
    assert result["summary"] == SAMPLE_RAW


@pytest.mark.asyncio
async def test_generate_clinical_support_raises_when_unconfigured(monkeypatch) -> None:
    """DX-C2: a None completion (provider unconfigured) raises ClinicalSupportUnavailable."""
    import services.cue.clinical_support as cs

    async def none_complete(**kwargs):
        return None

    monkeypatch.setattr(cs, "anthropic_complete", none_complete)

    with pytest.raises(cs.ClinicalSupportUnavailable):
        await cs.generate_clinical_support("any presentation")


# ---------------------------------------------------------------------------
# DX-D: tool registration + structural IDOR guard
# ---------------------------------------------------------------------------


def test_clinical_decision_support_tool_registered() -> None:
    """DX-D: clinical_decision_support is in NEUTRAL_TOOLS with a required presentation arg.

    NOTE: the tool name must NOT contain 'diagnosis' (legal framing — Hector 2026-06-29).
    """
    from services.cue.tools.registry import NEUTRAL_TOOLS

    tool = next((t for t in NEUTRAL_TOOLS if t.name == "clinical_decision_support"), None)
    assert tool is not None, "clinical_decision_support must be registered in NEUTRAL_TOOLS."
    assert "diagnos" not in tool.name.lower(), "the tool must NOT be named after 'diagnosis'."

    props = tool.input_schema.get("properties") or {}
    required = tool.input_schema.get("required") or []
    assert "presentation" in props
    assert "presentation" in required

    # CUE-11 structural IDOR guard — no identity arg exists for the model to fill.
    assert "physician_id" not in props
    assert "slug" not in props


# ---------------------------------------------------------------------------
# DX-E: executor returns a clinical-support card payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_returns_clinical_support_card_payload(monkeypatch) -> None:
    """DX-E: the executor returns a {kind:'clinical_support', ...} JSON card with a summary."""
    import services.cue.clinical_support as cs
    import services.cue.tools.executors as ex

    async def fake_generate(presentation, age_range=None, sex=None):
        return {
            "considerations": [
                {"condition": "X", "rationale": "r", "confidence": "HIGH", "distinguishing_factors": "d"}
            ],
            "red_flags": ["flag"],
            "disclaimer": cs.CLINICAL_SUPPORT_DISCLAIMER,
            "summary": "PROSE",
        }

    monkeypatch.setattr(ex, "generate_clinical_support", fake_generate)

    out = await ex.clinical_decision_support(physician_id="p1", presentation="a case")
    payload = json.loads(out)

    assert payload["kind"] == "clinical_support"
    assert payload["considerations"][0]["condition"] == "X"
    assert payload["red_flags"] == ["flag"]
    assert payload["disclaimer"] == cs.CLINICAL_SUPPORT_DISCLAIMER
    assert payload["summary"] == "PROSE"


# ---------------------------------------------------------------------------
# DX-F: executor signature IDOR discipline
# ---------------------------------------------------------------------------


def test_executor_declares_physician_id_kwarg() -> None:
    """DX-F: clinical_decision_support accepts physician_id (dispatcher) + presentation."""
    from services.cue.tools.executors import clinical_decision_support

    params = dict(inspect.signature(clinical_decision_support).parameters)
    assert "physician_id" in params
    assert "presentation" in params


# ---------------------------------------------------------------------------
# DX-G: dispatch routing (foreign physician_id stripped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_routes_clinical_decision_support(monkeypatch) -> None:
    """DX-G: dispatch_tool routes clinical_decision_support; a foreign physician_id is ignored."""
    import services.cue.clinical_support as cs
    import services.cue.tools.executors as ex

    async def fake_generate(presentation, age_range=None, sex=None):
        return {
            "considerations": [],
            "red_flags": [],
            "disclaimer": cs.CLINICAL_SUPPORT_DISCLAIMER,
            "summary": "s",
        }

    monkeypatch.setattr(ex, "generate_clinical_support", fake_generate)

    from services.cue.tools.registry import dispatch_tool

    out = await dispatch_tool(
        tool_name="clinical_decision_support",
        tool_input={"presentation": "a case", "physician_id": "foreign-xyz"},  # foreign id stripped
        physician_id="session-physician",
    )
    payload = json.loads(out)
    assert payload["kind"] == "clinical_support"


# ---------------------------------------------------------------------------
# DX-H: engine surfaces an additive card AND continues the loop (NOT terminal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clinical_support_tool_emits_card_and_continues_loop(monkeypatch) -> None:
    """DX-H: a clinical_decision_support tool_use emits an additive `card` event, the loop
    CONTINUES to a narration turn (unlike the confirm card), and the model is fed the
    readable summary prose (not the JSON) as the tool_result.
    """
    import services.cue.clinical_support as cs
    import services.cue.tools.executors as ex

    async def fake_generate(presentation, age_range=None, sex=None):
        return {
            "considerations": [
                {"condition": "X", "rationale": "r", "confidence": "HIGH", "distinguishing_factors": "d"}
            ],
            "red_flags": ["flag"],
            "disclaimer": cs.CLINICAL_SUPPORT_DISCLAIMER,
            "summary": "SUMMARY-PROSE",
        }

    monkeypatch.setattr(ex, "generate_clinical_support", fake_generate)

    from services.cue.engine import run_cue_turn_streaming

    adapter = ToolUseAdapter(
        rounds_with_tools=[
            [("clinical_decision_support", {"presentation": "sore throat, fever"}, "tool-cs-1")],
            # round 1: end_turn — Cue's spoken/text walkthrough.
        ],
        final_text="Here's a quick walkthrough of the considerations...",
    )

    events = []
    async for ev in run_cue_turn_streaming(
        adapter,
        model="test-model",
        system_prompt="Test.",
        messages=[{"role": "user", "content": "clinical decision support for sore throat, fever"}],
        physician_id="p1",
        max_tokens=512,
    ):
        events.append(ev)

    # An additive structured card event was emitted.
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1, "exactly one clinical-support card event must be emitted"
    card = cards[0]["card"]
    assert card["kind"] == "clinical_support"
    assert card["considerations"][0]["condition"] == "X"
    assert card["red_flags"] == ["flag"]
    assert card["disclaimer"] == cs.CLINICAL_SUPPORT_DISCLAIMER
    # The card payload is the clean structured surface — no readable-prose summary on it.
    assert "summary" not in card

    # The loop CONTINUED to the narration turn (NOT a terminal stop): two model calls.
    assert adapter._call_count == 2, (
        "the clinical-support card is additive — the loop must continue so Cue narrates "
        "a walkthrough (confirm cards stop the loop; support cards do not)."
    )

    done = [e for e in events if e.get("type") == "done"][-1]
    assert done["final_text"] == "Here's a quick walkthrough of the considerations..."
    assert done.get("pending_confirm") is None

    # The model was fed the readable summary prose (not the JSON) so it can narrate.
    round1_messages = adapter.captured_messages[1]
    tool_reply = round1_messages[-1]
    assert tool_reply["role"] == "user"
    tr_block = tool_reply["content"][0]
    assert tr_block["type"] == "tool_result"
    assert tr_block["content"] == "SUMMARY-PROSE", (
        "the model must receive the readable summary as the tool_result, not raw JSON."
    )


# ---------------------------------------------------------------------------
# DX-I: route forwards the engine `card` event as a \x1e {"card": {...}} sentinel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_emits_clinical_support_card_sentinel(monkeypatch) -> None:
    """DX-I: /cue/chat forwards a {"type":"card"} engine event as a NON-TERMINAL
    \\x1d (GS) sentinel carrying the structured card, and the narration after it
    stays on the wire (the card is additive, not the terminal \\x1e confirm tail).
    """
    import routes.cue_routes as cue_mod
    from tests.cue.test_tool_event_frames import _run_chat_raw, _stub_route_gates

    async def _fake_stream(*args, **kwargs):
        yield {"type": "delta", "text": "Un momento. "}
        yield {"type": "tool", "phase": "start", "tool": "clinical_decision_support"}
        yield {"type": "tool", "phase": "end", "tool": "clinical_decision_support", "ok": True}
        yield {
            "type": "card",
            "card": {
                "kind": "clinical_support",
                "considerations": [
                    {"condition": "X", "rationale": "r", "confidence": "HIGH", "distinguishing_factors": "d"}
                ],
                "red_flags": ["flag"],
                "disclaimer": "Clinical decision support only — not a diagnosis.",
            },
        }
        yield {"type": "delta", "text": "Aquí están las consideraciones."}
        yield {
            "type": "done",
            "final_text": "Un momento. Aquí están las consideraciones.",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "pending_confirm": None,
        }

    monkeypatch.setattr(cue_mod, "run_cue_turn_streaming", _fake_stream)
    _stub_route_gates(monkeypatch)

    raw = await _run_chat_raw(
        {"messages": [{"role": "user", "content": "decision support for sore throat"}], "locale": "es"}
    )

    # The \x1d (GS) card sentinel is on the wire, well-formed, carrying the structured
    # card — and it is NON-terminal, so the \x1e confirm byte must NOT appear here.
    assert b"\x1d" in raw
    assert b"\x1e" not in raw, "the additive card must NOT use the terminal \\x1e confirm byte"
    card_payload = None
    for chunk in raw.split(b"\x1d"):
        if b"\n" in chunk:
            head = chunk.split(b"\n", 1)[0]
            try:
                obj = json.loads(head.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict) and "card" in obj:
                card_payload = obj["card"]
                break
    assert card_payload is not None, 'the route must emit a \\x1d {"card": {...}} sentinel'
    assert card_payload["kind"] == "clinical_support"
    assert card_payload["considerations"][0]["condition"] == "X"
    assert "not a diagnosis" in card_payload["disclaimer"].lower()

    # The spoken narration after the card stays on the wire (unframed).
    assert "Aquí están las consideraciones.".encode("utf-8") in raw
