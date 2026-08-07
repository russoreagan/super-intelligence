"""Global-workspace spotlight → hippocampus recall wiring (locked contract).

The thalamus writes a "spotlight" verdict into ``features`` before recall runs.
This cluster is one consumer: when a coalition has IGNITED, recall (a) seeds its
cue with the focus's ``hot_entities`` and (b) widens the fan-out budget a notch.

The HARD requirement is neutrality: when the spotlight is not ignited — and when
the "spotlight" key is absent entirely (older callers, and the flag-off path,
which always yields not-ignited) — recall must be byte-identical to the pre-
spotlight behaviour: same entities in the cue, same budget. These tests drive the
real ``recall()`` and prove both the ignited bias and the no-op.
"""

from __future__ import annotations


class _Router:
    async def call(self, *a, **kw):
        return "{}"

    def supports(self, *a, **kw):
        return True


def _make_hippo():
    from brain.bus import Bus
    from brain.clusters.hippocampus import HippocampusCluster

    return HippocampusCluster(Bus(), _Router())


async def _capture_recall(features, entities):
    """Run ``recall()`` with schema/episode side effects stubbed out, and capture
    the effective entity list the cue was built from plus the fan-out budget used.

    ``_normalize_recall_key`` receives the final (post-seed) entities and is called
    exactly once per recall; ``_fanout_total_budget`` returns the budget actually
    spent. Spying on both reads out exactly what the spotlight did (or didn't) do.
    """
    hippo = _make_hippo()
    hippo._schema.grep = lambda keyword: []  # hermetic: no schema-store file I/O

    captured: dict = {}

    orig_key = hippo._normalize_recall_key

    def key_spy(query, ents):
        captured["entities"] = list(ents)
        return orig_key(query, ents)

    hippo._normalize_recall_key = key_spy

    orig_budget = hippo._fanout_total_budget

    def budget_spy(chem, spotlight=None):
        b = orig_budget(chem, spotlight)
        captured["budget"] = b
        return b

    hippo._fanout_total_budget = budget_spy

    await hippo.recall(
        query="the-quiet-signal",
        entities=list(entities),
        turn_id="t1",
        embedding_fn=None,  # skips episodic search; schema grep is stubbed above
        novelty=False,  # structural pass stays shut
        features=features,
    )
    return captured


def _ignited(**over):
    spot = {
        "ignited": True,
        "focus": "mem.recall",
        "coalition": "memory",
        "salience": 9.0,
        "quorum": True,
        "rising": True,
        "sustained_turns": 3,
        "hot_entities": [],
        "priorities": {},
    }
    spot.update(over)
    return {"spotlight": spot}


# ── (a) ignited: hot_entities seed the cue, budget widens ────────────────────


async def test_ignited_seeds_hot_entities_into_cue():
    cap = await _capture_recall(
        _ignited(hot_entities=["saturn", "MERCURY"]),  # MERCURY dups 'mercury'
        entities=["mercury", "venus"],
    )
    # 'saturn' (sustained focus not named this turn) is merged in; the
    # case-insensitive duplicate 'MERCURY' is dropped (dedup vs entities passed).
    assert cap["entities"] == ["mercury", "venus", "saturn"]


async def test_ignited_widens_fanout_budget():
    base = await _capture_recall({}, entities=["mercury", "venus"])
    ignited = await _capture_recall(_ignited(salience=10.0), entities=["mercury", "venus"])
    assert ignited["budget"] >= base["budget"]
    # At neutral chemistry the base is 8 and max salience nudges it strictly up.
    assert ignited["budget"] > base["budget"]


# ── (b) not ignited / absent key: strict, byte-identical no-op ───────────────


async def test_not_ignited_is_byte_identical():
    ent = ["mercury", "venus"]
    # Three not-ignited shapes: present-but-off (with hot_entities that MUST be
    # ignored), key absent, and features=None (oldest callers).
    off = await _capture_recall(
        {"spotlight": {"ignited": False, "salience": 10.0, "hot_entities": ["saturn"]}},
        entities=ent,
    )
    key_absent = await _capture_recall({"intent": "question"}, entities=ent)
    features_none = await _capture_recall(None, entities=ent)

    # Entities: the input list verbatim, no seeding, across every not-ignited shape.
    assert off["entities"] == ent
    assert key_absent["entities"] == ent
    assert features_none["entities"] == ent

    # Budget: identical across every not-ignited shape (no widening applied).
    assert off["budget"] == key_absent["budget"] == features_none["budget"]
