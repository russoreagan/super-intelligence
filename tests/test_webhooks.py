"""
Signed per-partner webhooks (migration 032).

Covers the four pieces independently: the HMAC signature scheme, the SSRF guard,
delivery/retry, and registration/routing scoping. The old path this replaces sent a
shared secret as a bearer token to one deployment-wide URL — replayable, unsigned, and
a cross-tenant leak.
"""

from __future__ import annotations

import pytest

from brain import net_guard
from brain.api import webhook_sign


# ── signing ─────────────────────────────────────────────────────────────────
def test_sign_verify_roundtrip():
    sig = webhook_sign.sign("whsec_x", b'{"a":1}', 1000)
    assert webhook_sign.verify("whsec_x", b'{"a":1}', sig, now=1000)


def test_a_mutated_body_fails():
    sig = webhook_sign.sign("whsec_x", b'{"a":1}', 1000)
    assert not webhook_sign.verify("whsec_x", b'{"a":2}', sig, now=1000)


def test_a_wrong_secret_fails():
    sig = webhook_sign.sign("whsec_x", b"body", 1000)
    assert not webhook_sign.verify("whsec_y", b"body", sig, now=1000)


def test_a_stale_timestamp_fails():
    sig = webhook_sign.sign("whsec_x", b"body", 1000)
    # 10 minutes later, default tolerance 5 min → replay rejected.
    assert not webhook_sign.verify("whsec_x", b"body", sig, now=1000 + 600)


def test_a_garbage_header_is_false_not_an_error():
    assert not webhook_sign.verify("whsec_x", b"body", "not-a-signature", now=1000)


def test_the_documented_verifier_snippet_agrees():
    """The Python snippet in the guide must actually validate a real signature."""
    import hashlib
    import hmac
    import time

    secret, body = "whsec_x", b'{"event":"job.completed"}'
    header = webhook_sign.sign(secret, body, int(time.time()))

    def verify(secret, body, header, tolerance=300):
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        if abs(time.time() - int(parts["t"])) > tolerance:
            return False
        expected = hmac.new(
            secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(parts["v1"], expected)

    assert verify(secret, body, header)


# ── SSRF guard ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",  # http not allowed by default (hosted = https)
        "https://user:pass@example.com/hook",  # userinfo
        "https://localhost/hook",
        "ftp://example.com",
        "https://nodot",  # bare host
    ],
)
def test_ssrf_rejects_bad_shapes(url):
    with pytest.raises(net_guard.UnsafeUrlError):
        net_guard.validate_url(url)


@pytest.mark.parametrize(
    "addr",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "192.168.1.1", "0.0.0.0"],
)
def test_ssrf_rejects_internal_addresses(addr, monkeypatch):
    # Force resolution to the internal address regardless of the hostname.
    import socket

    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *a, **k: [(fam, None, None, "", (addr, 0))],
    )
    with pytest.raises(net_guard.UnsafeUrlError):
        net_guard.validate_url("https://evil.example.com/hook")


def test_ssrf_rejects_when_any_record_is_internal(monkeypatch):
    """A hostname with one public and one private A record must be refused — not
    slip through on the public one."""
    import socket

    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, None, None, "", ("8.8.8.8", 0)),
            (socket.AF_INET, None, None, "", ("10.0.0.5", 0)),
        ],
    )
    with pytest.raises(net_guard.UnsafeUrlError):
        net_guard.validate_url("https://evil.example.com")


def test_ssrf_accepts_a_normal_public_https_url(monkeypatch):
    import socket

    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))],
    )
    assert net_guard.validate_url("https://hooks.acme.example.com/e") == ["93.184.216.34"]


# ── delivery + retry ────────────────────────────────────────────────────────
from brain.gateway import webhook_delivery as wd  # noqa: E402


