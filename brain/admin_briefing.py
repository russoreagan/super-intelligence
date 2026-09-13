"""The Admin's opening briefing — what is notable in the org right now.

When someone opens the console it lands on MRI, and The Admin (the org's built-in
internal operator, the default boot agent) greets them with a short summary of the
things worth knowing: a provider breaker, stuck or waiting jobs, approvals that need
a human, a connector in error, spend today, whether the idle loop is dormant.

Everything here is CONTENT-FREE by construction — states, counts, names of
connectors and providers, dollar totals. No conversation text, no job goals, no
thought text. That keeps the digest safe to hand to any signed-in member of the org
and safe to send to a model as plain context.

The model call itself lives in the session (session_loops.api_admin_briefing); this
module is pure: build the digest, rank what is notable, write the prompt, and give a
no-model fallback so the greeting never depends on a provider being reachable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# A reload within this window re-shows the same briefing instead of asking the model
# again — the owner asked for "every fresh open, but not on every hot reload".
BRIEFING_TTL_S = 30 * 60

ADMIN_SLUG = "the_admin"
ADMIN_NAME = "The Admin"

_OPEN_JOB_STATES = ("running", "awaiting_approval", "deferred", "blocked")


def build_digest(
    *,
    live: dict | None,
    jobs: list[dict] | None,
    approvals: int = 0,
    cost_today_usd: float | None = None,
    connectors: list[dict] | None = None,
    alerts: list[dict] | None = None,
    health: str = "",
    learning_mode: str = "",
    running_persona: str = "",
    agents_total: int | None = None,
    agents_paused: int | None = None,
    full: bool = True,
    now: float | None = None,
) -> dict:
    """Fold the org's live signals into one small, content-free dict.

    ``full=False`` is the member projection: health and queue depth only — no spend,
    no approvals, no provider names — matching what the fleet read policy shows a
    non-admin. Every value is a state, count, name or number."""
    now = time.time() if now is None else now
    live = live or {}
    jobs = jobs or []
    dmn = live.get("dmn") or {}
    breaker = live.get("breaker") or {}
    tasks = live.get("tasks") or {}

    open_jobs = [j for j in jobs if str(j.get("state") or "") in _OPEN_JOB_STATES]
    awaiting = [j for j in jobs if str(j.get("state") or "") == "awaiting_approval"]
    failed_recent = [
        j
        for j in jobs
        if str(j.get("state") or "") in ("failed", "stopped_budget")
        and _age_s(j.get("updated_at"), now) is not None
        and _age_s(j.get("updated_at"), now) < 24 * 3600
    ]
    conn_err = [
        str(c.get("display_name") or c.get("name") or "")
        for c in (connectors or [])
        if str(c.get("status") or "") in ("error", "missing")
    ]

    d: dict = {
        "health": health or "ok",
        "alerts": [
            {"code": str(a.get("code") or ""), "severity": str(a.get("severity") or "")}
            for a in (alerts or [])
        ],
        "dmn": {
            "enabled": bool(dmn.get("enabled", True)),
            "dormant": bool(dmn.get("dormant")),
            "roster_size": int(((dmn.get("roster") or {}).get("size")) or 0),
        },
        "jobs": {
            "open": len(open_jobs),
            "awaiting_approval": len(awaiting),
            "failed_24h": len(failed_recent),
            "queued": int(tasks.get("queued") or tasks.get("depth") or 0),
        },
        "running_persona": running_persona or "",
        "full": bool(full),
    }
    if agents_total is not None:
        d["agents"] = {"total": int(agents_total), "paused": int(agents_paused or 0)}
    if full:
        d["approvals_pending"] = int(approvals or 0)
        d["cost_today_usd"] = round(float(cost_today_usd or 0.0), 2)
        d["breaker"] = sorted(str(k) for k in breaker)
        d["connectors_error"] = [c for c in conn_err if c]
        d["learning_mode"] = learning_mode or ""
    return d


def _age_s(ts, now: float) -> float | None:
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)):
            return max(0.0, now - float(ts))
        from datetime import datetime

        s = str(ts).replace("Z", "+00:00")
        return max(0.0, now - datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def notable(digest: dict) -> list[str]:
    """Plain-sentence facts, most urgent first. The model gets these AND the raw
    digest; the fallback greeting uses these alone."""
    out: list[str] = []
    full = bool(digest.get("full", True))
    br = digest.get("breaker") or []
    if br:
        out.append(
            f"{_join(br)} {'is' if len(br) == 1 else 'are'} rejecting the org's key, so "
            "background work is paused until the key is fixed under Settings › Model providers."
        )
    ap = int(digest.get("approvals_pending") or 0)
    if ap:
        out.append(f"{ap} action{'s' if ap != 1 else ''} waiting for your approval.")
    jobs = digest.get("jobs") or {}
    aw = int(jobs.get("awaiting_approval") or 0)
    if aw and aw != ap:
        out.append(f"{aw} job{'s' if aw != 1 else ''} paused awaiting approval.")
    ce = digest.get("connectors_error") or []
    if ce:
        out.append(f"Connector{'s' if len(ce) != 1 else ''} in error: {_join(ce)}.")
    fl = int(jobs.get("failed_24h") or 0)
    if fl:
        out.append(f"{fl} job{'s' if fl != 1 else ''} failed in the last 24 hours.")
    crit = [a["code"] for a in (digest.get("alerts") or []) if a.get("severity") == "crit"]
    warn = [a["code"] for a in (digest.get("alerts") or []) if a.get("severity") == "warn"]
    seen_codes = {"breaker_open"}
    crit = [c for c in crit if c not in seen_codes]
    if crit:
        out.append(
            f"Critical alert{'s' if len(crit) != 1 else ''}: {_join(_pretty(c) for c in crit)}."
        )
    if warn:
        out.append(f"Warning{'s' if len(warn) != 1 else ''}: {_join(_pretty(c) for c in warn)}.")
    op = int(jobs.get("open") or 0)
    if op:
        out.append(f"{op} job{'s' if op != 1 else ''} open right now.")
    ag = digest.get("agents") or {}
    if ag.get("paused"):
        out.append(f"{ag['paused']} of {ag.get('total', '?')} agents are paused.")
    dmn = digest.get("dmn") or {}
    if not dmn.get("enabled", True):
        out.append("The idle loop is switched off, so nothing runs between conversations.")
    elif dmn.get("dormant"):
        out.append("The idle loop is dormant; it wakes on the next conversation.")
    if full and digest.get("cost_today_usd") is not None:
        out.append(f"Spend today is ${float(digest['cost_today_usd']):.2f}.")
    return out


def _pretty(code: str) -> str:
    return str(code or "").replace("_", " ")


def _join(items) -> str:
    items = [str(i) for i in items if str(i)]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def fallback_text(digest: dict, viewer: str = "") -> str:
    """A greeting that needs no model — used when the router is unavailable or the
    call fails. Short, plain, digits always."""
    facts = notable(digest)
    who = viewer.split("@")[0] if viewer else ""
    hello = f"Hello{', ' + who if who else ''}."
    urgent = [
        f for f in facts if not f.startswith("Spend today") and not f.endswith("open right now.")
    ]
    if not urgent:
        quiet = "Nothing needs you right now."
        tail = next((f for f in facts if f.startswith("Spend today")), "")
        return " ".join(x for x in (hello, quiet, tail) if x)
    return " ".join([hello, "Here is what is notable:"] + facts[:4])


def system_prompt(self_md: str) -> str:
    identity = (self_md or "").strip()
    if len(identity) > 6000:
        identity = identity[:6000]
    return (
        f"You are {ADMIN_NAME}, the internal operator for this Elyceum workspace: calm, "
        "attentive, precise. You watch the account's other agents and answer questions "
        "about the app. You are greeting the operator who just opened the console.\n\n"
        + (f"Your identity document:\n{identity}\n\n" if identity else "")
        + "Rules for this greeting: 2 to 4 short sentences, plain prose. Lead with the "
        "most important item; if nothing needs attention, say so briefly and warmly. "
        "Use digits for every number. No headers, bullets, markdown, emoji or quotes. "
        "Never invent facts that are not in the digest. Do not mention the digest itself."
    )


def user_prompt(digest: dict, viewer: str = "", local_hour: int | None = None) -> str:
    import json

    facts = notable(digest)
    when = ""
    if local_hour is not None:
        when = (
            "morning"
            if 5 <= local_hour < 12
            else "afternoon"
            if 12 <= local_hour < 18
            else "evening"
        )
    lines = [
        f"The operator {viewer or 'an org member'} just opened the console"
        + (f" this {when}" if when else "")
        + ".",
        "Notable, most urgent first:" if facts else "Nothing is flagged as notable.",
    ]
    lines += [f"- {f}" for f in facts]
    lines.append("")
    lines.append("Raw digest (content-free): " + json.dumps(digest, sort_keys=True))
    lines.append("")
    lines.append("Write the greeting now.")
    return "\n".join(lines)


@dataclass
class BriefingCache:
    """One briefing per viewer projection (admin / member), reused within the TTL so a
    reload does not cost a model call or re-greet."""

    ttl_s: float = BRIEFING_TTL_S
    _rows: dict = field(default_factory=dict)

    def get(self, scope: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        row = self._rows.get(scope)
        if not row or now - float(row.get("ts") or 0) > self.ttl_s:
            return None
        return row

    def put(self, scope: str, text: str, digest: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        row = {"text": text, "digest": digest, "ts": now, "scope": scope}
        self._rows[scope] = row
        return row

    def clear(self) -> None:
        self._rows.clear()
