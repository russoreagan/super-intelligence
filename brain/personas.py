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

from brain.persona_key import persona_slug, persona_state_root

logger = logging.getLogger("brain.personas")


class PersonaError(ValueError):
    """Invalid persona input → HTTP 400 at the API layer."""


# Dot-free (agent_id = "<persona>.<mandate>" splits on the first dot) and already
# canonical: the slug IS the storage key everywhere, so accept only what
# persona_slug() would emit — no display names, no dashes, no surprises.
PERSONA_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")

# The neutral default canvas for a spec with no (or a partial) baseline: the
# flat-affect control persona, mid everything, no strong leans — the authored
# channels then move it in character.
_DEFAULT_BASELINE_KEY = "The Stoic"

_TEXT_FIELDS = ("disposition", "personality", "speaking")
_MAX_TEXT_CHARS = 20_000


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
    return persona_state_root(slug) / "persona.json"


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


def _resolve_baseline(existing: dict | None, patch: dict | None) -> dict[str, float]:
    """Full resting profile: existing spec baseline (or the neutral default)
    overlaid with the caller's channels, values clamped to [0,1], GABA floored so
    inhibition can never be authored out of reach (see persona_chem)."""
    from brain import persona_chem

    base = dict(
        (existing or {}).get("baseline")
        or persona_chem.PERSONA_CHEMISTRY[_DEFAULT_BASELINE_KEY]
    )
    for ch, v in (patch or {}).items():
        if ch not in persona_chem.CHANNELS:
            raise PersonaError(
                f"unknown chemistry channel {ch!r} — valid: {', '.join(persona_chem.CHANNELS)}"
            )
        try:
            base[ch] = min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError) as e:
            raise PersonaError(f"baseline.{ch} must be a number in [0,1]") from e
    resting = {ch: float(base.get(ch, 0.0)) for ch in persona_chem.CHANNELS}
    resting["GABA"] = max(persona_chem.GABA_RESTING_FLOOR, resting["GABA"])
    return resting


def _clean_text(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise PersonaError(f"{field} must be a string")
    text = value.strip()
    if len(text) > _MAX_TEXT_CHARS:
        raise PersonaError(f"{field} exceeds {_MAX_TEXT_CHARS} chars")
    return text


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


def upsert(slug: str, body: dict) -> dict:
    """Create or update a custom persona (idempotent PUT semantics: provided
    fields replace stored ones, omitted fields keep their stored value)."""
    slug = valid_slug(slug)
    if is_builtin(slug):
        raise PersonaError(
            f"{slug!r} is a built-in persona — only custom personas can be authored"
        )
    body = body or {}
    existing = read_spec(slug)
    spec: dict = existing or {"slug": slug, "version": 0, "created": _now()}
    spec["slug"] = slug

    if "display_name" in body:
        name = _clean_text("display_name", body["display_name"])
        if not name:
            raise PersonaError("display_name must be non-empty")
        spec["display_name"] = name
    spec.setdefault("display_name", _default_display_name(slug))
    for field in _TEXT_FIELDS:
        if field in body:
            spec[field] = _clean_text(field, body[field])

    patch = body.get("baseline")
    if patch is not None and not isinstance(patch, dict):
        raise PersonaError("baseline must be an object of channel -> value")
    spec["baseline"] = _resolve_baseline(existing, patch)
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
    return dict(spec)


def get(slug: str) -> dict | None:
    """One persona: the stored spec (custom) or the canonical profile (built-in)."""
    slug = str(slug or "").strip()
    spec = read_spec(slug)
    if spec is not None:
        return {**spec, "builtin": False}
    from brain import persona_chem

    name = persona_chem.display_name_for(slug)
    if name is None:
        return None
    resting = dict(persona_chem.PERSONA_CHEMISTRY[name])
    resting["GABA"] = max(persona_chem.GABA_RESTING_FLOOR, resting["GABA"])
    return {
        "slug": persona_slug(name),
        "display_name": name,
        "builtin": True,
        "baseline": resting,
    }


def list_all() -> list[dict]:
    """Every persona this org can run: built-ins (slug + display name) first,
    then custom specs in slug order."""
    from brain import persona_chem

    out: list[dict] = [
        {"slug": persona_slug(name), "display_name": name, "builtin": True}
        for name in persona_chem.PERSONA_CHEMISTRY
    ]
    builtin_slugs = {e["slug"] for e in out}
    root = personas_dir()
    customs: list[dict] = []
    with contextlib.suppress(OSError):
        for entry in sorted(root.iterdir()):
            spec_file = entry / "persona.json"
            if entry.name in builtin_slugs or not spec_file.exists():
                continue
            try:
                spec = json.loads(spec_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[personas] unreadable spec %s: %s", spec_file, e)
                continue
            customs.append(
                {
                    "slug": spec.get("slug", entry.name),
                    "display_name": spec.get("display_name", _default_display_name(entry.name)),
                    "builtin": False,
                    "version": spec.get("version"),
                    "updated": spec.get("updated"),
                }
            )
    return out + customs


def capacity_limits() -> dict:
    """The caps that govern concurrent persona processes (see brain/provisioner.py):
    per-org dedicated persona instances, and total live brains on the host."""
    return {
        "max_dedicated_instances": int(os.environ.get("BRAIN_MAX_DEDICATED", "3") or 0),
        "max_live_brains": int(os.environ.get("BRAIN_MAX_TENANTS", "25") or 0),
    }


def delete(slug: str) -> bool:
    """Remove a custom persona's spec + chemistry + identity document. Learned
    state (episodes, wiring) is left in place — it is keyed by the slug and
    simply goes dormant. Built-ins cannot be deleted. Returns False when there
    was no spec to remove."""
    slug = valid_slug(slug)
    if is_builtin(slug):
        raise PersonaError(f"{slug!r} is a built-in persona and cannot be deleted")
    spec_path = _spec_path(slug)
    existed = spec_path.exists()
    with contextlib.suppress(OSError):
        spec_path.unlink()
    from brain import persona_chem

    with contextlib.suppress(OSError):
        persona_chem._path(slug).unlink()  # noqa: SLF001 — same-package state file
    if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase":
        try:
            from brain.second_brain import supabase_client

            if supabase_client.is_enabled():
                sb = supabase_client.get_client()
                org = supabase_client.get_org_id()
                sb.table("brain_schemas").delete().eq("org_id", org).eq(
                    "persona", slug
                ).execute()
        except Exception as e:
            logger.warning("[personas] brain_schemas cleanup failed for %s: %s", slug, e)
    else:
        with contextlib.suppress(OSError):
            (persona_state_root(slug) / "schema" / "self.md").unlink()
    return existed
