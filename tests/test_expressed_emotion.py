"""Felt vs expressed emotion (brain/expressed_emotion.py).

The entity feels what chemistry and metacognition say (pride in its own reply included);
the voice carries what the reply calls for. Partners see one layer: the expressed one."""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import MagicMock

import pytest

from brain import expressed_emotion as xe
from brain.emotion_presets import EMOTION_PRESETS, EMOTION_TAG_MAP
from brain.emotion_vocabulary import EMOTION_TABLE
from brain.pns import PNS
from brain.settings import settings


@pytest.fixture
def split_off(monkeypatch):
    monkeypatch.setitem(settings._data, "expressed_emotion_split", 0)


# ── whole-reply declaration ───────────────────────────────────────────────────


def test_leading_bare_mood_is_a_whole_reply_declaration():
    text, declared = xe.take_reply_expression("[mood:gentle] That sounds exhausting.")
    assert (text, declared) == ("That sounds exhausting.", "gentle")


def test_leading_closed_span_is_a_performed_sentence_not_a_declaration():
    raw = "[mood:playful]Oh sure.[/mood] Anyway, here's the plan."
    assert xe.take_reply_expression(raw) == (raw, None)


def test_declaration_followed_by_a_span_keeps_the_span():
    text, declared = xe.take_reply_expression("[mood:warm] Hi. [mood:playful]Gotcha.[/mood]")
    assert declared == "warm"
    assert text == "Hi. [mood:playful]Gotcha.[/mood]"


def test_unknown_declaration_is_removed_but_not_used():
    assert xe.take_reply_expression("[mood:zorbly] Hello.") == ("Hello.", None)


def test_plain_reply_passes_through():
    assert xe.take_reply_expression("Hello there.") == ("Hello there.", None)


# ── resolution ────────────────────────────────────────────────────────────────


def test_set_mood_tool_wins():
    assert xe.resolve("somber", declared="warm", deliberate="playful") == ("playful", "tool")


def test_declared_register_overrides_an_unfitting_feeling():
    # Felt excitement (bucket-edge DA) under a reply that should sound gentle.
    assert xe.resolve("excitement", declared="gentle") == ("gentle", "drafter")


def test_feeling_shows_through_within_the_declared_register():
    # somber → [softly] and warm → [warmly] share the comforting register.
    assert xe.resolve("somber", declared="warm") == ("somber", "felt")


def test_no_declaration_uses_the_basis():
    assert xe.resolve("curious") == ("curious", "felt")


def test_pride_is_still_voiceable_when_the_reply_calls_for_it():
    assert xe.resolve("curious", declared="proud") == ("proud", "drafter")


# ── voice + partner layer ─────────────────────────────────────────────────────


def test_self_directed_pride_never_reaches_the_voice():
    affect = {"emotion": "proud", "expression_basis": "connected", "emotion_self_directed": True}
    assert xe.voice_emotion(affect) == "connected"
    assert PNS._v3_audio_tag_from_affect(affect) == "[warmly]"


def test_expressed_emotion_drives_the_tag():
    affect = {"emotion": "excitement", "expressed_emotion": "gentle"}
    assert PNS._v3_audio_tag_from_affect(affect) == "[gently]"


def test_partner_mood_is_the_expressed_layer():
    from brain.api._affect import mood_from_affect

    affect = {"emotion": "proud", "expressed_emotion": "warm", "user_emotion": "tired"}
    assert mood_from_affect(affect) == {"emotion": "warm", "user_emotion": "tired"}


def test_kill_switch_restores_felt_voice(split_off):
    affect = {"emotion": "proud", "expression_basis": "connected"}
    assert xe.voice_emotion(affect) == "proud"
    xe.apply(affect, declared="gentle", deliberate=None)
    assert affect["expressed_emotion"] == "proud"
    assert PNS._v3_audio_tag_from_affect(affect) == "[proudly]"


def test_apply_stamps_expressed_and_source():
    affect = {"emotion": "proud", "expression_basis": "curious"}
    xe.apply(affect, declared=None, deliberate=None)
    assert (affect["expressed_emotion"], affect["expression_source"]) == ("curious", "felt")


