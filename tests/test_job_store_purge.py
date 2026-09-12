"""Local job records carry the customer's attribution and can be erased by it.

`jobs/*.json` holds the goal, tool outputs and results of work done for a partner's
customer, but until now carried no end_user_id — so DELETE /v1/end_users/{id}
could not reach it. JobStore.save now stamps end_user_id / partner_id /
origin_session_id from the bound turn context through the SAME helper the durable
agent_jobs mirror uses (brain.turn_ctx.current_field), and purge_end_user() removes
every file stamped with the customer and nothing else.
"""

from __future__ import annotations

import json

import brain.clusters.job_store as js_mod
from brain.clusters.job_store import JobStore
from brain.turn_ctx import bind_turn


def _save(store, job_id, **ctx):
    with bind_turn("agent", **ctx) if ctx else bind_turn("owner"):
        store.save(job_id, f"goal {job_id}", steps=[], results=["r"], success=True)


def test_save_stamps_attribution_from_turn_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    _save(store, "j1", session_id="s9", end_user_id="u_1", partner_id="acme")
    rec = json.loads((tmp_path / "j1.json").read_text())
    assert rec["end_user_id"] == "u_1"
    assert rec["partner_id"] == "acme"
    assert rec["origin_session_id"] == "s9"


def test_owner_lane_jobs_carry_empty_stamps(tmp_path, monkeypatch):
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    _save(store, "j0")
    rec = json.loads((tmp_path / "j0.json").read_text())
    assert rec["end_user_id"] == "" and rec["partner_id"] == "" and rec["origin_session_id"] == ""


def test_purge_removes_only_the_customers_files(tmp_path, monkeypatch):
    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    _save(store, "j1", session_id="s1", end_user_id="u_1", partner_id="acme")
    _save(store, "j2", session_id="s2", end_user_id="u_2", partner_id="acme")
    _save(store, "j3", session_id="s3", end_user_id="u_1", partner_id="acme")
    _save(store, "j4")  # legacy / owner: no stamp, never matched
    # Prime the caches so the purge has to invalidate them too.
    assert store.get("j1") is not None
    assert store.purge_end_user("u_1") == 2
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["j2.json", "j4.json"]
    assert store.get("j1") is None
    assert store.purge_end_user("u_1") == 0
    assert store.purge_end_user("") == 0


def test_stamps_match_the_durable_mirror(tmp_path, monkeypatch):
    """Both mirrors must resolve the same lane, or an erasure that hits one misses
    the other."""
    from brain import agent_jobs_store

    monkeypatch.setattr(js_mod, "JOBS_DIR", tmp_path)
    store = JobStore()
    with bind_turn("agent", session_id="s1", end_user_id="u_1", partner_id="acme"):
        store.save("j1", "g", steps=[], results=[], success=True)
        row = agent_jobs_store._row("org-1", store.get("j1"))
    assert (row["end_user_id"], row["partner_id"], row["origin_session_id"]) == (
        "u_1",
        "acme",
        "s1",
    )
