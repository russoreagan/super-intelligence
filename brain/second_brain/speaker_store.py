"""
Speaker profile store — cross-session voiceprint persistence.

Stores 192-dim ECAPA-TDNN embeddings as JSON files in
brain/second_brain/speaker_profiles/.

Each file: <speaker_id>.json with fields:
  speaker_id, name, embedding, sample_count, enrolled_ts, updated_ts

Cosine similarity identifies speakers across sessions.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_PROFILES_DIR = Path(__file__).parent / "speaker_profiles"
_THRESHOLD_DEFAULT = float(os.environ.get("AUDIO_SPEAKER_THRESHOLD", "0.70"))
_MAX_SAMPLE_COUNT = 20  # cap incremental mean to prevent drift


class SpeakerStore:
    def __init__(self, profiles_dir: Path | str | None = None) -> None:
        self._dir = Path(profiles_dir or _DEFAULT_PROFILES_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._profiles: dict[str, dict] = {}   # id → raw dict (with "embedding" as ndarray)
        self._load_all()

    def _load_all(self) -> None:
        self._profiles = {}
        for p in self._dir.glob("*.json"):
            try:
                with open(p) as f:
                    data = json.load(f)
                data["_vec"] = np.array(data["embedding"], dtype=np.float32)
                self._profiles[data["speaker_id"]] = data
            except Exception as e:
                logger.warning("SpeakerStore: failed to load %s: %s", p, e)
        logger.debug("SpeakerStore: loaded %d speaker profiles", len(self._profiles))

    def enroll(self, name: str, embedding: np.ndarray) -> str:
        """Create a new speaker profile. Returns the new speaker_id."""
        speaker_id = str(uuid.uuid4())
        vec = _normalize(embedding)
        data = {
            "speaker_id": speaker_id,
            "name": name,
            "embedding": vec.tolist(),
            "sample_count": 1,
            "enrolled_ts": time.time(),
            "updated_ts": time.time(),
        }
        data["_vec"] = vec
        self._profiles[speaker_id] = data
        self._save(data)
        logger.info("SpeakerStore: enrolled speaker %r as %s", name, speaker_id)
        return speaker_id

    def update(self, speaker_id: str, new_embedding: np.ndarray) -> None:
        """Incremental mean update, capped at MAX_SAMPLE_COUNT."""
        if speaker_id not in self._profiles:
            return
        data = self._profiles[speaker_id]
        n = min(data["sample_count"], _MAX_SAMPLE_COUNT)
        old_vec = data["_vec"]
        new_vec = _normalize(new_embedding)
        merged = _normalize((old_vec * n + new_vec) / (n + 1))
        data["_vec"] = merged
        data["embedding"] = merged.tolist()
        data["sample_count"] = n + 1
        data["updated_ts"] = time.time()
        self._save(data)

    def identify(
        self,
        query: np.ndarray,
        threshold: float | None = None,
    ) -> tuple[str | None, str | None, float, str]:
        """
        Cosine similarity scan over all profiles.
        Returns (speaker_id, name, score, status) where status is
        'recognized' | 'unknown' | 'no_profiles'.
        """
        if not self._profiles:
            return None, None, 0.0, "no_profiles"

        threshold = threshold if threshold is not None else _THRESHOLD_DEFAULT
        q = _normalize(query)
        best_id, best_name, best_score = None, None, -1.0

        for sid, data in self._profiles.items():
            score = float(np.dot(q, data["_vec"]))  # both L2-normalised
            if score > best_score:
                best_score = score
                best_id = sid
                best_name = data["name"]

        if best_score >= threshold:
            return best_id, best_name, best_score, "recognized"
        return best_id, best_name, best_score, "unknown"

    def update_prosody_baseline(self, speaker_id: str, features: dict) -> None:
        """Incremental mean update of prosody baseline for a known speaker.

        Tracks jitter, shimmer, energy_mean, and f0_std. Once count >= 10
        the baseline is considered calibrated and label_prosody_tone will
        use relative thresholds instead of universal ones.
        """
        if speaker_id not in self._profiles:
            return
        data = self._profiles[speaker_id]
        bl = data.get("prosody_baseline") or {
            "jitter": 0.0, "shimmer": 0.0, "energy_mean": 0.0, "f0_std": 0.0, "count": 0,
        }
        n = bl["count"]
        for key in ("jitter", "shimmer", "energy_mean", "f0_std"):
            val = features.get(key, 0.0)
            if val > 0:  # skip zero-padded frames (no signal)
                bl[key] = (bl[key] * n + val) / (n + 1)
        bl["count"] = n + 1
        data["prosody_baseline"] = bl
        self._save(data)

    def get_prosody_baseline(self, speaker_id: str) -> dict | None:
        """Return the prosody baseline dict for a speaker, or None if not found."""
        data = self._profiles.get(speaker_id)
        return data.get("prosody_baseline") if data else None

    def find_by_name(self, name: str) -> str | None:
        """Return speaker_id for the first profile whose name matches (case-insensitive)."""
        name_lower = name.lower().strip()
        for sid, data in self._profiles.items():
            if data["name"].lower() == name_lower:
                return sid
        return None

    def list_speakers(self) -> list[dict]:
        return [
            {
                "speaker_id": d["speaker_id"],
                "name": d["name"],
                "sample_count": d["sample_count"],
                "updated_ts": d["updated_ts"],
            }
            for d in self._profiles.values()
        ]

    def _save(self, data: dict) -> None:
        path = self._dir / f"{data['speaker_id']}.json"
        payload = {k: v for k, v in data.items() if not k.startswith("_")}
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec
