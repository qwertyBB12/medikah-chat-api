"""Opening-turn greeting branch — Phase 23 voice slice.

The greeting is a brain turn (compass-aware, clinical register), addressing the
doctor by honorific from physician_workspace_accounts.title with a name-only
fallback when title IS NULL.
"""
from routes.cue_routes import _resolve_doctor_address


class _Resp:
    def __init__(self, data): self.data = data


class _Table:
    def __init__(self, store, name): self._store, self._name = store, name
    def select(self, *_): return self
    def eq(self, *_): return self
    def limit(self, *_): return self
    def execute(self): return _Resp(self._store.get(self._name, []))


class _FakeDB:
    def __init__(self, store): self._store = store
    def table(self, name): return _Table(self._store, name)


def test_address_uses_dra_honorific():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": "Dra"}],
        "physicians": [{"full_name": "Erika Aguirre"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Doctora Aguirre"


def test_address_uses_dr_honorific():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": "Dr"}],
        "physicians": [{"full_name": "Juan Perez Lopez"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Doctor Lopez"


def test_address_name_only_fallback_when_title_null():
    db = _FakeDB({
        "physician_workspace_accounts": [{"title": None}],
        "physicians": [{"full_name": "Erika Aguirre"}],
    })
    assert _resolve_doctor_address(db, "p1") == "Aguirre"


def test_address_empty_when_no_records():
    db = _FakeDB({})
    assert _resolve_doctor_address(db, "p1") == ""
