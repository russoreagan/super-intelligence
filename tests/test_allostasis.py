"""Allostatic learning: baselines and reactivity learn from stable signals.

The 2026-10-19 audit found session-start chemistry flat for five months (nothing
learned `resting`), reactions that never habituated, and no organically learned
skill ever reaching use. These tests pin the behaviors the biology review asked
for (brain/allostasis.py, the tagged capture in brain/hebbian.py): the mood still
decays to baseline; the baseline moves only from grounded, repeated, spaced signals;
reactivity habituates to the predictable and sensitizes to the uncontrollable;
load recovers; bonds are per relationship; and a strong grounded outcome tags what
sleep keeps.
"""

from __future__ import annotations

import json
import time

import pytest

from brain import allostasis as al
from brain.observability.timeline import TurnTrace
from brain.settings import settings

DAY = 86400.0
T0 = 1_760_000_000.0
P = "the_stoic"


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    al._cache.clear()
    for k in (
        "allostatic_learning",
        "reward_rate_baseline",
        "reactivity_habituation",
        "chronic_load",
        "relationship_bonding",
        "tagged_consolidation",
    ):
        monkeypatch.setitem(settings._data, k, 1)
    return tmp_path


def _turn(
    sid="s1",
    tid="t",
    *,
    persona=P,
    eu="buyer",
    emissions=(),
    grade=None,
    neuromod=None,
    hormonal=None,
    key="",
    impulses=None,
    bond=0.0,
    affection=0,
):
    t = TurnTrace(turn_id=tid, session_id=sid, user_input="x")
    t.persona_name = persona
    t.end_user_id = eu
    t.reward_emissions = [dict(e) for e in emissions]
    t.external_grade = grade
    t.neuromod = dict(neuromod or {})
    t.hormonal = dict(hormonal or {})
    t.trigger_key = key
    t.appraisal_impulses = dict(impulses or {})
    t.bond = bond
    t.affection = affection
    return t


def _session(sid, n=3, **kw):
    return [_turn(sid, f"{sid}-{i}", **kw) for i in range(n)]


SUCCESS = {
    "delta": 0.15,
    "signal_type": "self_graded",
    "reward_source": "mastery",
    "reason": "tool_success",
}
FAILURE = {
    "delta": -0.09,
    "signal_type": "self_graded",
    "reward_source": "mastery",
    "reason": "tool_failure",
}
SELF = {
    "delta": 0.1,
    "signal_type": "self_graded",
    "reward_source": "correctness",
    "reason": "draft_quality_high",
}


# ── grounded signals ─────────────────────────────────────────────────────────


def test_grounded_reward_ignores_self_grading():
    assert al.grounded_reward(_turn(emissions=[SELF])) is None
    assert al.grounded_reward(_turn(emissions=[SUCCESS, SELF])) == pytest.approx(0.15)
    assert al.grounded_reward(_turn(grade=1.0)) == pytest.approx(0.2)
    user = {"delta": 0.05, "signal_type": "external_user", "reason": "sentiment"}
    assert al.grounded_reward(_turn(emissions=[user])) == pytest.approx(0.05)


# ── reward-rate DA ───────────────────────────────────────────────────────────


def test_reward_rate_moves_the_da_baseline_slowly_and_bounded():
    for d in range(40):
        al.learn("x", _session(f"s{d}", emissions=[SUCCESS]), now=T0 + d * DAY)
    off = al.offsets(P, now=T0 + 40 * DAY)
    assert 0.05 < off["DA"] <= 0.10  # brighter, never past the bound
    one = al.load_state(P)["reward"]["r_bar"]
    assert one < 1.0


def test_self_graded_reward_never_moves_the_baseline():
    for d in range(10):
        al.learn("x", _session(f"s{d}", emissions=[SELF]), now=T0 + d * DAY)
    assert "DA" not in al.offsets(P, now=T0 + 10 * DAY)


