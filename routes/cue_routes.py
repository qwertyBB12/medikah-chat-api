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

Plan 22-06 — tool loop / Phase-23 — TTFT streaming:
  run_cue_turn_streaming() (services/cue/engine.py) drives the multi-step
  tool_use/tool_result loop, but each round is a single adapter.stream_turn()
  call that yields live text deltas AND the terminal message (tool_use + usage).
  The route forwards each delta to the client immediately as a StreamingResponse
  (AI-SPEC §4b.2) — so Cue starts speaking sentence 1 while the rest is still
  generating, with NO second model round-trip. The opening greeting bypasses the
  loop entirely (adapter.stream(), no tools). Real usage counts (from the
  terminal `done` event) are recorded on the background task; the greeting path
  falls back to the char-length approximation.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import AsyncIterator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from slowapi import Limiter

from db.client import get_supabase
from services.cue.adapter import create_adapter, select_model
from services.cue.engine import run_cue_turn, run_cue_turn_streaming
from services.cue.memory.judge import run_memory_judge
from services.cue.memory.store import (
    load_relevant_notes,
    has_aviso_ack,
    list_notes,
    delete_note,
)
from services.cue.memory.recall import assemble_recall_envelope
from services.cue.memory.embeddings import embed as embed_text
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


def _select_turn_model(*, opening: bool) -> str:
    """Pick the model tier for a turn (diagnosis 2026-06-28).

    The opening greeting is a single warm sentence — no tools, no clinical
    reasoning — so the FAST brain (Haiku) keeps the first impression instant.
    Every CONVERSATIONAL / clinical + tool turn uses the documented reasoning
    brain (Sonnet, the adapter's _DEFAULT_TIER): Haiku — which the adapter
    reserves for background memory/flag judges — under-follows the ~6-7k-token
    clinical persona and fumbles the multi-step scheduling date/tool-arg math,
    which read to doctors as "not intelligent / confused" and mis-booked blocks.
    Sonnet's added input prefill is offset by prompt caching (the route threads
    system_cache_strategy="ephemeral").
    """
    return select_model(tier="haiku" if opening else "sonnet")

# PATCH-03 — current Cue memory aviso de privacidad version. Bump when the notice
# copy materially changes; a new version requires re-acknowledgment before the
# memory judge will write any new note (cross-session memory stays dark until ack).
AVISO_VERSION = "2026-06-28"


class CueChatRequest(BaseModel):
    messages: list[dict]       # [{"role": "user"|"assistant", "content": str}]
    locale: str = "es"         # "en" | "es" — physicians are Spanish-first
    context: str = "workspace" # surface hint for system-prompt builder
    mode: str = "text"         # "text" | "voice" — gates the brevity/no-markdown voice addendum
    max_tokens: int = 1024     # AI-SPEC §4b.3: max_tokens MANDATORY, explicit limit
    opening: bool = False      # Phase 23: brain-generated open-greeting turn


class CueConfirmWriteRequest(BaseModel):
    """Body for POST /cue/calendar/confirm-write (Plan 23-04 — the ONLY mutation).

    physician_id and 'confirmed-ness' come from auth + the route call itself —
    NEVER from this body. Calling the endpoint IS the confirmation (the doctor
    clicked Confirm in the UI).
    """

    action: str                       # "block" | "clear"
    start_iso: str
    end_iso: str
    title: Optional[str] = None       # required for block; ignored for clear
    idempotency_token: str            # per-proposal UUID — dedup key (HANDS-04)
    locale: str = "es"


class CueTtsRequest(BaseModel):
    """Body for POST /cue/tts (Plan 23-05 — VOICE-02/04).

    physician_id comes from auth (CUE-11 — never from body). The voice is
    resolved server-side by the catalog (provider-aware); the client only
    supplies the text + locale.
    """

    text: str
    locale: str = "es"               # "en" | "es" — physicians Spanish-first


# POST /cue/transcribe accepts a raw audio body (no JSON model). Cap at 5MB —
# ample for ~60s of speech and a cheap abuse guard (mirrors BeNeXT transcribe).
_MAX_AUDIO_BYTES = 5 * 1024 * 1024


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


def _request_ip_ua(request: Request) -> tuple[Optional[str], Optional[str]]:
    """Derive the audit IP + user-agent from the ROUTE's OWN Request (HANDS-08a).

    ip = X-Forwarded-For first hop, else request.client.host.
    ua = the User-Agent header.

    NOTE: this is the per-ACTION audit source for the route-level write/revoke
    paths (confirm-write, DELETE /credential). It is distinct from
    _physician_key_func above — that is the IP-ONLY rate-limit KEY func (no UA),
    NOT an audit source. Route audit attribution must use THIS helper.
    """
    xff = request.headers.get("X-Forwarded-For", "")
    ip = xff.split(",")[0].strip() if xff else None
    if not ip:
        client = getattr(request, "client", None)
        ip = getattr(client, "host", None) if client else None
    ua = request.headers.get("user-agent")
    return ip, ua


