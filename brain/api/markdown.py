"""
Minimal, dependency-free Markdown → HTML converter.

Handles the subset of Markdown the engine's docs use: ATX headings (with slug
ids), paragraphs, bold/italic/inline-code, fenced code blocks, pipe tables,
ordered/unordered lists, images, links, hard line breaks (two trailing spaces),
and horizontal rules. Stdlib only — no markdown library is a project dependency
(`markdown-it-py` is present in the lock, but only transitively via `rich`).

This lives under ``brain/`` rather than ``scripts/`` because it is imported at
RUNTIME by ``brain.api.docs`` to render the API guide, and only ``brain`` is
packaged (see ``[tool.hatch.build.targets.wheel]`` in pyproject). The standalone
document CLI — ``scripts/md_to_html.py``, which turns PAPER.md and friends into
self-contained HTML files — keeps its page shell and CSS and imports the engine
from here, so there is exactly one converter.

SECURITY CONTRACT — the reason the UI may inject this output as HTML:
``_inline`` escapes the source text BEFORE applying any inline markup, and
fenced/indented code is escaped wholesale. No HTML present in the Markdown
source can survive into the output; the only tags emitted are ones this module
writes itself. That is what makes it safe to render author-supplied content, and
it must stay true if the guide ever becomes editable in-app (see
``brain/api/docs.py``). ``tests/test_api_docs.py`` pins it.
"""

from __future__ import annotations

import html
import re

__all__ = ["convert", "slug", "split_h2"]


def slug(text: str) -> str:
    """Heading text → anchor id. Matches the `#8-sessions-and-turns` style of
    link the guide uses internally, so intra-document links resolve."""
    s = re.sub(r"<[^>]+>", "", text)
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s


# Back-compat alias: scripts/md_to_html_private.py imports the underscored names.
_slug = slug


# Schemes an href/src may carry. Anything else — javascript:, data:, vbscript: —
# is refused and the link renders as literal text.
_ALLOWED_SCHEME = re.compile(r"^(?:https?|mailto)$", re.I)


def _attr(value: str) -> str:
    """Neutralise quotes for an HTML attribute.

    NOT ``html.escape``: the caller has already escaped ``&``/``<``/``>`` in this
    text, so re-running it would double-escape (``&amp;`` → ``&amp;amp;``). Only
    the quote characters — the ones ``html.escape(quote=False)`` deliberately
    leaves alone — still need handling, and they are exactly what would let a URL
    break out of the attribute it sits in."""
    return value.replace('"', "&quot;").replace("'", "&#x27;")


def _safe_url(raw: str) -> str | None:
    """Attribute-safe URL, or ``None`` when the scheme isn't allowlisted.

    Relative paths, fragments and scheme-less URLs are allowed; an explicit
    scheme must be http, https or mailto. Control characters are stripped first
    so ``java&#9;script:`` cannot smuggle a scheme past the check."""
    u = re.sub(r"[\x00-\x20\x7f]", "", raw)
    if not u:
        return None
    # A colon before the first /, ? or # introduces a scheme.
    head = u.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if ":" in head and not _ALLOWED_SCHEME.match(head.split(":", 1)[0]):
        return None
    return _attr(u)


def _inline(text: str) -> str:
    """Escape HTML, then apply inline Markdown. Inline code is protected first."""
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(1))
        return f"\x00{len(placeholders) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = html.escape(text, quote=False)

    # images  ![alt](src)
    def _img(m: re.Match) -> str:
        src = _safe_url(m.group(2))
        if src is None:
            return m.group(0)  # already-escaped text; render it literally
        return f'<img src="{src}" alt="{_attr(m.group(1))}">'

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _img, text)

    # links  [text](href)
    def _link(m: re.Match) -> str:
        href = _safe_url(m.group(2))
        if href is None:
            return m.group(0)
        return f'<a href="{href}">{m.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, text)
    # bold then italic
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"<em>\1</em>", text)

    # restore inline code
    def _unstash(m: re.Match) -> str:
        code = html.escape(placeholders[int(m.group(1))], quote=False)
        return f"<code>{code}</code>"

    text = re.sub(r"\x00(\d+)\x00", _unstash, text)
    return text


def _render_table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header, body = cells[0], cells[2:]  # row 1 is the --- separator
    out = ["<table>", "<thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in header]
    out.append("</tr></thead>")
    out.append("<tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def split_h2(md: str) -> list[tuple[str, str, str]]:
    """Split a document into ``(title, slug, body_markdown)`` at each ``## `` heading.

    Fence-aware, and deliberately toggling on the SAME test ``convert`` uses
    (``line.startswith("```")``) so the splitter and the converter can never
    disagree about what counts as code. That matters: the API guide has ``#``
    comment lines inside bash fences, and a naive split would cut the document in
    the middle of a code block.

    Content before the first ``## `` (the H1 and its lede) becomes an untitled
    leading entry, so nothing is silently dropped."""
    pages: list[tuple[str, str, str]] = []
    title = ""
    buf: list[str] = []
    in_fence = False

    def _flush() -> None:
        body = "\n".join(buf).strip()
        if title or body:
            pages.append((title, slug(title), body))

    for line in md.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            m = re.match(r"^##\s+(.*)$", line)
            if m:
                _flush()
                title = m.group(1).strip()
                buf = []
                continue
        buf.append(line)
    _flush()
    return pages


def convert(md: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Markdown → ``(html, headings)`` where each heading is ``(level, text, slug)``
    for levels 2 and 3 (what a table of contents wants)."""
    lines = md.split("\n")
    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # fenced code block
        if line.startswith("```"):
            buf = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            code = html.escape("\n".join(buf), quote=False)
            out.append(f"<pre><code>{code}</code></pre>")
            continue

        # horizontal rule
        if line.strip() == "---":
            out.append("<hr>")
            i += 1
            continue

        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            content = _inline(m.group(2))
            heading_slug = slug(m.group(2))
            if level in (2, 3):
                toc.append((level, m.group(2), heading_slug))
            out.append(f'<h{level} id="{heading_slug}">{content}</h{level}>')
            i += 1
            continue

        # table (pipe line followed by a separator line)
        if (
            line.lstrip().startswith("|")
            and i + 1 < n
            and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1])
        ):
            tbl = []
            while i < n and lines[i].lstrip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            out.append(_render_table(tbl))
            continue

        # unordered list
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + "</ul>")
            continue

        # ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + "</ol>")
            continue

        # blank line
        if not line.strip():
            i += 1
            continue

        # paragraph: gather until blank / block start
        para = []
        while (
            i < n
            and lines[i].strip()
            and not re.match(r"^(#{1,6}\s|```|\s*[-*]\s|\s*\d+\.\s|\|)", lines[i])
            and lines[i].strip() != "---"
        ):
            para.append(lines[i])
            i += 1
        # hard line breaks: trailing two spaces → <br>
        joined = "<br>\n".join(
            _inline(p.rstrip()) if p.endswith("  ") else _inline(p) for p in para
        )
        out.append(f"<p>{joined}</p>")

    return "\n".join(out), toc
