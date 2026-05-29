"""Setup phase methods for BrainSession — imported as _SetupMixin."""

from __future__ import annotations

import asyncio
import logging
import os

from brain.settings import settings as _brain_settings

logger = logging.getLogger("brain.run")


class _SetupMixin:
    # ── Eval bootstrap ────────────────────────────────────────────────────────

    @staticmethod
    def _bootstrap_eval_system(obs) -> tuple:
        """Initialize eval subsystem. Returns 6-tuple, all None if eval unavailable."""
        eval_logger = baseline_runner = posthoc_scorer = None
        emotion_judge = learning_monitor = learning_judge = None
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
        for component in (
            baseline_runner,
            posthoc_scorer,
            emotion_judge,
            learning_monitor,
            learning_judge,
        ):
            if component is not None:
                component._obs = obs
        return (
            eval_logger,
            baseline_runner,
            posthoc_scorer,
            emotion_judge,
            learning_monitor,
            learning_judge,
        )

    # ── Setup phases ──────────────────────────────────────────────────────────

    async def _setup_core(self) -> None:
        from brain.brainstem import Brainstem
        from brain.bus import Bus
        from brain.model_router import ModelRouter
        from brain.observability.timeline import ObservabilityLayer
        from brain.pns import PNS

        self.bus = Bus()
        self.obs = ObservabilityLayer(self.session_id)
        (
            self._eval_logger,
            self._baseline_runner,
            self._posthoc_scorer,
            self._emotion_judge,
            self._learning_monitor,
            self._learning_judge,
        ) = self._bootstrap_eval_system(self.obs)
        self.obs._eval_logger = self._eval_logger
        self.router = ModelRouter(obs=self.obs)
        self.brainstem = Brainstem(self.bus, self.router)
        self._proactive_idle_threshold = _brain_settings.get("proactive_idle_threshold") or float(
            os.environ.get("BRAIN_PROACTIVE_IDLE_THRESHOLD", "180")
        )
        self._proactive_response_window = _brain_settings.get("proactive_response_window") or float(
            os.environ.get("BRAIN_PROACTIVE_RESPONSE_WINDOW", "8")
        )
        self.pns = PNS(self.bus, on_speaking_change=self._on_speaking_change)

    async def _setup_wiring(self) -> None:
        from brain.observability.decisions import decisions as decisions_log
        from brain.wiring import Wiring
        from brain.wiring_bootstrap import bootstrap as wiring_bootstrap

        self.wiring = Wiring()
        wiring_bootstrap(self.wiring)
        self.wiring.snapshot_baseline()
        self._wiring_frozen = os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true"
        if self._wiring_frozen:
            logger.info("Wiring FROZEN — weighted routing disabled (BRAIN_WIRING_FROZEN=true)")
        else:
            logger.info("Wiring: %d edges loaded", self.wiring.edge_count())
        decisions_log.configure(eval_logger=self._eval_logger)

    async def _setup_clusters(self) -> None:
        from brain.clusters.frontal import FrontalCluster
        from brain.clusters.hippocampus import HippocampusCluster
        from brain.clusters.hypothalamus import HypothalamusCluster
        from brain.clusters.occipital import OccipitalCluster
        from brain.clusters.parietal import ParietalCluster
        from brain.clusters.temporal import TemporalCluster
        from brain.clusters.thalamus import ThalamusCluster
        from brain.security import PseudonymizationGateway

        self.thalamus = ThalamusCluster(self.bus)
        self.temporal = TemporalCluster(self.bus, self.router, wiring=self.wiring)
        self.occipital = OccipitalCluster(self.bus, self.router)
        self.hypothalamus = HypothalamusCluster(self.bus)
        self.parietal = ParietalCluster(self.bus)
        self.hippocampus = HippocampusCluster(self.bus, self.router, wiring=self.wiring)
        self.frontal = FrontalCluster(self.bus, self.brainstem, self.router, wiring=self.wiring)
        # Wire the skill selector (loads embedding index from brain/skills/_humanity_index.json)
        try:
            from brain.clusters.skill_selector import SkillSelector

            self.skill_selector = SkillSelector(self.router)
            self.frontal.set_skill_selector(self.skill_selector, self.parietal)
        except FileNotFoundError as e:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "[Setup] SkillSelector disabled: %s — run `python -m brain.skills._import_humanity` to enable.",
                e,
            )
            self.skill_selector = None
        self._core_context, recent_episodes = await self.hippocampus.boot(self.session_id)
        self.parietal.seed(recent_episodes)
        self._egress = PseudonymizationGateway()

    async def _setup_ui(self) -> None:
        self._ui_enabled = self.args.ui or os.environ.get("BRAIN_UI", "false").lower() == "true"
        if not self._ui_enabled:
            return

        from brain.ui.emitter import emitter as _emitter
        from brain.ui.server import UIServer

        self._emitter = _emitter
        ui_server = UIServer(
            self._emitter.get_queue(),
            on_user_message=self._on_browser_message,
            on_voice_change=self.pns.set_voice_id,
            on_eval_mode=self._on_eval_mode,
            on_mic_toggle=self._on_mic_toggle,
            on_mic_ptt=self._on_mic_ptt,
            is_muted_fn=self._is_mic_muted,
            on_interrupt=self.pns.interrupt,
            wiring=self.wiring,
            bus=self.bus,
        )
        ui_server.set_wiring_frozen(self._wiring_frozen)
        self.brainstem.register_loop(
            "ui_server", lambda: ui_server.start(port=8765), restart_on_crash=False
        )
        self._ui_server = ui_server
        await asyncio.sleep(0.3)

        # Emit initial chemistry + emotion so the UI shows the correct resting
        # state immediately, rather than sitting on the HTML default "neutral"
        # until the first turn fires.
        from brain.emotion_vocabulary import apply_hormonal_color, name_emotion

        _nm = self.bus.neuromod.snapshot()
        _hs = self.bus.hormonal.snapshot()
        _emotion, _tendency = name_emotion(_nm["DA"], _nm["GABA"], _nm["ACh"], _nm["Glu"])
        _emotion, _tendency = apply_hormonal_color(
            _emotion,
            _tendency,
            _hs,
            oxt_connected=_brain_settings.get("hormonal_oxt_connected_threshold"),
            cort_withdrawn=_brain_settings.get("hormonal_cort_withdrawn_threshold"),
            oxt_guarded=_brain_settings.get("hormonal_oxt_guarded_threshold"),
            sht_dysphoric=_brain_settings.get("hormonal_sht_dysphoric_threshold"),
            aea_eased=_brain_settings.get("aea_eased_threshold"),
        )
        await self._emitter.emit_neuromod(_nm)
        await self._emitter.emit_hormonal(_hs)
        await self._emitter.emit_emotion(_emotion)

    async def _setup_motor(self) -> None:
        if not (self.args.motor or os.environ.get("BRAIN_MOTOR", "false").lower() == "true"):
            self.frontal.set_capabilities(
                "Tool use is DISABLED this session (motor cortex not enabled). "
                "If asked to use external tools, explain that you'd need to be "
                "restarted with --motor."
            )
            return

        from brain.clusters.cloud_executor import CloudExecutor
        from brain.clusters.follow_through import FollowThrough, ResultReporter
        from brain.clusters.frontal_task import FrontalTaskSubsystem, PendingTask
        from brain.clusters.lobe_bridge import LobeBridge
        from brain.clusters.motor_cortex import MotorCortexCluster
        from brain.clusters.motor_memory import MuscleMemorySubsystem
        from brain.clusters.task_queue import PersistentTaskQueue

        _motor_paths_raw = os.environ.get("BRAIN_MOTOR_PATHS", "")
        _motor_paths = [p.strip() for p in _motor_paths_raw.split(":") if p.strip()]
        _motor_cmds_raw = os.environ.get("BRAIN_MOTOR_COMMANDS", "")
        _motor_cmds = set(_motor_cmds_raw.split(":")) if _motor_cmds_raw else None

        cloud = CloudExecutor(self.bus, schema_store=self.hippocampus._schema)
        if not _motor_paths and cloud._trusted_dirs:
            _motor_paths = cloud._trusted_dirs[:]
            logger.info(
                "Motor cortex: inheriting trusted dirs from Claude Desktop: %s", _motor_paths
            )

        # Always include the project root so the agent can read/write its own
        # codebase regardless of how it was launched (start.sh vs direct invocation).
        from pathlib import Path as _Path

        _project_root = str(_Path(__file__).parent.parent.resolve())
        if _project_root not in _motor_paths:
            _motor_paths.insert(0, _project_root)
            logger.info("Motor cortex: project root auto-added to allowed paths: %s", _project_root)

        self.motor = MotorCortexCluster(
            self.bus,
            self.router,
            allowed_paths=_motor_paths,
            allowed_commands=_motor_cmds,
            cloud_executor=cloud,
        )
        if _motor_paths:
            logger.info("Motor cortex online. Allowed paths: %s", _motor_paths)
        else:
            logger.warning(
                "Motor cortex enabled but no project paths are accessible — "
                "add paths via BRAIN_MOTOR_PATHS or Claude Desktop trusted folders."
            )

        self._pending_task = PendingTask()
        self.motor.set_pending_task(self._pending_task)
        self.frontal.register_subsystem(FrontalTaskSubsystem(self._pending_task))
        self.motor.register_subsystem(MuscleMemorySubsystem())
        self._follow_through = FollowThrough(self.router)
        self._result_reporter = ResultReporter(self.router)
        self._task_queue = PersistentTaskQueue()
        self._recent_task_results: list[dict] = []  # ring buffer: last 3 completed tasks

        _recovered = self._task_queue.recover_interrupted()
        if _recovered:
            logger.info(
                "[TaskQueue] %d task(s) recovered from previous session: %s",
                len(_recovered),
                "; ".join(t.goal[:60] for t in _recovered),
            )

        self._lobe_bridge = LobeBridge()
        self._lobe_bridge.register("recall_memory", self._recall_memory)
        self._lobe_bridge.register("analyze_image", self._analyze_image)
        self.motor.set_lobe_bridge(self._lobe_bridge)
        self.motor.set_observability(self.obs)

        cap_lines = ["Tool use is ENABLED via the motor cortex. You can:"]
        if _motor_paths:
            cap_lines.append(
                f"- Read / write / list / search files within: {', '.join(_motor_paths)}"
            )
            cap_lines.append("- Run safe shell commands (git, ls, grep, etc.) in those paths")
        else:
            cap_lines.append("- (Filesystem tools are blocked — BRAIN_MOTOR_PATHS is unset)")
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
        self.frontal.set_capabilities("\n".join(cap_lines))

    async def _setup_dmn(self) -> None:
        if not (self.args.dmn or os.environ.get("BRAIN_DMN", "false").lower() == "true"):
            return

        from brain.dmn import DefaultModeNetwork

        self.dmn = DefaultModeNetwork(
            self.bus, self.router, self.hippocampus, self.parietal, obs=self.obs
        )
        if getattr(self, "skill_selector", None) is not None:
            self.dmn.set_skill_selector(self.skill_selector)

        if self._emitter:
            self._dmn_orig_tick = self.dmn._tick
            self.dmn._tick = self._dmn_tick_with_ui
            self._thought_inbox = self.bus.subscribe("stream.thought")
            self.brainstem.register_loop("forward_thoughts", self._forward_thoughts)

        await self.dmn.start(self.session_id)

        try:
            _oq_text = self.hippocampus._schema.read("open_questions.md")
            if _oq_text:
                self.dmn.set_projects_context(_oq_text)
                logger.info("[DMN] Projects context loaded (%d chars)", len(_oq_text))
        except Exception as _oq_err:
            logger.warning("[DMN] Could not load projects context: %s", _oq_err)

        # Seed DMN with last session memory so its first thoughts are grounded
        # in where things were left off, rather than starting cold.
        try:
            _recent = self.hippocampus._episodic.recall_recent(limit=4)
            if _recent:
                _lines = []
                for ep in reversed(_recent):
                    u = (ep.get("user_input") or "").strip()[:200]
                    r = (ep.get("entity_response") or "").strip()[:200]
                    tags = ep.get("topic_tags") or []
                    if u or r:
                        _lines.append(
                            f"[{', '.join(tags[:3]) if tags else 'unknown topic'}]\n"
                            f"  User: {u}\n  Me: {r}"
                        )
                if _lines:
                    _seed = "Last session (oldest → newest):\n\n" + "\n\n".join(_lines)
                    _seed += "\n\n(New session just started.)"
                    _self_schema = self.hippocampus._core_context.get("self", "")
                    self.dmn.update_context(_seed, self_schema=_self_schema)
                    topics = []
                    for ep in _recent:
                        topics.extend(ep.get("topic_tags") or [])
                    logger.info(
                        "[DMN] Seeded with %d recent episodes (topics: %s)",
                        len(_recent),
                        ", ".join(dict.fromkeys(topics))[:120] or "unknown",
                    )
                    asyncio.create_task(self.dmn.prime_startup())
        except Exception as _seed_err:
            logger.debug("[DMN] Could not seed last session context: %s", _seed_err)

    async def _setup_meta(self) -> None:
        if not (
            self.args.metacognition
            or os.environ.get("BRAIN_METACOGNITION", "false").lower() == "true"
        ):
            return
        from brain.metacognition import MetacognitionCell

        self.meta = MetacognitionCell(self.bus, self.router, self.hippocampus._schema)
        await self.meta.start()

    async def _setup_auditory(self) -> None:
        self._enrollment_complete_inbox = self.bus.subscribe("auditory.enrollment_complete")
        self._speaker_id_inbox = self.bus.subscribe("auditory.speaker_id")
        self._song_match_inbox = self.bus.subscribe("auditory.song_match")
        # Deliberate mood expressions (set_mood tool + inline markup) — collected
        # each turn and flushed into the TurnTrace just before record_turn().
        self._mood_expression_inbox = self.bus.subscribe("meta.mood_expression")
        if not (self.args.ears or os.environ.get("BRAIN_EARS", "false").lower() == "true"):
            return
        from brain.clusters.auditory_cortex import AuditoryCluster

        self.ears = AuditoryCluster(self.bus)
        self.brainstem.register_loop("ears", self.ears.run)

    async def _setup_streaming_mic(self) -> None:
        if not (self.args.voice or os.environ.get("BRAIN_VOICE_MODE", "false").lower() == "true"):
            return
        from brain.streaming_mic import StreamingMicSession

        self._streaming_mic = StreamingMicSession(
            self.bus,
            is_speaking_fn=lambda: self.pns.is_speaking,
            on_user_interrupt=self.pns.interrupt,
        )
        try:
            await self._streaming_mic.start()
        except Exception as e:
            logger.error("[I/O] Streaming mic failed to start — voice input is offline: %s", e)
            self._streaming_mic = None

    def _setup_speak_gate(self) -> None:
        if self.dmn is not None:
            self.brainstem.register_loop("speak_gate", self._speak_gate_loop)

    def _setup_voice_bridge(self) -> None:
        from brain.voice_bridge import parse_barge_words

        self._barge_in_words = parse_barge_words(os.environ.get("BRAIN_BARGE_IN_WORDS"))
        if self._streaming_mic is None or not self._ui_enabled:
            return
        self._pending_lock = asyncio.Lock()
        self.brainstem.register_loop("voice_bridge", self._voice_bridge)
        self.brainstem.register_loop("tts_drain", self._drain_pending_when_tts_ends)

    def _setup_loops(self) -> None:
        self.brainstem.register_loop("heartbeat", self._heartbeat_with_ui)
        if self.motor:
            self.brainstem.register_loop("task_worker", self._task_worker_loop)
        # Periodic in-process consolidation. Lets the brain run for days
        # without losing learning to a never-fired end-of-session pass.
        # Toggle via the Sleep Consolidation section in /settings (sleep_periodic_enabled)
        # or, for one-off CLI runs, BRAIN_SLEEP_PERIODIC=false.
        import os as _os

        from brain.settings import settings as _settings

        _env_on = _os.environ.get("BRAIN_SLEEP_PERIODIC", "").lower()
        _settings_on = int(_settings.get("sleep_periodic_enabled")) == 1
        _enabled = (_settings_on and _env_on != "false") or _env_on == "true"
        if _enabled:
            from brain.sleep import SleepConsolidation

            self._sleep = SleepConsolidation(
                self.router,
                self.hippocampus._schema,
                self.hippocampus._episodic,
                wiring=self.wiring,
            )
            import asyncio as _asyncio

            self._consolidation_lock = _asyncio.Lock()
            self.brainstem.register_loop("periodic_sleep", self._periodic_sleep_loop)
