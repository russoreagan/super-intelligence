"""
ClientChemRegistry — the per-(persona, end_user) chemistry contract.

Covers: get-or-create (baseline seed + caching), restore-with-absence-decay on a
returning customer, persist round-trip across registries, the interaction-mass
weighted average for cycle consolidation, and the one-way valve (the aggregate
never mutates a client pair).

Also pins the durable wiring: default_store's persona/tenant path routing (the
bug class where one tenant's customer moods land in another's tree, or every
persona writes the home persona's files), survival across a simulated process
restart, the per-customer write throttle, and the rule that a broken store
degrades to in-memory rather than raising into a turn.
"""

from __future__ import annotations

import pytest

from brain.bus import Bus
from brain.client_chem import (
    ClientChemRegistry,
    FileChemStore,
    InMemoryChemStore,
    default_store,
)


def _clock(start: float = 1000.0):
    box = {"t": start}
    return box, (lambda: box["t"])


def test_get_or_create_seeds_baseline_and_caches():
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    a1 = reg.get_or_create("alice")
    a2 = reg.get_or_create("alice")
    assert a1 is a2  # cached — same live pair across the session
    # A fresh customer starts at the persona's init levels (not the resting pair).
    assert a1 is not bus.resting_chem


def test_returning_customer_restored_with_absence_decay():
    bus = Bus()
    store = InMemoryChemStore()
    box, now = _clock()

    reg1 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    pair = reg1.get_or_create("alice")
    pair.neuromod.add("DA", 0.4)  # an uplifting session
    elevated = pair.neuromod.get("DA")
    reg1.persist("alice")  # snapshot at t0

    # Ten "turns" worth of time passes before she returns.
    box["t"] += 10 * 180.0

    reg2 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    returned = reg2.get_or_create("alice")
    baseline = returned.neuromod._baseline["DA"]
    da = returned.neuromod.get("DA")

    # Mood relaxed toward the temperament baseline while away, but isn't reset:
    assert baseline < da < elevated


def test_persist_roundtrip_across_registries():
    bus = Bus()
    store = InMemoryChemStore()
    box, now = _clock()

    reg1 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    p = reg1.get_or_create("bob")
    p.hormonal.add("OXT", 0.2)
    saved_oxt = p.hormonal.get("OXT")
    reg1.persist("bob")

    # No time passes → restored value should match (no absence-decay).
    reg2 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    p2 = reg2.get_or_create("bob")
    assert p2.hormonal.get("OXT") == pytest.approx(saved_oxt)


def test_weighted_average_by_interaction_mass():
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")

    a = reg.get_or_create("alice")
    b = reg.get_or_create("bob")
    a.neuromod.add("DA", 0.1)
    b.neuromod.add("DA", 0.2)
    da_a = a.neuromod.get("DA")
    da_b = b.neuromod.get("DA")

    # Alice had a long session (weight 3), Bob a brief one (weight 1).
    for _ in range(3):
        reg.note_interaction("alice")
    reg.note_interaction("bob")

    avg = reg.weighted_average()
    assert avg is not None
    assert avg["neuromod"]["DA"] == pytest.approx((3 * da_a + 1 * da_b) / 4)


def test_weighted_average_is_one_way_does_not_mutate_clients():
    """The aggregate is read-only over client pairs — it must never write back."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    a = reg.get_or_create("alice")
    a.neuromod.add("DA", 0.15)
    before = a.neuromod.snapshot()

    reg.note_interaction("alice")
    _ = reg.weighted_average()

    assert a.neuromod.snapshot() == before  # unchanged


def test_weighted_average_empty_returns_none():
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    assert reg.weighted_average() is None


def test_is_fanned_out_threshold():
    """Mode-emergent engine signal: fan-out only once ≥2 distinct customers are
    live, so all engine-only behaviour stays inert in companion mode (0–1)."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    assert reg.active_client_count() == 0
    assert not reg.is_fanned_out()

    reg.get_or_create("alice")
    assert reg.active_client_count() == 1
    assert not reg.is_fanned_out()  # one customer = still companion-like

    reg.get_or_create("bob")
    assert reg.active_client_count() == 2
    assert reg.is_fanned_out()  # ≥2 → engine mode


