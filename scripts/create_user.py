"""
Provision an invite-only Elyceum account.

The UI has no public sign-up — accounts are created here (or in the Supabase
dashboard) using the service-role key. The user is created already-confirmed so
they can sign in immediately.

Usage:
    python -m scripts.create_user EMAIL [PASSWORD]

If PASSWORD is omitted, a strong one is generated and printed once.

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


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    email = argv[0].strip()
    password = argv[1] if len(argv) > 1 else secrets.token_urlsafe(16)
    generated = len(argv) <= 1

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not service_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.", file=sys.stderr)
        return 1

    resp = httpx.post(
        f"{url}/auth/v1/admin/users",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password, "email_confirm": True},
        timeout=15.0,
    )

    if resp.status_code not in (200, 201):
        print(f"ERROR: failed to create user (status {resp.status_code}):", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        return 1

    user = resp.json()
    print(f"✓ Created user {email}")
    print(f"  id: {user.get('id')}")
    if generated:
        print(f"  password: {password}")
        print("  (shown once — store it securely)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
