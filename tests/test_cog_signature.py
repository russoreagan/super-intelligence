"""Cross-domain transfer via cognitive-signature recall.

Pure tests — no LLM, no live store. Cover the four properties the design hinges on:
  1. The signature is CONTENT-FREE (the brutal test): two turns from unrelated
     domains with identical chemistry/structure produce identical signatures and
     match; changing only the topic leaves the signature unchanged.
  2. Approach tags: only the canonical vocabulary is accepted, namespaced + slugged.
  3. Structural match ranks by signature, not topic; the no-match / anomalous
     fallback derives a stance from live state instead of the most-recent memory.
  4. The structural pathway is credited by the recall fan-out Hebbian surface.
"""

from __future__ import annotations

import types

from brain.clusters.hippocampus import (
    APPROACH_TAGS,
    SIGNATURE_KEYS,
    STRUCTURAL_ANOMALY_FLOOR,
    HippocampusCluster,
)
from brain.hebbian import HebbianUpdater
from brain.second_brain.store import EpisodicStore, _signature_cosine
from brain.wiring import Wiring
from brain.wiring_bootstrap import bootstrap


def _cluster() -> HippocampusCluster:
    # Bypass __init__ (needs bus/router/store) — the methods under test use only
    # static helpers and self._wiring (set below where relevant).
    c = object.__new__(HippocampusCluster)
    c._wiring = None
    c._wiring_frozen = False
    return c


# ── 1. The signature is content-free (the brutal test) ──────────────────────


def test_signature_is_content_free_across_domains():
    """Two turns from completely unrelated domains but identical chemistry +
    problem-structure must produce IDENTICAL signatures and match perfectly."""
    c = _cluster()
    nm = {"DA": 0.7, "ACh": 0.5, "GABA": 0.1, "NE": 0.3, "Glu": 0.4}

    audio = {
        "intent": "task",
        "salience": 0.8,
        "requires_action": True,
        "entities": ["reverb", "sidechain", "Ableton"],
        "topic_summary": "mixing a track",
    }
    planning = {
        "intent": "task",
        "salience": 0.8,
        "requires_action": True,
        "entities": ["roadmap", "Q3", "headcount"],
        "topic_summary": "planning a project",
    }

    sig_a = c._build_cog_signature(audio, nm, surprise_score=0.4, inhibition=0.2)
    sig_b = c._build_cog_signature(planning, nm, surprise_score=0.4, inhibition=0.2)

    assert sig_a == sig_b, "domain leaked into the signature"
    assert abs(_signature_cosine(sig_a, sig_b) - 1.0) < 1e-9
    # And it carries no topic/entity content.
    assert set(sig_a) == set(SIGNATURE_KEYS)


def test_changing_only_topic_leaves_signature_unchanged():
    c = _cluster()
    nm = {"DA": 0.6, "ACh": 0.4, "GABA": 0.12, "NE": 0.25, "Glu": 0.3}
    base = {"intent": "question", "salience": 0.5, "entities": ["a"], "topic_summary": "x"}
    other = {"intent": "question", "salience": 0.5, "entities": ["zzz"], "topic_summary": "yyy"}
    assert c._build_cog_signature(base, nm, 0.3) == c._build_cog_signature(other, nm, 0.3)


def test_structure_flags_are_problem_shape_not_domain():
    c = _cluster()
    flags = c._structure_flags({"intent": "task", "requires_action": True, "salience": 0.9})
    assert flags["requires_decomposition"] == 1.0
    assert flags["high_stakes"] == 1.0
    # A pure chitchat question is open-ended, needs no decomposition.
    flags2 = c._structure_flags({"intent": "chitchat", "salience": 0.2})
    assert flags2["open_ended"] == 1.0
    assert flags2["requires_decomposition"] == 0.0


# ── 2. Approach tags ────────────────────────────────────────────────────────


def test_approach_tags_namespaced_and_vocabulary_enforced():
    c = _cluster()
    tags = c._extract_approach_tags(
        {"strategy_tags": ["decomposed-into-steps", "Verified Before Acting", "made_up_tag"]}
    )
    assert "approach:decomposed-into-steps" in tags
    assert "approach:verified-before-acting" in tags  # slugged from spaces/case
    assert not any("made_up_tag" in t for t in tags)  # off-vocabulary rejected


def test_approach_tags_deduped():
    c = _cluster()
    tags = c._extract_approach_tags(
        {"strategy_tags": ["analogized-from-prior", "analogized-from-prior"]}
    )
    assert tags == ["approach:analogized-from-prior"]


def test_canonical_vocabulary_is_small_and_stable():
    assert len(APPROACH_TAGS) == 6  # conservative on purpose


# ── 3. Structural match + fallback stance ──────────────────────────────────


def _store_with_rows(rows):
    store = object.__new__(EpisodicStore)
    store._use_supabase = False
    store._ensure_ready = lambda: True
    store._table = types.SimpleNamespace(
        to_arrow=lambda: types.SimpleNamespace(to_pylist=lambda: rows)
    )
    return store


