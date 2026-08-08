"""
The owner-route registry must match what the handlers actually enforce.

`reference.is_owner_route()` drives two things: the "owner" chip in the developer
docs, and the filter that keeps owner-gated routes out of the PUBLIC OpenAPI
document. Both are wrong the moment a route's real gate and its registry entry
disagree — a new owner route that nobody registers gets published to the world as
though it were partner-callable.

Rather than trusting a hand-kept list, this introspects every handler's source for a
`_require_owner(` call and asserts the two agree in both directions. A missing
registration and a stale one both fail here.
"""

from __future__ import annotations

import inspect

from brain.api import reference
from brain.api.server import build_api_router


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("ok", {"emotion": "warm"})


def _routes():
    """(METHOD, path, enforces_owner) for every route on the engine router."""
    router = build_api_router(_FakeRunner())
    out = []
    for r in router.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None)
        if not path.startswith("/v1"):
            continue
        try:
            src = inspect.getsource(r.endpoint)
        except (OSError, TypeError):  # pragma: no cover - WS routes may lack source
            src = ""
        enforces = "_require_owner(" in src
        for m in sorted(methods or ["WS"]):
            if m in ("HEAD", "OPTIONS"):
                continue
            out.append((m, path, enforces))
    return out


def test_every_owner_gated_handler_is_registered():
    """A route whose handler calls _require_owner but which is_owner_route() does not
    know about would be published in the public OpenAPI schema."""
    missing = [
        f"{m} {p}"
        for m, p, enforces in _routes()
        if enforces and not reference.is_owner_route(m, p)
    ]
    assert not missing, f"owner-gated but unregistered: {missing}"


def test_no_registered_route_is_actually_open():
    """The inverse: a registry entry for a route that does not enforce owner is a
    false promise in the docs, and hides a real route from the public schema."""
    lying = [
        f"{m} {p}"
        for m, p, enforces in _routes()
        if reference.is_owner_route(m, p) and not enforces
    ]
    assert not lying, f"registered as owner-only but not enforced: {lying}"


def test_org_config_reads_stay_open_to_partners():
    """Partners must keep read access to the org config they run against — the
    decision was write-locked, not invisible."""
    for path in ("/v1/mandates", "/v1/agents", "/v1/personas"):
        assert not reference.is_owner_route("GET", path)


def test_org_config_writes_are_owner_only():
    for method, path in (
        ("PUT", "/v1/mandates/{mandate_id}"),
        ("DELETE", "/v1/agents/{agent_id}"),
        ("PUT", "/v1/personas/{persona}"),
    ):
        assert reference.is_owner_route(method, path)
