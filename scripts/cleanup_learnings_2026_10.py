"""One-off cleanup of misplaced learnings found by the 2026-10-19 prod audit.

Three things, in this order, all on Supabase rows (service key, bypasses RLS):

1. ORPHAN PERSONAS. Personas that no longer exist in the org's `personas` table and
   are not built-ins, but still hold episodes, wiring, snapshots, schema files and
   turns: the June/July forecasting-debate roster and two test personas. Run
   `DELETE /v1/personas/{slug}?purge=true` for each first (it also clears their
   files on the volume). This step removes whatever Supabase rows remain, and is the
   whole cleanup for a persona the API answers 404 for (no state root on disk).

2. LEGACY TEST USERS. Synthetic end users whose ids contain a ':', the format the
   API stopped accepting (brain/ids.py), so `DELETE /v1/end_users/{id}` answers 400
   and they cannot be erased through the API. Valid-format test users go through the
   API instead.

3. ONE PERSON, ONE IDENTITY. The owner reached the org as `<uuid>:trading` until
   2026-09-05 and as `<uuid>@trading` after, so personas held two (with the
   pre-digest file, three) speaker profiles for one person, and their history was
   split across two end-user ids. Rows are re-keyed to the current id; profiles are
   merged into the current one (new bullet lines from the old files are appended to
   the matching sections; relationship and score stay the current file's); the old
   registry row is removed.

Not reachable from here (it lives on the tenant volume): the hypothesis store's
source tokens and per-customer chemistry pairs. Once the lesson-review endpoints are
deployed, fold the old id's lesson contributions into the current one and drop the
test users' with POST /v1/org/hypotheses/sources/{merge,forget} (printed at the end).

Every row about to change is written to a local JSON backup BEFORE any write.
Dry run by default:
    .venv/bin/python scripts/cleanup_learnings_2026_10.py            # report only
    .venv/bin/python scripts/cleanup_learnings_2026_10.py --apply    # do it
Needs SUPABASE_URL and SUPABASE_SERVICE_KEY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

RUSS_ORG = "5d5b9e0b-0821-4dea-b493-6408bf3db463"
OWNER_UUID = "742c0dfd-d59a-426c-8d1e-d4473411954a"
CURRENT_ID = f"{OWNER_UUID}@trading"
OLD_IDS = (f"{OWNER_UUID}:trading",)
# The pre-digest speaker file (SchemaStore.speaker_filename before the hash suffix):
# the slug alone, truncated to 32 chars. Every id that maps to it is the owner.
PRE_DIGEST_FILE = "user_742c0dfd_d59a_426c_8d1e_d4473411.md"

ORPHAN_PERSONAS = (
    "the_contrarian",
    "the_empiricist",
    "the_falsifier",
    "the_humanist",
    "the_progressive",
    "the_realist",
    "the_synthesizer",
    "the_traditionalist",
    "test_character",
    "repro_test",
)
# Ids with a ':' (unerasable through the API). The owner's forecasting id only ever
# talked to the orphaned forecasting personas, so it goes with them.
LEGACY_TEST_USERS = (
    f"{OWNER_UUID}:story:director",
    f"{OWNER_UUID}:story:smoke_joel",
    f"{OWNER_UUID}:story:smoke_marta",
    f"{OWNER_UUID}:forecasting",
    "approval-verify-probe-9f3:trading",
    "probe-verify-2:trading",
    "forecasting:forecasting",
    "stage2-macro:forecasting",
)

_PERSONA_TABLES = (
    "episodes",
    "wiring_edges",
    "wiring_snapshots",
    "dmn_state",
    "brain_schemas",
    "tasks",
    "agent_turns",
    "agent_projects",
)
_PERSONA_AGENT_PREFIX_TABLES = ("api_sessions", "agent_jobs", "agents")
_END_USER_TABLES = ("episodes", "agent_turns", "api_sessions", "agent_jobs", "brain_schemas")
_REKEY_TABLES = ("episodes", "agent_turns", "api_sessions", "agent_jobs")
# Sections whose bullets carry over from an old profile; the rest are the current one's.
_MERGE_SECTIONS = (
    "Known facts",
    "Preferences",
    "Communication style",
    "Mood response patterns",
    "Emotional profile",
)
_PLACEHOLDER = "- (learning…)"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def speaker_filename(name: str) -> str:
    """Mirror of SchemaStore.speaker_filename (brain/second_brain/store.py)."""
    raw = name.strip()
    slug = _SLUG_RE.sub("_", raw.lower()).strip("_")[:32] or "unknown"
    digest = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"user_{slug}_{digest}.md"


def _sections(md: str) -> list[tuple[str, list[str]]]:
    """[(heading, lines)] in order; the text before the first '## ' is heading ''."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in md.splitlines():
        if line.startswith("## "):
            out.append((line[3:].strip(), []))
        else:
            out[-1][1].append(line)
    return out


