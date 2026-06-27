"""
Provision an invite-only Elyceum account.

The UI has no public sign-up — accounts are created here (or in the Supabase
dashboard) using the service-role key. The user is created already-confirmed so
they can sign in immediately, and a personal org + admin membership are created
so they can actually log in.

Usage:
    python -m scripts.create_user EMAIL [PASSWORD] [--admin]

If PASSWORD is omitted, a strong one is generated and printed once.
Pass --admin to set the is_admin flag (grants access to the API workspace).

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment (.env).
"""

from __future__ import annotations

import secrets
import sys

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import os


def _headers(service_key: str) -> dict:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    args = [a for a in argv if a != "--admin"]
    is_admin = "--admin" in argv

    email = args[0].strip()
    password = args[1] if len(args) > 1 else secrets.token_urlsafe(16)
    generated = len(args) <= 1

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not service_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.", file=sys.stderr)
        return 1

    hdrs = _headers(service_key)

    # 1. Create the auth user.
    user_payload: dict = {"email": email, "password": password, "email_confirm": True}
    if is_admin:
        user_payload["app_metadata"] = {"is_admin": True}

    resp = httpx.post(
        f"{url}/auth/v1/admin/users",
        headers=hdrs,
        json=user_payload,
        timeout=15.0,
    )

    if resp.status_code not in (200, 201):
        print(f"ERROR: failed to create user (status {resp.status_code}):", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        return 1

    user = resp.json()
    user_id = user["id"]

    # 2. Create a personal org (id == user_id, same pattern as migration 006 seed).
    resp2 = httpx.post(
        f"{url}/rest/v1/organizations",
        headers=hdrs,
        json={"id": user_id, "name": f"{email} (personal)", "plan": "platform"},
        timeout=15.0,
    )
    if resp2.status_code not in (200, 201):
        print(f"ERROR: failed to create org (status {resp2.status_code}):", file=sys.stderr)
        print(resp2.text, file=sys.stderr)
        return 1

    # 3. Add user as admin member of their org.
    resp3 = httpx.post(
        f"{url}/rest/v1/memberships",
        headers=hdrs,
        json={"user_id": user_id, "org_id": user_id, "role": "admin"},
        timeout=15.0,
    )
    if resp3.status_code not in (200, 201):
        print(f"ERROR: failed to create membership (status {resp3.status_code}):", file=sys.stderr)
        print(resp3.text, file=sys.stderr)
        return 1

    # 4. Seed the default personas' starting self-models (their "sense of self") so
    #    the new org has the full roster from first login — same content every other
    #    org has. Without this the personas boot with only a bare ensure_self_schema
    #    stub (or nothing at all). Non-fatal: the account is already usable, and the
    #    seed is idempotent, so a failure here can be retried with the printed command.
    try:
        from scripts.seed_persona_selfmd import seed_org

        n = seed_org(user_id, url, service_key)
        print(f"  ✓ seeded {n} persona self-models")
    except Exception as e:
        print(f"  ! WARNING: failed to seed persona self-models: {e}", file=sys.stderr)
        print(
            f"    Retry: BRAIN_USER_ID={user_id} python scripts/seed_persona_selfmd.py",
            file=sys.stderr,
        )

    print(f"✓ Created user {email}")
    print(f"  id:  {user_id}")
    print(f"  org: {user_id} (personal)")
    if is_admin:
        print("  role: admin (is_admin=true)")
    if generated:
        print(f"  password: {password}")
        print("  (shown once — store it securely)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
