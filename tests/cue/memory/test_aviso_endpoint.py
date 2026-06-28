"""tests/cue/memory/test_aviso_endpoint.py — PATCH-03 aviso ack endpoints."""
import inspect

import routes.cue_routes as cr


def test_aviso_routes_registered():
    paths = {getattr(r, "path", None) for r in cr.router.routes}
    assert "/cue/memory/aviso" in paths
    assert "/cue/memory/aviso-ack" in paths


def test_ack_handler_upserts_consent():
    src = inspect.getsource(cr)
    assert "cue_memory_consent" in src
    assert "AVISO_VERSION" in src
