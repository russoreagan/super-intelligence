"""One-time, idempotent: move markdown projects into the agent_projects table.

Reads every brain_schemas row named open_questions*.md, parses its
`## Projects assigned by Russ` section with the same parser the brain uses
(DefaultModeNetwork._parse_projects), and writes each project to agent_projects
under (persona, mandate) — the mandate taken from the filename
(`open_questions__<mandate>.md`; the base file means no mandate).

Mirrors agent_projects_store.upsert_content: a project that is MISSING is inserted
whole (initial state from the markdown status words); one that EXISTS gets only its
content fields (title/task/priority/max_runs) refreshed — never its lifecycle — so
re-running cannot resurrect work the scheduler has finished.

--cleanup deletes the mandate-suffixed brain_schemas rows afterwards (their resource
policy equals the skeleton, so nothing is lost; leaving them keeps stale projects
reachable through the schema grep() recall path). The base open_questions.md is
never deleted.

Run with the service key (bypasses RLS — this is an operator script):
    .venv/bin/python scripts/migrate_projects_to_table.py --dry-run
    .venv/bin/python scripts/migrate_projects_to_table.py
    .venv/bin/python scripts/migrate_projects_to_table.py --cleanup
    .venv/bin/python scripts/migrate_projects_to_table.py --org <org_id>   # one org
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from brain import agent_projects_store as store  # noqa: E402
from brain.dmn import DefaultModeNetwork  # noqa: E402

_FILE_RE = re.compile(r"^open_questions(?:__(?P<mandate>[A-Za-z0-9_-]+))?\.md$")


def _headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _records_for(row: dict) -> list[dict]:
    m = _FILE_RE.match(str(row.get("filename") or ""))
    if not m:
        return []
    mandate = m.group("mandate") or ""
    persona = str(row.get("persona") or "")
    out = []
    for p in DefaultModeNetwork._parse_projects(str(row.get("content") or "")):
        title, task = p["name"].strip(), p["task"].strip()
        if not title or not task:
            continue
        status = (p.get("status") or "").lower()
        if any(w in status for w in DefaultModeNetwork._PROJECT_DONE_WORDS):
            state = store.DONE
        elif any(w in status for w in DefaultModeNetwork._PROJECT_BLOCKED_WORDS):
            state = store.BLOCKED
        else:
            state = store.READY
        out.append(
            store.new_record(
                persona,
                mandate,
                title,
                task,
                priority=1 if p.get("priority") == "PRIMARY" else 2,
                source="markdown_import",
                state=state,
                status_note=(p.get("status") or "")[:200],
            )
        )
    return out


def migrate(url: str, key: str, *, org: str = "", dry: bool = False, cleanup: bool = False) -> None:
    base = url.rstrip("/")
    h = _headers(key)
    params = {
        "select": "org_id,persona,end_user_id,filename,content",
        "filename": "like.open_questions*",
        "end_user_id": "eq.",
    }
    if org:
        params["org_id"] = f"eq.{org}"
    r = httpx.get(f"{base}/rest/v1/brain_schemas", headers=h, params=params, timeout=60.0)
    r.raise_for_status()
    ledgers = r.json() or []
    print(f"{len(ledgers)} ledger file(s) found")

    inserted = updated = 0
    for row in ledgers:
        recs = _records_for(row)
        org_id = row["org_id"]
        label = f"{row['persona']}/{row['filename']} @ {org_id[:8]}"
        if not recs:
            print(f"  · {label}: no projects")
            continue
        ids = ",".join(x["id"] for x in recs)
        r = httpx.get(
            f"{base}/rest/v1/agent_projects",
            headers=h,
            params={"select": "id", "org_id": f"eq.{org_id}", "id": f"in.({ids})"},
            timeout=30.0,
        )
        if r.status_code == 404:
            # Migration 034 not applied yet. A dry run can still show the plan; a real
            # run must stop here rather than fail row by row.
            if not dry:
                sys.exit("agent_projects table not found — apply supabase/migrations/034 first")
            print("  ! agent_projects table not found (034 not applied) — treating all as missing")
            existing: set[str] = set()
        else:
            r.raise_for_status()
            existing = {x["id"] for x in (r.json() or [])}
        for rec in recs:
            if rec["id"] in existing:
                patch = {k: rec[k] for k in ("title", "task", "priority", "max_runs")}
                print(f"  ~ {label}: refresh content  {rec['title']!r}")
                if not dry:
                    httpx.patch(
                        f"{base}/rest/v1/agent_projects",
                        headers={**h, "Prefer": "return=minimal"},
                        params={"org_id": f"eq.{org_id}", "id": f"eq.{rec['id']}"},
                        json=patch,
                        timeout=30.0,
                    ).raise_for_status()
                updated += 1
            else:
                print(f"  + {label}: insert {rec['state']:8} {rec['title']!r}")
                if not dry:
                    httpx.post(
                        f"{base}/rest/v1/agent_projects",
                        headers={**h, "Prefer": "return=minimal"},
                        json=[{**store._to_db(rec), "org_id": org_id}],
                        timeout=30.0,
                    ).raise_for_status()
                inserted += 1

    print(
        f"{'would insert' if dry else 'inserted'} {inserted}, {'would refresh' if dry else 'refreshed'} {updated}"
    )

    if cleanup:
        removed = 0
        for row in ledgers:
            m = _FILE_RE.match(str(row.get("filename") or ""))
            if not m or not m.group("mandate"):
                continue  # the base file is never deleted
            print(f"  - delete {row['persona']}/{row['filename']} @ {row['org_id'][:8]}")
            if not dry:
                httpx.delete(
                    f"{base}/rest/v1/brain_schemas",
                    headers=h,
                    params={
                        "org_id": f"eq.{row['org_id']}",
                        "persona": f"eq.{row['persona']}",
                        "end_user_id": "eq.",
                        "filename": f"eq.{row['filename']}",
                    },
                    timeout=30.0,
                ).raise_for_status()
            removed += 1
        print(f"{'would delete' if dry else 'deleted'} {removed} mandate-suffixed ledger file(s)")


def main() -> None:
    dry = "--dry-run" in sys.argv
    cleanup = "--cleanup" in sys.argv
    org = ""
    if "--org" in sys.argv:
        org = sys.argv[sys.argv.index("--org") + 1]

    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    migrate(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
        org=org,
        dry=dry,
        cleanup=cleanup,
    )
    print("done.")


if __name__ == "__main__":
    main()
