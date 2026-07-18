"""
End-to-end tests for the user-avoidance gate — the first *learning* EvidenceGate.

Proves the full live loop on scripted turns, and the two behavioural bars the evidence
model must clear (the round-2 false-positive-avalanche fix):
  • a user who simply moved on to other topics NEVER arms the gate;
  • a user who visibly dodges (a surfaced topic not picked up, an abrupt shift with
    discomfort) DOES arm it.
Plus the lifecycle escape hatches: an armed belief expires without user action (leak
release + wall-clock max-armed age-out), and the per-client store stays bounded.
"""

from __future__ import annotations

import pytest

import brain.avoidance_gate as ag
from brain.avoidance_gate import AvoidanceTracker
from brain.bus import Bus

T0 = 1_700_000_000.0  # fixed wall-clock base so decay/expiry are deterministic


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path_factory):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "evidence_gates", 1)
    monkeypatch.setitem(settings._data, "avoidance_gate", 1)
    monkeypatch.setitem(settings._data, "avoidance_arm_threshold", 1.5)
    monkeypatch.setitem(settings._data, "avoidance_half_life_s", 1e9)  # no leak by default
    monkeypatch.setitem(settings._data, "avoidance_max_armed_s", 1e12)  # no age-out by default
    root = tmp_path_factory.mktemp("avoid_personas")
    monkeypatch.setattr(ag, "persona_state_root", lambda p: root / str(p or "home"))
    yield


def _turn(t, bus, store, *, current=(), stale, tc, emotion="neutral", agent_text="", now=T0):
    return t.observe_turn(
        current_entities=set(current),
        stale_entities=dict(stale),
        turn_count=tc,
        user_emotion=emotion,
        bus=bus,
        agent_text=agent_text,
        store=store,
        now=now,
    )


def _arm(t, bus, store, entity="secret", tc=5, now=T0):
    """One visible dodge with discomfort: the entity just crossed stale off a live
    thread (abrupt_shift) while the user shows discomfort → drift 2.0 ≥ 1.5 → arms."""
    return _turn(
        t, bus, store, current={"weather"}, stale={entity: tc - 2}, tc=tc,
        emotion="embarrassed", now=now,
    )


# ── neutral-when-off ───────────────────────────────────────────────────────────


def test_neutral_when_off(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "evidence_gates", 0)
    store: dict = {}
    assert _arm(AvoidanceTracker(), Bus(), store) == []
    assert store == {}


# ── the evidence model: dodges arm, moving on does not ─────────────────────────


def test_user_simply_moved_on_never_arms():
    """Normal topic rotation: 'secret' goes stale and stays unmentioned for 20 turns
    while the user talks about other things, neutral affect throughout. Passive
    staleness must contribute nothing — the entity never arms."""
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    # turn 3: "secret" (last seen turn 1) crosses stale — one abrupt shift, sub-threshold
    _turn(t, bus, store, current={"weather"}, stale={"secret": 1}, tc=3)
    for tc in range(4, 24):  # then ordinary rotation: staleness alone adds NOTHING
        _turn(t, bus, store, current={f"topic{tc}"}, stale={"secret": 1}, tc=tc)
    assert t.avoided_entities(store, now=T0) == []
    assert not t.is_avoided("secret", store, now=T0)
    lvl = (store.get("avoid:secret") or {}).get("level", 0.0)
    assert lvl <= 1.0 + 1e-9  # only the single shift ever accumulated


def test_visible_dodge_twice_arms():
    """The agent surfaces the topic, the user doesn't pick it up — twice. Two real
    dodges must arm the belief."""
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    _turn(t, bus, store, current={"weather"}, stale={"secret": 1}, tc=5,
          agent_text="want to talk about the secret?")
    assert _turn(t, bus, store, current={"news"}, stale={"secret": 1}, tc=6) == []  # one dodge: not yet
    _turn(t, bus, store, current={"news"}, stale={"secret": 1}, tc=7,
          agent_text="circling back to the secret")
    assert "secret" in _turn(t, bus, store, current={"sports"}, stale={"secret": 1}, tc=8)
    assert t.is_avoided("secret", store, now=T0)


def test_abrupt_shift_with_discomfort_arms_single_turn():
    """Changing the subject off a live thread while visibly uncomfortable is one
    strong dodge — arms in a single turn."""
    t = AvoidanceTracker()
    store: dict = {}
    assert "secret" in _arm(t, Bus(), store)
    assert t.avoided_entities(store, now=T0) == ["secret"]


