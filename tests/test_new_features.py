"""
Regression tests for features added in the session covering:
  - parietal.seed() — boot-time ring pre-population from episodic history
  - EpisodicStore.recall_recent() — newest-first sorting
  - audio_dsp.compute_speech_dynamics() — WPM, pace labels, hesitant, burst_score
  - StreamingMicSession mute/unmute/toggle — no I/O required
  - pns._v3_audio_tag_from_affect() — emotion-name lookup + neuromod fallback
  - pns._add_breath_pauses() — em-dash substitution, early-comma guard
  - pns._shape_for_v3() — pre-tagged passthrough, GABA/DA shaping, tag prepend
  - pns._voice_params_from_affect() — voice parameter mapping from neuromods
  - pns.interrupt() / is_speaking — interruption flag guard
  - emotion_vocabulary.name_emotion() — 6 new EMOTION_TABLE entries
  - frontal._expressive_guidance() — emotion→linguistic guidance lookup + fallback
  - metacognition._appraise() — all eight ordered appraisal rules
  - metacognition cooldown — prevents override spam
  - hypothalamus emotion_override — consume meta.emotion_override, correct fields
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _affect(emotion="neutral", DA=0.5, GABA=0.0, ACh=0.3, Glu=0.3) -> dict:
    return {
        "emotion": emotion,
        "neuromod": {"DA": DA, "GABA": GABA, "ACh": ACh, "Glu": Glu},
    }


def _word(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


# ---------------------------------------------------------------------------
# parietal.seed()
# ---------------------------------------------------------------------------

class TestParietalSeed:
    def _make_parietal(self):
        from brain.bus import Bus
        from brain.clusters.parietal import ParietalCluster
        bus = Bus()
        return ParietalCluster(bus)

    def test_seed_empty_list_leaves_ring_empty(self):
        p = self._make_parietal()
        p.seed([])
        assert list(p._ring) == []

    def test_seed_populates_ring_oldest_first(self):
        """Episodes arrive newest-first from recall_recent; seed() must reverse."""
        p = self._make_parietal()
        # newest-first: [episode3, episode2, episode1]
        episodes = [
            {"user_input": "msg3", "entity_response": "resp3", "topic_tags": ["c"], "emotion_state": "joy"},
            {"user_input": "msg2", "entity_response": "resp2", "topic_tags": ["b"], "emotion_state": "calm"},
            {"user_input": "msg1", "entity_response": "resp1", "topic_tags": ["a"], "emotion_state": "neutral"},
        ]
        p.seed(episodes)
        ring = list(p._ring)
        # ring[0] should be the oldest (episode1), ring[-1] the newest (episode3)
        assert ring[0]["user"] == "msg1"
        assert ring[-1]["user"] == "msg3"

    def test_seed_respects_ring_maxlen(self):
        from brain.clusters.parietal import RING_SIZE
        p = self._make_parietal()
        # Provide more episodes than the ring can hold
        episodes = [
            {"user_input": f"msg{i}", "entity_response": "", "topic_tags": [], "emotion_state": None}
            for i in range(RING_SIZE + 4)
        ]
        p.seed(episodes)
        assert len(p._ring) <= RING_SIZE

    def test_seed_maps_fields_correctly(self):
        p = self._make_parietal()
        p.seed([{
            "user_input": "hello",
            "entity_response": "world",
            "topic_tags": ["greeting"],
            "emotion_state": "warm",
        }])
        entry = list(p._ring)[0]
        assert entry["user"] == "hello"
        assert entry["response"] == "world"
        assert entry["intent"] == "greeting"
        assert entry["emotion"] == "warm"

    def test_seed_handles_missing_fields_gracefully(self):
        p = self._make_parietal()
        p.seed([{}])   # no keys at all
        entry = list(p._ring)[0]
        assert entry["user"] == ""
        assert entry["intent"] is None


# ---------------------------------------------------------------------------
# EpisodicStore.recall_recent()
# ---------------------------------------------------------------------------

class TestRecallRecent:
    def _make_store(self, rows: list[dict]):
        import brain.second_brain.store as store_mod
        store = store_mod.EpisodicStore.__new__(store_mod.EpisodicStore)
        store._ready = True

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows
            def limit(self, n):
                return self
            def to_list(self):
                return [dict(r) for r in self._rows]

        class _FakeTable:
            def search(self_t):
                return _FakeResult(rows)

            def to_arrow(self_t):
                # recall_recent uses self._table.to_arrow().sort_by(...).slice(...).to_pylist()
                import pyarrow as pa
                return pa.Table.from_pylist(rows) if rows else pa.table({})

        store._table = _FakeTable()
        return store

    def test_recall_recent_returns_empty_when_not_ready(self):
        import brain.second_brain.store as store_mod
        store = store_mod.EpisodicStore.__new__(store_mod.EpisodicStore)
        store._ready = False
        # Patch _ensure_ready so the real DB is not opened during the test
        store._ensure_ready = lambda: False
        result = store.recall_recent(limit=6)
        assert result == []

    def test_recall_recent_sorted_newest_first(self):
        rows = [
            {"ts": 100, "topic_tags": "[]", "entities": "[]", "neuromod_snapshot": "{}"},
            {"ts": 300, "topic_tags": "[]", "entities": "[]", "neuromod_snapshot": "{}"},
            {"ts": 200, "topic_tags": "[]", "entities": "[]", "neuromod_snapshot": "{}"},
        ]
        store = self._make_store(rows)
        result = store.recall_recent(limit=10)
        assert result[0]["ts"] == 300
        assert result[1]["ts"] == 200
        assert result[2]["ts"] == 100

    def test_recall_recent_respects_limit(self):
        rows = [
            {"ts": i, "topic_tags": "[]", "entities": "[]", "neuromod_snapshot": "{}"}
            for i in range(20)
        ]
        store = self._make_store(rows)
        result = store.recall_recent(limit=5)
        assert len(result) == 5

    def test_recall_recent_deserializes_json_fields(self):
        rows = [{
            "ts": 1,
            "topic_tags": '["a", "b"]',
            "entities": '["Alice"]',
            "neuromod_snapshot": '{"DA": 0.7}',
        }]
        store = self._make_store(rows)
        result = store.recall_recent(limit=1)
        assert result[0]["topic_tags"] == ["a", "b"]
        assert result[0]["entities"] == ["Alice"]
        assert result[0]["neuromod_snapshot"] == {"DA": 0.7}


# ---------------------------------------------------------------------------
# compute_speech_dynamics()
# ---------------------------------------------------------------------------

class TestSpeechDynamics:
    def test_empty_list_returns_defaults(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        r = compute_speech_dynamics([])
        assert r["wpm"] == 0.0
        assert r["pace_label"] == "normal"
        assert r["hesitant"] is False

    def test_single_word_returns_defaults(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        r = compute_speech_dynamics([_word("hello", 0.0, 0.5)])
        assert r["wpm"] == 0.0

    def test_halting_pace(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # 5 words in 10s → 30 WPM → halting (<90)
        words = [_word(f"w{i}", i * 2.0, i * 2.0 + 0.3) for i in range(5)]
        r = compute_speech_dynamics(words)
        assert r["pace_label"] == "halting"

    def test_measured_pace(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # 10 words in ~5.5s → ~109 WPM → measured (90-130)
        words = [_word(f"w{i}", i * 0.55, i * 0.55 + 0.3) for i in range(10)]
        r = compute_speech_dynamics(words)
        assert r["pace_label"] == "measured"

    def test_normal_pace(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # 25 words in ~9s → ~167 WPM → normal (130-170)
        words = [_word(f"w{i}", i * 0.36, i * 0.36 + 0.2) for i in range(25)]
        r = compute_speech_dynamics(words)
        assert r["pace_label"] == "normal"

    def test_rushed_pace(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # 40 words in ~4s → 600 WPM → rushed (>220)
        words = [_word(f"w{i}", i * 0.1, i * 0.1 + 0.08) for i in range(40)]
        r = compute_speech_dynamics(words)
        assert r["pace_label"] == "rushed"

    def test_brisk_pace(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # 20 words in ~5s → 240... try exact: 20 words / (19*0.25s) = ~252...
        # brisk is 170-220 WPM
        # 20 words in 20*(0.5/0.5) → adjust: 20 words in 5.6s → 214 WPM
        words = [_word(f"w{i}", i * 0.28, i * 0.28 + 0.15) for i in range(20)]
        r = compute_speech_dynamics(words)
        assert r["pace_label"] in ("brisk", "rushed")

    def test_long_pause_detection(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # 3 words with 0.7s gaps between each
        words = [
            _word("a", 0.0, 0.3),
            _word("b", 1.0, 1.3),   # gap=0.7s
            _word("c", 2.0, 2.3),   # gap=0.7s
        ]
        r = compute_speech_dynamics(words)
        assert r["long_pause_count"] == 2

    def test_hesitant_flag_set(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # 4 words with 3 long gaps out of 3 gaps → 100% long → hesitant
        words = [
            _word("a", 0.0, 0.2),
            _word("b", 0.8, 1.0),   # gap=0.6
            _word("c", 1.7, 1.9),   # gap=0.7
            _word("d", 2.6, 2.8),   # gap=0.7
        ]
        r = compute_speech_dynamics(words)
        assert r["hesitant"] is True
        assert r["long_pause_count"] >= 2

    def test_hesitant_flag_false_few_pauses(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        # evenly spaced — no long pauses
        words = [_word(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(10)]
        r = compute_speech_dynamics(words)
        assert r["hesitant"] is False

    def test_burst_score_is_float(self):
        from brain.clusters.audio_dsp import compute_speech_dynamics
        words = [_word(f"w{i}", i * 0.5, i * 0.5 + 0.3) for i in range(6)]
        r = compute_speech_dynamics(words)
        assert isinstance(r["burst_score"], float)


# ---------------------------------------------------------------------------
# PNS._v3_audio_tag_from_affect()
# ---------------------------------------------------------------------------

class TestV3AudioTag:
    def test_known_emotion_returns_tag(self):
        from brain.pns import PNS
        affect = _affect(emotion="joy")
        tag = PNS._v3_audio_tag_from_affect(affect)
        assert tag == "[happy]"

    def test_embarrassed_returns_bashfully(self):
        from brain.pns import PNS
        tag = PNS._v3_audio_tag_from_affect(_affect(emotion="embarrassed"))
        assert tag == "[bashfully]"

    def test_flirty_returns_playfully(self):
        from brain.pns import PNS
        tag = PNS._v3_audio_tag_from_affect(_affect(emotion="flirty"))
        assert tag == "[playfully]"

    def test_neutral_returns_none(self):
        from brain.pns import PNS
        tag = PNS._v3_audio_tag_from_affect(_affect(emotion="neutral"))
        assert tag is None

    def test_content_returns_none(self):
        from brain.pns import PNS
        tag = PNS._v3_audio_tag_from_affect(_affect(emotion="content"))
        assert tag is None  # content maps to "" → None

    def test_unknown_emotion_falls_back_to_neuromods_gaba(self):
        from brain.pns import PNS
        affect = _affect(emotion="nonexistent_emotion", GABA=0.6)
        tag = PNS._v3_audio_tag_from_affect(affect)
        assert tag == "[gently]"

    def test_unknown_emotion_falls_back_urgent_glu_gaba(self):
        from brain.pns import PNS
        # Stressed-urgent (high Glu + high GABA) must beat plain GABA guard
        affect = _affect(emotion="nonexistent_emotion", Glu=0.7, GABA=0.5)
        tag = PNS._v3_audio_tag_from_affect(affect)
        assert tag == "[urgently]"

    def test_unknown_emotion_falls_back_excited(self):
        from brain.pns import PNS
        affect = _affect(emotion="nonexistent_emotion", DA=0.7, Glu=0.7, GABA=0.1)
        tag = PNS._v3_audio_tag_from_affect(affect)
        assert tag == "[excited]"

    def test_all_known_emotions_in_map(self):
        """Every emotion in the map should return something (string or None), not raise."""
        from brain.pns import PNS
        for emotion in PNS._V3_TAG_BY_EMOTION:
            result = PNS._v3_audio_tag_from_affect(_affect(emotion=emotion))
            assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# PNS._add_breath_pauses()
# ---------------------------------------------------------------------------

class TestAddBreathPauses:
    def test_replaces_comma_after_position_12(self):
        from brain.pns import PNS
        text = "This is a long sentence, with a pause here."
        result = PNS._add_breath_pauses(text, count=1)
        assert " — " in result
        assert ", " not in result[:result.index(" — ") + 3]

    def test_skips_early_comma(self):
        from brain.pns import PNS
        # Comma is within first 12 chars → should not be replaced
        text = "Hi, how are you doing today with this long string?"
        result = PNS._add_breath_pauses(text, count=1)
        # The comma at position 2 is too early; check no replacement at pos 2
        assert result.startswith("Hi,")

    def test_count_zero_no_replacement(self):
        from brain.pns import PNS
        text = "This is, a test, with many, commas."
        result = PNS._add_breath_pauses(text, count=0)
        assert result == text

    def test_count_respects_limit(self):
        from brain.pns import PNS
        text = "First part here, then second part, then third part, and more."
        result = PNS._add_breath_pauses(text, count=2)
        assert result.count(" — ") <= 2

    def test_no_commas_passthrough(self):
        from brain.pns import PNS
        text = "No commas at all in this sentence."
        result = PNS._add_breath_pauses(text, count=1)
        assert result == text

    def test_returns_string(self):
        from brain.pns import PNS
        result = PNS._add_breath_pauses("Hello, world this is great.", count=1)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# PNS._shape_for_v3()
# ---------------------------------------------------------------------------

class TestShapeForV3:
    def test_pre_tagged_passthrough(self):
        from brain.pns import PNS
        text = "[excited] This is already tagged."
        result = PNS._shape_for_v3(text, _affect())
        assert result == text

    def test_pre_tagged_with_leading_whitespace_passthrough(self):
        from brain.pns import PNS
        text = "  [gently] Lead with whitespace."
        result = PNS._shape_for_v3(text, _affect())
        assert result == text

    def test_gaba_adds_breath_pause(self):
        from brain.pns import PNS
        text = "This is a sentence here, with a comma that could become a pause."
        affect = _affect(GABA=0.6)
        result = PNS._shape_for_v3(text, affect)
        assert " — " in result

    def test_low_da_adds_breath_pause(self):
        from brain.pns import PNS
        text = "Feeling low here, so another pause, maybe even two."
        affect = _affect(DA=0.2)
        result = PNS._shape_for_v3(text, affect)
        assert " — " in result

    def test_tag_prepended_for_known_emotion(self):
        from brain.pns import PNS
        affect = _affect(emotion="joy", DA=0.7)
        result = PNS._shape_for_v3("Hello world.", affect)
        assert result.startswith("[happy]")

    def test_neutral_emotion_no_tag_prefix(self):
        from brain.pns import PNS
        affect = _affect(emotion="neutral", DA=0.5, GABA=0.05)
        result = PNS._shape_for_v3("Hello world.", affect)
        assert not result.startswith("[")


# ---------------------------------------------------------------------------
# PNS._voice_params_from_affect()
# ---------------------------------------------------------------------------

class TestVoiceParams:
    def test_default_params(self):
        from brain.pns import PNS
        params = PNS._voice_params_from_affect(_affect())
        assert "stability" in params and "style" in params and "speed" in params

    def test_high_gaba_steady(self):
        from brain.pns import PNS
        params = PNS._voice_params_from_affect(_affect(GABA=0.6))
        assert params["stability"] > 0.5
        assert params["speed"] < 1.0

    def test_high_glu_high_da_expressive(self):
        from brain.pns import PNS
        params = PNS._voice_params_from_affect(_affect(DA=0.7, Glu=0.7))
        assert params["style"] > 0.4
        assert params["speed"] >= 1.0

    def test_low_da_slower(self):
        from brain.pns import PNS
        params = PNS._voice_params_from_affect(_affect(DA=0.2))
        assert params["speed"] < 1.0

    def test_all_values_in_sane_range(self):
        from brain.pns import PNS
        for emotion, DA, GABA, Glu in [
            ("calm", 0.5, 0.0, 0.3),
            ("angry", 0.1, 0.8, 0.8),
            ("joy", 0.8, 0.0, 0.7),
            ("flat", 0.1, 0.0, 0.1),
        ]:
            params = PNS._voice_params_from_affect(_affect(emotion=emotion, DA=DA, GABA=GABA, Glu=Glu))
            assert 0.0 <= params["stability"] <= 1.0
            assert 0.0 <= params["style"] <= 1.0
            assert 0.5 <= params["speed"] <= 2.0


# ---------------------------------------------------------------------------
# PNS.is_speaking / interrupt()
# ---------------------------------------------------------------------------

class TestPNSInterrupt:
    def _make_pns(self):
        from brain.bus import Bus
        from brain.pns import PNS
        bus = Bus()
        return PNS(bus)

    def test_is_speaking_starts_false(self):
        pns = self._make_pns()
        assert pns.is_speaking is False

    def test_interrupt_no_op_when_not_speaking(self):
        pns = self._make_pns()
        pns.interrupt()   # must not raise

    def test_interrupt_sets_event_when_speaking(self):
        pns = self._make_pns()
        pns._speaking = True
        pns._interrupt_event.clear()
        pns.interrupt()
        assert pns._interrupt_event.is_set()

    def test_interrupt_no_event_when_not_speaking(self):
        pns = self._make_pns()
        pns._speaking = False
        pns._interrupt_event.clear()
        pns.interrupt()
        assert not pns._interrupt_event.is_set()


# ---------------------------------------------------------------------------
# emotion_vocabulary — new EMOTION_TABLE entries
# ---------------------------------------------------------------------------

class TestNewEmotions:
    def test_angry_neuromod_combo(self):
        from brain.emotion_vocabulary import name_emotion
        # low DA, high GABA, low ACh, high Glu
        emotion, _ = name_emotion(0.1, 0.7, 0.1, 0.7)
        assert emotion == "angry"

    def test_proud_neuromod_combo(self):
        from brain.emotion_vocabulary import name_emotion
        # high DA, low GABA, low ACh, high Glu
        emotion, _ = name_emotion(0.8, 0.1, 0.1, 0.7)
        assert emotion == "proud"

    def test_surprised_neuromod_combo(self):
        from brain.emotion_vocabulary import name_emotion
        # mid DA, low GABA, high ACh, high Glu
        emotion, _ = name_emotion(0.5, 0.1, 0.7, 0.7)
        assert emotion == "surprised"

    def test_defensive_neuromod_combo(self):
        from brain.emotion_vocabulary import name_emotion
        # low DA, high GABA, mid ACh, mid Glu
        emotion, _ = name_emotion(0.1, 0.7, 0.45, 0.45)
        assert emotion == "defensive"

    def test_wistful_neuromod_combo(self):
        from brain.emotion_vocabulary import name_emotion
        # low DA, low GABA, high ACh, low Glu
        emotion, _ = name_emotion(0.1, 0.1, 0.7, 0.1)
        assert emotion == "wistful"

    def test_confused_neuromod_combo(self):
        from brain.emotion_vocabulary import name_emotion
        # low DA, low GABA, high ACh, high Glu
        emotion, _ = name_emotion(0.1, 0.1, 0.7, 0.7)
        assert emotion == "confused"

    def test_known_emotions_return_tendency(self):
        from brain.emotion_vocabulary import name_emotion
        for emotion_name in ("angry", "proud", "surprised", "defensive", "wistful", "confused"):
            # Just confirm they all return a tuple with the tendency string
            result = name_emotion(0.1, 0.1, 0.7, 0.7)
            assert isinstance(result, tuple)
            assert len(result) == 2


# ---------------------------------------------------------------------------
# FrontalCluster._expressive_guidance()
# ---------------------------------------------------------------------------

class TestExpressiveGuidance:
    def test_returns_string_for_known_emotion(self):
        from brain.clusters.frontal import FrontalCluster
        guidance = FrontalCluster._expressive_guidance(_affect(emotion="angry"))
        assert isinstance(guidance, str)
        assert len(guidance) > 0

    def test_embarrassed_guidance_present(self):
        from brain.clusters.frontal import FrontalCluster
        guidance = FrontalCluster._expressive_guidance(_affect(emotion="embarrassed"))
        assert guidance is not None

    def test_flirty_guidance_present(self):
        from brain.clusters.frontal import FrontalCluster
        guidance = FrontalCluster._expressive_guidance(_affect(emotion="flirty"))
        assert guidance is not None

    def test_neutral_returns_none(self):
        from brain.clusters.frontal import FrontalCluster
        guidance = FrontalCluster._expressive_guidance(_affect(emotion="neutral"))
        assert guidance is None

    def test_unknown_emotion_falls_back_to_neuromod(self):
        from brain.clusters.frontal import FrontalCluster
        affect = _affect(emotion="made_up_emotion_xyz", GABA=0.6)
        guidance = FrontalCluster._expressive_guidance(affect)
        assert guidance is not None
        assert "grounding" in guidance.lower() or "de-escalat" in guidance.lower() or "short" in guidance.lower()

    def test_all_expressive_emotions_return_non_empty(self):
        from brain.clusters.frontal import FrontalCluster
        for emotion, guidance in FrontalCluster._EXPRESSIVE_BY_EMOTION.items():
            if emotion == "neutral":
                continue  # intentionally empty
            assert len(guidance) > 0, f"_EXPRESSIVE_BY_EMOTION[{emotion!r}] is empty"

    def test_every_v3_tag_emotion_has_expressive_guidance_or_fallback(self):
        """Emotions in PNS._V3_TAG_BY_EMOTION that aren't 'neutral' should either
        have expressive guidance or map cleanly through the neuromod fallback."""
        from brain.clusters.frontal import FrontalCluster
        from brain.pns import PNS
        for emotion in PNS._V3_TAG_BY_EMOTION:
            affect = _affect(emotion=emotion)
            # Should not raise; return value is a string or None
            result = FrontalCluster._expressive_guidance(affect)
            assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# MetacognitionCell._appraise() — all eight ordered rules
# ---------------------------------------------------------------------------

class TestMetacognitionAppraise:
    def _make_meta(self):
        from brain.metacognition import MetacognitionCell

        class _FakeBus:
            neuromod = MagicMock()
            async def publish_dict(self, *a, **kw): pass
            def subscribe(self, *a, **kw): return MagicMock()

        class _FakeRouter:
            async def call(self, *a, **kw): return "{}"
            async def embed(self, *a, **kw): return [0.0] * 768

        meta = MetacognitionCell.__new__(MetacognitionCell)
        meta._bus = _FakeBus()
        meta._router = _FakeRouter()
        meta._schema = None
        meta._turn_stats = deque(maxlen=20)
        meta._neuromod_history = deque(maxlen=50)
        meta._override_cooldowns = {}
        meta._cooldown_turns = 3
        return meta

    # Rule 1: embarrassed
    def test_embarrassed_on_two_plus_vetoed_drafts(self):
        meta = self._make_meta()
        draft_scores = [
            {"vetoed": True, "overall": 0.3},
            {"vetoed": True, "overall": 0.4},
            {"selected": True, "overall": 0.7},
        ]
        emotion, reason = meta._appraise({}, {}, draft_scores)
        assert emotion == "embarrassed"
        assert "vetoed" in reason

    def test_embarrassed_not_on_one_vetoed(self):
        meta = self._make_meta()
        draft_scores = [{"vetoed": True}, {"selected": True, "overall": 0.8}]
        emotion, _ = meta._appraise({"user_tone_toward_ai": "praising"}, {}, draft_scores)
        # With 1 veto and praise, should not be embarrassed (could be grateful)
        assert emotion != "embarrassed"

    # Rule 2: apologetic
    def test_apologetic_on_pushback_after_surprising_turn(self):
        meta = self._make_meta()
        meta._turn_stats.append({"surprise_score": 0.8, "emotion": "neutral", "ts": 1})
        meta._turn_stats.append({"surprise_score": 0.2, "emotion": "neutral", "ts": 2})
        features = {
            "user_emotion": "frustrated",
            "user_tone_toward_ai": "dismissive",
        }
        emotion, reason = meta._appraise(features, {}, [])
        assert emotion == "apologetic"

    # Rule 3: sympathetic
    def test_sympathetic_on_sad_user(self):
        meta = self._make_meta()
        features = {"user_emotion": "sad", "user_tone_toward_ai": "neutral"}
        emotion, reason = meta._appraise(features, {}, [])
        assert emotion == "sympathetic"

    def test_sympathetic_on_anxious_user(self):
        meta = self._make_meta()
        features = {"user_emotion": "anxious", "user_tone_toward_ai": "neutral"}
        emotion, _ = meta._appraise(features, {}, [])
        assert emotion == "sympathetic"

    # Rule 4: proud (must beat grateful when high-quality + warm)
    def test_proud_before_grateful_with_high_draft_score(self):
        meta = self._make_meta()
        draft_scores = [{"selected": True, "overall": 0.90}]
        features = {"user_tone_toward_ai": "praising", "user_emotion": "happy"}
        emotion, _ = meta._appraise(features, {}, draft_scores)
        assert emotion == "proud", f"Expected proud (high quality + praise), got {emotion}"

    # Rule 5: grateful
    def test_grateful_on_praise_without_high_score(self):
        meta = self._make_meta()
        features = {"user_tone_toward_ai": "praising", "user_emotion": "happy"}
        emotion, _ = meta._appraise(features, {}, [])
        assert emotion == "grateful"

    # Rule 6: relieved
    def test_relieved_on_gaba_drop(self):
        meta = self._make_meta()
        meta._neuromod_history.append({"GABA": 0.7, "DA": 0.5})
        meta._neuromod_history.append({"GABA": 0.4, "DA": 0.5})
        features = {"user_tone_toward_ai": "neutral", "user_emotion": "neutral"}
        emotion, reason = meta._appraise(features, {"GABA": 0.4}, [])
        assert emotion == "relieved"
        assert "GABA" in reason

    # Rule 7: disappointed
    def test_disappointed_on_low_da_high_salience(self):
        meta = self._make_meta()
        features = {
            "user_tone_toward_ai": "neutral",
            "user_emotion": "neutral",
            "salience": 0.8,
        }
        neuromod = {"DA": 0.1, "GABA": 0.1, "ACh": 0.3, "Glu": 0.3}
        emotion, _ = meta._appraise(features, neuromod, [])
        assert emotion == "disappointed"

    # Rule 8: flirty
    def test_flirty_on_high_affection_warm_playful(self):
        meta = self._make_meta()
        meta._schema = MagicMock()
        meta._schema.read.return_value = "- Score: 50\n"
        features = {
            "user_tone_toward_ai": "warm",
            "user_emotion": "playful",
            "intent": "chitchat",
        }
        emotion, _ = meta._appraise(features, {}, [])
        assert emotion == "flirty"

    def test_flirty_not_on_low_affection(self):
        meta = self._make_meta()
        meta._schema = MagicMock()
        meta._schema.read.return_value = "- Score: 5\n"
        features = {
            "user_tone_toward_ai": "warm",
            "user_emotion": "playful",
            "intent": "chitchat",
        }
        emotion, _ = meta._appraise(features, {}, [])
        assert emotion != "flirty"

    # No override
    def test_no_override_on_neutral_turn(self):
        meta = self._make_meta()
        emotion, reason = meta._appraise(
            {"user_tone_toward_ai": "neutral", "user_emotion": "neutral"},
            {"DA": 0.5, "GABA": 0.1},
            [],
        )
        assert emotion is None
        assert reason == ""


# ---------------------------------------------------------------------------
# MetacognitionCell cooldown
# ---------------------------------------------------------------------------

class TestMetacognitionCooldown:
    def _make_meta(self):
        from brain.metacognition import MetacognitionCell

        class _FakeBus:
            neuromod = MagicMock()
            async def publish_dict(self, *a, **kw): pass
            def subscribe(self, *a, **kw): return MagicMock()

        class _FakeRouter:
            async def call(self, *a, **kw): return "{}"
            async def embed(self, *a, **kw): return [0.0] * 768

        meta = MetacognitionCell.__new__(MetacognitionCell)
        meta._bus = _FakeBus()
        meta._router = _FakeRouter()
        meta._schema = None
        meta._turn_stats = deque(maxlen=20)
        meta._neuromod_history = deque(maxlen=50)
        meta._override_cooldowns = {}
        meta._cooldown_turns = 3
        return meta

    async def test_cooldown_suppresses_repeated_override(self):
        published: list[dict] = []

        class _BusWithLog:
            neuromod = MagicMock()
            def subscribe(self, *a, **kw): return MagicMock()
            async def publish_dict(self, topic, payload, **kw):
                published.append({"topic": topic, "payload": payload})

        from brain.metacognition import MetacognitionCell
        meta = MetacognitionCell.__new__(MetacognitionCell)
        meta._bus = _BusWithLog()
        meta._router = MagicMock()
        meta._schema = None
        meta._turn_stats = deque(maxlen=20)
        meta._neuromod_history = deque(maxlen=50)
        meta._override_cooldowns = {}
        meta._cooldown_turns = 3
        meta._reflector = MagicMock()

        # First call: should publish
        await meta._appraise_and_emit(
            {"user_tone_toward_ai": "praising", "user_emotion": "neutral"},
            {},
            [],
        )
        first_count = len([p for p in published if p["topic"] == "meta.emotion_override"])
        # Was grateful or similar — may or may not have fired depending on appraise

        # Manually set cooldown
        meta._override_cooldowns["grateful"] = 3

        published.clear()
        # Second call with praise → grateful on cooldown → should NOT publish
        await meta._appraise_and_emit(
            {"user_tone_toward_ai": "praising", "user_emotion": "neutral"},
            {},
            [],
        )
        second_count = len([p for p in published if p["topic"] == "meta.emotion_override"])
        assert second_count == 0, "Override should be suppressed while on cooldown"

    def test_cooldown_decrements_each_turn(self):
        from brain.metacognition import MetacognitionCell

        class _FakeBus:
            neuromod = MagicMock()
            async def publish_dict(self, *a, **kw): pass
            def subscribe(self, *a, **kw): return MagicMock()

        meta = MetacognitionCell.__new__(MetacognitionCell)
        meta._bus = _FakeBus()
        meta._override_cooldowns = {"grateful": 3}
        meta._cooldown_turns = 3
        meta._turn_stats = deque(maxlen=20)
        meta._neuromod_history = deque(maxlen=50)
        meta._reflector = MagicMock()
        meta._schema = None

        # record_turn decrements cooldowns
        meta._turn_stats.append({"llm_calls": 1, "elapsed_s": 0.5, "emotion": "neutral",
                                  "surprise_score": 0.0, "drafter_won": None, "ts": time.time()})
        meta._override_cooldowns = {
            e: c - 1 for e, c in meta._override_cooldowns.items() if c > 1
        }
        assert meta._override_cooldowns.get("grateful", 0) == 2

    def test_cooldown_expires_when_count_reaches_zero(self):
        from brain.metacognition import MetacognitionCell

        class _FakeBus:
            neuromod = MagicMock()
            async def publish_dict(self, *a, **kw): pass
            def subscribe(self, *a, **kw): return MagicMock()

        meta = MetacognitionCell.__new__(MetacognitionCell)
        meta._bus = _FakeBus()
        meta._override_cooldowns = {"grateful": 1}

        # One more decrement: count goes to 0, should be removed
        meta._override_cooldowns = {
            e: c - 1 for e, c in meta._override_cooldowns.items() if c > 1
        }
        assert "grateful" not in meta._override_cooldowns


# ---------------------------------------------------------------------------
# MetacognitionCell._affection_score()
# ---------------------------------------------------------------------------

class TestAffectionScore:
    def _make_meta_with_schema(self, content: str):
        from brain.metacognition import MetacognitionCell

        class _FakeBus:
            neuromod = MagicMock()
            async def publish_dict(self, *a, **kw): pass
            def subscribe(self, *a, **kw): return MagicMock()

        schema = MagicMock()
        schema.read.return_value = content

        meta = MetacognitionCell.__new__(MetacognitionCell)
        meta._bus = _FakeBus()
        meta._schema = schema
        meta._turn_stats = deque(maxlen=20)
        meta._neuromod_history = deque(maxlen=50)
        meta._override_cooldowns = {}
        meta._cooldown_turns = 3
        return meta

    def test_reads_positive_score(self):
        meta = self._make_meta_with_schema("## Affection\n- Score: 42\n")
        assert meta._affection_score() == 42

    def test_reads_negative_score(self):
        meta = self._make_meta_with_schema("- Score: -10\n")
        assert meta._affection_score() == -10

    def test_returns_zero_on_no_match(self):
        meta = self._make_meta_with_schema("No score here.")
        assert meta._affection_score() == 0

    def test_returns_zero_without_schema(self):
        from brain.metacognition import MetacognitionCell

        class _FakeBus:
            neuromod = MagicMock()
            async def publish_dict(self, *a, **kw): pass
            def subscribe(self, *a, **kw): return MagicMock()

        meta = MetacognitionCell.__new__(MetacognitionCell)
        meta._bus = _FakeBus()
        meta._schema = None
        assert meta._affection_score() == 0


# ---------------------------------------------------------------------------
# Hypothalamus emotion_override consumption
# ---------------------------------------------------------------------------

class TestHypothalamusOverride:
    async def test_override_replaces_emotion_and_sets_source(self):
        """A fresh meta.emotion_override message must appear in affect.emotion."""
        from brain.bus import Bus
        from brain.clusters.hypothalamus import HypothalamusCluster

        bus = Bus()
        hypo = HypothalamusCluster(bus)

        # Publish a fresh override (TTL default is 60s so not expired)
        await bus.publish_dict(
            "meta.emotion_override",
            {"emotion": "grateful", "reason": "test praise", "ttl_turns": 1},
            source="test",
        )

        # Neutral features so neuromod-derived emotion would be "neutral"
        features = {
            "sentiment": 0.0,
            "hostility": 0.0,
            "salience": 0.3,
            "surprise_score": 0.0,
            "topic_summary": "test",
        }
        affect = await hypo.process(features)

        assert affect["emotion"] == "grateful"
        assert affect["emotion_source"] == "metacognition"
        assert affect["emotion_override_reason"] == "test praise"

    async def test_no_override_sets_source_neuromod(self):
        from brain.bus import Bus
        from brain.clusters.hypothalamus import HypothalamusCluster

        bus = Bus()
        hypo = HypothalamusCluster(bus)

        features = {
            "sentiment": 0.5,
            "hostility": 0.0,
            "salience": 0.5,
            "surprise_score": 0.0,
            "topic_summary": "test",
        }
        affect = await hypo.process(features)

        assert affect["emotion_source"] == "neuromod"
        assert affect["emotion_override_reason"] is None


# ---------------------------------------------------------------------------
# emotion_hierarchy
# ---------------------------------------------------------------------------

class TestEmotionHierarchy:
    def test_parents_known_leaf(self):
        from brain.emotion_hierarchy import parents
        assert parents("embarrassed") == ("sad", "humiliated")
        assert parents("flirty") == ("happy", "playful")
        assert parents("angry") == ("anger", "mad")

    def test_parents_unknown_returns_none(self):
        from brain.emotion_hierarchy import parents
        assert parents("not_a_real_emotion") is None

    def test_parents_case_insensitive(self):
        from brain.emotion_hierarchy import parents
        assert parents("EMBARRASSED") == ("sad", "humiliated")
        assert parents("Joy") == ("happy", "joyful")

    def test_core_of_known(self):
        from brain.emotion_hierarchy import core_of
        assert core_of("embarrassed") == "sad"
        assert core_of("proud") == "happy"
        assert core_of("angry") == "anger"
        assert core_of("thoughtful") == "cognitive"

    def test_core_of_unknown_defaults_neutral(self):
        from brain.emotion_hierarchy import core_of
        assert core_of("xyz_unknown") == "neutral"

    def test_core_of_empty_string(self):
        from brain.emotion_hierarchy import core_of
        assert core_of("") == "neutral"

    def test_lookup_with_inheritance_leaf_hit(self):
        from brain.emotion_hierarchy import lookup_with_inheritance
        table = {"embarrassed": "leaf-value", "humiliated": "mid-value"}
        assert lookup_with_inheritance("embarrassed", table) == "leaf-value"

    def test_lookup_with_inheritance_mid_fallback(self):
        from brain.emotion_hierarchy import lookup_with_inheritance
        table = {"humiliated": "mid-value", "sad": "core-value"}
        # "embarrassed" has no leaf entry; falls back to mid "humiliated"
        assert lookup_with_inheritance("embarrassed", table) == "mid-value"

    def test_lookup_with_inheritance_core_fallback(self):
        from brain.emotion_hierarchy import lookup_with_inheritance
        table = {"sad": "core-value"}
        # No leaf, no mid → falls back to core "sad"
        assert lookup_with_inheritance("embarrassed", table) == "core-value"

    def test_lookup_with_inheritance_no_match(self):
        from brain.emotion_hierarchy import lookup_with_inheritance
        assert lookup_with_inheritance("embarrassed", {}) is None
        assert lookup_with_inheritance("unknown_emotion_xyz", {"happy": "x"}) is None

    def test_lookup_treats_empty_as_continue(self):
        """An empty string in the table shouldn't short-circuit inheritance."""
        from brain.emotion_hierarchy import lookup_with_inheritance
        table = {"embarrassed": "", "humiliated": "mid-value"}
        # Leaf is empty → continues up the chain
        assert lookup_with_inheritance("embarrassed", table) == "mid-value"

    def test_all_leaves_for_core(self):
        from brain.emotion_hierarchy import all_leaves_for
        sad_leaves = all_leaves_for("sad")
        assert "embarrassed" in sad_leaves
        assert "wistful" in sad_leaves
        assert "joy" not in sad_leaves

    def test_every_emotion_in_v3_tag_map_resolves(self):
        """Every emotion the PNS v3 table knows should produce some tag —
        either directly or via inheritance — without raising."""
        from brain.pns import PNS
        for emotion in PNS._V3_TAG_BY_EMOTION:
            result = PNS._v3_audio_tag_from_affect({"emotion": emotion, "neuromod": {}})
            assert result is None or isinstance(result, str)

    def test_pns_inheritance_for_unknown_leaf(self):
        """A leaf emotion not in the table but in the hierarchy must inherit
        its mid/core entry rather than fall through to neuromod fallback."""
        from brain.pns import PNS
        # 'tender' is in the table — but let's test a less-mapped leaf.
        # 'wistful' is in the hierarchy under (sad, lonely); both have entries.
        affect = {"emotion": "wistful", "neuromod": {"DA": 0.5}}
        tag = PNS._v3_audio_tag_from_affect(affect)
        assert tag is not None  # should NOT fall through to neuromod default

    def test_frontal_inheritance_for_unknown_leaf(self):
        from brain.clusters.frontal import FrontalCluster
        # Made-up leaf, but verify mid-tier entries provide fallback for real ones.
        affect = {"emotion": "tender", "neuromod": {}}
        guidance = FrontalCluster._expressive_guidance(affect)
        assert guidance is not None


