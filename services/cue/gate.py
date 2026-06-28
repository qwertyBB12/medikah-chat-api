"""
services/cue/gate.py
--------------------
Request-gate helpers for the /cue envelope (CUE-04a/04b/06/11; PATCH-02).

Kill-switch (check_kill_switch)
    Reads `cue_feature_flags` for `cue:kill_switch`.
    Returns "tripped" when value is 'soft' or 'hard'.
    Returns "tripped" (fail CLOSED) on ANY exception reading the flag store
    — this is the explicit PATCH-02 reversal of BeNeXT chat.ts:100-101,
    which `continue`s on KV error (log the error at ERROR level and fail CLOSED).
    Returns "ok" ONLY when value is NULL and the read succeeded.

Budget check (budget_check)
    Reads `cue_usage_daily` for today's token consumption by a physician.
    Physicians are NEVER charged — caps gate quota/quality only (CUE-06).
    Returns (exceeded: bool, used_input: int, used_output: int, cap: int).

Usage tracking (record_usage)
    Calls the `increment_cue_usage` Supabase RPC.
    Called on the BackgroundTasks path after streaming (CUE-04b — non-blocking).
    physician_id ALWAYS comes from the verified session, never from the request body (CUE-11).

Daily caps by tier (CUE-06)
    'physician' tier: 200,000 input + 50,000 output tokens per UTC day
    'trial' tier:     20,000 input  +  5,000 output tokens per UTC day
    Physicians are NEVER charged; these caps exist to bound runaway usage.
"""

from __future__ import annotations

import logging
from typing import Literal, NamedTuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier daily caps (CUE-06 — physicians never charged; caps gate quota only)
# ---------------------------------------------------------------------------

# 2026-06-28 (launch day): the prior caps (physician 200k in / 50k out) throttled
# real demo use to a few dozen turns — Cue resends the full clinical system prompt
# as input EVERY turn, and since the launch-eve history-threading change each turn
# also carries up to 20 prior messages, so per-turn input is large. Verified
# physicians hit the daily 429 mid-event ("worked, then just stopped"). Per the
# product call, Cue must NEVER tell a doctor "daily limit reached." These caps are
# set effectively-unlimited for any human; the numbers remain only as a runaway-bug
# backstop (e.g. a tool loop gone wrong), NOT a usage quota. Physicians are never
# charged (CUE-06). Revisit only if abuse appears; do not lower to a human-reachable
# value.
_TIER_CAPS: dict[str, dict[str, int]] = {
    "physician": {"input": 100_000_000, "output": 25_000_000},
    "trial":     {"input":  50_000_000, "output": 12_500_000},
}

_DEFAULT_TIER = "physician"

# ---------------------------------------------------------------------------
# Kill-switch gate (CUE-04a / PATCH-02 — FAILS CLOSED)
# ---------------------------------------------------------------------------

KillSwitchResult = Literal["ok", "tripped"]


async def check_kill_switch(supabase, locale: str = "es") -> KillSwitchResult:
    """
    Read `cue_feature_flags` for the `cue:kill_switch` key.

    Returns "tripped" when:
      - The row value is 'soft' or 'hard' (admin has tripped the switch).
      - ANY exception is raised reading the flag store (PATCH-02 fail-CLOSED).

    Returns "ok" ONLY when the read succeeds AND value is NULL.

    PATCH-02 CRITICAL: BeNeXT chat.ts:100-101 wraps the KV read in a try/catch
    and `continue`s (fails OPEN) on error.  This is the explicit fix — on any
    exception, log at ERROR level and return "tripped" (503), never continue.
    """
    if supabase is None:
        # No flag store available — fail CLOSED per PATCH-02.
        logger.error(
            "[cue] kill-switch: supabase client is None — failing CLOSED (PATCH-02)"
        )
        return "tripped"

    try:
        result = (
            supabase.table("cue_feature_flags")
            .select("value")
            .eq("key", "cue:kill_switch")
            .maybe_single()
            .execute()
        )
        if result.data and result.data.get("value") in ("soft", "hard"):
            logger.warning(
                "[cue] kill-switch tripped (value=%r)", result.data.get("value")
            )
            return "tripped"
        # NULL value or no row → switch is OFF
        return "ok"
    except Exception as exc:
        # PATCH-02: fail CLOSED — ANY flag-store error means we cannot
        # confirm the switch is not tripped; safer to refuse than to serve.
        logger.error(
            "[cue] kill-switch flag store unreachable — failing CLOSED (PATCH-02): %s",
            exc,
        )
        return "tripped"


