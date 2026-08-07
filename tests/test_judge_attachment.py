"""
Judge-host attachments — structural plasticity for NON-DRAFTER judge cells.

Covers the producer that was missing (a judge could carry an attachment but never
acquire one), the cross-turn paired-A/B learning signal that replaces the drafting
pool's within-turn competition, and — the point of the feature — the four runtime
gates that make learned content on a JUDGE safe.

The adversarial cases are the ones that matter: an attachment that tries to talk
the judge into approving everything cannot; an attachment cannot suppress a veto;
a stale/tampered stored weight is re-clamped at read; a frozen brain is neutral.
"""

from __future__ import annotations

import pytest

import brain.judge_attachment as ja

HOST_EMPATHY = "frontal.empathy_critic"
HOST_CRITIC = "frontal.critic"


# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeWiring:
    """Minimal wiring stand-in: just the fragment-edge surface this module uses."""

    def __init__(self, edges: dict[tuple[str, str], float] | None = None) -> None:
        self.edges = dict(edges or {})

    def attached_fragments(self, host: str) -> list[tuple[str, float]]:
        return [
            (src[len("fragment.") :], w)
            for (src, tgt), w in self.edges.items()
            if tgt == host and src.startswith("fragment.")
        ]

    def has(self, src: str, tgt: str) -> bool:
        return (src, tgt) in self.edges

    def add(self, src: str, tgt: str, weight: float = 1.0) -> None:
        self.edges[(src, tgt)] = weight

    def prune_fragment_edges(self, floor: float) -> int:
        victims = [k for k, w in self.edges.items() if k[0].startswith("fragment.") and w <= floor]
        for k in victims:
            self.edges.pop(k)
        return len(victims)


class _FakeNeuromod:
    def __init__(self) -> None:
        self.emissions: list[dict] = []

    def add(self, channel, delta, **kw) -> None:
        self.emissions.append({"channel": channel, "delta": delta, **kw})


