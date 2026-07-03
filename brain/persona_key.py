"""
Canonical persona-name slugification — the ONE implementation.

'The Visionary' → 'the_visionary'. Hosted injects raw display names while local
paths carry slugs; every store/table/settings key must normalize the same way or
the persona forks into two identities (the hosted-amnesia bug class). This module
is dependency-free so anything — neuron, stores, the provisioner, the UI server —
can import it without cycles.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def persona_slug(name: object, default: str = "") -> str:
    """Slugify a persona name; idempotent on an already-slugged value. Returns
    `default` when the input is empty/None (call sites differ: stores fall back to
    'default', display paths to 'unnamed', lookups to '')."""
    return _SLUG_RE.sub("_", str(name or "").lower()).strip("_") or default


def persona_state_root(persona: str = "") -> Path:
    """Filesystem root for a persona's volume-backed state — THE routing rule.

    SECOND_BRAIN_PATH is frozen at boot to the HOME persona's root, but one
    process serves many personas via per-turn binding; anything file-backed
    that follows the bound persona (learning ledger/stories, sequence weights,
    chunks, …) must resolve through here or it silently reads and writes the
    home persona's files. Home/empty → the active root unchanged; any other
    persona → the sibling personas/<slug>/ dir (writers create it on first
    write; learning_reader resolves reads the same way). Resolved from env at
    CALL time so tests and /restart re-execs see current routing. Never raises.
    """
    root = Path(
        os.environ.get("SECOND_BRAIN_PATH")
        or str(Path(__file__).resolve().parent.parent / "second_brain")
    )
    slug = persona_slug(persona)
    if not slug:
        return root
    if root.parent.name == "personas":  # active root IS a persona dir
        return root if slug == root.name else root.parent / slug
    home = persona_slug(os.environ.get("BRAIN_PERSONA_NAME", ""))
    if not home:
        # Boot paths that skipped run.py's persona routing (bare local runs,
        # tests): the home persona still lives in settings.
        try:
            from brain.settings import settings

            home = persona_slug(settings.get("persona_name", ""))
        except Exception:
            home = ""
    if slug == home:
        return root
    return root / "personas" / slug


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
