"""
Custom personas — runtime-authored persona specs (the owner-API persona path).

Built-in personas are code tables (persona_chem.PERSONA_CHEMISTRY + the self.md
archetypes in run.py). A CUSTOM persona — e.g. a book character the Story engine
casts as an agent — is authored through PUT /v1/personas/{slug} and persisted as:

  personas/<slug>/persona.json   — the spec: display name, disposition text,
                                   and the resolved resting chemistry. Lives in
                                   the ORG-CANONICAL persona dir (resolved via
                                   persona_key.persona_state_root at call time),
                                   so the org's shared instance, any dedicated
                                   sibling instance, and the provisioner all see
                                   the same file.
  personas/<slug>/chemistry.json — the standard persona_chem state file, seeded
                                   from the spec baseline so the in-process view
                                   (per-turn binding, MRI) matches the spec.
  self.md                        — the identity document the brain actually
                                   performs, composed from the base scaffold
                                   (shared drives + non-negotiable principles)
                                   with the authored character sections. Hosted:
                                   the Supabase brain_schemas row; local: the
                                   persona's schema/self.md file.

How the chemistry reaches a DEDICATED persona instance: the provisioner reads the
spec at spawn (read_spec_under) and stamps chem_baseline_*/chem_init_* into the
instance's settings.json — the same channel the UI persona switch uses — so the
bus boots on the character's baseline, not the bundled default's.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from brain import ids as _ids
from brain.persona_key import persona_slug, persona_state_root

logger = logging.getLogger("brain.personas")


class PersonaError(ValueError):
    """Invalid persona input → HTTP 400 at the API layer."""


class PersonaNotFound(PersonaError):
    """The named persona / template does not exist → HTTP 404."""


class PersonaConflict(PersonaError):
    """The slug is taken by a different persona, or a cap is reached → HTTP 409."""


# What a clone starts with (organizations.instance_seed; a clone body may override).
CLONE_SEEDS = ("current", "default")

# Learned-state files copied by the `current` seed — competence, not memories:
# wiring weights, motor chunks, sequence (angle) weights, the ignition tally and
# the angle-synonym clusters. NEVER copied: episodes, user.md / speaker files, the
# open-questions ledger, chemistry pairs, dmn_*.json, learning ledger and stories,
# jobs — those are per-person memory or a life already lived (brain/api/api_guide.md
# §20 "Learning mode").
_CURRENT_SEED_FILES = (
    "wiring.json",
    "chunks.json",
    "sequence_weights.json",
    "ignition_tally.json",
    "angle_synonyms.json",
)
_CURRENT_SEED_SECTIONS = ("History summary", "Stable preferences")


# Dot-free (agent_id = "<persona>.<mandate>" splits on the first dot) and already
# canonical: the slug IS the storage key everywhere, so accept only what
# persona_slug() would emit — no display names, no dashes, no surprises.
PERSONA_SLUG_RE = _ids.SLUG_RE

# The neutral default canvas for a spec with no (or a partial) baseline: the
# flat-affect control persona, mid everything, no strong leans — the authored
# channels then move it in character.
_DEFAULT_BASELINE_KEY = "The Stoic"

_TEXT_FIELDS = ("disposition", "personality", "speaking")
_MAX_TEXT_CHARS = 20_000

# Short UI metadata carried on the spec (the persona rail's subtitle + blurb).
_META_FIELDS = ("tag", "note")
_MAX_META_CHARS = 2_000

# `vals` — the saved knob setup the settings UI restores when a persona is
# selected: settings-key -> scalar (chem boot levels, cognitive keys, toggles).
# The brain itself never reads it; it is the UI half of the unified store.
_MAX_VALS_KEYS = 512
_MAX_VALS_KEY_CHARS = 128
_MAX_VALS_STR_CHARS = 4_096


def _now() -> str:
    return datetime.now(UTC).isoformat()


def valid_slug(slug: str) -> str:
    s = str(slug or "").strip()
    if not PERSONA_SLUG_RE.match(s) or persona_slug(s) != s:
        raise PersonaError(
            "persona slug must be 1-64 chars of lowercase letters, digits or '_', "
            "starting with a letter or digit (e.g. 'captain_ahab')"
        )
    return s


def is_builtin(slug_or_name: str) -> bool:
    """True for the engine's built-in roster (the PERSONA_CHEMISTRY table)."""
    from brain import persona_chem

    return persona_chem.display_name_for(slug_or_name) is not None