# ---------------------------------------------------------------------------
# POST /cue/chat — full gate envelope (CUE-04)
# ---------------------------------------------------------------------------


@router.post("/chat")
@limiter.limit("120/minute")  # CUE-04c: per-physician abuse fuse, NOT a usage cap — never throttle a doctor (2026-06-28)
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
    # Cue ALWAYS originates in Spanish (2026-06-28 product call). The opening
    # greeting is forced to Spanish regardless of the incoming locale; from there
    # the language directive in the system prompt mirrors the doctor turn-by-turn,
    # so Cue switches to English the moment the doctor writes/speaks English.
    # FUTURE: derive the origination locale from the doctor's onboarding/preferred
    # language instead of hard-defaulting to Spanish.
    # ------------------------------------------------------------------
    cue_locale = "es" if body.opening else body.locale

    # ------------------------------------------------------------------
    # GATE 5: Context assembly (Plan 22-03 assemble())
    # Scoped to physician_id from session (CUE-11).
    # Slice 2: the last user message drives SEMANTIC recall (the right memories
    # for this moment, not just the newest). The opening greeting has no query →
    # recency fallback inside _build_system_prompt.
    # ------------------------------------------------------------------
    recall_query = None
    if not body.opening and body.messages:
        recall_query = next(
            (m.get("content", "") for m in reversed(body.messages) if m.get("role") == "user"),
            None,
        )
    system_prompt: str = await _build_system_prompt(
        physician_id=physician_id,
        locale=cue_locale,
        supabase=supabase,
        query_text=recall_query,
        mode=body.mode,
    )

    # ------------------------------------------------------------------
    # GATE 6: Model selection (tier-gated; physicians never charged)
    # ------------------------------------------------------------------
    # Tier per turn type (see _select_turn_model): the opening greeting stays on
    # the FAST brain (Haiku) for an instant first impression; every conversational
    # / clinical + tool turn runs on the documented reasoning brain (Sonnet), whose
    # added prefill is offset by prompt caching below. The clinical-deference anchor
    # is baked into the core prompt regardless of model, so scope-of-practice holds;
    # the high-stakes diagnosis surface (Phase 24) selects sonnet/opus via tier.
    model = _select_turn_model(opening=body.opening)

    # ------------------------------------------------------------------
    # Tool loop + stream — Plan 22-06 (CUE-03) + Phase-23 TTFT streaming
    # run_cue_turn_streaming drives the tool_use/tool_result loop via
    # adapter.stream_turn() — a single streamed model call per round that yields
    # live text deltas AND the terminal message (tool_use + usage). The route
    # forwards each delta to the client immediately (Time-To-First-Token), so Cue
    # starts speaking sentence 1 while the rest is still generating, instead of
    # waiting for the whole loop to assemble. No second model round-trip (the
    # double-call trap is avoided — see services/cue/engine.py).
    # ------------------------------------------------------------------
    adapter = create_adapter("anthropic")
    captured: list[str] = []
    usage_totals: dict = {"input_tokens": 0, "output_tokens": 0}

    # Phase 23: opening-turn greeting (brain turn, brevity-default, doctor-centric).
    # The directive is the user turn; the clinical register + compass live in
    # system_prompt. A greeting never proposes a write → no pending_confirm.
    if body.opening:
        address = _resolve_doctor_address(supabase, physician_id)
        if cue_locale == "es":  # opening greeting always originates in Spanish
            directive = (
                "El médico acaba de abrir el espacio de Cue. Salúdalo en UNA sola frase "
                "breve, cálida y concreta. " +
                (f"Dirígete a él o ella como «{address}». " if address else "") +
                "No enumeres funciones. No te describas a ti mismo ni a tu rol: no uses "
                "la palabra «testigo», no te presentes, no hables de tu memoria ni de lo "
                "que recuerdas en abstracto. Sin signos de exclamación. "
                # Anti-fabrication: this opening turn has NOT read any tool. The
                # greeting must not assert live operational state (Issue 1, Dr. José
                # 2026-06-28): no count of pending items, no «consultas administrativas»,
                # no evento del calendario, ni mensaje de la bandeja.
                "No afirmes ningún pendiente, conteo, evento del calendario ni mensaje "
                "de la bandeja: todavía no los has consultado. "
                "Si recuerdas un hilo concreto y específico de una sesión previa — un "
                "caso, una pregunta diferida o un seguimiento con nombre — lidera con ese "
                "hilo en concreto, nunca con la idea de que lo recuerdas. Si no tienes un "
                "hilo concreto, saluda de forma simple, cálida y abierta, sin describirte."
            )
        else:
            directive = (
                "The physician just opened the Cue workspace. Greet them in ONE short, "
                "warm, concrete sentence. " +
                (f"Address them as \"{address}\". " if address else "") +
                "Do not list capabilities. Do not describe yourself or your role: do not "
                "use the word \"witness,\" do not introduce yourself, do not talk about "
                "your memory or what you remember in the abstract. No exclamation marks. "
                # Anti-fabrication: see the Spanish branch — this opening turn reads no
                # tool, so it must not assert live operational state.
                "Do not assert any pending item, count, calendar event, or inbox message "
                "— you have not checked them yet. "
                "If you remember a concrete, specific thread from a prior session — a "
                "case, a deferred question, a named follow-up — lead with that concrete "
                "thread, never with the idea that you remember it. Otherwise greet "
                "simply, warmly, and openly, without describing yourself."
            )
        messages = [{"role": "user", "content": directive}]
    else:
        # Truncate history to last 10 turns (AI-SPEC §4 context strategy).
        messages = body.messages[-_MAX_MESSAGES:]

    async def _token_gen() -> AsyncIterator[bytes]:
        """
        Stream Cue's reply to the client token-by-token (Phase-23 TTFT).

        Opening greeting (body.opening): bypasses the tool loop entirely and
        streams adapter.stream() directly — a greeting never proposes a write and
        never calls a tool, so there is no tool-detection round to pay for. This
        is the biggest, lowest-risk latency win (the first thing every session
        speaks). Usage falls back to the char-length approximation below.

        Conversational turn: consumes run_cue_turn_streaming(), forwarding each
        `delta` event to the client as it arrives (so streamingTts can start
        speaking sentence 1 while the rest generates), then emitting the D-03
        confirm sentinel from the terminal `done` event when present.
        """
        nonlocal usage_totals
        # Perf instrumentation (PERF-INSPECT): log TTFT (time-to-first-token) +
        # total generation ms per turn so Render logs show the brain-leg budget.
        t0 = time.monotonic()
        ttft_logged = False

        def _log_ttft() -> None:
            nonlocal ttft_logged
            if not ttft_logged:
                ttft_logged = True
                logger.info(
                    "[cue][perf] TTFT physician=%s model=%s opening=%s ms=%d",
                    physician_id, model, int(body.opening), int((time.monotonic() - t0) * 1000),
                )

        try:
            # ----- Opening greeting: direct stream, no tool loop -----
            if body.opening:
                async for delta in adapter.stream(
                    model=model,
                    system_prompt=system_prompt,
                    messages=messages,
                    max_tokens=body.max_tokens,
                    # Cache the large static clinical system prefix (TTFT win).
                    system_cache_strategy="ephemeral",
                ):
                    if delta:
                        _log_ttft()
                        captured.append(delta)
                        yield delta.encode("utf-8")
                logger.info("[cue][perf] turn-total physician=%s opening=1 ms=%d",
                            physician_id, int((time.monotonic() - t0) * 1000))
                return

            # ----- Conversational turn: stream the tool loop's deltas -----
            pending_confirm: Optional[dict] = None
            async for ev in run_cue_turn_streaming(
                adapter,
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                physician_id=physician_id,
                locale=body.locale,
                max_tokens=body.max_tokens,
                # Cache the large static clinical system prefix so it is not
                # re-prefilled on every turn AND every tool round (TTFT win;
                # offsets the Sonnet upgrade above).
                system_cache_strategy="ephemeral",
            ):
                etype = ev.get("type")
                if etype == "delta":
                    delta = ev.get("text", "")
                    if delta:
                        _log_ttft()
                        captured.append(delta)
                        yield delta.encode("utf-8")
                elif etype == "tool":
                    # THINKING TRACE (wire-spec v2): a tool-event frame —
                    #   \x1f (US) + compact JSON {phase, tool, [ok], [items]} + \n
                    # Emitted as the agentic loop starts/finishes each tool call so
                    # the client can render cascading terminal-style steps. NOT
                    # spoken text: it is NOT appended to `captured` (the judge text)
                    # and does NOT count toward TTFT (a text metric). None-valued
                    # keys are dropped so 'ok'/'items' appear only when present.
                    frame = b"\x1f" + json.dumps({
                        k: v for k, v in {
                            "phase": ev.get("phase"),
                            "tool": ev.get("tool"),
                            "ok": ev.get("ok"),
                            "items": ev.get("items"),
                        }.items() if v is not None
                    }).encode("utf-8") + b"\n"
                    yield frame
                elif etype == "done":
                    usage_totals = ev.get("usage", usage_totals)
                    pending_confirm = ev.get("pending_confirm")
            logger.info("[cue][perf] turn-total physician=%s ms=%d",
                        physician_id, int((time.monotonic() - t0) * 1000))

            # D-03 surfacing (Plan 23-04): when a block/clear PROPOSER produced a
            # confirm card, emit it AFTER the text as ONE structured sentinel line:
            #   \x1e (RS) + compact JSON {"pending_confirm": {...}} + \n
            # The plain-text path is byte-identical to Phase 22 when None.
            # 23-03/23-06 parse this SAME framing (canonical contract).
            if pending_confirm is not None:
                sentinel = (
                    b"\x1e"
                    + json.dumps({"pending_confirm": pending_confirm}).encode("utf-8")
                    + b"\n"
                )
                yield sentinel
        except Exception as exc:
            logger.error(
                "[cue] run_cue_turn error for physician=%s: %s", physician_id, exc
            )
            # Headers are already on the wire (StreamingResponse defaults to 200),
            # so we cannot fail the status here. Degrade gracefully by streaming a
            # CLEAN spoken message (no brackets/newline — it is read aloud by the
            # voice surface, so it must sound like a sentence). A leading space
            # separates it cleanly if any partial text was already streamed.
            error_chunk = (
                " Cue no pudo completar la respuesta. Intenta de nuevo."
                if body.locale == "es"
                else " Cue could not complete the response. Please try again."
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
                # Phase 25 MEM-02: cross-session memory judge (non-blocking, never-throws).
                # physician_name resolves via the same helper the greeting uses; None on miss.
                try:
                    physician_name = _resolve_doctor_address(supabase, physician_id)
                except Exception:
                    physician_name = None
                await run_memory_judge(
                    supabase,
                    physician_id,
                    {"user": last_user_msg, "assistant": assistant_text},
                    body.locale,
                    physician_name,
                )
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
# POST /cue/calendar/confirm-write — the ONLY calendar mutation path (D-03)
# ---------------------------------------------------------------------------


def _confirm_write_lookup_cached(supabase, physician_id: str, token: str) -> Optional[dict]:
    """Return the cached result_json for (physician_id, idempotency_token), or None.

    Idempotency backstop (HANDS-04): a replayed token returns the cached result
    (one VEVENT, stable uid) instead of writing twice.
    """
    if supabase is None:
        return None
    try:
        res = (
            supabase.table("cue_write_idempotency")
            .select("result_json")
            .eq("physician_id", physician_id)
            .eq("idempotency_token", token)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0].get("result_json")
    except Exception:
        logger.exception(
            "[cue] confirm-write idempotency lookup failed physician=%s", physician_id
        )
    return None


def _confirm_write_store_result(
    supabase, physician_id: str, token: str, result_json: dict
) -> dict:
    """Persist the result with INSERT ... ON CONFLICT DO NOTHING (concurrency backstop).

    If a truly simultaneous second click also missed the lookup, the conflicting
    INSERT no-ops; we re-read and return the now-cached result rather than
    writing twice. Returns the authoritative result (cached on conflict).
    """
    if supabase is None:
        return result_json
    try:
        # supabase-py upsert with ignore_duplicates mirrors INSERT ... ON CONFLICT
        # (physician_id, idempotency_token) DO NOTHING — the composite PK is the
        # concurrency backstop defined in migration 031.
        (
            supabase.table("cue_write_idempotency")
            .upsert(
                {
                    "physician_id": physician_id,
                    "idempotency_token": token,
                    "result_json": result_json,
                },
                on_conflict="physician_id,idempotency_token",
                ignore_duplicates=True,
            )
            .execute()
        )
    except Exception:
        logger.exception(
            "[cue] confirm-write result persist failed physician=%s", physician_id
        )
    # Re-read so a concurrent winner's result is the one returned (never double-write).
    cached = _confirm_write_lookup_cached(supabase, physician_id, token)
    return cached if cached is not None else result_json


def _write_confirm_audit(
    supabase,
    physician_id: str,
    action: str,
    detail: dict,
    *,
    ip: Optional[str],
    ua: Optional[str],
) -> None:
    """Per-action audit row for a confirm-write (HANDS-08a writes path).

    Writes {physician_id, action, range, deleted/skipped count, ip, ua} — the IP
    and UA are derived from the route's OWN Request (this route HAS one). No
    bodies, no secrets.
    """
    if supabase is None:
        return
    row_detail = dict(detail)
    if ip is not None:
        row_detail["ip"] = ip
    if ua is not None:
        row_detail["ua"] = ua
    try:
        supabase.table("workspace_audit_log").insert(
            {
                "physician_id": physician_id,
                "actor_id": physician_id,
                "actor_role": "physician",
                "action": action,
                "resource_type": "cue_hands",
                "resource_id": None,
                "detail": row_detail,
            }
        ).execute()
    except Exception:
        logger.exception(
            "[cue] confirm-write audit insert failed action=%s physician=%s (non-fatal)",
            action,
            physician_id,
        )


@router.post("/calendar/confirm-write")
@limiter.limit("120/minute")  # per-physician abuse fuse, NOT a usage cap (2026-06-28)
async def cue_confirm_write(
    request: Request,
    body: CueConfirmWriteRequest,
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> dict:
    """The ONLY calendar mutation path (D-03). Idempotent + per-action audited.

    Gate envelope: kill-switch → identity FROM auth → origin. physician_id and
    confirmed-ness come from auth+route, NEVER from the body — calling this
    endpoint IS the confirmation (the doctor clicked Confirm in the UI).

    Idempotency (HANDS-04): a replayed (physician_id, idempotency_token) returns
    the cached result (exactly ONE block VEVENT, stable uid) and skips the write.
    """
    supabase = get_supabase()

    # GATE 1: Kill-switch (CUE-04a) — confirm-write IS gated (HANDS-09a).
    kill_status: KillSwitchResult = await check_kill_switch(supabase, body.locale)
    if kill_status == "tripped":
        raise HTTPException(
            status_code=503, detail=bilingual_unavailable(body.locale)
        )

    # GATE 2: Identity — session-derived only (CUE-11 — NEVER from body).
    physician_id: str = auth.physician_id
    request.state._cue_physician_id = physician_id  # noqa: SLF001

    # GATE 3: Origin check (CUE-04d) — state-changing route.
    _check_origin(request)

    action = (body.action or "").strip().lower()
    if action not in ("block", "clear"):
        raise HTTPException(status_code=400, detail="Invalid action")

    # IDEMPOTENCY FIRST (HANDS-04): a replayed token returns the cached result.
    token = body.idempotency_token
    if not token:
        raise HTTPException(status_code=400, detail="Missing idempotency_token")
    cached = _confirm_write_lookup_cached(supabase, physician_id, token)
    if cached is not None:
        logger.info(
            "[cue] confirm-write idempotent replay physician=%s action=%s", physician_id, action
        )
        return cached

    # Resolve the Cue credential (lazy-mint, kill-switch-gated inside the broker).
    from services.cue.credential_broker import get_cue_cred
    from services.cue.tools.executors import _load_workspace_context
    from services.cue import calendar_dav

    mailbox_local_part, verification_status = _load_workspace_context(physician_id)
    if verification_status != "verified" or not mailbox_local_part:
        raise HTTPException(status_code=403, detail="Workspace not connected")

    cred = await get_cue_cred(physician_id, mailbox_local_part)

    ip, ua = _request_ip_ua(request)

    if action == "block":
        title = body.title or "Blocked by Cue"
        uid = await calendar_dav.block_time(
            cred.username,
            cred.password,
            body.start_iso,
            body.end_iso,
            title,
            physician_id=physician_id,
        )
        result: dict = {"blocked": True, "uid": uid}
        _write_confirm_audit(
            supabase,
            physician_id,
            "cue.calendar_block_time",
            {"start_iso": body.start_iso, "end_iso": body.end_iso, "uid": uid},
            ip=ip,
            ua=ua,
        )
    else:  # clear
        cleared = await calendar_dav.clear_range(
            cred.username,
            cred.password,
            body.start_iso,
            body.end_iso,
            physician_id=physician_id,
        )
        result = {"deleted": cleared["deleted"], "skipped": cleared["skipped"]}
        _write_confirm_audit(
            supabase,
            physician_id,
            "cue.calendar_clear_range",
            {
                "start_iso": body.start_iso,
                "end_iso": body.end_iso,
                "deleted": cleared["deleted"],
                "skipped": cleared["skipped"],
            },
            ip=ip,
            ua=ua,
        )

    # Persist the result for idempotent replay (ON CONFLICT DO NOTHING backstop).
    authoritative = _confirm_write_store_result(supabase, physician_id, token, result)
    return authoritative


# ---------------------------------------------------------------------------
# DELETE /cue/credential — Disconnect Cue (HANDS-09 / HANDS-09a)
# NOT fail-closed on the kill-switch: a doctor MUST be able to Disconnect Cue
# DURING a tripped-kill-switch incident. Issuance + confirm-write are gated; revoke is not.
# ---------------------------------------------------------------------------


@router.delete("/credential")
@limiter.limit("60/minute")  # per-physician abuse fuse, NOT a usage cap (2026-06-28)
async def cue_revoke_credential(
    request: Request,
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> dict:
    """Revoke the physician's Cue app-password (HANDS-09 single DELETE).

    Gate: identity FROM auth → origin. The kill-switch is DELIBERATELY NOT
    checked here — revoke must succeed even during a tripped-switch incident so a
    doctor is never trapped with Cue connected. Revoke touches ONLY the Cue
    app-passwd id, NEVER the doctor's mailbox login password. The audit row
    carries IP+UA derived from THIS route's Request (HANDS-08a).
    """
    # GATE: Identity — session-derived only (CUE-11 — NEVER from body).
    physician_id: str = auth.physician_id
    request.state._cue_physician_id = physician_id  # noqa: SLF001

    # GATE: Origin check (CUE-04d) — state-changing route. (No kill-switch gate.)
    _check_origin(request)

    ip, ua = _request_ip_ua(request)

    from services.cue.credential_broker import revoke_cue_credential

    revoked = await revoke_cue_credential(physician_id, ip=ip, ua=ua)
    return {"revoked": bool(revoked)}


# ---------------------------------------------------------------------------
# POST /cue/transcribe — STT (VOICE-08). Cloud (Voxtral) by default; no VPS.
# Gate envelope: kill-switch → identity → origin. Same envelope as /cue/chat.
# The transcript is TRANSIENT — returned to the client, never persisted
# backend (T-23-05-02 / the HANDS-02 no-body-persist rule).
# ---------------------------------------------------------------------------


@router.post("/transcribe")
@limiter.limit("300/minute")  # voice utterances fire often; per-physician abuse fuse only, never throttle a doctor (2026-06-28)
async def cue_transcribe(
    request: Request,
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> dict:
    """Transcribe a posted audio blob → {transcript, language} (auto-detect EN/ES).

    Body is the raw audio bytes (WAV/WebM/mp3). An optional `X-Locale: en|es`
    header hints the language; otherwise the language is auto-detected (VOICE-08).
    """
    supabase = get_supabase()

    # GATE 1: Kill-switch (CUE-04a / PATCH-02 — fail CLOSED). Locale unknown
    # pre-transcribe; default to es for the bilingual unavailable message.
    kill_status: KillSwitchResult = await check_kill_switch(supabase, "es")
    if kill_status == "tripped":
        raise HTTPException(status_code=503, detail=bilingual_unavailable("es"))

    # GATE 2: Identity — session-derived only (CUE-11 — NEVER from body).
    physician_id: str = auth.physician_id
    request.state._cue_physician_id = physician_id  # noqa: SLF001

    # GATE 3: Origin check (CUE-04d).
    _check_origin(request)

    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="Empty audio")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large (max 5MB)")

    locale_hint = (request.headers.get("x-locale") or "").strip().lower() or None
    content_type = request.headers.get("content-type") or "audio/webm"

    from services.cue.voice import whisper_client

    _t_stt = time.monotonic()
    try:
        result = await whisper_client.transcribe(
            audio, language=locale_hint, content_type=content_type
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean 502, never leak audio
        logger.error("[cue] transcribe failed physician=%s: %s", physician_id, exc)
        raise HTTPException(status_code=502, detail="Transcription failed") from exc
    logger.info("[cue][perf] STT physician=%s bytes=%d ms=%d",
                physician_id, len(audio), int((time.monotonic() - _t_stt) * 1000))

    # Transient — returned, never persisted (T-23-05-02).
    return {"transcript": result.get("transcript", ""), "language": result.get("language")}


# ---------------------------------------------------------------------------
# POST /cue/tts — TTS (VOICE-02/04). Voxtral cloud default; F5 dormant.
# Gate envelope: kill-switch → identity → origin. Voice resolved server-side by
# the provider-aware catalog (id is namespace-valid for the selected provider).
# ---------------------------------------------------------------------------


@router.post("/tts")
@limiter.limit("600/minute")  # streaming TTS fires per-sentence; per-physician abuse fuse only, never throttle a doctor (2026-06-28)
async def cue_tts(
    request: Request,
    body: CueTtsRequest,
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> StreamingResponse:
    """Synthesize `text` → audio stream in the doctor's resolved EN/ES voice."""
    supabase = get_supabase()

    # GATE 1: Kill-switch (CUE-04a / PATCH-02 — fail CLOSED).
    kill_status: KillSwitchResult = await check_kill_switch(supabase, body.locale)
    if kill_status == "tripped":
        raise HTTPException(status_code=503, detail=bilingual_unavailable(body.locale))

    # GATE 2: Identity — session-derived only (CUE-11 — NEVER from body).
    physician_id: str = auth.physician_id
    request.state._cue_physician_id = physician_id  # noqa: SLF001

    # GATE 3: Origin check (CUE-04d).
    _check_origin(request)

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    from services.cue.voice.catalog import resolve
    from services.cue.voice.providers import VoiceProviderError, create_tts_provider

    # Resolve voice + provider (provider-aware; never crashes with zero DB rows).
    selection = resolve(physician_id, body.locale, supabase=supabase)
    provider = create_tts_provider(selection["provider"])

    _t_tts = time.monotonic()
    try:
        audio = await provider.synthesize(
            text=text, voice_id=selection["voice_id"], locale=body.locale
        )
    except VoiceProviderError as exc:
        status = 503 if exc.kind == "unauthorized" else 502
        logger.error(
            "[cue] tts failed physician=%s provider=%s kind=%s",
            physician_id,
            selection["provider"],
            exc.kind,
        )
        raise HTTPException(status_code=status, detail="Voice synthesis unavailable") from exc

    logger.info("[cue][perf] TTS physician=%s provider=%s chars=%d ms=%d",
                physician_id, selection["provider"], len(text), int((time.monotonic() - _t_tts) * 1000))

    media_type = "audio/mpeg" if selection["provider"] == "voxtral" else "audio/wav"
    return StreamingResponse(
        iter([audio]),
        media_type=media_type,
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
# Cue memory aviso de privacidad (PATCH-03) — the doctor-level consent gate that
# unlocks cross-session memory writes. LFPDPPP: stated purpose + acknowledgment.
# ---------------------------------------------------------------------------


@router.get("/memory/aviso")
async def get_memory_aviso(
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> dict:
    """Return whether this physician has acknowledged the memory aviso."""
    supabase = get_supabase()
    physician_id: str = auth.physician_id  # CUE-11: identity from session, never body
    return {
        "acknowledged": has_aviso_ack(supabase, physician_id),
        "aviso_version": AVISO_VERSION,
    }


@router.post("/memory/aviso-ack")
async def post_memory_aviso_ack(
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> dict:
    """Record the physician's acknowledgment of the memory aviso (PATCH-03 unlock)."""
    supabase = get_supabase()
    physician_id: str = auth.physician_id  # CUE-11
    if supabase is not None:
        try:
            supabase.table("cue_memory_consent").upsert({
                "physician_id": physician_id,
                "aviso_version": AVISO_VERSION,
            }).execute()
        except Exception as exc:
            logger.error("[cue-memory] aviso-ack upsert failed for %s: %s", physician_id, exc)
            raise HTTPException(status_code=500, detail="Could not record acknowledgment")
    return {"acknowledged": True}


# ---------------------------------------------------------------------------
# Doctor-visible / editable memory (Slice 3 — MEM transparency + data rights).
# Everything is scoped to auth.physician_id (CUE-11); the doctor can see, correct,
# delete, and export exactly what Cue remembers about them — the LFPDPPP posture.
# ---------------------------------------------------------------------------


@router.get("/memory")
async def get_memory_notes(
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> dict:
    """List everything Cue remembers about this physician (also serves export)."""
    supabase = get_supabase()
    return {"notes": list_notes(supabase, auth.physician_id)}


@router.delete("/memory/{note_id}")
async def delete_memory_note(
    note_id: str,
    auth: AuthenticatedPhysician = Depends(authenticated_physician),
) -> dict:
    """Forget one note. Scoped to the physician (a doctor can only delete their own)."""
    supabase = get_supabase()
    ok = delete_note(supabase, auth.physician_id, note_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Could not delete the note")
    return {"deleted": True}


# NOTE: there is deliberately NO edit/PATCH endpoint. Doctors can view and DELETE
# their notes, but never rewrite one — a rewritten note would silently change how
# Cue reasons, with effects the doctor cannot see (product decision 2026-06-28).
# Delete is a privacy right; edit authority is withheld.


# ---------------------------------------------------------------------------
# Context assembly helper (Phase 22 — calls Plan 22-03 assemble())
# ---------------------------------------------------------------------------


def _resolve_doctor_address(supabase, physician_id: str) -> str:
    """Return the spoken address: "Doctor X" / "Doctora X" / last-or-first name / "".

    Honorific from physician_workspace_accounts.title (Dr/Dra/NULL); name from
    physicians.full_name. NULL title or no record → name-only fallback (never
    mis-title). All reads scoped to the session-derived physician_id (CUE-11).
    """
    if supabase is None:
        return ""
    try:
        wa = (
            supabase.table("physician_workspace_accounts")
            .select("title").eq("physician_id", physician_id).limit(1).execute()
        )
        title = (wa.data[0].get("title") if getattr(wa, "data", None) else None)
        ph = (
            supabase.table("physicians")
            .select("full_name").eq("id", physician_id).limit(1).execute()
        )
        full_name = ((ph.data[0].get("full_name") if getattr(ph, "data", None) else "") or "").strip()
        last = full_name.split()[-1] if full_name else ""
        honorific = {"Dr": "Doctor", "Dra": "Doctora"}.get(title or "", "")
        if honorific and last:
            return f"{honorific} {last}"
        return last  # name-only fallback (may be "")
    except Exception:
        logger.exception("[cue] address resolve failed physician=%s", physician_id)
        return ""


# Launch market default. SOGo stores each physician's calendar in their own
# timezone (e.g. America/Mexico_City for Mexico). We resolve "today"/"tomorrow"
# in this zone so calendar_read_day queries the right day.
_DEFAULT_CUE_TIMEZONE = "America/Mexico_City"


def _build_date_directive(locale: str, tz_name: str = _DEFAULT_CUE_TIMEZONE) -> str:
    """Inject the current date so the model can resolve relative dates.

    Without this, the model has no idea what 'today' is and guesses (often its
    training-cutoff year), so calendar_read_day('tomorrow') queries the wrong
    day and returns "no events". See the Aguirre 2026-06-27 calendar bug.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        from datetime import timezone as _tz
        now = datetime.now(_tz.utc)
        tz_name = "UTC"

    iso = now.strftime("%Y-%m-%d")
    pretty = now.strftime("%A, %B %d, %Y")
    hhmm = now.strftime("%H:%M")

    if locale == "es":
        return (
            f"\n\nFecha y hora actual: {pretty}, {hhmm} ({tz_name}). "
            f"Hoy es {iso}. Cuando el médico diga 'hoy', usa esta fecha; "
            f"'mañana' es el día siguiente y 'ayer' el anterior. "
            f"Pasa siempre las fechas a las herramientas de calendario en formato "
            f"YYYY-MM-DD resueltas a partir de esta referencia."
        )
    return (
        f"\n\nCurrent date and time: {pretty}, {hhmm} ({tz_name}). "
        f"Today is {iso}. When the physician says 'today', use this date; "
        f"'tomorrow' is the next calendar day and 'yesterday' the previous one. "
        f"Always pass dates to calendar tools as YYYY-MM-DD resolved from this reference."
    )


async def _build_system_prompt(
    physician_id: str,
    locale: str,
    supabase,
    query_text: str | None = None,
    mode: str = "text",
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
        # assemble() is a SYNCHRONOUS pure prompt-builder (locale + surface only).
        # It does NOT accept physician_id/supabase and is NOT awaitable — the old
        # `await assemble(..., physician_id=..., supabase=...)` raised TypeError on
        # EVERY turn, silently dropping to the generic English fallback below
        # (which is why Cue was English-only and said "How can I help?" — a phrase
        # the real clinical core explicitly forbids).
        prompt = assemble(locale=locale, surface="workspace", mode=mode)
        # Phase 25 MEM-01: prepend the cross-session recall envelope (fail-open).
        # Recall is gated on the SAME aviso as writes — no consent means no notes
        # exist anyway, so we skip the embedding API call entirely (avoids paying
        # an embed per turn for every doctor until the consent UI ships).
        # Slice 2: embed the query (fail-open) for SEMANTIC recall — the right
        # memories for this moment; load_relevant_notes falls back to recency when
        # there is no query or nothing is embedded yet. Never raises.
        if has_aviso_ack(supabase, physician_id):
            query_embedding = await embed_text(query_text) if query_text else None
            notes = load_relevant_notes(supabase, physician_id, query_embedding, limit=10)
            if notes:
                recall = assemble_recall_envelope(notes, locale)
                prompt = recall + "\n\n" + prompt
        return prompt + _build_date_directive(locale)
    except Exception as exc:
        logger.error(
            "[cue] assemble() failed for physician=%s locale=%s — using fallback prompt: %s",
            physician_id,
            locale,
            exc,
        )
        fallback = _FALLBACK_PROMPT_ES if locale == "es" else _FALLBACK_PROMPT_EN
        return fallback + _build_date_directive(locale)
