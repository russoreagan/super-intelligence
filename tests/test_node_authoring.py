"""
Tier 2 structural plasticity — self-authoring pass.

Covers the draft → screen → admit flow: a clean screener verdict auto-enables the authored
skill (and scopes it), a flagged verdict leaves it in the owner review queue, and the cadence /
flag / no-signal gates make the pass a no-op. The architect runs on the local model (zero cloud).
"""

from __future__ import annotations

import importlib

from brain.wiring_bootstrap import bootstrap


def _isolated_wiring(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import brain.wiring as w_mod

    importlib.reload(w_mod)
    return w_mod.Wiring()


class _FakeCell:
    def __init__(self, out):
        self.out = out

    def reset_turn(self, t):
        pass

    async def call(self, msgs, **kw):
        return self.out


class _FakeScreener:
    def __init__(self, status):
        self.status = status

    async def screen(self, sid, body, desc):
        return {"status": self.status, "notes": {"static": {}, "judge": {}}}


class _CaptureDecisions:
    def __init__(self):
        self.records = []

    def log(self, decision, **fields):
        self.records.append((decision, fields))


def _fake_registry(monkeypatch, existing=None):
    """Patch skills_registry to an in-memory fake (no Supabase). Returns the call record."""
    rec = {"staged": {}, "status": {}, "all_agents": {}}
    existing = existing or [{"id": "alpha", "description": "do alpha"}]
    monkeypatch.setattr("brain.skills_registry.live_skills", lambda: existing)
    monkeypatch.setattr(
        "brain.skills_registry.stage_skill",
        lambda sid, body, desc="", **k: rec["staged"].__setitem__(sid, (body, desc)) or {"id": sid},
    )
    monkeypatch.setattr(
        "brain.skills_registry.set_status",
        lambda sid, status, **k: rec["status"].__setitem__(sid, status) or {"id": sid},
    )
    monkeypatch.setattr(
        "brain.skills_registry.set_skill_all_agents",
        lambda sid, v: rec["all_agents"].__setitem__(sid, v),
    )
    return rec


_GOOD = (
    '{"skill_id": "synth1", "display_name": "Synth", '
    '"description": "combine alpha and beta", "body": "Do alpha, then beta, carefully."}'
)


def _wiring_with_cluster(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    w.add("fragment.alpha", "frontal.drafter_A", weight=2.5)
    w.add("fragment.beta", "frontal.drafter_A", weight=2.4)
    return w


async def _author(monkeypatch, tmp_path, *, screener_status, out=_GOOD, trace_count=50, persona="p"):
    import brain.node_authoring as na

    cap = _CaptureDecisions()
    monkeypatch.setattr("brain.observability.decisions.decisions", cap)
    w = _wiring_with_cluster(monkeypatch, tmp_path)
    result = await na.author_and_admit(
        persona,
        session_id="s",
        wiring=w,
        architect_cell=_FakeCell(out),
        screener=_FakeScreener(screener_status),
        trace_count=trace_count,
    )
    return result, cap


def test_proven_cluster_evidence_includes_recruited_reserve(monkeypatch, tmp_path):
    """Tier 2 authoring must build on Tier 2 output: a fragment proven on a
    RECRUITED reserve drafter (not one of the 5 fixed drafters) must still surface
    as evidence, or a self-authored/recruited unit could never seed the next
    authored skill."""
    import brain.node_authoring as na

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    w.add("frontal.executive", "frontal.drafter_F", weight=1.0)  # recruit reserve F
    w.add("fragment.recruited_evidence", "frontal.drafter_F", weight=2.5)  # proven (>= 2.2)
    lines = na._proven_cluster_evidence(w, {"recruited_evidence": "learned from a reserve"})
    assert any("recruited_evidence" in line for line in lines)


async def test_clean_verdict_auto_enables_and_scopes(monkeypatch, tmp_path):
    rec = _fake_registry(monkeypatch)
    result, cap = await _author(monkeypatch, tmp_path, screener_status="enabled")
    assert result == {"skill_id": "self-synth1", "status": "enabled"}
    assert "self-synth1" in rec["staged"]
    assert rec["status"]["self-synth1"] == "enabled"
    assert rec["all_agents"]["self-synth1"] is True  # scoped live
    assert any(d == "node_self_authored" and f.get("status") == "enabled" for d, f in cap.records)


async def test_flagged_verdict_stays_in_review_queue(monkeypatch, tmp_path):
    rec = _fake_registry(monkeypatch)
    result, cap = await _author(monkeypatch, tmp_path, screener_status="flagged")
    assert result == {"skill_id": "self-synth1", "status": "flagged"}
    assert rec["status"]["self-synth1"] == "flagged"
    assert "self-synth1" not in rec["all_agents"]  # NOT made live — waits for owner approval
    assert any(d == "node_self_authored" and f.get("status") == "flagged" for d, f in cap.records)


async def test_gated_out_when_flag_off(monkeypatch, tmp_path):
    from brain.settings import settings

    _fake_registry(monkeypatch)
    monkeypatch.setitem(settings._data, "node_self_authoring", 0)
    result, _cap = await _author(monkeypatch, tmp_path, screener_status="enabled")
    assert result is None


async def test_gated_out_when_too_few_traces(monkeypatch, tmp_path):
    _fake_registry(monkeypatch)
    result, _cap = await _author(monkeypatch, tmp_path, screener_status="enabled", trace_count=3)
    assert result is None


async def test_gated_out_without_proven_cluster(monkeypatch, tmp_path):
    import brain.node_authoring as na

    _fake_registry(monkeypatch)
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)  # no proven fragments
    result = await na.author_and_admit(
        "p",
        session_id="s",
        wiring=w,
        architect_cell=_FakeCell(_GOOD),
        screener=_FakeScreener("enabled"),
        trace_count=50,
    )
    assert result is None


async def test_no_supabase_is_noop(monkeypatch, tmp_path):
    import brain.node_authoring as na
    from brain.skills_registry import SkillError

    def _raise():
        raise SkillError("no supabase")

    monkeypatch.setattr("brain.skills_registry.live_skills", _raise)
    w = _wiring_with_cluster(monkeypatch, tmp_path)
    result = await na.author_and_admit(
        "p",
        session_id="s",
        wiring=w,
        architect_cell=_FakeCell(_GOOD),
        screener=_FakeScreener("enabled"),
        trace_count=50,
    )
    assert result is None


async def test_empty_proposal_authors_nothing(monkeypatch, tmp_path):
    rec = _fake_registry(monkeypatch)
    result, _cap = await _author(
        monkeypatch, tmp_path, screener_status="enabled", out='{"skill_id": ""}'
    )
    assert result is None
    assert rec["staged"] == {}


def test_architect_cell_is_local_zero_cloud(monkeypatch, tmp_path):
    """The authoring model runs locally so self-authoring never bills cloud."""

    class _StubSchema:
        async def aappend_fact(self, *a, **kw):
            pass

        def read(self, name):
            return ""

        async def awrite(self, name, content):
            pass

    class _StubEpisodic:
        def encode(self, ep):
            pass

        def recall(self, vec, limit=4):
            return []

        def recall_recent(self, limit=6):
            return []

    class _StubRouter:
        def __init__(self):
            self._call_log = []

        async def call(self, *a, **kw):
            return "{}"

        async def embed(self, text):
            return [0.0] * 16

    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    assert sc._node_architect.locality == "local"
    assert sc._node_architect.model == "runpod-general"
