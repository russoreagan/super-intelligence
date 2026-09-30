"""Text to Dialogue WebSocket transport, shared by every speaking path.

ElevenLabs' realtime expressive models (eleven_v4_turbo, eleven_v4,
eleven_v3_conversational) exist only on
``wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input``; the TTS endpoints
reject them. The brain's own ``PNS._speak`` and the engine API
(``brain/api/audio.py`` → ``/v1/tts``, the SSE turn stream, the realtime WS) all
speak through ``DialogueSession`` so the two can never drift apart (the August
voice review found both upgrades finished locally and left hosted behind).

Behaviour measured in the Phase 0 probe (docs/ELEVEN_V4_TURBO_PLAN.md):
  - Socket connect is 0.14-0.26 s, about half of cold time-to-first-audio, so a
    session can be opened early (``open()`` at turn start) and fed later.
  - The server closes a socket that hears nothing for 20 s (1008
    ``input_timeout_exceeded``); ``keep_alive`` every few seconds holds it.
  - Short replies need ``flush``, or audio waits for the socket to close.
  - ``close_context`` flushes the rest of a context rather than cancelling it,
    so barge-in closes the whole socket; the next turn opens a fresh one.
  - Config errors (unknown voice, unsupported language, a format above the plan
    tier) arrive as clean 1008s with an ``error`` code. They describe one
    request, say nothing about the transport's health, and never count toward
    the circuit breaker. Account errors (quota exhausted, bad key) are different:
    every request will fail the same way, so they open the breaker at once for
    a cooldown instead of costing a socket attempt per utterance.
  - v4 ignores ``speed``/``style``; expression rides inline audio tags.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

WS_URL = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
DEFAULT_MODEL = "eleven_v4_turbo"
FLASH_MODEL = "eleven_flash_v2_5"
_V3C = "eleven_v3_conversational"

# Server error codes that describe this one request (bad voice, language,
# format), not the transport: fall back for this utterance, don't count them.
_CONFIG_ERRORS = frozenset(
    {
        "voice_not_found",
        "unsupported_language",
        "invalid_output_format",
        "output_format_not_allowed",
        "invalid_model",
        "model_not_found",
        "invalid_request",
    }
)
# Server error codes that describe the account: every request fails the same
# way until someone acts, so they hold the dialogue path off for a cooldown.
_ACCOUNT_ERRORS = frozenset({"unauthorized", "quota_exceeded"})


# ── model routing ────────────────────────────────────────────────────────────


def default_model_id() -> str:
    """The configured ElevenLabs model — the one resolver every reader uses
    (PNS, the engine API, the voice picker) so they cannot disagree. The org's
    `elevenlabs_model` setting wins, then the ELEVENLABS_MODEL_ID env (platform
    default), then eleven_v4_turbo."""
    chosen = ""
    try:
        from brain.settings import settings

        chosen = str(settings.get("elevenlabs_model", "") or "").strip()
    except Exception:  # noqa: BLE001 — settings unavailable (e.g. bare import)
        chosen = ""
    return chosen or (os.environ.get("ELEVENLABS_MODEL_ID") or "").strip() or DEFAULT_MODEL


def is_dialogue_model(model_id: str) -> bool:
    """Models that only exist on the Text to Dialogue WebSocket."""
    return model_id == _V3C or model_id.startswith("eleven_v4")


def uses_audio_tags(model_id: str) -> bool:
    """Models that perform inline audio tags instead of reading them aloud."""
    return model_id.startswith(("eleven_v3", "eleven_v4"))


def http_fallback_model(model_id: str) -> str:
    """What to speak with when the dialogue socket is unavailable.

    v3c keeps its tags on HTTP eleven_v3. v4 has no HTTP twin, so it lands on
    Flash, the proven low-latency path (tags stripped, moods → VoiceSettings)."""
    if model_id == _V3C:
        return "eleven_v3"
    if model_id.startswith("eleven_v4"):
        return FLASH_MODEL
    return model_id


def dialogue_ws_enabled() -> bool:
    """Kill switch: BRAIN_TTS_DIALOGUE_WS=0 routes every dialogue model to its
    HTTP fallback."""
    return os.environ.get("BRAIN_TTS_DIALOGUE_WS", "1").strip().lower() not in ("0", "false", "off")


def keepalive_interval_s() -> float:
    return float(os.environ.get("BRAIN_TTS_DIALOGUE_KEEPALIVE_S", "8"))


def breaker_cooldown_s() -> float:
    """How long an open breaker keeps the dialogue path off before one trial
    utterance goes over the socket again (half-open). The one knob for both a
    run of transport failures and an account error (quota, bad key)."""
    return float(os.environ.get("BRAIN_TTS_DIALOGUE_WS_COOLDOWN_S", "300"))


def prewarm_ttl_s() -> float:
    """How long a turn-start socket may sit unfed before it closes itself (a
    long tool-using turn, or one that ends without speaking)."""
    return float(os.environ.get("BRAIN_TTS_DIALOGUE_PREWARM_TTL_S", "90"))


# ── circuit breaker ──────────────────────────────────────────────────────────


class DialogueBreaker:
    """Consecutive transport failures BEFORE first audio (a full session pool,
    a network stall) each cost an open timeout of dead air before the fallback
    starts. Past BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES, stop trying for
    ``breaker_cooldown_s()``; after that the next utterance is a trial: success
    closes the breaker, one more failure reopens it for another cooldown.
    An account error (quota, bad key) opens it at once. MAX_FAILURES=0 disables
    the transport-failure trip (kill switch, not an enable switch)."""

    def __init__(self) -> None:
        self.failures = 0
        self.reason = ""
        self._open_until = 0.0

    @property
    def tripped(self) -> bool:
        return time.monotonic() < self._open_until

    def ok(self) -> None:
        self.failures = 0
        self._open_until = 0.0
        self.reason = ""

    def _trip(self, reason: str) -> bool:
        was = self.tripped
        self._open_until = time.monotonic() + breaker_cooldown_s()
        self.reason = reason
        return not was

    def fail(self, err: BaseException) -> bool:
        """Record one failure; returns True when this call tripped the breaker."""
        if isinstance(err, DialogueError):
            if err.account:
                return self._trip(err.code or "account")
            if err.config:
                return False  # the request was wrong, the transport is fine
        self.failures += 1
        limit = int(os.environ.get("BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES", "3"))
        if limit > 0 and self.failures >= limit and not self.tripped:
            # Also the half-open case: failures stay at the limit through the
            # cooldown, so a failed trial reopens it straight away.
            return self._trip("transport")
        return False


# The engine API's breaker (brain.api.audio). PNS keeps its own per instance.
ENGINE_BREAKER = DialogueBreaker()


# ── session ──────────────────────────────────────────────────────────────────


class DialogueError(Exception):
    """A server-reported error or a transport failure on the dialogue socket."""

    def __init__(
        self, message: str, *, code: str = "", config: bool = False, account: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.config = config  # this request is wrong (voice, language, format)
        self.account = account  # every request will fail (quota, credentials)


@dataclass
class DialogueFrame:
    audio: bytes
    alignment: dict | None = None


@dataclass
class DialogueSession:
    """One utterance on one socket. ``open()`` may run early (turn start) so the
    handshake overlaps the LLM; ``feed()`` text as it becomes final;
    ``finish()`` flushes and closes; ``frames()`` yields audio as it arrives;
    ``cancel()`` drops the socket (barge-in)."""

    model: str
    voice_id: str
    fmt: str = "pcm_22050"
    voice_settings: dict | None = None
    alignment: bool = False
    language_code: str | None = None
    api_key: str = ""
    # Close the socket if nothing is fed within this many seconds of opening.
    expire_after: float | None = None
    delivered: bool = False
    cancelled: bool = False
    opened_at: float = 0.0
    _cm: object = field(default=None, repr=False)
    _ws: object = field(default=None, repr=False)
    _open_task: asyncio.Task | None = field(default=None, repr=False)
    _recv_task: asyncio.Task | None = field(default=None, repr=False)
    _ka_task: asyncio.Task | None = field(default=None, repr=False)
    _queue: asyncio.Queue | None = field(default=None, repr=False)
    _last_send: float = 0.0
    _fed: bool = False
    _closed: bool = False
    _retried: bool = False
    # Everything sent after the voices frame, replayed if the socket is swapped.
    _script: list = field(default_factory=list, repr=False)

    @property
    def url(self) -> str:
        q = f"model_id={self.model}&output_format={self.fmt}"
        if self.alignment:
            q += "&sync_alignment=true"
        if self.language_code:
            q += f"&language_code={self.language_code}"
        return f"{WS_URL}?{q}"

    def start(self) -> DialogueSession:
        """Begin opening in the background and return immediately."""
        if self._open_task is None:
            self._open_task = asyncio.ensure_future(self._open())
        return self

    async def open(self) -> None:
        await self.start()._open_task

    async def _open(self) -> None:
        import websockets

        key = self.api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        headers = {"xi-api-key": key}
        timeout = float(os.environ.get("BRAIN_TTS_DIALOGUE_WS_OPEN_TIMEOUT", "3"))
        kwargs = {"max_size": 16 * 1024 * 1024, "open_timeout": timeout}
        try:
            self._cm = websockets.connect(self.url, additional_headers=headers, **kwargs)
        except TypeError:  # websockets < 14 spells it extra_headers
            self._cm = websockets.connect(self.url, extra_headers=headers, **kwargs)
        self._ws = await self._cm.__aenter__()
        self.opened_at = time.monotonic()
        self._queue = asyncio.Queue()
        first: dict = {"voices": [self.voice_id]}
        if self.voice_settings:
            first["voice_settings"] = self.voice_settings
        await self._send(first)
        self._recv_task = asyncio.ensure_future(self._receive())
        self._ka_task = asyncio.ensure_future(self._keepalive())

    async def _send(self, msg: dict) -> None:
        self._last_send = time.monotonic()
        await self._ws.send(json.dumps(msg))

    async def _keepalive(self) -> None:
        interval = keepalive_interval_s()
        with contextlib.suppress(Exception):
            while not self._closed:
                await asyncio.sleep(max(0.01, interval / 2))
                if self._closed:
                    return
                if (
                    self.expire_after is not None
                    and not self._fed
                    and time.monotonic() - self.opened_at > self.expire_after
                ):
                    await self.close()
                    return
                if time.monotonic() - self._last_send >= interval:
                    await self._send({"keep_alive": True})

    async def _receive(self) -> None:
        q = self._queue
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("audio"):
                    audio = base64.b64decode(msg["audio"])
                    if audio:
                        await q.put(DialogueFrame(audio, msg.get("alignment")))
                elif msg.get("error"):
                    code = str(msg.get("error") or "")
                    await q.put(
                        DialogueError(
                            str(msg.get("message") or code),
                            code=code,
                            config=code in _CONFIG_ERRORS,
                            account=code in _ACCOUNT_ERRORS,
                        )
                    )
                    return
                if msg.get("is_final"):
                    return
        except Exception as err:  # noqa: BLE001 — surfaced to the frames() consumer
            await q.put(_as_dialogue_error(err))
        finally:
            await q.put(None)

    async def feed(self, text: str) -> None:
        if text:
            await self._deliver({"inputs": [{"text": text, "voice_id": self.voice_id}]})

    async def finish(self) -> None:
        """No more text: flush (short replies are silent without it) and ask the
        server to close once the audio is out."""
        await self._deliver({"flush": True})
        await self._deliver({"close_socket": True})

    @property
    def prewarmed(self) -> bool:
        """Opened ahead of its text (turn-start prewarm), so it may have sat
        idle long enough for the server or the network to drop it."""
        return self.expire_after is not None

    async def _deliver(self, msg: dict) -> None:
        """Send one post-open message. A prewarmed socket that died while it sat
        idle (server timeout, lapsed keep_alive, network blip) is swapped for a
        fresh one once, replaying what was already sent, instead of surfacing a
        failure that says nothing about the transport's health now."""
        await self._ready()
        if not self._fed and not self.alive() and self._can_reopen():
            await self._reopen()
        self._fed = True
        self._script.append(msg)
        try:
            await self._send(msg)
        except Exception as err:
            if not self._can_reopen():
                raise _as_dialogue_error(err) from err
            logger.info("[tts_dialogue] prewarmed socket dropped (%s); reopening once", err)
            await self._reopen()

    def _can_reopen(self) -> bool:
        return self.prewarmed and not (
            self._retried or self.delivered or self.cancelled or self._closed
        )

    async def _reopen(self) -> None:
        self._retried = True
        me = asyncio.current_task()
        for t in (self._ka_task, self._recv_task):
            if t is not None and not t.done() and t is not me:
                t.cancel()
        if self._cm is not None:
            with contextlib.suppress(Exception):
                await self._cm.__aexit__(None, None, None)
        self._cm = self._ws = self._recv_task = self._ka_task = None
        self._open_task = None
        await self._ready()  # a failed reopen surfaces as a real failure
        for m in self._script:
            await self._send(m)

    def alive(self) -> bool:
        """The socket can still carry text: not closed by us, handshake not
        failed, receive loop still running (it ends when the server or the
        network closes the socket), and the connection itself not closing.
        True while the handshake is still in flight (feed() waits for it)."""
        if self._closed:
            return False
        t = self._open_task
        if t is None or not t.done():
            return True
        if t.cancelled() or t.exception() is not None:
            return False
        if self._recv_task is not None and self._recv_task.done():
            return False
        ws = self._ws
        # websockets >= 13 (asyncio ClientConnection) exposes only ``state``;
        # the legacy protocol also has a ``closed`` bool.
        state = getattr(ws, "state", None)
        if state is not None and getattr(state, "name", "") in ("CLOSING", "CLOSED"):
            return False
        return getattr(ws, "closed", False) is not True

    async def _ready(self) -> None:
        if self._open_task is None:
            self.start()
        try:
            await self._open_task
        except Exception as err:
            raise _as_dialogue_error(err) from err

    async def frames(self):
        """Yield DialogueFrames until the utterance ends. Raises DialogueError on
        a server error or a dropped socket (check ``delivered`` to tell a
        before-first-audio failure from a mid-stream one)."""
        await self._ready()
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                self.delivered = True
                yield item
        finally:
            await self.close()

    async def cancel(self) -> None:
        self.cancelled = True
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        me = asyncio.current_task()
        for t in (self._ka_task, self._recv_task):
            if t is not None and not t.done() and t is not me:
                t.cancel()
        if self._open_task is not None and not self._open_task.done():
            self._open_task.cancel()
        if self._cm is not None:
            with contextlib.suppress(Exception):
                await self._cm.__aexit__(None, None, None)

    @property
    def closed(self) -> bool:
        return self._closed


