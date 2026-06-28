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
