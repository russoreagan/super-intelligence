"""
AvoidanceTracker — the first *learning* EvidenceGate: "the user is avoiding subject X."

The use-case the evidence-gate mechanism was built for (docs/RFC_integrate_and_fire.md
§7): a private per-decision inference no single turn can make, NOT spiking and NOT a bus
topic. It accumulates per-entity avoidance evidence across turns, commits with hysteresis,
and LEARNS which cues predict real avoidance from the user's own behaviour.

STATE — everything transient is per-(persona, end_user), riding the bound ChemPair's
evidence store (so binding / isolation / the one-way valve are inherited):
  • `avoid:<entity>`     — the EvidenceGate scalar accumulator slice (level, armed, …).
  • `avoidmeta:<entity>` — tracker metadata: the cues captured at commit (for learning)
                           and the turn the agent last surfaced the entity (for positive
                           confirmation). A separate key so the gate's snapshot can't clobber it.
The only durable, tracker-held state is the LEARNED CUE WEIGHTS, kept PER PERSONA and
persisted to `persona_state_root(persona)/avoidance_cues.json` — so learning is not lost on
restart and never bleeds between personas.

LEARNING — an armed belief is a checkable prediction graded by the user's own behaviour,
run through neuron.prediction_reward (confidence floor + a MEASURED informativeness gate:
the persona's observed re-engagement base rate, persisted with the cue weights + λ) with
DA emitted through the audited chokepoint. Behavioural grading buys plasticity weight,
not provenance — the belief is self-generated, so its DA is stamped `self_inference`
(audited intrinsic, never external), and all resolutions in a turn share one
prediction_reward_turn_cap budget:
  • REFUTE (false alarm): the user spontaneously re-engages a flagged entity → correct=False
    → weaken the guilty cues.
  • CONFIRM (natural occurrence, no probe): the agent's own reply surfaced a flagged entity
    and the user *still* dodged it next turn → correct=True → strengthen. This is the balance
    that keeps a refutation-only detector from drifting to silence, and it needs no risky
    agent-initiated probing.

STEER — when `avoidance_gate=1`, an armed belief biases the DMN speak/deflect judge toward
letting the avoided topic drop (brain/dmn.py consumes `deflection_bias`/`avoided_entities`).
"""

from __future__ import annotations

import json

from brain.evidence_gate import EvidenceGate, consume_turn_resolution_budget
from brain.persona_key import active_or_home_persona, persona_slug, persona_state_root
from brain.settings import settings

CUES = ("not_reengaged", "topic_shifted", "discomfort")

# Cap on the resolution-outcome counters (exponential forgetting past this total):
# the measured re-engagement base rate must be able to drift with the user, not
# fossilize on ancient history.
_STATS_MAX = 200.0

# User emotions that count as social-discomfort evidence (in the spirit of the DMN's
# _DEFLECTION_OVERRIDES; a local copy keeps this a leaf module).
DISCOMFORT_EMOTIONS = frozenset(
    {
        "embarrassed", "ashamed", "humiliated", "anxious", "uncomfortable",
        "guilty", "apologetic", "sad", "hurt",
    }
)


