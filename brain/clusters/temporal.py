"""
Temporal Lobe — language understanding + prosody.
1 LLM understanding integrator + gating switches + predictor.
Publishes structured features to the bus for all downstream clusters.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random as _random
import re
import time

from brain.bus import Bus, Message
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.predictor import PredictorSwitch, input_signature, should_bypass_gating
from brain.observability.decisions import decisions
from brain.utils import safe_json_parse
from brain.wiring import Wiring

logger = logging.getLogger(__name__)

CLUSTER = "temporal"

UNDERSTANDING_SYSTEM = """You are the temporal lobe of a biologically-inspired AI brain.
Your sole job: parse the user's input and return a JSON object with exactly these fields:
{
  "intent": string,              // one of: greeting, chitchat, question, memory_recall, task, hostile, epistemic_action, other
  "register": string,            // casual | formal | terse | emotional
  "entities": [string],          // named things mentioned
  "tense": string,               // past | present | future | mixed
  "time_reference": string,      // e.g. "last week", "now", none
  "requires_memory": bool,       // needs recall from long-term store?
  "requires_vision": bool,       // references an image or visual?
  "requires_action": bool,       // requests a tool call / action?
  "epistemic_action": bool,      // is user using this brain as a cognitive tool / thinking out loud?
  "hostility": float,            // 0.0 to 1.0 — general hostility in the message
  "sentiment": float,            // -1.0 (negative) to 1.0 (positive) — message sentiment about its topic
  "salience": float,             // 0.0 to 1.0 — how important/novel is this?
  "topic_summary": string,       // 5 words max describing the topic
  "user_tone_toward_ai": string  // how is the user treating the AI specifically:
                                 // "warm" | "joking" | "praising" | "polite" | "neutral" |
                                 // "dismissive" | "impatient" | "insulting" | "testing"
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


# Tool-request signals — when ANY of these appears, never take the fast path.
# These are strong hints that the user wants the motor cortex / cloud executor
# to actually do something (not just reply conversationally).
_TOOL_REQUEST_PATTERNS = [
    # Service / connector names
    "claude", "claude code", "github", "ableton", "imessage", "messages",
    "chrome", "browser", "adobe", "react aria",
    # Imperative tool verbs
    "ask claude", "use claude", "tell claude", "have claude",
    "send a message", "send an imessage", "send a text",
    "open ", "close ", "play ", "pause ", "search for", "look up",
    "run ", "execute ", "create ", "make a ", "write a ", "save a ",
    "read the", "read my", "list ", "find ", "delete ", "move ",
    # Generic "do X" patterns
    "can you do", "could you do", "go ahead and", "for me",
    "try to use", "try using",
]


def _looks_like_tool_request(text: str) -> bool:
    """True if the text contains signals that the user wants a tool/action,
    not just a conversational reply. Used to short-circuit predict-and-surprise
    gating — the fast path would otherwise drop requires_action to false."""
    t = text.lower()
    return any(p in t for p in _TOOL_REQUEST_PATTERNS)


class TemporalCluster:
    def __init__(self, bus: Bus, router: ModelRouter, wiring: Wiring | None = None) -> None:
        self._bus = bus
        self._router = router
        self._wiring = wiring
        self._wiring_frozen = os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true"

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
            logger.warning("[Input parser] Timed out waiting for user message — no input arrived within 25s")
            return None

        if msg.expired:
            return None

        text: str = msg.payload.get("text", "")
        image_present: bool = msg.payload.get("image_present", False)

        # --- Switch layer (free, fast) ---

        # Fire switches in weight-sorted order so the firing_path records
        # which ones actually contributed this turn. Short-circuit on template
        # match (which preempts everything else).
        switch_order = self._ordered_switches(turn_id)

        trivial, trivial_type = _is_trivial(text)
        if trivial:
            # The template-match switch wins outright.
            self._template_switch.fire(1.0, trivial_type, {"text": text[:40]})
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
            logger.debug("[Input parser] Trivial input detected (%s) — using canned response, skipping LLM", trivial_type)
            return features

        words = text.split()
        length_tag = "tiny" if len(words) <= 3 else "short" if len(words) <= 15 else "long"
        sig = input_signature(text)

        # Length switch always fires — its level tags the input bucket
        self._length_switch.fire(0.6, length_tag)

        # Predictor
        predicted_tag, confidence = self._predictor.predict(sig)
        surprise = self._predictor.surprise(predicted_tag, length_tag, confidence)
        should_wake = self._predictor.should_wake_integrator(surprise)

        self_ref = _detect_self_reference(text)
        epistemic = _detect_epistemic_action(text)
        memory_hint = epistemic or any(w in text.lower() for w in
                                       ("remember", "last", "before", "told", "what was"))

        if self_ref:
            self._self_ref_switch.fire(0.9, "self_reference")
        if epistemic:
            self._epistemic_switch.fire(0.7, "epistemic_action")

        # The inhibitor's edge weight scales the confidence threshold for skipping.
        # High inhibitor weight → easier to skip the integrator. Low → harder.
        inhibitor_weight = self._inhibitor_weight()
        confidence_floor = max(0.4, 0.6 / max(0.5, inhibitor_weight))

        # Pre-flight bypass check (emotion vetoes are still cheap; we have no
        # affect dict yet, but length/trivial signals are coarse).
        bypass = False
        bypass_reason = ""
        if image_present:
            bypass = True
            bypass_reason = "image_present"
        elif _looks_like_tool_request(text):
            # Tool-request shape: never short-circuit. The LLM needs to set
            # requires_action so the motor cortex can fire downstream.
            bypass = True
            bypass_reason = "tool_request_pattern"

        # If predictor is confident AND input is routine → skip integrator
        if not bypass and not should_wake and not self_ref and not image_present and confidence > confidence_floor:
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
            self._integrator_inhibitor.fire(confidence, "integrator_skipped")
            decisions.log(
                "skip_temporal_integrator", turn_id=turn_id, cluster=CLUSTER,
                reason=f"predictor confidence {confidence:.2f} > {confidence_floor:.2f}; surprise {surprise:.2f}",
                predicted=predicted_tag, confidence=round(confidence, 3),
                surprise=round(surprise, 3),
                inhibitor_weight=round(inhibitor_weight, 3),
                cost_saved_est=0.0005,
            )
            trace = self._record_trace()
            if trace is not None:
                trace.llm_calls_saved += 1
                trace.predictor_outcomes.append({
                    "cluster": CLUSTER, "stage": "understanding",
                    "predicted": predicted_tag, "actual": None,
                    "confidence": round(confidence, 3),
                    "surprise": round(surprise, 3),
                    "integrator_woken": False,
                    "bypass_reason": None, "correct": None,
                })
            logger.debug("[Input parser] Skipping LLM parse — predictor confident (%.2f), using fast-path features", confidence)
            return features

        # --- Integrator (LLM) ---
        self._understanding.reset_turn(turn_id)
        messages = [{"role": "user", "content": text}]
        raw = await self._understanding.call(messages)

        features: dict = safe_json_parse(raw) or {}
        if not features:
            logger.warning("[Input parser] LLM returned invalid JSON — using fallback feature defaults. Raw output: %s", raw[:200])
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

        # Belt-and-braces: if the text clearly looks like a tool request, flip
        # requires_action true regardless of what the LLM thought. The motor
        # cortex's own planner makes the final decision (it can still say
        # "tool: none") so this is a safe override.
        if _looks_like_tool_request(text) and not features.get("requires_action"):
            features["requires_action"] = True
            logger.info("[Input parser] Tool-request pattern detected — forcing requires_action=true")

        # Record actual for predictor learning + post-hoc accuracy
        actual_tag = features.get("intent", "other")
        trace = self._record_trace()
        if trace is not None:
            trace.predictor_outcomes.append({
                "cluster": CLUSTER, "stage": "understanding",
                "predicted": predicted_tag, "actual": actual_tag,
                "confidence": round(confidence, 3),
                "surprise": round(surprise, 3),
                "integrator_woken": True,
                "bypass_reason": bypass_reason if bypass else None,
                "correct": (predicted_tag == actual_tag) if predicted_tag else None,
            })
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

        logger.debug("[Input parser] Features: intent=%s salience=%.2f",
                     features.get("intent"), features.get("salience", 0))
        return features

    # ── Wiring-weight helpers ────────────────────────────────────────────────

    def _ordered_switches(self, turn_id: str) -> list[SwitchNeuron]:
        """Return temporal gating switches ordered by incoming edge weight from
        sensory.text. Higher weight → earlier evaluation. ε-greedy (ε=0.05)."""
        switches = [
            self._template_switch,
            self._length_switch,
            self._salience_prefilter,
            self._self_ref_switch,
            self._epistemic_switch,
        ]
        if self._wiring is None or self._wiring_frozen:
            return switches

        weights = {
            sw.name: self._wiring.get_edge_weight("sensory.text", f"{CLUSTER}.{sw.name}")
            for sw in switches
        }

        if _random.random() < 0.05:
            _random.shuffle(switches)
            roll = "explore"
        else:
            switches = sorted(switches, key=lambda s: weights[s.name], reverse=True)
            roll = "exploit"

        decisions.log(
            "weighted_switch_order", turn_id=turn_id, cluster=CLUSTER,
            top3=[s.name for s in switches[:3]],
            weights={k: round(v, 3) for k, v in weights.items()},
            epsilon_roll=roll,
        )
        return switches

    def _inhibitor_weight(self) -> float:
        """The edge weight from integrator_inhibitor → understanding_integrator.
        Treated as positive magnitude (inhibitory edges store positive weight but
        their effective contribution is negative; here we want the magnitude)."""
        if self._wiring is None or self._wiring_frozen:
            return 1.0
        return self._wiring.get_edge_weight(
            f"{CLUSTER}.integrator_inhibitor",
            f"{CLUSTER}.understanding_integrator",
        )

    def _record_trace(self):
        try:
            from brain.observability.firing_path import current_turn_trace
            return current_turn_trace.get()
        except Exception:
            return None
