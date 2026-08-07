"""
Phase 4C — configure ≠ switch.

The decoupled persona-config save (POST /settings with `config_persona`) persists a
persona's resting/boot chemistry to its OWN file so the edit sticks and applies the
next time you switch to it — WITHOUT switching/restarting the running brain. These
cover the storage primitive that branch relies on: a config write lands on the target
persona's file only, never leaking into another persona, and reads back intact.
"""

from __future__ import annotations

import brain.persona_chem as pc


def test_config_save_persists_target_persona_chem(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_PERSONAS_ROOT", tmp_path)
    # Mirror the server's config_persona branch: write resting + boot for a persona.
    resting = dict.fromkeys(pc.CHANNELS, 0.4)
    resting["OXT"] = 0.77  # a distinctive edit
    pc.save_resting("The Empath", resting)
    pc.save_current("The Empath", dict.fromkeys(pc.CHANNELS, 0.33), {})

    assert pc.exists("The Empath")
    loaded = pc.load("The Empath")
    assert loaded is not None
    # The edited resting baseline persisted and reads back — so a later switch boots
    # The Empath from this config, not a stale/default profile.
    assert round(loaded["resting"]["OXT"], 2) == 0.77


def test_config_save_does_not_leak_to_other_personas(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_PERSONAS_ROOT", tmp_path)
    pc.save_resting("The Empath", dict.fromkeys(pc.CHANNELS, 0.5))
    # Configuring one persona must never write another's file (persona isolation).
    assert pc.exists("The Empath")
    assert not pc.exists("The Analyst")
