"""owner_ref: the content-free handle on a persona's buyer (persona_owners)."""

from __future__ import annotations

import re

import pytest

from brain import persona_owners as po


@pytest.fixture
def org(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "personas" / "home_p"))
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("BRAIN_OWNER_REF_SECRET", raising=False)
    monkeypatch.setattr(po, "_secret_cache", None)
    return tmp_path


def test_deterministic_12_hex_and_differs_per_secret(org, monkeypatch):
    a = po.owner_ref_for("buyer-1")
    assert re.fullmatch(r"[0-9a-f]{12}", a)
    assert po.owner_ref_for("buyer-1") == a
    assert po.owner_ref_for("buyer-2") != a
    assert po.owner_ref_for("") == ""
    # Env secret wins over the file and changes the ref.
    monkeypatch.setenv("BRAIN_OWNER_REF_SECRET", "other-secret")
    b = po.owner_ref_for("buyer-1")
    assert b != a and re.fullmatch(r"[0-9a-f]{12}", b)
    monkeypatch.setenv("BRAIN_OWNER_REF_SECRET", "third")
    assert po.owner_ref_for("buyer-1") not in (a, b)


def test_per_org_secret_file_created_once_0600(org, monkeypatch):
    import stat

    a = po.owner_ref_for("buyer-1")
    path = org / ".owner_ref_secret"
    assert path.is_file() and len(path.read_text().strip()) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A fresh process reads the same secret back.
    monkeypatch.setattr(po, "_secret_cache", None)
    assert po.owner_ref_for("buyer-1") == a
    assert len(list(org.glob(".owner_ref_secret*"))) == 1


def test_claim_writes_set_owner_with_ref_never_the_id(org, monkeypatch):
    from brain import persona_index
    from brain.second_brain import supabase_client

    class _Sb:
        def __init__(self):
            self.rows = {}
            self._table = ""

        def table(self, name):
            self._table = name
            self._filters = []
            return self

        def upsert(self, row, **k):
            self._op, self._row = "upsert", row
            return self

        def select(self, *a, **k):
            self._op = "select"
            return self

        def eq(self, k, v):
            self._filters.append((k, v))
            return self

        def limit(self, n):
            return self

        def execute(self):
            if self._op == "upsert":
                self.rows.setdefault(self._row["persona"], self._row)
                return type("R", (), {"data": []})()
            p = dict(self._filters).get("persona")
            r = self.rows.get(p)
            return type("R", (), {"data": [r] if r else []})()

    sb = _Sb()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: sb)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    seen = []
    monkeypatch.setattr(persona_index, "set_owner", lambda *a: seen.append(a) or True)

    assert po.claim("Ahab P1", "buyer-1", partner_id="acme") == "buyer-1"
    assert seen == [("ahab_p1", True, po.owner_ref_for("buyer-1"), 1, "acme")]
    assert "buyer-1" not in seen[0]
    # Second claimant: the record keeps the first owner; the rollup restates it.
    assert po.claim("ahab_p1", "buyer-2") == "buyer-1"
    assert seen[-1][2] == po.owner_ref_for("buyer-1") and seen[-1][4] == ""
