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

ASYMMETRIC SIGNING: this HS256 self-signing only works while the project accepts
the legacy shared secret. Once a project migrates to Supabase's asymmetric JWT
signing keys (ES256/RSA — now the default), the shared secret is inert and any
HS256 token we mint is REJECTED by PostgREST → the tenant's every DB call 401s →
the brain can't initialize and never boots. We can't mint an asymmetric token
(only Supabase holds the private key), so when the project signs asymmetrically
we return "" and let the caller (provisioner) keep the service-role key. Tenant
isolation then rests on the storage layer's in-query org scoping (every query
filters `org_id = <this org>`), which is already enforced independently of RLS.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 30 * 86400

# Cached tri-state: None = not yet checked, True/False = project signs asymmetrically.
_asymmetric: bool | None = None


def _uses_asymmetric_signing() -> bool:
    """True if the project publishes asymmetric JWKS keys (ES256/RSA), meaning an
    HS256 token signed with the legacy secret would be rejected. Cached; a JWKS
    that's unreachable is treated as legacy (preserve the HS256 path)."""
    global _asymmetric
    if _asymmetric is not None:
        return _asymmetric
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not base:
        return False
    try:
        req = urllib.request.Request(
            f"{base}/auth/v1/.well-known/jwks.json",
            headers={"apikey": os.environ.get("SUPABASE_ANON_KEY", "")},
        )
        with urllib.request.urlopen(req, timeout=5) as r:  # nosec B310 - fixed https Supabase URL
            keys = json.loads(r.read()).get("keys", [])
        _asymmetric = any((k or {}).get("kty") in ("EC", "RSA") for k in keys)
    except Exception as e:
        logger.warning("[org_token] JWKS probe failed (%s) — assuming legacy HS256", e)
        _asymmetric = False
    return _asymmetric


def mint_org_token(org_id: str, ttl_s: int = DEFAULT_TTL_S) -> str:
    """Return a signed JWT for this org, or "" when one can't be minted — no secret
    (local dev) OR the project signs asymmetrically (the secret is inert; callers
    fall back to the service key)."""
    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if not secret or not org_id:
        return ""
    if _uses_asymmetric_signing():
        return ""
    import jwt

    now = int(time.time())
    return jwt.encode(
        {
            "sub": org_id,
            "role": "authenticated",
            "aud": "authenticated",
            "iss": "brain-gateway",
            "iat": now,
            "exp": now + int(ttl_s),
        },
        secret,
        algorithm="HS256",
    )
