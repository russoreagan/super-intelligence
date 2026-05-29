"""
Auditory Cortex — the brain's "ears".

Two independent processing loops:

  auditory.raw_audio (published immediately after recording):
    → Fingerprinting (Shazam-style) → auditory.song_match
    → Prosody extraction             → auditory.prosody

  auditory.diarized_audio (published after Deepgram returns, with per-word speaker labels):
    → Per-speaker audio slicing + ECAPA-TDNN embedding
    → SessionSpeakerRegistry: cross-turn identity tracking
    → Speaker identification / enrollment → auditory.speaker_id
                                          → auditory.enrollment_needed
                                          → auditory.enrollment_complete

Single-mic multi-speaker support:
  Deepgram diarize_model="latest" separates speakers within each audio chunk.
  The SessionSpeakerRegistry tracks identity across turns using embedding
  similarity — Deepgram's per-call labels reset, our session keys don't.
  Multiple speakers can be in enrollment-pending state simultaneously.

Activated by --ears flag or BRAIN_EARS=true env var.
No LLM calls — pure DSP + local model inference.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from brain.bus import Bus
from brain.clusters.audio_dsp import (
    SILENCE_RMS,
    compute_speech_dynamics,
    decode_audio,
    extract_identity_name,
    extract_prosody,
    extract_speaker_audio_segments,
    extract_speaker_embedding,
    label_prosody_tone,
    match_fingerprint,
)
from brain.second_brain.speaker_store import SpeakerStore
from brain.settings import settings as _settings

logger = logging.getLogger(__name__)

CLUSTER = "auditory_cortex"

_FINGERPRINT_DB_PATH = Path(__file__).parent.parent / "second_brain" / "audio_fingerprints.json"

# Minimum per-speaker audio to bother embedding (seconds)
_MIN_SPEAKER_AUDIO_S: float = float(__import__("os").environ.get("AUDIO_SPEAKER_MIN_S", "0.4"))

# Cosine similarity thresholds (env or settings, settings wins if set)
_STORE_THRESHOLD = float(__import__("os").environ.get("AUDIO_SPEAKER_THRESHOLD", "0.70"))
_SESSION_THRESHOLD = 0.62  # lower — same session, fewer confounders


def _store_threshold() -> float:
    return float(_settings.get("speaker_store_threshold"))


def _session_threshold() -> float:
    return float(_settings.get("speaker_session_threshold"))


# ── Session speaker tracking ───────────────────────────────────────────────────


@dataclasses.dataclass
class SessionSpeaker:
    session_key: str  # stable within-session label: "spk_0", "spk_1", ...
    store_id: str | None  # speaker store profile ID, if matched
    store_name: str | None  # display name
    embedding_mean: np.ndarray  # running L2-normalised mean
    sample_count: int
    enrollment_pending: bool  # True = waiting for the user to say their name
    first_seen_ts: float
    last_seen_ts: float
    closest_match: str | None = None  # nearest store profile that fell below threshold
    enrollment_prompted: bool = (
        False  # True once we've asked this voice for a name (ask once, don't spam)
    )


class SessionSpeakerRegistry:
    """
    Maps voice embeddings to stable per-session speaker identities.

    Deepgram's speaker labels reset each API call; this registry provides
    continuity by comparing new embeddings against all speakers seen this
    session (and against the persistent speaker store).
    """

    def __init__(self, speaker_store: SpeakerStore) -> None:
        self._store = speaker_store
        self._speakers: dict[str, SessionSpeaker] = {}
        self._next_idx = 0

    # ── Public interface ────────────────────────────────────────────────────

    def match_or_create(self, embedding: np.ndarray) -> tuple[SessionSpeaker, bool, float]:
        """
        Find the session speaker whose voice best matches this embedding.
        Creates a new entry (enrollment_pending=True) if no match found.
        Returns (session_speaker, is_new_this_turn, similarity_score).
        """
        # 1. Check persistent store (pre-enrolled, cross-session)
        store_id, store_name, store_score, store_status = self._store.identify(
            embedding, threshold=_store_threshold()
        )
        if store_status == "recognized":
            spk = self._get_or_create_known(store_id, store_name, embedding)
            if store_id and spk.store_id:
                self._store.update(store_id, embedding)
            return spk, False, store_score

        # 2. Check session speakers by embedding similarity
        best_spk, best_score = self._find_best_session_match(embedding)
        if best_spk is not None and best_score >= _session_threshold():
            self._update_mean(best_spk, embedding)
            best_spk.last_seen_ts = time.time()
            return best_spk, False, best_score

        # 3. New speaker this session — store_name here is the closest sub-threshold profile
        spk = SessionSpeaker(
            session_key=self._new_key(),
            store_id=None,
            store_name=None,
            embedding_mean=embedding.copy(),
            sample_count=1,
            enrollment_pending=True,
            first_seen_ts=time.time(),
            last_seen_ts=time.time(),
            closest_match=store_name,  # nearest profile that didn't clear the bar
        )
        self._speakers[spk.session_key] = spk
        return spk, True, store_score

    def complete_enrollment(self, session_key: str, name: str) -> dict:
        """
        Link a session speaker to a name.
        If a store profile already exists for that name, merges the voice into it.
        Otherwise creates a new profile.
        """
        spk = self._speakers.get(session_key)
        if spk is None:
            return {"action": "not_found", "session_key": session_key, "name": name}
        if not spk.enrollment_pending:
            # Already enrolled this turn (e.g. both auto-detect and run.py fired) — no-op
            return {
                "action": "already_done",
                "session_key": session_key,
                "name": spk.store_name,
                "speaker_id": spk.store_id,
            }

        existing_id = self._store.find_by_name(name)
        if existing_id:
            self._store.update(existing_id, spk.embedding_mean)
            spk.store_id = existing_id
            spk.store_name = name
            action = "merged"
            logger.info(
                "Auditory: voice re-linked to existing profile '%s' (%s)", name, existing_id
            )
        else:
            sid = self._store.enroll(name, spk.embedding_mean)
            spk.store_id = sid
            spk.store_name = name
            action = "enrolled"
            logger.info("Auditory: new profile created for '%s' (%s)", name, sid)

        spk.enrollment_pending = False
        return {
            "action": action,
            "session_key": session_key,
            "name": name,
            "speaker_id": spk.store_id,
        }

    def cancel_enrollment(self, session_key: str) -> None:
        spk = self._speakers.get(session_key)
        if spk:
            spk.enrollment_pending = False

    def pending_enrollments(self) -> list[SessionSpeaker]:
        return [s for s in self._speakers.values() if s.enrollment_pending]

    def mark_prompted(self, session_key: str) -> None:
        """Record that we've already asked this voice for a name, so the
        'who are you?' prompt fires once rather than every turn."""
        spk = self._speakers.get(session_key)
        if spk is not None:
            spk.enrollment_prompted = True

    def all_speakers(self) -> list[SessionSpeaker]:
        return list(self._speakers.values())

    # ── Internal helpers ────────────────────────────────────────────────────

    def _new_key(self) -> str:
        k = f"spk_{self._next_idx}"
        self._next_idx += 1
        return k

    def _get_or_create_known(
        self, store_id: str, store_name: str | None, embedding: np.ndarray
    ) -> SessionSpeaker:
        for spk in self._speakers.values():
            if spk.store_id == store_id:
                self._update_mean(spk, embedding)
                spk.last_seen_ts = time.time()
                return spk
        spk = SessionSpeaker(
            session_key=self._new_key(),
            store_id=store_id,
            store_name=store_name,
            embedding_mean=embedding.copy(),
            sample_count=1,
            enrollment_pending=False,
            first_seen_ts=time.time(),
            last_seen_ts=time.time(),
        )
        self._speakers[spk.session_key] = spk
        return spk

    def _find_best_session_match(
        self, embedding: np.ndarray
    ) -> tuple[SessionSpeaker | None, float]:
        best_spk: SessionSpeaker | None = None
        best_score = -1.0
        for spk in self._speakers.values():
            score = float(np.dot(embedding, spk.embedding_mean))
            if score > best_score:
                best_score = score
                best_spk = spk
        return best_spk, best_score

    @staticmethod
    def _update_mean(spk: SessionSpeaker, embedding: np.ndarray) -> None:
        n = min(spk.sample_count, 20)
        merged = (spk.embedding_mean * n + embedding) / (n + 1)
        norm = np.linalg.norm(merged)
        spk.embedding_mean = merged / norm if norm > 0 else merged
        spk.sample_count = n + 1


# ── Main cluster ───────────────────────────────────────────────────────────────


class AuditoryCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._raw_inbox: asyncio.Queue = bus.subscribe("auditory.raw_audio")
        self._diarized_inbox: asyncio.Queue = bus.subscribe("auditory.diarized_audio")
        self._speaker_store = SpeakerStore()
        self._registry = SessionSpeakerRegistry(self._speaker_store)

        # Cross-loop state for per-speaker prosody calibration.
        # The raw loop extracts prosody; the diarized loop identifies the speaker.
        # We share the last known values so each loop can update the other's work.
        self._last_speaker_id: str | None = None  # set by diarized loop
        self._last_prosody_features: dict | None = None  # set by raw loop

        # Fingerprint DB (hash_val → [(song_id, ref_time_frame), ...])
        self._fp_db: dict[int, list[tuple[str, int]]] = {}
        self._songs: dict[str, dict] = {}
        self._load_fingerprint_db()

    def _load_fingerprint_db(self) -> None:
        if not _FINGERPRINT_DB_PATH.exists():
            return
        try:
            with open(_FINGERPRINT_DB_PATH) as f:
                data = json.load(f)
            self._songs = data.get("songs", {})
            self._fp_db = {
                int(k): [tuple(pair) for pair in v]  # type: ignore[misc]
                for k, v in data.get("hashes", {}).items()
            }
            logger.info(
                "Auditory: fingerprint DB — %d songs, %d hash buckets",
                len(self._songs),
                len(self._fp_db),
            )
        except Exception as e:
            logger.warning("Auditory: fingerprint DB load failed: %s", e)

    async def run(self) -> None:
        """Start both processing loops concurrently."""
        logger.info("Auditory Cortex started (single-mic multi-speaker mode)")
        await asyncio.gather(
            self._run_raw_loop(),
            self._run_diarized_loop(),
        )

    # ── Raw audio loop: fingerprinting + prosody ───────────────────────────

    async def _run_raw_loop(self) -> None:
        while True:
            try:
                msg = await asyncio.wait_for(self._raw_inbox.get(), timeout=60.0)
            except TimeoutError:
                continue
            except Exception as e:
                logger.error("Auditory raw loop error: %s", e)
                continue
            if not msg.expired:
                asyncio.create_task(self._process_raw(msg))

    async def _process_raw(self, msg) -> None:
        payload = msg.payload
        audio_bytes: bytes = payload.get("audio_bytes", b"")
        sr: int = payload.get("sample_rate", 16000)
        if not audio_bytes:
            return

        try:
            audio = decode_audio(audio_bytes, dtype=payload.get("dtype", "int16"))
        except Exception as e:
            logger.debug("Auditory: decode error: %s", e)
            return

        rms = float(np.sqrt(np.mean(audio**2)))
        if rms < SILENCE_RMS:
            return

        loop = asyncio.get_event_loop()
        fp_db, songs = self._fp_db, self._songs

        fp_result, pros_result = await asyncio.gather(
            loop.run_in_executor(None, match_fingerprint, audio, sr, fp_db),
            loop.run_in_executor(None, extract_prosody, audio, sr),
            return_exceptions=True,
        )

        if not isinstance(fp_result, BaseException):
            best_id = fp_result.pop("_best_song_id", None)
            if fp_result.get("matched") and best_id:
                meta = songs.get(best_id, {})
                fp_result["song_id"] = best_id
                fp_result["song_title"] = meta.get("title")
                fp_result["song_artist"] = meta.get("artist")
            logger.debug(
                "Auditory: song match=%s conf=%.3f",
                fp_result.get("matched"),
                fp_result.get("confidence", 0),
            )
            await self._bus.publish_dict("auditory.song_match", fp_result, source=CLUSTER)

        if not isinstance(pros_result, BaseException):
            self._last_prosody_features = pros_result

            # Re-label using the current speaker's personal baseline if available.
            # Falls back to universal thresholds when uncalibrated (< 10 obs).
            baseline = None
            if self._last_speaker_id:
                baseline = self._speaker_store.get_prosody_baseline(self._last_speaker_id)
            if baseline is not None:
                pros_result["tone_label"] = label_prosody_tone(pros_result, baseline)

            logger.debug(
                "Auditory: prosody tone=%s f0=%.0f energy=%.3f voiced=%.2f",
                pros_result.get("tone_label"),
                pros_result.get("f0_mean_hz", 0),
                pros_result.get("energy_mean", 0),
                pros_result.get("voiced_fraction", 0),
            )
            await self._bus.publish_dict("auditory.prosody", pros_result, source=CLUSTER)

    # ── Diarized audio loop: per-speaker ID + enrollment ──────────────────

    async def _run_diarized_loop(self) -> None:
        while True:
            try:
                msg = await asyncio.wait_for(self._diarized_inbox.get(), timeout=60.0)
            except TimeoutError:
                continue
            except Exception as e:
                logger.error("Auditory diarized loop error: %s", e)
                continue
            if not msg.expired:
                asyncio.create_task(self._process_diarized(msg))

    async def _process_diarized(self, msg) -> None:
        payload = msg.payload
        audio_bytes: bytes = payload.get("audio_bytes", b"")
        sr: int = payload.get("sample_rate", 16000)
        diarized_words: list[dict] = payload.get("diarized_words", [])

        if not audio_bytes:
            return

        try:
            audio = decode_audio(audio_bytes, dtype=payload.get("dtype", "int16"))
        except Exception as e:
            logger.debug("Auditory: diarized decode error: %s", e)
            return

        loop = asyncio.get_event_loop()

        if not diarized_words:
            # Fallback: treat entire audio as a single speaker
            await self._process_single_speaker_audio(audio, sr, loop)
            return

        # pace_switch + pause_distribution_switch: derive timing-based features
        # from the diarized words and publish them for the rest of the brain.
        dyn = compute_speech_dynamics(diarized_words)
        logger.debug(
            "Auditory: dynamics wpm=%.0f pace=%s pauses=%d burst=%.2f",
            dyn["wpm"],
            dyn["pace_label"],
            dyn["long_pause_count"],
            dyn["burst_score"],
        )
        await self._bus.publish_dict("auditory.speech_dynamics", dyn, source=CLUSTER)

        # Group word dicts by Deepgram speaker label
        by_speaker: dict[int, list[dict]] = defaultdict(list)
        for w in diarized_words:
            by_speaker[w.get("speaker", 0)].append(w)

        # Process each Deepgram speaker independently
        for deepgram_label, words in sorted(by_speaker.items()):
            await self._process_one_speaker(audio, sr, words, deepgram_label, loop)

    async def _process_single_speaker_audio(self, audio: np.ndarray, sr: int, loop) -> None:
        """Fallback when no diarization data is available."""
        rms = float(np.sqrt(np.mean(audio**2)))
        if rms < SILENCE_RMS:
            return
        embedding = await loop.run_in_executor(None, extract_speaker_embedding, audio, sr)
        if embedding is not None:
            await self._handle_speaker_embedding(embedding, None, [])

    async def _process_one_speaker(
        self,
        audio: np.ndarray,
        sr: int,
        words: list[dict],
        deepgram_label: int,
        loop,
    ) -> None:
        """Extract, embed, and identify one Deepgram speaker."""
        # Slice audio to this speaker's words only
        spk_audio = extract_speaker_audio_segments(audio, sr, words)
        if len(spk_audio) < int(sr * float(_settings.get("speaker_min_audio_s"))):
            logger.debug("Auditory: Deepgram spk %d — too little audio, skipping", deepgram_label)
            return

        embedding = await loop.run_in_executor(None, extract_speaker_embedding, spk_audio, sr)
        if embedding is None:
            return

        speaker_text = " ".join(w.get("word", "") for w in words)
        await self._handle_speaker_embedding(embedding, speaker_text, words)

    async def _handle_speaker_embedding(
        self,
        embedding: np.ndarray,
        speaker_text: str | None,
        words: list[dict],
    ) -> None:
        """Match embedding against registry; handle ID or enrollment."""
        session_spk, is_new, match_score = self._registry.match_or_create(embedding)

        # Update cross-loop speaker state and prosody baseline.
        if session_spk.store_id:
            self._last_speaker_id = session_spk.store_id
            if self._last_prosody_features:
                self._speaker_store.update_prosody_baseline(
                    session_spk.store_id, self._last_prosody_features
                )

        spk_payload = {
            "session_key": session_spk.session_key,
            "identified": session_spk.store_id is not None,
            "speaker_id": session_spk.store_id,
            "speaker_name": session_spk.store_name,
            "closest_match": session_spk.closest_match,
            "enrollment_pending": session_spk.enrollment_pending,
            "is_new_this_session": is_new,
            "match_score": round(match_score, 4),
        }
        logger.debug(
            "Auditory: session=%s name=%s enrolled=%s new=%s",
            session_spk.session_key,
            session_spk.store_name,
            not session_spk.enrollment_pending,
            is_new,
        )
        await self._bus.publish_dict("auditory.speaker_id", spk_payload, source=CLUSTER)

        if is_new:
            # Notify that a new unknown voice needs enrollment
            closest = session_spk.closest_match  # nearest sub-threshold profile, or None
            await self._bus.publish_dict(
                "auditory.enrollment_needed",
                {
                    "state": "awaiting_name",
                    "session_key": session_spk.session_key,
                    "closest_match_name": closest,
                },
                source=CLUSTER,
            )
            logger.info(
                "Auditory: new voice %s (closest=%s) — enrollment started",
                session_spk.session_key,
                closest,
            )

        # Auto-detect "I'm Alice" when this speaker has pending enrollment
        if session_spk.enrollment_pending and speaker_text:
            name = extract_identity_name(speaker_text)
            if name:
                result = self._registry.complete_enrollment(session_spk.session_key, name)
                await self._bus.publish_dict(
                    "auditory.enrollment_complete",
                    result,
                    source=CLUSTER,
                )
                logger.info(
                    "Auditory: auto-enrolled %s as '%s' (action=%s)",
                    session_spk.session_key,
                    name,
                    result["action"],
                )

    # ── Public API for run.py ──────────────────────────────────────────────

    @property
    def enrollment_pending_speakers(self) -> list[SessionSpeaker]:
        return self._registry.pending_enrollments()

    def complete_enrollment(self, session_key: str, name: str) -> dict:
        """Complete enrollment for a specific session speaker (called by run.py)."""
        return self._registry.complete_enrollment(session_key, name)

    def cancel_enrollment(self, session_key: str) -> None:
        self._registry.cancel_enrollment(session_key)

    def mark_enrollment_prompted(self, session_key: str) -> None:
        """Mark that we've asked this voice for a name (ask-once guard)."""
        self._registry.mark_prompted(session_key)
