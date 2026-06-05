"""Read-only stdio MCP client for the official Alpaca MCP server.

This is Path A from the plan: the brain owns a local ``uvx alpaca-mcp-server``
subprocess and speaks MCP to it for clean, structured market + account data.

ADVISE-ONLY ENFORCEMENT (layer 2 of 3):
- ``call(name, args)`` refuses any tool not in ``READ_ONLY_ALPACA_TOOLS``. Every
  order/position/account-write tool is therefore unreachable through this client,
  on top of the read-only-scoped Alpaca key (layer 1) and ``ALPACA_TOOLSETS``
  (layer 3). The allow-list is checked BEFORE the request is sent.

The client is resilient: if the ``mcp`` SDK, ``uvx``, or the server is
unavailable, ``available`` is False and callers fall back to yfinance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from . import BLOCKED_ALPACA_TOOLS, READ_ONLY_ALPACA_TOOLS

logger = logging.getLogger(__name__)


class AlpacaToolBlocked(RuntimeError):
    """Raised when a non-read-only Alpaca tool is requested. Never suppressed."""


class AlpacaMCPClient:
    """Lazy, long-lived, read-only client to ``alpaca-mcp-server`` over stdio."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool | None = None,
        toolsets: str | None = None,
        cache_ttl_s: float = 30.0,
        command: str = "uvx",
        args: tuple[str, ...] = ("alpaca-mcp-server",),
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        if paper is None:
            paper = os.environ.get("ALPACA_PAPER_TRADE", "true").lower() != "false"
        self._paper = paper
        # Default toolsets exclude the write categories (trading, watchlists) as a
        # backstop; this is layer 3. Override via ALPACA_TOOLSETS or the arg.
        self._toolsets = (
            toolsets
            or os.environ.get("ALPACA_TOOLSETS")
            or "account,stock-data,options-data,corporate-actions,news,assets"
        )
        self._cache_ttl_s = cache_ttl_s
        self._command = command
        self._args = tuple(args)

        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()
        self._session = None
        self._exit_stack = None

    @property
    def available(self) -> bool:
        """True only if credentials exist and the mcp SDK is importable."""
        if not (self._api_key and self._secret_key):
            return False
        try:
            import mcp  # noqa: F401
        except Exception:
            return False
        return True

    # ── connection management ────────────────────────────────────────────────

    async def _ensure_session(self):
        if self._session is not None:
            return self._session
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env.update(
            {
                "ALPACA_API_KEY": self._api_key,
                "ALPACA_SECRET_KEY": self._secret_key,
                "ALPACA_PAPER_TRADE": "true" if self._paper else "false",
                "ALPACA_TOOLSETS": self._toolsets,
            }
        )
        params = StdioServerParameters(command=self._command, args=list(self._args), env=env)
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._exit_stack = stack
        self._session = session
        logger.info("[AlpacaMCP] Connected (paper=%s, toolsets=%s)", self._paper, self._toolsets)
        return session

    async def close(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as e:  # pragma: no cover - best-effort teardown
                logger.debug("[AlpacaMCP] teardown error: %s", e)
        self._session = None
        self._exit_stack = None

    async def _reset(self) -> None:
        await self.close()

    # ── the only public call path ────────────────────────────────────────────

    async def call(self, name: str, args: dict | None = None, *, retries: int = 2) -> dict | list:
        """Call a READ-ONLY Alpaca tool and return parsed JSON.

        Raises AlpacaToolBlocked for any non-read-only tool. Returns a dict with
        an ``error`` key on transport failure (callers fall back gracefully).
        """
        if name in BLOCKED_ALPACA_TOOLS or name not in READ_ONLY_ALPACA_TOOLS:
            raise AlpacaToolBlocked(
                f"Refusing non-read-only Alpaca tool '{name}'. This client is advise-only."
            )
        if not self.available:
            return {"error": "alpaca_unavailable"}

        args = args or {}
        cache_key = name + ":" + json.dumps(args, sort_keys=True, default=str)
        now = time.time()
        hit = self._cache.get(cache_key)
        if hit and now - hit[0] < self._cache_ttl_s:
            return hit[1]

        async with self._lock:
            # re-check cache under lock
            hit = self._cache.get(cache_key)
            if hit and time.time() - hit[0] < self._cache_ttl_s:
                return hit[1]
            last_err: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    session = await self._ensure_session()
                    result = await session.call_tool(name, args)
                    parsed = self._parse(result)
                    self._cache[cache_key] = (time.time(), parsed)
                    return parsed
                except Exception as e:
                    last_err = e
                    logger.warning("[AlpacaMCP] %s failed (attempt %d): %s", name, attempt + 1, e)
                    await self._reset()
                    if attempt < retries:
                        await asyncio.sleep(0.5 * (2**attempt))
            return {"error": f"alpaca_call_failed: {last_err}"}

    @staticmethod
    def _parse(result) -> dict | list:
        """Extract JSON (or text) from an MCP CallToolResult."""
        content = getattr(result, "content", None) or []
        texts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                texts.append(text)
        blob = "\n".join(texts).strip()
        if not blob:
            return {"error": "empty_response"}
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            # Mark non-JSON server text as untrusted (it can include news/free text).
            return {"_untrusted_text": blob}
