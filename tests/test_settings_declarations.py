"""Settings declarations + the console catalogue stay in sync (audit 2026-09-13).

`api_key_openai` had an API_KEY_ENV mapping but no DEFAULTS entry, so a user-typed
OpenAI key was dropped by Settings.update/_load. The five motor timing keys were
documented as a settings.json tier but never declared, so a settings.json entry
was silently ignored. And the console catalogue carried a row for a key nothing
reads, plus admin-only rows without the adminOnly flag.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import brain.settings as bs
from brain.org_permissions import ADMIN_ONLY_KEYS
from brain.settings import API_KEY_ENV, DEFAULTS

REPO = Path(__file__).parent.parent


def test_every_api_key_env_mapping_has_a_default():
    missing = sorted(k for k in API_KEY_ENV if k not in DEFAULTS)
    assert not missing, f"API_KEY_ENV keys without a DEFAULTS entry: {missing}"
    assert DEFAULTS["api_key_openai"] == ""


def test_motor_timing_keys_match_the_literals_motor_cortex_passes():
    src = (REPO / "brain" / "clusters" / "motor_cortex.py").read_text()
    for key in (
        "tool_timeout_seconds",
        "tool_retries",
        "planner_timeout_seconds",
        "planner_retries",
        "job_timeout_seconds",
    ):
        m = re.search(rf'_brain_settings\.get\("{key}",\s*([0-9]+)\)', src)
        assert m, f"motor_cortex no longer reads {key} with a literal fallback"
        assert DEFAULTS[key] == int(m.group(1)), key


def test_openai_key_and_motor_keys_survive_update_save_and_reload(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(bs, "SETTINGS_PATH", path)
    s = bs.Settings()
    s.update({"api_key_openai": "sk-test", "tool_timeout_seconds": 90, "planner_retries": "5"})
    assert s.get("api_key_openai") == "sk-test"
    assert s.get("tool_timeout_seconds") == 90 and s.get("planner_retries") == 5
    s.save()
    fresh = bs.Settings()
    assert fresh.get("api_key_openai") == "sk-test"
    assert fresh.get("tool_timeout_seconds") == 90
    assert fresh.get("job_timeout_seconds") == 1800  # untouched default still declared


def test_apply_api_key_overrides_exports_the_openai_key(monkeypatch):
    monkeypatch.setitem(bs.settings._data, "api_key_openai", "sk-openai-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    bs.apply_api_key_overrides()
    assert os.environ.get("OPENAI_API_KEY") == "sk-openai-test"


# ── console catalogue (brain/ui/settings-data.js) ─────────────────────────────

_CAT_RE = re.compile(r"^\s{6}id: '([a-z]+)', name: '[^']*'.*$")
_ROW_RE = re.compile(r"\{\s*type:\s*'([^']*)',(?:\s*virtual:\s*true,)?\s*key:\s*'([^']+)'([^\n]*)")


def _catalogue_rows() -> list[tuple[str, str, str, str, bool]]:
    """(category id, row type, key, rest-of-line, member_reachable) per row.
    Categories flagged `system: true` / `motor: true` render only inside the
    org-admin Operational page (settings-ui.js renderOperational)."""
    out = []
    cat, reachable = "", True
    for line in (REPO / "brain" / "ui" / "settings-data.js").read_text().splitlines():
        m = _CAT_RE.match(line)
        if m:
            cat = m.group(1)
            reachable = "system: true" not in line and "motor: true" not in line
            continue
        for typ, key, rest in _ROW_RE.findall(line):
            out.append((cat, typ, key, rest, reachable))
    return out


def test_catalogue_keys_are_declared_settings():
    rows = _catalogue_rows()
    assert len(rows) > 150
    unknown = sorted({k for _c, typ, k, _r, _m in rows if typ != "master" and k not in DEFAULTS})
    assert not unknown, f"settings-data.js rows for undeclared keys: {unknown}"
    assert "bg_cloud_token_budget" not in {k for _c, _t, k, _r, _m in rows}


def test_member_reachable_admin_only_rows_carry_the_flag():
    rows = _catalogue_rows()
    bare = sorted(
        {
            k
            for _c, _t, k, rest, reachable in rows
            if reachable and k in ADMIN_ONLY_KEYS and "adminOnly: true" not in rest
        }
    )
    assert not bare, f"admin-only keys shown to members without adminOnly: {bare}"


def test_named_audit_rows_are_flagged_and_present():
    by_key = {k: (rest, c) for c, _t, k, rest, _m in _catalogue_rows()}
    for key in (
        "ralph_max_total_attempts",
        "self_model_deid",
        "partner_cloud_daily_usd_budget",
        "motor_auto_confirm_writes",
    ):
        assert key in by_key, key
        assert "adminOnly: true" in by_key[key][0], key
        assert key in ADMIN_ONLY_KEYS, key
