"""Fleet listing — the org admin's paged, searchable view of every persona.

Content-free by construction: every field is a state, count, cost, hash or
timestamp. A buyer's words never pass through here; the read policy
(brain/read_policy.py) is not even consulted because there is nothing to gate.

Interim backend (until `persona_index_read`, Phase D): rows are assembled from
the spec scan (personas.list_all), the agents table, the per-persona human-turn
stamps and the live usage meter, cached in-process for FLEET_CACHE_S. Sorting
and filtering run in memory, which is fine to a few thousand personas; the
index-backed RPC replaces `gather()` with the same row shape.

Per-row fields that cost a lookup each (owner binding, learned state, open
projects) are resolved for the PAGE only, after sort and slice.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

FLEET_CACHE_S = 30.0
OPEN_JOB_STATES = ("running", "awaiting_approval", "deferred", "blocked")
OPEN_PROJECT_STATES = ("ready", "running", "pending", "blocked")
SORTS = ("last_human_turn_ts", "slug", "cost_7d_usd", "turns_7d", "created_at", "health")
_HEALTH_RANK = {"crit": 0, "warn": 1, "ok": 2}

_lock = threading.Lock()
_cache: tuple[float, list[dict]] = (0.0, [])


def _slug(p) -> str:
    from brain.persona_key import persona_slug

    return persona_slug(p or "")


def _epoch(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    with contextlib.suppress(Exception):
        import datetime as _dt

        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    return None


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = (0.0, [])


# ── gather ────────────────────────────────────────────────────────────────────


def _base_rows() -> list[dict]:
    """One row per persona from the spec scan + specs (tag/note/template)."""
    from brain import org_settings, personas

    specs: dict[str, dict] = {}
    with contextlib.suppress(Exception):
        specs = personas._read_all_specs()  # noqa: SLF001 - same package, interim path
    rows: list[dict] = []
    home = org_settings.home_persona()
    for e in personas.list_all():
        slug = str(e.get("slug") or "")
        spec = specs.get(slug) or {}
        rows.append(
            {
                "slug": slug,
                "display_name": e.get("display_name") or slug,
                "builtin": bool(e.get("builtin")),
                "is_home": bool(slug and slug == home),
                "template": _slug(e.get("template") or spec.get("template") or ""),
                "is_clone": bool(e.get("template") or spec.get("template")),
                "seed": e.get("seed") or spec.get("seed") or "",
                "tag": str(spec.get("tag") or ""),
                "created_at": _epoch(
                    spec.get("cloned") or spec.get("created") or spec.get("updated")
                ),
                "updated_at": _epoch(spec.get("updated")),
            }
        )
    return rows


def _agent_rollup() -> dict[str, dict]:
    """persona → {tier, enabled_agents, answer_only, agents:[…]} from one agents query."""
    out: dict[str, dict] = {}
    try:
        from brain import agents

        rows = agents.list_agents()
    except Exception:
        return out
    for r in rows or []:
        p = _slug(r.get("persona"))
        e = out.setdefault(
            p, {"tier": "lite", "enabled_agents": 0, "answer_only": None, "agents": []}
        )
        perms = r.get("permissions") if isinstance(r.get("permissions"), dict) else {}
        ao = (
            str(perms.get("answer_only", "")).strip().lower() in ("1", "true", "yes", "on")
            or perms.get("answer_only") is True
        )
        e["agents"].append(
            {
                "agent_id": r.get("agent_id"),
                "mandate_id": r.get("mandate_id"),
                "name": r.get("name"),
                "tier": r.get("tier") or "lite",
                "enabled": bool(r.get("enabled")),
                "answer_only": ao,
            }
        )
        if r.get("enabled"):
            e["enabled_agents"] += 1
            if (r.get("tier") or "lite") == "full":
                e["tier"] = "full"
            e["answer_only"] = ao if e["answer_only"] is None else (e["answer_only"] and ao)
    for e in out.values():
        e["answer_only"] = bool(e["answer_only"])
    return out


def _usage_by_persona(usage: dict | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for aid, u in ((usage or {}).get("usage") or {}).items():
        p = _slug(str(aid).split(".", 1)[0])
        e = out.setdefault(p, {"cloud_usd": 0.0, "calls": 0, "last_ts": None})
        e["cloud_usd"] += float((u or {}).get("cloud_usd") or 0.0)
        e["calls"] += int((u or {}).get("calls") or 0)
        lt = _epoch((u or {}).get("last_ts"))
        if lt and (e["last_ts"] is None or lt > e["last_ts"]):
            e["last_ts"] = lt
    return out


def _jobs_by_persona(jobs: list[dict] | None, now: float | None = None) -> dict[str, dict]:
    from brain import fleet_alerts, read_policy

    stuck = {s["job_id"] for s in fleet_alerts.stuck_jobs(jobs or [], now=now)}
    out: dict[str, dict] = {}
    for j in jobs or []:
        p = read_policy.persona_of_job(j)
        e = out.setdefault(p, {"jobs_open": 0, "jobs_stuck": 0})
        if str(j.get("state") or "") in OPEN_JOB_STATES:
            e["jobs_open"] += 1
        if (j.get("job_id") or j.get("id")) in stuck:
            e["jobs_stuck"] += 1
    return out


def gather(
    *, live: dict | None, usage: dict | None, jobs: list[dict] | None, now: float | None = None
) -> list[dict]:
    """Every persona row (cached). Cheap fields only; see `enrich_page`."""
    global _cache
    ref = float(now if now is not None else time.time())
    with _lock:
        ts, rows = _cache
        base = [dict(r) for r in rows] if rows and ref - ts < FLEET_CACHE_S else None
    if base is None:
        from brain import human_activity

        base = _base_rows()
        agents = _agent_rollup()
        for r in base:
            a = agents.get(r["slug"]) or {}
            r["tier"] = a.get("tier", "lite" if not r["is_home"] else "full")
            r["enabled_agents"] = int(a.get("enabled_agents", 0))
            r["answer_only"] = bool(a.get("answer_only", False))
            r["agents"] = a.get("agents", [])
            r["last_human_turn_ts"] = human_activity.persona_last_turn_ts(r["slug"])
        with _lock:
            _cache = (ref, [dict(r) for r in base])
    # Live overlays are never cached: roster membership, spend, jobs, breaker.
    roster = {_slug(p) for p in ((live or {}).get("roster_personas") or [])}
    breaker = bool((live or {}).get("breaker"))
    days = float(((live or {}).get("dmn") or {}).get("roster", {}).get("days") or 0.0)
    ub = _usage_by_persona(usage)
    jb = _jobs_by_persona(jobs, ref)
    for r in base:
        u = ub.get(r["slug"]) or {}
        j = jb.get(r["slug"]) or {}
        r["on_roster"] = r["slug"] in roster
        r["cost_7d_usd"] = round(float(u.get("cloud_usd") or 0.0), 4)  # live meter until the rollup
        r["turns_7d"] = int(u.get("calls") or 0)
        r["jobs_open"] = int(j.get("jobs_open") or 0)
        r["jobs_stuck"] = int(j.get("jobs_stuck") or 0)
        lt = r.get("last_human_turn_ts")
        r["state"] = (
            "never"
            if not lt
            else ("active" if (days <= 0 or ref - lt <= days * 86400.0) else "dormant")
        )
        flags = []
        if r["jobs_stuck"]:
            flags.append("stuck_job")
        if breaker and r["tier"] == "full":
            flags.append("breaker_affected")
        if r["is_clone"] and not lt and r.get("created_at") and ref - r["created_at"] > 7 * 86400.0:
            flags.append("never_touched")
        if r["answer_only"]:
            flags.append("answer_only")
        r["flags"] = flags
        r["health"] = "crit" if "stuck_job" in flags else ("warn" if flags else "ok")
    return base


# ── filter / sort / page ──────────────────────────────────────────────────────


def _cursor_encode(offset: int, sort: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset, "s": sort}).encode()).decode()


def _cursor_decode(cursor: str | None, sort: str) -> int:
    if not cursor:
        return 0
    with contextlib.suppress(Exception):
        d = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if d.get("s") == sort:
            return max(0, int(d.get("o") or 0))
    return 0


def _sort_key(sort: str):
    desc = sort.startswith("-")
    key = sort.lstrip("-")

    def k(r: dict):
        if key == "slug":
            return (r["slug"],)
        if key == "health":
            return (_HEALTH_RANK.get(r.get("health"), 3), r["slug"])
        v = r.get(key)
        # None sorts last in either direction.
        return ((v is None), -(v or 0) if desc else (v or 0), r["slug"])

    return k, (desc and key == "slug")


def list_rows(
    rows: list[dict],
    *,
    q: str = "",
    template: str = "",
    tag: str = "",
    state: str = "",
    roster: str = "",
    learned: str = "",
    answer_only: str = "",
    is_clone: str = "",
    flag: str = "",
    sort: str = "-last_human_turn_ts",
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    """Filter, sort and slice `rows` (from gather). Returns {rows, next_cursor,
    total, sort}. Expensive per-row fields are then added by enrich_page."""
    key = sort.lstrip("-")
    if key not in SORTS:
        sort, key = "-last_human_turn_ts", "last_human_turn_ts"
    out = rows
    if q:
        ql = q.strip().lower()
        out = [
            r
            for r in out
            if ql in r["slug"]
            or ql in str(r.get("display_name") or "").lower()
            or ql in str(r.get("tag") or "").lower()
        ]
    if template:
        t = _slug(template)
        out = [r for r in out if r.get("template") == t]
    if tag:
        out = [r for r in out if str(r.get("tag") or "").lower() == tag.lower()]
    if state:
        out = [r for r in out if r.get("state") == state]
    if roster in ("on", "off"):
        out = [r for r in out if bool(r.get("on_roster")) == (roster == "on")]
    if answer_only in ("1", "true"):
        out = [r for r in out if r.get("answer_only")]
    if is_clone in ("0", "1"):
        out = [r for r in out if bool(r.get("is_clone")) == (is_clone == "1")]
    if flag:
        out = [r for r in out if flag in (r.get("flags") or [])]
    if learned in ("1", "true"):
        out = [r for r in out if r.get("learned_state")]
    k, reverse = _sort_key(sort)
    out = sorted(out, key=k, reverse=reverse)
    limit = max(1, min(int(limit or 50), 200))
    offset = _cursor_decode(cursor, sort)
    page = out[offset : offset + limit]
    nxt = _cursor_encode(offset + limit, sort) if offset + limit < len(out) else None
    return {"rows": page, "next_cursor": nxt, "total": len(out), "sort": sort}


def owner_ref(end_user_id: str | None) -> str:
    """The non-reversible reference shown to support (never the buyer id)."""
    from brain import persona_owners, read_policy

    eu = str(end_user_id or "")
    if not eu:
        return ""
    fn = getattr(persona_owners, "owner_ref_for", None)
    if callable(fn):
        with contextlib.suppress(Exception):
            return str(fn(eu) or "")
    return read_policy.end_user_hash(eu)


def enrich_page(rows: list[dict]) -> list[dict]:
    """Per-row lookups for the PAGE only: owner binding, learned state, open
    projects. Never the owner id itself."""
    from brain import agent_projects_store, persona_audit, persona_owners

    slugs = [r["slug"] for r in rows]
    projects: dict[str, int] = {}
    with contextlib.suppress(Exception):
        for p in agent_projects_store.list_for_personas(slugs):
            if str(p.get("state") or "") in OPEN_PROJECT_STATES:
                projects[_slug(p.get("persona"))] = projects.get(_slug(p.get("persona")), 0) + 1
    for r in rows:
        r["projects_open"] = int(projects.get(r["slug"], 0))
        owner = None
        with contextlib.suppress(Exception):
            owner = persona_owners.owner_of_cached(r["slug"]) if not r.get("is_home") else None
        r["owner_bound"] = bool(owner)
        r["owner_ref"] = owner_ref(owner) if owner else ""
        learned = False
        with contextlib.suppress(Exception):
            learned = bool(persona_audit.has_learned_state(r["slug"]))
        r["learned_state"] = learned
        if learned and not r.get("is_clone") and not r.get("is_home") and not r.get("builtin"):
            r.setdefault("flags", []).append("template_learned")
            if r.get("health") == "ok":
                r["health"] = "warn"
    return rows


# ── drawer ────────────────────────────────────────────────────────────────────

_PROJECT_META = (
    "id",
    "state",
    "priority",
    "deadline_at",
    "max_runs",
    "ready_at",
    "mandate_id",
    "runs",
    "created_at",
    "updated_at",
)


def drawer(slug: str, rows: list[dict], jobs: list[dict] | None, live: dict | None) -> dict | None:
    """The content-free persona card: the row + dials (spec) + chemistry state +
    projected jobs/projects + agents + fingerprint history + roster status."""
    from brain import persona_chem, personas, read_policy

    slug = _slug(slug)
    row = next((r for r in rows if r["slug"] == slug), None)
    if row is None:
        return None
    row = dict(row)
    enrich_page([row])
    spec = None
    with contextlib.suppress(Exception):
        spec = personas.get(slug)
    chem = None
    with contextlib.suppress(Exception):
        st = persona_chem.load(row.get("display_name") or slug) or persona_chem.load(slug)
        if st:
            chem = {
                "resting": st.get("resting"),
                "current": st.get("current"),
                "updated": st.get("updated"),
            }
    my_jobs = [
        read_policy.project_job(j) for j in (jobs or []) if read_policy.persona_of_job(j) == slug
    ][:20]
    projects = []
    with contextlib.suppress(Exception):
        from brain import agent_projects_store

        for p in agent_projects_store.list_for_personas([slug]):
            projects.append({k: p.get(k) for k in _PROJECT_META if k in p})
    roster = ((live or {}).get("dmn") or {}).get("roster") or {}
    return {
        **row,
        "spec": _dials(spec),
        "chemistry": chem,
        "series_30d": [],  # daily rollup (Phase D)
        "jobs": my_jobs,
        "projects": projects,
        "fingerprints": fingerprints(slug),
        "roster": {
            "on": bool(row.get("on_roster")),
            "mode": roster.get("mode"),
            "days": roster.get("days"),
        },
    }


def _dials(spec: dict | None) -> dict | None:
    """The authored persona spec minus anything learned. Specs hold dials,
    baselines, disposition/speaking text and the Seed — all operator-authored."""
    if not spec:
        return None
    out = {k: v for k, v in spec.items() if k not in ("self_md",)}
    return out


# ── fingerprints (per-persona history of the isolation audit) ─────────────────


def _fp_path(slug: str):
    from brain.persona_key import persona_state_root

    return persona_state_root(slug) / "fingerprints.jsonl"


def record_fingerprint(slug: str, fingerprint: str, trigger: str = "manual") -> None:
    with contextlib.suppress(Exception):
        p = _fp_path(_slug(slug))
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"ts": time.time(), "fingerprint": fingerprint, "trigger": trigger})
                + "\n"
            )


def fingerprints(slug: str, n: int = 20) -> list[dict]:
    out: list[dict] = []
    with contextlib.suppress(Exception):
        p = _fp_path(_slug(slug))
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines()[-n:]:
                with contextlib.suppress(Exception):
                    out.append(json.loads(ln))
    return out


_audit_ts: dict[str, float] = {}


def cheap_audit(slug: str, *, min_interval_s: float = 60.0, now: float | None = None) -> dict:
    """persona_audit.snapshot without the org-wide counts, without the owner id
    or state root, with owner_bound/owner_ref, and the fingerprint appended to
    the persona's history. Rate-limited per persona."""
    from brain import persona_audit, persona_owners

    slug = _slug(slug)
    ref = float(now if now is not None else time.time())
    last = _audit_ts.get(slug, 0.0)
    if ref - last < min_interval_s:
        return {
            "persona": slug,
            "rate_limited": True,
            "retry_in_s": round(min_interval_s - (ref - last)),
        }
    _audit_ts[slug] = ref
    snap = persona_audit.snapshot(slug, cheap=True)
    owner = snap.pop("owner_end_user_id", None)
    snap.pop("state_root", None)
    snap["owner_bound"] = bool(owner)
    snap["owner_ref"] = owner_ref(owner) if owner else ""
    with contextlib.suppress(Exception):
        snap["owner_bound"] = bool(owner or persona_owners.owner_of_cached(slug))
    record_fingerprint(slug, snap.get("fingerprint", ""), "manual")
    snap["fingerprints"] = fingerprints(slug)
    return snap


# ── owner lookup ──────────────────────────────────────────────────────────────


def lookup_owner(owner_id: str) -> list[str]:
    """Personas bound to this buyer id (the id is hashed for the audit line and
    never returned). Org-scoped persona_owners read."""
    eu = str(owner_id or "").strip()
    if not eu:
        return []
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return []
        client, org = supabase_client.get_client(), supabase_client.get_org_id()
        res = (
            client.table("persona_owners")
            .select("persona")
            .eq("org_id", org)
            .eq("end_user_id", eu)
            .execute()
        )
        return sorted(str(r.get("persona")) for r in (res.data or []) if r.get("persona"))
    except Exception as e:
        logger.debug("[fleet] owner lookup failed: %s", e)
        return []
