"""API Reference generator — the single source of truth is the route code.

The Reference page in the API workspace used to embed a hand-maintained
ENDPOINTS array that a drift test compared against the real routes. This module
inverts that: it introspects the actual router (`build_api_router`) and derives
every entry from the route itself — method + path from the route object, the
description from the endpoint's DOCSTRING (edit the route, the docs change in
the same diff), the section from the path, the owner-scope chip from the path
prefix. Only two things remain hand-authored, because they can't be derived:
the section blurbs (SECTIONS) and the example request bodies (BODY_EXAMPLES) —
both live here, next to the generator, and both are drift-tested.

Consumers: the owner UI's /api_reference route (workspaces.js renders it) and
anything else that wants a machine-readable curated reference (Swagger at
/v1/docs remains the raw OpenAPI view).
"""

from __future__ import annotations

import inspect
import re

# Ordered sections: (name, description, path prefixes that belong to it).
# First matching prefix wins; every /v1 route must match one (drift-tested).
SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "Sessions",
        "The conversational core: open a session for an end-user on an agent, run turns "
        "(sync, SSE streaming, or realtime voice), resolve pending approvals, and trigger "
        "learning consolidation.",
        ("/v1/sessions",),
    ),
    (
        "Utility",
        "Sessionless one-shot helpers — model calls without a persona, memory, or session "
        "to manage.",
        ("/v1/extract",),
    ),
    (
        "Jobs",
        "Outcomes of autonomous background work the brain ran on its own: durable, pollable "
        "records of what it did, what it spent, and why it stopped.",
        ("/v1/jobs",),
    ),
    (
        "Learning",
        "Read-only windows into what the brain has learned — plain-language stories, wiring "
        "drift, and the reward mix that drives plasticity.",
        ("/v1/learning",),
    ),
    (
        "Audio",
        "Stateless speech endpoints. TTS carries the affect→voice mapping (mood-driven "
        "prosody); STT is the commodity transcription path.",
        ("/v1/tts", "/v1/stt"),
    ),
    (
        "Mandates",
        "The org's role library. A mandate is a reusable role spec (charter, conduct rules, "
        "reward shaping) that can be assigned to any persona.",
        ("/v1/mandates",),
    ),
    (
        "Personas",
        "Persona identities — the built-in roster plus custom personas authored at runtime "
        "(display name, disposition text, emotional baseline) — and each persona's role "
        "assignments.",
        ("/v1/personas",),
    ),
    (
        "Agents",
        "The persona×role pairings your end-users actually talk to — each with its own "
        "name, permission ceiling, and model tier.",
        ("/v1/agents",),
    ),
    (
        "Skills",
        "App-provided skills: partner-submitted guidance injected into turns, screened on "
        "submission before it can go live.",
        ("/v1/skills",),
    ),
    (
        "Admin",
        "Owner-credential review queue — approve or reject what the automatic skill "
        "screener flagged.",
        ("/v1/admin",),
    ),
    (
        "Brain controls",
        "Owner-credential runtime switches on the brain itself — currently the DMN "
        "idle-thought loop (read/flip without a restart).",
        ("/v1/dmn",),
    ),
    (
        "Keys",
        "Credential and end-user lifecycle: mint/revoke partner keys and honor "
        "right-to-erasure for an end-user.",
        ("/v1/partner_keys", "/v1/end_users"),
    ),
    (
        "MCP tokens",
        "Per-end-user connector credentials (vault-encrypted) so managed agents can act "
        "through each user's own MCP servers.",
        ("/v1/mcp",),
    ),
]

# Routes gated on the owner credential (matched by prefix; rendered as a chip).
OWNER_PREFIXES = ("/v1/admin", "/v1/partner_keys", "/v1/end_users", "/v1/dmn")

# Routes served by the GATEWAY (brain/gateway/server.py), not by this router — the
# cost-control pair a partner calls without the brain being up. They belong in the
# docs and the endpoint index, but build_reference() cannot see them because they
# are not on the engine router. Defined here once so the docs builder and the drift
# tests share a single list instead of each keeping their own copy.
GATEWAY_ROUTES: tuple[tuple[str, str], ...] = (("GET", "/v1/status"), ("POST", "/v1/sleep"))

# Extra transport tags the route object can't express.
_TRANSPORT_TAGS = {"/v1/sessions/{session_id}/turns/stream": "SSE"}

