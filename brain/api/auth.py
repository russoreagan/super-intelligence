"""
Runtime API-key auth for the engine surface.

A partner's backend authenticates with a bearer key — the *runtime* credential
(distinct from the admin console login). It can open sessions and run turns, but
nothing else. Fail-closed: if no keys are configured, every request is denied, so
an accidentally-exposed server is not open by default.

Two key kinds:
  • The ORG OWNER key — BRAIN_API_KEYS / BRAIN_API_KEY env or the ``api_keys``
    setting. Constant-time compare. Full access (partner_id = None, owner = True).
    Per-tenant, so it is NOT resolvable at the multi-tenant gateway.
  • TABLE keys — rows in the ``api_keys`` table (011), each mapped to a partner_id
    and a role. Only the SHA-256 hash is stored; the token is shown once at mint.
    role='partner' is scoped (a partner only drives what it created); role='owner'
    is an owner-grade credential that, unlike the env key, works through the gateway.

``resolve_partner`` returns the partner context for a bearer token; ``check_bearer``
is the boolean gate built on it. ``resolve_key_context`` is the gateway-side variant
that resolves across orgs.

Three outcomes, never conflated: a context (known caller), None (no such key), and
AuthBackendError (the store is down, so identity is unknown → 503).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets


class AuthBackendError(RuntimeError):
    """The key store could not be reached, so the caller's identity is UNKNOWN.

    Distinct from "no such key" (None). Callers must map this to a 503 and never to
    a decision about who the caller is — treating a backend blip as "not a partner"
    is how a fail-closed design turns fail-open."""


def configured_keys() -> set[str]:
    raw = os.environ.get("BRAIN_API_KEYS") or os.environ.get("BRAIN_API_KEY") or ""
    if not raw:
        try:
            from brain.settings import settings

            raw = str(settings.get("api_keys", "") or "")
        except Exception:
            raw = ""
    return {k.strip() for k in raw.split(",") if k.strip()}


def _extract_token(authorization: str | None) -> str | None:
    """The token from an ``Authorization: Bearer <token>`` header, or None.

    Bearer only. A bare token with no scheme (and anything under another scheme) is
    rejected: accepting arbitrary header values as credentials widens what counts as
    a credential-bearing request and encourages clients to put secrets in
    oddly-shaped headers."""
    if not authorization:
        return None
    auth = authorization.strip()
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _matches_an_owner_key(token: str, owner_keys: set[str]) -> bool:
    """Constant-time membership. ``token in owner_keys`` compares with ``==``, whose
    early exit leaks a prefix-length timing signal; and iterating with a plain ``or``
    would short-circuit on the first hit, leaking position. Compare against every key
    and OR the results without branching."""
    hit = False
    for k in owner_keys:
        hit |= hmac.compare_digest(token, k)
    return hit


def resolve_partner(authorization: str | None) -> dict | None:
    """Return the caller's partner context, or None if the token is invalid.

    {"partner_id": str | None, "owner": bool}. The org owner's key → owner=True,
    partner_id=None (full access). A per-partner table key → owner=False with its
    partner_id, unless the row carries role='owner' (an owner-grade table key, which
    unlike the env key IS resolvable through the hosted gateway).

    Fail-closed: unknown token → None. Raises AuthBackendError if the key store is
    unreachable — an unknown identity is not the same as a known non-owner."""
    token = _extract_token(authorization)
    if not token:
        return None
    owner_keys = configured_keys()
    if owner_keys and _matches_an_owner_key(token, owner_keys):
        return {"partner_id": None, "owner": True, "allowed_agents": None, "key_id": None}
    row = _lookup_partner_key(token)
    if row:
        is_owner = str(row.get("role") or "partner") == "owner"
        return {
            "partner_id": row.get("partner_id"),
            "owner": is_owner,
            # None = unrestricted (every pre-036 row); a list pins the key to those
            # agent ids (POST /v1/sessions + the agent/persona listings filter on it).
            "allowed_agents": _allowed_agents(row),
            "key_id": row.get("id"),
        }
    return None


def _allowed_agents(row: dict) -> list[str] | None:
    """The key's agent allowlist as a list of ids, or None for unrestricted. A row
    from before migration 036 has no column and reads as None — today's scope."""
    raw = row.get("allowed_agents")
    if raw is None:
        return None
    if isinstance(raw, str):  # a text[] can come back serialised on some clients
        raw = [s for s in raw.strip("{}").split(",") if s]
    if not isinstance(raw, list):
        return None
    return [str(a).strip() for a in raw if str(a).strip()]


