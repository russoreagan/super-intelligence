"""
Engine-lane AGENDA gate — open threads may surface into a partner customer's turn
only when they bear on the active mandate's domain.

The live-work router (route_threads_for_turn → memory["open_threads"] → frontal
fence) runs on the shared turn path, so without a gate a persona's introspective
off-time threads (self-model, architecture) could surface into a customer-facing
engine turn whenever the wording happened to overlap. This pins the fix: on the
engine lane the router is handed the mandate's domain tags and drops any thread
whose bears_on doesn't overlap them; the companion owner lane is ungated and
unchanged.
"""

from __future__ import annotations

import brain.open_threads as ot
from brain.persona_context import mandate_domain_tags

# Reuse the routing test's DMN skeleton so this differs only in the domain gate.
from tests.test_dmn_routing import _make_dmn

# ── mandate_domain_tags derivation ───────────────────────────────────────────


def test_domain_tags_from_id_slug():
    # No catalog config needed — the id's own slug tokens are the default.
    assert mandate_domain_tags("market_analyst", {"market_analyst": {"text": "x"}}) == {
        "market",
        "analyst",
    }


def test_domain_tags_union_explicit_conduct_and_slug():
    catalog = {"trader": {"text": "x", "conduct": {"domain_tags": ["trading", "Markets"]}}}
    assert mandate_domain_tags("trader", catalog) == {"trading", "markets", "trader"}


def test_domain_tags_blank_id_is_empty():
    assert mandate_domain_tags("", {"trader": {"text": "x"}}) == set()
    assert mandate_domain_tags(None, None) == set()


def test_domain_tags_unknown_id_still_yields_slug():
    # An id not present in the catalog still contributes its own slug — the gate is
    # never accidentally empty just because config is missing.
    assert mandate_domain_tags("support", {}) == {"support"}


# ── the gate on route_threads_for_turn ───────────────────────────────────────


def _two_threads(dmn):
    """A domain thread (trading) and an introspective one (self-model), with an
    activity string that is relevant to BOTH so only the gate distinguishes them."""
    dmn._open_threads, t_domain = ot.open_thread(
        [], "watch AAPL momentum into earnings", bears_on=["trading", "aapl"]
    )
    dmn._open_threads, t_introspect = ot.open_thread(
        dmn._open_threads, "am I under-speaking at rest?", bears_on=["self-model"]
    )
    activity = "trading aapl and the self-model question at rest"
    return t_domain, t_introspect, activity


def test_engine_gate_surfaces_only_domain_threads():
    dmn = _make_dmn()
    t_domain, t_introspect, activity = _two_threads(dmn)
    routed = dmn.route_threads_for_turn(activity, budget=2, domain_tags={"trading"})
    ids = [t.id for t in routed]
    assert t_domain.id in ids  # bears_on overlaps the mandate domain
    assert t_introspect.id not in ids  # introspective thread withheld from the customer


def test_engine_gate_empty_domain_surfaces_nothing():
    """A customer turn with no mandate domain surfaces no threads at all."""
    dmn = _make_dmn()
    _t_domain, _t_introspect, activity = _two_threads(dmn)
    assert dmn.route_threads_for_turn(activity, budget=2, domain_tags=set()) == []


def test_companion_lane_ungated_is_unchanged():
    """domain_tags=None (the owner lane) keeps the introspective thread eligible —
    byte-identical to the pre-gate behaviour."""
    dmn = _make_dmn()
    _t_domain, t_introspect, activity = _two_threads(dmn)
    routed = dmn.route_threads_for_turn(activity, budget=2)  # None → ungated
    assert t_introspect.id in [t.id for t in routed]
