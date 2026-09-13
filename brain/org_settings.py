"""Org-level governance values that live on the `organizations` row.

Why a Supabase column and not settings.json: each brain instance's settings.json is
seeded once from the bundled defaults and never overwritten (brain/provisioner.py),
so a dedicated persona instance and the org's shared instance would hold different
copies of an org-wide value. The organizations row is read identically by every
process that serves the org.

Two values today (migration 037):

  learning_mode   'consolidated' (default) — a persona is one learning identity
                  shared across every customer it talks to; cross-customer transfer
                  runs through the de-identification gate.
                  'isolated' — every persona is a separate individual; nothing
                  learned reaches another persona (brain/api/api_guide.md §20).
  instance_seed   'default' | 'current' — what a persona clone starts with in an
                  isolated org (brain/personas.py::clone).

Two caps for the premium placement tier (migration 038, same row, same cache):

  max_dedicated_instances  per-org ceiling on dedicated persona instances; 0 = the
                           deployment's BRAIN_MAX_DEDICATED applies.
  gpu_daily_usd_budget     per-org daily ceiling on standalone / org pod spend;
                           0 = the org may not hold standalone pods (402 on POST
                           /v1/personas/{p}/placement with pod standalone|org).

Read semantics matter more than usual because the leak gates key on them:

  * 60 s TTL cache, so a switch takes effect on the next turn and the next sleep
    pass without a restart, and the turn hot path never pays a round trip.
  * On a read ERROR the last successfully read value is returned. A mode that was
    read once is never silently replaced by a default because the database blinked.
  * A process that has NEVER read the row successfully reports "unknown". Callers
    that gate a leak treat unknown as isolated (fail closed: better to withhold a
    shared principle than to leak one); callers that would REFUSE a customer on it
    (persona ownership binding) treat unknown as "do not enforce yet" so a database
    blip at boot cannot 404 legitimate traffic. See is_isolated() / is_isolated_known().
  * No Supabase backend (companion / local mode) → consolidated: one org, one human,
    nothing to isolate.
"""

from __future__ import annotations

import logging
import math
import threading
import time

logger = logging.getLogger(__name__)

MODES = ("consolidated", "isolated")
SEEDS = ("current", "default")
UNKNOWN = "unknown"

_TTL_S = 60.0
_lock = threading.Lock()
# (learning_mode, instance_seed, read_ts). Mode "" = never read successfully.
_cache: tuple[str, str, float] = ("", "", 0.0)
# The last successfully read organizations row (the caps read from it). Same
# last-known-value semantics as the mode: a read error never blanks it.
_row_cache: dict = {}


class OrgSettingsError(RuntimeError):
    """The organizations row could not be written (backend off, or migration 037
    not applied). Surfaces as a 503 at the API so the caller knows the switch did
    NOT happen, rather than a silent no-op."""


def _sb():
    from brain.second_brain import supabase_client

    try:
        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception as e:  # pragma: no cover - deployment shape
        logger.warning("[org_settings] backend unavailable: %s", e)
        return None


def _read_row() -> dict | None:
    """The organizations row for this org, or None on error. select("*") so a
    deployment that has not applied 037 still reads (the columns are then simply
    absent and default below)."""
    sb = _sb()
    if sb is None:
        return {}
    client, org = sb
    if not org:
        return None
    res = client.table("organizations").select("*").eq("id", org).limit(1).execute()
    rows = res.data or []
    return dict(rows[0]) if rows else {}


def refresh(force: bool = False) -> tuple[str, str]:
    """(learning_mode, instance_seed), re-reading the row when the cache is stale.
    Never raises; on error returns the last-known pair (or ("unknown", "default")
    when nothing was ever read)."""
    global _cache
    now = time.time()
    with _lock:
        mode, seed, ts = _cache
        if not force and mode and now - ts < _TTL_S:
            return mode, seed
    try:
        row = _read_row()
    except Exception as e:
        logger.warning("[org_settings] organizations read failed: %s", e)
        row = None
    if row is None:
        with _lock:
            mode, seed, _ts = _cache
        if mode:
            return mode, seed
        return UNKNOWN, "default"
    new_mode = str(row.get("learning_mode") or "consolidated")
    if new_mode not in MODES:
        new_mode = "consolidated"
    new_seed = str(row.get("instance_seed") or "default")
    if new_seed not in SEEDS:
        new_seed = "default"
    with _lock:
        _cache = (new_mode, new_seed, now)
        _row_cache.clear()
        _row_cache.update(row)
    return new_mode, new_seed


