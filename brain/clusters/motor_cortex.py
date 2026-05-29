"""
Motor Cortex — tool planning and execution.
Fires when temporal.features has requires_action=true.
Planner (LLM) selects the right tool + args; executor dispatches synchronously.
Result is published to motor.result and injected into frontal drafter context.

Allowed paths and commands are configured via env vars:
  BRAIN_MOTOR_PATHS     colon-separated list of allowed filesystem roots
  BRAIN_MOTOR_COMMANDS  colon-separated list of allowed base command names
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.clusters.motor_subsystem import MotorSubsystem
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.settings import settings as _brain_settings
from brain.utils import safe_json_parse

logger = logging.getLogger(__name__)

CLUSTER = "motor_cortex"

# ── Timeout / retry configuration ─────────────────────────────────────────────
# Per-tool dispatch timeout: how long a single tool call may run before it is
# killed and returns [error]. Covers all tools — sync file ops run in executor,
# async ops (run_command, fetch_url, query_langfuse) have their own inner
# timeouts but the envelope here is the hard ceiling.
# Override: BRAIN_TOOL_TIMEOUT_SECONDS env var > brain/settings.json > 30s default.
_TOOL_TIMEOUT_S: float = float(
    os.environ.get("BRAIN_TOOL_TIMEOUT_SECONDS") or _brain_settings.get("tool_timeout_seconds", 30)
)

# Per-dispatch retry count for transient [error] outputs (not [blocked]).
# A retry only fires when the tool returned [error] — it does NOT retry [blocked]
# (safety sandbox) or deliberate tool="none" skips.
# Override: BRAIN_TOOL_RETRIES env var > brain/settings.json > 1 default.
_TOOL_RETRIES: int = int(
    os.environ.get("BRAIN_TOOL_RETRIES") or _brain_settings.get("tool_retries", 1)
)

# Wall-clock deadline for a complete internal job (all stories + retries).
# Prevents a slow local model or pathological retry loop from running forever.
# Override: BRAIN_JOB_TIMEOUT_SECONDS env var > brain/settings.json > 300s (5 min).
_JOB_TIMEOUT_S: float = float(
    os.environ.get("BRAIN_JOB_TIMEOUT_SECONDS") or _brain_settings.get("job_timeout_seconds", 300)
)

# Single source of truth for every tool name the motor cortex can actually
# dispatch (local _dispatch, cloud_action, lobe-bridge, ask_user, none). Used to
# neutralize hallucinated/typo'd expected_tool values from the strategic planner.
_DISPATCHABLE_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "append_file",
        "list_files",
        "run_command",
        "search_files",
        "fetch_url",
        "query_langfuse",
        "set_mood",
        "cloud_action",
        "recall_memory",
        "analyze_image",
        "ask_user",
        "none",
    }
)

# Deterministic tool-appropriateness guard (no LLM). Targets the one observed
# failure class: the observability tool picked for a non-observability goal.
_OBSERVABILITY_KEYWORDS = (
    "langfuse",
    "trace",
    "observability",
    "telemetry",
    "score",
    "session",
    "latency",
    "span",
    "monitor",
)


def _tool_appropriateness_warning(tool: str, goal: str, story_desc: str) -> str | None:
    """Conservative, deterministic mismatch detector. Returns a retry hint or None.

    Only flags the highest-confidence mismatch we've actually seen fail —
    query_langfuse used on a goal/story with nothing to do with observability.
    Keep this narrow; broaden only if job_store shows new failure classes.
    """
    text = f"{goal} {story_desc}".lower()
    if tool == "query_langfuse" and not any(k in text for k in _OBSERVABILITY_KEYWORDS):
        return (
            "query_langfuse reads observability data, but this goal is not about "
            "traces/scores/telemetry. Use a file/codebase tool instead "
            "(list_files, read_file, search_files, run_command)."
        )
    return None


# Imports deferred to module bottom to avoid a circular import with the motor
# subpackage (job_store / motor_dispatcher import names defined above).
from brain.clusters.job_store import JobStore  # noqa: E402
from brain.clusters.motor_dispatcher import ToolDispatcher  # noqa: E402
from brain.clusters.motor_prompts import (  # noqa: E402
    CRITERIA_CHECK_SYSTEM as _CRITERIA_CHECK_SYSTEM,
)
from brain.clusters.motor_prompts import (  # noqa: E402
    PLANNER_SYSTEM_BASE as _PLANNER_SYSTEM_BASE,
)
from brain.clusters.motor_prompts import (  # noqa: E402
    STRATEGIC_SYSTEM as _STRATEGIC_SYSTEM,
)
from brain.clusters.motor_prompts import (  # noqa: E402
    VERIFIER_SYSTEM as _VERIFIER_SYSTEM,
)


class MotorCortexCluster:
    def __init__(
        self,
        bus: Bus,
        router: ModelRouter,
        allowed_paths: list[str] | None = None,
        allowed_commands: set[str] | None = None,
        cloud_executor=None,
    ) -> None:
        self._bus = bus
        self._router = router
        self._cloud = cloud_executor
        self._pending_task = None  # set post-init via set_pending_task()
        self._lobe_bridge = None  # set post-init via set_lobe_bridge()
        self._obs = None  # set post-init via set_observability()
        self.job_store = JobStore()
        self._subsystems: list[MotorSubsystem] = []

        self._dispatcher = ToolDispatcher(allowed_paths, allowed_commands)

        # Build planner prompt dynamically: include cloud connector hint if available
        if cloud_executor and cloud_executor.available:
            self._cloud_hint = (
                f"Cloud connectors currently enabled: {cloud_executor.connectors_summary()}. "
                "Use cloud_action for any request that involves these services."
            )
        else:
            self._cloud_hint = "No cloud connectors available — use local tools only."

        planner_system = _PLANNER_SYSTEM_BASE.format(
            path_hint=self._build_path_hint(),
            cloud_connector_hint=self._cloud_hint,
            lobe_hint="Lobe capabilities (recall_memory, analyze_image) not yet configured.",
        )

        # Planner: local Ollama — tool decisions stay on-device
        self._planner = IntegratorCell(
            name="tool_planner",
            cluster=CLUSTER,
            model="local-code",
            system_prompt=planner_system,
            topics=["temporal.features"],
            max_calls_per_turn=2,
            timeout_seconds=60.0,
            locality="local",
            skills=[
                "quality-debugging",
                "dev-process",
                "devops-git-advanced-workflows",
                # Unity skills — active when working on Evolution App or Karaoke Hero
                "unity-development",
                "unity-animation",
                "unity-physics",
                "unity-shader-graph",
                "unity-ui-toolkit",
                "unity-urp",
                "unity-input-system",
                "unity-vfx-graph",
            ],
        )
        self._planner.set_router(router)

        # Strategic planner: same cluster + model, different prompt. Used on the
        # first call of an internal job to produce an upfront plan; subsequent
        # calls use the tactical (per-step) planner above.
        self._strategic_planner = IntegratorCell(
            name="strategic_planner",
            cluster=CLUSTER,
            model="local-code",
            system_prompt=_STRATEGIC_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=60.0,
            locality="local",
        )
        self._strategic_planner.set_router(router)

        # Criteria checker: per-story acceptance criteria verification in Ralph-mode jobs.
        self._criteria_checker = IntegratorCell(
            name="criteria_checker",
            cluster=CLUSTER,
            model="local-code",
            system_prompt=_CRITERIA_CHECK_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=30.0,
            locality="local",
        )
        self._criteria_checker.set_router(router)

        # Verifier: final approval pass for medium/high complexity jobs.
        self._verifier = IntegratorCell(
            name="job_verifier",
            cluster=CLUSTER,
            model="local-code",
            system_prompt=_VERIFIER_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=30.0,
            locality="local",
        )
        self._verifier.set_router(router)

        # Switches (~6 total; 2 inhibitory ≈ 33% — acceptable for small action cluster)
        # Modulator profiles per plan /Users/russ/.claude/plans/and-what-affects-these-memoized-parnas.md.
        # Excitatory
        self._action_gate = SwitchNeuron(
            "action_gate", CLUSTER, polarity="excitatory", threshold=0.5, modulators={"DA": -0.10}
        )
        self._tool_selector = SwitchNeuron(
            "tool_selector",
            CLUSTER,
            polarity="excitatory",
            threshold=0.5,
            modulators={"ACh": +0.05},
        )
        self._result_publisher = SwitchNeuron("result_publisher", CLUSTER, polarity="excitatory")
        self._fallback_reporter = SwitchNeuron(
            "fallback_reporter",
            CLUSTER,
            polarity="excitatory",
            threshold=0.5,
            modulators={"NE": -0.10},
        )
        # Inhibitory — safety_inhibitor has a min_threshold floor so chemistry
        # alone cannot disable it (contract enforced by tests/test_motor_cortex.py).
        self._safety_inhibitor = SwitchNeuron(
            "safety_check",
            CLUSTER,
            polarity="inhibitory",
            threshold=0.5,
            modulators={"NE": -0.10, "CORT": -0.10},
            min_threshold=0.40,
        )
        # budget_inhibitor fires when budget_pressure (calls / effective_budget)
        # reaches 1.0. The chemistry effect on budget itself is already in
        # _effective_budget, so this switch stays chemistry-neutral to avoid
        # double-modulation. Acts as observational telemetry for "we hit the
        # ceiling."
        self._budget_inhibitor = SwitchNeuron(
            "budget_check", CLUSTER, polarity="inhibitory", threshold=1.0
        )

        self._calls_this_turn: int = 0

    def set_pending_task(self, pending_task) -> None:
        self._pending_task = pending_task

    def set_observability(self, obs) -> None:
        self._obs = obs

    def set_lobe_bridge(self, bridge) -> None:
        self._lobe_bridge = bridge
        caps = bridge.capabilities
        lobe_hint = (
            f"Active lobe capabilities: {', '.join(caps)}. "
            "Use recall_memory to retrieve episodic context; analyze_image for vision tasks."
            if caps
            else "No lobe capabilities registered."
        )
        self._planner.system_prompt = _PLANNER_SYSTEM_BASE.format(
            path_hint=self._build_path_hint(),
            cloud_connector_hint=self._cloud_hint,
            lobe_hint=lobe_hint,
        )
        logger.info("[MotorCortex] Lobe bridge registered: %s", caps)

    def register_subsystem(self, subsystem: MotorSubsystem) -> None:
        self._subsystems.append(subsystem)
        logger.info("[MotorCortex] Registered subsystem: %s", subsystem.name)

    def reset_turn(self, turn_id: str) -> None:
        self._calls_this_turn = 0
        self._current_turn_id = turn_id
        self._planner.reset_turn(turn_id)

    # ── Public entry point ─────────────────────────────────────────────────────

    async def execute(self, features: dict, turn_id: str) -> dict | None:
        """
        Plan and execute tools based on the user's request.

        Two modes:
        - Task mode: frontal deposited a goal → reactive loop runs until tool="none"
          or the 3-call budget is exhausted. Fires after_job hooks on completion.
        - Reactive mode: no task goal → single tool call, return result immediately.
        """
        chem = self._chem_snapshot()
        if not self._action_gate.should_fire(1.0, chem, turn_id):
            return None
        # Chemistry-modulated budget: high DA raises the effective budget
        # (eager pursuit), high CORT lowers it (resource conservation under
        # stress). Bounded to [1, 5] so the change is never extreme.
        budget = self._effective_budget(chem)
        budget_pressure = self._calls_this_turn / max(1, budget)
        if self._budget_inhibitor.should_fire(budget_pressure, chem, turn_id):
            self._budget_inhibitor.fire(
                budget_pressure,
                "budget_exhausted",
                {"calls": self._calls_this_turn, "limit": budget},
                snapshot=chem,
            )
            logger.warning(
                "[MotorCortex] Tool call budget exhausted for this turn (limit=%d)", budget
            )
            return None

        raw_text = features.get("raw_text", features.get("topic_summary", ""))

        # Pick up a goal deposited by the frontal task subsystem (if any)
        task_goal: str | None = None
        if self._pending_task and self._pending_task.has_pending():
            task_goal = self._pending_task.take()
            logger.info("[MotorCortex] Task goal received: %s", task_goal[:80])

        work_goal = task_goal or raw_text

        # Gather subsystem context (e.g. muscle memory prior procedures)
        subsystem_context = ""
        for sub in self._subsystems:
            try:
                ctx = await sub.before_plan(work_goal, self._router)
                if ctx:
                    subsystem_context += ctx + "\n\n"
            except Exception as e:
                logger.debug("[MotorCortex] Subsystem %s before_plan failed: %s", sub.name, e)

        # Check whether any subsystem has a high-confidence procedure we can run open-loop
        best_proc: dict | None = None
        best_sim: float = 0.0
        for sub in self._subsystems:
            try:
                proc, sim = await sub.recall_procedure(work_goal, self._router)
                if sim > best_sim:
                    best_proc, best_sim = proc, sim
            except Exception as e:
                logger.debug("[MotorCortex] Subsystem %s recall_procedure failed: %s", sub.name, e)

        if best_proc is not None:
            logger.info(
                "[MotorCortex] Running open-loop (sim=%.3f, uses=%d): %s",
                best_sim,
                best_proc.get("use_count", 0),
                work_goal[:60],
            )
            return await self._execute_open_loop(best_proc, task_goal, turn_id)

        steps_taken: list[dict] = []
        results_log: list[str] = []
        last_result: dict | None = None

        while self._calls_this_turn < budget:
            plan_prompt = self._build_plan_prompt(
                work_goal, features, subsystem_context, steps_taken, results_log
            )
            # Reset the planner cell each iteration: the motor's own budget
            # ([1,5], checked by the while-guard) governs how many tools a turn
            # may run, so the cell's max_calls_per_turn=2 cap must not silently
            # truncate a multi-step task goal to 2 calls (returning "" → "none").
            self._planner.reset_turn(turn_id)
            raw = await self._planner.call([{"role": "user", "content": plan_prompt}])
            plan: dict = safe_json_parse(raw) or {}

            if not plan or plan.get("tool") == "none":
                logger.debug("[MotorCortex] Planner done: %s", plan.get("reason", ""))
                break

            tool = plan.get("tool", "none")
            args = plan.get("args", {})
            reason = plan.get("reason", "")
            logger.info(
                "[MotorCortex] Step %d: %s(%s) — %s",
                len(steps_taken) + 1,
                tool,
                list(args.keys()),
                reason,
            )

            # tool_selector switch fires with the chosen tool as the tag.
            # Chemistry-modulated by ACh — high curiosity slightly raises the
            # bar for falling back to a familiar tool (favours exploration).
            self._tool_selector.fire(0.8, tool, {"args": list(args.keys())}, snapshot=chem)

            self._calls_this_turn += 1

            if tool == "cloud_action":
                last_result = await self._dispatch_cloud(args, turn_id)
                output = (last_result or {}).get("output", "")
            else:
                output = await self._dispatch(tool, args)
                last_result = {
                    "tool": tool,
                    "args": args,
                    "reason": reason,
                    "output": output,
                    "success": not output.startswith("[error]")
                    and not output.startswith("[blocked]"),
                }
                await self._bus.publish_dict("motor.result", last_result, source=CLUSTER)

            steps_taken.append({"tool": tool, "args": args, "reason": reason})
            results_log.append(output[:500] if output else "")

            # Result / fallback / safety telemetry switches.
            self._fire_outcome_switches(output, tool, chem)

            if not task_goal:
                # Reactive mode: one tool per turn
                break

        # Fire completion hooks for task-mode jobs
        if task_goal and steps_taken:
            success = all(
                not r.startswith("[error]") and not r.startswith("[blocked]") for r in results_log
            )
            await self._notify_job_complete(
                task_goal,
                steps_taken,
                results_log,
                success,
                job_id=turn_id,
            )
            step_summary = "; ".join(
                f"{s['tool']}→{'ok' if not results_log[i].startswith('[error]') else 'err'}"
                for i, s in enumerate(steps_taken)
            )
            if last_result is None:
                last_result = {}
            last_result["task_goal"] = task_goal
            last_result["steps_taken"] = len(steps_taken)
            last_result["step_summary"] = step_summary

        return last_result

    # ── Internal-directive entry point ─────────────────────────────────────
    async def execute_internal_job(self, goal: str, turn_id: str, budget: int = 0) -> dict:
        """Run a self-directed multi-step job: strategic plan upfront, then
        tactical step-by-step execution. Emits task lifecycle events for the
        UI Tasks tab. Returns a job summary dict.

        Differs from execute(): not gated by action_gate or per-turn budget;
        designed for sustained autonomous work triggered by the follow-through
        loop, not by a user-turn. Budget is chemistry-modulated (DA/CORT);
        pass budget>0 to override.
        """
        from brain.ui.emitter import emitter

        job_id = f"job_{turn_id}"
        self.reset_turn(job_id)

        # Sample neuromodulator / hormonal state once at job start.
        # This modulates how ambitious the plan is and how many steps we'll take.
        chem = self._chem_snapshot()
        effective_budget = budget if budget > 0 else self._effective_job_budget(chem)
        chem_ctx = self._chem_description(chem)

        # Wall-clock deadline for the entire job. Prevents a slow local model or
        # a pathological retry loop from hanging the task worker indefinitely.
        # _JOB_TIMEOUT_S defaults to 300s; override via BRAIN_JOB_TIMEOUT_SECONDS.
        import time as _time

        _job_deadline = _time.monotonic() + _JOB_TIMEOUT_S

        # Open a Langfuse trace so all LLM calls inside this job are nested under it.
        # record_llm_call() uses _active_spans[job_id] as the parent — the same
        # mechanism as per-turn tracing, keyed by job_id instead of turn_id.
        if self._obs:
            self._obs.begin_job(job_id, goal=goal, chem=chem)

        # Surface the job immediately so the user can see something is brewing
        # even before the strategic plan finishes (local-code planner can take
        # 10-30s on the 14B model).
        await emitter.emit_event(
            {
                "type": "task_planning",
                "job_id": job_id,
                "goal": goal,
            }
        )

        # 1. Strategic plan — pass chemistry state so the planner can calibrate
        # complexity (e.g. fewer steps when stressed, more thorough when motivated).
        self._strategic_planner.reset_turn(job_id)
        raw_plan = await self._strategic_planner.call(
            [{"role": "user", "content": f"Goal: {goal}\nBrain state: {chem_ctx}"}]
        )
        plan = safe_json_parse(raw_plan) or {}
        # Support Ralph-style "stories" (with acceptance_criteria) and legacy "steps" format.
        stories_planned = (
            plan.get("stories")
            or [
                {
                    "description": s.get("description", ""),
                    "expected_tool": s.get("expected_tool", "?"),
                    "acceptance_criteria": [],
                    "id": f"US-{i + 1:03d}",
                }
                for i, s in enumerate(plan.get("steps") or [])
            ]
            or [
                {
                    "description": goal,
                    "expected_tool": "?",
                    "acceptance_criteria": [],
                    "id": "US-001",
                }
            ]
        )
        # Neutralize hallucinated/typo'd expected_tool values so a bad strategic
        # hint can't mislead the tactical planner (or the UI). The tactical
        # planner can still pick a valid tool from the story description.
        for story in stories_planned:
            et = story.get("expected_tool", "?")
            if et not in ("?", "") and et not in _DISPATCHABLE_TOOLS:
                logger.warning(
                    "[InternalJob] Story %s: invalid expected_tool '%s' — neutralized",
                    story.get("id"),
                    et,
                )
                story["expected_tool"] = "?"

        success_criteria = plan.get("success_criteria", "")
        complexity = plan.get("complexity", "low")
        # Ralph loop activates for medium/high complexity: per-story retry + final verifier.
        use_ralph = complexity in ("medium", "high")
        _MAX_STORY_RETRIES = 2

        logger.info(
            "[InternalJob] %s — %d stories planned (complexity=%s, ralph=%s, criteria: %s)",
            goal[:60],
            len(stories_planned),
            complexity,
            use_ralph,
            success_criteria[:80],
        )
        await emitter.emit_event(
            {
                "type": "task_start",
                "job_id": job_id,
                "goal": goal,
                "steps": [s.get("description", "") for s in stories_planned],
                "success_criteria": success_criteria,
                "complexity": complexity,
            }
        )

        # 2. Ralph-style story execution: per-story acceptance criteria with retry on failure.
        # medium/high complexity activates the retry loop; low complexity executes linearly.
        # _MAX_TOTAL_ATTEMPTS caps total tool dispatches across all stories + retries so the
        # loop can never run indefinitely regardless of how many stories or retries remain.
        # Priority: BRAIN_RALPH_MAX_ATTEMPTS env var > brain/settings.json > default 12.
        _MAX_TOTAL_ATTEMPTS = int(
            os.environ.get("BRAIN_RALPH_MAX_ATTEMPTS")
            or _brain_settings.get("ralph_max_total_attempts", 12)
        )
        total_attempts = 0

        steps_taken: list[dict] = []
        results_log: list[str] = []
        last_result: dict | None = None
        clarification_question: str | None = None
        success = True

        for idx, story in enumerate(stories_planned):
            if self._calls_this_turn >= effective_budget:
                logger.warning(
                    "[InternalJob] Budget exhausted (%d) at story %d/%d",
                    effective_budget,
                    idx + 1,
                    len(stories_planned),
                )
                success = False
                break
            if total_attempts >= _MAX_TOTAL_ATTEMPTS:
                logger.warning(
                    "[InternalJob] Ralph total-attempt cap (%d) reached at story %d/%d",
                    _MAX_TOTAL_ATTEMPTS,
                    idx + 1,
                    len(stories_planned),
                )
                success = False
                break
            if _time.monotonic() >= _job_deadline:
                logger.warning(
                    "[InternalJob] Wall-clock deadline (%.0fs) reached at story %d/%d",
                    _JOB_TIMEOUT_S,
                    idx + 1,
                    len(stories_planned),
                )
                success = False
                break

            story_desc = story.get("description", "")
            story_criteria = story.get("acceptance_criteria", [])
            story_passed = False
            # Low complexity still gets ONE retry so a failed criteria/appropriateness
            # check can self-correct (the retry feeds the prior output back as a hint).
            # Extra attempts only fire on an actual failure, so happy-path stays single-attempt.
            max_attempts = (_MAX_STORY_RETRIES + 1) if use_ralph else 2

            for attempt in range(max_attempts):
                if self._calls_this_turn >= effective_budget:
                    break
                if total_attempts >= _MAX_TOTAL_ATTEMPTS:
                    logger.warning(
                        "[InternalJob] Ralph total-attempt cap reached mid-story %d", idx + 1
                    )
                    break
                if _time.monotonic() >= _job_deadline:
                    logger.warning(
                        "[InternalJob] Wall-clock deadline reached mid-story %d attempt %d",
                        idx + 1,
                        attempt + 1,
                    )
                    break

                retry_label = f" (retry {attempt})" if attempt > 0 else ""
                await emitter.emit_event(
                    {
                        "type": "task_step_start",
                        "job_id": job_id,
                        "step_index": idx,
                        "description": story_desc + retry_label,
                        "expected_tool": story.get("expected_tool", ""),
                        "attempt": attempt,
                    }
                )

                # Build tactical prompt; include acceptance criteria + retry hint
                tactical_features = {"raw_text": story_desc, "topic_summary": story_desc}
                plan_summary = " | ".join(
                    f"{i + 1}. {s.get('description', '')}" for i, s in enumerate(stories_planned)
                )
                criteria_hint = (
                    "\nAcceptance criteria:\n" + "\n".join(f"  - {c}" for c in story_criteria)
                    if story_criteria
                    else ""
                )
                retry_hint = ""
                if attempt > 0 and results_log:
                    retry_hint = (
                        f"\nPrevious attempt did not meet criteria. "
                        f"Output was:\n{results_log[-1][:300]}\nTry a different approach."
                    )
                extra_context = (
                    f"Overall goal: {goal}\nFull plan: {plan_summary}\n"
                    f"Current story ({idx + 1}/{len(stories_planned)}): {story_desc}"
                    f"{criteria_hint}{retry_hint}"
                )
                plan_prompt = self._build_plan_prompt(
                    story_desc,
                    tactical_features,
                    extra_context,
                    steps_taken,
                    results_log,
                    expected_tool=story.get("expected_tool", ""),
                )
                tactical, planner_failed = await self._tactical_plan(plan_prompt, job_id)
                tool = tactical.get("tool", "none")
                args = tactical.get("args", {})
                reason = tactical.get("reason", "")

                self._calls_this_turn += 1
                total_attempts += 1

                if planner_failed:
                    # Planner produced nothing usable (local-model timeout/error
                    # or unparseable output) even after retries. This is NOT a
                    # deliberate skip — do not mark the story passed. Record the
                    # failed attempt and let the retry loop try again; if attempts
                    # are exhausted the story stays unpassed → job fails honestly,
                    # instead of silently reporting success with zero work done.
                    logger.warning(
                        "[InternalJob] Story %d/%d attempt %d: planner produced no action",
                        idx + 1,
                        len(stories_planned),
                        attempt + 1,
                    )
                    await emitter.emit_event(
                        {
                            "type": "task_step_done",
                            "job_id": job_id,
                            "step_index": idx,
                            "success": False,
                            "criteria_verified": False,
                            "output": "[planner produced no action]",
                            "attempt": attempt,
                        }
                    )
                    steps_taken.append({"tool": "none", "args": {}, "reason": "[planner failed]"})
                    results_log.append("")
                    continue  # retry this story if attempts remain

                if tool == "none":
                    logger.info("[InternalJob] Story %d skipped: %s", idx + 1, reason)
                    await emitter.emit_event(
                        {
                            "type": "task_step_done",
                            "job_id": job_id,
                            "step_index": idx,
                            "success": True,
                            "skipped": True,
                            "output": reason[:200],
                            "attempt": attempt,
                        }
                    )
                    steps_taken.append({"tool": "none", "args": {}, "reason": reason})
                    results_log.append("")
                    story_passed = True
                    break

                if tool == "ask_user":
                    clarification_question = args.get("question", "I need more info to continue.")
                    logger.info(
                        "[InternalJob] Clarification needed at story %d: %s",
                        idx + 1,
                        clarification_question,
                    )
                    await emitter.emit_event(
                        {
                            "type": "task_clarification",
                            "job_id": job_id,
                            "step_index": idx,
                            "question": clarification_question,
                        }
                    )
                    success = False
                    break

                logger.info(
                    "[InternalJob] Story %d/%d attempt %d: %s — %s",
                    idx + 1,
                    len(stories_planned),
                    attempt + 1,
                    tool,
                    reason[:80],
                )

                if tool == "cloud_action":
                    last_result = await self._dispatch_cloud(args, job_id)
                    output = (last_result or {}).get("output", "")
                elif tool in ("recall_memory", "analyze_image"):
                    output = await self._dispatch_lobe(tool, args, job_id)
                    last_result = {
                        "tool": tool,
                        "args": args,
                        "reason": reason,
                        "output": output,
                        "success": not output.startswith("[error]"),
                    }
                    await self._bus.publish_dict("motor.result", last_result, source=CLUSTER)
                else:
                    output = await self._dispatch(tool, args)
                    last_result = {
                        "tool": tool,
                        "args": args,
                        "reason": reason,
                        "output": output,
                        "success": not output.startswith("[error]")
                        and not output.startswith("[blocked]"),
                    }
                    await self._bus.publish_dict("motor.result", last_result, source=CLUSTER)

                step_success = not output.startswith("[error]") and not output.startswith(
                    "[blocked]"
                )
                steps_taken.append({"tool": tool, "args": args, "reason": reason})
                results_log.append(output[:500] if output else "")
                self._fire_outcome_switches(output, tool, self._chem_snapshot())

                # Deterministic tool-appropriateness guard (no LLM). If the chosen
                # tool is an obvious mismatch, fail the story like an unmet criterion
                # so the existing retry loop re-plans with a concrete hint. The hint
                # is written into results_log[-1] because retry_hint reads it.
                warning = _tool_appropriateness_warning(tool, goal, story_desc)
                if warning:
                    logger.warning("[InternalJob] Tool appropriateness: %s", warning)
                    verified, unmet = False, [warning]
                    results_log[-1] = f"[tool-mismatch] {warning}"
                # Check acceptance criteria before marking story done. Runs on ALL
                # complexity levels (not just Ralph) — it's the only thing that can
                # detect "tool ran fine but produced the wrong kind of output."
                elif story_criteria:
                    verified, unmet = await self._check_story_criteria(
                        story, output, f"{job_id}_{idx}_{attempt}"
                    )
                    logger.info(
                        "[InternalJob] Story %d criteria: verified=%s unmet=%s",
                        idx + 1,
                        verified,
                        unmet,
                    )
                else:
                    verified = step_success
                    unmet = []

                await emitter.emit_event(
                    {
                        "type": "task_step_done",
                        "job_id": job_id,
                        "step_index": idx,
                        "tool": tool,
                        "success": step_success,
                        "criteria_verified": verified,
                        "output": (output or "")[:300],
                        "attempt": attempt,
                    }
                )

                if verified:
                    story_passed = True
                    break
                # criteria not met — retry if attempts remain

            if clarification_question:
                break
            if not story_passed:
                success = False
                logger.warning(
                    "[InternalJob] Story %d/%d not verified after %d attempt(s): %s",
                    idx + 1,
                    len(stories_planned),
                    max_attempts,
                    story_desc[:60],
                )

        # 3. Ralph: final verification pass for medium/high complexity jobs
        verification_issues = ""
        if use_ralph and success and not clarification_question and steps_taken:
            approved, issues = await self._verify_job(
                goal, success_criteria, steps_taken, results_log, job_id
            )
            if not approved:
                verification_issues = issues
                success = False
                logger.warning("[InternalJob] Final verifier rejected: %s", issues[:120])
            else:
                logger.info("[InternalJob] Final verifier approved")
            await emitter.emit_event(
                {
                    "type": "task_verified",
                    "job_id": job_id,
                    "approved": approved,
                    "issues": issues,
                }
            )

        # 4. Completion
        await self._notify_job_complete(
            goal,
            steps_taken,
            results_log,
            success,
            job_id=job_id,
            source="self" if getattr(self, "_current_source", "") == "self" else "user",
            ralph_mode=use_ralph,
            total_attempts=total_attempts,
            plan_steps=stories_planned,
        )
        await emitter.emit_event(
            {
                "type": "task_complete",
                "job_id": job_id,
                "success": success,
                "steps_completed": len(steps_taken),
                "clarification": clarification_question,
            }
        )
        logger.info(
            "[InternalJob] Done: %s (success=%s, ralph=%s, %d/%d stories)",
            goal[:60],
            success,
            use_ralph,
            len(steps_taken),
            len(stories_planned),
        )
        if self._obs:
            self._obs.end_job(
                job_id,
                success=success,
                steps_completed=len(steps_taken),
                steps_planned=len(stories_planned),
                total_attempts=total_attempts,
            )
        return {
            "job_id": job_id,
            "goal": goal,
            "success": success,
            "steps_taken_count": len(steps_taken),
            "steps_planned_count": len(stories_planned),
            "steps": steps_taken,
            "results": results_log,
            "plan_steps": stories_planned,
            "clarification": clarification_question,
            "last_output": (last_result or {}).get("output", "") if last_result else "",
            "ralph_mode": use_ralph,
            "verification_issues": verification_issues,
            "total_attempts": total_attempts,
            "attempt_cap": _MAX_TOTAL_ATTEMPTS,
        }

    async def _tactical_plan(
        self, plan_prompt: str, job_id: str, retries: int = 2
    ) -> tuple[dict, bool]:
        """Run the tactical (per-step) planner for an internal job.

        The same _planner cell is reused for every story in a job, but the job
        loop enforces its own budget (effective_budget + _MAX_TOTAL_ATTEMPTS).
        The cell's max_calls_per_turn cap would otherwise silently shadow that
        budget — after 2 calls _can_fire() returns False and call() returns ""
        for every remaining story. So reset the cell before each attempt to keep
        the cap from biting, and retry on empty/unparseable output to ride out a
        transient local-model timeout or error.

        Returns (tactical_dict, failed) where failed=True means the planner
        produced no usable plan after all retries — a real failure, NOT a
        deliberate tool="none" skip.
        """
        for attempt in range(retries + 1):
            self._planner.reset_turn(job_id)
            raw = await self._planner.call([{"role": "user", "content": plan_prompt}])
            parsed = safe_json_parse(raw)
            if raw and parsed is not None:
                return parsed, False
            logger.warning(
                "[InternalJob] tactical planner returned no usable output (try %d/%d) — retrying",
                attempt + 1,
                retries + 1,
            )
        return {}, True

    async def _check_story_criteria(
        self, story: dict, output: str, check_id: str
    ) -> tuple[bool, list[str]]:
        """Verify a story's acceptance criteria against the tool output.
        Returns (verified, unmet_criteria). Falls back to error-status check if no criteria."""
        criteria = story.get("acceptance_criteria", [])
        if not criteria:
            ok = not output.startswith("[error]") and not output.startswith("[blocked]")
            return ok, []
        prompt = (
            f"Story: {story.get('description', '')}\n"
            f"Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria) + "\n\n"
            f"Tool output:\n{output[:1200]}"
        )
        self._criteria_checker.reset_turn(check_id)
        raw = await self._criteria_checker.call([{"role": "user", "content": prompt}])
        result = safe_json_parse(raw) or {}
        return bool(result.get("verified", True)), result.get("unmet", [])

    async def _verify_job(
        self, goal: str, success_criteria: str, steps: list[dict], results: list[str], job_id: str
    ) -> tuple[bool, str]:
        """Final approval pass for medium/high complexity jobs. Returns (approved, issues)."""
        steps_summary = "\n".join(
            f"{i + 1}. {s['tool']}({list(s['args'].keys())}) → {r[:200]}"
            for i, (s, r) in enumerate(zip(steps, results, strict=False))
        )
        prompt = (
            f"Goal: {goal}\n"
            f"Success criteria: {success_criteria}\n\n"
            f"Steps executed:\n{steps_summary}"
        )
        self._verifier.reset_turn(f"{job_id}_verify")
        raw = await self._verifier.call([{"role": "user", "content": prompt}])
        result = safe_json_parse(raw) or {}
        return bool(result.get("approved", True)), result.get("issues", "")

    async def _execute_open_loop(
        self, procedure: dict, goal: str | None, turn_id: str
    ) -> dict | None:
        """
        Execute a familiar procedure without per-step LLM planning — analogous to
        how M1 fires pre-planned motor commands once a movement is well-learned.

        Validates each step's output against stored expected results. On significant
        divergence (error status mismatch or wildly different output length), marks
        the procedure as stale so it falls back to reactive planning next time.
        """
        steps = procedure.get("steps", [])
        proc_id = procedure.get("id", "")

        steps_taken: list[dict] = []
        results_log: list[str] = []
        prediction_errors: int = 0
        last_result: dict | None = None

        open_loop_chem = self._chem_snapshot()
        open_loop_budget = self._effective_budget(open_loop_chem)
        for i, step in enumerate(steps):
            if self._calls_this_turn >= open_loop_budget:
                logger.warning(
                    "[MotorCortex] Open-loop budget exhausted at step %d/%d (limit=%d)",
                    i + 1,
                    len(steps),
                    open_loop_budget,
                )
                break

            tool = step.get("tool", "none")
            args = step.get("args", {})
            reason = step.get("reason", "")
            logger.info(
                "[MotorCortex] Open-loop step %d/%d: %s — %s", i + 1, len(steps), tool, reason
            )

            self._calls_this_turn += 1

            if tool == "cloud_action":
                last_result = await self._dispatch_cloud(args, turn_id)
                output = (last_result or {}).get("output", "")
            else:
                output = await self._dispatch(tool, args)
                last_result = {
                    "tool": tool,
                    "args": args,
                    "reason": reason,
                    "output": output,
                    "success": not output.startswith("[error]")
                    and not output.startswith("[blocked]"),
                }
                await self._bus.publish_dict("motor.result", last_result, source=CLUSTER)

            steps_taken.append({"tool": tool, "args": args, "reason": reason})
            results_log.append(output[:500] if output else "")

            # Outcome validation — three-level prediction hierarchy:
            # 1. predict_outcome() from any subsystem (generalises across procedures)
            # 2. Embedded _sig in the stored step (specific to this procedure)
            # 3. Raw error-status check (always available as last resort)
            prediction: dict | None = None
            for sub in self._subsystems:
                try:
                    p = await sub.predict_outcome(tool, args, results_log, self._router)
                    if p is not None:
                        prediction = p
                        break
                except Exception:
                    pass
            if prediction is None:
                prediction = step.get("_sig")  # embedded at record time

            actual_error = output.startswith("[error]") or output.startswith("[blocked]")
            if prediction is not None:
                if actual_error != (not prediction.get("expected_success", True)):
                    prediction_errors += 1
                    logger.warning(
                        "[MotorCortex] Prediction error at step %d: "
                        "expected %s, got %s (source: %s)",
                        i + 1,
                        "ok" if prediction.get("expected_success") else "error",
                        "error" if actual_error else "ok",
                        "learned" if "sample_count" in prediction else "stored",
                    )
                elif not actual_error and not prediction.get("is_empty", False):
                    n = len(output)
                    if n < prediction.get("length_min", 0) or n > prediction.get(
                        "length_max", float("inf")
                    ):
                        prediction_errors += 1
                        logger.warning(
                            "[MotorCortex] Length divergence at step %d: "
                            "expected %d–%d chars, got %d",
                            i + 1,
                            prediction["length_min"],
                            prediction["length_max"],
                            n,
                        )
            elif actual_error:
                # Fallback: unexpected error with no prior prediction is always a divergence
                prediction_errors += 1
                logger.warning(
                    "[MotorCortex] Unexpected error at step %d (no prior prediction)", i + 1
                )

        success = all(
            not r.startswith("[error]") and not r.startswith("[blocked]") for r in results_log
        )

        if prediction_errors > 0:
            logger.warning(
                "[MotorCortex] Open-loop diverged (%d prediction error(s)) — "
                "marking procedure stale",
                prediction_errors,
            )
            for sub in self._subsystems:
                if hasattr(sub, "mark_diverged"):
                    with contextlib.suppress(Exception):
                        sub.mark_diverged(proc_id)

        if steps_taken:
            await self._notify_job_complete(
                goal or "",
                steps_taken,
                results_log,
                success,
                job_id=turn_id,
            )

        if last_result is None:
            last_result = {}
        last_result["task_goal"] = goal
        last_result["steps_taken"] = len(steps_taken)
        last_result["open_loop"] = True
        last_result["prediction_errors"] = prediction_errors
        return last_result

    def _build_path_hint(self) -> str:
        return self._dispatcher.build_path_hint()

    def _build_plan_prompt(
        self,
        goal: str,
        features: dict,
        subsystem_context: str,
        steps_done: list[dict],
        results: list[str],
        expected_tool: str = "",
    ) -> str:
        parts = [
            f"Goal: {goal}",
            f"Intent: {features.get('intent', 'task')}",
            f"Entities: {features.get('entities', [])}",
            f"CWD: {self._dispatcher._allowed_paths[0] if self._dispatcher._allowed_paths else 'unknown'}",
        ]
        # Current emotional state — lets the planner reason about whether
        # set_mood adds value (e.g. entity already feels curious → redundant;
        # entity feels anxious but topic calls for confident delivery → useful).
        try:
            _nm = self._bus.neuromod.snapshot()
            _hs = self._bus.hormonal.snapshot()
            from brain.emotion_vocabulary import (
                apply_hormonal_color as _apply_horm,
            )
            from brain.emotion_vocabulary import (
                apply_ne_color as _apply_ne,
            )
            from brain.emotion_vocabulary import (
                name_emotion as _name_emotion,
            )

            _em, _tend = _name_emotion(
                _nm.get("DA", 0.5),
                _nm.get("GABA", 0.0),
                _nm.get("ACh", 0.3),
                _nm.get("Glu", 0.3),
            )
            _em, _tend = _apply_ne(_em, _tend, _nm.get("NE", 0.25))
            _em, _tend = _apply_horm(_em, _tend, _hs)
            parts.append(f"Entity emotion: {_em} (tendency: {_tend})")
        except Exception:
            pass
        if subsystem_context.strip():
            parts.append(subsystem_context.strip())
        # Soft hint from the strategic plan — nudge toward the planned tool but
        # leave an explicit escape hatch (the strategic planner is itself fallible).
        if expected_tool and expected_tool not in ("", "?", "none"):
            parts.append(
                f"Strategic hint: the plan expects tool `{expected_tool}` for this step. "
                f"Use it unless the step clearly needs a different tool."
            )
        if steps_done:
            history = ["Steps completed so far:"]
            for i, (step, result) in enumerate(zip(steps_done, results, strict=False), 1):
                preview = result[:200] + "..." if len(result) > 200 else result
                history.append(f"  {i}. {step['tool']}({list(step['args'].keys())}) → {preview}")
            parts.append("\n".join(history))
            parts.append(
                "Based on the above, what is the next tool call? "
                'Return {"tool": "none"} if the goal is achieved.'
            )
        else:
            parts.append("Select the right tool and arguments.")
        return "\n".join(parts)

    async def _notify_job_complete(
        self,
        goal: str,
        steps: list[dict],
        results: list[str],
        success: bool,
        *,
        job_id: str | None = None,
        task_id: str | None = None,
        source: str = "user",
        ralph_mode: bool = False,
        total_attempts: int = 0,
        plan_steps: list[dict] | None = None,
    ) -> None:
        """Fire after_job hooks and persist the job record."""
        # Persist output before subsystem hooks (hooks may raise)
        if job_id:
            try:
                self.job_store.save(
                    job_id=job_id,
                    goal=goal,
                    steps=steps,
                    results=results,
                    success=success,
                    task_id=task_id,
                    source=source,
                    ralph_mode=ralph_mode,
                    total_attempts=total_attempts,
                    plan_steps=plan_steps,
                )
            except Exception as e:
                logger.warning("[MotorCortex] JobStore.save failed: %s", e)

        for sub in self._subsystems:
            try:
                import inspect

                sig = inspect.signature(sub.after_job)
                if "router" in sig.parameters:
                    await sub.after_job(goal, steps, results, success, router=self._router)
                else:
                    await sub.after_job(goal, steps, results, success)
            except Exception as e:
                logger.warning("[MotorCortex] Subsystem %s after_job failed: %s", sub.name, e)

    async def _dispatch_cloud(self, args: dict, turn_id: str) -> dict | None:
        """Route to CloudExecutor, applying the confirmation gate for write actions."""
        if not self._cloud or not self._cloud.available:
            output = (
                "[error] Cloud executor not available. Enable --motor with Claude CLI installed."
            )
            result = {"tool": "cloud_action", "args": args, "output": output, "success": False}
            await self._bus.publish_dict("motor.result", result, source=CLUSTER)
            return result

        task = args.get("task", "")
        is_write = bool(args.get("is_write", False))
        context_facts = args.get("context_facts", [])
        description = args.get("description", task)

        # Guardrail 3: confirmation gate — write actions need explicit user sign-off
        if is_write:
            self._cloud.set_pending(
                {"task": task, "context_facts": context_facts, "description": description}
            )
            await self._bus.publish_dict(
                "motor.confirmation_needed",
                {"description": description, "task": task},
                source=CLUSTER,
            )
            logger.info("[MotorCortex] Write action queued for confirmation: %s", description)
            return {
                "tool": "cloud_action",
                "args": args,
                "output": f"CONFIRMATION_NEEDED:{description}",
                "success": False,
                "pending": True,
            }

        # Read action — execute immediately
        result = await self._cloud.execute_read(task, context_facts, turn_id)
        await self._bus.publish_dict("motor.result", result, source=CLUSTER)
        return result

    # ── Tool dispatcher ────────────────────────────────────────────────────────

    async def _dispatch(self, tool: str, args: dict) -> str:
        """Dispatch a single tool call with per-call timeout and transient-error retry.

        Every call is wrapped in asyncio.wait_for(_TOOL_TIMEOUT_S) so a hung
        filesystem, slow network mount, or unresponsive API can never freeze the
        whole job. On a [error] result (transient — not [blocked], not [error] Unknown
        tool) the call is retried up to _TOOL_RETRIES times. [blocked] responses are
        safety-sandbox decisions and are never retried.
        """
        for attempt in range(_TOOL_RETRIES + 1):
            try:
                output = await asyncio.wait_for(
                    self._dispatch_once(tool, args),
                    timeout=_TOOL_TIMEOUT_S,
                )
            except TimeoutError:
                msg = f"[error] {tool} timed out after {_TOOL_TIMEOUT_S:.0f}s"
                logger.warning(
                    "[MotorCortex] %s (attempt %d/%d)", msg, attempt + 1, _TOOL_RETRIES + 1
                )
                output = msg
            except Exception as e:
                msg = f"[error] {tool} failed: {e}"
                logger.error("[MotorCortex] Tool %s raised on attempt %d: %s", tool, attempt + 1, e)
                output = msg

            # Don't retry safety blocks, unknown-tool errors, or success
            is_transient_error = (
                output.startswith("[error]")
                and not output.startswith("[error] Unknown tool")
                and not output.startswith("[error] Command not found")
            )
            if not is_transient_error or attempt >= _TOOL_RETRIES:
                return output

            logger.info(
                "[MotorCortex] Tool %s transient error — retrying (%d/%d): %s",
                tool,
                attempt + 1,
                _TOOL_RETRIES,
                output[:120],
            )

        return output  # unreachable; satisfies type checker

    async def _dispatch_once(self, tool: str, args: dict) -> str:
        """Single (non-retrying) tool dispatch. Called by _dispatch."""
        try:
            if tool == "read_file":
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._dispatcher._read_file, args.get("path", "")
                )
            elif tool == "write_file":
                return await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._dispatcher._write_file,
                    args.get("path", ""),
                    args.get("content", ""),
                )
            elif tool == "append_file":
                return await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._dispatcher._append_file,
                    args.get("path", ""),
                    args.get("content", ""),
                )
            elif tool == "list_files":
                return await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._dispatcher._list_files,
                    args.get("path", "."),
                    args.get("pattern", "*"),
                    args.get("recursive", False),
                )
            elif tool == "run_command":
                return await self._dispatcher._run_command(args.get("cmd", ""), args.get("cwd", ""))
            elif tool == "search_files":
                return await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._dispatcher._search_files,
                    args.get("path", "."),
                    args.get("query", ""),
                    args.get("file_pattern", "*"),
                )
            elif tool == "fetch_url":
                return await self._dispatcher._fetch_url(
                    args.get("url", ""), int(args.get("max_chars", 8000))
                )
            elif tool == "query_langfuse":
                return await self._dispatcher._query_langfuse(
                    args.get("operation", ""),
                    int(args.get("limit", 10)),
                    args.get("trace_id", ""),
                    args.get("score_name", ""),
                    args.get("session_id", ""),
                )
            elif tool == "set_mood":
                return await self._set_mood(args.get("emotion", ""))
            else:
                return f"[error] Unknown tool: {tool}"
        except Exception as e:
            logger.error("[MotorCortex] Tool %s failed: %s", tool, e)
            return f"[error] {tool} failed: {e}"

    # ── Deliberate mood control (audio + visual only) ─────────────────────────

    async def _set_mood(self, emotion: str) -> str:
        """Signal a deliberate emotional performance for this turn.

        PURELY cosmetic/audio — does NOT write to neuromod or hormonal channels
        and does not affect any cognitive system.  Two effects only:
          1. Publishes meta.deliberate_emotion so PNS uses the matching
             ElevenLabs audio tag for this turn's TTS.
          2. Emits an emotion event to the UI badge (dashed border = deliberate).

        For sub-turn (per-sentence) emotion, use [mood:X]...[/mood] markup
        directly in the response text instead of this tool.
        """
        from brain.settings import settings as _s

        if not _s.get("emotional_expression_enabled", 1):
            return "[blocked] Emotional expression is disabled in settings."

        from brain.emotion_presets import EMOTION_PRESETS, VALID_EMOTIONS

        if emotion == "auto":
            await self._bus.publish_dict(
                "meta.deliberate_emotion", {"emotion": None}, source=CLUSTER
            )
            from brain.ui.emitter import emitter

            await emitter.emit_event({"type": "emotion_lock_cleared"})
            return "Deliberate mood cleared — voice will follow reactive emotional state."

        if emotion not in EMOTION_PRESETS:
            return f"[error] Unknown emotion '{emotion}'. Available: {', '.join(VALID_EMOTIONS)}"

        desc = EMOTION_PRESETS[emotion]["desc"]
        await self._bus.publish_dict(
            "meta.deliberate_emotion", {"emotion": emotion}, source=CLUSTER
        )
        # Also publish on the mood_expression topic so session_turn can
        # collect all deliberate expressions (tool + inline) for the trace.
        await self._bus.publish_dict(
            "meta.mood_expression",
            {"emotion": emotion, "source": "tool"},
            source=CLUSTER,
        )

        from brain.ui.emitter import emitter

        await emitter.emit_event(
            {
                "type": "emotion",
                "emotion": emotion,
                "deliberate": True,
            }
        )

        # Record directly on the Langfuse span for this turn (if tracing active)
        if self._obs:
            turn_id = getattr(self, "_current_turn_id", "")
            with contextlib.suppress(Exception):
                self._obs.record_deliberate_emotion(turn_id, emotion, "tool")

        logger.info("[MotorCortex] set_mood: %s — %s", emotion, desc)
        return f"Mood set to '{emotion}' for this turn ({desc})."

    # ── Dispatcher delegation (preserve public API for callers and tests) ────────

    def _validate_path(self, path: str) -> tuple[bool, str]:
        return self._dispatcher._validate_path(path)

    def _validate_command(self, cmd: str) -> tuple[bool, str]:
        return self._dispatcher._validate_command(cmd)

    def _read_file(self, path: str) -> str:
        return self._dispatcher._read_file(path)

    def _write_file(self, path: str, content: str) -> str:
        return self._dispatcher._write_file(path, content)

    def _append_file(self, path: str, content: str) -> str:
        return self._dispatcher._append_file(path, content)

    def _list_files(self, path: str, pattern: str = "*", recursive: bool = False) -> str:
        return self._dispatcher._list_files(path, pattern, recursive)

    async def _run_command(self, cmd: str, cwd: str = "") -> str:
        return await self._dispatcher._run_command(cmd, cwd)

    def _search_files(self, path: str, query: str, file_pattern: str = "*") -> str:
        return self._dispatcher._search_files(path, query, file_pattern)

    @property
    def allowed_paths(self) -> list[str]:
        return self._dispatcher.allowed_paths

    def add_allowed_path(self, path: str) -> None:
        self._dispatcher.add_allowed_path(path)

    # ── Chemistry helpers ──────────────────────────────────────────────────────

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

    def _effective_budget(self, chem: dict[str, float]) -> int:
        """Tool-call budget per turn, modulated by DA (pursuit) and CORT (stress).
        Base is 3; bounded to [1, 5]."""
        base = 3
        if not chem:
            return base
        da = float(chem.get("DA", 0.5))
        cort = float(chem.get("CORT", 0.5))
        shift = (da - 0.5) * 2.0 - (cort - 0.5) * 2.0
        return max(1, min(5, base + int(round(shift))))

    def _effective_job_budget(self, chem: dict[str, float]) -> int:
        """Step budget for background jobs — higher base than reactive turns.
        DA raises it (motivated pursuit); CORT lowers it (stress-induced caution).
        Base is 12; bounded to [6, 20]."""
        base = 12
        if not chem:
            return base
        da = float(chem.get("DA", 0.5))
        cort = float(chem.get("CORT", 0.5))
        shift = (da - 0.5) * 6.0 - (cort - 0.5) * 6.0
        return max(6, min(20, base + int(round(shift))))

    def _chem_description(self, chem: dict[str, float]) -> str:
        """Short human-readable description of the brain's current chemical state,
        passed to the strategic planner so it can adjust plan complexity."""
        if not chem:
            return "balanced"
        da = float(chem.get("DA", 0.5))
        cort = float(chem.get("CORT", 0.5))
        gaba = float(chem.get("GABA", 0.5))
        parts: list[str] = []
        if cort > 0.65:
            parts.append("stressed — prefer fewer, safer steps")
        elif cort < 0.35:
            parts.append("relaxed — can take more thorough steps")
        if da > 0.65:
            parts.append("motivated — be thorough and ambitious")
        elif da < 0.35:
            parts.append("low drive — keep plan minimal")
        if gaba > 0.65:
            parts.append("calm — methodical approach preferred")
        return "; ".join(parts) if parts else "balanced"

    async def _dispatch_lobe(self, tool: str, args: dict, turn_id: str) -> str:
        """Route a lobe tool call through the LobeBridge."""
        if not self._lobe_bridge:
            return f"[error] Lobe bridge not configured — {tool} unavailable"
        if tool == "recall_memory":
            return await self._lobe_bridge.invoke(
                "recall_memory",
                topic=args.get("topic", ""),
                entities=args.get("entities") or [],
                turn_id=turn_id,
            )
        if tool == "analyze_image":
            return await self._lobe_bridge.invoke(
                "analyze_image",
                path=args.get("path", ""),
                question=args.get("question", ""),
                turn_id=turn_id,
            )
        return f"[error] Unknown lobe tool: {tool}"

    def _fire_outcome_switches(self, output: str, tool: str, chem: dict[str, float]) -> None:
        """Fire result_publisher / fallback_reporter / safety_inhibitor based
        on the outcome of a tool dispatch. Pure telemetry — no behavioural
        side effect beyond firing-path / decisions-log entries. The safety
        inhibitor's min_threshold=0.40 floor guarantees chemistry cannot
        silence its firing here."""
        if output.startswith("[blocked]"):
            self._safety_inhibitor.fire(
                1.0, "sandbox_block", {"tool": tool, "preview": output[:80]}, snapshot=chem
            )
        elif output.startswith("[error]"):
            self._fallback_reporter.fire(
                0.8, "tool_error", {"tool": tool, "preview": output[:80]}, snapshot=chem
            )
        else:
            self._result_publisher.fire(0.7, "success", {"tool": tool}, snapshot=chem)
