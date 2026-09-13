"""The persona index (migration 039, brain/persona_index.py) — dark writes.

Every sync point writes the `personas` table org-scoped and best-effort; nothing
reads it yet (Phase D). A fake Supabase client records the calls.
"""

from __future__ import annotations

import asyncio

import pytest

from brain import persona_index as pi
from brain import personas
from brain.settings import DEFAULTS, settings


class _Res:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class _Query:
    """One chained PostgREST call, recorded on execute()."""

    def __init__(self, sb, table):
        self.sb = sb
        self.table = table
        self.op = ""
        self.payload = None
        self.kwargs: dict = {}
        self.filters: list[tuple] = []
        self.range_: tuple | None = None
        self.orders: list[tuple] = []
        self.select_args: tuple = ()

    def select(self, *a, **k):
        self.op, self.select_args, self.kwargs = "select", a, k
        return self

    def upsert(self, payload, **k):
        self.op, self.payload, self.kwargs = "upsert", payload, k
        return self

    def insert(self, payload, **k):
        self.op, self.payload, self.kwargs = "insert", payload, k
        return self

    def update(self, patch, **k):
        self.op, self.payload = "update", patch
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _f(self, name):
        def _apply(*a):
            self.filters.append((name, *a))
            return self

        return _apply

    def __getattr__(self, name):
        if name in ("eq", "is_", "in_", "gte", "lt", "or_", "like"):
            return self._f(name)
        raise AttributeError(name)

    def order(self, col, **k):
        self.orders.append((col, k))
        return self

    def range(self, a, b):
        self.range_ = (a, b)
        return self

    def limit(self, n):
        self.filters.append(("limit", n))
        return self

    def execute(self):
        self.sb.calls.append(self)
        if self.sb.fail and self.table in self.sb.fail:
            raise RuntimeError(self.sb.fail[self.table])
        handler = self.sb.responses.get((self.table, self.op))
        return handler(self) if handler else _Res()


class _FakeSb:
    def __init__(self):
        self.calls: list[_Query] = []
        self.rpcs: list[tuple[str, dict]] = []
        self.fail: dict[str, str] = {}
        self.fail_rpc: str | None = None
        self.responses: dict = {}

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        sb = self

        class _Rpc:
            def execute(self_inner):
                sb.rpcs.append((name, dict(params)))
                if sb.fail_rpc:
                    raise RuntimeError(sb.fail_rpc)
                return _Res(3)

        return _Rpc()

    def of(self, table, op=None):
        return [c for c in self.calls if c.table == table and (op is None or c.op == op)]


@pytest.fixture
def fs(tmp_path, monkeypatch):
    from brain import persona_chem

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_MAX_PERSONAS", raising=False)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    monkeypatch.setitem(settings._data, "persona_index_enabled", 1)
    monkeypatch.setitem(settings._data, "persona_index_touch_debounce_s", 300)
    pi._reset_for_tests()
    yield tmp_path
    pi._reset_for_tests()


@pytest.fixture
def sb(fs, monkeypatch):
    from brain.second_brain import supabase_client

    fake = _FakeSb()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    return fake


# ── settings ────────────────────────────────────────────────────────────────────


def test_new_settings_keys_declared():
    expected = {
        "persona_index_enabled": 1,
        "persona_index_reconcile_on_boot": 1,
        "persona_index_touch_debounce_s": 300,
        "agent_usage_daily_enabled": 1,
        "agent_usage_raw_enabled": 1,
        "agent_usage_raw_retention_days": 7,
        "agent_usage_meter_end_users": 1,
        "persona_chem_root_resolve": 1,
    }
    for key, value in expected.items():
        assert key in DEFAULTS, f"{key} missing from DEFAULTS"
        assert DEFAULTS[key] == value
    # Phase D read switches (declared, default on).
    assert DEFAULTS["persona_index_read"] == 1
    assert DEFAULTS["agent_usage_read_daily"] == 1


# ── upsert / clone / delete ─────────────────────────────────────────────────────


def test_upsert_is_org_scoped_with_on_conflict(sb):
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "Unbending.", "tag": "t"})
    ups = sb.of("personas", "upsert")
    assert len(ups) == 1
    row = ups[0].payload[0]
    assert ups[0].kwargs["on_conflict"] == "org_id,persona"
    assert row["org_id"] == "org-1" and row["persona"] == "ahab"
    assert row["display_name"] == "Ahab" and row["builtin"] is False
    assert row["builtin_override"] is False and row["template"] == ""
    assert row["version"] == 1 and row["deleted_at"] is None and row["tag"] == "t"
    # No activity / learned columns on a spec write — they belong to other writers.
    assert "last_human_turn_ts" not in row and "learned_state" not in row