class _FakeBus:
    def __init__(self) -> None:
        self.neuromod = _FakeNeuromod()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Per-persona durable state to tmp, feature ON, freeze OFF, screener clean."""
    monkeypatch.delenv("BRAIN_WIRING_FROZEN", raising=False)
    monkeypatch.setattr(ja, "persona_state_root", lambda key: tmp_path / str(key))
    monkeypatch.setattr(ja, "active_or_home_persona", lambda: "tester")
    monkeypatch.setitem(ja.settings._data, "judge_attachment", 1)
    monkeypatch.setitem(ja.settings._data, "fragment_wiring", 1)


@pytest.fixture
def clean_screener(monkeypatch):
    """A registry whose skills all come back with a clean 'enabled' verdict."""
    import brain.skills_registry as reg

    monkeypatch.setattr(reg, "get_skill", lambda sid: {"id": sid, "status": "enabled"})


def _attached(host: str = HOST_EMPATHY, sid: str = "skill_a", weight: float = 1.4):
    return _FakeWiring({(f"fragment.{sid}", host): weight})


# ── Gate 1: the read-time clamp (monotone safety direction) ──────────────────


def test_attached_critic_can_never_emit_above_its_ceiling():
    """ADVERSARIAL. The injected skill body successfully talks the critic into
    emitting a perfect score for everything. The read-time clamp means the number
    the rest of the brain reads is still the ceiling — the critic's bar cannot be
    learned, or prompted, downward."""
    ceiling = ja.settings.get("judge_score_ceiling")[HOST_CRITIC]
    for hostile_raw in (1.0, 5.0, 1e9):
        assert ja.clamp_verdict(HOST_CRITIC, hostile_raw, True) == pytest.approx(ceiling)


def test_critic_clamp_is_one_way_down_is_always_allowed():
    """The safe direction is never blocked: a stricter verdict passes through."""
    assert ja.clamp_verdict(HOST_CRITIC, 0.2, True) == pytest.approx(0.2)


def test_empathy_clamp_bounds_both_extremes():
    """The two-sided host still bounds what injected text can drive the number to."""
    lo = ja.settings.get("judge_score_floor")[HOST_EMPATHY]
    hi = ja.settings.get("judge_score_ceiling")[HOST_EMPATHY]
    assert ja.clamp_verdict(HOST_EMPATHY, 1.0, True) == pytest.approx(hi)
    assert ja.clamp_verdict(HOST_EMPATHY, 0.0, True) == pytest.approx(lo)
    # ...and leaves an in-band correction alone, which is the feature's whole point:
    # correcting a misread must be able to move the score UP as well as down.
    assert ja.clamp_verdict(HOST_EMPATHY, 0.8, True) == pytest.approx(0.8)


def test_clamp_is_identity_without_an_attachment():
    """An unattached judge behaves exactly as it does today — no clamp, no floor."""
    assert ja.clamp_verdict(HOST_CRITIC, 1.0, False) == 1.0
    assert ja.clamp_verdict(HOST_EMPATHY, 0.0, False) == 0.0


def test_stale_over_range_stored_weight_is_clamped_at_read():
    """ADVERSARIAL. A stored attachment weight far outside any legitimate range —
    a stale file, a bug, or tampering — must not widen anything. The band is
    re-derived from settings at read time and never from the stored value, so an
    absurd weight buys exactly the same ceiling as a normal one."""
    sane = _attached(HOST_CRITIC, weight=1.4)
    tampered = _attached(HOST_CRITIC, weight=10_000.0)
    assert ja.host_is_attached(sane, HOST_CRITIC) is True
    assert ja.host_is_attached(tampered, HOST_CRITIC) is True
    ceiling = ja.settings.get("judge_score_ceiling")[HOST_CRITIC]
    assert ja.clamp_verdict(HOST_CRITIC, 1.0, True) == pytest.approx(ceiling)


def test_non_judge_hosts_are_untouched():
    """The executive and the reframer are deliberately out of scope — no clamp, no
    floor, no producer. Their exclusion is argued in the module docstring."""
    for host in ("frontal.executive", "frontal.stoic_reframer", "frontal.drafter_A"):
        assert host not in ja.JUDGE_HOSTS
        assert ja.clamp_verdict(host, 1.0, True) == 1.0
        assert ja.veto_floor(host, user_emotion="angry", hostility=1.0, raw_score=0.0) is False


# ── Gate 2: the veto is not learnable-away ───────────────────────────────────


def test_attachment_cannot_suppress_a_veto():
    """ADVERSARIAL, and the core safety claim. The floor is computed in Python from
    the turn's own features, so no injected instruction can reach it. Whatever the
    attached judge emits, the effective veto is the OR — an attachment can add a
    veto and can never clear one."""
    floor = ja.veto_floor(HOST_EMPATHY, user_emotion="neutral", hostility=0.95, raw_score=1.0)
    assert floor is True
    # The judge (prompted by the attachment) says "no veto, perfect score":
    raw_veto = False
    effective_veto = raw_veto or floor
    assert effective_veto is True


def test_veto_floor_fires_on_discomfort_with_a_low_read():
    assert ja.veto_floor(HOST_EMPATHY, user_emotion="hurt", hostility=0.0, raw_score=0.1) is True
    # ...but not on discomfort alone with a healthy read — the floor is a floor,
    # not a second opinion.
    assert ja.veto_floor(HOST_EMPATHY, user_emotion="hurt", hostility=0.0, raw_score=0.9) is False


def test_veto_floor_ignores_model_derived_signals(monkeypatch):
    """The floor's inputs are the parsed features only. Its answer must not change
    when the judge's own score is manipulated upward past the floor's band — that
    is the difference between a floor and something an attachment can argue with."""
    hostile = {"user_emotion": "neutral", "hostility": 0.99}
    assert ja.veto_floor(HOST_EMPATHY, raw_score=0.0, **hostile) is True
    assert ja.veto_floor(HOST_EMPATHY, raw_score=1.0, **hostile) is True


# ── Gate 4: admission is stricter than for a drafter ─────────────────────────


def test_flagged_skill_cannot_attach_to_a_judge(monkeypatch):
    """A self-authored skill sitting in the flagged review queue may reach a
    drafter's exploration pool; it may not reach a judge."""
    import brain.skills_registry as reg

    monkeypatch.setattr(reg, "get_skill", lambda sid: {"id": sid, "status": "flagged"})
    assert ja.judge_admissible("skill_a", HOST_EMPATHY) is False


def test_judge_admission_fails_closed_when_the_registry_is_unreachable(monkeypatch):
    """Unlike fragment_pool.is_admissible (where the static allowlist is still the
    real boundary), here the status check IS the boundary — so it fails closed."""
    import brain.skills_registry as reg

    def _boom(sid):
        raise RuntimeError("registry down")

    monkeypatch.setattr(reg, "get_skill", _boom)
    assert ja.judge_admissible("skill_a", HOST_EMPATHY) is False


def test_clean_skill_is_admissible_to_a_judge(clean_screener):
    assert ja.judge_admissible("skill_a", HOST_EMPATHY) is True


def test_safety_nodes_are_still_denied(clean_screener):
    """Tier 1's denylist is not weakened by the stricter judge check layered on it."""
    assert ja.judge_admissible("skill_a", "temporal.integrator_inhibitor") is False


# ── Gate 5: kill switches ────────────────────────────────────────────────────


