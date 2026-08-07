"""
Markdown → standalone HTML document CLI (PAPER.md, PAPER_PUBLIC.md, …).

The CONVERTER itself now lives in ``brain/api/markdown.py`` — it is imported at
runtime by ``brain.api.docs`` to render the API guide inside the app, and only
``brain`` is packaged. This module keeps what is specific to producing a
standalone document: the page shell, the embedded CSS, and the table of contents.

``convert`` / ``_inline`` / ``_slug`` are re-exported so existing importers
(``scripts/md_to_html_private.py``) keep working unchanged.

Usage:  python scripts/md_to_html.py PAPER_PUBLIC.md PAPER_PUBLIC.html
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

# Re-exported for scripts/md_to_html_private.py, which renders PAPER.md in its
# own serif style but reuses this engine.
from brain.api.markdown import _inline, _slug, convert, slug  # noqa: F401

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
    for level, text, heading_slug in toc:
        cls = "lvl3" if level == 3 else "lvl2"
        toc_html.append(f'<li class="{cls}"><a href="#{heading_slug}">{html.escape(text)}</a></li>')
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