def test_spacing_one_update_per_source_per_day():
    al.learn("x", _session("a", emissions=[SUCCESS]), now=T0)
    r1 = al.load_state(P)["reward"]["r_bar"]
    al.learn("x", _session("b", emissions=[SUCCESS]), now=T0 + 60)  # same person, same day
    assert al.load_state(P)["reward"]["r_bar"] == r1
    al.learn("x", _session("c", eu="someone_else", emissions=[SUCCESS]), now=T0 + 120)
    assert al.load_state(P)["reward"]["r_bar"] > r1  # a different source, same day
    al.learn("x", _session("d", emissions=[SUCCESS]), now=T0 + DAY)  # next day
    assert al.load_state(P)["reward"]["n"] == 3


def test_short_sessions_do_not_count():
    al.learn("x", _session("a", n=2, emissions=[SUCCESS]), now=T0)
    assert al.load_state(P)["reward"]["n"] == 0


# ── chronic load ─────────────────────────────────────────────────────────────


def _stressed(sid, uncontrolled=True):
    nm = {"GABA": 0.9, "NE": 0.9}
    return _session(
        sid,
        neuromod=nm,
        hormonal={"CORT": 0.6},
        emissions=[FAILURE] if uncontrolled else [SUCCESS],
        impulses={"GABA": 0.05} if uncontrolled else {},
    )


def test_uncontrollable_stress_builds_load_then_recovers():
    for d in range(10):
        al.learn("x", _stressed(f"s{d}"), now=T0 + d * DAY)
    off = al.offsets(P, now=T0 + 10 * DAY)
    assert 0 < off["GABA"] <= 0.05 and off["CORT"] == off["GABA"]
    later = al.offsets(P, now=T0 + 60 * DAY)
    assert later.get("GABA", 0.0) < off["GABA"]  # recovers once the stress stops


def test_controllable_stress_adds_little():
    for d in range(10):
        al.learn("x", _stressed(f"u{d}", uncontrolled=True), now=T0 + d * DAY)
    unc = al.load_state(P)["load"]["L"]
    al.save_state(P, al._empty())
    for d in range(10):
        al.learn("x", _stressed(f"c{d}", uncontrolled=False), now=T0 + d * DAY)
    assert al.load_state(P)["load"]["L"] < unc / 2


# ── per-trigger reactivity ───────────────────────────────────────────────────


def _trig(sid, tid, imp, *, key="debate|neutral", emissions=()):
    # A tense but predictable situation; "hostile|…" keys are the uncontrolled ones.
    return _turn(sid, tid, key=key, impulses={"GABA": imp, "ACh": imp}, emissions=emissions)


def test_predictable_manageable_trigger_habituates_but_threat_keeps_a_floor():
    for d in range(60):
        al.learn("x", [_trig(f"s{d}", "t", 0.05, emissions=[SUCCESS])], now=T0 + d * DAY)
    now = T0 + 60 * DAY
    # Habituated: daily exposure against daily recovery plateaus near 0.7 (floor 0.6).
    assert 0.6 <= al.gain(P, "debate|neutral", "ACh", now=now) < 0.75
    g_threat = al.gain(P, "debate|neutral", "GABA", now=now)
    assert 0.7 <= g_threat < 0.75  # held at the threat floor (a day's recovery on top)
    assert al.gain(P, "brand_new|neutral", "ACh", now=now) == 1.0  # new context: full


def test_uncontrollable_trigger_sensitizes():
    for d in range(10):
        al.learn("x", [_trig(f"s{d}", "t", 0.05, emissions=[FAILURE])], now=T0 + d * DAY)
    assert al.gain(P, "debate|neutral", "GABA", now=T0 + 10 * DAY) > 1.1


def test_reactivity_recovers_when_the_trigger_is_absent():
    for d in range(30):
        al.learn("x", [_trig(f"s{d}", "t", 0.05, emissions=[SUCCESS])], now=T0 + d * DAY)
    soon = al.gain(P, "debate|neutral", "ACh", now=T0 + 30 * DAY)
    later = al.gain(P, "debate|neutral", "ACh", now=T0 + 150 * DAY)
    assert soon < later < 1.0


