"""Seed the projects ledger (open_questions.md) in the Supabase brain_schemas table.

Why this exists: nothing ever created open_questions.md on hosted tenants. The DMN
reads its `## Projects assigned by Russ` section into `_last_projects` and injects it
as a PRE-AUTHORIZED PROJECTS block, but with no row the block was never emitted and
the project scheduler never fired — while the monologue prompt still promised "you
will receive a list of active projects". SchemaStore.ensure_open_questions_schema()
now creates an EMPTY skeleton at boot; this script adds real work for the personas
that have a mandate worth working on.

Scope: only personas whose effective tier is `full`. A lite-tier brain never builds
a DMN at all (brain/session_setup.py — "lite-tier brain runs no idle thinking loop"),
so a ledger for one would be dead weight in every turn's context.

Every entry here is READ-ONLY investigation inside the persona's own mandate, and
every Status is finite ("Not started"), NOT standing. `_project_eligible` only
excludes done/blocked, so a status like "Ongoing" stays eligible forever and the
scheduler re-runs it indefinitely — real recurring cloud spend. Make that a
deliberate choice, not a default.

Idempotent upsert on (org_id, persona, end_user_id, filename). Run with:
    .venv/bin/python scripts/seed_open_questions.py --dry-run   # print, no write
    .venv/bin/python scripts/seed_open_questions.py             # writes
    .venv/bin/python scripts/seed_open_questions.py --force     # overwrite existing
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent

HEADER = "# Open Questions & Projects\n\n"

RESOURCE_POLICY = (
    "## Resource policy (what I'm allowed to use)\n"
    "- Reading and summarising my own files, memory, and this org's own data:\n"
    "  always allowed.\n"
    "- Anything that spends money, writes outside my workspace, or reaches an\n"
    "  external system: propose it and wait for a yes.\n\n"
)

# `## Projects assigned by Russ` is a parser contract (brain/dmn.py::_parse_projects
# and add_manual_project both hardcode it). It reads oddly in a partner tenant, but
# renaming it here would silently orphan every project entry.
PROJECTS_HEADER = "## Projects assigned by Russ\n"


def _doc(projects: str) -> str:
    return HEADER + RESOURCE_POLICY + PROJECTS_HEADER + projects


# ── The Admin (app_admin) — internal operator; observes, summarises, queries; ──
# never acts in third-party systems. Everything below stays inside the house.
ADMIN = _doc(
    """
### Agent health sweep
- **Task**: Read this org's agent job and usage records and check which agents ran
  recently, which have gone quiet, and whether any job is stuck or repeating. Report
  what looks off, with the numbers.
- **Status**: Not started.

### Spend pattern check
- **Task**: Read the org's recent usage and cost records and check whether any agent
  is trending toward its daily cap or has changed spend pattern. Report the figures
  and name the agent; do not change any limit.
- **Status**: Not started.

### Platform self-knowledge
- **Task**: Read the app's own docs and settings surfaces so questions about what a
  feature or setting does can be answered exactly rather than guessed. Note where the
  docs are thin or contradict the settings.
- **Status**: Not started.
"""
)

# ── The Analyst (day_trading_analyst) — advisor and decision-journal coach. ────
# Informs decisions, never makes or executes them; these tasks are analysis of the
# user's own record only, and say so explicitly.
ANALYST = _doc(
    """
### Decision-journal review
- **Task**: Read the recent decision-journal entries and check which theses have
  resolved and what the outcomes were. Look for setups that keep working and mistakes
  that repeat, and write up what the record actually shows.
- **Status**: Not started.

### Watchlist level drift
- **Task**: Read the watchlist and check which entry, exit, and stop levels have gone
  stale against recent price action. Flag the ones worth revisiting for the user to
  decide on. Do not place, size, or execute anything.
- **Status**: Not started.
"""
)

# (org_id, persona, mandate_id) → document. The ledger is agent-scoped, so the mandate
# picks the filename (brain/open_threads.ledger_file). Only full-tier agents appear: a
# lite agent never builds a DMN, and every other persona/mandate gets the empty skeleton
# from ensure_open_questions_schema() at boot.
#
# Note what is NOT here: the_analyst's lite `trading_mispricing` agent. Under agent
# scoping it gets its own empty ledger instead of reading the day-trading projects out
# of a shared file on every debate round.
SEEDS: dict[tuple[str, str, str], str] = {
    # russ.oreagan@gmail.com (personal)
    ("5d5b9e0b-0821-4dea-b493-6408bf3db463", "the_admin", "app_admin"): ADMIN,
    ("5d5b9e0b-0821-4dea-b493-6408bf3db463", "the_analyst", "day_trading_analyst"): ANALYST,
    # elyceum.ai@gmail.com (personal)
    ("ae3ca444-fe24-412f-9000-237967588823", "the_admin", "app_admin"): ADMIN,
    # salon-test@elyceum.ai (personal)
    ("7b87724f-e789-466a-b9a2-25bc8033ae25", "the_admin", "app_admin"): ADMIN,
}


def seed(url: str, service_key: str, force: bool = False) -> tuple[int, int]:
    """Upsert each seeded ledger. Returns (written, skipped).

    Without --force an existing row is left alone: the DMN writes its own progress
    into these files (status rewrites, the `## Open threads` section), so a blind
    overwrite would discard real state.
    """
    base = url.rstrip("/")
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    from brain.open_threads import ledger_file

    written = skipped = 0
    for (org_id, persona, mandate), doc in SEEDS.items():
        filename = ledger_file(mandate)
        if not force:
            r = httpx.get(
                f"{base}/rest/v1/brain_schemas",
                headers=headers,
                params={
                    "select": "filename",
                    "org_id": f"eq.{org_id}",
                    "persona": f"eq.{persona}",
                    "end_user_id": "eq.",
                    "filename": f"eq.{filename}",
                },
                timeout=30.0,
            )
            r.raise_for_status()
            if r.json():
                print(f"  · skip {persona}.{mandate} @ {org_id[:8]} — ledger already exists")
                skipped += 1
                continue
        resp = httpx.post(
            f"{base}/rest/v1/brain_schemas",
            headers={**headers, "Prefer": "resolution=merge-duplicates"},
            params={"on_conflict": "org_id,persona,end_user_id,filename"},
            json=[
                {
                    "org_id": org_id,
                    "persona": persona,
                    "end_user_id": "",
                    "filename": filename,
                    "content": doc,
                }
            ],
            timeout=30.0,
        )
        resp.raise_for_status()
        print(f"  ✓ wrote {filename} for {persona}.{mandate} @ {org_id[:8]} ({len(doc)} chars)")
        written += 1
    return written, skipped


def main() -> None:
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    if dry:
        from brain.open_threads import ledger_file

        for (org_id, persona, mandate), doc in SEEDS.items():
            print(f"=== {ledger_file(mandate)} — {persona}.{mandate} @ {org_id} ===")
            print(doc)
        return

    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    url, key = os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    written, skipped = seed(url, key, force=force)
    print(f"done — {written} written, {skipped} skipped.")


if __name__ == "__main__":
    main()
