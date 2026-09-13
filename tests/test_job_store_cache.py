"""JobStore read-path caches (listing + mtime-keyed record cache) must stay correct:
a just-saved job is visible immediately, an overwrite is never served stale, and a
trimmed file is pruned. The caches only exist to avoid re-globbing + re-parsing every
job file on each scan (find_cached_fetch / recent_sources run in bursts)."""

from __future__ import annotations

import brain.clusters.job_store as js_mod
from brain.clusters.job_store import JobStore


def _fetch_job(store, job_id, url, content):
    store.save(
        job_id,
        f"read {url}",
        steps=[{"tool": "fetch_url", "args": {"url": url}}],
        results=[content],
        success=True,
    )


def test_find_cached_fetch_sees_new_save_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    assert store.find_cached_fetch("https://example.com") is None  # primes the listing cache
    _fetch_job(store, "job_1", "https://example.com", "PAGE BODY")
    hit = store.find_cached_fetch("https://example.com")
    assert hit is not None
    assert hit["content"] == "PAGE BODY"


def test_recent_sources_reflects_saved_links(tmp_path, monkeypatch):
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    _fetch_job(store, "job_1", "https://a.com/x", "BODY A")
    sources = store.recent_sources()
    urls = {u for entry in sources for u in entry["urls"]}
    assert "https://a.com/x" in urls


def test_overwrite_not_served_stale(tmp_path, monkeypatch):
    """update_summary overwrites the record; the cache must not return the old parse,
    even if the overwrite lands within the filesystem's mtime resolution."""
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    store.save("job_1", "goal", steps=[], results=[], success=True)
    assert store.list_recent()[0]["spoken_summary"] is None  # caches the parsed record
    store.update_summary("job_1", "all done")
    assert store.list_recent()[0]["spoken_summary"] == "all done"


def test_record_cache_prunes_deleted_files(tmp_path, monkeypatch):
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    _fetch_job(store, "job_1", "https://a.com", "BODY")
    store.list_recent()  # populate record cache
    assert str(tmp_path / "job_1.json") in store._record_cache
    # Delete the file out from under the store, then force a fresh listing.
    (tmp_path / "job_1.json").unlink()
    store._listing = None
    store._job_files()  # re-globs and prunes stale record-cache entries
    assert str(tmp_path / "job_1.json") not in store._record_cache


def _local_self_job(store, job_id, goal):
    store.save(
        job_id,
        goal,
        steps=[{"tool": "list_files", "args": {"path": "/docs"}}],
        results=["README.md\nSETTINGS.md"],
        success=True,
        source="self",
    )


def test_recent_sources_includes_linkless_self_jobs(tmp_path, monkeypatch):
    # A finished self job that only read local files has no source_links, so it was
    # invisible to the DMN's "already researched" block — and got re-queued.
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    _local_self_job(store, "job_self", "Read the app's own docs and settings surfaces")
    _fetch_job(store, "job_link", "https://a.com/x", "BODY A")
    entries = store.recent_sources()
    by_goal = {e["goal"]: e for e in entries}
    assert by_goal["Read the app's own docs and settings surfaces"]["urls"] == []
    assert by_goal["Read the app's own docs and settings surfaces"]["age_s"] >= 0.0
    assert by_goal["read https://a.com/x"]["urls"] == ["https://a.com/x"]
    # Opt-out keeps the old link-only shape.
    assert all(e["urls"] for e in store.recent_sources(include_self_jobs=False))


def test_recent_sources_linkless_user_jobs_are_not_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    store.save("job_u", "user asked for a listing", steps=[], results=[], success=True)
    assert store.recent_sources() == []
