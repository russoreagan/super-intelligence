"""
Voice-detection fixes (2026-11-01 eval): name extraction no longer enrolls
ordinary words, short clips don't spawn strangers, prosody is measured on the
speech (not the VAD padding) and calibrated per utterance, and a neutral
acoustic read no longer mutes the text affect cues.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from brain.clusters.audio_dsp import (
    extract_identity_name,
    extract_prosody,
    fold_prosody_baseline,
    label_prosody_tone,
    trim_to_speech,
)

SR = 16000


def _vec(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(192).astype(np.float32)
    return v / np.linalg.norm(v)


def _near(v: np.ndarray, seed: int, noise: float) -> np.ndarray:
    """Same voice, a noisier sample: high cosine to `v`."""
    n = v + noise * _vec(seed)
    return (n / np.linalg.norm(n)).astype(np.float32)


# ── Name extraction ──────────────────────────────────────────────────────────


class TestIdentityName:
    @pytest.mark.parametrize(
        "text",
        [
            "It's stocks I'm interested in, the ones with momentum.",
            "I'm good thanks.",
            "I'm going to need that by Friday.",
            "This is great.",
            "Honestly I'm not sure that approach is going to work.",
            "Yeah.",
            "Thanks.",
        ],
    )
    def test_ordinary_speech_is_not_a_name(self, text):
        assert extract_identity_name(text) is None

    @pytest.mark.parametrize(
        "text,name",
        [
            ("My name is Russ.", "Russ"),
            ("my name's Alice", "Alice"),
            ("Hi, I'm Alice", "Alice"),
            ("call me Mary Jane", "Mary Jane"),
        ],
    )
    def test_explicit_introductions(self, text, name):
        assert extract_identity_name(text) == name

    def test_short_answers_only_count_when_prompted(self):
        assert extract_identity_name("It's Russ.") is None
        assert extract_identity_name("Russ.") is None
        assert extract_identity_name("It's Russ.", prompted=True) == "Russ"
        assert extract_identity_name("Russ.", prompted=True) == "Russ"
        # Lowercase is still an everyday noun, prompted or not.
        assert extract_identity_name("it's stocks", prompted=True) is None


class TestTurnEnrollmentName:
    def test_lone_entity_of_an_unprompted_turn_is_not_a_name(self):
        """The 'Stocks' enrollment: first voice turn, nobody asked for a name."""
        from brain.session_turn import _extract_identity_name

        text = "I want to filter stocks by their performance this quarter"
        assert _extract_identity_name(text, {"entities": ["stocks"]}) is None

    def test_prompted_answer_corroborated_by_entities(self):
        from brain.session_turn import _extract_identity_name

        assert _extract_identity_name("Russ.", {"entities": ["Russ"]}, prompted=True) == "Russ"
        assert _extract_identity_name("Russ here", {"entities": ["Russ"]}, prompted=True) == "Russ"

    def test_prompted_but_uncorroborated_is_not_a_name(self):
        from brain.session_turn import _extract_identity_name

        assert _extract_identity_name("Yeah.", {"entities": []}, prompted=True) is None
        assert _extract_identity_name("stocks, mostly", {"entities": ["stocks"]}, True) is None

    def test_explicit_introduction_needs_no_prompt(self):
        from brain.session_turn import _extract_identity_name

        assert _extract_identity_name("My name is Russ", {"entities": []}) == "Russ"


# ── Short clips ──────────────────────────────────────────────────────────────


class TestShortClips:
    def _registry(self, tmp_path):
        from brain.clusters.auditory_cortex import SessionSpeakerRegistry
        from brain.second_brain.speaker_store import SpeakerStore

        return SessionSpeakerRegistry(SpeakerStore(tmp_path))

    def test_first_short_clip_stays_unattributed(self, tmp_path):
        reg = self._registry(tmp_path)
        spk, is_new, _ = reg.match_or_create(_vec(1), duration_s=0.6)
        assert spk is None and is_new is False
        assert reg.all_speakers() == []

    def test_unmatched_short_clip_credits_the_closest_voice(self, tmp_path):
        reg = self._registry(tmp_path)
        first, _, _ = reg.match_or_create(_vec(1), duration_s=3.0)
        mean_before = first.embedding_mean.copy()
        spk, is_new, _ = reg.match_or_create(_vec(42), duration_s=0.6)
        assert spk is first and is_new is False
        assert len(reg.all_speakers()) == 1
        # A noisy short embedding must not drag the voiceprint.
        assert np.allclose(first.embedding_mean, mean_before)

    def test_short_clip_goes_to_its_own_voice_not_the_last_one(self, tmp_path):
        reg = self._registry(tmp_path)
        a, _, _ = reg.match_or_create(_vec(1), duration_s=3.0)
        b, _, _ = reg.match_or_create(_vec(2), duration_s=3.0)  # B spoke last
        # A noisy short clip of A (below the 0.62 bar) still belongs to A.
        short_a = _near(_vec(1), 7, 1.2)
        assert float(np.dot(short_a, a.embedding_mean)) < 0.62
        spk, _, _ = reg.match_or_create(short_a, duration_s=0.6)
        assert spk is a

    def test_long_unmatched_clip_is_still_a_new_voice(self, tmp_path):
        reg = self._registry(tmp_path)
        reg.match_or_create(_vec(1), duration_s=3.0)
        spk, is_new, _ = reg.match_or_create(_vec(42), duration_s=3.0)
        assert is_new is True
        assert len(reg.all_speakers()) == 2

    def test_short_clip_does_not_move_the_stored_voiceprint(self, tmp_path):
        from brain.clusters.auditory_cortex import SessionSpeakerRegistry
        from brain.second_brain.speaker_store import SpeakerStore

        store = SpeakerStore(tmp_path)
        v = _vec(1)
        sid = store.enroll("Russ", v)
        reg = SessionSpeakerRegistry(store)
        spk, _, _ = reg.match_or_create(_near(v, 2, 0.2), duration_s=0.8)
        assert spk.store_id == sid
        assert store._profiles[sid]["sample_count"] == 1


# ── Prosody measurement ──────────────────────────────────────────────────────


def _speechlike(seconds: float = 2.0, amp: float = 0.2) -> np.ndarray:
    """Harmonic tone with a moving pitch and ~4.5 Hz syllable envelope."""
    t = np.linspace(0, seconds, int(SR * seconds), endpoint=False)
    phase = 2 * np.pi * (150 * t - (60 / (2 * np.pi * 2)) * np.cos(2 * np.pi * 2 * t))
    tone = sum(np.sin(k * phase) / k for k in range(1, 6))
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 4.5 * t)
    return (amp * tone * env).astype(np.float32)


def _noise(seconds: float) -> np.ndarray:
    return (np.random.default_rng(0).standard_normal(int(SR * seconds)) * 0.002).astype(np.float32)


def _pad(audio: np.ndarray) -> np.ndarray:
    """Browser VAD framing: ~0.5 s pre-roll, ~2 s hangover of room noise."""
    return np.concatenate([_noise(0.5), audio, _noise(2.0)])


class TestProsodyMeasurement:
    def test_trim_drops_vad_padding(self):
        speech = _speechlike()
        trimmed = trim_to_speech(_pad(speech), SR)
        assert abs(len(trimmed) - len(speech)) < 0.15 * SR

    def test_padding_no_longer_dilutes_features(self):
        pytest.importorskip("librosa")
        speech = _speechlike()
        plain = extract_prosody(speech, SR)
        padded = extract_prosody(_pad(speech), SR)
        assert padded["energy_mean"] == pytest.approx(plain["energy_mean"], rel=0.1)
        assert padded["speech_rate_hz"] == pytest.approx(plain["speech_rate_hz"], rel=0.25)

    def test_voiced_fraction_is_measured_not_constant(self):
        pytest.importorskip("librosa")
        voiced = extract_prosody(_speechlike(), SR)["voiced_fraction"]
        rng = np.random.default_rng(1)
        breathy = (rng.standard_normal(2 * SR) * 0.1).astype(np.float32)  # unvoiced hiss
        unvoiced = extract_prosody(breathy, SR)["voiced_fraction"]
        assert voiced > 0.5
        assert unvoiced < voiced
        assert unvoiced < 1.0

    def test_too_little_speech_is_silence(self):
        blip = np.concatenate([np.zeros(SR), _speechlike(0.1), np.zeros(SR)])
        assert extract_prosody(blip, SR)["tone_label"] == "silence"


class TestRelativeEnergy:
    def _f(self, **kw):
        base = {
            "voiced_fraction": 0.7,
            "f0_std_hz": 30.0,
            "energy_mean": 0.03,
            "energy_std": 0.02,
            "speech_rate_hz": 5.0,
            "jitter": 0.0,
            "shimmer": 0.0,
        }
        base.update(kw)
        return base

    def test_quiet_mic_can_read_energetic_once_calibrated(self):
        baseline = {"jitter": 0.01, "shimmer": 0.05, "energy_mean": 0.03, "count": 12}
        assert label_prosody_tone(self._f(energy_mean=0.03), baseline) == "calm"
        assert label_prosody_tone(self._f(energy_mean=0.06), baseline) == "energetic"

    def test_quiet_mic_is_not_monotone(self):
        # Same relative loudness variation, 4x quieter: not "monotone" any more.
        assert label_prosody_tone(self._f(energy_mean=0.025, energy_std=0.015)) != "monotone"


class TestBaselineFold:
    def test_tracks_f0_std_from_payload_key(self):
        bl = fold_prosody_baseline(None, {"f0_std_hz": 24.0, "energy_mean": 0.1})
        assert bl["f0_std"] == pytest.approx(24.0)
        assert bl["count"] == 1

    def test_silence_does_not_count(self):
        bl = fold_prosody_baseline(None, {"tone_label": "silence", "energy_mean": 0.001})
        assert bl["count"] == 0


# ── Cortex: prosody pairs with its own utterance's speaker ───────────────────


def _patch_store(monkeypatch, tmp_path):
    from brain.second_brain.speaker_store import SpeakerStore

    store = SpeakerStore(tmp_path)
    monkeypatch.setattr("brain.clusters.auditory_cortex.SpeakerStore", lambda: store)
    monkeypatch.setattr("brain.clusters.auditory_cortex.detect_vocal_events", lambda a, sr: {})
    return store


FIXED_PROSODY = {
    "tone_label": "calm",
    "tone_strength": 0.0,
    "jitter": 0.02,
    "shimmer": 0.07,
    "energy_mean": 0.09,
    "f0_std_hz": 22.0,
    "f0_mean_hz": 180.0,
    "laughter_likelihood": 0.0,
}


@pytest.mark.asyncio
class TestUtterancePairing:
    async def _cluster(self, monkeypatch, tmp_path):
        from brain.bus import Bus
        from brain.clusters.auditory_cortex import AuditoryCluster

        store = _patch_store(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "brain.clusters.auditory_cortex.extract_prosody", lambda a, sr: dict(FIXED_PROSODY)
        )
        cluster = AuditoryCluster(Bus())
        sid = store.enroll("Russ", _vec(1))
        return cluster, store, sid

    def _raw_msg(self, audio_bytes: bytes):
        return SimpleNamespace(payload={"audio_bytes": audio_bytes, "sample_rate": SR})

    async def test_raw_first_then_speaker(self, monkeypatch, tmp_path):
        from brain.clusters.auditory_cortex import _utterance_key

        cluster, store, sid = await self._cluster(monkeypatch, tmp_path)
        audio_bytes = (_speechlike() * 32767).astype(np.int16).tobytes()
        await cluster._process_raw(self._raw_msg(audio_bytes))
        assert store.get_prosody_baseline(sid) is None  # speaker not known yet
        await cluster._handle_speaker_embedding(
            _vec(1), None, [], duration_s=2.0, utt_key=_utterance_key(audio_bytes)
        )
        bl = store.get_prosody_baseline(sid)
        assert bl["count"] == 1 and bl["f0_std"] == pytest.approx(22.0)

    async def test_speaker_first_then_raw(self, monkeypatch, tmp_path):
        from brain.clusters.auditory_cortex import _utterance_key

        cluster, store, sid = await self._cluster(monkeypatch, tmp_path)
        audio_bytes = (_speechlike() * 32767).astype(np.int16).tobytes()
        await cluster._handle_speaker_embedding(
            _vec(1), None, [], duration_s=2.0, utt_key=_utterance_key(audio_bytes)
        )
        assert store.get_prosody_baseline(sid) is None
        await cluster._process_raw(self._raw_msg(audio_bytes))
        assert store.get_prosody_baseline(sid)["count"] == 1

    async def test_two_voice_utterance_calibrates_nobody(self, monkeypatch, tmp_path):
        from brain.clusters.auditory_cortex import _utterance_key

        cluster, store, sid = await self._cluster(monkeypatch, tmp_path)
        audio_bytes = (_speechlike() * 32767).astype(np.int16).tobytes()
        await cluster._process_raw(self._raw_msg(audio_bytes))
        await cluster._handle_speaker_embedding(
            _vec(1),
            None,
            [],
            duration_s=2.0,
            utt_key=_utterance_key(audio_bytes),
            sole_speaker=False,
        )
        assert store.get_prosody_baseline(sid) is None

    async def test_other_utterances_prosody_is_never_used(self, monkeypatch, tmp_path):
        from brain.clusters.auditory_cortex import _utterance_key

        cluster, store, sid = await self._cluster(monkeypatch, tmp_path)
        a = (_speechlike() * 32767).astype(np.int16).tobytes()
        b = (_speechlike(1.8) * 32767).astype(np.int16).tobytes()
        await cluster._process_raw(self._raw_msg(a))
        # Speaker result for a DIFFERENT utterance: no pairing, no update.
        await cluster._handle_speaker_embedding(
            _vec(1), None, [], duration_s=2.0, utt_key=_utterance_key(b)
        )
        assert store.get_prosody_baseline(sid) is None


# ── Hypothalamus: neutral prosody keeps the text cues ────────────────────────


async def _da_after(features: dict, prosody: dict | None) -> float:
    from brain.bus import Bus
    from brain.clusters.hypothalamus import HypothalamusCluster

    bus = Bus()
    hypo = HypothalamusCluster(bus)
    if prosody is not None:
        await bus.publish_dict("auditory.prosody", prosody, source="test")
    await hypo.process(features)
    return bus.neuromod.snapshot()["DA"]


def _voice(**extra) -> dict:
    return {
        "sentiment": 0.0,
        "hostility": 0.0,
        "salience": 0.3,
        "surprise_score": 0.0,
        "topic_summary": "test",
        "input_modality": "voice",
        **extra,
    }


@pytest.mark.asyncio
class TestTextCuesOnVoiceTurns:
    async def test_calm_prosody_keeps_text_warmth(self):
        from brain.settings import settings

        if not settings.get("enable_text_paralinguistics"):
            pytest.skip("text paralinguistics disabled")
        calm = {"tone_label": "calm", "tone_strength": 0.0}
        warm = {"text_paralinguistics": {"warmth": 1.0}}
        assert await _da_after(_voice(**warm), calm) > await _da_after(_voice(), calm) + 0.005

    async def test_real_acoustic_read_still_overrides_text(self):
        energetic = {"tone_label": "energetic", "tone_strength": 0.5}
        warm = {"text_paralinguistics": {"warmth": 1.0}}
        with_text = await _da_after(_voice(**warm), energetic)
        without = await _da_after(_voice(), energetic)
        assert abs(with_text - without) < 0.005

    async def test_text_laughter_not_double_counted_on_voice(self):
        calm = {"tone_label": "calm", "tone_strength": 0.0}
        laugh = {"text_paralinguistics": {"laughter": 1.0}}
        assert abs(await _da_after(_voice(**laugh), calm) - await _da_after(_voice(), calm)) < 0.005


# ── PANNs label CSV ──────────────────────────────────────────────────────────


class TestPannsLabels:
    def test_existing_complete_csv_is_reused(self, monkeypatch, tmp_path):
        import brain.clusters.vocal_events as ve

        monkeypatch.setenv("HOME", str(tmp_path))
        csv = tmp_path / "panns_data" / "class_labels_indices.csv"
        csv.parent.mkdir()
        csv.write_text(
            "index,mid,display_name\n" + "".join(f'{i},/m/{i},"L{i}"\n' for i in range(527)),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "urllib.request.urlretrieve", lambda *a, **k: pytest.fail("should not download")
        )
        assert ve._ensure_labels_csv() is True

    def test_failed_download_disables_without_leaving_partials(self, monkeypatch, tmp_path):
        import brain.clusters.vocal_events as ve

        monkeypatch.setenv("HOME", str(tmp_path))

        def boom(url, dest):
            with open(dest, "w") as f:
                f.write("partial")
            raise OSError("network down")

        monkeypatch.setattr("urllib.request.urlretrieve", boom)
        assert ve._ensure_labels_csv() is False
        assert not list((tmp_path / "panns_data").glob("*.partial"))
