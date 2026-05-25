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
import shlex
from pathlib import Path

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.clusters.motor_subsystem import MotorSubsystem
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.utils import safe_json_parse

logger = logging.getLogger(__name__)

CLUSTER = "motor_cortex"

_DEFAULT_COMMANDS = {
    "ls", "find", "grep", "cat", "head", "tail", "wc",
    "npm", "npx", "node", "python", "python3", "uv",
    "git", "curl", "echo", "mkdir", "cp", "mv", "rm",
    "sed", "awk", "sort", "uniq", "diff",
}

_PLANNER_SYSTEM_BASE = """You are the motor cortex of a biologically-inspired AI brain.
Your job: given a user request that requires an action, choose the right tool and exact arguments.

Available tools:
  read_file(path)                           — read a file's contents
  write_file(path, content)                 — write (overwrite) a file
  append_file(path, content)               — append content to a file
  list_files(path, pattern, recursive)      — list files; pattern is a glob (e.g. "*.ts"); recursive is bool
  run_command(cmd, cwd)                     — run a shell command; cwd is the working directory path
  search_files(path, query, file_pattern)   — search for text within files recursively
  cloud_action(task, is_write, context_facts, description)
      — use Claude with cloud connectors for anything needing external services:
        email, calendar, messages, web search, documents, music tools, etc.
        task: precise English instruction for Claude to execute
        is_write: true if the action sends, creates, modifies, or deletes anything; false for read/search
        context_facts: list of specific facts Claude needs (e.g. ["recipient is John Smith"])
                       — NEVER include memory dumps or personal history, only operational facts
        description: one short sentence describing the action for user confirmation
        NOTE: if the result contains a file path (e.g. second_brain/research/…), follow up
        with read_file on that path to retrieve the full findings.
  recall_memory(topic, entities)            — search episodic memory for context about a topic;
                                              entities: optional list of names/topics to narrow the search
  analyze_image(path, question)             — analyze an image using visual processing;
                                              path: absolute file path; question: what to look for

{cloud_connector_hint}
{lobe_hint}

Return JSON with exactly this shape:
{{
  "tool": "read_file"|"write_file"|"append_file"|"list_files"|"run_command"|"search_files"|"cloud_action"|"recall_memory"|"analyze_image"|"none",
  "args": {{ ...tool-specific args as above... }},
  "reason": "one sentence explaining why"
}}

If the request is conversational and needs no tool, return {{"tool": "none", "args": {{}}, "reason": "..."}}.
If you genuinely need information from the user to proceed and cannot reasonably guess, return
{{"tool": "ask_user", "args": {{"question": "..."}}, "reason": "..."}} — use sparingly; only when blocked.
Return ONLY the JSON object. No explanation."""


