"""Per-ORG spend caps and job rate limits.

In multitenant mode SECOND_BRAIN_PATH is re-namespaced per persona
(tenants/<org>/second_brain/personas/<slug>), so a cloud_usage.json / job_rate.json
resolved under it gave every persona of an org its own full daily budget and job
window (N personas = N× the cap). Both files now live at the org root there, with a
one-time fold of today's per-persona totals. Local mode also rewrites the env per
persona (brain/run.py) and must stay byte-identical, so the walk-up is gated on
BRAIN_MULTITENANT.
"""

from __future__ import annotations

import datetime as _dt
import json
import time

import brain.model_router as mr
from brain.clusters.motor_cortex import MotorCortexCluster

TODAY = _dt.date.today().isoformat()
YESTERDAY = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()


def _persona_root(tmp_path, slug="x"):
    root = tmp_path / "sb" / "personas" / slug
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ── cloud_usage.json ─────────────────────────────────────────────────────────────


def test_cloud_usage_path_walks_up_to_the_org_root_in_multitenant_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(_persona_root(tmp_path)))
    assert mr._resolve_cloud_usage_path() == str(tmp_path / "sb" / "cloud_usage.json")


def test_cloud_usage_path_is_unchanged_in_local_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    root = _persona_root(tmp_path)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(root))
    assert mr._resolve_cloud_usage_path() == str(root / "cloud_usage.json")
    # No migration side effect either.
    assert not (tmp_path / "sb" / "cloud_usage.json").exists()


def test_cloud_usage_migration_sums_todays_persona_files_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(_persona_root(tmp_path, "a")))
    sb = tmp_path / "sb"
    _write(
        sb / "personas" / "a" / "cloud_usage.json",
        {"date": TODAY, "usd": 1.25, "usd_autonomous": 0.5, "soft_cleared": ""},
    )
    _write(
        sb / "personas" / "b" / "cloud_usage.json",
        {"date": TODAY, "usd": 2.0, "usd_autonomous": 1.0, "soft_cleared": TODAY},
    )
    _write(
        sb / "personas" / "stale" / "cloud_usage.json",
        {"date": YESTERDAY, "usd": 99.0, "usd_autonomous": 99.0, "soft_cleared": YESTERDAY},
    )
    path = mr._resolve_cloud_usage_path()
    assert path == str(sb / "cloud_usage.json")
    data = json.loads((sb / "cloud_usage.json").read_text())
    assert data["date"] == TODAY
    assert abs(data["usd"] - 3.25) < 1e-9
    assert abs(data["usd_autonomous"] - 1.5) < 1e-9
    assert data["soft_cleared"] == TODAY
    # The persona files are left alone (they are simply no longer read).
    assert (sb / "personas" / "a" / "cloud_usage.json").exists()


def test_cloud_usage_migration_skips_when_the_org_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(_persona_root(tmp_path, "a")))
    sb = tmp_path / "sb"
    _write(sb / "cloud_usage.json", {"date": TODAY, "usd": 0.1, "usd_autonomous": 0.0})
    _write(sb / "personas" / "a" / "cloud_usage.json", {"date": TODAY, "usd": 5.0})
    mr._resolve_cloud_usage_path()
    assert json.loads((sb / "cloud_usage.json").read_text())["usd"] == 0.1


def test_cloud_usage_migration_writes_nothing_without_persona_files(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(_persona_root(tmp_path, "a")))
    mr._resolve_cloud_usage_path()
    assert not (tmp_path / "sb" / "cloud_usage.json").exists()


def test_cloud_usage_path_at_the_org_root_already_needs_no_walk_up(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    sb = tmp_path / "sb"
    sb.mkdir()
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(sb))
    assert mr._resolve_cloud_usage_path() == str(sb / "cloud_usage.json")


# ── job_rate.json ────────────────────────────────────────────────────────────────


def _motor() -> MotorCortexCluster:
    return MotorCortexCluster.__new__(MotorCortexCluster)


def test_job_rate_path_per_org_in_multitenant_mode_and_per_path_otherwise(tmp_path, monkeypatch):
    root = _persona_root(tmp_path)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(root))
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    assert _motor()._job_rate_path() == str(tmp_path / "sb" / "job_rate.json")
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    assert _motor()._job_rate_path() == str(root / "job_rate.json")


def test_load_job_starts_unions_persona_files_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(_persona_root(tmp_path, "a")))
    sb = tmp_path / "sb"
    now = time.time()
    _write(sb / "personas" / "a" / "job_rate.json", {"window_starts": [now - 60.0, now - 30.0]})
    _write(
        sb / "personas" / "b" / "job_rate.json",
        {"window_starts": [now - 45.0, now - 10 * 86400.0]},  # the old one is pruned
    )
    m = _motor()
    assert m._load_job_starts() == sorted([now - 60.0, now - 45.0, now - 30.0])
    # Once the org file exists it is the only source: the persona files no longer count.
    m._job_start_times = [now - 5.0]
    m._save_job_starts()
    assert json.loads((sb / "job_rate.json").read_text())["window_starts"] == [now - 5.0]
    assert _motor()._load_job_starts() == [now - 5.0]


def test_load_job_starts_is_empty_in_local_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    root = _persona_root(tmp_path)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(root))
    _write(root / "job_rate.json", {"window_starts": [time.time()]})
    assert _motor()._load_job_starts() == []
