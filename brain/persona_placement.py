"""Persona placement — the premium-tier entitlement row (migration 038).

A persona with no row here is SHARED: it binds per turn on the org's shared brain
and its local model calls ride the platform GPU pool. A `dedicated` row promises a
process of its own (`org::persona`, pinned past the idle reaper) and says which GPU
that process talks to:

  pod = "pool"        the platform pool, like everyone else (a full-cadence idle
                      mind, but a shared card);
  pod = "standalone"  a pod of its own — full-cadence idle thinking on its own GPU;
  pod = "org"         one pod shared by this org's dedicated instances.

The row is the entitlement; the gateway's desired-state loop (brain/gateway/
placement_control.py) makes processes and pods match it every tick. Nothing here
spawns anything.

Same migration-safety shape as brain/persona_owners.py: a missing table (038 not
applied) reads as "no placements" with ONE log line, so code deploys before
`supabase db push`; writes fail loudly with the migration name. Every query is
org-scoped.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

TABLE = "persona_placement"
MODES = ("shared", "dedicated")
PODS = ("pool", "standalone", "org")
FIELDS = ("mode", "pod", "gpu_type", "always_on", "paid_until")

_warned_missing = False


class PlacementError(ValueError):
    """A body that does not describe a placement (400)."""


class PlacementUnavailable(RuntimeError):
    """The registry cannot be written: no backend, or migration 038 not applied."""


def _sb():
    from brain.second_brain import supabase_client

    try:
        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception as e:  # pragma: no cover - deployment shape
        logger.warning("[persona_placement] backend unavailable: %s", e)
        return None


def _slug(persona: str) -> str:
    from brain.persona_key import persona_slug

    return persona_slug(persona)


def _missing(e: Exception, what: str) -> None:
    """Log the migration-missing hint once per process, then stay quiet."""
    global _warned_missing
    if not _warned_missing:
        _warned_missing = True
        logger.warning(
            "[persona_placement] %s failed (%s) — reading as 'no placements'; "
            "apply migration 038_persona_placement_and_gpu",
            what,
            e,
        )
    else:
        logger.debug("[persona_placement] %s failed: %s", what, e)


# ── validation ────────────────────────────────────────────────────────────────


def _bool(v, field: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise PlacementError(f"{field} must be a boolean")


def parse_paid_until(v) -> str | None:
    """ISO-8601 (or epoch seconds) → normalised UTC ISO string; None when unset."""
    if v is None or v == "":
        return None
    if isinstance(v, int | float) and not isinstance(v, bool):
        return datetime.fromtimestamp(float(v), tz=UTC).isoformat()
    if not isinstance(v, str):
        raise PlacementError("paid_until must be an ISO-8601 timestamp or null")
    s = v.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise PlacementError("paid_until must be an ISO-8601 timestamp or null") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def is_expired(row: dict | None, now: float | None = None) -> bool:
    """True when the row's paid_until is in the past (an expired placement reads
    as demoted: the controller stops the instance, the API reports shared)."""
    if not row or not row.get("paid_until"):
        return False
    try:
        until = datetime.fromisoformat(str(row["paid_until"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until.timestamp() <= (now if now is not None else time.time())


def validate(body: dict | None) -> dict:
    """Normalise a POST body into the row fields. Raises PlacementError (400)."""
    if not isinstance(body, dict):
        raise PlacementError("body must be an object")
    unknown = sorted(k for k in body if k not in FIELDS)
    if unknown:
        raise PlacementError(f"unknown placement fields: {', '.join(unknown)}")
    mode = str(body.get("mode") or "dedicated").strip().lower()
    if mode not in MODES:
        raise PlacementError(f"mode must be one of {list(MODES)}")
    pod = str(body.get("pod") or "pool").strip().lower()
    if pod not in PODS:
        raise PlacementError(f"pod must be one of {list(PODS)}")
    gpu_type = body.get("gpu_type")
    if gpu_type is not None:
        gpu_type = str(gpu_type).strip() or None
        if gpu_type and len(gpu_type) > 128:
            raise PlacementError("gpu_type is too long")
    always_on = _bool(body.get("always_on", True), "always_on")
    return {
        "mode": mode,
        "pod": pod,
        "gpu_type": gpu_type,
        "always_on": always_on,
        "paid_until": parse_paid_until(body.get("paid_until")),
    }


def default_row(persona: str) -> dict:
    """What an unplaced persona looks like: shared, on the pool."""
    return {
        "persona": _slug(persona),
        "mode": "shared",
        "pod": "pool",
        "gpu_type": None,
        "always_on": False,
        "paid_until": None,
        "placed": False,
    }


def _public(row: dict) -> dict:
    return {
        "persona": str(row.get("persona") or ""),
        "mode": str(row.get("mode") or "dedicated"),
        "pod": str(row.get("pod") or "pool"),
        "gpu_type": row.get("gpu_type") or None,
        "always_on": bool(row.get("always_on", True)),
        "paid_until": row.get("paid_until") or None,
        "created_by": row.get("created_by") or None,
        "created_at": row.get("created_at") or None,
        "updated_at": row.get("updated_at") or None,
        "placed": True,
        "expired": is_expired(row),
    }


# ── reads (tenant process: the org is the process's own) ─────────────────────


def get(persona: str) -> dict | None:
    """The placement row for a persona, or None (unplaced / no backend / table
    missing)."""
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        res = (
            client.table(TABLE)
            .select("*")
            .eq("org_id", org)
            .eq("persona", _slug(persona))
            .limit(1)
            .execute()
        )
    except Exception as e:
        _missing(e, f"lookup {persona}")
        return None
    rows = res.data or []
    return _public(dict(rows[0])) if rows else None


def list_for_org() -> list[dict]:
    """Every placement row of THIS org (the tenant process's own)."""
    sb = _sb()
    if sb is None:
        return []
    _client, org = sb
    return list_for(org, client=_client)


def _list_for(org_id: str, client=None) -> list[dict] | None:
    """Rows for one org, or None when the read FAILED (table missing, db down) —
    distinct from an empty org, which is a real answer."""
    if not org_id:
        return []
    if client is None:
        sb = _sb()
        if sb is None:
            return []
        client = sb[0]
    try:
        res = client.table(TABLE).select("*").eq("org_id", org_id).order("persona").execute()
    except Exception as e:
        _missing(e, f"list {str(org_id)[:8]}")
        return None
    return [_public(dict(r)) for r in (res.data or [])]


def list_for(org_id: str, client=None) -> list[dict]:
    """Every placement row of ONE org, by explicit id — the gateway's read (it
    serves many orgs under the service role). Always org-scoped; [] on error."""
    return _list_for(org_id, client=client) or []


def list_all(client=None) -> dict[str, list[dict]] | None:
    """EVERY org's rows in one service-role read → {org_id: [public rows]} — the
    placement controller's desired state. None when the read FAILED (table
    missing, db down), so the caller can keep its last-known view instead of
    treating a blink as a mass revocation."""
    if client is None:
        sb = _sb()
        if sb is None:
            return None
        client = sb[0]
    try:
        # Literal table name on purpose: tests/security/test_org_scoping.py allowlists
        # this one deliberately cross-org read by (file, table, op).
        res = (
            client.table("persona_placement").select("*").order("org_id").order("persona").execute()
        )
    except Exception as e:
        _missing(e, "list_all")
        return None
    out: dict[str, list[dict]] = {}
    for r in res.data or []:
        org = str(r.get("org_id") or "")
        if org:
            out.setdefault(org, []).append(_public(dict(r)))
    return out


def dedicated_count(exclude: str | None = None) -> int:
    """How many personas hold an unexpired dedicated row (the cap counter)."""
    ex = _slug(exclude) if exclude else None
    return sum(
        1
        for r in list_for_org()
        if r.get("mode") == "dedicated" and not r.get("expired") and r.get("persona") != ex
    )


def registry_available() -> bool:
    """True when the table answers (migration 038 applied)."""
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    try:
        client.table(TABLE).select("persona").eq("org_id", org).limit(1).execute()
        return True
    except Exception:
        return False


# ── writes ────────────────────────────────────────────────────────────────────


def upsert(persona: str, fields: dict, created_by: str | None = None) -> dict:
    """Write the placement row (insert or replace) and read it back."""
    sb = _sb()
    if sb is None:
        raise PlacementUnavailable("persona placement requires the Supabase storage backend")
    client, org = sb
    row = {
        "org_id": org,
        "persona": _slug(persona),
        "mode": fields["mode"],
        "pod": fields["pod"],
        "gpu_type": fields.get("gpu_type"),
        "always_on": bool(fields.get("always_on", True)),
        "paid_until": fields.get("paid_until"),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if created_by:
        row["created_by"] = str(created_by)[:128]
    try:
        client.table(TABLE).upsert(row, on_conflict="org_id,persona").execute()
    except Exception as e:
        raise PlacementUnavailable(
            f"persona_placement write failed ({e}) — apply migration 038_persona_placement_and_gpu"
        ) from e
    return get(persona) or _public(row)


def delete(persona: str) -> int:
    """Remove the placement row (the persona returns to shared). Rows removed."""
    sb = _sb()
    if sb is None:
        raise PlacementUnavailable("persona placement requires the Supabase storage backend")
    client, org = sb
    try:
        res = client.table(TABLE).delete().eq("org_id", org).eq("persona", _slug(persona)).execute()
    except Exception as e:
        raise PlacementUnavailable(
            f"persona_placement delete failed ({e}) — apply migration 038_persona_placement_and_gpu"
        ) from e
    return len(res.data or [])


# ── caps ──────────────────────────────────────────────────────────────────────


def env_max_dedicated() -> int:
    """The deployment default (BRAIN_MAX_DEDICATED, 3). 0 = uncapped."""
    try:
        return int(os.environ.get("BRAIN_MAX_DEDICATED", "3") or 0)
    except ValueError:
        return 3


def effective_max_dedicated() -> int:
    """The org row's max_dedicated_instances when set (> 0), else the deployment
    default. 0 = uncapped."""
    try:
        from brain import org_settings

        n = int(org_settings.max_dedicated_instances() or 0)
    except Exception:
        n = 0
    return n if n > 0 else env_max_dedicated()


# ── gateway-side cache (service role; many orgs) ─────────────────────────────

_GW_TTL_S = 60.0
_gw_lock = threading.Lock()
_gw_cache: dict[str, tuple[list[dict], float]] = {}


def cached_for(org_id: str, client=None, ttl_s: float | None = None) -> list[dict]:
    """list_for() behind a per-org TTL cache for the gateway's reconcile tick.
    Serves the last-known rows when a read fails (never an empty list because the
    database blinked, which would look like every placement being revoked)."""
    now = time.time()
    with _gw_lock:
        hit = _gw_cache.get(org_id)
    if hit and now - hit[1] < (ttl_s if ttl_s is not None else _GW_TTL_S):
        return hit[0]
    rows = _list_for(org_id, client=client)
    if rows is None:
        return hit[0] if hit else []
    with _gw_lock:
        _gw_cache[org_id] = (rows, now)
    return rows


def invalidate_cache() -> None:
    with _gw_lock:
        _gw_cache.clear()


__all__ = [
    "FIELDS",
    "MODES",
    "PODS",
    "PlacementError",
    "PlacementUnavailable",
    "cached_for",
    "dedicated_count",
    "default_row",
    "delete",
    "effective_max_dedicated",
    "env_max_dedicated",
    "get",
    "is_expired",
    "list_all",
    "list_for",
    "list_for_org",
    "registry_available",
    "upsert",
    "validate",
]
