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
"""

from __future__ import annotations

import os
import time

DEFAULT_TTL_S = 30 * 86400


def mint_org_token(org_id: str, ttl_s: int = DEFAULT_TTL_S) -> str:
    """Return a signed JWT for this org, or "" when SUPABASE_JWT_SECRET is unset
    (local dev without Supabase auth — callers fall back to the service key)."""
    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if not secret or not org_id:
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
