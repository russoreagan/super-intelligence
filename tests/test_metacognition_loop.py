"""The self-monitor's periodic loop survives a failed reflection.

`asyncio.create_task(self._loop())` swallows the traceback of a task that dies,
so one bad reflection (a router error, a malformed stats window) used to end
self-monitoring for the rest of the process with nothing in the log.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from brain import metacognition as mc
from brain.metacognition import MetacognitionCell


def _fake_asyncio(sleep):
    # Swap the module reference (not stdlib asyncio.sleep globally): _loop only
    # needs sleep + CancelledError.
    return types.SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)


@pytest.mark.asyncio
async def test_loop_logs_and_continues_after_a_reflection_error(monkeypatch, caplog):
    cell = MetacognitionCell.__new__(MetacognitionCell)
    calls: list[int] = []

    async def _reflect():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("router down")

    cell._reflect = _reflect
    ticks = {"n": 0}

    async def _sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(mc, "asyncio", _fake_asyncio(_sleep))
    with (
        caplog.at_level("ERROR", logger="brain.metacognition"),
        pytest.raises(asyncio.CancelledError),
    ):
        await cell._loop()
    # First reflection raised, the loop kept its cadence, the second one ran.
    assert calls == [1, 2]
    assert any("reflection failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_loop_still_propagates_cancellation_from_reflect(monkeypatch):
    cell = MetacognitionCell.__new__(MetacognitionCell)
    calls = {"n": 0}

    async def _reflect():
        calls["n"] += 1
        raise asyncio.CancelledError

    cell._reflect = _reflect

    async def _sleep(_s):
        return None

    monkeypatch.setattr(mc, "asyncio", _fake_asyncio(_sleep))
    with pytest.raises(asyncio.CancelledError):
        await cell._loop()
    assert calls["n"] == 1  # shutdown is not "an error to retry"
