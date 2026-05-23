"""
Integration tests for the Auditory Cortex cluster and SpeakerStore.

No microphone, no API keys, no SpeechBrain model download required
(speaker ID tests that need the model are skipped if speechbrain unavailable).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

SR = 16000


def _sine_bytes(freq: float = 440.0, duration: float = 3.0, amplitude: float = 0.4) -> bytes:
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    audio = (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype(np.int16)
    return audio.tobytes()


def _silence_bytes(duration: float = 3.0) -> bytes:
    return bytes(int(SR * duration) * 2)  # int16 zeros


# ── SpeakerStore ──────────────────────────────────────────────────────────────

class TestSpeakerStore:
    def test_enroll_and_identify(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore

        store = SpeakerStore(profiles_dir=tmp_path)
        vec = np.random.randn(192).astype(np.float32)
        vec /= np.linalg.norm(vec)

        sid = store.enroll("Alice", vec)
        assert len(sid) == 36  # UUID

        s_id, name, score, status = store.identify(vec, threshold=0.70)
        assert status == "recognized"
        assert name == "Alice"
        assert score > 0.99

    def test_unknown_speaker(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore

        store = SpeakerStore(profiles_dir=tmp_path)
        vec = np.random.randn(192).astype(np.float32)
        vec /= np.linalg.norm(vec)
        store.enroll("Alice", vec)

        noise = np.random.randn(192).astype(np.float32)
        noise /= np.linalg.norm(noise)
        _, _, score, status = store.identify(noise, threshold=0.70)
        assert status == "unknown"
        assert score < 0.70

    def test_no_profiles(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore

        store = SpeakerStore(profiles_dir=tmp_path)
        _, _, score, status = store.identify(np.ones(192, dtype=np.float32))
        assert status == "no_profiles"

    def test_incremental_update(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore

        store = SpeakerStore(profiles_dir=tmp_path)
        vec = np.random.randn(192).astype(np.float32)
        vec /= np.linalg.norm(vec)
        sid = store.enroll("Bob", vec)
        assert store._profiles[sid]["sample_count"] == 1

        new_vec = np.random.randn(192).astype(np.float32)
        new_vec /= np.linalg.norm(new_vec)
        store.update(sid, new_vec)
        assert store._profiles[sid]["sample_count"] == 2

    def test_persistence_across_reload(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore

        store = SpeakerStore(profiles_dir=tmp_path)
        vec = np.random.randn(192).astype(np.float32)
        vec /= np.linalg.norm(vec)
        sid = store.enroll("Carol", vec)

        # Reload from disk
        store2 = SpeakerStore(profiles_dir=tmp_path)
        assert sid in store2._profiles
        assert store2._profiles[sid]["name"] == "Carol"

        _, name, score, status = store2.identify(vec, threshold=0.70)
        assert status == "recognized"
        assert name == "Carol"

    def test_list_speakers(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore

        store = SpeakerStore(profiles_dir=tmp_path)
        for name in ["Alice", "Bob", "Carol"]:
            vec = np.random.randn(192).astype(np.float32)
            store.enroll(name, vec)

        speakers = store.list_speakers()
        assert len(speakers) == 3
        names = {s["name"] for s in speakers}
        assert names == {"Alice", "Bob", "Carol"}


# ── Fingerprint pipeline ──────────────────────────────────────────────────────

class TestFingerprintPipeline:
    def test_match_fingerprint_self(self):
        from brain.clusters.audio_dsp import (
            decode_audio, compute_spectrogram, extract_peaks,
            generate_hashes, match_fingerprint,
        )
        audio_bytes = _sine_bytes(440.0, duration=5.0)
        audio = decode_audio(audio_bytes)
        spec = compute_spectrogram(audio, SR)
        peaks = extract_peaks(spec)
        hashes = generate_hashes(peaks)

        db: dict[int, list] = {}
        for h, t in hashes:
            db.setdefault(h, []).append(["song_001", t])

        result = match_fingerprint(audio, SR, db)
        assert result["matched"] is True
        assert result["confidence"] > 0.05

    def test_no_match_empty_db(self):
        from brain.clusters.audio_dsp import decode_audio, match_fingerprint
        audio = decode_audio(_sine_bytes())
        result = match_fingerprint(audio, SR, {})
        assert result["matched"] is False


# ── AuditoryCluster bus integration ──────────────────────────────────────────

def _patch_store(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "brain.clusters.auditory_cortex.SpeakerStore",
        lambda: __import__("brain.second_brain.speaker_store",
                           fromlist=["SpeakerStore"]).SpeakerStore(tmp_path),
    )


@pytest.mark.asyncio
class TestAuditoryCluster:
    async def test_silence_gate_no_publish(self, tmp_path, monkeypatch):
        """Silent audio should not publish fingerprint/prosody messages."""
        from brain.bus import Bus
        from brain.clusters.auditory_cortex import AuditoryCluster

        _patch_store(monkeypatch, tmp_path)
        bus = Bus()
        song_q = bus.subscribe("auditory.song_match")
        pros_q = bus.subscribe("auditory.prosody")

        cluster = AuditoryCluster(bus)
        await bus.publish_dict(
            "auditory.raw_audio",
            {"audio_bytes": _silence_bytes(2.0), "sample_rate": SR, "dtype": "int16",
             "duration_s": 2.0, "channels": 1},
            source="pns",
        )
        msg = await asyncio.wait_for(cluster._raw_inbox.get(), timeout=1.0)
        await cluster._process_raw(msg)

        assert song_q.empty()
        assert pros_q.empty()

    async def test_prosody_published_for_tone(self, tmp_path, monkeypatch):
        """A real tone should publish auditory.prosody."""
        pytest.importorskip("scipy")
        from brain.bus import Bus
        from brain.clusters.auditory_cortex import AuditoryCluster

        _patch_store(monkeypatch, tmp_path)
        bus = Bus()
        pros_q = bus.subscribe("auditory.prosody")

        cluster = AuditoryCluster(bus)
        await bus.publish_dict(
            "auditory.raw_audio",
            {"audio_bytes": _sine_bytes(440.0, duration=3.0), "sample_rate": SR,
             "dtype": "int16", "duration_s": 3.0, "channels": 1},
            source="pns",
        )
        msg = await asyncio.wait_for(cluster._raw_inbox.get(), timeout=1.0)
        await cluster._process_raw(msg)

        assert not pros_q.empty()
        payload = pros_q.get_nowait().payload
        assert "tone_label" in payload
        assert "energy_mean" in payload

    async def test_song_match_published_when_in_db(self, tmp_path, monkeypatch):
        """Audio whose hashes are in the DB should publish auditory.song_match."""
        pytest.importorskip("scipy")
        from brain.bus import Bus
        from brain.clusters.auditory_cortex import AuditoryCluster
        from brain.clusters.audio_dsp import (
            decode_audio, compute_spectrogram, extract_peaks, generate_hashes,
        )

        audio_bytes = _sine_bytes(880.0, duration=5.0)
        audio = decode_audio(audio_bytes)
        peaks = extract_peaks(compute_spectrogram(audio, SR))
        hashes = generate_hashes(peaks)

        db_path = tmp_path / "audio_fingerprints.json"
        db_data = {"songs": {"test_001": {"title": "Test Song", "artist": "Test"}}, "hashes": {}}
        for h, t in hashes:
            db_data["hashes"].setdefault(str(h), []).append(["test_001", t])
        with open(db_path, "w") as f:
            json.dump(db_data, f)

        monkeypatch.setattr("brain.clusters.auditory_cortex._FINGERPRINT_DB_PATH", db_path)
        _patch_store(monkeypatch, tmp_path)

        bus = Bus()
        match_q = bus.subscribe("auditory.song_match")

        cluster = AuditoryCluster(bus)
        await bus.publish_dict(
            "auditory.raw_audio",
            {"audio_bytes": audio_bytes, "sample_rate": SR, "dtype": "int16",
             "duration_s": 5.0, "channels": 1},
            source="pns",
        )
        msg = await asyncio.wait_for(cluster._raw_inbox.get(), timeout=1.0)
        await cluster._process_raw(msg)

        assert not match_q.empty()
        payload = match_q.get_nowait().payload
        assert payload["matched"] is True
        assert payload["song_id"] == "test_001"


# ── SessionSpeakerRegistry: cross-turn identity tracking ──────────────────────

class TestSessionSpeakerRegistry:
    def _registry(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore
        from brain.clusters.auditory_cortex import SessionSpeakerRegistry
        return SessionSpeakerRegistry(SpeakerStore(tmp_path))

    def _vec(self, seed: int):
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(192).astype(np.float32)
        return v / np.linalg.norm(v)

    def test_new_speaker_is_pending(self, tmp_path):
        reg = self._registry(tmp_path)
        spk, is_new = reg.match_or_create(self._vec(1))
        assert is_new is True
        assert spk.enrollment_pending is True
        assert spk.store_id is None

    def test_same_voice_tracked_across_turns(self, tmp_path):
        """The same embedding should map to the same session speaker, not a new one."""
        reg = self._registry(tmp_path)
        v = self._vec(1)
        spk1, new1 = reg.match_or_create(v)
        spk2, new2 = reg.match_or_create(v)
        assert new1 is True
        assert new2 is False
        assert spk1.session_key == spk2.session_key

    def test_two_distinct_voices_get_distinct_keys(self, tmp_path):
        reg = self._registry(tmp_path)
        spk_a, _ = reg.match_or_create(self._vec(1))
        spk_b, _ = reg.match_or_create(self._vec(99))
        assert spk_a.session_key != spk_b.session_key
        assert len(reg.pending_enrollments()) == 2

    def test_complete_enrollment_new_person(self, tmp_path):
        reg = self._registry(tmp_path)
        spk, _ = reg.match_or_create(self._vec(1))
        result = reg.complete_enrollment(spk.session_key, "Alice")
        assert result["action"] == "enrolled"
        assert spk.enrollment_pending is False
        assert spk.store_name == "Alice"
        # After enrollment, the same voice is recognised (no longer pending)
        assert len(reg.pending_enrollments()) == 0

    def test_complete_enrollment_merges_known_name(self, tmp_path):
        """If a profile with that name exists, the voice links to it (re-link case)."""
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(tmp_path)
        store.enroll("Russ", self._vec(42))  # pre-existing profile
        from brain.clusters.auditory_cortex import SessionSpeakerRegistry
        reg = SessionSpeakerRegistry(store)

        spk, _ = reg.match_or_create(self._vec(7))  # different-sounding voice
        result = reg.complete_enrollment(spk.session_key, "Russ")
        assert result["action"] == "merged"
        assert spk.store_name == "Russ"

    def test_idempotent_complete(self, tmp_path):
        reg = self._registry(tmp_path)
        spk, _ = reg.match_or_create(self._vec(1))
        reg.complete_enrollment(spk.session_key, "Alice")
        again = reg.complete_enrollment(spk.session_key, "Alice")
        assert again["action"] == "already_done"

    def test_known_speaker_skips_enrollment(self, tmp_path):
        """A pre-enrolled voice is recognised immediately, never pending."""
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(tmp_path)
        v = self._vec(5)
        store.enroll("Bob", v)
        from brain.clusters.auditory_cortex import SessionSpeakerRegistry
        reg = SessionSpeakerRegistry(store)
        spk, is_new = reg.match_or_create(v)
        assert spk.store_name == "Bob"
        assert spk.enrollment_pending is False


# ── Multi-speaker diarized enrollment flow ────────────────────────────────────

@pytest.mark.asyncio
class TestDiarizedEnrollment:
    async def test_auto_enroll_from_identity_statement(self, tmp_path, monkeypatch):
        """A pending speaker who says 'I'm Alice' is auto-enrolled."""
        from brain.bus import Bus
        from brain.clusters.auditory_cortex import AuditoryCluster

        _patch_store(monkeypatch, tmp_path)
        bus = Bus()
        complete_q = bus.subscribe("auditory.enrollment_complete")
        cluster = AuditoryCluster(bus)

        emb = np.random.default_rng(3).standard_normal(192).astype(np.float32)
        emb /= np.linalg.norm(emb)

        # First contact: unknown → pending
        await cluster._handle_speaker_embedding(emb, "hello there", [])
        assert len(cluster.enrollment_pending_speakers) == 1

        # Same voice says their name → auto-enroll
        await cluster._handle_speaker_embedding(emb, "I'm Alice", [])
        assert len(cluster.enrollment_pending_speakers) == 0
        assert not complete_q.empty()
        # drain to the completion
        payload = None
        while not complete_q.empty():
            payload = complete_q.get_nowait().payload
        assert payload["action"] == "enrolled"
        assert payload["name"] == "Alice"


