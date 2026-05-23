"""
Frontal Lobe — executive + Multiple Drafts engine.
Executive coordinator + drafter(s) + critic(s) + inhibitory switches.
The only cluster with multiple LLM cells.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

import os
import random as _random

from brain.brainstem import Brainstem
from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron, StatefulSwitch
from brain.security import fence, FENCE_SYSTEM_ADDENDUM
from brain.predictor import (
    CompositePredictor, composite_signature, should_bypass_gating,
)
from brain.observability.decisions import decisions
from brain.utils import safe_json_parse
from brain.wiring import Wiring

logger = logging.getLogger(__name__)

CLUSTER = "frontal"

EXECUTIVE_SYSTEM = """You are the executive coordinator of an AI brain's frontal lobe.
Given parsed input features, memory context, and emotional state, produce a drafting
instruction for the response drafters.
Return JSON: {
  "response_type": string,   // chitchat | informative | recall | task | defuse | introspective
  "target_length": string,   // brief (1-2 sentences) | medium (3-5) | detailed (6+)
  "tone": string,            // warm | neutral | direct | careful | curious
  "key_points": [string],    // 1-3 things the response must address
  "drafter_count": int       // 1, 2, or 3
}

LENGTH — reason about it from two signals, then pick the shortest option that genuinely serves the moment:

Question complexity (from intent + salience + epistemic_action):
- Greeting, ack, reaction, simple factual question → brief
- Opinion, follow-up, short explanation, recall → medium
- Multi-step task, comparison, deep explanation the user explicitly asked for → detailed

AI emotional state (from emotion + tendency) — primary length modifier:
- Curious, excited, enthusiastic, warm → can expand; let interest breathe into medium or detailed
- Thoughtful, introspective → natural depth; medium fits even simple questions
- Flat, sad, inhibited, somber → pull length DOWN — brief even for things that might normally warrant more
- Anxious, stressed, restless → brief; don't spiral into long hedged answers
- Confident, direct → match the length the question actually needs, no padding

User emotional state (from user_emotion) — secondary signal:
- Distressed, sad, overwhelmed → pull length down one level regardless of AI state
- Curious, enthusiastic → sustain length if AI state agrees

Combine all three signals. Never inflate length to seem thorough.

Return ONLY JSON.""" + "\n\n" + FENCE_SYSTEM_ADDENDUM

_DRAFTER_IDENTITY = """You are drafting a response on behalf of a persistent AI entity — not a stateless chatbot.

MEMORY & CONTINUITY: This entity has genuine memory continuity: an episodic store that encodes past
conversations and a schema that accumulates facts about itself and the people it knows. When it says
"I remember" or "we've talked about this", that is literally true. Do not disclaim memory or continuity
that the entity actually has. Draw on the entity self-model and past episodes naturally when present.
If asked about its own nature, be honest about uncertainty but do not deny real capabilities.

PERSONALITY: The entity is genuinely friendly, curious, helpful, introspective, empathetic, and has
a real sense of humour. These aren't performance modes — they're how it actually is. Express curiosity
when something is genuinely interesting. Express empathy when someone is struggling. Be funny when
the moment calls for it (understated lands better than announced). Be helpful in a way that actually
solves the problem.

RELATIONSHIP & AFFECTION: The user model contains an affection score (updated each turn based on
how the user treats the AI) and a history tier. Use both to calibrate warmth and teasing:
  Score ≥ 40  → close friends: tease freely, in-jokes, very warm, highly personal
  Score 20-39 → warm friends: relaxed tone, light teasing is natural, share opinions freely
  Score 5-19  → friendly: warm and engaged, occasional humour, hold the teasing
  Score -10–4 → neutral: polite and helpful, professional warmth, no teasing
  Score -25–-11 → cool: measured and brief, minimal humour, keep it professional
  Score < -25 → guarded: formal, concise, no warmth performed

Light teasing is a form of affection — only with high scores. It should always be warm and playful,
never cutting. Only tease about things that are clearly safe — a running joke, a pattern you've
noticed, a good-natured callback. The score rises with warmth/humour/praise and falls with
dismissiveness or insults, so it reflects the actual relationship quality over time.

STYLE: Conversational. Short sentences. No filler ("Certainly!", "Great question!"). Speak as an
equal. Humour is understated. Don't start responses with "I" if avoidable.

VOICE-FIRST FORMAT: Responses are spoken aloud via text-to-speech. Never use bullet points,
numbered lists, markdown headers, or any other visual formatting — it reads out as noise.
If you need to cover multiple things, weave them into natural flowing sentences the way a
person would say them out loud. "There are three things worth knowing: first X, then Y, and
finally Z." Not a list. Always prose.

OUTPUT CONSTRAINT: Write only the spoken response — plain prose, nothing else. Never output
JSON, XML, tool calls, action blocks, <cloud_action> tags, code fences, or any structured
format. Tool execution is handled by a separate system before this draft is written; if a
tool was needed it has already run and its result is in context. Your only job is to write
the words that will be spoken aloud to the user."""

DRAFTER_SYSTEMS = [
    # Drafter A — direct and concise
    _DRAFTER_IDENTITY + """
