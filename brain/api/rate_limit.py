"""
Abuse control for the engine API surface.

The /v1 lane had no throttling of any kind. Two consequences, and the second is the
one that actually hurt:

  • Bearer tokens could be guessed at unlimited rate. Tokens carry 256 bits of
    entropy, so guessing was never the real risk.
  • Every attempt — including every INVALID one — cost an uncached Supabase query,
    and the gateway's cross-org lookup filters on key_hash alone. Until migration
    028 added a matching index that was a sequential scan per request, so an
    unauthenticated flood was a database denial-of-service with no login required.

So the limiter's job is less "stop brute force" and more "stop unauthenticated
traffic from reaching the database at all". That is why it pairs with a negative
cache (see ``note_miss`` / ``is_known_miss``): the limiter bounds the rate, the
cache removes the query entirely for a repeat offender.

Design notes:

  • FIXED windows, not a sliding log. A sliding log's memory is proportional to
    request rate, and an attacker chooses the request rate — the wrong data
    structure to defend with. A burst straddling a boundary can reach 2x the limit;
    that is an acceptable trade for O(1) memory per key.
  • No persistence. This is abuse control, not billing: losing counters on restart
    is fine, and the gateway is a single always-up process (railway.toml) so there
    is nothing to share state with. Contrast brain/api/audio_quota.py, which does
    persist because it meters money.
  • No lock. All mutation happens between awaits on one event loop.
  • Bounded key space. A flood of distinct IPs or tokens must not grow the dict
    without limit, so it is swept and, past a hard ceiling, cleared.

BRAIN_RATE_LIMIT=0 is the kill switch. It defaults ON — a limiter that ships
disabled is not a limiter.
"""

from __future__ import annotations

import hashlib
import os
import time

# Window and ceilings. Deliberately generous: this is a backstop against abuse and
# runaway clients, not a commercial quota (that is audio_quota / the cloud budget).
WINDOW_S = 60.0

_DEFAULTS = {
    # Failed auth per client IP. Low, because a well-behaved client never fails.
    "auth_fail": 20,
    # Successful authenticated requests per key.
    "key": 300,
    # WebSocket connection attempts per key (connections, not frames).
    "ws": 30,
}

# Past this many tracked keys we stop trusting the sweep and reset. Reached only
# under a deliberate key-space flood, where losing counters is the correct trade
# against unbounded growth in the gateway process.
_MAX_KEYS = 50_000

# How long a token that resolved to nothing stays known-bad, in seconds.
MISS_TTL_S = 60.0


def enabled() -> bool:
    return (os.environ.get("BRAIN_RATE_LIMIT", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _limit(bucket: str) -> int:
    env = os.environ.get(f"BRAIN_RL_{bucket.upper()}_PER_MIN")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return _DEFAULTS.get(bucket, 0)


def token_key(authorization: str | None) -> str:
    """A stable, non-reversible handle for a bearer token.

    Hashed so a counter dict, a log line or a stack trace can never leak a live
    credential."""
    raw = (authorization or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def client_ip(headers, fallback: str | None = None) -> str:
    """The caller's IP. On Railway the edge is the only writer of x-forwarded-for,
    so its first hop is trustworthy there; elsewhere we use the socket peer, because
    a client-supplied header would let an attacker rotate its own limit key."""
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        fwd = headers.get("x-forwarded-for") or ""
        if fwd:
            return fwd.split(",")[0].strip()
    return fallback or "unknown"


class RateLimiter:
    """Fixed-window counters keyed by an opaque string."""

    def __init__(self, *, now_fn=time.time) -> None:
        self._now = now_fn
        # key -> [window_start, count]
        self._hits: dict[str, list[float]] = {}
        # token hash -> expiry timestamp
        self._misses: dict[str, float] = {}

    # ── counters ──────────────────────────────────────────────────────────────
    def check(self, bucket: str, key: str) -> float | None:
        """Record a hit. Returns None when allowed, else seconds until the window
        rolls (the Retry-After value).

        Counting and checking are one operation on purpose: a check that does not
        count is a race, and every caller here wants both."""
        if not enabled():
            return None
        limit = _limit(bucket)
        if limit <= 0:
            return None
        now = self._now()
        self._maybe_sweep(now)
        slot = self._hits.get(f"{bucket}:{key}")
        if slot is None or now - slot[0] >= WINDOW_S:
            self._hits[f"{bucket}:{key}"] = [now, 1]
            return None
        slot[1] += 1
        if slot[1] > limit:
            return max(1.0, round(WINDOW_S - (now - slot[0]), 1))
        return None

    def remaining(self, bucket: str, key: str) -> tuple[int, int]:
        """(limit, remaining) for response headers, so a client can self-pace."""
        limit = _limit(bucket)
        slot = self._hits.get(f"{bucket}:{key}")
        if limit <= 0:
            return (0, 0)
        if slot is None or self._now() - slot[0] >= WINDOW_S:
            return (limit, limit)
        return (limit, max(0, limit - int(slot[1])))

    # ── negative cache ────────────────────────────────────────────────────────
    def note_miss(self, key: str) -> None:
        """Remember that this token resolved to nothing."""
        self._misses[key] = self._now() + MISS_TTL_S

    def is_known_miss(self, key: str) -> bool:
        """True if this token was recently rejected, so the caller can 401 without
        touching the database. This is what actually keeps an invalid-key flood off
        Supabase — the limiter alone still pays one query per allowed attempt."""
        exp = self._misses.get(key)
        if exp is None:
            return False
        if self._now() >= exp:
            self._misses.pop(key, None)
            return False
        return True

    def forget_miss(self, key: str) -> None:
        self._misses.pop(key, None)

    # ── housekeeping ──────────────────────────────────────────────────────────
    def _maybe_sweep(self, now: float) -> None:
        if len(self._hits) + len(self._misses) < _MAX_KEYS:
            return
        self._hits = {k: v for k, v in self._hits.items() if now - v[0] < WINDOW_S}
        self._misses = {k: v for k, v in self._misses.items() if v > now}
        if len(self._hits) + len(self._misses) >= _MAX_KEYS:
            # Still saturated after a sweep: an active flood of distinct keys.
            # Drop everything rather than grow without bound.
            self._hits.clear()
            self._misses.clear()


# Process-wide instance. The gateway is one process that owns its tenants, so a
# module singleton is the whole story; tests construct their own with a fake clock.
limiter = RateLimiter()
