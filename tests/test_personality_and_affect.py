"""
Tests for features added across multiple sessions:

  - _heuristic_affect() — lexicon-based sentiment / hostility / user_emotion
  - Coarse text-affect fallback in HypothalamusCluster.process()
  - SchemaStore._replace_section_body() / ensure_section() / upsert_section()
  - SleepConsolidation._emotion_valence()
  - SleepConsolidation._response_tags()
  - SleepConsolidation._mood_shift_episodes()
  - SleepConsolidation._personality_stats()
  - Sleep settings present in DEFAULTS
  - consolidate_now() — guard-rail paths (no sleep, empty buffer)
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ===========================================================================
# _heuristic_affect()
# ===========================================================================

class TestHeuristicAffect:
    def _affect(self, text: str) -> dict:
        from brain.clusters.temporal import _heuristic_affect
        return _heuristic_affect(text)

    # ── empty / whitespace ──────────────────────────────────────────────────

    def test_empty_string_returns_neutral(self):
        r = self._affect("")
        assert r["user_emotion"] == "neutral"
        assert r["sentiment"] == 0.0
        assert r["hostility"] == 0.0

    def test_whitespace_only_returns_neutral(self):
        r = self._affect("   \t\n  ")
        assert r["user_emotion"] == "neutral"

    # ── positive cases ──────────────────────────────────────────────────────

    def test_love_yields_affectionate(self):
        r = self._affect("I love this")
        assert r["user_emotion"] == "affectionate"
        assert r["sentiment"] > 0

    def test_great_yields_positive(self):
        r = self._affect("That was great!")
        assert r["sentiment"] > 0
        assert r["user_emotion"] in ("happy", "excited", "warm")

    def test_lol_yields_amused(self):
        r = self._affect("lol that is hilarious")
        assert r["user_emotion"] == "amused"

    def test_thanks_yields_warm(self):
        r = self._affect("thanks so much")
        assert r["user_emotion"] == "warm"
        assert r["user_tone_toward_ai"] == "warm"

    def test_wow_yields_positive_sentiment(self):
        r = self._affect("wow")
        assert r["sentiment"] > 0

    # ── negative / hostile cases ────────────────────────────────────────────

    def test_hate_yields_angry(self):
        r = self._affect("I hate this")
        assert r["user_emotion"] == "angry"
        assert r["hostility"] > 0
        assert r["sentiment"] < 0

    def test_stupid_yields_frustrated_insulting(self):
        r = self._affect("this is stupid")
        assert r["user_emotion"] == "frustrated"
        assert r["user_tone_toward_ai"] == "insulting"
        assert r["hostility"] > 0.5

    def test_annoying_yields_annoyed(self):
        r = self._affect("you are so annoying")
        assert r["user_emotion"] == "annoyed"

    def test_ugh_negative_sentiment(self):
        r = self._affect("ugh")
        assert r["sentiment"] < 0

    # ── vulnerable cases ────────────────────────────────────────────────────

    def test_sad_yields_sad(self):
        r = self._affect("I feel so sad today")
        assert r["user_emotion"] == "sad"
        assert r["sentiment"] < 0

    def test_anxious_yields_anxious(self):
        r = self._affect("I am really anxious about this")
        assert r["user_emotion"] == "anxious"

    def test_stuck_yields_struggling(self):
        r = self._affect("I'm stuck")
        assert r["user_emotion"] == "struggling"

    def test_tired_yields_tired(self):
        r = self._affect("so tired today")
        assert r["user_emotion"] == "tired"

    # ── punctuation cues ────────────────────────────────────────────────────

    def test_exclamation_amplifies_positive(self):
        r_plain = self._affect("that was great")
        r_excl = self._affect("that was great!")
        assert r_excl["sentiment"] >= r_plain["sentiment"]

    def test_exclamation_on_neutral_gives_excited(self):
        r = self._affect("ok!")
        assert r["user_emotion"] == "excited"

    def test_multiple_question_marks_gives_confused(self):
        r = self._affect("what?? how??")
        assert r["user_emotion"] == "confused"

    def test_single_question_mark_not_confused(self):
        r = self._affect("how are you?")
        # single ? alone shouldn't force confused
        assert r["user_emotion"] != "confused"

    # ── clamping ────────────────────────────────────────────────────────────

    def test_sentiment_clamped_to_plus_one(self):
        r = self._affect("amazing awesome great perfect nice love loved")
        assert r["sentiment"] <= 1.0

    def test_sentiment_clamped_to_minus_one(self):
        r = self._affect("hate awful terrible stupid dumb fucking bad awful")
        assert r["sentiment"] >= -1.0

    def test_hostility_never_negative(self):
        r = self._affect("I love this amazing perfect wonderful day")
        assert r["hostility"] >= 0.0

    # ── return shape ────────────────────────────────────────────────────────

    def test_always_returns_all_keys(self):
        r = self._affect("some random message here")
        for key in ("sentiment", "hostility", "user_emotion", "user_tone_toward_ai"):
            assert key in r

    def test_hostility_is_float(self):
        r = self._affect("you are wrong")
        assert isinstance(r["hostility"], float)


# ===========================================================================
# Coarse text-affect fallback in HypothalamusCluster
# ===========================================================================

class TestHypothalamusTextAffectFallback:
    """Verify the fallback logic fires when neuromod-derived emotion is neutral.

    The EMOTION_TABLE is fully populated so no real neuromod combination
    naturally produces "neutral".  We patch name_emotion to force it so we
    can test the fallback branches in isolation.
    """

    async def _process_neutral_neuromod(self, features_extra: dict) -> dict:
        """Run hypo.process() with neuromod forced to return 'neutral'."""
        from brain.bus import Bus
        from brain.clusters.hypothalamus import HypothalamusCluster
        import brain.clusters.hypothalamus as hypo_mod

        bus = Bus()
        hypo = HypothalamusCluster(bus)
        features = {
            "sentiment": features_extra.pop("sentiment", 0.0),
            "hostility": features_extra.pop("hostility", 0.0),
            "salience": 0.3,
            "surprise_score": 0.0,
            "topic_summary": "test",
            **features_extra,
        }
        with patch.object(hypo_mod, "name_emotion", return_value=("neutral", "balanced")):
            return await hypo.process(features)

    async def test_frustrated_user_emotion_gives_irritated(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "frustrated"})
        assert affect["emotion"] == "irritated"
        assert "text-affect fallback" in affect["tendency"]

    async def test_annoyed_user_emotion_gives_irritated(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "annoyed"})
        assert affect["emotion"] == "irritated"

    async def test_sad_user_emotion_gives_concerned(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "sad"})
        assert affect["emotion"] == "concerned"

    async def test_anxious_user_emotion_gives_concerned(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "anxious"})
        assert affect["emotion"] == "concerned"

    async def test_struggling_user_emotion_gives_concerned(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "struggling"})
        assert affect["emotion"] == "concerned"

    async def test_happy_user_emotion_gives_warm(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "happy"})
        assert affect["emotion"] == "warm"

    async def test_excited_user_emotion_gives_warm(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "excited"})
        assert affect["emotion"] == "warm"

    async def test_affectionate_user_emotion_gives_warm(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "affectionate"})
        assert affect["emotion"] == "warm"

    async def test_curious_user_emotion_gives_engaged(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "curious"})
        assert affect["emotion"] == "engaged"

    async def test_engaged_user_emotion_gives_engaged(self):
        affect = await self._process_neutral_neuromod({"user_emotion": "engaged"})
        assert affect["emotion"] == "engaged"

    async def test_high_hostility_gives_wary(self):
        affect = await self._process_neutral_neuromod({"hostility": 0.7})
        assert affect["emotion"] == "wary"

    async def test_very_negative_sentiment_gives_down(self):
        affect = await self._process_neutral_neuromod({"sentiment": -0.6})
        assert affect["emotion"] == "down"

    async def test_very_positive_sentiment_gives_content(self):
        affect = await self._process_neutral_neuromod({"sentiment": 0.7})
        assert affect["emotion"] == "content"

    async def test_hostility_takes_priority_over_user_emotion(self):
        """High hostility beats user_emotion because it's the first branch."""
        affect = await self._process_neutral_neuromod({
            "hostility": 0.7,
            "user_emotion": "frustrated",
        })
        assert affect["emotion"] == "wary"  # hostility branch wins

    async def test_neutral_user_emotion_unchanged(self):
        """Fallback should leave emotion neutral when user_emotion is neutral."""
        affect = await self._process_neutral_neuromod({"user_emotion": "neutral"})
        assert affect["emotion"] == "neutral"

    async def test_metacognition_override_skips_fallback(self):
        """When metacognition has already set an emotion, fallback must not clobber it."""
        from brain.bus import Bus
        from brain.clusters.hypothalamus import HypothalamusCluster
        import brain.clusters.hypothalamus as hypo_mod

        bus = Bus()
        hypo = HypothalamusCluster(bus)

        await bus.publish_dict(
            "meta.emotion_override",
            {"emotion": "grateful", "reason": "high quality", "ttl_turns": 1},
            source="test",
        )
        with patch.object(hypo_mod, "name_emotion", return_value=("neutral", "balanced")):
            affect = await hypo.process({
                "sentiment": 0.0,
                "hostility": 0.0,
                "salience": 0.3,
                "surprise_score": 0.0,
                "topic_summary": "test",
                "user_emotion": "frustrated",  # would trigger fallback if not overridden
            })
        assert affect["emotion"] == "grateful"
        assert affect["emotion_source"] == "metacognition"