class _FakeDB:
    """Minimal PostgREST double: one webhook row + a delivery ledger, with the exact
    chain methods the sweeper uses."""

    def __init__(self, hook, deliveries):
        self.hook = hook  # {"url","active","consecutive_failures"}
        self.deliveries = {d["id"]: d for d in deliveries}
        self.secret = "whsec_test"

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        self._rpc = (name, params)
        return self

    def execute(self):
        name, _p = self._rpc
        if name == "get_partner_webhook_secret":
            return type("R", (), {"data": self.secret})()
        return type("R", (), {"data": None})()


class _Q:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self._eq = {}
        self._patch = None
        self._op = "select"

    def select(self, *a, **k):
        return self

    def update(self, patch):
        self._op, self._patch = "update", patch
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals):
        self._eq[(col, "in")] = vals
        return self

    def lte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self.table_name == "partner_webhooks":
            if self._op == "update":
                self.db.hook.update(self._patch)
                return type("R", (), {"data": [self.db.hook]})()
            return type("R", (), {"data": [self.db.hook]})()
        # webhook_deliveries
        if self._op == "update":
            did = self._eq.get("id")
            row = self.db.deliveries.get(did)
            if row is None:
                return type("R", (), {"data": []})()
            # honour the conditional-claim state guard
            if "state" in self._eq and row["state"] != self._eq["state"]:
                return type("R", (), {"data": []})()
            row.update(self._patch)
            return type("R", (), {"data": [row]})()
        # select: honour the state filter the sweeper applies, like Postgres would.
        rows = list(self.db.deliveries.values())
        states = self._eq.get(("state", "in"))
        if states is not None:
            rows = [r for r in rows if r["state"] in states]
        return type("R", (), {"data": rows})()


def _delivery(**kw):
    base = {
        "org_id": "o1",
        "id": "dlv_1",
        "webhook_id": "wh_1",
        "event_id": "evt_1",
        "event_type": "job.completed",
        "payload": {"event": "job.completed", "data": {"job_id": "j1"}},
        "state": "pending",
        "attempts": 0,
    }
    base.update(kw)
    return base


async def _ok_post(url, body, headers):
    return 200


async def _500_post(url, body, headers):
    return 500


def test_a_2xx_marks_delivered_and_signs(monkeypatch):
    import asyncio

    seen = {}

    async def _post(url, body, headers):
        seen["url"], seen["headers"], seen["body"] = url, headers, body
        return 204

    db = _FakeDB({"url": "https://ok.example.com", "active": True}, [_delivery()])
    monkeypatch.setattr(wd, "validate_url", lambda u: ["1.2.3.4"])  # skip real DNS
    claimed = wd.claim_due(db, now=100.0)
    assert claimed and claimed[0]["state"] == "sending"
    state = asyncio.run(wd.deliver_one(db, claimed[0], now=100.0, http_post=_post))
    assert state == "delivered"
    assert webhook_sign.HEADER in seen["headers"]
    # signature verifies over the exact bytes sent
    assert webhook_sign.verify(
        db.secret, seen["body"], seen["headers"][webhook_sign.HEADER], now=100
    )


def test_a_500_reschedules_with_backoff(monkeypatch):
    import asyncio

    db = _FakeDB({"url": "https://ok.example.com", "active": True}, [_delivery()])
    monkeypatch.setattr(wd, "validate_url", lambda u: ["1.2.3.4"])
    claimed = wd.claim_due(db, now=100.0)
    state = asyncio.run(wd.deliver_one(db, claimed[0], now=100.0, http_post=_500_post))
    assert state == "failed"
    assert db.deliveries["dlv_1"]["state"] == "failed"
    assert db.deliveries["dlv_1"]["next_attempt_ts"] > wd._iso(100.0)


def test_exhaustion_dead_letters_and_disables(monkeypatch):
    import asyncio

    # attempts already near the end of the backoff table.
    db = _FakeDB(
        {"url": "https://ok.example.com", "active": True, "consecutive_failures": 19},
        [_delivery(attempts=len(wd._BACKOFF_S))],
    )
    monkeypatch.setattr(wd, "validate_url", lambda u: ["1.2.3.4"])
    state = asyncio.run(wd.deliver_one(db, db.deliveries["dlv_1"], now=100.0, http_post=_500_post))
    assert state == "dead_letter"
    # 20th consecutive failure → auto-disabled
    assert db.hook.get("active") is False
    assert db.hook.get("disabled_reason") == "repeated_delivery_failure"