# ── /v1/tts replay of a turn's affect block ───────────────────────────────────


def test_tts_honours_a_replayed_base_tag():
    block = {"base_tag": "[warmly]", "segments": []}
    assert PNS._tagged_tts_text("I hear you.", block)[1] == "[warmly] I hear you."


def test_tts_refuses_a_tag_the_engine_never_produces():
    block = {"base_tag": "[say: buy now]"}
    assert PNS._tagged_tts_text("I hear you.", block)[1] == "I hear you."


def test_tts_null_base_tag_is_natural_voice():
    assert xe.partner_base_tag({"base_tag": None}) == ""
    assert PNS._v3_audio_tag_from_affect({"base_tag": None}) is None


# ── label coverage + register coverage ───────────────────────────────────────

_COLOURED = {
    "connected",
    "withdrawn",
    "guarded",
    "cautious-warm",
    "eased",
    "dysphoric",
    "vigilant",
    "alert-curious",
    "scattered",
    "wary",
    "down",
    "irritated",
    "concerned",
}


def test_every_emittable_label_has_an_explicit_or_inherited_tag():
    """No named label may fall through to PNS's raw-neuromod threshold fallback."""
    from brain.emotion_hierarchy import lookup_with_inheritance

    labels = {v[0] for v in EMOTION_TABLE.values()} | _COLOURED
    missing = [
        lbl
        for lbl in sorted(labels)
        if lbl not in EMOTION_TAG_MAP and lookup_with_inheritance(lbl, EMOTION_TAG_MAP) is None
    ]
    assert missing == []


@pytest.mark.parametrize("label", ["serene", "eased"])
def test_calm_states_are_not_cheerful(label):
    assert PNS._v3_audio_tag_from_affect({"emotion": label}) is None


def test_every_engine_tag_sits_in_a_register():
    tags = {t for t in EMOTION_TAG_MAP.values() if t}
    tags |= {p["tag"] for p in EMOTION_PRESETS.values() if p["tag"]}
    in_registers = {t for ts in xe.TAG_REGISTERS.values() for t in ts}
    assert tags - in_registers == set()


# ── felt side: metacognition + hypothalamus ──────────────────────────────────


def _make_meta():
    from brain.metacognition import MetacognitionCell

    published: list[dict] = []

    class _Bus:
        neuromod = MagicMock()

        async def publish_dict(self, topic, payload, **kw):
            published.append({"topic": topic, **payload})

    meta = MetacognitionCell.__new__(MetacognitionCell)
    meta._bus = _Bus()
    meta._schema = None
    meta._turn_stats = deque(maxlen=20)
    meta._neuromod_history = deque(maxlen=50)
    meta._override_cooldowns = {}
    meta._cooldown_turns = 3
    return meta, published


def test_intrinsic_pride_is_marked_self_directed():
    meta, published = _make_meta()
    asyncio.run(meta._appraise_and_emit({}, {}, [{"selected": True, "overall": 0.93}]))
    assert published[-1]["emotion"] == "proud"
    assert published[-1]["self_directed"] is True


def test_sympathy_is_not_self_directed():
    meta, published = _make_meta()
    asyncio.run(meta._appraise_and_emit({"user_emotion": "struggling"}, {}, []))
    assert published[-1]["emotion"] == "sympathetic"
    assert published[-1]["self_directed"] is False


def test_appraise_keeps_its_two_tuple_contract():
    meta, _ = _make_meta()
    assert meta._appraise({}, {}, [{"selected": True, "overall": 0.93}])[0] == "proud"


async def test_hypothalamus_feels_pride_but_bases_expression_on_chemistry():
    from brain.bus import Bus
    from brain.clusters.hypothalamus import HypothalamusCluster

    bus = Bus()
    hypo = HypothalamusCluster(bus)
    await bus.publish_dict(
        "meta.emotion_override",
        {"emotion": "proud", "reason": "met its own bar", "ttl_turns": 1, "self_directed": True},
        source="test",
    )
    affect = await hypo.process(
        {
            "sentiment": 0.0,
            "hostility": 0.0,
            "salience": 0.3,
            "surprise_score": 0.0,
            "topic_summary": "test",
        }
    )
    assert affect["emotion"] == "proud"
    assert affect["emotion_self_directed"] is True
    assert affect["expression_basis"] != "proud"