def test_reactivity_steps_are_capped_per_day():
    traces = [_trig("s", f"t{i}", 0.05, emissions=[SUCCESS]) for i in range(50)]
    al.learn("x", traces, now=T0)
    assert al.gain(P, "debate|neutral", "ACh", now=T0) >= (1 - 0.02) - 1e-4


def test_hypothalamus_applies_the_learned_gain_to_the_appraisal(monkeypatch):
    from brain.bus import Bus
    from brain.clusters.hypothalamus import HypothalamusCluster as Hypothalamus
    from brain.observability.firing_path import reset_current_trace, set_current_trace

    h = Hypothalamus.__new__(Hypothalamus)
    h._bus = Bus()
    monkeypatch.setattr(al, "gain", lambda persona, key, ch, now=None: 0.5)
    nm = h._bus.neuromod
    before = nm.snapshot()
    nm.add("ACh", 0.2)
    trace = TurnTrace(turn_id="t", session_id="s", user_input="x")
    tok = set_current_trace(trace)
    try:
        h._apply_reactivity({"intent": "question", "user_tone_toward_ai": "warm"}, before)
    finally:
        reset_current_trace(tok)
    assert nm.get("ACh") == pytest.approx(before["ACh"] + 0.1, abs=1e-6)
    assert trace.trigger_key == "question|warm"
    assert trace.appraisal_impulses["ACh"] == pytest.approx(0.2, abs=1e-4)  # the RAW impulse


# ── per-relationship bonding ─────────────────────────────────────────────────


def test_bonds_are_per_relationship_spaced_and_fade(monkeypatch):
    for d in range(20):
        al.learn("x", _session(f"s{d}", n=2, bond=90.0, affection=80), now=T0 + d * DAY)
    now = T0 + 20 * DAY
    b = al.bond_offsets(P, "buyer", now=now)
    assert 0.05 < b["OXT"] <= 0.10 and b["CORT"] < 0
    assert al.bond_offsets(P, "stranger", now=now) == {}
    assert al.bond_offsets("the_sage", "buyer", now=now) == {}  # another persona: none
    faded = al.bond_offsets(P, "buyer", now=now + 120 * DAY)
    assert faded["OXT"] < b["OXT"]


def test_erasure_forgets_the_bond_everywhere():
    al.learn("x", _session("s", n=2, bond=90.0, affection=80), now=T0)
    al.learn("x", _session("s", n=2, persona="the_sage", bond=90.0, affection=80), now=T0)
    assert al.forget_person("buyer") == 2
    assert al.bond_offsets(P, "buyer") == {}


# ── baselines on live chemistry ──────────────────────────────────────────────


def test_nothing_changes_before_anything_is_learned():
    from brain.bus import ChemPair

    pair = ChemPair.fresh()
    base = dict(pair.neuromod._baseline)
    assert al.apply_to_pair(pair, P, "buyer") is False
    assert pair.neuromod._baseline == base


def test_learned_baseline_and_bond_reach_the_pair_and_stay_in_the_envelope():
    from brain.bus import ChemPair

    for d in range(40):
        al.learn(
            "x", _session(f"s{d}", emissions=[SUCCESS], bond=90.0, affection=80), now=T0 + d * DAY
        )
    anchor = al._anchor(P)
    pair = ChemPair.fresh()
    assert al.apply_to_pair(pair, P, "buyer") is True
    eff = al.pair_baseline(P, "buyer")
    assert pair.neuromod._baseline["DA"] == pytest.approx(eff["DA"])
    assert eff["DA"] - anchor["DA"] <= 0.10 + 1e-6
    assert all(v <= 0.8 + 1e-9 for v in eff.values())  # the resting ceiling holds
    assert pair.hormonal._baseline["OXT"] > min(1.0, anchor["OXT"]) - 1e-9