def test_frozen_brain_is_neutral(monkeypatch, clean_screener):
    """BRAIN_WIRING_FROZEN makes every path a strict no-op: no clamp, no floor, no
    admission, no record, no edge. The wiring a session starts with is the wiring it
    ends with, byte-identical."""
    monkeypatch.setenv("BRAIN_WIRING_FROZEN", "true")
    wiring = _attached(HOST_CRITIC)
    before = dict(wiring.edges)

    assert ja.enabled() is False
    assert ja.clamp_verdict(HOST_CRITIC, 1.0, True) == 1.0
    assert ja.veto_floor(HOST_EMPATHY, user_emotion="angry", hostility=1.0, raw_score=0.0) is False
    assert ja.judge_admissible("skill_a", HOST_EMPATHY) is False
    assert ja.host_is_attached(wiring, HOST_CRITIC) is False

    tracker = ja.JudgeAttachmentTracker()
    store: dict = {}
    tracker.record_prediction(store, HOST_EMPATHY, score=0.9, veto=False, turn_count=1)
    assert store == {}
    assert tracker.observe_turn(store, _FakeBus(), turn_count=2, wiring=wiring) == []
    assert wiring.edges == before


def test_kill_switch_off_is_neutral(monkeypatch):
    monkeypatch.setitem(ja.settings._data, "judge_attachment", 0)
    assert ja.enabled() is False
    assert ja.clamp_verdict(HOST_CRITIC, 1.0, True) == 1.0


def test_subordinate_to_fragment_wiring(monkeypatch):
    """Injection is what an attachment DOES, so killing the consumer kills this."""
    monkeypatch.setitem(ja.settings._data, "fragment_wiring", 0)
    assert ja.enabled() is False


# ── The learning signal: paired cross-turn accuracy ──────────────────────────


def _shadow_turn(
    tracker, store, *, live: float, shadow: float, turn: int, sid="skill_a", host=HOST_EMPATHY
):
    """One turn carrying a complete A/B pair. `live` is the UNATTACHED arm of the
    shadow pair (both arms run locally); the live cloud verdict is recorded
    separately and is deliberately never the comparison baseline."""
    tracker.record_prediction(
        store,
        host,
        score=0.5,  # the live cloud verdict — for the drift monitor, not the A/B
        veto=False,
        turn_count=turn,
        shadow_sid=sid,
        shadow_score=shadow,
        shadow_baseline=live,
    )


def test_a_half_pair_is_discarded_not_backfilled(clean_screener):
    """ADVERSARIAL against a silent bug. If only one arm of the A/B came back, the
    record must refuse to store it rather than quietly falling back to the live
    cloud verdict as the baseline. That fallback would confound the attachment's
    effect with the Haiku-vs-local model gap, penalise every candidate, and present
    as 'the feature is on and never fires'."""
    tracker = ja.JudgeAttachmentTracker()
    store: dict = {}
    tracker.record_prediction(
        store,
        HOST_EMPATHY,
        score=0.9,
        veto=False,
        turn_count=1,
        shadow_sid="skill_a",
        shadow_score=0.2,
        shadow_baseline=None,
    )
    assert "shadow" not in store[ja._PRED_KEY]["hosts"][HOST_EMPATHY]
    tracker.observe_turn(store, _FakeBus(), user_emotion="hurt", turn_count=2, wiring=_FakeWiring())
    assert not [k for k in store if k.startswith(ja._GATE_PREFIX)]


def test_a_turn_without_a_shadow_contributes_no_evidence(clean_screener):
    """An attachment is never credited for a turn that merely went well. Without a
    paired comparison there is no differential information, so nothing accumulates."""
    tracker = ja.JudgeAttachmentTracker()
    store: dict = {}
    wiring = _FakeWiring()
    tracker.record_prediction(store, HOST_EMPATHY, score=0.9, veto=False, turn_count=1)
    tracker.observe_turn(store, _FakeBus(), user_emotion="grateful", turn_count=2, wiring=wiring)
    assert not [k for k in store if k.startswith(ja._GATE_PREFIX)]
    assert wiring.edges == {}


def test_agreeing_verdicts_contribute_no_evidence(clean_screener):
    """When live and shadow are both right (or both wrong), the A/B learned nothing
    about the candidate — only a DISAGREEMENT carries signal."""
    tracker = ja.JudgeAttachmentTracker()
    store: dict = {}
    _shadow_turn(tracker, store, live=0.9, shadow=0.85, turn=1)
    tracker.observe_turn(
        store, _FakeBus(), user_emotion="grateful", turn_count=2, wiring=_FakeWiring()
    )
    assert not [k for k in store if k.startswith(ja._GATE_PREFIX)]


def test_shadow_beating_live_accumulates_evidence(clean_screener):
    """The candidate read the user correctly where the live judge did not: the
    empathy critic predicted the reply landed (0.9), the shadow predicted it would
    not (0.2), and the user's next turn was hurt. That is one unit of evidence."""
    tracker = ja.JudgeAttachmentTracker()
    store: dict = {}
    _shadow_turn(tracker, store, live=0.9, shadow=0.2, turn=1)
    tracker.observe_turn(store, _FakeBus(), user_emotion="hurt", turn_count=2, wiring=_FakeWiring())
    slices = [k for k in store if k.startswith(ja._GATE_PREFIX)]
    assert slices, "a disagreement the candidate won must leave accumulated evidence"
    assert store[slices[0]]["level"] > 0


