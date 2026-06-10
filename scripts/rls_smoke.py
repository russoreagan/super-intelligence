"""
RLS smoke test — proves tenancy is enforced by the DATABASE, not by convention.

Mints two org JWTs (org A = the seeded personal org, org B = a random UUID),
writes a probe row as the service role for each org, then verifies with
anon-key + JWT clients that:

  1. org A's token reads org A's row            (allowed)
  2. org A's token reads ZERO org B rows        (RLS blocks it)
  3. org A's token cannot INSERT a row for B    (with-check blocks it)

Run after applying 007:  uv run python scripts/rls_smoke.py
Needs SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET.
Cleans up its probe rows. Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from supabase import create_client  # noqa: E402

from brain.gateway.org_token import mint_org_token  # noqa: E402

URL = os.environ["SUPABASE_URL"]
ANON = os.environ["SUPABASE_ANON_KEY"]
SERVICE = os.environ["SUPABASE_SERVICE_KEY"]

PROBE_PERSONA = "_rls_smoke"


def org_client(org_id: str):
    tok = mint_org_token(org_id)
    if not tok:
        raise SystemExit("SUPABASE_JWT_SECRET not set — cannot mint org tokens")
    c = create_client(URL, ANON)
    c.postgrest.auth(tok)
    return c


def main() -> int:
    admin = create_client(URL, SERVICE)

    # Org A: the seeded personal org (any existing org row).
    orgs = admin.table("organizations").select("id").limit(1).execute().data or []
    if not orgs:
        print("FAIL: no organizations exist — run migration 007 (it re-seeds)")
        return 1
    org_a = orgs[0]["id"]
    # Org B: a fresh org so the cross-tenant probe is unambiguous.
    org_b = str(uuid.uuid4())
    admin.table("organizations").insert({"id": org_b, "name": "rls-smoke-b"}).execute()

    failures: list[str] = []
    try:
        for org in (org_a, org_b):
            admin.table("brain_schemas").insert(
                {"org_id": org, "persona": PROBE_PERSONA, "filename": "probe.md", "content": org}
            ).execute()

        a = org_client(org_a)

        rows = (
            a.table("brain_schemas").select("*").eq("persona", PROBE_PERSONA).execute().data or []
        )
        owned = [r for r in rows if r["org_id"] == org_a]
        foreign = [r for r in rows if r["org_id"] != org_a]
        if not owned:
            failures.append("org A token cannot read its OWN row (policy too strict / JWT wrong)")
        if foreign:
            failures.append(f"org A token read {len(foreign)} foreign row(s) — RLS NOT enforcing")

        # Explicit cross-org select must come back empty.
        cross = (
            a.table("brain_schemas").select("*").eq("org_id", org_b).execute().data or []
        )
        if cross:
            failures.append("org A token read org B's rows by explicit filter — RLS NOT enforcing")

        # Cross-org INSERT must be rejected by with-check.
        try:
            a.table("brain_schemas").insert(
                {"org_id": org_b, "persona": PROBE_PERSONA, "filename": "evil.md", "content": "x"}
            ).execute()
            failures.append("org A token INSERTED a row for org B — with-check NOT enforcing")
        except Exception:
            pass  # rejection is the pass condition

    finally:
        admin.table("brain_schemas").delete().eq("persona", PROBE_PERSONA).execute()
        admin.table("organizations").delete().eq("id", org_b).execute()

    if failures:
        print("RLS SMOKE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RLS SMOKE: PASS — own-org read OK, cross-org read blocked, cross-org write blocked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