def personas_dir() -> Path:
    """The org-canonical personas/ directory for THIS process.

    SECOND_BRAIN_PATH is persona-scoped in multi-tenant boots (…/personas/<home>),
    so resolve the sibling level — the same rule persona_state_root applies —
    rather than nesting a personas/ tree inside the home persona's dir."""
    root = Path(
        os.environ.get("SECOND_BRAIN_PATH")
        or str(Path(__file__).resolve().parent.parent / "second_brain")
    )
    return root.parent if root.parent.name == "personas" else root / "personas"


def _spec_path(slug: str) -> Path:
    """personas/<slug>/persona.json for EVERY persona, the home one included.

    Deliberately NOT persona_state_root(): that maps the home persona to the
    volume root (its learned-state routing), which would scatter the org's
    catalogue — the home persona's spec would sit at <root>/persona.json where
    the personas/ scan (list_all, read_spec_under, the provisioner) never looks.
    Specs are org-catalogue data, so they all live at the same level, exactly
    like chemistry.json (persona_chem._path)."""
    return personas_dir() / slug / "persona.json"


def read_spec_under(state_root: Path, slug: str) -> dict | None:
    """Read a persona spec from an EXPLICIT org state root (tenants/<org>/second_brain).

    For callers outside the tenant process — the provisioner materializing a
    dedicated instance's boot chemistry — where this process's SECOND_BRAIN_PATH
    points somewhere else entirely."""
    path = Path(state_root) / "personas" / slug / "persona.json"
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning("[personas] unreadable spec %s: %s", path, e)
    return None


def read_spec(slug: str) -> dict | None:
    """The persona's spec as stored, or None (unknown / built-in / unreadable)."""
    path = _spec_path(slug)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning("[personas] unreadable spec %s: %s", path, e)
    return None


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _default_display_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("_") if w)


def _index(op: str, *args) -> None:
    """Dark-write the persona index (brain/persona_index.py, migration 039). The
    spec file is the source of truth; an index failure must never fail the
    authoring call, so this swallows everything and the module logs once."""
    try:
        from brain import persona_index

        getattr(persona_index, op)(*args)
    except Exception as e:  # pragma: no cover - the module already never raises
        logger.debug("[personas] index %s skipped: %s", op, e)


def _canonical_baseline(slug: str) -> dict[str, float] | None:
    """A built-in slug's canonical resting chemistry (sanitized), else None."""
    from brain import persona_chem

    name = persona_chem.display_name_for(slug)
    if name is None:
        return None
    return persona_chem._floor_resting(dict(persona_chem.PERSONA_CHEMISTRY[name]))  # noqa: SLF001 — shared sanitizer


def _resolve_baseline(existing: dict | None, patch: dict | None, slug: str) -> dict[str, float]:
    """Full resting profile: existing spec baseline (or, for a built-in slug, its
    canonical chemistry; for a new custom, the neutral default) overlaid with the
    caller's channels, then sanitized into the structural envelope the whole
    model is designed around — every channel capped at RESTING_CEILING (the same
    0.8 the UI's chemistry sliders enforce; resting is a setpoint, and live
    dynamics need headroom above it), GABA raised to the inhibition floor."""
    from brain import persona_chem

    base = dict(
        (existing or {}).get("baseline")
        or _canonical_baseline(slug)
        or persona_chem.PERSONA_CHEMISTRY[_DEFAULT_BASELINE_KEY]
    )
    for ch, v in (patch or {}).items():
        if ch not in persona_chem.CHANNELS:
            raise PersonaError(
                f"unknown chemistry channel {ch!r} — valid: {', '.join(persona_chem.CHANNELS)}"
            )
        try:
            base[ch] = max(0.0, float(v))
        except (TypeError, ValueError) as e:
            raise PersonaError(
                f"baseline.{ch} must be a number in [0, {persona_chem.RESTING_CEILING}]"
            ) from e
    resting = {ch: float(base.get(ch, 0.0)) for ch in persona_chem.CHANNELS}
    return persona_chem._floor_resting(resting)  # noqa: SLF001 — shared sanitizer


