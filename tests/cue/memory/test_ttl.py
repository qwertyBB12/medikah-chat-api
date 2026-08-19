"""
tests/cue/memory/test_ttl.py — PATCH-03 retention TTL: stamp, exclude, purge.

Migration 036 declared expires_at and nothing wrote or enforced it. These tests
pin the three halves of that promise:

  stamp   — insert_note dates every note MEMORY_RETENTION_DAYS from creation,
            and consolidation never pushes that date out,
  exclude — recall filters expired notes at the query, whether or not the sweep
            has run,
  purge   — the sweep hard-deletes expired rows, leaves legacy nulls alone, and
            reports a failed sweep instead of a green zero.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import routes.cue_routes as cr
from services.cue.memory.store import (
    MEMORY_RETENTION_DAYS,
    insert_note,
    load_recent_notes,
    purge_expired_notes,
    update_note,
)


def _chain(execute_data):
    """supabase mock whose .table(...)....execute() returns .data (see test_store)."""
    sb = MagicMock()
    result = MagicMock()
    result.data = execute_data
    builder = sb.table.return_value
    for attr in ("select", "insert", "update", "delete", "eq", "or_", "lt", "order", "limit"):
        getattr(builder, attr).return_value = builder
    builder.execute.return_value = result
    return sb


class TestStampOnInsert:
    def test_insert_stamps_expiry_at_retention_horizon(self):
        sb = _chain([{"id": "note-1"}])
        insert_note(sb, "phys-1", "the doctor runs a Tuesday clinic", "practice", "en")

        row = sb.table.return_value.insert.call_args[0][0]
        expires = datetime.fromisoformat(row["expires_at"])
        expected = datetime.now(timezone.utc) + timedelta(days=MEMORY_RETENTION_DAYS)
        assert abs((expires - expected).total_seconds()) < 60

    def test_expiry_stamped_even_with_an_embedding(self):
        sb = _chain([{"id": "note-1"}])
        insert_note(sb, "phys-1", "note", "general", "es", embedding=[0.1] * 8)
        assert "expires_at" in sb.table.return_value.insert.call_args[0][0]

    def test_consolidation_never_extends_the_clock(self):
        """The aviso anchors retention to creation — update_note must not touch it."""
        sb = _chain([{"id": "note-1"}])
        update_note(sb, "note-1", "refreshed text", [0.1] * 8, salience=3)

        payload = sb.table.return_value.update.call_args[0][0]
        assert "expires_at" not in payload


class TestExcludeExpiredFromRecall:
    def test_recall_filters_expired_at_the_query(self):
        sb = _chain([{"note": "n", "appended_at": "2026-06-27T10:00:00Z", "category": "general"}])
        load_recent_notes(sb, "phys-1", limit=10)

        clause = sb.table.return_value.or_.call_args[0][0]
        assert "expires_at.gt." in clause
        # legacy rows (null expires_at) predate the TTL and stay recallable
        assert "expires_at.is.null" in clause

    def test_filter_boundary_is_now(self):
        sb = _chain([])
        load_recent_notes(sb, "phys-1")

        clause = sb.table.return_value.or_.call_args[0][0]
        boundary = datetime.strptime(
            clause.split("expires_at.gt.")[1], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        assert abs((boundary - datetime.now(timezone.utc)).total_seconds()) < 60


class TestPurge:
    def test_deletes_only_rows_past_their_expiry(self):
        sb = _chain([{"id": "a"}, {"id": "b"}])
        assert purge_expired_notes(sb) == 2

        column, boundary = sb.table.return_value.lt.call_args[0]
        assert column == "expires_at"
        assert boundary.endswith("Z")
        sb.table.return_value.delete.assert_called_once()

    def test_is_not_physician_scoped(self):
        """A retention sweep spans the table — scoping it to one doctor would
        leave every other doctor's expired notes in place."""
        sb = _chain([])
        purge_expired_notes(sb)
        sb.table.return_value.eq.assert_not_called()

    def test_zero_when_client_missing(self):
        assert purge_expired_notes(None) == 0

    def test_failed_sweep_reports_minus_one_and_never_raises(self):
        sb = MagicMock()
        sb.table.side_effect = RuntimeError("db down")
        assert purge_expired_notes(sb) == -1


class _Req:
    """Minimal stand-in for the Request the internal handler reads headers from."""

    def __init__(self, headers: dict):
        self.headers = headers


class TestPurgeEndpoint:
    def test_route_registered_as_post(self):
        methods_by_path = {}
        for r in cr.router.routes:
            p = getattr(r, "path", None)
            methods_by_path.setdefault(p, set()).update(getattr(r, "methods", []) or [])
        assert "POST" in methods_by_path.get("/cue/internal/purge-expired-memory", set())

    @pytest.mark.asyncio
    async def test_purges_with_the_shared_secret(self):
        with patch.dict("os.environ", {"INTERNAL_API_SHARED_SECRET": "s3cret"}), \
             patch.object(cr, "get_supabase", return_value=MagicMock()), \
             patch.object(cr, "purge_expired_notes", return_value=4):
            result = await cr.internal_purge_expired_memory(
                _Req({"X-Internal-Secret": "s3cret"})
            )
        assert result == {"purged": 4}

    @pytest.mark.asyncio
    async def test_wrong_secret_is_forbidden(self):
        with patch.dict("os.environ", {"INTERNAL_API_SHARED_SECRET": "s3cret"}), \
             patch.object(cr, "purge_expired_notes") as purge:
            with pytest.raises(cr.HTTPException) as exc:
                await cr.internal_purge_expired_memory(_Req({"X-Internal-Secret": "nope"}))
        assert exc.value.status_code == 403
        purge.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_secret_config_refuses_rather_than_opening(self):
        with patch.dict("os.environ", {"INTERNAL_API_SHARED_SECRET": ""}), \
             patch.object(cr, "purge_expired_notes") as purge:
            with pytest.raises(cr.HTTPException) as exc:
                await cr.internal_purge_expired_memory(_Req({}))
        assert exc.value.status_code == 503
        purge.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_sweep_surfaces_as_500(self):
        """A cron must not read a failed sweep as a clean one."""
        with patch.dict("os.environ", {"INTERNAL_API_SHARED_SECRET": "s3cret"}), \
             patch.object(cr, "get_supabase", return_value=MagicMock()), \
             patch.object(cr, "purge_expired_notes", return_value=-1):
            with pytest.raises(cr.HTTPException) as exc:
                await cr.internal_purge_expired_memory(_Req({"X-Internal-Secret": "s3cret"}))
        assert exc.value.status_code == 500
