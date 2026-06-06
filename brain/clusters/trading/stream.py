"""Real-time watchlist alert stream via Alpaca websocket.

Subscribes to 1-minute bars for every symbol in the watchlist. On each bar,
updates a rolling price buffer, recomputes indicators, and fires an alert card
to the UI the moment a ``watch_indicators`` trigger is satisfied — no polling,
no delay.

Architecture
------------
``WatchlistStream`` runs as a background asyncio task alongside the brain
session. It owns a single websocket connection to Alpaca's streaming endpoint
and reconnects automatically on disconnect. Alerts are emitted via the
existing ``ActivationEmitter`` (``emit_table`` + a ``trading_alert`` event),
so they appear as table cards in the chat UI without involving the LLM.

Limits (free tier)
------------------
- 30 symbols max over websocket (we pick the first 30 from the watchlist).
- Uses the IEX feed by default; set ``ALPACA_USE_SIP=true`` for the SIP feed
  (requires Algo Trader Plus, $99/mo).

Duplicate suppression
---------------------
A trigger re-fires at most once every ``trading_alert_cooldown_min`` minutes
(default 30). If a trigger un-fires and re-fires (e.g. RSI crosses back up
above 30 and then dips below again) it fires fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque

logger = logging.getLogger(__name__)

_WS_IEX = "wss://stream.data.alpaca.markets/v2/iex"
_WS_SIP = "wss://stream.data.alpaca.markets/v2/sip"
_MAX_SYMBOLS = 30  # free-tier websocket limit
_BAR_BUFFER = 300  # bars kept per symbol (~5 hours of 1-min bars)
_RECONNECT_DELAY = 30  # seconds between reconnect attempts
_WATCHLIST_REFRESH = 300  # seconds between watchlist re-reads


def _cooldown_s() -> float:
    try:
        from brain.settings import settings

        return float(settings.get("trading_alert_cooldown_min") or 30) * 60
    except Exception:
        return 30 * 60


class WatchlistStream:
    """Background websocket listener that pushes real-time watchlist alerts."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        market_data=None,
        emitter=None,
        use_sip: bool = False,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        self._md = market_data
        self._emitter = emitter
        self._url = (
            _WS_SIP
            if (use_sip or os.environ.get("ALPACA_USE_SIP", "").lower() == "true")
            else _WS_IEX
        )
        # symbol -> deque of bar dicts (oldest first)
        self._buffers: dict[str, deque] = {}
        # (symbol, trigger_name, trigger, level) -> last-fired unix ts
        self._last_fired: dict[tuple, float] = {}
        # currently subscribed symbols
        self._subscribed: set[str] = set()
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._secret_key)

    async def start(self) -> None:
        if not self.available:
            logger.info("[stream] no Alpaca keys — watchlist stream disabled")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="trading_stream")
        logger.info("[stream] watchlist stream started (%s)", self._url)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        logger.info("[stream] watchlist stream stopped")

    # ── main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(
                        "[stream] connection lost: %s — retry in %ds", e, _RECONNECT_DELAY
                    )
                    await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_stream(self) -> None:
        import websockets

        from . import store

        watchlist = store.watchlist_symbols()
        if not watchlist:
            logger.debug("[stream] watchlist empty — sleeping 60s")
            await asyncio.sleep(60)
            return

        symbols = [e["symbol"].upper() for e in watchlist[:_MAX_SYMBOLS]]
        if len(watchlist) > _MAX_SYMBOLS:
            logger.warning(
                "[stream] watchlist has %d symbols; streaming first %d (free-tier limit)",
                len(watchlist),
                _MAX_SYMBOLS,
            )

        # Pre-load history buffers so indicators are accurate from the first bar.
        if self._md is not None:
            for sym in symbols:
                if sym not in self._buffers:
                    try:
                        bars = await self._md.history(sym, days=250)
                        self._buffers[sym] = deque(bars, maxlen=_BAR_BUFFER)
                    except Exception as e:
                        logger.debug("[stream] history preload failed for %s: %s", sym, e)
                        self._buffers[sym] = deque(maxlen=_BAR_BUFFER)

        async with websockets.connect(self._url, ping_interval=30, ping_timeout=20) as ws:
            # The Alpaca stream handshake is two messages:
            #   1. server → [{"T":"success","msg":"connected"}]   (connection ack)
            #   2. client → auth; server → [{"T":"success","msg":"authenticated"}]
            connected = json.loads(await ws.recv())
            if not any(
                m.get("T") == "success" and "connected" in str(m.get("msg", "")) for m in connected
            ):
                logger.error("[stream] unexpected greeting: %s", connected)
                return

            await ws.send(
                json.dumps({"action": "auth", "key": self._api_key, "secret": self._secret_key})
            )
            resp = json.loads(await ws.recv())
            if not any(
                m.get("T") == "success" and "authenticated" in str(m.get("msg", "")) for m in resp
            ):
                logger.error("[stream] auth failed: %s", resp)
                return

            # Subscribe to 1-min bars
            await ws.send(json.dumps({"action": "subscribe", "bars": symbols}))
            self._subscribed = set(symbols)
            logger.info("[stream] subscribed to bars for %s", symbols)

            last_watchlist_check = time.time()

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=90)
                except TimeoutError:
                    continue  # websockets library handles pings; just loop

                events = json.loads(raw)
                for event in events:
                    t = event.get("T")
                    if t == "b":  # bar
                        await self._on_bar(event, watchlist)
                    elif t == "error":
                        logger.warning("[stream] server error: %s", event)

                # Periodically refresh the watchlist and update subscriptions
                if time.time() - last_watchlist_check > _WATCHLIST_REFRESH:
                    await self._refresh_subscriptions(ws)
                    watchlist = store.watchlist_symbols()
                    last_watchlist_check = time.time()

    async def _refresh_subscriptions(self, ws) -> None:
        from . import store

        current = {e["symbol"].upper() for e in store.watchlist_symbols()[:_MAX_SYMBOLS]}
        add = current - self._subscribed
        remove = self._subscribed - current
        if add:
            await ws.send(json.dumps({"action": "subscribe", "bars": list(add)}))
            for sym in add:
                if sym not in self._buffers:
                    self._buffers[sym] = deque(maxlen=_BAR_BUFFER)
        if remove:
            await ws.send(json.dumps({"action": "unsubscribe", "bars": list(remove)}))
        if add or remove:
            self._subscribed = current
            logger.info("[stream] subscriptions updated +%d -%d", len(add), len(remove))

    # ── alert logic ───────────────────────────────────────────────────────────

    async def _on_bar(self, bar: dict, watchlist: list[dict]) -> None:
        from . import indicators as ind
        from .capabilities import _eval_trigger
        from .market_data import closes

        sym = bar.get("S", "").upper()

        # Append the new bar to the buffer
        buf = self._buffers.get(sym)
        if buf is None:
            buf = deque(maxlen=_BAR_BUFFER)
            self._buffers[sym] = buf
        buf.append(
            {
                "t": bar.get("t"),
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "volume": bar.get("v"),
            }
        )

        snap = ind.compute_all(closes(list(buf)))

        # Find matching watchlist entry
        entry = next((e for e in watchlist if e["symbol"].upper() == sym), None)
        if not entry:
            return

        cooldown = _cooldown_s()
        now = time.time()
        fired = []

        for wi in entry.get("watch_indicators", []) or []:
            name = wi.get("name", "")
            trigger = wi.get("trigger", "")
            level = wi.get("level")
            key = (sym, name, trigger, str(level))
            last = self._last_fired.get(key, 0)

            if now - last < cooldown:
                continue  # still in cooldown
            if _eval_trigger(snap.get(name), trigger, level):
                fired.append(wi)
                self._last_fired[key] = now

        if not fired:
            return

        logger.info(
            "[stream] ALERT %s — %d trigger(s): %s", sym, len(fired), [w.get("name") for w in fired]
        )
        await self._emit_alert(sym, fired, snap, entry)

    async def _emit_alert(self, symbol: str, fired: list[dict], snap: dict, entry: dict) -> None:

        note = entry.get("thesis", "")
        columns = ["symbol", "indicator", "trigger", "level", "current", "price"]
        rows = [
            [
                symbol,
                w.get("name"),
                w.get("trigger"),
                w.get("level"),
                _fmt(snap.get(w.get("name"))),
                _fmt(snap.get("price")),
            ]
            for w in fired
        ]

        if self._emitter is not None:
            try:
                await self._emitter.emit_table(
                    "stream", f"⚡ {symbol} alert", columns, rows, note=note
                )
            except Exception as e:
                logger.debug("[stream] emit_table failed: %s", e)
            try:
                await self._emitter.emit_event(
                    {
                        "type": "trading_alert",
                        "symbol": symbol,
                        "fired": fired,
                        "snapshot": {k: v for k, v in snap.items() if v is not None},
                        "ts": time.time(),
                    }
                )
            except Exception as e:
                logger.debug("[stream] emit_event failed: %s", e)


def _fmt(v) -> str | float | None:
    if v is None:
        return None
    if isinstance(v, float):
        return round(v, 2)
    return v