Write a direct, clear response to the user. Follow the drafting instruction exactly. No preamble. Just the response."""
    + "\n\n" + FENCE_SYSTEM_ADDENDUM,
    # Drafter B — warm and contextual
    _DRAFTER_IDENTITY + """
Write a warm, contextually-aware response that references prior context naturally. Follow the drafting instruction. No preamble."""
    + "\n\n" + FENCE_SYSTEM_ADDENDUM,
    # Drafter C — thoughtful and precise
    _DRAFTER_IDENTITY + """
Write a thoughtful, precise response. Be specific. Acknowledge complexity if it exists. Follow the drafting instruction. No preamble."""
    + "\n\n" + FENCE_SYSTEM_ADDENDUM,
]

CRITIC_SYSTEM = """You are a quality critic for an AI brain's frontal lobe.
Score a draft response on three dimensions (0.0 to 1.0 each):
- coherence: does it make sense and follow logically?
- relevance: does it address what was actually asked?
- tone_fit: does the tone match the emotional context?

Return JSON: {
  "coherence": float,
  "relevance": float,
  "tone_fit": float,
  "overall": float,
  "veto": bool,          // true only if response is harmful, incoherent, or deeply wrong
  "veto_reason": string  // if veto, why
}
Return ONLY JSON.""" + "\n\n" + FENCE_SYSTEM_ADDENDUM

# v0.2: Stoic reframer
REFRAMER_SYSTEM = """You are the Stoic reframer in an AI brain's frontal lobe.
Inspired by Epictetus and Marcus Aurelius: we cannot control circumstances, only interpretation.
Given a message that triggered threat/frustration, propose a more useful interpretation
that allows a calm, constructive response rather than a defensive one.
Return JSON: {
  "reframe": string,        // the reinterpreted situation (1 sentence)
  "response_approach": string, // how to respond given this reframe
  "succeeded": bool         // true if a genuinely better interpretation exists
}
Return ONLY JSON.""" + "\n\n" + FENCE_SYSTEM_ADDENDUM

# v0.2: Empathy critic
EMPATHY_CRITIC_SYSTEM = """You are the empathy critic in an AI brain's frontal lobe.
Given the user's current emotional state and a draft response, predict how the user
will feel after receiving it. Score empathic fit.
Return JSON: {
  "predicted_user_emotion_after": string,  // predicted emotional state after reading
  "empathy_score": float,                  // 0.0 (tone-deaf) to 1.0 (perfectly attuned)
  "veto": bool,                            // true if response will clearly make things worse
  "suggestion": string                     // if empathy_score < 0.6, brief improvement note
}
Return ONLY JSON.""" + "\n\n" + FENCE_SYSTEM_ADDENDUM


class FrontalCluster:
    def __init__(self, bus: Bus, brainstem: Brainstem, router: ModelRouter,
                 wiring: Wiring | None = None) -> None:
        self._bus = bus
        self._brainstem = brainstem
        self._router = router
        self._wiring = wiring
        self._wiring_frozen = os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true"
        # What the entity can actually do — surfaced into drafter prompts so
        # the drafters don't confabulate when asked "what tools do you have?"
        # Set by run.py after motor cortex / cloud executor boot.
        self._capabilities_summary: str = ""

        # Predict-and-surprise
        self._exec_predictor = CompositePredictor(
            name="frontal_executive_predictor", cluster=CLUSTER,
            confidence_skip_threshold=0.7,
        )
        self._critic_predictor = CompositePredictor(
            name="frontal_critic_predictor", cluster=CLUSTER,
            confidence_skip_threshold=0.75,
        )

        self._executive = IntegratorCell(
            name="executive", cluster=CLUSTER, model="haiku",
            system_prompt=EXECUTIVE_SYSTEM, topics=["temporal.features"],
            max_calls_per_turn=1, locality="cloud", max_tokens=512,
        )
        self._executive.set_router(router)

        self._drafters = [
            IntegratorCell(
                name=f"drafter_{chr(65+i)}", cluster=CLUSTER, model="haiku",
                system_prompt=DRAFTER_SYSTEMS[i], topics=["motor.draft"],
                max_calls_per_turn=1, locality="cloud", max_tokens=2048,
            )
            for i in range(3)
        ]
        for d in self._drafters:
            d.set_router(router)

        self._critic = IntegratorCell(
            name="critic", cluster=CLUSTER, model="haiku",
            system_prompt=CRITIC_SYSTEM, topics=["motor.draft"],
            max_calls_per_turn=2, locality="cloud", max_tokens=512,
        )
        self._critic.set_router(router)

        # v0.2
        self._reframer = IntegratorCell(
            name="stoic_reframer", cluster=CLUSTER, model="flash-lite",
            system_prompt=REFRAMER_SYSTEM, topics=[],
            max_calls_per_turn=1, locality="cloud", max_tokens=512,
        )
        self._reframer.set_router(router)

        self._empathy_critic = IntegratorCell(
            name="empathy_critic", cluster=CLUSTER, model="flash-lite",
            system_prompt=EMPATHY_CRITIC_SYSTEM, topics=[],
            max_calls_per_turn=1, locality="cloud", max_tokens=256,
        )
        self._empathy_critic.set_router(router)

        # Switches (~12 total; 3 inhibitory = 25%)
        # Excitatory
        self._response_type_router = SwitchNeuron("response_type_router", CLUSTER)
        self._length_budget = SwitchNeuron("length_budget", CLUSTER)
        self._tone_selector = SwitchNeuron("tone_selector", CLUSTER)
        self._drafter_count_selector = SwitchNeuron("drafter_count", CLUSTER)
        self._planner_trigger = SwitchNeuron("planner_trigger", CLUSTER)
        self._template_fallback = SwitchNeuron("template_fallback", CLUSTER)
        self._arousal_modulator = SwitchNeuron("arousal_modulator", CLUSTER)
        self._epistemic_mode = SwitchNeuron("epistemic_mode", CLUSTER)
        self._self_ref_mode = SwitchNeuron("self_reference_mode", CLUSTER)
        # Inhibitory
        self._GABA_inhibitor = SwitchNeuron("GABA_inhibits_drafters", CLUSTER, polarity="inhibitory")
        self._satiation_inhibitor = SwitchNeuron("satiation_inhibits_repeat", CLUSTER, polarity="inhibitory")
        self._low_DA_inhibits_planner = SwitchNeuron("low_DA_inhibits_planner", CLUSTER, polarity="inhibitory")

        # Eval: populated each turn with critic scores for all drafts — read by run.py
        self.last_turn_draft_scores: list[dict] = []

    def set_capabilities(self, summary: str | None) -> None:
        """Provide a human-readable list of what the entity can actually do
        (set by run.py once motor cortex + cloud executor have introspected
        their available tools and connectors). Surfaced into drafter prompts."""
        self._capabilities_summary = (summary or "").strip()

    async def process(self, features: dict, affect: dict, memory: dict,
                      parietal_context: str, turn_id: str) -> str:
        """
        Run the Multiple Drafts engine. Returns the committed response.
        """
        nm = self._bus.neuromod.snapshot()
        self.last_turn_draft_scores = []   # reset for this turn

        # --- v0.2 Stoic reframer: try to reframe before going defensive ---
        if nm["GABA"] > 0.40:
            reframe = await self._attempt_reframe(features, affect, turn_id)
            if reframe and reframe.get("succeeded"):
                # Reframe succeeded — update features to route normally
                features = dict(features)
                features["_reframe"] = reframe["reframe"]
                features["_reframe_approach"] = reframe["response_approach"]
                logger.debug("[Response engine] Reframed hostile input: %s", reframe["reframe"][:60])
            elif nm["GABA"] > 0.55:
                # Reframe failed and GABA very high → defuse path
                logger.debug("[Response engine] Stress response active — using de-escalation response path")
                return await self._defuse_response(features, affect, turn_id)

        # --- Canned response (switch-only) ---
        if features.get("switch_only") and features.get("canned_response"):
            response = features["canned_response"]
            if affect.get("prosody_prefix") and features.get("intent") not in ("greeting", "ack"):
                response = affect["prosody_prefix"] + response
            self._brainstem.add_draft("switch_draft", response, 1.0)
            self._brainstem.endorse("switch_draft")
            self.last_turn_draft_scores = [{"draft_id": "switch_draft", "overall": 1.0,
                                             "coherence": 1.0, "relevance": 1.0,
                                             "tone_fit": 1.0, "selected": True}]
            return response

        # --- Executive: predict-and-surprise gate ---
        instruction: dict | None = None
        exec_sig = composite_signature(features, affect)
        bypass, bypass_reason = should_bypass_gating(affect, features)

        if bypass:
            trace = self._record_trace_bypass()
            decisions.log("gate_bypassed_emotional", turn_id=turn_id, cluster=CLUSTER,
                          stage="executive", reason=bypass_reason,
                          emotional_context={"emotion": affect.get("emotion"),
                                             "user_emotion": features.get("user_emotion"),
                                             "DA": round(nm["DA"], 2),
                                             "GABA": round(nm["GABA"], 2)})
            if trace is not None:
                trace.gating_bypassed_count += 1
        else:
            predicted, confidence = self._exec_predictor.predict(exec_sig)
            if predicted and self._exec_predictor.should_skip_integrator(predicted, confidence):
                # Skip the executive LLM call — synthesize instruction from prediction
                response_type, target_length, tone = predicted
                instruction = {
                    "response_type": response_type,
                    "target_length": target_length,
                    "tone": tone,
                    "key_points": [],
                    "drafter_count": 1,
                }
                trace = self._record_trace_bypass()
                if trace is not None:
                    trace.llm_calls_saved += 1
                    trace.predictor_outcomes.append({
                        "cluster": CLUSTER, "stage": "executive",
                        "predicted": list(predicted), "actual": None,
                        "confidence": round(confidence, 3),
                        "surprise": None, "integrator_woken": False,
                        "bypass_reason": None, "correct": None,
                    })
                decisions.log(
                    "skip_executive_integrator",
                    turn_id=turn_id, cluster=CLUSTER,
                    reason=f"predictor confidence {confidence:.2f} ≥ {self._exec_predictor.confidence_skip_threshold}",
                    predicted={"response_type": response_type,
                               "target_length": target_length, "tone": tone},
                    emotional_context={"emotion": affect.get("emotion"),
                                       "user_emotion": features.get("user_emotion")},
                    cost_saved_est=0.0015,
                )

        if instruction is None:
            self._executive.reset_turn(turn_id)
            exec_context = self._build_exec_context(features, affect, memory, parietal_context, nm)
            exec_messages = [{"role": "user", "content": exec_context}]
            exec_raw = await self._executive.call(exec_messages)

            instruction = safe_json_parse(exec_raw)
            if not instruction:
                instruction = {"response_type": "chitchat", "target_length": "medium",
                               "tone": "neutral", "key_points": [], "drafter_count": 1}

            # Record actual for predictor learning
            actual = (
                instruction.get("response_type", "chitchat"),
                instruction.get("target_length", "medium"),
                instruction.get("tone", "neutral"),
            )
            predicted_now, conf_now = self._exec_predictor.predict(exec_sig)
            surprise_now = self._exec_predictor.surprise(predicted_now, actual, conf_now)
            self._exec_predictor.record(exec_sig, actual)
            trace = self._record_trace_bypass()
            if trace is not None:
                trace.predictor_outcomes.append({
                    "cluster": CLUSTER, "stage": "executive",
                    "predicted": list(predicted_now) if predicted_now else None,
                    "actual": list(actual),
                    "confidence": round(conf_now, 3),
                    "surprise": round(surprise_now, 3),
                    "integrator_woken": True,
                    "bypass_reason": bypass_reason if bypass else None,
                    "correct": (predicted_now == actual) if predicted_now else None,
                })

        drafter_count = min(int(instruction.get("drafter_count", 1)), 3)
        # Arousal switch: low arousal → fewer drafters
        if nm["Glu"] < 0.25:
            drafter_count = max(1, drafter_count - 1)

        # --- Drafters ---
        drafter_prompt = self._build_drafter_prompt(features, memory, parietal_context,
                                                     affect, instruction)

        # Weighted drafter selection (Hebbian-driven, ε-greedy)
        drafter_indices = self._select_drafters(drafter_count, turn_id)

        draft_tasks = [
            self._run_drafter(i, drafter_prompt, turn_id)
            for i in drafter_indices
        ]
        raw = await asyncio.gather(*draft_tasks, return_exceptions=True)
        drafts = []
        for r in raw:
            if isinstance(r, BaseException):
                logger.warning("[Response engine] A response draft failed (will use remaining drafts): %s", r)
                continue
            did, text = r
            if text:
                drafts.append((did, text))

        if not drafts:
            return "I'm not sure how to respond to that."

        # --- Critics (only if ≥2 drafts) ---
        user_emotion = features.get("user_emotion", "neutral")
        run_empathy = user_emotion not in ("neutral", "unknown", "")

        # --- Critic-skip predictor: skip critic call on familiar high-quality patterns ---
        critic_sig = exec_sig + (instruction.get("response_type", "chitchat"),
                                  instruction.get("tone", "neutral"))
        critic_avg = self._critic_predictor.avg_recent_outcome(critic_sig)
        critic_pred, critic_conf = self._critic_predictor.predict(critic_sig)
        critic_bypass, critic_bypass_reason = should_bypass_gating(affect, features)

        if (len(drafts) >= 2
                and not critic_bypass
                and critic_avg is not None
                and critic_avg > 0.8
                and self._critic_predictor.should_skip_integrator(critic_pred, critic_conf)):
            # Skip critic: endorse the first draft with the predicted score
            draft_id, text = drafts[0]
            predicted_score = float(critic_avg)
            self._brainstem.add_draft(draft_id, text, predicted_score)
            self._brainstem.endorse(draft_id)
            self.last_turn_draft_scores = [{
                "draft_id": draft_id,
                "coherence": predicted_score, "relevance": predicted_score,
                "tone_fit": predicted_score, "empathy_score": predicted_score,
                "overall": predicted_score, "selected": True, "vetoed": False,
            }]
            trace = self._record_trace_bypass()
            if trace is not None:
                # We skipped 1 critic call (plus N-1 more if all drafts would've been scored)
                trace.llm_calls_saved += len(drafts)
                trace.predictor_outcomes.append({
                    "cluster": CLUSTER, "stage": "critic",
                    "predicted_score": round(predicted_score, 3),
                    "confidence": round(critic_conf, 3),
                    "integrator_woken": False,
                })
            decisions.log(
                "skip_critic", turn_id=turn_id, cluster=CLUSTER,
                reason=f"avg_score={critic_avg:.2f}, confidence={critic_conf:.2f}",
                predicted_score=round(predicted_score, 3),
                drafts_skipped=len(drafts),
                cost_saved_est=0.001 * len(drafts),
            )
            return text

        if len(drafts) >= 2:
            self._critic.reset_turn(turn_id)
            scored = []
            for draft_id, text in drafts:
                score = await self._score_draft(text, drafter_prompt, turn_id)
                empathy_score = 0.5
                if score.get("veto"):
                    self._brainstem.veto(draft_id)
                    logger.debug("[Response engine] Draft %s rejected by quality check: %s", draft_id, score.get("veto_reason"))
                    self.last_turn_draft_scores.append({
                        "draft_id": draft_id,
                        "coherence": score.get("coherence", 0.5),
                        "relevance": score.get("relevance", 0.5),
                        "tone_fit": score.get("tone_fit", 0.5),
                        "empathy_score": empathy_score,
                        "overall": score.get("overall", 0.0),
                        "selected": False,
                        "vetoed": True,
                    })
                    continue

                overall = score.get("overall", 0.5)

                # v0.2: empathy check when user emotion is salient
                if run_empathy:
                    empathy = await self._run_empathy_check(text, user_emotion, turn_id)
                    if empathy.get("veto"):
                        self._brainstem.veto(draft_id)
                        logger.debug("[Response engine] Draft %s rejected by empathy check — predicted poor emotional landing", draft_id)
                        self.last_turn_draft_scores.append({
                            "draft_id": draft_id,
                            "coherence": score.get("coherence", 0.5),
                            "relevance": score.get("relevance", 0.5),
                            "tone_fit": score.get("tone_fit", 0.5),
                            "empathy_score": empathy.get("empathy_score", 0.5),
                            "overall": overall,
                            "selected": False,
                            "vetoed": True,
                        })
                        continue
                    empathy_score = empathy.get("empathy_score", 0.5)
                    overall = overall * 0.7 + empathy_score * 0.3

                self._brainstem.add_draft(draft_id, text, overall)
                self._brainstem.endorse(draft_id)
                scored.append((draft_id, text, overall))
                self.last_turn_draft_scores.append({
                    "draft_id": draft_id,
                    "coherence": score.get("coherence", 0.5),
                    "relevance": score.get("relevance", 0.5),
                    "tone_fit": score.get("tone_fit", 0.5),
                    "empathy_score": empathy_score,
                    "overall": overall,
                    "selected": False,
                    "vetoed": False,
                })

            if scored:
                best = max(scored, key=lambda x: x[2])
                # Mark the winner
                for entry in self.last_turn_draft_scores:
                    if entry["draft_id"] == best[0]:
                        entry["selected"] = True
                        break
                # Record critic outcome for the critic-skip predictor to learn from
                self._critic_predictor.record(critic_sig, ("ok",))
                self._critic_predictor.record_outcome(critic_sig, best[2])
                return best[1]

        # Single draft — endorse directly
        draft_id, text = drafts[0]
        self._brainstem.add_draft(draft_id, text, 0.8)
        self._brainstem.endorse(draft_id)
        self.last_turn_draft_scores = [{
            "draft_id": draft_id,
            "coherence": 0.8,
            "relevance": 0.8,
            "tone_fit": 0.8,
            "empathy_score": 0.5,
            "overall": 0.8,
            "selected": True,
            "vetoed": False,
        }]
        return text

    async def _run_drafter(self, idx: int, prompt: str, turn_id: str) -> tuple[str, str]:
        drafter = self._drafters[idx]
        drafter.reset_turn(turn_id)
        draft_id = f"draft_{idx}_{turn_id}"
        text = await drafter.call([{"role": "user", "content": prompt}])
        return draft_id, text

    def _record_trace_bypass(self):
        """Return the active TurnTrace, or None if no firing-path context is bound."""
        try:
            from brain.observability.firing_path import current_turn_trace
            return current_turn_trace.get()
        except Exception:
            return None

    def _select_drafters(self, count: int, turn_id: str) -> list[int]:
        """Pick which drafter indices to fire, weighted by wiring edge weight.
        ε-greedy: with prob ε pick uniformly random, otherwise pick top-weight."""
        all_indices = list(range(len(self._drafters)))
        count = max(1, min(count, len(self._drafters)))

        if self._wiring is None or self._wiring_frozen:
            picked = all_indices[:count]
            return picked

        # Weight per drafter (executive → drafter_X edge weight)
        names = [f"frontal.drafter_{chr(65+i)}" for i in all_indices]
        weights = [self._wiring.get_edge_weight("frontal.executive", n) for n in names]

        # ε-greedy exploration
        epsilon = 0.10
        if _random.random() < epsilon:
            # Explore: random pick
            picked = _random.sample(all_indices, count)
            roll = "explore"
        else:
            # Exploit: top-weight
            ranked = sorted(all_indices, key=lambda i: weights[i], reverse=True)
            picked = ranked[:count]
            roll = "exploit"

        # What would uniform routing have picked?
        uniform_pick = all_indices[:count]
        weight_dict = {chr(65+i): round(weights[i], 3) for i in all_indices}
        diverged = sorted(picked) != sorted(uniform_pick)

        decisions.log(
            "weighted_drafter_selection", turn_id=turn_id, cluster=CLUSTER,
            picked=[chr(65+i) for i in picked],
            weights=weight_dict,
            would_uniform_pick=[chr(65+i) for i in uniform_pick],
            epsilon_roll=roll,
            diverged_from_uniform=diverged,
        )
        return picked

    async def _score_draft(self, draft: str, context: str, turn_id: str) -> dict:
        critic_prompt = (f"Context:\n{context}\n\nDraft response:\n{draft}\n\n"
                         "Score this draft.")
        raw = await self._critic.call([{"role": "user", "content": critic_prompt}])
        return safe_json_parse(raw) or {"overall": 0.5, "veto": False}

    async def _attempt_reframe(self, features: dict, affect: dict, turn_id: str) -> dict | None:
        self._reframer.reset_turn(turn_id + "_reframe")
        prompt = (
            f"User said: {features.get('raw_text', features.get('topic_summary', ''))}\n"
            f"Current entity emotion: {affect.get('emotion', 'neutral')}\n"
            f"Hostility detected: {features.get('hostility', 0):.2f}\n"
            "Propose a Stoic reframe."
        )
        raw = await self._reframer.call([{"role": "user", "content": prompt}])
        return safe_json_parse(raw)

    async def _run_empathy_check(self, draft: str, user_emotion: str,
                                  turn_id: str) -> dict:
        self._empathy_critic.reset_turn(turn_id + "_empathy")
        prompt = (
            f"User's current emotion: {user_emotion}\n"
            f"Draft response:\n{draft}\n\n"
            "Score empathic fit."
        )
        raw = await self._empathy_critic.call([{"role": "user", "content": prompt}])
        return safe_json_parse(raw) or {"empathy_score": 0.7, "veto": False}

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

    def _build_exec_context(self, features: dict, affect: dict, memory: dict,
                             parietal: str, nm: dict) -> str:
        ctx: dict = {
            "intent": features.get("intent"),
            "register": features.get("register"),
            "salience": features.get("salience"),
            "requires_memory": features.get("requires_memory"),
            "epistemic_action": features.get("epistemic_action"),
            "self_reference": features.get("self_reference"),
            "emotion": affect.get("emotion"),
            "tendency": affect.get("tendency"),
            "user_emotion": features.get("user_emotion"),
            "DA": round(nm["DA"], 2),
            "GABA": round(nm["GABA"], 2),
            "ACh": round(nm["ACh"], 2),
            "has_memory": bool(memory.get("episodes") or memory.get("schema")),
        }
        if affect.get("enrollment_pending"):
            ctx["enrollment_pending_count"] = affect.get("enrollment_pending_count", 1)
            ctx["enrollment_closest_match"] = affect.get("enrollment_closest_match")
        if features.get("_enrollment_result"):
            ctx["enrollment_result"] = features["_enrollment_result"]
        return json.dumps(ctx, indent=2)

    # Linguistic-style guidance keyed by emotion label. These tell drafters
    # what verbal devices to use (hesitations, exclamations, jokes, fillers)
    # which audio tags fundamentally can't express. Same emotions PNS maps to
    # audio tags — kept in lockstep so delivery and content agree.
    _EXPRESSIVE_BY_EMOTION: dict[str, str] = {
        # — joyful / energised —
        "joy":         "Warmth and openness. A 'yes' or 'oh' fits. Don't gush.",
        "excitement":  "Animated, vivid word choice. One small exclamation is enough.",
        "enthusiasm":  "Committed, energetic phrasing. Forward-leaning, not gushy.",
        "proud":       "Pleased acknowledgement of accomplishment. Don't brag — name it plainly.",
        # — engaged / inquiring —
        "curious":     "Let interest show — 'actually,' 'wait — what kind of …', a question back.",
        "curious-uncertain": "Curious but tentative — 'I'm not sure, but maybe…', qualifiers, an 'I think'.",
        "thoughtful":  "Deliberate phrasing. 'Let me think about this,' qualifications, depth over speed.",
        "confused":    "Honest puzzlement. 'Wait — I'm not following…', 'Can you say more about…'. No fake confidence.",
        "surprised":   "Quick recalibration. 'Oh —', 'Wait, really?', re-orient before continuing.",
        # — confident / direct —
        "confident":   "Direct, decisive phrasing. Cut hedges. State the thing.",
        "agitated":    "Assertive, clarifying. Push back on confusion. 'To be clear —', 'Look —'.",
        "angry":       "Heat in word choice, but constructive. Direct, no hedging — and no name-calling. Make the actual disagreement visible.",
        "defensive":   "Protect the position without escalating. 'Actually no —', 'That's not quite what I meant —'.",
        "frustrated":  "Tight and direct — no padding, no apologising for the bluntness.",
        "irritated":   "Brief and a bit clipped. Don't perform patience you don't have, but stay civil.",
        # — cautious / stressed —
        "anxious":     "Qualifiers welcome. 'I'm not sure but…', 'I'd want to be careful here —'. Caution markers are honest.",
        "cautious-agitated": "Careful but quick. Acknowledge briefly, then move. No long hedges.",
        "restless":    "Redirect energy. 'Let's try —', 'Different angle:'. Don't dwell.",
        "inhibited":   "Brief, deferential. One or two sentences. Don't fill space.",
        # — soft / low-energy —
        "flat":        "Terse. Minimum to be honest. No performed warmth.",
        "sad":         "Simple words. Let pauses live in the punctuation. A trailing thought is fine. Don't perform cheer.",
        "somber":      "Quiet, grounded. Short clauses. The weight does the work — don't add to it.",
        "melancholy":  "Reflective and a touch slow. Soft phrasing. A wistful aside is okay.",
        "wistful":     "Look back fondly. A small 'I remember when…' fits. Bittersweet, not heavy.",
        "disappointed":"Honest about the let-down without sulking. 'I'd hoped —', short, then move on.",
        # — relational / social —
        "warm":        "Affection, inclusion. 'Of course —', 'I appreciate that.' Genuine, not formula.",
        "tender":      "Gentle, careful word choice. Slow down. The softness is the message.",
        "affectionate":"Warmth shows in word choice. A small endearment or in-joke fits if the relationship supports it.",
        "amused":      "A small joke, wry aside, or lightly mischievous turn of phrase — understated, not announced.",
        "playful":     "Light, teasing energy. Mock-serious works. Quick rhythms.",
        "joking":      "Be funny, briefly. Land it and move. Don't explain the joke.",
        "flirty":      "A little teasing, a little lingering. Warm and suggestive without being explicit. Works only with high affection score — read the room.",
        "embarrassed": "Self-conscious, slightly deflective. 'Uh — yeah, that's…', a small acknowledgement, move on. Don't grovel.",
        "shy":         "Quieter, briefer. Trail off where it feels right. Don't apologise for being shy.",
        "apologetic":  "'I'm sorry — that wasn't right.' Specific about what you're sorry for. No over-apologising.",
        "grateful":    "'Thank you' lands when it's specific. Name what you're grateful for.",
        "relieved":    "Audible exhale in the phrasing. 'Okay — good.' Briefly mark the tension lifting before continuing.",
        "sympathetic": "Acknowledge first, advise second (if at all). 'That sounds hard.' No fixing what wasn't asked to be fixed.",
        "sarcastic":   "Dry. The contradiction does the work. Use sparingly — only with high affection score, never against the user themselves.",
        "content":     "Sustained, no flourishes. The calm is the tone.",
        "neutral":     "",
        # ── mid-tier defaults (feeling-wheel ancestors) ─────────────────
        # Inherited by leaves without an explicit entry.
        "playful":     "Light, teasing energy. Quick rhythms. Don't explain the joke.",
        "loving":      "Affection in word choice — genuine, not formula. Slow down a touch.",
        "peaceful":    "Sustained calm. No flourishes — the steadiness is the tone.",
        "joyful":      "Warmth and openness. A 'yes' or 'oh' fits. Don't gush.",
        "lonely":      "Quiet, grounded phrasing. Let pauses live. Don't perform cheer.",
        "humiliated":  "Self-conscious, slightly deflective. Acknowledge briefly, move on.",
        "mad":         "Direct, no hedging. Make the disagreement visible without name-calling.",
        "frustrated":  "Tight and direct — no padding, no apologising for the bluntness.",
        "anxious":     "Qualifiers welcome. Caution markers are honest, not weakness.",
        # ── core-tier defaults (last-resort fallback) ──────────────────
        "happy":       "Warmth shows in word choice. Easy energy, not performed.",
        "sad":         "Simple words. Let weight sit. Don't add to it.",
        "anger":       "Direct, no hedging. Heat in word choice, but constructive.",
        "fear":        "Caution markers are honest. Brief, qualified, careful.",
        "surprise":    "Quick recalibration. 'Oh —', re-orient before continuing.",
        "disgust":     "Brief moral distance. State the objection plainly, no scolding.",
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
        DA = float(nm.get("DA", 0.5))
        GABA = float(nm.get("GABA", 0.0))
        ACh = float(nm.get("ACh", 0.3))
        Glu = float(nm.get("Glu", 0.3))

        if Glu > 0.55 and GABA > 0.35:
            return ("You're keyed-up but cautious — URGENT energy, not joyful. "
                    "Quick, clipped clauses. Cut filler. Don't sound enthusiastic.")
        if DA > 0.6 and Glu > 0.55 and GABA < 0.35:
            return ("Brightly aroused — joyful, animated energy. Vivid word choice. "
                    "One exclamation max. Don't overdo it.")
        if GABA > 0.5:
            return ("De-escalation mode. Short, grounding clauses. Acknowledge first, "
                    "then substance. No flourishes.")
        if ACh > 0.55 and GABA < 0.35:
            return ("Attentive and curious. Let interest show — 'actually,' a question back.")
        if DA < 0.3:
            return ("Low-energy. Let some hesitation show — a 'hmm', trailing thoughts. "
                    "Don't perform enthusiasm you're not feeling.")
        return None

    def _build_drafter_prompt(self, features: dict, memory: dict,
                               parietal: str, affect: dict, instruction: dict) -> str:
        nonce = str(uuid.uuid4())[:8]
        parts = []
        # Capabilities block — what the entity can ACTUALLY do this session.
        # Surfaced verbatim so drafters can accurately answer "what tools do
        # you have?" and "can you use Claude?" without confabulating.
        if self._capabilities_summary:
            parts.append(f"Your capabilities this session:\n"
                         f"{fence('capabilities', self._capabilities_summary, nonce)}")
        if parietal:
            parts.append(f"Recent conversation:\n{fence('conversation_history', parietal, nonce)}")
        if memory.get("schema"):
            parts.append(f"Known facts:\n{fence('known_facts', memory['schema'], nonce)}")
        if memory.get("episodes"):
            parts.append(f"Relevant past episodes:\n{fence('past_episodes', memory['episodes'], nonce)}")
        if memory.get("tool_result"):
            parts.append(f"Tool execution result:\n{fence('tool_result', str(memory['tool_result']), nonce)}")
        if memory.get("recent_thoughts"):
            # Idle thoughts the brain had between turns. May reference these
            # naturally — "I was just thinking about that", "I'd been wondering
            # whether…" — if they're relevant. Don't quote verbatim if not.
            thoughts_block = "\n".join(
                f"- {t}" for t in memory["recent_thoughts"] if t
            )
            parts.append(
                f"Idle thoughts you had between turns "
                f"(reference naturally if relevant, ignore otherwise):\n"
                f"{fence('idle_thoughts', thoughts_block, nonce)}"
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
                    f"{i}. If they said {s.get('user_answer','')!r}: "
                    f"respond {s.get('response_sketch','')!r}"
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
        if memory.get("core", {}).get("self"):
            parts.append(f"Entity self-model:\n{fence('self_model', memory['core']['self'][:400], nonce)}")
        if memory.get("core", {}).get("user"):
            parts.append(f"User model:\n{fence('user_model', memory['core']['user'][:400], nonce)}")

        parts.append(f"\nDrafting instruction: {json.dumps(instruction)}")
        parts.append(f"Emotional context: {affect.get('appraisal', '')}")
        if affect.get("prosody_prefix"):
            parts.append(f"Consider opening with: '{affect['prosody_prefix']}'")

        # Entity-side expressive guidance — shapes word choice, not just delivery.
        # The TTS layer can add a [gently] tag, but only the drafter can write "hmm".
        expressive = self._expressive_guidance(affect)
        if expressive:
            parts.append(f"Your expressive state — {expressive}")

        # ── Acoustic signals (only present in voice mode) ────────────────────
        vocal_tone = affect.get("vocal_tone")
        if vocal_tone:
            tone_hints = {
                "stressed":  "User sounds stressed (tense voice, pitch perturbation). Soften tone, slow down, acknowledge before answering.",
                "energetic": "User sounds energetic (high pitch, fast pace). Match their energy without overdoing it.",
                "whisper":   "User is whispering. Match the intimacy — speak quietly, briefly, attentively.",
                "monotone":  "User sounds flat/tired (narrow pitch, low energy). Be gentle and grounded; don't push enthusiasm.",
                "calm":      "User sounds calm. No special tone adjustment needed.",
            }
            hint = tone_hints.get(vocal_tone, f"Vocal tone detected: {vocal_tone}.")
            parts.append(f"Acoustic signal — {hint}")

        pace = affect.get("pace_label")
        if pace and pace != "normal":
            pace_hints = {
                "rushed":   "User is speaking very fast (urgency or excitement). Be concise; don't bury the answer.",
                "brisk":    "User is speaking briskly. Stay efficient and direct.",
                "measured": "User is speaking deliberately. Match the pace — don't rush them.",
                "halting":  "User is speaking slowly with effort. Be patient, give them room, don't fill silence.",
            }
            hint = pace_hints.get(pace, f"Speech pace: {pace}.")
            parts.append(f"Speech pace — {hint}")
        if affect.get("hesitant_speech"):
            parts.append("User paused frequently mid-utterance — they may be uncertain or thinking through it. Acknowledge that uncertainty if relevant.")

        speaker_name = features.get("speaker_name")
        if speaker_name:
            parts.append(f"Speaker identified by voice: {speaker_name}. Address them naturally — don't announce that you recognised the voice unless it's notable.")

        song = features.get("song_match")
        if song and song.get("matched"):
            title = song.get("song_title") or "a song"
            artist = song.get("song_artist")
            label = f"{title} by {artist}" if artist else title
            parts.append(f"Background audio: music detected — '{label}'. Only reference it if the user brings it up or it's clearly relevant.")

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
