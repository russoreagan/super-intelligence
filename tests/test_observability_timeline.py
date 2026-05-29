"""
Tests for ObservabilityLayer.begin_job / end_job (brain/observability/timeline.py).

These tests exercise the tracing interface directly, without Langfuse credentials —
they confirm the no-Langfuse code path is safe and that the signature / field contract
introduced by the planning-quality changes (total_attempts, retries) is enforced.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


class TestBeginEndJobNoLangfuse:
    """begin_job / end_job are safe no-ops when Langfuse is not configured."""

    def _obs(self):
        from brain.observability.timeline import ObservabilityLayer
        return ObservabilityLayer(session_id="test-session")

    def test_begin_job_no_crash_without_langfuse(self):
        obs = self._obs()
        assert obs._langfuse is None  # credentials not set in test env
        obs.begin_job("job_1", goal="do something")  # must not raise

    def test_begin_job_with_chem_no_crash(self):
        obs = self._obs()
        obs.begin_job("job_2", goal="test", chem={"DA": 0.6, "CORT": 0.3, "NE": 0.2})

    def test_end_job_no_crash_without_langfuse(self):
        obs = self._obs()
        obs.end_job("job_1", success=True, steps_completed=3,
                    steps_planned=3, total_attempts=3)

    def test_end_job_with_retries_no_crash(self):
        obs = self._obs()
        obs.end_job("job_2", success=True, steps_completed=2,
                    steps_planned=2, total_attempts=5)

    def test_end_job_without_total_attempts_uses_default(self):
        """total_attempts defaults to 0 — backward-compatible with callers that omit it."""
        obs = self._obs()
        obs.end_job("job_3", success=False, steps_completed=1, steps_planned=2)
        # No exception — default is accepted

    def test_begin_then_end_no_span_leak(self):
        """end_job without Langfuse never pollutes _active_spans."""
        obs = self._obs()
        obs.begin_job("job_x", goal="x")
        obs.end_job("job_x", success=True, steps_completed=1,
                    steps_planned=1, total_attempts=1)
        assert "job_x" not in obs._active_spans


class TestEndJobLangfuseSpanUpdate:
    """end_job writes the right fields onto the Langfuse span (mocked)."""

    def _obs_with_langfuse(self):
        from brain.observability.timeline import ObservabilityLayer
        obs = ObservabilityLayer.__new__(ObservabilityLayer)
        obs._session_id = "s1"
        obs._langfuse = MagicMock()
        obs._active_spans = {}
        obs._active_cluster_spans = {}
        obs._trace_ids = {}
        obs._traces = []
        obs._neuromod_history = __import__("collections").deque()
        obs._eval_logger = None
        return obs

    def test_end_job_updates_span_with_total_attempts(self):
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_a"] = mock_span

        obs.end_job("job_a", success=True, steps_completed=3,
                    steps_planned=4, total_attempts=5)

        mock_span.update.assert_called_once()
        call_kwargs = mock_span.update.call_args[1]
        metadata = call_kwargs["metadata"]
        assert metadata["total_attempts"] == 5
        assert metadata["steps_completed"] == 3
        assert metadata["steps_planned"] == 4
        assert metadata["success"] is True
        # retries = total_attempts - steps_completed
        assert metadata["retries"] == 2
        mock_span.end.assert_called_once()

    def test_end_job_retries_zero_when_no_retries(self):
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_b"] = mock_span

        obs.end_job("job_b", success=True, steps_completed=3,
                    steps_planned=3, total_attempts=3)

        metadata = mock_span.update.call_args[1]["metadata"]
        assert metadata["retries"] == 0

    def test_end_job_retries_clamped_at_zero(self):
        """retries is never negative (e.g. if total_attempts < steps_completed somehow)."""
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_c"] = mock_span

        # Pathological: total_attempts=0 (default), steps_completed=2
        obs.end_job("job_c", success=True, steps_completed=2,
                    steps_planned=2, total_attempts=0)

        metadata = mock_span.update.call_args[1]["metadata"]
        assert metadata["retries"] == 0  # max(0, 0-2) = 0

    def test_end_job_pops_span_from_active_spans(self):
        obs = self._obs_with_langfuse()
        obs._active_spans["job_d"] = MagicMock()

        obs.end_job("job_d", success=True, steps_completed=1,
                    steps_planned=1, total_attempts=1)

        assert "job_d" not in obs._active_spans

    def test_end_job_missing_span_no_crash(self):
        """end_job is a no-op when the span was never opened."""
        obs = self._obs_with_langfuse()
        obs.end_job("never_started", success=False, steps_completed=0,
                    steps_planned=1, total_attempts=0)

    def test_end_job_span_exception_does_not_propagate(self):
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        mock_span.update.side_effect = RuntimeError("langfuse down")
        obs._active_spans["job_e"] = mock_span

        # Must not raise — logged as debug
        obs.end_job("job_e", success=True, steps_completed=1,
                    steps_planned=1, total_attempts=1)

    def test_output_dict_reflects_success_and_steps_completed(self):
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_f"] = mock_span

        obs.end_job("job_f", success=False, steps_completed=2,
                    steps_planned=5, total_attempts=4)

        output = mock_span.update.call_args[1]["output"]
        assert output["success"] is False
        assert output["steps_completed"] == 2