# Example request bodies, keyed "METHOD /path". Hand-authored (examples can't
# be derived from code) but drift-tested: every key must match a real route.
BODY_EXAMPLES: dict[str, dict] = {
    "POST /v1/sessions": {
        "agent_id": "the_visionary.research_lead",
        "end_user_id": "u_8821",
        "skills": ["house_policy_v2"],
        "answer_only": False,
    },
    "POST /v1/sessions/{session_id}/turns": {
        "message": "What changed in the market today?",
        "answer_only": False,
    },
    "POST /v1/sessions/{session_id}/turns/stream": {
        "message": "Summarise the thread",
        "audio": {"enabled": True},
    },
    "POST /v1/sessions/{session_id}/consolidate": {"reason": "debate_end"},
    "POST /v1/sessions/{session_id}/confirm": {"approve": True},
    "POST /v1/sessions/{session_id}/approvals/{approval_id}/resolve": {"approve": True},
    "POST /v1/extract": {
        "input": "Apple beat earnings expectations…",
        "schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "sentiment": {"type": "string"}},
        },
        "instructions": "Pull the tradeable signal.",
    },
    "POST /v1/tts": {
        "text": "Markets are calm today.",
        "affect": {"valence": 0.4, "arousal": 0.2},
        "voice_id": "…",
    },
    "POST /v1/stt": {"audio": "<base64>", "mimetype": "audio/wav", "diarize": False},
    "PUT /v1/mandates/{mandate_id}": {
        "role_text": "You are a meticulous research lead…",
        "conduct_rules": None,
        "reward_weights": None,
    },
    "PUT /v1/personas/{persona}/mandates/{mandate_id}": {"sort_order": 0},
    "PUT /v1/personas/{persona}": {
        "display_name": "Captain Ahab",
        "disposition": "Captain Ahab — consumed, magnetic, unbending. The whale took my "
        "leg and I will have my reckoning…",
        "speaking": "- Grand, biblical cadence; oaths and omens\n- Commands, never asks",
        "baseline": {"DA": 0.45, "NE": 0.55, "CORT": 0.3, "GABA": 0.18, "5HT": 0.3},
    },
    "PUT /v1/agents/{agent_id}": {
        "name": "Research Lead",
        "tier": "full",
        "permissions": {"cloud_writes": False},
    },
    "PUT /v1/skills/{skill_id}": {
        "body": "When the user asks about returns, cite the 30-day policy…",
        "description": "House return-policy answers",
        "keywords": ["returns", "policy"],
        "tier": 2,
    },
    "POST /v1/admin/skills/{skill_id}/reject": {"reason": "duplicates built-in behaviour"},
    "POST /v1/partner_keys": {"partner_id": "acme", "label": "Acme production"},
    "POST /v1/mcp/tokens": {
        "end_user_id": "u_8821",
        "server_name": "gmail",
        "server_url": "https://mcp.example.com",
        "access_token": "…",
        "expires_at": None,
    },
}

_cache: dict | None = None


def section_for(path: str) -> str:
    for name, _desc, prefixes in SECTIONS:
        if any(path.startswith(p) for p in prefixes):
            return name
    return ""


def _clean_doc(doc: str) -> str:
    """Docstring → one display paragraph (collapse the hard wraps)."""
    return re.sub(r"\s+", " ", (doc or "").strip())


def build_reference() -> dict:
    """Introspect the real /v1 router into the Reference page's data shape.
    Cached — the route table is static for the life of the process."""
    global _cache
    if _cache is not None:
        return _cache
    from starlette.routing import WebSocketRoute

    from brain.api.server import build_api_router

    async def _dummy(*a, **k):  # never called — we only read the route table
        return {}

    router = build_api_router(_dummy)
    endpoints: list[dict] = []
    for r in router.routes:
        path = getattr(r, "path", "")
        doc = _clean_doc(inspect.getdoc(r.endpoint) or "")
        if isinstance(r, WebSocketRoute):
            methods = ["ws"]
        else:
            methods = [
                {"GET": "get", "POST": "post", "PUT": "put", "DELETE": "del"}[m]
                for m in (r.methods or [])
                if m in ("GET", "POST", "PUT", "DELETE")
            ]
        for m in methods:
            entry: dict = {
                "m": m,
                "p": path,
                "t": doc,
                "grp": section_for(path),
            }
            if m == "ws":
                entry["tag"] = "WS"
            elif path in _TRANSPORT_TAGS:
                entry["tag"] = _TRANSPORT_TAGS[path]
            if any(path.startswith(p) for p in OWNER_PREFIXES):
                entry["scope"] = "owner"
            body = BODY_EXAMPLES.get(f"{m.upper().replace('DEL', 'DELETE')} {path}")
            if body is not None:
                entry["body"] = body
            endpoints.append(entry)
    # Order by section (declaration order), then by path within a section.
    order = {name: i for i, (name, _d, _p) in enumerate(SECTIONS)}
    endpoints.sort(key=lambda e: (order.get(e["grp"], 99), e["p"], e["m"]))
    _cache = {
        "sections": [{"name": n, "description": d} for n, d, _p in SECTIONS],
        "endpoints": endpoints,
    }
    return _cache