def _as_dialogue_error(err: BaseException) -> DialogueError:
    if isinstance(err, DialogueError):
        return err
    # websockets surfaces a server 1008 as ConnectionClosedError whose reason is
    # the server message; config problems are recognisable by their wording.
    text = str(err)
    low = text.lower()
    config = any(
        s in low for s in ("was not found", "does not support language", "only available on the")
    )
    account = any(
        s in low
        for s in ("invalid api key", "quota_exceeded", "exceeds your quota", "unauthorized")
    )
    return DialogueError(
        f"{type(err).__name__}: {text}", code="transport", config=config, account=account
    )


# ── turn-start prewarm ───────────────────────────────────────────────────────
#
# A transport opens the session at turn start and parks it here; the synthesis
# call for that turn claims it if the model/voice/format still match. A
# contextvar rather than a parameter: the engine's TTS runner signature is a
# partner-facing seam that tests and callers fake, and the turn's own task is the
# only reader that should ever see its session.

_PREWARMED: contextvars.ContextVar[DialogueSession | None] = contextvars.ContextVar(
    "tts_dialogue_prewarmed", default=None
)


def prewarm(
    model: str, voice_id: str, fmt: str = "pcm_22050", *, alignment: bool = False
) -> DialogueSession | None:
    """Start opening a session for the coming utterance. Returns None when the
    model isn't a dialogue model, the transport is switched off, or there is no
    key. The caller owns the returned session until ``claim`` hands it over, and
    must ``release`` it when the turn ends without speaking."""
    if not (is_dialogue_model(model) and dialogue_ws_enabled()):
        return None
    if not os.environ.get("ELEVENLABS_API_KEY") or not voice_id:
        return None
    s = DialogueSession(
        model=model, voice_id=voice_id, fmt=fmt, alignment=alignment, expire_after=prewarm_ttl_s()
    ).start()
    _PREWARMED.set(s)
    return s


def usable(s: DialogueSession, model: str, voice_id: str, fmt: str) -> bool:
    """A prewarmed session can carry this utterance: still alive (not closed by
    us, by the server, or by the network; handshake didn't fail), and opened for
    the same model/voice/format."""
    return s.alive() and (s.model, s.voice_id, s.fmt) == (model, voice_id, fmt)


def claim(model: str, voice_id: str, fmt: str) -> DialogueSession | None:
    """Take this task's prewarmed session if it matches and is still usable."""
    s = _PREWARMED.get()
    if s is None:
        return None
    _PREWARMED.set(None)
    if usable(s, model, voice_id, fmt):
        return s
    asyncio.ensure_future(s.close())
    return None


async def release() -> None:
    """Close an unclaimed prewarmed session (turn failed, answered silently,
    audio refused)."""
    s = _PREWARMED.get()
    _PREWARMED.set(None)
    if s is not None:
        await s.close()