def test_structural_match_finds_domainA_by_signature_not_topic():
    """A domain-A episode with a matching signature is returned for a domain-B
    query; an episode whose signature is far is not surfaced (low cosine)."""
    sig_match = dict.fromkeys(SIGNATURE_KEYS, 0.5)
    sig_far = {k: (0.0 if i % 2 else 1.0) for i, k in enumerate(SIGNATURE_KEYS)}
    rows = [
        {
            "session_id": "old",
            "user_input": "how do I tame a harsh resonance",
            "entity_response": "sweep a narrow EQ cut",
            "topic_tags": '["audio", "approach:decomposed-into-steps"]',
            "entities": "[]",
            "neuromod_snapshot": "{}",
            "cog_signature": __import__("json").dumps(sig_match),
        },
        {
            "session_id": "old",
            "user_input": "unrelated far episode",
            "entity_response": "nope",
            "topic_tags": '["weather"]',
            "entities": "[]",
            "neuromod_snapshot": "{}",
            "cog_signature": __import__("json").dumps(sig_far),
        },
    ]
    store = _store_with_rows(rows)
    out = store.recall_structural(
        dict.fromkeys(SIGNATURE_KEYS, 0.5),
        approach_tags=["approach:decomposed-into-steps"],
        limit=3,
        exclude_session="current",
    )
    assert out[0]["cog_sim"] == 1.0
    assert out[0]["approach_overlap"] == 1  # boosted by matching approach tag
    assert "audio" in out[0]["topic_tags"]
    # The far episode scores well below the match.
    assert out[-1]["cog_sim"] < out[0]["cog_sim"]


def test_structural_excludes_current_session_and_empty_signatures():
    rows = [
        {
            "session_id": "current",
            "topic_tags": "[]",
            "entities": "[]",
            "neuromod_snapshot": "{}",
            "cog_signature": '{"DA": 0.5}',
        },
        {
            "session_id": "old",
            "topic_tags": "[]",
            "entities": "[]",
            "neuromod_snapshot": "{}",
            "cog_signature": "{}",
        },  # empty sig → skipped
    ]
    store = _store_with_rows(rows)
    out = store.recall_structural({"DA": 0.5}, exclude_session="current")
    assert out == []


def test_fallback_stance_from_state_not_recency():
    c = _cluster()
    threat = c._fallback_stance({"GABA": 0.8, "CORT": 0.3, "DA": 0.1, "ACh": 0.1}, anomalous=False)
    assert threat["stance"] == "caution"
    engage = c._fallback_stance({"GABA": 0.1, "CORT": 0.0, "DA": 0.8, "ACh": 0.6}, anomalous=False)
    assert engage["stance"] == "exploration"


def test_anomalous_state_signals_max_uncertainty():
    c = _cluster()
    st = c._fallback_stance({"GABA": 0.9, "DA": 0.9}, anomalous=True)
    assert st["stance"] == "anomalous"
    assert "no precedent" in st["note"]
    # Anomaly is declared only when even the closest candidate is weak.
    assert STRUCTURAL_ANOMALY_FLOOR < 0.8


# ── 3b. Gate meter (cost lever) ────────────────────────────────────────────


def test_gate_shut_on_routine_turn():
    """No novelty → structural pass never runs, regardless of chemistry."""
    c = _cluster()
    # _structural_gate short-circuits on novelty=False before touching the switch.
    c._structural_recall = types.SimpleNamespace(
        should_fire=lambda *a, **k: (_ for _ in ()).throw(AssertionError("switch consulted"))
    )
    assert c._structural_gate(False, {"ACh": 0.9}, "t1") is False


def test_gate_can_open_on_novelty():
    c = _cluster()
    c._structural_recall = types.SimpleNamespace(should_fire=lambda *a, **k: True)
    assert c._structural_gate(True, {"ACh": 0.9}, "t1") is True


# ── 4. Hebbian credit for the structural pathway ───────────────────────────


def _w():
    w = Wiring()
    bootstrap(w)
    return w


def test_structural_pathway_credited_by_its_share():
    """Structural hits → the mem.recall→hippocampus.structural_recall edge rises."""
    w = _w()
    assert w.has("mem.recall", "hippocampus.structural_recall")  # seeded by bootstrap
    upd = HebbianUpdater(w)
    before = w.get_edge_weight("mem.recall", "hippocampus.structural_recall")
    upd._apply_recall_credit(
        types.SimpleNamespace(
            recall_contrib={"schema": 0, "episode": 0, "structural": 3}, turn_id="t"
        ),
        outcome=0.7,
        plasticity=1.0,
        turn_plast=1.0,
        gainers=[],
        losers=[],
    )
    assert w.get_edge_weight("mem.recall", "hippocampus.structural_recall") > before


def test_structural_share_does_not_break_schema_episode_split():
    """Adding the structural side leaves the existing two-side credit intact when
    structural didn't contribute."""
    w = _w()
    upd = HebbianUpdater(w)
    n = upd._apply_recall_credit(
        types.SimpleNamespace(recall_contrib={"schema": 0, "episode": 4}, turn_id="t"),
        outcome=0.6,
        plasticity=1.0,
        turn_plast=1.0,
        gainers=[],
        losers=[],
    )
    assert n == 2  # episode side only (cosine_recall + time_filter)