def _clean_text(field: str, value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise PersonaError(f"{field} must be a string")
    text = value.strip()
    if len(text) > limit:
        raise PersonaError(f"{field} exceeds {limit} chars")
    return text


def _clean_vals(value: object) -> dict:
    """Validate a saved knob setup: flat dict of settings-key -> scalar."""
    if not isinstance(value, dict):
        raise PersonaError("vals must be an object of settings-key -> value")
    if len(value) > _MAX_VALS_KEYS:
        raise PersonaError(f"vals exceeds {_MAX_VALS_KEYS} keys")
    out: dict = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k or len(k) > _MAX_VALS_KEY_CHARS:
            raise PersonaError("vals keys must be non-empty strings")
        if isinstance(v, bool | int | float):
            out[k] = v
        elif isinstance(v, str):
            if len(v) > _MAX_VALS_STR_CHARS:
                raise PersonaError(f"vals[{k!r}] exceeds {_MAX_VALS_STR_CHARS} chars")
            out[k] = v
        elif v is None:
            continue
        else:
            raise PersonaError(f"vals[{k!r}] must be a number, string or boolean")
    return out


_BASE_SELF_MD = Path(__file__).resolve().parent.parent / "second_brain" / "schema" / "self.md"


def compose_self_md(spec: dict) -> str:
    """The persona's self.md: the base identity scaffold (shared core drives +
    non-negotiable guiding principles, which authored content never replaces)
    with Who I am / Personality / Speaking style swapped for the character's own
    sections, history reset, and the mood signature stamped from the baseline."""
    name = spec.get("display_name") or _default_display_name(spec.get("slug", ""))
    disposition = str(spec.get("disposition") or "").strip()
    personality = str(spec.get("personality") or "").strip() or disposition
    speaking = str(spec.get("speaking") or "").strip()
    baseline = spec.get("baseline") or {}

    try:
        text = _BASE_SELF_MD.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("[personas] base self.md unreadable (%s) — minimal doc", e)
        text = (
            "# Self-Model\n\n## Who I am\n\n## Personality\n\n## Speaking style\n\n"
            "## History summary\n\n## Current mood signature\n\n"
        )

    def _swap(section: str, body: str, doc: str) -> str:
        if not body:
            return doc
        return re.sub(
            rf"(## {re.escape(section)}\n).*?(?=\n## |\Z)",
            lambda m: m.group(1) + "\n" + body + "\n",
            doc,
            count=1,
            flags=re.S,
        )

    text = text.replace("# Self-Model", f"# Self-Model — {name}", 1)
    text = _swap("Who I am", disposition, text)
    text = _swap("Personality", personality, text)
    text = _swap("Speaking style", speaking, text)
    text = re.sub(r"(## History summary\n).*?(?=\n## |\Z)", r"\1", text, count=1, flags=re.S)
    mood = (
        f"DA={float(baseline.get('DA', 0.0)):.2f} "
        f"GABA={float(baseline.get('GABA', 0.0)):.2f} "
        f"ACh={float(baseline.get('ACh', 0.0)):.2f} dominant=baseline ({name})"
    )
    return re.sub(
        r"(## Current mood signature\n).*?(?=\n## |\Z)", r"\1" + mood, text, count=1, flags=re.S
    )


def _write_self_md(slug: str, spec: dict) -> None:
    text = compose_self_md(spec)
    if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase":
        from brain.second_brain.store import SchemaStore

        SchemaStore(persona=slug).write("self.md", text)
        return
    target = persona_state_root(slug) / "schema" / "self.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def upsert(slug: str, body: dict, *, _index_row: bool = True) -> dict:
    """Create or update a persona spec (idempotent PUT semantics: provided
    fields replace stored ones, omitted fields keep their stored value).

    A CUSTOM slug gets a full spec. A BUILT-IN slug gets an OVERRIDE spec — a
    saved knob/temperament setup layered over the canonical persona: baseline,
    tag/note and vals are allowed; identity (display_name + the text fields that
    compose self.md) stays canonical and is refused. DELETE restores defaults by
    removing the override.

    `_index_row=False` skips the persona-index mirror (clone() writes the final
    spec, with template/seed, itself)."""
    slug = valid_slug(slug)
    builtin = is_builtin(slug)
    body = body or {}
    if builtin:
        refused = [f for f in ("display_name", *_TEXT_FIELDS) if f in body]
        if refused:
            raise PersonaError(
                f"{slug!r} is a built-in persona — {', '.join(refused)} cannot be overridden "
                "(only baseline, tag, note and vals; DELETE the override to restore defaults)"
            )
    existing = read_spec(slug)
    spec: dict = existing or {"slug": slug, "version": 0, "created": _now()}
    spec["slug"] = slug

    if "display_name" in body:
        name = _clean_text("display_name", body["display_name"])
        if not name:
            raise PersonaError("display_name must be non-empty")
        spec["display_name"] = name
    if builtin:
        from brain import persona_chem

        spec["display_name"] = persona_chem.display_name_for(slug)
    spec.setdefault("display_name", _default_display_name(slug))
    for field in _TEXT_FIELDS:
        if field in body:
            spec[field] = _clean_text(field, body[field])
    for field in _META_FIELDS:
        if field in body:
            spec[field] = _clean_text(field, body[field], _MAX_META_CHARS)
    if "vals" in body:
        spec["vals"] = _clean_vals(body["vals"])

    patch = body.get("baseline")
    if patch is not None and not isinstance(patch, dict):
        raise PersonaError("baseline must be an object of channel -> value")
    spec["baseline"] = _resolve_baseline(existing, patch, slug)
    spec["version"] = int(spec.get("version", 0)) + 1
    spec["updated"] = _now()
    _atomic_write(_spec_path(slug), spec)

    # Keep the in-process chemistry view in step with the authored temperament.
    # The resting setpoint IS the spec; the evolved current state is a life —
    # reset it only on first creation (or never let a re-author snap the mood back).
    from brain import persona_chem

    fresh = not persona_chem.exists(slug)
    persona_chem.save_resting(slug, spec["baseline"])
    if fresh:
        persona_chem.save_current(slug, spec["baseline"], {})

    # Identity document: (re)compose only when the PUT carried identity text —
    # a chemistry-only update must not clobber a persona's grown self.md.
    if any(f in body for f in _TEXT_FIELDS) or "display_name" in body:
        try:
            _write_self_md(slug, spec)
        except Exception as e:
            logger.warning("[personas] self.md write failed for %s: %s", slug, e)
    if _index_row:
        _index("upsert_from_spec", spec)
    return dict(spec)


def get(slug: str) -> dict | None:
    """One persona: the stored spec (custom), the canonical profile overlaid
    with its override spec (built-in with a saved override), or the canonical
    profile alone (built-in)."""
    slug = str(slug or "").strip()
    spec = read_spec(slug)
    builtin = is_builtin(slug)
    if spec is not None:
        return {**spec, "builtin": builtin, "overridden": builtin}
    from brain import persona_chem

    name = persona_chem.display_name_for(slug)
    if name is None:
        return None
    return {
        "slug": persona_slug(name),
        "display_name": name,
        "builtin": True,
        "overridden": False,
        "baseline": _canonical_baseline(persona_slug(name)),
    }


def _read_all_specs() -> dict[str, dict]:
    """Every stored spec on disk, keyed by slug (customs + built-in overrides)."""
    specs: dict[str, dict] = {}
    root = personas_dir()
    with contextlib.suppress(OSError):
        for entry in sorted(root.iterdir()):
            spec_file = entry / "persona.json"
            if not spec_file.exists():
                continue
            try:
                spec = json.loads(spec_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[personas] unreadable spec %s: %s", spec_file, e)
                continue
            if isinstance(spec, dict):
                specs[str(spec.get("slug", entry.name))] = spec
    return specs


def list_all() -> list[dict]:
    """Every persona this org can run: built-ins (slug + display name, flagged
    when a saved override spec exists) first, then custom specs in slug order."""
    from brain import persona_chem

    specs = _read_all_specs()
    out: list[dict] = [
        {
            "slug": persona_slug(name),
            "display_name": name,
            "builtin": True,
            "overridden": persona_slug(name) in specs,
        }
        for name in persona_chem.PERSONA_CHEMISTRY
    ]
    builtin_slugs = {e["slug"] for e in out}
    customs: list[dict] = []
    for slug, spec in specs.items():
        if slug in builtin_slugs:
            continue
        entry = {
            "slug": slug,
            "display_name": spec.get("display_name", _default_display_name(slug)),
            "builtin": False,
            "version": spec.get("version"),
            "updated": spec.get("updated"),
        }
        if spec.get("template"):
            entry["template"] = spec["template"]
            entry["seed"] = spec.get("seed")
        customs.append(entry)
    return out + customs


def page(
    rows: list[dict],
    *,
    include_clones: bool = False,
    template: str | None = None,
    limit: int = 200,
    offset: int = 0,
    max_limit: int = 1000,
) -> dict:
    """The listing contract for GET /v1/personas: clones (specs carrying a
    `template`) are hidden by default — a marketplace org has thousands and the
    roster a partner resolves agents against is the templates; `?template=` lists
    one template's clones; `?include_clones=true` lists everything. Paged with
    `limit` (default 200, cap 1000) and `offset`; `total` counts the filtered set
    and `next_offset` is None on the last page."""
    if template:
        t = persona_slug(template)
        rows = [r for r in rows if persona_slug(r.get("template") or "") == t]
    elif not include_clones:
        rows = [r for r in rows if not r.get("template")]
    try:
        limit = int(limit)
    except (TypeError, ValueError) as e:
        raise PersonaError("limit must be an integer") from e
    try:
        offset = int(offset)
    except (TypeError, ValueError) as e:
        raise PersonaError("offset must be an integer") from e
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)
    total = len(rows)
    chunk = rows[offset : offset + limit]
    nxt = offset + limit if offset + limit < total else None
    return {"personas": chunk, "total": total, "limit": limit, "offset": offset, "next_offset": nxt}


def list_for_ui() -> list[dict]:
    """The unified catalogue for the settings UI, one full entry per persona:
    identity + UI metadata + resolved baseline + the saved knob setup. Built-ins
    without an override carry their canonical baseline and no vals."""
    from brain import persona_chem

    specs = _read_all_specs()
    out: list[dict] = []
    for name in persona_chem.PERSONA_CHEMISTRY:
        slug = persona_slug(name)
        spec = specs.get(slug) or {}
        out.append(
            {
                "slug": slug,
                "display_name": name,
                "builtin": True,
                "overridden": slug in specs,
                "tag": spec.get("tag", ""),
                "note": spec.get("note", ""),
                "baseline": spec.get("baseline") or _canonical_baseline(slug),
                "vals": spec.get("vals") or {},
            }
        )
    builtin_slugs = {e["slug"] for e in out}
    for slug, spec in specs.items():
        if slug in builtin_slugs:
            continue
        out.append(
            {
                "slug": slug,
                "display_name": spec.get("display_name", _default_display_name(slug)),
                "builtin": False,
                "overridden": False,
                "tag": spec.get("tag", ""),
                "note": spec.get("note", ""),
                "baseline": spec.get("baseline") or {},
                "vals": spec.get("vals") or {},
                "disposition": spec.get("disposition", ""),
                "updated": spec.get("updated"),
            }
        )
    return out


def capacity_limits() -> dict:
    """The caps that govern personas: concurrent persona PROCESSES (see
    brain/provisioner.py — per-org dedicated instances, total live brains on the
    host) and the number of custom persona SPECS an org may hold (max_personas,
    BRAIN_MAX_PERSONAS, default 10000; the clone route refuses with 409 at the cap).
    0 = uncapped."""
    from brain import persona_placement as _pp

    return {
        # The org row's cap (organizations.max_dedicated_instances, migration 038)
        # when set, else the deployment's BRAIN_MAX_DEDICATED.
        "max_dedicated_instances": _pp.effective_max_dedicated(),
        "max_live_brains": int(os.environ.get("BRAIN_MAX_TENANTS", "25") or 0),
        "max_personas": int(os.environ.get("BRAIN_MAX_PERSONAS", "10000") or 0),
    }


def custom_count() -> int:
    """How many custom persona specs (built-in overrides excluded) the org holds.
    A head count on the persona index when it answers (one query, O(1) in the
    catalogue — this is what lets max_personas sit at 10000 without the clone
    cap check parsing every spec on the volume); the spec scan otherwise."""
    try:
        from brain import persona_index

        n = persona_index.count_custom()
    except Exception as e:  # pragma: no cover - the module already never raises
        logger.debug("[personas] index count skipped: %s", e)
        n = None
    if n is not None:
        return int(n)
    return custom_count_on_disk()


def custom_count_on_disk() -> int:
    """The spec-scan count, always from the volume — what the index's boot
    reconcile compares itself against (it must never read its own answer)."""
    return sum(1 for slug in _read_all_specs() if not is_builtin(slug))


# ── clone ───────────────────────────────────────────────────────────────────────


def _section(content: str, name: str) -> str:
    """Body of `## <name>` in a markdown doc ("" when absent)."""
    m = re.search(
        r"^##[ \t]+" + re.escape(name) + r"[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)",
        content or "",
        re.MULTILINE | re.DOTALL,
    )
    return (m.group(1) if m else "").strip()


def _read_self_md(slug: str) -> str:
    if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase":
        from brain.second_brain.store import SchemaStore

        return SchemaStore(persona=slug).read("self.md") or ""
    path = persona_state_root(slug) / "schema" / "self.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    from brain.second_brain.store import SchemaStore

    try:
        return SchemaStore(persona=slug).read("self.md") or ""
    except Exception:
        return ""


def _write_self_md_text(slug: str, text: str) -> None:
    if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase":
        from brain.second_brain.store import SchemaStore

        SchemaStore(persona=slug).write("self.md", text)
        return
    target = persona_state_root(slug) / "schema" / "self.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def _copy_learned_files(template: str, slug: str) -> list[str]:
    """Copy the `current`-seed learned files that exist on the template. Returns
    the names copied. Home template: its wiring lives at BRAIN_WIRING_PATH
    (= its state root / wiring.json), so the same root rule applies."""
    import shutil

    src_root = persona_state_root(template)
    dst_root = persona_state_root(slug)
    copied: list[str] = []
    for name in _CURRENT_SEED_FILES:
        src = src_root / name
        if not src.is_file():
            continue
        dst_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst_root / name)
        copied.append(name)
    return copied


