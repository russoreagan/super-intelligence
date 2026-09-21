"""
Provision a SECOND org for an EXISTING login — the environments path.

scripts/create_user.py creates an account, and with it a "personal org" whose id
is hardcoded equal to the new auth user's id (the pattern migration 006 seeded).
That is right for signup and structurally cannot produce a second org: the id is
taken. So there has never been a way to give one login a staging environment
alongside prod, which is what this module adds.

What it deliberately does NOT do is create auth users. Account creation stays in
create_user.py; the whole scope here is attaching another org to someone who can
already sign in. The two concerns want different approvals and different audit
trails, and folding them together is how a provisioning route quietly becomes a
user-admin route.

Everything runs under the SERVICE ROLE over PostgREST, the same way
scripts/seed_persona_selfmd.py does — organizations has no insert policy for
`authenticated` by design (006), so this is control-plane work by construction.

The steps, in order, and each one idempotent so a retry after a partial failure
converges rather than duplicating:

  1. resolve the target user (by id or email)
  2. refuse a duplicate name for that user unless force=True
  3. INSERT organizations WITHOUT an id  → Postgres mints a fresh uuid
  4. UPSERT memberships (user, org, role)
  5. seed the persona self-models and The Admin default agent
  6. copy the source org's provider keys so the new org can actually boot

Steps 5 and 6 are non-fatal: the org and membership are already usable and both
are re-runnable, so a seeding hiccup returns warnings instead of stranding a
half-made org the caller cannot see or retry. That mirrors create_user.py, which
treats its own seeding the same way.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Every call is a control-plane round trip on the gateway's own event loop budget;
# the seeds push ~13 rows. 30s matches seed_persona_selfmd's upserts.
_TIMEOUT_S = 30.0


class OrgCreateError(RuntimeError):
    """Provisioning failed in a way the caller should surface verbatim."""


def _env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY", ""
    )
    if not url or not key:
        raise OrgCreateError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return url, key


def _headers(key: str, prefer: str = "") -> dict:
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def resolve_user(url: str, key: str, *, user_id: str = "", email: str = "") -> str | None:
    """The auth user id for an explicit id or an email, or None if there is no
    such user.

    `user_id` wins when both are given. The email lookup pages through the admin
    list and compares locally rather than trusting a server-side filter: GoTrue's
    filter parameter has changed shape across versions, and a filter that silently
    matches nothing would read here as "no such user" — which would make this
    route create an org for the wrong person if it ever fell back to a default."""
    if user_id:
        return user_id
    email = (email or "").strip().lower()
    if not email:
        return None
    page = 1
    while page <= 20:  # 20 * 200 = 4000 users; far past this deployment's size
        resp = httpx.get(
            f"{url}/auth/v1/admin/users",
            headers=_headers(key),
            params={"page": page, "per_page": 200},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json() or {}
        users = body.get("users", body if isinstance(body, list) else []) or []
        for u in users:
            if str(u.get("email", "")).strip().lower() == email:
                return str(u["id"])
        if len(users) < 200:
            return None
        page += 1
    return None


def create_org(
    *,
    user_id: str = "",
    email: str = "",
    name: str,
    role: str = "admin",
    copy_keys_from: str = "",
    force: bool = False,
) -> dict:
    """Create an org for an existing user and return a report.

    {"ok", "org_id", "name", "user_id", "existing", "seeded": {...}, "warnings": []}

    `copy_keys_from` is an org id whose provider keys are copied into the new org
    (migration 045's copy_org_api_keys creates fresh vault secrets, so the two
    orgs can diverge afterwards). Pass the user's default org to make a staging
    environment that boots without re-entering an Anthropic key. Omit it and the
    new org has no keys, so its brain will not spawn until someone sets one.
    """
    name = (name or "").strip()
    if not name:
        raise OrgCreateError("name required")
    if role not in ("admin", "member"):
        raise OrgCreateError("role must be 'admin' or 'member'")

    url, key = _env()
    uid = resolve_user(url, key, user_id=user_id, email=email)
    if not uid:
        raise OrgCreateError("no_such_user")

    warnings: list[str] = []

    # ── 2. idempotency guard ────────────────────────────────────────────────
    # Name is the only handle a caller has before an id exists, so a repeated call
    # (a double-clicked button, a retried deploy step) must not mint a second org
    # and a second brain process. `force` is the escape hatch for someone who
    # genuinely wants two orgs with the same label.
    from brain import org as org_mod

    if not force:
        for existing in org_mod.orgs_for_user(uid):
            if existing["name"].strip().lower() == name.lower():
                return {
                    "ok": True,
                    "existing": True,
                    "org_id": existing["org_id"],
                    "name": existing["name"],
                    "user_id": uid,
                    "seeded": {},
                    "warnings": ["an org with this name already exists for this user"],
                }

    # ── 3. the organizations row ────────────────────────────────────────────
    # No "id" in the payload: organizations.id defaults to gen_random_uuid(), and
    # NOT overriding it is the one substantive difference from create_user.py,
    # which pins id == user_id and therefore can only ever make a personal org.
    resp = httpx.post(
        f"{url}/rest/v1/organizations",
        headers=_headers(key, "return=representation"),
        json={"name": name, "plan": "platform"},
        timeout=_TIMEOUT_S,
    )
    if resp.status_code not in (200, 201):
        raise OrgCreateError(f"could not create org ({resp.status_code}): {resp.text}")
    rows = resp.json() or []
    if not rows:
        raise OrgCreateError("org insert returned no row")
    org_id = str(rows[0]["id"])

    # ── 4. the membership ───────────────────────────────────────────────────
    resp = httpx.post(
        f"{url}/rest/v1/memberships",
        headers=_headers(key, "resolution=merge-duplicates"),
        params={"on_conflict": "user_id,org_id"},
        json={"user_id": uid, "org_id": org_id, "role": role},
        timeout=_TIMEOUT_S,
    )
    if resp.status_code not in (200, 201, 204):
        # The org row exists but nobody can reach it. Say so precisely — the org id
        # is in the message so an operator can finish or delete it by hand.
        raise OrgCreateError(
            f"org {org_id} created but membership failed ({resp.status_code}): {resp.text}"
        )

    # ── 5. seeds (non-fatal) ────────────────────────────────────────────────
    seeded: dict = {}
    try:
        from scripts.seed_persona_selfmd import seed_default_admin, seed_org

        seeded["self_models"] = seed_org(org_id, url, key)
        seeded["agent"] = seed_default_admin(org_id, url, key)
    except Exception as e:
        # Without the default agent the org boots on the bundled persona instead
        # of The Admin — degraded, not broken, and fixed by re-running the seed.
        logger.warning("[org_create] seeding %s failed: %s", org_id, e)
        warnings.append(f"persona/agent seeding failed: {e}")

    # ── 6. provider keys (non-fatal) ────────────────────────────────────────
    if copy_keys_from:
        try:
            seeded["keys_copied"] = copy_keys(copy_keys_from, org_id)
        except Exception as e:
            logger.warning("[org_create] key copy into %s failed: %s", org_id, e)
            warnings.append(f"key copy failed: {e} — set a provider key before first use")

    return {
        "ok": True,
        "existing": False,
        "org_id": org_id,
        "name": name,
        "user_id": uid,
        "seeded": seeded,
        "warnings": warnings,
    }


def copy_keys(from_org: str, to_org: str) -> int:
    """Copy provider keys between orgs via migration 045's copy_org_api_keys, and
    return how many were copied. Service-role only (the RPC decrypts).

    Fresh vault secrets, not shared pointers — so rotating or deleting a key in
    one environment never reaches the other."""
    from brain.second_brain import supabase_client

    resp = (
        supabase_client.get_client()
        .rpc("copy_org_api_keys", {"p_from_org": from_org, "p_to_org": to_org})
        .execute()
    )
    return int(resp.data or 0)
