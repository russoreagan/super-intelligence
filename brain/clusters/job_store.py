"""
JobStore — persistent storage for completed job outputs.

Each completed job is saved as a JSON file under second_brain/jobs/.
Tracks goal, steps, full tool outputs, any files written during the job,
and the spoken summary generated after completion.

Cleanup runs after every save: trims oldest jobs when count or total size
exceeds configured limits. Generated files (write_file targets) are tracked
by path but never deleted by the store — they're user content.

Config (env vars override settings.json):
  BRAIN_JOB_STORE_MAX_JOBS — max number of job files to keep (default 100)
  BRAIN_JOB_STORE_MAX_MB   — max total size in MB (default 100)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from brain.second_brain.store import SECOND_BRAIN_ROOT

logger = logging.getLogger(__name__)

JOBS_DIR = SECOND_BRAIN_ROOT / "jobs"

_DEFAULT_MAX_JOBS = 100
_DEFAULT_MAX_MB = 100

# How long the cached job-file listing stays valid between scans. Short enough that a
# just-written job is picked up promptly even without explicit invalidation; long
# enough to coalesce a burst of scans within one research/idle pass.
_LISTING_TTL_S = 2.0


def _max_jobs() -> int:
    try:
        from brain.settings import settings

        return int(
            os.environ.get("BRAIN_JOB_STORE_MAX_JOBS")
            or settings.get("job_store_max_jobs", _DEFAULT_MAX_JOBS)
        )
    except Exception:
        return _DEFAULT_MAX_JOBS


def _max_bytes() -> int:
    try:
        from brain.settings import settings

        mb = int(
            os.environ.get("BRAIN_JOB_STORE_MAX_MB")
            or settings.get("job_store_max_mb", _DEFAULT_MAX_MB)
        )
        return mb * 1024 * 1024
    except Exception:
        return _DEFAULT_MAX_MB * 1024 * 1024


class JobStore:
    """
    Disk-backed store for completed job outputs.

    Files live at second_brain/jobs/{job_id}.json.
    All writes are atomic (tmp → rename). Not thread-safe — asyncio single-thread use only.
    """

    def __init__(self) -> None:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        # Read-path caches. The scan helpers (find_cached_fetch / recent_sources /
        # list_recent) are hit in bursts during research + idle loops and otherwise
        # re-glob + re-read + re-parse EVERY job file on each call. The record cache is
        # keyed by mtime so an unchanged file is parsed once; the listing is cached for
        # a short TTL and invalidated on write/cleanup so a burst doesn't re-stat the dir.
        self._record_cache: dict[str, tuple[float, dict]] = {}
        self._listing: list[Path] | None = None
        self._listing_ts: float = 0.0

    def _job_files(self) -> list[Path]:
        """Job files newest-first, with the directory listing cached for _LISTING_TTL_S.
        A burst of scans (a research loop reusing fetches) reuses one glob instead of
        re-globbing+stat-ing the whole dir per call. Invalidated on every write/cleanup,
        so a just-saved job is visible immediately."""
        now = time.time()
        if self._listing is not None and (now - self._listing_ts) < _LISTING_TTL_S:
            return self._listing
        files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        self._listing = files
        self._listing_ts = now
        # Drop cached records for files that no longer exist (trimmed by _cleanup),
        # keeping the record cache bounded to roughly the live job count.
        if len(self._record_cache) > len(files):
            live = {str(p) for p in files}
            for stale in [k for k in self._record_cache if k not in live]:
                del self._record_cache[stale]
        return files

    def _load_record(self, path: Path) -> dict | None:
        """Parsed job record for `path`, memoised by mtime so an unchanged file is read +
        parsed once across scans. Returns None on a missing/corrupt file. The returned
        dict is shared — callers treat it read-only (the scan paths only read)."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._record_cache.get(str(path))
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            record = json.loads(path.read_text())
        except Exception:
            return None
        self._record_cache[str(path)] = (mtime, record)
        return record

    # ── Write ─────────────────────────────────────────────────────────────────

    def save(
        self,
        job_id: str,
        goal: str,
        steps: list[dict],
        results: list[str],
        success: bool,
        *,
        task_id: str | None = None,
        source: str = "user",
        ralph_mode: bool = False,
        total_attempts: int = 0,
        plan_steps: list[dict] | None = None,
        spoken_summary: str | None = None,
        done: bool = True,
        stories_completed: int = 0,
        productive_steps: int = 0,
        unverified_stories: list[str] | None = None,
        success_criteria: str = "",
        complexity: str = "",
        state: str = "",
        reason_code: str = "",
        reason_human: str = "",
        backoff_s: float = 0.0,
        stories_total: int = 0,
        cloud_usd: float = 0.0,
    ) -> None:
        """Save or overwrite a job record. Triggers cleanup afterward.

        done=False marks an in-progress (resumable) checkpoint written after each
        story; get_resumable() finds these. The final save passes done=True.
        stories_completed + plan_steps are what a resumed run reads to skip
        already-finished stories.

        state/reason_code/reason_human/backoff_s carry the brain.autonomy JobState
        model. Default-safe: when state is unset (legacy callers), the read path
        synthesizes it from `success`/`done` so old records still load.
        """
        written_files = _extract_written_files(steps)
        source_links = _extract_source_links(steps, results)
        # Stamp the persona bound while the job ran (jobs execute inside
        # bind_persona) — sleep's chunk mining groups by this so each persona
        # automatizes ITS OWN recurring tool sequences. Unstamped = home.
        try:
            from brain.persona_key import active_or_home_persona, persona_slug

            persona = persona_slug(active_or_home_persona())
        except Exception:
            persona = ""
        resolved_state = state or (
            "running" if not done else ("completed" if success else "failed")
        )
        # A failed record must never persist reasonless (legacy callers predate the
        # JobOutcome guarantee) — the durable table otherwise shows a bare 'failed'.
        if resolved_state == "failed" and not reason_code:
            reason_code = "unspecified_failure"
            reason_human = reason_human or "The job failed before a reason was recorded."
        record = {
            "job_id": job_id,
            "task_id": task_id,
            "persona": persona,
            "goal": goal,
            "success": success,
            "done": done,
            "state": resolved_state,
            "reason_code": reason_code,
            "reason_human": reason_human,
            "backoff_s": backoff_s,
            "source": source,
            "ralph_mode": ralph_mode,
            "total_attempts": total_attempts,
            "steps": steps,
            "results": results,
            "written_files": written_files,
            "source_links": source_links,
            "plan_steps": plan_steps or [],
            "stories_completed": stories_completed,
            "stories_total": int(stories_total or len(plan_steps or [])),
            "cloud_usd": round(float(cloud_usd or 0.0), 4),
            "productive_steps": productive_steps,
            "unverified_stories": unverified_stories or [],
            "success_criteria": success_criteria,
            "complexity": complexity,
            "spoken_summary": spoken_summary,
            "created_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
        }
        self._write(job_id, record)
        self._cleanup()
        logger.info(
            "[JobStore] Saved job %s (done=%s, success=%s, %d steps, %d files, %d links)",
            job_id,
            done,
            success,
            len(steps),
            len(written_files),
            len(source_links),
        )

    def update_summary(self, job_id: str, spoken_summary: str) -> bool:
        """Attach a spoken summary to an existing job record. Returns True if found."""
        path = JOBS_DIR / f"{job_id}.json"
        if not path.exists():
            return False
        try:
            record = json.loads(path.read_text())
            record["spoken_summary"] = spoken_summary
            self._write(job_id, record)
            return True
        except Exception as e:
            logger.warning("[JobStore] update_summary failed for %s: %s", job_id, e)
            return False

    def link_task(self, job_id: str, task_id: str) -> bool:
        """Associate a task queue ID with an existing job record."""
        path = JOBS_DIR / f"{job_id}.json"
        if not path.exists():
            return False
        try:
            record = json.loads(path.read_text())
            record["task_id"] = task_id
            self._write(job_id, record)
            return True
        except Exception as e:
            logger.warning("[JobStore] link_task failed for %s: %s", job_id, e)
            return False

    # ── Read ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _synth_state(record: dict) -> str:
        """State for a record, synthesizing from legacy success/done when absent so
        pre-migration records still render in the jobs surface."""
        st = record.get("state")
        if st:
            return st
        if not record.get("done", True):
            return "running"
        return "completed" if record.get("success") else "failed"

    def get(self, job_id: str) -> dict | None:
        path = JOBS_DIR / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text())
            record.setdefault("state", self._synth_state(record))
            return record
        except Exception as e:
            logger.warning("[JobStore] get failed for %s: %s", job_id, e)
            return None

    def get_resumable(self, job_id: str) -> dict | None:
        """Return a prior record for job_id ONLY if it was left incomplete
        (done=False) with a usable plan and at least one story still pending.
        Returns None for fresh jobs and for fully-completed prior runs, so a
        normal re-run is never mistaken for a resume.
        """
        record = self.get(job_id)
        if not record:
            return None
        if record.get("done", True):
            return None  # completed normally — not resumable
        plan = record.get("plan_steps") or []
        completed = int(record.get("stories_completed", 0))
        if not plan or completed >= len(plan):
            return None  # nothing left to resume
        return record

    def list_recent(self, limit: int = 20) -> list[dict]:
        """Return the most recent jobs (metadata only, no step results)."""
        out = []
        for f in self._job_files()[:limit]:
            record = self._load_record(f)
            if record is not None:
                out.append(
                    {
                        "job_id": record.get("job_id"),
                        "task_id": record.get("task_id"),
                        "goal": record.get("goal"),
                        "success": record.get("success"),
                        "state": self._synth_state(record),
                        "reason_human": record.get("reason_human", ""),
                        "source": record.get("source"),
                        "written_files": record.get("written_files", []),
                        "source_links": record.get("source_links", []),
                        "spoken_summary": record.get("spoken_summary"),
                        "summary": record.get("spoken_summary") or record.get("reason_human", ""),
                        "created_at": record.get("created_at"),
                        "steps_count": len(record.get("steps", [])),
                    }
                )
        return out

    def recent_sources(self, limit: int = 12, max_urls: int = 40) -> list[dict]:
        """Recently-read external sources across jobs, deduped by URL (newest first).

        Lets the idle loop see what it has already read so it doesn't re-fetch the
        same article or re-research a topic it just covered. Only jobs that actually
        read a source are included. Each entry: {goal, summary, urls:[...]}, where
        `urls` holds only the URLs not already seen in a more-recent job.
        """
        seen: set[str] = set()
        out: list[dict] = []
        for f in self._job_files():
            if len(out) >= limit or len(seen) >= max_urls:
                break
            record = self._load_record(f)
            if record is None:
                continue
            fresh: list[str] = []
            for link in record.get("source_links") or []:
                url = link.get("url") if isinstance(link, dict) else None
                if url and url not in seen:
                    seen.add(url)
                    fresh.append(url)
            if fresh:
                out.append(
                    {
                        "goal": record.get("goal", ""),
                        "summary": record.get("spoken_summary") or "",
                        "urls": fresh,
                    }
                )
        return out

    def find_cached_fetch(self, url: str, max_age_s: float | None = None) -> dict | None:
        """Most recent successfully-fetched content for `url`, if read within the
        freshness window. Lets the executor reuse a page it already pulled instead of
        re-fetching the identical URL. Returns {content, goal, age_s} or None.

        max_age_s defaults to BRAIN_FETCH_CACHE_TTL_S (or 6h) so genuinely stale
        content (news moves) is re-fetched rather than served from a week-old job.
        """
        if not url:
            return None
        if max_age_s is None:
            try:
                max_age_s = float(os.environ.get("BRAIN_FETCH_CACHE_TTL_S", "21600"))
            except Exception:
                max_age_s = 21600.0
        now = time.time()
        for f in self._job_files():
            try:
                age = now - f.stat().st_mtime
            except OSError:
                continue
            if age > max_age_s:
                break  # newest-first: everything past here is older still
            record = self._load_record(f)
            if record is None:
                continue
            steps = record.get("steps") or []
            results = record.get("results") or []
            for i, step in enumerate(steps):
                if step.get("tool") not in _FETCH_TOOLS:
                    continue
                if (step.get("args") or {}).get("url") != url:
                    continue
                content = results[i] if i < len(results) else ""
                if not isinstance(content, str) or not content.strip():
                    continue
                if _looks_like_fetch_error(content):
                    continue
                return {"content": content, "goal": record.get("goal", ""), "age_s": age}
        return None

    @property
    def count(self) -> int:
        return len(list(JOBS_DIR.glob("*.json")))

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _cleanup(self) -> None:
        """Trim oldest job files when count or total size exceeds limits."""
        files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        max_j = _max_jobs()
        max_b = _max_bytes()

        total_bytes = sum(f.stat().st_size for f in files)
        removed = 0

        while files and (len(files) > max_j or total_bytes > max_b):
            oldest = files.pop(0)
            size = oldest.stat().st_size
            try:
                oldest.unlink()
                total_bytes -= size
                removed += 1
            except Exception as e:
                logger.warning("[JobStore] cleanup: could not delete %s: %s", oldest.name, e)

        if removed:
            self._listing = None  # files unlinked → next scan re-globs + prunes record cache
            logger.info("[JobStore] Cleanup removed %d old job file(s)", removed)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _write(self, job_id: str, record: dict) -> None:
        path = JOBS_DIR / f"{job_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2))
        os.replace(tmp, path)
        # Invalidate both caches for this write: the listing (a new/changed file), and
        # this path's parsed record — evicted explicitly (not just by mtime) so an
        # overwrite within the filesystem's mtime resolution can't serve stale content.
        self._listing = None
        self._record_cache.pop(str(path), None)


