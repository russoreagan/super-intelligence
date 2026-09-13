"""Lane-aware text for process logs.

The brain logs short slices of goals, reasons, summaries and thoughts at INFO so
an operator can follow what it is doing. On the OWNER lane (the org's own person
talking in the app, the home persona's idle life) that text is the operator's
own. On the ENGINE lane it is a partner's customer's words, and in an ISOLATED
org a DMN tick or sleep pass bound to a non-home persona is working on one
buyer's private material even though no turn is bound. Those must not reach
Railway stdout, which the platform superadmin can read across every tenant.

`lane_text(s, n)` returns the slice on the owner lane and a content-free digest
(`sha256:<12 hex>/<len>`, like the eval log) everywhere else. Never raises.
"""

from __future__ import annotations

import hashlib


def _engine_lane() -> bool:
    try:
        from brain.turn_ctx import current_field

        return current_field("channel") == "agent"
    except Exception:
        return False


def _isolated_non_home_bound() -> bool:
    try:
        from brain import org_settings
        from brain.second_brain.store import active_persona

        bound = active_persona()
        if not bound:
            return False
        return org_settings.is_isolated_known() and not org_settings.is_home(bound)
    except Exception:
        return False


def private_lane() -> bool:
    """True when text produced right now belongs to a partner's customer."""
    return _engine_lane() or _isolated_non_home_bound()


def digest(text: object) -> str:
    s = str(text or "")
    if not s:
        return ""
    return f"sha256:{hashlib.sha256(s.encode('utf-8')).hexdigest()[:12]}/{len(s)}"


def lane_text(text: object, n: int = 80) -> str:
    """`text[:n]` on the owner lane, a digest on a private lane."""
    s = str(text or "")
    try:
        if private_lane():
            return digest(s)
    except Exception:
        pass
    return s[:n]
