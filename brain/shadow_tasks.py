"""Shadow validation off the user's critical path.

A predictor gate (frontal executive, temporal understanding) skips an LLM call when
it is confident. On a sampled fraction of those skips (`gating_shadow_sample_rate`)
the skipped call runs anyway, purely to measure whether the gate was right: the
answer is discarded, only the verdict is recorded. Nothing on the turn reads it,
yet it used to be awaited — the user waited a full Sonnet call (executive) or
Gemini parse (temporal) for a number that only feeds the predictor's history.

`spawn` runs that work as a background task instead. Each shadow runs on its own
copy of the cell (`shadow_cell`), so it never shares the live cell's per-turn call
counter with the next turn. The task copies the turn's context at creation, so the
chemistry, persona and lane bindings it records against are this turn's.

`shadow_validation_background` (default 1) is the kill switch: 0 awaits the shadow
inline exactly as before.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)

# Strong references: the event loop keeps only weak ones, so an unreferenced task
# can be garbage-collected mid-run.
_pending: set[asyncio.Task] = set()


def in_background() -> bool:
    from brain.settings import settings

    return bool(int(settings.get("shadow_validation_background", 1) or 0))


def shadow_cell(cell):
    """A private copy of an IntegratorCell for one shadow run (same model, prompt and
    limits; its own call counter). Non-dataclass stand-ins are returned as-is."""
    if not dataclasses.is_dataclass(cell):
        return cell
    clone = dataclasses.replace(cell)
    clone.set_router(cell._router)
    return clone


async def run(coro: Coroutine, what: str) -> None:
    """Run a shadow: in the background (default) or inline (kill switch off)."""
    if not in_background():
        await coro
        return
    task = asyncio.get_running_loop().create_task(coro)
    _pending.add(task)

    def _done(t: asyncio.Task) -> None:
        _pending.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("[shadow] %s failed: %s", what, t.exception())

    task.add_done_callback(_done)


def pending() -> list[asyncio.Task]:
    """Shadows still running on the CURRENT event loop. A task whose loop has closed
    can never finish (a short-lived loop, e.g. a test's) — it is dropped here rather
    than held forever."""
    loop = asyncio.get_running_loop()
    for t in list(_pending):
        if t.get_loop().is_closed():
            _pending.discard(t)
    return [t for t in _pending if t.get_loop() is loop]


async def drain() -> None:
    """Wait for every pending shadow on this loop (tests, orderly shutdown)."""
    while tasks := pending():
        await asyncio.gather(*tasks, return_exceptions=True)