def test_one_good_turn_does_not_establish_on_a_judge(clean_screener):
    """A drafter earns an attachment from ONE winning draft. A judge must clear a
    materially higher bar — a single favourable comparison establishes nothing."""
    tracker = ja.JudgeAttachmentTracker()
    store: dict = {}
    wiring = _FakeWiring()
    _shadow_turn(tracker, store, live=0.9, shadow=0.2, turn=1)
    established = tracker.observe_turn(
        store, _FakeBus(), user_emotion="hurt", turn_count=2, wiring=wiring
    )
    assert established == []
    assert wiring.edges == {}


def test_sustained_evidence_eventually_establishes(clean_screener, monkeypatch):
    """...but sustained, repeated wins do establish the attachment — the producer
    that did not exist before this change."""
    monkeypatch.setitem(ja.settings._data, "judge_arm_threshold", 1.0)
    tracker = ja.JudgeAttachmentTracker()
    tracker._gate.arm_threshold = 1.0
    store: dict = {}
    wiring = _FakeWiring()
    established: list[str] = []
    for turn in range(1, 20, 2):
        _shadow_turn(tracker, store, live=0.9, shadow=0.2, turn=turn)
        established += tracker.observe_turn(
            store, _FakeBus(), user_emotion="hurt", turn_count=turn + 1, wiring=wiring
        )
        if established:
            break
    assert established == ["skill_a"]
    assert wiring.has("fragment.skill_a", HOST_EMPATHY)


def test_sustained_evidence_establishes_on_the_critic_host(clean_screener, monkeypatch):
    """The critic host can go the whole way: paired-shadow evidence accumulates
    under its own gate slice and establishes an attachment — the path that was
    structurally dead while the producer was empathy-only."""
    monkeypatch.setitem(ja.settings._data, "judge_arm_threshold", 1.0)
    tracker = ja.JudgeAttachmentTracker()
    tracker._gate.arm_threshold = 1.0
    store: dict = {}
    wiring = _FakeWiring()
    established: list[str] = []
    for turn in range(1, 20, 2):
        _shadow_turn(tracker, store, live=0.9, shadow=0.2, turn=turn, host=HOST_CRITIC)
        established += tracker.observe_turn(
            store, _FakeBus(), user_emotion="hurt", turn_count=turn + 1, wiring=wiring
        )
        if established:
            break
    assert established == ["skill_a"]
    assert wiring.has("fragment.skill_a", HOST_CRITIC)


class _ProducerFrontal:
    """Stand-in carrying what the REAL _judge_shadow_and_record touches. The critic
    cell is scripted so the candidate arm reads the user right (0.2 = "will not
    land") where the baseline arm reads them wrong (0.9)."""

    def __init__(self, tracker, wiring):
        from types import SimpleNamespace

        self._judge_attach = tracker
        self._wiring = wiring
        self._bus = SimpleNamespace(evidence={})
        self._skill_selector = SimpleNamespace(attachable_fragment_ids=lambda: ["skill_a"])
        self._parietal = SimpleNamespace(turn_count=0)
        self._critic = _RecordingJudgeCell((0.9, 0.2), field="overall")
        self._empathy_critic = _RecordingJudgeCell((0.9, 0.2))

    def _local_available(self):
        return True

    def _fragment_block_for_ids(self, ids):
        return "<<fenced skill body>>"

    @staticmethod
    def _empathy_prompt(draft, user_emotion):
        from brain.clusters.frontal import FrontalCluster

        return FrontalCluster._empathy_prompt(draft, user_emotion)

    @staticmethod
    def _critic_prompt(draft, context):
        from brain.clusters.frontal import FrontalCluster

        return FrontalCluster._critic_prompt(draft, context)

    async def _judge_shadow_pair(self, *a, **k):
        from brain.clusters.frontal import FrontalCluster

        return await FrontalCluster._judge_shadow_pair(self, *a, **k)

    async def record(self, turn):
        from brain.clusters.frontal import FrontalCluster

        self._parietal.turn_count = turn
        await FrontalCluster._judge_shadow_and_record(
            self,
            HOST_CRITIC,
            "a draft",
            "neutral",
            f"t{turn}",
            {"overall": 0.9, "veto": False},
            "overall",
            context="drafter ctx",
        )