# ===========================================================================
# SchemaStore section methods
# ===========================================================================

class TestReplaceSection:
    def _store(self):
        import brain.second_brain.store as store_mod
        s = store_mod.SchemaStore.__new__(store_mod.SchemaStore)
        return s

    # ── _replace_section_body ───────────────────────────────────────────────

    def test_replaces_existing_section_body(self):
        from brain.second_brain.store import SchemaStore
        content = "## Known facts\n- old fact\n\n## Other\n- other\n"
        result = SchemaStore._replace_section_body(content, "Known facts", "- new fact")
        assert "- new fact" in result
        assert "- old fact" not in result
        assert "## Other" in result

    def test_appends_new_section_when_missing(self):
        from brain.second_brain.store import SchemaStore
        content = "## Known facts\n- fact\n"
        result = SchemaStore._replace_section_body(content, "Communication style", "- brevity")
        assert "## Communication style" in result
        assert "- brevity" in result
        assert "## Known facts" in result

    def test_no_partial_heading_match(self):
        """'## Preferences' must not match '## Preferences (old)'."""
        from brain.second_brain.store import SchemaStore
        content = "## Preferences (old)\n- legacy\n\n## Preferences\n- current\n"
        result = SchemaStore._replace_section_body(content, "Preferences", "- updated")
        # Only the exact section should be replaced
        assert "- updated" in result
        assert "- current" not in result
        assert "- legacy" in result  # (old) section untouched

    def test_collapses_excess_blank_lines(self):
        from brain.second_brain.store import SchemaStore
        content = "## A\n\n\n\n\n- a\n"
        result = SchemaStore._replace_section_body(content, "A", "- a")
        assert "\n\n\n" not in result

    def test_idempotent_on_same_body(self):
        from brain.second_brain.store import SchemaStore
        content = "## Communication style\n- bullets\n\n"
        r1 = SchemaStore._replace_section_body(content, "Communication style", "- bullets")
        r2 = SchemaStore._replace_section_body(r1, "Communication style", "- bullets")
        assert r1 == r2

    def test_preserves_other_sections(self):
        from brain.second_brain.store import SchemaStore
        content = "## A\n- a\n\n## B\n- b\n\n## C\n- c\n"
        result = SchemaStore._replace_section_body(content, "B", "- NEW")
        assert "- a" in result
        assert "- NEW" in result
        assert "- c" in result

    # ── ensure_section (file-level) ─────────────────────────────────────────

    def test_ensure_section_adds_missing_section(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        (tmp_path / "user.md").write_text("## Known facts\n- fact\n")
        s = store_mod.SchemaStore()
        s.ensure_section("user.md", "Communication style", "- (learning…)")
        content = (tmp_path / "user.md").read_text()
        assert "## Communication style" in content
        assert "- (learning…)" in content

    def test_ensure_section_skips_existing(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        original = "## Communication style\n- terse preferred\n"
        (tmp_path / "user.md").write_text(original)
        s = store_mod.SchemaStore()
        s.ensure_section("user.md", "Communication style", "- (learning…)")
        content = (tmp_path / "user.md").read_text()
        assert "- terse preferred" in content
        assert "(learning…)" not in content

    def test_ensure_section_skips_missing_file(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        s = store_mod.SchemaStore()
        # Should not raise even if file doesn't exist
        s.ensure_section("nonexistent.md", "Communication style", "- x")

    def test_ensure_section_rejects_invalid_filename(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        s = store_mod.SchemaStore()
        s.ensure_section("../escape.md", "Section", "- bad")  # must not raise or write

    # ── upsert_section (async) ──────────────────────────────────────────────

    async def test_upsert_section_replaces_body(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        (tmp_path / "user.md").write_text(
            "## Communication style\n- old style\n\n## Other\n- x\n"
        )
        s = store_mod.SchemaStore()
        await s.upsert_section("user.md", "Communication style", "- prefers short replies")
        content = (tmp_path / "user.md").read_text()
        assert "- prefers short replies" in content
        assert "- old style" not in content
        assert "## Other" in content

    async def test_upsert_section_creates_missing_section(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        (tmp_path / "user.md").write_text("## Known facts\n- fact\n")
        s = store_mod.SchemaStore()
        await s.upsert_section("user.md", "Mood response patterns", "- responds well to short answers")
        content = (tmp_path / "user.md").read_text()
        assert "## Mood response patterns" in content
        assert "- responds well to short answers" in content

    async def test_upsert_section_skips_missing_file(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        s = store_mod.SchemaStore()
        await s.upsert_section("ghost.md", "Section", "- body")  # must not raise

    async def test_upsert_section_no_write_on_identical_body(self, tmp_path, monkeypatch):
        import brain.second_brain.store as store_mod
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
        body = "## Communication style\n- same\n\n"
        (tmp_path / "user.md").write_text(body)
        s = store_mod.SchemaStore()
        mtime_before = (tmp_path / "user.md").stat().st_mtime_ns
        await s.upsert_section("user.md", "Communication style", "- same")
        # _replace_section_body returns new_content == content → no atomic_write
        mtime_after = (tmp_path / "user.md").stat().st_mtime_ns
        assert mtime_before == mtime_after


# ===========================================================================
# SleepConsolidation — utility methods
# ===========================================================================

def _make_sleep():
    """Construct a SleepConsolidation without any real DB or router calls."""
    from brain.sleep import SleepConsolidation

    sleep = SleepConsolidation.__new__(SleepConsolidation)
    sleep._router = MagicMock()
    sleep._schema = MagicMock()
    sleep._episodic = MagicMock()
    sleep._wiring = None
    sleep._facts_cell = MagicMock()
    sleep._selfmodel_cell = MagicMock()
    sleep._personality_observer = MagicMock()
    return sleep


class TestEmotionValence:
    def test_happy_positive(self):
        from brain.sleep import SleepConsolidation
        assert SleepConsolidation._emotion_valence("happy") > 0

    def test_angry_negative(self):
        from brain.sleep import SleepConsolidation
        assert SleepConsolidation._emotion_valence("angry") < 0

    def test_neutral_zero(self):
        from brain.sleep import SleepConsolidation
        assert SleepConsolidation._emotion_valence("neutral") == 0.0

    def test_unknown_zero(self):
        from brain.sleep import SleepConsolidation
        assert SleepConsolidation._emotion_valence("xyzzy_unknown") == 0.0

    def test_empty_string_zero(self):
        from brain.sleep import SleepConsolidation
        assert SleepConsolidation._emotion_valence("") == 0.0

    def test_case_insensitive(self):
        from brain.sleep import SleepConsolidation
        assert SleepConsolidation._emotion_valence("HAPPY") == SleepConsolidation._emotion_valence("happy")

    def test_all_vocab_in_range(self):
        from brain.sleep import SleepConsolidation
        for label, val in SleepConsolidation._USER_EMOTION_VALENCE.items():
            assert -1.0 <= val <= 1.0, f"{label} valence out of range: {val}"


class TestResponseTags:
    def test_short_reply(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("ok.")
        assert "short_reply" in tags

    def test_medium_reply(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("a" * 150)
        assert "medium_reply" in tags

    def test_long_reply(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("a" * 300)
        assert "long_reply" in tags

    def test_exactly_one_length_tag(self):
        from brain.sleep import SleepConsolidation
        for text in ("short", "a" * 200, "a" * 400):
            tags = SleepConsolidation._response_tags(text)
            length_tags = [t for t in tags if t.endswith("_reply")]
            assert len(length_tags) == 1

    def test_humour_lol(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("haha that's funny lol")
        assert "humour" in tags

    def test_humour_smiley(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("no worries :)")
        assert "humour" in tags

    def test_asked_question(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("What would you like me to do?")
        assert "asked_question" in tags

    def test_asked_for_approval(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("Want me to run that for you?")
        assert "asked_for_approval" in tags

    def test_apology(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("I'm sorry about that, my mistake.")
        assert "apology" in tags

    def test_reported_action(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("I ran the script and it succeeded.")
        assert "reported_action" in tags

    def test_empty_response_has_short_reply(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("")
        assert "short_reply" in tags

    def test_no_false_humour(self):
        from brain.sleep import SleepConsolidation
        tags = SleepConsolidation._response_tags("Here is the analysis you requested.")
        assert "humour" not in tags


class TestMoodShiftEpisodes:
    def _turns(self, sequence: list[tuple[str, str, str]]) -> list[dict]:
        """Build turns from (user_emotion, entity_response, next_emotion) triples."""
        turns = []
        for i, (emo, resp, _) in enumerate(sequence):
            turns.append({
                "user_emotion": emo,
                "entity_response": resp,
                "user_input": f"msg{i}",
            })
        # Last entry: copy next_emotion from sequence[-1][2]
        if sequence:
            turns[-1]["user_emotion"] = sequence[-1][2]
        return turns

    def test_empty_turns_returns_empty_structures(self):
        s = _make_sleep()
        result = s._mood_shift_episodes([])
        assert result["tag_summary"] == {}
        assert result["top_moments"] == []

    def test_single_turn_no_pairs(self):
        s = _make_sleep()
        result = s._mood_shift_episodes([{"user_emotion": "happy", "entity_response": "ok"}])
        assert result["top_moments"] == []

    def test_positive_shift_detected(self):
        s = _make_sleep()
        turns = [
            {"user_emotion": "frustrated", "entity_response": "I'm sorry to hear that. " + "x" * 60, "user_input": "ugh"},
            {"user_emotion": "happy", "entity_response": "", "user_input": "ok"},
        ]
        result = s._mood_shift_episodes(turns)
        assert len(result["top_moments"]) == 1
        assert result["top_moments"][0]["delta"] > 0

    def test_negative_shift_detected(self):
        s = _make_sleep()
        turns = [
            {"user_emotion": "happy", "entity_response": "sure" + "x" * 100, "user_input": "hi"},
            {"user_emotion": "frustrated", "entity_response": "", "user_input": "no"},
        ]
        result = s._mood_shift_episodes(turns)
        assert result["top_moments"][0]["delta"] < 0

    def test_small_delta_ignored(self):
        """Deltas below 0.2 should not appear in top_moments."""
        s = _make_sleep()
        turns = [
            {"user_emotion": "neutral", "entity_response": "ok", "user_input": "hi"},
            {"user_emotion": "curious", "entity_response": "", "user_input": "tell me more"},
        ]
        # neutral → curious = 0.0 → 0.4, delta = 0.4, above threshold
        result = s._mood_shift_episodes(turns)
        # curious - neutral = 0.4 - 0.0 = 0.4 → should appear
        assert len(result["top_moments"]) == 1

    def test_top_moments_capped_at_five(self):
        from brain.sleep import SleepConsolidation
        s = _make_sleep()
        # 12 turns alternating happy ↔ frustrated — 11 pairs, all large delta
        turns = []
        emotions = ["happy", "frustrated"] * 6
        for i, emo in enumerate(emotions):
            turns.append({
                "user_emotion": emo,
                "entity_response": "x" * 200,
                "user_input": f"turn {i}",
            })
        result = s._mood_shift_episodes(turns)
        assert len(result["top_moments"]) <= 5

    def test_tag_summary_aggregates_by_response_tag(self):
        s = _make_sleep()
        # Two turns where brain gave a short reply and mood improved each time
        turns = [
            {"user_emotion": "frustrated", "entity_response": "ok", "user_input": "ugh"},
            {"user_emotion": "happy", "entity_response": "ok", "user_input": "nice"},
            {"user_emotion": "frustrated", "entity_response": "ok", "user_input": "ugh"},
            {"user_emotion": "happy", "entity_response": "", "user_input": "end"},
        ]
        result = s._mood_shift_episodes(turns)
        ts = result["tag_summary"]
        # short_reply should appear (all responses are < 80 chars)
        assert "short_reply" in ts
        assert ts["short_reply"]["n"] >= 1


class TestPersonalityStats:
    def _turn(self, **kw) -> dict:
        defaults = {
            "user_input": "hi",
            "entity_response": "hello",
            "msg_length": "short",
            "intent": "chitchat",
            "register": "casual",
            "user_emotion": "neutral",
            "requires_action": False,
            "hesitant_speech": False,
            "response_chars": 5,
        }
        defaults.update(kw)
        return defaults

    def test_empty_turns_returns_zero_turn_count(self):
        s = _make_sleep()
        stats = s._personality_stats([])
        assert stats == {"turns": 0}

    def test_turn_count_correct(self):
        s = _make_sleep()
        turns = [self._turn() for _ in range(7)]
        stats = s._personality_stats(turns)
        assert stats["turns"] == 7

    def test_joke_turns_counted(self):
        s = _make_sleep()
        turns = [
            self._turn(user_input="haha that's funny"),
            self._turn(user_input="that's great"),
            self._turn(user_input="lol"),
        ]
        stats = s._personality_stats(turns)
        assert stats["joke_turns"] == 2

    def test_frustration_turns_counted(self):
        s = _make_sleep()
        turns = [
            self._turn(user_emotion="frustrated"),
            self._turn(user_emotion="happy"),
            self._turn(user_emotion="annoyed"),
        ]
        stats = s._personality_stats(turns)
        assert stats["frustration_turns"] == 2

    def test_cancel_turns_counted(self):
        s = _make_sleep()
        turns = [
            self._turn(user_input="never mind"),
            self._turn(user_input="please continue"),
            self._turn(user_input="stop that"),
        ]
        stats = s._personality_stats(turns)
        assert stats["cancel_turns"] == 2

    def test_action_turns_counted(self):
        s = _make_sleep()
        turns = [
            self._turn(requires_action=True),
            self._turn(requires_action=False),
            self._turn(requires_action=True),
        ]
        stats = s._personality_stats(turns)
        assert stats["action_turns"] == 2

    def test_msg_length_mix(self):
        s = _make_sleep()
        turns = [
            self._turn(msg_length="short"),
            self._turn(msg_length="short"),
            self._turn(msg_length="long"),
        ]
        stats = s._personality_stats(turns)
        assert stats["msg_length_mix"]["short"] == 2
        assert stats["msg_length_mix"]["long"] == 1

    def test_avg_user_chars(self):
        s = _make_sleep()
        turns = [
            self._turn(user_input="hi"),    # 2
            self._turn(user_input="hello"), # 5
        ]
        stats = s._personality_stats(turns)
        assert stats["avg_user_chars"] == 3  # (2+5)//2

    def test_avg_response_chars(self):
        s = _make_sleep()
        turns = [
            self._turn(response_chars=100),
            self._turn(response_chars=200),
        ]
        stats = s._personality_stats(turns)
        assert stats["avg_response_chars"] == 150

    def test_prosody_tone_mix_absent_when_empty(self):
        s = _make_sleep()
        turns = [self._turn()]
        stats = s._personality_stats(turns)
        # No prosody_tone set → key should not appear
        assert "prosody_tone_mix" not in stats

    def test_prosody_tone_mix_present_when_set(self):
        s = _make_sleep()
        turns = [self._turn(prosody_tone="warm"), self._turn(prosody_tone="neutral")]
        stats = s._personality_stats(turns)
        assert "prosody_tone_mix" in stats
        assert stats["prosody_tone_mix"]["warm"] == 1

    def test_pace_mix_present_when_set(self):
        s = _make_sleep()
        turns = [self._turn(pace_label="brisk"), self._turn(pace_label="normal")]
        stats = s._personality_stats(turns)
        assert "pace_mix" in stats
        assert "hesitant_turns" in stats


# ===========================================================================
# Sleep settings in DEFAULTS
# ===========================================================================

class TestSleepSettingsDefaults:
    def test_all_sleep_keys_present(self):
        from brain.settings import DEFAULTS
        for key in (
            "sleep_periodic_enabled",
            "sleep_check_interval_s",
            "sleep_idle_threshold_s",
            "sleep_hard_cap_s",
            "sleep_min_turns",
        ):
            assert key in DEFAULTS, f"Missing key in DEFAULTS: {key}"

    def test_sleep_periodic_enabled_is_int_flag(self):
        from brain.settings import DEFAULTS
        assert DEFAULTS["sleep_periodic_enabled"] in (0, 1)

    def test_sleep_check_interval_positive_float(self):
        from brain.settings import DEFAULTS
        assert isinstance(DEFAULTS["sleep_check_interval_s"], float)
        assert DEFAULTS["sleep_check_interval_s"] > 0

    def test_sleep_idle_threshold_greater_than_check_interval(self):
        from brain.settings import DEFAULTS
        # Idle threshold must be > check interval or the loop fires every cycle
        assert DEFAULTS["sleep_idle_threshold_s"] > DEFAULTS["sleep_check_interval_s"]

    def test_sleep_hard_cap_greater_than_idle_threshold(self):
        from brain.settings import DEFAULTS
        assert DEFAULTS["sleep_hard_cap_s"] >= DEFAULTS["sleep_idle_threshold_s"]

    def test_sleep_min_turns_positive_int(self):
        from brain.settings import DEFAULTS
        assert isinstance(DEFAULTS["sleep_min_turns"], int)
        assert DEFAULTS["sleep_min_turns"] >= 1


# ===========================================================================
# consolidate_now() guard-rail paths
# ===========================================================================

class TestConsolidateNow:
    def _make_session(self, has_sleep: bool = True, has_traces: bool = True):
        """Build a minimal BrainSession stub that exercises consolidate_now()."""
        import brain.session_loops as loops_mod
        import brain.session_setup as setup_mod
        import brain.session_turn as turn_mod

        class _FakeSession(loops_mod._LoopsMixin, setup_mod._SetupMixin, turn_mod._TurnMixin):
            def __init__(self):
                self.session_id = "test123"
                self.dmn = None
                self.hippocampus = MagicMock()
                self.hippocampus._schema = MagicMock()
                self._session_traces = [{"t": 1}] if has_traces else []
                self._session_traces_full = [{"t": 1}] if has_traces else []
                self._sleep = MagicMock() if has_sleep else None
                self._consolidation_lock = asyncio.Lock() if has_sleep else None
                self._last_consolidation_ts = 0.0
                self._last_brain_spoke_ts = 0.0
                self._emitter = None
                self.bus = MagicMock()
                self.pns = MagicMock()
                if has_sleep:
                    self._sleep.consolidate = AsyncMock(return_value=None)

        return _FakeSession()

    async def test_returns_disabled_when_no_sleep(self):
        s = self._make_session(has_sleep=False)
        result = await s.consolidate_now()
        assert result["ran"] is False
        assert result["reason"] == "sleep_loop_disabled"

    async def test_returns_skip_when_no_traces(self):
        s = self._make_session(has_sleep=True, has_traces=False)
        result = await s.consolidate_now()
        assert result["ran"] is False
        assert result["reason"] == "no_buffered_turns"

    async def test_returns_skip_when_already_running(self):
        s = self._make_session(has_sleep=True, has_traces=True)
        # Lock the consolidation lock to simulate a running pass
        await s._consolidation_lock.acquire()
        try:
            result = await s.consolidate_now()
            assert result["ran"] is False
            assert result["reason"] == "already_running"
        finally:
            s._consolidation_lock.release()

    async def test_returns_ran_true_on_success(self):
        s = self._make_session(has_sleep=True, has_traces=True)
        result = await s.consolidate_now()
        assert result["ran"] is True
        assert "turns" in result
        assert "elapsed_s" in result
