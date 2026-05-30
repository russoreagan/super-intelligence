"""
Minimal, dependency-free Markdown → standalone HTML converter for PAPER_PUBLIC.md.

Handles the subset of Markdown this paper uses: ATX headings, paragraphs,
bold/italic/inline-code, fenced code blocks, pipe tables, ordered/unordered
lists, images, links, hard line breaks (two trailing spaces), and horizontal
rules. Emits a single self-contained HTML file with embedded CSS and an
auto-generated table of contents.

Usage:  python scripts/md_to_html.py PAPER_PUBLIC.md PAPER_PUBLIC.html
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path


def _slug(text: str) -> str:
    s = re.sub(r"<[^>]+>", "", text)
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s


def _inline(text: str) -> str:
    """Escape HTML, then apply inline Markdown. Inline code is protected first."""
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(1))
        return f"\x00{len(placeholders) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = html.escape(text, quote=False)

    # images  ![alt](src)
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: f'<img src="{m.group(2)}" alt="{m.group(1)}">',
        text,
    )
    # links  [text](href)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        text,
    )
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


def convert(md: str) -> tuple[str, list[tuple[int, str, str]]]:
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
            slug = _slug(m.group(2))
            if level in (2, 3):
                toc.append((level, m.group(2), slug))
            out.append(f'<h{level} id="{slug}">{content}</h{level}>')
            i += 1
            continue

        # table (pipe line followed by a separator line)
        if line.lstrip().startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
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
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|```|\s*[-*]\s|\s*\d+\.\s|\|)", lines[i]
        ) and lines[i].strip() != "---":
            para.append(lines[i])
            i += 1
        # hard line breaks: trailing two spaces → <br>
        joined = "<br>\n".join(
            _inline(p.rstrip()) if p.endswith("  ") else _inline(p) for p in para
        )
        out.append(f"<p>{joined}</p>")

    return "\n".join(out), toc


CSS = """
:root { --ink:#1a1a1a; --muted:#666; --rule:#e2e2e2; --accent:#3a5a8c;
        --code-bg:#f5f5f5; --link:#2a5db0; }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; color:var(--ink); background:#fff;
       font:17px/1.65 "Charter","Georgia","Times New Roman",serif;
       -webkit-font-smoothing:antialiased; }
.wrap { max-width:820px; margin:0 auto; padding:64px 28px 120px; }
h1 { font-size:2.05rem; line-height:1.2; margin:0 0 .2em;
     letter-spacing:-.01em; }
h2 { font-size:1.5rem; margin:2.4em 0 .6em; padding-top:.4em;
     border-top:1px solid var(--rule); }
h3 { font-size:1.18rem; margin:1.8em 0 .5em; color:#222; }
p { margin:0 0 1.05em; }
a { color:var(--link); text-decoration:none; }
a:hover { text-decoration:underline; }
strong { font-weight:600; }
code { font-family:"SF Mono",ui-monospace,"Menlo","Consolas",monospace;
       font-size:.86em; background:var(--code-bg); padding:.12em .38em;
       border-radius:4px; }
pre { background:var(--code-bg); border:1px solid var(--rule);
      border-radius:8px; padding:16px 18px; overflow-x:auto; margin:1.2em 0; }
pre code { background:none; padding:0; font-size:.85em; line-height:1.5; }
hr { border:0; border-top:1px solid var(--rule); margin:2.4em 0; }
table { border-collapse:collapse; width:100%; margin:1.4em 0; font-size:.9rem;
        font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }
th,td { border:1px solid var(--rule); padding:9px 12px; text-align:left;
        vertical-align:top; }
th { background:#fafafa; font-weight:600; }
tbody tr:nth-child(even) { background:#fcfcfc; }
img { max-width:100%; height:auto; display:block; margin:1.4em auto;
      border:1px solid var(--rule); border-radius:8px; }
ul,ol { margin:0 0 1.05em; padding-left:1.5em; }
li { margin:.3em 0; }
.byline { color:var(--muted); font-style:italic; margin-top:0; }
.toc { background:#fafbfc; border:1px solid var(--rule); border-radius:10px;
       padding:20px 26px; margin:2.2em 0; font-family:system-ui,-apple-system,
       "Segoe UI",sans-serif; font-size:.92rem; }
.toc h2 { border:0; margin:0 0 .6em; padding:0; font-size:1.05rem; }
.toc ul { list-style:none; padding-left:0; margin:0; }
.toc li { margin:.18em 0; }
.toc li.lvl3 { padding-left:1.4em; font-size:.92em; color:var(--muted); }
.toc a { color:var(--ink); }
@media (max-width:600px){ .wrap{ padding:40px 18px 80px; } body{ font-size:16px; } }
@media print { .toc{ display:none; } a{ color:var(--ink); } body{ font-size:11pt; } }
"""


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "PAPER_PUBLIC.md")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "PAPER_PUBLIC.html")

    md = src.read_text(encoding="utf-8")
    # title = first H1
    title_m = re.search(r"^#\s+(.*)$", md, re.MULTILINE)
    title = title_m.group(1) if title_m else "Technical Report"

    body, toc = convert(md)

    toc_html = ['<nav class="toc"><h2>Contents</h2><ul>']
    for level, text, slug in toc:
        cls = "lvl3" if level == 3 else "lvl2"
        toc_html.append(f'<li class="{cls}"><a href="#{slug}">{html.escape(text)}</a></li>')
    toc_html.append("</ul></nav>")
    toc_block = "\n".join(toc_html)

    # insert TOC after the first <hr> (i.e. after the byline block)
    parts = body.split("<hr>", 1)
    body = parts[0] + "<hr>\n" + toc_block + "\n" + parts[1] if len(parts) == 2 else toc_block + body

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<main class="wrap">
{body}
</main>
</body>
</html>
"""
    dst.write_text(doc, encoding="utf-8")
    print(f"Wrote {dst} ({len(doc):,} bytes, {len(toc)} TOC entries)")


if __name__ == "__main__":
    main()
