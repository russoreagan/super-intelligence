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
import contextlib
import logging
import os
import time
import uuid

from dotenv import load_dotenv

load_dotenv(override=True)  # override=True so .env values win over empty shell exports

logging.basicConfig(
    level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
from brain.emotion_hierarchy import core_of  # noqa: E402
from brain.security import SecretRedactingFilter  # noqa: E402
from brain.settings import settings as _brain_settings  # noqa: E402
from brain.utils import get_idle_seconds  # noqa: E402

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
    from brain.brainstem import Brainstem
    from brain.bus import Bus
    from brain.clusters.frontal import FrontalCluster
    from brain.clusters.hippocampus import HippocampusCluster
    from brain.clusters.hypothalamus import HypothalamusCluster
    from brain.clusters.occipital import OccipitalCluster
    from brain.clusters.parietal import ParietalCluster
    from brain.clusters.temporal import TemporalCluster
    from brain.clusters.thalamus import ThalamusCluster
    from brain.model_router import ModelRouter
    from brain.observability.decisions import decisions as decisions_log
    from brain.observability.firing_path import reset_current_trace, set_current_trace
    from brain.observability.timeline import ObservabilityLayer, TurnTrace
    from brain.pns import PNS
    from brain.wiring import Wiring
    from brain.wiring_bootstrap import bootstrap as wiring_bootstrap

    session_id = str(uuid.uuid4())[:8]
    logger.info("Session %s starting", session_id)

    bus = Bus()

    # ── Eval system (always-on JSONL logging; baseline/scorer gated by env flags) ──
    eval_logger = None
    baseline_runner = None
    posthoc_scorer = None
    emotion_judge = None
    learning_monitor = None
    learning_judge = None
    try:
        from eval.baseline import BaselineRunner
        from eval.emotion_judge import EmotionJudge
        from eval.learning_judge import LearningJudge
        from eval.learning_monitor import LearningMonitor
        from eval.scorer import PostHocScorer
        from eval.turn_logger import EvalLogger
        eval_logger = EvalLogger()
        baseline_runner = BaselineRunner(eval_logger)
        posthoc_scorer = PostHocScorer(eval_logger)
        baseline_runner._scorer = posthoc_scorer
        emotion_judge = EmotionJudge(eval_logger)
        learning_monitor = LearningMonitor()
        learning_judge = LearningJudge(eval_logger)
        logger.info("Eval: logging to %s", eval_logger._path)
    except Exception as _eval_err:
        logger.debug("Eval system unavailable: %s", _eval_err)

    obs = ObservabilityLayer(session_id, eval_logger=eval_logger)
    if baseline_runner is not None:
        baseline_runner._obs = obs
    if posthoc_scorer is not None:
        posthoc_scorer._obs = obs
    if emotion_judge is not None:
        emotion_judge._obs = obs
    if learning_monitor is not None:
        learning_monitor._obs = obs
    if learning_judge is not None:
        learning_judge._obs = obs
    router = ModelRouter(obs=obs)
    brainstem = Brainstem(bus, router)
    # Delay (seconds) between TTS ending and mic re-opening.  Deepgram's
    # Delay (seconds) between TTS ending and mic re-opening.  The main use
    # case is speaker users with acoustic echo: the last words the brain
    # said still ring in the room for ~200-400 ms, and Deepgram needs a
    # moment to flush those frames before real user speech arrives.
    # Headphone users don't need any delay — set BRAIN_MIC_UNMUTE_DELAY_S=0
    # or use BRAIN_MIC_MUTE_DURING_TTS=false to skip muting entirely.
    _MIC_UNMUTE_DELAY_S = float(os.environ.get("BRAIN_MIC_UNMUTE_DELAY_S", "0.3"))
    # True only when TTS auto-muted the mic (i.e. mic was NOT already muted
    # by the user when TTS started).  Auto-unmute must only run if we were
    # the ones that muted — never override a deliberate user mute.
    _tts_did_mute = False

    def _on_speaking_change(active: bool) -> None:
        nonlocal _tts_did_mute
        if emitter:
            asyncio.ensure_future(emitter.emit_event({"type": "speaking", "active": active}))
        # Mute the mic while TTS is playing so Deepgram never transcribes
        # the brain's own voice.  This is the definitive fix for the
        # recurring "mic stops after first utterance" bug — bleed-through
        # transcript comparison was too fragile and kept being removed.
        #
        # Set BRAIN_MIC_MUTE_DURING_TTS=false to disable (headphone users
        # who rely on voice barge-in during playback).
        _mute_enabled = os.environ.get("BRAIN_MIC_MUTE_DURING_TTS", "true").lower() != "false"
        if _mute_enabled and streaming_mic is not None:
            if active:
                if not streaming_mic.is_muted:
                    # Mic was live — auto-mute it and remember we did so.
                    streaming_mic.mute()
                    _tts_did_mute = True
                    if emitter:
                        asyncio.ensure_future(emitter.emit_event({
                            "type": "voice_mode", "active": True, "muted": True,
                        }))
                else:
                    # User already had the mic muted — leave it alone.
                    _tts_did_mute = False
            else:
                if _tts_did_mute:
                    _tts_did_mute = False
                    async def _unmute_after_drain() -> None:
                        await asyncio.sleep(_MIC_UNMUTE_DELAY_S)
                        if streaming_mic is not None:
                            streaming_mic.unmute()
                            # Sync the UI so the user sees the mic come back live.
                            if emitter:
                                await emitter.emit_event({
                                    "type": "voice_mode", "active": True, "muted": False,
                                })
                    asyncio.ensure_future(_unmute_after_drain())

    pns = PNS(bus, on_speaking_change=_on_speaking_change)

    # Idle-time gate for proactive TTS: don't speak aloud if the user hasn't
    # touched the computer in this many seconds. Configurable via env var.
    # Set to 0 to disable the gate entirely.
    PROACTIVE_IDLE_THRESHOLD = _brain_settings.get("proactive_idle_threshold") or float(
        os.environ.get("BRAIN_PROACTIVE_IDLE_THRESHOLD", "180")  # 3 minutes
    )

    # Response-window gate: after the brain finishes speaking, hold proactive
    # thoughts for this many seconds so the user has time to reply — especially
    # important when the brain just asked a question.
    PROACTIVE_RESPONSE_WINDOW = _brain_settings.get("proactive_response_window") or float(
        os.environ.get("BRAIN_PROACTIVE_RESPONSE_WINDOW", "8")
    )
    _last_brain_spoke_ts: float = 0.0  # updated every time pns.emit completes
    _last_turn_ts: float = time.time()  # updated at end of each user turn; used to detect return-after-absence

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

    from brain.security import EGRESS_MODE, PseudonymizationGateway
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
                             on_interrupt=pns.interrupt,
                             python_voice_mode=_voice_flag,
                             wiring=wiring)
        ui_server.set_wiring_frozen(wiring_frozen)
        brainstem.register_loop("ui_server", lambda: ui_server.start(port=8765), restart_on_crash=False)
        # Give the server a moment to bind
        await asyncio.sleep(0.3)

    async def _emit(cluster: str, intensity: float, note: str, turn_id: str = "") -> None:
        if emitter:
            await emitter.emit(cluster, intensity, note, turn_id)
        if turn_id:
            obs.begin_cluster(turn_id, cluster, note)

    async def _emit_end(cluster: str, turn_id: str = "") -> None:
        if emitter:
            await emitter.emit(cluster, 0.0, "done", turn_id)
        if turn_id:
            obs.end_cluster(turn_id, cluster)

    # ── Motor Cortex (tool use) ───────────────────────────────────────────────
    motor = None
    if args.motor or os.environ.get("BRAIN_MOTOR", "false").lower() == "true":
        from brain.clusters.cloud_executor import CloudExecutor
        from brain.clusters.motor_cortex import MotorCortexCluster

        _motor_paths_raw = os.environ.get("BRAIN_MOTOR_PATHS", "")
        _motor_paths = [p.strip() for p in _motor_paths_raw.split(":") if p.strip()]
        _motor_cmds_raw = os.environ.get("BRAIN_MOTOR_COMMANDS", "")
        _motor_cmds = set(_motor_cmds_raw.split(":")) if _motor_cmds_raw else None

        cloud = CloudExecutor(bus, schema_store=hippocampus._schema)

        # If no explicit paths set, inherit whatever the user granted Claude Desktop
        if not _motor_paths and cloud._trusted_dirs:
            _motor_paths = cloud._trusted_dirs[:]
            logger.info("Motor cortex: inheriting trusted dirs from Claude Desktop: %s",
                        _motor_paths)

        motor = MotorCortexCluster(bus, router,
                                   allowed_paths=_motor_paths,
                                   allowed_commands=_motor_cmds,
                                   cloud_executor=cloud)
        if _motor_paths:
            logger.info("Motor cortex online. Allowed paths: %s", _motor_paths)
        else:
            logger.warning(
                "Motor cortex enabled but no project paths are accessible — "
                "add paths via BRAIN_MOTOR_PATHS or Claude Desktop trusted folders."
            )

        # Register frontal and motor subsystems
        from brain.clusters.follow_through import FollowThrough, ResultReporter
        from brain.clusters.frontal_task import FrontalTaskSubsystem, PendingTask
        from brain.clusters.lobe_bridge import LobeBridge
        from brain.clusters.motor_memory import MuscleMemorySubsystem
        from brain.clusters.task_queue import PersistentTaskQueue
        pending_task = PendingTask()
        motor.set_pending_task(pending_task)
        frontal.register_subsystem(FrontalTaskSubsystem(pending_task))
        motor.register_subsystem(MuscleMemorySubsystem())
        follow_through = FollowThrough(router)
        result_reporter = ResultReporter(router)
        task_queue = PersistentTaskQueue()
        # Re-queue any tasks that were pending or mid-execution when the brain
        # last shut down (page refresh, restart, crash).
        _recovered = task_queue.recover_interrupted()
        if _recovered:
            logger.info("[TaskQueue] %d task(s) recovered from previous session: %s",
                        len(_recovered), "; ".join(t.goal[:60] for t in _recovered))

        # Wire lobe capabilities into the motor cortex so background jobs can
        # request visual analysis or episodic memory recall as named tools.
        lobe_bridge = LobeBridge()

        async def _recall_memory(*, topic: str, entities: list, turn_id: str) -> str:
            result = await hippocampus.recall(topic, entities, turn_id, router.embed)
            parts: list[str] = []
            if result.get("episodes"):
                parts.append(f"Relevant episodes:\n{result['episodes']}")
            if result.get("schema"):
                parts.append(f"Known facts:\n{result['schema']}")
            return "\n\n".join(parts) or "(no relevant memories found)"

        async def _analyze_image(*, path: str, question: str, turn_id: str) -> str:
            result = await occipital.process(path, question, turn_id)
            if result is None:
                return "[error] Could not analyze image (occipital returned nothing)"
            # Flatten vision features into readable text for the planner
            parts: list[str] = []
            for key in ("description", "caption", "objects", "text_content", "scene"):
                val = result.get(key)
                if val:
                    parts.append(f"{key}: {val}")
            return "\n".join(parts) or str(result)

        lobe_bridge.register("recall_memory", _recall_memory)
        lobe_bridge.register("analyze_image", _analyze_image)
        motor.set_lobe_bridge(lobe_bridge)
        motor.set_observability(obs)

        # Surface capabilities into drafter prompts so the entity can answer
        # "what tools do you have?" accurately instead of confabulating.
        cap_lines = ["Tool use is ENABLED via the motor cortex. You can:"]
        if _motor_paths:
            cap_lines.append(
                f"- Read / write / list / search files within: {', '.join(_motor_paths)}"
            )
            cap_lines.append("- Run safe shell commands (git, ls, grep, etc.) in those paths")
        else:
            cap_lines.append(
                "- (Filesystem tools are blocked — BRAIN_MOTOR_PATHS is unset)"
            )
        if cloud and cloud.available:
            cap_lines.append(
                "- Invoke Claude Code as a cloud agent for tasks requiring external services "
                "(email, calendar, messages, web search, documents, etc.). Available "
                f"connectors: {cloud.connectors_summary()}."
            )
            cap_lines.append(
                "When the user asks to 'use Claude', 'ask Claude', 'access my X', "
                "'send a message to Y', etc., the brain dispatches a cloud_action and "
                "you get the result back as 'Tool execution result' in your context."
            )
        frontal.set_capabilities("\n".join(cap_lines))
    else:
        frontal.set_capabilities(
            "Tool use is DISABLED this session (motor cortex not enabled). "
            "If asked to use external tools, explain that you'd need to be "
            "restarted with --motor."
        )

    # ── v0.2: Default Mode Network ────────────────────────────────────────────
    dmn = None
    if args.dmn or os.environ.get("BRAIN_DMN", "false").lower() == "true":
        from brain.dmn import DefaultModeNetwork
        dmn = DefaultModeNetwork(bus, router, hippocampus, parietal, obs=obs)

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

        # Seed the DMN with the active projects manifest so it knows what work
        # is pre-authorized without needing to ask. Refreshed once at boot;
        # if open_questions.md changes mid-session the next boot picks it up.
        try:
            _oq_text = hippocampus._schema.read("open_questions.md")
            if _oq_text:
                dmn.set_projects_context(_oq_text)
                logger.info("[DMN] Projects context loaded (%d chars)", len(_oq_text))
        except Exception as _oq_err:
            logger.warning("[DMN] Could not load projects context: %s", _oq_err)

    # Forward DMN stream-of-consciousness thoughts to the UI panel (shown as
    # italic, unspeakable thought bubbles between turns).
    if emitter:
        _thought_inbox: asyncio.Queue = bus.subscribe("stream.thought")

        async def _forward_thoughts() -> None:
            while True:
                msg = await _thought_inbox.get()
                thought = msg.payload.get("thought", "") if not msg.expired else ""
                chem_delta = msg.payload.get("chem_delta", {}) if not msg.expired else {}
                if thought:
                    await emitter.emit_stream_thought(thought, chem_delta=chem_delta)

        brainstem.register_loop("forward_thoughts", _forward_thoughts)

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
        brainstem.register_loop("ears", ears.run)

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

    # ── Speak gate (DMN proactive utterance evaluator) ────────────────────────
    # Drains DMN's candidate queue, applies fast heuristic gates, then asks
    # the judge LLM to make a yes/wait/drop call on borderline ones. "yes"
    # promotes the candidate into _proactive_q where the existing main-loop
    # drain (around line 1037) routes it to TTS.
    #
    # Lives here so it can close over pns, streaming_mic, ui_message_queue,
    # and _last_brain_spoke_ts — the speak-appropriateness signals that
    # already exist in this function's scope. The gate runs continuously
    # alongside the DMN's tick loop.
    if dmn is not None:
        SPEAK_GATE_INTERVAL = float(_brain_settings.get("speak_gate_poll_interval") or 5.0)
        SPEAK_CAND_MAX_AGE = float(_brain_settings.get("speak_candidate_max_age_s") or 60.0)

        async def _speak_gate_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(SPEAK_GATE_INTERVAL)
                    if dmn.candidate_count() == 0:
                        continue
                    # Snapshot all the appropriateness signals once per poll.
                    now = time.time()
                    since_last_spoke = now - _last_brain_spoke_ts
                    idle_s = get_idle_seconds()
                    user_active = (PROACTIVE_IDLE_THRESHOLD <= 0
                                   or idle_s < PROACTIVE_IDLE_THRESHOLD)
                    # If the user has been OS-idle past the threshold, we
                    # don't speak ANY candidate this cycle. Internal thoughts
                    # keep flowing in the UI — only TTS is suppressed.
                    if not user_active:
                        # Drop any candidate that's already aged out; leave
                        # fresh ones in the queue for when the user returns.
                        while dmn.candidate_count() > 0:
                            c = dmn.take_oldest_candidate()
                            if c is None:
                                break
                            age = now - float(c.get("created_ts", now))
                            if age <= SPEAK_CAND_MAX_AGE:
                                dmn.return_candidate(c)
                                break
                        continue
                    # Hard heuristic blockers — don't even ask the judge.
                    if pns.is_speaking or not ui_message_queue.empty():
                        continue
                    if streaming_mic is not None and getattr(
                            streaming_mic, "is_user_speaking", False):
                        continue
                    if since_last_spoke < PROACTIVE_RESPONSE_WINDOW:
                        continue
                    # All clear — evaluate one candidate per cycle (oldest
                    # first). Drop expired candidates as we encounter them.
                    while dmn.candidate_count() > 0:
                        c = dmn.take_oldest_candidate()
                        if c is None:
                            break
                        age = now - float(c.get("created_ts", now))
                        if age > SPEAK_CAND_MAX_AGE:
                            logger.info(
                                "[Speak gate] Dropping aged candidate "
                                "(age=%.0fs > %.0fs): %r",
                                age, SPEAK_CAND_MAX_AGE,
                                (c.get("spoken") or "")[:60],
                            )
                            continue  # discard, look at the next one
                        verdict, reason = await dmn.judge_candidate(c)
                        logger.info(
                            "[Speak gate] verdict=%s reason=%s candidate=%r",
                            verdict, reason, (c.get("spoken") or "")[:60],
                        )
                        if verdict == "yes":
                            # Local-only bridge rewrite: if the candidate is
                            # a clear tangent, rewrite the spoken form via
                            # Ollama so the change-of-subject lands smoothly.
                            # Returns the original on any failure — never
                            # blocks the commit.
                            try:
                                bridged = await dmn.bridge_if_needed(c)
                                if bridged and bridged != c.get("spoken"):
                                    c["spoken"] = bridged
                            except Exception as _bridge_err:
                                logger.debug("[Speak gate] Bridge step failed: %s",
                                             _bridge_err)
                            dmn.commit_candidate_to_speech(c)
                        elif verdict == "wait":
                            dmn.return_candidate(c)
                        # "drop" → already popped, just don't return
                        break  # one judge call per poll cycle
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Let the exception propagate so brainstem's supervisor
                    # can apply exponential backoff and restart the loop.
                    raise

        brainstem.register_loop("speak_gate", _speak_gate_loop)

    # ── Voice → UI bridge: forward utterances as turns ──────────────────────
    # In UI+voice mode the browser drives the conversation via ui_message_queue.
    # Without this bridge, voice utterances accumulate in streaming_mic.utterances
    # forever and the brain appears to ignore everything spoken.
    #
    # Behaviour:
    #   - Brain NOT speaking → dispatch immediately.
    #   - Brain IS speaking → interrupt TTS and dispatch.
    # Everything the user says is sent; only empty transcripts (background noise)
    # are dropped.
    from brain.voice_bridge import (  # noqa: E402
        classify_utterance,
        parse_barge_words,
        pick_dispatch_from_queue,
    )
    barge_in_words = parse_barge_words(os.environ.get("BRAIN_BARGE_IN_WORDS"))

    voice_bridge_task = None  # will be a LoopState if voice bridge starts
    if streaming_mic is not None and ui_enabled:
        pending_during_tts: list[str] = []
        pending_lock = asyncio.Lock()

        async def _dispatch_text(text: str) -> None:
            logger.info("[I/O] voice → turn: %r", text[:80])
            if emitter:
                with contextlib.suppress(Exception):
                    await emitter.emit_event({
                        "type": "transcript", "text": text, "final": True,
                    })
            await ui_message_queue.put(text)

        async def _drain_pending_when_tts_ends() -> None:
            """When TTS finishes, flush queued utterances — minus any bleed."""
            was_speaking = False
            while True:
                try:
                    await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    return
                now_speaking = pns.is_speaking
                if was_speaking and not now_speaking:
                    async with pending_lock:
                        # If mic was muted while TTS was playing, discard the
                        # queue — user switched to text input and doesn't want
                        # stale voice utterances dispatched after TTS ends.
                        if streaming_mic.is_muted:
                            if pending_during_tts:
                                logger.debug("[I/O] voice → discarded %d queued utterance(s) (mic muted)",
                                             len(pending_during_tts))
                            pending_during_tts.clear()
                        else:
                            text, n = pick_dispatch_from_queue(pending_during_tts)
                            pending_during_tts.clear()
                            if text:
                                logger.info("[I/O] voice → flushing %d queued utterance(s): %r",
                                            n, text[:80])
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
                # Guard: if the user muted between when this utterance was
                # generated and when we dequeued it, discard it silently.
                # mute() clears _utterance_start_s/_pending_words but can't
                # reach utterances already sitting in the queue.
                if streaming_mic.is_muted:
                    logger.debug("[I/O] voice → discarded stale utterance (mic muted)")
                    continue
                text = (utt.get("transcript") or "").strip()

                decision, _ = classify_utterance(
                    text,
                    brain_is_speaking=pns.is_speaking,
                    barge_words=barge_in_words,
                )

                if decision == "drop_empty":
                    continue
                if decision == "barge_in":
                    pns.interrupt()
                    await _dispatch_text(text)
                    continue
                if decision == "queue":
                    async with pending_lock:
                        pending_during_tts.append(text)
                    logger.info("[I/O] voice → queued during TTS: %r", text[:60])
                    continue
                await _dispatch_text(text)

        voice_bridge_task = brainstem.register_loop("voice_bridge", _voice_bridge)
        brainstem.register_loop("tts_drain", _drain_pending_when_tts_ends)

    # Brainstem heartbeat — pulses the UI and logs loop health every 60 s
    async def _heartbeat_with_ui() -> None:
        while True:
            await asyncio.sleep(60)
            await brainstem.heartbeat_once(emitter=emitter)

    brainstem.register_loop("heartbeat", _heartbeat_with_ui)

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
        except TimeoutError:
            logger.warning(
                "Turn timed out after %.1fs — sending fallback response. "
                "If Ollama is slow, increase BRAIN_TURN_TIMEOUT_SECONDS (currently %.1fs).",
                TURN_TIMEOUT, TURN_TIMEOUT,
            )
            timeout_msg = "I'm taking too long to think. Let me try again."
            with contextlib.suppress(Exception):
                brainstem.end_turn()
            if emitter:
                # Best-effort UI signal that the turn ended
                with contextlib.suppress(Exception):
                    await emitter.emit_turn_end("timeout", timeout_msg, TURN_TIMEOUT, 0)
            if dmn:
                dmn.resume()
            return timeout_msg, {}

    async def _process_turn_body(user_input: str, image_path: str | None = None) -> tuple[str, dict]:
        if dmn:
            # pause() is now a no-op (continuous-thought design); call kept
            # for backward compat in case anything still reaches for it.
            dmn.pause()
            # Seed the DMN with the IN-PROGRESS user input so any ticks that
            # fire while we're drafting see fresh context, not the prior turn.
            # Speaker + relationship will be refreshed near the end of the
            # turn (we don't have a speaker_name yet at this point).
            try:
                _interim_context = (
                    f"{parietal.recent_turns_text()}\n\nUser just said: {user_input}"
                )
                # Upsert semantics: only refresh the parietal slice. The
                # previously-stored self-schema, emotion, speaker, and
                # relationship persist until the post-turn call replaces
                # them with the freshly appraised set.
                dmn.update_context(_interim_context)
            except Exception:
                # Best-effort — don't let a context refresh derail the turn.
                pass

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
        trace.prior_neuromod = bus.neuromod.snapshot()
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
                # Persist each enrolled name to that speaker's schema file,
                # migrating any facts from the session_key placeholder if it exists.
                for _ec in completed:
                    _enrolled_name = _ec.get("name", "")
                    _session_key = _ec.get("session_key", "")
                    if _enrolled_name:
                        _sf = hippocampus._schema.ensure_speaker_schema(_enrolled_name)
                        asyncio.ensure_future(
                            hippocampus._schema.aappend_fact(
                                _sf, f"User's name is {_enrolled_name}"
                            )
                        )
                        # Migrate facts from placeholder (e.g. user_spk_0.md → user_owen.md)
                        if _session_key:
                            _placeholder = hippocampus._schema.speaker_filename(_session_key)
                            _placeholder_content = hippocampus._schema.read(_placeholder)
                            if _placeholder_content:
                                asyncio.ensure_future(
                                    hippocampus._schema.migrate_placeholder(_placeholder, _sf)
                                )

        # ── Surface latest speaker identity + song match into features ────────
        # Drain whatever the auditory cortex has emitted since the last turn;
        # keep the most recent valid payload of each kind.
        # Accept all payloads (identified or not) — routing logic below decides
        # whether this is the primary user, a known person, or an unknown stranger.
        latest_speaker = None
        while True:
            try:
                sm = speaker_id_inbox.get_nowait()
                if not sm.expired:
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
            if latest_speaker:
                features["_speaker_match_score"] = latest_speaker.get("match_score", 0.0)
                if latest_speaker.get("identified") and latest_speaker.get("speaker_name"):
                    # Recognized — set name, route to their schema
                    features["speaker_name"] = latest_speaker["speaker_name"]
                elif not latest_speaker.get("identified"):
                    # Unrecognized — decide: primary user, or stranger?
                    from brain.settings import settings as _settings_ref
                    _soft_threshold = float(_settings_ref.get("speaker_primary_soft_threshold"))
                    _match_score = latest_speaker.get("match_score", 0.0)
                    _closest = latest_speaker.get("closest_match") or ""
                    _primary = hippocampus._schema.primary_user_name()
                    if (_primary and _closest.lower() == _primary.lower()
                            and _match_score >= _soft_threshold):
                        # Voice is close enough to primary user — don't set speaker_name
                        # so it falls back to user.md (primary user schema).
                        pass
                    else:
                        # Unknown stranger — use session_key as placeholder identity
                        _session_key = latest_speaker.get("session_key", "unknown")
                        features["speaker_name"] = _session_key
                        features["_speaker_unknown"] = True
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
            if affect.get("hormonal"):
                await emitter.emit_hormonal(affect["hormonal"])
        # Emit user mood: vocal tone (prosody) takes priority over text-based tone
        if emitter:
            user_tone = affect.get("vocal_tone") or features.get("user_tone_toward_ai") or ""
            if user_tone:
                await emitter.emit_user_emotion(user_tone)

        # ── Occipital: vision (any time an image is present) ─────────────────
        vision_features = None
        if image_path:
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
                if features.get("response_type") == "task":
                    # Task mode: skip synchronous 14B planner. The brain responds
                    # immediately with an acknowledgment; _follow_through_check()
                    # dispatches the real multi-step job via execute_internal_job()
                    # in the background once FrontalTaskSubsystem has deposited the goal.
                    memory["tool_result"] = "[task_queued]\nTask acknowledged — working on it now."
                    logger.info("[MotorCortex] Task mode — deferring planning to background")
                else:
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
            # Pull relationship signals for this speaker so the DMN's speak
            # gate has them as structured inputs. Affection + familiarity
            # come from the per-speaker schema; for unknown speakers we let
            # the readers return their reserved defaults (0 / "new").
            from brain.metacognition import read_affection_score, read_familiarity
            _speaker_name_for_dmn = features.get("speaker_name") if isinstance(features, dict) else None
            _schema_for_dmn = getattr(hippocampus, "_schema", None) if hippocampus else None
            _relationship = {
                "score": read_affection_score(_schema_for_dmn, _speaker_name_for_dmn or ""),
                "familiarity": read_familiarity(_schema_for_dmn, _speaker_name_for_dmn or ""),
            }
            dmn.update_context(parietal_context, affect.get("emotion", "neutral"),
                               core_context.get("self", ""),
                               speaker_name=_speaker_name_for_dmn,
                               relationship=_relationship)
            # On return after a long absence, surface deferred thoughts and
            # proposal summaries so the brain can weave them into its response.
            ABSENCE_THRESHOLD_S = 300.0  # 5 minutes away = "returning"
            absence_s = time.time() - _last_turn_ts
            if absence_s >= ABSENCE_THRESHOLD_S and dmn.has_deferred_content():
                deferred = dmn.take_deferred_thoughts()
                proposals = dmn.list_proposals()
                returning_context_parts = []
                if deferred:
                    returning_context_parts.append(
                        f"Thoughts and questions saved while you were away:\n{deferred}"
                    )
                if proposals:
                    awaiting = [p for p in proposals if "awaiting_review" in p.get("status", "")]
                    if awaiting:
                        prop_lines = "\n".join(
                            f"- {p['title']} ({p['proposed']}) — {p['path']}"
                            for p in awaiting
                        )
                        returning_context_parts.append(
                            f"Work proposals ready for your review:\n{prop_lines}"
                        )
                if returning_context_parts:
                    memory["returning_content"] = "\n\n".join(returning_context_parts)
                    logger.info("[DMN] Surfacing deferred content on user return "
                                "(absent %.0fs)", absence_s)

            # Surface idle thoughts into this turn's drafter context so the
            # entity can reference what it was musing about (or quietly use
            # the priming effect even if it doesn't reference them aloud).
            thoughts = dmn.recent_thoughts_tagged(n=4)
            if thoughts:
                memory["recent_thoughts"] = thoughts
            # Consume any pre-prepared anticipations from the DMN. If the
            # brain pre-thought "if user says X, respond with Y" and the
            # user actually said something close to X, the drafter gets a
            # head start.
            anticipations = dmn.take_anticipations()
            if anticipations:
                memory["anticipations"] = anticipations
                logger.info("[Anticipator] Surfacing %d pre-prepared scenarios "
                            "to drafters", len(anticipations))
            # Consume proactive pre-fetched context the DMN pulled while idle.
            # Topic-related episodes / schema that might be relevant this turn.
            prefetched = dmn.take_prefetched()
            if prefetched:
                memory["prefetched_context"] = prefetched
                logger.info("[Prefetcher] Surfacing %d pre-fetched topics to drafters",
                            len(prefetched))
                # Phase 2b: if a recent idle thought has high word-overlap
                # with what the user actually said, the brain was right to
                # have been thinking about it — encode that thought as a
                # low-priority episode (autobiographical record of mind-
                # wandering that proved useful).
                from brain.voice_bridge import bleed_overlap as _word_overlap
                useful: list[tuple[str, float]] = []
                for entry in thoughts:
                    t = entry["thought"] if isinstance(entry, dict) else entry
                    o = _word_overlap(user_input, t)
                    if o >= 0.35:
                        useful.append((t, o))
                if useful:
                    # Encode each useful thought as a separate idle episode
                    # in the background so it doesn't block the turn.
                    for thought_text, overlap in useful:
                        asyncio.create_task(hippocampus.encode_idle_thought(
                            session_id=session_id,
                            thought=thought_text,
                            overlap_with_user_input=overlap,
                            user_input=user_input,
                            embedding_fn=router.embed,
                        ))

        # ── Per-turn speaker context injection ───────────────────────────────
        # Swap in the current speaker's schema so the frontal lobe sees the
        # right person's facts and affection level, not the generic user.md.
        _speaker = features.get("speaker_name", "")
        if _speaker:
            _speaker_schema = hippocampus._schema.load_speaker_context(_speaker)
            memory = dict(memory)
            memory["core"] = dict(memory.get("core", {}))
            memory["core"]["user"] = _speaker_schema

        # ── Egress pseudonymisation (Phase 0H) ────────────────────────────────
        if EGRESS_MODE != "off":
            ps_memory = dict(memory)
            ps_schema, _ = egress.pseudonymize(memory.get("schema", ""))
            ps_episodes, _ = egress.pseudonymize(memory.get("episodes", ""))
            if memory.get("recent_thoughts"):
                ps_memory["recent_thoughts"] = [
                    {**entry, "thought": egress.pseudonymize(entry["thought"])[0]}
                    for entry in memory["recent_thoughts"]
                ]
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
            response = await frontal.process(ps_features, ps_affect, ps_memory, ps_parietal_context, turn_id, image_path=image_path)
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
        # Match brainstem.end_turn — exclude background DMN calls so turn
        # telemetry reflects work done for the turn itself.
        llm_calls = router.turn_calls_excluding_background()

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
        memory_hit_count = len([ln for ln in (memory.get("episodes") or "").splitlines() if ln.strip()])

        selected_draft = next((d for d in draft_scores if d.get("selected")), {})
        selected_coherence = selected_draft.get("coherence", 0.5)
        selected_emotional_fit = (selected_draft.get("empathy_score")
                                  or selected_draft.get("tone_fit", 0.5))
        selected_draft_id = selected_draft.get("draft_id", "")

        # Final neuromod + hormonal push to UI
        if emitter:
            await emitter.emit_neuromod(nm_snap)
            h_snap_final = bus.hormonal.snapshot()
            await emitter.emit_hormonal(h_snap_final)
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
        trace.hormonal = affect.get("hormonal") or bus.hormonal.snapshot()
        trace.draft_scores = draft_scores
        trace.selected_draft_id = selected_draft_id
        trace.drafter_count = len(draft_scores)
        trace.cluster_tokens = cluster_tokens
        trace.memory_recalled = memory_recalled
        trace.memory_hit_count = memory_hit_count
        trace.user_emotion = features.get("user_emotion", "") if isinstance(features, dict) else ""
        trace.speaker_name = features.get("speaker_name", "")
        trace.speaker_score = features.get("_speaker_match_score", 0.0)
        trace.prosody_tone = affect.get("vocal_tone") or ""
        trace.prosody_f0_hz = affect.get("prosody_f0_hz", 0.0)
        trace.prosody_energy = affect.get("prosody_energy", 0.0)
        trace.prosody_jitter = affect.get("prosody_jitter", 0.0)
        trace.prosody_shimmer = affect.get("prosody_shimmer", 0.0)
        obs.record_turn(trace)

        # Append the full trace (with fired_path, neuromod, emotion etc.) so
        # sleep consolidation can apply Hebbian updates along the path.
        session_traces_full.append(trace)

        # ── Follow-through (non-blocking) ────────────────────────────────────
        # Detects spoken commitments and enqueues them in the persistent task
        # queue rather than firing immediately. Actual execution happens in the
        # _task_worker_loop below, which also handles self-initiated DMN tasks
        # and tasks recovered from a previous session.
        async def _follow_through_check() -> None:
            # Path 1: task-mode — FrontalTaskSubsystem deposited a goal.
            # Reformulate via follow_through so the task list shows a clean
            # imperative summary rather than the user's verbatim utterance.
            deferred_goal = pending_task.take() if pending_task else None
            if deferred_goal:
                try:
                    goal = await follow_through.extract(user_input, final, turn_id) or deferred_goal
                except Exception:
                    goal = deferred_goal
                task_queue.enqueue(goal, source="user", priority=1)
                logger.info("[FollowThrough] Task enqueued (task-mode): %s", goal[:120])
                return

            # Path 2: reactive — LLM extraction from spoken response.
            try:
                goal = await follow_through.extract(user_input, final, turn_id)
                if goal:
                    task_queue.enqueue(goal, source="user", priority=1)
                    logger.info("[FollowThrough] Task enqueued (reactive): %s", goal[:120])
            except Exception as _e:
                logger.warning("[FollowThrough] failed: %s", _e)
        asyncio.create_task(_follow_through_check())

        # ── Background eval tasks (non-blocking) ─────────────────────────────
        if emotion_judge:
            emotion_judge.fire(trace)

        if learning_monitor:
            learning_monitor.record_turn(trace)

        if baseline_runner:
            memory_ctx = ((memory.get("episodes") or "") + "\n"
                          + (memory.get("schema") or ""))
            baseline_runner.fire(
                turn_id, user_input, final,
                memory_ctx[:1000], selected_coherence, selected_emotional_fit,
                trace=trace,
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
            "speaker_name": features.get("speaker_name", ""),
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
            # Record whether our response was a question — if so, the next
            # DMN tick will fire the anticipator to pre-prepare for likely
            # user answers.
            dmn.note_last_response(final)
            dmn.resume()

        _last_turn_ts = time.time()  # track when the last real user turn completed

        logger.info("Turn %s: %d LLM calls | %.2fs | emotion=%s",
                    turn_id, llm_calls, turn_result.elapsed(), affect.get("emotion"))

        # Release the firing-path context binding for this turn
        with contextlib.suppress(Exception):
            reset_current_trace(_ctx_token)

        return final, affect

    # ── Shared task executor (used by worker loop and follow-through) ────────
    async def _run_task(task) -> None:
        """Execute one task from the queue and speak the result."""
        job_turn_id = f"task_{task.id}"
        # Self-initiated tasks run under background resource policy:
        # cloud calls are budgeted and capped; Ollama is rate-limited.
        is_self = getattr(task, "source", "") == "self"
        if is_self:
            router.enter_background_mode()
        try:
            summary = await motor.execute_internal_job(task.goal, job_turn_id)
        except Exception as _e:
            logger.warning("[TaskWorker] Task [%s] execution failed: %s", task.id, _e)
            task_queue.mark_done(task.id, success=False)
            return
        finally:
            if is_self:
                router.exit_background_mode()

        if summary.get("clarification"):
            # Task hit a blocker — park it as blocked (not failed) so it can be
            # resumed when the user answers. The brain treats blocked = idle.
            question = summary["clarification"]
            task_queue.mark_blocked(task.id, reason=question)
            logger.info("[TaskWorker] Task [%s] blocked on clarification: %s",
                        task.id, question[:120])
            if emitter:
                await emitter.emit_proactive_speech(question)
            await pns.emit(question, {"emotion": "curious"})
            return

        task_queue.mark_done(task.id, success=bool(summary.get("success")))

        spoken_summary = await result_reporter.report(summary, job_turn_id)
        if not spoken_summary:
            spoken_summary = (
                "Done — but I don't have a clean summary to share."
                if summary.get("success")
                else "I couldn't finish that — something went wrong."
            )
        logger.info("[TaskWorker] Reporting result [%s]: %s", task.id, spoken_summary[:160])
        if emitter:
            await emitter.emit_event({
                "type": "task_summary",
                "job_id": summary.get("job_id"),
                "summary": spoken_summary,
            })
            await emitter.emit_proactive_speech(spoken_summary)
        await pns.emit(spoken_summary,
                       {"emotion": "lively" if summary.get("success") else "concerned"})

    # ── Background task worker ────────────────────────────────────────────────
    # Drains the persistent task queue when the brain is idle. Handles all
    # sources: user commitments, DMN self-initiated tasks, and recovered tasks.
    if motor:
        async def _task_worker_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(3.0)
                    if not task_queue.has_pending():
                        # Also drain any self-initiated goals the DMN queued.
                        if dmn:
                            self_goal = dmn.take_self_task()
                            if self_goal:
                                task_queue.enqueue(self_goal, source="self", priority=2)
                        continue
                    # Only execute when the brain isn't mid-conversation.
                    if pns.is_speaking or not ui_message_queue.empty():
                        continue
                    since_spoke = time.time() - _last_brain_spoke_ts
                    if since_spoke < PROACTIVE_RESPONSE_WINDOW:
                        continue
                    task = task_queue.take_next()
                    if task:
                        source_label = {"recovery": "📋 resuming", "self": "💭 self-initiated",
                                        "user": "▶ executing"}.get(task.source, "▶")
                        logger.info("[TaskWorker] %s task [%s]: %s",
                                    source_label, task.id, task.goal[:80])
                        await _run_task(task)
                except asyncio.CancelledError:
                    return
                except Exception as _e:
                    logger.error("[TaskWorker] Unexpected error: %s", _e, exc_info=True)

        brainstem.register_loop("task_worker", _task_worker_loop)

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
            except TimeoutError:
                # Between turns: speak any proactive thought the DMN queued,
                # but only when the brain isn't already talking, the user
                # hasn't queued something, AND enough time has passed since
                # the brain last spoke (response window — gives the user a
                # chance to reply before we jump in with a new thought).
                since_last_spoke = time.time() - _last_brain_spoke_ts
                if (dmn is not None
                        and not pns.is_speaking
                        and ui_message_queue.empty()
                        and since_last_spoke >= PROACTIVE_RESPONSE_WINDOW):
                    spoken = dmn.take_proactive()
                    if spoken:
                        idle = get_idle_seconds()
                        if PROACTIVE_IDLE_THRESHOLD > 0 and idle >= PROACTIVE_IDLE_THRESHOLD:
                            logger.debug(
                                "[Proactive] Suppressed — user idle %.0fs (threshold %.0fs)",
                                idle, PROACTIVE_IDLE_THRESHOLD,
                            )
                        else:
                            logger.info("[Proactive] Speaking (idle=%.0fs, since_spoke=%.0fs): %r",
                                        idle, since_last_spoke, spoken[:80])
                            if emitter:
                                await emitter.emit_proactive_speech(spoken)
                            await pns.emit(spoken, {"emotion": "curious"})
                            _last_brain_spoke_ts = time.time()
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
            _last_brain_spoke_ts = time.time()
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
            _last_brain_spoke_ts = time.time()
            await _emit_end("motor", "speak")
            await _emit_end("brainstem", "speak")

    # ── Shutdown ──────────────────────────────────────────────────────────────
    brainstem.cancel_all_loops()

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
            await sleep.consolidate(
                session_id, session_traces,
                full_traces=session_traces_full,
                session_thoughts=dmn.session_thoughts() if dmn else [],
            )
            # Refresh DMN's projects manifest — sleep may have rewritten open_questions.md
            if dmn:
                try:
                    _oq_refreshed = hippocampus._schema.read("open_questions.md")
                    if _oq_refreshed:
                        dmn.set_projects_context(_oq_refreshed)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("End-of-session memory consolidation failed — recent facts may not be saved: %s", e)

    # Learning judge runs after sleep consolidation so wiring.session_deltas()
    # reflects the Hebbian pass that just completed.
    if learning_monitor and learning_judge and session_traces_full:
        try:
            session_metrics = learning_monitor.session_metrics(wiring=wiring)
            await learning_judge.evaluate(session_id, session_traces_full, session_metrics)
        except Exception as e:
            logger.warning("Learning judge failed: %s", e)

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
