"""
Check every Connectors-directory entry against its live server and print what
Connect does for it today (one_click / platform / own_app / api_key / unreachable).

Usage:
    python scripts/check_connector_catalog.py

Read-only: it fetches public OAuth discovery documents and nothing else. Run it
after editing brain/connectors/catalog.py, or when a vendor moves its endpoint.
An `own_app` vendor missing from catalog.APPS needs an `app` key added there; an
`unreachable` one should be fixed or dropped. New entries come from Claude's
connector directory: Claude Code's MCP registry search returns each server's URL.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _main() -> int:
    from brain.connectors import setup_check
    from brain.connectors.catalog import catalog_entries

    entries = catalog_entries()
    await setup_check.recheck(entries)
    rows = setup_check.annotate(entries)
    width = max(len(e["id"]) for e in rows)
    flagged = 0
    for e in sorted(rows, key=lambda e: (e["setup"], e["id"])):
        note = ""
        if e["setup"] == "own_app" and not e.get("app"):
            note = "  <- needs an `app` key in catalog.py"
            flagged += 1
        elif e["setup"] == "unreachable":
            note = f"  <- {e['setup_detail'][:90]}"
            flagged += 1
        print(f"{e['id']:<{width}}  {e['setup']:<11}  {e['url']}{note}")
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
