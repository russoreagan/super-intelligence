"""X-RateLimit-Reset: the epoch second the current window rolls at.

A client that only knows Remaining has to guess when it refills; Reset lets it
pace against the clock. With no live window the next request opens one, so the
answer is a full window from now; inside a window it is the window's start plus
its length, whatever the count.
"""

from __future__ import annotations

import pytest

from brain.api import rate_limit as rl


@pytest.fixture
def clock():
    class _C:
        t = 1000.0

        def __call__(self):
            return self.t

        def advance(self, dt):
            self.t += dt

    return _C()


@pytest.fixture
def limiter(clock, monkeypatch):
    monkeypatch.setenv("BRAIN_RATE_LIMIT", "1")
    monkeypatch.setenv("BRAIN_RL_KEY_PER_MIN", "2")
    return rl.RateLimiter(now_fn=clock)


def test_reset_at_without_a_window_is_a_full_window_from_now(limiter):
    assert limiter.reset_at("key", "k") == int(1000.0 + rl.WINDOW_S)


def test_reset_at_is_the_window_start_plus_length(limiter, clock):
    limiter.check("key", "k")  # window opens at t=1000
    clock.advance(10.0)
    limiter.check("key", "k")
    assert limiter.reset_at("key", "k") == int(1000.0 + rl.WINDOW_S)
    clock.advance(rl.WINDOW_S)  # rolled: the next hit opens a fresh window
    assert limiter.reset_at("key", "k") == int(clock.t + rl.WINDOW_S)


def test_reset_at_matches_retry_after_on_refusal(limiter, clock):
    limiter.check("key", "k")
    limiter.check("key", "k")
    clock.advance(5.0)
    retry = limiter.check("key", "k")
    assert retry is not None
    assert abs((clock.t + retry) - limiter.reset_at("key", "k")) <= 1.0