def bilingual_unavailable(locale: str) -> str:
    """Return the bilingual unavailability message for tripped/503 responses."""
    if locale == "es":
        return "Cue no está disponible en este momento. Intenta más tarde."
    return "Cue is unavailable right now. Try again later."


# ---------------------------------------------------------------------------
# Budget check (CUE-06)
# ---------------------------------------------------------------------------


class BudgetStatus(NamedTuple):
    exceeded: bool
    used_input: int
    used_output: int
    cap_input: int
    cap_output: int


async def budget_check(
    supabase,
    physician_id: str,
    tier: str = _DEFAULT_TIER,
) -> BudgetStatus:
    """
    Read today's token usage for `physician_id` from `cue_usage_daily`.
    Returns a BudgetStatus indicating whether either cap is exceeded.

    physician_id MUST be session-derived by the gate (CUE-11 — never client-supplied).

    On any Supabase error, returns not-exceeded (fail open for budget —
    the kill-switch gate above is the hard safety; budget is a quota control).
    Physicians are NEVER charged (CUE-06).
    """
    caps = _TIER_CAPS.get(tier, _TIER_CAPS[_DEFAULT_TIER])
    cap_input  = caps["input"]
    cap_output = caps["output"]

    if supabase is None:
        logger.warning(
            "[cue] budget_check: supabase not available — skipping quota check for %s",
            physician_id,
        )
        return BudgetStatus(
            exceeded=False,
            used_input=0,
            used_output=0,
            cap_input=cap_input,
            cap_output=cap_output,
        )

    try:
        result = (
            supabase.table("cue_usage_daily")
            .select("input_tokens, output_tokens")
            .eq("physician_id", physician_id)
            .eq("usage_date", _today_utc())
            .maybe_single()
            .execute()
        )
        if result.data:
            used_input  = int(result.data.get("input_tokens",  0))
            used_output = int(result.data.get("output_tokens", 0))
        else:
            used_input  = 0
            used_output = 0

        exceeded = (used_input >= cap_input) or (used_output >= cap_output)
        return BudgetStatus(
            exceeded=exceeded,
            used_input=used_input,
            used_output=used_output,
            cap_input=cap_input,
            cap_output=cap_output,
        )
    except Exception as exc:
        # Budget check failure → log and allow the request (quota control,
        # not a safety gate like the kill-switch).
        logger.error(
            "[cue] budget_check failed for physician=%s tier=%s — allowing request: %s",
            physician_id,
            tier,
            exc,
        )
        return BudgetStatus(
            exceeded=False,
            used_input=0,
            used_output=0,
            cap_input=cap_input,
            cap_output=cap_output,
        )


def _today_utc() -> str:
    """Return today's UTC date as an ISO 8601 string (YYYY-MM-DD)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Usage recording (CUE-04b / CUE-06 — called on BackgroundTasks path)
# ---------------------------------------------------------------------------


async def record_usage(
    supabase,
    physician_id: str,
    input_tokens: int,
    output_tokens: int,
    tier: str = _DEFAULT_TIER,
) -> None:
    """
    Call the `increment_cue_usage` Supabase RPC to record token consumption.

    Called by BackgroundTasks AFTER the streaming response has been sent
    (CUE-04b — never blocks the streamed response).

    physician_id MUST be session-derived by the gate (CUE-11).

    Any exception is logged and swallowed — a usage-tracking failure
    must never surface a 500 to the physician (CUE-04b requirement).
    """
    if supabase is None:
        logger.warning(
            "[cue] record_usage: supabase not available — usage not tracked for %s",
            physician_id,
        )
        return

    try:
        supabase.rpc(
            "increment_cue_usage",
            {
                "p_physician_id": physician_id,
                "p_input":        int(input_tokens),
                "p_output":       int(output_tokens),
                "p_tier":         tier,
            },
        ).execute()
        logger.debug(
            "[cue] record_usage: physician=%s in=%d out=%d tier=%s",
            physician_id,
            input_tokens,
            output_tokens,
            tier,
        )
    except Exception as exc:
        # CUE-04b: swallow — a background tracking error must not surface.
        logger.error(
            "[cue] record_usage failed for physician=%s — usage not tracked: %s",
            physician_id,
            exc,
        )