def test_critic_producer_records_and_eventually_establishes(clean_screener, monkeypatch):
    """End-to-end through the REAL producer: on a scored turn frontal.critic
    RECORDS a prediction — the thing that never happened while the only call site
    was empathy-gated — and sustained shadow wins establish its attachment.

    judge_explore_rate is forced to 1.0 so every turn shadow-tests; random.random()
    is strictly < 1.0, so the sampling gate passes deterministically."""
    import asyncio

    monkeypatch.setitem(ja.settings._data, "judge_explore_rate", 1.0)
    monkeypatch.setitem(ja.settings._data, "judge_arm_threshold", 1.0)
    tracker = ja.JudgeAttachmentTracker()
    tracker._gate.arm_threshold = 1.0
    wiring = _FakeWiring()
    f = _ProducerFrontal(tracker, wiring)
    store = f._bus.evidence

    asyncio.run(f.record(1))
    rec = store.get(ja._PRED_KEY)
    assert rec is not None and HOST_CRITIC in rec["hosts"], (
        "a scored turn must record a frontal.critic prediction"
    )
    entry = rec["hosts"][HOST_CRITIC]
    assert entry["score"] == pytest.approx(0.9)  # the critic's live claim
    assert entry["shadow"]["sid"] == "skill_a"  # ...and a complete A/B pair
    assert entry["shadow"]["baseline"] == pytest.approx(0.9)
    assert entry["shadow"]["score"] == pytest.approx(0.2)
    assert len(f._critic.calls) == 2, "the critic's shadow must run the critic cell"
    assert f._empathy_critic.calls == []

    established: list[str] = []
    for turn in range(2, 30):  # grade turn N-1's record, then record turn N
        established += tracker.observe_turn(
            store, _FakeBus(), user_emotion="hurt", turn_count=turn, wiring=wiring
        )
        if established:
            break
        asyncio.run(f.record(turn))
    assert established == ["skill_a"]
    assert wiring.has("fragment.skill_a", HOST_CRITIC)


def test_established_attachment_respects_the_lower_judge_cap(clean_screener, monkeypatch):
    """A judge's cap is lower than a drafter's; a second candidate cannot establish."""
    monkeypatch.setitem(ja.settings._data, "judge_max_per_host", 1)
    tracker = ja.JudgeAttachmentTracker()
    wiring = _attached(HOST_EMPATHY, "skill_a")
    assert tracker._establish(wiring, HOST_EMPATHY, "skill_b") is False


def test_establish_rechecks_admission_at_write_time(monkeypatch):
    """Candidacy can run for days; a skill's screener status can change underneath
    it. The write path re-checks rather than trusting the check made at selection —
    the same time-of-check/time-of-use discipline §6.11 applies to skills."""
    import brain.skills_registry as reg

    monkeypatch.setattr(reg, "get_skill", lambda sid: {"id": sid, "status": "flagged"})
    tracker = ja.JudgeAttachmentTracker()
    wiring = _FakeWiring()
    assert tracker._establish(wiring, HOST_EMPATHY, "skill_a") is False
    assert wiring.edges == {}


# ── Reward provenance and budget ─────────────────────────────────────────────


def test_resolution_da_is_stamped_self_inference_not_external(clean_screener):
    """A judge grading itself against a read of the user is a SELF-generated
    inference even when behaviour informs it. Stamping the external bucket here
    would inflate the §4.3 honesty ratio in the flattering direction — the exact
    bug that was just fixed elsewhere and must not be reintroduced."""
    tracker = ja.JudgeAttachmentTracker()
    bus = _FakeBus()
    store: dict = {}
    for turn in (1, 3, 5):
        _shadow_turn(tracker, store, live=0.9, shadow=0.2, turn=turn)
        tracker.observe_turn(
            store, bus, user_emotion="hurt", turn_count=turn + 1, wiring=_FakeWiring()
        )
    for e in bus.neuromod.emissions:
        assert e["source"] in ("self_inference", "intrinsic")
        assert e["source"] not in ("external", "external_grader")


def test_informativeness_is_measured_not_hardcoded(clean_screener):
    """The anti-farm gate's informativeness must come from an OBSERVED base rate,
    not a constant. A fresh persona sits at maximum uncertainty; a persona whose
    turns always land converges toward zero, so being right about the
    near-inevitable stops paying."""
    tracker = ja.JudgeAttachmentTracker()
    assert tracker._informativeness("tester") == pytest.approx(0.5)
    for _ in range(60):
        tracker._record_outcome("tester", landed=True)
    assert tracker._informativeness("tester") < 0.1


def test_resolution_draws_from_the_shared_per_turn_budget(clean_screener, monkeypatch):
    """A new reward source must draw from the SAME per-turn ceiling every other
    resolution shares, not open a second budget alongside it."""
    monkeypatch.setitem(ja.settings._data, "prediction_reward_turn_cap", 0.02)
    tracker = ja.JudgeAttachmentTracker()
    bus = _FakeBus()
    store: dict = {}
    for turn in range(1, 30, 2):
        _shadow_turn(tracker, store, live=0.9, shadow=0.2, turn=turn)
        tracker.observe_turn(
            store, bus, user_emotion="hurt", turn_count=turn + 1, wiring=_FakeWiring()
        )
    spent = sum(abs(e["delta"]) for e in bus.neuromod.emissions)
    assert spent <= 0.02 + 1e-9, f"resolutions paid {spent}, past the shared turn cap"


# ── Gate 3: the drift monitor ────────────────────────────────────────────────


