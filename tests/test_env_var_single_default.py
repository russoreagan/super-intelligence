"""
Drift guard: BRAIN_SESSION_IDLE_TIMEOUT_S has exactly ONE coded default.

The var once had two — 600 in brain/session_manager.py (dead code from the
pre-provisioner in-process multi-session design, never imported) and 86400 in
brain/provisioner.py (the live abandoned-tenant backstop). Both sites meant the
same thing — keep the brain awake for the DMN after disconnect, reap only when
truly abandoned — but anyone reading the dead file's 600 would conclude tenants
are culled after 10 idle minutes. The dead module was deleted; this test fails
if a second read site (or a different fallback) for the var ever reappears.

On failure: either the new read site should reuse the provisioner's semantic
(import/derive from brain.provisioner instead of re-reading the env var), or it
is a genuinely different timeout and needs its own clearly-named var — then
document it in docs/ENV_VARS.md and update this test.
"""

from __future__ import annotations

import re
from pathlib import Path

BRAIN_DIR = Path(__file__).parent.parent / "brain"

VAR = "BRAIN_SESSION_IDLE_TIMEOUT_S"
EXPECTED_SITE = "provisioner.py"
EXPECTED_DEFAULT = "86400"

_READ_RE = re.compile(
    r"""environ(?:\.get\(|\[)\s*["']""" + VAR + r"""["']\s*(?:,\s*["']([^"']*)["'])?"""
)


def _read_sites() -> list[tuple[str, str | None]]:
    """All (relative path, coded default) pairs where brain/ reads the var."""
    sites = []
    for py in sorted(BRAIN_DIR.rglob("*.py")):
        for m in _READ_RE.finditer(py.read_text()):
            sites.append((str(py.relative_to(BRAIN_DIR)), m.group(1)))
    return sites


def test_session_idle_timeout_has_single_default() -> None:
    sites = _read_sites()
    assert sites == [(EXPECTED_SITE, EXPECTED_DEFAULT)], (
        f"{VAR} must be read in exactly one place ({EXPECTED_SITE}, default "
        f"{EXPECTED_DEFAULT}); found {sites}. See this test's docstring for how "
        "to resolve."
    )


def test_dead_session_manager_stays_deleted() -> None:
    """brain/session_manager.py was dead code whose stale default caused the
    original conflict — it must not silently return without being wired in."""
    assert not (BRAIN_DIR / "session_manager.py").exists(), (
        "brain/session_manager.py was deleted as dead code (never imported); if "
        "it is being revived, wire it into an entrypoint and give its timeout "
        "its own env var instead of reusing " + VAR
    )
