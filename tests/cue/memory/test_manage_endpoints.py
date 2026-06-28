"""tests/cue/memory/test_manage_endpoints.py — Slice 3 doctor-visible/editable routes registered."""
import inspect

import routes.cue_routes as cr


def test_memory_management_routes_registered():
    routes = {(getattr(r, "path", None), tuple(sorted(getattr(r, "methods", []) or [])))
              for r in cr.router.routes}
    paths = {p for p, _ in routes}
    assert "/cue/memory" in paths
    assert "/cue/memory/{note_id}" in paths


def test_delete_and_patch_methods_present():
    methods_by_path = {}
    for r in cr.router.routes:
        p = getattr(r, "path", None)
        methods_by_path.setdefault(p, set()).update(getattr(r, "methods", []) or [])
    assert "DELETE" in methods_by_path.get("/cue/memory/{note_id}", set())
    assert "PATCH" in methods_by_path.get("/cue/memory/{note_id}", set())
    assert "GET" in methods_by_path.get("/cue/memory", set())


def test_handlers_use_store_ops_and_scope_to_auth():
    src = inspect.getsource(cr)
    assert "list_notes(supabase, auth.physician_id)" in src
    assert "delete_note(supabase, auth.physician_id, note_id)" in src
    assert "correct_note(supabase, auth.physician_id, note_id" in src
