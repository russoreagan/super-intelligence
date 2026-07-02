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


def active_or_home_persona() -> str:
    """The persona whose TEMPERAMENT scales the current signal: the turn-bound
    persona when one is bound (multi-persona agent lanes, round-robin DMN ticks),
    else the process home persona from settings.

    This is the resolution every reward_weight/loss_aversion/sensory_gain caller
    must use. Reading settings.persona_name directly gives the HOME persona even
    inside a bound turn — which silently hands every agent the home persona's
    reward valuations and risk posture (found in the 2026-07 chemistry audit; the
    DMN's _reward_persona had the fix, the turn path didn't). Consumers
    canonicalize via persona_slug, so display name or slug is fine. Never raises.
    """
    try:
        from brain.second_brain.store import active_persona

        bound = active_persona()
        if bound:
            return bound
    except Exception:
        pass
    try:
        from brain.settings import settings

        return str(settings.get("persona_name", ""))
    except Exception:
        return ""