class AvoidanceTracker:
    def __init__(self, cluster: str = "metacognition") -> None:
        self._cluster = cluster
        self._scalar = EvidenceGate(
            name="avoidance",
            cluster=cluster,
            arm_threshold=float(settings.get("avoidance_arm_threshold", 1.5)),
            release_ratio=float(settings.get("avoidance_release_ratio", 0.5)),
            half_life_s=float(settings.get("avoidance_half_life_s", 900.0)),
            cap=float(settings.get("avoidance_cap", 5.0)),
        )
        # Durable learned cue weights, per persona slug (lazy-loaded from disk).
        self._cue_w: dict[str, dict[str, float]] = {}
        # Durable per-persona resolution-outcome counters ({"reengaged", "dodged"}):
        # the observed base rate that makes informativeness a MEASURED quantity
        # (persisted in the same avoidance_cues.json, under "_stats").
        self._stats: dict[str, dict[str, float]] = {}

    # ── durable per-persona cue weights ───────────────────────────────────────

    def _weights(self, persona: str) -> dict[str, float]:
        key = persona_slug(persona)
        w = self._cue_w.get(key)
        if w is None:
            w = self._load_weights(key)
            self._cue_w[key] = w
        return w

    def _load_weights(self, key: str) -> dict[str, float]:
        self._stats.setdefault(key, {"reengaged": 0.0, "dodged": 0.0})
        try:
            path = persona_state_root(key) / "avoidance_cues.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                st = data.get("_stats") or {}
                self._stats[key] = {
                    "reengaged": max(0.0, float(st.get("reengaged", 0.0))),
                    "dodged": max(0.0, float(st.get("dodged", 0.0))),
                }
                return {c: float(data.get(c, 1.0)) for c in CUES}
        except Exception:
            pass
        return {c: 1.0 for c in CUES}

    def _save_weights(self, key: str) -> None:
        try:
            root = persona_state_root(key)
            root.mkdir(parents=True, exist_ok=True)
            payload: dict = dict(self._cue_w.get(key, {}))
            if self._stats.get(key):
                payload["_stats"] = self._stats[key]
            (root / "avoidance_cues.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ── measured informativeness (the §4.8 anti-farm gate, not a constant) ─────

    def _informativeness(self, persona: str) -> float:
        """1 − dominant-outcome frequency of this persona's resolved avoidance
        beliefs — being right about the near-inevitable earns nothing, and that
        base rate is MEASURED from the confirm/refute events the tracker itself
        observes, not assumed. Laplace-smoothed, so a fresh persona starts at
        maximum uncertainty (0.5) and converges as outcomes accumulate."""
        self._weights(persona)  # primes the per-persona load from disk
        st = self._stats.get(persona_slug(persona)) or {}
        re_ = float(st.get("reengaged", 0.0))
        do = float(st.get("dodged", 0.0))
        p = (re_ + 1.0) / (re_ + do + 2.0)  # observed re-engagement base rate
        return min(p, 1.0 - p)

    def _record_resolution(self, persona: str, *, reengaged: bool) -> None:
        """Fold one resolved belief's outcome into the persisted base-rate stats
        (refute = the stale entity WAS re-engaged; confirm = it stayed dodged)."""
        self._weights(persona)
        key = persona_slug(persona)
        st = self._stats.setdefault(key, {"reengaged": 0.0, "dodged": 0.0})
        st["reengaged" if reengaged else "dodged"] += 1.0
        if st["reengaged"] + st["dodged"] > _STATS_MAX:  # exponential forgetting
            st["reengaged"] *= 0.5
            st["dodged"] *= 0.5
        self._save_weights(key)

    def cue_weights(self, persona: str | None = None) -> dict[str, float]:
        return dict(self._weights(persona if persona is not None else active_or_home_persona()))

    # ── per-turn ingestion ────────────────────────────────────────────────────

    def observe_turn(
        self,
        current_entities: set[str],
        stale_entities: dict[str, int],
        turn_count: int,
        user_emotion: str,
        bus,
        *,
        agent_text: str = "",
        store: dict | None = None,
        snapshot: dict[str, float] | None = None,
        now: float | None = None,
    ) -> list[str]:
        """Ingest one turn. Confirm/refute prior beliefs from the user's behaviour, then
        accumulate fresh avoidance evidence. Returns entities newly armed this turn.
        No-op-safe and gated: does nothing when `evidence_gates` is off."""
        if not settings.get("evidence_gates", 0) or store is None:
            return []
        try:
            persona = active_or_home_persona()
            weights = self._weights(persona)
            stale_after = int(settings.get("avoidance_stale_turns", 2))
            emo = (user_emotion or "").lower()
            discomfort = emo in DISCOMFORT_EMOTIONS
            topic_shifted = bool(current_entities)

            # 1) CONFIRM (natural): the agent surfaced a flagged entity last turn and the
            #    user still isn't engaging it → the avoidance was real.
            for ent in list(self._armed_entities(store)):
                meta = store.get(f"avoidmeta:{ent}", {})
                if meta.get("surfaced_turn") and ent not in current_entities:
                    self._confirm(ent, True, bus, persona, weights, store)

            # 2) REFUTE: the user spontaneously re-engaged a flagged entity → false alarm.
            for ent in list(current_entities):
                if self._is_armed(ent, store):
                    self._confirm(ent, False, bus, persona, weights, store)
                store.pop(f"avoid:{ent}", None)  # engaging clears the accumulator either way
                store.pop(f"avoidmeta:{ent}", None)

            # 3) ACCUMULATE for stale entities the user is not engaging this turn.
            newly_armed: list[str] = []
            for ent, last_seen in stale_entities.items():
                if ent in current_entities or (turn_count - int(last_seen)) < stale_after:
                    continue
                cues = {
                    "not_reengaged": 1.0,
                    "topic_shifted": 1.0 if topic_shifted else 0.0,
                    "discomfort": 1.0 if discomfort else 0.0,
                }
                drift = sum(weights.get(c, 1.0) * v for c, v in cues.items())
                payload = self._scalar.observe(
                    drift, snapshot=snapshot, now=now, store=store, key=f"avoid:{ent}"
                )
                if payload is not None:  # fresh arm edge
                    lvl = self._scalar.peek(now=now, store=store, key=f"avoid:{ent}")
                    conf = min(1.0, lvl / max(1e-6, self._scalar.arm_threshold))
                    store[f"avoidmeta:{ent}"] = {"cues": cues, "confidence": conf}
                    newly_armed.append(ent)
                    self._log("avoidance_armed", entity=ent, confidence=round(conf, 3), cues=cues)

            # 4) Record which flagged entities the agent's OWN reply surfaced, so next
            #    turn can check whether the user dodged it (positive confirmation).
            if agent_text:
                low = agent_text.lower()
                for ent in self._armed_entities(store):
                    # length guard: avoid trivial substring false-positives on 1–2 char entities
                    if len(ent) >= 3 and ent.lower() in low:
                        meta = store.get(f"avoidmeta:{ent}")
                        if meta is not None:
                            meta["surfaced_turn"] = turn_count
            return newly_armed
        except Exception:
            return []

    # ── learning ──────────────────────────────────────────────────────────────

    def _confirm(
        self, entity: str, correct: bool, bus, persona: str, weights: dict, store: dict,
        *, external: bool = True,
    ) -> float:
        """Grade an armed belief the user's behaviour confirmed (correct=True) or refuted
        (correct=False) and learn. The live signals (re-engagement / continued dodging)
        are external + audited; anti-farm via prediction_reward. Clears the belief's
        state. Emits DA only when steering is on (avoidance_gate)."""
        meta = store.get(f"avoidmeta:{entity}") or {}
        cues = meta.get("cues") or {}
        conf = float(meta.get("confidence", 0.7))
        store.pop(f"avoid:{entity}", None)
        store.pop(f"avoidmeta:{entity}", None)
        if not cues:
            self._record_resolution(persona, reengaged=not correct)
            return 0.0
        da = self._reward_and_learn(correct, conf, cues, bus, persona, weights, external=external)
        # Record AFTER grading so informativeness reflects the base rate as it stood
        # BEFORE this outcome (the prior uncertainty of the prediction). The outcome
        # feeds the measured rate regardless of reward gating (refuted = the user
        # DID re-engage the entity).
        self._record_resolution(persona, reengaged=not correct)
        self._log(
            "avoidance_confirmed" if correct else "avoidance_refuted",
            entity=entity, correct=correct, da=round(da, 4),
        )
        return da

    def confirm(
        self, entity: str, correct: bool, bus, *, external: bool = True, store: dict | None = None
    ) -> float:
        """Explicit confirmation hook (tests / a future active probe)."""
        persona = active_or_home_persona()
        weights = self._weights(persona)
        if store is not None:
            return self._confirm(entity, correct, bus, persona, weights, store, external=external)
        return 0.0

    def _reward_and_learn(
        self, correct: bool, confidence: float, cues: dict, bus, persona: str,
        weights: dict, *, external: bool,
    ) -> float:
        """Reuse neuron.prediction_reward for the anti-farm-gated reward, emit the audited
        DA (only when avoidance_gate steers), and nudge this persona's cue weights.
        Informativeness is MEASURED from this persona's observed re-engagement base
        rate, and the DA is stamped `self_inference` — the belief is self-generated
        even when the user's behaviour grades it, so it must never count as external
        in the honesty tally (that bucket is for genuine partner/owner grades)."""
        from brain.neuron import prediction_reward, reward_weight

        pr = prediction_reward(confidence, correct, self._informativeness(persona))
        if not pr:
            return 0.0
        ext_w = (
            float(settings.get("evidence_external_weight", 1.0)) if external
            else float(settings.get("evidence_self_weight", 0.35))
        )
        delta = 0.0
        if settings.get("avoidance_gate", 0):  # live: move real chemistry (audited)
            base = float(settings.get("prediction_reward_base"))
            cap = float(settings.get("prediction_reward_turn_cap"))
            delta = max(-cap, min(cap, pr * base * reward_weight(persona, "correctness") * ext_w))
            # cap = the turn's TOTAL resolution budget (shared with every
            # EvidenceGate), not a per-entity clamp — N resolutions ≠ N caps.
            delta = consume_turn_resolution_budget(bus.neuromod, delta, cap)
            if delta:
                bus.neuromod.add(
                    "DA", delta,
                    source="self_inference" if external else "intrinsic",
                    reward_source="correctness", reason="avoidance_resolve",
                )
        # learn this persona's cue weights (bounded); persist so it survives restart
        lr = float(settings.get("evidence_cue_lr", 0.05))
        w_min = float(settings.get("evidence_cue_w_min", 0.1))
        w_max = float(settings.get("evidence_cue_w_max", 3.0))
        for c, v in cues.items():
            if c in weights:
                weights[c] = max(w_min, min(w_max, weights[c] + lr * pr * ext_w * float(v)))
        self._save_weights(persona_slug(persona))
        return delta

    # ── steering (consumed by the DMN speak/deflect judge) ────────────────────

    def _armed_entities(self, store: dict) -> list[str]:
        return [
            k[len("avoid:"):]
            for k, v in store.items()
            if k.startswith("avoid:") and isinstance(v, dict) and v.get("armed")
        ]

    def _is_armed(self, entity: str, store: dict) -> bool:
        v = store.get(f"avoid:{entity}")
        return bool(isinstance(v, dict) and v.get("armed"))

    def avoided_entities(self, store: dict | None) -> list[str]:
        """Entities currently believed avoided in the bound context (armed)."""
        return self._armed_entities(store) if store else []

    def is_avoided(self, entity: str, store: dict | None) -> bool:
        return self._is_armed(entity, store) if store else False

    def deflection_bias(self, store: dict | None = None) -> bool:
        """Whether the mind should lean toward letting an avoided topic drop. False in
        shadow (`avoidance_gate=0`); the DMN judge only consults this when the flag is on."""
        if not settings.get("avoidance_gate", 0) or not store:
            return False
        return bool(self._armed_entities(store))

    # ── observability ─────────────────────────────────────────────────────────

    def _log(self, kind: str, **fields) -> None:
        try:
            from brain.observability.decisions import decisions

            decisions.log(kind, steer=int(bool(settings.get("avoidance_gate", 0))), **fields)
        except Exception:
            pass
