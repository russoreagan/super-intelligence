"""
Tests for the hardened DMN internal-thoughts pipeline:

  - skip-and-backoff resilience (a failing local model makes the loop back off
    instead of stalling or hammering the shared model)
  - semantic dedup gate (catches paraphrases + thoughts beyond the word window)
  - novelty memory persistence across a simulated restart
  - idle-gated, chemistry dual-driver rumination router (anxious vs engaged)
  - rumination depth cap + seed-exempt emission
  - instrumentation of skill picks / rumination chains via the decision log
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import brain.dmn as D
from brain.dmn import DefaultModeNetwork, _cosine
from brain.settings import settings


def _make_dmn():
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._bus.neuromod = MagicMock()
    dmn._bus.neuromod.snapshot = MagicMock(return_value={})
    dmn._bus.neuromod.add = MagicMock()
    dmn._bus.hormonal = MagicMock()
    dmn._bus.hormonal.snapshot = MagicMock(return_value={})
    dmn._bus.hormonal.add = MagicMock()
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    dmn._hippocampus = None
    dmn._parietal = None
    dmn._obs = None
    dmn._running = True
    dmn._last_context = "Recent: hello world"
    dmn._thought_count = 0
    dmn._recent_thoughts = deque(maxlen=10)
    dmn._recent_angles = deque(maxlen=8)
    dmn._suppressed_count = 0
    dmn._session_id = "test"
    dmn._last_emotion = "neutral"
    dmn._last_speaker_name = None
    dmn._last_affection_score = 0
    dmn._last_familiarity = "new"
    dmn._last_projects = ""
    dmn._session_thought_buf = []
    dmn._session_thought_limit = 200
    dmn._candidate_q = deque(maxlen=8)
    dmn._self_task_q = deque(maxlen=4)
    dmn._skill_selector = None
    # mocked monologue cell
    dmn._monologue_cell = MagicMock()
    dmn._monologue_cell.reset_turn = MagicMock()
    dmn._monologue_cell.call = AsyncMock(return_value="A fresh new thought.")
    dmn._simulation_cell = MagicMock()
    dmn._simulation_cell.reset_turn = MagicMock()
    dmn._simulation_cell.call = AsyncMock(return_value="{}")
    dmn.predicted_next = None
    dmn.last_was_question = False
    dmn.last_assistant_message = ""
    dmn.anticipations = []
    dmn.prefetched = []
    dmn._ensure_runtime_state()
    return dmn


def _meta(angle=None, spoken=None):
    return {
        "angle": angle,
        "spoken_form": spoken,
        "task_goal": None,
        "is_propose": False,
        "is_plan": False,
        "defer_text": None,
        "defer_urgency": "high",
        "defer_tags": [],
        "chem_delta": {},
    }


# ── Skip-and-backoff resilience ─────────────────────────────────────────────


def test_note_tick_outcome_backs_off_then_resets():
    dmn = _make_dmn()
    after = int(settings.get("dmn_backoff_after_failures"))
    # Failures below the threshold don't back off yet.
    for _ in range(after - 1):
        dmn._note_tick_outcome(False)
    assert dmn._backoff_mult == 1.0
    # Crossing the threshold starts geometric backoff.
    dmn._note_tick_outcome(False)
    assert dmn._backoff_mult > 1.0
    assert dmn._consec_errors == after
    # A success fully resets.
    dmn._note_tick_outcome(True)
    assert dmn._backoff_mult == 1.0
    assert dmn._consec_errors == 0
    assert dmn._last_tick_failed is False


def test_backoff_is_capped():
    dmn = _make_dmn()
    for _ in range(50):
        dmn._note_tick_outcome(False)
    assert dmn._backoff_mult <= float(settings.get("dmn_backoff_max_multiplier"))


def test_current_interval_reflects_backoff():
    dmn = _make_dmn()
    base = dmn._current_interval()
    dmn._backoff_mult = 4.0
    assert dmn._current_interval() == base * 4.0


def test_tick_survives_model_failure_and_counts_it():
    """An injected monologue failure must skip-and-continue, not crash, and the
    failure must register so backoff can kick in."""
    dmn = _make_dmn()
    dmn._monologue_cell.call = AsyncMock(side_effect=RuntimeError("model down"))
    asyncio.run(dmn._tick())  # must not raise
    assert dmn._consec_errors == 1
    assert dmn._last_tick_failed is True
    # Recovery on next good tick.
    dmn._monologue_cell.call = AsyncMock(return_value="Recovered thought.")
    asyncio.run(dmn._tick())
    assert dmn._consec_errors == 0
    assert dmn._last_tick_failed is False


def test_empty_model_output_counts_as_failure():
    dmn = _make_dmn()
    dmn._monologue_cell.call = AsyncMock(return_value="")
    asyncio.run(dmn._tick())
    assert dmn._consec_errors == 1


def test_health_snapshot_shape():
    dmn = _make_dmn()
    h = dmn.health()
    for k in (
        "consecutive_errors",
        "backoff_multiplier",
        "suppressed_count",
        "candidate_queue_depth",
        "ruminations_in_progress",
    ):
        assert k in h


# ── Semantic dedup ──────────────────────────────────────────────────────────


def _vec_router(mapping):
    """Router whose embed returns a fixed vector per exact input string."""
    r = MagicMock()

    async def _embed(text):
        return mapping.get(text)

    r.embed = _embed
    return r


def test_semantic_dedup_suppresses_paraphrase_with_low_word_overlap():
    dmn = _make_dmn()
    a = "The microphone keeps capturing my own speaker output."
    b = "Audio feedback from playback is being recorded again."
    # Near-identical embeddings, but few shared content words.
    dmn._router = _vec_router({a: [1.0, 0.0, 0.0], b: [0.99, 0.01, 0.0]})
    dmn._recent_thoughts.append(a)
    dmn._recent_embeddings.append([1.0, 0.0, 0.0])
    asyncio.run(dmn._process_thought(b, _meta(), "dmn_1"))
    assert dmn._suppressed_count == 1
    assert _cosine([1.0, 0.0, 0.0], [0.99, 0.01, 0.0]) >= 0.88


def test_semantic_dedup_catches_thought_beyond_word_window():
    """A thought similar to one 6 back (outside the narrow word window) is still
    caught by the full-window semantic gate."""
    dmn = _make_dmn()
    old = "I wonder how the hippocampus consolidation cycle decides salience."
    # Fill 6 unrelated thoughts after `old` (orthogonal vectors).
    dmn._recent_thoughts.append(old)
    dmn._recent_embeddings.append([1.0, 0.0, 0.0])
    for i in range(6):
        dmn._recent_thoughts.append(f"unrelated filler thought number {i}")
        dmn._recent_embeddings.append([0.0, 1.0, 0.0])
    new = "How does memory consolidation pick what is worth keeping?"
    dmn._router = _vec_router({new: [0.98, 0.0, 0.0]})
    asyncio.run(dmn._process_thought(new, _meta(), "dmn_8"))
    assert dmn._suppressed_count == 1


def test_distinct_thought_passes_semantic_gate():
    dmn = _make_dmn()
    a = "Thinking about audio routing."
    b = "Thinking about the user's travel plans."
    dmn._router = _vec_router({a: [1.0, 0.0], b: [0.0, 1.0]})
    dmn._recent_thoughts.append(a)
    dmn._recent_embeddings.append([1.0, 0.0])
    asyncio.run(dmn._process_thought(b, _meta(), "dmn_1"))
    assert dmn._suppressed_count == 0
    assert len(dmn._recent_thoughts) == 2


# ── Rumination seed-exemption ───────────────────────────────────────────────


def test_rumination_output_is_seed_exempt_but_normal_repeat_is_not():
    seed = "Maybe the recurring tension is about trust, not competence."
    deepened = "The recurring tension is really about trust rather than competence."

    # Normal path: deepened text repeats the seed → suppressed.
    dmn1 = _make_dmn()
    dmn1._recent_thoughts.append(seed)
    dmn1._recent_embeddings.append(None)
    asyncio.run(dmn1._process_thought(deepened, _meta(), "dmn_1"))
    assert dmn1._suppressed_count == 1

    # Rumination path: same text, but seed-exempt → emitted.
    dmn2 = _make_dmn()
    dmn2._recent_thoughts.append(seed)
    dmn2._recent_embeddings.append(None)
    asyncio.run(
        dmn2._process_thought(deepened, _meta(), "dmn_1", exempt_seed=seed, source_tag="rumination")
    )
    assert dmn2._suppressed_count == 0
    assert dmn2._bus.publish_dict.await_count == 1
    payload = dmn2._bus.publish_dict.await_args.args[1]
    assert payload.get("rumination") is True


# ── Novelty persistence across restart ──────────────────────────────────────


def test_novelty_persists_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "NOVELTY_STATE_PATH", tmp_path / "dmn_novelty.json")
    dmn = _make_dmn()
    dmn._recent_thoughts.append("A persistent idea about scheduling.")
    dmn._recent_angles.append("scheduling")
    dmn._last_rumination_seed = "scheduling seed"
    dmn._persist_novelty()

    fresh = _make_dmn()
    fresh._recent_thoughts.clear()
    fresh._recent_angles.clear()
    fresh._load_novelty()
    assert any("persistent idea" in t for t in fresh._recent_thoughts)
    assert "scheduling" in fresh._recent_angles
    assert fresh._last_rumination_seed == "scheduling seed"


# ── Idle-gated dual-driver rumination router ────────────────────────────────


def test_rumination_drive_flavors():
    dmn = _make_dmn()
    anx, flavor_a = dmn._rumination_drive({"CORT": 0.8, "NE": 0.7, "5HT": 0.05})
    assert flavor_a == "anxious"
    assert anx > 0
    eng, flavor_e = dmn._rumination_drive({"DA": 0.9, "ACh": 0.85, "5HT": 0.1})
    assert flavor_e == "engaged"
    assert eng > 0


def test_rumination_never_fires_during_live_conversation(monkeypatch):
    """Idle is a hard precondition: high drive but user active → always normal."""
    monkeypatch.setattr(D, "get_idle_seconds", lambda: 0.0)
    dmn = _make_dmn()
    for _ in range(40):
        mode, _flavor, drive = dmn._rumination_decision({"CORT": 0.9, "NE": 0.8, "5HT": 0.0})
        assert mode == "normal"
    assert drive > float(settings.get("dmn_rumination_drive_threshold"))


def test_rumination_eligible_when_idle_with_high_drive(monkeypatch):
    monkeypatch.setattr(D, "get_idle_seconds", lambda: 999.0)
    monkeypatch.setattr(D.random, "random", lambda: 0.0)  # force the probabilistic fire
    dmn = _make_dmn()
    mode, flavor, _drive = dmn._rumination_decision({"CORT": 0.9, "NE": 0.8, "5HT": 0.0})
    assert mode == "ruminate"
    assert flavor == "anxious"


def test_rumination_depth_cap(monkeypatch):
    monkeypatch.setattr(D, "get_idle_seconds", lambda: 999.0)
    monkeypatch.setattr(D.random, "random", lambda: 0.0)
    dmn = _make_dmn()
    dmn._consecutive_ruminations = int(settings.get("dmn_rumination_max_consecutive"))
    mode, _flavor, _drive = dmn._rumination_decision({"CORT": 0.9, "NE": 0.8, "5HT": 0.0})
    assert mode == "normal"  # capped — stop deepening the same seed


def test_low_drive_idle_stays_normal(monkeypatch):
    monkeypatch.setattr(D, "get_idle_seconds", lambda: 999.0)
    dmn = _make_dmn()
    mode, _flavor, _drive = dmn._rumination_decision({"5HT": 0.9})  # calm → low drive
    assert mode == "normal"


# ── Rumination run + instrumentation ────────────────────────────────────────


def test_run_rumination_emits_and_logs(monkeypatch):
    logged = []
    monkeypatch.setattr(
        "brain.observability.decisions.decisions.log",
        lambda name, **kw: logged.append((name, kw)),
    )
    dmn = _make_dmn()
    dmn._recent_thoughts.append("Seed: the cadence of our check-ins feels off.")
    dmn._recent_embeddings.append(None)

    selector = MagicMock()
    chain = [
        {"thought": "seed", "skill": None, "parent": None, "mode": "seed"},
        {
            "thought": "a deeper take via systems lens",
            "skill": "systems-feedback-mapping",
            "parent": 0,
            "mode": "branch",
        },
    ]
    selector.ruminate = AsyncMock(return_value=("A synthesized deeper take.", chain))
    dmn._skill_selector = selector

    produced = asyncio.run(dmn._run_rumination("dmn_9", {}, "engaged", 0.7))
    assert produced is True
    assert dmn._bus.publish_dict.await_count == 1
    assert dmn._consecutive_ruminations == 1
    names = [n for n, _ in logged]
    assert "dmn_rumination" in names
    rum_kw = dict(logged[[n for n, _ in logged].index("dmn_rumination")][1])
    assert rum_kw.get("flavor") == "engaged"
    assert "skills" in rum_kw


def test_apply_monologue_skills_logs_pick(monkeypatch):
    monkeypatch.setattr(D, "get_idle_seconds", lambda: 999.0)
    logged = []
    monkeypatch.setattr(
        "brain.observability.decisions.decisions.log",
        lambda name, **kw: logged.append((name, kw)),
    )
    dmn = _make_dmn()
    dmn._monologue_baseline_skills = ["logic-check", "emotional"]
    dmn._recent_thoughts.append("A thought worth analyzing from a fresh angle.")

    from brain.clusters.skill_selector import SkillBundle

    selector = MagicMock()
    selector.select_autonomous = AsyncMock(
        return_value=SkillBundle(
            tier1=["logic-check"], chosen=["systems-feedback-mapping"], pick_source="autonomous"
        )
    )
    dmn._skill_selector = selector

    asyncio.run(dmn._apply_monologue_skills("dmn_3", {}, drive=0.9))
    assert "systems-feedback-mapping" in dmn._monologue_cell.skills
    assert any(n == "dmn_skill_pick" for n, _ in logged)


def test_apply_monologue_skills_resets_to_baseline_when_low_drive(monkeypatch):
    monkeypatch.setattr(D, "get_idle_seconds", lambda: 999.0)
    dmn = _make_dmn()
    dmn._monologue_baseline_skills = ["logic-check", "emotional"]
    dmn._skill_selector = MagicMock()
    dmn._skill_selector.select_autonomous = AsyncMock()
    asyncio.run(dmn._apply_monologue_skills("dmn_3", {}, drive=0.0))
    assert dmn._monologue_cell.skills == ["logic-check", "emotional"]
    dmn._skill_selector.select_autonomous.assert_not_called()
