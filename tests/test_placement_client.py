"""
Placement consumer (shared instance side): promoted_personas() reads the org's
placement file with an mtime-checked cache and FAILS OPEN to empty — the shared
instance serving everyone is always safe. Plus the DMN roster contract: promoted
personas are dropped, home never is.
"""

from __future__ import annotations

import json
import os

import brain.placement_client as pc


def _reset(monkeypatch, path):
    monkeypatch.setattr(pc, "_cached", set())
    monkeypatch.setattr(pc, "_cached_at", 0.0)
    monkeypatch.setattr(pc, "_cached_mtime", -1.0)
    if path is None:
        monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    else:
        monkeypatch.setenv("BRAIN_PLACEMENT_FILE", str(path))


def test_no_env_means_nobody_promoted(monkeypatch):
    _reset(monkeypatch, None)
    assert pc.promoted_personas() == set()


def test_missing_file_fails_open(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path / ".placement.json")
    assert pc.promoted_personas() == set()


def test_reads_promoted_and_caches_by_mtime(monkeypatch, tmp_path):
    f = tmp_path / ".placement.json"
    f.write_text(json.dumps({"promoted": ["the_analyst"], "ts": 1}), encoding="utf-8")
    _reset(monkeypatch, f)
    assert pc.promoted_personas() == {"the_analyst"}

    # Same mtime + within TTL → cached (rewrite the file, force TTL expiry but
    # same mtime → still cached; then bump mtime → re-read).
    monkeypatch.setattr(pc, "_cached_at", 0.0)  # expire the TTL
    assert pc.promoted_personas() == {"the_analyst"}

    f.write_text(json.dumps({"promoted": ["the_poet"], "ts": 2}), encoding="utf-8")
    os.utime(f, (os.path.getmtime(f) + 5, os.path.getmtime(f) + 5))
    monkeypatch.setattr(pc, "_cached_at", 0.0)
    assert pc.promoted_personas() == {"the_poet"}


def test_file_removed_means_demoted(monkeypatch, tmp_path):
    f = tmp_path / ".placement.json"
    f.write_text(json.dumps({"promoted": ["the_analyst"]}), encoding="utf-8")
    _reset(monkeypatch, f)
    assert pc.promoted_personas() == {"the_analyst"}
    f.unlink()
    monkeypatch.setattr(pc, "_cached_at", 0.0)
    assert pc.promoted_personas() == set()


def test_dmn_roster_drops_promoted_never_home(monkeypatch, tmp_path):
    """The shared instance's DMN rotation excludes personas hosted by dedicated
    sibling processes; the home persona survives even a confused placement file."""
    from brain.dmn import DefaultModeNetwork

    f = tmp_path / ".placement.json"
    f.write_text(
        json.dumps({"promoted": ["the_analyst", "home_persona"]}), encoding="utf-8"
    )
    _reset(monkeypatch, f)

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn.__dict__["_home"] = "home_persona"

    class _Agents:
        @staticmethod
        def list_agents():
            return [
                {"persona": "the_analyst", "enabled": True},
                {"persona": "the_poet", "enabled": True},
            ]

        @staticmethod
        def effective_tier(p):
            return "full"

    # Cover BOTH import forms: `from brain import agents` resolves the package
    # attribute (set below), while `import brain.agents` reads sys.modules.
    import brain as brain_pkg

    monkeypatch.setattr(brain_pkg, "agents", _Agents, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "brain.agents", _Agents)
    monkeypatch.delenv("BRAIN_PERSONA_PINNED", raising=False)
    roster = dmn._roster()
    assert "home_persona" in roster  # home never dropped
    assert "the_analyst" not in roster  # promoted → dedicated instance owns it
    assert "the_poet" in roster  # unpromoted full persona stays