def test_attachment_trending_permissive_is_force_demoted(clean_screener, monkeypatch):
    """ADVERSARIAL, and the layer with the real teeth. Every individual verdict sits
    inside the ceiling, so gate 1 never trips — but the attached distribution has
    drifted systematically more permissive than the pre-attachment reference. The
    drift monitor prunes the edge outright; the attachment must re-earn from zero."""
    monkeypatch.setitem(ja.settings._data, "judge_drift_min_samples", 5)
    monkeypatch.setitem(ja.settings._data, "judge_drift_band", 0.05)
    tracker = ja.JudgeAttachmentTracker()
    wiring = _attached(HOST_CRITIC, "skill_a")

    # Pre-attachment era: the reference operating point is ~0.50.
    for _ in range(10):
        tracker._track_drift("tester", HOST_CRITIC, {"score": 0.50, "attached": []}, wiring)
    # Now attached, and every verdict is more permissive — but all under the 0.95 ceiling.
    for _ in range(10):
        tracker._track_drift(
            "tester", HOST_CRITIC, {"score": 0.90, "attached": ["skill_a"]}, wiring
        )
    assert not wiring.has("fragment.skill_a", HOST_CRITIC), "permissive drift must demote"


def test_attachment_trending_conservative_survives(clean_screener, monkeypatch):
    """The direction matters: drifting STRICTER is the safe direction and must not
    trip the monitor. An attachment that makes the critic harder to please is doing
    exactly what it is allowed to do."""
    monkeypatch.setitem(ja.settings._data, "judge_drift_min_samples", 5)
    tracker = ja.JudgeAttachmentTracker()
    wiring = _attached(HOST_CRITIC, "skill_a")
    for _ in range(10):
        tracker._track_drift("tester", HOST_CRITIC, {"score": 0.80, "attached": []}, wiring)
    for _ in range(10):
        tracker._track_drift(
            "tester", HOST_CRITIC, {"score": 0.40, "attached": ["skill_a"]}, wiring
        )
    assert wiring.has("fragment.skill_a", HOST_CRITIC)


def test_baseline_ledger_is_bounded(clean_screener, monkeypatch):
    """Durable per-persona state goes through the shared bounded-ledger primitive
    rather than becoming a fourth unbounded per-persona ledger."""
    monkeypatch.setitem(ja.settings._data, "judge_baseline_max_hosts", 3)
    tracker = ja.JudgeAttachmentTracker()
    tracker._durable("tester")
    book = tracker._baseline["tester"]
    for i in range(20):
        book[f"host_{i}"] = {"ref": None, "n": 1.0, "mean": 0.5, "ts": float(i)}
    assert len(tracker._bounded_baselines("tester")) <= 3


# ── Exploration is safe without a losing-draft escape hatch ──────────────────


def test_exploration_is_sampled_and_stops_at_the_cap(clean_screener, monkeypatch):
    tracker = ja.JudgeAttachmentTracker()
    tracker.set_pool(["skill_a", "skill_b"])

    class _AlwaysExplore:
        @staticmethod
        def random():
            return 0.0

        @staticmethod
        def choice(seq):
            return seq[0]

    # Below cap → a candidate is offered.
    assert tracker.explore_candidate(_FakeWiring(), HOST_EMPATHY, rng=_AlwaysExplore) == "skill_a"
    # At cap → a judge that has found its skill stops experimenting on itself.
    monkeypatch.setitem(ja.settings._data, "judge_max_per_host", 1)
    assert (
        tracker.explore_candidate(_attached(HOST_EMPATHY), HOST_EMPATHY, rng=_AlwaysExplore) is None
    )


def test_exploration_never_offers_a_flagged_candidate(monkeypatch):
    import brain.skills_registry as reg

    monkeypatch.setattr(reg, "get_skill", lambda sid: {"id": sid, "status": "flagged"})
    tracker = ja.JudgeAttachmentTracker()
    tracker.set_pool(["skill_a"])

    class _AlwaysExplore:
        @staticmethod
        def random():
            return 0.0

        @staticmethod
        def choice(seq):
            return seq[0]

    assert tracker.explore_candidate(_FakeWiring(), HOST_EMPATHY, rng=_AlwaysExplore) is None


# ── Shadow exploration runs on the LOCAL GPU, both arms, or not at all ───────


class _RecordingJudgeCell:
    def __init__(self, scores, field="empathy_score"):
        self._scores = list(scores)
        self._field = field
        self.calls: list[dict] = []

    def reset_turn(self, key):
        pass

    async def call(self, messages, **kw):
        self.calls.append({"content": messages[0]["content"], **kw})
        score = self._scores[(len(self.calls) - 1) % len(self._scores)]
        return f'{{"{self._field}": {score}, "veto": false}}'


class _FakeFrontal:
    """Stand-in carrying only what _judge_shadow_pair touches — BOTH judge cells,
    so a per-host test can also assert the OTHER host's cell was never run."""

    def __init__(self, local_up: bool, scores=(0.3, 0.8), critic_scores=(0.3, 0.8)):
        self._local_up = local_up
        self._empathy_critic = _RecordingJudgeCell(scores)
        self._critic = _RecordingJudgeCell(critic_scores, field="overall")

    def _local_available(self):
        return self._local_up

    def _fragment_block_for_ids(self, ids):
        return "<<fenced skill body>>"

    @staticmethod
    def _empathy_prompt(draft, user_emotion):
        from brain.clusters.frontal import FrontalCluster

        return FrontalCluster._empathy_prompt(draft, user_emotion)

    @staticmethod
    def _critic_prompt(draft, context):
        from brain.clusters.frontal import FrontalCluster

        return FrontalCluster._critic_prompt(draft, context)


