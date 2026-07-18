"""
Gateway-minted org JWTs — the tenant brain's database credential.

A tenant process must never hold the Supabase service-role key (a compromised
tenant would own every org's data). Instead the gateway, which does hold the
service key + JWT secret, mints a scoped token per tenant at spawn:

    sub  = org_id          → auth.uid() in Postgres IS the org id
    role = authenticated   → PostgREST maps the request to the RLS-governed role

The brain then connects with the anon key + this token, and the 007 policies
(`auth.uid() = org_id`) enforce tenancy in the database itself — a tenant that
asks for another org's rows gets nothing, no matter what its code does.

Long-lived (default 30 days) because a tenant process can outlive any user
session; the reaper + respawn cycle naturally rotates it well before expiry.

WHY A PROBE (and not a JWKS heuristic): this HS256 self-signing only works while
the project still ACCEPTS the legacy shared secret. Supabase's asymmetric JWT
signing keys (ES256/RSA) can be added as a *standby/current signer* while the
legacy HS256 secret stays in the verification set — in which case our HS256
tokens are still accepted and RLS works. The presence of an asymmetric key in the
JWKS therefore does NOT mean HS256 is dead; the only thing that settles it is
whether a token we sign is actually accepted. So we test exactly that: mint a
throwaway token and ask PostgREST. If accepted → mint for real (RLS enforced). If
rejected (or anything is uncertain) → return "" and let the caller (provisioner)
keep the service-role key, so a tenant never boots on a credential Supabase 401s.
Set BRAIN_DISABLE_ORG_JWT=1 to force the service-key path (instant kill-switch).
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 30 * 86400
# A deliberately non-existent table: PostgREST verifies the JWT *before* routing,
# so a validly-signed token yields 404 (table not found) while a bad signature
# yields 401 — a table-/RLS-independent acceptance signal.
_PROBE_PATH = "/rest/v1/__org_token_probe__?select=x&limit=1"
_PROBE_SUB = "00000000-0000-0000-0000-000000000000"

# Cached tri-state: None = not yet probed; True/False = will the project accept a
# token we sign?
_mintable: bool | None = None


def _kill_switch() -> bool:
    return os.environ.get("BRAIN_DISABLE_ORG_JWT", "").strip().lower() in ("1", "true", "yes")


def _sign(secret: str, sub: str, ttl_s: int) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "role": "authenticated",
            "aud": "authenticated",
            "iss": "brain-gateway",
            "iat": now,
            "exp": now + int(ttl_s),
        },
        secret,
        algorithm="HS256",
    )


def _token_accepted(secret: str) -> bool:
    """Mint a throwaway HS256 token and ask PostgREST whether it accepts it. True
    ONLY on a clear positive; False on rejection (401), missing config, or any
    error — fail safe to the service-role key rather than boot a tenant on a
    credential Supabase would 401."""
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    if not base or not anon:
        return False
    probe = _sign(secret, _PROBE_SUB, 300)
    try:
        req = urllib.request.Request(
            f"{base}{_PROBE_PATH}",
            headers={"apikey": anon, "Authorization": f"Bearer {probe}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:  # nosec B310 - fixed https Supabase URL
            return 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        # 401 = the JWT could not be decoded (signature rejected → HS256 inert).
        # Any other status (404 for the missing probe table, etc.) means the token
        # decoded and auth passed → it's mintable.
        return e.code != 401
    except Exception as e:
        logger.warning("[org_token] token-acceptance probe failed (%s) — using the service key", e)
        return False


def _can_mint(secret: str) -> bool:
    """Cached: is an HS256 token signed with our secret accepted by the project?
    Logs the verdict once, loudly — it decides whether RLS is the enforcing layer
    or whether tenants fall back to the service-role key."""
    global _mintable
    if _mintable is not None:
        return _mintable
    _mintable = _token_accepted(secret)
    if _mintable:
        logger.warning(
            "[org_token] org-JWT minting ENABLED — a self-signed HS256 token is accepted; "
            "tenants boot RLS-scoped (auth.uid() = org_id)."
        )
    else:
        logger.warning(
            "[org_token] org-JWT minting DISABLED — a self-signed HS256 token was rejected; "
            "tenants fall back to the SERVICE-ROLE key and RLS is bypassed (in-query scoping only)."
        )
    return _mintable


def mint_org_token(org_id: str, ttl_s: int = DEFAULT_TTL_S) -> str:
    """Return a signed org JWT for this org, or "" when one can't/shouldn't be
    minted — no secret / no org (local dev), the BRAIN_DISABLE_ORG_JWT kill-switch
    is set, or the project would reject a token we sign. Empty → the caller keeps
    the service-role key."""
    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if not secret or not org_id:
        return ""
    if _kill_switch():
        return ""
    if not _can_mint(secret):
        return ""
    return _sign(secret, org_id, ttl_s)
