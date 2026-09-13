"""Small fixes from the 2026-09-13 audit: the webhook gate fails closed after
repeated probe errors, the RunPod watchdog is importable, and the per-customer
chemistry bound only evicts the persona it was asked about."""

from __future__ import annotations

import importlib
import logging

import pytest

import brain.gateway.webhook_delivery as wd


class _Empty:
    def table(self, n):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": []})()


class _Boom:
    def table(self, n):
        raise RuntimeError("db down")


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    monkeypatch.setattr(wd, "_gate_probe_failures", 0)


def test_gate_fails_open_for_a_few_errors_then_closed(caplog):
    caplog.set_level(logging.WARNING, logger="brain.gateway.webhook_delivery")
    for _ in range(wd._GATE_FAIL_CLOSED_AFTER - 1):
        assert wd.any_active_webhook(_Boom()) is True
    assert not caplog.records
    assert wd.any_active_webhook(_Boom()) is False  # 5th consecutive error: closed
    assert len(caplog.records) == 1 and "failing CLOSED" in caplog.records[0].getMessage()
    assert wd.any_active_webhook(_Boom()) is False  # stays closed, logged once
    assert len(caplog.records) == 1


def test_gate_reopens_on_the_next_successful_probe():
    for _ in range(wd._GATE_FAIL_CLOSED_AFTER):
        wd.any_active_webhook(_Boom())
    assert wd.any_active_webhook(_Boom()) is False
    assert wd.any_active_webhook(_Empty()) is False  # real answer: no active webhook
    assert wd._gate_probe_failures == 0
    assert wd.any_active_webhook(_Boom()) is True  # counter reset → fail-open again


def test_a_success_between_errors_resets_the_streak():
    for _ in range(wd._GATE_FAIL_CLOSED_AFTER - 1):
        wd.any_active_webhook(_Boom())
    wd.any_active_webhook(_Empty())
    for _ in range(wd._GATE_FAIL_CLOSED_AFTER - 1):
        assert wd.any_active_webhook(_Boom()) is True


def test_runpod_watchdog_imports_without_argv():
    mod = importlib.import_module("brain.runpod_watchdog")
    assert mod.parse_args(["pod-1", "4242", "key", "3600"]) == ("pod-1", 4242, "key", 3600.0)
    assert mod.parse_args(["pod-1", "4242", "key"])[3] == 8 * 3600.0
    with pytest.raises(SystemExit):
        mod.parse_args(["pod-1"])


class _Reg:
    def __init__(self):
        self.evicted: list[int] = []

    def evict_idle(self, cap):
        self.evicted.append(cap)
        return 2


def test_bound_client_pairs_only_touches_the_named_persona():
    from brain.session_setup import _bound_client_pairs

    class _S:
        _persona_chem = {"alpha": _Reg(), "beta": _Reg()}

    s = _S()
    assert _bound_client_pairs(s, "alpha", 64) == 2
    assert s._persona_chem["alpha"].evicted == [64]
    assert s._persona_chem["beta"].evicted == []
    # Display names normalise to the slug; unknown personas and a missing cache are no-ops.
    assert _bound_client_pairs(s, "Beta", 8) == 2
    assert s._persona_chem["beta"].evicted == [8]
    assert _bound_client_pairs(s, "gamma", 8) == 0
    assert _bound_client_pairs(object(), "alpha", 8) == 0
