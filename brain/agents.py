"""
Agents — the first-class (persona, role) pairing.

Elsewhere "agent" = one fused system prompt. Here a persona is a durable identity
(chemistry/memory) and a role/mandate is a swappable job, so the PAIRING is the
agent. Each enabled row in the `agents` table (009_agents.sql; the assignment CRUD
lives in brain/mandates.py) is an agent:

    agent_id = "<persona_slug>.<mandate_id>"   (derived, not stored — both halves
               are dot-free slugs, so the composite is unambiguous)
    name     = optional display label
    permissions = a per-agent NARROWING of the org-level motor/operational ceiling

Two responsibilities:
  1. resolve(agent_id) — the engine API turns a partner's agent_id into
     (persona, mandate_id), verifying the agent is enabled and belongs to THIS
     process's persona (one process per (org, persona); a cross-persona agent is
     served by a different process).
  2. effective_permissions(org, agent) — fold the agent's optional restrictions
     INTO the org ceiling. The rule is always "more restrictive wins": an agent can
     only narrow within the org bounds, never widen past them. permissions={} is a
     no-op (companion/local mode behaves exactly as before).
"""

from __future__ import annotations

import logging

from brain.mandates import MandateError, _persona, _sb, _valid_id

logger = logging.getLogger(__name__)


class AgentNotFound(MandateError):
    """agent_id is malformed, unknown, or disabled → HTTP 404."""


class AgentPersonaMismatch(MandateError):
    """agent_id names a persona this process doesn't serve → HTTP 409.
    A different (org, persona) process serves it; the gateway routes there."""


# ── permission key groups + combine operators ─────────────────────────────────
# Each agent permission key names the SAME setting as brain/settings.py, so the
# resolver is a fold, not a translation. Grouped by how org+agent combine.

# Numeric ceilings → the tighter (smaller) wins.
_CAP_KEYS = (
    "ralph_max_total_attempts",
    "motor_max_concurrent_jobs",
    "motor_max_jobs_per_window",
    "motor_max_jobs_per_session",
    "cloud_daily_usd_budget",
    "bg_cloud_max_tokens_per_call",
    "local_max_concurrent",
    "cloud_max_concurrent",
    "bg_cloud_max_concurrent",
)
# Capability switches → AND (an agent may switch a capability OFF, never ON).
_FLAG_KEYS = (
    "motor_enable_shell",
    "motor_enable_network",
    "motor_enable_cloud_actions",
    "motor_user_writes",
    "motor_user_network",
    "motor_self_writes",
    "motor_self_network",
)
# Cloud grant level full > ro > off → the more restrictive wins.
_CLOUD_KEYS = ("motor_user_cloud", "motor_self_cloud")
# Filesystem roots → empty means NO access (fail closed); agent dirs must sit
# inside an org root (path containment), so the agent can only sub-scope.
_DIR_KEYS = ("motor_allowed_dirs", "motor_read_only_dirs")
# Allowlists → set intersection. Empty connectors = "all configured"; empty
# commands = the dispatcher's DEFAULT_COMMANDS (resolved before intersecting so an
# agent can't name a command the org's default set excludes).
_CONNECTOR_KEYS = ("motor_user_connectors", "motor_self_connectors")
_COMMAND_KEY = "motor_allowed_commands"

PERMISSION_KEYS = frozenset(
    _CAP_KEYS + _FLAG_KEYS + _CLOUD_KEYS + _DIR_KEYS + _CONNECTOR_KEYS + (_COMMAND_KEY,)
)

_CLOUD_RANK = {"off": 0, "ro": 1, "full": 2}


# ── resolution ────────────────────────────────────────────────────────────────


def resolve(agent_id: str) -> tuple[str, str]:
    """Map an agent_id ('persona.mandate') to (persona_slug, mandate_id).

    Raises MandateError if it is malformed, not an enabled agent, or belongs to a
    different persona than this process serves (cross-persona → a different
    process; the engine API maps that to 409)."""
    persona_slug, mandate_id = _split(agent_id)
    active = _persona("")
    if persona_slug != active:
        raise AgentPersonaMismatch(
            f"agent '{agent_id}' belongs to persona '{persona_slug}', but this "
            f"process serves '{active}'"
        )
    sb, org = _sb()
    res = (
        sb.table("agents")
        .select("mandate_id, enabled")
        .eq("org_id", org)
        .eq("persona", persona_slug)
        .eq("mandate_id", mandate_id)
        .eq("enabled", True)
        .execute()
    )
    if not (res.data or []):
        raise AgentNotFound(f"unknown or disabled agent '{agent_id}'")
    return persona_slug, mandate_id


