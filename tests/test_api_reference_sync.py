"""
Drift guards for the GENERATED API Reference (brain/api/reference.py).

The Reference page no longer embeds a hand-maintained endpoint array — it is
derived from the live route table + endpoint docstrings, so the route list
CANNOT drift. What can still rot is the hand-authored residue and the inputs
the generator depends on; these tests make that unmergeable:

  - every route must carry a real docstring (it IS the endpoint's description),
  - every route must map to a SECTIONS entry (new path prefix → add a section),
  - every section must be described and actually used,
  - every BODY_EXAMPLES key must match a real route (no phantom examples).

The hand-written developer guide (docs/API.md) documents what the generated page
cannot — bodies, response shapes, error semantics, transports. Its endpoint index
is the one part that CAN silently rot, so it is drift-tested here too: add a route,
document it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

# Gateway-level routes (brain/gateway/server.py). Documented in docs/API.md but not
# served by build_api_router, so they're exempt from the "must be a real route" check.
_GATEWAY_ROUTES = {("GET", "/v1/status"), ("POST", "/v1/sleep")}


def _routes():
    from starlette.routing import WebSocketRoute

    from brain.api.server import build_api_router

    async def _dummy(*a, **k):  # never called — we only read the route table
        return {}

    out = []
    for r in build_api_router(_dummy).routes:
        if isinstance(r, WebSocketRoute):
            methods = ["WS"]
        else:
            methods = [m for m in (r.methods or []) if m not in ("HEAD", "OPTIONS")]
        for m in methods:
            out.append((m, r.path, inspect.getdoc(r.endpoint) or ""))
    return out


def test_every_route_has_docstring():
    """The docstring is the endpoint's user-facing description on the Reference
    page — a route without one ships an undocumented endpoint."""
    thin = [(m, p) for m, p, doc in _routes() if len(doc.strip()) < 25]
    assert not thin, f"routes need real docstrings (≥25 chars): {thin}"


def test_every_route_maps_to_a_section():
    from brain.api.reference import section_for

    unmapped = sorted({p for _m, p, _d in _routes() if not section_for(p)})
    assert not unmapped, (
        f"routes with no SECTIONS prefix (add one in brain/api/reference.py): {unmapped}"
    )


def test_sections_described_and_used():
    from brain.api.reference import SECTIONS, section_for

    used = {section_for(p) for _m, p, _d in _routes()}
    for name, desc, _prefixes in SECTIONS:
        assert len(desc) >= 40, f"SECTIONS['{name}'] description too thin"
        assert name in used, f"SECTIONS['{name}'] matches no served route — remove it"


def test_body_examples_match_real_routes():
    from brain.api.reference import BODY_EXAMPLES

    real = {(m, p) for m, p, _d in _routes()}
    phantom = [k for k in BODY_EXAMPLES if tuple(k.split(" ", 1)) not in real]
    assert not phantom, f"BODY_EXAMPLES keys with no matching route: {phantom}"


def test_build_reference_output_complete():
    from brain.api.reference import build_reference

    ref = build_reference()
    assert len(ref["endpoints"]) == len(_routes())
    assert len(ref["endpoints"]) >= 40
    for e in ref["endpoints"]:
        assert e["grp"], f"{e['p']}: no section"
        assert len(e["t"]) >= 25, f"{e['p']}: thin description"
    owner = [e for e in ref["endpoints"] if e.get("scope") == "owner"]
    assert len(owner) >= 7, "owner-scope chips missing"
    assert any(e["m"] == "ws" for e in ref["endpoints"]), "WS route missing"


# ── docs/API.md (the hand-written developer guide) ────────────────────────────


def _documented_routes() -> set[tuple[str, str]]:
    """(METHOD, path) pairs from the endpoint-index tables in docs/API.md."""
    doc = (Path(__file__).resolve().parent.parent / "docs" / "API.md").read_text(encoding="utf-8")
    return {
        (m, p) for m, p in re.findall(r"\|\s*`(GET|POST|PUT|DELETE|WS)`\s*\|\s*`(/v1/[^`]*)`", doc)
    }


def test_api_md_documents_every_route():
    """A new endpoint must be added to the docs/API.md index, not just shipped."""
    real = {(m, p) for m, p, _d in _routes()}
    missing = sorted(real - _documented_routes())
    assert not missing, f"routes absent from docs/API.md: {missing}"


def test_api_md_has_no_phantom_routes():
    """A path renamed or removed in code must not linger in the guide."""
    real = {(m, p) for m, p, _d in _routes()}
    phantom = sorted(_documented_routes() - real - _GATEWAY_ROUTES)
    assert not phantom, f"docs/API.md documents routes that don't exist: {phantom}"
