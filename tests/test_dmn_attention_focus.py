"""
The DMN is the cross-turn consumer that makes the paper's Global-Workspace claim
literal: content promoted to `attention.focus` is "available system-wide" BECAUSE
this subscriber reads it, and the persistent spotlight biases what the idle mind
dwells on.

Proves three properties:
  (a) REAL SUBSCRIBER — a DefaultModeNetwork built via its real __init__ subscribes
      to `attention.focus`; a publish is received and drained into DMN state.
  (b) BIAS — when the workspace is ignited with hot entities, the memory-seed and the
      rumination-seed selectors both prefer content matching the spotlight, flipping
      the choice away from the un-biased (last-context / finish-out) pick.
  (c) NO-OP — when nothing has ever ignited (including the flag-off path, where no
      broadcast is ever published) and on de-ignition, `_current_focus()` is None,
      `_spotlight_terms()` is "", and seeding is byte-identical to finish-out /
      last-context-only. Also the drained-broadcast liveness gate.

The LLM is never called: the seed selectors are pure functions of DMN state.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import brain.open_threads as ot
from brain.bus import Bus
from brain.dmn import (
    DMN_MEMORY_SEED_EVERY,
    DefaultModeNetwork,
    IdlePhase,
)

# ── Doubles ──────────────────────────────────────────────────────────────────


def _seed_double(
    *,
    episodes: list[dict] | None = None,
    last_context: str = "",
    last_focus: dict | None = None,
    thalamus=None,
    open_threads: list | None = None,
):
    """A DMN double (bypasses __init__) wired only with what the two seed selectors
    read. New workspace attrs default via getattr in the code under test, but we set
    them explicitly here so each test states its own spotlight condition."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    hip = MagicMock()
    hip._episodic.sample_random = MagicMock(return_value=list(episodes or []))
    dmn._hippocampus = hip
    dmn._thought_count = DMN_MEMORY_SEED_EVERY or 3  # % EVERY == 0 → seed fires
    dmn._tick_idle_phase = IdlePhase.WANDERING  # >= COOLING → idle gate open
    dmn._last_context = last_context
    dmn._memory_seed = ""
    dmn._last_focus = last_focus
    dmn._thalamus = thalamus
    dmn._open_threads = open_threads or []
    return dmn


_GLACIER_EP = {
    "user_input": "the glacier fields keep shrinking",
    "entity_response": "and methane release keeps accelerating",
    "topic_tags": ["climate"],
}
_FINANCE_EP = {
    "user_input": "quarterly revenue looks strong",
    "entity_response": "margins improved sharply",
    "topic_tags": ["finance"],
}
_TRAVEL_EP = {
    "user_input": "the mountain trail was steep",
    "entity_response": "the summit view was worth it",
    "topic_tags": ["travel"],
}

# hot entities/focus token-match the glacier episode exactly (token equality — no stemming).
_CLIMATE_FOCUS = {"cluster": "climate", "hot_entities": ["glacier", "methane"]}


# ── (a) Real subscriber ──────────────────────────────────────────────────────


async def test_dmn_is_a_real_attention_focus_subscriber():
    """A real DMN.__init__ registers an attention.focus subscription; a thalamus-style
    publish is received and drained into `_last_focus`, and `_current_focus()` exposes
    it. This is the literal GWT broadcast having a live cross-turn subscriber."""
    bus = Bus()
    dmn = DefaultModeNetwork(bus, router=MagicMock(), hippocampus=None, parietal=None)

    # __init__ wired the subscription (the queue is registered on the bus).
    assert "attention.focus" in bus._subscribers
    assert dmn._last_focus is None  # nothing ignited yet
    assert dmn._current_focus() is None
    assert dmn._spotlight_terms() == ""

    # The thalamus promotes a focus onto the workspace (locked payload shape).
    await bus.publish_dict(
        "attention.focus",
        {
            "cluster": "world_news",
            "coalition": "salience",
            "salience": 3.1,
            "hot_entities": ["ukraine", "aid"],
            "sustained_turns": 2,
        },
        source="thalamus",
    )

    dmn._drain_attention_focus()
    assert dmn._last_focus is not None
    assert dmn._last_focus["cluster"] == "world_news"
    assert dmn._last_focus["hot_entities"] == ["ukraine", "aid"]
    # No thalamus ref wired → the drained broadcast alone is the current focus.
    assert dmn._current_focus() == {
        "focus": "world_news",
        "hot_entities": ["ukraine", "aid"],
    }


async def test_drain_keeps_latest_and_persists_across_empty_drains():
    """The kept focus is the LATEST broadcast and persists across ticks: a drain that
    finds an empty queue leaves `_last_focus` untouched (the spotlight is cross-turn)."""
    bus = Bus()
    dmn = DefaultModeNetwork(bus, router=MagicMock(), hippocampus=None, parietal=None)

    await bus.publish_dict(
        "attention.focus", {"cluster": "a", "hot_entities": ["x"]}, source="thalamus"
    )
    await bus.publish_dict(
        "attention.focus", {"cluster": "b", "hot_entities": ["y"]}, source="thalamus"
    )
    dmn._drain_attention_focus()
    assert dmn._last_focus["cluster"] == "b"  # latest wins

    # Nothing new published — the focus persists (does not reset to None).
    dmn._drain_attention_focus()
    assert dmn._last_focus["cluster"] == "b"


# ── (b) Bias toward the spotlight ────────────────────────────────────────────