def test_discomfort_alone_is_not_evidence():
    """A discomfort emotion with no dodge (entity long stale, nothing surfaced) must
    not accumulate — the affect isn't about this entity."""
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    for tc in range(5, 15):
        _turn(t, bus, store, current={"work"}, stale={"secret": 1}, tc=tc, emotion="embarrassed")
    assert "avoid:secret" not in store
    assert not t.is_avoided("secret", store, now=T0)


def test_surfacing_consumed_once():
    """One agent mention yields at most ONE surfaced_dodge, however many turns the
    user stays off the topic afterwards."""
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    _turn(t, bus, store, current={"a"}, stale={"secret": 1}, tc=5, agent_text="the secret")
    _turn(t, bus, store, current={"b"}, stale={"secret": 1}, tc=6)  # dodge → 1.0
    _turn(t, bus, store, current={"c"}, stale={"secret": 1}, tc=7)
    _turn(t, bus, store, current={"d"}, stale={"secret": 1}, tc=8)
    assert store["avoid:secret"]["level"] == pytest.approx(1.0, abs=1e-6)


# ── lifecycle: an armed belief can die without user action ─────────────────────


def test_armed_belief_leaks_out_without_fresh_dodges(monkeypatch):
    """B3: passive staleness no longer re-feeds an armed belief, so the leak wins —
    release first, then eviction of the decayed slice."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "avoidance_half_life_s", 900.0)
    t = AvoidanceTracker()  # constructed after the override so the leak is real
    store: dict = {}
    bus = Bus()
    assert "secret" in _arm(t, bus, store, tc=5, now=T0)
    # an hour later: still stale, no dodge → sweep releases (2.0 → 0.125 < 0.75)
    _turn(t, bus, store, current={"news"}, stale={"secret": 3}, tc=6, now=T0 + 3600)
    assert not t.is_avoided("secret", store, now=T0 + 3600)
    # three hours in, the decayed slice is evicted from the persisted store
    _turn(t, bus, store, current={"news"}, stale={"secret": 3}, tc=7, now=T0 + 3 * 3600)
    assert "avoid:secret" not in store
    assert "avoidmeta:secret" not in store


def test_max_armed_age_expires_without_user_action(monkeypatch):
    """B3: even with no leak (belief pinned), a wall-clock age-out clears the slate."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "avoidance_max_armed_s", 3600.0)
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    assert "secret" in _arm(t, bus, store, tc=5, now=T0)
    assert t.is_avoided("secret", store, now=T0 + 60)
    # read path filters the expired belief even before any turn runs (DMN between turns)
    assert not t.is_avoided("secret", store, now=T0 + 4000)
    assert t.deflection_bias(store, now=T0 + 4000) is False
    # and the next turn's sweep actually clears the slate
    _turn(t, bus, store, current={"news"}, stale={"secret": 3}, tc=6, now=T0 + 4000)
    assert "avoid:secret" not in store
    assert "avoidmeta:secret" not in store


# ── learning: external refutation + positive confirmation ─────────────────────


def test_reengagement_refutes_and_weakens_cues():
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    _arm(t, bus, store)
    before = t.cue_weights()
    _turn(t, bus, store, current={"secret"}, stale={"secret": 3}, tc=6)
    assert not t.is_avoided("secret", store, now=T0)  # belief cleared by re-engagement
    assert t.cue_weights()["abrupt_shift"] < before["abrupt_shift"]  # false alarm weakened it
    tally = bus.da_source_tally()
    assert tally["intrinsic"] > 0  # audited — but as a self-inference, NOT external
    assert tally["external"] == 0.0


def test_agent_surfaces_and_user_keeps_dodging_confirms_and_strengthens():
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    _arm(t, bus, store, tc=5)
    # turn 6: the agent's own reply surfaces the flagged entity
    _turn(t, bus, store, current={"news"}, stale={"secret": 3}, tc=6,
          agent_text="we could talk about the secret if you want")
    before = t.cue_weights()
    # turn 7: user still not engaging it → the avoidance was real → confirm+strengthen
    _turn(t, bus, store, current={"news"}, stale={"secret": 3}, tc=7)
    assert t.cue_weights()["abrupt_shift"] > before["abrupt_shift"]


# ── persistence across restart ─────────────────────────────────────────────────


def test_cue_weights_persist_across_tracker_instances():
    bus = Bus()
    store: dict = {}
    t1 = AvoidanceTracker()
    _arm(t1, bus, store)
    t1.observe_turn(  # refute → weakens + persists to disk
        current_entities={"secret"}, stale_entities={"secret": 3}, turn_count=6,
        user_emotion="neutral", bus=bus, store=store, now=T0,
    )
    learned = t1.cue_weights()["abrupt_shift"]
    assert learned < 1.0
    # a fresh tracker (simulating a restart) loads the persisted per-persona weights
    t2 = AvoidanceTracker()
    assert t2.cue_weights()["abrupt_shift"] == pytest.approx(learned, abs=1e-9)