def _copy_wiring_rows(template: str, slug: str) -> int | None:
    """Supabase backend: copy the template's wiring_edges rows under the clone's
    slug. None when not on Supabase (the file copy covers it)."""
    if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() != "supabase":
        return None
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return None
    sb = supabase_client.get_client()
    org = supabase_client.get_org_id()
    res = (
        sb.table("wiring_edges")
        .select("source,target,weight,polarity")
        .eq("org_id", org)
        .eq("persona", template)
        .execute()
    )
    rows = [
        {
            "org_id": org,
            "persona": slug,
            "source": r["source"],
            "target": r["target"],
            "weight": r["weight"],
            "polarity": r["polarity"],
            "updated_at": "now()",
        }
        for r in (res.data or [])
    ]
    if rows:
        sb.table("wiring_edges").upsert(rows, on_conflict="org_id,persona,source,target").execute()
    return len(rows)


async def clone(
    template: str,
    body: dict | None,
    *,
    org_seed: str = "default",
    deid=None,
    copy_agents_fn=None,
) -> dict:
    """Clone a custom template persona into a new persona (POST /v1/personas/{t}/clone).

    Body: `slug` | `suffix` (slug = `<template>_<suffix>`; the canonical slugifier
    collapses runs of `_`, so a double underscore cannot survive as a store key),
    `display_name`, `copy_agents` (default true), `seed` (per-clone override of the
    org instance_seed), `tag`, `note`. Custom templates only (built-in → 400;
    unknown → 404). Idempotent on the same slug + template (`created: false`);
    the slug held by any other persona → 409; the max_personas cap → 409.

    `default` seed: spec + freshly composed self.md + baseline wiring (nothing
    copied). `current` seed: the template's learned competence — wiring, chunks,
    sequence weights, ignition tally, angle synonyms — plus its self.md History
    summary and Stable preferences passed through `deid` (an async
    (text, source) → scrubbed | None; None = not carried, fail closed; no `deid`
    at all = not carried). Chemistry always starts at the resting baseline. Never
    copied: episodes, user.md / speaker files, open threads, chemistry pairs, DMN
    state, ledgers and stories, jobs.

    Spec-first: the spec, chemistry and self.md are written before the agent copy,
    and agent/skill errors land in `errors[]` rather than failing the clone."""
    body = dict(body or {})
    template = valid_slug(template)
    if is_builtin(template):
        raise PersonaError(
            f"{template!r} is a built-in persona — only custom personas can be templates"
        )
    tspec = read_spec(template)
    if tspec is None:
        raise PersonaNotFound(f"unknown template persona {template!r}")

    if body.get("slug"):
        slug = valid_slug(body["slug"])
    elif body.get("suffix"):
        slug = valid_slug(f"{template}_{str(body['suffix']).strip()}")
    else:
        raise PersonaError("slug or suffix is required")
    if slug == template:
        raise PersonaError("a clone needs its own slug")
    if is_builtin(slug):
        raise PersonaConflict(f"{slug!r} is a built-in persona")

    seed = str(body.get("seed") or org_seed or "default")
    if seed not in CLONE_SEEDS:
        raise PersonaError(f"seed must be one of {list(CLONE_SEEDS)}")
    copy_agents = body.get("copy_agents", True)
    if not isinstance(copy_agents, bool):
        raise PersonaError("copy_agents must be a boolean")

    existing = read_spec(slug)
    if existing is not None:
        if str(existing.get("template") or "") == template:
            return {
                "created": False,
                "persona": {**existing, "builtin": False},
                "template": template,
                "seed": existing.get("seed"),
                "copied": {},
                "errors": [],
            }
        raise PersonaConflict(f"slug {slug!r} already belongs to a different persona")

    cap = capacity_limits()["max_personas"]
    if cap and custom_count() >= cap:
        raise PersonaConflict(
            f"max_personas reached ({cap}) — purge unused personas or raise BRAIN_MAX_PERSONAS"
        )

    spec_body: dict = {
        "display_name": body.get("display_name")
        or tspec.get("display_name")
        or _default_display_name(slug),
        "baseline": dict(tspec.get("baseline") or {}),
    }
    for f in _TEXT_FIELDS:
        if tspec.get(f):
            spec_body[f] = tspec[f]
    for f in _META_FIELDS:
        if body.get(f) is not None:
            spec_body[f] = body[f]
        elif tspec.get(f):
            spec_body[f] = tspec[f]
    if tspec.get("vals"):
        spec_body["vals"] = tspec["vals"]
    # upsert(): spec + resting chemistry (fresh → current == resting) + a freshly
    # composed self.md, since identity text is in the body.
    spec = upsert(slug, spec_body, _index_row=False)
    spec["template"] = template
    spec["seed"] = seed
    spec["cloned"] = _now()
    _atomic_write(_spec_path(slug), spec)
    _index("upsert_from_spec", spec)

    copied: dict = {}
    errors: list[str] = []
    if seed == "current":
        try:
            copied["files"] = _copy_learned_files(template, slug)
            if copied["files"]:
                # The clone starts with the template's competence: learned state
                # from day one, as far as the index (and the switch report) go.
                _index("set_learned_state", slug)
        except Exception as e:
            errors.append(f"learned files: {e}")
        try:
            n = _copy_wiring_rows(template, slug)
            if n is not None:
                copied["wiring_edges"] = n
        except Exception as e:
            errors.append(f"wiring_edges: {e}")
        carried: list[str] = []
        try:
            source = _read_self_md(template)
            if source and deid is not None:
                target = _read_self_md(slug) or compose_self_md(spec)
                for section in _CURRENT_SEED_SECTIONS:
                    text = _section(source, section)
                    if not text:
                        continue
                    scrubbed = await deid(text, source)
                    if not scrubbed:
                        errors.append(f"self.md {section}: not carried (de-id rejected)")
                        continue
                    from brain.second_brain.store import SchemaStore

                    target = SchemaStore._replace_section_body(target, section, scrubbed)
                    carried.append(section)
                if carried:
                    _write_self_md_text(slug, target)
            elif source and deid is None:
                errors.append("self.md sections: not carried (de-id gate unavailable)")
        except Exception as e:
            errors.append(f"self.md sections: {e}")
        copied["self_md_sections"] = carried

    if copy_agents:
        fn = copy_agents_fn
        if fn is None:
            try:
                from brain import agents as _agents

                fn = _agents.copy_agents
            except Exception as e:  # pragma: no cover - import shape
                errors.append(f"agents: {e}")
        if fn is not None:
            try:
                copied["agents"] = fn(template, slug)
            except Exception as e:
                errors.append(f"agents: {e}")

    return {
        "created": True,
        "persona": {**spec, "builtin": False},
        "template": template,
        "seed": seed,
        "copied": copied,
        "errors": errors,
    }


