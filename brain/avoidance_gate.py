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
run through neuron.prediction_reward (confidence floor + informativeness gate + λ) with DA
emitted through the audited chokepoint, external-weighted so it cannot self-grade:
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

from brain.evidence_gate import EvidenceGate
from brain.persona_key import active_or_home_persona, persona_slug, persona_state_root
from brain.settings import settings

CUES = ("not_reengaged", "topic_shifted", "discomfort")

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

    # ── durable per-persona cue weights ───────────────────────────────────────

    def _weights(self, persona: str) -> dict[str, float]:
        key = persona_slug(persona)
        w = self._cue_w.get(key)
        if w is None:
            w = self._load_weights(key)
            self._cue_w[key] = w
        return w

    def _load_weights(self, key: str) -> dict[str, float]:
        try:
            path = persona_state_root(key) / "avoidance_cues.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return {c: float(data.get(c, 1.0)) for c in CUES}
        except Exception:
            pass
        return {c: 1.0 for c in CUES}

    def _save_weights(self, key: str) -> None:
        try:
            root = persona_state_root(key)
            root.mkdir(parents=True, exist_ok=True)
            (root / "avoidance_cues.json").write_text(
                json.dumps(self._cue_w.get(key, {}), indent=2), encoding="utf-8"
            )
        except Exception:
            pass

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
            return 0.0
        da = self._reward_and_learn(correct, conf, cues, bus, persona, weights, external=external)
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
        DA (only when avoidance_gate steers), and nudge this persona's cue weights."""
        from brain.neuron import prediction_reward, reward_weight

        pr = prediction_reward(confidence, correct, float(settings.get("avoidance_informativeness", 0.6)))
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
            bus.neuromod.add(
                "DA", delta,
                source="external_grader" if external else "intrinsic",
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