def test_mood_still_decays_to_the_learned_baseline():
    from brain.bus import ChemPair

    for d in range(40):
        al.learn("x", _session(f"s{d}", emissions=[SUCCESS]), now=T0 + d * DAY)
    pair = ChemPair.fresh()
    al.apply_to_pair(pair, P, "owner")
    pair.neuromod.add("DA", 0.3)
    pair.neuromod.decay(50)
    assert pair.neuromod.get("DA") == pytest.approx(pair.neuromod._baseline["DA"], abs=0.01)


def test_manual_temperament_edit_resets_learned_offsets():
    from brain import persona_chem

    for d in range(10):
        al.learn("x", _session(f"s{d}", emissions=[SUCCESS]), now=T0 + d * DAY)
    assert al.offsets(P)
    persona_chem.save_resting(P, {**al._anchor(P), "DA": 0.4})
    assert "DA" not in al.offsets(P)


# ── kill switches ────────────────────────────────────────────────────────────


def test_master_switch_stops_everything(monkeypatch):
    monkeypatch.setitem(settings._data, "allostatic_learning", 0)
    assert al.learn("x", _session("s", emissions=[SUCCESS])) == []
    assert al.gain(P, "k", "ACh") == 1.0
    assert al.effective_baseline(P) is None


def test_each_mechanism_has_its_own_switch(monkeypatch):
    monkeypatch.setitem(settings._data, "reward_rate_baseline", 0)
    for d in range(10):
        al.learn("x", _session(f"s{d}", emissions=[SUCCESS]), now=T0 + d * DAY)
    assert "DA" not in al.offsets(P)


# ── exactly once at sleep ────────────────────────────────────────────────────


def test_sleep_stage_learns_each_turn_once(tmp_path, monkeypatch):
    from brain.observability import trace_journal

    monkeypatch.setattr(trace_journal, "_root", lambda: tmp_path)
    monkeypatch.setattr(trace_journal, "_enabled", lambda: True)
    traces = [_trig("s", f"t{i}", 0.05, emissions=[FAILURE]) for i in range(3)]
    fresh = trace_journal.unapplied(trace_journal.STAGE_ALLOSTASIS, traces)
    al.learn("x", fresh, now=T0)
    trace_journal.mark_applied(trace_journal.STAGE_ALLOSTASIS, fresh)
    g1 = al.gain(P, "debate|neutral", "GABA", now=T0)
    again = trace_journal.unapplied(trace_journal.STAGE_ALLOSTASIS, traces)
    assert again == []  # a replayed batch learns nothing twice
    al.learn("x", again, now=T0)
    assert al.gain(P, "debate|neutral", "GABA", now=T0) == g1


def test_summary_is_content_free():
    al.learn("x", _session("s", n=3, emissions=[SUCCESS], bond=90.0, affection=80), now=T0)
    s = al.summary(P)
    assert s["bonds"] == 1 and "buyer" not in json.dumps(s)
    assert set(s) >= {"anchor", "offsets", "effective", "reward_rate", "load", "reactivity"}


def test_state_lives_in_the_persona_state_root(tmp_path):
    from brain.persona_key import persona_state_root

    al.learn("x", _session("s", emissions=[SUCCESS]), now=time.time())
    assert al.state_path(P).parent == persona_state_root(P)
    assert al.state_path(P).is_file()


def test_tension_habituates_but_unresolved_hostility_sensitizes():
    for d in range(20):
        now = T0 + d * DAY
        al.learn("x", [_trig(f"a{d}", "t", 0.05, key="debate|challenging")], now=now)
        al.learn("x", [_trig(f"b{d}", "t", 0.05, key="hostile|hostile")], now=now)
    now = T0 + 20 * DAY
    assert al.gain(P, "debate|challenging", "GABA", now=now) < 0.95
    assert al.gain(P, "hostile|hostile", "GABA", now=now) > 1.1


