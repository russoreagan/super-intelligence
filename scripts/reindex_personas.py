"""Backfill the persona index (migration 039) for one or more orgs.

The `personas` table is kept in step by every write, but an org that existed
before the migration — or whose volume was restored — needs one full rebuild. The
brain does it on boot when the table holds fewer customs than the volume; this
script forces it through the owner route POST /v1/personas/reindex, which is the
only place that can see the org's Railway volume (a scripts/ walker cannot).

One owner key per org: pass --owner-key once per org (or a file of keys, one per
line, `label=key` optional). Each call reports {indexed, learned, elapsed_s}.

    .venv/bin/python scripts/reindex_personas.py --base-url https://elyceum.app \
        --owner-key ely_owner_… --owner-key ely_owner_…
    .venv/bin/python scripts/reindex_personas.py --base-url https://elyceum.app \
        --keys-file ~/.elyceum/owner_keys.txt

Env fallbacks: BRAIN_BASE_URL, BRAIN_OWNER_KEYS (comma-separated). A `.env` at the
repo root is read for defaults, like the other operator scripts.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _keys_from_file(path: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, key = line.partition("=")
        out.append((label.strip(), key.strip()) if sep else ("", line))
    return out


def reindex(base_url: str, key: str, *, label: str = "", timeout: float = 600.0) -> dict:
    url = f"{base_url.rstrip('/')}/v1/personas/reindex"
    r = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    tag = label or f"…{key[-6:]}"
    if r.status_code != 200:
        print(f"  ! {tag}: HTTP {r.status_code} {r.text[:200]}")
        return {"ok": False, "status": r.status_code}
    body = r.json() or {}
    print(
        f"  · {tag}: indexed {body.get('indexed', 0)}, learned {body.get('learned', 0)}, "
        f"{body.get('elapsed_s', 0)}s"
    )
    return {"ok": True, **body}


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", default=os.environ.get("BRAIN_BASE_URL", ""))
    ap.add_argument("--owner-key", action="append", default=[], help="one per org; repeatable")
    ap.add_argument("--keys-file", default="", help="file of owner keys, one per line (label=key)")
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args(argv)

    if not args.base_url:
        ap.error("--base-url (or BRAIN_BASE_URL) is required")
    keys: list[tuple[str, str]] = [("", k) for k in args.owner_key if k]
    if args.keys_file:
        keys.extend(_keys_from_file(args.keys_file))
    for k in os.environ.get("BRAIN_OWNER_KEYS", "").split(","):
        if k.strip():
            keys.append(("", k.strip()))
    if not keys:
        ap.error("no owner keys: pass --owner-key, --keys-file or BRAIN_OWNER_KEYS")

    print(f"{len(keys)} org(s) → {args.base_url}")
    failed = 0
    for label, key in keys:
        try:
            res = reindex(args.base_url, key, label=label, timeout=args.timeout)
        except Exception as e:
            print(f"  ! {label or '…' + key[-6:]}: {e}")
            failed += 1
            continue
        if not res.get("ok"):
            failed += 1
    print("done." if not failed else f"done with {failed} failure(s).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