def delete(slug: str) -> bool:
    """Remove a custom persona's spec + chemistry + identity document. Learned
    state (episodes, wiring) is left in place — it is keyed by the slug and
    simply goes dormant. For a BUILT-IN slug this is 'restore defaults': the
    override spec is removed and the resting chemistry reset to canonical, but
    the persona itself, its evolved current mood and its grown self.md all stay.
    Returns False when there was no spec to remove."""
    slug = valid_slug(slug)
    if is_builtin(slug):
        from brain import persona_chem

        spec_path = _spec_path(slug)
        existed = spec_path.exists()
        with contextlib.suppress(OSError):
            spec_path.unlink()
        if existed:
            with contextlib.suppress(Exception):
                persona_chem.save_resting(slug, _canonical_baseline(slug) or {})
            _index("upsert_builtin", slug)  # override gone → builtin_override False
        return existed
    spec_path = _spec_path(slug)
    existed = spec_path.exists()
    with contextlib.suppress(OSError):
        spec_path.unlink()
    if existed:
        _index("mark_deleted", slug)
    from brain import persona_chem

    with contextlib.suppress(OSError):
        persona_chem._path(slug).unlink()  # noqa: SLF001 — same-package state file
    if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase":
        try:
            from brain.second_brain import supabase_client

            if supabase_client.is_enabled():
                sb = supabase_client.get_client()
                org = supabase_client.get_org_id()
                sb.table("brain_schemas").delete().eq("org_id", org).eq("persona", slug).execute()
        except Exception as e:
            logger.warning("[personas] brain_schemas cleanup failed for %s: %s", slug, e)
    else:
        with contextlib.suppress(OSError):
            (persona_state_root(slug) / "schema" / "self.md").unlink()
    return existed