# ── label_prosody_tone — per-speaker calibration ─────────────────────────────

class TestLabelProsodyTone:
    """Tests for the extracted tone labelling function with and without baseline."""

    def _features(self, **overrides) -> dict:
        base = {
            "voiced_fraction": 0.7,
            "f0_std_hz": 30.0,
            "energy_mean": 0.08,
            "energy_std": 0.03,
            "speech_rate_hz": 3.0,
            "jitter": 0.0,
            "shimmer": 0.0,
        }
        base.update(overrides)
        return base

    def test_calm_default(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        assert label_prosody_tone(self._features()) == "calm"

    def test_whisper_low_voiced_fraction(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        assert label_prosody_tone(self._features(voiced_fraction=0.1)) == "whisper"

    def test_monotone_flat_pitch_and_energy(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        assert label_prosody_tone(self._features(f0_std_hz=10.0, energy_std=0.01)) == "monotone"

    def test_energetic_high_energy_and_rate(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        assert label_prosody_tone(self._features(energy_mean=0.15, speech_rate_hz=5.0)) == "energetic"

    def test_stressed_requires_both_jitter_and_shimmer(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        # Only jitter elevated → not stressed
        assert label_prosody_tone(self._features(jitter=0.05, shimmer=0.01)) == "calm"
        # Only shimmer elevated → not stressed
        assert label_prosody_tone(self._features(jitter=0.01, shimmer=0.08)) == "calm"
        # Both elevated → stressed
        assert label_prosody_tone(self._features(jitter=0.05, shimmer=0.08)) == "stressed"

    def test_uncalibrated_baseline_uses_universal_thresholds(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        baseline = {"jitter": 0.01, "shimmer": 0.01, "energy_mean": 0.05,
                    "f0_std": 20.0, "count": 5}  # < 10 → not calibrated
        # Universal stressed threshold still applies
        assert label_prosody_tone(
            self._features(jitter=0.05, shimmer=0.08), baseline=baseline
        ) == "stressed"

    def test_calibrated_baseline_raises_stressed_threshold(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        # Speaker whose natural jitter=0.04, shimmer=0.06 — above universal values
        # but normal for them. With calibration, 1.8x means threshold is ~0.072/0.108.
        baseline = {"jitter": 0.04, "shimmer": 0.06, "energy_mean": 0.08,
                    "f0_std": 25.0, "count": 15}
        # At their baseline → should be calm
        assert label_prosody_tone(
            self._features(jitter=0.04, shimmer=0.06), baseline=baseline
        ) == "calm"
        # Well above their baseline → stressed
        assert label_prosody_tone(
            self._features(jitter=0.09, shimmer=0.12), baseline=baseline
        ) == "stressed"

    def test_calibrated_baseline_raises_energetic_threshold(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        # Speaker with naturally high energy_mean=0.10 → threshold becomes 0.15
        baseline = {"jitter": 0.01, "shimmer": 0.02, "energy_mean": 0.10,
                    "f0_std": 25.0, "count": 20}
        # energy_mean=0.13, rate=5.0 — above universal threshold but below theirs
        assert label_prosody_tone(
            self._features(energy_mean=0.13, speech_rate_hz=5.0), baseline=baseline
        ) == "calm"
        # energy_mean=0.17 — above their calibrated threshold
        assert label_prosody_tone(
            self._features(energy_mean=0.17, speech_rate_hz=5.0), baseline=baseline
        ) == "energetic"

    def test_floor_prevents_threshold_below_universal(self):
        from brain.clusters.audio_dsp import label_prosody_tone
        # Very low baseline values — floors at universal thresholds
        baseline = {"jitter": 0.001, "shimmer": 0.001, "energy_mean": 0.001,
                    "f0_std": 5.0, "count": 20}
        # jitter=0.04, shimmer=0.06 → above the floor (0.03, 0.05) → stressed
        assert label_prosody_tone(
            self._features(jitter=0.04, shimmer=0.06), baseline=baseline
        ) == "stressed"


# ── SpeakerStore prosody baseline ────────────────────────────────────────────

class TestSpeakerStoreProsodyBaseline:
    def test_get_prosody_baseline_none_before_any_update(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(profiles_dir=tmp_path)
        sid = store.enroll("Russ", np.random.randn(192).astype(np.float32))
        assert store.get_prosody_baseline(sid) is None

    def test_update_increments_count(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(profiles_dir=tmp_path)
        sid = store.enroll("Russ", np.random.randn(192).astype(np.float32))
        features = {"jitter": 0.03, "shimmer": 0.04, "energy_mean": 0.09, "f0_std": 25.0}
        store.update_prosody_baseline(sid, features)
        bl = store.get_prosody_baseline(sid)
        assert bl["count"] == 1
        store.update_prosody_baseline(sid, features)
        bl = store.get_prosody_baseline(sid)
        assert bl["count"] == 2

    def test_running_mean_converges(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(profiles_dir=tmp_path)
        sid = store.enroll("Russ", np.random.randn(192).astype(np.float32))
        for _ in range(20):
            store.update_prosody_baseline(sid, {
                "jitter": 0.04, "shimmer": 0.06, "energy_mean": 0.08, "f0_std": 22.0
            })
        bl = store.get_prosody_baseline(sid)
        assert abs(bl["jitter"] - 0.04) < 0.005
        assert abs(bl["shimmer"] - 0.06) < 0.005
        assert bl["count"] == 20

    def test_unknown_speaker_is_noop(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(profiles_dir=tmp_path)
        store.update_prosody_baseline("nonexistent-id", {"jitter": 0.05})
        assert store.get_prosody_baseline("nonexistent-id") is None

    def test_zero_values_not_included_in_mean(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(profiles_dir=tmp_path)
        sid = store.enroll("Russ", np.random.randn(192).astype(np.float32))
        # First update with real values
        store.update_prosody_baseline(sid, {
            "jitter": 0.04, "shimmer": 0.06, "energy_mean": 0.08, "f0_std": 20.0
        })
        # Second update with zero jitter/shimmer (unvoiced frame — should be skipped)
        store.update_prosody_baseline(sid, {
            "jitter": 0.0, "shimmer": 0.0, "energy_mean": 0.07, "f0_std": 19.0
        })
        bl = store.get_prosody_baseline(sid)
        # jitter and shimmer should still reflect only the non-zero observation
        assert bl["jitter"] > 0.03
        assert bl["shimmer"] > 0.05

    def test_persists_across_reload(self, tmp_path):
        from brain.second_brain.speaker_store import SpeakerStore
        store = SpeakerStore(profiles_dir=tmp_path)
        sid = store.enroll("Russ", np.random.randn(192).astype(np.float32))
        for _ in range(12):
            store.update_prosody_baseline(sid, {
                "jitter": 0.035, "shimmer": 0.055, "energy_mean": 0.09, "f0_std": 24.0
            })
        store2 = SpeakerStore(profiles_dir=tmp_path)
        bl = store2.get_prosody_baseline(sid)
        assert bl is not None
        assert bl["count"] == 12
        assert abs(bl["jitter"] - 0.035) < 0.005
