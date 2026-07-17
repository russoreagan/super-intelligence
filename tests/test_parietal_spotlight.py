"""
Parietal records the global-workspace spotlight (advisory arm of the
thalamus → parietal fan-out).

The thalamus writes its locked-contract spotlight verdict onto
``features["spotlight"]`` before ``parietal.update()`` runs. Parietal RECORDS
what coalition holds the workspace each turn but gates nothing on it. These
tests cover:

  (a) ignited  → focus recorded + a 'current focus' note surfaced
  (b) not ignited / key absent / malformed → None recorded, empty note, and
      every pre-existing output byte-identical to a run without the spotlight
      (the hard no-op guarantee, including the flag-off neutral-verdict path)
  (c) the short rolling record of recent ignited foci
"""

from __future__ import annotations


def _make_parietal():
    from brain.bus import Bus
    from brain.clusters.parietal import ParietalCluster

    return ParietalCluster(Bus())


def _ignited(focus="hippocampus", coalition="memory", sustained=3, salience=0.72) -> dict:
    """A spotlight verdict shaped exactly like the thalamus contract, ignited."""
    return {
        "ignited": True,
        "focus": focus,
        "coalition": coalition,
        "salience": salience,
        "quorum": True,
        "rising": True,
        "sustained_turns": sustained,
        "hot_entities": ["mars"],
        "priorities": {"hippocampus": 0.4, "frontal": 0.5, "occipital": 0.0},
    }


def _neutral() -> dict:
    """The verdict returned when the workspace is disabled or never ignited."""
    return {
        "ignited": False,
        "focus": None,
        "coalition": None,
        "salience": 0.0,
        "quorum": False,
        "rising": False,
        "sustained_turns": 0,
        "hot_entities": [],
        "priorities": {"hippocampus": 0.0, "frontal": 0.5, "occipital": 0.0},
    }


# ── (a) ignited: focus recorded + note surfaced ───────────────────────────────


class TestSpotlightIgnited:
    def test_records_focus_when_ignited(self):
        p = _make_parietal()
        features = {"intent": "task", "spotlight": _ignited()}
        p.update(features, "tell me about mars", "sure")

        rec = p.last_workspace_focus()
        assert rec is not None
        assert rec["focus"] == "hippocampus"
        assert rec["coalition"] == "memory"
        assert rec["sustained_turns"] == 3
        assert rec["salience"] == 0.72
        assert rec["turn"] == p.turn_count

    def test_note_surfaced_when_ignited(self):
        p = _make_parietal()
        p.update({"spotlight": _ignited(sustained=3)}, "u", "r")
        note = p.workspace_focus_note()
        assert note != ""
        assert "memory" in note
        assert "hippocampus" in note
        assert "held 3 turns" in note

    def test_note_omits_hold_count_for_single_turn(self):
        p = _make_parietal()
        p.update({"spotlight": _ignited(sustained=1)}, "u", "r")
        note = p.workspace_focus_note()
        assert note != ""
        assert "held" not in note

    def test_history_records_ignited_foci(self):
        p = _make_parietal()
        p.update({"spotlight": _ignited(focus="occipital", coalition="vision")}, "u1", "r1")
        p.update({"spotlight": _ignited(focus="hippocampus", coalition="memory")}, "u2", "r2")
        foci = p.recent_workspace_foci()
        assert [f["focus"] for f in foci] == ["occipital", "hippocampus"]


# ── (b) not ignited: None recorded, empty note, and NO-OP on prior outputs ─────


class TestSpotlightNeutralIsNoOp:
    def test_neutral_records_none_and_empty_note(self):
        p = _make_parietal()
        p.update({"spotlight": _neutral()}, "hello", "hi")
        assert p.last_workspace_focus() is None
        assert p.workspace_focus_note() == ""
        assert p.recent_workspace_foci() == []

    def test_absent_key_records_none_and_empty_note(self):
        p = _make_parietal()
        p.update({"intent": "chat"}, "hello", "hi")  # no 'spotlight' key at all
        assert p.last_workspace_focus() is None
        assert p.workspace_focus_note() == ""
        assert p.recent_workspace_foci() == []

    def test_malformed_spotlight_is_ignored(self):
        p = _make_parietal()
        p.update({"spotlight": "not-a-dict"}, "hello", "hi")
        assert p.last_workspace_focus() is None
        assert p.workspace_focus_note() == ""

    def test_neutral_vs_absent_are_byte_identical_on_every_prior_output(self):
        """Presence of a neutral spotlight must not perturb any pre-existing
        output. Feed identical turns to two parietals — one always gets a neutral
        verdict on features, the other never gets a spotlight key at all — and
        assert every observable that existed before this feature is equal."""
        turns = [
            ({"intent": "chat", "entities": ["mars"], "topic_summary": "space"}, "hi", "hello"),
            ({"intent": "task", "entities": ["moon"], "topic_summary": "orbit"}, "go", "ok"),
            ({"intent": "chat", "entities": [], "topic_summary": None}, "cool", "yep"),
        ]

        p_neutral = _make_parietal()
        p_absent = _make_parietal()
        for feats, user, resp in turns:
            with_neutral = dict(feats, spotlight=_neutral())
            p_neutral.update(with_neutral, user, resp)
            p_absent.update(dict(feats), user, resp)  # no spotlight key

        # Every pre-existing observable is identical.
        assert p_neutral.session_summary() == p_absent.session_summary()
        assert p_neutral.recent_turns_text() == p_absent.recent_turns_text()
        assert p_neutral.recent_turns() == p_absent.recent_turns()
        assert p_neutral.turn_count == p_absent.turn_count
        assert p_neutral._entities == p_absent._entities
        assert list(p_neutral._ring) == list(p_absent._ring)

        # And both recorded nothing for the workspace.
        assert p_neutral.last_workspace_focus() is None
        assert p_absent.last_workspace_focus() is None
        assert p_neutral.workspace_focus_note() == ""
        assert p_absent.workspace_focus_note() == ""

    def test_ignited_then_neutral_clears_current_but_keeps_history(self):
        """A later non-ignited turn resets the *current* focus to None (there is
        no current workspace focus) while the rolling record retains the past
        ignited focus."""
        p = _make_parietal()
        p.update({"spotlight": _ignited(focus="hippocampus", coalition="memory")}, "u1", "r1")
        assert p.last_workspace_focus() is not None
        p.update({"spotlight": _neutral()}, "u2", "r2")
        assert p.last_workspace_focus() is None
        assert p.workspace_focus_note() == ""
        # History still holds the earlier ignited focus.
        assert [f["focus"] for f in p.recent_workspace_foci()] == ["hippocampus"]

    def test_history_respects_maxlen(self):
        from brain.clusters.parietal import FOCUS_HISTORY_SIZE

        p = _make_parietal()
        for i in range(FOCUS_HISTORY_SIZE + 4):
            p.update({"spotlight": _ignited(focus=f"c{i}")}, "u", "r")
        assert len(p.recent_workspace_foci(n=100)) <= FOCUS_HISTORY_SIZE
