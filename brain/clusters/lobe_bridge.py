"""
LobeBridge — lets the motor cortex invoke other brain lobes during background jobs.

Motor planning is tool-driven. This bridge registers lobe capabilities as callable
handlers so execute_internal_job() can request visual analysis, memory recall, etc.
as named tools — the same interface as read_file or run_command — without the motor
cortex holding direct references to lobe objects.

Register handlers in run.py after the lobes are created:

    bridge = LobeBridge()
    bridge.register("recall_memory", lambda *, topic, entities, turn_id: ...)
    bridge.register("analyze_image", lambda *, path, question, turn_id: ...)
    motor.set_lobe_bridge(bridge)
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class LobeBridge:
    """Registry mapping capability names to async handlers that return plain text."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., Awaitable[str]]] = {}

    def register(self, capability: str, handler: Callable[..., Awaitable[str]]) -> None:
        self._handlers[capability] = handler
        logger.info("[LobeBridge] Registered: %s", capability)

    async def invoke(self, capability: str, **kwargs) -> str:
        handler = self._handlers.get(capability)
        if not handler:
            available = ", ".join(self._handlers) or "none"
            return f"[error] Capability '{capability}' not registered. Available: {available}"
        try:
            return await handler(**kwargs)
        except Exception as e:
            logger.error("[LobeBridge] %s failed: %s", capability, e)
            return f"[error] {capability} failed: {e}"

    @property
    def capabilities(self) -> list[str]:
        return list(self._handlers.keys())

    @property
    def available(self) -> bool:
        return bool(self._handlers)
