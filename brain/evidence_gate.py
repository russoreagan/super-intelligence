"""
EvidenceGate — a bounded evidence accumulator with hysteresis, for decisions no
single turn can make.

WHAT THIS IS (claim the verdict, not the name). This is the **bounded-evidence-
accumulation** model of decision-making — the drift-diffusion / sequential-sampling
family (Ratcliff; Gold & Shadlen). Evidence for a proposition accumulates as noisy
input arrives across turns, leaks when unfed, and the gate *commits* when the
accumulated level crosses a bound. A hysteresis band (arm high, release low) makes
the commitment resist chattering on a noisy boundary.

WHAT THIS IS NOT. It is **not** an integrate-and-fire / spiking neuron. There is no
membrane potential in the biophysical sense, no spike train, and no spike-timing
plasticity (STDP). The switch layer stays stateless comparators + neuromodulatory
gain control (see docs/SYSTEMS.md §2.1, Part II "Integrate-and-fire → Not
implemented"). This is a *different, narrower* claim that earns its own verdict:
sequential-sampling decision-making for a handful of named propositions.

RELATION TO THE CONCENTRATION LAYER. This is deliberately the SAME shape as the bus
concentration/quorum machine (bus.py: track_concentration/_accumulate/_decay_to/
UNARMED→ARMED→QUIET/quorum), which is already load-bearing as the thalamus GWT
spotlight. That one accumulates per *topic* (a published salient signal). This one
accumulates per *decision gate* (a private inference no cluster publishes as a
topic — "the user is avoiding X"). Use topic concentration for salient published
topics; use an EvidenceGate for private per-decision inferences. Do not point both
at the same signal — that would integrate it twice.

LEARNING. An armed gate is a *checkable prediction*. When the world later confirms
or refutes it, `resolve()` routes the outcome through neuron.prediction_reward (the
same anti-farming path the shadow-prediction reward uses: confidence floor +
informativeness gate + per-turn cap) and nudges the per-cue drift weights toward
whichever cues predicted a *confirmed* inference. Plasticity is weighted toward
EXTERNAL / independent confirmation (the external_grader channel) so the gate cannot
learn to fire on cues its own appraiser happens to like — that self-grading loop is
the exact premise-audit risk we must not deepen. Every reward emission flows through
bus.neuromod.add so it is audited in the intrinsic/external DA tally.

STATE HAS TWO LIFECYCLES.
  • The accumulated `level`/`armed` state is TRANSIENT and per-(persona, end_user):
    it rides the ChemPair (see bus.ChemPair.snapshot/restore) exactly like mood, so
    binding, isolation, and the one-way valve are inherited. In-memory, relaxes at
    rest — a fact about the live relationship, not a durable record.
  • The learned `cue_weights` are DURABLE and per-PERSONA — they belong with the
    wiring/efficacy learning that persists per personality, and must go through the
    valve the right way (never contaminated by one client's transient session).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from brain.neuron import SwitchNeuron

logger = logging.getLogger(__name__)

# The two commit modes a gate can declare.
MODE_LATCH = "latch"  # a held belief: arm high, hold until it decays below release
MODE_FIRE_RESET = "fire_reset"  # a one-shot event: emit on arm, then reset to zero

# Log-once flag for non-finite evidence input. A NaN delta would propagate through
# max(0, min(cap, level+nan)) → NaN, get persisted in the transient snapshot, and
# poison the cue-weight learning — so non-finite input is dropped at the entry.
_nonfinite_warned = False


def _drop_nonfinite(gate: str) -> float:
    global _nonfinite_warned
    if not _nonfinite_warned:
        _nonfinite_warned = True
        logger.warning(
            "[EvidenceGate] non-finite evidence ignored (gate=%s); treating as 0.0", gate
        )
    return 0.0


@dataclass
class EvidenceGate:
    name: str
    cluster: str
    # Base commit bound (chemistry shifts it via the internal switch's gain control,
    # and a learned efficacy can lower it — both inherited from SwitchNeuron).
    arm_threshold: float = 1.0
    # Hysteresis: release when level falls to arm_threshold * release_ratio. <1.0
    # gives a genuine band (no chatter); 1.0 collapses to a single threshold.
    release_ratio: float = 0.5
    # Leak: evidence half-life in wall-clock seconds. A suspicion you stop seeing
    # fades. Mirrors the concentration layer's colony_conc_half_life_s.
    half_life_s: float = 90.0
    # After an arm edge, block re-arming for this long (0 = none; the hysteresis
    # band already prevents chatter, so refractory is only for fire_reset gates
    # that would otherwise re-emit immediately).
    refractory_s: float = 0.0
    cap: float = 10.0  # upper bound on accumulated evidence
    mode: str = MODE_LATCH
    # Chemistry channels that modulate the commit bound, same contract as a switch
    # ({channel: signed coeff}); empty = bound is chemistry-independent.
    modulators: dict[str, float] = field(default_factory=dict)
    # Named evidence cues whose weighted sum is the per-observation drift. Empty =
    # scalar mode (observe() takes a bare delta; no cue learning — e.g. satiation).
    cue_names: tuple[str, ...] = ()

    # ── transient state (rides the ChemPair; snapshot/restore) ────────────────
    _level: float = field(default=0.0, init=False, repr=False)
    _armed: bool = field(default=False, init=False, repr=False)
    _last_ts: float | None = field(default=None, init=False, repr=False)
    # "never armed" sentinel: far in the past so an initial observe at now≈0 is not
    # mistaken for being inside a refractory window from an arm that never happened.
    _armed_at: float = field(default=-1e18, init=False, repr=False)
    _edge_pending: bool = field(default=False, init=False, repr=False)
    # cues captured at the moment of arming, so a later resolve() credits the cues
    # that actually drove THIS commitment (not whatever is current at resolve time).
    _cues_at_arm: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    # ── durable learned params (per-persona; cue_weights()/load_cue_weights) ───
    # RAIL: gate objects are process-global singletons, and _cue_w lives on the
    # instance — it is NOT per-persona by itself. A cue-mode gate serving multiple
    # personas must key its weights per persona externally (persist cue_weights()
    # per persona and drive load_cue_weights() on bind), or it will bleed one
    # persona's learning into another. AvoidanceTracker sidesteps this by running
    # its shared gates in scalar mode (no cue learning on the shared object).
    _cue_w: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _switch: SwitchNeuron = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # The internal switch supplies chemistry gain control (and a learned-efficacy
        # hook) for the commit bound. Its clamp is widened to the ACCUMULATOR scale:
        # a switch clamps thresholds to [0.05, 0.95] because switch inputs live in
        # [0, 1], but evidence levels run up to `cap`, so the bound must be free to
        # sit anywhere in [floor, cap] or chemistry modulation would be clipped away.
        self._switch = SwitchNeuron(
            name=self.name,
            cluster=self.cluster,
            threshold=self.arm_threshold,
            modulators=dict(self.modulators),
            min_threshold=0.01,
            max_threshold=max(self.cap, self.arm_threshold),
        )
        self._cue_w = {c: 1.0 for c in self.cue_names}

    # ── core accumulate / leak / commit ───────────────────────────────────────

    def _leak_to(self, now: float) -> None:
        """Apply half-life decay of the accumulated level up to `now`."""
        if self._last_ts is None:
            self._last_ts = now
            return
        elapsed = max(0.0, now - self._last_ts)
        if self.half_life_s > 0 and elapsed > 0:
            self._level *= 0.5 ** (elapsed / self.half_life_s)
        self._last_ts = now

    def _drift(self, evidence, snapshot: dict[str, float] | None) -> float:
        """Turn an observation into a signed evidence delta.

        Scalar mode (no cues): `evidence` is the delta itself.
        Cue mode: `evidence` is a {cue: value} dict; the delta is Σ w_cue · value,
        and the cue values are remembered so a later resolve() can credit them.
        """
        if not self.cue_names:
            v = float(evidence)
            return v if math.isfinite(v) else _drop_nonfinite(self.name)
        # Non-finite cue values are dropped BEFORE capture so they can never reach
        # _cues_at_arm (persisted in the snapshot) or _learn_cues.
        cues: dict[str, float] = {}
        for c, v in dict(evidence or {}).items():
            fv = float(v)
            if math.isfinite(fv):
                cues[c] = fv
            else:
                _drop_nonfinite(self.name)
        self._pending_cues = cues  # most-recent observation, for arm capture
        return sum(self._cue_w.get(c, 1.0) * v for c, v in cues.items())

    def observe(
        self,
        evidence,
        snapshot: dict[str, float] | None = None,
        efficacy: float = 1.0,
        now: float | None = None,
        turn_id: str = "",
        store: dict | None = None,
        key: str | None = None,
    ) -> dict | None:
        """Accumulate one observation and advance the commit state.

        Returns the switch activation payload on a FRESH arm edge (unarmed→armed),
        else None. `snapshot` (neuromod+hormonal) modulates the commit bound exactly
        as it modulates a switch's threshold; `efficacy` is the learned route
        strength (>1 lowers the bound). `store` (the bound ChemPair's evidence dict)
        holds the per-client transient state; None uses the gate's own fields. `key`
        overrides the store slot (one gate object, many independent series).
        Reading `.level`/`.armed` after this call reflects the update.
        """
        now = time.time() if now is None else now
        self._load(store, key)
        self._leak_to(now)
        self._pending_cues = {}
        delta = self._drift(evidence, snapshot)
        self._level = max(0.0, min(self.cap, self._level + delta))

        arm_bound = self._switch.effective_threshold(snapshot, efficacy)
        release_bound = arm_bound * self.release_ratio

        edge_payload = None
        if not self._armed:
            in_refractory = (
                self.refractory_s > 0 and (now - self._armed_at) < self.refractory_s
            )
            if self._level >= arm_bound and not in_refractory:
                self._armed = True
                self._armed_at = now
                self._edge_pending = True
                self._cues_at_arm = dict(getattr(self, "_pending_cues", {}) or {})
                # Commit = the internal switch fires. This records `<cluster>.<name>`
                # on the turn's firing path (SwitchNeuron.fire → record_switch_fire),
                # so the gate is a wiring node and the session Hebbian pass credits
                # its downstream edge by the turn outcome — commit-bound + influence
                # learning, for free, through the existing three-factor machinery.
                edge_payload = self._switch.fire(
                    self._level,
                    tag=f"evidence_commit:{self.name}",
                    evidence={"gate_level": round(self._level, 3), "turn_id": turn_id},
                    snapshot=snapshot,
                )
                if self.mode == MODE_FIRE_RESET:
                    self._level = 0.0
                    self._armed = False  # fire_reset re-arms immediately after refractory
        else:
            if self._level <= release_bound:
                self._armed = False
        self._save(store, key)
        return edge_payload

    @property
    def level(self) -> float:
        """Current accumulated evidence (does NOT leak-to-now; call observe or
        peek() for a time-accurate read). Provided for the pure-accumulator use
        (e.g. habituation reading a decaying scalar, like the old .state)."""
        return self._level

    def peek(
        self, now: float | None = None, store: dict | None = None, key: str | None = None
    ) -> float:
        """Leak to `now` and return the current level, without adding evidence.
        The time-accurate read for gates that consult level between observations."""
        self._load(store, key)
        self._leak_to(time.time() if now is None else now)
        self._save(store, key)
        return self._level

    @property
    def armed(self) -> bool:
        return self._armed

    def fired_edge(self) -> bool:
        """True exactly once per arm edge, then clears (fire-once debounce), mirroring
        bus.consume_quiet_onset. Lets one commitment trigger exactly one action."""
        if self._edge_pending:
            self._edge_pending = False
            return True
        return False

    # ── learning: an armed gate is a checkable prediction ─────────────────────

    def resolve(
        self,
        correct: bool,
        informativeness: float,
        bus,
        *,
        external: bool = False,
        confidence: float | None = None,
        reward_source: str = "correctness",
        store: dict | None = None,
        cues: dict[str, float] | None = None,
        emit_da: bool = True,
    ) -> float:
        """Grade a committed inference the world later confirmed/refuted, and learn.

        Routes through neuron.prediction_reward (confidence floor + informativeness
        gate + λ on a wrong confident call), emits the DA delta via bus.neuromod.add
        so it is AUDITED in the intrinsic/external tally, and nudges the drift-cue
        weights toward the cues that drove a CONFIRMED commitment. `external=True`
        (a grounded, outside-the-brain confirmation) gives full plasticity; a
        self/critic-graded confirmation is down-weighted so the gate cannot farm its
        own appraiser. `emit_da=False` learns the cue weights but does NOT touch live
        chemistry (true-shadow mode a gate uses before it is trusted to move reward).
        Returns the DA delta computed (0.0 if the anti-farm gates zeroed it); it is
        applied to chemistry only when emit_da. No-op-safe: never raises into a caller.

        Confidence defaults to the committed level normalized by the base bound (how
        far past the bar the evidence stood) — a natural stand-in for "how sure."
        """
        try:
            from brain.neuron import prediction_reward, reward_weight
            from brain.persona_key import active_or_home_persona
            from brain.settings import settings as _s

            self._load(store)
            # A shared learner (one gate learning cues across many series, e.g. the
            # avoidance tracker's per-entity gates) supplies the cues captured at that
            # series' commit, rather than relying on this gate's own _cues_at_arm.
            if cues is not None:
                self._cues_at_arm = dict(cues)
            conf = confidence
            if conf is None:
                conf = max(0.0, min(1.0, self._level / max(1e-6, self.arm_threshold) - 0.0))
                conf = min(1.0, conf)
            pr = prediction_reward(conf, correct, informativeness)
            if not pr:
                self._cues_at_arm = {}
                self._save(store)
                return 0.0

            persona = active_or_home_persona()
            base = float(_s.get("prediction_reward_base"))
            cap = float(_s.get("prediction_reward_turn_cap"))
            # External confirmation is the gold signal; self/critic is discounted so
            # it can nudge but never dominate (keeps the DA tally honest).
            ext_w = float(_s.get("evidence_external_weight", 1.0)) if external else float(
                _s.get("evidence_self_weight", 0.35)
            )
            delta = pr * base * reward_weight(persona, reward_source) * ext_w
            delta = max(-cap, min(cap, delta))
            source = "external_grader" if external else "intrinsic"
            if emit_da:
                bus.neuromod.add(
                    "DA",
                    delta,
                    source=source,
                    reward_source=reward_source,
                    reason=f"evidence_gate_resolve:{self.name}",
                )
            self._learn_cues(pr * ext_w)
            self._save(store)
            return delta
        except Exception:
            self._cues_at_arm = {}
            self._save(store)
            return 0.0

    def _learn_cues(self, signed_reward: float) -> None:
        """Nudge the drift-cue weights toward the cues present at commit, scaled by
        the (already anti-farm-gated, externally-weighted) reward. Cues that drove a
        confirmed commit strengthen; cues behind a refuted one weaken. Clamped, and a
        strict no-op in scalar mode or when the learning rate is zero."""
        if not self.cue_names or not self._cues_at_arm:
            return
        try:
            from brain.settings import settings as _s

            lr = float(_s.get("evidence_cue_lr", 0.05))
            w_min = float(_s.get("evidence_cue_w_min", 0.1))
            w_max = float(_s.get("evidence_cue_w_max", 3.0))
        except Exception:
            lr, w_min, w_max = 0.05, 0.1, 3.0
        # The setting only floors at <=0; clamp the top too so a fat-fingered lr
        # can't slam a weight across its whole [w_min, w_max] range in one resolve.
        lr = min(lr, 1.0)
        if lr <= 0:
            self._cues_at_arm = {}
            return
        for cue, val in self._cues_at_arm.items():
            if cue not in self._cue_w:
                continue
            self._cue_w[cue] = max(
                w_min, min(w_max, self._cue_w[cue] + lr * signed_reward * float(val))
            )
        self._cues_at_arm = {}

    def cue_weights(self) -> dict[str, float]:
        """The durable learned drift weights (persist per-persona, not per-client)."""
        return dict(self._cue_w)

    def load_cue_weights(self, weights: dict[str, float]) -> None:
        """Restore durable learned weights (only for declared cues)."""
        for c in self.cue_names:
            if c in weights:
                self._cue_w[c] = float(weights[c])

    # ── transient-state serialization (rides the ChemPair) ────────────────────
    #
    # Gate objects are process-global cluster singletons, but the accumulated level
    # is per-(persona, end_user). So the transient state does NOT live on the gate
    # instance in production — it lives in the bound ChemPair's `evidence` dict, and
    # the gate loads/saves its slice around each operation (pass `store=chem.evidence`
    # at the call site). With no store (tests / companion mode) it uses its own
    # fields. The `_cues_at_arm` ride along because a resolve() may land many turns
    # after the arm that captured them.

    def snapshot(self) -> dict:
        """Transient accumulator state for per-client persistence. NOT the learned
        weights (those are durable/per-persona — see cue_weights())."""
        return {
            "level": self._level,
            "armed": self._armed,
            "last_ts": self._last_ts,
            "armed_at": self._armed_at,
            "edge_pending": self._edge_pending,
            "cues_at_arm": dict(self._cues_at_arm),
        }

    def restore(self, snap: dict | None) -> None:
        """Set transient state from a snapshot. A falsy snap RESETS to a clean gate —
        this is what keeps the store-backed path per-client isolated: loading a store
        slice that does not exist yet must wipe the previous client's state off the
        shared (process-global) gate object, not leave it in place."""
        snap = snap or {}
        self._level = float(snap.get("level", 0.0))
        self._armed = bool(snap.get("armed", False))
        lt = snap.get("last_ts", None)
        self._last_ts = float(lt) if lt is not None else None
        self._armed_at = float(snap.get("armed_at", -1e18))
        self._edge_pending = bool(snap.get("edge_pending", False))
        self._cues_at_arm = dict(snap.get("cues_at_arm") or {})

    def _load(self, store: dict | None, key: str | None = None) -> None:
        """Pull this gate's transient slice out of a per-client store (the bound
        ChemPair's evidence dict). Keyed by `key` if given, else the gate name — the
        `key` override lets ONE gate object accumulate many independent series (e.g.
        one avoidance gate tracking a separate level per entity). No-op when store
        is None."""
        if store is not None:
            self.restore(store.get(key or self.name))

    def _save(self, store: dict | None, key: str | None = None) -> None:
        if store is not None:
            store[key or self.name] = self.snapshot()
