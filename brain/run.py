"""
Brain run.py — session lifecycle and cluster wiring.
See brain/CONSTITUTION.md for design commitments.

Usage:
    uv run python -m brain.run
    uv run python -m brain.run --message "Hi, I'm Russ"
    uv run python -m brain.run --ui              # browser UI at http://localhost:8765
    uv run python -m brain.run --ui --dmn        # + stream of consciousness
    uv run python -m brain.run --ui --metacognition
    uv run python -m brain.run --voice           # Deepgram + ElevenLabs
    uv run python -m brain.run --voice --ears    # + auditory cortex
    uv run python -m brain.run --motor           # + motor cortex (tool use)

Environment feature flags:
    BRAIN_UI=true             enable browser UI
    BRAIN_DMN=true            enable Default Mode Network
    BRAIN_METACOGNITION=true  enable metacognition cell
    BRAIN_VOICE_MODE=true     enable voice I/O
    BRAIN_EARS=true           enable auditory cortex
    BRAIN_MOTOR=true          enable motor cortex (tool execution)
    BRAIN_MOTOR_PATHS         colon-separated allowed filesystem roots
    BRAIN_MOTOR_COMMANDS      colon-separated allowed shell commands (overrides defaults)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)  # override=True so .env values win over empty shell exports

logging.basicConfig(
    level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
from brain.emotion_hierarchy import core_of
from brain.security import SecretRedactingFilter
_redact_filter = SecretRedactingFilter()
logging.getLogger().addFilter(_redact_filter)
logger = logging.getLogger("brain.run")


_CANCEL_WORDS = frozenset(["never mind", "nevermind", "skip", "cancel", "forget it",
                            "don't bother", "no thanks", "not now"])


def _extract_identity_name(text: str, features: dict) -> str | None:
    """Extract a person's name from a self-identification utterance."""
    from brain.clusters.audio_dsp import extract_identity_name
    name = extract_identity_name(text)
    if name:
        return name
    # Fallback: a single short alphabetic entity from the temporal parse
    entities = features.get("entities", [])
    if len(entities) == 1:
        candidate = entities[0].strip()
        if 2 <= len(candidate) <= 30 and candidate.replace(" ", "").isalpha():
            return candidate.title()
    return None


def _is_enrollment_cancellation(text: str) -> bool:
    """True if the user is declining enrollment."""
    return text.lower().strip() in _CANCEL_WORDS or any(w in text.lower() for w in _CANCEL_WORDS)