def test_builtin_override_upsert_flags_override(sb):
    personas.upsert("the_sage", {"baseline": {"DA": 0.3}})
    row = sb.of("personas", "upsert")[-1].payload[0]
    assert row["persona"] == "the_sage" and row["builtin"] is True
    assert row["builtin_override"] is True and row["display_name"] == "The Sage"


def test_clone_writes_template_once_and_learned_only_for_current_seed(sb):
    from brain.persona_key import persona_state_root

    personas.upsert("tmpl", {"display_name": "Template", "disposition": "Steady."})
    sb.calls.clear()
    out = asyncio.run(
        personas.clone("tmpl", {"suffix": "a", "copy_agents": False, "seed": "default"})
    )
    assert out["created"] is True
    ups = sb.of("personas", "upsert")
    assert len(ups) == 1, "clone indexes the FINAL spec once (not the inner upsert too)"
    row = ups[0].payload[0]
    assert row["persona"] == "tmpl_a" and row["template"] == "tmpl" and row["seed"] == "default"
    assert not sb.of("personas", "update"), "default seed carries no learned state"

    # `current` seed with learned files on the template → learned_state flagged.
    (persona_state_root("tmpl") / "wiring.json").write_text("[]")
    sb.calls.clear()
    out = asyncio.run(
        personas.clone("tmpl", {"suffix": "b", "copy_agents": False, "seed": "current"})
    )
    assert out["copied"]["files"] == ["wiring.json"]
    upd = sb.of("personas", "update")
    assert len(upd) == 1 and upd[0].payload["learned_state"] is True
    assert ("eq", "org_id", "org-1") in upd[0].filters
    assert ("eq", "persona", "tmpl_b") in upd[0].filters


def test_delete_marks_custom_and_resets_builtin_override(sb):
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x"})
    personas.upsert("the_sage", {"baseline": {"DA": 0.3}})
    sb.calls.clear()
    assert personas.delete("ahab") is True
    upd = sb.of("personas", "update")
    assert len(upd) == 1 and upd[0].payload["deleted_at"]
    assert ("eq", "persona", "ahab") in upd[0].filters
    sb.calls.clear()
    assert personas.delete("the_sage") is True
    row = sb.of("personas", "upsert")[-1].payload[0]
    assert row["persona"] == "the_sage" and row["builtin_override"] is False
    # Deleting a spec that was never there writes nothing.
    sb.calls.clear()
    assert personas.delete("ghost") is False
    assert not sb.of("personas")


def test_purge_sweep_covers_the_index_row():
    from brain.session_turn import _TurnMixin

    assert "personas" in _TurnMixin._PERSONA_PURGE_TABLES
    assert "personas" not in _TurnMixin._PERSONA_PURGE_KEPT


def test_remove_deletes_row_and_clears_process_state(sb):
    pi.touch_human_turn("ahab", 100.0)
    pi._learned_marked.add("ahab")
    assert pi.remove("ahab") is True
    d = sb.of("personas", "delete")
    assert len(d) == 1 and ("eq", "persona", "ahab") in d[0].filters
    assert "ahab" not in pi._touches and "ahab" not in pi._learned_marked


# ── human-turn touches ──────────────────────────────────────────────────────────


def test_touch_is_debounced_and_batched_into_one_rpc(sb):
    from brain import human_activity

    assert pi.touch_human_turn("ahab", 1_000.0) is True
    assert pi.touch_human_turn("ahab", 1_100.0) is False, "inside the 300 s debounce"
    assert pi.touch_human_turn("ahab", 1_400.0) is True
    assert pi.touch_human_turn("ishmael", 1_050.0) is True
    assert not sb.rpcs, "touch is a dict write only"
    # The on-disk stamp writer mirrors into the same queue.
    human_activity.stamp_persona("queequeg", 1_200.0, force=True)
    assert "queequeg" in pi._touches

    assert pi.flush_touches() == 3
    assert len(sb.rpcs) == 1
    name, params = sb.rpcs[0]
    assert name == "persona_touch_batch" and params["p_org_id"] == "org-1"
    touches = params["p_touches"]
    assert set(touches) == {"ahab", "ishmael", "queequeg"}
    assert touches["ahab"].startswith("1970-01-01T00:23:20")  # 1400 s → max wins
    assert pi.flush_touches() == 0 and len(sb.rpcs) == 1, "queue drained"


