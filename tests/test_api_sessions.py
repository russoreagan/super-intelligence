"""
Durable ApiSessionRegistry: create write-through to Supabase, and get read-through
on a memory miss (the redeploy case). Companion mode (no Supabase) stays in-memory.
"""

from __future__ import annotations

from brain.api.sessions import ApiSessionRegistry


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self._f = {}

    def upsert(self, row, on_conflict=None):
        self._pending = row
        return self

    def select(self, *a):
        self._mode = "select"
        return self

    def eq(self, k, v):
        self._f[k] = v
        return self

    def execute(self):
        if getattr(self, "_pending", None):
            key = (self._pending["org_id"], self._pending["session_id"])
            self._rows[key] = self._pending
            self._pending = None
            return type("R", (), {"data": []})()
        match = [
            r
            for r in self._rows.values()
            if r["org_id"] == self._f.get("org_id") and r["session_id"] == self._f.get("session_id")
        ]
        return type("R", (), {"data": match})()


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeTable(self._rows)


def _enable(monkeypatch, rows):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _FakeClient(rows))
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")


def test_create_persists_and_survives_memory_loss(monkeypatch):
    rows = {}
    _enable(monkeypatch, rows)
    reg = ApiSessionRegistry(now_fn=lambda: 1.0, id_fn=lambda: "sess1")
    reg.create("cust-1", agent_id="the_analyst.billing", mandate_id="billing")
    assert ("org-1", "sess1") in rows  # write-through happened

    # Simulate a redeploy: a brand-new registry with empty memory.
    reg2 = ApiSessionRegistry()
    loaded = reg2.get("sess1")
    assert loaded is not None
    assert loaded.end_user_id == "cust-1"
    assert loaded.mandate_id == "billing"
    assert loaded.agent_id == "the_analyst.billing"


def test_companion_mode_in_memory_only(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    reg = ApiSessionRegistry(id_fn=lambda: "s2")
    reg.create("c2")
    assert reg.get("s2") is not None  # served from memory
    assert ApiSessionRegistry().get("s2") is None  # nothing persisted


def test_update_persists_pending(monkeypatch):
    rows = {}
    _enable(monkeypatch, rows)
    reg = ApiSessionRegistry(id_fn=lambda: "s3")
    s = reg.create("c3")
    s.pending = {"task": "send email", "is_write": True}
    reg.update(s)
    assert rows[("org-1", "s3")]["pending"]["task"] == "send email"
    assert ApiSessionRegistry().get("s3").pending["is_write"] is True