# ── anti-farming ───────────────────────────────────────────────────────────────


def test_self_graded_is_discounted_bounded_and_never_external():
    bus = Bus()
    t_ext = AvoidanceTracker()
    t_self = AvoidanceTracker()
    _arm(t_ext, bus, s_e := {})
    _arm(t_self, bus, s_s := {})
    t_ext.confirm("secret", correct=True, bus=bus, external=True, store=s_e)
    t_self.confirm("secret", correct=True, bus=bus, external=False, store=s_s)
    assert (t_ext.cue_weights()["abrupt_shift"] - 1.0) > (
        t_self.cue_weights()["abrupt_shift"] - 1.0
    ) > 0  # self nudges, external moves it more

    # farming: hammering a self-graded "correct" is clamped and only ever intrinsic.
    t_farm = AvoidanceTracker()
    fbus = Bus()
    for _ in range(200):
        s: dict = {}
        _arm(t_farm, fbus, s)
        t_farm.confirm("secret", correct=True, bus=fbus, external=False, store=s)
    assert t_farm.cue_weights()["abrupt_shift"] <= 3.0  # clamped, no runaway
    tally = fbus.da_source_tally()
    assert tally.get("external", 0.0) == 0.0  # self-graded never counts as external
    assert sum(tally.values()) > 0.0  # it still lands (audited) elsewhere


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
    # spontaneous re-engagement → refute → DA emission
    _turn(t, bus, store, current={"secret"}, stale={"secret": 3}, tc=6)
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
    # one visible-dodge turn arms all five (each just crossed stale, discomfort riding)
    _turn(t, bus, store, current={"weather"}, stale=dict.fromkeys(ents, 3), tc=5,
          emotion="embarrassed")
    assert all(t.is_avoided(e, store, now=T0) for e in ents)
    # user re-engages all five at once → five refute resolutions in one turn
    _turn(t, bus, store, current=ents, stale=dict.fromkeys(ents, 3), tc=6)
    tally = bus.da_source_tally()
    assert 0 < tally["intrinsic"] <= 0.02 + 1e-9  # one shared cap, not 5 × cap


# ── steering + isolation ───────────────────────────────────────────────────────


def test_deflection_bias_gated_by_subflag(monkeypatch):
    from brain.settings import settings

    t = AvoidanceTracker()
    store: dict = {}
    _arm(t, Bus(), store)
    assert t.deflection_bias(store, now=T0) is True  # avoidance_gate=1 in fixture
    monkeypatch.setitem(settings._data, "avoidance_gate", 0)
    assert t.deflection_bias(store, now=T0) is False  # shadow → no steer


def test_per_client_isolation():
    t = AvoidanceTracker()
    bus = Bus()
    a: dict = {}
    b: dict = {}
    _turn(t, bus, a, current={"y"}, stale={"x": 3}, tc=5)  # abrupt shift → 1.0 in a only
    assert a["avoid:x"]["level"] == pytest.approx(1.0, abs=1e-9)
    assert "avoid:x" not in b


# ── bounded stores (B5) ────────────────────────────────────────────────────────


def test_stale_bare_surfacing_records_evicted():
    """A surfacing the user never got to dodge (entity dropped from the stale map)
    doesn't linger in the persisted store."""
    t = AvoidanceTracker()
    store: dict = {}
    bus = Bus()
    _turn(t, bus, store, current={"a"}, stale={"secret": 1}, tc=5, agent_text="secret stuff")
    assert store["avoidmeta:secret"]["surfaced_turn"] == 5
    _turn(t, bus, store, current={"b"}, stale={}, tc=6)  # still fresh, kept one turn
    assert "avoidmeta:secret" in store
    _turn(t, bus, store, current={"c"}, stale={}, tc=7)  # stale → swept
    assert "avoidmeta:secret" not in store


def test_parietal_entity_map_bounded():
    from brain.clusters.parietal import MAX_TRACKED_ENTITIES, ParietalCluster

    p = ParietalCluster(Bus())
    for i in range(MAX_TRACKED_ENTITIES + 50):
        p.update({"entities": [f"e{i}"]}, "hi", "yo")
    seen = p.entity_last_seen()
    assert len(seen) == MAX_TRACKED_ENTITIES
    assert "e0" not in seen  # oldest evicted
    assert f"e{MAX_TRACKED_ENTITIES + 49}" in seen  # newest kept