def test_failed_flush_requeues_and_warns_once(sb, caplog):
    sb.fail_rpc = "network down"
    pi.touch_human_turn("ahab", 1_000.0)
    with caplog.at_level("WARNING"):
        assert pi.flush_touches() == 0
        assert pi.flush_touches() == 0
    assert "ahab" in pi._touches
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warns) == 1, "second failure logs at DEBUG"


# ── learned state / owner ───────────────────────────────────────────────────────


def test_set_learned_state_writes_once_per_process(sb):
    assert pi.set_learned_state("ahab") is True
    assert pi.set_learned_state("ahab") is False
    assert len(sb.of("personas", "update")) == 1
    assert pi.set_learned_state("ahab", on=False) is True
    assert pi.set_learned_state("ahab") is True, "cleared → can be set again"


def test_sleep_batch_flags_learned_state_for_non_home_only(sb, monkeypatch):
    from brain.sleep import SleepConsolidation

    SleepConsolidation._flag_learned_state("tmpl_a")
    SleepConsolidation._flag_learned_state("home_p")
    upd = sb.of("personas", "update")
    assert len(upd) == 1 and ("eq", "persona", "tmpl_a") in upd[0].filters


def test_set_owner_updates_rollup_columns(sb):
    assert pi.set_owner("ahab", True, "abc123def456", 1, "acme") is True
    upd = sb.of("personas", "update")[0]
    assert upd.payload["owner_bound"] is True and upd.payload["owner_ref"] == "abc123def456"
    assert upd.payload["owner_count"] == 1 and upd.payload["partner_id"] == "acme"
    assert ("eq", "org_id", "org-1") in upd.filters


# ── readers (Phase D wiring, verified now) ──────────────────────────────────────


def test_count_custom_is_a_head_count(sb):
    sb.responses[("personas", "select")] = lambda q: _Res([], count=42)
    assert pi.count_custom() == 42
    q = sb.of("personas", "select")[0]
    assert q.kwargs == {"count": "exact", "head": True}
    assert ("eq", "org_id", "org-1") in q.filters
    assert ("eq", "builtin", False) in q.filters
    assert ("is_", "deleted_at", "null") in q.filters


def test_page_uses_range_and_returns_the_listing_contract(sb):
    rows = [
        {
            "persona": "the_sage",
            "display_name": "The Sage",
            "builtin": True,
            "builtin_override": True,
        },
        {
            "persona": "ahab",
            "display_name": "Ahab",
            "builtin": False,
            "template": "",
            "version": 3,
            "spec_updated": "2026-09-12T00:00:00+00:00",
        },
        {
            "persona": "ahab_p1",
            "display_name": "Ahab",
            "builtin": False,
            "template": "ahab",
            "seed": "current",
            "version": 1,
        },
    ]
    sb.responses[("personas", "select")] = lambda q: _Res(rows, count=250)
    out = pi.page(include_clones=True, limit=3, offset=6)
    q = sb.of("personas", "select")[0]
    assert q.kwargs == {"count": "exact"} and q.range_ == (6, 8)
    assert ("eq", "org_id", "org-1") in q.filters and ("is_", "deleted_at", "null") in q.filters
    assert out == {
        "personas": [
            {"slug": "the_sage", "display_name": "The Sage", "builtin": True, "overridden": True},
            {
                "slug": "ahab",
                "display_name": "Ahab",
                "builtin": False,
                "version": 3,
                "updated": "2026-09-12T00:00:00+00:00",
            },
            {
                "slug": "ahab_p1",
                "display_name": "Ahab",
                "builtin": False,
                "version": 1,
                "updated": None,
                "template": "ahab",
                "seed": "current",
            },
        ],
        "total": 250,
        "limit": 3,
        "offset": 6,
        "next_offset": 9,
    }
    assert set(pi.page(limit=200, offset=0)) == {
        "personas",
        "total",
        "limit",
        "offset",
        "next_offset",
    }


