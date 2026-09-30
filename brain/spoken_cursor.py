"""What the listener has actually heard of an utterance.

Audio is generated faster than it plays (v4 Turbo runs ~7x realtime), so what
has been *sent* says little about what has been *heard*. Playback starts with
the first audio chunk and runs in real time, so the heard position is the wall
clock since then, mapped onto the text through the per-character alignment the
dialogue socket returns (or, on transports without alignment, onto whole chunks
by their audio duration).

Two consumers:
  - the echo guard compares what the microphone heard against the text spoken
    in the last few seconds rather than the whole reply, so real speech that
    happens to reuse the reply's words is not thrown away as echo;
  - barge-in records only what the user heard, so the next turn knows the rest
    of the reply never landed.
"""

from __future__ import annotations

import time

_BACK_MS = 6000  # echo can trail playback by the STT round trip + endpointing
_AHEAD_MS = 1500  # and the mic can catch audio just ahead of the estimate


class SpokenCursor:
    def __init__(self) -> None:
        self._pieces: list[tuple[float, str]] = []  # (start_ms, display text)
        self._clip_ms = 0.0  # audio queued so far
        self._aligned_to_ms = 0.0
        self._in_tag = False
        self._after_tag = False
        self.started_at: float | None = None  # monotonic time of first audio
        self._block_end_ms = 0.0  # running end of relative alignment blocks
        self._plain_text = ""  # whole-utterance text when no alignment arrives

    # ── feeding ──────────────────────────────────────────────────────────────

    def start(self, at: float | None = None) -> None:
        if self.started_at is None:
            self.started_at = time.monotonic() if at is None else at

    def add_chunk(self, text: str, audio_ms: float, alignment: dict | None = None) -> None:
        """Record one chunk of audio. ``alignment`` start times must be absolute
        within the utterance (brain.api.audio makes them so). Without alignment
        the chunk's text is placed at the chunk's start."""
        self.start()
        chars = (alignment or {}).get("chars") or []
        starts = (alignment or {}).get("char_start_times_ms") or []
        if chars and len(starts) == len(chars):
            for ch, t in zip(chars, starts, strict=True):
                shown = self._display_char(ch)
                if shown:
                    self._pieces.append((float(t), shown))
            self._aligned_to_ms = max(self._aligned_to_ms, float(starts[-1]))
        elif text:
            self._pieces.append((self._clip_ms, text))
        self._clip_ms += max(0.0, audio_ms)

    def set_text(self, text: str) -> None:
        """The utterance's display text, used when the transport returns no
        alignment: heard position is then estimated in proportion to audio."""
        self._plain_text = (text or "").strip()

    def add_alignment_block(self, alignment: dict) -> None:
        """One alignment block straight off the dialogue socket, whose start
        times restart at 0 each block (consecutive blocks tile the clip)."""
        chars = alignment.get("chars") or alignment.get("characters") or []
        starts = alignment.get("char_start_times_ms") or []
        durs = alignment.get("char_durations_ms") or []
        if not chars or len(starts) != len(chars):
            return
        base = self._block_end_ms
        for ch, t in zip(chars, starts, strict=True):
            shown = self._display_char(ch)
            if shown:
                self._pieces.append((base + float(t), shown))
        self._block_end_ms = base + float(starts[-1]) + (float(durs[-1]) if durs else 0.0)
        self._aligned_to_ms = max(self._aligned_to_ms, self._block_end_ms)

    def add_audio(self, audio_ms: float) -> None:
        """Audio handed to playback (starts the clock on the first call)."""
        self.start()
        self._clip_ms += max(0.0, audio_ms)

    def _display_char(self, ch: str) -> str:
        """Drop audio-tag characters ([warmly] ...) and the space a tag adds."""
        if ch == "[":
            self._in_tag = True
            return ""
        if ch == "]" and self._in_tag:
            self._in_tag, self._after_tag = False, True
            return ""
        if self._in_tag:
            return ""
        if self._after_tag and ch == " ":
            self._after_tag = False
            return ""
        self._after_tag = False
        return ch

    # ── reading ──────────────────────────────────────────────────────────────

    def elapsed_ms(self, now: float | None = None) -> float:
        if self.started_at is None:
            return 0.0
        now = time.monotonic() if now is None else now
        return max(0.0, (now - self.started_at) * 1000)

    @property
    def total_ms(self) -> float:
        return max(self._clip_ms, self._aligned_to_ms)

    def _timeline(self) -> list[tuple[float, str]]:
        if self._pieces or not self._plain_text or self._clip_ms <= 0:
            return self._pieces
        step = self._clip_ms / len(self._plain_text)
        return [(i * step, ch) for i, ch in enumerate(self._plain_text)]

    def _text_between(self, lo: float, hi: float) -> str:
        return "".join(t for start, t in self._timeline() if lo <= start < hi)

    def heard_text(self, now: float | None = None) -> str:
        """Text whose audio has started playing, trimmed back to a whole word."""
        heard = self._text_between(float("-inf"), self.elapsed_ms(now))
        full = self._text_between(float("-inf"), float("inf"))
        if heard == full:
            return full.strip()
        cut = heard.rstrip()
        # A word caught mid-way wasn't really heard; keep complete words only.
        if cut and not heard.endswith((" ", "\n")) and len(heard) < len(full):
            nxt = full[len(heard) : len(heard) + 1]
            if nxt and not nxt.isspace():
                cut = cut.rsplit(None, 1)[0] if " " in cut else ""
        return cut.strip()

    def window_text(self, now: float | None = None) -> str:
        """Text spoken around the playback position — the echo guard's reference."""
        at = self.elapsed_ms(now)
        return self._text_between(at - _BACK_MS, at + _AHEAD_MS).strip()

    def finished(self, now: float | None = None) -> bool:
        return self.started_at is not None and self.elapsed_ms(now) >= self.total_ms


def interruption_note(heard: str, full: str = "", rest_cap: int = 300) -> str:
    """How an interrupted reply reads in the next turn's conversation history.

    Keeps the unspoken remainder rather than deleting it: most surfaces (the
    console, partner chat UIs) show the full reply as text, so the user may
    have read what they didn't hear. The brain gets the fact of the
    interruption without losing what it said."""
    heard = " ".join((heard or "").split())
    full = " ".join((full or "").split())
    rest = full[len(heard) :].strip() if full.startswith(heard) else ""
    if len(rest) > rest_cap:
        rest = rest[:rest_cap].rsplit(" ", 1)[0] + "…"
    unspoken = f' Not heard: "{rest}"' if rest else ""
    if not heard:
        return f"(the user cut in before hearing this reply.{unspoken})"
    return f"{heard}… (the user cut in here and may not have heard the rest.{unspoken})"