def test_memory_seed_biased_toward_spotlight_hot_entities():
    """With an ignited focus and NO conversational context, the memory seed is drawn
    from the spotlight: the glacier episode (matching hot entities) is chosen over the
    unrelated episodes that would otherwise win the empty-context first-episode path."""
    dmn = _seed_double(
        episodes=[_FINANCE_EP, _TRAVEL_EP, _GLACIER_EP],  # glacier is NOT episodes[0]
        last_context="",
        last_focus=_CLIMATE_FOCUS,
        thalamus=None,
    )
    assert dmn._spotlight_terms() != ""
    dmn._maybe_inject_memory_seed()
    assert "glacier" in dmn._memory_seed
    assert "revenue" not in dmn._memory_seed  # the finance episodes[0] did NOT win


def test_rumination_seed_biased_toward_spotlight_thread():
    """The rumination seed prefers the open thread matching the live focus over the
    most-advanced (finish-out) one. Without the spotlight, finish-out picks the
    high-advances revenue thread; WITH it, the low-advances glacier thread wins."""
    revenue = ot.Thread(id="t-rev", summary="quarterly revenue forecast model", advances=9, last_ts=200.0)
    glacier = ot.Thread(id="t-gla", summary="glacier melt and methane feedback", advances=1, last_ts=50.0)

    # Baseline (no spotlight) → finish-out picks the most-advanced thread.
    base = _seed_double(open_threads=[revenue, glacier], last_focus=None)
    assert base._current_seed_thread().id == "t-rev"

    # Ignited focus → the spotlight-matching thread wins despite fewer advances.
    biased = _seed_double(open_threads=[revenue, glacier], last_focus=_CLIMATE_FOCUS)
    assert biased._current_seed_thread().id == "t-gla"


# ── (c) No-op guarantee + liveness gate ──────────────────────────────────────


def test_no_ignition_is_a_strict_noop():
    """Nothing has ever ignited (flag-off path: no broadcast ever published →
    `_last_focus` None). `_current_focus()` is None, `_spotlight_terms()` is "", and
    both selectors behave exactly as before the workspace existed."""
    dmn = _seed_double(last_focus=None, thalamus=None)
    assert dmn._current_focus() is None
    assert dmn._spotlight_terms() == ""

    # Memory seed: with a real conversational context and no spotlight, selection is
    # driven purely by ctx overlap (the finance episode), never by any spotlight leak.
    dmn2 = _seed_double(
        episodes=[_GLACIER_EP, _TRAVEL_EP, _FINANCE_EP],
        last_context="quarterly revenue margins for finance",
        last_focus=None,
    )
    dmn2._maybe_inject_memory_seed()
    assert "revenue" in dmn2._memory_seed
    assert "glacier" not in dmn2._memory_seed

    # Rumination seed: finish-out (most-advanced) with no spotlight influence.
    revenue = ot.Thread(id="t-rev", summary="quarterly revenue forecast model", advances=9, last_ts=200.0)
    glacier = ot.Thread(id="t-gla", summary="glacier melt and methane feedback", advances=1, last_ts=50.0)
    dmn3 = _seed_double(open_threads=[revenue, glacier], last_focus=None)
    assert dmn3._current_seed_thread().id == "t-rev"


def test_memory_seed_byte_identical_with_and_without_workspace_attrs():
    """A double that never sets `_last_focus`/`_thalamus` (pre-workspace shape) and one
    that sets `_last_focus=None` produce the SAME memory seed — the workspace code adds
    nothing until an ignition occurs."""
    eps = [_GLACIER_EP, _TRAVEL_EP, _FINANCE_EP]
    ctx = "quarterly revenue margins for finance"

    with_attr = _seed_double(episodes=list(eps), last_context=ctx, last_focus=None)
    with_attr._maybe_inject_memory_seed()

    # Pre-workspace shape: strip the attributes so the code falls through getattr(None).
    bare = _seed_double(episodes=list(eps), last_context=ctx)
    del bare._last_focus
    del bare._thalamus
    bare._maybe_inject_memory_seed()

    assert with_attr._memory_seed == bare._memory_seed
    assert with_attr._memory_seed  # non-empty (the finance episode was surfaced)


def test_thalamus_gate_stops_bias_on_deignition():
    """A wired thalamus is the authoritative liveness gate. Even with a last-drained
    ignited broadcast in `_last_focus`, once current_spotlight() reports NOT ignited
    (de-ignition, which the change-only broadcast never re-announces) `_current_focus()`
    returns None and the bias switches off."""
    ignited_thal = MagicMock()
    ignited_thal.current_spotlight = MagicMock(return_value={"ignited": True, "focus": "climate"})
    quiet_thal = MagicMock()
    quiet_thal.current_spotlight = MagicMock(
        return_value={"ignited": False, "focus": None, "hot_entities": []}
    )

    # Ignited: the drained broadcast is used.
    live = _seed_double(last_focus=_CLIMATE_FOCUS, thalamus=ignited_thal)
    assert live._current_focus() == {"focus": "climate", "hot_entities": ["glacier", "methane"]}
    assert live._spotlight_terms() != ""

    # De-ignited: same stale `_last_focus`, but the gate closes → no bias.
    quiet = _seed_double(last_focus=_CLIMATE_FOCUS, thalamus=quiet_thal)
    assert quiet._current_focus() is None
    assert quiet._spotlight_terms() == ""

    # And the rumination selector falls back to finish-out under the closed gate.
    revenue = ot.Thread(id="t-rev", summary="quarterly revenue forecast model", advances=9, last_ts=200.0)
    glacier = ot.Thread(id="t-gla", summary="glacier melt and methane feedback", advances=1, last_ts=50.0)
    quiet._open_threads = [revenue, glacier]
    assert quiet._current_seed_thread().id == "t-rev"