def test_page_filters_clones_template_query_and_allowed(sb):
    sb.responses[("personas", "select")] = lambda q: _Res([], count=0)
    pi.page()
    assert ("eq", "template", "") in sb.of("personas", "select")[-1].filters
    pi.page(template="Ahab")
    assert ("eq", "template", "ahab") in sb.of("personas", "select")[-1].filters
    pi.page(include_clones=True, q="ish")
    f = sb.of("personas", "select")[-1].filters
    assert ("eq", "template", "") not in f
    assert ("or_", "persona.ilike.%ish%,display_name.ilike.%ish%") in f
    pi.page(allowed=["the_visionary", "Captain Ahab", ""])
    f = sb.of("personas", "select")[-1].filters
    assert ("in_", "persona", ["captain_ahab", "the_visionary"]) in f
    # Empty allowlist → nothing, without a query.
    n = len(sb.calls)
    assert pi.page(allowed=[])["total"] == 0 and len(sb.calls) == n
    with pytest.raises(personas.PersonaError):
        pi.page(limit="x")


def test_slugs_and_template_of(sb):
    sb.responses[("personas", "select")] = lambda q: _Res(
        [{"persona": "a"}, {"persona": "b"}, {"persona": "home_p"}]
    )
    assert pi.slugs(learned_state=True, active_days=7, exclude=["home_p"]) == ["a", "b"]
    f = sb.of("personas", "select")[-1].filters
    assert ("eq", "learned_state", True) in f
    assert any(x[0] == "gte" and x[1] == "last_human_turn_ts" for x in f)
    sb.responses[("personas", "select")] = lambda q: _Res([{"template": "ahab"}])
    assert pi.template_of("ahab_p1") == "ahab"
    sb.responses[("personas", "select")] = lambda q: _Res([])
    assert pi.template_of("ghost") is None


# ── reconcile ───────────────────────────────────────────────────────────────────


def test_reconcile_upserts_builtins_and_specs_with_stamps(sb):
    from brain import human_activity, persona_chem

    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x"})
    personas.upsert("the_sage", {"baseline": {"DA": 0.3}})
    human_activity.stamp_persona("ahab", 1_700_000_000.0, force=True)
    sb.calls.clear()
    pi._reset_for_tests()
    out = pi.reconcile()
    ups = sb.of("personas", "upsert")
    assert out["batches"] == 1 and len(ups) == 1
    rows = {r["persona"]: r for r in ups[0].payload}
    assert out["indexed"] == len(persona_chem.PERSONA_CHEMISTRY) + 1
    assert rows["the_sage"]["builtin"] is True and rows["the_sage"]["builtin_override"] is True
    assert rows["the_visionary"]["builtin_override"] is False
    assert rows["ahab"]["builtin"] is False and rows["ahab"]["display_name"] == "Ahab"
    assert rows["ahab"]["last_human_turn_ts"].startswith("2023-11-14T22:13:20")
    assert rows["the_sage"]["last_human_turn_ts"] is None
    assert all(r["org_id"] == "org-1" for r in ups[0].payload)
    # Uniform keys per batch (PostgREST bulk-upsert rule).
    assert len({tuple(sorted(r)) for r in ups[0].payload}) == 1
    assert "learned_state" not in rows["ahab"]


def test_reconcile_learned_probes_customs_only(sb, monkeypatch):
    from brain import persona_audit

    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x"})
    probed = []
    monkeypatch.setattr(persona_audit, "has_learned_state", lambda s: probed.append(s) or True)
    sb.calls.clear()
    out = pi.reconcile(learned=True)
    assert probed == ["ahab"] and out["learned"] == 1
    rows = {r["persona"]: r for r in sb.of("personas", "upsert")[0].payload}
    assert rows["ahab"]["learned_state"] is True and rows["ahab"]["learned_state_at"]
    assert rows["the_visionary"]["learned_state"] is False
    assert rows["the_visionary"]["learned_state_at"] is None
    assert len({tuple(sorted(r)) for r in rows.values()}) == 1


def test_reconcile_batches_of_200(sb, monkeypatch):
    specs = {f"p{i:04d}": {"slug": f"p{i:04d}", "display_name": f"P{i}"} for i in range(450)}
    monkeypatch.setattr(personas, "_read_all_specs", lambda: specs)
    from brain import persona_chem

    out = pi.reconcile()
    sizes = [len(q.payload) for q in sb.of("personas", "upsert")]
    assert out["batches"] == len(sizes) and sum(sizes) == out["indexed"]
    assert max(sizes) == 200 and out["indexed"] == 450 + len(persona_chem.PERSONA_CHEMISTRY)


