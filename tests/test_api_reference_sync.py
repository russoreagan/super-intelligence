"""
Drift guard: the API workspace's Reference page (the hand-maintained ENDPOINTS
array in brain/ui/workspaces.js) must document exactly the routes the engine API
actually serves (brain/api/server.py). The array drifted once (35/37, /v1/jobs
missing) because nothing checked it — this test makes drift unmergeable while
keeping the hand-authored examples/grouping, which route metadata can't generate.

On failure: update the ENDPOINTS array in brain/ui/workspaces.js (match the
existing style — group, scope chip, JSON body example from the route docstring).
"""

from __future__ import annotations

import re
from pathlib import Path

WORKSPACES_JS = Path(__file__).parent.parent / "brain" / "ui" / "workspaces.js"

# Doc method tag → HTTP method. 'ws' marks the realtime WebSocket route.
_DOC_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "del": "DELETE", "ws": "WS"}


def _normalize(path: str) -> str:
    """Param names differ between docs ({id}) and code ({session_id}) — compare shape."""
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/"))


def _documented_endpoints() -> set[tuple[str, str]]:
    src = WORKSPACES_JS.read_text()
    m = re.search(r"const ENDPOINTS = \[(.*?)\n  \];", src, re.S)
    assert m, "ENDPOINTS array not found in workspaces.js"
    out = set()
    for method, path in re.findall(r"m: '(\w+)', p: '([^']+)'", m.group(1)):
        assert method in _DOC_METHODS, f"unknown method tag {method!r} for {path}"
        out.add((_DOC_METHODS[method], _normalize(path)))
    return out


def _real_endpoints() -> set[tuple[str, str]]:
    from starlette.routing import WebSocketRoute

    from brain.api.server import build_api_router

    async def _dummy_turn_runner(*a, **k):  # never called — we only read the routes
        return {}

    router = build_api_router(_dummy_turn_runner)
    out = set()
    for r in router.routes:
        path = _normalize(getattr(r, "path", ""))
        if isinstance(r, WebSocketRoute):
            out.add(("WS", path))
            continue
        for method in getattr(r, "methods", None) or []:
            if method in ("HEAD", "OPTIONS"):
                continue
            out.add((method, path))
    return out


def test_api_reference_matches_real_routes():
    documented = _documented_endpoints()
    real = _real_endpoints()
    undocumented = sorted(real - documented)
    phantom = sorted(documented - real)
    msg = []
    if undocumented:
        msg.append(
            "Routes served but MISSING from the API Reference (add to ENDPOINTS in "
            f"brain/ui/workspaces.js): {undocumented}"
        )
    if phantom:
        msg.append(
            "Endpoints documented but NOT served (remove from ENDPOINTS or fix the "
            f"route): {phantom}"
        )
    assert not msg, "\n".join(msg)


def test_reference_every_endpoint_has_description():
    """Every documented endpoint carries a description (t:) except the WS route,
    whose entry is self-describing via its tag."""
    src = WORKSPACES_JS.read_text()
    m = re.search(r"const ENDPOINTS = \[(.*?)\n  \];", src, re.S)
    entries = [e for e in m.group(1).split("{ grp:") if e.strip().startswith("'")]
    assert len(entries) >= 30, f"only {len(entries)} entries parsed"
    for entry in entries:
        path = re.search(r"p: '([^']+)'", entry)
        assert path, f"entry without a path: {entry[:80]!r}"
        assert re.search(r"t: '..", entry), f"{path.group(1)}: missing description"
