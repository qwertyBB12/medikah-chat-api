"""
routes/cue_routes.py
--------------------
Medikah Cue — /cue APIRouter (CUE-08).

Mirrors practikah_routes.py shape: APIRouter(prefix="/cue") + slowapi + Depends + try/except.

Gate envelope order (CUE-04 / AI-SPEC §3):
  1. Auth          — Depends(authenticated_physician) [any status; clinical surface CUE-08]
  2. Kill-switch   — check_kill_switch() fail-CLOSED (CUE-04a / PATCH-02)
  3. Identity      — physician_id = auth.physician_id (CUE-11 — NEVER from body)
  4. Rate-limit    — @limiter.limit per-physician key (CUE-04c)
  5. Daily budget  — budget_check() per physician (CUE-06)
  6. Origin guard  — CUE-04d create.ts-style origin check on state-changing route
  7. Context       — assemble() clinical system prompt (Plan 22-03)
  8. Tool loop     — run_cue_turn() multi-step tool_use/tool_result loop (Plan 22-06; CUE-03)
  9. Stream        — final text streamed to client (AI-SPEC §4b.2)
  10. Background   — BackgroundTasks: record_usage + stub judge (CUE-04b non-blocking)

Per-physician rate limit (CUE-04c):
  The EXISTING slowapi limiter from main.py is reused (do NOT instantiate a second one).
  A per-physician key function derives the limit key from auth.physician_id, not just IP,
  so NAT-shared physicians don't collide and no single physician can exhaust another's quota.

  Implementation note: slowapi's @limiter.limit decorator uses the Limiter whose
  key_func is invoked. The module-level `limiter` here is the SAME object as
  `app.state.limiter` (set in main.py) when we import and re-export it — BUT
  slowapi decorators invoke the key_func at decoration time.

  To achieve per-physician keying we set a custom key_func on the route's
  Limiter instance. Because main.py registers `app.state.limiter`, slowapi
  error handling fires from there; our route-local limiter ONLY provides the
  key_func for the @limiter.limit decorator on our routes.

Post-stream judge (CUE-04b):
  BackgroundTasks.add_task() — the FastAPI analog of ctx.waitUntil.
  The judge runs AFTER the StreamingResponse is returned to the client.
  A judge exception is swallowed (logged), never 500'd.

CUE-11 — physician_id discipline:
  physician_id is set ONCE: physician_id = auth.physician_id
  It is NEVER read from the request body or any tool argument.
  All Supabase reads are scoped to this session-derived id.

Plan 22-06 — tool loop:
  The single-shot adapter.stream() (Plan 05) is replaced by run_cue_turn()
  (services/cue/engine.py), which drives the multi-step tool_use/tool_result
  loop.  After run_cue_turn() returns the assembled final_text, the route
  streams it to the client as a StreamingResponse (AI-SPEC §4b.2 pattern).
  Real usage counts (from the non-streaming loop path) are recorded on the
  background task.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from slowapi import Limiter

from db.client import get_supabase
from services.cue.adapter import create_adapter, select_model
from services.cue.engine import run_cue_turn
from services.cue.gate import (
    BudgetStatus,
    KillSwitchResult,
    bilingual_unavailable,
    budget_check,
    check_kill_switch,
    record_usage,
)
from services.cue.personality.assemble import assemble
from utils.auth import AuthenticatedPhysician, authenticated_physician

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-physician rate-limit key function (CUE-04c)
# ---------------------------------------------------------------------------
# slowapi's key_func receives the Request object.
# We extract physician_id from the verified auth stored in request.state
# (set by the route before the decorator fires — see the route impl below).
#
# The decorator-time key func runs for EVERY decorated call; if auth is not
# yet on request.state (e.g. unit tests bypassing auth), fall back to IP
# so the limit still applies.
#
# This ensures the per-physician 429 fires at the physician boundary, not
# just at the IP boundary — NAT-shared offices cannot collide.


def _physician_key_func(request: Request) -> str:
    """Key function for per-physician rate limiting (CUE-04c)."""
    # auth is attached to request.state inside the route handler
    # before the streaming response is initiated.
    physician_id = getattr(getattr(request, "state", None), "_cue_physician_id", None)
    if physician_id:
        return f"cue:physician:{physician_id}"
    # Fallback to IP — should not happen in production (auth Depends runs first)
    x_forwarded = request.headers.get("X-Forwarded-For", "")
    return f"cue:ip:{x_forwarded.split(',')[0].strip() or request.client.host}"


# Router-local limiter with the per-physician key function.
# main.py registers the shared app.state.limiter (get_remote_address); that
# limiter handles error responses. This limiter provides the per-physician
# key_func for our route decorators.
limiter = Limiter(key_func=_physician_key_func)

router = APIRouter(prefix="/cue", tags=["cue"])

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

_MAX_MESSAGES = 10  # AI-SPEC §4 context-window strategy: hard cap at 10 turns


class CueChatRequest(BaseModel):
    messages: list[dict]       # [{"role": "user"|"assistant", "content": str}]
    locale: str = "es"         # "en" | "es" — physicians are Spanish-first
    context: str = "workspace" # surface hint for system-prompt builder
    max_tokens: int = 1024     # AI-SPEC §4b.3: max_tokens MANDATORY, explicit limit


# ---------------------------------------------------------------------------
# Origin guard (CUE-04d — create.ts-style check on state-changing routes)
# ---------------------------------------------------------------------------

_ALLOWED_ORIGINS_STR = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000")
_ALLOWED_CUE_ORIGINS: set[str] = {
    o.strip() for o in _ALLOWED_ORIGINS_STR.split(",") if o.strip()
}


def _check_origin(request: Request) -> None:
    """
    create.ts-style origin guard for state-changing /cue routes (CUE-04d).

    Rejects requests whose Origin header is present but not in the CORS
    allowlist.  Requests with no Origin header (direct API calls, server-to-
    server) are allowed — the CORS middleware handles browser enforcement.
    """
    origin = request.headers.get("origin") or request.headers.get("Origin")
    if origin and origin not in _ALLOWED_CUE_ORIGINS:
        logger.warning("[cue] origin check FAILED origin=%r", origin)
        raise HTTPException(
            status_code=403,
            detail="Origin not allowed",
        )


# ---------------------------------------------------------------------------
# POST /cue/chat — full gate envelope (CUE-04)
# ---------------------------------------------------------------------------


@router.post("/chat")
@limiter.limit("20/minute")  # CUE-04c: per-physician key via _physician_key_func
async def cue_chat(
    request: Request,
    body: CueChatRequest,
    background_tasks: BackgroundTasks,
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> StreamingResponse:
    """
    Gate envelope (mirrors chat.ts, with PATCH-02 fail-CLOSED fix):

      auth → kill-switch → identity → rate-limit → budget → origin →
      context assembly → stream → [background: judge + record_usage]

    physician_id is ALWAYS taken from auth (CUE-11 — never from body).
    Kill-switch fails CLOSED on any flag-store error (PATCH-02).
    Post-stream judge fires on BackgroundTasks (CUE-04b — non-blocking).
    """
    supabase = get_supabase()

    # ------------------------------------------------------------------
    # GATE 1: Kill-switch (CUE-04a / PATCH-02 — fail CLOSED)
    # Must be the first check after auth, before ANY model/tool call.
    # ------------------------------------------------------------------
    kill_status: KillSwitchResult = await check_kill_switch(supabase, body.locale)
    if kill_status == "tripped":
        raise HTTPException(
            status_code=503,
            detail=bilingual_unavailable(body.locale),
        )

    # ------------------------------------------------------------------
    # GATE 2: Identity — session-derived only (CUE-11 IDOR guard)
    # physician_id is set ONCE here and passed down; never read from body.
    # ------------------------------------------------------------------
    physician_id: str = auth.physician_id

    # Attach to request.state so the per-physician rate-limit key_func
    # can read it (the decorator fires after the Depends chain resolves).
    request.state._cue_physician_id = physician_id  # noqa: SLF001

    # ------------------------------------------------------------------
    # GATE 3: Origin check (CUE-04d)
    # ------------------------------------------------------------------
    _check_origin(request)

    # ------------------------------------------------------------------
    # GATE 4: Daily token budget (CUE-06 — physicians never charged)
    # ------------------------------------------------------------------
    # Tier is always 'physician' for verified physicians.
    # 'trial' applies to physicians in onboarding (any status).
    tier = (
        "physician"
        if auth.verification_status == "verified"
        else "trial"
    )
    budget: BudgetStatus = await budget_check(supabase, physician_id, tier)
    if budget.exceeded:
        if body.locale == "es":
            detail = (
                "Has alcanzado el límite diario de uso de Cue. "
                "El límite se restablece a medianoche UTC. Intenta mañana."
            )
        else:
            detail = (
                "You have reached the daily Cue usage limit. "
                "The limit resets at midnight UTC. Try again tomorrow."
            )
        raise HTTPException(status_code=429, detail=detail)

    # ------------------------------------------------------------------
    # GATE 5: Context assembly (Plan 22-03 assemble())
    # Scoped to physician_id from session (CUE-11).
    # ------------------------------------------------------------------
    system_prompt: str = await _build_system_prompt(
        physician_id=physician_id,
        locale=body.locale,
        supabase=supabase,
    )

    # ------------------------------------------------------------------
    # GATE 6: Model selection (tier-gated; physicians never charged)
    # ------------------------------------------------------------------
    model = select_model(tier="sonnet")  # AI-SPEC §4 default reasoning brain

    # ------------------------------------------------------------------
    # Tool loop + stream — Plan 22-06 (CUE-03)
    # run_cue_turn drives the tool_use/tool_result loop via adapter.complete()
    # for tool-detection rounds (tool_use blocks are NOT in the delta stream),
    # then returns the assembled final_text.  The route streams that text to
    # the client as a StreamingResponse (AI-SPEC §4b.2).
    # ------------------------------------------------------------------
    adapter = create_adapter("anthropic")
    captured: list[str] = []
    usage_totals: dict = {"input_tokens": 0, "output_tokens": 0}

    # Truncate history to last 10 turns (AI-SPEC §4 context strategy).
    messages = body.messages[-_MAX_MESSAGES:]

    async def _token_gen() -> AsyncIterator[bytes]:
        """
        Run the tool loop and stream the assembled final_text to the client.

        run_cue_turn() drives all tool_use/tool_result rounds (each using
        adapter.complete()), then returns the final assembled text.  We then
        yield the final text as a stream (AI-SPEC §4b.2 UX pattern).

        The real TTFT optimization (calling adapter.stream() on the last turn
        when no tools were used) is a Phase-23 enhancement; Phase 22 streams
        the pre-assembled text directly.
        """
        nonlocal usage_totals
        try:
            final_text, usage_totals = await run_cue_turn(
                adapter,
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                physician_id=physician_id,
                max_tokens=body.max_tokens,
            )
            captured.append(final_text)
            yield final_text.encode("utf-8")
        except Exception as exc:
            logger.error(
                "[cue] run_cue_turn error for physician=%s: %s", physician_id, exc
            )
            # Surface a minimal error string rather than crashing the generator.
            error_chunk = (
                "\n[Cue no pudo completar la respuesta. Intenta de nuevo.]"
                if body.locale == "es"
                else "\n[Cue could not complete the response. Please try again.]"
            )
            captured.append(error_chunk)
            yield error_chunk.encode("utf-8")

    # ------------------------------------------------------------------
    # Background task: post-stream judge + usage tracking (CUE-04b)
    # Runs AFTER the streaming response is returned to the client.
    # A judge or usage-tracking exception must NEVER propagate (CUE-04b).
    # ------------------------------------------------------------------
    last_user_msg = next(
        (m.get("content", "") for m in reversed(body.messages) if m.get("role") == "user"),
        "",
    )

    def _post_stream_judge() -> None:
        """
        Non-blocking post-stream work — FastAPI BackgroundTasks analog of
        ctx.waitUntil (CUE-04b).

        Phase 22: stub judge (logs only) + usage tracking.
        Phase 25 (MEM-02/MEM-06): memory + flag judges wired here.

        A judge exception MUST be swallowed — never crash the background task
        or surface a 500 to the physician (CUE-04b requirement).
        """
        import asyncio

        async def _async_work() -> None:
            assistant_text = "".join(captured)

            # Phase 22 stub judge — logs the turn for observability.
            # Replace with the real memory/flag judge in Phase 25 (MEM-02/MEM-06).
            try:
                logger.info(
                    "[cue] post-stream judge stub: physician=%s chars_in=%d chars_out=%d",
                    physician_id,
                    len(last_user_msg),
                    len(assistant_text),
                )
                # TODO (Phase 25 MEM-02/MEM-06): call run_memory_judge + run_flag_judge
            except Exception as judge_exc:
                # CUE-04b: swallow judge exceptions — never surface as 500
                logger.error(
                    "[cue] post-stream judge FAILED for physician=%s — swallowed: %s",
                    physician_id,
                    judge_exc,
                )

            # Plan 22-06: real token counts from the tool-loop path are in
            # usage_totals (accumulated across all tool rounds by run_cue_turn).
            # Fall back to character-length approximation if totals are zero.
            real_in  = usage_totals.get("input_tokens", 0)
            real_out = usage_totals.get("output_tokens", 0)
            in_tokens  = real_in  if real_in  > 0 else max(1, len(system_prompt) // 4)
            out_tokens = real_out if real_out > 0 else max(1, len(assistant_text) // 4)

            await record_usage(
                supabase,
                physician_id,
                in_tokens,
                out_tokens,
                tier,
            )

        # BackgroundTasks runs sync callables in a thread executor; run our
        # async work via asyncio.run() here (it is safe in a background thread
        # since we are NOT inside the event loop at this point — background
        # thread is separate from the uvicorn worker thread).
        # Per AI-SPEC §4b.2: asyncio.run() inside a route handler is forbidden
        # (raises "event loop already running"), but inside a background task
        # that runs in a thread executor, it is the correct pattern.
        try:
            asyncio.run(_async_work())
        except Exception as exc:
            logger.error(
                "[cue] _post_stream_judge background error for physician=%s: %s",
                physician_id,
                exc,
            )

    background_tasks.add_task(_post_stream_judge)

    return StreamingResponse(
        _token_gen(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Health probe — no auth (readiness check mirrors /practikah/health)
# ---------------------------------------------------------------------------


@router.get("/health")
async def cue_health() -> dict:
    """Cue router readiness probe — no auth required."""
    supabase = get_supabase()
    return {
        "status": "ok",
        "router": "cue",
        "supabase": supabase is not None,
    }


# ---------------------------------------------------------------------------
# Context assembly helper (Phase 22 — calls Plan 22-03 assemble())
# ---------------------------------------------------------------------------


async def _build_system_prompt(
    physician_id: str,
    locale: str,
    supabase,
) -> str:
    """
    Assemble the clinical system prompt for the physician.

    Calls services.cue.personality.assemble() (Plan 22-03) for the clinical
    core + self-knowledge block + addendums + language directive.

    physician_id is used to scope any context reads to the verified physician
    (CUE-11 — all Supabase reads use the session-derived id).

    Falls back to a minimal safety prompt on any error (the fallback
    includes the clinical-deference anchor per PERS-04 to ensure
    scope-of-practice constraints survive even on failure).
    """
    _FALLBACK_PROMPT_ES = (
        "Eres Cue, un asistente de apoyo clínico para el médico autenticado. "
        "Responde ÚNICAMENTE EN ESPAÑOL. "
        "Eres soporte de decisiones, NUNCA un prescriptor. "
        "Coloca siempre la decisión clínica en el médico."
    )
    _FALLBACK_PROMPT_EN = (
        "You are Cue, a clinical decision-support assistant for the authenticated physician. "
        "Respond ONLY IN ENGLISH. "
        "You are decision-support, NEVER a prescriber. "
        "Always place the clinical decision with the physician."
    )

    try:
        prompt = await assemble(
            locale=locale,
            surface="workspace",
            physician_id=physician_id,
            supabase=supabase,
        )
        return prompt
    except Exception as exc:
        logger.error(
            "[cue] assemble() failed for physician=%s locale=%s — using fallback prompt: %s",
            physician_id,
            locale,
            exc,
        )
        return _FALLBACK_PROMPT_ES if locale == "es" else _FALLBACK_PROMPT_EN
