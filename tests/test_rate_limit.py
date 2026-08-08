"""
Unit tests for the fixed-window limiter and its negative cache.

Driven with an injected clock (the AudioQuota pattern) so window rollover is exact
rather than slept for.
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
    monkeypatch.setenv("BRAIN_RL_KEY_PER_MIN", "3")
    return rl.RateLimiter(now_fn=clock)


def test_allows_up_to_the_limit(limiter):
    assert all(limiter.check("key", "k") is None for _ in range(3))


def test_refuses_past_the_limit_with_retry_after(limiter):
    for _ in range(3):
        limiter.check("key", "k")
    retry = limiter.check("key", "k")
    assert retry is not None and 0 < retry <= rl.WINDOW_S


def test_window_rolls(limiter, clock):
    for _ in range(4):
        limiter.check("key", "k")
    assert limiter.check("key", "k") is not None
    clock.advance(rl.WINDOW_S + 0.1)
    assert limiter.check("key", "k") is None


def test_keys_are_independent(limiter):
    for _ in range(4):
        limiter.check("key", "a")
    assert limiter.check("key", "a") is not None
    assert limiter.check("key", "b") is None


def test_buckets_are_independent(limiter):
    for _ in range(4):
        limiter.check("key", "k")
    assert limiter.check("key", "k") is not None
    assert limiter.check("auth_fail", "k") is None


def test_kill_switch_disables_enforcement(limiter, monkeypatch):
    monkeypatch.setenv("BRAIN_RATE_LIMIT", "0")
    assert all(limiter.check("key", "k") is None for _ in range(50))


def test_zero_limit_means_unlimited(clock, monkeypatch):
    monkeypatch.setenv("BRAIN_RL_KEY_PER_MIN", "0")
    lim = rl.RateLimiter(now_fn=clock)
    assert all(lim.check("key", "k") is None for _ in range(50))


def test_remaining_reports_headroom(limiter):
    assert limiter.remaining("key", "k") == (3, 3)
    limiter.check("key", "k")
    assert limiter.remaining("key", "k") == (3, 2)


def test_key_space_flood_does_not_grow_without_bound(limiter, monkeypatch):
    """An attacker rotating tokens must not be able to grow the gateway's memory."""
    monkeypatch.setattr(rl, "_MAX_KEYS", 100)
    for i in range(500):
        limiter.check("key", f"k{i}")
    assert len(limiter._hits) <= 100


# ── negative cache ──────────────────────────────────────────────────────────
# The limiter bounds the RATE of database lookups; this removes them entirely for a
# token already known to be bad, which is what an invalid-key flood actually is.


def test_miss_is_remembered_then_expires(limiter, clock):
    limiter.note_miss("tok")
    assert limiter.is_known_miss("tok")
    clock.advance(rl.MISS_TTL_S + 0.1)
    assert not limiter.is_known_miss("tok")


def test_unknown_token_is_not_a_miss(limiter):
    assert not limiter.is_known_miss("never-seen")


def test_forget_miss_clears_it(limiter):
    limiter.note_miss("tok")
    limiter.forget_miss("tok")
    assert not limiter.is_known_miss("tok")


# ── key derivation ──────────────────────────────────────────────────────────


def test_token_key_never_contains_the_token():
    tok = "sk_super_secret_value"
    assert tok not in rl.token_key(f"Bearer {tok}")


def test_token_key_is_stable_and_distinct():
    assert rl.token_key("Bearer a") == rl.token_key("Bearer a")
    assert rl.token_key("Bearer a") != rl.token_key("Bearer b")


def test_client_ip_ignores_forwarded_header_off_railway(monkeypatch):
    """Off Railway there is no trusted edge, so a client-supplied header would let an
    attacker rotate its own limit key at will."""
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    headers = {"x-forwarded-for": "1.2.3.4"}
    assert rl.client_ip(headers, "9.9.9.9") == "9.9.9.9"


def test_client_ip_trusts_the_edge_on_railway(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}
    assert rl.client_ip(headers, "10.0.0.1") == "1.2.3.4"