# Strategic-mode prompt: produces an upfront plan for an internal directive.
# Used only on the first planner call of an internal job; subsequent calls use the
# tactical (per-step) prompt above with prior steps + results in context.
_STRATEGIC_SYSTEM = """You are the motor cortex planning a multi-step internal task.

Given the overall goal, decompose it into a concrete plan of 2-8 steps. Each step is one
discrete action a tool can perform. Steps should build on each other (later steps use
earlier outputs).

Return STRICT JSON, nothing else:
{
  "steps": [
    {"description": "<imperative, concrete action>", "expected_tool": "list_files|read_file|search_files|run_command|cloud_action"},
    ...
  ],
  "success_criteria": "<one sentence: what counts as done>",
  "complexity": "low|medium|high"
}

Plan for what you genuinely need to do — don't pad. If two steps could collapse, collapse them.
If the goal is trivial (one tool call), return a single-step plan."""


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
        self._lobe_bridge = None   # set post-init via set_lobe_bridge()
        self._obs = None           # set post-init via set_observability()
        self._subsystems: list[MotorSubsystem] = []

        # Resolve and normalise allowed path roots at startup
        self._allowed_paths: list[str] = []
        for p in (allowed_paths or []):
            try:
                self._allowed_paths.append(str(Path(p).resolve()))
            except Exception:
                logger.warning("[MotorCortex] Ignoring invalid allowed path: %s", p)

        self._allowed_commands: set[str] = allowed_commands or set(_DEFAULT_COMMANDS)

        # Build planner prompt dynamically: include cloud connector hint if available
        if cloud_executor and cloud_executor.available:
            self._cloud_hint = (
                f"Cloud connectors currently enabled: {cloud_executor.connectors_summary()}. "
                "Use cloud_action for any request that involves these services."
            )
        else:
            self._cloud_hint = "No cloud connectors available — use local tools only."

        planner_system = _PLANNER_SYSTEM_BASE.format(
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
                "quality-debugging", "dev-process", "devops-git-advanced-workflows",
                # Unity skills — active when working on Evolution App or Karaoke Hero
                "unity-development", "unity-animation", "unity-physics",
                "unity-shader-graph", "unity-ui-toolkit", "unity-urp",
                "unity-input-system", "unity-vfx-graph",
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

        # Switches (~6 total; 2 inhibitory ≈ 33% — acceptable for small action cluster)
        # Modulator profiles per plan /Users/russ/.claude/plans/and-what-affects-these-memoized-parnas.md.
        # Excitatory
        self._action_gate = SwitchNeuron("action_gate", CLUSTER, polarity="excitatory",
                                          threshold=0.5,
                                          modulators={"DA": -0.10})
        self._tool_selector = SwitchNeuron("tool_selector", CLUSTER, polarity="excitatory",
                                            threshold=0.5,
                                            modulators={"ACh": +0.05})
        self._result_publisher = SwitchNeuron("result_publisher", CLUSTER, polarity="excitatory")
        self._fallback_reporter = SwitchNeuron("fallback_reporter", CLUSTER, polarity="excitatory",
                                                threshold=0.5,
                                                modulators={"NE": -0.10})
        # Inhibitory — safety_inhibitor has a min_threshold floor so chemistry
        # alone cannot disable it (contract enforced by tests/test_motor_cortex.py).
        self._safety_inhibitor = SwitchNeuron("safety_check", CLUSTER, polarity="inhibitory",
                                               threshold=0.5,
                                               modulators={"NE": -0.10, "CORT": -0.10},
                                               min_threshold=0.40)
        # budget_inhibitor fires when budget_pressure (calls / effective_budget)
        # reaches 1.0. The chemistry effect on budget itself is already in
        # _effective_budget, so this switch stays chemistry-neutral to avoid
        # double-modulation. Acts as observational telemetry for "we hit the
        # ceiling."
        self._budget_inhibitor = SwitchNeuron("budget_check", CLUSTER, polarity="inhibitory",
                                               threshold=1.0)

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
            cloud_connector_hint=self._cloud_hint,
            lobe_hint=lobe_hint,
        )
        logger.info("[MotorCortex] Lobe bridge registered: %s", caps)

    def register_subsystem(self, subsystem: MotorSubsystem) -> None:
        self._subsystems.append(subsystem)
        logger.info("[MotorCortex] Registered subsystem: %s", subsystem.name)

    def reset_turn(self, turn_id: str) -> None:
        self._calls_this_turn = 0
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
            self._budget_inhibitor.fire(budget_pressure, "budget_exhausted",
                                         {"calls": self._calls_this_turn, "limit": budget},
                                         snapshot=chem)
            logger.warning("[MotorCortex] Tool call budget exhausted for this turn (limit=%d)", budget)
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
            logger.info("[MotorCortex] Running open-loop (sim=%.3f, uses=%d): %s",
                        best_sim, best_proc.get("use_count", 0), work_goal[:60])
            return await self._execute_open_loop(best_proc, task_goal, turn_id)

        steps_taken: list[dict] = []
        results_log: list[str] = []
        last_result: dict | None = None

        while self._calls_this_turn < budget:
            plan_prompt = self._build_plan_prompt(
                work_goal, features, subsystem_context, steps_taken, results_log
            )
            raw = await self._planner.call([{"role": "user", "content": plan_prompt}])
            plan: dict = safe_json_parse(raw) or {}

            if not plan or plan.get("tool") == "none":
                logger.debug("[MotorCortex] Planner done: %s", plan.get("reason", ""))
                break

            tool = plan.get("tool", "none")
            args = plan.get("args", {})
            reason = plan.get("reason", "")
            logger.info("[MotorCortex] Step %d: %s(%s) — %s",
                        len(steps_taken) + 1, tool, list(args.keys()), reason)

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
                    "tool": tool, "args": args, "reason": reason, "output": output,
                    "success": not output.startswith("[error]") and not output.startswith("[blocked]"),
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
                not r.startswith("[error]") and not r.startswith("[blocked]")
                for r in results_log
            )
            await self._notify_job_complete(task_goal, steps_taken, results_log, success)
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
    async def execute_internal_job(self, goal: str, turn_id: str,
                                    budget: int = 0) -> dict:
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

        # Open a Langfuse trace so all LLM calls inside this job are nested under it.
        # record_llm_call() uses _active_spans[job_id] as the parent — the same
        # mechanism as per-turn tracing, keyed by job_id instead of turn_id.
        if self._obs:
            self._obs.begin_job(job_id, goal=goal, chem=chem)

        # Surface the job immediately so the user can see something is brewing
        # even before the strategic plan finishes (local-code planner can take
        # 10-30s on the 14B model).
        await emitter.emit_event({
            "type": "task_planning",
            "job_id": job_id,
            "goal": goal,
        })

        # 1. Strategic plan — pass chemistry state so the planner can calibrate
        # complexity (e.g. fewer steps when stressed, more thorough when motivated).
        self._strategic_planner.reset_turn(job_id)
        raw_plan = await self._strategic_planner.call(
            [{"role": "user", "content": f"Goal: {goal}\nBrain state: {chem_ctx}"}]
        )
        plan = safe_json_parse(raw_plan) or {}
        steps_planned = plan.get("steps") or [{"description": goal, "expected_tool": "?"}]
        success_criteria = plan.get("success_criteria", "")

        logger.info("[InternalJob] %s — %d steps planned (criteria: %s)",
                    goal[:60], len(steps_planned), success_criteria[:80])
        await emitter.emit_event({
            "type": "task_start",
            "job_id": job_id,
            "goal": goal,
            "steps": [s.get("description", "") for s in steps_planned],
            "success_criteria": success_criteria,
            "complexity": plan.get("complexity", "?"),
        })

        # 2. Step-by-step execution. Each step is a sub-goal handed to the
        # tactical planner with the full plan + prior results as context.
        steps_taken: list[dict] = []
        results_log: list[str] = []
        last_result: dict | None = None
        clarification_question: str | None = None
        success = True

        for idx, step in enumerate(steps_planned):
            if self._calls_this_turn >= effective_budget:
                logger.warning("[InternalJob] Budget exhausted (%d) at step %d/%d",
                                effective_budget, idx + 1, len(steps_planned))
                success = False
                break

            step_desc = step.get("description", "")
            await emitter.emit_event({
                "type": "task_step_start",
                "job_id": job_id,
                "step_index": idx,
                "description": step_desc,
                "expected_tool": step.get("expected_tool", ""),
            })

            # Build the tactical prompt with plan context for goal-coherence
            tactical_features = {"raw_text": step_desc, "topic_summary": step_desc}
            plan_summary = " | ".join(
                f"{i+1}. {s.get('description', '')}" for i, s in enumerate(steps_planned)
            )
            extra_context = f"Overall goal: {goal}\nFull plan: {plan_summary}\nCurrent step ({idx+1}/{len(steps_planned)}): {step_desc}"
            plan_prompt = self._build_plan_prompt(
                step_desc, tactical_features, extra_context, steps_taken, results_log
            )
            raw = await self._planner.call([{"role": "user", "content": plan_prompt}])
            tactical = safe_json_parse(raw) or {}
            tool = tactical.get("tool", "none")
            args = tactical.get("args", {})
            reason = tactical.get("reason", "")

            self._calls_this_turn += 1

            if tool == "none":
                logger.info("[InternalJob] Step %d skipped: %s", idx + 1, reason)
                await emitter.emit_event({
                    "type": "task_step_done",
                    "job_id": job_id, "step_index": idx,
                    "success": True, "skipped": True,
                    "output": reason[:200],
                })
                steps_taken.append({"tool": "none", "args": {}, "reason": reason})
                results_log.append("")
                continue

            if tool == "ask_user":
                clarification_question = args.get("question", "I need more info to continue.")
                logger.info("[InternalJob] Clarification needed at step %d: %s",
                            idx + 1, clarification_question)
                await emitter.emit_event({
                    "type": "task_clarification",
                    "job_id": job_id, "step_index": idx,
                    "question": clarification_question,
                })
                success = False  # paused, not failed; caller decides what to do
                break

            logger.info("[InternalJob] Step %d/%d: %s — %s",
                        idx + 1, len(steps_planned), tool, reason[:80])

            if tool == "cloud_action":
                last_result = await self._dispatch_cloud(args, job_id)
                output = (last_result or {}).get("output", "")
            elif tool in ("recall_memory", "analyze_image"):
                output = await self._dispatch_lobe(tool, args, job_id)
                last_result = {
                    "tool": tool, "args": args, "reason": reason, "output": output,
                    "success": not output.startswith("[error]"),
                }
                await self._bus.publish_dict("motor.result", last_result, source=CLUSTER)
            else:
                output = await self._dispatch(tool, args)
                last_result = {
                    "tool": tool, "args": args, "reason": reason, "output": output,
                    "success": not output.startswith("[error]") and not output.startswith("[blocked]"),
                }
                await self._bus.publish_dict("motor.result", last_result, source=CLUSTER)

            step_success = not output.startswith("[error]") and not output.startswith("[blocked]")
            if not step_success:
                success = False

            steps_taken.append({"tool": tool, "args": args, "reason": reason})
            results_log.append(output[:500] if output else "")
            self._fire_outcome_switches(output, tool, self._chem_snapshot())
            await emitter.emit_event({
                "type": "task_step_done",
                "job_id": job_id, "step_index": idx,
                "tool": tool,
                "success": step_success,
                "output": (output or "")[:300],
            })

        # 3. Completion
        await self._notify_job_complete(goal, steps_taken, results_log, success)
        await emitter.emit_event({
            "type": "task_complete",
            "job_id": job_id,
            "success": success,
            "steps_completed": len(steps_taken),
            "clarification": clarification_question,
        })
        logger.info("[InternalJob] Done: %s (success=%s, %d/%d steps)",
                    goal[:60], success, len(steps_taken), len(steps_planned))
        if self._obs:
            self._obs.end_job(job_id, success=success,
                              steps_completed=len(steps_taken),
                              steps_planned=len(steps_planned))
        return {
            "job_id": job_id, "goal": goal, "success": success,
            "steps_taken_count": len(steps_taken),
            "steps_planned_count": len(steps_planned),
            "steps": steps_taken,            # list of {tool, args, reason}
            "results": results_log,          # parallel list of output strings (each truncated to 500ch)
            "plan_steps": steps_planned,     # original strategic plan (list of {description, expected_tool})
            "clarification": clarification_question,
            "last_output": (last_result or {}).get("output", "") if last_result else "",
        }

    async def _execute_open_loop(self, procedure: dict, goal: str | None,
                                  turn_id: str) -> dict | None:
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
                logger.warning("[MotorCortex] Open-loop budget exhausted at step %d/%d (limit=%d)",
                               i + 1, len(steps), open_loop_budget)
                break

            tool = step.get("tool", "none")
            args = step.get("args", {})
            reason = step.get("reason", "")
            logger.info("[MotorCortex] Open-loop step %d/%d: %s — %s",
                        i + 1, len(steps), tool, reason)

            self._calls_this_turn += 1

            if tool == "cloud_action":
                last_result = await self._dispatch_cloud(args, turn_id)
                output = (last_result or {}).get("output", "")
            else:
                output = await self._dispatch(tool, args)
                last_result = {
                    "tool": tool, "args": args, "reason": reason, "output": output,
                    "success": not output.startswith("[error]") and not output.startswith("[blocked]"),
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
                    logger.warning("[MotorCortex] Prediction error at step %d: "
                                   "expected %s, got %s (source: %s)",
                                   i + 1,
                                   "ok" if prediction.get("expected_success") else "error",
                                   "error" if actual_error else "ok",
                                   "learned" if "sample_count" in prediction else "stored")
                elif not actual_error and not prediction.get("is_empty", False):
                    n = len(output)
                    if n < prediction.get("length_min", 0) or n > prediction.get("length_max", float("inf")):
                        prediction_errors += 1
                        logger.warning("[MotorCortex] Length divergence at step %d: "
                                       "expected %d–%d chars, got %d",
                                       i + 1, prediction["length_min"], prediction["length_max"], n)
            elif actual_error:
                # Fallback: unexpected error with no prior prediction is always a divergence
                prediction_errors += 1
                logger.warning("[MotorCortex] Unexpected error at step %d (no prior prediction)",
                               i + 1)

        success = all(
            not r.startswith("[error]") and not r.startswith("[blocked]")
            for r in results_log
        )

        if prediction_errors > 0:
            logger.warning("[MotorCortex] Open-loop diverged (%d prediction error(s)) — "
                           "marking procedure stale", prediction_errors)
            for sub in self._subsystems:
                if hasattr(sub, "mark_diverged"):
                    with contextlib.suppress(Exception):
                        sub.mark_diverged(proc_id)

        if steps_taken:
            await self._notify_job_complete(goal or "", steps_taken, results_log, success)

        if last_result is None:
            last_result = {}
        last_result["task_goal"] = goal
        last_result["steps_taken"] = len(steps_taken)
        last_result["open_loop"] = True
        last_result["prediction_errors"] = prediction_errors
        return last_result

    def _build_plan_prompt(self, goal: str, features: dict, subsystem_context: str,
                           steps_done: list[dict], results: list[str]) -> str:
        parts = [
            f"Goal: {goal}",
            f"Intent: {features.get('intent', 'task')}",
            f"Entities: {features.get('entities', [])}",
        ]
        if subsystem_context.strip():
            parts.append(subsystem_context.strip())
        if steps_done:
            history = ["Steps completed so far:"]
            for i, (step, result) in enumerate(zip(steps_done, results, strict=False), 1):
                preview = result[:200] + "..." if len(result) > 200 else result
                history.append(f"  {i}. {step['tool']}({list(step['args'].keys())}) → {preview}")
            parts.append("\n".join(history))
            parts.append('Based on the above, what is the next tool call? '
                         'Return {"tool": "none"} if the goal is achieved.')
        else:
            parts.append("Select the right tool and arguments.")
        return "\n".join(parts)

    async def _notify_job_complete(self, goal: str, steps: list[dict],
                                   results: list[str], success: bool) -> None:
        """Fire after_job hooks on all motor subsystems."""
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
            output = "[error] Cloud executor not available. Enable --motor with Claude CLI installed."
            result = {"tool": "cloud_action", "args": args, "output": output, "success": False}
            await self._bus.publish_dict("motor.result", result, source=CLUSTER)
            return result

        task = args.get("task", "")
        is_write = bool(args.get("is_write", False))
        context_facts = args.get("context_facts", [])
        description = args.get("description", task)

        # Guardrail 3: confirmation gate — write actions need explicit user sign-off
        if is_write:
            self._cloud.set_pending({"task": task, "context_facts": context_facts,
                                     "description": description})
            await self._bus.publish_dict(
                "motor.confirmation_needed",
                {"description": description, "task": task},
                source=CLUSTER,
            )
            logger.info("[MotorCortex] Write action queued for confirmation: %s", description)
            return {
                "tool": "cloud_action", "args": args,
                "output": f"CONFIRMATION_NEEDED:{description}",
                "success": False, "pending": True,
            }

        # Read action — execute immediately
        result = await self._cloud.execute_read(task, context_facts, turn_id)
        await self._bus.publish_dict("motor.result", result, source=CLUSTER)
        return result

    # ── Tool dispatcher ────────────────────────────────────────────────────────

    async def _dispatch(self, tool: str, args: dict) -> str:
        try:
            if tool == "read_file":
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._read_file, args.get("path", ""))
            elif tool == "write_file":
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._write_file, args.get("path", ""), args.get("content", ""))
            elif tool == "append_file":
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._append_file, args.get("path", ""), args.get("content", ""))
            elif tool == "list_files":
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._list_files,
                    args.get("path", "."), args.get("pattern", "*"), args.get("recursive", False))
            elif tool == "run_command":
                return await self._run_command(args.get("cmd", ""), args.get("cwd", ""))
            elif tool == "search_files":
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._search_files,
                    args.get("path", "."), args.get("query", ""), args.get("file_pattern", "*"))
            else:
                return f"[error] Unknown tool: {tool}"
        except Exception as e:
            logger.error("[MotorCortex] Tool %s failed: %s", tool, e)
            return f"[error] {tool} failed: {e}"

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

    def _fire_outcome_switches(self, output: str, tool: str,
                                chem: dict[str, float]) -> None:
        """Fire result_publisher / fallback_reporter / safety_inhibitor based
        on the outcome of a tool dispatch. Pure telemetry — no behavioural
        side effect beyond firing-path / decisions-log entries. The safety
        inhibitor's min_threshold=0.40 floor guarantees chemistry cannot
        silence its firing here."""
        if output.startswith("[blocked]"):
            self._safety_inhibitor.fire(1.0, "sandbox_block",
                                         {"tool": tool, "preview": output[:80]},
                                         snapshot=chem)
        elif output.startswith("[error]"):
            self._fallback_reporter.fire(0.8, "tool_error",
                                          {"tool": tool, "preview": output[:80]},
                                          snapshot=chem)
        else:
            self._result_publisher.fire(0.7, "success",
                                         {"tool": tool}, snapshot=chem)

    # ── Path / command safety ──────────────────────────────────────────────────

    def _validate_path(self, path: str) -> tuple[bool, str]:
        """Returns (is_safe, resolved_path_or_error_message)."""
        if not self._allowed_paths:
            return False, "No paths configured. Set BRAIN_MOTOR_PATHS env var."
        if not path:
            return False, "Empty path."
        try:
            resolved = str(Path(path).resolve())
        except Exception as e:
            return False, f"Invalid path '{path}': {e}"
        for allowed in self._allowed_paths:
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return True, resolved
        return False, (f"Path '{path}' (resolved: {resolved}) is outside allowed roots: "
                       f"{self._allowed_paths}")

    def _validate_command(self, cmd: str) -> tuple[bool, str]:
        """Returns (is_safe, error_message_or_empty)."""
        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            return False, f"Invalid command syntax: {e}"
        if not parts:
            return False, "Empty command."
        base = os.path.basename(parts[0])
        if base not in self._allowed_commands:
            return False, (f"Command '{base}' is not in the allowed list. "
                           f"Allowed: {sorted(self._allowed_commands)}")
        return True, ""

    # ── Tool implementations ───────────────────────────────────────────────────

    def _read_file(self, path: str) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            content = Path(resolved).read_text(errors="replace")
            if len(content) > 4000:
                content = content[:4000] + "\n[... truncated at 4000 chars ...]"
            return content
        except FileNotFoundError:
            return f"[error] File not found: {resolved}"
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    def _write_file(self, path: str, content: str) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            Path(resolved).write_text(content)
            return f"Written {len(content)} bytes to {resolved}"
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    def _append_file(self, path: str, content: str) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "a") as f:
                f.write(content)
            return f"Appended {len(content)} bytes to {resolved}"
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    def _list_files(self, path: str, pattern: str = "*", recursive: bool = False) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            p = Path(resolved)
            if not p.is_dir():
                return f"[error] Not a directory: {resolved}"
            matches = list(p.rglob(pattern)) if recursive else list(p.glob(pattern))
            if not matches:
                return "(no files matched)"
            lines = [str(m.relative_to(p)) for m in sorted(matches)[:200]]
            result = "\n".join(lines)
            if len(matches) > 200:
                result += f"\n[... {len(matches) - 200} more files not shown ...]"
            return result
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    async def _run_command(self, cmd: str, cwd: str = "") -> str:
        safe_cmd, err = self._validate_command(cmd)
        if not safe_cmd:
            return f"[blocked] {err}"

        cwd_resolved = None
        if cwd:
            safe_cwd, resolved_cwd = self._validate_path(cwd)
            if not safe_cwd:
                return f"[blocked] cwd: {resolved_cwd}"
            cwd_resolved = resolved_cwd
        elif self._allowed_paths:
            # Default cwd to first allowed path if none given
            cwd_resolved = self._allowed_paths[0]

        try:
            parts = shlex.split(cmd)
            proc = await asyncio.create_subprocess_exec(
                *parts,
                cwd=cwd_resolved,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            output = stdout.decode(errors="replace")
            if len(output) > 3000:
                output = output[:3000] + "\n[... truncated ...]"
            return output or "(command produced no output)"
        except TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            return "[error] Command timed out after 30s."
        except FileNotFoundError:
            return f"[error] Command not found: {shlex.split(cmd)[0]}"
        except Exception as e:
            return f"[error] {e}"

    def _search_files(self, path: str, query: str, file_pattern: str = "*") -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        if not query:
            return "[error] Empty search query."
        try:
            p = Path(resolved)
            matches: list[str] = []
            for fpath in p.rglob(file_pattern):
                if not fpath.is_file():
                    continue
                try:
                    text = fpath.read_text(errors="replace")
                    for i, line in enumerate(text.splitlines(), 1):
                        if query.lower() in line.lower():
                            rel = fpath.relative_to(p)
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(matches) >= 100:
                                break
                except Exception:
                    continue
                if len(matches) >= 100:
                    break
            if not matches:
                return f"(no matches for '{query}' in {resolved})"
            result = "\n".join(matches)
            if len(matches) == 100:
                result += "\n[... search limited to 100 results ...]"
            return result
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    @property
    def allowed_paths(self) -> list[str]:
        return list(self._allowed_paths)

    def add_allowed_path(self, path: str) -> None:
        try:
            resolved = str(Path(path).resolve())
            if resolved not in self._allowed_paths:
                self._allowed_paths.append(resolved)
                logger.info("[MotorCortex] Added allowed path: %s", resolved)
        except Exception as e:
            logger.warning("[MotorCortex] Could not add path %s: %s", path, e)