def get(agent_id: str) -> dict | None:
    """Full agent row (incl. name + permissions) or None if it doesn't exist.
    Org-scoped (RLS), NOT restricted to this process's persona — agents are
    org-level data the admin/partner manages regardless of which persona the
    process is currently serving. Runtime persona-binding is resolve()'s job."""
    persona_slug, mandate_id = _split(agent_id)
    sb, org = _sb()
    res = (
        sb.table("agents")
        .select("persona, mandate_id, name, enabled, permissions, sort_order")
        .eq("org_id", org)
        .eq("persona", persona_slug)
        .eq("mandate_id", mandate_id)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    row = rows[0]
    row["agent_id"] = agent_id
    return row


def list_agents() -> list[dict]:
    """Every agent row for the org (all personas), with derived agent_id."""
    sb, org = _sb()
    res = (
        sb.table("agents")
        .select("persona, mandate_id, name, enabled, permissions, sort_order")
        .eq("org_id", org)
        .order("persona")
        .order("mandate_id")
        .execute()
    )
    out = []
    for r in res.data or []:
        r["agent_id"] = f"{r['persona']}.{r['mandate_id']}"
        out.append(r)
    return out


def permissions(agent_id: str) -> dict:
    """The agent's stored permission overrides ({} if none / unknown)."""
    row = get(agent_id)
    if not row:
        return {}
    p = row.get("permissions")
    return p if isinstance(p, dict) else {}


def set_name(agent_id: str, name: str | None) -> dict:
    sb, org = _sb()
    persona_slug, mandate_id = _split(agent_id)
    sb.table("agents").update({"name": (name or None)}).eq("org_id", org).eq(
        "persona", persona_slug
    ).eq("mandate_id", mandate_id).execute()
    return get(agent_id) or {}


def set_permissions(agent_id: str, perms: dict) -> dict:
    """Store an agent's permission overrides. Only known keys are kept; values are
    not required to be tighter than the org — effective_permissions enforces that
    at read time, so a stale/looser stored value can never actually widen."""
    sb, org = _sb()
    persona_slug, mandate_id = _split(agent_id)
    clean = _clean_permissions(perms)
    sb.table("agents").update({"permissions": clean}).eq("org_id", org).eq(
        "persona", persona_slug
    ).eq("mandate_id", mandate_id).execute()
    return get(agent_id) or {}


# ── bounded permission resolution (org ceiling ∩ agent narrowing) ─────────────


def effective_permissions(org: dict, agent: dict | None) -> dict:
    """Fold an agent's restrictions into the org ceiling. Returns the effective
    value for every managed key. 'More restrictive wins'; an unset agent key
    inherits the org value; an empty agent dict reproduces org exactly."""
    agent = agent or {}
    out: dict = {}
    for k in _CAP_KEYS:
        out[k] = _combine_cap(org.get(k), agent.get(k))
    for k in _FLAG_KEYS:
        out[k] = _combine_flag(org.get(k), agent.get(k))
    for k in _CLOUD_KEYS:
        out[k] = _combine_cloud(org.get(k), agent.get(k))
    for k in _DIR_KEYS:
        out[k] = _combine_dirs(org.get(k), agent.get(k))
    for k in _CONNECTOR_KEYS:
        out[k] = _combine_list(org.get(k), agent.get(k))
    out[_COMMAND_KEY] = _combine_list(
        org.get(_COMMAND_KEY), agent.get(_COMMAND_KEY), default=_default_commands()
    )
    return out


def _combine_cap(org_v, agent_v):
    if agent_v is None or agent_v == "":
        return org_v
    if org_v is None or org_v == "":
        return agent_v
    try:
        return type(org_v)(min(float(org_v), float(agent_v)))
    except (TypeError, ValueError):
        return org_v


def _combine_flag(org_v, agent_v):
    o = 1 if _truthy(org_v) else 0
    if agent_v is None or agent_v == "":
        return o
    return 1 if (o and _truthy(agent_v)) else 0


def _combine_cloud(org_v, agent_v):
    o = _CLOUD_RANK.get(str(org_v or "off"), 0)
    if agent_v is None or agent_v == "":
        rank = o
    else:
        rank = min(o, _CLOUD_RANK.get(str(agent_v), 0))
    return {0: "off", 1: "ro", 2: "full"}[rank]


def _combine_dirs(org_v, agent_v):
    """Newline-joined roots. Empty = no access. The agent may only sub-scope: keep
    agent roots that resolve inside an org root."""
    org_dirs = _lines(org_v)
    if agent_v is None or str(agent_v).strip() == "":
        return "\n".join(org_dirs)
    import os

    agent_dirs = _lines(agent_v)
    org_norm = [os.path.realpath(os.path.expanduser(d)) for d in org_dirs]
    kept = []
    for d in agent_dirs:
        rd = os.path.realpath(os.path.expanduser(d))
        if any(rd == o or rd.startswith(o.rstrip("/") + "/") for o in org_norm):
            kept.append(d)
    return "\n".join(kept)


def _combine_list(org_v, agent_v, default: list[str] | None = None):
    """Allowlist intersection. Empty org = `default` if given else 'no extra
    restriction' (the agent set passes through). Empty agent = inherit org."""
    org_set = _lines(org_v)
    if agent_v is None or str(agent_v).strip() == "":
        return "\n".join(org_set)
    agent_set = _lines(agent_v)
    if not org_set:
        base = default if default is not None else agent_set
        inter = [c for c in agent_set if c in base] if default is not None else agent_set
        return "\n".join(inter)
    inter = [c for c in agent_set if c in org_set]
    return "\n".join(inter)


# ── internals ─────────────────────────────────────────────────────────────────


def _split(agent_id: str) -> tuple[str, str]:
    s = str(agent_id or "").strip()
    if "." not in s:
        raise AgentNotFound("agent_id must be '<persona>.<mandate_id>'")
    persona_slug, mandate_id = s.split(".", 1)
    if not persona_slug:
        raise AgentNotFound("agent_id missing persona")
    return persona_slug, _valid_id(mandate_id)


def _clean_permissions(perms: dict | None) -> dict:
    if not isinstance(perms, dict):
        raise MandateError("permissions must be a JSON object")
    return {k: v for k, v in perms.items() if k in PERMISSION_KEYS}


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _lines(v) -> list[str]:
    return [ln.strip() for ln in str(v or "").splitlines() if ln.strip()]


def _default_commands() -> list[str]:
    try:
        from brain.clusters.motor_dispatcher import DEFAULT_COMMANDS

        return list(DEFAULT_COMMANDS)
    except Exception:
        return []
