"""
Render PAPER.md → PAPER.html in the bespoke private style (Georgia serif,
title-block, abstract box, dark code/table chrome). Reuses the markdown engine in
md_to_html.py for the body and preserves the existing PAPER.html <style> block
verbatim, so regenerating after an edit keeps the hand-tuned styling intact.

Usage:  python scripts/md_to_html_private.py PAPER.md PAPER.html
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

# Reuse the proven block/inline converter.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.md_to_html import _inline, convert  # noqa: E402


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "PAPER.md")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "PAPER.html")

    md = src.read_text(encoding="utf-8")

    # Preserve the existing bespoke <style> block verbatim if the target exists.
    css = ""
    if dst.exists():
        m = re.search(r"<style>(.*?)</style>", dst.read_text(encoding="utf-8"), re.DOTALL)
        if m:
            css = m.group(1)

    lines = md.split("\n")

    # ── Header: # Title / **Author** / *dateline* ────────────────────────────
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), "Technical Report")
    author = ""
    dateline = ""
    for l in lines[:12]:
        a = re.match(r"^\*\*(.+?)\*\*\s*$", l.strip())
        if a and not author:
            author = a.group(1)
            continue
        d = re.match(r"^\*(.+?)\*\s*$", l.strip())
        if d and not dateline:
            dateline = d.group(1)
    # Split the title on the first colon into title / subtitle (matches the
    # bespoke <h1> two-line layout).
    if ": " in title:
        head, sub = title.split(": ", 1)
        title_html = f"{html.escape(head)}:<br>{html.escape(sub)}"
    else:
        title_html = html.escape(title)

    # ── Abstract: text between "## Abstract" and the following "---" ──────────
    abstract = ""
    for i, l in enumerate(lines):
        if l.strip() == "## Abstract":
            j = i + 1
            buf = []
            while j < len(lines) and lines[j].strip() != "---":
                if lines[j].strip():
                    buf.append(lines[j].strip())
                j += 1
            abstract = " ".join(buf)
            break

    # ── Body: everything from the first numbered section onward ───────────────
    start = next((i for i, l in enumerate(lines) if re.match(r"^## 1\. ", l)), 0)
    body_md = "\n".join(lines[start:])
    body_html, _ = convert(body_md)
    # Section comments in the original are cosmetic; not required.

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title.split(':')[0])}</title>
  <style>{css}</style>
</head>
<body>
<div class="paper">

  <div class="title-block">
    <h1>{title_html}</h1>
    <p class="author">{html.escape(author)}</p>
    <p class="dateline">{html.escape(dateline)}</p>
  </div>

  <div class="abstract">
    <h2>Abstract</h2>
    <p>{_inline(abstract)}</p>
  </div>

  <hr>

{body_html}

</div>
</body>
</html>
"""
    dst.write_text(doc, encoding="utf-8")
    print(f"Wrote {dst} ({len(doc):,} bytes)")


if __name__ == "__main__":
    main()
