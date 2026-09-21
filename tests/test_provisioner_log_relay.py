"""Tenant log relay levels + the gateway-injected-keys marker (audit 2026-09-13).

The relay used to emit every tenant line at INFO; the child writes everything to
stderr so Railway tagged all of it severity=error and the dashboard's error filter
matched every line. And every tenant re-ran the vault RPC at boot on an org JWT
that has no grant for it — 36 "permission denied for function get_user_api_keys"
errors a day for keys the gateway had already injected.
"""

from __future__ import annotations

import json
import logging
import sys
import time

import brain.provisioner as pv


def test_relay_level_parses_the_child_format():
    lvl = pv.relay_level
    assert lvl("2026-09-13 10:00:00,000 brain.dmn WARNING pod asleep") == (logging.WARNING, False)
    assert lvl("2026-09-13 10:00:00,000 brain.run ERROR boom") == (logging.ERROR, False)
    assert lvl("2026-09-13 10:00:00,000 brain.x CRITICAL down") == (logging.CRITICAL, False)
    assert lvl("2026-09-13 10:00:00,000 brain.x DEBUG detail") == (logging.DEBUG, False)
    assert lvl("2026-09-13 10:00:00,000 brain.x INFO ok") == (logging.INFO, False)
    assert lvl("plain print() output") == (logging.INFO, False)
    # Tracebacks and their continuation lines relay at ERROR until the next record.
    assert lvl("Traceback (most recent call last):") == (logging.ERROR, True)
    assert lvl('  File "x.py", line 1, in <module>', in_traceback=True) == (logging.ERROR, True)
    assert lvl("ValueError: bad", in_traceback=True) == (logging.ERROR, True)
    assert lvl("2026-09-13 10:00:01,000 brain.x INFO recovered", in_traceback=True) == (
        logging.INFO,
        False,
    )
    # A level word inside the MESSAGE (past the header) does not lie about the level.
    assert lvl("2026-09-13 10:00:00,000 brain.x INFO " + "x" * 200 + " ERROR")[0] == logging.INFO


class _Proc:
    def __init__(self, lines):
        self.stdout = iter(lines)


def test_relay_thread_logs_each_line_at_its_level(caplog):
    caplog.set_level(logging.DEBUG, logger="brain.provisioner")
    lines = [
        "2026-09-13 10:00:00,000 brain.run INFO booted\n",
        "2026-09-13 10:00:00,000 brain.dmn WARNING pod asleep\n",
        "Traceback (most recent call last):\n",
        '  File "x.py", line 1\n',
        "RuntimeError: boom\n",
        "2026-09-13 10:00:01,000 brain.run INFO recovered\n",
    ]
    pv._start_log_relay(_Proc(lines), "tenant-abcdef12")
    deadline = time.time() + 3.0
    while time.time() < deadline:
        recs = [r for r in caplog.records if "[tenant:tenant-a]" in r.getMessage()]
        if len(recs) == len(lines):
            break
        time.sleep(0.02)
    assert [r.levelno for r in recs] == [
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.ERROR,
        logging.ERROR,
        logging.INFO,
    ]


def _launch(tmp_path, monkeypatch, fetch):
    import brain.gateway.org_token as ot
    import brain.vault as vault

    monkeypatch.setattr(pv, "TENANTS_DIR", tmp_path)
    uid = "tenant-marker"
    root = tmp_path / uid
    (root / "second_brain").mkdir(parents=True)
    (root / "settings.json").write_text(json.dumps({"persona_name": "the_analyst"}))
    monkeypatch.setattr(ot, "mint_org_token", lambda _uid: "")
    monkeypatch.setattr(vault, "fetch_org_keys", fetch)
    seen: dict = {}

    def _builder(_port, env):
        seen.update(env)
        return [sys.executable, "-c", "pass"]

    prov = pv.Provisioner(cmd_builder=_builder)
    proc, _port, _api = prov._build_and_launch(uid)
    proc.terminate()
    return seen


def test_spawn_marks_injected_keys_for_the_child(tmp_path, monkeypatch):
    env = _launch(tmp_path, monkeypatch, lambda _uid: {"deepgram": "d"})
    assert env.get("BRAIN_TENANT_KEYS_INJECTED") == "1"
    assert env.get("DEEPGRAM_API_KEY") == "d"


def test_spawn_marks_even_when_the_vault_holds_no_keys(tmp_path, monkeypatch):
    env = _launch(tmp_path, monkeypatch, lambda _uid: {})
    assert env.get("BRAIN_TENANT_KEYS_INJECTED") == "1"


def test_spawn_leaves_the_marker_off_when_the_vault_fetch_fails(tmp_path, monkeypatch):
    def _boom(_uid):
        raise RuntimeError("vault down")

    env = _launch(tmp_path, monkeypatch, _boom)
    assert "BRAIN_TENANT_KEYS_INJECTED" not in env


def test_tenant_skips_the_vault_reload_when_the_gateway_injected(monkeypatch):
    import brain.run as brun

    monkeypatch.setenv("BRAIN_USER_ID", "u-1")
    monkeypatch.delenv("BRAIN_TENANT_KEYS_INJECTED", raising=False)
    assert brun._vault_reload_wanted() is True  # hand-launched pod: reload
    monkeypatch.setenv("BRAIN_TENANT_KEYS_INJECTED", "1")
    assert brun._vault_reload_wanted() is False  # gateway child: skip
    monkeypatch.delenv("BRAIN_USER_ID")
    assert brun._vault_reload_wanted() is False  # local single-user dev: never