# ── Legacy persona_store migration ──────────────────────────────────────────────
# Before 2026-08 the settings UI kept its own persona catalogue in the
# `persona_store` settings key: one JSON blob keyed by display name, holding each
# persona's tag/note, chemistry and saved knob setup (`vals`). That blob and the
# per-persona spec files above were two disjoint stores; the spec files are now
# canonical for BOTH surfaces. This fold runs once per org (marker:
# `persona_store_migrated`), turning each blob entry into a spec file — an
# override spec for built-ins, a full spec for customs. Existing spec files win
# (the API authored them; the blob copy is staler by construction). The blob
# itself is left in place untouched as a rollback artifact; nothing writes it
# any more.

_migration_checked = False


def migrate_persona_store() -> int:
    """Fold the legacy persona_store settings blob into per-persona spec files.
    Idempotent (settings marker + in-process flag); returns how many personas
    were migrated this call."""
    global _migration_checked
    if _migration_checked:
        return 0
    _migration_checked = True
    try:
        from brain.settings import settings
    except Exception as e:  # pragma: no cover — settings is always importable in-process
        logger.warning("[personas] migration skipped, settings unavailable: %s", e)
        return 0
    if str(settings.get("persona_store_migrated", "") or "").strip():
        return 0
    raw = str(settings.get("persona_store", "") or "").strip()
    entries: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                entries = parsed
        except Exception as e:
            logger.warning("[personas] persona_store unparseable — migrating nothing: %s", e)
    from brain import persona_chem

    migrated = 0
    for name, e in entries.items():
        if not isinstance(e, dict):
            continue
        try:
            slug = persona_slug(str(name))
            if not PERSONA_SLUG_RE.match(slug) or read_spec(slug) is not None:
                continue  # unusable name, or a spec already exists — the spec wins
            vals = e.get("vals") if isinstance(e.get("vals"), dict) else {}
            chem = e.get("chem") if isinstance(e.get("chem"), dict) else {}
            if not chem:  # built-in overrides carried chemistry only inside vals
                chem = {
                    ch: vals[f"chem_baseline_{ch}"]
                    for ch in persona_chem.CHANNELS
                    if f"chem_baseline_{ch}" in vals
                }
            body: dict = {"vals": vals}
            if chem:
                body["baseline"] = chem
            for f in _META_FIELDS:
                if isinstance(e.get(f), str) and e[f].strip():
                    body[f] = e[f]
            if not is_builtin(slug):
                body["display_name"] = str(name)
            upsert(slug, body)
            # The blob's self.md copy: only fill a HOLE — the schema store is the
            # live document (sleep consolidation rewrites it) and must not be
            # clobbered by a stale UI snapshot.
            self_md = e.get("selfMd")
            if isinstance(self_md, str) and self_md.strip():
                try:
                    from brain.persona_models import read_self_model

                    if not str(read_self_model(str(name)) or "").strip():
                        from brain.second_brain.store import SchemaStore

                        SchemaStore(persona=slug).write("self.md", self_md)
                except Exception as se:
                    logger.warning("[personas] self.md migration failed for %s: %s", name, se)
            migrated += 1
        except Exception as ex:
            logger.warning("[personas] persona_store entry %r not migrated: %s", name, ex)
    settings.save({"persona_store_migrated": _now()})
    if migrated:
        logger.info(
            "[personas] migrated %d persona_store entr%s into spec files",
            migrated,
            "y" if migrated == 1 else "ies",
        )
    return migrated
