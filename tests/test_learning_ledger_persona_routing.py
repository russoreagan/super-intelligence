"""Per-persona routing of the learning ledger + Learning-surface reads.

SECOND_BRAIN_PATH is frozen at boot to the HOME persona's root, but records are
stamped with the TURN-BOUND persona (agent lanes, round-robin DMN). The ledger
must route each record to the stamped persona's own file — otherwise every
bound persona's learning lands in the home persona's ledger, the Learning
workspace never lists them, and the home view double-counts their rewards
(found 2026-07-03: the Analyst — the Scheduler App's agent persona — was
invisible in the Learning tab).
"""

import json

import pytest

from brain.observability import learning_ledger, learning_reader


@pytest.fixture
def home_root(tmp_path, monkeypatch):
    root = tmp_path / "tenant" / "personas" / "the_companion"
    root.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(root))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_companion")
    monkeypatch.setattr(learning_ledger, "_line_counts", {})
    return root


def _emit(persona, **fields):
    learning_ledger.append(
        {
            "type": "decision",
            "decision": "reward_emission",
            "persona": persona,
            "signal_type": "self_graded",
            "turn_id": fields.pop("turn_id", "t"),
            **fields,
        }
    )


def test_records_route_to_stamped_personas_file(home_root):
    _emit("the_companion", turn_id="t1")
    _emit("the_analyst", turn_id="t2")
    _emit("The Analyst", turn_id="t3")  # raw display name normalizes to the same slug

    analyst = home_root.parent / "the_analyst" / "learning_ledger.jsonl"
    assert analyst.exists()
    assert len(analyst.read_text().splitlines()) == 2
    assert len((home_root / "learning_ledger.jsonl").read_text().splitlines()) == 1


def test_routed_persona_appears_in_learning_surface(home_root):
    _emit("the_analyst")
    assert "the_analyst" in learning_reader.list_personas()
    mix = learning_reader._reward_mix("the_analyst")
    assert mix["by_signal_type"] == {"self_graded": 1}


def test_legacy_mixed_home_file_filtered_by_stamp(home_root):
    # Pre-routing history: an analyst-stamped record sitting in the HOME file
    # must not count toward the home persona's view...
    _emit("the_companion")
    with (home_root / "learning_ledger.jsonl").open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "decision",
                    "decision": "reward_emission",
                    "persona": "the_analyst",
                    "signal_type": "self_graded",
                    "turn_id": "legacy",
                }
            )
            + "\n"
        )
        # ...while unstamped (pre-ledger) records are kept.
        f.write(
            json.dumps(
                {
                    "type": "decision",
                    "decision": "reward_emission",
                    "signal_type": "self_graded",
                    "turn_id": "prestamp",
                }
            )
            + "\n"
        )
    mix = learning_reader._reward_mix("the_companion")
    assert mix["by_signal_type"] == {"self_graded": 2}


def test_rotation_is_per_file(home_root, monkeypatch):
    monkeypatch.setattr(learning_ledger, "_MAX_LINES", 20)
    monkeypatch.setattr(learning_ledger, "_KEEP_LINES", 10)
    _emit("the_companion")
    for i in range(25):
        _emit("the_analyst", turn_id=f"t{i}")
    analyst = home_root.parent / "the_analyst" / "learning_ledger.jsonl"
    assert len(analyst.read_text().splitlines()) <= 20
    assert len((home_root / "learning_ledger.jsonl").read_text().splitlines()) == 1


def test_bare_root_routes_non_home_under_personas_dir(tmp_path, monkeypatch):
    bare = tmp_path / "second_brain"
    bare.mkdir()
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(bare))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_companion")
    monkeypatch.setattr(learning_ledger, "_line_counts", {})
    _emit("the_companion")
    _emit("the_analyst")
    assert (bare / "learning_ledger.jsonl").exists()
    assert (bare / "personas" / "the_analyst" / "learning_ledger.jsonl").exists()
