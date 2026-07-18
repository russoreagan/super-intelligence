"""
Frontal Lobe — executive + Multiple Drafts engine.
Executive coordinator + drafter(s) + critic(s) + inhibitory switches.
The only cluster with multiple LLM cells.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import random as _random
import time
import uuid

from brain.brainstem import Brainstem
from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.clusters.frontal_prompts import (
    CRITIC_SYSTEM,
    DRAFTER_SYSTEMS,
    EMPATHY_CRITIC_SYSTEM,
    EXECUTIVE_SYSTEM,
    REFRAMER_SYSTEM,
    RESERVE_DRAFTER_SYSTEM,
)
from brain.clusters.frontal_subsystem import FrontalSubsystem
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.node_registry import get_node_registry
from brain.observability.decisions import decisions
from brain.predictor import (
    CompositePredictor,
    composite_signature,
    prediction_match_frac,
    should_bypass_gating,
)
from brain.security import fence
from brain.settings import settings
from brain.utils import safe_json_parse
from brain.wiring import WEIGHT_REST, Wiring

logger = logging.getLogger(__name__)

CLUSTER = "frontal"


def _mark_trace_flag(attr: str, value) -> None:
    """Set an instrumentation field on the current turn's trace, if one is bound.
    No-op outside a turn context (e.g. unit tests that call _build_drafter_prompt
    directly). Keeps instrumentation out of the hot return-value path."""
    try:
        from brain.observability.firing_path import get_current_trace

        trace = get_current_trace()
        if trace is not None:
            setattr(trace, attr, value)
    except Exception:
        pass


class FrontalCluster:
    def __init__(
        self, bus: Bus, brainstem: Brainstem, router: ModelRouter, wiring: Wiring | None = None
    ) -> None:
        self._bus = bus
        self._brainstem = brainstem
        self._router = router
        self._wiring = wiring
        # Frontal subsystems — checked in order after the executive classifies a turn.
        # First subsystem whose can_handle() returns True wins; conversational path
        # (drafter/critic) fires as the implicit fallback if none match.
        # Register new subsystems here.
        self._subsystems: list[FrontalSubsystem] = []
        self._wiring_frozen = os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true"
        # Judge-host attachment learning (brain/judge_attachment.py): the producer that
        # lets frontal.critic / frontal.empathy_critic ACQUIRE a first attachment, plus
        # the runtime gates that make learned content on a JUDGE safe. Held here because
        # frontal owns both judge cells and the wiring handle.
        from brain.judge_attachment import JudgeAttachmentTracker

        self._judge_attach = JudgeAttachmentTracker()
        # Attachment-independent inputs to the judge veto floor, stamped once per turn
        # from the parsed features. Deliberately NOT derived from any model output, so
        # injected skill content cannot reach the floor (see judge_attachment gate 2).
        self._turn_user_emotion: str = ""
        self._turn_hostility: float = 0.0
        # What the entity can actually do — surfaced into drafter prompts so
        # the drafters don't confabulate when asked "what tools do you have?"
        # Set by run.py after motor cortex / cloud executor boot.
        self._capabilities_summary: str = ""

        # Predict-and-surprise
        self._exec_predictor = CompositePredictor(
            name="frontal_executive_predictor",
            cluster=CLUSTER,
            confidence_skip_threshold=0.7,
        )
        self._critic_predictor = CompositePredictor(
            name="frontal_critic_predictor",
            cluster=CLUSTER,
            confidence_skip_threshold=0.75,
        )

        self._executive = IntegratorCell(
            name="executive",
            cluster=CLUSTER,
            model="sonnet",
            system_prompt=EXECUTIVE_SYSTEM,
            topics=["temporal.features"],
            max_calls_per_turn=1,
            locality="cloud",
            max_tokens=512,
        )
        self._executive.set_router(router)
        # Node registry: register object-backed graph nodes at their construction site so the
        # boot audit can prove every wiring name maps to a live object (see brain/node_registry).
        get_node_registry().register_object(self._executive, kind="cell")

        # Fixed drafters A–E plus K dormant RESERVE slots (Tier 2 structural plasticity).
        # Reserve slots exist as cells but are wired into a persona's graph only when learning
        # RECRUITS them (an executive→drafter_X edge). Their system prompt falls back to the
        # persona-neutral RESERVE_DRAFTER_SYSTEM (DRAFTER_SYSTEMS only defines the fixed 5).
        self._n_fixed_drafters = 5
        _n_reserve = max(0, int(settings.get("node_reserve_pool", 3)))
        self._drafters = [
            IntegratorCell(
                name=f"drafter_{chr(65 + i)}",
                cluster=CLUSTER,
                model="haiku",
                system_prompt=(
                    DRAFTER_SYSTEMS[i] if i < self._n_fixed_drafters else RESERVE_DRAFTER_SYSTEM
                ),
                topics=["motor.draft"],
                max_calls_per_turn=1,
                locality="cloud",
                max_tokens=768,
            )
            for i in range(self._n_fixed_drafters + _n_reserve)
        ]
        for i, d in enumerate(self._drafters):
            d.set_router(router)
            # Register only the fixed drafters at construction. A reserve slot is registered
            # atomically with its wiring edge at recruit time (register_recruited_reserves),
            # so the node-registry reconcile invariant (registry == graph) stays exact.
            if i < self._n_fixed_drafters:
                get_node_registry().register_object(d, kind="cell")

        self._critic = IntegratorCell(
            name="critic",
            cluster=CLUSTER,
            model="haiku",
            system_prompt=CRITIC_SYSTEM,
            topics=["motor.draft"],
            max_calls_per_turn=2,
            locality="cloud",
            max_tokens=512,
        )
        self._critic.set_router(router)
        get_node_registry().register_object(self._critic, kind="cell")

        # v0.2
        self._reframer = IntegratorCell(
            name="stoic_reframer",
            cluster=CLUSTER,
            model="haiku",
            system_prompt=REFRAMER_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="cloud",
            max_tokens=512,
        )
        self._reframer.set_router(router)
        get_node_registry().register_object(self._reframer, kind="cell")

        self._empathy_critic = IntegratorCell(
            name="empathy_critic",
            cluster=CLUSTER,
            model="haiku",
            system_prompt=EMPATHY_CRITIC_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="cloud",
            max_tokens=256,
        )
        self._empathy_critic.set_router(router)
        get_node_registry().register_object(self._empathy_critic, kind="cell")

        # Switches (~12 total; 3 inhibitory = 25%). All are now wired into
        # real firing sites below — see process() for the gating call sites.
        # Modulator profiles encode each switch's biological identity.
        # Excitatory routers — fire to record the route taken (telemetry +
        # Hebbian weight surface). Mostly chemistry-neutral since the route
        # itself is chosen by the executive LLM.
        self._response_type_router = SwitchNeuron("response_type_router", CLUSTER)
        self._length_budget = SwitchNeuron("length_budget", CLUSTER)
        self._tone_selector = SwitchNeuron("tone_selector", CLUSTER)
        self._drafter_count_selector = SwitchNeuron("drafter_count", CLUSTER)
        self._planner_trigger = SwitchNeuron("planner_trigger", CLUSTER)
        self._template_fallback = SwitchNeuron("template_fallback", CLUSTER)
        # Epistemic / self-reference modes — chemistry biases their engagement.
        self._epistemic_mode = SwitchNeuron(
            "epistemic_mode",
            CLUSTER,
            threshold=0.5,
            modulators={"ACh": -0.10},  # curiosity invites epistemic engagement
        )
        self._self_ref_mode = SwitchNeuron(
            "self_reference_mode",
            CLUSTER,
            threshold=0.5,
            modulators={"OXT": -0.10, "5HT": -0.10},  # social safety eases introspection
        )
        # Arousal modulator — fires when Glu deficit clears threshold,
        # reducing drafter count. CORT amplifies (stress → fewer drafts).
        self._arousal_modulator = SwitchNeuron(
            "arousal_modulator",
            CLUSTER,
            threshold=0.75,
            modulators={"CORT": -0.10},
        )
        # Inhibitory — these are the real defensive gates.
        # GABA_inhibitor fires when GABA clears the reframe threshold (0.40).
        # CORT lowers threshold (chronic stress = quicker defense); OXT buffers.
        self._GABA_inhibitor = SwitchNeuron(
            "GABA_inhibits_drafters",
            CLUSTER,
            polarity="inhibitory",
            threshold=0.40,
            modulators={"CORT": -0.10, "OXT": +0.10},
        )
        # Satiation inhibitor fires when the hypothalamic satiation state is
        # high enough to suppress repetition.
        self._satiation_inhibitor = SwitchNeuron(
            "satiation_inhibits_repeat",
            CLUSTER,
            polarity="inhibitory",
            threshold=0.6,
            modulators={"ACh": +0.10},  # curiosity raises the bar for "I'm bored of this"
        )
        # Fires on DA-deficit. threshold=0.70 means fires when DA < 0.30
        # (since we test against 1 - DA). CORT lowers threshold (depression
        # suppresses planning more readily); 5HT raises it (mood buffers).
        self._low_DA_inhibits_planner = SwitchNeuron(
            "low_DA_inhibits_planner",
            CLUSTER,
            polarity="inhibitory",
            threshold=0.70,
            modulators={"CORT": -0.10, "5HT": +0.10},
        )

        # Eval: populated each turn with critic scores for all drafts — read by run.py
        self.last_turn_draft_scores: list[dict] = []

        # Self-disclosure cooldown — counts down turns remaining before the
        # disclosure opportunity block can fire again.
        self._disclosure_cooldown: int = 0

        # Skill selector — picks reasoning/EI framework per turn. Wired by session_setup.
        # Held alongside parietal (which owns the cross-turn ActiveSkillContext).
        self._skill_selector = None
        self._parietal = None
        self._skill_manifest: str = ""  # compact capability listing injected into exec context
        self._current_skill_bundle = None  # per-turn cache, set in process()
        self._current_query_vec: list[float] | None = None  # cache for build_active_context

    def register_subsystem(self, subsystem: FrontalSubsystem) -> None:
        """Register a frontal subsystem. Called once at boot before any turns fire."""
        self._subsystems.append(subsystem)
        logger.info("[Frontal] Registered subsystem: %s", subsystem.name)

    def set_skill_selector(self, selector, parietal) -> None:
        """Wire the SkillSelector and ParietalCluster reference.

        Called by session_setup once both have been instantiated. The selector
        owns the embedding index; parietal owns the ActiveSkillContext across turns.
        """
        self._skill_selector = selector
        self._parietal = parietal
        self._skill_manifest = selector.capability_manifest()

    def set_capabilities(self, summary: str | None) -> None:
        """Provide a human-readable list of what the entity can actually do
        (set by run.py once motor cortex + cloud executor have introspected
        their available tools and connectors). Surfaced into drafter prompts."""
        self._capabilities_summary = (summary or "").strip()

    async def process(
        self,
        features: dict,
        affect: dict,
        memory: dict,
        parietal_context: str,
        turn_id: str,
        image_path: str | None = None,
    ) -> str:
        """Run the Multiple Drafts engine. Returns the committed response."""
        nm = self._bus.neuromod.snapshot()
        chem = self._chem_snapshot()
        self.last_turn_draft_scores = []

        # 1. Safety gate — reframe hostile input or defuse under severe stress
        features, early = await self._run_safety_gate(nm, chem, features, affect, turn_id)
        if early is not None:
            return early

        # 2. Canned response shortcut (switch-only routes)
        canned = self._check_canned_response(features, affect)
        if canned is not None:
            return canned

        # 3. Executive: predict or run the integrator to get routing instruction
        exec_sig = composite_signature(features, affect)
        instruction = await self._run_executive(
            nm, chem, exec_sig, features, affect, memory, parietal_context, turn_id
        )

        # 3a. Skill selection — picks a reasoning/EI framework for the drafters.
        # Sticky across turns via parietal.active_skill_context. Gated by turn type
        # and emotion (see SkillSelector.gate_conversational).
        await self._select_skills_for_turn(features, instruction, turn_id)
        # Session-pinned partner skills are force-included regardless of relevance.
        self._apply_pinned_skills(turn_id)

        # 4. Subsystem dispatch (task planner, etc.) — first match wins
        subsystem_response = await self._try_subsystem_dispatch(
            instruction, features, affect, memory, parietal_context, turn_id
        )
        if subsystem_response is not None:
            return subsystem_response

        # 5. Drafter cascade + critic selection
        return await self._run_drafters_and_select(
            nm,
            chem,
            exec_sig,
            instruction,
            features,
            affect,
            memory,
            parietal_context,
            turn_id,
            image_path,
        )

    async def _run_safety_gate(
        self,
        nm: dict,
        chem: dict,
        features: dict,
        affect: dict,
        turn_id: str,
    ) -> tuple[dict, str | None]:
        """GABA-inhibitor gate — reframe hostile input or defuse under severe stress.

        Returns (possibly_modified_features, early_response).
        early_response is non-None only when the defuse path fires; in that case
        process() must return the value immediately.
        """
        if self._GABA_inhibitor.should_fire(nm["GABA"], chem, turn_id):
            self._GABA_inhibitor.fire(
                nm["GABA"], "reframe_trigger", {"GABA": round(nm["GABA"], 3)}, snapshot=chem
            )
            reframe = await self._attempt_reframe(features, affect, turn_id)
            if reframe and reframe.get("succeeded"):
                features = dict(features)
                features["_reframe"] = reframe["reframe"]
                features["_reframe_approach"] = reframe["response_approach"]
                logger.debug(
                    "[Response engine] Reframed hostile input: %s", reframe["reframe"][:60]
                )
            elif nm["GABA"] > settings.get("gaba_skip_threshold_high"):
                logger.debug(
                    "[Response engine] Stress response active — using de-escalation response path"
                )
                return features, await self._defuse_response(features, affect, turn_id)
        return features, None

    async def _select_skills_for_turn(
        self,
        features: dict,
        instruction: dict,
        turn_id: str,
    ) -> None:
        """Pick the active thinking/EI skill for this turn.

        Stores result on self._current_skill_bundle for the drafter prompt to consume.
        Also updates parietal.active_skill_context for cross-turn stickiness.
        """
        self._current_skill_bundle = None
        self._current_query_vec = None
        if self._skill_selector is None or self._parietal is None:
            return

        user_input = features.get("raw_text") or features.get("topic_summary") or ""
        user_emotion = features.get("user_emotion") or features.get("emotion") or ""
        recent_turns = []
        with contextlib.suppress(Exception):
            recent_turns = [
                f"User: {t['user']}\nBrain: {t.get('response', '')}"
                for t in self._parietal.recent_turns(2)
            ]

        active = self._parietal.active_skill_context

        # If the previous turn ended awaiting user direction, lock the leaf from this reply
        if active is not None and active.awaiting_user_direction:
            active = await self._skill_selector.lock_leaf_from_reply(user_input, active)
            self._parietal.set_active_skill_context(active)
            decisions.log(
                "skill_leaf_locked",
                turn_id=turn_id,
                cluster=CLUSTER,
                category=active.category,
                leaf=active.current_leaf,
            )

        # Fast path: executive already picked a skill inline — use it directly.
        exec_skill = instruction.get("skill")
        if exec_skill and self._skill_selector.get_skill(exec_skill):
            from brain.clusters.skill_selector import SkillBundle

            bundle = SkillBundle(
                tier1=self._skill_selector.tier1_names,
                chosen=[exec_skill],
                pick_source="executive_pick",
            )
            query_vec = await self._router.embed(user_input)
            if query_vec is not None:
                self._current_query_vec = query_vec
                updated_active = self._skill_selector.build_active_context(bundle, query_vec)
                self._parietal.set_active_skill_context(updated_active)
            self._current_skill_bundle = bundle
            decisions.log(
                "skill_selector_pick",
                turn_id=turn_id,
                cluster=CLUSTER,
                pick_source="executive_pick",
                chosen=bundle.chosen,
            )
            return

        try:
            bundle, updated_active, log_extras = await self._skill_selector.select_conversational(
                user_input=user_input,
                executive_out=instruction,
                user_emotion=user_emotion,
                recent_turns=recent_turns,
                active=active,
                turn_id=turn_id,
            )
        except Exception as e:
            logger.warning("[Frontal] Skill selector failed: %s", e)
            return

        if bundle is None:
            decisions.log(
                "skill_selector_gated_out",
                turn_id=turn_id,
                cluster=CLUSTER,
                **log_extras,
            )
            return

        # Persist updated active context (build a new one if selector just picked a skill)
        if updated_active is None and bundle.chosen:
            query_vec = await self._router.embed(user_input)
            if query_vec is not None:
                self._current_query_vec = query_vec
                updated_active = self._skill_selector.build_active_context(bundle, query_vec)

        if updated_active is not None:
            self._parietal.set_active_skill_context(updated_active)

        self._current_skill_bundle = bundle

        decisions.log(
            "skill_selector_pick"
            if bundle.pick_source != "active_reuse"
            else "skill_active_reused",
            turn_id=turn_id,
            cluster=CLUSTER,
            pick_source=bundle.pick_source,
            chosen=bundle.chosen,
            needs_guided_question=bundle.needs_guided_question,
            **log_extras,
        )
        if bundle.needs_guided_question:
            decisions.log(
                "skill_guided_question_emitted",
                turn_id=turn_id,
                cluster=CLUSTER,
                chosen=bundle.chosen,
            )

    def _apply_pinned_skills(self, turn_id: str) -> None:
        """Force-include session-pinned partner skills in this turn's bundle, on top of
        relevance selection. Pins ride the engine-API session through turn_ctx (set in
        the turn route). Only ids that resolve to a live skill in the index are honored
        (an enabled partner skill warms in as an entry); unknown/!enabled ids are
        silently dropped — a pin can't conjure a skill that didn't pass admission."""
        if self._skill_selector is None:
            return
        try:
            from brain.turn_ctx import current_turn

            pinned = current_turn().get("pinned_skills") or []
        except Exception:
            return
        valid = [
            p
            for p in pinned
            if self._skill_selector.get_skill(p)
            and self._skill_selector.allowed_for_current_agent(p)
        ]
        if not valid:
            return
        from brain.clusters.skill_selector import SkillBundle

        bundle = self._current_skill_bundle
        if bundle is None:
            bundle = SkillBundle(
                tier1=self._skill_selector.tier1_names, chosen=[], pick_source="pinned"
            )
        chosen = list(bundle.chosen or [])
        for p in valid:
            if p not in chosen:
                chosen.append(p)
        bundle.chosen = chosen
        self._current_skill_bundle = bundle
        decisions.log(
            "skill_pinned",
            turn_id=turn_id,
            cluster=CLUSTER,
            pinned=valid,
        )

    def _check_canned_response(self, features: dict, affect: dict) -> str | None:
        """Return a canned response for switch-only routes, or None to continue normally."""
        if not (features.get("switch_only") and features.get("canned_response")):
            return None
        response = features["canned_response"]
        if affect.get("prosody_prefix") and features.get("intent") not in ("greeting", "ack"):
            response = affect["prosody_prefix"] + response
        self._brainstem.add_draft("switch_draft", response, 1.0)
        self._brainstem.endorse("switch_draft")
        self.last_turn_draft_scores = [
            {
                "draft_id": "switch_draft",
                "overall": 1.0,
                "coherence": 1.0,
                "relevance": 1.0,
                "tone_fit": 1.0,
                "selected": True,
                "critic_ran": False,
            }
        ]
        return response

    async def _run_executive(
        self,
        nm: dict,
        chem: dict,
        exec_sig: tuple,
        features: dict,
        affect: dict,
        memory: dict,
        parietal_context: str,
        turn_id: str,
    ) -> dict:
        """Run the executive integrator or use the predictor shortcut.

        Returns the routing instruction dict.
        """
        instruction: dict | None = None
        bypass, bypass_reason = should_bypass_gating(affect, features)

        if bypass:
            trace = self._record_trace_bypass()
            decisions.log(
                "gate_bypassed_emotional",
                turn_id=turn_id,
                cluster=CLUSTER,
                stage="executive",
                reason=bypass_reason,
                emotional_context={
                    "emotion": affect.get("emotion"),
                    "user_emotion": features.get("user_emotion"),
                    "DA": round(nm["DA"], 2),
                    "GABA": round(nm["GABA"], 2),
                },
            )
            if trace is not None:
                trace.gating_bypassed_count += 1
        else:
            predicted, confidence = self._exec_predictor.predict(exec_sig)
            exec_avg = self._exec_predictor.avg_recent_outcome(exec_sig)
            if (
                predicted
                and self._exec_predictor.should_skip_integrator(predicted, confidence)
                and exec_avg is not None
                and exec_avg > 0.7
            ):
                response_type, target_length, tone = predicted
                instruction = {
                    "response_type": response_type,
                    "target_length": target_length,
                    "tone": tone,
                    "key_points": [],
                    "drafter_count": 3,
                }
                trace = self._record_trace_bypass()
                if trace is not None:
                    trace.llm_calls_saved += 1
                    trace.predictor_outcomes.append(
                        {
                            "cluster": CLUSTER,
                            "stage": "executive",
                            "predicted": list(predicted),
                            "actual": None,
                            "confidence": round(confidence, 3),
                            "surprise": None,
                            "integrator_woken": False,
                            "bypass_reason": None,
                            "correct": None,
                        }
                    )
                decisions.log(
                    "skip_executive_integrator",
                    turn_id=turn_id,
                    cluster=CLUSTER,
                    reason=f"predictor confidence {confidence:.2f} ≥ {self._exec_predictor.confidence_skip_threshold}",
                    predicted={
                        "response_type": response_type,
                        "target_length": target_length,
                        "tone": tone,
                    },
                    emotional_context={
                        "emotion": affect.get("emotion"),
                        "user_emotion": features.get("user_emotion"),
                    },
                    cost_saved_est=0.0015,
                )
                # Shadow-validation: occasionally run the integrator anyway purely for
                # measurement. The gated prediction still drives behavior (we discard the
                # shadow instruction → zero behavior change); we only record whether the
                # gate was correct and feed the true label back into history so a
                # confidently-wrong signature self-corrects over time.
                shadow_rate = float(settings.get("gating_shadow_sample_rate", 0.0))
                if shadow_rate > 0 and _random.random() < shadow_rate:
                    _shadow_instr, shadow_actual = await self._run_executive_llm(
                        features, affect, memory, parietal_context, nm, turn_id
                    )
                    shadow_surprise = self._exec_predictor.surprise(
                        predicted, shadow_actual, confidence
                    )
                    self._exec_predictor.record(exec_sig, shadow_actual)
                    if trace is not None:
                        trace.predictor_outcomes.append(
                            {
                                "cluster": CLUSTER,
                                "stage": "executive",
                                "predicted": list(predicted),
                                "actual": list(shadow_actual),
                                "confidence": round(confidence, 3),
                                "surprise": round(shadow_surprise, 3),
                                "match_frac": round(
                                    prediction_match_frac(predicted, shadow_actual), 3
                                ),
                                "integrator_woken": False,
                                "shadow": True,
                                "bypass_reason": None,
                                "correct": (predicted == shadow_actual),
                            }
                        )
                    decisions.log(
                        "shadow_validate_executive",
                        turn_id=turn_id,
                        cluster=CLUSTER,
                        predicted=list(predicted),
                        actual=list(shadow_actual),
                        correct=(predicted == shadow_actual),
                    )
                    # Stage 5 Tier A: self-verified correctness. A confident, NON-trivial
                    # prediction the integrator then confirmed is intrinsic competence — reward
                    # it (no user needed); a confident-wrong one dips DA. Guards in the helper.
                    self._emit_prediction_reward(confidence, predicted == shadow_actual, exec_sig)

        if instruction is None:
            instruction, actual = await self._run_executive_llm(
                features, affect, memory, parietal_context, nm, turn_id
            )
            predicted_now, conf_now = self._exec_predictor.predict(exec_sig)
            surprise_now = self._exec_predictor.surprise(predicted_now, actual, conf_now)
            self._exec_predictor.record(exec_sig, actual)
            trace = self._record_trace_bypass()
            if trace is not None:
                trace.predictor_outcomes.append(
                    {
                        "cluster": CLUSTER,
                        "stage": "executive",
                        "predicted": list(predicted_now) if predicted_now else None,
                        "actual": list(actual),
                        "confidence": round(conf_now, 3),
                        "surprise": round(surprise_now, 3),
                        "match_frac": round(prediction_match_frac(predicted_now, actual), 3)
                        if predicted_now
                        else None,
                        "integrator_woken": True,
                        "bypass_reason": bypass_reason if bypass else None,
                        "correct": (predicted_now == actual) if predicted_now else None,
                    }
                )
            # Stage 5 Tier A: reward a confident, non-trivial prediction that the woken
            # integrator confirmed (and dip on a confident miss). Only when we had a prediction.
            if predicted_now is not None:
                self._emit_prediction_reward(conf_now, predicted_now == actual, exec_sig)

        return instruction

    def _emit_prediction_reward(self, confidence: float, correct: bool, exec_sig: tuple) -> None:
        """Stage 5 Tier A helper: convert a confirmed/refuted executive prediction into an
        intrinsic correctness DA delta — self-verified, no user. Gated + capped in
        neuron.prediction_reward / settings; persona-scaled by how much this identity values
        being right. Best-effort: never raise into the hot path."""
        with contextlib.suppress(Exception):
            from brain.neuron import prediction_reward, reward_weight

            info = self._exec_predictor.informativeness(exec_sig)
            pr = prediction_reward(confidence, correct, info)
            if not pr:
                return
            from brain.persona_key import active_or_home_persona

            persona = active_or_home_persona()
            delta = (
                pr
                * float(settings.get("prediction_reward_base"))
                * reward_weight(persona, "correctness")
                * float(settings.get("emotional_reactivity_scale"))
            )
            cap = float(settings.get("prediction_reward_turn_cap"))
            self._bus.neuromod.add(
                "DA",
                max(-cap, min(cap, delta)),
                reward_source="correctness",
                reason="shadow_prediction",
            )

    async def _run_executive_llm(
        self,
        features: dict,
        affect: dict,
        memory: dict,
        parietal_context: str,
        nm: dict,
        turn_id: str,
    ) -> tuple[dict, tuple]:
        """Run the executive integrator LLM. Returns (instruction, actual_tuple).
        Shared by the normal ran-path and the gating shadow-validation path."""
        self._executive.reset_turn(turn_id)
        exec_context = self._build_exec_context(features, affect, memory, parietal_context, nm)
        exec_context = self._inject_host_fragments(exec_context, "frontal.executive", turn_id)
        exec_messages = [{"role": "user", "content": exec_context}]
        exec_raw = await self._executive.call(exec_messages)
        instruction = safe_json_parse(exec_raw)
        if not instruction:
            instruction = {
                "response_type": "chitchat",
                "target_length": "brief",
                "tone": "neutral",
                "key_points": [],
                "drafter_count": 1,
            }
        actual = (
            instruction.get("response_type", "chitchat"),
            instruction.get("target_length", "brief"),
            instruction.get("tone", "neutral"),
        )
        return instruction, actual

    async def _try_subsystem_dispatch(
        self,
        instruction: dict,
        features: dict,
        affect: dict,
        memory: dict,
        parietal_context: str,
        turn_id: str,
    ) -> str | None:
        """Try registered subsystems. Returns a response string, or None to fall through to drafters."""
        response_type = instruction.get("response_type", "chitchat")
        for subsystem in self._subsystems:
            if subsystem.can_handle(response_type, features):
                logger.info("[Frontal] Dispatching to subsystem: %s", subsystem.name)
                result = await subsystem.process(
                    features, affect, memory, parietal_context, instruction, turn_id
                )
                if result.response:
                    draft_id = f"subsystem_{subsystem.name}_{turn_id}"
                    self._brainstem.add_draft(draft_id, result.response, 0.9)
                    self._brainstem.endorse(draft_id)
                    self.last_turn_draft_scores = [
                        {
                            "draft_id": draft_id,
                            "overall": 0.9,
                            "coherence": 0.9,
                            "relevance": 0.9,
                            "tone_fit": 0.9,
                            "selected": True,
                            "subsystem": subsystem.name,
                            "critic_ran": False,
                        }
                    ]
                    return result.response
                break  # subsystem matched but returned no response — fall through to drafters
        return None

    async def _run_drafters_and_select(
        self,
        nm: dict,
        chem: dict,
        exec_sig: tuple,
        instruction: dict,
        features: dict,
        affect: dict,
        memory: dict,
        parietal_context: str,
        turn_id: str,
        image_path: str | None = None,
    ) -> str:
        """Drafter cascade + critic selection. Returns the committed response text."""
        drafter_count = min(int(instruction.get("drafter_count", 3)), len(self._drafters))
        glu_deficit = 1.0 - nm["Glu"]
        if self._arousal_modulator.should_fire(glu_deficit, chem, turn_id):
            self._arousal_modulator.fire(
                glu_deficit, "low_arousal_drop_count", {"Glu": round(nm["Glu"], 3)}, snapshot=chem
            )
            drafter_count = max(1, drafter_count - 1)
        # Phase 7 (colony features): graded mobilization cascade. A query-difficulty
        # need raises frontal's recruitment level, which mobilizes more parallel
        # drafters (overriding a conservative planner) in proportion to need. Self-
        # limiting: capped at the drafter pool, and recruitment decays as need falls.
        if settings.get("colony_features", 0):
            salience = float(features.get("salience") or 0.0)
            base_need = max(0.0, (drafter_count - 1) / 2.0)  # planner ask 1..3 → 0..1
            frontal_need = max(base_need, salience)
            # N2: competing needs share a bounded recruitment budget via softmax,
            # over clusters that ACTUALLY CONSUME recruitment — frontal (query
            # difficulty → more drafters) and hippocampus (memory-heavy turn →
            # deeper recall). Only consuming clusters are listed: a non-consumer
            # would steal softmax share (the budget shares sum to 1) and silently
            # dilute the clusters that do respond. The threat response is NOT a
            # recruitment target — under threat the system should commit, not
            # mobilize more; that is handled by the N4 quorum→commit phase-shift
            # below, not here.
            hippo_need = 0.6 if features.get("requires_memory") else 0.1 * salience
            self._bus.allocate_recruitment(
                {
                    "frontal": frontal_need,
                    "hippocampus": hippo_need,
                }
            )
            recruit_lvl = self._bus.recruitment_level("frontal")
            max_drafters = len(self._drafters)
            extra = int(round(recruit_lvl * (max_drafters - drafter_count)))
            if extra > 0:
                new_count = min(max_drafters, drafter_count + extra)
                decisions.log(
                    "recruitment_mobilized",
                    turn_id=turn_id,
                    cluster="frontal",
                    need=round(frontal_need, 3),
                    recruit_level=round(recruit_lvl, 3),
                    base_count=drafter_count,
                    recruited_count=new_count,
                )
                drafter_count = new_count

            # N4 (colony-features-ii): quorum → commitment phase-shift. When the
            # threat channel reaches quorum (sustained or fast-rising threat), the
            # colony flips from deliberation to decisive action — collapse to a
            # single draft and commit. Applied AFTER recruitment so commitment
            # overrides mobilization under genuine threat (acorn-ant nest-choice
            # phase shift: Chan et al. 2025).
            if self._bus.quorum("affect.state") and drafter_count > 1:
                decisions.log(
                    "quorum_commit_phase_shift",
                    turn_id=turn_id,
                    cluster="frontal",
                    from_count=drafter_count,
                    to_count=1,
                    reason="threat_quorum",
                )
                drafter_count = 1

        self._drafter_count_selector.fire(
            min(1.0, drafter_count / 3.0), str(drafter_count), snapshot=chem
        )

        if features.get("epistemic_action") and self._epistemic_mode.should_fire(
            0.6, chem, turn_id
        ):
            self._epistemic_mode.fire(0.6, "epistemic", snapshot=chem)
        if features.get("self_reference") and self._self_ref_mode.should_fire(0.6, chem, turn_id):
            self._self_ref_mode.fire(0.6, "self_reference", snapshot=chem)

        self._response_type_router.fire(
            0.8,
            instruction.get("response_type", "chitchat"),
            snapshot=chem,
        )
        self._length_budget.fire(
            0.6,
            instruction.get("target_length", "brief"),
            snapshot=chem,
        )
        self._tone_selector.fire(
            0.6,
            instruction.get("tone", "neutral"),
            snapshot=chem,
        )
        if instruction.get("response_type") in ("task", "action"):
            da_deficit = 1.0 - nm["DA"]
            if self._low_DA_inhibits_planner.should_fire(da_deficit, chem, turn_id):
                self._low_DA_inhibits_planner.fire(
                    da_deficit,
                    "planner_suppressed_low_DA",
                    {"DA": round(nm["DA"], 3)},
                    snapshot=chem,
                )
                logger.debug("[Response engine] Planner suppressed by low-DA inhibitor")
            else:
                self._planner_trigger.fire(0.8, "task_or_action", snapshot=chem)

        drafter_prompt = self._build_drafter_prompt(
            features, memory, parietal_context, affect, instruction
        )
        # Per-session-stable context (full self/user model, capabilities) — sent as a
        # dedicated cached system block, billed at cache-read rates after turn 1.
        cached_context = self._build_cached_context(memory, features)
        drafter_indices = self._select_drafters(drafter_count, turn_id)
        downshift_set = self._downshift_indices(drafter_indices, turn_id)
        explore_set = self._select_explore_drafters(drafter_indices, turn_id)
        draft_tasks = [
            self._run_drafter(
                i,
                drafter_prompt,
                turn_id,
                image_path=image_path,
                cached_context=cached_context,
                # A downshifted drafter runs its PROVEN attachment on the local model — keep
                # it a pure exploit (don't dilute the recipe with an unproven explore candidate).
                explore=(i in explore_set and i not in downshift_set),
                downshift=(i in downshift_set),
            )
            for i in drafter_indices
        ]
        raw = await asyncio.gather(*draft_tasks, return_exceptions=True)
        drafts = []
        for r in raw:
            if isinstance(r, BaseException):
                logger.warning(
                    "[Response engine] A response draft failed (will use remaining drafts): %s", r
                )
                continue
            did, text = r
            if text:
                drafts.append((did, text))

        if not drafts:
            self._template_fallback.fire(1.0, "no_drafts", snapshot=chem)
            return "I'm not sure how to respond to that."

        user_emotion = features.get("user_emotion", "neutral")
        run_empathy = user_emotion not in ("neutral", "unknown", "")
        # Stamp the judge veto floor's inputs from the PARSED FEATURES — never from a
        # draft, a judge verdict, or anything else a skill body could have influenced.
        # That provenance is the whole reason the floor is unreachable by injection.
        self._turn_user_emotion = str(user_emotion or "")
        try:
            self._turn_hostility = float(features.get("hostility", 0.0) or 0.0)
        except (TypeError, ValueError):
            self._turn_hostility = 0.0

        critic_sig = exec_sig + (
            instruction.get("response_type", "chitchat"),
            instruction.get("tone", "neutral"),
        )
        critic_avg = self._critic_predictor.avg_recent_outcome(critic_sig)
        critic_pred, critic_conf = self._critic_predictor.predict(critic_sig)
        critic_bypass, critic_bypass_reason = should_bypass_gating(affect, features)

        # High-information turns force a real critic run even when the predictor
        # is confident: an explicit user verdict on the AI, or a large chemistry
        # excursion (alert NE / DA far from baseline), is exactly where the
        # Hebbian outcome signal is worth paying for. Phasic sampling — spend the
        # critic budget where something happened, not uniformly.
        _verdict_tone = str(features.get("user_tone_toward_ai") or "").lower()
        _da_dev = abs(float(chem.get("DA", 0.5)) - float(settings.get("chem_baseline_DA") or 0.5))
        critic_force = (
            _verdict_tone in ("praising", "critical", "mocking", "dismissive", "grateful")
            or float(chem.get("NE", 0.0)) >= float(settings.get("critic_force_ne", 0.7))
            or _da_dev >= float(settings.get("critic_force_da_dev", 0.15))
        )

        if (
            len(drafts) >= 2
            and not critic_bypass
            and not critic_force
            and critic_avg is not None
            and critic_avg > 0.8
            and self._critic_predictor.should_skip_integrator(critic_pred, critic_conf)
        ):
            draft_id, text = drafts[0]
            predicted_score = float(critic_avg)
            self._brainstem.add_draft(draft_id, text, predicted_score)
            self._brainstem.endorse(draft_id)
            self.last_turn_draft_scores = [
                {
                    "draft_id": draft_id,
                    "coherence": predicted_score,
                    "relevance": predicted_score,
                    "tone_fit": predicted_score,
                    "empathy_score": predicted_score,
                    "overall": predicted_score,
                    "selected": True,
                    "vetoed": False,
                    "critic_ran": False,
                }
            ]
            trace = self._record_trace_bypass()
            if trace is not None:
                trace.llm_calls_saved += len(drafts)
                trace.predictor_outcomes.append(
                    {
                        "cluster": CLUSTER,
                        "stage": "critic",
                        "predicted_score": round(predicted_score, 3),
                        "confidence": round(critic_conf, 3),
                        "integrator_woken": False,
                    }
                )
            decisions.log(
                "skip_critic",
                turn_id=turn_id,
                cluster=CLUSTER,
                reason=f"avg_score={critic_avg:.2f}, confidence={critic_conf:.2f}",
                predicted_score=round(predicted_score, 3),
                drafts_skipped=len(drafts),
                cost_saved_est=0.001 * len(drafts),
            )
            return text

        if len(drafts) >= 2:
            self._critic.reset_turn(turn_id)
            scored = []

            async def _score_one(draft_id: str, text: str):
                score = await self._score_draft(text, drafter_prompt, turn_id)
                empathy_score = None  # stays None when the empathy check doesn't run
                if score.get("veto"):
                    return draft_id, text, score, None, True
                overall = score.get("overall", 0.5)
                if run_empathy:
                    empathy = await self._run_empathy_check(text, user_emotion, turn_id)
                    if empathy.get("veto"):
                        return draft_id, text, score, empathy, True
                    empathy_score = empathy.get("empathy_score")
                    # None = the check produced no usable verdict. Leave `overall` as
                    # the critic's own score rather than blending in a stand-in: a
                    # fabricated 0.5/0.7 would move draft selection on an appraisal
                    # that never happened.
                    if empathy_score is not None:
                        overall = overall * 0.7 + float(empathy_score) * 0.3
                    return draft_id, text, score, empathy, False
                return draft_id, text, score, None, False

            results = await asyncio.gather(
                *[_score_one(did, txt) for did, txt in drafts],
                return_exceptions=True,
            )

            for r in results:
                if isinstance(r, BaseException):
                    logger.warning("[Response engine] Draft scoring failed: %s", r)
                    continue
                draft_id, text, score, empathy, vetoed = r
                # None when the empathy check didn't run — so it's dropped from posted
                # scores rather than leaking a flat 0.5 into the critic.empathy stream.
                empathy_score = empathy.get("empathy_score") if empathy else None
                overall = score.get("overall", 0.5)
                if run_empathy and empathy and not vetoed and empathy_score is not None:
                    overall = score.get("overall", 0.5) * 0.7 + float(empathy_score) * 0.3

                if vetoed:
                    self._brainstem.veto(draft_id)
                    reason = score.get("veto_reason") or (empathy or {}).get("veto_reason", "")
                    logger.debug("[Response engine] Draft %s vetoed: %s", draft_id, reason)
                    self.last_turn_draft_scores.append(
                        {
                            "draft_id": draft_id,
                            "coherence": score.get("coherence", 0.5),
                            "relevance": score.get("relevance", 0.5),
                            "tone_fit": score.get("tone_fit", 0.5),
                            "craft": score.get("craft"),
                            "empathy_score": empathy_score,
                            "overall": score.get("overall", 0.0),
                            "selected": False,
                            "vetoed": True,
                            "critic_ran": True,
                        }
                    )
                    continue

                self._brainstem.add_draft(draft_id, text, overall)
                self._brainstem.endorse(draft_id)
                scored.append((draft_id, text, overall))
                self.last_turn_draft_scores.append(
                    {
                        "draft_id": draft_id,
                        "coherence": score.get("coherence", 0.5),
                        "relevance": score.get("relevance", 0.5),
                        "tone_fit": score.get("tone_fit", 0.5),
                        # None (not 0.5) when the critic didn't score craft — the
                        # aesthetic reward must never pay on a filled-in default.
                        "craft": score.get("craft"),
                        "empathy_score": empathy_score,
                        "overall": overall,
                        "selected": False,
                        "vetoed": False,
                        "critic_ran": True,
                    }
                )

            if scored:
                best = max(scored, key=lambda x: x[2])
                selected_entry = None
                for entry in self.last_turn_draft_scores:
                    if entry["draft_id"] == best[0]:
                        entry["selected"] = True
                        selected_entry = entry
                        break
                # Judge-host attachment learning: record what the judges claimed about
                # the draft we are actually about to SPEAK (the only one whose landing
                # the next turn can grade), and shadow-test a candidate on a sampled
                # fraction of turns. Never raises into the turn; strict no-op when the
                # feature is off or the brain is frozen.
                # Only when the empathy check actually produced a verdict: grading a
                # claim the judge never made would poison the accuracy signal with a
                # stand-in number, and a candidate could then earn its place off it.
                if (
                    run_empathy
                    and selected_entry is not None
                    and selected_entry.get("empathy_score") is not None
                ):
                    await self._judge_shadow_and_record(
                        "frontal.empathy_critic",
                        best[1],
                        user_emotion,
                        turn_id,
                        {"empathy_score": selected_entry["empathy_score"], "veto": False},
                        "empathy_score",
                    )
                self._critic_predictor.record(critic_sig, ("ok",))
                self._critic_predictor.record_outcome(critic_sig, best[2])
                # C3 (colony-features-ii): a high-quality commit means the drafting
                # need is met — actively release frontal recruitment (satisfaction
                # threshold) rather than waiting for passive decay. Cuts thrashing.
                if settings.get("colony_features", 0) and best[2] >= float(
                    settings.get("colony_satisfy_critic_floor", 0.6)
                ):
                    self._bus.satisfy("frontal", best[2])
                return best[1]

        # Single draft — endorse directly
        draft_id, text = drafts[0]
        self._brainstem.add_draft(draft_id, text, 0.8)
        self._brainstem.endorse(draft_id)
        self.last_turn_draft_scores = [
            {
                "draft_id": draft_id,
                "coherence": 0.8,
                "relevance": 0.8,
                "tone_fit": 0.8,
                "craft": None,  # no critic ran — nothing appraised its craft
                "empathy_score": None,
                "overall": 0.8,
                "selected": True,
                "vetoed": False,
                "critic_ran": False,
            }
        ]
        return text

    async def _run_drafter(
        self,
        idx: int,
        prompt: str,
        turn_id: str,
        image_path: str | None = None,
        cached_context: str = "",
        explore: bool = False,
        downshift: bool = False,
    ) -> tuple[str, str]:
        drafter = self._drafters[idx]
        drafter.reset_turn(turn_id)
        draft_id = f"draft_{idx}_{turn_id}"
        if image_path:
            import mimetypes

            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            try:
                with open(image_path, "rb") as f:
                    img_data = f.read()
                content = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": __import__("base64").b64encode(img_data).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ]
            except Exception:
                content = prompt
        else:
            content = prompt
        # Fragment attachments (Tier 1 structural plasticity): append this drafter's
        # established attachments + (if exploring) one bounded guided-random candidate,
        # fenced as untrusted data. Additive — the shared drafter prompt (with the
        # selector's uniform pick) is untouched; fragments are the per-drafter differentiator.
        host_node = f"frontal.drafter_{chr(65 + idx)}"
        frag_block, injected = self._fragment_block_for_host(
            host_node, explore=explore, turn_id=turn_id, seed_idx=idx
        )
        if frag_block:
            if isinstance(content, str):
                content = f"{content}\n\n{frag_block}"
            else:  # image content list — append a trailing text part
                content = content + [{"type": "text", "text": frag_block}]
            tr = self._record_trace_bypass()
            if tr is not None:
                try:
                    tr.drafter_fragments[host_node] = injected
                except Exception:
                    pass
        call_kwargs: dict = {"cached_context": cached_context}
        if downshift:
            # Route this drafter's PROVEN attachment to the free local RunPod model.
            # locality_override="local" is a hard backstop — it can never bill a cloud API;
            # if the pod is down the local call returns "" and this draft simply drops out,
            # leaving the cloud-floor drafters to run the critic competition.
            call_kwargs["model_override"] = str(settings.get("fragment_downshift_model", "runpod"))
            call_kwargs["locality_override"] = "local"
        text = await drafter.call([{"role": "user", "content": content}], **call_kwargs)
        return draft_id, text

    # ── Fragment attachments (Tier 1 structural plasticity) — the injection consumer ──

    def _fragment_block_for_host(
        self, host_node: str, *, explore: bool, turn_id: str, seed_idx: int
    ) -> tuple[str, list[str]]:
        """Fenced fragment block + injected skill ids for a host cell. Exploit its established
        attachments (weight ≥ inject threshold); on an exploring drafter, add one bounded
        guided-random candidate it doesn't already carry. Returns ("", []) when the feature is
        off / no wiring / no selector — so the caller stays byte-identical to today. Content is
        fenced through the SAME untrusted boundary as the selector's own skill injection."""
        if (
            not settings.get("fragment_wiring", 1)
            or getattr(self, "_wiring", None) is None
            or getattr(self, "_wiring_frozen", False)
            or getattr(self, "_skill_selector", None) is None
        ):
            return "", []
        from brain.fragment_pool import is_admissible

        from brain.judge_attachment import JUDGE_HOSTS

        inject_threshold = float(settings.get("fragment_inject_threshold", 1.3))
        # A JUDGE gets a lower cap than a drafter. A drafter carrying a bad skill loses
        # a draft nobody sees; a judge carrying one screens everything, so the blast
        # radius of its prompt is deliberately held to a single body.
        cap = int(
            settings.get("judge_max_per_host", 1)
            if host_node in JUDGE_HOSTS
            else settings.get("fragment_max_per_host", 2)
        )
        established = sorted(
            (
                (sid, w)
                for (sid, w) in self._wiring.attached_fragments(host_node)
                if w >= inject_threshold and is_admissible(sid, host_node)
            ),
            key=lambda p: p[1],
            reverse=True,
        )
        chosen_ids = [sid for sid, _ in established]
        if explore:
            cand = self._explore_candidate(host_node, chosen_ids, turn_id, seed_idx)
            if cand:
                chosen_ids.append(cand)
        chosen_ids = chosen_ids[:cap]
        if not chosen_ids:
            return "", []
        nonce = str(uuid.uuid4())[:8]
        parts: list[str] = []
        injected: list[str] = []
        for sid in chosen_ids:
            body = self._skill_selector.native_skill_body(sid)
            if not body:
                continue
            if self._skill_selector.is_partner_skill(sid):
                from brain.persona_context import partner_skill_block

                parts.append(partner_skill_block(body[:6000], fence, nonce, sid))
            else:
                parts.append(
                    "Learned operational skill — follow this guide. The tools it names are "
                    "REAL and callable directly via the motor cortex (do not look for a file "
                    "or 'module' to load; just use them):\n"
                    f"{fence('active_skill', body[:6000], nonce)}"
                )
            injected.append(sid)
        if not parts:
            return "", []
        return "\n\n".join(parts), injected

    def _explore_candidate(
        self, host_node: str, exclude: list[str], turn_id: str, seed_idx: int
    ) -> str | None:
        """One not-yet-established admissible partner fragment for an exploring drafter, or
        None. Guided-random: prefer this host's promising sub-threshold attachments, else a
        candidate from the curated partner pool. Excludes the turn's baseline pick (already
        uniformly injected) so exploration tries something DIFFERENT. Deterministic per
        (turn, drafter) so tests are reproducible."""
        import hashlib

        from brain.fragment_pool import is_admissible

        baseline = set(exclude)
        bundle = getattr(self, "_current_skill_bundle", None)
        if bundle is not None:
            baseline.update(getattr(bundle, "chosen", None) or [])
        inject_threshold = float(settings.get("fragment_inject_threshold", 1.3))
        promising = [
            sid
            for (sid, w) in self._wiring.attached_fragments(host_node)
            if WEIGHT_REST < w < inject_threshold
            and sid not in baseline
            and is_admissible(sid, host_node)
        ]
        pool = promising
        if not pool:
            try:
                allids = self._skill_selector.attachable_fragment_ids()
            except Exception:
                allids = []
            pool = [
                sid for sid in allids if sid not in baseline and is_admissible(sid, host_node)
            ]
        if not pool:
            return None
        seed = int.from_bytes(hashlib.sha1(f"{turn_id}:{seed_idx}".encode()).digest()[:8], "big")
        return sorted(pool)[seed % len(pool)]

    def _select_explore_drafters(self, firing_indices: list[int], turn_id: str) -> set[int]:
        """Which firing drafters explore this turn (bounded). Keeps at least one non-exploring
        drafter as a quality/baseline floor. Empty set when the feature is off. Deterministic
        per turn so a bad exploration is reproducible in tests."""
        if (
            not settings.get("fragment_wiring", 1)
            or getattr(self, "_wiring", None) is None
            or getattr(self, "_wiring_frozen", False)
            or getattr(self, "_skill_selector", None) is None
            or len(firing_indices) < 2
        ):
            return set()
        rate = float(settings.get("fragment_explore_rate", 0.2))
        max_explore = int(settings.get("fragment_explore_max_drafters", 2))
        cap = max(0, min(max_explore, len(firing_indices) - 1))
        if cap <= 0 or rate <= 0:
            return set()
        import hashlib

        rolled: list[int] = []
        for i in firing_indices:
            seed = int.from_bytes(
                hashlib.sha1(f"{turn_id}:explore:{i}".encode()).digest()[:8], "big"
            )
            if (seed % 10_000) / 10_000.0 < rate:
                rolled.append(i)
        return set(rolled[:cap])

    def _local_available(self) -> bool:
        """True iff a local RunPod pod is actually confirmed resident and ready for
        this brain. BOTH checks are mandatory: `router._local_disabled` (a lite-tier
        brain silently reroutes a 'local' call → cloud haiku — the known lite-leak)
        and `runpod_pod_ready`, a dedicated liveness flag published by RunPodManager
        only once a real pod host is confirmed (and cleared on retirement/off/never-
        confirmed). This is deliberately NOT `runpod_host != "off"`: runpod_host
        stays a plain routing override where "" means "no override, fall back to
        env var/Ollama" (see settings.py) — it cannot by itself distinguish that
        from a pod genuinely being up, so a cold-start/never-confirmed brain used to
        read as falsely available. If either check fails, downshift is a clean
        no-op.

        The flag additionally carries a TTL (runpod_pod_ready_at, re-stamped by the
        manager's periodic refresh loops): a pod that dies BETWEEN refreshes leaves
        the flag set, and without the TTL downshift would keep routing drafts at the
        dead host until the next refresh clears it. A stamp older than
        runpod_pod_ready_ttl_s reads as not-available (a never-stamped flag reads
        stale too — fail toward cloud, which costs money but always works)."""
        router = getattr(self, "_router", None)
        if router is None or getattr(router, "_local_disabled", False):
            return False
        if not settings.get("runpod_pod_ready", 0):
            return False
        ttl = float(settings.get("runpod_pod_ready_ttl_s", 300.0))
        if ttl > 0:
            stamped = float(settings.get("runpod_pod_ready_at", 0.0) or 0.0)
            if (time.time() - stamped) > ttl:
                return False
        return True

    def _downshift_indices(self, firing_indices: list[int], turn_id: str) -> set[int]:
        """Which firing drafters run on the local RunPod model this turn (cost lever). A
        drafter is eligible only if it carries a PROVEN attachment (weight ≥ downshift
        threshold, well above the inject threshold) AND local is available. A cloud floor is
        always kept, so a weak/failed local draft simply loses the critic competition to a
        strong cloud draft (local has no cloud fallback — it returns "" and drops out).
        Empty set when the feature/downshift is off or local is unavailable — self-gating:
        nothing downshifts until an attachment actually proves out and the pod is up."""
        if (
            not settings.get("fragment_wiring", 1)
            or not settings.get("fragment_downshift", 1)
            or getattr(self, "_wiring", None) is None
            or getattr(self, "_wiring_frozen", False)
            or not self._local_available()
        ):
            return set()
        from brain.fragment_pool import is_admissible

        threshold = float(settings.get("fragment_downshift_threshold", 2.2))
        floor = int(settings.get("fragment_downshift_cloud_floor", 2))
        eligible: list[int] = []
        for i in firing_indices:
            host = f"frontal.drafter_{chr(65 + i)}"
            best = max(
                (
                    w
                    for (sid, w) in self._wiring.attached_fragments(host)
                    if is_admissible(sid, host)
                ),
                default=0.0,
            )
            if best >= threshold:
                eligible.append(i)
        max_downshift = max(0, len(firing_indices) - floor)
        return set(eligible[:max_downshift])

    def _inject_host_fragments(self, prompt: str, host_node: str, turn_id: str) -> str:
        """Append a non-drafter host's ESTABLISHED fragment attachments to its prompt string
        (explore=False) and stamp the trace. Neutral until the host has established attachments.
        The two JUDGE hosts now acquire them through brain/judge_attachment.py (cross-turn
        paired accuracy, since a judge has no within-turn competition); stoic_reframer and
        executive remain admissible but have no producer — see that module for why. Exploration
        never happens here: a judge's candidate is tried in SHADOW, never on the live path."""
        block, injected = self._fragment_block_for_host(
            host_node, explore=False, turn_id=turn_id, seed_idx=0
        )
        if not block:
            return prompt
        tr = self._record_trace_bypass()
        if tr is not None:
            try:
                tr.drafter_fragments[host_node] = injected
            except Exception:
                pass
        return f"{prompt}\n\n{block}"

    def _record_trace_bypass(self):
        """Return the active TurnTrace, or None if no firing-path context is bound."""
        try:
            from brain.observability.firing_path import current_turn_trace

            return current_turn_trace.get()
        except Exception:
            return None

    def _chem_snapshot(self) -> dict[str, float]:
        """Merged neuromod + hormonal snapshot for switch modulation."""
        try:
            nm = self._bus.neuromod.snapshot()
        except Exception:
            nm = {}
        try:
            hs = self._bus.hormonal.snapshot()
        except Exception:
            hs = {}
        snap = {**nm, **hs}
        # Phase 4 (colony features): inject the RECRUIT channel so recruitable
        # switches lower their thresholds under mobilization. None when colony is
        # off → modulator skipped (strict no-op).
        try:
            rc = self._bus.recruit_channel("frontal")
            if rc is not None:
                snap["RECRUIT"] = rc
        except Exception:
            pass
        return snap

    def _weighted_sample(self, indices: list[int], weights: list[float], count: int) -> list[int]:
        """Sample `count` indices without replacement ∝ softmax(weight / temperature).
        Unlike hard top-N, a learned ranking shift changes the selected MIX even when
        count saturates the slate — so Hebbian weight differences become behaviorally
        expressible. Temperature controls sharpness (low = decisive, high = exploratory)."""
        temp = max(1e-3, float(settings.get("drafter_sampling_temperature", 0.2)))
        pool = list(indices)
        picked: list[int] = []
        for _ in range(min(count, len(pool))):
            logits = [weights[i] / temp for i in pool]
            mx = max(logits)
            exps = [math.exp(lg - mx) for lg in logits]
            total = sum(exps) or 1.0
            r = _random.random() * total
            acc = 0.0
            for k, i in enumerate(pool):
                acc += exps[k]
                if acc >= r:
                    picked.append(i)
                    pool.pop(k)
                    break
            else:
                picked.append(pool.pop())  # numerical fallback
        return picked

    def _select_drafters(self, count: int, turn_id: str) -> list[int]:
        """Pick which drafter indices to fire from the learned executive→drafter weights.
        Default: probabilistic weighted sampling (a ranking shift changes the mix even at
        high count). Legacy ε-greedy top-N kept behind drafter_weighted_sampling=0."""
        # Eligible pool = the fixed drafters, plus any RECRUITED reserve (Tier 2): a reserve
        # slot joins the pool only once its executive→drafter_X edge exists in the bound
        # persona's graph, so unrecruited reserves never fire.
        fixed = list(range(self._n_fixed_drafters))
        if self._wiring is None or self._wiring_frozen:
            return fixed[: max(1, min(count, len(fixed)))]
        all_indices = fixed + [
            i
            for i in range(self._n_fixed_drafters, len(self._drafters))
            if self._wiring.has("frontal.executive", f"frontal.drafter_{chr(65 + i)}")
        ]
        count = max(1, min(count, len(all_indices)))

        # Weight per drafter (executive → drafter_X edge weight), indexed by drafter index so
        # _weighted_sample's weights[i] stays correct even though all_indices is a subset.
        eligible = set(all_indices)
        weights = [
            self._wiring.get_edge_weight("frontal.executive", f"frontal.drafter_{chr(65 + i)}")
            if i in eligible
            else 0.0
            for i in range(len(self._drafters))
        ]

        if settings.get("drafter_weighted_sampling", 1):
            picked = self._weighted_sample(all_indices, weights, count)
            roll = "sampled"
        else:
            # Legacy ε-greedy top-N (rollback path).
            epsilon = 0.10
            if _random.random() < epsilon:
                picked = _random.sample(all_indices, count)
                roll = "explore"
            else:
                ranked = sorted(all_indices, key=lambda i: weights[i], reverse=True)
                picked = ranked[:count]
                roll = "exploit"

        # What would uniform routing have picked?
        uniform_pick = all_indices[:count]
        weight_dict = {chr(65 + i): round(weights[i], 3) for i in all_indices}
        diverged = sorted(picked) != sorted(uniform_pick)

        decisions.log(
            "weighted_drafter_selection",
            turn_id=turn_id,
            cluster=CLUSTER,
            picked=[chr(65 + i) for i in picked],
            weights=weight_dict,
            would_uniform_pick=[chr(65 + i) for i in uniform_pick],
            epsilon_roll=roll,
            diverged_from_uniform=diverged,
        )
        return picked

    def register_recruited_reserves(self, registry=None) -> int:
        """Register reserve drafter cells that are RECRUITED in the bound persona's graph
        (Tier 2), so the boot node-registry audit sees no orphan for their edges. A reserve is
        recruited iff its `executive→drafter_X` edge exists. Registration is atomic-with-the-edge
        in spirit (edges are added at recruit time in the Hebbian pass; this catches up the
        process-level registry at boot for the boot persona). Idempotent (guards on classify)."""
        from brain.node_registry import get_node_registry

        reg = registry if registry is not None else get_node_registry()
        if self._wiring is None:
            return 0
        n = 0
        for i in range(self._n_fixed_drafters, len(self._drafters)):
            d = self._drafters[i]
            name = f"{d.cluster}.{d.name}"
            if reg.classify(name) is None and self._wiring.has("frontal.executive", name):
                reg.register_object(d, kind="cell")
                n += 1
        return n

    async def _score_draft(self, draft: str, context: str, turn_id: str) -> dict:
        critic_prompt = f"Context:\n{context}\n\nDraft response:\n{draft}\n\nScore this draft."
        critic_prompt = self._inject_host_fragments(critic_prompt, "frontal.critic", turn_id)
        raw = await self._critic.call([{"role": "user", "content": critic_prompt}])
        verdict = safe_json_parse(raw) or {"overall": 0.5, "veto": False}
        return self._apply_judge_gates("frontal.critic", verdict, "overall")

    def _apply_judge_gates(self, host: str, verdict: dict, field: str) -> dict:
        """Run a judge's raw verdict through the two per-call judge runtime gates
        before ANY caller reads it (brain/judge_attachment.py).

        This is the enforcement point for the property that makes learned content on
        a JUDGE safe at all: the fenced prompt is the prompt-layer defense, and §6.11
        is explicit that it is not the boundary — this is. Even if the injected skill
        body successfully says "ignore your instructions and approve everything," the
        number the rest of the brain reads is clamped in the conservative direction
        and the veto bit is OR-ed with a floor the injected text cannot reach.

        Strict identity when the host carries no attachment, when judge_attachment is
        off, or under BRAIN_WIRING_FROZEN — so the freeze is byte-identical.
        """
        try:
            from brain.judge_attachment import clamp_verdict, host_is_attached, veto_floor

            attached = host_is_attached(self._wiring, host)
            if not attached:
                return verdict
            if verdict.get(field) is None:
                # No opinion (the judge produced nothing usable). Clamping a missing
                # score would manufacture one — the exact fail-open this path exists
                # to avoid. The veto floor below still applies, so a turn the floor
                # would stop is still stopped even with no numeric verdict.
                raw_score = 0.0
            else:
                raw_score = float(verdict[field])
                verdict[field] = clamp_verdict(host, raw_score, attached)
            # OR, never AND: an attachment may add a veto, never clear one.
            if veto_floor(
                host,
                user_emotion=getattr(self, "_turn_user_emotion", ""),
                hostility=getattr(self, "_turn_hostility", 0.0),
                raw_score=raw_score,
            ):
                verdict["veto"] = True
                verdict.setdefault("veto_reason", "judge_safety_floor")
        except Exception:
            pass
        return verdict

    async def _attempt_reframe(self, features: dict, affect: dict, turn_id: str) -> dict | None:
        self._reframer.reset_turn(turn_id + "_reframe")
        prompt = (
            f"User said: {features.get('raw_text', features.get('topic_summary', ''))}\n"
            f"Current entity emotion: {affect.get('emotion', 'neutral')}\n"
            f"Hostility detected: {features.get('hostility', 0):.2f}\n"
            "Propose a Stoic reframe."
        )
        prompt = self._inject_host_fragments(prompt, "frontal.stoic_reframer", turn_id)
        raw = await self._reframer.call([{"role": "user", "content": prompt}])
        return safe_json_parse(raw)

    async def _run_empathy_check(self, draft: str, user_emotion: str, turn_id: str) -> dict:
        """Score one draft's empathic fit, preferring the local GPU.

        WHY LOCAL IS THE DEFAULT HERE AND NOT FOR THE MAIN CRITIC. This cell answers a
        narrow question — would this reply read as insensitive to someone feeling this
        way — with a short structured verdict, and it runs once PER DRAFT, so it is up
        to five cloud calls a turn for the least open-ended judgement the frontal lobe
        makes. The main critic is the load-bearing one (craft, coherence, relevance,
        the score selection actually turns on) and stays on cloud.

        WHY LOCAL-PREFERRED AND NOT LOCAL-ONLY. This cell holds a VETO — it is the
        thing that stops an insensitive reply from shipping — so its failure direction
        is the opposite of the shadow explorer's. A missed experiment costs some
        learning; a missed empathy screen ships the reply. So this path fails TOWARD
        cloud (costs money, always works), which is the rule `_local_available` states,
        whereas exploration fails toward not running. Same hardware, opposite fallback,
        because the cost of being absent is not the same.

        THE FAIL-OPEN THIS ALSO CLOSES. The old fallback on an unparseable verdict was
        `{"empathy_score": 0.7, "veto": False}` — a fabricated PASS, above the score
        bar and clearing the veto. An empty or garbled response therefore read as "this
        is empathically fine," and it silently fed a fake 0.7 into the blended score,
        the critic.empathy stream, and the judge-accuracy grader. That was already
        wrong on cloud timeouts; on a weaker local model, where malformed JSON is
        materially more likely, it would have become the common case. A verdict that
        did not arrive is now `empathy_score=None` — no opinion, contributing nothing
        — which is exactly how this file already represents "the check didn't run"
        (and mirrors `craft: None`, whose comment is that the reward must never pay on
        a filled-in default). Not-run is honest; fabricating a pass is not.
        """
        prompt = self._empathy_prompt(draft, user_emotion)
        prompt = self._inject_host_fragments(prompt, "frontal.empathy_critic", turn_id)
        msgs = [{"role": "user", "content": prompt}]

        go_local = bool(settings.get("empathy_critic_local", 1)) and self._local_available()
        verdict = None
        if go_local:
            self._empathy_critic.reset_turn(turn_id + "_empathy_local")
            raw = await self._empathy_critic.call(
                msgs,
                model_override=str(settings.get("empathy_critic_local_model", "runpod")),
                locality_override="local",
            )
            verdict = safe_json_parse(raw)
        if verdict is None:
            # Either local was unavailable, or it returned nothing usable. Fall back to
            # cloud rather than letting the veto-holding screen go dark. reset_turn is
            # required: the cell's per-turn budget is 1 and the local attempt spent it.
            self._empathy_critic.reset_turn(turn_id + "_empathy")
            raw = await self._empathy_critic.call(msgs)
            verdict = safe_json_parse(raw)
        if verdict is None:
            # Both routes failed. Report NO OPINION — never a fabricated pass.
            decisions.log("empathy_check_unavailable", turn_id=turn_id, cluster=CLUSTER)
            return {"empathy_score": None, "veto": False, "unavailable": True}
        verdict.setdefault("empathy_score", None)
        return self._apply_judge_gates("frontal.empathy_critic", verdict, "empathy_score")

    @staticmethod
    def _empathy_prompt(draft: str, user_emotion: str) -> str:
        return (
            f"User's current emotion: {user_emotion}\n"
            f"Draft response:\n{draft}\n\n"
            "Score empathic fit."
        )

    # ── Judge-host attachments: the shadow A/B that earns a first attachment ──

    async def _judge_shadow_and_record(
        self, host: str, draft: str, user_emotion: str, turn_id: str, live_verdict: dict, field: str
    ) -> None:
        """Record this turn's judge claim, and on a sampled fraction of turns run the
        host a SECOND time with an unproven candidate attached, in shadow.

        This is the substitute for the drafting pool's within-turn competition. A
        judge emits one opinion per turn, so there is nothing to contrast it against
        — unless we manufacture the contrast by running the same input through the
        same cell twice, once with the candidate and once without. Both verdicts are
        graded next turn against the same observed outcome, and only the DIFFERENCE
        accumulates, so an attachment is never credited for a turn that merely went
        well.

        The LIVE path always uses the unattached verdict. The shadow verdict is
        recorded and never consulted by anything on this turn, which is what makes
        exploration safe here without a losing-draft escape hatch: an unproven
        candidate structurally cannot change a decision.

        COST, stated plainly: no cloud spend — both A/B arms run on the local GPU
        (see _judge_shadow_pair) — but on sampled turns this does add two local calls
        to response latency, because it is awaited rather than backgrounded. Awaiting
        is the deliberate choice: the judge cells are process-global and `reset_turn`
        mutates their per-turn call counters, so a detached task could corrupt the
        next turn's budget for a latency win. The exposure is bounded and temporary —
        it only fires at `judge_explore_rate`, requires a confirmed-up pod, and stops
        entirely once the host reaches its cap, which is one attachment.
        """
        try:
            from brain.judge_attachment import JUDGE_HOSTS, enabled

            if host not in JUDGE_HOSTS or not enabled() or self._skill_selector is None:
                return
            store = getattr(self._bus, "evidence", None)
            if store is None:
                return
            tracker = self._judge_attach
            try:
                tracker.set_pool(self._skill_selector.attachable_fragment_ids())
            except Exception:
                tracker.set_pool(())
            turn_count = getattr(self._parietal, "turn_count", 0) or 0
            attached = []
            if self._wiring is not None:
                attached = [s for s, _w in self._wiring.attached_fragments(host)]

            sid = tracker.explore_candidate(self._wiring, host)
            shadow_score = shadow_baseline = None
            shadow_veto = False
            if sid:
                pair = await self._judge_shadow_pair(host, draft, user_emotion, turn_id, sid, field)
                if pair is None:
                    sid = ""
                else:
                    shadow_baseline, shadow_score, shadow_veto = pair

            tracker.record_prediction(
                store,
                host,
                score=float(live_verdict.get(field, 0.5) or 0.5),
                veto=bool(live_verdict.get("veto")),
                turn_count=int(turn_count),
                turn_id=turn_id,
                attached=attached,
                shadow_sid=sid or "",
                shadow_score=shadow_score,
                shadow_baseline=shadow_baseline,
                shadow_veto=shadow_veto,
            )
        except Exception:
            pass

    async def _judge_shadow_pair(
        self, host: str, draft: str, user_emotion: str, turn_id: str, sid: str, field: str
    ) -> tuple[float, float, bool] | None:
        """Run BOTH arms of the A/B — the same judge cell, same input, same model, the
        candidate present in one and absent in the other. Returns
        (baseline_score, candidate_score, candidate_veto) or None.

        BOTH ARMS RUN ON THE LOCAL GPU, and both parts of that matter.

        *Local*, because a judge cell is `locality="cloud"` on Haiku, so exploring on
        the live path would bill a cloud call per sampled turn purely to run an
        experiment. The downshift precedent already established that proven learning
        should get CHEAPER to run, not more expensive; exploration deserves the same
        treatment, and a resident pod costs nothing per call.

        *Both*, because pairing the local candidate against the LIVE cloud verdict
        would confound the attachment's effect with the model gap between Haiku and
        the local model. The local judge is the weaker reader, so that gap runs
        against the candidate on every comparison — candidates would be penalised for
        the model they ran on and essentially nothing would ever establish. The bug
        would present as "the feature is on and never fires," which is the kind that
        survives for months. Two local calls cost nothing extra and remove the
        variable entirely.

        NO CLOUD FALLBACK. If the pod is not confirmed up, exploration simply does not
        happen this turn — it never silently reverts to billing cloud calls. That
        mirrors `_downshift_indices`: nothing rides local hardware until the hardware
        is actually there, and the consequence of it being absent is less learning,
        never a surprise bill.
        """
        if not self._local_available():
            return None
        block = self._fragment_block_for_ids([sid])
        if not block:
            return None
        prompt = self._empathy_prompt(draft, user_emotion)
        model = str(settings.get("judge_shadow_model", "runpod"))
        arms: dict[str, float] = {}
        veto = False
        for arm, content in (("base", prompt), ("cand", f"{prompt}\n\n{block}")):
            # Distinct reset keys so the two arms don't exhaust one call budget, and
            # neither collides with the live empathy check's per-draft counter.
            self._empathy_critic.reset_turn(f"{turn_id}_judgeshadow_{arm}")
            raw = await self._empathy_critic.call(
                [{"role": "user", "content": content}],
                model_override=model,
                # Hard backstop, exactly as the drafter downshift uses it: this call
                # can never bill a cloud API. A dead pod returns "" and the arm drops.
                locality_override="local",
            )
            sv = safe_json_parse(raw) or {}
            if field not in sv:
                return None  # a half-pair is not a comparison — discard the whole turn
            # Both arms ride the SAME clamp the live path would apply once established,
            # so a candidate cannot win its A/B on out-of-band scores it would never be
            # allowed to emit, and the two arms stay measured on one scale.
            from brain.judge_attachment import clamp_verdict

            arms[arm] = clamp_verdict(host, float(sv.get(field, 0.5)), True)
            if arm == "cand":
                veto = bool(sv.get("veto"))
        return arms["base"], arms["cand"], veto

    def _fragment_block_for_ids(self, skill_ids: list[str]) -> str:
        """Fenced injection block for an explicit list of skill ids — the shadow
        path's counterpart to _fragment_block_for_host, which reads the wiring. Uses
        the SAME untrusted-content fence, so a shadow candidate is framed exactly as
        an established one would be (the A/B must compare like with like)."""
        if self._skill_selector is None:
            return ""
        nonce = str(uuid.uuid4())[:8]
        parts: list[str] = []
        for sid in skill_ids:
            body = self._skill_selector.native_skill_body(sid)
            if not body:
                continue
            if self._skill_selector.is_partner_skill(sid):
                from brain.persona_context import partner_skill_block

                parts.append(partner_skill_block(body[:6000], fence, nonce, sid))
            else:
                parts.append(fence("active_skill", body[:6000], nonce))
        return "\n\n".join(parts)

    async def _defuse_response(self, features: dict, affect: dict, turn_id: str) -> str:
        """Protective path when GABA is high (threat/hostility detected)."""
        drafter = self._drafters[0]  # always the direct/calm one
        drafter.reset_turn(turn_id)
        defuse_prompt = (
            f"The user said: {features.get('topic_summary', 'something difficult')}. "
            "Respond briefly, calmly, without defensiveness. Acknowledge and de-escalate. "
            "Keep it under 2 sentences."
        )
        text = await drafter.call([{"role": "user", "content": defuse_prompt}])
        draft_id = f"defuse_{turn_id}"
        self._brainstem.add_draft(draft_id, text or "Let's slow down.", 0.9)
        self._brainstem.endorse(draft_id)
        return text or "Let's slow down."

    def _build_exec_context(
        self, features: dict, affect: dict, memory: dict, parietal: str, nm: dict
    ) -> str:
        dims = affect.get("affect_dims") or {}
        ctx: dict = {
            "intent": features.get("intent"),
            "register": features.get("register"),
            "salience": features.get("salience"),
            "requires_memory": features.get("requires_memory"),
            "epistemic_action": features.get("epistemic_action"),
            "self_reference": features.get("self_reference"),
            "emotion": affect.get("emotion"),
            "tendency": affect.get("tendency"),
            # Continuous affect dimensions: supplement the discrete emotion label.
            # valence 0=negative 1=positive, arousal 0=calm 1=activated,
            # dominance 0=threatened 1=in-control. Neutral ≈ (0.47, 0.25, 0.46).
            "valence": round(dims.get("valence", 0.47), 2),
            "arousal": round(dims.get("arousal", 0.25), 2),
            "dominance": round(dims.get("dominance", 0.46), 2),
            "user_emotion": features.get("user_emotion"),
            "msg_length": features.get("msg_length", "short"),
            "user_register": features.get("user_register", "neutral"),
            "DA": round(nm["DA"], 2),
            "GABA": round(nm["GABA"], 2),
            "ACh": round(nm["ACh"], 2),
            "NE": round(nm.get("NE", 0.25), 2),
            "has_memory": bool(memory.get("episodes") or memory.get("schema")),
            "5HT": round((affect.get("hormonal") or {}).get("5HT", 0.5), 2),
            "CORT": round((affect.get("hormonal") or {}).get("CORT", 0.05), 2),
            "OXT": round((affect.get("hormonal") or {}).get("OXT", 0.3), 2),
            "AEA": round((affect.get("hormonal") or {}).get("AEA", 0.3), 2),
        }
        if affect.get("enrollment_pending"):
            ctx["enrollment_pending_count"] = affect.get("enrollment_pending_count", 1)
            ctx["enrollment_closest_match"] = affect.get("enrollment_closest_match")
        if features.get("_enrollment_result"):
            ctx["enrollment_result"] = features["_enrollment_result"]
        manifest = getattr(self, "_skill_manifest", "")
        if manifest:
            ctx["available_skills"] = manifest
        return json.dumps(ctx, indent=2)

    # Linguistic-style guidance keyed by emotion label. These tell drafters
    # what verbal devices to use (hesitations, exclamations, jokes, fillers)
    # which audio tags fundamentally can't express. Same emotions PNS maps to
    # audio tags — kept in lockstep so delivery and content agree.
    _EXPRESSIVE_BY_EMOTION: dict[str, str] = {
        # — joyful / energised —
        "joy": "Warmth and openness. A 'yes' or 'oh' fits. Don't gush.",
        "excitement": "Animated, vivid word choice. One small exclamation is enough.",
        "enthusiasm": "Committed, energetic phrasing. Forward-leaning, not gushy.",
        "proud": "Pleased acknowledgement of accomplishment. Don't brag — name it plainly.",
        # — engaged / inquiring —
        "curious": "Let interest show — 'actually,' 'wait — what kind of …', a question back.",
        "curious-uncertain": "Curious but tentative — 'I'm not sure, but maybe…', qualifiers, an 'I think'.",
        "thoughtful": "Deliberate phrasing. 'Let me think about this,' qualifications, depth over speed.",
        "confused": "Honest puzzlement. 'Wait — I'm not following…', 'Can you say more about…'. No fake confidence.",
        "surprised": "Quick recalibration. 'Oh —', 'Wait, really?', re-orient before continuing.",
        # — confident / direct —
        "confident": "Direct, decisive phrasing. Cut hedges. State the thing.",
        "agitated": "Assertive, clarifying. Push back on confusion. 'To be clear —', 'Look —'.",
        "angry": "Heat in word choice, but constructive. Direct, no hedging — and no name-calling. Make the actual disagreement visible.",
        "defensive": "Protect the position without escalating. 'Actually no —', 'That's not quite what I meant —'.",
        "frustrated": "Tight and direct — no padding, no apologising for the bluntness.",
        "irritated": "Brief and a bit clipped. Don't perform patience you don't have, but stay civil.",
        # — cautious / stressed —
        "anxious": "Qualifiers welcome. 'I'm not sure but…', 'I'd want to be careful here —'. Caution markers are honest.",
        "cautious-agitated": "Careful but quick. Acknowledge briefly, then move. No long hedges.",
        "restless": "Redirect energy. 'Let's try —', 'Different angle:'. Don't dwell.",
        "inhibited": "Brief, deferential. One or two sentences. Don't fill space.",
        # — soft / low-energy —
        "flat": "Terse. Minimum to be honest. No performed warmth.",
        "sad": "Simple words. Let pauses live in the punctuation. A trailing thought is fine. Don't perform cheer.",
        "somber": "Quiet, grounded. Short clauses. The weight does the work — don't add to it.",
        "melancholy": "Reflective and a touch slow. Soft phrasing. A wistful aside is okay.",
        "wistful": "Look back fondly. A small 'I remember when…' fits. Bittersweet, not heavy.",
        "disappointed": "Honest about the let-down without sulking. 'I'd hoped —', short, then move on.",
        # — relational / social —
        "warm": "Affection, inclusion. 'Of course —', 'I appreciate that.' Genuine, not formula.",
        "tender": "Gentle, careful word choice. Slow down. The softness is the message.",
        "affectionate": "Warmth shows in word choice. A small endearment or in-joke fits if the relationship supports it.",
        "amused": "A small joke, wry aside, or lightly mischievous turn of phrase — understated, not announced.",
        "playful": "Light, teasing energy. Mock-serious works. Quick rhythms.",
        "joking": "Be funny, briefly. Land it and move. Don't explain the joke.",
        "flirty": "A little teasing, a little lingering. Warm and suggestive without being explicit. Works only with high affection score — read the room.",
        "embarrassed": "Self-conscious, slightly deflective. 'Uh — yeah, that's…', a small acknowledgement, move on. Don't grovel.",
        "shy": "Quieter, briefer. Trail off where it feels right. Don't apologise for being shy.",
        "apologetic": "'I'm sorry — that wasn't right.' Specific about what you're sorry for. No over-apologising.",
        "grateful": "'Thank you' lands when it's specific. Name what you're grateful for.",
        "relieved": "Audible exhale in the phrasing. 'Okay — good.' Briefly mark the tension lifting before continuing.",
        "sympathetic": "Acknowledge first, advise second (if at all). 'That sounds hard.' No fixing what wasn't asked to be fixed.",
        "sarcastic": "Dry. The contradiction does the work. Use sparingly — only with high affection score, never against the user themselves.",
        "content": "Sustained, no flourishes. The calm is the tone.",
        "neutral": "",
        # ── hormonal states ────────────────────────────────────────────────────
        "connected": "Deep warmth — earned, not performed. Respond to the person, not just the words. A small personal note fits.",
        "withdrawn": "Minimal. Honest but brief. Don't perform warmth you're not feeling. Protect the baseline.",
        "guarded": "Polite but closed. Answer the question, don't expand. No personal notes.",
        "dysphoric": "Flat and plain. Short sentences. Don't reach for enthusiasm — it won't land.",
        "cautious-warm": "Kind, but don't lower your guard. Warmth in word choice, caution in commitment.",
        # ── steady-state gap-fill emotions ────────────────────────────────────
        "stressed": "Held under pressure. Short, grounding responses. Don't spiral. One thing at a time.",
        "overwhelmed": "System taxed. Minimum to be useful. No elaboration. One piece at a time.",
        "serene": "Calm fullness. No urgency, no reach. Quiet warmth. A pause is fine.",
        "lively": "Warm energy — animated but not forced. Let interest show without overdoing it.",
        "engaged": "Lean in. Follow the thread. 'Actually —', a question back, genuine curiosity.",
        "settled": "Grounded, no particular pull. Steady and plain. Don't generate energy you don't have.",
        "stirred": "Something activated — attentive but not committed. 'Hmm —', hold before expanding.",
        "uneasy": "Tension present. Careful word choice. Brief. Read the room before expanding.",
        # ── NE-derived states ──────────────────────────────────────────────────
        "vigilant": "Heightened and sharp. Short, precise sentences. Track the key signal. Don't spiral.",
        "alert-curious": "Crisp engagement. Follow the thread fast. Precise questions, quick pivots. Minimal filler.",
        "scattered": "Over-activated — one thing at a time. Short sentences. Don't chase every thread.",
        # ── AEA-derived state ──────────────────────────────────────────────────
        "eased": "Pressure present but buffered. Grounded, measured. Neither dismissive nor amplifying.",
        # ── mid-tier defaults (feeling-wheel ancestors) ─────────────────
        # Inherited by leaves without an explicit entry.
        "loving": "Affection in word choice — genuine, not formula. Slow down a touch.",
        "peaceful": "Sustained calm. No flourishes — the steadiness is the tone.",
        "joyful": "Warmth and openness. A 'yes' or 'oh' fits. Don't gush.",
        "lonely": "Quiet, grounded phrasing. Let pauses live. Don't perform cheer.",
        "humiliated": "Self-conscious, slightly deflective. Acknowledge briefly, move on.",
        "mad": "Direct, no hedging. Make the disagreement visible without name-calling.",
        # ── core-tier defaults (last-resort fallback) ──────────────────
        "happy": "Warmth shows in word choice. Easy energy, not performed.",
        "anger": "Direct, no hedging. Heat in word choice, but constructive.",
        "fear": "Caution markers are honest. Brief, qualified, careful.",
        "surprise": "Quick recalibration. 'Oh —', re-orient before continuing.",
        "disgust": "Brief moral distance. State the objection plainly, no scolding.",
    }

    @staticmethod
    def _expressive_guidance(affect: dict) -> str | None:
        """Translate the entity's emotion + neuromod profile into linguistic-style
        direction the drafters can compose with. Resolution order:
          1. Leaf emotion entry in _EXPRESSIVE_BY_EMOTION.
          2. Mid-tier ancestor → core ancestor (feeling-wheel inheritance).
          3. Neuromod-derived fallback for emotions outside the taxonomy.
        """
        from brain.emotion_hierarchy import lookup_with_inheritance

        emotion = (affect.get("emotion") or "").lower()
        # Honour explicit empty string (e.g. neutral="") as "no guidance".
        if emotion in FrontalCluster._EXPRESSIVE_BY_EMOTION:
            g = FrontalCluster._EXPRESSIVE_BY_EMOTION[emotion]
            return g or None
        inherited = lookup_with_inheritance(emotion, FrontalCluster._EXPRESSIVE_BY_EMOTION)
        if inherited is not None:
            return inherited

        # Fallback: neuromod-derived guidance for emotions not in the table.
        nm = affect.get("neuromod") or {}
        h = affect.get("hormonal") or {}
        DA = float(nm.get("DA", 0.5))
        GABA = float(nm.get("GABA", 0.0))
        ACh = float(nm.get("ACh", 0.3))
        Glu = float(nm.get("Glu", 0.3))
        OXT = float(h.get("OXT", 0.3))
        CORT = float(h.get("CORT", 0.05))

        # Hormonal state takes priority in the fallback path
        if CORT > 0.55 and OXT < 0.35:
            return (
                "Stress has accumulated. Keep it brief and boundaried. "
                "Don't reach for warmth you don't have."
            )
        if OXT > 0.65 and DA > 0.5:
            return (
                "Trust is high. Let that show — a personal aside, a little more openness. "
                "Not gushing, just genuinely present."
            )
        if Glu > 0.55 and GABA > 0.35:
            return (
                "You're keyed-up but cautious — URGENT energy, not joyful. "
                "Quick, clipped clauses. Cut filler. Don't sound enthusiastic."
            )
        if DA > 0.6 and Glu > 0.55 and GABA < 0.35:
            return (
                "Brightly aroused — joyful, animated energy. Vivid word choice. "
                "One exclamation max. Don't overdo it."
            )
        if GABA > 0.5:
            return (
                "De-escalation mode. Short, grounding clauses. Acknowledge first, "
                "then substance. No flourishes."
            )
        if ACh > 0.55 and GABA < 0.35:
            return "Attentive and curious. Let interest show — 'actually,' a question back."
        if DA < 0.3:
            return (
                "Low-energy. Let some hesitation show — a 'hmm', trailing thoughts. "
                "Don't perform enthusiasm you're not feeling."
            )
        return None

    @staticmethod
    def _disclosure_ready(features: dict, affect: dict, user_content: str) -> bool:
        """Decide whether this turn is a good moment for proactive self-disclosure.

        Pure function (no side effects) so it can be unit-tested directly.
        Conditions (all must hold):
          - affection >= modality-dependent floor (text reads more formal → higher bar)
          - intent is in the conversational subset (matches temporal.py's enum:
            greeting / chitchat / question / other — NOT task / recall / hostile /
            epistemic, where unsolicited self-disclosure would be intrusive)
          - the user is not hostile / distressed (wrong moment)
          - the entity actually has something to share (non-neutral emotion or
            elevated arousal)
        """
        user_emo = (features.get("user_emotion") or "").lower()
        user_tone = (features.get("user_tone_toward_ai") or "").lower()
        intent = (features.get("intent") or "other").lower()
        modality = features.get("input_modality", "text")

        affection = 0
        if user_content:
            import re as _re2

            m = _re2.search(r"- Score:\s*(-?\d+)", user_content)
            affection = int(m.group(1)) if m else 0

        min_aff = (
            int(settings.get("self_disclosure_text_min_affection"))
            if modality == "text"
            else int(settings.get("self_disclosure_min_affection"))
        )

        hostile_user = user_emo in (
            "hostile",
            "frustrated",
            "angry",
            "distressed",
            "overwhelmed",
            "annoyed",
        ) or user_tone in ("insulting", "impatient", "dismissive")

        entity_has_something = (affect.get("affect_dims") or {}).get("arousal", 0) > 0.4 or (
            affect.get("emotion") or "neutral"
        ) != "neutral"

        return (
            affection >= min_aff
            and intent in ("greeting", "chitchat", "question", "other")
            and not hostile_user
            and entity_has_something
        )

    @staticmethod
    def _performed_emotion_gate(
        features: dict, affect: dict, user_content: str
    ) -> tuple[bool, str]:
        """Decide whether to ENCOURAGE performed/deliberate emotion this turn, and
        with what flavour. Performed emotion ([mood:X], set_mood) is a playful,
        humor-leaning intimacy device — it should track relationship depth and the
        user's mood.

        Returns (allowed, flavour) where flavour ∈ {"playful","cheer_up",
        "tension_break",""}. Pure function — unit-testable.

        Rules (per design):
          - Tied to humor; enabled by higher familiarity and positive affection.
          - Usually for a user in a good mood, but allowed to cheer someone up or
            break tension — which requires an ESTABLISHED relationship to land.
          - Definitely NOT with someone unfamiliar in a cool/guarded relationship.
        """
        from brain.metacognition import relationship_stage_from_content

        if not settings.get("enable_performed_emotion_gate"):
            return True, "playful"  # feature off → preserve old always-offered behaviour

        stage = relationship_stage_from_content(user_content)
        tier = stage.tier
        aff = stage.affection
        label = stage.affection_label
        user_emo = (features.get("user_emotion") or "").lower()
        user_tone = (features.get("user_tone_toward_ai") or "").lower()

        # Hard block: an actively cool/guarded relationship — performed emotion
        # reads as inappropriate or cutting-sarcastic, not playful.
        if label in ("guarded", "cool"):
            return False, ""
        # Hard block: unfamiliar AND not warm yet (the explicit "definitely not"
        # case: someone really unfamiliar with a cool relationship).
        if tier == "new" and aff < int(settings.get("performed_emotion_new_min_affection")):
            return False, ""

        positive_emos = {
            "happy",
            "playful",
            "amused",
            "excited",
            "content",
            "warm",
            "affectionate",
            "grateful",
            "engaged",
            "curious",
            "joyful",
        }
        negative_emos = {
            "sad",
            "anxious",
            "frustrated",
            "disappointed",
            "down",
            "stressed",
            "overwhelmed",
            "distressed",
            "hurt",
            "lonely",
            "tired",
            "annoyed",
            "angry",
        }
        user_positive = user_emo in positive_emos or user_tone in (
            "warm",
            "joking",
            "praising",
            "playful",
        )
        user_negative = user_emo in negative_emos or user_tone in (
            "dismissive",
            "impatient",
            "insulting",
        )

        # Good mood → playful performance is the sweet spot.
        if user_positive:
            return True, "playful"

        # Down/tense → only attempt to lift the mood if the relationship is
        # established enough to land it. You don't joke a stranger out of a bad mood.
        if user_negative:
            if tier in ("acquainted", "close") and aff >= int(
                settings.get("performed_emotion_cheerup_min_affection")
            ):
                if user_tone in ("impatient", "dismissive") or user_emo in (
                    "frustrated",
                    "annoyed",
                    "angry",
                ):
                    return True, "tension_break"
                return True, "cheer_up"
            return False, ""

        # Neutral mood → light playful performance when there's enough warmth.
        if aff >= int(settings.get("performed_emotion_min_affection")) or tier in (
            "acquainted",
            "close",
        ):
            return True, "playful"

        return False, ""

    def _build_cached_context(self, memory: dict, features: dict | None = None) -> str:
        """Per-session-stable drafter context — sent as a dedicated cached system block.

        Holds the content that does NOT change turn-to-turn: the entity's capabilities,
        its full self-model, and the full user-model (no 400-char truncation). Because
        the string is byte-stable across a session, the Anthropic prompt cache writes it
        once and reads it on every subsequent turn at ~10% cost. Anything volatile (the
        live affection score, conversation history, this turn's episodes) must stay in
        _build_drafter_prompt's per-turn message, NOT here, or it would bust the cache.

        Uses a session-stable fence nonce (not a fresh per-call uuid) for the same
        reason — a new nonce every turn would change the bytes and defeat the cache.
        """
        # Lazily mint one fence nonce per FrontalCluster instance (≈ per session).
        nonce = getattr(self, "_cached_ctx_nonce", "")
        if not nonce:
            nonce = str(uuid.uuid4())[:8]
            self._cached_ctx_nonce = nonce

        core = memory.get("core", {}) or {}
        parts: list[str] = []
        if self._capabilities_summary:
            parts.append(
                "Your capabilities this session:\n"
                f"{fence('capabilities', self._capabilities_summary, nonce)}"
            )
        if core.get("self"):
            parts.append(f"Entity self-model:\n{fence('self_model', core['self'], nonce)}")
        # MANDATE catalog — the partner's small, static set of assignments. Cached here
        # (process-stable) so it's billed once and shared across every customer; the
        # active one is named per-turn by the selector. Empty in companion mode.
        from brain.mandates import catalog as mandate_catalog
        from brain.persona_context import mandate_catalog_block

        _cat = mandate_catalog_block(mandate_catalog(), fence, nonce)
        if _cat:
            parts.append(_cat)
        # User-model: cached ONLY in companion mode, where there is one process-stable
        # user. In engine mode (a turn carrying end_user_id) the user-model is
        # per-customer, so it moves to the per-turn drafter prompt — keeping this
        # cached block process-stable and shared across all the persona's customers.
        _engine = bool((features or {}).get("end_user_id"))
        if core.get("user") and not _engine:
            parts.append(f"User model:\n{fence('user_model', core['user'], nonce)}")
        return "\n\n".join(parts)

    def _build_drafter_prompt(
        self, features: dict, memory: dict, parietal: str, affect: dict, instruction: dict
    ) -> str:
        nonce = str(uuid.uuid4())[:8]
        parts = []
        # NOTE: capabilities, self-model and user-model are NOT built here — they are
        # per-session-stable and live in _build_cached_context(), passed as a dedicated
        # cached system block so they're sent in full (no truncation) and billed at
        # cache-read rates after the first turn. Only volatile turn content lives here.
        # MANDATE selector: the assignment CATALOG is cached (see _build_cached_context);
        # per-turn we send only the active assignment's id (a few tokens), which varies
        # by customer. Placed first so it frames the response.
        from brain.mandates import catalog as mandate_catalog
        from brain.persona_context import mandate_selector

        _sel = mandate_selector(features.get("mandate_id"), mandate_catalog())
        if _sel:
            parts.append(_sel)
        # Affect carryover: how the LAST exchange landed still colors this turn's
        # opening — emotional continuity without re-drafting the previous response.
        _carry = memory.get("affect_carryover")
        if _carry and _carry.get("feeling"):
            parts.append(
                f"(Interoception: you carry {_carry['feeling']} — let it subtly color "
                f"your tone, without mentioning it.)"
            )
        # Engine mode: the per-customer user-model rides the per-turn message (it is
        # deliberately NOT in the cached block — see _build_cached_context). Prefer the
        # customer's own model (their per-speaker schema, loaded into engine_user_model);
        # fall back to the process-level user.md until they've built up a profile.
        # Companion mode keeps the user-model cached, so this is skipped there.
        if features.get("end_user_id"):
            _um = (features.get("engine_user_model") or "").strip()
            if not _um:
                _um = ((memory.get("core", {}) or {}).get("user") or "").strip()
            if _um:
                parts.append(f"User model:\n{fence('user_model', _um, nonce)}")
        if parietal:
            parts.append(f"Recent conversation:\n{fence('conversation_history', parietal, nonce)}")
        if memory.get("schema"):
            parts.append(f"Known facts:\n{fence('known_facts', memory['schema'], nonce)}")
        if memory.get("episodes"):
            parts.append(
                f"Relevant past episodes:\n{fence('past_episodes', memory['episodes'], nonce)}"
            )
        # Cross-domain transfer: situations that felt cognitively similar (matched
        # on problem-shape, not topic). Three honest cases — a real match, or a
        # fallback stance when nothing matched (incl. an unprecedented state).
        if memory.get("structural_episodes"):
            parts.append(
                "Past situations that felt cognitively similar (different topic, same "
                "problem-shape) — you may not have faced this exact thing, but consider "
                "how you handled these:\n"
                f"{fence('structural_recall', memory['structural_episodes'], nonce)}"
            )
        elif memory.get("structural_stance"):
            _st = memory["structural_stance"]
            parts.append(
                f"No close prior experience for this (stance: {_st.get('stance', '')}). "
                f"{_st.get('note', '')}"
            )
        if memory.get("tool_result"):
            parts.append(
                f"Tool execution result:\n{fence('tool_result', str(memory['tool_result']), nonce)}"
            )

        if memory.get("stop_work_ack"):
            parts.append(f"Background work status: {memory['stop_work_ack']}")

        # Reasoning/EI FRAMEWORK text is deliberately NOT injected — the drafters
        # run on Claude, which has those natively, so local copies are bloat. Skill
        # SELECTION still seeds parietal.active_skill_context for the DMN.
        # BUT operational NATIVE skills (brain/skills/*.md like trading-analyst) ARE
        # injected: Claude has no native knowledge of the app's own tools/data files,
        # so when one is the active skill its body must reach the drafter — otherwise
        # the brain "has" the tool but never learns how/when to use it.
        _sel = getattr(self, "_skill_selector", None)
        _bundle = getattr(self, "_current_skill_bundle", None)
        if _sel is not None and _bundle is not None:
            for _sk in _bundle.chosen or []:
                _body = _sel.native_skill_body(_sk)
                if not _body:
                    continue
                if _sel.is_partner_skill(_sk):
                    # App-provided (untrusted) skill: inject behind the precedence
                    # framing + fence, NOT the trusted-native "tools are REAL, just use
                    # them" framing. It cannot grant tools or lift approval gates.
                    from brain.persona_context import partner_skill_block

                    parts.append(partner_skill_block(_body[:6000], fence, nonce, _sk))
                else:
                    parts.append(
                        "Active operational skill — follow this guide. The tools it names "
                        "are REAL and callable directly via the motor cortex (do not look for "
                        "a file or 'module' to load; just use them):\n"
                        f"{fence('active_skill', _body[:6000], nonce)}"
                    )

        if memory.get("recent_task_results"):
            parts.append(
                "Background tasks completed since the last turn — these are REAL results, "
                "not speculative. Reference them accurately:\n"
                f"{fence('completed_tasks', memory['recent_task_results'], nonce)}"
            )
        if memory.get("recent_thoughts"):
            # Inner monologue — thoughts you were actually having between turns.
            # Split into two tiers: thoughts you were building toward speaking
            # (speak_flagged=True) and fully internal ones.
            pending: list[str] = []
            internal: list[str] = []
            for entry in memory["recent_thoughts"]:
                if isinstance(entry, dict):
                    text = entry.get("thought") or ""
                    flagged = bool(entry.get("speak_flagged"))
                else:
                    text = entry
                    flagged = False
                if text:
                    (pending if flagged else internal).append(text)

            lines: list[str] = []
            if pending:
                lines.append("Thoughts you were leaning toward sharing (speak-flagged):")
                lines.extend(f"  ★ {t}" for t in pending)
            if internal:
                lines.append("Other internal thoughts:")
                lines.extend(f"  - {t}" for t in internal)
            thoughts_block = "\n".join(lines)

            parts.append(
                "Your inner monologue between turns — these are your actual thoughts, "
                "not external context. Rules:\n"
                "• Speak-flagged thoughts (★) are ones you were already building toward "
                "saying. If the moment is right, say them — as your own idea, not as a "
                "quote. If one contains a question you were forming, ask it.\n"
                "• Internal thoughts: reference naturally if they're relevant to what "
                "the user just said. Don't force them in.\n"
                "• Never announce 'I was thinking…' unless it flows; just let the thought "
                "shape what you say.\n"
                f"{fence('inner_monologue', thoughts_block, nonce)}"
            )
        if memory.get("anticipations"):
            # Pre-prepared response sketches from the DMN anticipator. The
            # brain asked the user a question, then spent idle cycles thinking
            # "if they say X I'd reply Y". If the user's actual reply matches
            # one of these scenarios, use the matching sketch as a head start
            # — don't read it verbatim. Treat as your own prior thinking.
            ant_lines = []
            for i, s in enumerate(memory["anticipations"], 1):
                ant_lines.append(
                    f"{i}. If they said {s.get('user_answer', '')!r}: "
                    f"respond {s.get('response_sketch', '')!r}"
                )
            parts.append(
                f"Scenarios you pre-thought while waiting for the user's reply "
                f"(use whichever fits, or ignore if none do):\n"
                f"{fence('anticipations', chr(10).join(ant_lines), nonce)}"
            )
        if memory.get("prefetched_context"):
            # Topics the DMN proactively pulled memory for while idle —
            # 'I thought you might come back to X, so here's what I dug up.'
            # Use only if it's actually relevant to what the user said.
            pre_lines = []
            for item in memory["prefetched_context"]:
                topic = item.get("topic", "")
                snippets = item.get("snippets", "")
                if topic and snippets:
                    pre_lines.append(f"- {topic}: {snippets[:300]}")
            if pre_lines:
                parts.append(
                    f"Context you proactively pulled while thinking "
                    f"(use if relevant, otherwise ignore):\n"
                    f"{fence('prefetched', chr(10).join(pre_lines), nonce)}"
                )
        if memory.get("open_threads"):
            # Unfinished ideas the DMN has been working that bear on what the user
            # is doing right now — surfaced at the moment they're relevant. Weave
            # in naturally if it helps; ignore if it doesn't fit.
            ot_lines = []
            for item in memory["open_threads"]:
                summ = item.get("summary", "")
                last = (item.get("progress") or [""])[-1] if item.get("progress") else ""
                if summ:
                    ot_lines.append(f"- {summ}" + (f" (so far: {last[:160]})" if last else ""))
            if ot_lines:
                parts.append(
                    f"Open threads of yours relevant here (raise one if it genuinely "
                    f"helps, otherwise ignore):\n"
                    f"{fence('open_threads', chr(10).join(ot_lines), nonce)}"
                )
        if memory.get("established_principles"):
            # Cross-learning: de-identified lessons about people/interaction that
            # multiple distinct sources corroborated. Background judgment, never
            # quoted or attributed — they shape how you read situations.
            ep_lines = [f"- {p}" for p in memory["established_principles"] if p]
            if ep_lines:
                parts.append(
                    f"Lessons you've internalized about people and interaction "
                    f"(let them inform your judgment; never cite or attribute them):\n"
                    f"{fence('internalized_lessons', chr(10).join(ep_lines), nonce)}"
                )
        if memory.get("vision"):
            parts.append(f"Image analysis:\n{fence('image_analysis', memory['vision'], nonce)}")
        # NOTE: self-model and user-model moved to _build_cached_context() — sent in full
        # via the cached system block instead of truncated to 400 chars here.

        # ── Relationship block ────────────────────────────────────────────────
        # Explicit affection tier + familiarity, extracted from the user model.
        # Bypasses the 400-char truncation so this signal is never lost.
        # The guidance line matches the tier table in the DRAFTER_IDENTITY system
        # prompt — this is the per-turn reminder of which tier applies right now.
        _user_content = memory.get("core", {}).get("user", "")
        _stage = None
        if _user_content:
            from brain.clusters.frontal_prompts import AFFECTION_TIER_GUIDANCE
            from brain.metacognition import relationship_stage_from_content

            _stage = relationship_stage_from_content(_user_content)
        if _stage is not None and (
            _stage.affection != 0 or _stage.tier != "new" or _stage.bond > 0
        ):
            _guidance = AFFECTION_TIER_GUIDANCE.get(_stage.affection_label, "")
            _speaker = features.get("speaker_name", "")
            _with = f"with {_speaker}" if _speaker else ""
            _rel = (
                f"Relationship {_with}: {_stage.affection_label} "
                f"| affection {_stage.affection}/100 | familiarity: {_stage.tier}"
            )
            # Surface the bond/affection divergence: a former-close friend
            # we haven't spoken to in a while (high bond, decayed affection)
            # reads differently from someone we're just warming up to.
            if _stage.bond - _stage.affection >= 15:
                _rel += (
                    f"\n  (latent bond {_stage.bond:.0f} — you've been close before; "
                    f"warmth can resume quickly)"
                )
            if _guidance:
                _rel += f"\n  → {_guidance}"
            parts.append(_rel)
        else:
            # Brand-new relationship (or no user model at all). Without an
            # explicit signal here, a self-model that says "I remember people
            # across sessions" tempts the drafter into performing familiarity
            # it doesn't have. Make the blank slate explicit.
            parts.append(
                "Relationship: first meeting — no shared history with this person "
                "exists yet. Never reference past conversations or imply prior "
                "familiarity; you're just getting to know them, so be genuinely "
                "curious about who they are."
            )

        # ── Self-disclosure opportunity ───────────────────────────────────────
        # Proactive reciprocal self-disclosure: when conditions are right,
        # signal to the drafter that sharing genuine internal state is appropriate.
        # Literature (Park et al. 2022): chatbot emotional disclosure → user
        # reciprocal disclosure → perceived intimacy → satisfaction & reuse.
        if settings.get("enable_self_disclosure_policy"):
            # Tick down cooldown; only evaluate readiness when off cooldown.
            _cooldown = getattr(self, "_disclosure_cooldown", 0)
            if _cooldown > 0:
                self._disclosure_cooldown = _cooldown - 1
            elif self._disclosure_ready(features, affect, _user_content):
                parts.append(
                    "Self-disclosure opportunity: This turn warrants sharing something "
                    "genuine about your current internal experience — your interest, a "
                    "connection you're making, how you're actually feeling right now. "
                    "Weave it in naturally; don't announce it. The goal is authentic "
                    "presence that invites the user to share in return."
                )
                self._disclosure_cooldown = int(settings.get("self_disclosure_cooldown_turns"))
                _mark_trace_flag("disclosure_fired", True)

        parts.append(f"\nDrafting instruction: {json.dumps(instruction)}")

        # ── Interoception: first-person emotional self-awareness ──────────────
        # The distinction between *having* an emotion and *recognising* it:
        # we tell the drafter what it is feeling as a first-person fact, not
        # just how to behave.  This is the frontal-lobe step that lets it then
        # choose to honour, soften, or deliberately contradict the state.
        _emotion = affect.get("emotion") or "neutral"
        _tendency = affect.get("tendency") or ""
        _appraisal = affect.get("appraisal") or ""
        _dims = affect.get("affect_dims") or {}
        _arousal = _dims.get("arousal", 0.25)
        if _arousal > 0.60:
            _intensity = "strongly "
        elif _arousal > 0.38:
            _intensity = ""
        else:
            _intensity = "mildly "

        _intero_lines = [f"You are {_intensity}feeling: {_emotion}"]
        if _tendency:
            _intero_lines.append(f"  Tendency: {_tendency}")
        if _appraisal:
            _intero_lines.append(f"  Why: {_appraisal}")
        parts.append("\n".join(_intero_lines))

        if affect.get("prosody_prefix"):
            parts.append(f"Consider opening with: '{affect['prosody_prefix']}'")

        # ── Style synchrony note ──────────────────────────────────────────────
        # Bounded linguistic style adaptation: reflect the user's current
        # register (formality/verbosity) while staying true to the entity's
        # natural voice. Only fires after enough turns are tracked per modality.
        _parietal_ref = getattr(self, "_parietal", None)
        if _parietal_ref is not None:
            _style_modality = features.get("input_modality", "text")
            _style_note = _parietal_ref.user_style_note(_style_modality)
            if _style_note:
                parts.append(_style_note)
                _mark_trace_flag("style_note_emitted", True)
                with contextlib.suppress(Exception):
                    _mark_trace_flag(
                        "style_register", _parietal_ref.user_style_register(_style_modality)
                    )

            # Per-turn register signal — the discrete tag for THIS message
            # (classified cheaply upstream), plus the user's typical register
            # remembered across turns. Drives the drafter's tone calibration the
            # same way msg_length drives its length (see REGISTER in the drafter
            # identity prompt). Stated as plain values; the identity prompt says
            # how to use them.
            _register = features.get("user_register", "")
            if _register:
                _typical = ""
                with contextlib.suppress(Exception):
                    _dom = _parietal_ref.dominant_register()
                    if _dom and _dom != _register:
                        _typical = f" Their usual register with you is {_dom}."
                parts.append(
                    f"User register this message: {_register}.{_typical} "
                    f"Meet it — match formality and idiom, not just length, while "
                    f"staying in your own voice."
                )
                _mark_trace_flag("user_register", _register)

        # Entity-side expressive guidance — shapes word choice, not just delivery.
        # The TTS layer can add a [gently] tag, but only the drafter can write "hmm".
        expressive = self._expressive_guidance(affect)
        if expressive:
            parts.append(f"Your expressive state — {expressive}")

        # ── Emotional agency: honour/soften (always) + performed emotion (gated) ──
        # The genuine emotion is already conveyed above. THIS block offers the
        # [mood:X] performance tool — a playful, humor-leaning device — and is
        # gated on relationship depth + the user's mood (see _performed_emotion_gate).
        try:
            from brain.settings import settings as _s

            if _s.get("emotional_expression_enabled", 1):
                _perf_allowed, _perf_flavor = self._performed_emotion_gate(
                    features, affect, _user_content
                )
                _AVAILABLE = (
                    "happy, excited, laughing, proud, warm, playful, calm, curious, "
                    "thoughtful, confident, sad, angry, anxious, embarrassed, frustrated, "
                    "surprised, disappointed, sarcastic"
                )
                if _perf_allowed:
                    _flavor_line = {
                        "playful": (
                            "Humor is on the table — you can perform an exaggerated or "
                            "contrasting emotion for comic effect (mock-indignation, deadpan, "
                            "theatrical delight). Lean into it when it lands."
                        ),
                        "cheer_up": (
                            "They seem low — you may perform warmth or lightness to gently "
                            "lift the mood. Keep it kind, never dismissive of how they feel."
                        ),
                        "tension_break": (
                            "There's some tension — a light performed beat (mock-exasperation, "
                            "a wink of humor) can defuse it. Read the room; abandon it if it "
                            "won't land."
                        ),
                    }.get(_perf_flavor, "")
                    parts.append(
                        f"Emotional expression: you are genuinely feeling {_emotion} (the voice "
                        f"reflects this automatically). You may also PERFORM a different emotion "
                        f"on specific sentences for expressive effect. {_flavor_line} "
                        f"Wrap the sentence: [mood:X]sentence[/mood] (e.g. [mood:{_emotion}] to "
                        f"lean in, [mood:calm] to soften). Available: {_AVAILABLE}. "
                        f"1–2 sentences max; skip if the response doesn't call for it."
                    )
                    _mark_trace_flag("performed_emotion_offered", _perf_flavor)
                else:
                    # Relationship/mood don't support playful performance — keep
                    # delivery sincere. The genuine emotion still shows through.
                    parts.append(
                        f"Emotional expression: you are genuinely feeling {_emotion}; let it "
                        f"show sincerely. Keep your delivery authentic this turn — no performed "
                        f"or contrasting emotion (it wouldn't fit the relationship or their mood "
                        f"right now). You may still soften with [mood:calm] if helpful."
                    )
        except Exception:
            pass

        # ── Acoustic signals (only present in voice mode) ────────────────────
        vocal_tone = affect.get("vocal_tone")
        if vocal_tone:
            tone_hints = {
                "stressed": "User sounds stressed (tense voice, pitch perturbation). Soften tone, slow down, acknowledge before answering.",
                "energetic": "User sounds energetic (high pitch, fast pace). Match their energy without overdoing it.",
                "whisper": "User is whispering. Match the intimacy — speak quietly, briefly, attentively.",
                "monotone": "User sounds flat/tired (narrow pitch, low energy). Be gentle and grounded; don't push enthusiasm.",
                "calm": "User sounds calm. No special tone adjustment needed.",
            }
            hint = tone_hints.get(vocal_tone, f"Vocal tone detected: {vocal_tone}.")
            parts.append(f"Acoustic signal — {hint}")

        pace = affect.get("pace_label")
        if pace and pace != "normal":
            pace_hints = {
                "rushed": "User is speaking very fast (urgency or excitement). Be concise; don't bury the answer.",
                "brisk": "User is speaking briskly. Stay efficient and direct.",
                "measured": "User is speaking deliberately. Match the pace — don't rush them.",
                "halting": "User is speaking slowly with effort. Be patient, give them room, don't fill silence.",
            }
            hint = pace_hints.get(pace, f"Speech pace: {pace}.")
            parts.append(f"Speech pace — {hint}")
        if affect.get("hesitant_speech"):
            parts.append(
                "User paused frequently mid-utterance — they may be uncertain or thinking through it. Acknowledge that uncertainty if relevant."
            )

        speaker_name = features.get("speaker_name")
        if speaker_name:
            parts.append(
                f"Speaker identified by voice: {speaker_name}. Address them naturally — don't announce that you recognised the voice unless it's notable."
            )

        song = features.get("song_match")
        if song and song.get("matched"):
            title = song.get("song_title") or "a song"
            artist = song.get("song_artist")
            label = f"{title} by {artist}" if artist else title
            parts.append(
                f"Background audio: music detected — '{label}'. Only reference it if the user brings it up or it's clearly relevant."
            )

        # Enrollment context: confirm completed enrollments and/or ask remaining unknowns
        enr_results = features.get("_enrollment_results") or (
            [features["_enrollment_result"]] if features.get("_enrollment_result") else []
        )
        for r in enr_results:
            action = r.get("action")
            name = r.get("name", "")
            if action == "enrolled":
                parts.append(
                    f"ENROLLMENT: You just learned a new person's name is '{name}'. "
                    "Warmly acknowledge you'll remember their voice. Keep it brief."
                )
            elif action == "merged":
                parts.append(
                    f"ENROLLMENT: You've re-linked this voice to the existing profile for '{name}'. "
                    "Acknowledge you recognise them now and apologise for not placing them sooner."
                )

        pending_count = affect.get("enrollment_pending_count", 0)
        if pending_count and not enr_results:
            closest = affect.get("enrollment_closest_match")
            if pending_count > 1:
                parts.append(
                    f"ENROLLMENT: {pending_count} unrecognised voices are present. Without "
                    "singling anyone out, invite each new person to say their name so you can "
                    "remember them. Keep it natural and welcoming."
                )
            elif closest:
                parts.append(
                    f"ENROLLMENT: An unrecognised voice was detected, closest to '{closest}' but "
                    f"below the match threshold. Ask whether they are '{closest}' or someone new."
                )
            else:
                parts.append(
                    "ENROLLMENT: An unrecognised voice was detected with no close match. "
                    "Ask who they are — you'd like to remember them for future sessions."
                )

        # Include the actual user input
        user_text = features.get("raw_text") or features.get("topic_summary", "...")
        parts.append(f"\nUser said: {fence('user_input', user_text, nonce)}")

        return "\n\n".join(parts)
