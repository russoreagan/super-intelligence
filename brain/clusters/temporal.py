"""
Temporal Lobe — language understanding + prosody.
1 LLM understanding integrator + gating switches + predictor.
Publishes structured features to the bus for all downstream clusters.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from brain.bus import Bus, Message
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.predictor import PredictorSwitch, input_signature

logger = logging.getLogger(__name__)

CLUSTER = "temporal"

UNDERSTANDING_SYSTEM = """You are the temporal lobe of a biologically-inspired AI brain.
Your sole job: parse the user's input and return a JSON object with exactly these fields:
{
  "intent": string,           // one of: greeting, chitchat, question, memory_recall, task, hostile, epistemic_action, other
  "register": string,         // casual | formal | terse | emotional
  "entities": [string],       // named things mentioned
  "tense": string,            // past | present | future | mixed
  "time_reference": string,   // e.g. "last week", "now", none
  "requires_memory": bool,    // needs recall from long-term store?
  "requires_vision": bool,    // references an image or visual?
  "requires_action": bool,    // requests a tool call / action?
  "epistemic_action": bool,   // is user using this brain as a cognitive tool / thinking out loud?
  "hostility": float,         // 0.0 to 1.0
  "sentiment": float,         // -1.0 (negative) to 1.0 (positive)
  "salience": float,          // 0.0 to 1.0 — how important/novel is this?
  "topic_summary": string     // 5 words max describing the topic
}
Return ONLY the JSON object. No explanation."""

TRIVIAL_PATTERNS = [
    r"^(hi|hey|hello|yo|sup|hiya)[\s!.]*$",
    r"^(ok|okay|k|sure|yep|nope|yes|no|thanks|thank you|thx|ty|np)[\s!.]*$",
    r"^(bye|goodbye|cya|see ya|later)[\s!.]*$",
]

CANNED_RESPONSES = {
    "greeting": ["Hey — what's on your mind?", "Hi. What are we working on?", "Hello."],
    "farewell": ["Later.", "Take care.", "Goodbye."],
    "ack":      ["Got it.", "Sure.", "Okay."],
}


def _is_trivial(text: str) -> tuple[bool, str]:
    t = text.strip().lower()
    for pattern in TRIVIAL_PATTERNS:
        if re.match(pattern, t):
            if any(w in t for w in ("hi", "hey", "hello", "yo", "hiya")):
                return True, "greeting"
            if any(w in t for w in ("bye", "goodbye", "cya", "later")):
                return True, "farewell"
            return True, "ack"
    return False, ""


def _detect_self_reference(text: str) -> bool:
    patterns = ["what are you", "who are you", "do you remember", "how are you",
                "what do you think of yourself", "your name", "you feel"]
    t = text.lower()
    return any(p in t for p in patterns)


def _detect_epistemic_action(text: str) -> bool:
    """Is user using the brain as a cognitive tool — thinking out loud, recalling?"""
    patterns = ["what was that", "i told you", "remind me", "help me think",
                "what do you know about", "i was thinking", "so to recap",
                "what were we", "we discussed"]
    t = text.lower()
    return any(p in t for p in patterns)


class TemporalCluster:
    def __init__(self, bus: Bus, router: ModelRouter) -> None:
        self._bus = bus
        self._router = router

        self._understanding = IntegratorCell(
            name="understanding_integrator",
            cluster=CLUSTER,
            model="flash-lite",
            system_prompt=UNDERSTANDING_SYSTEM,
            topics=["sensory.text"],
        )
        self._understanding.set_router(router)

        self._predictor = PredictorSwitch(name="temporal_predictor", cluster=CLUSTER)

        # Switches (6 + predictor = 7; 1 inhibitory → ~14% inhibitory, acceptable for small cluster)
        self._template_switch = SwitchNeuron("template_match", CLUSTER, polarity="excitatory")
        self._length_switch = SwitchNeuron("length_bucket", CLUSTER, polarity="excitatory")
        self._salience_prefilter = SwitchNeuron("salience_prefilter", CLUSTER, polarity="excitatory")
        self._self_ref_switch = SwitchNeuron("self_reference", CLUSTER, polarity="excitatory")
        self._epistemic_switch = SwitchNeuron("epistemic_action", CLUSTER, polarity="excitatory")
        self._integrator_inhibitor = SwitchNeuron("integrator_inhibitor", CLUSTER, polarity="inhibitory")

        self._inbox = bus.subscribe("sensory.text")

    async def run(self, turn_id: str) -> dict | None:
        """Process the next sensory input for this turn. Returns parsed features or None."""
        try:
            msg: Message = await asyncio.wait_for(self._inbox.get(), timeout=25.0)
        except asyncio.TimeoutError:
            logger.warning("Temporal: no sensory input received")
            return None

        if msg.expired:
            return None

        text: str = msg.payload.get("text", "")
        image_present: bool = msg.payload.get("image_present", False)

        # --- Switch layer (free, fast) ---

        trivial, trivial_type = _is_trivial(text)
        if trivial:
            import random
            canned = random.choice(CANNED_RESPONSES.get(trivial_type, ["..."]))
            features = {
                "intent": trivial_type,
                "register": "casual",
                "entities": [],
                "tense": "present",
                "time_reference": "none",
                "requires_memory": False,
                "requires_vision": False,
                "requires_action": False,
                "epistemic_action": False,
                "hostility": 0.0,
                "sentiment": 0.5,
                "salience": 0.0,
                "topic_summary": trivial_type,
                "raw_text": text,
                "canned_response": canned,
                "switch_only": True,
            }
            await self._bus.publish_dict("temporal.features", features, source=CLUSTER)
            logger.debug("Temporal: trivial match → canned response, 0 LLM calls")
            return features

        words = text.split()
        length_tag = "tiny" if len(words) <= 3 else "short" if len(words) <= 15 else "long"
        sig = input_signature(text)

        # Predictor
        predicted_tag, confidence = self._predictor.predict(sig)
        surprise = self._predictor.surprise(predicted_tag, length_tag, confidence)
        should_wake = self._predictor.should_wake_integrator(surprise)

        self_ref = _detect_self_reference(text)
        epistemic = _detect_epistemic_action(text)
        memory_hint = epistemic or any(w in text.lower() for w in
                                       ("remember", "last", "before", "told", "what was"))

        # If predictor is confident AND input is routine → skip integrator
        if not should_wake and not self_ref and not image_present and confidence > 0.6:
            # Build a lightweight feature dict from switches only
            features = {
                "intent": "question" if "?" in text else "chitchat",
                "register": "casual",
                "entities": [],
                "tense": "present",
                "time_reference": "none",
                "requires_memory": memory_hint,
                "requires_vision": False,
                "requires_action": False,
                "epistemic_action": epistemic,
                "hostility": 0.0,
                "sentiment": 0.5,
                "salience": 0.3,
                "topic_summary": length_tag + " input",
                "raw_text": text,
                "switch_only": True,
                "surprise_score": surprise,
            }
            self._predictor.record(sig, length_tag)
            await self._bus.publish_dict("temporal.features", features, source=CLUSTER)
            logger.debug("Temporal: predictor confident (%.2f), integrator suppressed", confidence)
            return features

        # --- Integrator (LLM) ---
        self._understanding.reset_turn(turn_id)
        messages = [{"role": "user", "content": text}]
        raw = await self._understanding.call(messages)

        features: dict = {}
        try:
            features = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON block
            import re as _re
            m = _re.search(r'\{.*\}', raw, _re.DOTALL)
            if m:
                try:
                    features = json.loads(m.group(0))
                except Exception:
                    pass
            if not features:
                logger.warning("Temporal: could not parse integrator output: %s", raw[:200])
                features = {"intent": "other", "salience": 0.5, "requires_memory": False,
                            "requires_vision": False, "requires_action": False,
                            "epistemic_action": False, "hostility": 0.0, "sentiment": 0.5,
                            "topic_summary": "unknown", "entities": [], "register": "casual",
                            "tense": "present", "time_reference": "none"}

        features["switch_only"] = False
        features["surprise_score"] = surprise
        features["raw_text"] = text
        features["self_reference"] = self_ref or features.get("intent") == "self_inquiry"
        features["epistemic_action"] = epistemic or features.get("epistemic_action", False)

        self._predictor.record(sig, features.get("intent", "other"))

        await self._bus.publish_dict("temporal.features", features, source=CLUSTER)

        # Signal hippocampus if memory is needed
        if features.get("requires_memory"):
            await self._bus.publish_dict(
                "mem.recall",
                {"query": text, "entities": features.get("entities", []),
                 "time_ref": features.get("time_reference", "none")},
                source=CLUSTER,
            )

        logger.debug("Temporal: features published (intent=%s, salience=%.2f)",
                     features.get("intent"), features.get("salience", 0))
        return features
