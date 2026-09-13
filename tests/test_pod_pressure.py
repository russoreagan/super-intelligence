"""GPU-pod pressure telemetry (brain/pod_pressure) and the semaphore regression.

Background: the RunPod STREAMING path in ModelRouter._call_local never acquired the
`local_max_concurrent` semaphore — only the non-stream fallback and the local-Ollama
path did. So the one dial meant to bound in-flight pod calls bounded nothing on the
pod: N brains fanned N concurrent calls onto one serialised 32B, and saturation showed
up as timeouts (a quiet-looking DMN) rather than as a queue. These tests pin the fix
and the telemetry the pod pool scales on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

import brain.pod_pressure as pp


@pytest.fixture(autouse=True)
def _fresh():
    pp.reset()
    yield
    pp.reset()


# ── ring statistics ──────────────────────────────────────────────────────────


def test_snapshot_is_empty_before_any_call():
    s = pp.snapshot(now=1000.0)
    assert s["calls_1m"] == 0
    assert s["busy_s_1m"] == 0
    assert s["wait_p95_s"] == 0
    assert s["sat_frac_1m"] == 0.0
    assert s["fail_1m"] == 0
    assert s["demand_ts"] is None and s["use_ts"] is None


def test_snapshot_reduces_the_last_minute():
    now = 1000.0
    for i in range(10):
        # waits 0..9 s, each call 2 s long, one failure
        pp.record(wait_s=float(i), latency_s=2.0, ok=(i != 3), now=now - 30 + i)
    s = pp.snapshot(now=now)
    assert s["calls_1m"] == 10
    assert s["busy_s_1m"] == 20.0
    assert s["fail_1m"] == 1
    assert s["wait_p50_s"] == pytest.approx(4.0, abs=1.0)
    assert s["wait_p95_s"] >= 8.0
    # waits of 4 s and above count as saturated: 4,5,6,7,8,9 → 6 of 10
    assert s["sat_frac_1m"] == pytest.approx(0.6)


def test_samples_age_out_of_the_window():
    now = 5000.0
    pp.record(1.0, 1.0, True, now=now - 120)  # two minutes old → gone
    pp.record(1.0, 1.0, True, now=now - 10)
    s = pp.snapshot(now=now)
    assert s["calls_1m"] == 1


def test_inflight_and_permits_are_tracked():
    pp.set_permits(3)
    pp.note_inflight(+1)
    pp.note_inflight(+1)
    pp.note_inflight(-1)
    s = pp.snapshot()
    assert s["permits"] == 3 and s["inflight"] == 1
    pp.note_inflight(-5)  # never negative
    assert pp.snapshot()["inflight"] == 0


def test_demand_and_use_stamps_ride_along():
    pp.note_demand(now=10.0)
    pp.note_use(now=12.0)
    s = pp.snapshot(now=20.0)
    assert s["demand_ts"] == 10.0 and s["use_ts"] == 12.0


# ── publication ──────────────────────────────────────────────────────────────


def test_write_snapshot_is_keyed_and_atomic(tmp_path):
    pp.record(0.5, 1.0, True, now=100.0)
    path = pp.file_for("org-1::the_analyst", tmp_path)
    body = pp.write_snapshot(path, "org-1::the_analyst", now=100.0)
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists(), "temp file must be renamed away"
    on_disk = json.loads(path.read_text())
    assert on_disk == body
    assert on_disk["proc_key"] == "org-1::the_analyst"
    assert on_disk["ts"] == 100.0
    assert on_disk["calls_1m"] == 1


def test_read_all_returns_fresh_files_and_drops_stale_ones(tmp_path, monkeypatch):
    import time as _time

    now = _time.time()
    fresh = tmp_path / "a.json"
    fresh.write_text(json.dumps({"proc_key": "org-a", "ts": now, "busy_s_1m": 3}))
    stale = tmp_path / "b.json"
    stale.write_text(json.dumps({"proc_key": "org-b", "ts": now - 3600, "busy_s_1m": 3}))
    junk = tmp_path / "c.json"
    junk.write_text("not json")
    out = pp.read_all(tmp_path, max_age_s=180.0)
    assert set(out) == {"org-a"}
    assert not stale.exists(), "a dead process's file is cleaned up"
    assert junk.exists(), "unreadable files are ignored, not deleted"


def test_writer_loop_returns_without_a_proc_key(monkeypatch):
    monkeypatch.delenv("BRAIN_PROC_KEY", raising=False)

    async def _run():
        await asyncio.wait_for(pp.writer_loop(), timeout=1.0)

    asyncio.run(_run())  # returns at once — nothing would read the file


def test_writer_loop_writes_the_keyed_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PROC_KEY", "org-1")
    monkeypatch.setenv("BRAIN_POD_PRESSURE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_POD_PRESSURE_S", "5")  # floor is 5 s
    monkeypatch.setattr(pp, "interval_s", lambda: 0.02)

    async def _run():
        task = asyncio.create_task(pp.writer_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    body = json.loads((tmp_path / "org-1.json").read_text())
    assert body["proc_key"] == "org-1"
    assert "wait_p95_s" in body


def test_pressure_dir_defaults_under_the_tenants_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAIN_POD_PRESSURE_DIR", raising=False)
    monkeypatch.setenv("BRAIN_TENANTS_DIR", str(tmp_path))
    assert pp.pressure_dir() == tmp_path.resolve() / ".pod_pressure"


def test_provisioner_injects_proc_key_and_pressure_dir():
    """The spawn env block must carry the key the pool assigns under, or the brain's
    pressure file and the gateway's assignment name different processes."""
    from pathlib import Path

    src = Path("brain/provisioner.py").read_text(encoding="utf-8")
    assert '"BRAIN_PROC_KEY": self._key(user_id, persona)' in src
    assert '"BRAIN_POD_PRESSURE_DIR": str(POD_PRESSURE_DIR)' in src


