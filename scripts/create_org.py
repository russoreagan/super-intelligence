"""
Attach an additional org to an EXISTING Elyceum account.

An organization is the tenant unit — its own brain process, volume, connectors,
skills, budgets and data. So a second org is how one login gets a second
ENVIRONMENT: a staging workspace alongside production, fully isolated, with no
second account to sign in and out of.

This is the sibling of scripts/create_user.py, which creates an account together
with a "personal org" whose id equals the new user's id. That pattern is correct
for signup and cannot produce a second org (the id is already taken), so this
script exists for every org after the first. It never creates auth users.

Usage:
    python -m scripts.create_org EMAIL "Acme (staging)" [--role member]
                                 [--copy-keys-from ORG_ID | --copy-keys]
                                 [--force]

  --copy-keys-from ORG_ID  copy that org's BYO provider keys into the new org
  --copy-keys              shorthand: copy from the user's current default org
  --role                   membership role in the new org (default: admin)
  --force                  create even if the user already has an org by this name

Copied keys are NEW vault secrets, so the environments can rotate independently
afterwards. Note they still bill to the same provider account — use a separate
Anthropic key if you want a separate invoice.

Without keys the new org cannot boot a brain (the spawn gate requires an
Anthropic key), so copy them or set one in the console before first use.

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment (.env).
"""

from __future__ import annotations

import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    from brain import org as org_mod
    from brain.gateway import org_create

    flags = {"--force": False, "--copy-keys": False}
    role = "admin"
    copy_from = ""
    positional: list[str] = []

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in flags:
            flags[a] = True
        elif a == "--role":
            i += 1
            if i >= len(argv):
                print("ERROR: --role needs a value", file=sys.stderr)
                return 2
            role = argv[i].strip()
        elif a == "--copy-keys-from":
            i += 1
            if i >= len(argv):
                print("ERROR: --copy-keys-from needs an org id", file=sys.stderr)
                return 2
            copy_from = argv[i].strip()
        else:
            positional.append(a)
        i += 1

    if len(positional) < 2:
        print("ERROR: need EMAIL and a NAME for the org.\n", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    email, name = positional[0].strip(), positional[1].strip()

    try:
        url, key = org_create._env()
        uid = org_create.resolve_user(url, key, email=email)
        if not uid:
            print(f"ERROR: no account for {email}", file=sys.stderr)
            return 1
        if flags["--copy-keys"] and not copy_from:
            # The user's current default org — the one they are on today, and so
            # the one whose keys they expect a new environment to start from.
            copy_from = org_mod.org_id_for_user(uid) or ""
            if not copy_from:
                print("ERROR: --copy-keys but the user has no existing org", file=sys.stderr)
                return 1

        row = org_create.create_org(
            user_id=uid,
            name=name,
            role=role,
            copy_keys_from=copy_from,
            force=flags["--force"],
        )
    except org_create.OrgCreateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if row.get("existing"):
        print(f"• {email} already has an org named {name!r} — nothing to do")
        print(f"  org: {row['org_id']}")
        print("  (pass --force to create a second one with the same name)")
        return 0

    print(f"✓ Created org {name!r} for {email}")
    print(f"  org:  {row['org_id']}")
    print(f"  role: {role}")
    seeded = row.get("seeded") or {}
    if seeded.get("self_models"):
        print(f"  seeded {seeded['self_models']} persona self-models")
    if seeded.get("agent"):
        print(f"  seeded default agent {seeded['agent']}")
    if copy_from:
        print(f"  copied {seeded.get('keys_copied', 0)} provider key(s) from {copy_from}")
    for w in row.get("warnings") or []:
        print(f"  ! {w}", file=sys.stderr)
    if not copy_from:
        print("  NOTE: no provider keys — set an Anthropic key before first use,")
        print("        or the brain will not spawn.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