def merge_profile(current: str, old: str) -> str:
    """The current profile with the old one's new bullet lines appended to the
    matching sections. Exact duplicates and placeholders are skipped; a section the
    current file lacks is added at the end. Relationship / affection stay the
    current file's (they are counters the sleep pass maintains)."""
    cur = _sections(current)
    old_bullets: dict[str, list[str]] = {}
    for head, lines in _sections(old):
        if head in _MERGE_SECTIONS:
            old_bullets[head] = [
                ln for ln in lines if ln.strip().startswith("- ") and ln.strip() != _PLACEHOLDER
            ]
    have = {h for h, _ in cur}
    for i, (head, lines) in enumerate(cur):
        add = [b for b in old_bullets.get(head, []) if b not in lines]
        if not add:
            continue
        kept = [ln for ln in lines if ln.strip() != _PLACEHOLDER]
        while kept and not kept[-1].strip():
            kept.pop()
        cur[i] = (head, [*kept, *add, ""])
    for head in _MERGE_SECTIONS:
        if head not in have and old_bullets.get(head):
            cur.append((head, [*old_bullets[head], ""]))
    parts: list[str] = []
    for head, lines in cur:
        if head:
            parts.append(f"## {head}")
        parts.extend(lines)
    return "\n".join(parts).rstrip("\n") + "\n"


class Rest:
    """Thin PostgREST client (service key). Tests swap in a fake with the same API."""

    def __init__(self, base: str, key: str) -> None:
        import httpx

        self._httpx = httpx
        self._base = base.rstrip("/") + "/rest/v1"
        self._h = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def select(self, table: str, params: dict) -> list[dict]:
        rows: list[dict] = []
        step = 1000
        while True:
            h = {**self._h, "Range-Unit": "items", "Range": f"{len(rows)}-{len(rows) + step - 1}"}
            r = self._httpx.get(
                f"{self._base}/{table}", headers=h, params={"select": "*", **params}, timeout=60
            )
            r.raise_for_status()
            page = r.json()
            rows.extend(page)
            if len(page) < step:
                return rows

    def delete(self, table: str, params: dict) -> None:
        r = self._httpx.delete(f"{self._base}/{table}", headers=self._h, params=params, timeout=60)
        r.raise_for_status()

    def update(self, table: str, params: dict, patch: dict) -> None:
        r = self._httpx.patch(
            f"{self._base}/{table}", headers=self._h, params=params, json=patch, timeout=60
        )
        r.raise_for_status()


def _eq(v: str) -> str:
    return f"eq.{v}"


