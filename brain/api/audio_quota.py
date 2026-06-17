"""
Per-partner audio quota for the engine API.

STT/TTS hit paid third parties, so a partner key's audio usage is metered the way
those services bill: **TTS by characters** synthesised, **STT by input
audio-seconds**. Each meter is a rolling-window ceiling per ``partner_id``
(``audio_tts_chars_per_window`` / ``audio_stt_seconds_per_window`` /
``audio_quota_window_s`` in settings; 0 = unlimited). Owner-key calls are never
metered — the owner is us.

Enforcement mirrors the motor job rate limiter's philosophy (no cost
prediction): ``check()`` refuses a call only when the partner is *already* at or
over the cap for the window; ``record()`` logs the *actual* usage after the call
succeeds (TTS character count; STT duration from Deepgram's metadata). A single
request can therefore overshoot slightly before the partner is blocked — the same
trade the motor limiter makes, and the right one when a call's cost isn't known
until it returns.

Persistence also mirrors motor: the rolling window is kept in memory and written
to a JSON file ONLY under ``BRAIN_MULTITENANT`` (so a hosted redeploy can't reset
a partner's quota), staying ephemeral for companion/local/tests.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Meter names (also the persisted keys). TTS counts characters sent to the
# provider; STT counts seconds of input audio.
TTS_CHARS = "tts_chars"
STT_SECONDS = "stt_seconds"

_CAP_SETTING = {
    TTS_CHARS: "audio_tts_chars_per_window",
    STT_SECONDS: "audio_stt_seconds_per_window",
}


class AudioQuota:
    """Rolling-window usage tracker keyed by (partner_id, meter)."""

    def __init__(self, *, now_fn=time.time, persist_path: str | None = None) -> None:
        self._now = now_fn
        # (partner_id, meter) -> list[(timestamp, amount)]
        self._events: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self._persist_path = persist_path
        self._load()

    # ── config ────────────────────────────────────────────────────────────────
    @staticmethod
    def _cap(meter: str) -> float:
        from brain.settings import settings

        try:
            return float(settings.get(_CAP_SETTING.get(meter, "")) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _window_s() -> float:
        from brain.settings import settings

        try:
            return float(settings.get("audio_quota_window_s") or 86400.0)
        except (TypeError, ValueError):
            return 86400.0

    # ── public API ──────────────────────────────────────────────────────────────
    def window_total(self, partner_id: str, meter: str) -> float:
        """Sum of recorded usage for this partner+meter inside the live window
        (prunes expired events as a side effect)."""
        key = (partner_id, meter)
        window_s = self._window_s()
        now = self._now()
        events = [(t, a) for t, a in self._events.get(key, []) if now - t <= window_s]
        self._events[key] = events
        return sum(a for _t, a in events)

    def check(self, partner_id: str | None, meter: str) -> str | None:
        """Return a decline reason if ``partner_id`` is already at/over the cap for
        ``meter``; else None. Unlimited (cap 0) and owner (partner_id None) always
        pass."""
        cap = self._cap(meter)
        if not cap or not partner_id:
            return None
        if self.window_total(partner_id, meter) >= cap:
            window_s = self._window_s()
            unit = "characters" if meter == TTS_CHARS else "audio-seconds"
            return f"audio quota reached ({int(cap)} {unit} per {int(window_s)}s)"
        return None

    def record(self, partner_id: str | None, meter: str, amount: float) -> None:
        """Log actual usage after a successful call. No-op for owner / non-positive
        amounts. Persists in multitenant mode."""
        if not partner_id or amount is None or amount <= 0:
            return
        self._events.setdefault((partner_id, meter), []).append((self._now(), float(amount)))
        self._save()

    # ── persistence (multitenant only, mirrors motor_cortex) ──────────────────
    @staticmethod
    def _persist() -> bool:
        return bool(os.environ.get("BRAIN_MULTITENANT"))

    def _path(self) -> str:
        if self._persist_path:
            return self._persist_path
        root = os.environ.get(
            "SECOND_BRAIN_PATH",
            os.path.join(os.path.dirname(__file__), "..", "..", "second_brain"),
        )
        return os.path.join(root, "audio_quota.json")

    def _load(self) -> None:
        if not self._persist():
            return
        try:
            with open(self._path()) as f:
                data = json.load(f)
            window_s = self._window_s()
            now = self._now()
            for flat_key, events in (data or {}).items():
                partner_id, _, meter = str(flat_key).partition("::")
                kept = [
                    (float(t), float(a))
                    for t, a in events
                    if now - float(t) <= window_s
                ]
                if kept:
                    self._events[(partner_id, meter)] = kept
        except Exception:  # noqa: BLE001 — missing/corrupt file → start empty
            pass

    def _save(self) -> None:
        if not self._persist():
            return
        try:
            flat = {
                f"{partner_id}::{meter}": events
                for (partner_id, meter), events in self._events.items()
                if events
            }
            with open(self._path(), "w") as f:
                json.dump(flat, f)
        except Exception:  # noqa: BLE001 — best-effort; quota never breaks a call
            pass
