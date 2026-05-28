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
from datetime import UTC, datetime
from pathlib import Path

from brain.second_brain.store import SECOND_BRAIN_ROOT

logger = logging.getLogger(__name__)

JOBS_DIR = SECOND_BRAIN_ROOT / "jobs"

_DEFAULT_MAX_JOBS = 100
_DEFAULT_MAX_MB = 100


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
    ) -> None:
        """Save or overwrite a job record. Triggers cleanup afterward."""
        written_files = _extract_written_files(steps)
        record = {
            "job_id": job_id,
            "task_id": task_id,
            "goal": goal,
            "success": success,
            "source": source,
            "ralph_mode": ralph_mode,
            "total_attempts": total_attempts,
            "steps": steps,
            "results": results,
            "written_files": written_files,
            "plan_steps": plan_steps or [],
            "spoken_summary": spoken_summary,
            "created_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
        }
        self._write(job_id, record)
        self._cleanup()
        logger.info("[JobStore] Saved job %s (success=%s, %d steps, %d files)",
                    job_id, success, len(steps), len(written_files))

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

    def get(self, job_id: str) -> dict | None:
        path = JOBS_DIR / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logger.warning("[JobStore] get failed for %s: %s", job_id, e)
            return None

    def list_recent(self, limit: int = 20) -> list[dict]:
        """Return the most recent jobs (metadata only, no step results)."""
        files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for f in files[:limit]:
            try:
                record = json.loads(f.read_text())
                out.append({
                    "job_id": record.get("job_id"),
                    "task_id": record.get("task_id"),
                    "goal": record.get("goal"),
                    "success": record.get("success"),
                    "source": record.get("source"),
                    "written_files": record.get("written_files", []),
                    "spoken_summary": record.get("spoken_summary"),
                    "created_at": record.get("created_at"),
                    "steps_count": len(record.get("steps", [])),
                })
            except Exception:
                pass
        return out

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
            logger.info("[JobStore] Cleanup removed %d old job file(s)", removed)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _write(self, job_id: str, record: dict) -> None:
        path = JOBS_DIR / f"{job_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2))
        os.replace(tmp, path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_written_files(steps: list[dict]) -> list[str]:
    """Collect paths from any write_file or append_file steps."""
    paths: list[str] = []
    for step in steps:
        if step.get("tool") in ("write_file", "append_file"):
            p = (step.get("args") or {}).get("path", "")
            if p and p not in paths:
                paths.append(p)
    return paths