def _lookup_partner_key(token: str) -> dict | None:
    """The api_keys row for a token, or None when there is no such active key.

    Raises AuthBackendError when the lookup itself fails. Swallowing that and
    returning None told callers "not a partner" during a Supabase blip, which
    _require then upgraded to full org owner."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return None
    try:
        client = supabase_client.get_client()
        org = supabase_client.get_org_id()
        res = (
            client.table("api_keys")
            # "*" rather than naming `role`: PostgREST errors on an unknown column, so
            # naming it would turn EVERY auth lookup into a 503 on any deployment that
            # ships this code before migration 028 lands. With "*" a pre-migration row
            # simply has no role and reads as 'partner' — the safe direction.
            .select("*")
            .eq("org_id", org)
            .eq("key_hash", _hash(token))
            .eq("active", True)
            .execute()
        )
    except Exception as e:
        raise AuthBackendError(str(e)) from e
    rows = res.data or []
    return rows[0] if rows else None


def resolve_key_context(authorization: str | None) -> dict | None:
    """GATEWAY-side: map a bearer token to its org, partner and role, ACROSS all orgs
    — so the multi-tenant gateway can route a request to the right brain AND decide
    whether the caller may drive org-wide controls like /v1/sleep.

    Returns {"org_id": str, "partner_id": str | None, "role": "owner"|"partner"}, or
    None for an unknown/inactive token. Requires service-role Supabase (the gateway
    has it). Raises AuthBackendError if the lookup fails.

    Per-partner table keys only — env owner keys (BRAIN_API_KEYS) are per-tenant and
    invisible here, which is why an org that needs an API-reachable owner credential
    mints a table key with role='owner'."""
    token = _extract_token(authorization)
    if not token:
        return None
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return None
    try:
        client = supabase_client.get_client()
        res = (
            client.table("api_keys")
            # See _lookup_partner_key: "*" keeps this working before migration 028.
            .select("*")
            .eq("key_hash", _hash(token))
            .eq("active", True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise AuthBackendError(str(e)) from e
    rows = res.data or []
    if not rows:
        return None
    row = rows[0]
    return {
        "org_id": row["org_id"],
        "partner_id": row.get("partner_id"),
        "role": str(row.get("role") or "partner"),
        "allowed_agents": _allowed_agents(row),
        "key_id": row.get("id"),
    }


def has_any_api_keys() -> bool:
    """BRAIN-side (org-scoped): does THIS org have any active per-partner key? Lets a
    tenant brain decide to start its engine API server even when no owner env key is
    set (the multi-tenant B2B path keys live in the api_keys table, not env)."""
    if configured_keys():
        return True
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return False
        client = supabase_client.get_client()
        org = supabase_client.get_org_id()
        res = (
            client.table("api_keys")
            .select("id")
            .eq("org_id", org)
            .eq("active", True)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:
        return False


def check_bearer(authorization: str | None) -> bool:
    """True iff the Authorization header carries a valid owner or partner key.
    Fail-closed: no match → False. Propagates AuthBackendError rather than reporting
    False on a backend blip, so callers can tell "denied" from "cannot tell"."""
    return resolve_partner(authorization) is not None


# ── key management (owner-only; used by the engine API mint/revoke routes) ─────


KEY_ROLES = ("partner", "owner")


MAX_ALLOWED_AGENTS = 200

# Allowlist PINS (plan §3.7): one entry that stands for a family of agents, so a
# marketplace partner key covers every clone of a template without re-minting per
# purchase. Grammar, alongside the plain '<persona>.<mandate_id>' id:
#   template:<slug>              every persona whose spec `template` is <slug>, any mandate
#   template:<slug>.<mandate>    ...restricted to that mandate ('*' = any)
#   prefix:<p>                   every persona whose slug starts with <p>, any mandate
# A pin is one entry against MAX_ALLOWED_AGENTS. The template itself is NOT covered
# by its template pin (it has no `template`); list it by id if the key needs it.
PIN_TEMPLATE = "template:"
PIN_PREFIX = "prefix:"
_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


def parse_pin(entry: str) -> tuple[str, str, str | None] | None:
    """('template', slug, mandate|None) or ('prefix', p, None) for a pin entry;
    None for a plain agent id. Does NOT validate — _clean_allowed_agents does, at
    mint; a stored entry that fails to parse here simply never matches."""
    s = str(entry or "").strip()
    if s.startswith(PIN_TEMPLATE):
        rest = s[len(PIN_TEMPLATE) :]
        slug, _, mandate = rest.partition(".")
        return ("template", slug, (mandate or None))
    if s.startswith(PIN_PREFIX):
        return ("prefix", s[len(PIN_PREFIX) :], None)
    return None


def _clean_pin(entry: str) -> str:
    """Validate one pin entry (see parse_pin); returns it normalised."""
    from brain.ids import SLUG_RE
    from brain.mandates import _valid_id

    kind, value, mandate = parse_pin(entry)  # type: ignore[misc]
    if kind == "prefix":
        if mandate is not None or not _PREFIX_RE.match(value):
            raise ValueError(f"allowed_agents: bad prefix pin {entry!r}")
        return f"{PIN_PREFIX}{value}"
    if not SLUG_RE.match(value):
        raise ValueError(f"allowed_agents: bad template pin {entry!r}")
    if mandate is None or mandate == "*":
        return f"{PIN_TEMPLATE}{value}"
    try:
        mandate = _valid_id(mandate)
    except Exception as e:
        raise ValueError(f"allowed_agents: bad template pin {entry!r}: {e}") from e
    return f"{PIN_TEMPLATE}{value}.{mandate}"


def _clean_allowed_agents(raw: object) -> list[str] | None:
    """Validate a mint-time allowlist: None/absent = unrestricted; else a list of
    well-formed '<persona>.<mandate_id>' ids and/or pins (template:<slug>[.<mandate>],
    prefix:<p>), deduped, order kept. Existence is NOT checked — a key may be
    minted before its agents are (the same way an agent id in POST /v1/sessions is
    resolved at open time, not at mint). Each pin counts as one entry."""
    if raw is None:
        return None
    if not isinstance(raw, list) or any(not isinstance(a, str) for a in raw):
        raise ValueError("allowed_agents must be a list of agent ids")
    from brain.agents import AgentNotFound, _split

    out: list[str] = []
    for a in raw:
        a = a.strip()
        if not a:
            continue
        if parse_pin(a) is not None:
            a = _clean_pin(a)
        else:
            try:
                _split(a)
            except AgentNotFound as e:
                raise ValueError(f"allowed_agents: {e}") from e
        if a not in out:
            out.append(a)
    if len(out) > MAX_ALLOWED_AGENTS:
        raise ValueError(f"allowed_agents exceeds {MAX_ALLOWED_AGENTS} entries")
    return out


def mint_partner_key(
    partner_id: str,
    label: str | None = None,
    role: str = "partner",
    allowed_agents: list[str] | None = None,
) -> dict:
    """Create a per-partner key. Returns {id, partner_id, role, allowed_agents, token}
    — the plaintext ``token`` is shown ONCE and never stored (only its hash is).
    Requires Supabase.

    ``role='owner'`` mints an owner-grade key. Unlike the env owner key it lives in
    api_keys, so it resolves through the hosted gateway — this is how an org gets a
    credential that can call owner-gated routes on api.elyceum.app at all. Minting
    one is itself owner-gated at every call site.

    ``allowed_agents`` (migration 036) pins a partner key to a list of agent ids:
    sessions may only open on those agents and the agent/persona listings are
    filtered to them. None = unrestricted. Refused on an owner-grade key (an owner
    is by definition unrestricted)."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        raise RuntimeError("per-partner keys require the Supabase storage backend")
    pid = str(partner_id or "").strip()
    if not pid:
        raise ValueError("partner_id required")
    role = str(role or "partner").strip().lower()
    if role not in KEY_ROLES:
        raise ValueError(f"role must be one of {', '.join(KEY_ROLES)}")
    allowed = _clean_allowed_agents(allowed_agents)
    if allowed is not None and role == "owner":
        raise ValueError("allowed_agents cannot be set on an owner key")
    if allowed is not None and not allowed:
        raise ValueError("allowed_agents must name at least one agent (omit it for all)")
    token = "sk_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(8)
    client = supabase_client.get_client()
    org = supabase_client.get_org_id()
    # allowed_agents is only named when a list was supplied: an unrestricted mint
    # must keep working on a deployment that has not applied migration 036 yet.
    client.table("api_keys").insert(
        {
            "org_id": org,
            "id": key_id,
            "key_hash": _hash(token),
            "partner_id": pid,
            "label": label,
            "active": True,
            "role": role,
            **({"allowed_agents": allowed} if allowed is not None else {}),
        }
    ).execute()
    return {
        "id": key_id,
        "partner_id": pid,
        "label": label,
        "role": role,
        "allowed_agents": allowed,
        "token": token,
    }


_KEY_META_FIELDS = ("id", "partner_id", "label", "active", "role", "created_ts", "allowed_agents")


def list_partner_keys() -> list[dict]:
    """Key metadata (never the token/hash) for the org. select("*") then project:
    naming `allowed_agents` in the select would 503 every listing on a deployment
    that has not applied migration 036."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return []
    client = supabase_client.get_client()
    org = supabase_client.get_org_id()
    res = client.table("api_keys").select("*").eq("org_id", org).order("created_ts").execute()
    out = []
    for r in res.data or []:
        row = {k: r.get(k) for k in _KEY_META_FIELDS}
        row["allowed_agents"] = _allowed_agents(r)
        out.append(row)
    return out


def revoke_partner_key(key_id: str) -> bool:
    """Deactivate a key by its public id. Returns False if not found."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return False
    client = supabase_client.get_client()
    org = supabase_client.get_org_id()
    existing = client.table("api_keys").select("id").eq("org_id", org).eq("id", key_id).execute()
    if not (existing.data or []):
        return False
    client.table("api_keys").update({"active": False}).eq("org_id", org).eq("id", key_id).execute()
    return True
