"""A tenant brain's memory must not ratchet up with the host's core count.

Railway shows 48 cores. The first voice turn loaded PyTorch and openSMILE, their
thread pools sized themselves to every core, glibc gave the busy threads their own
heap arenas, and freed pages were never handed back: one brain went 0.36 → 2.3 GB.
Spawns now cap the native pools and arenas, and the heartbeat trims the heap.
"""

from __future__ import annotations

import brain.brainstem as bs
import brain.provisioner as pv


def test_spawn_env_caps_native_threads_and_arenas(monkeypatch):
    monkeypatch.delenv("BRAIN_TENANT_NUM_THREADS", raising=False)
    env = pv.native_runtime_env()
    assert env["OMP_NUM_THREADS"] == "2"
    assert env["MKL_NUM_THREADS"] == "2"
    assert env["OPENBLAS_NUM_THREADS"] == "2"
    assert env["MALLOC_ARENA_MAX"] == "2"


def test_thread_count_is_tunable():
    assert pv.native_runtime_env("4")["OMP_NUM_THREADS"] == "4"


def test_zero_leaves_the_libraries_alone():
    assert pv.native_runtime_env("0") == {}


def test_bad_value_falls_back_to_the_default():
    assert pv.native_runtime_env("lots")["OMP_NUM_THREADS"] == "2"


def test_heap_trim_calls_malloc_trim(monkeypatch):
    calls = []
    monkeypatch.setattr(bs, "_MALLOC_TRIM", True)
    monkeypatch.setattr(bs, "_libc_checked", True)
    monkeypatch.setattr(bs, "_libc_trim", lambda pad: calls.append(pad) or 1)
    assert bs.release_free_heap() is True
    assert calls == [0]


def test_heap_trim_is_a_no_op_without_glibc(monkeypatch):
    monkeypatch.setattr(bs, "_MALLOC_TRIM", True)
    monkeypatch.setattr(bs, "_libc_checked", True)
    monkeypatch.setattr(bs, "_libc_trim", None)
    assert bs.release_free_heap() is False


def test_heap_trim_kill_switch(monkeypatch):
    calls = []
    monkeypatch.setattr(bs, "_MALLOC_TRIM", False)
    monkeypatch.setattr(bs, "_libc_checked", True)
    monkeypatch.setattr(bs, "_libc_trim", lambda pad: calls.append(pad) or 1)
    assert bs.release_free_heap() is False
    assert calls == []


async def test_heartbeat_trims_the_heap(monkeypatch):
    calls = []
    monkeypatch.setattr(bs, "release_free_heap", lambda: calls.append(1) or True)
    await bs.Brainstem(bus=None, model_router=None).heartbeat_once()
    assert calls == [1]
