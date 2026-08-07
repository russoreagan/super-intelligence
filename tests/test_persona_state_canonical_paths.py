"""
Foundation of elastic persona placement: a persona's volume-backed state must
resolve to ONE canonical directory — tenants/<org>/second_brain/personas/<slug>
— no matter which instance hosts the persona:

  case (a): the org's SHARED instance (home persona H boots it; persona X is
            served by per-turn binding) resolving X via persona_state_root();
  case (b): a DEDICATED Path A instance FOR X (provisioner spawn with persona=X,
            run.py multitenant routing), whose own SECOND_BRAIN_PATH must land
            on the same directory.

If these diverge, promoting an agent to its own brain instance forks its
learned state (ledger, stories, chemistry, chunks) — the exact corruption the
placement design must never allow.
"""

from __future__ import annotations

import json

import brain.provisioner as pv
from brain.persona_key import persona_state_root


def _case_a_root(monkeypatch, org_root) -> str:
    """Shared instance: run.py already routed SECOND_BRAIN_PATH to the HOME
    persona's dir; persona X resolves through persona_state_root()."""
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(org_root / "second_brain" / "personas" / "home"))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home")
    return str(persona_state_root("the_analyst"))


def _case_b_root(monkeypatch, tmp_path, org_root) -> str:
    """Dedicated instance for X: spawn env from the provisioner, then run.py's
    multitenant routing derives the process's SECOND_BRAIN_PATH."""
    import brain.run as brun

    monkeypatch.setattr(pv, "TENANTS_DIR", tmp_path, raising=False)
    # The state root exactly as _build_and_launch injects it for a persona spawn.
    spawn_root = pv.tenant_state_root(org_root.name, "the_analyst")

    # run.py multitenant branch: derive persona_root from the spawned env.
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(spawn_root))
    settings_path = org_root / "personas" / "the_analyst" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"persona_name": "the_analyst"}), encoding="utf-8")
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_analyst")
    brun._route_persona_state()
    import os

    return os.environ["SECOND_BRAIN_PATH"]


def test_persona_state_root_is_placement_agnostic(tmp_path, monkeypatch):
    org_root = tmp_path / "org-1"
    (org_root / "second_brain").mkdir(parents=True)

    a = _case_a_root(monkeypatch, org_root)
    b = _case_b_root(monkeypatch, tmp_path, org_root)

    assert a == b, f"persona state forks across instance types:\n  shared:    {a}\n  dedicated: {b}"
    assert a.endswith(f"personas{'/' if '/' in a else chr(92)}the_analyst")


def test_tenant_state_root_org_canonical(tmp_path, monkeypatch):
    """The provisioner's state root for a persona spawn is the ORG second_brain
    (run.py appends personas/<slug>) — NOT a per-instance tree."""
    monkeypatch.setattr(pv, "TENANTS_DIR", tmp_path, raising=False)
    default_root = pv.tenant_state_root("org-1", None)
    persona_root = pv.tenant_state_root("org-1", "the_analyst")
    assert default_root == persona_root, (
        "persona spawns must share the org-canonical second_brain root"
    )