async def session(args) -> None:
    from brain.bus import Bus
    from brain.brainstem import Brainstem
    from brain.model_router import ModelRouter
    from brain.pns import PNS
    from brain.clusters.temporal import TemporalCluster
    from brain.clusters.thalamus import ThalamusCluster
    from brain.clusters.occipital import OccipitalCluster
    from brain.clusters.hypothalamus import HypothalamusCluster
    from brain.clusters.parietal import ParietalCluster
    from brain.clusters.hippocampus import HippocampusCluster
    from brain.clusters.frontal import FrontalCluster
    from brain.observability.timeline import ObservabilityLayer, TurnTrace
    from brain.observability.firing_path import set_current_trace, reset_current_trace
    from brain.observability.decisions import decisions as decisions_log
    from brain.wiring import Wiring
    from brain.wiring_bootstrap import bootstrap as wiring_bootstrap

    session_id = str(uuid.uuid4())[:8]
    logger.info("Session %s starting", session_id)

    bus = Bus()

    # ── Eval system (always-on JSONL logging; baseline/scorer gated by env flags) ──
    eval_logger = None
    baseline_runner = None
    posthoc_scorer = None
    try:
        from eval.turn_logger import EvalLogger
        from eval.baseline import BaselineRunner
        from eval.scorer import PostHocScorer
        eval_logger = EvalLogger()
        baseline_runner = BaselineRunner(eval_logger)
        posthoc_scorer = PostHocScorer(eval_logger)
        baseline_runner._scorer = posthoc_scorer
        logger.info("Eval: logging to %s", eval_logger._path)
    except Exception as _eval_err:
        logger.debug("Eval system unavailable: %s", _eval_err)

    obs = ObservabilityLayer(session_id, eval_logger=eval_logger)
    if posthoc_scorer is not None:
        posthoc_scorer._obs = obs
    router = ModelRouter(obs=obs)
    brainstem = Brainstem(bus, router)
    pns = PNS(bus)

    # ── Wiring graph (Hebbian edge weights) ───────────────────────────────────
    wiring = Wiring()
    wiring_bootstrap(wiring)
    wiring.snapshot_baseline()
    wiring_frozen = os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true"
    if wiring_frozen:
        logger.info("Wiring FROZEN — weighted routing disabled (BRAIN_WIRING_FROZEN=true)")
    else:
        logger.info("Wiring: %d edges loaded", wiring.edge_count())

    # ── Decisions log (predict-and-surprise + Hebbian) ────────────────────────
    decisions_log.configure(eval_logger=eval_logger)

    # Clusters
    thalamus = ThalamusCluster(bus)
    temporal = TemporalCluster(bus, router, wiring=wiring)
    occipital = OccipitalCluster(bus, router)
    hypothalamus = HypothalamusCluster(bus)
    parietal = ParietalCluster(bus)
    hippocampus = HippocampusCluster(bus, router, wiring=wiring)
    frontal = FrontalCluster(bus, brainstem, router, wiring=wiring)

    # Boot: pre-load core schema + seed parietal with recent episodes
    core_context, recent_episodes = await hippocampus.boot(session_id)
    parietal.seed(recent_episodes)

    from brain.security import PseudonymizationGateway, EGRESS_MODE
    egress = PseudonymizationGateway()

    # ── UI server (optional) ──────────────────────────────────────────────────
    ui_enabled = args.ui or os.environ.get("BRAIN_UI", "false").lower() == "true"
    ui_server = None
    emitter = None
    # Queue for WebSocket-driven user messages (replaces stdin when --ui)
    ui_message_queue: asyncio.Queue = asyncio.Queue()

    if ui_enabled:
        from brain.ui.emitter import emitter as _emitter
        from brain.ui.server import UIServer
        emitter = _emitter

        async def _on_browser_message(text: str) -> None:
            await ui_message_queue.put(text)

        def _on_eval_mode(intensive: bool) -> None:
            if baseline_runner:
                baseline_runner.set_intensive(intensive)

        # Voice mode: determined by --voice flag or BRAIN_VOICE_MODE env.
        # streaming_mic is assigned later (line ~224) but the closure captures
        # the outer-scope variable so the callback always sees the live value.
        _voice_flag = args.voice or os.environ.get("BRAIN_VOICE_MODE", "false").lower() == "true"

        def _on_mic_toggle() -> bool:
            if streaming_mic is not None:
                return streaming_mic.toggle_mute()
            return False

        ui_server = UIServer(emitter.get_queue(), on_user_message=_on_browser_message,
                             on_voice_change=pns.set_voice_id,
                             on_eval_mode=_on_eval_mode,
                             on_mic_toggle=_on_mic_toggle,
                             python_voice_mode=_voice_flag)
        ui_server.set_wiring_frozen(wiring_frozen)
        asyncio.create_task(ui_server.start(port=8765))
        # Give the server a moment to bind
        await asyncio.sleep(0.3)

    async def _emit(cluster: str, intensity: float, note: str, turn_id: str = "") -> None:
        if emitter:
            await emitter.emit(cluster, intensity, note, turn_id)

    async def _emit_end(cluster: str, turn_id: str = "") -> None:
        if emitter:
            await emitter.emit(cluster, 0.0, "done", turn_id)

    # ── Motor Cortex (tool use) ───────────────────────────────────────────────
    motor = None
    if args.motor or os.environ.get("BRAIN_MOTOR", "false").lower() == "true":
        from brain.clusters.motor_cortex import MotorCortexCluster
        from brain.clusters.cloud_executor import CloudExecutor

        _motor_paths_raw = os.environ.get("BRAIN_MOTOR_PATHS", "")
        _motor_paths = [p.strip() for p in _motor_paths_raw.split(":") if p.strip()]
        _motor_cmds_raw = os.environ.get("BRAIN_MOTOR_COMMANDS", "")
        _motor_cmds = set(_motor_cmds_raw.split(":")) if _motor_cmds_raw else None

        cloud = CloudExecutor(bus, schema_store=hippocampus._schema)

        motor = MotorCortexCluster(bus, router,
                                   allowed_paths=_motor_paths,
                                   allowed_commands=_motor_cmds,
                                   cloud_executor=cloud)
        if _motor_paths:
            logger.info("Motor cortex online. Allowed paths: %s", _motor_paths)
        else:
            logger.warning(
                "Motor cortex enabled but BRAIN_MOTOR_PATHS is not set — "
                "filesystem operations will be blocked until paths are configured."
            )

    # ── v0.2: Default Mode Network ────────────────────────────────────────────
    dmn = None
    if args.dmn or os.environ.get("BRAIN_DMN", "false").lower() == "true":
        from brain.dmn import DefaultModeNetwork
        dmn = DefaultModeNetwork(bus, router, hippocampus, parietal)

        # Hook DMN thoughts into UI stream
        if emitter:
            orig_tick = dmn._tick

            async def _dmn_tick_with_ui():
                if emitter:
                    await emitter.emit("dmn", 0.25, "thinking...", "dmn")
                    await emitter.emit("hippocampus", 0.15, "consolidating...", "dmn")
                await orig_tick()
                if emitter:
                    await emitter.emit("dmn", 0.0, "done", "dmn")
                    await emitter.emit("hippocampus", 0.0, "done", "dmn")

            dmn._tick = _dmn_tick_with_ui

        await dmn.start(session_id)

    # Forward DMN stream-of-consciousness thoughts to the UI panel (shown as
    # italic, unspeakable thought bubbles between turns).
    if emitter:
        _thought_inbox: asyncio.Queue = bus.subscribe("stream.thought")

        async def _forward_thoughts() -> None:
            while True:
                msg = await _thought_inbox.get()
                thought = msg.payload.get("thought", "") if not msg.expired else ""
                if thought:
                    await emitter.emit_stream_thought(thought)

        asyncio.create_task(_forward_thoughts())

    # ── v0.3: Metacognition ───────────────────────────────────────────────────
    meta = None
    if args.metacognition or os.environ.get("BRAIN_METACOGNITION", "false").lower() == "true":
        from brain.metacognition import MetacognitionCell
        meta = MetacognitionCell(bus, router, hippocampus._schema)
        await meta.start()

    # ── v0.4: Auditory Cortex ─────────────────────────────────────────────────
    ears = None
    enrollment_complete_inbox: asyncio.Queue = bus.subscribe("auditory.enrollment_complete")
    speaker_id_inbox: asyncio.Queue = bus.subscribe("auditory.speaker_id")
    song_match_inbox: asyncio.Queue = bus.subscribe("auditory.song_match")
    if args.ears or os.environ.get("BRAIN_EARS", "false").lower() == "true":
        from brain.clusters.auditory_cortex import AuditoryCluster
        ears = AuditoryCluster(bus)
        asyncio.create_task(ears.run())

    # ── Streaming mic (voice mode): always-on listener with barge-in ─────────
    streaming_mic = None
    if args.voice or os.environ.get("BRAIN_VOICE_MODE", "false").lower() == "true":
        from brain.streaming_mic import StreamingMicSession
        streaming_mic = StreamingMicSession(
            bus,
            is_speaking_fn=lambda: pns.is_speaking,
            on_user_interrupt=pns.interrupt,
        )
        try:
            await streaming_mic.start()
        except Exception as e:
            logger.error("[I/O] Streaming mic failed to start — voice input is offline: %s", e)
            streaming_mic = None

    # ── Voice → UI bridge: forward utterances as turns ──────────────────────
    # In UI+voice mode the browser drives the conversation via ui_message_queue.
    # Without this bridge, voice utterances accumulate in streaming_mic.utterances
    # forever and the brain appears to ignore everything spoken.
    #
    # Behaviour:
    #   - Brain NOT speaking → dispatch normally (also emit a transcript event
    #     so the UI shows the text in the chat input box).
    #   - Brain IS speaking AND transcript looks like TTS bleed (high word
    #     overlap with what the brain is currently saying) → drop silently.
    #   - Brain IS speaking AND transcript contains a barge-in keyword
    #     (stop, wait, cut it out, etc.) → interrupt TTS and dispatch.
    #   - Brain IS speaking AND transcript is a genuine new utterance →
    #     QUEUE it for dispatch once TTS finishes. This is the fix for
    #     "brain ignored me when I asked a follow-up question": queueing
    #     beats dropping for real human speech.
    from brain.voice_bridge import (
        parse_barge_words, classify_utterance, pick_dispatch_from_queue,
    )
    barge_in_words = parse_barge_words(os.environ.get("BRAIN_BARGE_IN_WORDS"))

    voice_bridge_task = None
    if streaming_mic is not None and ui_enabled:
        # Buffer of utterances received while TTS was playing — drained when TTS ends
        pending_during_tts: list[str] = []
        pending_lock = asyncio.Lock()

        async def _dispatch_text(text: str) -> None:
            logger.info("[I/O] voice → turn: %r", text[:80])
            if emitter:
                try:
                    await emitter.emit_event({
                        "type": "transcript", "text": text, "final": True,
                    })
                except Exception:
                    pass
            await ui_message_queue.put(text)

        async def _drain_pending_when_tts_ends() -> None:
            """Poll pns.is_speaking; when it transitions false, dispatch the
            chosen queued utterance (most recent — older ones tend to be
            reactions to the in-progress TTS rather than what the user wants now)."""
            was_speaking = False
            while True:
                try:
                    await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    return
                now_speaking = pns.is_speaking
                if was_speaking and not now_speaking:
                    async with pending_lock:
                        text, n_dropped = pick_dispatch_from_queue(pending_during_tts)
                        pending_during_tts.clear()
                    if text:
                        logger.info("[I/O] voice → flushing queued utterance after TTS"
                                    " (kept latest, dropped %d older): %r",
                                    n_dropped, text[:80])
                        await _dispatch_text(text)
                was_speaking = now_speaking

        async def _voice_bridge() -> None:
            while True:
                try:
                    utt = await streaming_mic.next_utterance()
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("[I/O] voice bridge read failed: %s", e)
                    await asyncio.sleep(0.5)
                    continue
                text = (utt.get("transcript") or "").strip()

                decision, info = classify_utterance(
                    text,
                    brain_is_speaking=pns.is_speaking,
                    speaking_text=pns._speaking_text,  # noqa: SLF001
                    barge_words=barge_in_words,
                )

                if decision == "drop_empty":
                    continue
                if decision == "dispatch":
                    await _dispatch_text(text)
                    continue
                if decision == "barge_in":
                    logger.info("[I/O] voice → barge-in keyword detected: %r", text[:80])
                    pns.interrupt()
                    await _dispatch_text(text)
                    continue
                if decision == "drop_bleed":
                    logger.debug("[I/O] voice → dropped as TTS bleed (overlap %.2f): %r",
                                 info.get("overlap", 0.0), text[:80])
                    continue
                if decision == "queue":
                    async with pending_lock:
                        pending_during_tts.append(text)
                    logger.info("[I/O] voice → queued during TTS (overlap %.2f): %r",
                                info.get("overlap", 0.0), text[:80])
                    if emitter:
                        try:
                            await emitter.emit_event({
                                "type": "transcript", "text": text, "final": True,
                            })
                        except Exception:
                            pass

        voice_bridge_task = asyncio.create_task(_voice_bridge())
        voice_drain_task = asyncio.create_task(_drain_pending_when_tts_ends())

    # Brainstem heartbeat — also pulses the UI every 60 s
    async def _heartbeat_with_ui() -> None:
        while True:
            await asyncio.sleep(60)
            logger.info("Heartbeat: %d total LLM calls this session", brainstem._session_cost_calls)
            if emitter:
                await emitter.emit("brainstem", 0.2, "heartbeat", "hb")
                await asyncio.sleep(0.8)
                await emitter.emit("brainstem", 0.0, "done", "hb")

    hb_task = asyncio.create_task(_heartbeat_with_ui())

    # Session trace accumulator for sleep consolidation
    session_traces: list[dict] = []
    # Full TurnTrace objects (carry fired_path, neuromod, draft_scores) for Hebbian pass
    session_traces_full: list = []

    # Background hippocampus encode tasks — must flush at shutdown
    pending_encodes: set[asyncio.Task] = set()

    def _track_encode(task: asyncio.Task) -> None:
        pending_encodes.add(task)
        def _done(t: asyncio.Task) -> None:
            pending_encodes.discard(t)
            exc = t.exception() if not t.cancelled() else None
            if exc:
                logger.error("Memory write failed for this turn — episode will not be saved to long-term memory: %s", exc)
        task.add_done_callback(_done)

    # ── Core turn processor ───────────────────────────────────────────────────
    from brain.brainstem import TURN_TIMEOUT

    async def process_turn(user_input: str, image_path: str | None = None) -> tuple[str, dict]:
        try:
            return await asyncio.wait_for(
                _process_turn_body(user_input, image_path),
                timeout=TURN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Turn timed out after %.1fs — sending fallback response. "
                "If Ollama is slow, increase BRAIN_TURN_TIMEOUT_SECONDS (currently %.1fs).",
                TURN_TIMEOUT, TURN_TIMEOUT,
            )
            timeout_msg = "I'm taking too long to think. Let me try again."
            try:
                brainstem.end_turn()
            except Exception:
                pass
            if emitter:
                # Best-effort UI signal that the turn ended
                try:
                    await emitter.emit_turn_end("timeout", timeout_msg, TURN_TIMEOUT, 0)
                except Exception:
                    pass
            if dmn:
                dmn.resume()
            return timeout_msg, {}

    async def _process_turn_body(user_input: str, image_path: str | None = None) -> tuple[str, dict]:
        if dmn:
            dmn.pause()

        turn = brainstem.begin_turn()
        turn_id = turn.turn_id
        obs.begin_turn(turn_id, user_input)

        # Bind a fresh TurnTrace as the current firing-path context so every
        # switch.fire() and integrator.call() this turn appends to its trace.
        trace = TurnTrace(
            turn_id=turn_id,
            session_id=session_id,
            user_input=user_input,
        )
        _ctx_token = set_current_trace(trace)

        # Notify UI of turn start
        if emitter:
            await emitter.emit_turn_start(turn_id, user_input, session_id=session_id)

        # Reset integrators
        temporal._understanding.reset_turn(turn_id)
        frontal._executive.reset_turn(turn_id)
        for d in frontal._drafters:
            d.reset_turn(turn_id)
        frontal._critic.reset_turn(turn_id)

        # PNS → bus
        await pns.receive_text(user_input, image_path)

        # ── Temporal: language understanding ──────────────────────────────────
        await _emit("temporal", 0.7, "parsing input", turn_id)
        features = await temporal.run(turn_id)
        await _emit_end("temporal", turn_id)

        if features is None:
            brainstem.end_turn()
            if emitter:
                await emitter.emit_turn_end(turn_id, "...", 0.0, 0)
            return "..."

        # ── Enrollment (multi-speaker, single shared mic) ─────────────────────
        if ears is not None:
            # 1. Collect auto-completions the auditory cortex already resolved
            #    (multi-speaker case — it knows which voice slice said which name).
            completed: list[dict] = []
            while True:
                try:
                    ec = enrollment_complete_inbox.get_nowait()
                    if not ec.expired and ec.payload.get("action") in ("enrolled", "merged"):
                        completed.append(ec.payload)
                except asyncio.QueueEmpty:
                    break

            # 2. Deterministic fallback: exactly one pending voice + a name in this
            #    turn's text → attribute it to that speaker. (Common 1-on-1 case.)
            pending = ears.enrollment_pending_speakers
            if pending and not completed:
                if _is_enrollment_cancellation(user_input):
                    for spk in pending:
                        ears.cancel_enrollment(spk.session_key)
                elif len(pending) == 1:
                    name = _extract_identity_name(user_input, features)
                    if name:
                        result = ears.complete_enrollment(pending[0].session_key, name)
                        if result.get("action") in ("enrolled", "merged"):
                            completed.append(result)
                            logger.info("Enrollment: %s '%s'", result["action"], name)

            if completed:
                features = dict(features)
                features["_enrollment_result"] = completed[0]
                features["_enrollment_results"] = completed

        # ── Surface latest speaker identity + song match into features ────────
        # Drain whatever the auditory cortex has emitted since the last turn;
        # keep the most recent valid payload of each kind.
        latest_speaker = None
        while True:
            try:
                sm = speaker_id_inbox.get_nowait()
                if not sm.expired and sm.payload.get("identified"):
                    latest_speaker = sm.payload
            except asyncio.QueueEmpty:
                break
        latest_song = None
        while True:
            try:
                mm = song_match_inbox.get_nowait()
                if not mm.expired and mm.payload.get("matched"):
                    latest_song = mm.payload
            except asyncio.QueueEmpty:
                break
        if latest_speaker or latest_song:
            features = dict(features)
            if latest_speaker and latest_speaker.get("speaker_name"):
                features["speaker_name"] = latest_speaker["speaker_name"]
            if latest_song:
                features["song_match"] = latest_song

        # ── Hypothalamus + Thalamus: parallel ─────────────────────────────────
        await _emit("hypothalamus", 0.6, "updating affect", turn_id)
        await _emit("thalamus", 0.55, "routing attention", turn_id)
        affect_task = asyncio.create_task(hypothalamus.process(features))
        thalamus_task = asyncio.create_task(thalamus.route(features, {}))
        results = await asyncio.gather(affect_task, thalamus_task, return_exceptions=True)
        affect, routing = results
        if isinstance(affect, BaseException):
            logger.warning("Emotion analysis failed — using neutral defaults: %s", affect)
            affect = {"emotion": "neutral", "user_emotion": "unknown"}
        if isinstance(routing, BaseException):
            logger.warning("Attention routing failed — using defaults: %s", routing)
            routing = {}
        await _emit_end("hypothalamus", turn_id)
        await _emit_end("thalamus", turn_id)

        # Surface live enrollment state to the frontal lobe (registry is source of truth)
        if ears is not None and isinstance(affect, dict):
            pending = ears.enrollment_pending_speakers
            affect["enrollment_pending"] = len(pending) > 0
            affect["enrollment_pending_count"] = len(pending)
            affect["enrollment_closest_match"] = pending[0].closest_match if pending else None

        # Emit emotion to UI
        if emitter and affect.get("emotion"):
            await emitter.emit_emotion(affect["emotion"])
            await emitter.emit_neuromod(bus.neuromod.snapshot())

        # ── Occipital: vision (only if image present) ─────────────────────────
        vision_features = None
        if image_path and features.get("requires_vision"):
            await _emit("occipital", 0.9, "processing image", turn_id)
            vision_features = await occipital.process(image_path, user_input, turn_id)
            await _emit_end("occipital", turn_id)

        # ── Hippocampus: recall ───────────────────────────────────────────────
        memory = {}
        if features.get("requires_memory") or features.get("epistemic_action"):
            await _emit("hippocampus", 0.75, "recalling memory", turn_id)
            memory = await hippocampus.recall(
                query=user_input,
                entities=features.get("entities", []),
                turn_id=turn_id,
                embedding_fn=router.embed,
            )
            await _emit_end("hippocampus", turn_id)
        else:
            memory = {"core": core_context, "schema": "", "episodes": ""}

        if vision_features:
            memory["vision"] = (
                f"Image: {vision_features.get('description', '')}\n"
                f"Text in image: {vision_features.get('text_in_image', '')}\n"
                f"Context: {vision_features.get('context_for_response', '')}"
            )

        # ── Motor Cortex: tool execution (only when action required) ──────────
        if motor:
            cloud = getattr(motor, "_cloud", None)

            # Check if user is responding to a pending write confirmation
            if cloud and cloud.has_pending:
                raw_text = features.get("raw_text", user_input)
                if cloud.is_user_confirming(raw_text):
                    await _emit("motor_cortex", 0.9, "executing confirmed action", turn_id)
                    try:
                        tool_result = await cloud.execute_pending(turn_id)
                        if tool_result:
                            output = tool_result.get("output", "")
                            memory["tool_result"] = f"[cloud_action — confirmed]\n{output}"
                            logger.info("[CloudExecutor] Confirmed write executed (success=%s)",
                                        tool_result.get("success"))
                    except Exception as _ce:
                        logger.error("Cloud executor failed on confirmed write: %s", _ce)
                    await _emit_end("motor_cortex", turn_id)
                elif cloud.is_user_denying(raw_text):
                    cloud.clear_pending()
                    memory["tool_result"] = "[cloud_action — cancelled by user]"
                    logger.info("[CloudExecutor] Pending write action cancelled by user")

            # Normal tool execution when action is required this turn
            elif features.get("requires_action"):
                await _emit("motor_cortex", 0.85, "executing tool", turn_id)
                motor.reset_turn(turn_id)
                try:
                    tool_result = await motor.execute(features, turn_id)
                    if tool_result:
                        output = tool_result.get("output", "")
                        tool_name = tool_result.get("tool", "tool")
                        if tool_result.get("pending"):
                            # Write action queued — inject confirmation prompt into context
                            desc = output.replace("CONFIRMATION_NEEDED:", "").strip()
                            memory["tool_result"] = (
                                f"[confirmation_needed]\n"
                                f"You are about to: {desc}\n"
                                "Ask the user to confirm before proceeding."
                            )
                        else:
                            memory["tool_result"] = f"[{tool_name}]\n{output}"
                        logger.info("[MotorCortex] %s → %d chars (success=%s)",
                                    tool_name, len(output), tool_result.get("success"))
                except Exception as _mc_err:
                    logger.error("Motor cortex failed this turn: %s", _mc_err)
                await _emit_end("motor_cortex", turn_id)

        parietal_context = parietal.recent_turns_text()

        if dmn:
            dmn.update_context(parietal_context, affect.get("emotion", "neutral"),
                               core_context.get("self", ""))

        # ── Egress pseudonymisation (Phase 0H) ────────────────────────────────
        if EGRESS_MODE != "off":
            ps_memory = dict(memory)
            ps_schema, _ = egress.pseudonymize(memory.get("schema", ""))
            ps_episodes, _ = egress.pseudonymize(memory.get("episodes", ""))
            ps_core_self, _ = egress.pseudonymize(memory.get("core", {}).get("self", ""))
            ps_core_user, _ = egress.pseudonymize(memory.get("core", {}).get("user", ""))
            ps_memory["schema"] = ps_schema
            ps_memory["episodes"] = ps_episodes
            ps_core = dict(memory.get("core", {}))
            ps_core["self"] = ps_core_self
            ps_core["user"] = ps_core_user
            ps_memory["core"] = ps_core
            ps_user_input, _ = egress.pseudonymize(user_input)
            ps_parietal_context, _ = egress.pseudonymize(parietal_context)
        else:
            ps_memory = memory
            ps_user_input = user_input
            ps_parietal_context = parietal_context

        # ── Frontal: Multiple Drafts engine ───────────────────────────────────
        draft_scores: list[dict] = []
        if not brainstem.check_budget():
            response = "I've reached my thinking limit for this turn."
        else:
            await _emit("frontal", 0.9, "drafting response", turn_id)
            # Pass pseudonymised context to cloud frontal cells
            ps_features = dict(features)
            ps_affect = affect
            if EGRESS_MODE != "off":
                ps_features["raw_text"] = ps_user_input
                # Pseudonymise speaker name + enrollment names so PII never reaches the cloud.
                if ps_features.get("speaker_name"):
                    ps_name, _ = egress.pseudonymize(
                        ps_features["speaker_name"], known_entities=[ps_features["speaker_name"]]
                    )
                    ps_features["speaker_name"] = ps_name
                for key in ("_enrollment_result", "_enrollment_results"):
                    val = ps_features.get(key)
                    if not val:
                        continue
                    items = val if isinstance(val, list) else [val]
                    ps_items = []
                    for item in items:
                        if isinstance(item, dict) and item.get("name"):
                            ps_item = dict(item)
                            ps_item["name"], _ = egress.pseudonymize(
                                item["name"], known_entities=[item["name"]]
                            )
                            ps_items.append(ps_item)
                        else:
                            ps_items.append(item)
                    ps_features[key] = ps_items if isinstance(val, list) else ps_items[0]
                # affect.appraisal is templated free text that may echo names —
                # run it through the gateway so vault tokens stay consistent.
                if affect.get("appraisal"):
                    ps_appraisal, _ = egress.pseudonymize(affect["appraisal"])
                    ps_affect = dict(affect)
                    ps_affect["appraisal"] = ps_appraisal
            response = await frontal.process(ps_features, ps_affect, ps_memory, ps_parietal_context, turn_id)
            # Capture draft scores immediately (single-threaded asyncio — no race)
            draft_scores = list(frontal.last_turn_draft_scores)
            # Restore real values in response before delivering to user
            response = egress.depseudonymize(response)
            if egress.vault_size > 0:
                logger.debug("Egress: %s", egress.audit_summary())
            await _emit_end("frontal", turn_id)

        # ── Brainstem: articulation ───────────────────────────────────────────
        await _emit("brainstem", 0.4, "articulating", turn_id)
        if not turn.committed:
            brainstem.add_draft(f"final_{turn_id}", response, 0.9)
            brainstem.endorse(f"final_{turn_id}")
        final = await brainstem.articulation_gate(turn)
        await _emit_end("brainstem", turn_id)

        # Post-turn housekeeping
        await _emit("parietal", 0.3, "updating context", turn_id)
        parietal.update(features, user_input, final)
        await _emit_end("parietal", turn_id)
        hypothalamus.decay_turn()
        turn_result = brainstem.end_turn()

        nm_snap = bus.neuromod.snapshot()
        llm_calls = len(router._call_log)

        # ── Per-cluster token breakdown (from call log accumulated this turn) ─
        cluster_tokens: dict[str, dict] = {}
        for _entry in router._call_log:
            _cl = _entry.get("cluster", "unknown")
            if _cl not in cluster_tokens:
                cluster_tokens[_cl] = {"in": 0, "out": 0, "calls": 0}
            cluster_tokens[_cl]["in"] += _entry.get("in", 0)
            cluster_tokens[_cl]["out"] += _entry.get("out", 0)
            cluster_tokens[_cl]["calls"] += 1

        memory_recalled = bool(memory.get("episodes") or memory.get("schema"))
        memory_hit_count = len([l for l in (memory.get("episodes") or "").splitlines() if l.strip()])

        selected_draft = next((d for d in draft_scores if d.get("selected")), {})
        selected_coherence = selected_draft.get("coherence", 0.5)
        selected_emotional_fit = (selected_draft.get("empathy_score")
                                  or selected_draft.get("tone_fit", 0.5))
        selected_draft_id = selected_draft.get("draft_id", "")

        # Final neuromod push to UI
        if emitter:
            await emitter.emit_neuromod(nm_snap)
            await emitter.emit_turn_end(turn_id, final, turn_result.elapsed(), llm_calls)

        # ── Quality badge to UI (free — from internal critic scores) ─────────
        if emitter and draft_scores:
            try:
                await emitter.emit_event({
                    "type": "quality_score",
                    "turn_id": turn_id,
                    "score": round(selected_draft.get("overall", 0.5), 3),
                    "coherence": round(selected_coherence, 3),
                    "emotional_fit": round(selected_emotional_fit, 3),
                    "drafter_count": len(draft_scores),
                    "memory_used": memory_recalled,
                })
            except Exception as _qe:
                logger.debug("quality_score emit failed: %s", _qe)

        # Observability — fill the trace bound at turn start (carries fired_path,
        # predictor_outcomes, llm_calls_saved, gating_bypassed_count)
        trace.response = final
        trace.llm_calls = llm_calls
        trace.elapsed_s = turn_result.elapsed()
        trace.emotion = affect.get("emotion", "neutral")
        trace.emotion_core = core_of(affect.get("emotion", "neutral"))
        trace.neuromod = nm_snap
        trace.draft_scores = draft_scores
        trace.selected_draft_id = selected_draft_id
        trace.drafter_count = len(draft_scores)
        trace.cluster_tokens = cluster_tokens
        trace.memory_recalled = memory_recalled
        trace.memory_hit_count = memory_hit_count
        obs.record_turn(trace)

        # Append the full trace (with fired_path, neuromod, emotion etc.) so
        # sleep consolidation can apply Hebbian updates along the path.
        session_traces_full.append(trace)

        # ── Background eval tasks (non-blocking) ─────────────────────────────
        if baseline_runner:
            memory_ctx = ((memory.get("episodes") or "") + "\n"
                          + (memory.get("schema") or ""))
            baseline_runner.fire(
                turn_id, user_input, final,
                memory_ctx[:1000], selected_coherence, selected_emotional_fit,
            )

        if meta:
            meta.record_turn(
                turn_id=turn_id,
                llm_calls=llm_calls,
                elapsed_s=turn_result.elapsed(),
                emotion=affect.get("emotion", "neutral"),
                neuromod=nm_snap,
                surprise_score=features.get("surprise_score", 0.5),
                features=features,
                draft_scores=draft_scores,
            )

        session_traces.append({
            "user_input": user_input,
            "entity_response": final,
            "emotion": affect.get("emotion", "neutral"),
            "topic_tags": features.get("entities", []),
        })

        # Hippocampus: encode in background (tracked for shutdown flush)
        await _emit("hippocampus", 0.45, "encoding episode", turn_id)
        encode_task = asyncio.create_task(hippocampus.encode(
            session_id=session_id,
            turn_id=turn_id,
            user_input=user_input,
            entity_response=final,
            features=features,
            affect=affect,
            neuromod_snap=nm_snap,
            surprise_score=features.get("surprise_score", 0.5),
            embedding_fn=router.embed,
        ))
        _track_encode(encode_task)

        if dmn:
            dmn.resume()

        logger.info("Turn %s: %d LLM calls | %.2fs | emotion=%s",
                    turn_id, llm_calls, turn_result.elapsed(), affect.get("emotion"))

        # Release the firing-path context binding for this turn
        try:
            reset_current_trace(_ctx_token)
        except Exception:
            pass

        return final, affect

    # ── Run modes ─────────────────────────────────────────────────────────────
    if args.message:
        response, affect = await process_turn(args.message)
        await pns.emit(response, affect)

    elif ui_enabled:
        # Browser drives the conversation via WebSocket
        print("Brain online. Open http://localhost:8765 to interact.\n")
        while True:
            try:
                user_input = await asyncio.wait_for(ui_message_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if not user_input:
                continue

            image_path = None
            if "[image:" in user_input:
                import re
                m = re.search(r'\[image:([^\]]+)\]', user_input)
                if m:
                    image_path = m.group(1).strip()
                    user_input = user_input.replace(m.group(0), "").strip()

            response, affect = await process_turn(user_input, image_path)
            # response already sent to browser via turn_end event;
            # also speak it aloud if voice mode is enabled
            await _emit("motor", 0.7, "articulating", "speak")
            await _emit("brainstem", 0.35, "speaking", "speak")
            await pns.emit(response, affect)
            await _emit_end("motor", "speak")
            await _emit_end("brainstem", "speak")

    else:
        # CLI REPL
        print("Brain online. Type your message, or 'quit' to exit.\n")
        while True:
            try:
                if args.voice:
                    if streaming_mic is None:
                        # Streaming session failed to start — surface the error and exit
                        print("Voice input is offline. Check DEEPGRAM_API_KEY and mic permissions.")
                        break
                    await _emit("temporal", 0.4, "listening...", "mic")
                    await _emit("brainstem", 0.15, "listening...", "mic")
                    utt = await streaming_mic.next_utterance()
                    await _emit_end("temporal", "mic")
                    await _emit_end("brainstem", "mic")
                    user_input = (utt.get("transcript") or "").strip()
                    if not user_input:
                        continue
                    print(f"You: {user_input}")
                else:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("You: ").strip()
                    )
            except (EOFError, KeyboardInterrupt):
                print("\nSession ending...")
                break

            if user_input.lower() in ("quit", "exit", "bye"):
                print("Brain: Goodbye.")
                break
            if not user_input:
                continue

            image_path = None
            if "[image:" in user_input:
                import re
                m = re.search(r'\[image:([^\]]+)\]', user_input)
                if m:
                    image_path = m.group(1).strip()
                    user_input = user_input.replace(m.group(0), "").strip()

            response, affect = await process_turn(user_input, image_path)
            await _emit("motor", 0.7, "articulating", "speak")
            await _emit("brainstem", 0.35, "speaking", "speak")
            await pns.emit(response, affect)
            await _emit_end("motor", "speak")
            await _emit_end("brainstem", "speak")

    # ── Shutdown ──────────────────────────────────────────────────────────────
    hb_task.cancel()

    if voice_bridge_task is not None:
        voice_bridge_task.cancel()
        try:
            await voice_bridge_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        voice_drain_task.cancel()  # type: ignore[name-defined]
        await voice_drain_task     # type: ignore[name-defined]
    except (asyncio.CancelledError, Exception, NameError):
        pass

    if streaming_mic is not None:
        try:
            await streaming_mic.stop()
        except Exception as _e:
            logger.debug("streaming mic shutdown error: %s", _e)

    # Flush pending hippocampus encodes so episodes aren't lost on exit
    if pending_encodes:
        logger.info("Waiting for %d in-progress memory writes to finish before exit...", len(pending_encodes))
        await asyncio.gather(*pending_encodes, return_exceptions=True)

    if session_traces:
        try:
            from brain.sleep import SleepConsolidation
            sleep = SleepConsolidation(router, hippocampus._schema, hippocampus._episodic,
                                       wiring=wiring)
            logger.info("Running end-of-session memory consolidation (summarising facts, updating self-model, applying Hebbian updates)...")
            await sleep.consolidate(session_id, session_traces, full_traces=session_traces_full)
        except Exception as e:
            logger.warning("End-of-session memory consolidation failed — recent facts may not be saved: %s", e)

    obs.flush()
    logger.info("Session %s complete. Total LLM calls: %d",
                session_id, brainstem._session_cost_calls)

    if meta:
        summary = meta.summary()
        if summary:
            print(f"\nSession stats: {summary.get('turn_count')} turns | "
                  f"avg {summary.get('avg_llm_calls')} LLM calls | "
                  f"dominant emotion: {summary.get('dominant_emotion')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Biologically-inspired AI brain")
    parser.add_argument("--message", "-m", help="Single-turn message (non-interactive)")
    parser.add_argument("--voice", action="store_true", help="Enable voice I/O")
    parser.add_argument("--ui", action="store_true", help="Enable browser UI at :8765")
    parser.add_argument("--dmn", action="store_true", help="v0.2: Enable Default Mode Network")
    parser.add_argument("--metacognition", action="store_true",
                        help="v0.3: Enable metacognition cell")
    parser.add_argument("--ears", action="store_true",
                        help="v0.4: Enable Auditory Cortex (fingerprinting, speaker ID, prosody)")
    parser.add_argument("--motor", action="store_true",
                        help="v0.5: Enable Motor Cortex (tool use: file I/O, shell commands)")
    args = parser.parse_args()

    if args.voice:
        os.environ["BRAIN_VOICE_MODE"] = "true"
    if args.dmn:
        os.environ["BRAIN_DMN"] = "true"
    if args.metacognition:
        os.environ["BRAIN_METACOGNITION"] = "true"
    if args.ui:
        os.environ["BRAIN_UI"] = "true"
    if args.ears:
        os.environ["BRAIN_EARS"] = "true"
    if args.motor:
        os.environ["BRAIN_MOTOR"] = "true"

    asyncio.run(session(args))


if __name__ == "__main__":
    main()