def test_consolidate_into_resting_blends_and_is_one_way():
    """The cycle average pulls the resting mood toward it by alpha, leaves every
    client pair untouched (one-way valve), and clears cycle mass."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    a = reg.get_or_create("alice")
    b = reg.get_or_create("bob")
    a.neuromod.add("DA", 0.1)
    b.neuromod.add("DA", 0.2)
    reg.note_interaction("alice")
    reg.note_interaction("bob")

    avg_da = reg.weighted_average()["neuromod"]["DA"]
    resting_before = bus.resting_chem.neuromod.get("DA")
    a_before, b_before = a.neuromod.snapshot(), b.neuromod.snapshot()

    snap = reg.consolidate_into_resting(alpha=0.5)
    assert snap is not None

    # Resting moved halfway (alpha=0.5) toward the weighted average.
    assert bus.resting_chem.neuromod.get("DA") == pytest.approx(
        resting_before + 0.5 * (avg_da - resting_before)
    )
    # One-way valve: the aggregate never wrote back onto a client.
    assert a.neuromod.snapshot() == a_before
    assert b.neuromod.snapshot() == b_before
    # Cycle mass cleared for the next cycle.
    assert reg._mass == {}


def test_consolidate_into_resting_noop_without_fanout():
    """With only one customer (not fanned out) there is no separate 'overall' mood
    to fold in — companion-equivalent, so resting is left untouched."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    reg.get_or_create("alice")
    reg.note_interaction("alice")
    resting_before = bus.resting_chem.neuromod.snapshot()
    assert reg.consolidate_into_resting(0.5) is None
    assert bus.resting_chem.neuromod.snapshot() == resting_before


def test_filechemstore_roundtrip(tmp_path):
    store = FileChemStore(tmp_path)
    assert store.load("empath:alice") == (None, None)
    snap = {"neuromod": {"DA": 0.4}, "hormonal": {"OXT": 0.2}}
    store.save("empath:alice", snap, 1234.0)
    loaded, ts = store.load("empath:alice")
    assert loaded == snap
    assert ts == 1234.0


def test_registry_persists_via_filechemstore(tmp_path):
    """A returning customer is restored from disk across separate registries."""
    bus = Bus()
    box, now = _clock()
    store = FileChemStore(tmp_path)

    reg1 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    p = reg1.get_or_create("alice")
    p.neuromod.add("DA", 0.3)
    saved = p.neuromod.get("DA")
    reg1.persist("alice")

    reg2 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    p2 = reg2.get_or_create("alice")  # no time passed → no absence decay
    assert p2.neuromod.get("DA") == pytest.approx(saved)


# ── Durability across a restart ───────────────────────────────────────────────


def test_mood_survives_simulated_restart(tmp_path):
    """THE point of the durable store: a fresh Bus + fresh registry over the same
    root (i.e. the process died and came back) resumes the customer's mood instead
    of resetting them to the temperament baseline on every deploy."""
    box, now = _clock()

    reg1 = ClientChemRegistry(Bus(), FileChemStore(tmp_path), persona="empath", now_fn=now)
    p = reg1.get_or_create("alice")
    baseline = p.neuromod._baseline["DA"]
    p.neuromod.add("DA", 0.3)
    saved = p.neuromod.get("DA")
    reg1.persist("alice")

    # ── process restart: nothing in memory survives, only the volume ──
    reg2 = ClientChemRegistry(Bus(), FileChemStore(tmp_path), persona="empath", now_fn=now)
    restored = reg2.get_or_create("alice").neuromod.get("DA")

    assert restored == pytest.approx(saved)
    assert restored > baseline  # and it is NOT the cold-start baseline


def test_two_customers_stay_independent_across_restart(tmp_path):
    """One persona, two customers, one store: each relationship keeps its own mood
    through a restart. A shared file or colliding key would show up as equal moods."""
    box, now = _clock()

    reg1 = ClientChemRegistry(Bus(), FileChemStore(tmp_path), persona="empath", now_fn=now)
    reg1.get_or_create("alice").neuromod.add("DA", 0.30)
    reg1.get_or_create("bob").neuromod.add("DA", 0.05)
    alice_da = reg1.get_or_create("alice").neuromod.get("DA")
    bob_da = reg1.get_or_create("bob").neuromod.get("DA")
    assert alice_da != pytest.approx(bob_da)
    reg1.flush()

    reg2 = ClientChemRegistry(Bus(), FileChemStore(tmp_path), persona="empath", now_fn=now)
    assert reg2.get_or_create("alice").neuromod.get("DA") == pytest.approx(alice_da)
    assert reg2.get_or_create("bob").neuromod.get("DA") == pytest.approx(bob_da)


