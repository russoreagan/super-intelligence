"""scripts/reembed_episodes.py — the only path out of the split embedding space.

Production carries ~9,100 rows embedded with gemini-embedding-001, ~875 with
nomic-embed-text and ~1,262 with no vector at all. Migration 044 made searches
compare only same-model rows, which makes the split safe but not fixed; this
script is what closes it, and it had no tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reembed_episodes.py"
_spec = importlib.util.spec_from_file_location("reembed_episodes", _PATH)
re_mod = importlib.util.module_from_spec(_spec)
sys.modules["reembed_episodes"] = re_mod
_spec.loader.exec_module(re_mod)

MODEL = re_mod.OLLAMA_EMBED_MODEL


# ── model identity ───────────────────────────────────────────────────────────
# A dimension check is not identity: bge-base, all-mpnet and a stale gemini proxy
# are all 768-dim, and any of them would be written to every row LABELLED
# nomic-embed-text — recreating the corruption 044 exists to fix, but invisibly.


def test_same_model_ignores_the_latest_tag():
    assert re_mod._same_model("nomic-embed-text:latest", "nomic-embed-text")
    assert re_mod._same_model("nomic-embed-text", "nomic-embed-text:latest")
    assert not re_mod._same_model("bge-base:latest", "nomic-embed-text")


def test_verify_accepts_a_host_serving_the_model(monkeypatch):
    monkeypatch.setattr(re_mod, "_served_models", lambda h: [f"{MODEL}:latest", "llama3:8b"])
    assert MODEL in re_mod.verify_model("http://h")


def test_verify_refuses_a_different_model_of_the_same_width(monkeypatch):
    """The dangerous case: 768-dim, so the old probe passed."""
    monkeypatch.setattr(re_mod, "_served_models", lambda h: ["bge-base:latest"])
    with pytest.raises(SystemExit) as e:
        re_mod.verify_model("http://h")
    assert "does not serve" in str(e.value) and "bge-base" in str(e.value)


def test_verify_refuses_a_host_that_will_not_identify_itself(monkeypatch):
    monkeypatch.setattr(re_mod, "_served_models", lambda h: None)
    with pytest.raises(SystemExit) as e:
        re_mod.verify_model("http://h")
    assert "Refusing" in str(e.value)


def test_verify_can_be_overridden_deliberately(monkeypatch):
    monkeypatch.setattr(re_mod, "_served_models", lambda h: None)
    assert "UNVERIFIED" in re_mod.verify_model("http://h", allow_unverified=True)


# ── row selection ────────────────────────────────────────────────────────────


class _Q:
    def __init__(self, rows, sink):
        self.rows, self.sink = rows, sink

    def select(self, cols):
        self.sink["cols"] = cols
        return self

    def or_(self, expr):
        self.sink["or"] = expr
        return self

    def gt(self, col, val):
        self.sink["after"] = val
        self.rows = [r for r in self.rows if r["id"] > val]
        return self

    def order(self, col):
        return self

    def limit(self, n):
        self.rows = sorted(self.rows, key=lambda r: r["id"])[:n]
        return self

    def eq(self, col, val):
        self.rows = [r for r in self.rows if r.get(col) == val]
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


def _sb(rows, sink):
    return type("C", (), {"table": lambda self, n: _Q(list(rows), sink)})()


ROWS = [
    {"id": 1, "topic_tags": ["idle_thought", "reinforced"], "org_id": "o"},
    {"id": 2, "topic_tags": ["deferred_question", "normal"], "org_id": "o"},
    {"id": 3, "topic_tags": ["conclusion", "knowledge", "dmn"], "org_id": "o"},
    {"id": 4, "topic_tags": ["conclusion", "knowledge", "confirmed"], "org_id": "o"},
    {"id": 5, "topic_tags": ["conclusion", "knowledge", "sleep"], "org_id": "o"},
    {"id": 6, "topic_tags": ["trading"], "org_id": "o"},
]


def test_default_run_repairs_the_rows_a_person_would_miss():
    """Including user-confirmed conclusions and sleep insights, which the old
    user_input filter excluded — so they stayed stranded in the old space forever."""
    sink = {}
    got = re_mod._select(_sb(ROWS, sink), None, include_idle=False, after_id=0, limit=100)
    assert [r["id"] for r in got] == [4, 5, 6]


def test_include_idle_takes_everything():
    sink = {}
    got = re_mod._select(_sb(ROWS, sink), None, include_idle=True, after_id=0, limit=100)
    assert [r["id"] for r in got] == [1, 2, 3, 4, 5, 6]


def test_selection_targets_rows_outside_the_current_space():
    sink = {}
    re_mod._select(_sb(ROWS, sink), None, include_idle=True, after_id=0, limit=100)
    assert "embed_model.is.null" in sink["or"]
    assert f"embed_model.neq.{MODEL}" in sink["or"]
    # topic_tags must be selected, or the idle filter silently matches nothing.
    assert "topic_tags" in sink["cols"]


def test_org_scope_is_applied():
    sink = {}
    rows = ROWS + [{"id": 7, "topic_tags": [], "org_id": "other"}]
    got = re_mod._select(_sb(rows, sink), "o", include_idle=True, after_id=0, limit=100)
    assert all(r["org_id"] == "o" for r in got)


def test_paging_advances_past_rows_already_seen():
    sink = {}
    got = re_mod._select(_sb(ROWS, sink), None, include_idle=True, after_id=3, limit=100)
    assert [r["id"] for r in got] == [4, 5, 6]


def test_the_idle_predicate_is_the_one_retention_uses():
    """If these two ever disagree, rows are either pruned without being repaired or
    repaired forever without being pruned."""
    from brain.second_brain import store

    assert re_mod.is_dmn_idle_episode is store.is_dmn_idle_episode


# ── the RPC contract (migration 044) ─────────────────────────────────────────


def _migration(name: str) -> str:
    root = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
    return (root / name).read_text()


def _body_starts(sql: str) -> list[int]:
    return [sql.index(f"function {fn}(") for fn in ("match_episodes", "match_episodes_by_tag")]


def test_both_rpcs_filter_on_the_embedding_model():
    """Without this a search compares vectors from two different models, where
    cosine distance is noise."""
    sql = _migration("044_episode_embed_model.sql")
    bodies = [sql[i : sql.index("$$;", i)] for i in _body_starts(sql)]
    assert len(bodies) == 2
    for body in bodies:
        assert "embed_model_param is null or embed_model = embed_model_param" in body
        assert "embed_model_param text default null" in body


def test_the_params_python_sends_exist_in_the_sql():
    """Nothing tied store.py's param names to the migration — a rename on either
    side is a PostgREST 404 in production with a green suite."""
    from brain.second_brain import store

    src = Path(store.__file__).read_text()
    sql = _migration("044_episode_embed_model.sql")
    for name in ("embed_model_param", "end_user_param", "match_count", "persona_param"):
        assert f'"{name}"' in src, f"{name} not sent by store.py"
        assert name in sql, f"{name} not declared in the migration"


def test_recall_is_exact_not_approximate():
    """Checked against production 2026-09-20: the planner serves these queries from
    the (org_id, persona, end_user_id, ts) btree and an exact top-N sort — it does
    NOT use the ivfflat vector index, which Supabase's advisor confirms has never
    been used. So pgvector's post-filter behaviour (an ivfflat scan probes `probes`
    lists FIRST and applies the WHERE afterwards, which can return fewer rows than
    match_count) does not apply here, and no ivfflat.probes tuning is needed.

    This test pins the reason: every RPC filters org_id AND persona, which is what
    makes the btree the cheaper plan. If that ever stops being true, revisit the
    post-filter question before trusting recall completeness.
    """
    sql = _migration("044_episode_embed_model.sql")
    bodies = [sql[i : sql.index("$$;", i)] for i in _body_starts(sql)]
    for body in bodies:
        assert "org_id = org_id_param" in body
        assert "persona = persona_param" in body


# ── import has no side effects ───────────────────────────────────────────────
# The script loads the repo's .env so it runs as one command. It must only do that
# when RUN: an import-time load_dotenv() walks up from a git worktree to the real
# .env and injects production SUPABASE_* credentials into the importing process.
# Under pytest that silently moved every later test off the local file backend and
# onto Supabase — 23 unrelated failures, each passing in isolation. A spy rather
# than a real .env, so this discriminates in CI too, where no .env exists.


def test_importing_the_script_does_not_load_dotenv(monkeypatch):
    import dotenv

    calls = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: calls.append((a, k)) or True)
    spec = importlib.util.spec_from_file_location("reembed_episodes_import_probe", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert calls == [], "reembed_episodes must not call load_dotenv() on import"


def test_running_the_script_still_loads_dotenv(monkeypatch):
    # The other half of the contract: as a command it must still read .env, or
    # `reembed_episodes.py --dry-run` regresses to the failure 8511bd6 fixed.
    import dotenv

    calls = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: calls.append((a, k)) or True)
    spec = importlib.util.spec_from_file_location("__main__", _PATH)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "argv", ["reembed_episodes.py", "--help"])
    with pytest.raises(SystemExit):
        spec.loader.exec_module(mod)
    assert len(calls) == 2, "running the script must load the repo .env, then any above cwd"
