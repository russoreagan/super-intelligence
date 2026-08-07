"""
Per-turn persona binding (multi-persona Path B). bind_persona() scopes persona
resolution for the duration of a turn so ONE process can serve many personas; the
store/mandate layers resolve through _resolve_persona, which now consults the
contextvar. Unbound → process env, byte-for-byte unchanged.
"""

from __future__ import annotations

from brain.second_brain.store import _persona_key, _resolve_persona, active_persona, bind_persona


def test_unbound_uses_env_default(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    assert _resolve_persona("") == "The Sage"
    assert active_persona() == ""  # nothing bound


def test_bind_persona_overrides_resolution(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    with bind_persona("The Visionary"):
        assert active_persona() == "The Visionary"
        assert _resolve_persona("") == "The Visionary"
        assert _persona_key(_resolve_persona("")) == "the_visionary"
    # Resets cleanly after the block → back to the process default.
    assert active_persona() == ""
    assert _resolve_persona("") == "The Sage"


def test_explicit_arg_still_wins_over_binding(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    with bind_persona("The Visionary"):
        # An explicit persona (e.g. a store constructed for a specific persona) wins.
        assert _resolve_persona("The Adversary") == "The Adversary"


def test_empty_bind_is_a_noop(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    with bind_persona(""):
        assert _resolve_persona("") == "The Sage"  # unchanged


def test_new_chem_for_applies_persona_baseline_and_levels():
    # Per-persona chemistry (Path B): a ChemPair can carry a specific persona's
    # resting BASELINES (what it decays toward) and current LEVELS, split across the
    # neuromod (DA…) and hormonal (OXT…) layers from one flat profile dict.
    from brain.bus import Bus

    bus = Bus()
    baseline = {"DA": 0.62, "OXT": 0.45}  # DA = neuromod, OXT = hormonal
    levels = {"DA": 0.70, "OXT": 0.50}
    pair = bus.new_chem_for(baseline, levels)
    assert abs(pair.neuromod._baseline["DA"] - 0.62) < 1e-6
    assert abs(pair.neuromod.get("DA") - 0.70) < 1e-6
    assert abs(pair.hormonal._baseline["OXT"] - 0.45) < 1e-6
    assert abs(pair.hormonal.snapshot()["OXT"] - 0.50) < 1e-6
    # Decay relaxes toward THIS persona's baseline, not the process default.
    pair.neuromod.add("DA", 0.25)
    pair.neuromod.decay(8)
    assert pair.neuromod.get("DA") < 0.80  # pulled back toward 0.62


def test_catalog_caches_per_persona(monkeypatch):
    # In local mode the catalog is empty, but the cache must be keyed per persona so
    # two personas don't share one entry (the Path B cache-by-persona invariant).
    import brain.mandates as mandates

    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    mandates._catalog = {}
    with bind_persona("The Visionary"):
        mandates.catalog()
    with bind_persona("The Adversary"):
        mandates.catalog()
    assert set(mandates._catalog.keys()) == {"the_visionary", "the_adversary"}
    mandates._catalog = {}  # don't leak into other tests


# ── Async schema writes must key to the persona bound at CALL time ────────────
# Regression for the cross-persona write leak: awrite/aappend_fact/upsert_section/
# migrate_placeholder ran _sb_write on an executor thread, and worker threads don't
# inherit the bind_persona contextvar — so the write re-resolved the persona to
# BRAIN_PERSONA_NAME (the process home persona). In prod, the Trading-bear session
# (bound the_adversary) sleep-consolidated on the shared brain (home the_visionary)
# and wrote the Adversary's living self.md onto the_visionary's row. The fix
# resolves the persona key in-task and passes it into _sb_write.


class _FakeSupabase:
    """Minimal brain_schemas double: serves reads from {persona_key: content} and
    records every upserted row."""

    def __init__(self, docs: dict[str, str]):
        self.docs = docs
        self.upserts: list[dict] = []

    def table(self, _name):
        return _FakeTable(self)


class _FakeTable:
    def __init__(self, db: _FakeSupabase):
        self.db = db
        self.filters: dict[str, str] = {}
        self._row: dict | None = None

    def select(self, *_a):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def maybe_single(self):
        return self

    def upsert(self, row, **_kw):
        self._row = row
        return self

    def execute(self):
        import types

        if self._row is not None:
            self.db.upserts.append(self._row)
            return types.SimpleNamespace(data=self._row)
        content = self.db.docs.get(self.filters.get("persona", ""), "")
        return types.SimpleNamespace(data={"content": content} if content else None)


def _supabase_schema_store(monkeypatch, docs: dict[str, str]):
    from brain.second_brain.store import SchemaStore

    fake = _FakeSupabase(docs)
    store = SchemaStore()
    store._use_supabase = True
    monkeypatch.setattr(store, "_sb", lambda: (fake, "org-test"))
    return store, fake


def test_awrite_keys_to_bound_persona_not_home(monkeypatch):
    import asyncio

    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Visionary")
    store, fake = _supabase_schema_store(monkeypatch, {})

    async def run():
        with bind_persona("The Adversary"):
            await store.awrite("self.md", "# Self-Model — The Adversary")

    asyncio.run(run())
    assert [r["persona"] for r in fake.upserts] == ["the_adversary"]


def test_upsert_section_reads_and_writes_the_same_bound_persona(monkeypatch):
    # The metacognition mood-stamp path: read-modify-write of the BOUND persona's
    # doc, written back to the BOUND persona's row (not merged onto the home row).
    import asyncio

    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Visionary")
    docs = {
        "the_adversary": ("# Self-Model — The Adversary\n\n## Current mood signature\nbaseline\n"),
        "the_visionary": "# Self-Model — The Visionary\n",
    }
    store, fake = _supabase_schema_store(monkeypatch, docs)

    async def run():
        with bind_persona("The Adversary"):
            await store.upsert_section("self.md", "Current mood signature", "DA=0.71")

    asyncio.run(run())
    assert len(fake.upserts) == 1
    row = fake.upserts[0]
    assert row["persona"] == "the_adversary"
    assert "DA=0.71" in row["content"]
    assert "The Adversary" in row["content"]


def test_awrite_unbound_still_falls_back_to_home_persona(monkeypatch):
    import asyncio

    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Visionary")
    store, fake = _supabase_schema_store(monkeypatch, {})

    async def run():
        await store.awrite("self.md", "doc")

    asyncio.run(run())
    assert [r["persona"] for r in fake.upserts] == ["the_visionary"]


def test_aappend_fact_keys_to_bound_persona(monkeypatch):
    import asyncio

    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Visionary")
    docs = {"the_adversary": "# notes"}
    store, fake = _supabase_schema_store(monkeypatch, docs)

    async def run():
        with bind_persona("The Adversary"):
            await store.aappend_fact("self.md", "prefers counterexamples")

    asyncio.run(run())
    assert [r["persona"] for r in fake.upserts] == ["the_adversary"]
    assert "prefers counterexamples" in fake.upserts[0]["content"]
