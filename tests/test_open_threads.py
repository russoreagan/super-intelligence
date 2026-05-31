"""Unit tests for the open-threads ledger (B1) — pure markdown round-trip + lifecycle."""

from __future__ import annotations

import brain.open_threads as ot


def test_render_parse_roundtrip():
    threads, t = ot.open_thread(
        [], "Does emotional gating reduce token cost?",
        angle="efficiency-question", bears_on=["efficiency-question"],
        bearing="affects-measurement", now=1000.0,
    )
    body = ot.render_section_body(threads)
    assert "```json" in body
    back = ot.parse_threads(body)
    assert len(back) == 1
    assert back[0].id == t.id
    assert back[0].summary == "Does emotional gating reduce token cost?"
    assert back[0].bears_on == ["efficiency-question"]
    assert back[0].bearing == "affects-measurement"
    assert back[0].opened_ts == 1000.0


def test_parse_empty_and_garbage():
    assert ot.parse_threads("") == []
    assert ot.parse_threads("## Open threads\n(no threads)") == []
    assert ot.parse_threads("```json\nnot valid json\n```") == []


def test_extract_section_ignores_other_sections():
    doc = (
        "# Open Questions & Projects\n\n"
        "## Architecture\n- a hand-written question\n\n"
        "## Open threads\n```json\n[]\n```\n\n"
        "## Projects assigned by Russ\n### Foo\n**Task**: bar\n"
    )
    body = ot.extract_section(doc)
    assert "```json" in body
    assert "hand-written" not in body  # only the Open threads section


def test_advance_increments_and_appends_progress():
    threads, t = ot.open_thread([], "seed", now=1.0)
    threads, adv = ot.advance_thread(threads, t.id, "made progress", now=2.0)
    assert adv.advances == 1
    assert adv.progress == ["made progress"]
    assert adv.last_ts == 2.0


def test_advance_cap_triggers_retirement_flag():
    threads, t = ot.open_thread([], "seed", now=1.0)
    for i in range(ot.THREAD_MAX_ADVANCES):
        threads, t = ot.advance_thread(threads, t.id, f"step {i}", now=2.0 + i)
    assert ot.should_retire_for_advances(t)


def test_cap_evicts_oldest_least_advanced():
    threads: list = []
    ids = []
    for i in range(ot.MAX_OPEN_THREADS):
        threads, t = ot.open_thread(threads, f"seed {i}", now=float(i))
        ids.append(t.id)
    # Give the second thread some advances so it is NOT the eviction victim.
    threads, _ = ot.advance_thread(threads, ids[0], "keep me", now=100.0)
    # Opening one more exceeds the cap → evicts oldest-least-advanced (ids[1], 0 advances, oldest after ids[0]).
    threads, newest = ot.open_thread(threads, "overflow", now=200.0)
    assert len(threads) == ot.MAX_OPEN_THREADS
    assert ot.find(threads, ids[0]) is not None  # advanced one survived
    assert ot.find(threads, ids[1]) is None       # oldest-least-advanced evicted
    assert ot.find(threads, newest.id) is not None


def test_reap_aged_enforces_wallclock():
    now = 1_000_000.0
    threads, fresh = ot.open_thread([], "fresh", now=now)
    threads, old = ot.open_thread(threads, "stale", now=now - ot.THREAD_MAX_AGE_S - 10)
    kept, retired = ot.reap_aged(threads, now=now)
    kept_ids = {t.id for t in kept}
    retired_ids = {t.id for t in retired}
    assert fresh.id in kept_ids
    assert old.id in retired_ids


def test_mark_pending_and_remove():
    threads, t = ot.open_thread([], "uncertain conclusion", now=1.0)
    threads, p = ot.mark_pending(threads, t.id, now=2.0)
    assert p.status == ot.STATUS_PENDING
    threads = ot.remove_thread(threads, t.id)
    assert ot.find(threads, t.id) is None
