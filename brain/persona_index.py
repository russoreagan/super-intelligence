"""Persona index — the `personas` table (migration 039), kept in step with the spec files.

`persona.json` stays the source of truth for a persona's SPEC (it is only ever read
one at a time). This table is the source of truth for LISTING, SEARCH, ACTIVITY and
the fleet rollups: an org with thousands of purchase personas must page its
catalogue in O(page), not parse every spec on the network volume per request.

Phase C (this module as shipped): dark writes only. Every sync point — spec
upsert/clone/delete, the hard purge, the per-persona human-turn stamp, the
learned-state flag after a sleep pass or a `current`-seed clone, ownership — writes
here, and `page()` / `slugs()` / `count_custom()` exist for Phase D to switch the
readers behind `persona_index_read`. Nothing reads the index yet.

Contract: best-effort, never raises into a caller. A failure is logged at WARNING
once per (operation) and then at DEBUG. A missing table (039 not applied) is
remembered for 60 s, during which `enabled()` is False and every call is a no-op —
so code can deploy ahead of `supabase db push`. Every query is scoped
`.eq("org_id", org)`; one process serves one org.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

TABLE = "personas"
RECONCILE_BATCH = 200
_MISSING_TTL_S = 60.0

# Column set the listing reads — the page contract needs nothing else.
_PAGE_COLUMNS = "persona,display_name,builtin,builtin_override,template,seed,version,spec_updated"

# In-memory human-turn stamps waiting for the next flush: slug -> unix ts.
_touches: dict[str, float] = {}
# Last stamp SENT (or queued) per slug — the debounce clock.
_touch_sent: dict[str, float] = {}
_touch_lock = threading.Lock()
# learned_state is set once per process per slug; the flag is monotonic.
_learned_marked: set[str] = set()
# "table missing" probe cache: enabled() is False until this wall-clock time.
_missing_until: float = 0.0
# Operations that have already logged their failure at WARNING.
_warned: set[str] = set()

_MISSING_MARKERS = (
    "does not exist",
    "could not find the table",
    "pgrst205",
    "42p01",
    "pgrst202",
    "could not find the function",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ts_iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), UTC).isoformat()


def _sb():
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception:
        return None


def _setting(key: str, default):
    try:
        from brain.settings import settings

        return settings.get(key, default)
    except Exception:
        return default


def _looks_missing(e: BaseException) -> bool:
    msg = str(e).lower()
    return any(m in msg for m in _MISSING_MARKERS)


def _fail(op: str, e: BaseException) -> None:
    """Log once at WARNING per operation, then DEBUG. A missing table/RPC parks
    the index for _MISSING_TTL_S so a pre-migration deploy stays quiet."""
    global _missing_until
    if _looks_missing(e):
        _missing_until = time.time() + _MISSING_TTL_S
        level = logging.WARNING if "missing" not in _warned else logging.DEBUG
        _warned.add("missing")
        logger.log(
            level,
            "[persona_index] %s: table/RPC missing (apply migration 039_persona_scale) — "
            "index off for %ds: %s",
            op,
            int(_MISSING_TTL_S),
            e,
        )
        return
    level = logging.WARNING if op not in _warned else logging.DEBUG
    _warned.add(op)
    logger.log(level, "[persona_index] %s failed: %s", op, e)


def enabled() -> bool:
    """Supabase on, `persona_index_enabled`, and the table not known to be missing."""
    if not _setting("persona_index_enabled", 1):
        return False
    if time.time() < _missing_until:
        return False
    return _sb() is not None


def _slug(persona: str) -> str:
    from brain.persona_key import persona_slug

    return persona_slug(persona)


def _is_builtin(slug: str) -> bool:
    try:
        from brain import personas

        return personas.is_builtin(slug)
    except Exception:
        return False


def _row_from_spec(spec: dict) -> dict | None:
    slug = _slug(str(spec.get("slug") or ""))
    if not slug:
        return None
    builtin = _is_builtin(slug)
    return {
        "persona": slug,
        "display_name": str(spec.get("display_name") or ""),
        "builtin": builtin,
        # A spec on disk for a built-in slug IS the override (personas.upsert).
        "builtin_override": builtin,
        "template": _slug(str(spec.get("template") or "")),
        "seed": str(spec.get("seed") or ""),
        "tag": str(spec.get("tag") or "")[:2000],
        "note": str(spec.get("note") or "")[:2000],
        "version": int(spec.get("version") or 0),
        "spec_updated": spec.get("updated") or None,
        "updated_at": _now_iso(),
        "deleted_at": None,
    }


def _row_for_builtin(slug: str, display_name: str, overridden: bool) -> dict:
    return {
        "persona": slug,
        "display_name": display_name,
        "builtin": True,
        "builtin_override": bool(overridden),
        "template": "",
        "seed": "",
        "tag": "",
        "note": "",
        "version": 0,
        "spec_updated": None,
        "updated_at": _now_iso(),
        "deleted_at": None,
    }


def _upsert_rows(rows: list[dict], op: str) -> bool:
    sb = _sb()
    if sb is None or not rows:
        return False
    client, org = sb
    payload = [{"org_id": org, **r} for r in rows]
    try:
        client.table(TABLE).upsert(payload, on_conflict="org_id,persona").execute()
        return True
    except Exception as e:
        _fail(op, e)
        return False


# ── sync points ─────────────────────────────────────────────────────────────────


def upsert_from_spec(spec: dict) -> bool:
    """Mirror a spec (custom or built-in override) into the index. Never raises."""
    if not enabled():
        return False
    try:
        row = _row_from_spec(spec or {})
    except Exception as e:
        _fail("upsert_from_spec", e)
        return False
    if row is None:
        return False
    return _upsert_rows([row], "upsert_from_spec")


def upsert_builtin(slug: str, *, overridden: bool = False) -> bool:
    """Index a built-in persona row (overridden = a saved override spec exists).
    DELETE of a built-in override lands here with overridden=False."""
    if not enabled():
        return False
    try:
        from brain import persona_chem

        s = _slug(slug)
        name = persona_chem.display_name_for(s)
        if not s or name is None:
            return False
        return _upsert_rows([_row_for_builtin(s, name, overridden)], "upsert_builtin")
    except Exception as e:
        _fail("upsert_builtin", e)
        return False


def mark_deleted(slug: str) -> bool:
    """Soft delete (DELETE /v1/personas/{p} on a custom). The hard purge removes
    the row through session_turn._PERSONA_PURGE_TABLES instead."""
    if not enabled():
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    s = _slug(slug)
    if not s:
        return False
    try:
        now = _now_iso()
        client.table(TABLE).update({"deleted_at": now, "updated_at": now}).eq("org_id", org).eq(
            "persona", s
        ).execute()
        return True
    except Exception as e:
        _fail("mark_deleted", e)
        return False


def remove(slug: str) -> bool:
    """Hard delete of the row (the purge path also sweeps it by table name)."""
    if not enabled():
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    s = _slug(slug)
    if not s:
        return False
    try:
        client.table(TABLE).delete().eq("org_id", org).eq("persona", s).execute()
        _learned_marked.discard(s)
        with _touch_lock:
            _touches.pop(s, None)
            _touch_sent.pop(s, None)
        return True
    except Exception as e:
        _fail("remove", e)
        return False


def touch_human_turn(slug: str, ts: float | None = None) -> bool:
    """Queue a human-turn stamp (dict write only — no I/O). Debounced per slug by
    `persona_index_touch_debounce_s`; the queue is flushed as ONE RPC by
    flush_touches() on the usage-flush cadence. Returns True when queued."""
    s = _slug(slug)
    if not s:
        return False
    t = float(ts if ts is not None else time.time())
    try:
        debounce = float(_setting("persona_index_touch_debounce_s", 300) or 0.0)
    except (TypeError, ValueError):
        debounce = 300.0
    with _touch_lock:
        last = _touch_sent.get(s)
        if last is not None and debounce > 0 and (t - last) < debounce:
            # Inside the window: fold into an entry still queued (free), else drop.
            if s in _touches:
                _touches[s] = max(t, _touches[s])
            return False
        _touches[s] = max(t, _touches.get(s, 0.0))
        _touch_sent[s] = t
    return True


def flush_touches() -> int:
    """Push every queued stamp in one `persona_touch_batch` RPC. Blocking Supabase
    I/O — call from a thread. Returns the number of slugs sent (0 when nothing is
    queued, the index is off, or the RPC failed — failed stamps are re-queued)."""
    with _touch_lock:
        if not _touches:
            return 0
        batch = dict(_touches)
        _touches.clear()
    if not enabled():
        # Index off: drop the batch (the on-disk stamp is the fallback source and
        # reconcile() re-reads it). Re-queueing forever would only grow memory.
        return 0
    sb = _sb()
    if sb is None:
        return 0
    client, org = sb
    try:
        client.rpc(
            "persona_touch_batch",
            {"p_org_id": org, "p_touches": {s: _ts_iso(t) for s, t in batch.items()}},
        ).execute()
        return len(batch)
    except Exception as e:
        _fail("flush_touches", e)
        with _touch_lock:
            for s, t in batch.items():
                _touches[s] = max(t, _touches.get(s, 0.0))
        return 0


def set_learned_state(slug: str, on: bool = True) -> bool:
    """Flag that a persona holds learned state (a sleep pass ran under its binding,
    or a `current`-seed clone copied the template's competence). Written at most
    once per process per slug — the flag is monotonic and the writers are hot."""
    s = _slug(slug)
    if not s:
        return False
    if on and s in _learned_marked:
        return False
    if not enabled():
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    try:
        now = _now_iso()
        client.table(TABLE).update(
            {"learned_state": bool(on), "learned_state_at": now, "updated_at": now}
        ).eq("org_id", org).eq("persona", s).execute()
        if on:
            _learned_marked.add(s)
        else:
            _learned_marked.discard(s)
        return True
    except Exception as e:
        _fail("set_learned_state", e)
        return False


def set_owner(
    slug: str,
    owner_bound: bool,
    owner_ref: str = "",
    owner_count: int = 0,
    partner_id: str = "",
) -> bool:
    """Ownership rollup (persona_owners.claim): bound, the HMAC ref of the owner
    (never the buyer id), how many distinct owners were seen, the partner."""
    s = _slug(slug)
    if not s or not enabled():
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    try:
        client.table(TABLE).update(
            {
                "owner_bound": bool(owner_bound),
                "owner_ref": str(owner_ref or "")[:64],
                "owner_count": int(owner_count or 0),
                "partner_id": str(partner_id or "")[:128],
                "updated_at": _now_iso(),
            }
        ).eq("org_id", org).eq("persona", s).execute()
        return True
    except Exception as e:
        _fail("set_owner", e)
        return False


# ── readers (Phase D switches callers onto these behind persona_index_read) ─────


def count_custom() -> int | None:
    """Head count of live custom personas (built-ins excluded). None when the index
    cannot answer — callers fall back to the on-disk scan."""
    if not enabled():
        return None
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        res = (
            client.table(TABLE)
            .select("persona", count="exact", head=True)
            .eq("org_id", org)
            .eq("builtin", False)
            .is_("deleted_at", "null")
            .execute()
        )
        n = getattr(res, "count", None)
        if n is None:
            n = len(getattr(res, "data", None) or [])
        return int(n)
    except Exception as e:
        _fail("count_custom", e)
        return None


def _entry(row: dict) -> dict:
    """One index row in the shape personas.list_all() emits."""
    slug = str(row.get("persona") or "")
    if row.get("builtin"):
        return {
            "slug": slug,
            "display_name": row.get("display_name") or slug,
            "builtin": True,
            "overridden": bool(row.get("builtin_override")),
        }
    entry = {
        "slug": slug,
        "display_name": row.get("display_name") or slug,
        "builtin": False,
        "version": row.get("version"),
        "updated": row.get("spec_updated"),
    }
    if row.get("template"):
        entry["template"] = row["template"]
        entry["seed"] = row.get("seed") or None
    return entry


def page(
    *,
    include_clones: bool = False,
    template: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
    allowed: list[str] | tuple[str, ...] | None = None,
    max_limit: int = 1000,
) -> dict | None:
    """The GET /v1/personas listing contract (personas.page) served from the index
    in one ranged query: clones hidden unless `template=` (one template's clones)
    or `include_clones`; `q` matches slug or display name (substring); `allowed`
    restricts to a key's agent allowlist via `.in_`. None on any failure so the
    caller can fall back to the spec scan."""
    if not enabled():
        return None
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    from brain.personas import PersonaError

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
    try:
        query = (
            client.table(TABLE)
            .select(_PAGE_COLUMNS, count="exact")
            .eq("org_id", org)
            .is_("deleted_at", "null")
        )
        if template:
            query = query.eq("template", _slug(template))
        elif not include_clones:
            query = query.eq("template", "")
        if q:
            needle = str(q).strip().replace(",", " ").replace("%", "")[:64]
            if needle:
                query = query.or_(f"persona.ilike.%{needle}%,display_name.ilike.%{needle}%")
        if allowed is not None:
            allowed_slugs = sorted({_slug(a) for a in allowed if _slug(a)})
            if not allowed_slugs:
                return {
                    "personas": [],
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "next_offset": None,
                }
            query = query.in_("persona", allowed_slugs)
        res = (
            query.order("builtin", desc=True)
            .order("persona")
            .range(offset, offset + limit - 1)
            .execute()
        )
    except Exception as e:
        _fail("page", e)
        return None
    rows = [_entry(r) for r in (res.data or [])]
    total = getattr(res, "count", None)
    if total is None:
        total = offset + len(rows)
    total = int(total)
    nxt = offset + limit if offset + limit < total else None
    return {"personas": rows, "total": total, "limit": limit, "offset": offset, "next_offset": nxt}


def slugs(
    *,
    learned_state: bool | None = None,
    active_days: float | None = None,
    exclude: list[str] | tuple[str, ...] = (),
    limit: int = 10000,
) -> list[str] | None:
    """Live persona slugs matching the filters: `learned_state` (True/False),
    `active_days` (a human turn in the last N days; 0/None = no activity filter).
    None on any failure."""
    if not enabled():
        return None
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        query = client.table(TABLE).select("persona").eq("org_id", org).is_("deleted_at", "null")
        if learned_state is not None:
            query = query.eq("learned_state", bool(learned_state))
        if active_days:
            cutoff = time.time() - float(active_days) * 86400.0
            query = query.gte("last_human_turn_ts", _ts_iso(cutoff))
        res = query.order("persona").limit(int(limit)).execute()
    except Exception as e:
        _fail("slugs", e)
        return None
    skip = {_slug(x) for x in exclude}
    return [
        str(r.get("persona"))
        for r in (res.data or [])
        if r.get("persona") and str(r.get("persona")) not in skip
    ]


def template_of(slug: str) -> str | None:
    """The template a clone was cut from ("" for a non-clone), or None when the
    row is absent or the index cannot answer."""
    s = _slug(slug)
    if not s or not enabled():
        return None
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        res = (
            client.table(TABLE)
            .select("template")
            .eq("org_id", org)
            .eq("persona", s)
            .limit(1)
            .execute()
        )
    except Exception as e:
        _fail("template_of", e)
        return None
    rows = res.data or []
    return str(rows[0].get("template") or "") if rows else None


# ── reconcile ───────────────────────────────────────────────────────────────────


def reconcile(learned: bool = False) -> dict:
    """Walk the spec files + built-ins + per-persona stamps and upsert them in
    batches of RECONCILE_BATCH. `learned=True` also evaluates
    persona_audit.has_learned_state per custom persona (slower: it counts rows).
    Returns {indexed, learned, batches}. Never raises."""
    out = {"indexed": 0, "learned": 0, "batches": 0}
    if not enabled():
        return out
    try:
        from brain import human_activity, org_settings, persona_chem, personas
        from brain.persona_key import persona_slug

        specs = personas._read_all_specs()  # noqa: SLF001 — the reconcile source
        rows: list[dict] = []
        for name in persona_chem.PERSONA_CHEMISTRY:
            s = persona_slug(name)
            rows.append(_row_for_builtin(s, name, s in specs))
        for slug, spec in specs.items():
            if _is_builtin(slug):
                continue
            row = _row_from_spec({**spec, "slug": slug})
            if row is not None:
                rows.append(row)
        for row in rows:
            s = row["persona"]
            ts = human_activity.persona_last_turn_ts(s)
            row["last_human_turn_ts"] = _ts_iso(ts) if ts else None
            if learned:
                flag = False
                if not row["builtin"] and not org_settings.is_home(s):
                    try:
                        from brain import persona_audit

                        flag = bool(persona_audit.has_learned_state(s))
                    except Exception as e:
                        logger.debug("[persona_index] learned probe failed for %s: %s", s, e)
                # Uniform keys across the batch: PostgREST bulk upserts require
                # every row to carry the same columns.
                row["learned_state"] = flag
                row["learned_state_at"] = row["updated_at"] if flag else None
                if flag:
                    out["learned"] += 1
                    _learned_marked.add(s)
        for i in range(0, len(rows), RECONCILE_BATCH):
            chunk = rows[i : i + RECONCILE_BATCH]
            if _upsert_rows(chunk, "reconcile"):
                out["indexed"] += len(chunk)
                out["batches"] += 1
            else:
                break
    except Exception as e:
        _fail("reconcile", e)
    return out


def reconcile_on_boot() -> threading.Thread | None:
    """Boot hook: when `persona_index_reconcile_on_boot` and the index holds fewer
    live customs than the volume, run reconcile() in a daemon thread. All the
    probing happens inside the thread so boot never waits on Supabase."""
    if not _setting("persona_index_reconcile_on_boot", 1):
        return None

    def _run() -> None:
        try:
            if not enabled():
                return
            from brain import personas

            on_disk = personas.custom_count()
            indexed = count_custom()
            if indexed is not None and indexed >= on_disk:
                logger.debug(
                    "[persona_index] boot reconcile skipped (%d indexed ≥ %d on disk)",
                    indexed,
                    on_disk,
                )
                return
            res = reconcile()
            logger.info(
                "[persona_index] boot reconcile: %d rows in %d batch(es)",
                res["indexed"],
                res["batches"],
            )
        except Exception as e:
            _fail("reconcile_on_boot", e)

    t = threading.Thread(target=_run, name="persona-index-reconcile", daemon=True)
    t.start()
    return t


def _reset_for_tests() -> None:
    """Clear process state (touch queue, learned marks, missing probe, warn-once)."""
    global _missing_until
    with _touch_lock:
        _touches.clear()
        _touch_sent.clear()
    _learned_marked.clear()
    _warned.clear()
    _missing_until = 0.0
