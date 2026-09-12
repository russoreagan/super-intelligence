"""Isolation audit — a cheap, deterministic snapshot of one persona's learned state.

The partner-facing proof that nothing crosses personas (GET /v1/personas/{p}/isolation):
snapshot B, talk to A, snapshot B again, compare. Everything here is a read: head
counts + last-modified per Supabase store, sha256 + mtime of the identity documents
and chemistry file, line counts of the ledgers, whether the persona sits in this
process's DMN roster, the org learning mode and the persona's owner.

`fingerprint` is a sha256 over the CANONICAL stores only (content hashes and
counts, never mtimes or timestamps), so it is byte-stable while a persona is left
alone and changes the moment any learned store does. tests/test_persona_isolation_
invariants.py asserts exactly that across turns on a sibling persona.

Also the home of `has_learned_state(slug)`, which the learning-mode switch uses to
list the personas that would become templates (→ isolated) or to refuse the switch
back (→ consolidated, 409 unless force).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# (org_id, persona)-keyed Supabase stores. The purge (session_turn._PERSONA_PURGE_TABLES)
# and this audit must agree on what "the persona's rows" are.
COUNTED_TABLES = (
    "episodes",
    "wiring_edges",
    "wiring_snapshots",
    "dmn_state",
    "tasks",
    "agent_turns",
    "agent_projects",
)

# Files under persona_state_root(slug) that carry learned state (the audit hashes
# them; the `current` clone seed copies the LEARNED subset — see personas.clone).
STATE_FILES = (
    "wiring.json",
    "chunks.json",
    "sequence_weights.json",
    "ignition_tally.json",
    "angle_synonyms.json",
    "dmn_novelty.json",
    "dmn_routing_weights.json",
    "hypotheses.json",
)
LEDGER_FILES = ("learning_ledger.jsonl", "learning_stories.jsonl")


def _sha(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _file_entry(path: Path) -> dict | None:
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
        return {"sha256": _sha(data), "bytes": len(data), "mtime": path.stat().st_mtime}
    except OSError:
        return None


def _line_count(path: Path) -> int:
    try:
        if not path.is_file():
            return 0
        with path.open("rb") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def _use_supabase() -> bool:
    return os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase"


def _sb():
    from brain.second_brain import supabase_client

    try:
        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception:
        return None


def _count(client, org: str, table: str, slug: str) -> int | str:
    """head=True exact count of (org, persona) rows; "error: …" on failure."""
    try:
        res = (
            client.table(table)
            .select("*", count="exact", head=True)
            .eq("org_id", org)
            .eq("persona", slug)
            .execute()
        )
        n = getattr(res, "count", None)
        if n is None:
            n = len(getattr(res, "data", None) or [])
        return int(n)
    except Exception as e:
        return f"error: {e}"


def _read_schema(slug: str, filename: str) -> str:
    """One identity document for the persona. Supabase: the brain_schemas row.
    Local: the persona's own schema/ dir (SchemaStore's local dir is the home
    persona's; personas._write_self_md routes non-home customs by state root)."""
    from brain.persona_key import persona_state_root

    if _use_supabase():
        try:
            from brain.second_brain.store import SchemaStore

            return SchemaStore(persona=slug).read(filename) or ""
        except Exception as e:
            logger.debug("[persona_audit] schema read failed %s/%s: %s", slug, filename, e)
            return ""
    path = persona_state_root(slug) / "schema" / filename
    if path.is_file():
        with contextlib.suppress(OSError):
            return path.read_text(encoding="utf-8")
    try:
        from brain.second_brain.store import SchemaStore

        return SchemaStore(persona=slug).read(filename) or ""
    except Exception:
        return ""


def _speaker_files(slug: str) -> dict[str, str]:
    """{filename: content} for the persona's user model files (user.md + user_*.md)."""
    from brain.persona_key import persona_state_root

    out: dict[str, str] = {}
    if _use_supabase():
        try:
            from brain.second_brain.store import SchemaStore

            for name, content in (SchemaStore(persona=slug).read_all() or {}).items():
                if name == "user.md" or (name.startswith("user_") and name.endswith(".md")):
                    out[name] = content or ""
        except Exception as e:
            logger.debug("[persona_audit] speaker scan failed for %s: %s", slug, e)
        return out
    schema_dir = persona_state_root(slug) / "schema"
    with contextlib.suppress(OSError):
        for p in sorted(schema_dir.glob("user*.md")):
            with contextlib.suppress(OSError):
                out[p.name] = p.read_text(encoding="utf-8")
    return out


