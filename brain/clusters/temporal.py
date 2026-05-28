"""
Temporal Lobe — language understanding + prosody.
1 LLM understanding integrator + gating switches + predictor.
Publishes structured features to the bus for all downstream clusters.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random as _random
import re

from brain.bus import Bus, Message
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.observability.decisions import decisions
from brain.predictor import PredictorSwitch, input_signature
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
  "user_tone_toward_ai": string, // how is the user treating the AI specifically:
                                 // "warm" | "joking" | "praising" | "polite" | "neutral" |
                                 // "dismissive" | "impatient" | "insulting" | "testing"
  "user_emotion": string         // the user's apparent inner emotional state:
                                 // "happy" | "playful" | "amused" | "warm" | "affectionate" |
                                 // "curious" | "engaged" | "excited" |
                                 // "neutral" |
                                 // "frustrated" | "annoyed" | "disappointed" | "angry" |
                                 // "sad" | "anxious" | "distressed" | "struggling" | "tired" |
                                 // "confused" | "surprised"
                                 // Pick the single best label. Prefer a specific emotion over
                                 // "neutral" whenever the message gives any signal at all.
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
    "look into", "look at", "look in", "check ", "check the", "check my",
    "investigate", "browse", "explore", "directory", "folder",
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


# Cheap lexicon-based affect heuristic for fast-paths that skip the LLM.
# Not as good as the LLM read, but vastly better than slamming everything to
# sentiment=0.5/hostility=0/user_emotion=neutral — which made the affect system
# blind on the very turns the predictor was most confident about.
_AFFECT_LEX: dict[str, tuple[float, float, str, str]] = {
    # word: (sentiment_delta, hostility_delta, user_emotion, user_tone_toward_ai)
    # positive
    "love": (0.6, 0.0, "affectionate", "warm"),
    "loved": (0.5, 0.0, "warm", "warm"),
    "great": (0.5, 0.0, "happy", "warm"),
    "awesome": (0.6, 0.0, "excited", "warm"),
    "amazing": (0.6, 0.0, "excited", "warm"),
    "perfect": (0.6, 0.0, "happy", "praising"),
    "nice": (0.4, 0.0, "happy", "warm"),
    "good": (0.3, 0.0, "happy", "polite"),
    "thanks": (0.4, 0.0, "warm", "warm"),
    "thank": (0.4, 0.0, "warm", "warm"),
    "happy": (0.5, 0.0, "happy", "warm"),
    "haha": (0.4, 0.0, "amused", "joking"),
    "lol": (0.4, 0.0, "amused", "joking"),
    "lmao": (0.5, 0.0, "amused", "joking"),
    "cool": (0.3, 0.0, "engaged", "warm"),
    "yes!": (0.5, 0.0, "excited", "warm"),
    "yay": (0.6, 0.0, "happy", "warm"),
    "curious": (0.2, 0.0, "curious", "neutral"),
    "wonder": (0.2, 0.0, "curious", "neutral"),
    "interesting": (0.3, 0.0, "engaged", "warm"),
    # negative / hostile
    "hate": (-0.6, 0.4, "angry", "insulting"),
    "stupid": (-0.5, 0.6, "frustrated", "insulting"),
    "dumb": (-0.4, 0.5, "frustrated", "insulting"),
    "idiot": (-0.6, 0.7, "angry", "insulting"),
    "shut up": (-0.5, 0.7, "angry", "dismissive"),
    "wrong": (-0.3, 0.2, "frustrated", "impatient"),
    "broken": (-0.3, 0.1, "frustrated", "neutral"),
    "fucking": (-0.4, 0.5, "frustrated", "impatient"),
    "fuck": (-0.4, 0.4, "frustrated", "impatient"),
    "annoying": (-0.5, 0.4, "annoyed", "impatient"),
    "annoyed": (-0.5, 0.3, "annoyed", "impatient"),
    "frustrated": (-0.5, 0.2, "frustrated", "neutral"),
    "angry": (-0.6, 0.4, "angry", "impatient"),
    "ugh": (-0.4, 0.2, "frustrated", "impatient"),
    "no.": (-0.2, 0.2, "annoyed", "dismissive"),
    "stop": (-0.3, 0.3, "frustrated", "impatient"),
    "bad": (-0.4, 0.1, "disappointed", "neutral"),
    "terrible": (-0.6, 0.1, "disappointed", "neutral"),
    "awful": (-0.6, 0.1, "disappointed", "neutral"),
    # vulnerable
    "sad": (-0.5, 0.0, "sad", "neutral"),
    "tired": (-0.3, 0.0, "tired", "neutral"),
    "exhausted": (-0.4, 0.0, "tired", "neutral"),
    "anxious": (-0.4, 0.0, "anxious", "neutral"),
    "worried": (-0.4, 0.0, "anxious", "neutral"),
    "scared": (-0.5, 0.0, "anxious", "neutral"),
    "stuck": (-0.3, 0.0, "struggling", "neutral"),
    "lost": (-0.3, 0.0, "struggling", "neutral"),
    "confused": (-0.2, 0.0, "confused", "neutral"),
    "help": (-0.1, 0.0, "struggling", "polite"),
    # surprise
    "wait": (0.0, 0.0, "surprised", "neutral"),
    "really?": (0.1, 0.0, "surprised", "neutral"),
    "whoa": (0.2, 0.0, "surprised", "neutral"),
    "wow": (0.3, 0.0, "surprised", "warm"),
}


def _heuristic_affect(text: str) -> dict:
    """Tiny lexicon-based read of sentiment/hostility/user_emotion. Used on
    fast-paths that skip the LLM. Returns sensible defaults (neutral, sentiment≈0)
    if nothing matches — *not* the misleading sentiment=0.5 that the old fast
    paths used."""
    t = (text or "").lower()
    if not t.strip():
        return {"sentiment": 0.0, "hostility": 0.0,
                "user_emotion": "neutral", "user_tone_toward_ai": "neutral"}
    sentiment = 0.0
    hostility = 0.0
    best_emotion: str | None = None
    best_tone: str | None = None
    best_weight = 0.0
    for word, (s, h, e, tone) in _AFFECT_LEX.items():
        if word in t:
            sentiment += s
            hostility += h
            w = abs(s) + h
            if w > best_weight:
                best_weight = w
                best_emotion = e
                best_tone = tone
    # punctuation cues
    if "!" in t:
        # exclamation amplifies the dominant valence; if no valence, mild excitement
        if sentiment > 0:
            sentiment += 0.1
        elif sentiment < 0:
            sentiment -= 0.1
            hostility += 0.05
        else:
            best_emotion = best_emotion or "excited"
    if t.count("?") >= 2 and best_emotion is None:
        best_emotion = "confused"
    # clamp
    sentiment = max(-1.0, min(1.0, sentiment))
    hostility = max(0.0, min(1.0, hostility))
    return {
        "sentiment": round(sentiment, 3),
        "hostility": round(hostility, 3),
        "user_emotion": best_emotion or "neutral",
        "user_tone_toward_ai": best_tone or "neutral",
    }


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
        # Modulator profiles encode each switch's biological identity — see plan
        # /Users/russ/.claude/plans/and-what-affects-these-memoized-parnas.md.
        self._template_switch = SwitchNeuron("template_match", CLUSTER, polarity="excitatory",
                                             threshold=0.5,
                                             modulators={"ACh": +0.15})
        self._length_switch = SwitchNeuron("length_bucket", CLUSTER, polarity="excitatory")
        self._salience_prefilter = SwitchNeuron("salience_prefilter", CLUSTER, polarity="excitatory",
                                                threshold=0.4,
                                                # NE lowers floor (alert state = more things tripped),
                                                # CORT raises it (stress narrows attention to true threats).
                                                modulators={"DA": +0.10, "NE": -0.15, "CORT": +0.10})
        self._self_ref_switch = SwitchNeuron("self_reference", CLUSTER, polarity="excitatory",
                                             threshold=0.5,
                                             modulators={"OXT": -0.10, "5HT": -0.10})
        self._epistemic_switch = SwitchNeuron("epistemic_action", CLUSTER, polarity="excitatory",
                                              threshold=0.5,
                                              modulators={"ACh": -0.15})
        # Note: GABA modulator was removed — the should_bypass_gating() helper
        # already forces the integrator awake at high GABA (emotional states
        # engage understanding, not less of it). The old {"GABA": -0.15}
        # contradicted that bypass and effectively cancelled out. ACh keeps the
        # integrator engaged on simple inputs when the brain is curious.
        self._integrator_inhibitor = SwitchNeuron("integrator_inhibitor", CLUSTER, polarity="inhibitory",
                                                  threshold=0.5,
                                                  modulators={"ACh": +0.10})

        self._inbox = bus.subscribe("sensory.text")
        # DMN's top-down prediction of the user's next message. Drained each
        # turn — if the actual input matches, predictor confidence is boosted.
        self._dmn_prediction_inbox = bus.subscribe("stream.prediction")

    def _consume_dmn_prediction(self) -> dict | None:
        """Drain stream.prediction queue, keep the most recent fresh prediction."""
        latest: dict | None = None
        while True:
            try:
                m = self._dmn_prediction_inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not m.expired:
                latest = m.payload
        return latest

    async def run(self, turn_id: str) -> dict | None:
        """Process the next sensory input for this turn. Returns parsed features or None."""
        try:
            msg: Message = await asyncio.wait_for(self._inbox.get(), timeout=25.0)
        except TimeoutError:
            logger.warning("[Input parser] Timed out waiting for user message — no input arrived within 25s")
            return None

        if msg.expired:
            return None

        text: str = msg.payload.get("text", "")
        image_present: bool = msg.payload.get("image_present", False)

        # Chemistry snapshot for switch modulation — fetched once per turn so
        # every switch sees the same state. Hypothalamus updates this in-place
        # between turns; reading it sync is cheap.
        chem = self._chem_snapshot()

        # ── DMN top-down prediction: if the brain's idle "user simulator"
        # guessed something close to what the user actually said, that's
        # strong evidence the routine path is correct — boost predictor.
        dmn_prediction = self._consume_dmn_prediction()
        dmn_hit_overlap = 0.0
        if dmn_prediction and dmn_prediction.get("predicted_input"):
            from brain.voice_bridge import bleed_overlap as _word_overlap
            dmn_hit_overlap = _word_overlap(text, dmn_prediction["predicted_input"])
            if dmn_hit_overlap >= 0.5:
                decisions.log(
                    "dmn_prediction_hit", turn_id=turn_id, cluster=CLUSTER,
                    reason=f"DMN guessed {dmn_prediction.get('predicted_input', '')[:60]!r}, "
                           f"user said {text[:60]!r}, overlap {dmn_hit_overlap:.2f}",
                    predicted=dmn_prediction.get("predicted_input", "")[:120],
                    actual=text[:120],
                    overlap=round(dmn_hit_overlap, 3),
                    dmn_confidence=dmn_prediction.get("confidence", 0),
                )

        # --- Switch layer (free, fast) ---

        # Fire switches in weight-sorted order so the firing_path records
        # which ones actually contributed this turn. Short-circuit on template
        # match (which preempts everything else).
        self._ordered_switches(turn_id)

        trivial, trivial_type = _is_trivial(text)
        if trivial and self._template_switch.should_fire(0.7, chem, turn_id):
            # The template-match switch wins outright. High ACh (curiosity)
            # raises the threshold and can suppress the canned-response shortcut
            # even on a trivial-pattern hit — engaging the LLM instead.
            self._template_switch.fire(0.7, trivial_type, {"text": text[:40]}, chem)
            import random
            canned = random.choice(CANNED_RESPONSES.get(trivial_type, ["..."]))
            _words = text.split()
            _aff = _heuristic_affect(text)
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
                "hostility": _aff["hostility"],
                "sentiment": _aff["sentiment"],
                "salience": 0.0,
                "topic_summary": trivial_type,
                "user_emotion": _aff["user_emotion"],
                "user_tone_toward_ai": _aff["user_tone_toward_ai"],
                "raw_text": text,
                "canned_response": canned,
                "switch_only": True,
                "msg_length": "tiny" if len(_words) <= 3 else "short" if len(_words) <= 15 else "long",
            }
            await self._bus.publish_dict("temporal.features", features, source=CLUSTER)
            logger.debug("[Input parser] Trivial input detected (%s) — using canned response, skipping LLM", trivial_type)
            return features

        words = text.split()
        length_tag = "tiny" if len(words) <= 3 else "short" if len(words) <= 15 else "long"
        sig = input_signature(text)

        # Length switch always fires — its level tags the input bucket. No
        # chemistry modulation (granularity should not depend on mood).
        self._length_switch.fire(0.6, length_tag, snapshot=chem)

        # Predictor
        predicted_tag, confidence = self._predictor.predict(sig)
        surprise = self._predictor.surprise(predicted_tag, length_tag, confidence)
        should_wake = self._predictor.should_wake_integrator(surprise)

        self_ref = _detect_self_reference(text)
        epistemic = _detect_epistemic_action(text)
        memory_hint = epistemic or any(w in text.lower() for w in
                                       ("remember", "last", "before", "told", "what was"))

        if self_ref and self._self_ref_switch.should_fire(0.6, chem, turn_id):
            self._self_ref_switch.fire(0.6, "self_reference", snapshot=chem)
        if epistemic and self._epistemic_switch.should_fire(0.55, chem, turn_id):
            self._epistemic_switch.fire(0.55, "epistemic_action", snapshot=chem)

        # The inhibitor's edge weight scales the confidence threshold for skipping.
        # High inhibitor weight → easier to skip the integrator. Low → harder.
        inhibitor_weight = self._inhibitor_weight()
        confidence_floor = max(0.4, 0.6 / max(0.5, inhibitor_weight))

        # Chemistry modulation: high ACh (positive coefficient on integrator_inhibitor)
        # raises inhibitor threshold → harder to skip → integrator stays engaged when curious.
        chem_shift = self._integrator_inhibitor.modulation_delta(chem)
        confidence_floor = max(0.30, min(0.90, confidence_floor + chem_shift))

        # DMN top-down hit: if the brain already simulated something close to
        # this input, drop the confidence floor (skip the LLM more readily)
        # and reduce surprise. This is "compute scales with novelty" extended
        # with predictive processing — the brain bypasses understanding for
        # inputs it had already imagined.
        if dmn_hit_overlap >= 0.5:
            confidence_floor = max(0.3, confidence_floor - 0.25 * dmn_hit_overlap)
            surprise = max(0.0, surprise - 0.4 * dmn_hit_overlap)
            should_wake = self._predictor.should_wake_integrator(surprise)

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
            _aff = _heuristic_affect(text)
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
                "hostility": _aff["hostility"],
                "sentiment": _aff["sentiment"],
                "salience": 0.3,
                "topic_summary": length_tag + " input",
                "user_emotion": _aff["user_emotion"],
                "user_tone_toward_ai": _aff["user_tone_toward_ai"],
                "raw_text": text,
                "switch_only": True,
                "surprise_score": surprise,
                "msg_length": length_tag,
            }
            self._predictor.record(sig, length_tag)
            await self._bus.publish_dict("temporal.features", features, source=CLUSTER)
            self._integrator_inhibitor.fire(confidence, "integrator_skipped", snapshot=chem)
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
            _aff = _heuristic_affect(text)
            features = {"intent": "other", "salience": 0.5, "requires_memory": False,
                        "requires_vision": False, "requires_action": False,
                        "epistemic_action": False,
                        "hostility": _aff["hostility"], "sentiment": _aff["sentiment"],
                        "user_emotion": _aff["user_emotion"],
                        "user_tone_toward_ai": _aff["user_tone_toward_ai"],
                        "topic_summary": "unknown", "entities": [], "register": "casual",
                        "tense": "present", "time_reference": "none"}
        else:
            # LLM path: if the model omitted user_emotion (older prompts, partial JSON),
            # backfill from the cheap heuristic rather than letting downstream code
            # read None and collapse to "unknown".
            if not features.get("user_emotion"):
                _aff = _heuristic_affect(text)
                features["user_emotion"] = _aff["user_emotion"]
                if not features.get("user_tone_toward_ai"):
                    features["user_tone_toward_ai"] = _aff["user_tone_toward_ai"]

        features["switch_only"] = False
        features["surprise_score"] = surprise
        features["raw_text"] = text
        features["self_reference"] = self_ref or features.get("intent") == "self_inquiry"
        features["epistemic_action"] = epistemic or features.get("epistemic_action", False)
        features["msg_length"] = length_tag

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
        return {**nm, **hs}
