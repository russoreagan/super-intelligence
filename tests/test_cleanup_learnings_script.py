"""scripts/cleanup_learnings_2026_10.py against an in-memory PostgREST double.

Never touches a network: the Rest client is swapped for _FakeRest, which answers
the same eq./like. filters the script sends.
"""

from __future__ import annotations

import fnmatch
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).parent.parent / "scripts" / "cleanup_learnings_2026_10.py"
_spec = importlib.util.spec_from_file_location("cleanup_learnings_2026_10", _PATH)
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)

ORG = cl.RUSS_ORG
OLD = cl.OLD_IDS[0]
CUR = cl.CURRENT_ID


def _match(row: dict, params: dict) -> bool:
    for col, cond in params.items():
        if col == "select":
            continue
        op, _, val = cond.partition(".")
        have = str(row.get(col) if row.get(col) is not None else "")
        if op == "eq" and have != val:
            return False
        if op == "like" and not fnmatch.fnmatchcase(have, val):
            return False
    return True


class _FakeRest:
    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.t = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.writes: list[tuple] = []

    def select(self, table, params):
        return [dict(r) for r in self.t.get(table, []) if _match(r, params)]

    def delete(self, table, params):
        self.writes.append(("delete", table, params))
        self.t[table] = [r for r in self.t.get(table, []) if not _match(r, params)]

    def update(self, table, params, patch):
        self.writes.append(("update", table, params, patch))
        for r in self.t.get(table, []):
            if _match(r, params):
                r.update(patch)


PROFILE_CUR = (
    f"# User: {CUR}\n\n## Known facts\n- Trades small caps\n\n## Preferences\n- (learning…)\n\n"
    "## Relationship\n- Familiarity: regular\n\n## Affection score\n- Score: 4\n"
)
PROFILE_OLD = (
    f"# User: {OLD}\n\n## Known facts\n- Trades small caps\n- Prefers morning reviews\n\n"
    "## Preferences\n- Wants terse answers\n\n## Relationship\n- Familiarity: new\n\n"
    "## Affection score\n- Score: 1\n"
)


def _world() -> _FakeRest:
    sf = cl.speaker_filename
    return _FakeRest(
        {
            "personas": [
                {"org_id": ORG, "persona": "the_analyst", "deleted_at": None},
                {"org_id": ORG, "persona": "gone", "deleted_at": "2026-09-01"},
            ],
            "episodes": [
                {"org_id": ORG, "persona": "the_realist", "end_user_id": OLD},
                {"org_id": ORG, "persona": "the_analyst", "end_user_id": OLD},
                {"org_id": ORG, "persona": "the_analyst", "end_user_id": CUR},
                {"org_id": ORG, "persona": "the_sage", "end_user_id": "probe-verify-2:trading"},
                {"org_id": "other-org", "persona": "the_realist", "end_user_id": OLD},
            ],
            "wiring_edges": [{"org_id": ORG, "persona": "the_realist"}],
            "agent_turns": [{"org_id": ORG, "persona": "the_analyst", "end_user_id": OLD}],
            "api_sessions": [
                {"org_id": ORG, "agent_id": "the_realist.debate", "end_user_id": "x"},
                {"org_id": ORG, "agent_id": "the_analyst.day", "end_user_id": OLD},
            ],
            "end_users": [
                {"org_id": ORG, "end_user_id": OLD},
                {"org_id": ORG, "end_user_id": CUR},
                {"org_id": ORG, "end_user_id": "probe-verify-2:trading"},
            ],
            "brain_schemas": [
                {
                    "id": 1,
                    "org_id": ORG,
                    "persona": "the_analyst",
                    "filename": sf(CUR),
                    "content": PROFILE_CUR,
                    "updated_at": "2026-10-19",
                },
                {
                    "id": 2,
                    "org_id": ORG,
                    "persona": "the_analyst",
                    "filename": sf(OLD),
                    "content": PROFILE_OLD,
                    "updated_at": "2026-09-05",
                },
                {
                    "id": 3,
                    "org_id": ORG,
                    "persona": "the_companion",
                    "filename": cl.PRE_DIGEST_FILE,
                    "content": PROFILE_OLD,
                    "updated_at": "2026-06-27",
                },
                {
                    "id": 4,
                    "org_id": ORG,
                    "persona": "the_realist",
                    "filename": "self.md",
                    "content": "x",
                },
                {
                    "id": 5,
                    "org_id": ORG,
                    "persona": "the_sage",
                    "filename": sf("probe-verify-2:trading"),
                    "content": "p",
                },
            ],
        }
    )