def in_dmn_roster(slug: str) -> bool:
    """Would this process's DMN rotate into the persona? Home always; in an isolated
    org nobody else; otherwise any full-tier persona not promoted to its own
    instance (mirrors dmn._roster without needing the DMN object)."""
    from brain import org_settings

    if org_settings.is_home(slug):
        return True
    if org_settings.is_isolated():
        return False
    try:
        from brain.placement_client import promoted_personas

        if slug in {str(p) for p in promoted_personas()}:
            return False
    except Exception:
        pass
    if _sb() is not None:
        try:
            from brain import agents

            return agents.effective_tier(slug) == "full"
        except Exception:
            return True
    return True


def snapshot(slug: str) -> dict:
    """The audit snapshot. Never raises; failing stores are reported inline."""
    from brain import org_settings, persona_chem, persona_owners
    from brain.open_threads import active_ledger_file
    from brain.persona_key import persona_slug, persona_state_root

    slug = persona_slug(slug)
    root = persona_state_root(slug)
    files: dict[str, dict | None] = {}
    for name in STATE_FILES:
        files[name] = _file_entry(root / name)
    chem_path = persona_chem._path(slug)  # noqa: SLF001 - same-package state file
    files["chemistry.json"] = _file_entry(chem_path)

    documents: dict[str, dict] = {}
    self_md = _read_schema(slug, "self.md")
    documents["self.md"] = {"sha256": _sha(self_md), "bytes": len(self_md)}
    oq = _read_schema(slug, active_ledger_file())
    documents[active_ledger_file()] = {"sha256": _sha(oq), "bytes": len(oq)}
    speakers = _speaker_files(slug)
    documents["user_model"] = {
        "files": len(speakers),
        "sha256": _sha(json.dumps({k: _sha(v) for k, v in sorted(speakers.items())})),
    }

    ledgers = {name: _line_count(root / name) for name in LEDGER_FILES}
    client_chem_dir = root / "client_chem"
    pairs = 0
    with contextlib.suppress(OSError):
        pairs = len(list(client_chem_dir.glob("*.json"))) if client_chem_dir.is_dir() else 0

    counts: dict[str, int | str] = {}
    sb = _sb()
    if sb is not None:
        client, org = sb
        for table in COUNTED_TABLES:
            counts[table] = _count(client, org, table, slug)

    canonical = {
        "files": {k: (v or {}).get("sha256") for k, v in sorted(files.items())},
        "documents": {k: v.get("sha256") for k, v in sorted(documents.items())},
        "ledgers": ledgers,
        "counts": {k: v for k, v in sorted(counts.items()) if isinstance(v, int)},
        "client_chem_pairs": pairs,
    }
    return {
        "persona": slug,
        "learning_mode": org_settings.learning_mode(),
        "owner_end_user_id": persona_owners.owner_of(slug),
        "in_dmn_roster": in_dmn_roster(slug),
        "is_home": org_settings.is_home(slug),
        "state_root": str(root),
        "files": files,
        "documents": documents,
        "ledgers": ledgers,
        "client_chem_pairs": pairs,
        "counts": counts,
        "fingerprint": _sha(json.dumps(canonical, sort_keys=True)),
    }


def has_learned_state(slug: str) -> bool:
    """Cheap: does this persona hold anything a turn or a sleep pass wrote? Learned
    files or ledgers on disk, episodes/wiring rows in Supabase, or a self.md that
    differs from the freshly composed identity."""
    from brain.persona_key import persona_slug, persona_state_root

    slug = persona_slug(slug)
    root = persona_state_root(slug)
    for name in (*STATE_FILES, *LEDGER_FILES):
        if name == "hypotheses.json":
            continue
        with contextlib.suppress(OSError):
            if (root / name).is_file() and (root / name).stat().st_size > 2:
                return True
    sb = _sb()
    if sb is not None:
        client, org = sb
        for table in ("episodes", "wiring_edges"):
            n = _count(client, org, table, slug)
            if isinstance(n, int) and n > 0:
                return True
    try:
        from brain import personas

        spec = personas.read_spec(slug)
        if spec is not None:
            current = _read_schema(slug, "self.md")
            if current and _sha(current) != _sha(personas.compose_self_md(spec)):
                return True
    except Exception:
        pass
    return False