def test_sleep_consolidation_runs_the_allostasis_stage_once(tmp_path, monkeypatch):
    import asyncio

    from brain.observability import trace_journal
    from tests.test_isolated_mode_gates import _make_sleep

    monkeypatch.setattr(trace_journal, "_root", lambda: tmp_path / "journal")
    monkeypatch.setattr(trace_journal, "_enabled", lambda: True)
    monkeypatch.setitem(settings._data, "cross_learning", 0)
    seen: list[int] = []
    monkeypatch.setattr(al, "learn", lambda sid, traces, now=None: seen.append(len(traces)) or [])
    full = [_trig("s", f"t{i}", 0.05) for i in range(3)]
    s = _make_sleep({})
    summaries = [
        {
            "user_input": "x",
            "entity_response": "y",
            "persona": P,
            "session_id": "s",
            "turn_id": f"t{i}",
        }
        for i in range(3)
    ]
    asyncio.run(s.consolidate("s", summaries, full, []))
    asyncio.run(s.consolidate("s", summaries, full, []))  # a replay of the same batch
    assert seen == [3]


def test_bond_strength_reads_the_relationship_scale():
    # The relationship model's bond is 0..100: an acquaintance at bond 5 with no
    # affection must stay a weak bond, not saturate like a close one.
    weak = _turn("s", "w", bond=5.0, affection=0)
    close = _turn("s", "c", bond=90.0, affection=80)
    assert al._bond_strength(weak) < 0.05
    assert al._bond_strength(close) > 0.8


def test_each_customer_is_its_own_session_in_one_process_batch():
    # Engine lane: every turn carries the PROCESS session id; the conversation is
    # the API session. Two customers in one batch take two spacing slots.
    batch = []
    for eu in ("alice", "bob"):
        for t in _session("proc", n=3, eu=eu, emissions=[SUCCESS]):
            t.api_session_id = f"api-{eu}"
            batch.append(t)
    (report,) = al.learn("x", batch, now=T0)
    assert report["sessions_used"] == 2


def test_saving_an_unchanged_temperament_keeps_learned_offsets():
    # Every persona save (name, voice, text) re-posts the resting profile. Only a
    # real change re-anchors; an unchanged one must not wipe what was learned.
    from brain import persona_chem

    persona_chem.save_resting(P, {**al._anchor(P), "DA": 0.4})
    for d in range(10):
        al.learn("x", _session(f"s{d}", emissions=[SUCCESS]), now=T0 + d * DAY)
    learned = al.offsets(P)
    assert "DA" in learned
    persona_chem.save_resting(P, dict(persona_chem.load(P)["resting"]))
    assert al.offsets(P) == learned


def test_rebaseline_keeps_the_learned_baseline():
    # A Fleet chemistry reset or a settings save re-reads the static baseline into
    # every live pair; the learned shift must be put back, not dropped until sleep.
    from brain.bus import Bus

    for d in range(10):
        al.learn("x", _session(f"s{d}", emissions=[SUCCESS]), now=T0 + d * DAY)
    bus = Bus()
    bus.after_rebaseline = lambda: al.apply_live(bus, P)
    al.apply_live(bus, P)
    learned = bus.resting_chem.neuromod._baseline["DA"]
    bus.after_rebaseline = None
    bus.rebaseline_chem()
    assert bus.resting_chem.neuromod._baseline["DA"] != pytest.approx(learned)  # the bug
    bus.after_rebaseline = lambda: al.apply_live(bus, P)
    bus.rebaseline_chem()
    assert bus.resting_chem.neuromod._baseline["DA"] == pytest.approx(learned)


def test_the_personas_own_reactivity_correction_is_not_grounded_reward():
    t = _turn(
        "s",
        "r",
        emissions=[
            {"delta": -0.05, "signal_type": "external_user", "reason": "reactivity"},
        ],
    )
    assert al.grounded_reward(t) is None


def test_the_isolation_audit_fingerprints_learned_chemistry():
    from brain import persona_audit

    assert al.STATE_FILE in persona_audit.STATE_FILES