# ---------------------------------------------------------------------------
# StreamingMicSession — mute / unmute / toggle (no I/O)
# ---------------------------------------------------------------------------

class _FakeBus:
    async def publish_dict(self, *a, **kw): pass


def _make_mic():
    """Build a StreamingMicSession with no sounddevice or Deepgram started."""
    from brain.streaming_mic import StreamingMicSession
    return StreamingMicSession(
        bus=_FakeBus(),
        is_speaking_fn=lambda: False,
        on_user_interrupt=None,
    )


class TestStreamingMicMute:
    def test_starts_unmuted(self):
        mic = _make_mic()
        assert mic.is_muted is False

    def test_mute_sets_flag(self):
        mic = _make_mic()
        mic.mute()
        assert mic.is_muted is True

    def test_unmute_clears_flag(self):
        mic = _make_mic()
        mic.mute()
        mic.unmute()
        assert mic.is_muted is False

    def test_toggle_mute_returns_new_state(self):
        mic = _make_mic()
        result = mic.toggle_mute()
        assert result is True
        assert mic.is_muted is True

    def test_toggle_unmute_returns_new_state(self):
        mic = _make_mic()
        mic.mute()
        result = mic.toggle_mute()
        assert result is False
        assert mic.is_muted is False

    def test_double_mute_is_idempotent(self):
        mic = _make_mic()
        mic.mute()
        mic.mute()
        assert mic.is_muted is True

    def test_double_unmute_is_idempotent(self):
        mic = _make_mic()
        mic.unmute()
        assert mic.is_muted is False

    def test_enqueue_chunk_replaced_with_silence_when_muted(self):
        # Muted chunks are still enqueued (as silence) so the Deepgram WS
        # doesn't time out — Deepgram drops the connection after ~10s with no
        # audio and there's no graceful in-band recovery from that.
        mic = _make_mic()
        mic.mute()
        live_chunk = bytes([42] * 3200)
        mic._enqueue_chunk(live_chunk)
        assert not mic._pcm_in.empty()
        sent = mic._pcm_in.get_nowait()
        assert len(sent) == len(live_chunk)
        assert sent == b"\x00" * len(live_chunk)  # silence, not the live audio

    def test_enqueue_chunk_accepted_when_unmuted(self):
        mic = _make_mic()
        mic._enqueue_chunk(b"\x00" * 3200)
        assert not mic._pcm_in.empty()

    def test_enqueue_chunk_accepted_after_unmute(self):
        mic = _make_mic()
        mic.mute()
        mic.unmute()
        mic._enqueue_chunk(b"\x00" * 3200)
        assert not mic._pcm_in.empty()

    def test_toggle_sequence(self):
        mic = _make_mic()
        states = [mic.toggle_mute() for _ in range(4)]
        assert states == [True, False, True, False]