def test_two_personas_same_customer_do_not_alias(tmp_path):
    """The same end_user talking to two personas is two relationships. Keys are
    persona-qualified, so even sharing one store root they never alias."""
    box, now = _clock()
    store = FileChemStore(tmp_path)

    empath = ClientChemRegistry(Bus(), store, persona="empath", now_fn=now)
    cynic = ClientChemRegistry(Bus(), store, persona="cynic", now_fn=now)
    empath.get_or_create("alice").neuromod.add("DA", 0.30)
    cynic.get_or_create("alice").neuromod.add("DA", 0.02)
    empath_da = empath.get_or_create("alice").neuromod.get("DA")
    cynic_da = cynic.get_or_create("alice").neuromod.get("DA")
    empath.flush()
    cynic.flush()

    assert ClientChemRegistry(Bus(), store, persona="empath", now_fn=now).get_or_create(
        "alice"
    ).neuromod.get("DA") == pytest.approx(empath_da)
    assert ClientChemRegistry(Bus(), store, persona="cynic", now_fn=now).get_or_create(
        "alice"
    ).neuromod.get("DA") == pytest.approx(cynic_da)


def test_absence_decay_applies_on_restore_from_disk(tmp_path):
    """Absence-decay is a property of restoring, not of the in-memory store: time
    away still relaxes a disk-restored mood toward the temperament baseline."""
    box, now = _clock()

    reg1 = ClientChemRegistry(Bus(), FileChemStore(tmp_path), persona="empath", now_fn=now)
    p = reg1.get_or_create("alice")
    p.neuromod.add("DA", 0.4)
    elevated = p.neuromod.get("DA")
    reg1.persist("alice")

    box["t"] += 10 * 180.0  # ten "turns" of absence, then a restart

    reg2 = ClientChemRegistry(Bus(), FileChemStore(tmp_path), persona="empath", now_fn=now)
    returned = reg2.get_or_create("alice")
    da = returned.neuromod.get("DA")

    # Relaxed toward baseline while away, but the relationship isn't erased.
    assert returned.neuromod._baseline["DA"] < da < elevated


# ── Write volume: per-customer throttle + shutdown flush ──────────────────────


class CountingStore(InMemoryChemStore):
    """InMemoryChemStore that counts writes — stands in for the file store's I/O."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def save(self, key: str, snapshot: dict, last_seen_ts: float) -> None:
        self.writes += 1
        super().save(key, snapshot, last_seen_ts)


def test_persist_is_throttled_per_customer():
    """Chemistry moves every turn and the turn path persists every turn, so the
    engine throttles. The throttle is PER CUSTOMER — a chatty customer must not
    starve a quiet one's writes."""
    box, now = _clock()
    store = CountingStore()
    reg = ClientChemRegistry(Bus(), store, persona="empath", min_persist_interval_s=5.0, now_fn=now)
    reg.get_or_create("alice")
    reg.get_or_create("bob")

    reg.persist("alice")  # first write for alice — always lands
    assert store.writes == 1
    for _ in range(20):  # a burst of turns inside the interval
        reg.persist("alice")
    assert store.writes == 1  # ...collapses to the one write

    reg.persist("bob")  # bob's first write is NOT throttled by alice's
    assert store.writes == 2

    box["t"] += 5.0  # interval elapses
    reg.persist("alice")
    assert store.writes == 3


def test_persist_unthrottled_by_default():
    """Default interval 0 = write on every call: the contract the registry had
    before throttling existed, and what the rest of these tests rely on."""
    store = CountingStore()
    reg = ClientChemRegistry(Bus(), store, persona="empath")
    reg.get_or_create("alice")
    for _ in range(3):
        reg.persist("alice")
    assert store.writes == 3


def test_flush_forces_past_the_throttle():
    """Shutdown must not lose the last turns to a throttle that hasn't expired."""
    box, now = _clock()
    store = CountingStore()
    reg = ClientChemRegistry(Bus(), store, persona="empath", min_persist_interval_s=5.0, now_fn=now)
    reg.get_or_create("alice").neuromod.add("DA", 0.2)
    reg.get_or_create("bob").neuromod.add("DA", 0.1)
    reg.persist("alice")
    reg.persist("bob")
    store.writes = 0

    reg.flush()  # inside the throttle window, but forced
    assert store.writes == 2

    # And the flushed values are the live ones, not the throttled-stale ones.
    snap, _ = store.load("empath:alice")
    assert snap["neuromod"]["DA"] == pytest.approx(reg.get_or_create("alice").neuromod.get("DA"))


def test_forget_clears_throttle_state():
    """A purge drops throttle bookkeeping too, so a re-seen id writes immediately
    rather than being silently skipped by the departed customer's timestamp."""
    box, now = _clock()
    store = CountingStore()
    reg = ClientChemRegistry(Bus(), store, persona="empath", min_persist_interval_s=5.0, now_fn=now)
    reg.get_or_create("alice")
    reg.persist("alice")
    reg.forget("alice")
    assert "alice" not in reg._last_persist

    reg.get_or_create("alice")
    reg.persist("alice")
    assert store.writes == 2  # not swallowed by the pre-purge timestamp