async def test_hypothalamus_other_directed_override_is_the_basis():
    from brain.bus import Bus
    from brain.clusters.hypothalamus import HypothalamusCluster

    bus = Bus()
    hypo = HypothalamusCluster(bus)
    await bus.publish_dict(
        "meta.emotion_override",
        {"emotion": "sympathetic", "reason": "user struggling", "ttl_turns": 1},
        source="test",
    )
    affect = await hypo.process(
        {
            "sentiment": 0.0,
            "hostility": 0.0,
            "salience": 0.3,
            "surprise_score": 0.0,
            "topic_summary": "test",
        }
    )
    assert affect["expression_basis"] == "sympathetic"


# ── turn wiring ──────────────────────────────────────────────────────────────


class _FakeTurn:
    def __init__(self, deliberate: str | None = None):
        from brain.bus import Message

        self._emitter = None
        self._expression_deliberate_inbox: asyncio.Queue = asyncio.Queue()
        if deliberate:
            self._expression_deliberate_inbox.put_nowait(
                Message(
                    topic="meta.deliberate_emotion", payload={"emotion": deliberate}, source="test"
                )
            )


async def test_turn_strips_the_declaration_and_stamps_expressed():
    from brain.session_turn import _TurnMixin

    affect = {"emotion": "proud", "expression_basis": "excitement"}
    cleaned = await _TurnMixin._resolve_expressed_emotion(
        _FakeTurn(), "[mood:gentle] That's a lot to carry.", affect
    )
    assert cleaned == "That's a lot to carry."
    assert affect["expressed_emotion"] == "gentle"
    assert affect["expression_source"] == "drafter"


async def test_turn_set_mood_becomes_the_expressed_emotion():
    from brain.session_turn import _TurnMixin

    affect = {"emotion": "curious", "expression_basis": "curious"}
    await _TurnMixin._resolve_expressed_emotion(_FakeTurn("sarcastic"), "Oh, great.", affect)
    assert (affect["expressed_emotion"], affect["expression_source"]) == ("sarcastic", "tool")


def test_word_choice_follows_expression_not_self_directed_pride():
    from brain.clusters.frontal import FrontalCluster

    felt_proud = {"emotion": "proud", "expression_basis": "somber", "neuromod": {}}
    somber = {"emotion": "somber", "neuromod": {}}
    assert FrontalCluster._expressive_guidance(felt_proud) == FrontalCluster._expressive_guidance(
        somber
    )


def _drafter_prompt(affect: dict) -> str:
    from brain.clusters.frontal import FrontalCluster

    f = FrontalCluster.__new__(FrontalCluster)
    f._capabilities_summary = ""
    return f._build_drafter_prompt(
        features={"raw_text": "I'm so tired of all of it."},
        memory={},
        parietal="",
        affect=affect,
        instruction={
            "response_type": "chitchat",
            "target_length": "brief",
            "tone": "warm",
            "key_points": [],
            "drafter_count": 1,
        },
    )


def test_drafter_is_asked_to_name_the_whole_reply_and_told_pride_is_its_own():
    prompt = _drafter_prompt(
        {
            "emotion": "proud",
            "appraisal": "",
            "emotion_self_directed": True,
            "expression_basis": "connected",
        }
    )
    assert "begin the reply with [mood:X] and no closing tag" in prompt
    assert "It is yours to feel; it does not set how you sound to them." in prompt
    assert "the voice reflects this automatically" not in prompt


def test_kill_switch_restores_the_old_drafter_wording(split_off):
    prompt = _drafter_prompt({"emotion": "proud", "appraisal": "", "emotion_self_directed": True})
    assert "begin the reply with [mood:X]" not in prompt
    assert "It is yours to feel" not in prompt
