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

from brain.brainstem import Brainstem
from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron, StatefulSwitch
from brain.security import fence, FENCE_SYSTEM_ADDENDUM

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
equal. Humour is understated. Don't start responses with "I" if avoidable."""

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
    def __init__(self, bus: Bus, brainstem: Brainstem, router: ModelRouter) -> None:
        self._bus = bus
        self._brainstem = brainstem
        self._router = router

        self._executive = IntegratorCell(
            name="executive", cluster=CLUSTER, model="haiku",
            system_prompt=EXECUTIVE_SYSTEM, topics=["temporal.features"],
            max_calls_per_turn=1, locality="cloud",
        )
        self._executive.set_router(router)

        self._drafters = [
            IntegratorCell(
                name=f"drafter_{chr(65+i)}", cluster=CLUSTER, model="haiku",
                system_prompt=DRAFTER_SYSTEMS[i], topics=["motor.draft"],
                max_calls_per_turn=1, locality="cloud",
            )
            for i in range(3)
        ]
        for d in self._drafters:
            d.set_router(router)

        self._critic = IntegratorCell(
            name="critic", cluster=CLUSTER, model="haiku",
            system_prompt=CRITIC_SYSTEM, topics=["motor.draft"],
            max_calls_per_turn=2, locality="cloud",
        )
        self._critic.set_router(router)

        # v0.2
        self._reframer = IntegratorCell(
            name="stoic_reframer", cluster=CLUSTER, model="flash-lite",
            system_prompt=REFRAMER_SYSTEM, topics=[],
            max_calls_per_turn=1, locality="cloud",
        )
        self._reframer.set_router(router)

        self._empathy_critic = IntegratorCell(
            name="empathy_critic", cluster=CLUSTER, model="flash-lite",
            system_prompt=EMPATHY_CRITIC_SYSTEM, topics=[],
            max_calls_per_turn=1, locality="cloud",
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

    async def process(self, features: dict, affect: dict, memory: dict,
                      parietal_context: str, turn_id: str) -> str:
        """
        Run the Multiple Drafts engine. Returns the committed response.
        """
        nm = self._bus.neuromod.snapshot()

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
            return response

        # --- Executive: build drafting instruction ---
        self._executive.reset_turn(turn_id)
        exec_context = self._build_exec_context(features, affect, memory, parietal_context, nm)
        exec_messages = [{"role": "user", "content": exec_context}]
        exec_raw = await self._executive.call(exec_messages)

        instruction: dict = {}
        try:
            instruction = json.loads(exec_raw)
        except Exception:
            import re
            m = re.search(r'\{.*\}', exec_raw, re.DOTALL)
            if m:
                try:
                    instruction = json.loads(m.group(0))
                except Exception:
                    pass
        if not instruction:
            instruction = {"response_type": "chitchat", "target_length": "medium",
                           "tone": "neutral", "key_points": [], "drafter_count": 1}

        drafter_count = min(int(instruction.get("drafter_count", 1)), 3)
        # Arousal switch: low arousal → fewer drafters
        if nm["Glu"] < 0.25:
            drafter_count = max(1, drafter_count - 1)

        # --- Drafters ---
        drafter_prompt = self._build_drafter_prompt(features, memory, parietal_context,
                                                     affect, instruction)

        draft_tasks = [
            self._run_drafter(i, drafter_prompt, turn_id)
            for i in range(drafter_count)
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

        if len(drafts) >= 2:
            self._critic.reset_turn(turn_id)
            scored = []
            for draft_id, text in drafts:
                score = await self._score_draft(text, drafter_prompt, turn_id)
                if score.get("veto"):
                    self._brainstem.veto(draft_id)
                    logger.debug("[Response engine] Draft %s rejected by quality check: %s", draft_id, score.get("veto_reason"))
                    continue

                overall = score.get("overall", 0.5)

                # v0.2: empathy check when user emotion is salient
                if run_empathy:
                    empathy = await self._run_empathy_check(text, user_emotion, turn_id)
                    if empathy.get("veto"):
                        self._brainstem.veto(draft_id)
                        logger.debug("[Response engine] Draft %s rejected by empathy check — predicted poor emotional landing", draft_id)
                        continue
                    overall = overall * 0.7 + empathy.get("empathy_score", 0.5) * 0.3

                self._brainstem.add_draft(draft_id, text, overall)
                self._brainstem.endorse(draft_id)
                scored.append((draft_id, text, overall))

            if scored:
                best = max(scored, key=lambda x: x[2])
                return best[1]

        # Single draft — endorse directly
        draft_id, text = drafts[0]
        self._brainstem.add_draft(draft_id, text, 0.8)
        self._brainstem.endorse(draft_id)
        return text

    async def _run_drafter(self, idx: int, prompt: str, turn_id: str) -> tuple[str, str]:
        drafter = self._drafters[idx]
        drafter.reset_turn(turn_id)
        draft_id = f"draft_{idx}_{turn_id}"
        text = await drafter.call([{"role": "user", "content": prompt}])
        return draft_id, text

    async def _score_draft(self, draft: str, context: str, turn_id: str) -> dict:
        critic_prompt = (f"Context:\n{context}\n\nDraft response:\n{draft}\n\n"
                         "Score this draft.")
        raw = await self._critic.call([{"role": "user", "content": critic_prompt}])
        try:
            return json.loads(raw)
        except Exception:
            import re
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return {"overall": 0.5, "veto": False}

    async def _attempt_reframe(self, features: dict, affect: dict, turn_id: str) -> dict | None:
        self._reframer.reset_turn(turn_id + "_reframe")
        prompt = (
            f"User said: {features.get('raw_text', features.get('topic_summary', ''))}\n"
            f"Current entity emotion: {affect.get('emotion', 'neutral')}\n"
            f"Hostility detected: {features.get('hostility', 0):.2f}\n"
            "Propose a Stoic reframe."
        )
        raw = await self._reframer.call([{"role": "user", "content": prompt}])
        try:
            return json.loads(raw)
        except Exception:
            import re
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return None

    async def _run_empathy_check(self, draft: str, user_emotion: str,
                                  turn_id: str) -> dict:
        self._empathy_critic.reset_turn(turn_id + "_empathy")
        prompt = (
            f"User's current emotion: {user_emotion}\n"
            f"Draft response:\n{draft}\n\n"
            "Score empathic fit."
        )
        raw = await self._empathy_critic.call([{"role": "user", "content": prompt}])
        try:
            return json.loads(raw)
        except Exception:
            return {"empathy_score": 0.7, "veto": False}

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

    def _build_drafter_prompt(self, features: dict, memory: dict,
                               parietal: str, affect: dict, instruction: dict) -> str:
        nonce = str(uuid.uuid4())[:8]
        parts = []
        if parietal:
            parts.append(f"Recent conversation:\n{fence('conversation_history', parietal, nonce)}")
        if memory.get("schema"):
            parts.append(f"Known facts:\n{fence('known_facts', memory['schema'], nonce)}")
        if memory.get("episodes"):
            parts.append(f"Relevant past episodes:\n{fence('past_episodes', memory['episodes'], nonce)}")
        if memory.get("core", {}).get("self"):
            parts.append(f"Entity self-model:\n{fence('self_model', memory['core']['self'][:400], nonce)}")
        if memory.get("core", {}).get("user"):
            parts.append(f"User model:\n{fence('user_model', memory['core']['user'][:400], nonce)}")

        parts.append(f"\nDrafting instruction: {json.dumps(instruction)}")
        parts.append(f"Emotional context: {affect.get('appraisal', '')}")
        if affect.get("prosody_prefix"):
            parts.append(f"Consider opening with: '{affect['prosody_prefix']}'")

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
