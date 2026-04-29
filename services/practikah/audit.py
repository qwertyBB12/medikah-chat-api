"""Append-only writer for practikah_provisioning_log (Phase 11).

Best-effort INSERT into practikah_provisioning_log. Never raises.
Mirrors credentialAuditService.ts logChange semantics — audit failures
are logged via logger.exception but never propagate to the caller.

Per Phase 11 D-13 (Table A — system events, ~90-day retention).
Per Phase 11 D-08: rollback runner reads this table to determine which
steps completed and need undoing. Append-only RLS enforced in 017 migration.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from db.client import get_supabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProvisioningLogEntry:
    """One row in practikah_provisioning_log.

    `event` values: 'requested' | 'succeeded' | 'failed' |
                    'rollback_started' | 'rollback_succeeded' | 'rollback_failed'

    `resource_type` values: 'domain' | 'mailbox' | 'dns' |
                             'cloudflare_zone' | 'cloudflare_hostname' | 'workspace'
    """

    physician_id: str
    run_id: str
    step_name: str
    resource_type: str
    event: str
    detail: dict[str, Any]
    initiated_by: str = "system"  # 'doctor' | 'admin' | 'system_renewal' | 'system_health' | 'system'


# ---------------------------------------------------------------------------
# Idempotency key (per D-10)
# ---------------------------------------------------------------------------

def _idempotency_key(
    physician_id: str,
    resource_type: str,
    run_id: str,
    step_name: str,
    event: str,
) -> str:
    """Return a deterministic SHA-256 hex string for deduplication.

    The UNIQUE INDEX idx_practikah_provisioning_log_idempotency on this column
    (set in Plan 11-01) causes duplicate inserts to fail silently.
    The audit writer's try/except swallows the unique-violation gracefully (T-11-06-01).
    """
    raw = f"{physician_id}|{resource_type}|{run_id}|{step_name}|{event}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Low-level writer (best-effort, never raises)
# ---------------------------------------------------------------------------

async def log_provisioning_event(entry: ProvisioningLogEntry) -> None:
    """Best-effort INSERT into practikah_provisioning_log. Never raises.

    1. Obtain the Supabase client. If unavailable, log and return.
    2. Compute the idempotency key.
    3. INSERT the row. On any exception (including unique-violation on
       idempotency_key), swallow via logger.exception and return.
    """
    db = get_supabase()
    if db is None:
        logger.error(
            "[provisioning_audit] supabase not configured — audit skipped "
            "run_id=%s step=%s event=%s",
            entry.run_id,
            entry.step_name,
            entry.event,
        )
        return

    idem_key = _idempotency_key(
        entry.physician_id,
        entry.resource_type,
        entry.run_id,
        entry.step_name,
        entry.event,
    )

    try:
        db.table("practikah_provisioning_log").insert(
            {
                "physician_id": entry.physician_id,
                "run_id": entry.run_id,
                "step_name": entry.step_name,
                "resource_type": entry.resource_type,
                "event": entry.event,
                "detail": entry.detail,
                "initiated_by": entry.initiated_by,
                "idempotency_key": idem_key,
            }
        ).execute()
    except Exception:
        logger.exception(
            "[provisioning_audit] insert failed run_id=%s step=%s event=%s",
            entry.run_id,
            entry.step_name,
            entry.event,
        )


# ---------------------------------------------------------------------------
# Per-run convenience wrapper
# ---------------------------------------------------------------------------

class ProvisioningLogWriter:
    """Per-run convenience wrapper that auto-fills physician_id + run_id.

    Mirrors the logCreate / logUpdateDiff / logDelete pattern in
    credentialAuditService.ts. Provides one method per event type so call
    sites are readable and event strings can't be typo-ed.
    """

    def __init__(self, physician_id: str, run_id: str) -> None:
        self.physician_id = physician_id
        self.run_id = run_id

    # -----------------------------------------------------------------------
    # Forward-direction events
    # -----------------------------------------------------------------------

    async def requested(
        self,
        *,
        step: str,
        detail: dict[str, Any],
        resource_type: str = "workspace",
    ) -> None:
        """Log 'requested' — a step has been initiated."""
        await log_provisioning_event(
            ProvisioningLogEntry(
                physician_id=self.physician_id,
                run_id=self.run_id,
                step_name=step,
                resource_type=resource_type,
                event="requested",
                detail=detail,
            )
        )

    async def succeeded(
        self,
        *,
        step: str,
        detail: Optional[dict[str, Any]] = None,
        resource_type: str = "workspace",
    ) -> None:
        """Log 'succeeded' — a step completed without error."""
        await log_provisioning_event(
            ProvisioningLogEntry(
                physician_id=self.physician_id,
                run_id=self.run_id,
                step_name=step,
                resource_type=resource_type,
                event="succeeded",
                detail=detail or {},
            )
        )

    async def failed(
        self,
        *,
        step: str,
        detail: dict[str, Any],
        resource_type: str = "workspace",
    ) -> None:
        """Log 'failed' — a step encountered an error."""
        await log_provisioning_event(
            ProvisioningLogEntry(
                physician_id=self.physician_id,
                run_id=self.run_id,
                step_name=step,
                resource_type=resource_type,
                event="failed",
                detail=detail,
            )
        )

    # -----------------------------------------------------------------------
    # Rollback events
    # -----------------------------------------------------------------------

    async def rollback_started(
        self,
        *,
        step: str,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Log 'rollback_started' — the compensating action for a step has begun."""
        await log_provisioning_event(
            ProvisioningLogEntry(
                physician_id=self.physician_id,
                run_id=self.run_id,
                step_name=step,
                resource_type="workspace",
                event="rollback_started",
                detail=detail or {},
            )
        )

    async def rollback_succeeded(
        self,
        *,
        step: str,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Log 'rollback_succeeded' — the compensating action completed."""
        await log_provisioning_event(
            ProvisioningLogEntry(
                physician_id=self.physician_id,
                run_id=self.run_id,
                step_name=step,
                resource_type="workspace",
                event="rollback_succeeded",
                detail=detail or {},
            )
        )

    async def rollback_failed(
        self,
        *,
        step: str,
        detail: dict[str, Any],
        resource_type: str = "workspace",
    ) -> None:
        """Log 'rollback_failed' — the compensating action itself failed."""
        await log_provisioning_event(
            ProvisioningLogEntry(
                physician_id=self.physician_id,
                run_id=self.run_id,
                step_name=step,
                resource_type=resource_type,
                event="rollback_failed",
                detail=detail,
            )
        )

    # -----------------------------------------------------------------------
    # Query helpers used by the orchestrator and crash-resume
    # -----------------------------------------------------------------------

    async def list_completed_steps_for_run(self) -> list[dict[str, Any]]:
        """Return all 'succeeded' rows for this run, ordered by recorded_at ASC.

        Used by the rollback runner to determine which steps need undoing.
        Returns a list of dicts with at minimum: step_name, resource_type, detail.
        Returns [] if Supabase is unavailable.
        """
        db = get_supabase()
        if db is None:
            logger.error(
                "[provisioning_audit] list_completed_steps: supabase not configured run_id=%s",
                self.run_id,
            )
            return []

        try:
            result = (
                db.table("practikah_provisioning_log")
                .select("step_name, resource_type, detail, recorded_at")
                .eq("physician_id", self.physician_id)
                .eq("run_id", self.run_id)
                .eq("event", "succeeded")
                .order("recorded_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception:
            logger.exception(
                "[provisioning_audit] list_completed_steps failed run_id=%s", self.run_id
            )
            return []

    @staticmethod
    async def list_orphan_runs() -> list[tuple[str, str]]:
        """Find runs that have 'requested' events but no terminal event.

        A run is considered orphaned if:
        - It has at least one 'requested' event (provisioning started), AND
        - It has NO 'rollback_succeeded' or 'rollback_failed' event (never finished), AND
        - Its last activity was more than 5 minutes ago (avoid racing in-flight runs).

        Returns a list of (physician_id, run_id) tuples for orphaned runs.
        Returns [] if Supabase is unavailable.

        Per D-09: crash-resume scans for these on FastAPI startup. resume_orphan_runs
        invokes run_rollback for each — see orchestrator.py.
        """
        db = get_supabase()
        if db is None:
            logger.error(
                "[provisioning_audit] list_orphan_runs: supabase not configured"
            )
            return []

        try:
            # Use a raw RPC call for the GROUP BY HAVING query since the
            # PostgREST query builder doesn't support aggregations directly.
            # The SQL: find (physician_id, run_id) pairs where there is a
            # 'requested' event, no terminal event ('rollback_succeeded' or
            # 'rollback_failed'), and the newest event is >5 minutes old.
            result = db.rpc(
                "list_orphan_provisioning_runs",
                {},
            ).execute()
            if result.data:
                return [(r["physician_id"], r["run_id"]) for r in result.data]
            return []
        except Exception:
            # If the RPC function doesn't exist yet (pre-migration), log and
            # return [] so startup doesn't crash. Plan 11-01 creates this function.
            logger.warning(
                "[provisioning_audit] list_orphan_runs RPC unavailable — "
                "orphan detection skipped. Ensure 017_practikah.sql has been applied."
            )
            return []
