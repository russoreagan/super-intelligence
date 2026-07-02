"""
Canonical persona-name slugification — the ONE implementation.

'The Visionary' → 'the_visionary'. Hosted injects raw display names while local
paths carry slugs; every store/table/settings key must normalize the same way or
the persona forks into two identities (the hosted-amnesia bug class). This module
is dependency-free so anything — neuron, stores, the provisioner, the UI server —
can import it without cycles.
"""

from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def persona_slug(name: object, default: str = "") -> str:
    """Slugify a persona name; idempotent on an already-slugged value. Returns
    `default` when the input is empty/None (call sites differ: stores fall back to
    'default', display paths to 'unnamed', lookups to '')."""
    return _SLUG_RE.sub("_", str(name or "").lower()).strip("_") or default
