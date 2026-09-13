"""Per-process GPU-pod pressure telemetry — the signal the pod pool scales on.

WHY: the gateway's reconciler could only see two bits per pod: "something asked"
(`.pod_demand`) and "something got output" (`.pod_used`). Both are enough to decide
whether ONE pod should be awake, and neither says anything about whether one pod is
ENOUGH. The 32B model is serialised on a single card; when N brains queue on it the
symptom is not an error but rising semaphore wait and DMN ticks that time out, which
from the outside looks exactly like a quiet pod. This module makes saturation
observable: every local call records how long it waited for a slot in
`local_max_concurrent`, how long the pod took, and whether it produced anything.

One ring buffer per process. `snapshot()` reduces the last minute to the numbers the
pool needs — in-flight, permits, calls, busy seconds, wait p50/p95, saturated fraction,
failures — and `writer_loop()` publishes it to `tenants/.pod_pressure/<proc_key>.json`
every BRAIN_POD_PRESSURE_S (atomic temp + rename, same idiom as the host file). The
gateway reads every file in that directory, attributes each to the pod its process is
assigned to, and sums: pod utilisation = Σ busy_s_1m(consumers) / (60 × parallel).

`proc_key` is the provisioner's key for this process (`org` or `org::persona`),
injected at spawn as BRAIN_PROC_KEY. A process without one (single-brain / local mode)
records in memory for /health but writes nothing — there is no reconciler to read it.

Pure bookkeeping, no I/O except the writer loop; safe to import from the router.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

# Samples kept in memory. At the DMN's fastest cadence (~5 s) plus turn fan-outs this
# comfortably covers the one-minute window snapshot() reduces; older entries are
# dropped by age, not by count, so the cap is only a memory bound.
_RING_MAX = 4096
_WINDOW_S = 60.0
# A slot wait this long counts the call as "saturated" for sat_frac_1m. Half the
# pool's scale-up wait threshold (PoolConfig.up_wait_p95_s = 8 s): the fraction is
# meant to move before p95 does, so the reconciler sees pressure building rather
# than only pressure arrived.
_SAT_WAIT_S = 4.0

_ring: deque[tuple[float, float, float, bool]] = deque(maxlen=_RING_MAX)
_inflight = 0
_permits = 0
_demand_ts: float | None = None
_use_ts: float | None = None


def reset() -> None:
    """Forget everything (tests, and a fresh process)."""
    global _inflight, _permits, _demand_ts, _use_ts
    _ring.clear()
    _inflight = 0
    _permits = 0
    _demand_ts = None
    _use_ts = None


def set_permits(n: int) -> None:
    """The semaphore's size (settings.local_max_concurrent) — published so the pool
    can tell 'two in flight of three' from 'two in flight of two'."""
    global _permits
    _permits = max(0, int(n))


def note_inflight(delta: int) -> None:
    global _inflight
    _inflight = max(0, _inflight + int(delta))


def note_demand(now: float | None = None) -> None:
    """A runpod-routed cell wanted the pod (recorded before the off-check, like the
    global `.pod_demand` touch — a call that finds the pod asleep IS the wake signal)."""
    global _demand_ts
    _demand_ts = time.time() if now is None else now


def note_use(now: float | None = None) -> None:
    """The pod produced real output for this process (content + output tokens)."""
    global _use_ts
    _use_ts = time.time() if now is None else now


def record(wait_s: float, latency_s: float, ok: bool, now: float | None = None) -> None:
    """One completed local call: seconds spent waiting for a semaphore slot, seconds the
    call itself took, and whether it produced content."""
    ts = time.time() if now is None else now
    _ring.append((ts, max(0.0, float(wait_s)), max(0.0, float(latency_s)), bool(ok)))


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, int(round(q * (len(vs) - 1)))))
    return vs[idx]


def snapshot(now: float | None = None) -> dict:
    """Reduce the last minute to the pool's inputs. Cheap; called by the writer loop
    and by /health."""
    ts_now = time.time() if now is None else now
    cutoff = ts_now - _WINDOW_S
    # Drop aged-out samples from the left so the ring never fills with stale data.
    while _ring and _ring[0][0] < cutoff:
        _ring.popleft()
    recent = [s for s in _ring if s[0] >= cutoff]
    waits = [s[1] for s in recent]
    busy = sum(s[2] for s in recent)
    fails = sum(1 for s in recent if not s[3])
    sat = sum(1 for s in recent if s[1] >= _SAT_WAIT_S)
    return {
        "inflight": _inflight,
        "permits": _permits,
        "calls_1m": len(recent),
        # Wall-clock seconds the pod spent on this process's calls in the window.
        # Utilisation against the pod = busy / (60 × OLLAMA_NUM_PARALLEL).
        "busy_s_1m": round(busy, 2),
        "wait_p50_s": round(_percentile(waits, 0.50), 3),
        "wait_p95_s": round(_percentile(waits, 0.95), 3),
        "sat_frac_1m": round(sat / len(recent), 3) if recent else 0.0,
        "fail_1m": fails,
        "demand_ts": _demand_ts,
        "use_ts": _use_ts,
    }


# ── publication ──────────────────────────────────────────────────────────────


def proc_key() -> str:
    """This process's provisioner key (`org` or `org::persona`); empty when the
    process was not spawned by a gateway."""
    return os.environ.get("BRAIN_PROC_KEY", "").strip()


def pressure_dir() -> Path:
    """Where pressure files live. BRAIN_POD_PRESSURE_DIR (injected at spawn) else
    `<BRAIN_TENANTS_DIR>/.pod_pressure` — the gateway resolves the same default, so
    the two agree even when the env var is missing."""
    override = os.environ.get("BRAIN_POD_PRESSURE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("BRAIN_TENANTS_DIR", "tenants")).resolve() / ".pod_pressure"


def file_for(key: str, directory: Path | None = None) -> Path:
    """`<dir>/<proc_key>.json`. The key is used literally: `::` is a legal path
    character on the hosts we run on, and the file body carries `proc_key` too so a
    reader never has to parse the filename."""
    return (directory or pressure_dir()) / f"{key}.json"


def write_snapshot(path: Path, key: str, now: float | None = None) -> dict:
    """Write one snapshot atomically. Returns the body written. Best-effort: a
    failure here must never disturb inference, it only delays a scaling decision."""
    body = snapshot(now)
    body["proc_key"] = key
    body["ts"] = time.time() if now is None else now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("[pod_pressure] write failed: %s", e)
    return body


def read_all(directory: Path | None = None, *, max_age_s: float = 180.0) -> dict[str, dict]:
    """Gateway side: every fresh pressure file, keyed by proc_key. Files older than
    `max_age_s` (a dead or reaped process) are skipped — and unlinked, so the
    directory does not grow forever. Unreadable files are ignored."""
    d = directory or pressure_dir()
    out: dict[str, dict] = {}
    try:
        entries = list(d.glob("*.json"))
    except Exception:
        return out
    now = time.time()
    for p in entries:
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(body, dict):
                continue
            ts = float(body.get("ts") or 0.0)
            if now - ts > max_age_s:
                p.unlink(missing_ok=True)
                continue
            key = str(body.get("proc_key") or p.stem)
            out[key] = body
        except Exception:
            continue
    return out


def interval_s() -> float:
    try:
        return max(5.0, float(os.environ.get("BRAIN_POD_PRESSURE_S", "30")))
    except ValueError:
        return 30.0


async def writer_loop() -> None:
    """Publish this process's snapshot every BRAIN_POD_PRESSURE_S. Returns at once
    when the process has no proc key (nothing would read the file)."""
    key = proc_key()
    if not key:
        return
    path = file_for(key)
    while True:
        await asyncio.sleep(interval_s())
        snap = write_snapshot(path, key)
        # A snapshot with activity is an event for the gateway's pool scaler; a
        # quiet one is not (the gateway's own deadlines handle idleness).
        try:
            if int(snap.get("calls_1m") or 0) > 0 or int(snap.get("inflight") or 0) > 0:
                from brain import gateway_nudge

                gateway_nudge.nudge("pressure")
        except Exception:
            pass
