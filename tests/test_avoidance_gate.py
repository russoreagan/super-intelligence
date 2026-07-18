"""
End-to-end tests for the user-avoidance gate — the first *learning* EvidenceGate.

Proves the full live loop on scripted turns: per-client accumulation → commit → learn the
per-persona cue weights from external behaviour (refutation on re-engagement AND positive
confirmation when the agent surfaces a topic the user keeps dodging) → persistence across
restart → farming resistance → steering gated by avoidance_gate.
"""

from __future__ import annotations

import pytest

import brain.avoidance_gate as ag
from brain.avoidance_gate import AvoidanceTracker
from brain.bus import Bus


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path_factory):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "evidence_gates", 1)
    monkeypatch.setitem(settings._data, "avoidance_gate", 1)
    monkeypatch.setitem(settings._data, "avoidance_arm_threshold", 1.5)
    monkeypatch.setitem(settings._data, "avoidance_half_life_s", 1e9)  # no leak in tests
    root = tmp_path_factory.mktemp("avoid_personas")
    monkeypatch.setattr(ag, "persona_state_root", lambda p: root / str(p or "home"))
    yield


def _arm(t, bus, store, entity="secret", emotion="embarrassed", agent_text="", tc=5):
    """One high-evidence turn (not_reengaged + topic_shift + discomfort → drift 3.0 ≥ 1.5)."""
    return t.observe_turn(
        current_entities={"weather"},
        stale_entities={entity: 1},
        turn_count=tc,
        user_emotion=emotion,
        bus=bus,
        agent_text=agent_text,
        store=store,
        now=0.0,
    )


# ── neutral-when-off / accumulate → commit ─────────────────────────────────────


def test_neutral_when_off(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "evidence_gates", 0)
    store: dict = {}
    assert _arm(AvoidanceTracker(), Bus(), store) == []
    assert store == {}


def test_single_high_evidence_turn_arms():
    t = AvoidanceTracker()
    store: dict = {}
    assert "secret" in _arm(t, Bus(), store)
    assert t.is_avoided("secret", store)
    assert t.avoided_entities(store) == ["secret"]


def test_low_evidence_takes_multiple_turns():
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()

    def turn(tc):
        return t.observe_turn(
            current_entities=set(), stale_entities={"topic": 1}, turn_count=tc,
            user_emotion="neutral", bus=bus, store=store, now=0.0,
        )

    assert turn(5) == []  # drift 1.0 < 1.5
    assert "topic" in turn(6)  # 2.0 ≥ 1.5


# ── learning: external refutation + positive confirmation ─────────────────────


def test_reengagement_refutes_and_weakens_cues():
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    _arm(t, bus, store)
    before = t.cue_weights()
    t.observe_turn(
        current_entities={"secret"}, stale_entities={"secret": 1}, turn_count=6,
        user_emotion="neutral", bus=bus, store=store, now=0.0,
    )
    assert not t.is_avoided("secret", store)  # belief cleared by re-engagement
    assert t.cue_weights()["not_reengaged"] < before["not_reengaged"]  # false alarm weakened it
    tally = bus.da_source_tally()
    assert tally["intrinsic"] > 0  # audited — but as a self-inference, NOT external
    assert tally["external"] == 0.0


def test_agent_surfaces_and_user_keeps_dodging_confirms_and_strengthens():
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    # turn 5: arm "secret", and the agent's own reply mentions it → surfaced_turn set
    _arm(t, bus, store, agent_text="we could talk about the secret if you want", tc=5)
    before = t.cue_weights()
    # turn 6: user still not engaging "secret" → the avoidance was real → confirm+strengthen
    t.observe_turn(
        current_entities={"weather"}, stale_entities={"secret": 1}, turn_count=6,
        user_emotion="neutral", bus=bus, agent_text="", store=store, now=0.0,
    )
    assert t.cue_weights()["not_reengaged"] > before["not_reengaged"]


# ── persistence across restart ─────────────────────────────────────────────────


def test_cue_weights_persist_across_tracker_instances():
    bus = Bus()
    store: dict = {}
    t1 = AvoidanceTracker()
    _arm(t1, bus, store)
    t1.observe_turn(  # refute → weakens + persists to disk
        current_entities={"secret"}, stale_entities={"secret": 1}, turn_count=6,
        user_emotion="neutral", bus=bus, store=store, now=0.0,
    )
    learned = t1.cue_weights()["not_reengaged"]
    assert learned < 1.0
    # a fresh tracker (simulating a restart) loads the persisted per-persona weights
    t2 = AvoidanceTracker()
    assert t2.cue_weights()["not_reengaged"] == pytest.approx(learned, abs=1e-9)


# ── anti-farming ───────────────────────────────────────────────────────────────