def test_reconcile_on_boot_skips_when_index_is_complete(sb, monkeypatch):
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x"})
    sb.responses[("personas", "select")] = lambda q: _Res([], count=1)
    sb.calls.clear()
    t = pi.reconcile_on_boot()
    t.join(5)
    assert not sb.of("personas", "upsert")
    # Behind → reconcile runs.
    sb.responses[("personas", "select")] = lambda q: _Res([], count=0)
    t = pi.reconcile_on_boot()
    t.join(5)
    assert sb.of("personas", "upsert")
    monkeypatch.setitem(settings._data, "persona_index_reconcile_on_boot", 0)
    assert pi.reconcile_on_boot() is None


# ── off / missing ───────────────────────────────────────────────────────────────


def test_disabled_without_supabase_makes_no_calls_and_scan_still_used(fs, monkeypatch):
    from brain.second_brain import supabase_client

    calls = []
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    monkeypatch.setattr(supabase_client, "get_client", lambda: calls.append("client"))
    assert pi.enabled() is False
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x"})
    asyncio.run(personas.clone("ahab", {"suffix": "p1", "copy_agents": False}))
    personas.delete("ahab_p1")
    assert pi.touch_human_turn("ahab", 1.0) is True and pi.flush_touches() == 0
    assert pi.set_learned_state("ahab") is False and pi.count_custom() is None
    assert pi.page() is None and pi.slugs() is None and pi.reconcile()["indexed"] == 0
    assert calls == []
    # The listing keeps reading the spec scan.
    assert [r["slug"] for r in personas.list_all() if not r["builtin"]] == ["ahab"]
    assert personas.custom_count() == 1


def test_kill_switch_and_missing_table_probe(sb, monkeypatch):
    monkeypatch.setitem(settings._data, "persona_index_enabled", 0)
    assert pi.enabled() is False
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x"})
    assert not sb.of("personas")
    monkeypatch.setitem(settings._data, "persona_index_enabled", 1)
    assert pi.enabled() is True
    sb.fail["personas"] = 'relation "public.personas" does not exist (PGRST205)'
    personas.upsert("ishmael", {"display_name": "Ishmael", "disposition": "y"})
    assert pi.enabled() is False, "missing table parks the index"
    n = len(sb.calls)
    personas.upsert("queequeg", {"display_name": "Queequeg", "disposition": "z"})
    assert len(sb.calls) == n, "no further calls while parked"
    monkeypatch.setattr(pi, "_missing_until", 0.0)
    assert pi.enabled() is True


# ── owner route ─────────────────────────────────────────────────────────────────


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "kp"},
        "Bearer ko": {"partner_id": None, "owner": True, "key_id": "ko"},
    }.get(authorization)


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from brain.api.server import build_api_router
    from brain.api.sessions import ApiSessionRegistry

    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "s1"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


def test_reindex_route_is_owner_only_and_runs_learned_reconcile(client, sb, monkeypatch):
    from brain.api import reference

    assert reference.is_owner_route("POST", "/v1/personas/reindex")
    assert client.post("/v1/personas/reindex").status_code == 401
    assert (
        client.post("/v1/personas/reindex", headers={"Authorization": "Bearer kp"}).status_code
        == 403
    )
    seen = []
    monkeypatch.setattr(
        pi, "reconcile", lambda learned=False: seen.append(learned) or {"indexed": 7, "learned": 2}
    )
    r = client.post("/v1/personas/reindex", headers={"Authorization": "Bearer ko"})
    assert r.status_code == 200 and seen == [True]
    body = r.json()
    assert body["indexed"] == 7 and body["learned"] == 2 and "elapsed_s" in body
    monkeypatch.setitem(settings._data, "persona_index_enabled", 0)
    assert (
        client.post("/v1/personas/reindex", headers={"Authorization": "Bearer ko"}).status_code
        == 503
    )


def test_reindex_script_posts_once_per_key(monkeypatch, capsys):
    import httpx

    from scripts import reindex_personas as script

    posted = []

    def _post(url, headers=None, timeout=None):
        posted.append((url, headers["Authorization"]))
        return httpx.Response(200, json={"indexed": 3, "learned": 1, "elapsed_s": 0.5})

    monkeypatch.setattr(httpx, "post", _post)
    rc = script.main(["--base-url", "https://x.test/", "--owner-key", "k1", "--owner-key", "k2"])
    assert rc == 0
    assert posted == [
        ("https://x.test/v1/personas/reindex", "Bearer k1"),
        ("https://x.test/v1/personas/reindex", "Bearer k2"),
    ]
    assert "indexed 3" in capsys.readouterr().out