def learning_mode() -> str:
    """'consolidated' | 'isolated' | 'unknown' (never read successfully)."""
    return refresh()[0]


def instance_seed() -> str:
    """'current' | 'default'."""
    return refresh()[1]


def row() -> dict:
    """The last successfully read organizations row ({} before any read). Refreshes
    on the same TTL as the mode."""
    refresh()
    with _lock:
        return dict(_row_cache)


def _int_col(r: dict, key: str) -> int:
    try:
        return max(0, int(r.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _float_col(r: dict, key: str) -> float:
    try:
        return max(0.0, float(r.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def max_dedicated_instances() -> int:
    """This org's dedicated-instance cap from the row; 0 = deployment default
    (brain/persona_placement.effective_max_dedicated applies BRAIN_MAX_DEDICATED)."""
    return _int_col(row(), "max_dedicated_instances")


def gpu_daily_usd_budget() -> float:
    """This org's daily standalone/org pod budget in USD; 0 = no standalone pods."""
    return _float_col(row(), "gpu_daily_usd_budget")


def set_gpu_daily_usd_budget(value: float) -> float:
    """Write the org's GPU budget (owner key / org admin). Raises OrgSettingsError
    when there is no backend or the column is missing (migration 038)."""
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise OrgSettingsError("gpu_daily_usd_budget must be a number") from e
    if v < 0 or math.isnan(v) or math.isinf(v):
        raise OrgSettingsError("gpu_daily_usd_budget must be >= 0")
    sb = _sb()
    if sb is None:
        raise OrgSettingsError("gpu_daily_usd_budget requires the Supabase storage backend")
    client, org = sb
    try:
        res = (
            client.table("organizations")
            .update({"gpu_daily_usd_budget": v})
            .eq("id", org)
            .execute()
        )
    except Exception as e:
        raise OrgSettingsError(
            f"organizations update failed ({e}) — apply migration 038_persona_placement_and_gpu"
        ) from e
    if not (res.data or []):
        raise OrgSettingsError("organizations row not found for this org")
    invalidate()
    refresh(force=True)
    return gpu_daily_usd_budget()


# ── gateway side: many orgs, service role ────────────────────────────────────
# The gateway serves every org, so it cannot use the process-wide cache above
# (that is keyed on supabase_client.get_org_id(), the tenant's own org). One small
# per-org cache, same TTL, same last-known-value rule; refreshed off the event
# loop by the reconciler (asyncio.to_thread) and read lock-free by the sync
# capacity check. Never raises.

_org_rows: dict[str, tuple[dict, float]] = {}


def read_org_row(org_id: str, client=None) -> dict | None:
    """Fetch one org's row by id under the service role. None on error."""
    if not org_id:
        return None
    try:
        if client is None:
            sb = _sb()
            if sb is None:
                return None
            client = sb[0]
        res = client.table("organizations").select("*").eq("id", org_id).limit(1).execute()
        rows = res.data or []
        return dict(rows[0]) if rows else {}
    except Exception as e:
        logger.debug("[org_settings] org row read failed for %s: %s", str(org_id)[:8], e)
        return None


def refresh_org_caps(org_id: str, client=None, force: bool = False) -> dict:
    """Read (or serve from the per-org cache) {max_dedicated_instances,
    gpu_daily_usd_budget} for an org the GATEWAY is managing. Blocking; call it
    from a thread. Returns the last-known caps on a read error, zeros when the org
    was never read."""
    now = time.time()
    with _lock:
        hit = _org_rows.get(org_id)
    if hit and not force and now - hit[1] < _TTL_S:
        return _caps_of(hit[0])
    r = read_org_row(org_id, client=client)
    if r is None:
        return _caps_of(hit[0]) if hit else _caps_of({})
    with _lock:
        _org_rows[org_id] = (r, now)
    return _caps_of(r)


def cached_org_caps(org_id: str) -> dict | None:
    """The gateway's lock-free, non-blocking view of an org's caps: None when the
    org has never been read (callers then fall back to the deployment defaults)."""
    with _lock:
        hit = _org_rows.get(org_id)
    return _caps_of(hit[0]) if hit else None


def _caps_of(r: dict) -> dict:
    return {
        "max_dedicated_instances": _int_col(r, "max_dedicated_instances"),
        "gpu_daily_usd_budget": _float_col(r, "gpu_daily_usd_budget"),
    }


def is_isolated() -> bool:
    """Fail-CLOSED predicate for the leak gates: True unless the org is known to be
    consolidated. An unread org withholds shared learning rather than leaking it."""
    return learning_mode() != "consolidated"


def is_isolated_known() -> bool:
    """Fail-OPEN predicate for refusals (persona ownership binding): True only when
    the org has been read and is isolated. An unread org never 404s a customer."""
    return learning_mode() == "isolated"


def set_learning_mode(mode: str, instance_seed: str | None = None) -> tuple[str, str]:
    """Write the mode (and optionally the seed) to the organizations row and refresh
    the cache. Raises OrgSettingsError when there is no backend or the update is
    refused (migration 037 not applied)."""
    if mode not in MODES:
        raise OrgSettingsError(f"learning_mode must be one of {MODES}")
    if instance_seed is not None and instance_seed not in SEEDS:
        raise OrgSettingsError(f"instance_seed must be one of {SEEDS}")
    sb = _sb()
    if sb is None:
        raise OrgSettingsError("learning_mode requires the Supabase storage backend")
    client, org = sb
    patch: dict = {"learning_mode": mode}
    if instance_seed is not None:
        patch["instance_seed"] = instance_seed
    try:
        res = client.table("organizations").update(patch).eq("id", org).execute()
    except Exception as e:
        raise OrgSettingsError(
            f"organizations update failed ({e}) — apply migration 037_org_learning_mode"
        ) from e
    if not (res.data or []):
        raise OrgSettingsError("organizations row not found for this org")
    invalidate()
    return refresh(force=True)


def set_instance_seed(seed: str) -> tuple[str, str]:
    """Change only the seeding policy (allowed in either mode)."""
    if seed not in SEEDS:
        raise OrgSettingsError(f"instance_seed must be one of {SEEDS}")
    sb = _sb()
    if sb is None:
        raise OrgSettingsError("instance_seed requires the Supabase storage backend")
    client, org = sb
    try:
        res = client.table("organizations").update({"instance_seed": seed}).eq("id", org).execute()
    except Exception as e:
        raise OrgSettingsError(
            f"organizations update failed ({e}) — apply migration 037_org_learning_mode"
        ) from e
    if not (res.data or []):
        raise OrgSettingsError("organizations row not found for this org")
    invalidate()
    return refresh(force=True)


def invalidate() -> None:
    """Drop the cache so the next read hits the row (tests, and after a write)."""
    global _cache
    with _lock:
        _cache = ("", "", 0.0)
        _row_cache.clear()
        _org_rows.clear()


def home_persona() -> str:
    """The org's home persona slug — the one persona that is never isolated from the
    org itself (it IS the org's agent). Env first (run.py sets it at boot), then
    settings; '' when neither is known."""
    import os

    from brain.persona_key import persona_slug

    home = persona_slug(os.environ.get("BRAIN_PERSONA_NAME", ""))
    if home:
        return home
    try:
        from brain.settings import settings

        return persona_slug(settings.get("persona_name", ""))
    except Exception:
        return ""


def is_home(persona: str) -> bool:
    from brain.persona_key import persona_slug

    slug = persona_slug(persona)
    return not slug or slug == home_persona()