class Cleanup:
    def __init__(self, rest, org: str, apply: bool, log=print) -> None:
        self.rest = rest
        self.org = org
        self.apply = apply
        self.log = log
        self.backup: list[dict] = []
        self.counts: dict[str, int] = {}

    def _save(self, step: str, table: str, rows: list[dict]) -> None:
        if rows:
            self.backup.append({"step": step, "table": table, "rows": rows})
            key = f"{step}:{table}"
            self.counts[key] = self.counts.get(key, 0) + len(rows)

    def _remove(self, step: str, table: str, params: dict) -> int:
        params = {"org_id": _eq(self.org), **params}
        rows = self.rest.select(table, params)
        self._save(step, table, rows)
        if rows and self.apply:
            self.rest.delete(table, params)
        return len(rows)

    # 1 ─────────────────────────────────────────────────────────────────────────
    def orphans(self, slugs=ORPHAN_PERSONAS) -> None:
        from brain import persona_chem

        live = {
            r["persona"]
            for r in self.rest.select("personas", {"org_id": _eq(self.org)})
            if not r.get("deleted_at")
        }
        for slug in slugs:
            if slug in live or persona_chem.display_name_for(slug) is not None:
                self.log(f"[orphans] SKIP {slug}: live or built-in")
                continue
            n = sum(self._remove("orphans", t, {"persona": _eq(slug)}) for t in _PERSONA_TABLES)
            n += sum(
                self._remove("orphans", t, {"agent_id": f"like.{slug}.*"})
                for t in _PERSONA_AGENT_PREFIX_TABLES
            )
            self.log(f"[orphans] {slug}: {n} rows")

    # 2 ─────────────────────────────────────────────────────────────────────────
    def legacy_users(self, ids=LEGACY_TEST_USERS) -> None:
        for eu in ids:
            n = sum(
                self._remove("legacy_users", t, {"end_user_id": _eq(eu)}) for t in _END_USER_TABLES
            )
            n += self._remove(
                "legacy_users", "brain_schemas", {"filename": _eq(speaker_filename(eu))}
            )
            n += self._remove("legacy_users", "end_users", {"end_user_id": _eq(eu)})
            self.log(f"[legacy_users] {eu}: {n} rows")

    # 3 ─────────────────────────────────────────────────────────────────────────
    def merge_identity(self, old_ids=OLD_IDS, current=CURRENT_ID) -> None:
        for old in old_ids:
            for t in _REKEY_TABLES:
                params = {"org_id": _eq(self.org), "end_user_id": _eq(old)}
                rows = self.rest.select(t, params)
                self._save("merge_rekey", t, rows)
                if rows and self.apply:
                    self.rest.update(t, params, {"end_user_id": current})
                self.log(f"[merge] {t}: {len(rows)} rows {old} -> {current}")
            self._remove("merge_registry", "end_users", {"end_user_id": _eq(old)})
        self._merge_profiles([speaker_filename(o) for o in old_ids] + [PRE_DIGEST_FILE], current)

    def _merge_profiles(self, old_files: list[str], current: str) -> None:
        target = speaker_filename(current)
        rows = self.rest.select(
            "brain_schemas",
            {"org_id": _eq(self.org), "filename": f"like.user_{OWNER_UUID[:8]}*"},
        )
        by_persona: dict[str, dict[str, dict]] = {}
        for r in rows:
            by_persona.setdefault(r["persona"], {})[r["filename"]] = r
        for persona, files in sorted(by_persona.items()):
            olds = [files[f] for f in old_files if f in files]
            if not olds:
                continue
            self._save("merge_profiles", "brain_schemas", list(files.values()))
            cur = files.get(target)
            if cur is None:
                # No current-id profile yet for this persona: the newest old one
                # becomes it, and the rest fold into it.
                olds.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
                cur, olds = olds[0], olds[1:]
                content = re.sub(
                    r"^# User: .*$", f"# User: {current}", cur["content"] or "", count=1, flags=re.M
                )
                if self.apply:
                    self.rest.update(
                        "brain_schemas",
                        {"id": _eq(str(cur["id"]))},
                        {"filename": target, "content": content},
                    )
                cur = {**cur, "filename": target, "content": content}
            merged = cur["content"] or ""
            for o in olds:
                merged = merge_profile(merged, o["content"] or "")
            if self.apply:
                if merged != cur["content"]:
                    self.rest.update(
                        "brain_schemas", {"id": _eq(str(cur["id"]))}, {"content": merged}
                    )
                for o in olds:
                    self.rest.delete("brain_schemas", {"id": _eq(str(o["id"]))})
            self.log(f"[merge] {persona}: {len(olds)} old profile(s) folded into {target}")


def volume_followups() -> list[str]:
    """The owner-key calls that finish the job on the tenant volume."""
    out = [
        "POST /v1/org/hypotheses/sources/merge "
        + json.dumps({"from": OLD_IDS[0], "to": CURRENT_ID})
    ]
    out += [
        "POST /v1/org/hypotheses/sources/forget " + json.dumps({"end_user_id": eu})
        for eu in LEGACY_TEST_USERS
    ]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Clean up misplaced learnings (2026-10 audit).")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--backup", default=f"cleanup_learnings_backup_{int(time.time())}.json")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    rest = Rest(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    c = Cleanup(rest, RUSS_ORG, apply=args.apply)
    c.orphans()
    c.legacy_users()
    c.merge_identity()
    Path(args.backup).write_text(json.dumps(c.backup, default=str, indent=1), encoding="utf-8")
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: backup of every touched row: {args.backup}")
    for k, v in sorted(c.counts.items()):
        print(f"  {k}: {v}")
    print("\nVolume side (after the lesson-review endpoints deploy), with the owner key:")
    for line in volume_followups():
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
