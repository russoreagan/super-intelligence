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
        """Initialize eval subsystem. Returns 7-tuple, all None if eval unavailable."""
        eval_logger = baseline_runner = posthoc_scorer = None
        emotion_judge = learning_monitor = learning_judge = relationship_judge = None
        try:
            from eval.baseline import BaselineRunner
            from eval.emotion_judge import EmotionJudge
            from eval.learning_judge import LearningJudge
            from eval.learning_monitor import LearningMonitor
            from eval.relationship_judge import RelationshipJudge
            from eval.scorer import PostHocScorer
            from eval.turn_logger import EvalLogger

            eval_logger = EvalLogger()
            baseline_runner = BaselineRunner(eval_logger)
            posthoc_scorer = PostHocScorer(eval_logger)
            baseline_runner._scorer = posthoc_scorer
            emotion_judge = EmotionJudge(eval_logger)
            learning_monitor = LearningMonitor()
            learning_judge = LearningJudge(eval_logger)
            # Built but DISABLED by default — opt in with BRAIN_EVAL_RELATIONSHIP=true.
            relationship_judge = RelationshipJudge(eval_logger)
            logger.info("Eval: logging to %s", eval_logger._path)
        except Exception as _eval_err:
            logger.debug("Eval system unavailable: %s", _eval_err)
        for component in (
            baseline_runner,
            posthoc_scorer,
            emotion_judge,
            learning_monitor,
            learning_judge,
            relationship_judge,
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
            relationship_judge,
        )

    # ── Setup phases ──────────────────────────────────────────────────────────

    async def _setup_runpod(self) -> None:
        from brain.runpod_manager import RunPodManager

        self._runpod = RunPodManager()
        await self._runpod.start()

    async def _setup_core(self) -> None:
        from brain.brainstem import Brainstem
        from brain.bus import Bus
        from brain.model_router import ModelRouter
        from brain.observability.timeline import ObservabilityLayer
        from brain.pns import PNS

        self.bus = Bus()
        # Colony features: concentration tracking. Threat was the first channel
        # (prototype); the same principle — a decaying summed-concentration field
        # with quorum, slope, and silence-as-signal — now applies to several
        # channels. Each magnitude_fn returns the per-message contribution; a
        # baseline subtraction keeps only genuinely-elevated signal accumulating.
        # Silence-triggered recall (DMN) auto-generalizes across every tracked
        # topic, so a thread going quiet on ANY of these cues a reflective recall.
        from brain.settings import settings as _colony_s

        if _colony_s.get("colony_features", 0):
            # Threat — GABA dimension of affect.state.
            self.bus.track_concentration(
                "affect.state",
                lambda p: max(0.0, float((p.get("neuromod") or {}).get("GABA", 0.0)) - 0.2),
            )
            # Salience / engagement — how much the live conversation "matters".
            # temporal.features carries entities, so silence-recall gets real cues
            # (the entities that were hot before the thread went quiet).
            self.bus.track_concentration(
                "temporal.features",
                lambda p: max(0.0, float(p.get("salience", 0.0)) - 0.3),
            )
            # Memory demand — recall requests building up over a memory-heavy stretch.
            self.bus.track_concentration("mem.recall", lambda p: 1.0)
        self.obs = ObservabilityLayer(self.session_id)
        (
            self._eval_logger,
            self._baseline_runner,
            self._posthoc_scorer,
            self._emotion_judge,
            self._learning_monitor,
            self._learning_judge,
            self._relationship_judge,
        ) = self._bootstrap_eval_system(self.obs)
        self.obs._eval_logger = self._eval_logger
        self.router = ModelRouter(obs=self.obs)
        # Per-brain tier gate (multi-persona Step 5C). A 'lite' persona holds no local
        # pod, so its local routes must run on cloud; a 'full' persona gets the pod +
        # background thinking. An explicit BRAIN_TIER=full|lite is AUTHORITATIVE — the
        # operator's choice wins and is never silently downgraded by the agents table.
        # Only when BRAIN_TIER is unset/"auto" do we derive the effective tier from this
        # persona's enabled agents (full if any is full; full by default when the persona
        # has no agent config at all). Falls back to the router default if unreachable.
        _tier_env = os.environ.get("BRAIN_TIER", "").strip().lower()
        if _tier_env in ("full", "lite"):
            self.router._local_disabled = _tier_env == "lite"
        else:
            try:
                _tier_persona = str(_brain_settings.get("persona_name", "")).strip()
                if _tier_persona:
                    from brain import agents as _agents

                    self.router._local_disabled = _agents.effective_tier(_tier_persona) == "lite"
            except Exception:
                logger.debug("[setup] tier resolution skipped (agents table unavailable)", exc_info=True)
        self.brainstem = Brainstem(self.bus, self.router)
        self._proactive_idle_threshold = _brain_settings.get("proactive_idle_threshold") or float(
            os.environ.get("BRAIN_PROACTIVE_IDLE_THRESHOLD", "180")
        )
        self._proactive_response_window = _brain_settings.get("proactive_response_window") or float(
            os.environ.get("BRAIN_PROACTIVE_RESPONSE_WINDOW", "8")
        )
        self.pns = PNS(self.bus, on_speaking_change=self._on_speaking_change)
        # Apply the active persona's saved voice ID at boot so the voice follows
        # the persona across restarts without requiring manual re-selection.
        # Prefer the persona-specific voice (persona_voice_<slug>); fall back to
        # the generic persona_voice_id only when this persona has none set.
        from brain.persona_chem import voice_id_for

        _persona_vid = voice_id_for()
        if _persona_vid:
            self.pns.set_voice_id(_persona_vid)

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
        # Stamp ledger records with this session (persona comes from the contextvar).
        try:
            from brain.observability import learning_ledger

            learning_ledger.set_session(self.session_id)
        except Exception:
            pass

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
            await self.skill_selector.warm_native_skills()
            # App-provided skills (org's approved library) — best-effort: no Supabase
            # or no skills → no-op, never blocks boot.
            await self.skill_selector.warm_partner_skills()
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
        # Bond model: apply absence decay for known speakers before any turns,
        # so a long gap has cooled the relationship (and a warm reengagement can
        # recover it fast). Refreshing of `Last seen` happens at consolidation.
        await self.hippocampus.apply_relationship_decay_at_boot()
        # Reload the user's learned style register so style synchrony resumes
        # warm instead of needing 3 fresh turns to re-warm every session (F3).
        try:
            _primary = self.hippocampus._schema.primary_user_name()
            self.parietal.load_style_from_schema(self.hippocampus._schema, _primary or "")
        except Exception:
            pass
        self._egress = PseudonymizationGateway()
        # Inject into the router so ALL cloud calls are pseudonymized at the
        # dispatch layer (R1 gateway backstop — catches motor, DMN, metacognition
        # paths that don't go through session_turn's per-field pseudonymization).
        self.router.set_egress(self._egress)

    async def _setup_node_registry(self) -> None:
        """Populate the non-object node manifest and audit the wiring graph against the node
        registry (brain/node_registry). Object-backed nodes (cells, switches) registered
        themselves during cluster construction; this fills in the intentional non-object
        classifications (channels / recall strategies / coarse subsystems) and logs ORPHAN NAMES
        (graph nodes with no backing object or classification — the dead-edge danger) and UNWIRED
        OBJECTS. Behavior-neutral: nothing routes through the registry. Wrapped so a registry bug
        can never break boot (mirrors the learning_ledger / Autonomy guards elsewhere in setup)."""
        try:
            from brain.node_registry import (
                audit_node_registry,
                get_node_registry,
                register_fragment_nodes,
                register_manifest,
            )
            from brain.observability.decisions import decisions as decisions_log

            reg = get_node_registry()
            register_manifest(reg)
            # Classify any learned fragment attachments the boot persona already has, so
            # `fragment.*` edge endpoints reconcile as kind="fragment" instead of ORPHAN.
            register_fragment_nodes(self.wiring, reg)
            report = audit_node_registry(self.wiring, reg, log=logger)
            decisions_log.log(
                "node_registry_audit",
                cluster="wiring",
                orphans=report["orphans"],
                unwired=report["unwired"],
                graph_nodes=report["graph_nodes"],
                registered=report["registered"],
                object_backed=report["object_backed"],
            )
        except Exception as e:
            logger.warning("[node-registry] audit skipped (non-fatal): %s", e)

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
            on_tts_mute=self.pns.set_tts_muted,
            is_muted_fn=self._is_mic_muted,
            mic_status_fn=self._mic_status,
            on_interrupt=self.pns.interrupt,
            on_tasks_clear=self.kill_self_directed_work,
            on_task_kill=self.kill_task,
            on_task_approve=self.approve_action,
            on_task_skip=self.skip_action,
            approvals_fn=self.list_approvals,
            jobs_list_fn=self.api_list_jobs,
            job_get_fn=self.api_get_job,
            on_feedback=self.api_grade_turn,
            connectors_fn=lambda: (
                self.motor.list_connectors() if getattr(self, "motor", None) else []
            ),
            connector_reload_fn=lambda: (
                self.motor._cloud.reload_mcp_config()
                if getattr(self, "motor", None)
                and getattr(getattr(self, "motor", None), "_cloud", None) is not None
                and hasattr(getattr(self, "motor", None)._cloud, "reload_mcp_config")
                else None
            ),
            # Status of the cloud connector (Claude) the MCP connectors run through,
            # so the Connectors UI can show whether it's hooked up + which model.
            cloud_status_fn=lambda: (
                {
                    "available": bool(getattr(self.motor._cloud, "available", False)),
                    "model": str(getattr(self.motor._cloud, "_model", "") or ""),
                    "actions_enabled": bool(_brain_settings.get("motor_enable_cloud_actions")),
                    # Claude's built-in (native) tools, distinct from MCP connectors.
                    "native_tools": (
                        self.motor._cloud.native_tools()
                        if hasattr(self.motor._cloud, "native_tools")
                        else []
                    ),
                }
                if getattr(self, "motor", None)
                and getattr(getattr(self, "motor", None), "_cloud", None) is not None
                else {"available": False}
            ),
            # Report the resolved tier on /health so the gateway only spins the shared
            # GPU pod for full-tier brains. _local_disabled is the authoritative signal:
            # a lite brain has local routing off (every local/runpod route → cloud), so
            # it never touches the pod. Read lazily so it reflects the final resolution.
            tier_fn=lambda: "lite" if getattr(self.router, "_local_disabled", False) else "full",
            # Per-agent model usage for the Agents dashboard cost monitor. No range →
            # the live in-memory meter ("This session"); a [since, until] range → the
            # durable ledger summed across restarts (migration 016).
            usage_fn=self._agent_usage_for_ui,
            # Reload approved app-provided skills into the live index after the Skills
            # tab approves/rejects/deletes one (no-op when the selector is unavailable).
            skill_rewarm_fn=(
                self.skill_selector.warm_partner_skills
                if getattr(self, "skill_selector", None) is not None
                else None
            ),
            wiring=self.wiring,
            bus=self.bus,
        )
        ui_server.set_wiring_frozen(self._wiring_frozen)
        self.brainstem.register_loop("ui_server", lambda: ui_server.start(), restart_on_crash=False)
        self._ui_server = ui_server

        # Wire TTS chunks to the browser when in browser audio mode
        from brain.pns import BROWSER_AUDIO_MODE

        if BROWSER_AUDIO_MODE:
            ui_server.attach_tts_queue(self.pns)
            logger.info("TTS audio routed to browser WebSocket")

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
        from brain.emotion_vocabulary import compute_affect_dims

        _arousal0 = compute_affect_dims(_nm, _hs).get("arousal")
        await self._emitter.emit_emotion(_emotion, _arousal0)

    async def _setup_api(self) -> None:
        """Start the engine API server iff this org has a runtime key — an owner env
        key (BRAIN_API_KEYS) OR per-partner keys in the api_keys table (the
        multi-tenant B2B path the gateway routes /v1 to). OFF by default: the
        companion product sets no key and provisions none, so it stays inert there."""
        from brain.api.auth import has_any_api_keys

        if not has_any_api_keys():
            return
        from brain.api import ApiServer

        # App-provided skills: the screener runs the admission pipeline on submit; the
        # rewarm reloads the org's approved skills into the live index after any change.
        # Both are no-ops when the skill selector is unavailable (index missing).
        skill_screener = None
        skill_rewarm = None
        if getattr(self, "skill_selector", None) is not None:
            from brain.skills_screener import SkillScreener

            _screener = SkillScreener(self.router)
            skill_screener = _screener.screen
            skill_rewarm = self.skill_selector.warm_partner_skills

        self._api_server = ApiServer(
            self.api_turn,
            consolidate_runner=self.consolidate_now,
            confirm_runner=self.api_confirm,
            approvals_list_runner=self.api_list_approvals,
            approval_resolve_runner=self.api_resolve_approval,
            jobs_list_runner=self.api_list_jobs,
            job_get_runner=self.api_get_job,
            learning_runner=self.api_learning,
            purge_runner=self.api_purge_end_user,
            extract_runner=self.api_extract,
            skill_screener=skill_screener,
            skill_rewarm=skill_rewarm,
        )
        self.brainstem.register_loop(
            "api_server", lambda: self._api_server.start(), restart_on_crash=False
        )
        logger.info("Engine API enabled on port %s", os.environ.get("BRAIN_API_PORT", "8780"))

    async def _setup_motor(self) -> None:
        if not (self.args.motor or os.environ.get("BRAIN_MOTOR", "false").lower() == "true"):
            self.frontal.set_capabilities(
                "Tool use is DISABLED this session (motor cortex not enabled). "
                "If asked to use external tools, explain that you'd need to be "
                "restarted with --motor."
            )
            return

        from brain.clusters.chunk_memory import ChunkMemorySubsystem
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

        # Executor selection: "cma" → Anthropic Managed Agents (server-side, no
        # local CLI); anything else → the local Claude CLI subprocess (default,
        # unchanged). BRAIN_EXECUTOR env overrides the `brain_executor` setting.
        from brain.settings import settings as _settings

        # User-managed allowlist (Settings → Motor Cortex). On a hosted tenant
        # this is the only path source besides BRAIN_MOTOR_PATHS, since there's
        # no Claude Desktop to inherit trusted folders from. One dir per line.
        _settings_dirs = [
            _p.strip()
            for _p in str(_settings.get("motor_allowed_dirs") or "").splitlines()
            if _p.strip()
        ]
        # Read-only roots + capability switches (Settings → Motor Permissions).
        _motor_ro_paths = [
            _p.strip()
            for _p in str(_settings.get("motor_read_only_dirs") or "").splitlines()
            if _p.strip()
        ]
        # Hosted: re-enforce the tenant-root jail on the TENANT-EDITABLE dirs at
        # bake time. The settings-save path already jails them, but a dir that
        # resolved inside the root at save time can be swapped for a symlink
        # pointing outside it before the next boot (TOCTOU) — re-check what each
        # path resolves to NOW. Operator-set BRAIN_MOTOR_PATHS is trusted as-is.
        if os.environ.get("BRAIN_MULTITENANT", "").lower() in ("1", "true", "yes"):
            from brain.security import jail_dirs_to_tenant_root

            _settings_dirs = jail_dirs_to_tenant_root(_settings_dirs, label="motor rw")
            _motor_ro_paths = jail_dirs_to_tenant_root(_motor_ro_paths, label="motor ro")
        for _p in _settings_dirs:
            if _p not in _motor_paths:
                _motor_paths.append(_p)
        _motor_enable_shell = bool(int(_settings.get("motor_enable_shell", 1) or 0))
        _motor_enable_network = bool(int(_settings.get("motor_enable_network", 1) or 0))
        _motor_enable_cloud = bool(int(_settings.get("motor_enable_cloud_actions", 1) or 0))
        _motor_enable_world = bool(int(_settings.get("motor_enable_world", 0) or 0))
        # Command allowlist: env wins, then the setting, then the built-in set.
        if _motor_cmds is None:
            _cmds_setting = {
                c.strip()
                for c in str(_settings.get("motor_allowed_commands") or "").splitlines()
                if c.strip()
            }
            if _cmds_setting:
                _motor_cmds = _cmds_setting

        _executor_kind = (
            os.environ.get("BRAIN_EXECUTOR", "").strip().lower()
            or str(_settings.get("brain_executor") or "local").lower()
        )
        if _executor_kind == "cma":
            from brain.clusters.cma_executor import CMAExecutor

            cloud = CMAExecutor(
                self.bus, schema_store=self.hippocampus._schema, router=self.router
            )
            logger.info("Motor cortex: using Managed Agents executor (CMA)")
        elif _executor_kind == "generic":
            from brain.clusters.generic_executor import GenericExecutor

            cloud = GenericExecutor(
                self.bus,
                schema_store=self.hippocampus._schema,
                router=self.router,
            )
            logger.info(
                "Motor cortex: using provider-agnostic generic executor (model=%s)",
                _settings.get("motor_model") or "gpt",
            )
        else:
            from brain.clusters.cloud_executor import CloudExecutor

            cloud = CloudExecutor(self.bus, schema_store=self.hippocampus._schema)

        # CloudExecutor inherits local trusted dirs from Claude Desktop; CMAExecutor
        # runs in a cloud sandbox and has none — guard the attribute access.
        _trusted_dirs = getattr(cloud, "_trusted_dirs", None)
        if not _motor_paths and _trusted_dirs:
            _motor_paths = _trusted_dirs[:]
            logger.info(
                "Motor cortex: inheriting trusted dirs from Claude Desktop: %s", _motor_paths
            )

        # Locally, include the project root so the agent can read/write its own
        # codebase regardless of how it was launched (start.sh vs direct invocation).
        # Hosted (online) MUST NOT do this — the deployed brain has no business
        # touching its own source. This covers BOTH hosted shapes: the single
        # Railway brain (RAILWAY_ENVIRONMENT set) and per-tenant subprocesses
        # (BRAIN_MULTITENANT set; the provisioner strips RAILWAY_ENVIRONMENT from
        # them). There, paths come solely from BRAIN_MOTOR_PATHS / the
        # motor_allowed_dirs setting.
        _hosted = bool(os.environ.get("RAILWAY_ENVIRONMENT")) or os.environ.get(
            "BRAIN_MULTITENANT", ""
        ).lower() in ("1", "true", "yes")
        if _hosted:
            logger.info("Motor cortex: hosted mode — project root NOT added to allowed paths")
        else:
            from pathlib import Path as _Path

            _project_root = str(_Path(__file__).parent.parent.resolve())
            if _project_root not in _motor_paths:
                _motor_paths.insert(0, _project_root)
                logger.info(
                    "Motor cortex: project root auto-added to allowed paths: %s", _project_root
                )

        # The generic executor runs the brain's own toolset in-process, so it
        # must share the SAME final allowlist the motor cortex uses (resolved
        # just above, after trusted-dir inheritance + project-root rules).
        if _executor_kind == "generic" and hasattr(cloud, "set_allowed_paths"):
            cloud.set_allowed_paths(_motor_paths)
            if _motor_cmds is not None:
                cloud._dispatcher._allowed_commands = _motor_cmds
            # Mirror the full Motor Permissions policy onto the in-process
            # executor's dispatcher (same enforcement surface as the motor's own).
            from pathlib import Path as _P

            cloud._dispatcher._ro_paths = [str(_P(p).resolve()) for p in _motor_ro_paths]
            cloud._dispatcher._enable_shell = _motor_enable_shell
            cloud._dispatcher._enable_network = _motor_enable_network
            cloud._dispatcher._enable_world = _motor_enable_world

        self.motor = MotorCortexCluster(
            self.bus,
            self.router,
            allowed_paths=_motor_paths,
            allowed_commands=_motor_cmds,
            cloud_executor=cloud,
            read_only_paths=_motor_ro_paths,
            enable_shell=_motor_enable_shell,
            enable_network=_motor_enable_network,
            enable_cloud=_motor_enable_cloud,
            enable_world=_motor_enable_world,
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
        self.motor.register_subsystem(ChunkMemorySubsystem())
        self._follow_through = FollowThrough(self.router)
        self._result_reporter = ResultReporter(self.router)
        self._task_queue = PersistentTaskQueue()
        self._recent_task_results: list[dict] = []  # ring buffer: last 3 completed tasks

        # Action-approval ledger: sensitive tool calls the cloud executor flagged
        # 'ask' are recorded here, surfaced for approval, and re-run once approved.
        from brain.clusters.approvals import PendingApprovals

        self._approvals = PendingApprovals()
        if hasattr(self.motor, "_cloud") and hasattr(self.motor._cloud, "set_approval_fn"):
            self.motor._cloud.set_approval_fn(self._gate_action)

        # Spend/risk gate (brain.autonomy): the single autonomy policy point — autonomous
        # budget (soft $30 pause / hard $50 stop), rate, cloud-health → RUN/DEFER/STOP, and
        # external-side-effect → approval. Injected into the motor, which also hands it to
        # the router so bg cloud-health (timeout/success) feeds the CLOUD_UNREACHABLE cooldown.
        try:
            from brain.autonomy import AutonomousBudget, SpendRiskGate

            self._spend_gate = SpendRiskGate(
                AutonomousBudget(self.router), self._approvals, self.router
            )
            if hasattr(self.motor, "set_spend_gate"):
                self.motor.set_spend_gate(self._spend_gate)
        except Exception as e:
            logger.warning("[Autonomy] SpendRiskGate wiring failed (non-fatal): %s", e)

        _recovered = self._task_queue.recover_interrupted()
        if _recovered:
            logger.info(
                "[TaskQueue] %d task(s) recovered from previous session: %s",
                len(_recovered),
                "; ".join(t.goal[:60] for t in _recovered),
            )

        # Repair the job-outcome mirror split-brain: local JobStore JSON and the
        # durable agent_jobs table are written independently best-effort, so a job
        # can finish locally yet stay missing/stuck-'running' in the table. Runs
        # off-thread (network I/O); silent no-op in local/companion mode.
        _job_store = getattr(getattr(self, "motor", None), "job_store", None)
        if _job_store is not None:

            async def _reconcile_jobs() -> None:
                from brain import agent_jobs_store

                try:
                    await asyncio.to_thread(agent_jobs_store.reconcile, _job_store)
                except Exception as e:
                    logger.warning("[agent_jobs] boot reconcile failed: %s", e)

            asyncio.create_task(_reconcile_jobs())

        self._lobe_bridge = LobeBridge()
        self._lobe_bridge.register("recall_memory", self._recall_memory)
        self._lobe_bridge.register("analyze_image", self._analyze_image)
        self.motor.set_lobe_bridge(self._lobe_bridge)
        self.motor.set_observability(self.obs)

        # ── Advise-only day-trading layer (dark unless trading_enabled) ──────────
        # BOUNDARY: this native layer is retired in favour of the trading app's MCP
        # connectors (register "trading" via BRAIN_CMA_MCP_SERVERS +
        # BRAIN_CMA_MCP_TRADING_TOKEN, bound to the trading mandate; plus the
        # read-only "trading-readonly" + BRAIN_CMA_MCP_TRADING_READONLY_TOKEN, bound
        # to the six bull/bear/risk/pm/mispricing/reflection debate agents — see
        # brain/clusters/trading/README.md). The package stays in-tree so it can be
        # re-enabled, but it is OFF by default. BRAIN_NATIVE_TRADING is an env-level
        # override (Railway-settable) that wins over the per-tenant settings.json so
        # a stale `trading_enabled: 1` on a tenant volume can be force-disabled
        # without editing that file: "0"/"false"/"off"/"no" → off, any other value
        # → on, unset → fall back to the trading_enabled setting (default 0).
        from brain.settings import settings as _bsettings

        _native_trading = bool(int(_bsettings.get("trading_enabled") or 0))
        _env_native = os.environ.get("BRAIN_NATIVE_TRADING", "").strip().lower()
        if _env_native:
            _native_trading = _env_native not in ("0", "false", "off", "no")

        if _native_trading:
            try:
                from brain.clusters.trading.alpaca_mcp_client import AlpacaMCPClient
                from brain.clusters.trading.subsystem import TradingSubsystem
                from brain.clusters.trading.tools import TradingTools

                _ttl = float(_bsettings.get("trading_cache_ttl_s") or 30.0)

                # Market-data client: paper key — fetches quotes/bars/indicators.
                # This key never authenticates against the real account.
                _alpaca_data = AlpacaMCPClient(cache_ttl_s=_ttl)

                # Account-sync client: live read-only key, if configured.
                # Used ONLY by account_sync (positions, fills, portfolio history).
                # Falls back to the paper client if live keys aren't set yet.
                _live_key = os.environ.get("ALPACA_LIVE_API_KEY", "").strip()
                _live_secret = os.environ.get("ALPACA_LIVE_SECRET_KEY", "").strip()
                if _live_key and _live_secret:
                    _alpaca_account = AlpacaMCPClient(
                        api_key=_live_key,
                        secret_key=_live_secret,
                        paper=False,
                        cache_ttl_s=_ttl,
                    )
                    logger.info("Trading: live account client configured (read-only)")
                else:
                    _alpaca_account = _alpaca_data  # same paper client until live keys arrive
                    logger.info("Trading: live keys not set — account sync uses paper client")

                _trading = TradingTools(
                    alpaca_client=_alpaca_data,
                    alpaca_account_client=_alpaca_account,
                    router=self.router,
                    hippocampus=self.hippocampus,
                )
                self.motor.set_trading_tools(_trading)
                self.motor.register_subsystem(
                    TradingSubsystem(market_data=_trading._md, hippocampus=self.hippocampus)
                )

                # Watchlist stream — constructed but NOT started.
                # Start it explicitly via the start_watchlist_stream tool.
                from brain.clusters.trading.stream import WatchlistStream
                from brain.ui.emitter import emitter as _ui_emitter

                self._trading_stream = WatchlistStream(
                    api_key=os.environ.get("ALPACA_API_KEY"),
                    secret_key=os.environ.get("ALPACA_SECRET_KEY"),
                    market_data=_trading._md,
                    emitter=_ui_emitter,
                )
                _trading.set_stream(self._trading_stream)

                logger.info(
                    "Trading layer ENABLED (advise-only; data=%s live_account=%s — "
                    "stream ready, use start_watchlist_stream to activate)",
                    _alpaca_data.available,
                    _alpaca_account.available,
                )
            except Exception as e:
                logger.warning("Trading layer failed to initialize: %s", e)

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
        # Advise-only trading tools: advertise to the drafters so the brain knows
        # in conversation that it already HAS these (reusing the motor's own hint
        # as the single source of truth). The explicit note stops it from hunting
        # for a "trading skill module" file instead of just calling the tools.
        if getattr(self.motor, "_trading", None) is not None:
            cap_lines.append(
                "- Day-trading analysis is AVAILABLE right now as DIRECT, built-in tools "
                "(advise-only — read-only, never places orders). Call them directly; do NOT "
                "look for a 'trading skill module' file or load anything first:\n"
                + getattr(self.motor, "_trading_hint", "")
            )
        self.frontal.set_capabilities("\n".join(cap_lines))

    async def _setup_dmn(self) -> None:
        if not (self.args.dmn or os.environ.get("BRAIN_DMN", "false").lower() == "true"):
            return
        # Lite-tier brains hold no local pod and exist only to serve on-demand turns —
        # they must never run an autonomous idle loop. A lite brain remaps every local
        # route to cloud (_resolve_model_id), so DMN's every-8s monologue would bill
        # Anthropic continuously while the user is away. No idle thinking for lite brains;
        # that absence is the whole point of the tier.
        if getattr(self.router, "_local_disabled", False):
            logger.info("[DMN] Skipped — lite-tier brain runs no idle thinking loop")
            return

        from brain.dmn import DefaultModeNetwork

        self.dmn = DefaultModeNetwork(
            self.bus, self.router, self.hippocampus, self.parietal, obs=self.obs
        )
        # Wire the thalamus so the idle mind can consult the persistent workspace
        # spotlight (its liveness gate) alongside the drained attention.focus broadcasts.
        self.dmn.set_thalamus(self.thalamus)
        if getattr(self, "skill_selector", None) is not None:
            self.dmn.set_skill_selector(self.skill_selector)

        # Let the idle loop see what it has already read (deduped source log) so it
        # avoids re-fetching the same article or re-researching a covered topic.
        self.dmn.set_sources_provider(
            lambda: (
                self.motor.job_store.recent_sources()
                if getattr(self, "motor", None) is not None
                and hasattr(self.motor, "job_store")
                else []
            )
        )

        if self._emitter:
            self._dmn_orig_tick = self.dmn._tick
            self.dmn._tick = self._dmn_tick_with_ui
            self._thought_inbox = self.bus.subscribe("stream.thought")
            self.brainstem.register_loop("forward_thoughts", self._forward_thoughts)

        await self.dmn.start(self.session_id)

        # One-shot: fold any legacy self.md "## Open Questions" into the unified
        # ledger so open questions live in one place (no-op if none exist).
        try:
            await self.dmn.migrate_legacy_open_questions()
        except Exception as _mig_err:
            logger.warning("[DMN] Legacy open-questions migration skipped: %s", _mig_err)

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
        # Same tier gate as DMN: metacognition's periodic self-reflection is an
        # autonomous idle loop whose calls resolve to cloud on a lite brain. Lite
        # brains run no background thinking, so skip it.
        if getattr(self.router, "_local_disabled", False):
            logger.info("[Self-monitor] Skipped — lite-tier brain runs no idle reflection loop")
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
        if not self._voice_requested:
            # No server-side capture requested → leave _streaming_mic None and let
            # the UI report "off" so the browser captures audio itself.
            self._mic_setup_done = True
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
            # Hosted servers have no audio input device — start() raises here.
            # Drop to None so the UI reports "off" and the browser self-captures.
            logger.error("[I/O] Streaming mic failed to start — voice input is offline: %s", e)
            self._streaming_mic = None
        finally:
            self._mic_setup_done = True

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
        self.brainstem.register_loop("runpod_heartbeat", self._runpod_heartbeat_loop)
        self.brainstem.register_loop("usage_flush", self._usage_flush_loop)
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

        # Crash-safety replay: fold any turn traces a prior (ungracefully killed)
        # run left un-consolidated back into the buffers, so their learning still
        # lands on the next consolidation — periodic, trace-cap, or the SIGTERM
        # end-of-session pass — with per-persona attribution intact (each trace
        # carries its own persona_name). Runs regardless of the periodic-sleep
        # toggle, since the shutdown pass commits the buffer even with it off, so
        # replayed orphans are never stranded. Guarded — never breaks boot.
        try:
            from brain.observability import trace_journal

            _orphan_full, _orphan_sum = trace_journal.load_orphans()
            if _orphan_full or _orphan_sum:
                self._session_traces_full.extend(_orphan_full)
                self._session_traces.extend(_orphan_sum)
                logger.info(
                    "[trace_journal] Replayed %d orphaned turn trace(s) from a prior run "
                    "— they will consolidate on the next pass",
                    max(len(_orphan_full), len(_orphan_sum)),
                )
        except Exception as _tj_err:
            logger.debug("[trace_journal] boot replay skipped: %s", _tj_err)