# ── Helpers ───────────────────────────────────────────────────────────────────

# Tools whose output is fetched external page content (native + cloud-agent names).
_FETCH_TOOLS = frozenset({"fetch_url", "web_fetch"})


def _looks_like_fetch_error(content: str) -> bool:
    """Cheap screen so a cached blocked/failed fetch isn't reused as if it were real
    page content. Only the very start matters — fetch errors lead with a marker."""
    head = content.lstrip()[:80].lower()
    return head.startswith(("[blocked]", "[error", "[fetch failed", "error:")) or (
        "network fetch is disabled" in head
    )


def _extract_written_files(steps: list[dict]) -> list[str]:
    """Collect paths from any write_file or append_file steps."""
    paths: list[str] = []
    for step in steps:
        if step.get("tool") in ("write_file", "append_file"):
            p = (step.get("args") or {}).get("path", "")
            if p and p not in paths:
                paths.append(p)
    return paths


# URL embedded in tool output (search results, fetched-page text, connector JSON).
# Stops at whitespace and common trailing punctuation/brackets so we don't swallow
# closing parens or quotes that surround a link in prose.
_URL_RE = re.compile(r"https?://[^\s)\]}>\"'`]+")
_MAX_SOURCE_LINKS = 25


def _extract_source_links(steps: list[dict], results: list[str]) -> list[dict]:
    """Collect external source URLs the job actually read — web_fetch/read targets
    and links surfaced in web_search / connector outputs — so an idle research job's
    sources are refer-back-able from the durable job record instead of being buried
    in raw step output. Mirrors _extract_written_files. Each entry is {url, via}
    where `via` is the tool that surfaced it (e.g. "web_fetch", "web_search")."""
    seen: set[str] = set()
    links: list[dict] = []

    def _add(url: str, via: str) -> None:
        url = url.rstrip(".,;:!?)]}>\"'`")
        if not url or url in seen:
            return
        seen.add(url)
        links.append({"url": url, "via": via})

    for i, step in enumerate(steps):
        tool = step.get("tool", "") or "result"
        args = step.get("args") or {}
        # Explicit fetch/read targets passed in the call args.
        for key in ("url", "link", "uri", "href"):
            val = args.get(key)
            if isinstance(val, str) and val.startswith("http"):
                _add(val, tool)
        # URLs embedded in this step's output (search hits, connector JSON, page text).
        out = results[i] if i < len(results) else ""
        if isinstance(out, str):
            for match in _URL_RE.findall(out):
                _add(match, tool)
        if len(links) >= _MAX_SOURCE_LINKS:
            break

    return links[:_MAX_SOURCE_LINKS]