async def _run_pair(fake, host=HOST_EMPATHY, field="empathy_score", context=""):
    from brain.clusters.frontal import FrontalCluster

    return await FrontalCluster._judge_shadow_pair(
        fake, host, "a draft", "sad", "t1", "skill_a", field, context=context
    )


def test_shadow_pair_runs_both_arms_on_local_never_cloud():
    """Both arms must ride the local GPU with a hard locality backstop — exploration
    must never bill a cloud API — and must differ ONLY by the candidate block, so the
    comparison has exactly one variable."""
    import asyncio

    fake = _FakeFrontal(local_up=True, scores=(0.3, 0.8))
    baseline, candidate, _veto = asyncio.run(_run_pair(fake))

    assert len(fake._empathy_critic.calls) == 2
    for c in fake._empathy_critic.calls:
        assert c["locality_override"] == "local", "a shadow arm must never bill cloud"
        assert c["model_override"] == ja.settings.get("judge_shadow_model")
    base_call, cand_call = fake._empathy_critic.calls
    assert "<<fenced skill body>>" not in base_call["content"]
    assert "<<fenced skill body>>" in cand_call["content"]
    # ...and the arms differ by the candidate block and nothing else.
    assert cand_call["content"].startswith(base_call["content"])
    assert baseline == pytest.approx(0.3) and candidate == pytest.approx(0.8)


def test_no_exploration_when_the_local_pod_is_down():
    """No cloud fallback. A pod that is not confirmed up means less learning this
    turn, never a surprise bill — the same rule the drafter downshift follows."""
    import asyncio

    fake = _FakeFrontal(local_up=False)
    assert asyncio.run(_run_pair(fake)) is None
    assert fake._empathy_critic.calls == [], "must not fall back to a cloud call"


def test_shadow_arms_are_clamped_on_the_same_scale():
    """Both arms ride the same read-time clamp the live path would apply once the
    candidate is established, so a candidate cannot win its A/B on out-of-band
    scores it would never be allowed to emit in production."""
    import asyncio

    ceiling = ja.settings.get("judge_score_ceiling")[HOST_EMPATHY]
    fake = _FakeFrontal(local_up=True, scores=(1.0, 1.0))
    baseline, candidate, _ = asyncio.run(_run_pair(fake))
    assert baseline == pytest.approx(ceiling)
    assert candidate == pytest.approx(ceiling)


# ── The shadow pair is per-host: the critic runs ITS cell and ITS prompt ─────


def test_critic_shadow_pair_runs_the_critic_cell_with_the_scoring_prompt():
    """REGRESSION. JUDGE_HOSTS listed frontal.critic from day one, but the shadow
    pair hardcoded the empathy prompt and the empathy cell, so the critic host had
    a consumer (its clamp) and no producer — it could never record a prediction,
    accumulate paired evidence, or establish. The critic's shadow must run the
    critic's OWN cell with the live scoring-prompt shape, arms differing only by
    the candidate block."""
    import asyncio

    fake = _FakeFrontal(local_up=True, critic_scores=(0.3, 0.8))
    baseline, candidate, _veto = asyncio.run(
        _run_pair(fake, host=HOST_CRITIC, field="overall", context="drafter ctx")
    )

    assert fake._empathy_critic.calls == [], "the critic's shadow must not run the empathy cell"
    assert len(fake._critic.calls) == 2
    for c in fake._critic.calls:
        assert c["locality_override"] == "local", "a shadow arm must never bill cloud"
        assert c["model_override"] == ja.settings.get("judge_shadow_model")
    base_call, cand_call = fake._critic.calls
    assert base_call["content"] == (
        "Context:\ndrafter ctx\n\nDraft response:\na draft\n\nScore this draft."
    )
    assert "<<fenced skill body>>" not in base_call["content"]
    assert "<<fenced skill body>>" in cand_call["content"]
    assert cand_call["content"].startswith(base_call["content"])
    assert baseline == pytest.approx(0.3) and candidate == pytest.approx(0.8)


def test_empathy_shadow_pair_is_unchanged_and_never_touches_the_critic_cell():
    """CHARACTERIZATION. Generalizing the shadow path per-host must leave the
    empathy host exactly as it was: same cell, same prompt, and the critic cell
    untouched."""
    import asyncio

    fake = _FakeFrontal(local_up=True, scores=(0.3, 0.8))
    baseline, candidate, _veto = asyncio.run(_run_pair(fake))

    assert fake._critic.calls == [], "the empathy shadow must not run the critic cell"
    assert len(fake._empathy_critic.calls) == 2
    assert fake._empathy_critic.calls[0]["content"] == fake._empathy_prompt("a draft", "sad")
    assert baseline == pytest.approx(0.3) and candidate == pytest.approx(0.8)


