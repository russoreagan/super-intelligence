"""
BrainSession — session lifecycle for the AI brain.

The logic is split across three focused mixin modules:
  session_setup.py  — _SetupMixin   (all _setup_* methods)
  session_loops.py  — _LoopsMixin   (callbacks + background loops)
  session_turn.py   — _TurnMixin    (process_turn, _process_turn_body, _run_task)

This file contains only __init__, run(), the three run-mode coroutines, and shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid

from brain.session_loops import _LoopsMixin
from brain.session_setup import _SetupMixin
from brain.session_turn import _TurnMixin
from brain.utils import get_idle_seconds

logger = logging.getLogger("brain.run")


class BrainSession(_SetupMixin, _LoopsMixin, _TurnMixin):
    def __init__(self, args) -> None:
        self.args = args

        # Core session identity
        self.session_id = str(uuid.uuid4())[:8]
        # Active persona (empty = neutral). Stamped onto every TurnTrace so the
        # shared eval log can be filtered/grouped per persona. Set from settings.
        from brain.settings import settings as _settings

        self.persona_name = str(_settings.get("persona_name", ""))
        self.bus = None
        self.obs = None
        self.router = None
        self.brainstem = None
        self.pns = None

        # TTS / mic mute control
        self._mic_unmute_delay_s = float(os.environ.get("BRAIN_MIC_UNMUTE_DELAY_S", "0.3"))
        self._tts_did_mute = False
        # Push-to-talk: True while the user is holding Space (or toggled the mic
        # on). The mic is live only when this is True and the entity isn't
        # speaking; on release we flush the held phrase and re-mute.
        self._ptt_held = False

        # Proactive / idle gates (overwritten during _setup_core from settings)
        self._proactive_idle_threshold: float = 180.0
        self._proactive_response_window: float = 8.0
        self._last_brain_spoke_ts: float = 0.0
        self._last_turn_ts: float = time.time()

        # Wiring
        self.wiring = None
        self._wiring_frozen = False

        # Clusters
        self.thalamus = None
        self.temporal = None
        self.occipital = None
        self.hypothalamus = None
        self.parietal = None
        self.hippocampus = None
        self.frontal = None
        self._core_context: dict = {}
        self._egress = None

        # UI
        self._ui_enabled = False
        self._ui_server = None
        self._emitter = None
        self._ui_message_queue: asyncio.Queue = asyncio.Queue()

        # Eval
        self._eval_logger = None
        self._baseline_runner = None
        self._posthoc_scorer = None
        self._emotion_judge = None
        self._learning_monitor = None
        self._learning_judge = None

        # Motor
        self.motor = None
        self._follow_through = None
        self._result_reporter = None
        self._task_queue = None
        self._lobe_bridge = None
        self._pending_task = None

        # DMN
        self.dmn = None
        self._dmn_orig_tick = None
        self._thought_inbox = None

        # Meta
        self.meta = None

        # Auditory
        self.ears = None
        self._enrollment_complete_inbox = None
        self._speaker_id_inbox = None
        self._song_match_inbox = None

        # Voice / mic
        self._streaming_mic = None
        self._barge_in_words = None
        self._pending_during_tts: list[str] = []
        self._pending_lock: asyncio.Lock | None = None

        # Session state
        self._session_traces: list[dict] = []
        self._session_traces_full: list = []
        self._pending_encodes: set[asyncio.Task] = set()

        # Periodic in-process consolidation (sleep) — runs while the app keeps
        # running, so long-lived sessions don't lose learning. See
        # _periodic_sleep_loop and consolidate_now in session_loops.
        self._sleep: SleepConsolidation | None = None  # type: ignore[name-defined]  # noqa: F821
        self._consolidation_lock: asyncio.Lock | None = None
        self._last_consolidation_ts: float = time.time()

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("Session %s starting", self.session_id)
        await self._setup_core()
        await self._setup_wiring()
        await self._setup_clusters()
        await self._setup_ui()
        await self._setup_motor()
        await self._setup_dmn()
        await self._setup_meta()
        await self._setup_auditory()
        await self._setup_streaming_mic()
        self._setup_speak_gate()
        self._setup_voice_bridge()
        self._setup_loops()

        # Emit a module summary so it's immediately obvious after restart
        # (or any startup) which subsystems came up and which didn't.
        def _on(flag):
            return "✓" if flag else "✗"

        logger.info(
            "Session %s online — UI:%s  Motor:%s  DMN:%s  Meta:%s  Voice:%s  Ears:%s",
            self.session_id,
            _on(self._ui_enabled),
            _on(self.motor is not None),
            _on(self.dmn is not None),
            _on(self.meta is not None),
            _on(self._streaming_mic is not None),
            _on(self.ears is not None),
        )
        if self._ui_server is not None:
            self._ui_server.set_subsystem_status(
                {
                    "ui": self._ui_enabled,
                    "motor": self.motor is not None,
                    "dmn": self.dmn is not None,
                    "meta": self.meta is not None,
                    "voice": self._streaming_mic is not None,
                    "ears": self.ears is not None,
                }
            )

        if self.args.message:
            await self._run_single_message()
        elif self._ui_enabled:
            await self._run_ui_loop()
        else:
            await self._run_cli_loop()

        await self._shutdown()

    # ── Run modes ─────────────────────────────────────────────────────────────

    async def _run_single_message(self) -> None:
        response, affect = await self.process_turn(self.args.message)
        await self.pns.emit(response, affect)

    async def _run_ui_loop(self) -> None:
        print("Brain online. Open http://localhost:8765 to interact.\n")
        while True:
            try:
                user_input = await asyncio.wait_for(self._ui_message_queue.get(), timeout=1.0)
            except TimeoutError:
                since_last_spoke = time.time() - self._last_brain_spoke_ts
                if (
                    self.dmn is not None
                    and not self.pns.is_speaking
                    and self._ui_message_queue.empty()
                    and since_last_spoke >= self._proactive_response_window
                ):
                    spoken = self.dmn.take_proactive()
                    if spoken:
                        idle = get_idle_seconds()
                        if (
                            self._proactive_idle_threshold > 0
                            and idle >= self._proactive_idle_threshold
                        ):
                            logger.debug(
                                "[Proactive] Suppressed — user idle %.0fs (threshold %.0fs)",
                                idle,
                                self._proactive_idle_threshold,
                            )
                        else:
                            logger.info(
                                "[Proactive] Speaking (idle=%.0fs, since_spoke=%.0fs): %r",
                                idle,
                                since_last_spoke,
                                spoken[:80],
                            )
                            if self._emitter:
                                await self._emitter.emit_proactive_speech(spoken)
                            await self.pns.emit(spoken, {"emotion": "curious"})
                            self._last_brain_spoke_ts = time.time()
                continue
            except asyncio.CancelledError:
                break

            if not user_input:
                continue

            # Slash commands intercepted before the turn pipeline. /consolidate
            # forces an in-process sleep pass on whatever has buffered so far.
            if user_input.strip().lower().startswith("/consolidate"):
                status = await self.consolidate_now(reason="manual_ui")
                msg = (
                    f"Consolidation: ran on {status.get('turns', 0)} turns "
                    f"in {status.get('elapsed_s', 0)}s ✓"
                    if status.get("ran")
                    else f"Consolidation skipped: {status.get('reason', 'unknown')}"
                )
                if self._emitter:
                    await self.pns.emit(msg, {})
                self._last_brain_spoke_ts = time.time()
                continue

            image_path = None
            if "[image:" in user_input:
                m = re.search(r"\[image:([^\]]+)\]", user_input)
                if m:
                    image_path = m.group(1).strip()
                    user_input = user_input.replace(m.group(0), "").strip()

            response, affect = await self.process_turn(user_input, image_path)
            await self._emit("motor", 0.7, "articulating", "speak")
            await self._emit("brainstem", 0.35, "speaking", "speak")
            await self.pns.emit(response, affect)
            self._last_brain_spoke_ts = time.time()
            await self._emit_end("motor", "speak")
            await self._emit_end("brainstem", "speak")

    async def _run_cli_loop(self) -> None:
        print("Brain online. Type your message, or 'quit' to exit.\n")
        while True:
            try:
                if self.args.voice:
                    if self._streaming_mic is None:
                        print("Voice input is offline. Check DEEPGRAM_API_KEY and mic permissions.")
                        break
                    await self._emit("temporal", 0.4, "listening...", "mic")
                    await self._emit("brainstem", 0.15, "listening...", "mic")
                    utt = await self._streaming_mic.next_utterance()
                    await self._emit_end("temporal", "mic")
                    await self._emit_end("brainstem", "mic")
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

            if user_input.strip().lower().startswith("/consolidate"):
                status = await self.consolidate_now(reason="manual_cli")
                if status.get("ran"):
                    print(
                        f"Brain: Consolidation ran on {status.get('turns', 0)} "
                        f"turns in {status.get('elapsed_s', 0)}s."
                    )
                else:
                    print(f"Brain: Consolidation skipped — {status.get('reason')}.")
                continue

            image_path = None
            if "[image:" in user_input:
                m = re.search(r"\[image:([^\]]+)\]", user_input)
                if m:
                    image_path = m.group(1).strip()
                    user_input = user_input.replace(m.group(0), "").strip()

            response, affect = await self.process_turn(user_input, image_path)
            await self._emit("motor", 0.7, "articulating", "speak")
            await self._emit("brainstem", 0.35, "speaking", "speak")
            await self.pns.emit(response, affect)
            self._last_brain_spoke_ts = time.time()
            await self._emit_end("motor", "speak")
            await self._emit_end("brainstem", "speak")

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def _shutdown(self) -> None:
        self.brainstem.cancel_all_loops()

        if self._streaming_mic is not None:
            try:
                await self._streaming_mic.stop()
            except Exception as _e:
                logger.debug("streaming mic shutdown error: %s", _e)

        if self._pending_encodes:
            logger.info(
                "Waiting for %d in-progress memory writes to finish before exit...",
                len(self._pending_encodes),
            )
            await asyncio.gather(*self._pending_encodes, return_exceptions=True)

        if self._session_traces:
            try:
                # Reuse the periodic-sleep instance if it exists; fall back to a
                # fresh one (older boot paths / BRAIN_SLEEP_PERIODIC=false).
                sleep = self._sleep
                if sleep is None:
                    from brain.sleep import SleepConsolidation

                    sleep = SleepConsolidation(
                        self.router,
                        self.hippocampus._schema,
                        self.hippocampus._episodic,
                        wiring=self.wiring,
                    )
                logger.info(
                    "Running end-of-session memory consolidation "
                    "(summarising facts, updating self-model, applying Hebbian updates)..."
                )
                await sleep.consolidate(
                    self.session_id,
                    self._session_traces,
                    full_traces=self._session_traces_full,
                    session_thoughts=self.dmn.session_thoughts() if self.dmn else [],
                )
                if self.dmn:
                    try:
                        _oq_refreshed = self.hippocampus._schema.read("open_questions.md")
                        if _oq_refreshed:
                            self.dmn.set_projects_context(_oq_refreshed)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(
                    "End-of-session memory consolidation failed — recent facts may not be saved: %s",
                    e,
                )

        if self._learning_monitor and self._learning_judge and self._session_traces_full:
            try:
                session_metrics = self._learning_monitor.session_metrics(wiring=self.wiring)
                await self._learning_judge.evaluate(
                    self.session_id, self._session_traces_full, session_metrics
                )
            except Exception as e:
                logger.warning("Learning judge failed: %s", e)

        self.obs.flush()
        logger.info(
            "Session %s complete. Total LLM calls: %d",
            self.session_id,
            self.brainstem._session_cost_calls,
        )

        if self.meta:
            summary = self.meta.summary()
            if summary:
                print(
                    f"\nSession stats: {summary.get('turn_count')} turns | "
                    f"avg {summary.get('avg_llm_calls')} LLM calls | "
                    f"dominant emotion: {summary.get('dominant_emotion')}"
                )