def test_speaker_filename_matches_the_store():
    from brain.second_brain.store import SchemaStore

    for name in (CUR, OLD, "status-check-probe"):
        assert cl.speaker_filename(name) == SchemaStore.speaker_filename(SchemaStore, name)
    assert cl.speaker_filename(CUR).endswith("_3443186d.md")
    assert cl.speaker_filename(OLD).endswith("_368c14c1.md")


def test_merge_profile_appends_new_bullets_only():
    out = cl.merge_profile(PROFILE_CUR, PROFILE_OLD)
    assert out.count("- Trades small caps") == 1
    assert "- Prefers morning reviews" in out and "- Wants terse answers" in out
    assert "(learning…)" not in out.split("## Preferences")[1].split("##")[0]
    assert "- Score: 4" in out and "- Score: 1" not in out  # counters stay current
    assert out.startswith(f"# User: {CUR}")


def test_dry_run_writes_nothing_but_backs_everything_up():
    rest = _world()
    c = cl.Cleanup(rest, ORG, apply=False, log=lambda *_: None)
    c.orphans(("the_realist",))
    c.legacy_users(("probe-verify-2:trading",))
    c.merge_identity()
    assert rest.writes == []
    assert c.counts["orphans:episodes"] == 1
    # Nothing was removed, so the orphan's row is still there to be re-keyed too.
    assert c.counts["merge_rekey:episodes"] == 2
    assert any(b["table"] == "brain_schemas" for b in c.backup)


def test_apply_cleans_orphans_legacy_users_and_merges_identity():
    rest = _world()
    c = cl.Cleanup(rest, ORG, apply=True, log=lambda *_: None)
    c.orphans(("the_realist", "the_analyst", "the_admin", "gone"))
    c.legacy_users(("probe-verify-2:trading",))
    c.merge_identity()
    t = rest.t
    # orphans: the_realist gone from this org only; live and built-in personas kept.
    assert not [r for r in t["episodes"] if r["org_id"] == ORG and r["persona"] == "the_realist"]
    assert [r for r in t["episodes"] if r["org_id"] == "other-org"]
    assert t["wiring_edges"] == []
    assert [r["agent_id"] for r in t["api_sessions"]] == ["the_analyst.day"]
    assert {r["id"] for r in t["brain_schemas"]} >= {1}
    assert 4 not in {r["id"] for r in t["brain_schemas"]}
    # legacy user: rows, profile and registry gone.
    assert all(r["end_user_id"] != "probe-verify-2:trading" for r in t["episodes"])
    assert 5 not in {r["id"] for r in t["brain_schemas"]}
    # identity: re-keyed, registry row dropped, profiles folded.
    assert all(r["end_user_id"] != OLD for r in t["episodes"] if r["org_id"] == ORG)
    assert t["agent_turns"][0]["end_user_id"] == CUR
    assert [r["end_user_id"] for r in t["end_users"]] == [CUR]
    by_id = {r["id"]: r for r in t["brain_schemas"]}
    assert 2 not in by_id and "- Prefers morning reviews" in by_id[1]["content"]
    # a persona with only the pre-digest file: that file becomes the current one.
    assert by_id[3]["filename"] == cl.speaker_filename(CUR)
    assert by_id[3]["content"].startswith(f"# User: {CUR}")


def test_followups_name_the_volume_calls():
    lines = cl.volume_followups()
    assert lines[0].startswith("POST /v1/org/hypotheses/sources/merge") and OLD in lines[0]
    assert len(lines) == 1 + len(cl.LEGACY_TEST_USERS)


@pytest.mark.parametrize("slug", cl.ORPHAN_PERSONAS)
def test_no_orphan_is_a_builtin(slug):
    from brain import persona_chem

    assert persona_chem.display_name_for(slug) is None
