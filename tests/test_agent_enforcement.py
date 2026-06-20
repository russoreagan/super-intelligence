"""
Per-agent motor enforcement: with an agent bound, the ToolDispatcher narrows
WITHIN its baked (org/process) ceiling — never widens. No agent bound reproduces
the original behaviour exactly.
"""

from __future__ import annotations

import os
import tempfile

from brain.agent_ctx import bind_agent
from brain.clusters.motor_dispatcher import ToolDispatcher


def _disp(tmp):
    sub = os.path.join(tmp, "projects")
    other = os.path.join(tmp, "other")
    os.makedirs(sub, exist_ok=True)
    os.makedirs(other, exist_ok=True)
    d = ToolDispatcher(
        allowed_paths=[tmp],
        allowed_commands={"git", "ls", "cat"},
        enable_shell=True,
        enable_network=True,
    )
    return d, sub, other


def test_no_agent_uses_baked_ceiling():
    with tempfile.TemporaryDirectory() as tmp:
        d, sub, _ = _disp(tmp)
        ok, _ = d._validate_path(sub)
        assert ok
        assert d._eff_enable_shell() is True
        assert "git" in d._eff_commands()


def test_agent_dir_narrowing_blocks_sibling():
    with tempfile.TemporaryDirectory() as tmp:
        d, sub, other = _disp(tmp)
        # Agent scoped to the projects sub-dir only.
        with bind_agent("p.role", {"motor_allowed_dirs": sub}):
            ok_sub, _ = d._validate_path(sub)
            ok_other, _ = d._validate_path(other)
        assert ok_sub is True  # within the agent's narrowed root
        assert ok_other is False  # sibling under the org root, but outside the agent


def test_agent_cannot_escape_org_root():
    with tempfile.TemporaryDirectory() as tmp:
        d, _, _ = _disp(tmp)
        # Agent tries to grant itself /etc — outside the org ceiling → dropped.
        with bind_agent("p.role", {"motor_allowed_dirs": "/etc"}):
            ok, _ = d._validate_path("/etc/hosts")
        assert ok is False


def test_agent_disables_capability_but_cannot_enable():
    with tempfile.TemporaryDirectory() as tmp:
        d, _, _ = _disp(tmp)
        with bind_agent("p.role", {"motor_enable_shell": 0}):
            assert d._eff_enable_shell() is False
        # org has shell on; an agent value of 1 stays on (no change), never forces on
        d2 = ToolDispatcher(allowed_paths=[tmp], enable_shell=False)
        with bind_agent("p.role", {"motor_enable_shell": 1}):
            assert d2._eff_enable_shell() is False  # org off → stays off


def test_agent_command_intersection():
    with tempfile.TemporaryDirectory() as tmp:
        d, _, _ = _disp(tmp)
        with bind_agent("p.role", {"motor_allowed_commands": "git\nrm"}):
            cmds = d._eff_commands()
        assert cmds == {"git"}  # rm not in org set → excluded