def test_self_graded_is_discounted_bounded_and_never_external():
    bus = Bus()
    t_ext = AvoidanceTracker()
    t_self = AvoidanceTracker()
    _arm(t_ext, bus, s_e := {})
    _arm(t_self, bus, s_s := {})
    t_ext.confirm("secret", correct=True, bus=bus, external=True, store=s_e)
    t_self.confirm("secret", correct=True, bus=bus, external=False, store=s_s)
    assert (t_ext.cue_weights()["not_reengaged"] - 1.0) > (
        t_self.cue_weights()["not_reengaged"] - 1.0
    ) > 0  # self nudges, external moves it more

    # farming: hammering a self-graded "correct" is clamped and only ever intrinsic.
    t_farm = AvoidanceTracker()
    fbus = Bus()
    for _ in range(200):
        s: dict = {}
        _arm(t_farm, fbus, s)
        t_farm.confirm("secret", correct=True, bus=fbus, external=False, store=s)
    assert t_farm.cue_weights()["not_reengaged"] <= 3.0  # clamped, no runaway
    assert fbus.da_source_tally()["external"] == 0.0  # self-graded never counts as external
    assert fbus.da_source_tally()["intrinsic"] > 0.0  # it lands (audited) in intrinsic


# ── B1: reward provenance + measured informativeness ───────────────────────────


def test_resolution_da_is_self_inference_never_external():
    """A behavioural confirm/refute grades a SELF-generated inference: the DA must
    carry source=self_inference and land in the intrinsic tally — stamping it
    external_grader would inflate the §4.3 honesty ratio in the flattering
    direction."""
    bus = Bus()
    sources: list[str] = []
    orig_add = bus.neuromod.add

    def spy(channel, delta, **kw):
        sources.append(kw.get("source"))
        return orig_add(channel, delta, **kw)

    bus.neuromod.add = spy
    t = AvoidanceTracker()
    store: dict = {}
    _arm(t, bus, store)
    t.observe_turn(  # spontaneous re-engagement → refute → DA emission
        current_entities={"secret"}, stale_entities={"secret": 1}, turn_count=6,
        user_emotion="neutral", bus=bus, store=store, now=0.0,
    )
    assert sources == ["self_inference"]
    tally = bus.da_source_tally()
    assert tally["external"] == 0.0 and tally["intrinsic"] > 0


def test_informativeness_is_measured_from_reengagement_base_rate():
    """Informativeness follows the OBSERVED confirm/refute base rate (1 − dominant
    outcome frequency), not a hardcoded constant — once dodging dominates,
    confirming the near-inevitable pays nothing (§4.8)."""
    from brain.persona_key import active_or_home_persona

    t = AvoidanceTracker()
    persona = active_or_home_persona()
    assert t._informativeness(persona) == pytest.approx(0.5)  # fresh: max uncertainty
    for _ in range(30):
        t._record_resolution(persona, reengaged=False)  # dodging dominates
    skewed = t._informativeness(persona)
    assert skewed < 0.2  # below prediction_informativeness_min → gate closes
    bus = Bus()
    store: dict = {}
    _arm(t, bus, store)
    # the near-inevitable confirm now pays zero DA and moves no cue weight
    assert t.confirm("secret", correct=True, bus=bus, external=True, store=store) == 0.0
    assert bus.da_source_tally()["intrinsic"] == 0.0
    # re-engagements observed → the base rate recovers → informative again
    for _ in range(30):
        t._record_resolution(persona, reengaged=True)
    assert t._informativeness(persona) > skewed


# ── B4: the per-turn DA cap aggregates across resolutions ──────────────────────


def test_da_capped_per_turn_across_entities(monkeypatch):
    """prediction_reward_turn_cap bounds the SUM of resolution DA in a turn — five
    entities refuted in one observe_turn must not pay five caps."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "prediction_reward_base", 0.2)
    monkeypatch.setitem(settings._data, "prediction_reward_turn_cap", 0.02)
    t = AvoidanceTracker()
    bus = Bus()
    store: dict = {}
    ents = {f"topic{i}" for i in range(5)}
    t.observe_turn(  # one high-evidence turn arms all five
        current_entities={"weather"}, stale_entities=dict.fromkeys(ents, 1), turn_count=5,
        user_emotion="embarrassed", bus=bus, store=store, now=0.0,
    )
    assert all(t.is_avoided(e, store) for e in ents)
    t.observe_turn(  # user re-engages all five at once → five refute resolutions
        current_entities=ents, stale_entities=dict.fromkeys(ents, 1), turn_count=6,
        user_emotion="neutral", bus=bus, store=store, now=0.0,
    )
    tally = bus.da_source_tally()
    assert 0 < tally["intrinsic"] <= 0.02 + 1e-9  # one shared cap, not 5 × cap


# ── steering + isolation ───────────────────────────────────────────────────────


def test_deflection_bias_gated_by_subflag(monkeypatch):
    from brain.settings import settings

    t = AvoidanceTracker()
    store: dict = {}
    _arm(t, Bus(), store)
    assert t.deflection_bias(store) is True  # avoidance_gate=1 in fixture
    monkeypatch.setitem(settings._data, "avoidance_gate", 0)
    assert t.deflection_bias(store) is False  # shadow → no steer


def test_per_client_isolation():
    t = AvoidanceTracker()
    bus = Bus()
    a: dict = {}
    b: dict = {}
    t.observe_turn(
        current_entities=set(), stale_entities={"x": 1}, turn_count=5,
        user_emotion="neutral", bus=bus, store=a, now=0.0,
    )
    assert a["avoid:x"]["level"] == pytest.approx(1.0, abs=1e-9)
    assert "avoid:x" not in b
