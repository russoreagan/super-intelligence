"""
AvoidanceTracker — the first *learning* EvidenceGate: "the user is avoiding subject X."

The use-case the evidence-gate mechanism was built for (docs/RFC_integrate_and_fire.md
§7): a private per-decision inference no single turn can make, NOT spiking and NOT a bus
topic. It accumulates per-entity avoidance evidence across turns, commits with hysteresis,
and LEARNS which cues predict real avoidance from the user's own behaviour.

EVIDENCE — only ACTIVE dodge signals accumulate; merely not talking about something is
NOT avoiding it (a user who simply moved on must never arm this gate):
  • `surfaced_dodge` — the entity was surfaced (the agent's own reply mentioned it) and
                       the user's next turn didn't pick it up. Each surfacing is
                       consumable exactly once, so one mention can't drip evidence forever.
  • `abrupt_shift`   — the entity just went stale off a live thread while the user
                       actively moved to other topics (fires once, at the crossing).
  • `discomfort`     — a discomfort emotion riding one of the two dodges above (never
                       counted on its own — affect without a dodge is not about this entity).
Passive staleness contributes nothing, so an armed belief is only re-fed by fresh dodges
and the leak can genuinely win (see LIFECYCLE).

STATE — everything transient is per-(persona, end_user), riding the bound ChemPair's
evidence store (so binding / isolation / the one-way valve are inherited):
  • `avoid:<entity>`     — the EvidenceGate scalar accumulator slice (level, armed, …).
  • `avoidmeta:<entity>` — tracker metadata: the cues captured at commit (for learning)
                           and the turn the agent last surfaced the entity (for dodge
                           detection + positive confirmation). A separate key so the
                           gate's snapshot can't clobber it.
The only durable, tracker-held state is the LEARNED CUE WEIGHTS, kept PER PERSONA and
persisted to `persona_state_root(persona)/avoidance_cues.json` — so learning is not lost on
restart and never bleeds between personas.

LIFECYCLE — an armed belief must be able to expire WITHOUT user action (steering
suppresses the very surfacing that would refute it, so it can't rely on refutation):
  • the leak (avoidance_half_life_s) releases an unfed suspicion — a per-turn sweep
    applies decay + hysteresis release even when no fresh evidence arrives;
  • a wall-clock cap (avoidance_max_armed_s) clears the slate on any belief armed too
    long, the same escape hatch open_threads and habituation carry, and the read path
    the DMN consults filters by it so a stale flag can't steer between turns;
  • decayed accumulator slices below avoidance_evict_floor (and stale bare surfacing
    records) are deleted, so persisted client_chem snapshots stay bounded.

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
import time

from brain.evidence_gate import EvidenceGate, consume_turn_resolution_budget
from brain.persona_key import active_or_home_persona, persona_slug, persona_state_root
from brain.settings import settings

CUES = ("surfaced_dodge", "abrupt_shift", "discomfort")

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
        """Ingest one turn. Sweep expired/decayed beliefs, confirm/refute prior beliefs
        from the user's behaviour, then accumulate fresh avoidance evidence — from ACTIVE
        dodge signals only (a surfaced topic the user didn't pick up; an abrupt shift off
        a live thread; discomfort riding either). Mere staleness contributes nothing, so a
        user who simply moved on never arms this gate. Returns entities newly armed this
        turn. No-op-safe and gated: does nothing when `evidence_gates` is off."""
        if not settings.get("evidence_gates", 0) or store is None:
            return []
        try:
            now_ts = time.time() if now is None else now
            persona = active_or_home_persona()
            weights = self._weights(persona)
            stale_after = int(settings.get("avoidance_stale_turns", 2))
            emo = (user_emotion or "").lower()
            discomfort = emo in DISCOMFORT_EMOTIONS

            # 0) SWEEP: leak/release unfed suspicions, expire beliefs armed past the
            #    wall-clock cap, evict decayed slices — the escape hatches that let an
            #    armed belief die without user action.
            self._sweep(store, now_ts, turn_count)

            # 1) CONFIRM (natural): the agent surfaced a flagged entity last turn and the
            #    user still isn't engaging it → the avoidance was real.
            for ent in list(self._armed_entities(store, now=now_ts)):
                meta = store.get(f"avoidmeta:{ent}") or {}
                if self._fresh_surfacing(meta, turn_count) and ent not in current_entities:
                    self._confirm(ent, True, bus, persona, weights, store)

            # 2) REFUTE: the user spontaneously re-engaged a flagged entity → false alarm.
            for ent in list(current_entities):
                if self._is_armed(ent, store, now=now_ts):
                    self._confirm(ent, False, bus, persona, weights, store)
                store.pop(f"avoid:{ent}", None)  # engaging clears the accumulator either way
                store.pop(f"avoidmeta:{ent}", None)

            # 3) ACCUMULATE — only for stale entities with an active dodge THIS turn.
            newly_armed: list[str] = []
            for ent, last_seen in stale_entities.items():
                turns_stale = turn_count - int(last_seen)
                if ent in current_entities or turns_stale < stale_after:
                    continue
                meta = store.get(f"avoidmeta:{ent}") or {}
                surfaced_dodge = 1.0 if self._fresh_surfacing(meta, turn_count) else 0.0
                # An abrupt shift fires once, at the moment the entity crosses stale off
                # a live thread while the user is actively on other topics — not on every
                # later turn it happens to remain unmentioned.
                abrupt_shift = (
                    1.0 if (turns_stale == stale_after and current_entities) else 0.0
                )
                if not (surfaced_dodge or abrupt_shift):
                    continue  # no dodge → not evidence; the leak keeps winning
                cues = {
                    "surfaced_dodge": surfaced_dodge,
                    "abrupt_shift": abrupt_shift,
                    "discomfort": 1.0 if discomfort else 0.0,
                }
                if surfaced_dodge:  # one surfacing yields at most one dodge
                    m = store.get(f"avoidmeta:{ent}")
                    if m is not None:
                        m.pop("surfaced_turn", None)
                drift = sum(weights.get(c, 1.0) * v for c, v in cues.items())
                payload = self._scalar.observe(
                    drift, snapshot=snapshot, now=now_ts, store=store, key=f"avoid:{ent}"
                )
                if payload is not None:  # fresh arm edge
                    lvl = self._scalar.peek(now=now_ts, store=store, key=f"avoid:{ent}")
                    conf = min(1.0, lvl / max(1e-6, self._scalar.arm_threshold))
                    store[f"avoidmeta:{ent}"] = {"cues": cues, "confidence": conf}
                    newly_armed.append(ent)
                    self._log("avoidance_armed", entity=ent, confidence=round(conf, 3), cues=cues)

            # 4) Record which known entities the agent's OWN reply surfaced, so next turn
            #    can check whether the user dodged it (surfaced_dodge evidence for a
            #    candidate; positive confirmation for an armed belief).
            if agent_text:
                low = agent_text.lower()
                for ent in stale_entities:
                    # length guard: avoid trivial substring false-positives on 1–2 char entities
                    if ent in current_entities or len(ent) < 3 or ent.lower() not in low:
                        continue
                    meta = store.get(f"avoidmeta:{ent}")
                    if meta is None:
                        meta = store[f"avoidmeta:{ent}"] = {}
                    meta["surfaced_turn"] = turn_count
            return newly_armed
        except Exception:
            return []

    # ── lifecycle: sweep / expiry / eviction ──────────────────────────────────

    @staticmethod
    def _fresh_surfacing(meta: dict, turn_count: int) -> bool:
        """True when the entity was surfaced on the immediately preceding turn (or this
        one) — the only window in which non-engagement counts as a dodge."""
        st = meta.get("surfaced_turn")
        return st is not None and int(st) >= turn_count - 1

    def _decayed_level(self, v: dict, now: float) -> float:
        """The slice's accumulator level leaked to `now` (read-only math; the stored
        snapshot is not touched)."""
        lvl = float(v.get("level", 0.0))
        lt = v.get("last_ts")
        hl = self._scalar.half_life_s
        if hl > 0 and lt is not None:
            lvl *= 0.5 ** (max(0.0, now - float(lt)) / hl)
        return lvl

    def _sweep(self, store: dict, now: float, turn_count: int) -> None:
        """Per-turn lifecycle pass over this client's slices. Because accumulation now
        requires an active dodge, nothing else re-touches an unfed entity — this sweep is
        where the leak releases it, the wall-clock cap expires it, and dead slices are
        deleted so the persisted store stays bounded."""
        max_armed = float(settings.get("avoidance_max_armed_s", 86400.0))
        floor = float(settings.get("avoidance_evict_floor", 0.05))
        release = self._scalar.arm_threshold * self._scalar.release_ratio
        for key in [k for k in store if k.startswith("avoid:")]:
            ent = key[len("avoid:"):]
            v = store.get(key)
            if not isinstance(v, dict):
                store.pop(key, None)
                continue
            lvl = self._decayed_level(v, now)
            if v.get("armed"):
                age = now - float(v.get("armed_at", now))
                if age > max_armed:
                    # clear the slate: a belief held this long without fresh refutation
                    # or confirmation expires and must be re-earned from fresh dodges.
                    store.pop(key, None)
                    store.pop(f"avoidmeta:{ent}", None)
                    self._log("avoidance_expired", entity=ent, age_s=round(age, 1))
                    continue
                if lvl <= release:  # the leak won: stand down (hysteresis release)
                    v["armed"] = False
                    v["level"] = lvl
                    v["last_ts"] = now
                    self._log("avoidance_released", entity=ent)
            if not v.get("armed") and lvl < floor:
                store.pop(key, None)
                meta = store.get(f"avoidmeta:{ent}")
                if meta is not None and not self._fresh_surfacing(meta, turn_count):
                    store.pop(f"avoidmeta:{ent}", None)
        # bare surfacing records (no accumulator) are consumable for exactly one turn
        for key in [k for k in store if k.startswith("avoidmeta:")]:
            ent = key[len("avoidmeta:"):]
            if f"avoid:{ent}" in store:
                continue
            if not self._fresh_surfacing(store.get(key) or {}, turn_count):
                store.pop(key, None)

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
    #
    # Read paths re-derive the EFFECTIVE armed state at `now` (stored flag, minus
    # wall-clock expiry, minus leak below the release band) without mutating the
    # store — so a stale flag can't steer the DMN between turns; the next
    # observe_turn's sweep does the actual cleanup.

    def _slice_armed(self, v, now: float) -> bool:
        if not (isinstance(v, dict) and v.get("armed")):
            return False
        if (now - float(v.get("armed_at", now))) > float(
            settings.get("avoidance_max_armed_s", 86400.0)
        ):
            return False
        release = self._scalar.arm_threshold * self._scalar.release_ratio
        return self._decayed_level(v, now) > release

    def _armed_entities(self, store: dict, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        return [
            k[len("avoid:"):]
            for k, v in store.items()
            if k.startswith("avoid:") and self._slice_armed(v, now)
        ]

    def _is_armed(self, entity: str, store: dict, now: float | None = None) -> bool:
        return self._slice_armed(store.get(f"avoid:{entity}"), time.time() if now is None else now)

    def avoided_entities(self, store: dict | None, now: float | None = None) -> list[str]:
        """Entities currently believed avoided in the bound context (effectively armed)."""
        return self._armed_entities(store, now=now) if store else []

    def is_avoided(self, entity: str, store: dict | None, now: float | None = None) -> bool:
        return self._is_armed(entity, store, now=now) if store else False

    def deflection_bias(self, store: dict | None = None, now: float | None = None) -> bool:
        """Whether the mind should lean toward letting an avoided topic drop. False in
        shadow (`avoidance_gate=0`); the DMN judge only consults this when the flag is on."""
        if not settings.get("avoidance_gate", 0) or not store:
            return False
        return bool(self._armed_entities(store, now=now))

    # ── observability ─────────────────────────────────────────────────────────

    def _log(self, kind: str, **fields) -> None:
        try:
            from brain.observability.decisions import decisions

            decisions.log(kind, steer=int(bool(settings.get("avoidance_gate", 0))), **fields)
        except Exception:
            pass
