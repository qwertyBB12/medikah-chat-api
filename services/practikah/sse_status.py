"""SSE stream generator for the Pro upgrade saga (Phase 13-07).

Streams Server-Sent Events (SSE) for the doctor-facing Vercel-deploy-style
stepped checklist UX (D-16, 3-minute live UX). Polls ``provisioning_runs``
and ``practikah_provisioning_log`` once per second, emitting one event per
newly-observed log row plus a terminal event when the run reaches a final
state.

Wire format (newline-terminated SSE):

    data: {"event":"step.requested","step":"pro.register_domain","ts":...}\n\n
    data: {"event":"step.succeeded","step":"pro.register_domain","ts":...}\n\n
    : ping\n\n
    data: {"event":"run.succeeded","run_id":"...","domain":"..."}\n\n

Headers expected on the FastAPI ``StreamingResponse``:
- ``Content-Type: text/event-stream``
- ``Cache-Control: no-cache, no-transform``
- ``Connection: keep-alive``
- ``X-Accel-Buffering: no``  (defeats Netlify edge buffering — D-16)

Owner-only auth: the FastAPI handler MUST verify ``physician_id`` ownership
before opening the stream. This generator does NOT re-check ownership.

Per D-15: when the saga lands in ``partial_finish_later`` (post-POR retry
state), the UI shows a warm bilingual finish-later message — the stream
emits ``run.partial_finish_later`` and closes.

Threat T-13-07-02: payloads contain ONLY public-shape data (step name,
event, public domain) — never Stripe customer/session/subscription IDs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# Heartbeat cadence — must beat any intermediary buffer flush window
# (Netlify edge ~30s, Render ~60s). 15s is a safe lower bound. (D-16)
HEARTBEAT_SEC = 15

# Poll cadence for new log rows. Saga emits ~7 step transitions across ~3min
# so 1s polls produce smooth Vercel-style checkmark animation without
# saturating the DB.
POLL_INTERVAL_SEC = 1.0

# Hard cap: saga is ~3min normal, ~5min worst case. 5min absolute ceiling
# protects against runaway connections (T-13-07-03 DoS mitigation).
MAX_DURATION_SEC = 300

# Saga states that close the stream. Mirrors ``provisioning_runs.status``
# CHECK constraint in supabase/migrations/021_provisioning_runs.sql.
TERMINAL_STATUSES: set[str] = {"succeeded", "failed", "partial_finish_later"}


def _frame_data(payload: dict) -> bytes:
    """Encode a payload as one SSE ``data:`` frame."""
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


async def stream_run_status(db, run_id: str, physician_id: str) -> AsyncIterator[bytes]:
    """Yield SSE-framed bytes until the saga reaches a terminal state.

    The generator polls two tables:

    - ``provisioning_runs`` — to detect terminal status (succeeded / failed /
      partial_finish_later) and close the stream.
    - ``practikah_provisioning_log`` — to emit one ``data:`` frame per
      newly-observed log row. ``id`` deduplication ensures replays on
      reconnect don't double-emit (T-13-07-06 idempotent UI updates).

    Heartbeat ``: ping\\n\\n`` lines flush intermediary buffers (Netlify edge,
    Render) every ``HEARTBEAT_SEC`` seconds.

    Args:
        db: Supabase client (service-role; caller has already enforced the
            owner-only check before opening the stream).
        run_id: provisioning_runs.run_id (UUID string)
        physician_id: only used for log correlation; ownership is enforced
            by the route handler.

    Yields:
        bytes: SSE-framed messages (data lines and heartbeats).
    """
    seen_log_ids: set[str] = set()
    last_heartbeat = time.time()
    started = time.time()

    while time.time() - started < MAX_DURATION_SEC:
        # ---- Run status fetch -----------------------------------------------
        try:
            run_resp = (
                db.table("provisioning_runs")
                .select("status, domain_name, error")
                .eq("run_id", run_id)
                .single()
                .execute()
            )
            run = run_resp.data
        except Exception:
            logger.exception(
                "[sse_status] provisioning_runs fetch failed run_id=%s", run_id
            )
            run = None

        if not run:
            yield _frame_data({"event": "run.not_found"})
            return

        # ---- New log rows ---------------------------------------------------
        try:
            log_resp = (
                db.table("practikah_provisioning_log")
                .select("id, step_name, event, detail, recorded_at")
                .eq("run_id", run_id)
                .order("recorded_at", desc=False)
                .execute()
            )
            logs = log_resp.data or []
        except Exception:
            logger.exception(
                "[sse_status] practikah_provisioning_log fetch failed run_id=%s",
                run_id,
            )
            logs = []

        for log_row in logs:
            row_id = log_row.get("id")
            if not row_id or row_id in seen_log_ids:
                continue
            seen_log_ids.add(row_id)
            yield _frame_data(
                {
                    "event": f"step.{log_row.get('event')}",
                    "step": log_row.get("step_name"),
                    "detail": log_row.get("detail"),
                    "ts": log_row.get("recorded_at"),
                }
            )

        # ---- Heartbeat ------------------------------------------------------
        if time.time() - last_heartbeat >= HEARTBEAT_SEC:
            yield b": ping\n\n"
            last_heartbeat = time.time()

        # ---- Terminal state -------------------------------------------------
        status = run.get("status")
        if status in TERMINAL_STATUSES:
            yield _frame_data(
                {
                    "event": f"run.{status}",
                    "run_id": run_id,
                    "domain": run.get("domain_name"),
                    "error": run.get("error"),
                }
            )
            return

        await asyncio.sleep(POLL_INTERVAL_SEC)

    # Hit MAX_DURATION_SEC without terminal — emit timeout marker and close.
    yield _frame_data({"event": "run.timeout"})
