"""
Tests for LobeBridge.

Coverage:
  - register() stores handler; capabilities and available reflect it
  - invoke() dispatches to registered handler with correct kwargs
  - invoke() with unregistered capability returns [error] listing available ones
  - invoke() when handler raises wraps exception as [error]
  - available is False when empty, True after registration
"""

from __future__ import annotations

from brain.clusters.lobe_bridge import LobeBridge

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _echo_handler(*, topic: str = "", **_kwargs) -> str:
    return f"echoed: {topic}"


async def _fail_handler(**_kwargs) -> str:
    raise ValueError("handler exploded")


# ---------------------------------------------------------------------------
# Registration & introspection
# ---------------------------------------------------------------------------


class TestLobeBridgeRegistration:
    def test_empty_bridge_has_no_capabilities(self):
        bridge = LobeBridge()
        assert bridge.capabilities == []
        assert bridge.available is False

    def test_register_adds_capability(self):
        bridge = LobeBridge()
        bridge.register("recall_memory", _echo_handler)
        assert "recall_memory" in bridge.capabilities
        assert bridge.available is True

    def test_register_multiple_capabilities(self):
        bridge = LobeBridge()
        bridge.register("recall_memory", _echo_handler)
        bridge.register("analyze_image", _echo_handler)
        assert set(bridge.capabilities) == {"recall_memory", "analyze_image"}

    def test_register_overwrites_existing(self):
        bridge = LobeBridge()

        async def handler_v1(**_kwargs) -> str:
            return "v1"

        async def handler_v2(**_kwargs) -> str:
            return "v2"

        bridge.register("recall_memory", handler_v1)
        bridge.register("recall_memory", handler_v2)
        assert len(bridge.capabilities) == 1  # not duplicated


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


class TestLobeBridgeInvoke:
    async def test_invoke_dispatches_to_handler(self):
        bridge = LobeBridge()
        bridge.register("recall_memory", _echo_handler)
        result = await bridge.invoke("recall_memory", topic="neural plasticity")
        assert result == "echoed: neural plasticity"

    async def test_invoke_passes_all_kwargs(self):
        received: dict = {}

        async def capturing_handler(**kwargs) -> str:
            received.update(kwargs)
            return "ok"

        bridge = LobeBridge()
        bridge.register("analyze_image", capturing_handler)
        await bridge.invoke(
            "analyze_image", path="/tmp/img.png", question="what is in this image", turn_id="t1"
        )
        assert received["path"] == "/tmp/img.png"
        assert received["question"] == "what is in this image"
        assert received["turn_id"] == "t1"

    async def test_invoke_unregistered_returns_error(self):
        bridge = LobeBridge()
        bridge.register("recall_memory", _echo_handler)
        result = await bridge.invoke("analyze_image", path="/tmp/x.png", question="?")
        assert result.startswith("[error]")
        assert "analyze_image" in result
        assert "recall_memory" in result  # lists what IS available

    async def test_invoke_empty_bridge_returns_error(self):
        bridge = LobeBridge()
        result = await bridge.invoke("recall_memory", topic="anything")
        assert result.startswith("[error]")
        assert "none" in result.lower() or "not registered" in result.lower()

    async def test_handler_exception_returns_error(self):
        bridge = LobeBridge()
        bridge.register("recall_memory", _fail_handler)
        result = await bridge.invoke("recall_memory", topic="x")
        assert result.startswith("[error]")
        assert "exploded" in result

    async def test_handler_exception_does_not_propagate(self):
        """Exceptions from handlers must never raise out of invoke()."""
        bridge = LobeBridge()
        bridge.register("recall_memory", _fail_handler)
        # Should not raise:
        result = await bridge.invoke("recall_memory", topic="anything")
        assert "[error]" in result
