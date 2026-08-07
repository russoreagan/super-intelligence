"""API documentation builder — the payload behind the app's Documentation section.

The API workspace used to show a bare generated endpoint list. This module turns
the hand-written guide (``api_guide.md``, next to this file) and the generated
route table (``reference.py``) into ONE surface: prose pages, each carrying the
live endpoint cards for the routes it documents.

The split of responsibility is the point:

  • Endpoint facts — method, path, description — come from ``build_reference()``,
    which introspects the real router. They cannot drift from what ships.
  • Prose comes from the guide. It CAN go stale, so ``tests/test_api_docs.py``
    fails when a route has no section documenting it.

**The join** is the guide's own per-endpoint H3 headings, which already name the
route they document::

    ### `POST /v1/sessions/{session_id}/turns/stream`

No extra markup, no side table to maintain, and each endpoint card lands next to
its own prose. Section-level mapping was tried and does not work: ``reference.py``
groups all nine ``/v1/sessions`` routes under one section, but the guide spreads
them across five pages (turns, SSE, WebSocket, approvals, grading).

**Two seams**, so the follow-up work is small:

  • ``_load_source()`` returns the guide's markdown. Swap it for a Supabase read
    and the docs become editable in-app; nothing else moves.
  • ``build_docs()`` is a pure function — no request, no auth, no session state,
    JSON-serialisable out. The owner UI wraps it at ``/api_docs`` today; a public
    gateway route can call the same function unchanged.

Because the renderer's output is injected as HTML by the client, the converter it
uses (``brain/api/markdown.py``) escapes source text and allowlists URL schemes —
see that module's security contract. That property is what will keep an in-app
editor from becoming stored XSS in the owner UI.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# `### `METHOD /path`` — the guide's endpoint headings, which are the join key.
_ENDPOINT_H3 = re.compile(r"^###\s+`(GET|POST|PUT|DELETE|WS)\s+(/v1/[^`]*)`", re.M)

# reference.py's compact method tokens → real HTTP methods.
_METHOD = {"get": "GET", "post": "POST", "put": "PUT", "del": "DELETE", "ws": "WS"}

_cache: dict | None = None


def _load_source() -> str:
    """The guide's markdown. THE SEAM: a Supabase-backed loader drops in here,
    falling back to this packaged copy, and nothing else in the module changes."""
    return (Path(__file__).parent / "api_guide.md").read_text(encoding="utf-8")


def api_base_url() -> str:
    """ORIGIN for copy-ready snippets — no ``/v1`` suffix.

    Route paths from ``reference.py`` already carry the ``/v1`` prefix (the router
    is mounted there), so a base ending in ``/v1`` would produce ``/v1/v1/...``.

    Prefers the dedicated API host so a reader copies ``api.elyceum.app`` rather
    than whatever origin the app happens to be served from. The tenant brain
    inherits the gateway's environment (provisioner does ``os.environ.copy()``),
    so BRAIN_API_HOST is visible here."""
    host = os.environ.get("BRAIN_API_HOST", "").strip()
    if host:
        return f"https://{host}"
    port = os.environ.get("BRAIN_API_PORT", "8780").strip() or "8780"
    return f"http://127.0.0.1:{port}"


def _curl(method: str, path: str, body: dict | None, base: str) -> str | None:
    """A runnable curl for one endpoint, or None for WebSocket (no curl form).

    ``method`` is a real HTTP verb — callers must map reference.py's ``del`` token
    first, or the snippet reads ``-X DEL``."""
    import json

    if method == "WS":
        return None
    lines = (
        [f"curl -sS {base}{path}"] if method == "GET" else [f"curl -sS -X {method} {base}{path}"]
    )
    lines.append('  -H "Authorization: Bearer $ELYCEUM_KEY"')
    if body is not None:
        lines.append('  -H "Content-Type: application/json"')
        lines.append("  -d '" + json.dumps(body) + "'")
    return " \\\n".join(lines)


def _ws_snippet(path: str, base: str) -> str:
    """The WebSocket equivalent of a curl line — auth is on the upgrade request."""
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f'wscat -c "{ws_base}{path}" \\\n  -H "Authorization: Bearer $ELYCEUM_KEY"'


def build_docs() -> dict:
    """Guide + generated route table → the Documentation section's payload.

    Cached for the life of the process, like ``reference.py`` — the route table is
    static and the guide ships with the code, so a content edit needs a restart.

    Returns::

        {
          "base_url": "https://api.elyceum.app/v1",
          "pages":   [{id, title, slug, html, endpoints: [...]}],
          "anchors": {heading_slug: page_id},   # every H2/H3, for cross-page links
          "index":   [{m, p, page, anchor, scope?, gateway?}],   # the endpoint index page
        }
    """
    global _cache
    if _cache is not None:
        return _cache

    from brain.api.markdown import convert, slug, split_h2
    from brain.api.reference import GATEWAY_ROUTES, build_reference

    src = _load_source()
    base = api_base_url()

    by_route: dict[tuple[str, str], dict] = {}
    for e in build_reference()["endpoints"]:
        by_route[(_METHOD.get(e["m"], e["m"].upper()), e["p"])] = e

    pages: list[dict] = []
    anchors: dict[str, int] = {}
    index: list[dict] = []

    for page_id, (title, page_slug, body) in enumerate(split_h2(src)):
        # split_h2 yields the pre-first-heading content (the H1 and its lede) with
        # no title. It is a real page — name it so the rail has something to show.
        if not title:
            title, page_slug = "Overview", "overview"
        html_body, headings = convert(body)

        endpoints: list[dict] = []
        for method, path in _ENDPOINT_H3.findall(body):
            anchor = slug(f"`{method} {path}`")
            ref = by_route.get((method, path))
            is_gateway = (method, path) in GATEWAY_ROUTES
            if ref is None and not is_gateway:
                # A heading naming a route that no longer exists. Tests catch this;
                # at runtime, skip it rather than render a card for a dead endpoint.
                continue
            card = {
                "method": method,
                "path": path,
                "anchor": anchor,
                "description": (ref or {}).get("t", ""),
                "scope": (ref or {}).get("scope", ""),
                "tag": (ref or {}).get("tag", ""),
                "body": (ref or {}).get("body"),
                "gateway": is_gateway,
            }
            card["curl"] = (
                _ws_snippet(path, base)
                if method == "WS"
                else _curl(method, path, card["body"], base)
            )
            endpoints.append(card)
            index.append(
                {
                    "method": method,
                    "path": path,
                    "page": page_id,
                    "anchor": anchor,
                    "scope": card["scope"],
                    "gateway": is_gateway,
                }
            )

        pages.append(
            {
                "id": page_id,
                "title": title,
                "slug": page_slug,
                "html": html_body,
                "endpoints": endpoints,
            }
        )
        # Cross-page links resolve through these: the page's own slug plus every
        # heading inside it. An unknown slug is a no-op in the client, never a
        # dead jump.
        if page_slug:
            anchors[page_slug] = page_id
        for _level, _text, heading_slug in headings:
            anchors[heading_slug] = page_id

    _cache = {"base_url": base, "pages": pages, "anchors": anchors, "index": index}
    return _cache