def test_a_410_stops_immediately(monkeypatch):
    import asyncio

    async def _410(url, body, headers):
        return 410

    db = _FakeDB({"url": "https://ok.example.com", "active": True}, [_delivery()])
    monkeypatch.setattr(wd, "validate_url", lambda u: ["1.2.3.4"])
    claimed = wd.claim_due(db, now=100.0)
    state = asyncio.run(wd.deliver_one(db, claimed[0], now=100.0, http_post=_410))
    assert state == "dead_letter"


def test_delivery_revalidates_the_url_each_attempt(monkeypatch):
    """A URL that was public at registration but now resolves internal must be caught
    at delivery — the check-then-connect window is closed by re-validating."""
    import asyncio

    def _boom(u):
        raise net_guard.UnsafeUrlError("now resolves to 10.0.0.1")

    db = _FakeDB({"url": "https://rebind.example.com", "active": True}, [_delivery()])
    monkeypatch.setattr(wd, "validate_url", _boom)
    claimed = wd.claim_due(db, now=100.0)
    state = asyncio.run(wd.deliver_one(db, claimed[0], now=100.0, http_post=_ok_post))
    assert state == "dead_letter"


def test_default_post_connects_to_pinned_ip(monkeypatch):
    """The production poster connects to the freshly-vetted IP (closing the DNS-rebind
    window) while Host + TLS SNI still present the real hostname, redirects disabled."""
    import asyncio

    import httpx

    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (net_guard.socket.AF_INET, net_guard.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    seen: dict = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            seen["url"], seen["kwargs"] = url, kw
            return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    status = asyncio.run(
        wd._default_post("https://hooks.acme.example.com/e", b"{}", {"X-Test": "1"})
    )
    assert status == 204
    assert seen["url"] == "https://93.184.216.34/e"
    assert seen["kwargs"]["headers"].get("Host") == "hooks.acme.example.com"
    assert seen["kwargs"]["headers"].get("X-Test") == "1"  # caller headers preserved
    assert seen["kwargs"]["extensions"].get("sni_hostname") == "hooks.acme.example.com"
    assert seen["kwargs"]["follow_redirects"] is False


def test_claim_is_idempotent_under_a_double_sweeper():
    """The conditional-state claim means a second sweeper can't re-claim a row."""
    db = _FakeDB({"url": "https://ok.example.com", "active": True}, [_delivery()])
    first = wd.claim_due(db, now=100.0)
    second = wd.claim_due(db, now=100.0)  # already 'sending' now
    assert len(first) == 1
    assert second == []


# ── emission from a terminal job outcome ────────────────────────────────────
from brain.autonomy.outcome import JobOutcome, JobState  # noqa: E402
from brain.clusters.motor_cortex import MotorCortexCluster  # noqa: E402


def _outcome(state=JobState.COMPLETED):
    return JobOutcome(state=state, job_id="j1", reason_human="did it", summary="a summary")


def test_terminal_outcome_enqueues_one_delivery_with_the_turn_partner(monkeypatch):
    from brain import turn_ctx
    from brain.api import webhooks

    calls = []
    monkeypatch.setattr(
        webhooks, "enqueue", lambda ev, payload, pid: calls.append((ev, payload, pid))
    )

    with turn_ctx.bind_turn("agent", partner_id="A"):
        MotorCortexCluster._enqueue_job_webhook(object(), _outcome(), "Draft the note")

    assert len(calls) == 1
    ev, payload, pid = calls[0]
    assert ev == "job.completed"
    assert pid == "A"
    assert payload["data"]["job_id"] == "j1"
    # summary + a job id to fetch the rest — never the full record.
    assert "steps" not in payload["data"] and "results" not in payload["data"]


@pytest.mark.parametrize(
    "state,expected",
    [
        (JobState.COMPLETED, "job.completed"),
        (JobState.FAILED, "job.failed"),
        (JobState.DEFERRED, "job.deferred"),
        (JobState.STOPPED_BUDGET, "job.stopped_budget"),
    ],
)
def test_each_terminal_state_maps_to_its_event(monkeypatch, state, expected):
    from brain.api import webhooks

    calls = []
    monkeypatch.setattr(webhooks, "enqueue", lambda ev, p, pid: calls.append(ev))
    MotorCortexCluster._enqueue_job_webhook(object(), _outcome(state), "g")
    assert calls == [expected]


def test_a_webhook_failure_never_breaks_the_outcome(monkeypatch):
    from brain.api import webhooks

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(webhooks, "enqueue", _boom)
    # Must not raise.
    MotorCortexCluster._enqueue_job_webhook(object(), _outcome(), "g")


def test_owner_lane_job_enqueues_with_empty_partner(monkeypatch):
    from brain.api import webhooks

    calls = []
    monkeypatch.setattr(webhooks, "enqueue", lambda ev, p, pid: calls.append(pid))
    # No bound turn → owner lane.
    MotorCortexCluster._enqueue_job_webhook(object(), _outcome(), "g")
    assert calls == [""]


# ── push-first delivery: enqueue nudges the gateway; the sweeper gates + decays ──


class _EnqueueClient:
    """Org-scoped client double for webhooks.enqueue: active hooks + insert ledger."""

    def __init__(self, hooks):
        self.hooks = hooks
        self.inserted = []
        self._t = None

    def table(self, name):
        self._t = name
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def insert(self, row):
        self.inserted.append(row)
        return self

    def execute(self):
        if self._t == "partner_webhooks":
            return type("R", (), {"data": list(self.hooks)})()
        return type("R", (), {"data": [self.inserted[-1]] if self.inserted else []})()


def test_enqueue_touches_the_nudge_file(monkeypatch, tmp_path):
    from brain.api import webhooks

    nudge = tmp_path / "outbox"
    monkeypatch.setenv("BRAIN_WEBHOOK_NUDGE_FILE", str(nudge))
    client = _EnqueueClient([{"id": "wh_1", "partner_id": ""}])
    monkeypatch.setattr(webhooks, "_sb", lambda: (client, "org1"))

    assert webhooks.enqueue("job.completed", {"event": "job.completed"}, "") == 1
    assert nudge.exists()


def test_enqueue_with_no_matching_hook_does_not_nudge(monkeypatch, tmp_path):
    from brain.api import webhooks

    nudge = tmp_path / "outbox"
    monkeypatch.setenv("BRAIN_WEBHOOK_NUDGE_FILE", str(nudge))
    client = _EnqueueClient([])  # nothing registered → nothing enqueued
    monkeypatch.setattr(webhooks, "_sb", lambda: (client, "org1"))

    assert webhooks.enqueue("job.completed", {"event": "job.completed"}, "") == 0
    assert not nudge.exists()


def test_enqueue_without_the_env_var_still_enqueues(monkeypatch):
    from brain.api import webhooks

    monkeypatch.delenv("BRAIN_WEBHOOK_NUDGE_FILE", raising=False)
    client = _EnqueueClient([{"id": "wh_1", "partner_id": ""}])
    monkeypatch.setattr(webhooks, "_sb", lambda: (client, "org1"))
    assert webhooks.enqueue("job.completed", {"event": "job.completed"}, "") == 1


def test_nudge_mtime_tracks_the_file(monkeypatch, tmp_path):
    f = tmp_path / "outbox"
    monkeypatch.setenv("BRAIN_WEBHOOK_NUDGE_FILE", str(f))
    assert wd._nudge_mtime() == -1.0
    f.touch()
    assert wd._nudge_mtime() > 0


def test_active_webhook_gate_closes_on_empty_and_fails_open():
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

    assert wd.any_active_webhook(_Empty()) is False
    assert wd.any_active_webhook(_Boom()) is True
