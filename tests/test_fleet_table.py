"""brain/fleet — the paged, content-free persona table, drawer, audit and actions."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from brain import agents, fleet, human_activity, learning_mode, org_settings, personas  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402

NOW = time.time()
CONTENT_KEYS = {
    "prompt",
    "response",
    "goal",
    "summary",
    "thought",
    "tool_input",
    "end_user_id",
    "steps_json",
    "results_json",
    "reason_human",
    "user_input",
    "text",
    "owner_end_user_id",
}


def _walk(o, found: set):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in CONTENT_KEYS:
                found.add(k)
            _walk(v, found)
    elif isinstance(o, list):
        for v in o:
            _walk(v, found)


@pytest.fixture
def org(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb" / "personas" / "home_p"))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setattr(human_activity, "_persona_last_write_ts", {})
    monkeypatch.setattr(learning_mode, "audit_log_path", lambda: tmp_path / "g.jsonl")
    fleet.invalidate()
    specs = {
        "tmpl": {
            "slug": "tmpl",
            "display_name": "Template",
            "tag": "shop",
            "updated": NOW - 30 * 86400,
        },
        "tmpl_c1": {
            "slug": "tmpl_c1",
            "display_name": "Buyer one",
            "template": "tmpl",
            "cloned": NOW - 10 * 86400,
            "updated": NOW - 10 * 86400,
        },
        "tmpl_c2": {
            "slug": "tmpl_c2",
            "display_name": "Buyer two",
            "template": "tmpl",
            "cloned": NOW - 20 * 86400,
            "updated": NOW - 20 * 86400,
        },
        "tmpl_c3": {
            "slug": "tmpl_c3",
            "display_name": "Buyer three",
            "template": "tmpl",
            "cloned": NOW - 1 * 86400,
            "updated": NOW - 86400,
        },
    }
    monkeypatch.setattr(personas, "_read_all_specs", lambda: {k: dict(v) for k, v in specs.items()})
    monkeypatch.setattr(
        personas,
        "list_all",
        lambda: [
            {"slug": "home_p", "display_name": "Home", "builtin": True, "overridden": False},
            *[
                {
                    "slug": k,
                    "display_name": v["display_name"],
                    "builtin": False,
                    **({"template": v["template"]} if v.get("template") else {}),
                }
                for k, v in specs.items()
            ],
        ],
    )
    rows = [
        {
            "persona": "home_p",
            "mandate_id": "m",
            "enabled": True,
            "tier": "full",
            "permissions": {},
        },
        {
            "persona": "tmpl_c1",
            "mandate_id": "m",
            "enabled": True,
            "tier": "full",
            "permissions": {"answer_only": True},
        },
        {
            "persona": "tmpl_c2",
            "mandate_id": "m",
            "enabled": True,
            "tier": "full",
            "permissions": {},
        },
        {
            "persona": "tmpl_c3",
            "mandate_id": "m",
            "enabled": True,
            "tier": "lite",
            "permissions": {},
        },
    ]
    monkeypatch.setattr(
        agents,
        "list_agents",
        lambda **kw: [{**r, "agent_id": f"{r['persona']}.{r['mandate_id']}"} for r in rows],
    )
    human_activity.stamp_persona("tmpl_c1", NOW - 3600, force=True)  # active
    human_activity.stamp_persona("tmpl_c2", NOW - 12 * 86400, force=True)  # dormant
    live = {
        "roster_personas": ["home_p", "tmpl_c1"],
        "breaker": {"anthropic": {"kind": "auth"}},
        "dmn": {"roster": {"mode": "active", "size": 2, "days": 7, "cadence_s": 10}},
    }
    usage = {
        "usage": {
            "tmpl_c1.m": {"cloud_usd": 1.5, "calls": 4},
            "home_p.m": {"cloud_usd": 0.2, "calls": 1},
        }
    }
    jobs = [
        {
            "job_id": "j1",
            "agent_id": "tmpl_c2.m",
            "goal": "secret",
            "state": "running",
            "updated_at": NOW - 3 * 3600,
            "end_user_id": "u2",
        },
        {
            "job_id": "j2",
            "agent_id": "tmpl_c1.m",
            "goal": "s2",
            "state": "completed",
            "updated_at": NOW - 60,
            "end_user_id": "u1",
        },
    ]
    return {"live": live, "usage": usage, "jobs": jobs}


def test_gather_and_page_are_content_free_and_sorted(org, monkeypatch):
    rows = fleet.gather(live=org["live"], usage=org["usage"], jobs=org["jobs"], now=NOW)
    found: set = set()
    _walk(rows, found)
    assert not found
    by = {r["slug"]: r for r in rows}
    assert (
        by["tmpl_c1"]["state"] == "active"
        and by["tmpl_c2"]["state"] == "dormant"
        and by["tmpl_c3"]["state"] == "never"
    )
    assert by["tmpl_c1"]["on_roster"] and not by["tmpl_c2"]["on_roster"]
    assert by["tmpl_c1"]["cost_7d_usd"] == 1.5 and by["tmpl_c1"]["answer_only"] is True
    assert by["tmpl_c2"]["jobs_stuck"] == 1 and by["tmpl_c2"]["health"] == "crit"
    assert (
        "breaker_affected" in by["tmpl_c2"]["flags"]
        and "breaker_affected" not in by["tmpl_c3"]["flags"]
    )
    assert (
        by["tmpl_c1"]["is_clone"]
        and by["tmpl_c1"]["template"] == "tmpl"
        and not by["tmpl"]["is_clone"]
    )
    assert by["home_p"]["is_home"]
    page = fleet.list_rows(rows, limit=2)
    assert (
        [r["slug"] for r in page["rows"]] == ["tmpl_c1", "tmpl_c2"]
        and page["total"] == 5
        and page["next_cursor"]
    )
    page2 = fleet.list_rows(rows, cursor=page["next_cursor"], limit=2)
    assert [r["slug"] for r in page2["rows"]] and page2["rows"][0]["slug"] not in (
        "tmpl_c1",
        "tmpl_c2",
    )
    assert [r["slug"] for r in fleet.list_rows(rows, sort="slug")["rows"]][:2] == ["home_p", "tmpl"]
    assert [r["slug"] for r in fleet.list_rows(rows, sort="-cost_7d_usd")["rows"]][0] == "tmpl_c1"
    assert [r["slug"] for r in fleet.list_rows(rows, sort="-health")["rows"]][0] == "tmpl_c2"


def test_filters(org):
    rows = fleet.gather(live=org["live"], usage=org["usage"], jobs=org["jobs"], now=NOW)
    f = lambda **kw: {r["slug"] for r in fleet.list_rows(rows, **kw)["rows"]}  # noqa: E731
    assert f(q="buyer") == {"tmpl_c1", "tmpl_c2", "tmpl_c3"}
    assert f(q="shop") == {"tmpl"}
    assert f(template="tmpl") == {"tmpl_c1", "tmpl_c2", "tmpl_c3"}
    assert f(state="never") == {"home_p", "tmpl", "tmpl_c3"}
    assert f(roster="on") == {"home_p", "tmpl_c1"}
    assert f(answer_only="1") == {"tmpl_c1"}
    assert f(is_clone="0") == {"home_p", "tmpl"}
    assert f(flag="stuck_job") == {"tmpl_c2"}


def test_cache_and_invalidate(org, monkeypatch):
    calls = {"n": 0}
    orig = personas.list_all

    def counting():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(personas, "list_all", counting)
    fleet.gather(live={}, usage={}, jobs=[], now=NOW)
    fleet.gather(live={}, usage={}, jobs=[], now=NOW + 5)
    assert calls["n"] == 1
    fleet.invalidate()
    fleet.gather(live={}, usage={}, jobs=[], now=NOW + 6)
    assert calls["n"] == 2


def test_enrich_page_and_drawer_never_carry_the_owner_id(org, monkeypatch):
    from brain import persona_owners

    monkeypatch.setattr(
        persona_owners, "owner_of_cached", lambda p: "u_8821" if p == "tmpl_c1" else None
    )
    rows = fleet.gather(live=org["live"], usage=org["usage"], jobs=org["jobs"], now=NOW)
    page = fleet.enrich_page(fleet.list_rows(rows, q="buyer one")["rows"])
    r = page[0]
    assert r["owner_bound"] is True and r["owner_ref"] and "u_8821" not in json.dumps(page)
    d = fleet.drawer("tmpl_c1", rows, org["jobs"], org["live"])
    found: set = set()
    _walk(d, found)
    assert not found and "u_8821" not in json.dumps(d)
    assert d["roster"] == {"on": True, "mode": "active", "days": 7}
    assert [j["job_id"] for j in d["jobs"]] == ["j2"] and d["jobs"][0]["content"] is False
    assert "spec" in d and "chemistry" in d
    assert fleet.drawer("nobody", rows, [], {}) is None


def test_cheap_audit_strips_ids_and_records_fingerprints(org, monkeypatch, tmp_path):
    from brain import persona_audit

    snap0 = persona_audit.snapshot("tmpl_c1", cheap=True)
    assert "agent_turns" not in snap0["counts"] and "tasks" not in snap0["counts"]
    a = fleet.cheap_audit("tmpl_c1", now=NOW)
    assert "owner_end_user_id" not in a and "state_root" not in a and a["fingerprint"]
    assert a["fingerprints"][-1]["fingerprint"] == a["fingerprint"]
    assert fleet.cheap_audit("tmpl_c1", now=NOW + 10)["rate_limited"] is True
    b = fleet.cheap_audit("tmpl_c1", now=NOW + 120)
    assert (
        len(b["fingerprints"]) == 2
        and b["fingerprints"][0]["fingerprint"] == b["fingerprints"][1]["fingerprint"]
    )


@pytest.fixture
def console(org, tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    monkeypatch.setattr(org_settings, "instance_seed", lambda: "default")
    from brain.ui.server import UIServer

    actions: list = []

    async def act(action, slug):
        actions.append((action, slug))
        return (
            {"ok": True, "persona": slug}
            if slug != "home_p"
            else {"ok": False, "refused": 400, "error": "home"}
        )

    server = UIServer(
        emitter_queue=asyncio.Queue(),
        jobs_list_fn=lambda limit=20, state=None: [dict(j) for j in org["jobs"]],
        usage_fn=lambda since, until, scope: {"scope": "org", **org["usage"]},
        fleet_fn=lambda: org["live"],
        fleet_action_fn=act,
    )
    client = TestClient(server._build_app())

    def as_role(role):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    return client, as_role, actions, tmp_path


def test_table_and_drawer_routes(console):
    client, as_role, _, _ = console
    as_role("member")
    assert client.get("/fleet/personas").status_code == 403
    as_role("admin")
    d = client.get("/fleet/personas?q=buyer&sort=slug&limit=2").json()
    assert (
        [r["slug"] for r in d["rows"]] == ["tmpl_c1", "tmpl_c2"]
        and d["total"] == 3
        and d["next_cursor"]
    )
    found: set = set()
    _walk(d, found)
    assert not found
    assert "owner_bound" in d["rows"][0] and "learned_state" in d["rows"][0]
    card = client.get("/fleet/personas/tmpl_c2").json()
    assert (
        card["jobs_stuck"] == 1
        and card["jobs"][0]["state"] == "running"
        and "goal" not in card["jobs"][0]
    )
    assert client.get("/fleet/personas/nobody").status_code == 404


def test_actions_are_audited_and_refusals_map_to_status(console):
    client, as_role, actions, tmp_path = console
    as_role("admin")
    assert client.delete("/fleet/personas/tmpl_c1").status_code == 400  # needs ?purge=true
    r = client.delete("/fleet/personas/tmpl_c1?purge=true")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.post("/fleet/personas/tmpl_c1/chemistry/reset").json()["ok"] is True
    assert (
        client.post("/fleet/personas/tmpl_c1/roster", json={"action": "remove"}).json()["ok"]
        is True
    )
    assert client.post("/fleet/personas/tmpl_c1/roster", json={"action": "x"}).status_code == 400
    assert client.delete("/fleet/personas/home_p?purge=true").status_code == 400
    assert [a for a, _ in actions] == ["purge", "chem_reset", "roster_remove", "purge"]
    evs = [json.loads(ln) for ln in (tmp_path / "g.jsonl").read_text().splitlines()]
    assert [e["event"] for e in evs] == [
        "persona_purge",
        "persona_chem_reset",
        "persona_roster_remove",
        "persona_purge",
    ]
    assert evs[-1]["ok"] is False
    as_role("member")
    assert client.post("/fleet/personas/tmpl_c1/chemistry/reset").status_code == 403


def test_owner_lookup_hashes_the_id(console, monkeypatch):
    client, as_role, _, tmp_path = console
    as_role("admin")
    monkeypatch.setattr(fleet, "lookup_owner", lambda eu: ["tmpl_c1"] if eu == "u_8821" else [])
    r = client.post("/fleet/personas/lookup", json={"owner_id": "u_8821"})
    assert r.json() == {"slugs": ["tmpl_c1"]}
    assert client.post("/fleet/personas/lookup", json={}).status_code == 400
    ev = json.loads((tmp_path / "g.jsonl").read_text().splitlines()[-1])
    assert ev["event"] == "owner_lookup" and ev["matches"] == 1 and "u_8821" not in json.dumps(ev)
    assert ev["end_user_hash"] and len(ev["end_user_hash"]) == 12


def test_fleet_action_roster_remove_and_chem_reset(org, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from brain import persona_chem
    from brain.session_loops import _LoopsMixin

    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "chem")
    sess = _LoopsMixin.__new__(_LoopsMixin)
    sess.dmn = SimpleNamespace(__dict__={"_roster_cache": ["x"], "_roster_ts": time.time()})
    sess.persona_name = "home_p"
    assert human_activity.persona_last_turn_ts("tmpl_c1") is not None
    r = asyncio.run(sess.fleet_action("roster_remove", "tmpl_c1"))
    assert r["ok"] and r["stamp_removed"] is True
    assert human_activity.persona_last_turn_ts("tmpl_c1") is None
    assert sess.dmn.__dict__["_roster_cache"] == []
    persona_chem._merge_write(
        "tmpl_c1", resting={"DA": 0.5, "5HT": 0.6}, current={"DA": 0.9, "5HT": 0.1}
    )
    r = asyncio.run(sess.fleet_action("chem_reset", "tmpl_c1"))
    assert r["ok"] and persona_chem.load("tmpl_c1")["current"]["DA"] == 0.5
    assert asyncio.run(sess.fleet_action("bogus", "tmpl_c1"))["refused"] == 400