# ── Failure containment: a broken store must never break a turn ───────────────


class BrokenStore:
    """A store where everything raises — disk full, volume detached, bad backend."""

    def load(self, key: str) -> tuple[dict | None, float | None]:
        raise OSError("volume gone")

    def save(self, key: str, snapshot: dict, last_seen_ts: float) -> None:
        raise OSError("volume gone")


def test_store_failure_degrades_to_memory_and_never_breaks_a_turn():
    """A store error must degrade to in-memory behaviour, not raise into the turn
    path — losing a customer's mood is survivable, dropping their turn is not."""
    bus = Bus()
    reg = ClientChemRegistry(bus, BrokenStore(), persona="empath")

    pair = reg.get_or_create("alice")  # load raises → cold-start baseline
    assert pair.neuromod.get("DA") == pytest.approx(pair.neuromod._baseline["DA"])

    # The full turn shape still works: bind, move chemistry, persist.
    with bus.bind(pair):
        bus.neuromod.add("DA", 0.2)
    moved = pair.neuromod.get("DA")
    reg.persist("alice")  # save raises → swallowed
    reg.flush()

    # In-memory behaviour intact: the live pair kept the mood for this session.
    assert reg.get_or_create("alice") is pair
    assert pair.neuromod.get("DA") == pytest.approx(moved)


def test_broken_store_does_not_disturb_the_resting_pair():
    """Failure containment must not spill into the persona's own chemistry."""
    bus = Bus()
    resting_before = bus.resting_chem.neuromod.snapshot()
    reg = ClientChemRegistry(bus, BrokenStore(), persona="empath")
    with bus.bind(reg.get_or_create("alice")):
        bus.neuromod.add("DA", 0.3)
    reg.persist("alice")
    assert bus.resting_chem.neuromod.snapshot() == resting_before


# ── default_store: multi-tenant / multi-persona path routing ──────────────────


def test_default_store_routes_home_persona_to_state_root(tmp_path, monkeypatch):
    """The home persona's customer moods live under the tenant's own root."""
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Empath")

    store = default_store("The Empath")
    assert isinstance(store, FileChemStore)
    assert store._root == tmp_path / "client_chem"


def test_default_store_routes_other_persona_to_its_own_dir(tmp_path, monkeypatch):
    """A NON-home persona must resolve to its sibling personas/<slug>/ dir. Without
    this it silently reads and writes the HOME persona's files — SECOND_BRAIN_PATH
    is frozen at boot to the home persona's root."""
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Empath")

    store = default_store("The Analyst")
    assert store._root == tmp_path / "personas" / "the_analyst" / "client_chem"
    # Display name or slug resolves identically (persona_slug is idempotent).
    assert default_store("the_analyst")._root == store._root
    # ...and that is NOT the home persona's tree.
    assert store._root != default_store("The Empath")._root


def test_default_store_isolates_tenants(tmp_path, monkeypatch):
    """Each hosted org gets its own volume via SECOND_BRAIN_PATH, resolved at CALL
    time. One tenant's customer moods must never land in another tenant's tree."""
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Empath")
    org_a, org_b = tmp_path / "org_a", tmp_path / "org_b"

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(org_a))
    store_a = default_store("The Empath")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(org_b))
    store_b = default_store("The Empath")

    assert store_a._root == org_a / "client_chem"
    assert store_b._root == org_b / "client_chem"

    # The same persona + same customer id writes to two disjoint trees.
    ClientChemRegistry(Bus(), store_a, persona="The Empath").get_or_create("alice")
    reg_a = ClientChemRegistry(Bus(), store_a, persona="The Empath")
    reg_a.get_or_create("alice").neuromod.add("DA", 0.3)
    reg_a.persist("alice")

    assert store_b.load("The Empath:alice") == (None, None)  # nothing bled across
    assert not any(org_b.rglob("*.json"))


def test_default_store_degrades_to_memory_when_root_unwritable(tmp_path, monkeypatch):
    """An unwritable volume must not raise on the way to a turn — losing durability
    is the correct trade against failing the customer's turn."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("i am a file")  # mkdir under a regular file → OSError
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(blocker / "brain"))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Empath")

    store = default_store("The Empath")
    assert isinstance(store, InMemoryChemStore)

    # And it is a working store — the turn path carries on in memory.
    reg = ClientChemRegistry(Bus(), store, persona="The Empath")
    reg.get_or_create("alice").neuromod.add("DA", 0.2)
    reg.persist("alice")
    assert store.load("The Empath:alice")[0] is not None
