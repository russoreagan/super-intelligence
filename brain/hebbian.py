"""Hebbian learning pass — runs at session end as part of sleep consolidation."""

from __future__ import annotations

import contextlib
import logging
import os
from functools import lru_cache

from brain.emotion_hierarchy import CORE_VALENCE, valence_of
from brain.observability.decisions import decisions
from brain.settings import settings
from brain.wiring import WEIGHT_REST, Wiring

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _competition_owned(n_reserve: int) -> frozenset[tuple[str, str]]:
    """Edges OWNED by an explicit contrastive competition.

    These must not also receive ordinary path credit. The competition credit is
    winner-contingent and small (~margin × bonus_scale × 0.5); path credit is ~20x
    larger and is awarded to whichever competitor happened to fire FIRST, so it
    swamps the quality signal with an ordering artifact. Observed on two families
    in second_brain/wiring_history: executive→drafter_A reached 1.0088 while
    drafter_B sat at 0.9999 (A is first-fired), and drafter_B→critic moved while
    drafter_A→critic never did (there, LAST-before-the-critic wins).

    Derived from the same node-name constructors the competition helpers use rather
    than tagged onto the edges themselves: a persisted `credit_owner` field would
    assert that a crediting function exists, and would silently starve its edges the
    day that function is renamed. A derived predicate cannot drift, because deleting
    the helper deletes the constructor it is built from.
    """
    drafters = [f"frontal.drafter_{chr(65 + i)}" for i in range(5 + max(0, n_reserve))]
    owned = {("frontal.executive", d) for d in drafters}
    # Approach cells: _apply_approach_stance_credit documents them as "interchangeable
    # by design, so a source→cell edge means nothing" — yet frontal._select_competitors
    # samples on these weights, and today they quietly collect first-fired credit.
    # Excluding them makes the runtime match the documented intent.
    owned |= {
        ("temporal.understanding_integrator", f"frontal.approach_{chr(65 + i)}") for i in range(3)
    }
    # Drafter→judge edges have no competition owner, so path credit here is pure
    # ordering noise. The co-activation pass re-credits them by the drafter's own
    # critic score, which tracks QUALITY instead of position in the path.
    owned |= {
        (d, judge)
        for d in drafters
        for judge in ("frontal.critic", "frontal.empathy_critic", "frontal.commitment_extractor")
    }
    return frozenset(owned)


