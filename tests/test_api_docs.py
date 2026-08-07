"""
The app's API Documentation section: renderer safety, and the drift guards that
keep the guide honest about what the server actually serves.

The endpoint CARDS are generated from the live router, so they can't drift. What
can rot is the prose — a route ships and nobody writes a section for it. These
tests make that unmergeable, replacing the pair that used to regex-scrape a
hand-maintained appendix table (a check on a second copy, rather than on what the
app renders).

The security tests matter more than they look. ``build_docs()`` output is injected
into the owner UI with innerHTML, and that UI holds the session cookie for every
workspace. Today the source is trusted repo content; the whole point of the
``_load_source`` seam is that it won't be forever. So the converter is required to
be safe regardless of who wrote the markdown.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from brain.api.markdown import convert, slug, split_h2

# ── renderer: safety ─────────────────────────────────────────────────────────
#
# These assert on PARSED HTML, not on substrings. The difference is the whole
# point: `<div onclick='x'>` arriving escaped as `&lt;div onclick='x'&gt;` is
# inert text that a substring check would wrongly flag, while a real `onclick`
# attribute on a real tag is the thing that must never exist.

_SAFE_SCHEMES = ("http:", "https:", "mailto:")


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))


def _live_tags(html_out: str) -> list[tuple[str, dict[str, str]]]:
    p = _Tags()
    p.feed(html_out)
    return p.tags


def _assert_inert(html_out: str) -> None:
    for tag, attrs in _live_tags(html_out):
        assert tag != "script", f"live <script> emitted: {html_out}"
        for name, value in attrs.items():
            assert not name.startswith("on"), f"live event handler {name}=: {html_out}"
            if name in ("href", "src"):
                v = value.strip().lower()
                if ":" in v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]:
                    assert v.startswith(_SAFE_SCHEMES), f"disallowed URL scheme: {value}"


@pytest.mark.parametrize(
    "hostile",
    [
        "[click](javascript:alert(1))",
        "[tab](java\tscript:alert(1))",
        "[data](data:text/html,<script>alert(1)</script>)",
        "[vb](vbscript:msgbox(1))",
        '[quote](" onmouseover="alert(1))',
        '![img](x" onerror="alert(1))',
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<div onclick='steal()'>hi</div>",
    ],
)
def test_converter_neutralises_hostile_markdown(hostile):
    """No script tag, no event handler, no script-scheme URL may reach the output
    as LIVE HTML. Escaped-to-text is a pass — that is the converter working."""
    out, _ = convert(hostile)
    _assert_inert(out)


def test_converter_keeps_legitimate_links():
    """The allowlist must not break ordinary links — absolute, relative, anchor,
    mailto, and images."""
    for src, expect in [
        ("[a](https://elyceum.app/v1)", 'href="https://elyceum.app/v1"'),
        ("[b](http://127.0.0.1:8780)", 'href="http://127.0.0.1:8780"'),
        ("[c](#8-sessions-and-turns)", 'href="#8-sessions-and-turns"'),
        ("[d](brain/api/docs.py)", 'href="brain/api/docs.py"'),
        ("[e](mailto:a@b.com)", 'href="mailto:a@b.com"'),
        ("![f](/x.png)", 'src="/x.png"'),
    ]:
        out, _ = convert(src)
        assert expect in out, f"{src} → {out}"


# ── renderer: the features the guide actually uses ───────────────────────────


def test_converter_renders_tables_and_fences():
    """The UI's own mdToHtml supports neither, which is why it can't be used here."""
    table, _ = convert("| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert "<table>" in table and "<th>A</th>" in table and "<td>1</td>" in table

    fence, _ = convert('```bash\ncurl -sS "x"\n```')
    assert "<pre><code>" in fence and "curl -sS" in fence


def test_converter_escapes_inside_fences():
    out, _ = convert("```\n<script>alert(1)</script>\n```")
    assert "&lt;script&gt;" in out and "<script>" not in out


# ── splitter ─────────────────────────────────────────────────────────────────


def test_split_h2_is_fence_aware():
    """The guide has `#` comment lines inside bash fences; a splitter that isn't
    fence-aware cuts the document mid-code-block."""
    src = "# T\nlede\n\n## One\na\n\n```bash\n## not a heading\n# nor this\n```\n\n## Two\nb\n"
    pages = split_h2(src)
    assert [t for t, _s, _b in pages] == ["", "One", "Two"]
    assert "## not a heading" in pages[1][2], "fenced content must stay with its page"


def test_split_h2_keeps_preamble():
    pages = split_h2("# Title\nintro\n\n## First\nbody\n")
    assert pages[0][0] == "" and "intro" in pages[0][2]


# ── the guide ↔ router join (this is the drift guard) ────────────────────────

_ENDPOINT_H3 = re.compile(r"^###\s+`(GET|POST|PUT|DELETE|WS)\s+(/v1/[^`]*)`", re.M)


def _real_routes() -> set[tuple[str, str]]:
    from starlette.routing import WebSocketRoute

    from brain.api.server import build_api_router

    async def _dummy(*a, **k):
        return {}

    out: set[tuple[str, str]] = set()
    for r in build_api_router(_dummy).routes:
        if isinstance(r, WebSocketRoute):
            out.add(("WS", r.path))
            continue
        out.update((m, r.path) for m in (r.methods or []) if m not in ("HEAD", "OPTIONS"))
    return out


def _guide_headings() -> list[tuple[str, str]]:
    from brain.api.docs import _load_source

    return _ENDPOINT_H3.findall(_load_source())


def test_every_route_is_documented():
    """Ship a route without writing a section for it and this fails. The guide's
    `### \\`METHOD /path\\`` headings are the join key."""
    missing = sorted(_real_routes() - set(_guide_headings()))
    assert not missing, f"routes with no section in the guide: {missing}"


def test_no_route_is_documented_twice():
    headings = _guide_headings()
    dupes = sorted({k for k in headings if headings.count(k) > 1})
    assert not dupes, f"duplicate endpoint headings (the card would render twice): {dupes}"


def test_no_phantom_endpoint_headings():
    """A renamed or deleted route must not linger as a heading."""
    from brain.api.reference import GATEWAY_ROUTES

    phantom = sorted(set(_guide_headings()) - _real_routes() - set(GATEWAY_ROUTES))
    assert not phantom, f"guide documents routes that don't exist: {phantom}"


def test_gateway_routes_are_documented():
    """/v1/status and /v1/sleep live on the gateway, so build_reference() can't see
    them — they'd vanish silently without their own check."""
    from brain.api.reference import GATEWAY_ROUTES

    missing = sorted(set(GATEWAY_ROUTES) - set(_guide_headings()))
    assert not missing, f"gateway routes missing from the guide: {missing}"


