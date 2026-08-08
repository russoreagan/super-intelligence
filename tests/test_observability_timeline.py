"""
Tests for ObservabilityLayer.begin_job / end_job (brain/observability/timeline.py).

These tests exercise the tracing interface directly, without Langfuse credentials —
they confirm the no-Langfuse code path is safe and that the signature / field contract
introduced by the planning-quality changes (total_attempts, retries) is enforced.
"""

from __future__ import annotations

from unittest.mock import MagicMock


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
        obs.end_job("job_1", success=True, steps_completed=3, steps_planned=3, total_attempts=3)

    def test_end_job_with_retries_no_crash(self):
        obs = self._obs()
        obs.end_job("job_2", success=True, steps_completed=2, steps_planned=2, total_attempts=5)

    def test_end_job_without_total_attempts_uses_default(self):
        """total_attempts defaults to 0 — backward-compatible with callers that omit it."""
        obs = self._obs()
        obs.end_job("job_3", success=False, steps_completed=1, steps_planned=2)
        # No exception — default is accepted

    def test_begin_then_end_no_span_leak(self):
        """end_job without Langfuse never pollutes _active_spans."""
        obs = self._obs()
        obs.begin_job("job_x", goal="x")
        obs.end_job("job_x", success=True, steps_completed=1, steps_planned=1, total_attempts=1)
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

        obs.end_job("job_a", success=True, steps_completed=3, steps_planned=4, total_attempts=5)

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

        obs.end_job("job_b", success=True, steps_completed=3, steps_planned=3, total_attempts=3)

        metadata = mock_span.update.call_args[1]["metadata"]
        assert metadata["retries"] == 0

    def test_end_job_retries_clamped_at_zero(self):
        """retries is never negative (e.g. if total_attempts < steps_completed somehow)."""
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_c"] = mock_span

        # Pathological: total_attempts=0 (default), steps_completed=2
        obs.end_job("job_c", success=True, steps_completed=2, steps_planned=2, total_attempts=0)

        metadata = mock_span.update.call_args[1]["metadata"]
        assert metadata["retries"] == 0  # max(0, 0-2) = 0

    def test_end_job_pops_span_from_active_spans(self):
        obs = self._obs_with_langfuse()
        obs._active_spans["job_d"] = MagicMock()

        obs.end_job("job_d", success=True, steps_completed=1, steps_planned=1, total_attempts=1)

        assert "job_d" not in obs._active_spans

    def test_end_job_missing_span_no_crash(self):
        """end_job is a no-op when the span was never opened."""
        obs = self._obs_with_langfuse()
        obs.end_job(
            "never_started", success=False, steps_completed=0, steps_planned=1, total_attempts=0
        )

    def test_end_job_span_exception_does_not_propagate(self):
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        mock_span.update.side_effect = RuntimeError("langfuse down")
        obs._active_spans["job_e"] = mock_span

        # Must not raise — logged as debug
        obs.end_job("job_e", success=True, steps_completed=1, steps_planned=1, total_attempts=1)

    def test_output_dict_reflects_success_and_steps_completed(self):
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_f"] = mock_span

        obs.end_job("job_f", success=False, steps_completed=2, steps_planned=5, total_attempts=4)

        output = mock_span.update.call_args[1]["output"]
        assert output["success"] is False
        assert output["steps_completed"] == 2


class TestEndJobOutcomeFields:
    """The terminal JobOutcome (state / reason_code / reason_human) reaches Langfuse.

    Without these, a job that deliberately refused a contentless goal
    (reason_code=no_productive_steps) is indistinguishable in the trace from one
    that crashed — both show only success=False.
    """

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

    def test_reason_code_reaches_output_and_metadata(self):
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_r"] = mock_span

        obs.end_job(
            "job_r",
            success=False,
            steps_completed=1,
            steps_planned=0,
            total_attempts=1,
            state="failed",
            reason_code="no_productive_steps",
            reason_human="The goal had no actionable content.",
            productive_steps=0,
        )

        output = mock_span.update.call_args[1]["output"]
        metadata = mock_span.update.call_args[1]["metadata"]
        # Visible on the trace row itself, not just buried in metadata
        assert output["reason_code"] == "no_productive_steps"
        assert output["state"] == "failed"
        assert metadata["reason_human"] == "The goal had no actionable content."
        assert metadata["productive_steps"] == 0

    def test_outcome_fields_omitted_when_absent(self):
        """Callers that don't pass outcome fields don't get empty keys on the span."""
        obs = self._obs_with_langfuse()
        mock_span = MagicMock()
        obs._active_spans["job_s"] = mock_span

        obs.end_job("job_s", success=True, steps_completed=2, steps_planned=2, total_attempts=2)

        output = mock_span.update.call_args[1]["output"]
        metadata = mock_span.update.call_args[1]["metadata"]
        assert "reason_code" not in output
        assert "state" not in output
        assert "reason_human" not in metadata


class TestPostScoresRangeGuard:
    """Only 0..1 rates are publishable as scores; raw counts are refused."""

    def _obs_with_langfuse(self):
        from brain.observability.timeline import ObservabilityLayer

        obs = ObservabilityLayer.__new__(ObservabilityLayer)
        obs._session_id = "s1"
        obs._langfuse = MagicMock()
        obs._trace_ids = {"turn1": "trace-abc"}
        return obs

    def test_in_range_score_is_submitted(self):
        obs = self._obs_with_langfuse()
        obs._post_scores("turn1", {"gating.efficiency": 0.25})
        obs._langfuse.create_score.assert_called_once()
        assert obs._langfuse.create_score.call_args[1]["value"] == 0.25

    def test_raw_count_is_refused(self):
        """A count like gating_bypassed_count=3 must never become a score."""
        obs = self._obs_with_langfuse()
        obs._post_scores("turn1", {"gating.bypassed_count": 3})
        obs._langfuse.create_score.assert_not_called()

    def test_negative_score_is_refused(self):
        obs = self._obs_with_langfuse()
        obs._post_scores("turn1", {"some.metric": -0.5})
        obs._langfuse.create_score.assert_not_called()

    def test_bounds_are_inclusive(self):
        obs = self._obs_with_langfuse()
        obs._post_scores("turn1", {"a.zero": 0.0, "b.one": 1.0})
        assert obs._langfuse.create_score.call_count == 2

    def test_out_of_range_does_not_block_siblings(self):
        """One bad value must not suppress the good scores posted alongside it."""
        obs = self._obs_with_langfuse()
        obs._post_scores("turn1", {"bad.count": 7, "good.rate": 0.4})
        assert obs._langfuse.create_score.call_count == 1
        assert obs._langfuse.create_score.call_args[1]["name"] == "good.rate"
