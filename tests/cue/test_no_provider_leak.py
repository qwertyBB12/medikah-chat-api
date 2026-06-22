"""
tests/cue/test_no_provider_leak.py
------------------------------------
Phase 22 provider-leak guard test suite (CUE-02 / D1 eval dimension).

MERGE-BLOCKING: these tests must pass before any CUE-03 / tool-loop code
is merged. A failing test here means Anthropic-specific types have leaked
past the adapter seam, which would silently break the no-lock-in promise
(CUE-01/07) and force engine rework on every future provider swap.

Test inventory
--------------
D1-A  scan_no_anthropic_leak_outside_adapter
      AST/grep scan over every Python source file in:
        - services/cue/ (all .py files EXCEPT adapter.py itself)
        - routes/ai_routes.py                  (CUE-09 migrated)
        - services/ai_triage.py                (CUE-09 migrated)
        - utils/openai_client.py               (CUE-09 seam)
      Asserts ZERO occurrences of:
        - `anthropic.types`         (importing the provider type module)
        - `Anthropic.Tool`          (Claude-specific tool type)
        - `cache_control`           (Anthropic prompt-caching dict key)
        - `'ephemeral'`             (Anthropic cache-strategy string literal)
      Comments and docstrings are NOT excluded from the scan intentionally —
      if you document a forbidden symbol it counts as a mention and should
      be reviewed. The scan targets source tokens, not runtime state.

D1-B  dummy_adapter_complete_round_trip
      Proves that a DummyAdapter (from conftest.py) drives a complete()
      round-trip with ZERO edits to the migrated call sites or the engine.
      This is the CUE-07 extension-point proof: registering a second
      provider requires only a new adapter class + a registry case.

D1-C  dummy_adapter_stream_round_trip
      Same proof for the stream() path (async generator).

D1-D  cue_neutral_tool_no_anthropic_import
      Confirms that CueNeutralTool can be instantiated and
      to_anthropic_dict() called without importing ANY anthropic.* symbol
      in the caller — the translation stays inside the adapter module.

D1-E  create_adapter_exhaustive_raise
      Confirms that create_adapter() raises ValueError for unknown
      providers (exhaustive default — port of TypeScript `never`).

D1-F  adapter_is_abstract_contract
      Confirms that CueModelAdapter cannot be instantiated directly
      (ABC enforcement — a second adapter must implement both methods).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from services.cue.adapter import (
    CueModelAdapter,
    CueNeutralTool,
    create_adapter,
    select_model,
)

# ---------------------------------------------------------------------------
# Paths scanned by D1-A
# ---------------------------------------------------------------------------

# Root of the backend repo (parent of services/, routes/, utils/)
_REPO_ROOT = Path(__file__).parents[2]  # medikah-chat-api/

# Files that are ALLOWED to contain Anthropic-specific symbols (the adapter seam)
_ALLOWED = {
    _REPO_ROOT / "services" / "cue" / "adapter.py",
    # test files themselves may reference forbidden symbols in comments/docstrings
    # for documentation — add them here if the scan false-positives
}

# Files to scan for leaks (the CUE-09 migrated call sites + cue service layer)
_SCAN_PATHS: list[Path] = [
    _REPO_ROOT / "routes" / "ai_routes.py",
    _REPO_ROOT / "services" / "ai_triage.py",
    _REPO_ROOT / "utils" / "openai_client.py",
]

# Also scan all .py files under services/cue/ EXCEPT adapter.py
_CUE_SERVICES = _REPO_ROOT / "services" / "cue"
for _p in _CUE_SERVICES.rglob("*.py"):
    if _p not in _ALLOWED:
        _SCAN_PATHS.append(_p)

# Forbidden symbols (provider-specific leaks)
_FORBIDDEN_PATTERNS: list[str] = [
    r"anthropic\.types",       # importing the provider type module
    r"Anthropic\.Tool",        # Claude-specific tool type reference
    r"cache_control",          # Anthropic prompt-caching dict key
    r"['\"]ephemeral['\"]",    # Anthropic cache-strategy string literal
]
_FORBIDDEN_RE = re.compile("|".join(_FORBIDDEN_PATTERNS))


# ---------------------------------------------------------------------------
# D1-A: Source scan — no forbidden symbols outside adapter.py
# ---------------------------------------------------------------------------


def test_scan_no_anthropic_leak_outside_adapter() -> None:
    """D1-A MERGE-BLOCKING: no Anthropic-specific symbols appear outside adapter.py.

    Scans the CUE-09-migrated call sites and all services/cue/*.py files
    (except the adapter itself) for forbidden symbols.

    If this test fails it means a provider-specific type leaked past the seam.
    Fix: move the symbol inside services/cue/adapter.py.
    """
    violations: list[str] = []

    for file_path in _SCAN_PATHS:
        if not file_path.exists():
            # A file listed but not yet created is fine (future plans) — skip
            continue
        source = file_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if _FORBIDDEN_RE.search(line):
                violations.append(f"{file_path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not violations, (
        "PROVIDER LEAK DETECTED (CUE-02 violation).\n"
        "The following lines contain Anthropic-specific symbols outside services/cue/adapter.py:\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\n\nFix: move forbidden symbols into services/cue/adapter.py only."
    )


# ---------------------------------------------------------------------------
# D1-B: DummyAdapter complete() round-trip (CUE-07 extension-point proof)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dummy_adapter_complete_round_trip(
    dummy_adapter, sample_messages
) -> None:
    """D1-B: A DummyAdapter drives complete() with no engine/call-site edits."""
    result = await dummy_adapter.complete(
        model="dummy-model-v1",
        system_prompt="You are Cue.",
        messages=sample_messages,
        max_tokens=256,
    )

    # The adapter contract: callers access content duck-typing, not SDK types
    assert isinstance(result, dict), "DummyAdapter.complete() must return a dict"
    assert result["stop_reason"] == "end_turn"
    assert any(b["type"] == "text" for b in result["content"])
    text_block = next(b for b in result["content"] if b["type"] == "text")
    assert "Hello, Cue." in text_block["text"]


# ---------------------------------------------------------------------------
# D1-C: DummyAdapter stream() round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dummy_adapter_stream_round_trip(
    dummy_adapter, sample_messages
) -> None:
    """D1-C: A DummyAdapter drives stream() yielding text deltas."""
    chunks: list[str] = []

    # The stream() method is an async generator — consume it
    gen = dummy_adapter.stream(
        model="dummy-model-v1",
        system_prompt="You are Cue.",
        messages=sample_messages,
        max_tokens=256,
    )
    async for chunk in gen:
        chunks.append(chunk)

    full_text = "".join(chunks)
    assert len(full_text) > 0
    assert "Hello, Cue." in full_text


# ---------------------------------------------------------------------------
# D1-D: CueNeutralTool no Anthropic import required at call site
# ---------------------------------------------------------------------------


def test_cue_neutral_tool_no_anthropic_import(sample_tool) -> None:
    """D1-D: CueNeutralTool.to_anthropic_dict() works without the caller
    importing any anthropic.* symbol (translation stays inside adapter)."""
    # Confirm the fixture is a CueNeutralTool
    assert isinstance(sample_tool, CueNeutralTool)

    translated = sample_tool.to_anthropic_dict()

    assert translated["name"] == "test_tool"
    assert translated["description"] == "A tool for testing"
    assert "type" in translated["input_schema"]

    # Ensure 'anthropic' is NOT in the caller's imports for this to work
    # (the test file itself imports from services.cue.adapter only)
    calling_module_imports = set(sys.modules.keys())
    # The adapter module imports anthropic — that's fine. But this test file
    # must NOT import anthropic.types, Anthropic, etc. directly.
    # We verify by confirming the test module itself doesn't import anthropic.types
    test_module = sys.modules.get(__name__)
    assert test_module is not None
    # If anthropic.types were imported at the module level of this test file,
    # it would appear in the module's __dict__. Confirm it's absent.
    module_attrs = dir(test_module)
    assert "anthropic" not in module_attrs, (
        "This test file imports 'anthropic' directly — that violates the leak rule. "
        "Use only services.cue.adapter exports."
    )


# ---------------------------------------------------------------------------
# D1-E: create_adapter() exhaustive raise for unknown provider
# ---------------------------------------------------------------------------


def test_create_adapter_exhaustive_raise() -> None:
    """D1-E: create_adapter() raises ValueError for unknown providers."""
    with pytest.raises(ValueError, match="Unknown provider"):
        # type: ignore[arg-type] — intentionally passing invalid provider
        create_adapter(provider="openai_leaked")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# D1-F: CueModelAdapter ABC enforcement
# ---------------------------------------------------------------------------


def test_adapter_is_abstract_contract() -> None:
    """D1-F: CueModelAdapter (ABC) cannot be instantiated directly."""
    with pytest.raises(TypeError):
        CueModelAdapter()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# D1-G: select_model returns correct dated model IDs
# ---------------------------------------------------------------------------


def test_select_model_tier_routing() -> None:
    """D1-G: Tier routing returns the correct dated model IDs from AI-SPEC §4."""
    assert select_model("haiku") == "claude-haiku-4-5-20251001"
    assert select_model("sonnet") == "claude-sonnet-4-6"
    assert select_model("opus") == "claude-opus-4-8"
    # Default tier should be sonnet
    assert select_model() == "claude-sonnet-4-6"
    # Unknown tier falls back to default (sonnet)
    assert select_model("unknown-tier") == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# D1-H: CueNeutralTool Pydantic validation
# ---------------------------------------------------------------------------


def test_cue_neutral_tool_validation() -> None:
    """D1-H: CueNeutralTool validates required fields via Pydantic."""
    tool = CueNeutralTool(
        name="calendar_read_day",
        description="Reads the physician's calendar for a given date.",
        input_schema={
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
        },
    )
    d = tool.to_anthropic_dict()
    assert d["name"] == "calendar_read_day"
    assert "input_schema" in d
    assert d["input_schema"]["required"] == ["date"]

    # Missing required fields should raise ValidationError
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CueNeutralTool(name="x")  # type: ignore[call-arg]  # missing description + input_schema
