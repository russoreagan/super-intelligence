"""Org-wide permission ceilings — the one place that knows which settings keys are
admin-only and how to read, sanitize and write them.

Two surfaces edit the same keys: the console's Account limits page (brain/ui/
server.py POST /settings) and the owner-key API (GET/PUT /v1/org/permissions). Both
route through here so they cannot disagree about which keys are governance and
which are ordinary preferences. The keys are exactly brain/agents.PERMISSION_KEYS
(the ceilings every agent's `permissions` narrows) plus the org-wide operational
and privacy switches that act on every persona and customer of the org:
partner_cloud_daily_usd_budget, dmn_enabled, engine_lane_scoping, self_model_deid,
persona_ownership_binding, the read-path content policy (content_read_policy,
content_read_audit, content_read_audit_window_s) and the org-wide DMN levers
(dmn_isolated_roster, dmn_active_roster_days, dmn_pause_after_idle_s). (The
learning mode itself is NOT a settings key — it lives on the organizations row;
see brain/org_settings.py.)

Filesystem roots are jailed to the tenant's own volume on write (the helpers used
to live in the UI server; brain/security.jail_dirs_to_tenant_root re-enforces the
same boundary at session bake time against symlink swaps).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class OrgPermissionsError(ValueError):
    """A write named a key that is not a ceiling, or a value that cannot be coerced."""


def _admin_only_keys() -> frozenset[str]:
    from brain.agents import PERMISSION_KEYS

    return frozenset(PERMISSION_KEYS) | {
        "partner_cloud_daily_usd_budget",
        "dmn_enabled",
        # Org-wide privacy switches (2026-09): they act on every persona and
        # customer of the org, so only an owner / org admin may flip them.
        "engine_lane_scoping",
        "self_model_deid",
        "persona_ownership_binding",
        # Read-path content policy (brain/read_policy.py). Until 2026-09-13 these
        # were ordinary preferences, so any org member could POST
        # content_read_policy=0 and switch off the gate that keeps a buyer's
        # conversation from the partner's staff.
        "content_read_policy",
        "content_read_audit",
        "content_read_audit_window_s",
        # Org-wide DMN levers: which personas think when idle and for how long —
        # i.e. what burns GPU on the org's account (brain/dmn.py).
        "dmn_isolated_roster",
        "dmn_active_roster_days",
        "dmn_pause_after_idle_s",
    }


ADMIN_ONLY_KEYS: frozenset[str] = _admin_only_keys()

_DIR_KEYS = ("motor_allowed_dirs", "motor_read_only_dirs")


# ── tenant jail (moved from brain/ui/server.py) ───────────────────────────────


def tenant_root() -> Path | None:
    """The pod's own tenant directory — the boundary org-admin filesystem grants
    are jailed to. settings.json lives at the org root (BRAIN_SETTINGS_PATH); fall
    back to the grandparent of the persona-namespaced SECOND_BRAIN_PATH. Returns
    None when it can't be resolved (callers then fail closed)."""
    sp = os.environ.get("BRAIN_SETTINGS_PATH", "").strip()
    if sp:
        try:
            return Path(sp).resolve().parent
        except Exception:
            return None
    sb = os.environ.get("SECOND_BRAIN_PATH", "").strip()
    if sb:
        try:
            p = Path(sb).resolve()
            return p.parent.parent if p.parent.name == "personas" else p
        except Exception:
            return None
    return None


def within_root(path: str, root: Path | None) -> bool:
    """True iff ``path`` resolves inside the tenant root. Fail closed (deny) when
    the root is unknown — better to reject a new grant than to leak one."""
    if root is None:
        return False
    try:
        rp = Path(path).resolve()
    except Exception:
        return False
    return rp == root or str(rp).startswith(str(root) + os.sep)


def jail_motor_dirs(body: dict) -> list[str]:
    """Confine an org-admin's filesystem grants to their own tenant root. Mutates
    ``body`` in place: keeps each path that is inside the tenant root OR already
    stored (a platform super-admin may have set out-of-jail roots on a self-hosted
    box — those are grandfathered); drops the rest and returns them. Defence in
    depth so a tenant can't point the motor cortex at the host or another pod's
    volume."""
    if not any(k in body for k in _DIR_KEYS):
        return []
    from brain.settings import settings as _settings

    root = tenant_root()
    dropped_all: list[str] = []
    for k in _DIR_KEYS:
        if k not in body:
            continue
        grandfathered = {
            ln.strip() for ln in str(_settings.get(k) or "").splitlines() if ln.strip()
        }
        kept, dropped = [], []
        for ln in str(body.get(k) or "").splitlines():
            p = ln.strip()
            if not p:
                continue
            (kept if (p in grandfathered or within_root(p, root)) else dropped).append(p)
        body[k] = "\n".join(kept)
        if dropped:
            dropped_all.extend(dropped)
            logger.warning(
                "[settings] org-admin filesystem path(s) outside tenant root %s dropped: %s",
                root,
                dropped,
            )
    return dropped_all


# ── read / sanitize / write ───────────────────────────────────────────────────


def read() -> dict:
    """Every ceiling key with its current org value."""
    from brain.settings import settings

    return {k: settings.get(k) for k in sorted(ADMIN_ONLY_KEYS)}


def sanitize(body: dict) -> tuple[dict, list[str]]:
    """Validate a partial ceilings update: only ceiling keys (anything else is an
    OrgPermissionsError — an API caller must not learn by silent drop that a key
    was ignored), values coerced to the declared settings type (bool → 0/1),
    filesystem roots jailed. Returns (clean, dropped_paths)."""
    from brain.settings import DEFAULTS

    if not isinstance(body, dict):
        raise OrgPermissionsError("body must be a JSON object")
    unknown = sorted(k for k in body if k not in ADMIN_ONLY_KEYS)
    if unknown:
        raise OrgPermissionsError(f"not a permission ceiling: {', '.join(unknown)}")
    clean: dict = {}
    for k, v in body.items():
        if k not in DEFAULTS:
            raise OrgPermissionsError(f"{k} is not a declared setting")
        typ = type(DEFAULTS[k])
        try:
            if isinstance(v, bool):
                v = int(v)
            if isinstance(v, list) and typ is str:
                v = "\n".join(str(x).strip() for x in v if str(x).strip())
            clean[k] = typ(v)
        except (TypeError, ValueError) as e:
            raise OrgPermissionsError(f"{k}: cannot coerce {v!r} to {typ.__name__}") from e
    dropped = jail_motor_dirs(clean)
    return clean, dropped


def write(body: dict) -> dict:
    """Apply a sanitized partial update to the org's settings.json and return the
    full ceilings plus any dropped filesystem paths. Invalidates the per-agent
    answer_only cache so the org switch takes effect on the next turn."""
    from brain.settings import settings

    clean, dropped = sanitize(body)
    if clean:
        settings.save(clean)
        try:
            from brain import agents

            agents._answer_only_cache.clear()
        except Exception:  # pragma: no cover - cache is best-effort
            pass
    return {"permissions": read(), "dropped_paths": dropped}
