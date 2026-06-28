"""tests/cue/memory/test_write_seam.py — the post-stream task calls run_memory_judge."""
import inspect

import routes.cue_routes as cr


def test_route_imports_memory_judge():
    assert hasattr(cr, "run_memory_judge")


def test_post_stream_task_invokes_judge():
    src = inspect.getsource(cr)
    # The L500 TODO is gone and the judge is called in the background task.
    assert "run_memory_judge(" in src
    assert "TODO (Phase 25 MEM-02/MEM-06)" not in src