# ── build_docs() ─────────────────────────────────────────────────────────────


@pytest.fixture
def docs():
    import brain.api.docs as d

    d._cache = None  # the module caches for process life
    try:
        yield d.build_docs()
    finally:
        d._cache = None


def test_every_route_gets_a_card(docs):
    """Nothing may be silently dropped between the guide and the rendered payload."""
    from brain.api.reference import GATEWAY_ROUTES

    carded = {(e["method"], e["path"]) for p in docs["pages"] for e in p["endpoints"]}
    assert not (_real_routes() - carded), f"routes with no card: {sorted(_real_routes() - carded)}"
    assert len(docs["index"]) == len(_real_routes()) + len(GATEWAY_ROUTES)


def test_every_internal_anchor_resolves(docs):
    """The guide cross-links between sections; once it is paginated those links
    only work if every target is in the anchors map."""
    from brain.api.docs import _load_source

    targets = set(re.findall(r"\]\(#([^)]+)\)", _load_source()))
    broken = sorted(targets - set(docs["anchors"]))
    assert not broken, f"internal links to nowhere: {broken}"


def test_pages_have_titles_and_html(docs):
    assert len(docs["pages"]) > 20
    for p in docs["pages"]:
        assert p["title"], f"page {p['id']} has no title"
        assert p["slug"], f"page {p['id']} has no slug"
        assert p["html"].strip(), f"page {p['title']} rendered empty"


def test_curl_snippets_are_runnable(docs):
    """Two traps: reference.py's method token is `del`, not DELETE; and WS has no
    curl form. Also the route paths already carry /v1, so the base must not."""
    cards = [e for p in docs["pages"] for e in p["endpoints"]]
    for e in cards:
        assert "/v1/v1/" not in (e["curl"] or ""), f"doubled prefix: {e['curl']}"
        assert "-X DEL " not in (e["curl"] or ""), f"unmapped method token: {e['curl']}"
    delete = next(e for e in cards if e["method"] == "DELETE")
    assert "-X DELETE " in delete["curl"]
    ws = next(e for e in cards if e["method"] == "WS")
    assert ws["curl"].startswith("wscat"), "WebSocket must not get a curl snippet"
    assert "curl" not in ws["curl"]


def test_base_url_prefers_the_api_host(monkeypatch):
    import brain.api.docs as d

    monkeypatch.setenv("BRAIN_API_HOST", "api.elyceum.app")
    assert d.api_base_url() == "https://api.elyceum.app"
    monkeypatch.delenv("BRAIN_API_HOST")
    monkeypatch.setenv("BRAIN_API_PORT", "9999")
    assert d.api_base_url() == "http://127.0.0.1:9999"


def test_build_docs_is_a_pure_serialisable_function(docs):
    """Locks the public seam: no request, no auth, no session state in, JSON out —
    so a public gateway route can call it unchanged."""
    import inspect

    from brain.api.docs import build_docs

    assert not inspect.signature(build_docs).parameters
    json.dumps(docs)  # raises if anything non-serialisable crept in


def test_rendered_html_carries_no_event_handlers(docs):
    """End-to-end version of the converter safety tests, over the real guide —
    this is the HTML actually injected into the owner UI."""
    for p in docs["pages"]:
        _assert_inert(p["html"])


def test_heading_slugs_match_the_guides_own_link_style():
    """The guide links to `#8-sessions-and-turns`; the converter must produce that
    exact slug or every cross-reference dies."""
    assert slug("8. Sessions and turns") == "8-sessions-and-turns"
    assert slug("9. Streaming: SSE") == "9-streaming-sse"
    assert slug("17. Mandates (roles)") == "17-mandates-roles"
