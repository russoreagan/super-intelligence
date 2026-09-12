"""
Self-task cooldown: a DMN self-task is deduplicated against recently COMPLETED
self/commitment work for self_task_dedup_recency_s (default 24h), using
content-word overlap, with a small ledger that survives MAX_TASKS trimming.
Project steps keep the short PROJECT_DEDUP_RECENCY window (recurring rows).

Background (2026-09): the only self-task dedup was a hardcoded 2h window on the
queue file, so "Read the app's own docs and settings surfaces…" re-ran every ~2h
(45x in 8 days, 12 list_files steps each).
"""

from __future__ import annotations

import json

import pytest

import brain.clusters.task_queue as tq
from brain.clusters.task_queue import PersistentTaskQueue
from brain.settings import settings

DOCS_GOAL = (
    "Read the app's own docs and settings surfaces so questions about configuration "
    "can be answered from the source."
)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    # Isolate from the real on-disk queue + ledger (never clobber them, never
    # inherit their dedup state).
    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(tq, "SELF_LEDGER_PATH", tmp_path / "self_task_ledger.json")
    return PersistentTaskQueue()


def _complete(q: PersistentTaskQueue, goal: str, source: str = "self", **kw):
    t = q.enqueue(goal, source=source, priority=2, **kw)
    assert t is not None, f"expected {goal!r} to enqueue"
    q.take_next()
    q.mark_done(t.id, success=True)
    return t


def _advance(monkeypatch, seconds: float):
    base = tq.time.time()
    monkeypatch.setattr(tq.time, "time", lambda: base + seconds)


def test_default_window_is_a_day():
    assert settings.get("self_task_dedup_recency_s") == 86400
    assert tq.PROJECT_DEDUP_RECENCY == 2 * 60 * 60
    assert tq.SELF_DEDUP_RECENCY == tq.PROJECT_DEDUP_RECENCY  # legacy alias intact


def test_self_task_deduped_within_window_and_admitted_after(queue, monkeypatch):
    _complete(queue, DOCS_GOAL)
    _advance(monkeypatch, 3 * 3600)  # 3h: inside 24h, outside the old 2h window
    assert queue.enqueue(DOCS_GOAL, source="self", priority=2) is None
    # A light rewording is still the same work.
    assert (
        queue.enqueue(
            "Read the app's docs & settings surfaces; answer configuration questions from source",
            source="self",
        )
        is None
    )
    _advance(monkeypatch, 25 * 3600)
    assert queue.enqueue(DOCS_GOAL, source="self", priority=2) is not None


def test_commitment_source_shares_the_window(queue, monkeypatch):
    _complete(queue, "look into the unusual options sweep on IWM", source="commitment")
    _advance(monkeypatch, 5 * 3600)
    assert queue.enqueue("look into the unusual IWM options sweep", source="commitment") is None


def test_user_tasks_are_never_cooled_down(queue):
    _complete(queue, DOCS_GOAL, source="user")
    assert queue.enqueue(DOCS_GOAL, source="user") is not None


def test_stop_words_do_not_inflate_overlap(queue, monkeypatch):
    # Raw whitespace overlap scored these ≥0.55 on "the/and/for" alone.
    _complete(queue, "Read the docs and check the settings for the app")
    assert queue.enqueue("Check the logs and read the queue for the worker", source="self")


def test_distinct_steps_of_one_project_are_not_deduped(queue):
    _complete(queue, "Read brain/dmn.py and summarise the idle loop architecture")
    assert queue.enqueue(
        "Read brain/pns.py and summarise the speech pipeline architecture", source="self"
    )


def test_project_steps_keep_the_short_window(queue, monkeypatch):
    goal = "Review the historical market data for the last year and identify regimes"
    _complete(queue, goal, dedup_recency_s=tq.PROJECT_DEDUP_RECENCY)
    _advance(monkeypatch, 3 * 3600)
    # Same goal as a project step (explicit 2h window) → admitted after 3h …
    assert queue.enqueue(goal, source="self", dedup_recency_s=tq.PROJECT_DEDUP_RECENCY)
    # … while the ad-hoc default would still refuse it.
    assert queue.enqueue(goal + " again", source="self") is None


def test_settings_window_is_live(queue, monkeypatch):
    monkeypatch.setitem(settings._data, "self_task_dedup_recency_s", 600)
    _complete(queue, DOCS_GOAL)
    _advance(monkeypatch, 900)
    assert queue.enqueue(DOCS_GOAL, source="self") is not None


def test_env_seeds_the_window(monkeypatch):
    from brain.settings import Settings

    monkeypatch.setenv("BRAIN_SELF_TASK_DEDUP_RECENCY_S", "3600")
    assert Settings().get("self_task_dedup_recency_s") == 3600


def test_ledger_survives_queue_trimming_and_restart(queue, tmp_path, monkeypatch):
    _complete(queue, DOCS_GOAL)
    # Push MAX_TASKS+ user tasks through so the completed self entry is trimmed
    # out of the queue file.
    for i in range(tq.MAX_TASKS + 5):
        _complete(queue, f"user task number {i} about topic {i * 7}", source="user")
    assert all(t.goal != DOCS_GOAL for t in queue._tasks)
    assert queue.enqueue(DOCS_GOAL, source="self") is None  # ledger still remembers
    # Persisted, bounded, and reloaded by a fresh instance.
    ledger = json.loads((tmp_path / "self_task_ledger.json").read_text())
    assert [e["goal"] for e in ledger] == [DOCS_GOAL]  # user tasks are not recorded
    fresh = PersistentTaskQueue()
    assert fresh.enqueue(DOCS_GOAL, source="self") is None
    _advance(monkeypatch, 25 * 3600)
    assert fresh.enqueue(DOCS_GOAL, source="self") is not None


def _word(i: int) -> str:
    # Unique alphabetic token per index (digits are not content words).
    return "".join(chr(97 + (i // 26**k) % 26) for k in range(4))


def test_ledger_is_bounded(queue):
    for i in range(tq.SELF_LEDGER_MAX + 20):
        _complete(queue, f"investigate {_word(i)} {_word(i + 1000)} {_word(i + 2000)} thoroughly")
    assert len(queue._ledger) == tq.SELF_LEDGER_MAX


def test_pending_dedup_unchanged(queue):
    # The pending/running dedup still uses the looser raw-overlap threshold.
    assert queue.enqueue("scan NVDA options flow for unusual sweeps", source="self")
    assert queue.enqueue("scan NVDA options flow for unusual sweeps", source="self") is None