class HebbianUpdater:
    """Apply Hebbian weight updates to the wiring graph after a session."""

    _CORE_VALENCE = CORE_VALENCE

    def __init__(self, wiring: Wiring) -> None:
        self._wiring = wiring

    @classmethod
    def _emotion_valence(cls, emotion: str | None) -> float:
        return valence_of(emotion)

    def _composite_outcome(self, trace) -> tuple[float, dict]:
        """Return (outcome, breakdown) for a single TurnTrace. Outcome in [-1, +1].

        Signal sources:
        - DA delta (50%): how much DA changed THIS turn vs start of turn.
        - Critic score (30%): actual LLM critic assessment; only when critic_ran=True.
        - User emotion valence (20%): valence of the user's detected emotional state.

        When the turn carries an EXTERNAL grade (a thumbs verdict, a validator) the
        mix re-weights to make room for it — the one signal here that is grounded
        outside the brain's own appraisal (the premise-audit's self-grading fix).
        Absent a grade, the legacy 0.5/0.3/0.2 path is untouched.
        """
        nm = trace.neuromod or {}
        da = float(nm.get("DA", 0.5))

        prior_nm = getattr(trace, "prior_neuromod", None) or {}
        da_prior = float(prior_nm.get("DA", da))
        da_delta = (da - da_prior) * 4.0
        da_delta = max(-1.0, min(1.0, da_delta))

        critic_term = 0.0
        for d in trace.draft_scores or []:
            if d.get("selected") and d.get("critic_ran"):
                critic_term = (float(d.get("overall", 0.5)) - 0.5) * 2.0
                break

        user_emotion = getattr(trace, "user_emotion", "") or ""
        user_term = self._emotion_valence(user_emotion)

        ext_grade = getattr(trace, "external_grade", None)
        if ext_grade is not None:
            ext = max(-1.0, min(1.0, float(ext_grade)))
            w_da = float(settings.get("hebbian_w_da_ext", 0.4))
            w_critic = float(settings.get("hebbian_w_critic_ext", 0.2))
            w_user = float(settings.get("hebbian_w_user_ext", 0.2))
            w_ext = float(settings.get("hebbian_w_external", 0.2))
            outcome = w_da * da_delta + w_critic * critic_term + w_user * user_term + w_ext * ext
        else:
            outcome = 0.5 * da_delta + 0.3 * critic_term + 0.2 * user_term
        outcome = max(-1.0, min(1.0, outcome))
        breakdown = {
            "da_delta": round(da_delta, 3),
            "da_prior": round(da_prior, 3),
            "da_current": round(da, 3),
            "critic": round(critic_term, 3),
            "user_emotion": round(user_term, 3),
        }
        if ext_grade is not None:
            breakdown["external"] = round(float(ext_grade), 3)
        return outcome, breakdown

    @staticmethod
    def _credit_pairs(path_names: list[str]) -> list[tuple[str, str]]:
        """Consecutive pairs of a fired path, minus the competition-owned edges.

        Used by BOTH the main pass and the eligibility replay. Filtering only the
        main pass would let every past-path replay quietly re-inject the ordering
        artifact this exists to remove."""
        if len(path_names) < 2:
            return []
        pairs = [(path_names[i], path_names[i + 1]) for i in range(len(path_names) - 1)]
        if not settings.get("credit_purity", 1):
            return pairs
        owned = _competition_owned(int(settings.get("node_reserve_pool", 3)))
        return [p for p in pairs if p not in owned]

    @staticmethod
    def _batch_decay(rate_per_turn: float, n_turns: int) -> float:
        """Scale a PER-TURN decay rate to the `n_turns` in a consolidation batch.

        Consolidation runs once per batch but reinforcement accrues per turn, so the
        two must be expressed in the same unit or equilibrium drifts with session
        length (it was 1.15 for a 1-turn session against a clamped 3.92 for a 20-turn
        one — a 26x spread on identical settings).

        The scaling is LINEAR (n·r), not compounded (1-(1-r)^n), and that pairing is
        forced by how gain is applied. The batch adds the sum of its turns' deltas, so
        equilibrium is w* = 1 + ΣG/E; for that to equal the per-turn 1 + ḡ/r for every
        n, E must be exactly n·r. Compounding is the correct decay-only answer but is
        sublinear (1-(1-r)^n < n·r), which leaves longer batches settling ~10% higher —
        measured at 1.490/1.509/1.548 for n = 1/5/20 before this was corrected.

        Capped by `decay_batch_max` because n is unbounded in practice — the idle loop
        can consolidate a large backlog in one pass, and an uncapped linear rate would
        exceed 1.0, overshooting rest and inverting the deviation. Invariance therefore
        holds while n·r <= the cap; beyond it a very large backlog forgets proportionally
        less than a strict reading would demand, which is the safe direction to err.
        """
        r = max(0.0, min(1.0, rate_per_turn))
        cap = float(settings.get("decay_batch_max", 0.90))
        return min(cap, r * max(1, n_turns))

    def _plasticity_modulator(self, full_traces: list) -> float:
        """Session-averaged DA + ACh → plasticity scalar in [0.3, 1.2]."""
        if not full_traces:
            return 1.0
        da_avg = sum(float(t.neuromod.get("DA", 0.5)) for t in full_traces) / len(full_traces)
        ach_avg = sum(float(t.neuromod.get("ACh", 0.3)) for t in full_traces) / len(full_traces)
        mod = 0.5 + da_avg + 0.5 * ach_avg
        return max(0.3, min(1.2, mod))

    def _turn_plasticity(self, trace) -> float:
        """Per-turn plasticity multiplier keyed to AROUSAL / emotional INTENSITY
        (not valence sign), with an inverted-U high-stress knee.

        Basis (see plan References): three-factor learning rules — neuromodulators
        continuously scale how much a co-activation imprints — plus the inverted-U
        of stress on memory: moderate arousal ENHANCES encoding (emotionally intense
        events of EITHER sign imprint hard — fear learning), while only EXTREME
        stress impairs it. This replaces the legacy all-or-nothing `defuse_path`
        skip with a graded factor.

        Returns 1.0 (identity — legacy behaviour preserved) when the
        `graded_plasticity` flag is off.
        """
        if not settings.get("graded_plasticity", 0):
            return 1.0
        nm = trace.neuromod or {}
        ach = float(nm.get("ACh", 0.3))
        ne = float(nm.get("NE", 0.15))
        da = float(nm.get("DA", 0.5))
        gaba = float(nm.get("GABA", 0.0))
        prior = getattr(trace, "prior_neuromod", None) or {}
        da_swing = abs(da - float(prior.get("DA", da)))
        hormonal = getattr(trace, "hormonal", None) or {}
        cort = float(hormonal.get("CORT", 0.0))

        # Arousal drive (alertness + novelty + reward swing). Centered so a
        # resting turn (~0.3) contributes nothing.
        arousal = (ach + ne + da_swing) / 3.0
        # Emotional intensity: |valence| of entity or user emotion. Either sign.
        intensity = max(
            abs(self._emotion_valence(trace.emotion)),
            abs(self._emotion_valence(getattr(trace, "user_emotion", "") or "")),
        )
        w_ar = float(settings.get("plasticity_arousal_weight", 0.5))
        w_in = float(settings.get("plasticity_intensity_weight", 0.4))
        plast = 1.0 + w_ar * (arousal - 0.3) + w_in * intensity

        # Inverted-U descending limb: only stress above the knee dampens, scaling
        # from 1.0 at the knee toward (1 - damp) at maximal stress.
        knee = float(settings.get("plasticity_stress_knee", 0.7))
        stress = max(gaba, cort)
        if stress > knee:
            damp = float(settings.get("plasticity_stress_damp", 0.6))
            frac = min(1.0, (stress - knee) / max(1e-6, 1.0 - knee))
            plast *= 1.0 - damp * frac

        lo = float(settings.get("plasticity_turn_min", 0.4))
        hi = float(settings.get("plasticity_turn_max", 1.3))
        return max(lo, min(hi, plast))

    def _should_skip_hebbian(self, trace, outcome: float) -> tuple[bool, str]:
        """Skip Hebbian for turns where the entity wasn't in a state worth learning from."""
        if abs(outcome) < 0.02:
            return True, "outcome_near_zero"
        # Legacy all-or-nothing defensive skip. When graded_plasticity is on this
        # is replaced by the inverted-U high-stress dampener in _turn_plasticity
        # (learn LESS from extreme-stress turns, not zero) — biologically faithful.
        if not settings.get("graded_plasticity", 0):
            gaba = float(trace.neuromod.get("GABA", 0.0))
            if gaba > settings.get("gaba_skip_threshold_high") and len(trace.draft_scores) <= 1:
                return True, "defuse_path"
        emotion = (trace.emotion or "").lower()
        if emotion in ("confused", "flat"):
            return True, f"dissociated_emotion={emotion}"
        return False, ""

    # ── Within-turn competition credit ────────────────────────────────────────
    #
    # N candidates emit, a critic scores them, one is selected; the winner's
    # source→cell edge gains in proportion to its margin over the mean loser and
    # the losers decay in proportion to their shortfall. The DRAFTERS compete on
    # phrasing (post-plan); the APPROACHES (planned pre-tool stage) will compete
    # on strategy. Only the namespace, the entry→node mapping, and the log
    # identity differ, so the reward math is single-sourced here: two copies of
    # a scoring rule drift, and drift in a REWARD rule is silent.

    def _apply_competition(
        self,
        trace,
        entries: list,
        *,
        source: str,
        resolve_node,
        bonus_scale: float,
        event: str,
        role: str,
        gainers: list,
        losers: list,
    ) -> int:
        """Reinforce the selected entry's source→cell edge; decay the others.

        Only entries with critic_ran=True count, and fewer than two of those
        means there was no competition to learn from. The winner is found by
        IDENTITY (the `selected` entry) — the core never touches an ID string;
        `resolve_node` owns the entry→node mapping entirely and may return None
        to skip an entry (non-competitor producers, malformed ids).

        Returns the number of LIVE edges touched (folded into total_updated by
        the caller). A turn with no `selected` entry returns 0 — the old
        separate edge-count helper counted those edges even though the apply
        pass had bailed, quietly inflating edges_updated."""
        real_scored = [d for d in (entries or []) if d.get("critic_ran")]
        if len(real_scored) < 2:
            return 0
        selected = next((d for d in real_scored if d.get("selected")), None)
        if selected is None:
            return 0

        winner_overall = float(selected.get("overall", 0.5))
        loser_scores = [float(x.get("overall", 0.5)) for x in real_scored if x is not selected]
        margin = winner_overall - (sum(loser_scores) / len(loser_scores) if loser_scores else 0.5)

        updated = 0
        for d in real_scored:
            node = resolve_node(d)
            if not node:
                continue
            edge = (source, node)
            if not self._wiring.has(*edge):
                continue
            updated += 1

            prev = self._wiring.get_edge_weight(*edge)
            won = d is selected
            if won:
                self._wiring.hebbian_update([edge[0], edge[1]], margin * bonus_scale * 0.5)
            else:
                shortfall = winner_overall - float(d.get("overall", 0.5))
                self._wiring.hebbian_update([edge[0], edge[1]], -(shortfall * bonus_scale * 0.25))

            now = self._wiring.get_edge_weight(*edge)
            edge_delta = now - prev
            if abs(edge_delta) > 0.001:
                label = f"{edge[0]}→{edge[1]}"
                (gainers if edge_delta > 0 else losers).append((label, edge_delta))
                decisions.log(
                    event,
                    turn_id=trace.turn_id,
                    won=won,
                    from_weight=round(prev, 4),
                    to_weight=round(now, 4),
                    delta=round(edge_delta, 4),
                    winner_score=round(winner_overall, 3),
                    **{role: node},
                )
        return updated

    def _apply_approach_stance_credit(
        self, trace, plasticity: float, gainers: list, losers: list
    ) -> int:
        """Stance credit for the pre-tool approach competition — attaches to the
        STANCES, not the cells (the approach cells are interchangeable by design,
        so a source→cell edge means nothing). Both winning axes earn fragment
        edges on the single frontal.approach_stage anchor.

        Credit is OUTCOME-FIRST: the verifier's grounded per-axis verdict
        (trace.approach_outcome, patched a turn late) weighs heavily; absent
        verification, the critic's margin contributes a small self-graded step.
        Loser demotion is LIGHT and only on existing edges — a loser was never
        executed, and the only thing against it is the critic's preference."""
        if not settings.get("approach_competition_credit", 1):
            return 0
        scores = getattr(trace, "approach_scores", None) or []
        real = [d for d in scores if d.get("critic_ran")]
        if len(real) < 2:
            return 0
        selected = next((d for d in real if d.get("selected")), None)
        if selected is None:
            return 0
        from brain.fragment_pool import APPROACH_ANCHOR, fragment_node_name, is_admissible
        from brain.wiring import WEIGHT_REST as _REST

        outcome = getattr(trace, "approach_outcome", None) or {}
        gain = float(settings.get("fragment_gain", 0.2)) * plasticity
        updated = 0
        for d in real:
            won = d is selected
            for axis, sid_key, adm_kind in (
                ("info", "info_id", None),
                ("method", "method_id", "method"),
            ):
                sid = str(d.get(sid_key, "") or "")
                if not sid or not is_admissible(sid, APPROACH_ANCHOR, adm_kind):
                    continue
                fnode = fragment_node_name(sid)
                if won:
                    verified = outcome.get(axis) if outcome else None
                    if verified is not None:
                        # Grounded evidence: full weight, and the edge is created
                        # regardless of sign — the winner EXECUTED, so a refutation
                        # is real ground truth worth remembering (unlike a loser's,
                        # which is only the critic's preference).
                        delta = float(verified) * gain
                        verb = "approach_verified"
                        self._wiring.add(fnode, APPROACH_ANCHOR, weight=_REST)
                    else:
                        margin = float(d.get("overall", 0.5)) - 0.5
                        delta = margin * gain * 0.35  # self-graded: small step
                        verb = "approach_critic"
                        if delta > 0:
                            self._wiring.add(fnode, APPROACH_ANCHOR, weight=_REST)
                        elif not self._wiring.has(fnode, APPROACH_ANCHOR):
                            continue  # never CREATE an edge just to self-demote
                    n = self._wiring.hebbian_update([fnode, APPROACH_ANCHOR], delta)
                else:
                    if not self._wiring.has(fnode, APPROACH_ANCHOR):
                        continue
                    n = self._wiring.hebbian_update([fnode, APPROACH_ANCHOR], -(gain * 0.1))
                    verb = "approach_loser"
                if n:
                    updated += n
                    w = self._wiring.get_edge_weight(fnode, APPROACH_ANCHOR)
                    (gainers if won and w > _REST else losers).append(
                        (f"{fnode}→{APPROACH_ANCHOR}", round(w - _REST, 4))
                    )
                    decisions.log(
                        "approach_stance_credit",
                        turn_id=trace.turn_id,
                        stance=sid,
                        axis=axis,
                        verb=verb,
                        won=won,
                        weight=round(w, 4),
                    )
        return updated

    @staticmethod
    def _drafter_node(entry: dict) -> str | None:
        """`draft_<idx>_<turn_id>` → `frontal.drafter_<LETTER>`. Non-drafter producers
        that share the draft_scores list ("switch_draft", "subsystem_<name>_<turn>")
        fail the int() and return None — existing behavior, deliberately preserved."""
        parts = str(entry.get("draft_id", "")).split("_")
        if len(parts) < 2:
            return None
        try:
            idx = int(parts[1])
        except ValueError:
            return None
        return f"frontal.drafter_{chr(65 + idx)}"

    def _apply_drafter_competition(
        self, trace, outcome: float, plasticity: float, gainers: list, losers: list
    ) -> int:
        """Competition on PHRASING. Signature unchanged (tests call it directly);
        the math lives in _apply_competition."""
        return self._apply_competition(
            trace,
            trace.draft_scores,
            source="frontal.executive",
            resolve_node=self._drafter_node,
            bonus_scale=settings.get("hebbian_outcome_delta") * plasticity,
            event="drafter_competition_applied",
            role="drafter",
            gainers=gainers,
            losers=losers,
        )

    def _apply_attachment_credit(self, trace, outcome: float) -> int:
        """Tier 1 structural plasticity: learn WHICH curated fragment attaches to WHICH host.

        Contrastive over the drafter competition — the fragment(s) the SELECTED drafter
        carried on a positive-outcome turn are reinforced, creating/strengthening a
        per-persona `fragment.<skill_id> → drafter` edge; fragments carried by LOSING
        drafters are gently demoted (existing edges only, so a fresh losing exploration
        simply never establishes). Non-drafter hosts (critic/reframer/empathy/executive)
        get co-activation credit for an established attachment they carried on a good turn —
        MAINTENANCE only. This pass still creates nothing on a non-drafter, deliberately: a
        judge's first attachment comes from brain/judge_attachment.py, which earns it on
        cross-turn PAIRED accuracy and gates it behind judge-specific runtime clamps. A whole
        turn's outcome is far too coarse a signal to hand a judge new content on.

        The step is `outcome * fragment_gain` — deliberately NOT the tiny per-path Hebbian
        delta, so ONE good win lifts a new attachment (starting at rest) clear of the prune
        floor; sustained wins then climb it toward the downshift threshold, while an
        attachment that stops winning fades (decay_fragment_edges) and is pruned. Reads
        trace.drafter_fragments (host → [skill_id], stamped per-drafter by frontal). Gated by
        fragment_wiring; the top-level run() gates the whole pass on BRAIN_WIRING_FROZEN.
        Returns edge updates."""
        if not settings.get("fragment_wiring", 1):
            return 0
        frags = getattr(trace, "drafter_fragments", None) or {}
        if not frags:
            return 0
        from brain.fragment_pool import EXPLORE_HOSTS, fragment_node_name, is_admissible

        selected = next((d for d in (trace.draft_scores or []) if d.get("selected")), None)
        winner_host = None
        if selected is not None:
            parts = str(selected.get("draft_id", "")).split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                winner_host = f"frontal.drafter_{chr(65 + int(parts[1]))}"

        att_delta = outcome * float(settings.get("fragment_gain", 0.2))
        penalty = float(settings.get("fragment_explore_penalty", 0.02))
        updated = 0
        for host, sids in frags.items():
            is_drafter = host in EXPLORE_HOSTS
            for sid in sids or []:
                if not is_admissible(sid, host):
                    continue
                fnode = fragment_node_name(sid)
                has = self._wiring.has(fnode, host)
                if is_drafter and host == winner_host:
                    if att_delta > 0:
                        # create/strengthen the winning attachment
                        self._wiring.add(fnode, host, weight=WEIGHT_REST)
                        n = self._wiring.hebbian_update([fnode, host], att_delta)
                        verb = "reinforce"
                    elif has:
                        # selected but the turn went badly — weaken an existing attachment
                        n = self._wiring.hebbian_update([fnode, host], att_delta)
                        verb = "demote"
                    else:
                        continue
                elif is_drafter:
                    # carried by a losing drafter — gentle demotion of an EXISTING attachment
                    if not has:
                        continue
                    n = self._wiring.hebbian_update([fnode, host], -penalty)
                    verb = "loser_penalty"
                else:
                    # non-drafter host — co-activation credit for an EXISTING attachment
                    if att_delta <= 0 or not has:
                        continue
                    n = self._wiring.hebbian_update([fnode, host], att_delta)
                    verb = "coactivation"
                if n:
                    updated += n
                    decisions.log(
                        "attachment_learned",
                        turn_id=trace.turn_id,
                        fragment=sid,
                        host=host,
                        verb=verb,
                        won=(host == winner_host),
                        weight=round(self._wiring.get_edge_weight(fnode, host), 4),
                        outcome=round(outcome, 4),
                    )
        return updated

    # ── Tier 2 structural plasticity: recruit / demote reserve drafter nodes ──────

    def _maybe_recruit_nodes(self, session_id: str) -> None:
        """Recruit a dormant reserve drafter into the bound persona's active set when a fixed
        host has a STABLE cluster of proven fragment attachments, and demote a recruited reserve
        whose executive→ edge has decayed below the floor. The recruited node's identity = the
        copied proven fragments (injected via the Tier-1 seam); it earns or loses its place
        through the drafter competition. Runs per-persona (inside _run_for_persona, bound),
        gated by node_recruitment + BRAIN_WIRING_FROZEN. At most one recruitment per pass.

        ALTERNATIVE trigger (node_recruit_from_ignition): sustained Global-Workspace ignition
        (a decayed per-persona tally, brain/ignition_tally.py) opens a relaxed path — an
        ESTABLISHED cluster at ≥ the inject/promote midpoint may crystallize before it is
        fully proven. Full-proven clusters always win first, and every existing gate (flags,
        FROZEN, pool, admissibility, dedup, demotion) applies unchanged."""
        if not settings.get("node_recruitment", 1):
            return
        if os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true":
            return
        from brain.fragment_pool import is_admissible

        K = max(0, int(settings.get("node_reserve_pool", 3)))
        if K <= 0:
            return
        promote = float(settings.get("node_promote_threshold", 2.2))
        min_cluster = max(1, int(settings.get("node_promote_min_cluster", 2)))
        inject_threshold = float(settings.get("fragment_inject_threshold", 1.3))
        fixed = [f"frontal.drafter_{chr(65 + i)}" for i in range(5)]
        reserves = [f"frontal.drafter_{chr(65 + 5 + j)}" for j in range(K)]

        # DEMOTION: a recruited reserve that has LOST its specialization — no fragment
        # attachment left above the inject threshold — is returned to the pool. Its copied
        # fragments fade via fragment_forget if it stops winning (they are reinforced only
        # when it wins), so this tracks reward: an empty specialist is retired.
        for r in reserves:
            if not self._wiring.has("frontal.executive", r):
                continue
            best_frag = max((w for (_sid, w) in self._wiring.attached_fragments(r)), default=0.0)
            if best_frag < inject_threshold:
                removed = self._wiring.remove_node_edges(r)
                decisions.log(
                    "node_demoted",
                    session_id=session_id,
                    node=r,
                    reason="no_specialization",
                    edges_removed=removed,
                )

        free = [r for r in reserves if not self._wiring.has("frontal.executive", r)]
        if not free:
            return
        recruited = [r for r in reserves if self._wiring.has("frontal.executive", r)]

        def _already_covered(pids: set) -> bool:
            for r in recruited:
                r_ids = {sid for sid, _ in self._wiring.attached_fragments(r)}
                if pids <= r_ids:
                    return True
            return False

        # RECRUITMENT: crystallize the first fixed host with a stable proven cluster not already
        # covered by an existing recruited reserve. One recruitment per pass.
        for host in fixed:
            proven = [
                (sid, w)
                for (sid, w) in self._wiring.attached_fragments(host)
                if w >= promote and is_admissible(sid, host)
            ]
            if len(proven) < min_cluster:
                continue
            pids = {sid for sid, _ in proven}
            if _already_covered(pids):
                continue
            self._recruit_reserve(free[0], proven)
            decisions.log(
                "node_recruited",
                session_id=session_id,
                node=free[0],
                source=host,
                fragments=sorted(pids),
                trigger="proven_cluster",
            )
            return

        # ── ALTERNATIVE trigger: sustained Global-Workspace ignition ─────────────
        # Repeated ignition is a content-free "the mind is under sustained load" signal:
        # it lowers the proof bar to the inject/promote midpoint (established, not yet
        # proven — and safely above the demotion floor, so an ignition recruit cannot be
        # insta-demoted next pass). consume() makes one accumulation window pay for at
        # most one recruitment.
        if not settings.get("node_recruit_from_ignition", 1):
            return
        try:
            from brain import ignition_tally

            score, coalition = ignition_tally.pressure()
        except Exception:
            return
        if score < float(settings.get("ignition_recruit_min_score", 3.0)):
            return
        relaxed = max(inject_threshold, (inject_threshold + promote) / 2.0)
        for host in fixed:
            cluster = [
                (sid, w)
                for (sid, w) in self._wiring.attached_fragments(host)
                if w >= relaxed and is_admissible(sid, host)
            ]
            if len(cluster) < min_cluster:
                continue
            pids = {sid for sid, _ in cluster}
            if _already_covered(pids):
                continue
            self._recruit_reserve(free[0], cluster)
            decisions.log(
                "node_recruited",
                session_id=session_id,
                node=free[0],
                source=host,
                fragments=sorted(pids),
                trigger="workspace_ignition",
                coalition=coalition,
                ignition_score=round(score, 3),
                bar=round(relaxed, 3),
            )
            with contextlib.suppress(Exception):
                ignition_tally.consume()
            return

    def _recruit_reserve(self, reserve: str, proven: list) -> None:
        """Wire a reserve drafter into the bound persona's active graph (mirrors the drafter
        edges in wiring_bootstrap) and copy the proven fragments onto it — its baked specialist
        identity, injected each turn via the existing Tier-1 consumer."""
        from brain.fragment_pool import fragment_node_name

        self._wiring.add("frontal.executive", reserve, weight=WEIGHT_REST)
        self._wiring.add(reserve, "frontal.critic", weight=WEIGHT_REST)
        self._wiring.add(reserve, "frontal.empathy_critic", weight=WEIGHT_REST)
        self._wiring.add(reserve, "frontal.commitment_extractor", weight=WEIGHT_REST)
        self._wiring.add(
            "hypothalamus.threat_to_GABA", reserve, weight=WEIGHT_REST, polarity="inhibitory"
        )
        for sid, w in proven:
            self._wiring.add(fragment_node_name(sid), reserve, weight=w)

    # Switch-ordering edges (sensory.text → temporal.<switch>) are never consecutive
    # pairs on fired_path (sensory.text is a bus channel, not a fired node), so the
    # main path credit can't reach them. Credit them explicitly here for the gated
    # switches that fired, mirroring drafter competition. Half-scaled vs the path
    # credit on the second hop (temporal.<switch>→understanding_integrator) so the
    # two hops of the same route don't compound into a runaway.
    _CREDITED_SWITCHES = {"template_match", "self_reference", "epistemic_action"}

    def _apply_switch_routing_credit(
        self,
        trace,
        outcome: float,
        plasticity: float,
        turn_plast: float,
        gainers: list,
        losers: list,
    ) -> int:
        if not settings.get("switch_routing_credit", 1):
            return 0
        scale = (
            settings.get("hebbian_outcome_delta")
            * plasticity
            * turn_plast
            * settings.get("switch_routing_credit_scale", 0.5)
        )
        delta = outcome * scale
        if abs(delta) < 1e-6:
            return 0
        updated = 0
        seen: set[str] = set()
        for entry in trace.fired_path or []:
            if entry.get("cluster") != "temporal" or entry.get("kind") != "switch":
                continue
            name = entry.get("name", "")
            local = name.split(".")[-1]
            if local not in self._CREDITED_SWITCHES or name in seen:
                continue
            seen.add(name)
            edge = ("sensory.text", name)
            if not self._wiring.has(*edge):
                continue
            prev = self._wiring.get_edge_weight(*edge)
            self._wiring.hebbian_update([edge[0], edge[1]], delta)
            now = self._wiring.get_edge_weight(*edge)
            edge_delta = now - prev
            if abs(edge_delta) > 0.001:
                (gainers if edge_delta > 0 else losers).append((f"{edge[0]}→{edge[1]}", edge_delta))
                decisions.log(
                    "switch_routing_credit_applied",
                    turn_id=trace.turn_id,
                    switch=local,
                    from_weight=round(prev, 4),
                    to_weight=round(now, 4),
                    delta=round(edge_delta, 4),
                    outcome=round(outcome, 3),
                )
                updated += 1
        return updated

    # Recall fan-out edges (mem.recall → hippocampus.<strategy>) are never consecutive
    # pairs on fired_path either — recall strategies are budget allocations, not fired
    # nodes — so the main path credit can't reach them. The four strategies group into
    # two pathways whose weight ratio sets the schema-vs-episode budget split
    # (hippocampus._allocate_recall_budget). Credit each side by its contribution share
    # so the split learns toward whichever pathway surfaced memories on good-outcome
    # turns. Half-scaled like switch credit; signed by outcome (a bad turn that leaned
    # on one side nudges it down). Attribution is at side granularity by hit-count — a
    # volume proxy for usefulness, the faithful grain since weights only gate the split.
    _RECALL_SIDES = {
        "schema": ("hippocampus.schema_grep", "hippocampus.entity_tracker"),
        "episode": ("hippocampus.cosine_recall", "hippocampus.time_filter"),
        # Cross-domain transfer pathway — credited by its own hit share so the
        # brain learns whether analogical (problem-shape) recall actually helped.
        "structural": ("hippocampus.structural_recall",),
    }

    def _apply_recall_credit(
        self,
        trace,
        outcome: float,
        plasticity: float,
        turn_plast: float,
        gainers: list,
        losers: list,
    ) -> int:
        if not settings.get("recall_routing_credit", 1):
            return 0
        contrib = getattr(trace, "recall_contrib", None) or {}
        n_schema = float(contrib.get("schema", 0) or 0)
        n_episode = float(contrib.get("episode", 0) or 0)
        n_structural = float(contrib.get("structural", 0) or 0)
        total = n_schema + n_episode + n_structural
        if total <= 0:
            return 0
        scale = (
            settings.get("hebbian_outcome_delta")
            * plasticity
            * turn_plast
            * settings.get("recall_routing_credit_scale", 0.5)
        )
        base = outcome * scale
        if abs(base) < 1e-6:
            return 0
        shares = {
            "schema": n_schema / total,
            "episode": n_episode / total,
            "structural": n_structural / total,
        }
        updated = 0
        for side, strategies in self._RECALL_SIDES.items():
            delta = base * shares[side]
            if abs(delta) < 1e-6:
                continue
            for strat in strategies:
                edge = ("mem.recall", strat)
                if not self._wiring.has(*edge):
                    continue
                prev = self._wiring.get_edge_weight(*edge)
                self._wiring.hebbian_update([edge[0], edge[1]], delta)
                now = self._wiring.get_edge_weight(*edge)
                edge_delta = now - prev
                if abs(edge_delta) > 0.001:
                    (gainers if edge_delta > 0 else losers).append(
                        (f"{edge[0]}→{edge[1]}", edge_delta)
                    )
                    decisions.log(
                        "recall_routing_credit_applied",
                        turn_id=trace.turn_id,
                        strategy=strat.split(".")[-1],
                        side=side,
                        from_weight=round(prev, 4),
                        to_weight=round(now, 4),
                        delta=round(edge_delta, 4),
                        outcome=round(outcome, 3),
                    )
                    updated += 1
        return updated

    # Edges already owned by the two hand-written per-family helpers. Phase 1 keeps
    # those helpers authoritative and has the co-activation pass step around them, so
    # this change adds coverage WITHOUT touching any existing reward math. Once the
    # ledger shows co-activation reproducing their deltas, they can be deleted — but
    # in a separate change: folding that in would turn a coverage PR into a reward PR,
    # and per the note on _apply_competition, drift in a REWARD rule is silent.
    def _helper_owned_edges(self) -> set[tuple[str, str]]:
        owned = {("sensory.text", f"temporal.{s}") for s in self._CREDITED_SWITCHES}
        for strategies in self._RECALL_SIDES.values():
            owned |= {("mem.recall", s) for s in strategies}
        return owned

    def _active_levels(self, trace) -> dict[str, float]:
        """Every node that participated this turn → its level in [0,1].

        Merges the nodes that actually FIRED (fired_path, where a switch carries its
        own fire level and an integrator counts as full participation) with the ones
        that only participated (trace.coactive), keeping the max where both apply."""
        levels: dict[str, float] = {}
        for entry in trace.fired_path or []:
            name = entry.get("name")
            if not name:
                continue
            lvl = 1.0 if entry.get("kind") == "integrator" else float(entry.get("level", 1.0) or 0.0)
            lvl = max(0.0, min(1.0, lvl))
            if lvl > levels.get(name, -1.0):
                levels[name] = lvl
        for name, lvl in (getattr(trace, "coactive", None) or {}).items():
            lvl = max(0.0, min(1.0, float(lvl)))
            if lvl > levels.get(name, -1.0):
                levels[name] = lvl
        return levels

    def _apply_coactivation_credit(
        self,
        trace,
        outcome: float,
        plasticity: float,
        turn_plast: float,
        already_credited: set[tuple[str, str]],
        gainers: list,
        losers: list,
    ) -> int:
        """Credit any edge whose BOTH endpoints participated this turn.

        Path credit only ever reaches CONSECUTIVE pairs of fired_path, and only
        SwitchNeuron.fire()/IntegratorCell.call() reach that list — so 35 of the 72
        wired edges could never be credited by it at all, because an endpoint is a bus
        channel, a chemistry mapper, a state holder or a bookkeeping node. This closes
        that gap generically instead of by adding a per-family helper each time.

        Scaled by min(level_src, level_tgt) — the Hebbian pre×post product. That factor
        is load-bearing, not decoration: a blanket "both endpoints active" delta would
        land identically on ~50 edges every turn, and since every consumer reads
        RELATIVE weight (evaluation order, sibling gap, budget ratio), a common-mode
        delta carries exactly zero information. Grading is what makes the credit
        differentiate."""
        if not settings.get("coactivation_credit", 1):
            return 0
        levels = self._active_levels(trace)
        if len(levels) < 2:
            return 0
        base = (
            outcome
            * settings.get("hebbian_outcome_delta")
            * plasticity
            * turn_plast
            * float(settings.get("coactivation_credit_scale", 0.25))
        )
        if abs(base) < 1e-9:
            return 0
        skip = already_credited | self._helper_owned_edges()
        if settings.get("credit_purity", 1):
            skip = skip | _competition_owned(int(settings.get("node_reserve_pool", 3)))
        allow_inhib = bool(settings.get("coactivation_credit_inhibitory", 1))

        updated = 0
        for src, tgt in self._wiring.edges_among(set(levels)):
            if (src, tgt) in skip or src.startswith("fragment."):
                continue  # fragments have their own gain/forget economy
            if not allow_inhib and self._wiring.polarity(src, tgt) == "inhibitory":
                continue
            delta = base * min(levels[src], levels[tgt])
            if abs(delta) < 1e-9:
                continue
            prev = self._wiring.get_edge_weight(src, tgt)
            if not self._wiring.hebbian_update_pairs([(src, tgt)], delta):
                continue
            updated += 1
            now = self._wiring.get_edge_weight(src, tgt)
            edge_delta = now - prev
            if abs(edge_delta) > 0.001:
                (gainers if edge_delta > 0 else losers).append((f"{src}→{tgt}", edge_delta))
                decisions.log(
                    "coactivation_credit_applied",
                    turn_id=trace.turn_id,
                    src=src,
                    tgt=tgt,
                    from_weight=round(prev, 4),
                    to_weight=round(now, 4),
                    delta=round(edge_delta, 4),
                    level=round(min(levels[src], levels[tgt]), 3),
                    outcome=round(outcome, 3),
                )
        return updated

    def _apply_eligibility_credit(
        self,
        trace,
        source_turn_id: str,
        past_path: list[str],
        age: int,
        decay: float,
        elig_delta: float,
        outcome: float,
        gainers: list,
        losers: list,
    ) -> int:
        """Apply — and LOG — one earlier turn's age-decayed share of this turn's outcome.

        Logged under its OWN decision kind rather than `hebbian_update_applied`,
        because the two answer different questions: "this edge moved because of
        THIS turn" vs "…because of a payoff `age` turns later". The split is also
        what keeps the aggregates honest — every existing consumer filters on the
        main kind, so eligibility is excluded from them for free and nothing
        double-counts. Readers that SHOULD see it opt in explicitly.

        Volume: eligibility fires `eligibility_lookback` times per turn over paths
        the main pass already logs edge-by-edge, so per-edge records here would
        multiply ledger volume by ~(1 + lookback) and shrink the 5000-line
        rotation window for the sparse, high-value records that share the file
        (session_plasticity_summary, learning_story). So: ONE record per
        (crediting turn, age), carrying every edge it moved and that edge's delta.
        Same information, one line instead of N.

        Returns the wiring's own updated-edge count, so the caller's
        `edges_updated` total stays reconcilable against what was logged.
        """
        pairs = self._credit_pairs(past_path)
        before = {
            (s, t): self._wiring.get_edge_weight(s, t) for (s, t) in pairs if self._wiring.has(s, t)
        }
        updated = self._wiring.hebbian_update_pairs(pairs, elig_delta)
        if not updated:
            return 0

        moved: list[dict] = []
        for (src, tgt), prev in before.items():
            now = self._wiring.get_edge_weight(src, tgt)
            edge_delta = now - prev
            if abs(edge_delta) <= 0.001:
                continue
            (gainers if edge_delta > 0 else losers).append((f"{src}→{tgt}", edge_delta))
            moved.append(
                {
                    "src": src,
                    "tgt": tgt,
                    "from_weight": round(prev, 4),
                    "to_weight": round(now, 4),
                    "delta": round(edge_delta, 4),
                }
            )
        # Emitted even when `moved` is empty (every edge landed under the 0.001
        # reporting floor): `edges_updated` is what the session summary counts,
        # so the record has to exist for the two to reconcile.
        decisions.log(
            "hebbian_eligibility_applied",
            turn_id=trace.turn_id,  # the turn whose outcome paid out
            source_turn_id=source_turn_id,  # the earlier turn whose path earned it
            age=age,
            decay=round(decay, 4),
            delta=round(elig_delta, 5),
            outcome=round(outcome, 3),
            edges_updated=updated,
            edges=moved,
        )
        return updated

    def run(self, session_id: str, full_traces: list) -> None:
        """Apply the Hebbian pass per originating persona.

        The trace buffer is process-wide and one process serves many personas
        (agent lanes bind per turn), while consolidation is triggered under a
        SINGLE binding (the /consolidate route's session persona, or none at all
        from the idle loop). Applying the whole buffer under that one binding
        credits every persona's turns to whoever consolidated first. So: group
        traces by their persona stamp and bind each group's persona around its
        updates — attribution no longer depends on who pulled the trigger.
        Unstamped traces (older builds, no-persona deployments) run under the
        ambient binding, exactly the old behavior.

        BRAIN_WIRING_FROZEN is a TRUE panic switch: it halts the ENTIRE pass here
        — decay, weight learning, drafter/switch/recall credit, attachment credit,
        node recruitment, prune, and save — so wiring.json is left byte-identical
        and the brain falls back to its fixed map, unchanged (SYSTEMS.md §2.9)."""
        import contextlib

        if os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true":
            decisions.log("hebbian_pass_frozen", session_id=session_id, traces=len(full_traces))
            return

        from brain.persona_key import persona_slug
        from brain.second_brain.store import bind_persona

        groups: dict[str, list] = {}
        for trace in full_traces:
            groups.setdefault(persona_slug(getattr(trace, "persona_name", "") or ""), []).append(
                trace
            )
        for key, traces in groups.items():
            with bind_persona(key) if key else contextlib.nullcontext():
                self._run_for_persona(session_id, traces)

    def _run_for_persona(self, session_id: str, full_traces: list) -> None:
        """Decay + per-turn Hebbian updates along firing paths, for ONE persona's
        traces (the wiring resolves the bound persona's graph on every access)."""
        # Decay is expressed PER TURN and compounded over the turns in this batch.
        # Reinforcement accrues per turn, so a per-SESSION rate made the equilibrium
        # w_eq = 1 + n_turns·gain/rate depend on how long the session happened to be:
        # 1.15 for a 1-turn session against 3.92 (clamped at weight_max) for a 20-turn
        # one — a 26x spread on identical settings. Compounding restores the invariant.
        # This also connects the dial for the first time: the rate was hardcoded here
        # and `decay_toward_rest_rate` was never read on the production path.
        n_turns = max(1, len(full_traces))
        _r_turn = float(settings.get("decay_toward_rest_rate_per_turn", 0.01))
        eff = self._batch_decay(_r_turn, n_turns)
        self._wiring.decay_toward_rest(rest=1.0, rate=eff)
        # Fragment attachments decay first, so the trace loop's reinforcement can lift the
        # productive ones back above the prune floor while unused ones fade toward removal.
        # Their rate is tuned against their OWN thresholds (prune 1.05 / inject 1.30 /
        # promote 2.20) rather than against the topology rate: gain is earned only on turns
        # the host wins, so a rate that looks slow beside topology decay is what keeps a
        # proven attachment clear of the promote threshold. See the settings comment.
        _frag_on = bool(settings.get("fragment_wiring", 1))
        if _frag_on:
            self._wiring.decay_fragment_edges(
                self._batch_decay(float(settings.get("fragment_forget_per_turn", 0.01)), n_turns)
            )

        plasticity = self._plasticity_modulator(full_traces)
        gainers: list[tuple[str, float]] = []
        losers: list[tuple[str, float]] = []
        turn_plasticities: list[float] = []
        total_updated = 0
        elig_updated = 0
        skipped = 0

        # Eligibility trace: conversational payoff is often delayed — the turn
        # where DA finally moves is rarely the only turn that earned it. Recent
        # turns' fired paths stay "eligible" and receive an age-decayed share of
        # the current outcome. Lookback 0 disables (pre-trace behavior).
        import math as _math

        _elig_lookback = int(settings.get("eligibility_lookback", 2))
        _elig_tau = max(0.1, float(settings.get("eligibility_tau_turns", 2.0)))
        _recent_paths: list[tuple[str, list[str]]] = []  # (turn_id, path), most recent last

        for trace in full_traces:
            if not trace.fired_path:
                skipped += 1
                continue

            outcome, breakdown = self._composite_outcome(trace)
            skip, reason = self._should_skip_hebbian(trace, outcome)
            if skip:
                decisions.log(
                    "hebbian_update_skipped",
                    turn_id=trace.turn_id,
                    reason=reason,
                    outcome=round(outcome, 3),
                )
                skipped += 1
                continue

            turn_plast = self._turn_plasticity(trace)
            turn_plasticities.append(turn_plast)
            delta = outcome * settings.get("hebbian_outcome_delta") * plasticity * turn_plast
            path_names = [n["name"] for n in trace.fired_path]
            path_pairs = self._credit_pairs(path_names)
            before = {
                (s, t): self._wiring.get_edge_weight(s, t)
                for (s, t) in path_pairs
                if self._wiring.has(s, t)
            }
            updated = self._wiring.hebbian_update_pairs(path_pairs, delta)
            total_updated += updated
            for (src, tgt), prev in before.items():
                now = self._wiring.get_edge_weight(src, tgt)
                edge_delta = now - prev
                if abs(edge_delta) > 0.001:
                    if edge_delta > 0:
                        gainers.append((f"{src}→{tgt}", edge_delta))
                    else:
                        losers.append((f"{src}→{tgt}", edge_delta))
                    decisions.log(
                        "hebbian_update_applied",
                        turn_id=trace.turn_id,
                        src=src,
                        tgt=tgt,
                        from_weight=round(prev, 4),
                        to_weight=round(now, 4),
                        delta=round(edge_delta, 4),
                        outcome=round(outcome, 3),
                        turn_plasticity=round(turn_plast, 3),
                        breakdown=breakdown,
                    )

            # Delayed credit: prior turns' paths get an age-decayed share of this
            # outcome (the turn that set up the payoff learns too, not only the
            # turn where DA finally moved). Decay e^(-age/τ) keeps it local.
            if _elig_lookback > 0:
                for age, (past_turn, past_path) in enumerate(
                    reversed(_recent_paths[-_elig_lookback:]), 1
                ):
                    if not past_path or past_path == path_names:
                        continue
                    decay = _math.exp(-age / _elig_tau)
                    elig_delta = delta * decay
                    if abs(elig_delta) <= 1e-5:
                        continue
                    n = self._apply_eligibility_credit(
                        trace,
                        past_turn,
                        past_path,
                        age,
                        decay,
                        elig_delta,
                        outcome,
                        gainers,
                        losers,
                    )
                    total_updated += n
                    elig_updated += n
            _recent_paths.append((trace.turn_id, path_names))

            total_updated += self._apply_drafter_competition(
                trace, outcome, plasticity, gainers, losers
            )
            total_updated += self._apply_approach_stance_credit(trace, plasticity, gainers, losers)
            total_updated += self._apply_switch_routing_credit(
                trace, outcome, plasticity, turn_plast, gainers, losers
            )
            total_updated += self._apply_recall_credit(
                trace, outcome, plasticity, turn_plast, gainers, losers
            )
            # Runs LAST and is told which pairs the main path pass already moved, so a
            # co-active edge that also happened to be path-adjacent is credited once.
            total_updated += self._apply_coactivation_credit(
                trace, outcome, plasticity, turn_plast, set(path_pairs), gainers, losers
            )
            total_updated += self._apply_attachment_credit(trace, outcome)

        # Prune faded fragment attachments after all reinforcement (topology never pruned).
        if _frag_on:
            pruned = self._wiring.prune_fragment_edges(
                float(settings.get("fragment_prune_floor", 1.05))
            )
            if pruned:
                decisions.log("attachment_pruned", session_id=session_id, count=pruned)

        # Tier 2: recruit/demote reserve drafter nodes for this persona (gated, best-effort).
        try:
            self._maybe_recruit_nodes(session_id)
        except Exception as e:
            logger.warning("[Tier2] node recruitment skipped (non-fatal): %s", e)

        try:
            self._wiring.save()
        except Exception as e:
            logger.warning("[Memory consolidation] Wiring save failed: %s", e)
        try:
            self._wiring.snapshot_to_history(session_id)
        except Exception as e:
            logger.debug("[Memory consolidation] Wiring snapshot failed: %s", e)

        turns_with_critic = sum(
            1
            for t in full_traces
            if any(d.get("critic_ran") and d.get("selected") for d in (t.draft_scores or []))
        )
        turns_with_user_emotion = sum(1 for t in full_traces if getattr(t, "user_emotion", ""))
        turns_with_da_delta = sum(
            1
            for t in full_traces
            if abs(
                float(
                    (getattr(t, "prior_neuromod", None) or {}).get(
                        "DA", float((t.neuromod or {}).get("DA", 0.5))
                    )
                )
                - float((t.neuromod or {}).get("DA", 0.5))
            )
            > 0.01
        )
        top_gainers = sorted(gainers, key=lambda x: x[1], reverse=True)[:5]
        top_losers = sorted(losers, key=lambda x: x[1])[:5]
        decisions.log(
            "session_plasticity_summary",
            session_id=session_id,
            plasticity_modulator=round(plasticity, 3),
            graded_plasticity=int(settings.get("graded_plasticity", 0)),
            # Decay actually applied this batch. Recorded because the per-turn rate is
            # compounded over n_turns at the call site, so neither number alone explains
            # an observed weight change — the calibration harness needs both to replay.
            decay_turns=n_turns,
            decay_effective=round(eff, 5),
            avg_turn_plasticity=(
                round(sum(turn_plasticities) / len(turn_plasticities), 3)
                if turn_plasticities
                else None
            ),
            edges_updated=total_updated,
            # The eligibility share of edges_updated, broken out so the headline
            # number decomposes: it equals the sum of `edges_updated` across this
            # session's hebbian_eligibility_applied records, exactly.
            eligibility_edges_updated=elig_updated,
            turns_skipped=skipped,
            signal_quality={
                "turns_with_critic_score": turns_with_critic,
                "turns_with_user_emotion": turns_with_user_emotion,
                "turns_with_da_delta": turns_with_da_delta,
                "total_turns": len(full_traces),
            },
            top_gainers=[{"edge": e, "delta": round(d, 4)} for e, d in top_gainers],
            top_losers=[{"edge": e, "delta": round(d, 4)} for e, d in top_losers],
        )
        logger.info(
            "[Memory consolidation] Hebbian: plasticity=%.2f edges_updated=%d "
            "turns_skipped=%d critic_turns=%d/%d",
            plasticity,
            total_updated,
            skipped,
            turns_with_critic,
            len(full_traces),
        )