# ── the regression: the STREAM path must hold the semaphore ──────────────────


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0)
            yield line


class _FakeStreamCtx:
    def __init__(self, client, lines):
        self._client = client
        self._lines = lines

    async def __aenter__(self):
        self._client.entered.append(self._client.sem.locked())
        self._client.inflight_seen.append(pp.snapshot()["inflight"])
        await asyncio.sleep(0.02)  # hold the stream open so a second caller must wait
        return _FakeResp(self._lines)

    async def __aexit__(self, *exc):
        return False


class _FakeHttp:
    """Streams one done-chunk; records whether the router's semaphore was held while
    the stream was open."""

    def __init__(self, sem):
        self.sem = sem
        self.entered: list[bool] = []
        self.inflight_seen: list[int] = []

    def stream(self, method, url, **kw):
        lines = [
            json.dumps({"message": {"content": "hello"}}),
            json.dumps({"done": True, "prompt_eval_count": 7, "eval_count": 3}),
        ]
        return _FakeStreamCtx(self, lines)


def _router_with(monkeypatch, sem):
    import brain.settings as settings_mod
    from brain.model_router import ModelRouter

    monkeypatch.setattr(
        settings_mod.settings,
        "get",
        lambda k, d=None: {
            "runpod_host": "https://pod-11434.proxy.runpod.net",
            "runpod_model": "qwen2.5:32b",
            "runpod_num_ctx": 8192,
            "runpod_stream_retries": 0,
        }.get(k, d),
    )
    r = ModelRouter.__new__(ModelRouter)
    r._local_semaphore = sem
    http = _FakeHttp(sem)
    r._get_http = lambda: http  # type: ignore[assignment]
    r._warn_if_context_full = lambda *a, **k: None  # type: ignore[assignment]
    return r, http


def test_runpod_stream_path_holds_the_local_semaphore(monkeypatch):
    sem = asyncio.Semaphore(1)
    r, http = _router_with(monkeypatch, sem)
    out = asyncio.run(
        r._call_local("sys", [{"role": "user", "content": "hi"}], local_variant="runpod")
    )
    assert out == ("hello", 7, 3)
    assert http.entered == [True], "the STREAM must run with the semaphore held"
    assert http.inflight_seen == [1]
    assert sem.locked() is False, "the slot is released afterwards"
    s = pp.snapshot()
    assert s["calls_1m"] == 1 and s["fail_1m"] == 0
    assert s["busy_s_1m"] > 0
    assert s["use_ts"] is not None and s["demand_ts"] is not None


def test_second_runpod_call_queues_and_records_its_wait(monkeypatch):
    """With one permit, two concurrent calls serialise on the pod and the second's
    wait is recorded — that wait is the pool's scale-up signal."""
    sem = asyncio.Semaphore(1)
    r, http = _router_with(monkeypatch, sem)

    async def _two():
        a = r._call_local("sys", [{"role": "user", "content": "1"}], local_variant="runpod")
        b = r._call_local("sys", [{"role": "user", "content": "2"}], local_variant="runpod")
        return await asyncio.gather(a, b)

    outs = asyncio.run(_two())
    assert all(o == ("hello", 7, 3) for o in outs)
    assert http.entered == [True, True]
    assert http.inflight_seen == [1, 1], "never two in flight with one permit"
    s = pp.snapshot()
    assert s["calls_1m"] == 2
    assert s["wait_p95_s"] > 0.0, "the queued call's semaphore wait must be recorded"


def test_failed_runpod_call_is_recorded_as_a_failure(monkeypatch):
    import brain.settings as settings_mod
    from brain.model_router import ModelRouter

    monkeypatch.setattr(
        settings_mod.settings,
        "get",
        lambda k, d=None: {
            "runpod_host": "https://pod-11434.proxy.runpod.net",
            "runpod_stream_retries": 0,
            "runpod_num_ctx": 8192,
        }.get(k, d),
    )
    r = ModelRouter.__new__(ModelRouter)
    r._local_semaphore = asyncio.Semaphore(2)
    r._warn_if_context_full = lambda *a, **k: None  # type: ignore[assignment]

    class _Boom:
        def stream(self, *a, **k):
            raise RuntimeError("connection refused")

        async def post(self, *a, **k):
            raise RuntimeError("connection refused")

    r._get_http = lambda: _Boom()  # type: ignore[assignment]

    async def _noop():
        pass

    r._reset_http = _noop  # type: ignore[assignment]
    out = asyncio.run(
        r._call_local("sys", [{"role": "user", "content": "hi"}], local_variant="runpod")
    )
    assert out == ("", 0, 0)
    s = pp.snapshot()
    assert s["calls_1m"] == 1 and s["fail_1m"] == 1
    assert s["use_ts"] is None, "a failed call is demand, never use"


def test_health_reports_pressure_and_skips():
    from pathlib import Path

    src = Path("brain/ui/server.py").read_text(encoding="utf-8")
    for field in ("local_wait_p95_s", "local_inflight", "runpod_skips"):
        assert f'body["{field}"]' in src, f"/health must carry {field}"
