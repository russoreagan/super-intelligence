"""Console navigation refresh (design handoff 2A "Atlas Console"): the /app deep-link
route, The Admin's opening briefing (digest, fallback, cache) and the nav model the
shell is built from."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from brain import admin_briefing

UI = Path(__file__).resolve().parent.parent / "brain" / "ui"


# ── digest / prompt / fallback (pure) ─────────────────────────────────────────


def _live(**kw):
    base = {
        "dmn": {"enabled": True, "dormant": False, "roster": {"size": 3}},
        "tasks": {"queued": 0},
        "breaker": {},
    }
    base.update(kw)
    return base


def test_digest_is_content_free_and_ranks_urgent_first():
    jobs = [
        {"state": "awaiting_approval", "goal": "email the board the Q3 numbers"},
        {"state": "running", "goal": "reconcile CRM"},
        {"state": "failed", "goal": "sync wiki", "updated_at": 1_000_000.0},
    ]
    d = admin_briefing.build_digest(
        live=_live(breaker={"anthropic": {"strikes": 2}}),
        jobs=jobs,
        approvals=2,
        cost_today_usd=12.3456,
        connectors=[
            {"name": "linear", "status": "error"},
            {"name": "slack", "status": "connected"},
        ],
        alerts=[
            {"code": "breaker_open", "severity": "crit"},
            {"code": "roster_stale", "severity": "warn"},
        ],
        health="crit",
        learning_mode="isolated",
        running_persona="The Analyst",
        agents_total=4,
        agents_paused=1,
        now=1_000_100.0,
    )
    flat = repr(d)
    assert "Q3 numbers" not in flat and "reconcile" not in flat  # no job goals, ever
    assert d["jobs"] == {"open": 2, "awaiting_approval": 1, "failed_24h": 1, "queued": 0}
    assert d["approvals_pending"] == 2 and d["cost_today_usd"] == 12.35
    assert d["breaker"] == ["anthropic"] and d["connectors_error"] == ["linear"]
    facts = admin_briefing.notable(d)
    assert facts[0].startswith("anthropic is rejecting")  # the breaker outranks everything
    assert any(f.startswith("2 actions waiting") for f in facts)
    assert any("linear" in f for f in facts)
    assert facts[-1] == "Spend today is $12.35."
    text = admin_briefing.fallback_text(d, "russ@example.com")
    assert text.startswith("Hello, russ.") and "anthropic" in text
    assert "\n" not in text and "*" not in text


def test_member_projection_drops_spend_and_approvals():
    d = admin_briefing.build_digest(
        live=_live(), jobs=[], approvals=5, cost_today_usd=9.0, alerts=[], full=False
    )
    assert "approvals_pending" not in d and "cost_today_usd" not in d and "breaker" not in d
    assert d["full"] is False
    assert admin_briefing.fallback_text(d) == "Hello. Nothing needs you right now."


def test_quiet_org_fallback_mentions_spend_only():
    d = admin_briefing.build_digest(live=_live(), jobs=[], cost_today_usd=0.42, alerts=[])
    assert admin_briefing.fallback_text(d, "ops@acme.io") == (
        "Hello, ops. Nothing needs you right now. Spend today is $0.42."
    )


def test_prompts_carry_identity_rules_and_digest():
    d = admin_briefing.build_digest(live=_live(dmn={"enabled": False}), jobs=[], alerts=[])
    sp = admin_briefing.system_prompt("# Self\n\n## Who I am\nThe internal operator.")
    assert "The Admin" in sp and "The internal operator." in sp and "2 to 4 short sentences" in sp
    up = admin_briefing.user_prompt(d, "russ@example.com", local_hour=9)
    assert "this morning" in up and "idle loop is switched off" in up and '"dmn"' in up


def test_briefing_cache_expires_per_scope():
    c = admin_briefing.BriefingCache(ttl_s=100)
    c.put("admin", "hi", {}, now=1000.0)
    assert c.get("admin", now=1050.0)["text"] == "hi"
    assert c.get("member", now=1050.0) is None
    assert c.get("admin", now=1101.0) is None


# ── console routes ────────────────────────────────────────────────────────────


@pytest.fixture
def console(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from brain.ui import auth as ui_auth
    from brain.ui.server import UIServer

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)

    calls: list[dict] = []
    fail = {"on": False}

    async def briefing_fn(digest):
        calls.append(dict(digest))
        if fail["on"]:
            raise RuntimeError("provider down")
        return "  Good morning. 1 approval is waiting; everything else is quiet.  "

    server = UIServer(
        emitter_queue=asyncio.Queue(),
        approvals_fn=lambda: [{"id": "a1"}],
        jobs_list_fn=lambda limit, state: [{"state": "awaiting_approval"}],
        fleet_fn=lambda: {"dmn": {"enabled": True, "dormant": True}, "tasks": {}, "breaker": {}},
        briefing_fn=briefing_fn,
    )
    client = TestClient(server._build_app())

    def as_role(role):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    return client, as_role, calls, fail, server


def test_app_deep_links_serve_the_shell(console):
    client, as_role, _c, _f, _s = console
    as_role("member")
    for path in (
        "/app/mri/live",
        "/app/agents/connectors",
        "/app/settings/tenants",
        "/app/learning",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'id="shell-rail"' in r.text and 'id="ws-tabs"' in r.text
    # JSON namespaces are untouched: /settings is still the settings payload, not HTML.
    assert "text/html" not in client.get("/agents/usage").headers.get("content-type", "")


def test_briefing_is_written_by_the_admin_then_cached(console):
    client, as_role, calls, _f, _s = console
    as_role("admin")
    r = client.post("/admin/briefing")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persona"] == "The Admin" and body["cached"] is False
    assert body["text"] == "Good morning. 1 approval is waiting; everything else is quiet."
    assert body["digest"]["approvals_pending"] == 1 and body["digest"]["full"] is True
    assert body["digest"]["dmn"]["dormant"] is True
    assert calls and calls[0]["viewer"] == "" and "approvals_pending" in calls[0]
    # Second open within the TTL: same text, same ts, no second model call.
    r2 = client.post("/admin/briefing")
    assert r2.json()["cached"] is True and r2.json()["ts"] == body["ts"]
    assert len(calls) == 1
    # force=1 recomputes.
    assert client.post("/admin/briefing?force=1").json()["cached"] is False
    assert len(calls) == 2


def test_briefing_member_projection_and_fallback(console):
    client, as_role, calls, fail, _s = console
    as_role("member")
    fail["on"] = True
    r = client.post("/admin/briefing")
    assert r.status_code == 200
    body = r.json()
    # The model failed → the content-free fallback greeting, never an error.
    assert body["text"].startswith("Hello.")
    assert "approvals_pending" not in body["digest"] and body["digest"]["full"] is False
    assert "1 job paused awaiting approval" in body["text"]


def test_briefing_without_a_writer_uses_fallback(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from brain.ui import auth as ui_auth
    from brain.ui.server import UIServer

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: True)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    client = TestClient(UIServer(emitter_queue=asyncio.Queue())._build_app())
    body = client.post("/admin/briefing").json()
    assert body["text"] == "Hello. Nothing needs you right now. Spend today is $0.00."


# ── the nav model the shell is built from (static guards on the JS source) ────


def test_nav_model_tab_order_and_settings_rows():
    js = (UI / "workspaces.js").read_text(encoding="utf-8")
    assert "const TAB_ORDER = ['labs', 'agents', 'fleet', 'learning', 'api'];" in js
    assert "const HOME = { section: 'labs', key: 'live' };" in js
    nav = js[js.index("const NAV = {") : js.index("const TAB_ORDER")]
    # Every product section has its group label from the handoff; the three
    # credential classes each have their own page.
    for label in (
        "Observation",
        "Agent workspace",
        "Fleet operations",
        "Retention",
        "Developer home",
    ):
        assert f"group: '{label}'" in nav, label
    assert "label: 'Client keys'" in nav and "label: 'Model providers'" in nav
    assert "label: 'Connectors'" in nav
    # Superadmin rows are permission-filtered rows in the same list, not a mode.
    assert re.search(r"key: 'tenants'.*perm: 'isAdmin', platform: true", nav)
    assert "viewer role" not in js.lower() or "no viewer-role switcher" in js
    # Each credential page carries the callout; the copy is the handoff's, verbatim.
    for phrase in (
        "They are not the keys that run the models, and not the keys your systems use to call us.",
        "they never touch a model vendor, and they are not the credentials your agents use to reach other tools.",
        "They are not the keys your systems use to call us, and not the tool credentials your agents use.",
    ):
        assert phrase in js, phrase


def test_index_shell_markup():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'id="ws-tabs"' in html and 'id="shell-rail"' in html and 'id="shell-content"' in html
    assert 'id="console-back-btn"' in html and 'id="ws-settings-crumb"' in html
    assert 'id="ws-fleet"' in html and 'id="ws-settings-extra"' in html
    assert 'id="ws-menu"' not in html  # the dropdown switcher is gone
    assert "--navy-deep:" in html
    assert "requestAdminBriefing" in html


def test_shell_layout_is_responsive():
    css = (UI / "workspaces.css").read_text(encoding="utf-8")
    block = css[css.index("RESPONSIVE SHELL") :]
    # MRI's side columns are clamped proportions of the space next to the rail,
    # never a fixed pixel pair, and the middle track is a plain 1fr (Chrome will
    # not transition a minmax() track, and a track transition from 0px sticks).
    assert "clamp(220px, 24%, 480px) 1fr clamp(240px, 27%, 424px)" in block
    assert "transition: none" in block
    # Breakpoints: the rail folds to a toggle at 1100, the specimen column at 900,
    # both side columns at 760.
    for bp in (
        "@media (max-width: 1100px)",
        "@media (max-width: 900px)",
        "@media (max-width: 760px)",
    ):
        assert bp in block, bp
    assert "body.rail-open #shell-rail" in block
    js = (UI / "workspaces.js").read_text(encoding="utf-8")
    assert 'id="shell-rail-toggle"' in js and "function setRailOpen" in js
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert "function effectiveWidths()" in html and "let leftChoice" in html


def test_mri_has_no_rail():
    js = (UI / "workspaces.js").read_text(encoding="utf-8")
    assert "rail.hidden = workspace === 'labs';" in js
    assert "#shell-rail[hidden] { display: none; }" in (UI / "workspaces.css").read_text(
        encoding="utf-8"
    )