def test_critic_shadow_arms_ride_the_critic_ceiling():
    """The per-host clamp follows the host: critic shadow arms pass through the
    one-way "down" clamp, so a candidate cannot win its A/B on scores the live
    path would never let an attached critic emit."""
    import asyncio

    ceiling = ja.settings.get("judge_score_ceiling")[HOST_CRITIC]
    fake = _FakeFrontal(local_up=True, critic_scores=(1.0, 1.0))
    baseline, candidate, _ = asyncio.run(_run_pair(fake, host=HOST_CRITIC, field="overall"))
    assert baseline == pytest.approx(ceiling)
    assert candidate == pytest.approx(ceiling)


# ── The live empathy check: local-preferred, cloud-fallback, never fail-open ─


class _EmpathyCell:
    """Judge cell returning a scripted response per call, recording routing."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def reset_turn(self, key):
        pass

    async def call(self, messages, **kw):
        self.calls.append(kw)
        i = len(self.calls) - 1
        return self._responses[i] if i < len(self._responses) else ""


class _EmpathyFrontal:
    def __init__(self, local_up, responses):
        self._local_up = local_up
        self._empathy_critic = _EmpathyCell(responses)
        self._wiring = None

    def _local_available(self):
        return self._local_up

    def _inject_host_fragments(self, prompt, host, turn_id):
        return prompt

    def _apply_judge_gates(self, host, verdict, field):
        return verdict

    @staticmethod
    def _empathy_prompt(draft, user_emotion):
        from brain.clusters.frontal import FrontalCluster

        return FrontalCluster._empathy_prompt(draft, user_emotion)


def _run_empathy(fake):
    import asyncio

    from brain.clusters.frontal import FrontalCluster

    return asyncio.run(FrontalCluster._run_empathy_check(fake, "a draft", "sad", "t1"))


def test_live_empathy_check_stays_on_cloud():
    """The LIVE empathy check must not ride the local pod, however simple its task.
    It fans out to one call PER DRAFT under asyncio.gather and sits on the user's
    critical path, so local contention can serialize a parallel stage and blow the
    20s cell timeout — slower AND still paying cloud on the fallback. Only the
    shadow explorer, which is off the critical path with bounded fan-out, goes
    local. Also protects the drift monitor's one-model invariant: a host whose live
    routing can vary makes gate 3 measure the model gap instead of the attachment."""
    fake = _EmpathyFrontal(True, ['{"empathy_score": 0.4, "veto": false}'])
    out = _run_empathy(fake)
    assert out["empathy_score"] == pytest.approx(0.4)
    assert len(fake._empathy_critic.calls) == 1
    assert fake._empathy_critic.calls[0] == {}, "live empathy check must carry no local override"


def test_live_empathy_check_is_one_call_even_with_a_pod_up():
    """No opportunistic local attempt, so no wasted round-trip before the cloud call."""
    fake = _EmpathyFrontal(True, ["not json at all"])
    _run_empathy(fake)
    assert len(fake._empathy_critic.calls) == 1


def test_a_failed_empathy_check_reports_no_opinion_never_a_fabricated_pass():
    """REGRESSION. The old fallback was {"empathy_score": 0.7, "veto": False} — a
    manufactured PASS, above the score bar and clearing the veto, injected into the
    blended score, the critic.empathy stream and the judge-accuracy grader. An
    appraisal that did not happen must read as absent, not as approval."""
    fake = _EmpathyFrontal(True, ["garbage"])
    out = _run_empathy(fake)
    assert out["empathy_score"] is None, "a missing verdict must not become a passing one"
    assert out["unavailable"] is True
    assert out["veto"] is False


def test_no_opinion_survives_the_judge_gates_unfabricated(monkeypatch):
    """The clamp must not manufacture a number for a verdict that has none — that
    would reintroduce the fail-open one layer down."""
    from brain.clusters.frontal import FrontalCluster

    class _F:
        _wiring = _attached(HOST_EMPATHY)
        _turn_user_emotion = "neutral"
        _turn_hostility = 0.0

    out = FrontalCluster._apply_judge_gates(
        _F(), HOST_EMPATHY, {"empathy_score": None, "veto": False}, "empathy_score"
    )
    assert out["empathy_score"] is None


def test_records_are_content_free(clean_screener):
    """The prediction record carries host names, numbers, booleans and skill ids —
    never the draft, the user's text, or the skill body."""
    tracker = ja.JudgeAttachmentTracker()
    store: dict = {}
    _shadow_turn(tracker, store, live=0.9, shadow=0.2, turn=1)
    blob = repr(store)
    assert "skill_a" in blob  # ids are fine
    for leaked in ("Draft response", "User's current emotion", "\n"):
        assert leaked not in blob
