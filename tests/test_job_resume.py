"""
Tests for job resume: JobStore.get_resumable + execute_internal_job resume wiring.

A job that was interrupted mid-flight (done=False, some stories completed) should
be picked up at the next unfinished story instead of restarting from scratch.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── JobStore.get_resumable ──────────────────────────────────────────────────────

class TestGetResumable:
    def _store(self, tmp_path, monkeypatch):
        import brain.clusters.job_store as js
        monkeypatch.setattr(js, "JOBS_DIR", tmp_path)
        return js.JobStore()

    def test_fresh_job_not_resumable(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        assert store.get_resumable("job_never_ran") is None

    def test_completed_job_not_resumable(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        store.save("job_done", "goal", [{"tool": "read_file"}], ["x"], True,
                   done=True, plan_steps=[{"id": "US-001"}], stories_completed=1)
        assert store.get_resumable("job_done") is None

    def test_interrupted_job_is_resumable(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        store.save("job_mid", "goal",
                   steps=[{"tool": "read_file", "args": {}, "reason": "r"}],
                   results=["content"], success=True, done=False,
                   plan_steps=[{"id": "US-001"}, {"id": "US-002"}],
                   stories_completed=1, productive_steps=1)
        rec = store.get_resumable("job_mid")
        assert rec is not None
        assert rec["stories_completed"] == 1
        assert len(rec["plan_steps"]) == 2

    def test_interrupted_but_all_stories_done_not_resumable(self, tmp_path, monkeypatch):
        """done=False but stories_completed == len(plan) → nothing left to resume."""
        store = self._store(tmp_path, monkeypatch)
        store.save("job_edge", "goal", [{"tool": "read_file"}], ["x"], True,
                   done=False, plan_steps=[{"id": "US-001"}], stories_completed=1)
        assert store.get_resumable("job_edge") is None

    def test_interrupted_no_plan_not_resumable(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        store.save("job_noplan", "goal", [], [], False,
                   done=False, plan_steps=[], stories_completed=0)
        assert store.get_resumable("job_noplan") is None


# ── execute_internal_job resume wiring ──────────────────────────────────────────

class TestExecuteJobResume:
    def _make_motor(self, tmp_path, router):
        from brain.bus import Bus
        from brain.clusters.motor_cortex import MotorCortexCluster
        return MotorCortexCluster(Bus(), router, allowed_paths=[str(tmp_path)])

    async def test_resume_skips_completed_stories(self, tmp_path):
        """A resumable record with story 1 done → only story 2 executes, and the
        strategic planner is NOT called (plan comes from the saved record)."""
        f = tmp_path / "f.txt"
        f.write_text("hello")

        # Router: since strategic planning is skipped on resume, the FIRST call is
        # the tactical planner for story 2. We assert the strategic plan is never
        # requested by making every call return a tactical read_file.
        calls = {"n": 0}

        class ResumeRouter:
            async def call(self, model_key, system_prompt, messages, **kwargs):
                calls["n"] += 1
                return json.dumps({"tool": "read_file", "args": {"path": str(f)}, "reason": "r"})

            async def embed(self, text):
                return [0.0] * 768

            async def warmup_local(self, *a, **k):
                return True

            def enter_background_mode(self) -> None: pass
            def exit_background_mode(self) -> None: pass

        motor = self._make_motor(tmp_path, ResumeRouter())

        # Craft an interrupted prior run: 2-story plan, story 1 already done.
        prior = {
            "job_id": "job_t_resume", "done": False,
            "plan_steps": [
                {"id": "US-001", "description": "read pass 1",
                 "expected_tool": "read_file", "acceptance_criteria": []},
                {"id": "US-002", "description": "read pass 2",
                 "expected_tool": "read_file", "acceptance_criteria": []},
            ],
            "stories_completed": 1,
            "steps": [{"tool": "read_file", "args": {"path": str(f)}, "reason": "done earlier"}],
            "results": ["hello"],
            "productive_steps": 1,
            "unverified_stories": [],
            "success_criteria": "both read", "complexity": "low",
        }
        motor.job_store.get_resumable = MagicMock(return_value=prior)

        mock_emitter = MagicMock()
        mock_emitter.emit_event = AsyncMock()
        with patch("brain.ui.emitter.emitter", mock_emitter):
            result = await motor.execute_internal_job("read it twice", "t_resume")

        # The restored step (story 1) plus the newly-run story 2 step are both present.
        assert result["steps_taken_count"] == 2
        assert result["steps"][0]["reason"] == "done earlier"   # restored, not re-run
        assert result["success"] is True
        assert result["productive_steps"] >= 2
        # A task_resumed event was emitted.
        events = [c.args[0].get("type") for c in mock_emitter.emit_event.call_args_list]
        assert "task_resumed" in events

    async def test_no_resume_runs_strategic_plan(self, tmp_path):
        """Without a resumable record, the strategic planner IS called (normal path)."""
        f = tmp_path / "f.txt"
        f.write_text("hi")
        responses = [
            json.dumps({"stories": [{"id": "US-001", "description": "read it",
                                     "expected_tool": "read_file", "acceptance_criteria": []}],
                        "success_criteria": "read", "complexity": "low"}),
            json.dumps({"tool": "read_file", "args": {"path": str(f)}, "reason": "r"}),
        ]
        seen = {"n": 0}

        class NormalRouter:
            async def call(self, model_key, system_prompt, messages, **kwargs):
                i = seen["n"]; seen["n"] += 1
                return responses[i] if i < len(responses) else json.dumps(
                    {"tool": "none", "args": {}, "reason": "done"})

            async def embed(self, text):
                return [0.0] * 768

            async def warmup_local(self, *a, **k):
                return True

            def enter_background_mode(self) -> None: pass
            def exit_background_mode(self) -> None: pass

        motor = self._make_motor(tmp_path, NormalRouter())
        # No resumable record
        motor.job_store.get_resumable = MagicMock(return_value=None)

        mock_emitter = MagicMock()
        mock_emitter.emit_event = AsyncMock()
        with patch("brain.ui.emitter.emitter", mock_emitter):
            result = await motor.execute_internal_job("read f.txt", "t_fresh")

        assert result["success"] is True
        events = [c.args[0].get("type") for c in mock_emitter.emit_event.call_args_list]
        assert "task_resumed" not in events
        assert "task_start" in events   # normal planning path emitted task_start

    async def test_warmup_called_before_planning(self, tmp_path):
        """The warmup step fires once per job before planning."""
        f = tmp_path / "f.txt"
        f.write_text("hi")

        warmups = {"n": 0}

        class R:
            async def call(self, model_key, system_prompt, messages, **kwargs):
                return json.dumps({"tool": "none", "args": {}, "reason": "done"})

            async def embed(self, text):
                return [0.0] * 768

            async def warmup_local(self, *a, **k):
                warmups["n"] += 1
                return True

            def enter_background_mode(self) -> None: pass
            def exit_background_mode(self) -> None: pass

        motor = self._make_motor(tmp_path, R())
        motor.job_store.get_resumable = MagicMock(return_value=None)

        mock_emitter = MagicMock()
        mock_emitter.emit_event = AsyncMock()
        with patch("brain.ui.emitter.emitter", mock_emitter):
            await motor.execute_internal_job("do nothing", "t_warm")

        assert warmups["n"] == 1
        events = [c.args[0].get("type") for c in mock_emitter.emit_event.call_args_list]
        assert "task_warming_up" in events

    async def test_warmup_failure_is_nonfatal(self, tmp_path):
        """If warmup raises, the job still proceeds."""
        f = tmp_path / "f.txt"
        f.write_text("hi")

        class R:
            async def call(self, model_key, system_prompt, messages, **kwargs):
                return json.dumps({"tool": "read_file", "args": {"path": str(f)}, "reason": "r"})

            async def embed(self, text):
                return [0.0] * 768

            async def warmup_local(self, *a, **k):
                raise RuntimeError("ollama unreachable")

            def enter_background_mode(self) -> None: pass
            def exit_background_mode(self) -> None: pass

        motor = self._make_motor(tmp_path, R())
        motor.job_store.get_resumable = MagicMock(return_value=None)

        mock_emitter = MagicMock()
        mock_emitter.emit_event = AsyncMock()
        with patch("brain.ui.emitter.emitter", mock_emitter):
            result = await motor.execute_internal_job("read f", "t_warmfail")

        assert "job_id" in result   # did not crash
