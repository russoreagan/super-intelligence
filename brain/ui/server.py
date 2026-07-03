"""
UI WebSocket server — serves the brain visualizer HTML and pushes activation events.
Runs as asyncio.create_task alongside the brain session.

GET /        → index.html
WebSocket /ws → bidirectional:
    server → client: activation events, neuromod, emotion, turn start/end
    client → server: {"type": "user_message", "text": "..."}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable
from html import escape as html_escape
from pathlib import Path

# FastAPI/WebSocket imports at module level so that `from __future__ import annotations`
# (PEP 563 lazy strings) doesn't prevent FastAPI's dependency injector from resolving
# the `WebSocket` annotation in ws_endpoint. When these are imported only inside
# _build_app(), the string 'WebSocket' can't be found in the module globals and FastAPI
# misclassifies the parameter as a query param, causing an immediate 403 on every
# WebSocket connection attempt.
try:
    from fastapi import FastAPI, Request, UploadFile, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
except ImportError:
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment]
    WebSocket = None  # type: ignore[assignment]
    WebSocketDisconnect = None  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]
    UploadFile = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _tenant_root() -> Path | None:
    """The pod's own tenant directory — the boundary org-admin filesystem grants
    are jailed to. settings.json lives at the org root (BRAIN_SETTINGS_PATH); fall
    back to the grandparent of the persona-namespaced SECOND_BRAIN_PATH. Returns
    None when it can't be resolved (callers then fail closed)."""
    sp = os.environ.get("BRAIN_SETTINGS_PATH", "").strip()
    if sp:
        try:
            return Path(sp).resolve().parent
        except Exception:
            return None
    sb = os.environ.get("SECOND_BRAIN_PATH", "").strip()
    if sb:
        try:
            p = Path(sb).resolve()
            return p.parent.parent if p.parent.name == "personas" else p
        except Exception:
            return None
    return None


def _within_root(path: str, root: Path | None) -> bool:
    """True iff ``path`` resolves inside the tenant root. Fail closed (deny) when
    the root is unknown — better to reject a new grant than to leak one."""
    if root is None:
        return False
    try:
        rp = Path(path).resolve()
    except Exception:
        return False
    return rp == root or str(rp).startswith(str(root) + os.sep)


def _jail_motor_dirs(body: dict) -> None:
    """Confine an org-admin's filesystem grants to their own tenant root. Mutates
    ``body`` in place: keeps each path that is inside the tenant root OR already
    stored (a platform super-admin may have set out-of-jail roots on a self-hosted
    box — those are grandfathered); drops the rest. Defence in depth so a tenant
    can't point the motor cortex at the host or another pod's volume."""
    keys = ("motor_allowed_dirs", "motor_read_only_dirs")
    if not any(k in body for k in keys):
        return
    from brain.settings import settings as _settings

    root = _tenant_root()
    for k in keys:
        if k not in body:
            continue
        grandfathered = {
            ln.strip() for ln in str(_settings.get(k) or "").splitlines() if ln.strip()
        }
        kept, dropped = [], []
        for ln in str(body.get(k) or "").splitlines():
            p = ln.strip()
            if not p:
                continue
            (kept if (p in grandfathered or _within_root(p, root)) else dropped).append(p)
        body[k] = "\n".join(kept)
        if dropped:
            logger.warning(
                "[settings] org-admin filesystem path(s) outside tenant root %s dropped: %s",
                root,
                dropped,
            )


def _persona_dial_positions() -> dict:
    """Per-persona non-chemistry dial positions for the settings UI.
    Cognitive + lingering: the authored fingerprint (persona_chem). Motivation
    (warmth/curiosity/mastery-seeking): derived from the reward table so the
    needle matches the per-persona reward profile the brain actually runs on —
    map a reward weight in ~[0.5,1.6] to a 0..1 needle position."""
    try:
        from brain.neuron import _PERSONA_REWARD_WEIGHTS, _PERSONA_RISK_POSTURE
        from brain.persona_chem import PERSONA_COG_POSITIONS
        from brain.persona_key import persona_slug as _slug

        def _pos(w: float) -> float:
            return max(0.0, min(1.0, (float(w) - 0.5) / 1.1))

        def _pos_la(lam: float) -> float:
            # Loss aversion λ: neutral 1.0 → mid-dial 0.5; the panel spreads either side.
            return max(0.0, min(1.0, 0.5 + (float(lam) - 1.0) / 4.0))

        def _pos_ua(kap: float) -> float:
            # Uncertainty aversion κ is one-sided: 0.0 (risk-neutral) → bottom, 1.5 → top.
            return max(0.0, min(1.0, float(kap) / 1.5))

        out: dict[str, dict[str, float]] = {}
        for name, cog in PERSONA_COG_POSITIONS.items():
            out[name] = dict(cog)
        # Add motivation + risk-posture positions for every persona. Both pose from an
        # innate neuron table (reward weights / risk posture) — the brain runs on the table,
        # the *_scale multipliers stay neutral, so only the NEEDLE needs deriving here.
        for name in set(list(out) + list(_reward_persona_names())):
            out.setdefault(name, {})
            rw = _PERSONA_REWARD_WEIGHTS.get(_slug(name), {})
            if rw:
                out[name]["warmth-seeking"] = _pos(rw.get("connection", 1.0))
                out[name]["curiosity-seeking"] = _pos(rw.get("novelty", 1.0))
                out[name]["mastery-seeking"] = _pos(
                    (rw.get("correctness", 1.0) + rw.get("mastery", 1.0)) / 2.0
                )
            # Risk posture is the avoidance-side mirror of motivation (what the persona is
            # wired to FEAR). Defaults λ=1.0 / κ=0.0 (symmetric, risk-neutral) for the unlisted.
            rp = _PERSONA_RISK_POSTURE.get(_slug(name), {})
            out[name]["loss-sensitivity"] = _pos_la(float(rp.get("loss_aversion", 1.0)))
            out[name]["uncertainty-aversion"] = _pos_ua(float(rp.get("uncertainty_aversion", 0.0)))
        return out
    except Exception as e:
        logger.debug("[settings] persona dial positions unavailable: %s", e)
        return {}


def _reward_persona_names() -> list[str]:
    """Display names for personas that carry reward weights (so motivation
    positions cover every built-in, not just those with a cognitive profile)."""
    try:
        from brain.persona_chem import PERSONA_CHEMISTRY

        return list(PERSONA_CHEMISTRY.keys())
    except Exception:
        return []


HTML_PATH = Path(__file__).parent / "index.html"
LOGIN_HTML_PATH = Path(__file__).parent / "login.html"
RESET_HTML_PATH = Path(__file__).parent / "reset.html"


class UIServer:
    def __init__(
        self,
        emitter_queue: asyncio.Queue,
        on_user_message: Callable[[str], None] | None = None,
        on_voice_change: Callable[[str], None] | None = None,
        on_eval_mode: Callable[[bool], None] | None = None,
        on_mic_toggle: Callable[[], bool] | None = None,
        on_mic_ptt: Callable[[bool], None] | None = None,
        on_tts_mute: Callable[[bool], None] | None = None,
        is_muted_fn: Callable[[], bool] | None = None,
        mic_status_fn: Callable[[], str] | None = None,
        on_interrupt: Callable[[], None] | None = None,
        on_tasks_clear: Callable[[], dict] | None = None,
        on_task_kill: Callable[[str], dict] | None = None,
        on_task_approve: Callable[[str], dict] | None = None,
        on_task_skip: Callable[[str], dict] | None = None,
        approvals_fn: Callable[[], list] | None = None,
        jobs_list_fn: Callable[..., list] | None = None,
        job_get_fn: Callable[[str], dict | None] | None = None,
        on_feedback: Callable[..., dict] | None = None,
        connectors_fn: Callable[[], list] | None = None,
        connector_reload_fn: Callable[[], None] | None = None,
        cloud_status_fn: Callable[[], dict] | None = None,
        tier_fn: Callable[[], str] | None = None,
        usage_fn: Callable[..., dict] | None = None,
        skill_rewarm_fn: Callable[[], object] | None = None,
        wiring=None,
        bus=None,
    ) -> None:
        self._queue = emitter_queue
        self._on_user_message = on_user_message
        self._on_voice_change = on_voice_change
        self._on_eval_mode = on_eval_mode
        self._on_mic_toggle = on_mic_toggle  # () -> is_muted (bool) — toggles
        self._on_mic_ptt = on_mic_ptt  # (down: bool) -> None — push-to-talk hold
        self._on_tts_mute = on_tts_mute  # (muted: bool) -> None — skip TTS synthesis when muted
        self._is_muted_fn = is_muted_fn  # () -> is_muted (bool) — read-only; None = no Python voice
        # () -> 'off'|'muted'|'active'. Authoritative status that knows whether a
        # server-side mic exists at all. 'off' tells the browser to self-capture
        # (hosted, no audio device). Preferred over _is_muted_fn when provided.
        self._mic_status_fn = mic_status_fn
        self._on_interrupt = on_interrupt
        self._on_tasks_clear = on_tasks_clear  # () -> stats dict; kills self-directed work
        self._on_task_kill = on_task_kill  # (job_id) -> stats dict; kills one job
        self._on_task_approve = on_task_approve  # (approval_id) -> dict; approve + re-queue
        self._on_task_skip = on_task_skip  # (approval_id) -> dict; skip a pending approval
        self._approvals_fn = approvals_fn  # () -> list of pending approvals
        self._jobs_list_fn = jobs_list_fn  # (limit, state) -> job outcome rows, newest first
        self._job_get_fn = job_get_fn  # (job_id) -> full job record or None
        self._on_feedback = on_feedback  # (turn_id, grade, source) -> dict; external grade
        self._connectors_fn = connectors_fn  # () -> configured cloud connector names
        self._connector_reload_fn = (
            connector_reload_fn  # () -> None; hot-reload after register/remove
        )
        # () -> {available, model, actions_enabled}. Status of the cloud connector
        # (Claude / Anthropic cloud actions) the MCP connectors are reached through.
        self._cloud_status_fn = cloud_status_fn
        # () -> 'full'|'lite'. The brain's resolved runtime tier, surfaced on /health
        # so the gateway's pod reconciler knows whether THIS brain actually uses the
        # shared GPU pod: a 'lite' brain remaps every local/runpod route to cloud, so
        # keeping a pod up for it is pure waste. None = report 'full' (the safe default
        # — a real full brain must never be denied its pod).
        self._tier_fn = tier_fn
        # (since, until) -> { agent_id: {calls, cloud_calls, in_tok, out_tok,
        # cloud_usd, pod_s, last_ts} }. Per-agent model usage for the Agents
        # dashboard: no range → live session meter; a range → durable ledger sum.
        self._usage_fn = usage_fn
        # () -> Awaitable; reloads the org's approved app-provided skills into the live
        # SkillSelector index after an approve/reject/delete in the Skills tab. None when
        # the selector is unavailable (then admin changes apply at the next boot).
        self._skill_rewarm_fn = skill_rewarm_fn
        self._clients: set = set()
        self._last_neuromod: dict = {}
        self._last_hormonal: dict = {}
        self._last_emotion: str = ""
        self._last_thoughts: list[dict] = []
        self._chat_history: list[dict] = []  # completed turns for page-refresh replay
        self._pending_turn: dict | None = None  # turn_start awaiting its turn_end
        # Agent-lane activity (engine-API/partner turns) kept OUT of the main feed
        # and surfaced separately in the Agents view. Mirrors the main-chat shape:
        # recent completed turns for replay, plus per-session turn_start awaiting end.
        self._agent_history: list[dict] = []
        self._agent_pending: dict[str, dict] = {}  # route_sid -> turn_start payload
        self._wiring_frozen: bool = False
        self._subsystem_status: dict[str, bool] = {}
        self._wiring = wiring
        self._bus = bus  # for publishing to auditory pipeline from press-to-talk
        self._app = None

    def set_wiring_frozen(self, frozen: bool) -> None:
        self._wiring_frozen = bool(frozen)

    def set_subsystem_status(self, status: dict[str, bool]) -> None:
        """Store subsystem up/down flags to broadcast on every connect."""
        self._subsystem_status = dict(status)

    def _mic_status(self) -> str:
        """Single status string for the mic: 'off' | 'muted' | 'active'.
        'off' means no server-side mic (browser self-captures). 'muted'/'active'
        reflect a present server-side mic's live state."""
        if self._mic_status_fn is not None:
            return self._mic_status_fn()
        # Legacy fallback: bool-only predicate can't express "off", so it always
        # reported "muted"/"active" even with no mic — the bug this replaces.
        if self._is_muted_fn is None:
            return "off"
        return "muted" if self._is_muted_fn() else "active"

    def set_message_handler(self, fn: Callable[[str], None]) -> None:
        self._on_user_message = fn

    def set_voice_change_handler(self, fn: Callable[[str], None]) -> None:
        self._on_voice_change = fn

    def _build_app(self):
        app = FastAPI(docs_url=None, redoc_url=None)

        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

        from brain.ui import auth as ui_auth

        # ── Auth gate ─────────────────────────────────────────────────────────
        # Nothing below /login is reachable without a valid Supabase session.
        # Browser navigations bounce to /login; API calls get 401. Fail closed
        # when Supabase isn't configured. Disable with BRAIN_AUTH_DISABLED=true.
        @app.middleware("http")
        async def _auth_gate(request: Request, call_next):
            path = request.url.path
            if ui_auth.is_disabled() or ui_auth.is_public_path(path):
                return await call_next(request)
            if not ui_auth.is_configured():
                return ui_auth.config_error_response(request)
            claims, refreshed = await ui_auth.authenticate(request)
            if claims is None or ui_auth.owner_mismatch(claims):
                return ui_auth.unauthorized_response(request)
            request.state.user = claims
            # Expose the EFFECTIVE access token (the freshly-refreshed one when the
            # middleware just rotated it, else the cookie). Handlers that call out
            # to Supabase as the user — e.g. the key-vault endpoints — must use this
            # instead of request.cookies, which still holds the stale/expired token
            # until the browser receives the new cookie set on the response below.
            request.state.access_token = (
                refreshed.get("access_token")
                if refreshed
                else request.cookies.get(ui_auth.ACCESS_COOKIE)
            )
            response = await call_next(request)
            if refreshed:
                ui_auth.set_session_cookies(
                    response, refreshed, remember=ui_auth.remembered(request)
                )
            return response

        # ── HTTPS upgrade + HSTS ──────────────────────────────────────────────
        # Defined AFTER the auth gate so it wraps outermost: it can redirect an
        # http request to https BEFORE auth runs, and it stamps HSTS on every
        # response (including 301s and 401s). Railway terminates TLS at the edge
        # and forwards over http, so the client's real scheme is in
        # x-forwarded-proto. On localhost (no proxy header, http) both branches
        # are skipped, so local dev is untouched. HSTS is omitted by design when
        # the connection isn't secure — never pin localhost to https-only.
        _HSTS_MAX_AGE = os.environ.get("BRAIN_HSTS_MAX_AGE", "31536000")  # 1 year

        @app.middleware("http")
        async def _https_and_hsts(request: Request, call_next):
            from fastapi.responses import RedirectResponse

            fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
            is_secure = fwd_proto == "https" or request.url.scheme == "https"

            # Client reached us over plain http through a proxy → bounce to https.
            # (Railway's edge already does this; this covers any other front door
            # and makes the guarantee app-level rather than infra-dependent.)
            if fwd_proto == "http":
                https_url = request.url.replace(scheme="https")
                return RedirectResponse(str(https_url), status_code=301)

            response = await call_next(request)
            if is_secure and _HSTS_MAX_AGE != "0":
                response.headers.setdefault("Strict-Transport-Security", f"max-age={_HSTS_MAX_AGE}")
            return response

        @app.get("/login")
        async def login_page():
            return HTMLResponse(LOGIN_HTML_PATH.read_text(encoding="utf-8"))

        @app.post("/auth/login")
        async def auth_login(request: Request):
            if not ui_auth.is_configured():
                return JSONResponse(
                    {"ok": False, "error": "Authentication is not configured."},
                    status_code=503,
                )
            body = await request.json()
            email = str(body.get("email", "")).strip()
            password = str(body.get("password", ""))
            if not email or not password:
                return JSONResponse(
                    {"ok": False, "error": "Email and password are required."},
                    status_code=400,
                )
            session = await ui_auth.password_login(email, password)
            if not session or not session.get("access_token"):
                return JSONResponse(
                    {"ok": False, "error": "Invalid email or password."},
                    status_code=401,
                )
            remember = bool(body.get("remember", True))
            resp = JSONResponse({"ok": True, "next": ui_auth.safe_next(body.get("next"))})
            ui_auth.set_session_cookies(resp, session, remember=remember)
            return resp

        @app.post("/auth/forgot")
        async def auth_forgot(request: Request):
            # Always answer ok — never disclose whether the address has an
            # account (email-enumeration defense). The recovery email only
            # actually sends if Supabase auth + email templates are configured.
            if ui_auth.is_configured():
                body = await request.json()
                # Land the email's link on our own reset page, derived from the
                # request so it's correct on localhost and on Railway alike.
                reset_url = str(request.base_url).rstrip("/") + "/auth/reset"
                await ui_auth.request_password_reset(
                    str(body.get("email", "")).strip(), redirect_to=reset_url
                )
            return JSONResponse({"ok": True})

        @app.get("/auth/reset")
        async def reset_page():
            # Supabase bounces the recovery link here with the token in the URL
            # hash. The page reads it client-side and sets the new password via
            # GoTrue. Inject the public Supabase URL + anon key (both publishable).
            html = RESET_HTML_PATH.read_text(encoding="utf-8")
            html = html.replace("__SUPABASE_URL__", os.environ.get("SUPABASE_URL", "").rstrip("/"))
            html = html.replace("__SUPABASE_ANON_KEY__", os.environ.get("SUPABASE_ANON_KEY", ""))
            return HTMLResponse(html)

        @app.post("/auth/admission")
        async def auth_admission(request: Request):
            # Route admission requests through thegaim.app's Resend mail service
            # to a real inbox (configurable; never the user's hardcoded address).
            from brain.ui import mailer

            body = await request.json()
            applicant = str(body.get("email", "")).strip()
            note = str(body.get("note", "")).strip()
            if not applicant:
                return JSONResponse(
                    {"ok": False, "error": "An email is required."}, status_code=400
                )
            to = os.environ.get("ADMISSION_NOTIFY_EMAIL", "").strip() or "admin@thegaim.app"
            safe_applicant = html_escape(applicant)
            safe_note = html_escape(note) if note else ""
            note_html = (
                f"<p style='margin:16px 0 0;color:#52525b'><strong>Note:</strong> {safe_note}</p>"
                if safe_note
                else ""
            )
            html_body = (
                '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
                'max-width:520px;margin:0 auto;color:#18181b">'
                "<h2 style='font-weight:600'>New Elyceum admission request</h2>"
                f"<p style='color:#52525b'><strong>{safe_applicant}</strong> "
                "has requested admission to Elyceum.</p>"
                f"{note_html}"
                "<p style='margin-top:24px;color:#71717a;font-size:13px'>"
                "Provision the account via <code>scripts/create_user.py</code> if approved.</p>"
                "</div>"
            )
            text_body = f"New Elyceum admission request from {applicant}." + (
                f"\n\nNote: {note}" if note else ""
            )
            await mailer.send_email(
                to,
                "Elyceum — new admission request",
                html_body,
                text=text_body,
            )
            # Always ok: the applicant shouldn't learn whether mail actually sent.
            return JSONResponse({"ok": True})

        @app.post("/auth/logout")
        async def auth_logout():
            resp = JSONResponse({"ok": True})
            ui_auth.clear_session_cookies(resp)
            return resp

        @app.get("/auth/me")
        async def auth_me(request: Request):
            # Gated by the auth middleware, which attaches the verified claims.
            claims = getattr(request.state, "user", None) or {}
            return JSONResponse(
                {
                    "email": claims.get("email"),
                    # is_admin = platform super-user (sets ceilings, cross-org god
                    # view). org_admin = may manage THIS org's agents/roles/keys.
                    "is_admin": ui_auth.is_admin(claims),
                    "org_admin": ui_auth.is_org_admin(claims),
                }
            )

        @app.get("/health")
        async def health():
            # tier lets the gateway gate the shared GPU pod on full-tier brains only
            # (the provisioner records it off this response). Default 'full' if unknown.
            tier = "full"
            if self._tier_fn is not None:
                try:
                    tier = self._tier_fn()
                except Exception:
                    pass
            return {"status": "ok", "tier": tier}

        @app.get("/")
        async def index():
            html = HTML_PATH.read_text(encoding="utf-8")
            # Replace the manual ?v=N cache-busters on the settings assets with
            # a content-derived token (newest asset mtime). The hand-bumped
            # numbers have repeatedly gone stale — edits shipped without a bump
            # served old JS and masqueraded as app bugs. The literal v=N stays
            # in the file as a fallback for static serving (preview/dev).
            try:
                import re as _re

                _dir = HTML_PATH.parent
                _assets = (
                    "settings.css",
                    "settings-data.js",
                    "settings-ui.js",
                    "workspaces.css",
                    "workspaces.js",
                )
                _stamp = max(
                    int((_dir / f).stat().st_mtime) for f in _assets if (_dir / f).exists()
                )
                html = _re.sub(
                    r"(settings(?:-data|-ui)?|workspaces)\.(css|js)\?v=\d+",
                    rf"\1.\2?v={_stamp}",
                    html,
                )
            except Exception as _cb_err:
                logger.debug("[ui] cache-bust injection failed: %s", _cb_err)
            # Always revalidate: a stale cached shell has repeatedly masqueraded
            # as an app bug (missing sub-tabs, dead mic). The page is rebuilt
            # per-request anyway, so caching buys nothing.
            return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

        @app.get("/settings")
        async def get_settings(request: Request):
            from brain.settings import API_KEY_ENV, DEFAULTS, settings

            s = settings.all()
            # API-key status: prefer the Supabase Vault (per-user, encrypted) when
            # configured + authenticated; otherwise fall back to local settings.json.
            # Never ship the secret VALUES to the browser — the fields load blank.
            vault_status = None
            token = getattr(request.state, "access_token", None)
            if token and os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_ANON_KEY"):
                try:
                    from brain import vault

                    st = vault.get_status(token)
                    vault_status = {
                        f"api_key_{p}": bool(v) for p, v in st.items() if p in vault.VALID_PROVIDERS
                    }
                except Exception as e:
                    logger.warning("[settings] vault status unavailable: %s", e)
            secrets_set = {}
            for k in API_KEY_ENV:
                secrets_set[k] = (
                    bool(vault_status.get(k))
                    if vault_status is not None
                    else bool(str(s.get(k) or "").strip())
                )
                s[k] = ""
            # Return the active persona's self.md from the schema store (Supabase or
            # local) so the Sense-of-Self Seed page shows the real stored content,
            # not the static JS template.
            self_md = ""
            try:
                from brain.second_brain.store import SchemaStore

                self_md = SchemaStore(persona=str(s.get("persona_name", ""))).read("self.md")
            except Exception as _sm_err:
                logger.warning("[settings] self.md read failed: %s", _sm_err)
            # Per-persona non-chemistry dial positions, so the UI poses the
            # cognitive + motivation needles per persona (temperament poses from
            # chemistry; these have none). Cognitive positions are authored;
            # motivation positions are derived from the reward table so the needle
            # reflects the same per-persona reward profile the brain actually uses.
            return {
                "settings": s,
                "defaults": DEFAULTS,
                "secrets_set": secrets_set,
                "self_md": self_md,
                "persona_dial_positions": _persona_dial_positions(),
            }

        @app.post("/settings")
        async def save_settings(request: Request):
            from brain.settings import settings

            body = await request.json()
            # Motor authorization keys govern host filesystem + capability grants,
            # so who may set them is gated here too (not just in the UI) — a
            # hand-crafted POST must not be able to widen the sandbox. Policy:
            #   • auth disabled (local dev)  → unrestricted;
            #   • platform super-admin       → unrestricted (self-hosted / cross-org);
            #   • org admin                  → may set its OWN org's motor capability
            #     + paths, but filesystem roots are jailed to the tenant root
            #     (can't escape the pod's own volume — defence in depth);
            #   • anyone else                → motor keys stripped.
            if not ui_auth.is_disabled():
                _claims = getattr(request.state, "user", None) or {}
                if ui_auth.is_admin(_claims):
                    pass  # platform super-admin: unrestricted
                elif ui_auth.is_org_admin(_claims):
                    _jail_motor_dirs(body)  # confine FS roots to this tenant
                else:
                    _stripped = [
                        k
                        for k in list(body)
                        if k.startswith("motor_") or k == "ralph_max_total_attempts"
                    ]
                    for k in _stripped:
                        body.pop(k, None)
                    if _stripped:
                        logger.warning(
                            "[settings] Non-admin tried to set motor keys %s — stripped",
                            _stripped,
                        )
            # Config-only save: persist a (possibly non-running) persona's profile —
            # its own resting/boot chemistry file + the persona_store snapshot + its
            # self.md — WITHOUT switching the live brain or re-execing. Decouples
            # "configure" from "activate": editing a persona you're not running never
            # restarts the process (the switch stays the explicit Open-in-MRI action).
            _config_persona = str(body.get("config_persona") or "").strip()
            if _config_persona:
                from fastapi.responses import JSONResponse

                from brain import persona_chem

                _cc = body.get("config_chem") or {}
                _ci = body.get("config_chem_init") or {}
                try:
                    if _cc:
                        persona_chem.save_resting(
                            _config_persona,
                            {ch: float(_cc[ch]) for ch in persona_chem.CHANNELS if ch in _cc},
                        )
                    if _ci:
                        persona_chem.save_current(
                            _config_persona,
                            {ch: float(_ci[ch]) for ch in persona_chem.CHANNELS if ch in _ci},
                            {},
                        )
                except Exception as _ce:
                    logger.warning("[settings] config chem write failed for %s: %s", _config_persona, _ce)
                if "persona_store" in body:
                    settings.save({"persona_store": str(body["persona_store"])})
                _csm = body.get("config_self_md")
                if _csm is not None:
                    try:
                        from brain.second_brain.store import SchemaStore

                        SchemaStore(persona=_config_persona).write("self.md", str(_csm))
                    except Exception as _se:
                        logger.warning("[settings] config self.md write failed: %s", _se)
                return JSONResponse({"ok": True})

            try:
                # API keys route to the Supabase Vault (per-user, encrypted) when
                # configured; otherwise they persist to local settings.json
                # (single-user dev). Either way they're removed from the
                # settings.json patch, and an empty field leaves the stored key
                # unchanged (never let a blank wipe a saved secret).
                from brain.settings import API_KEY_ENV

                _token = getattr(request.state, "access_token", None)
                _vault_on = bool(
                    _token
                    and os.environ.get("SUPABASE_URL")
                    and os.environ.get("SUPABASE_ANON_KEY")
                )
                for _k in list(body):
                    if _k not in API_KEY_ENV:
                        continue
                    _val = str(body.pop(_k) or "").strip()
                    if not _val:
                        continue  # blank = keep existing
                    if _vault_on:
                        from brain import vault

                        vault.set_key(_token, _k.replace("api_key_", ""), _val)
                        # Apply live so a not-yet-constructed client picks it up
                        # this session too (clients read os.environ lazily).
                        os.environ[API_KEY_ENV[_k]] = _val
                    else:
                        body[_k] = _val  # local dev: persist to settings.json

                # self_md goes to the SchemaStore (Supabase brain_schemas table or
                # local file), never into settings.json.
                _self_md = str(body.pop("self_md", "") or "").strip()
                if _self_md:
                    _persona_name = str(
                        body.get("persona_name") or settings.get("persona_name", "") or ""
                    )
                    try:
                        from brain.second_brain.store import SchemaStore

                        SchemaStore(persona=_persona_name).write("self.md", _self_md)
                    except Exception as _sm_err:
                        logger.warning("[settings] self.md write failed: %s", _sm_err)

                prior_persona = str(settings.get("persona_name", ""))
                settings.save(body)
                # A persona SWITCH posts the new persona's chem_baseline/chem_init
                # (= its resting profile) wholesale. Don't treat that as a slider
                # edit — it would overwrite the incoming persona's saved evolved
                # state with its baseline. Boot re-materializes chem from the
                # persona's own file (resting from the table, current preserved).
                is_switch = (
                    "persona_name" in body
                    and str(settings.get("persona_name", "")) != prior_persona
                )
                # Creating a NEW persona arrives as a switch to a name with no
                # chemistry file yet. Seed its file from the posted chem_baseline/
                # chem_init so it persists immediately (and isn't reliant on the
                # boot-time seed reading whatever global chem happens to be set).
                if is_switch and any(k.startswith("chem_baseline_") for k in body):
                    from brain import persona_chem

                    _new = str(settings.get("persona_name", ""))
                    if _new and not persona_chem.exists(_new):
                        persona_chem.save_resting(
                            _new,
                            {
                                ch: float(settings.get(f"chem_baseline_{ch}"))
                                for ch in persona_chem.CHANNELS
                            },
                        )
                        persona_chem.save_current(
                            _new,
                            {
                                ch: float(settings.get(f"chem_init_{ch}"))
                                for ch in persona_chem.CHANNELS
                            },
                            {},
                        )
                # If the user edited resting/boot chemistry sliders, persist them
                # to the ACTIVE persona's own file so the edit sticks per-persona
                # instead of leaking through global settings on the next switch.
                edited_resting = not is_switch and any(k.startswith("chem_baseline_") for k in body)
                edited_boot = not is_switch and any(k.startswith("chem_init_") for k in body)
                if edited_resting or edited_boot:
                    persona = str(settings.get("persona_name", ""))
                    if persona:
                        from brain import persona_chem

                        if edited_resting:
                            persona_chem.save_resting(
                                persona,
                                {
                                    ch: float(settings.get(f"chem_baseline_{ch}"))
                                    for ch in persona_chem.CHANNELS
                                },
                            )
                        if edited_boot:
                            persona_chem.save_current(
                                persona,
                                {
                                    ch: float(settings.get(f"chem_init_{ch}"))
                                    for ch in persona_chem.CHANNELS
                                },
                                {},
                            )
                # Apply a resting-chemistry change LIVE: the bus caches baselines
                # at init for the hot decay path, so without this a temperament
                # dial's chem_baseline_* component only lands on the next boot
                # while its live-read components apply instantly (mixed-latency
                # dials). Levels are untouched — only the relaxation setpoints.
                if (edited_resting or is_switch) and self._bus is not None:
                    try:
                        self._bus.rebaseline_chem()
                    except Exception as _rb_err:
                        logger.warning("live rebaseline failed: %s", _rb_err)
                # A persona switch MUST re-key storage. Two layers, because memory
                # bleeding across personas is never acceptable:
                #  1) Re-point BRAIN_PERSONA_NAME immediately — the Supabase stores
                #     resolve it per call, so episode/schema/dmn_state writes stop
                #     landing in the old persona's bucket the moment this returns.
                #  2) Re-exec the process — file-backed state (SECOND_BRAIN_PATH:
                #     jobs/, research/, proposals/, dmn_*.json, wiring) is resolved
                #     at import time and can only re-namespace through a boot. Do
                #     it automatically rather than trusting the operator to restart.
                if is_switch:
                    import sys as _sys

                    from brain.persona_key import persona_slug as _pslug

                    _new_persona = str(settings.get("persona_name", ""))
                    _new_slug = _pslug(_new_persona, "unnamed")
                    os.environ["BRAIN_PERSONA_NAME"] = _new_slug

                    async def _restart_for_switch():
                        await asyncio.sleep(0.4)
                        cmd = [_sys.executable] + _sys.argv
                        logger.info(
                            "[Persona] Switch to %r — restarting to re-namespace "
                            "file-backed state: %s",
                            _new_slug,
                            " ".join(cmd),
                        )
                        os.execv(_sys.executable, cmd)

                    asyncio.create_task(_restart_for_switch())
                    return {"ok": True, "restarting": True, "persona": _new_persona}
                # Apply a voice change LIVE so the settings-page Voice dropdown
                # (which only writes the setting) takes effect immediately, just
                # like the header pill's set_voice — no restart required.
                if self._on_voice_change and any(k.startswith("persona_voice_") for k in body):
                    from brain.persona_chem import _slug

                    persona = str(settings.get("persona_name", ""))
                    vid = ""
                    if persona:
                        vid = str(settings.get(f"persona_voice_{_slug(persona)}", "")).strip()
                    if not vid:
                        vid = str(settings.get("persona_voice_id", "")).strip()
                    if vid:
                        self._on_voice_change(vid)
                return {"ok": True}
            except Exception as e:
                from fastapi.responses import JSONResponse

                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        @app.post("/settings/reset")
        async def reset_settings():
            from brain.settings import settings

            settings.reset_to_defaults()
            settings.save()
            return {"ok": True, "settings": settings.all()}

        @app.get("/connectors")
        async def list_connectors(request: Request):
            names: list = []
            if self._connectors_fn is not None:
                try:
                    names = list(self._connectors_fn() or [])
                except Exception as _cn_err:
                    logger.debug("[connectors] list failed: %s", _cn_err)
            # The full view (URLs + env-managed flag) is admin-only; bare names are
            # needed by any admin for the per-agent connector permission grid.
            if request.query_params.get("full"):
                claims = getattr(request.state, "user", None) or {}
                if not (ui_auth.is_disabled() or ui_auth.is_org_admin(claims)):
                    from fastapi import HTTPException

                    raise HTTPException(status_code=403, detail="org admin only")
                from brain.clusters.cma_executor import is_env_managed, list_connector_details

                cloud = None
                if self._cloud_status_fn is not None:
                    try:
                        cloud = self._cloud_status_fn()
                    except Exception:
                        cloud = None
                try:
                    return {
                        "connectors": names,
                        "details": list_connector_details(),
                        "env_managed": is_env_managed(),
                        # The cloud connector (Claude) the MCP connectors run through.
                        "cloud": cloud,
                    }
                except Exception:
                    pass
            return {"connectors": names}

        @app.post("/connectors")
        async def register_connector_ui(request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            _mandate_admin_or_403(request)
            body = await request.json()
            name = str((body or {}).get("name", "")).strip()
            url = str((body or {}).get("url", "")).strip()
            display_name = str((body or {}).get("display_name", "")).strip()
            if not name or not url:
                raise HTTPException(status_code=400, detail="name and url are required")
            from brain.clusters.cma_executor import register_connector

            try:
                secret = register_connector(name, url, display_name)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if self._connector_reload_fn is not None:
                try:
                    self._connector_reload_fn()
                except Exception as _rl_err:
                    logger.warning("[connectors] reload after register failed: %s", _rl_err)
            env_key = name.upper().replace("-", "_")
            return JSONResponse(
                {
                    "name": name,
                    "secret": secret,
                    "brain_env_var": f"BRAIN_CMA_MCP_{env_key}_TOKEN",
                    "app_env_var": f"{env_key}_MCP_SECRET",
                }
            )

        @app.delete("/connectors/{name}")
        async def remove_connector_ui(name: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            _mandate_admin_or_403(request)
            from brain.clusters.cma_executor import remove_connector

            if not remove_connector(name):
                raise HTTPException(status_code=404, detail="connector not found")
            if self._connector_reload_fn is not None:
                try:
                    self._connector_reload_fn()
                except Exception as _rl_err:
                    logger.warning("[connectors] reload after remove failed: %s", _rl_err)
            return JSONResponse({"ok": True})

        @app.post("/tasks/clear")
        async def tasks_clear():
            if self._on_tasks_clear is None:
                return {"ok": False, "error": "no task handler wired"}
            try:
                stats = self._on_tasks_clear() or {}
            except Exception as _tc_err:
                logger.warning("[tasks] clear failed: %s", _tc_err)
                return {"ok": False, "error": str(_tc_err)}
            return {"ok": True, **stats}

        @app.post("/tasks/kill")
        async def tasks_kill(request: Request):
            if self._on_task_kill is None:
                return {"ok": False, "error": "no task handler wired"}
            try:
                body = await request.json()
            except Exception:
                body = {}
            job_id = str((body or {}).get("job_id", "")).strip()
            if not job_id:
                return {"ok": False, "error": "missing job_id"}
            try:
                stats = self._on_task_kill(job_id) or {}
            except Exception as _tk_err:
                logger.warning("[tasks] kill failed: %s", _tk_err)
                return {"ok": False, "error": str(_tk_err)}
            return {"ok": True, **stats}

        @app.get("/tasks/approvals")
        async def tasks_approvals():
            if self._approvals_fn is None:
                return {"approvals": []}
            try:
                return {"approvals": self._approvals_fn() or []}
            except Exception as _ae:
                logger.warning("[tasks] approvals list failed: %s", _ae)
                return {"approvals": []}

        async def _resolve_approval(request: Request, handler, label):
            if handler is None:
                return {"ok": False, "error": "no approval handler wired"}
            try:
                body = await request.json()
            except Exception:
                body = {}
            approval_id = str((body or {}).get("id", "")).strip()
            if not approval_id:
                return {"ok": False, "error": "missing id"}
            try:
                return handler(approval_id) or {"ok": True}
            except Exception as _err:
                logger.warning("[tasks] %s failed: %s", label, _err)
                return {"ok": False, "error": str(_err)}

        @app.post("/tasks/approve")
        async def tasks_approve(request: Request):
            return await _resolve_approval(request, self._on_task_approve, "approve")

        @app.post("/tasks/skip")
        async def tasks_skip(request: Request):
            return await _resolve_approval(request, self._on_task_skip, "skip")

        @app.get("/tasks/jobs")
        async def tasks_jobs(request: Request):
            if self._jobs_list_fn is None:
                return {"jobs": []}
            try:
                limit = int(request.query_params.get("limit", "50"))
            except ValueError:
                limit = 50
            state = str(request.query_params.get("state", "")).strip() or None
            try:
                return {"jobs": self._jobs_list_fn(limit=limit, state=state) or []}
            except Exception as _je:
                logger.warning("[tasks] jobs list failed: %s", _je)
                return {"jobs": []}

        @app.get("/tasks/jobs/{job_id}")
        async def tasks_job_detail(job_id: str):
            from fastapi.responses import JSONResponse

            if self._job_get_fn is None:
                return JSONResponse({"error": "jobs not wired"}, status_code=404)
            try:
                rec = self._job_get_fn(job_id)
            except Exception as _jd_err:
                logger.warning("[tasks] job get failed: %s", _jd_err)
                rec = None
            if not rec:
                return JSONResponse({"error": "job not found"}, status_code=404)
            return rec

        @app.get("/self-model")
        async def get_self_model(request: Request):
            from fastapi.responses import JSONResponse

            from brain.settings import settings

            # The settings UI can view any persona's Sense of Self, not just the
            # active one — honor ?persona=<display name> (SchemaStore slugifies).
            persona_name = str(
                request.query_params.get("persona", "").strip() or settings.get("persona_name", "")
            )
            content = ""
            try:
                from brain.second_brain.store import SchemaStore

                content = SchemaStore(persona=persona_name).read("self.md")
            except Exception as _e:
                logger.warning("[self-model] read failed: %s", _e)
            return JSONResponse({"content": content, "persona": persona_name})

        @app.get("/agents/turns")
        async def get_agent_turns(request: Request):
            """Durable history for the Agents view — what each engine-API agent
            (e.g. the trading app) has been asked, separate from the main chat.
            Owner-authed by the global gate; reads the org's own agent_turns rows."""
            from fastapi.responses import JSONResponse

            from brain import agent_log

            agent_id = str(request.query_params.get("agent_id", "").strip()) or None
            try:
                limit = int(request.query_params.get("limit", "50"))
            except ValueError:
                limit = 50
            turns = await asyncio.to_thread(agent_log.recent, limit, agent_id)
            return JSONResponse({"turns": turns})

        @app.get("/user-model")
        async def get_user_model(request: Request):
            from fastapi.responses import JSONResponse

            from brain.settings import settings

            # Read-only "Sense of You" tab: the persona's model of the user
            # (user.md), written during sleep consolidation. Same persona
            # resolution as /self-model.
            persona_name = str(
                request.query_params.get("persona", "").strip() or settings.get("persona_name", "")
            )
            content = ""
            try:
                from brain.second_brain.store import SchemaStore

                content = SchemaStore(persona=persona_name).read("user.md")
            except Exception as _e:
                logger.warning("[user-model] read failed: %s", _e)
            return JSONResponse({"content": content, "persona": persona_name})

        # ── Roles / mandates: the org's role library + per-persona assignments ──
        # Reads are open to any member; writes are admin-gated (mirrors the Motor
        # Permissions gate). When the Supabase backend is off the section reports
        # enabled:false so the UI hides the tab.
        def _mandate_admin_or_403(request: Request) -> None:
            # Managing this org's agents / roles / connectors / keys is an
            # org-admin action (the per-agent narrowing within the account
            # ceilings). The platform super-user sets the ceilings and gets the
            # cross-org view elsewhere; it is implicitly an org-admin too.
            if ui_auth.is_disabled():
                return
            claims = getattr(request.state, "user", None) or {}
            if not ui_auth.is_org_admin(claims):
                from fastapi import HTTPException

                raise HTTPException(status_code=403, detail="org admin only")

        @app.get("/mandates")
        async def list_mandates_ui(request: Request):
            from fastapi.responses import JSONResponse

            from brain.second_brain import supabase_client

            claims = getattr(request.state, "user", None) or {}
            is_admin = ui_auth.is_disabled() or ui_auth.is_admin(claims)
            if not supabase_client.is_enabled():
                return JSONResponse(
                    {"enabled": False, "mandates": [], "assignments": {}, "is_admin": is_admin}
                )
            from brain import mandates

            # Roles are org-level and many-to-many with personas; return the whole
            # library plus every pairing so the UI can render the full matrix.
            try:
                lib = mandates.list_mandates(include_inactive=False)
                assigns = mandates.list_all_assignments()
            except Exception as e:
                logger.warning("[mandates] list failed: %s", e)
                return JSONResponse(
                    {"enabled": True, "mandates": [], "assignments": [], "is_admin": is_admin}
                )
            return JSONResponse(
                {
                    "enabled": True,
                    "is_admin": is_admin,
                    "mandates": lib,
                    "assignments": assigns,
                }
            )

        @app.post("/mandates")
        async def upsert_mandate_ui(request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.mandates import MandateError

            _mandate_admin_or_403(request)
            body = await request.json()
            mid = str((body or {}).get("id", "")).strip()
            role_text = (body or {}).get("role_text", "")
            if not isinstance(role_text, str):
                raise HTTPException(status_code=400, detail="role_text (string) is required")
            try:
                from brain import mandates

                row = mandates.upsert_mandate(mid, role_text)
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            return JSONResponse({"ok": True, "mandate": row})

        @app.delete("/mandates/{mandate_id}")
        async def deactivate_mandate_ui(mandate_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.mandates import MandateError

            _mandate_admin_or_403(request)
            try:
                from brain import mandates

                ok = mandates.deactivate_mandate(mandate_id)
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if not ok:
                raise HTTPException(status_code=404, detail="unknown mandate id")
            return JSONResponse({"ok": True})

        @app.post("/mandates/{mandate_id}/assign")
        async def assign_mandate_ui(mandate_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.mandates import MandateError

            _mandate_admin_or_403(request)
            body = await request.json()
            persona = (body or {}).get("persona")
            assigned = bool((body or {}).get("assigned", True))
            try:
                from brain import mandates

                if assigned:
                    mandates.assign(persona, mandate_id)
                else:
                    mandates.unassign(persona, mandate_id)
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            return JSONResponse({"ok": True, "assigned": assigned})

        # ── App-provided skills: the org's skill library + admission review queue ──
        # Skills are partner-supplied content injected into the agent's prompt, so a
        # submission is SCREENED (brain/skills_screener) before going live. Authoring
        # happens over the engine API; this surface is for the org admin to review the
        # flagged queue (approve/reject) and manage the library. Admin-gated like roles.
        async def _rewarm_skills() -> None:
            if self._skill_rewarm_fn is None:
                return
            try:
                await self._skill_rewarm_fn()
            except Exception as e:  # noqa: BLE001 — a rewarm miss self-heals at next boot
                logger.warning("[skills] rewarm failed: %s", e)

        @app.get("/skills")
        async def list_skills_ui(request: Request):
            from fastapi.responses import JSONResponse

            from brain.second_brain import supabase_client

            claims = getattr(request.state, "user", None) or {}
            is_admin = ui_auth.is_disabled() or ui_auth.is_org_admin(claims)
            if not supabase_client.is_enabled():
                return JSONResponse(
                    {"enabled": False, "is_admin": is_admin, "skills": [], "flagged": []}
                )
            from brain import skills_registry as sr

            try:
                skills = sr.list_skills(include_inactive=False)
                # The flagged queue carries untrusted bodies + screener notes — admin only.
                flagged = sr.list_flagged() if is_admin else []
            except Exception as e:
                logger.warning("[skills] list failed: %s", e)
                return JSONResponse(
                    {"enabled": True, "is_admin": is_admin, "skills": [], "flagged": []}
                )
            return JSONResponse(
                {"enabled": True, "is_admin": is_admin, "skills": skills, "flagged": flagged}
            )

        @app.post("/skills")
        async def create_skill_ui(request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.skills_registry import SkillError

            _mandate_admin_or_403(request)
            body = await request.json()
            sid = str((body or {}).get("id", "")).strip()
            skill_body = (body or {}).get("body", "")
            if not isinstance(skill_body, str) or not skill_body.strip():
                raise HTTPException(status_code=400, detail="body (non-empty string) is required")
            from brain import skills_registry as sr

            try:
                sr.stage_skill(
                    sid,
                    skill_body,
                    str((body or {}).get("description", "") or ""),
                    display_name=(body or {}).get("display_name") or None,
                    keywords=(body or {}).get("keywords") or None,
                    tier=int((body or {}).get("tier", 2) or 2),
                    submitted_by="ui-admin",
                )
                # Authored in the trusted settings surface by the org admin (who could
                # approve any submission anyway), so it goes live directly. The screener
                # gates UNTRUSTED engine-API submissions, not the owner's own authoring.
                sr.set_status(sid, "enabled", reviewed_by="ui-admin")
            except SkillError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            await _rewarm_skills()
            return JSONResponse({"ok": True, "id": sid, "status": "enabled"})

        @app.get("/skills/{skill_id}")
        async def get_skill_ui(skill_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            _mandate_admin_or_403(request)
            from brain import skills_registry as sr
            from brain.skills_registry import SkillError

            try:
                row = sr.get_skill(skill_id)
            except SkillError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if row is None:
                raise HTTPException(status_code=404, detail="unknown skill id")
            return JSONResponse({"skill": row})

        @app.post("/skills/{skill_id}/agents")
        async def set_skill_agents_ui(skill_id: str, request: Request):
            """Set a skill's audience: all agents, or a specific set. Body:
            {all_agents: bool, agents: ['persona.mandate_id', ...]}."""
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.skills_registry import SkillError

            _mandate_admin_or_403(request)
            body = await request.json()
            all_agents = bool((body or {}).get("all_agents", True))
            agents = (body or {}).get("agents") or []
            if not isinstance(agents, list):
                raise HTTPException(status_code=400, detail="agents must be a list")
            from brain import skills_registry as sr

            try:
                sr.set_skill_all_agents(skill_id, all_agents)
                # When global, clear any specific mappings so they don't linger.
                sr.set_skill_agents(skill_id, [] if all_agents else [str(a) for a in agents])
            except SkillError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            await _rewarm_skills()
            return JSONResponse(
                {
                    "ok": True,
                    "id": skill_id,
                    "all_agents": all_agents,
                    "agents": [] if all_agents else [str(a) for a in agents],
                }
            )

        @app.post("/skills/{skill_id}/approve")
        async def approve_skill_ui(skill_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.skills_registry import SkillError

            _mandate_admin_or_403(request)
            from brain import skills_registry as sr

            try:
                if sr.get_skill(skill_id) is None:
                    raise HTTPException(status_code=404, detail="unknown skill id")
                sr.set_status(skill_id, "enabled", reviewed_by="ui-admin")
            except SkillError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            await _rewarm_skills()
            return JSONResponse({"ok": True, "status": "enabled"})

        @app.post("/skills/{skill_id}/reject")
        async def reject_skill_ui(skill_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.skills_registry import SkillError

            _mandate_admin_or_403(request)
            body = await request.json()
            from brain import skills_registry as sr

            try:
                existing = sr.get_skill(skill_id)
                if existing is None:
                    raise HTTPException(status_code=404, detail="unknown skill id")
                notes = dict(existing.get("screen_notes") or {})
                notes["review"] = {
                    "action": "rejected",
                    "reason": str((body or {}).get("reason") or ""),
                }
                sr.set_status(skill_id, "rejected", screen_notes=notes, reviewed_by="ui-admin")
            except SkillError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            # A rejected edit never goes live (prior approved body, if any, keeps serving),
            # so no rewarm is needed.
            return JSONResponse({"ok": True, "status": "rejected"})

        @app.delete("/skills/{skill_id}")
        async def delete_skill_ui(skill_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.skills_registry import SkillError

            _mandate_admin_or_403(request)
            from brain import skills_registry as sr

            try:
                ok = sr.delete_skill(skill_id)
            except SkillError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if not ok:
                raise HTTPException(status_code=404, detail="unknown skill id")
            await _rewarm_skills()
            return JSONResponse({"ok": True})

        # ── Agents: the (persona, role) pairings + per-agent permission narrowing ──
        # An agent is created by assigning a role to a persona; its permissions are
        # a narrowing WITHIN the org/account ceiling (settings.json). Admin-gated.
        @app.get("/agents")
        async def list_agents_ui(request: Request):
            from fastapi.responses import JSONResponse

            from brain.second_brain import supabase_client

            claims = getattr(request.state, "user", None) or {}
            is_admin = ui_auth.is_disabled() or ui_auth.is_admin(claims)
            if not supabase_client.is_enabled():
                return JSONResponse(
                    {
                        "enabled": False,
                        "agents": [],
                        "roles": [],
                        "ceilings": {},
                        "is_admin": is_admin,
                    }
                )
            from brain import agents as _agents
            from brain import mandates
            from brain.settings import settings as _s

            try:
                ags = _agents.list_agents()
                roles = mandates.list_mandates(include_inactive=False)
            except Exception as e:
                logger.warning("[agents] list failed: %s", e)
                return JSONResponse(
                    {
                        "enabled": True,
                        "agents": [],
                        "roles": [],
                        "ceilings": {},
                        "is_admin": is_admin,
                    }
                )
            ceilings = {k: _s.get(k) for k in _agents.PERMISSION_KEYS}
            return JSONResponse(
                {
                    "enabled": True,
                    "is_admin": is_admin,
                    "agents": ags,
                    "roles": roles,
                    "ceilings": ceilings,
                }
            )

        @app.get("/agents/usage")
        async def agents_usage_ui(request: Request):
            """Per-agent model usage (tokens, pod compute-seconds, cloud $) for the
            Agents dashboard cost monitor. No ?since/?until → the live in-memory meter
            (current session). A [since, until] range (ISO-8601) → this org's durable
            ledger summed across restarts. ?scope=all → the cross-org fleet view, but
            ONLY for a platform super-admin (is_admin); any other caller is silently
            forced back to their own org. Returns {scope, usage|rows, since, until}."""
            from fastapi.responses import JSONResponse

            since = request.query_params.get("since") or None
            until = request.query_params.get("until") or None
            claims = getattr(request.state, "user", None) or {}
            is_admin = ui_auth.is_disabled() or ui_auth.is_admin(claims)
            # Cross-org scope is platform-admin only — never honor it for a tenant.
            scope = "all" if (request.query_params.get("scope") == "all" and is_admin) else "org"
            result: dict = {"scope": scope}
            if self._usage_fn is not None:
                try:
                    # May hit Supabase on the ledger paths → keep it off the event loop.
                    result = await asyncio.to_thread(self._usage_fn, since, until, scope) or result
                except Exception as e:
                    logger.debug("[agents] usage fn failed: %s", e)
            return JSONResponse({**result, "since": since, "until": until, "is_admin": is_admin})

        @app.post("/agents")
        async def create_agent_ui(request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.mandates import MandateError

            _mandate_admin_or_403(request)
            body = await request.json()
            persona = (body or {}).get("persona")
            mandate_id = str((body or {}).get("mandate_id", "")).strip()
            name = (body or {}).get("name")
            skills = (body or {}).get("skills")
            try:
                from brain import agents as _agents
                from brain import mandates

                mandates.assign(persona, mandate_id)  # creating an agent = the pairing
                agent_id = f"{mandates._persona(persona)}.{mandate_id}"
                if name:
                    _agents.set_name(agent_id, str(name))
                row = _agents.get(agent_id) or {}
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            # Map the chosen specific-scope skills to the new agent.
            if isinstance(skills, list) and skills:
                from brain.skills_registry import SkillError

                try:
                    from brain import skills_registry as sr

                    sr.set_agent_skills(persona, mandate_id, [str(s) for s in skills])
                except SkillError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                await _rewarm_skills()
            return JSONResponse({"ok": True, "agent": row})

        @app.post("/agents/{agent_id}/permissions")
        async def set_agent_permissions_ui(agent_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.mandates import MandateError

            _mandate_admin_or_403(request)
            body = await request.json()
            perms = (body or {}).get("permissions") or {}
            try:
                from brain import agents as _agents

                row = _agents.set_permissions(agent_id, perms)
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            return JSONResponse({"ok": True, "agent": row})

        @app.post("/agents/{agent_id}/name")
        async def set_agent_name_ui(agent_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.mandates import MandateError

            _mandate_admin_or_403(request)
            body = await request.json()
            try:
                from brain import agents as _agents

                row = _agents.set_name(agent_id, (body or {}).get("name"))
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            return JSONResponse({"ok": True, "agent": row})

        @app.delete("/agents/{agent_id}")
        async def delete_agent_ui(agent_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            from brain.mandates import MandateError

            _mandate_admin_or_403(request)
            try:
                from brain import mandates

                persona, _, mid = str(agent_id).partition(".")
                ok = mandates.unassign(persona, mid)
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if not ok:
                raise HTTPException(status_code=404, detail="unknown agent")
            return JSONResponse({"ok": True})

        # ── Partner keys (admin UI; the engine /v1/partner_keys is bearer-only) ──
        @app.get("/partner_keys")
        async def list_partner_keys_ui(request: Request):
            from fastapi.responses import JSONResponse

            from brain.second_brain import supabase_client

            claims = getattr(request.state, "user", None) or {}
            org_admin = ui_auth.is_disabled() or ui_auth.is_org_admin(claims)
            if not (org_admin and supabase_client.is_enabled()):
                return JSONResponse({"enabled": False, "keys": []})
            from brain.api import auth as _a

            try:
                return JSONResponse({"enabled": True, "keys": _a.list_partner_keys()})
            except Exception as e:
                logger.warning("[partner_keys] list failed: %s", e)
                return JSONResponse({"enabled": True, "keys": []})

        @app.post("/partner_keys")
        async def mint_partner_key_ui(request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            _mandate_admin_or_403(request)
            body = await request.json()
            partner_id = str((body or {}).get("partner_id", "")).strip()
            if not partner_id:
                raise HTTPException(status_code=400, detail="partner_id is required")
            from brain.api import auth as _a

            try:
                return JSONResponse(_a.mint_partner_key(partner_id, (body or {}).get("label")))
            except (ValueError, RuntimeError) as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

        @app.delete("/partner_keys/{key_id}")
        async def revoke_partner_key_ui(key_id: str, request: Request):
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse

            _mandate_admin_or_403(request)
            from brain.api import auth as _a

            if not _a.revoke_partner_key(key_id):
                raise HTTPException(status_code=404, detail="unknown key id")
            return JSONResponse({"ok": True})

        @app.get("/wiring")
        async def get_wiring():
            w = self._wiring
            if w is None:
                return {"top": [], "deltas": [], "edge_count": 0}
            return {
                "top": w.top_edges(20),
                "deltas": w.session_deltas(),
                "edge_count": w.edge_count(),
            }

        # ── Learning surface (MRI → Learning tab) ──────────────────────────
        # Read-only views over what the learning subsystems already persist;
        # the active persona reads live objects, others read their files.

        @app.get("/learning/stories")
        async def learning_stories(request: Request):
            from brain.observability import learning_reader

            try:
                limit = int(request.query_params.get("limit", "50"))
            except ValueError:
                limit = 50
            before = request.query_params.get("before_ts")
            try:
                return learning_reader.stories(
                    persona=str(request.query_params.get("persona", "")),
                    limit=limit,
                    before_ts=float(before) if before else None,
                    live_wiring=self._wiring,
                )
            except Exception as _ls_err:
                logger.warning("[learning] stories failed: %s", _ls_err)
                return {"stories": [], "generated_on_read": False, "personas": []}

        @app.get("/learning/wiring")
        async def learning_wiring(request: Request):
            from brain.observability import learning_reader

            try:
                return learning_reader.wiring_view(
                    persona=str(request.query_params.get("persona", "")),
                    edge=str(request.query_params.get("edge", "")),
                    live_wiring=self._wiring,
                )
            except Exception as _lw_err:
                logger.warning("[learning] wiring failed: %s", _lw_err)
                return {"top": [], "deltas": [], "edge_count": 0}

        @app.post("/feedback")
        async def turn_feedback(request: Request):
            """Thumbs verdict on a brain message — the external-grade write path."""
            if self._on_feedback is None:
                return {"ok": False, "error": "feedback not wired"}
            try:
                body = await request.json()
            except Exception:
                body = {}
            turn_id = str((body or {}).get("turn_id", "")).strip()
            grade = (body or {}).get("grade")
            if not turn_id or grade is None:
                return {"ok": False, "error": "missing turn_id or grade"}
            try:
                return self._on_feedback(turn_id, grade, str(body.get("source", "user_thumbs")))
            except Exception as _fb_err:
                logger.warning("[learning] feedback failed: %s", _fb_err)
                return {"ok": False, "error": str(_fb_err)}

        @app.get("/learning/summary")
        async def learning_summary(request: Request):
            from brain.observability import learning_reader

            try:
                return learning_reader.summary(
                    persona=str(request.query_params.get("persona", "")),
                    live_wiring=self._wiring,
                    live_bus=self._bus,
                )
            except Exception as _lsm_err:
                logger.warning("[learning] summary failed: %s", _lsm_err)
                return {"plasticity": [], "reward_mix": {}, "switches": [], "chunks": {}, "predictor": {}}

        @app.post("/restart")
        async def restart_brain():
            """Re-exec the current process with the same args — restarts the brain."""

            async def _do_restart():
                await asyncio.sleep(0.4)
                cmd = [sys.executable] + sys.argv
                logger.info("Restarting brain: %s", " ".join(cmd))
                os.execv(sys.executable, cmd)

            asyncio.create_task(_do_restart())
            return {"ok": True}

        @app.post("/shutdown")
        async def shutdown_brain():
            """Gracefully shut down the brain process."""

            async def _do_shutdown():
                await asyncio.sleep(0.4)
                os.kill(os.getpid(), __import__("signal").SIGTERM)

            asyncio.create_task(_do_shutdown())
            return {"ok": True}

        @app.get("/voices")
        async def list_voices():
            """Return voices compatible with the configured ElevenLabs model.

            Filtering rules:
              - Always exclude category=premade if any non-premade voices remain
                (the user's own voices are what they're after).
              - If the configured model doesn't serve professional voice clones
                (e.g. eleven_v3 has serves_pro_voices=false), exclude
                category=professional too — calling those with that model
                silently substitutes a default voice.
              - If filtering would yield zero voices, fall back to showing
                premade ones (which work with any model) so the dropdown
                isn't empty.

            Response also includes a `message` field when voices were filtered
            out, so the UI can explain to the user why some are missing.
            """
            import httpx

            api_key = os.environ.get("ELEVENLABS_API_KEY", "")
            if not api_key:
                return {
                    "voices": [],
                    "reason": "no_elevenlabs_key",
                    "message": "ELEVENLABS_API_KEY not set",
                }
            model_id = (
                os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip()
                or "eleven_flash_v2_5"
            )
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    # Fetch both in parallel — model capabilities + voice list
                    voices_resp, models_resp = await asyncio.gather(
                        client.get(
                            "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": api_key}
                        ),
                        client.get(
                            "https://api.elevenlabs.io/v1/models", headers={"xi-api-key": api_key}
                        ),
                    )
                voices_resp.raise_for_status()
                models_resp.raise_for_status()
                voices_raw = voices_resp.json().get("voices", [])

                # eleven_v3 silently substitutes its own default voice when given
                # a Professional Voice Clone voice_id — hide them to prevent that.
                # All other models (flash, turbo, multilingual) work fine with PVCs.
                is_v3 = model_id == "eleven_v3"

                # Categorize the user's voices
                pro_voices = [v for v in voices_raw if v.get("category") == "professional"]
                custom_voices = [
                    v for v in voices_raw if v.get("category") not in ("premade", "professional")
                ]
                premade_voices = [v for v in voices_raw if v.get("category") == "premade"]

                if is_v3:
                    candidates = custom_voices
                    excluded_pro = len(pro_voices)
                else:
                    candidates = custom_voices + pro_voices
                    excluded_pro = 0

                message = ""
                if not candidates:
                    # Fall back to premade so dropdown isn't empty
                    candidates = premade_voices
                    if excluded_pro:
                        message = (
                            f"{excluded_pro} of your voices are Professional Voice Clones, "
                            f"which {model_id} does not serve. Showing premade voices instead. "
                            "Switch to eleven_flash_v2_5 or another non-v3 model to access them."
                        )
                elif excluded_pro:
                    message = (
                        f"Hiding {excluded_pro} Professional Voice Clones — "
                        f"{model_id} does not support them."
                    )

                voices = [{"voice_id": v["voice_id"], "name": v["name"]} for v in candidates]
                return {"voices": voices, "model_id": model_id, "message": message}
            except Exception as e:
                logger.warning("Failed to fetch ElevenLabs voices: %s", e)
                return {"voices": [], "message": f"Failed to fetch voices: {e}"}

        ui_dir = HTML_PATH.parent

        @app.post("/upload_image")
        async def upload_image(file: UploadFile):
            import tempfile

            suffix = Path(file.filename or "upload").suffix or ".jpg"
            content = await file.read()
            with tempfile.NamedTemporaryFile(
                suffix=suffix, prefix="brain_ui_img_", delete=False
            ) as tmp:
                tmp.write(content)
            return {"path": tmp.name}

        @app.get("/{filename}")
        async def static_asset(filename: str):
            from fastapi import HTTPException

            filepath = ui_dir / filename
            if filepath.is_file() and filepath.parent == ui_dir:
                return FileResponse(str(filepath))
            raise HTTPException(status_code=404)

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            # Gate the socket with the same session cookies as the HTTP routes.
            # Reject before accept() so an unauthenticated client never connects.
            if not ui_auth.is_disabled():
                if not ui_auth.is_configured():
                    await websocket.close(code=1008)
                    return
                claims, _ = await ui_auth.authenticate(websocket)
                if claims is None or ui_auth.owner_mismatch(claims):
                    await websocket.close(code=1008)
                    return
            await websocket.accept()
            self._clients.add(websocket)
            logger.info("UI: client connected (%d total)", len(self._clients))

            # Send current mic state so the button reflects reality on connect.
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "mic_state",
                            "status": self._mic_status(),
                        }
                    )
                )

            # Send current neuromod + hormonal state immediately on connect
            if self._last_neuromod:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "neuromod", **self._last_neuromod})
                    )
            if self._last_hormonal:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "hormonal", **self._last_hormonal})
                    )
            if self._last_emotion:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "emotion", "emotion": self._last_emotion})
                    )

            # Tell the client about wiring state (frozen tag in plasticity panel)
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "wiring_status",
                            "frozen": self._wiring_frozen,
                        }
                    )
                )

            # Subsystem health — sent on every connect so status pill is always current
            if self._subsystem_status:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "subsystem_status",
                                **self._subsystem_status,
                            }
                        )
                    )

            # Replay chat history so the conversation survives a page refresh
            if self._chat_history:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "chat_history",
                                "turns": list(self._chat_history),
                            }
                        )
                    )

            # Replay agent-lane history so the Agents view survives a refresh too.
            if self._agent_history:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "agent_history",
                                "turns": list(self._agent_history),
                            }
                        )
                    )

            # Replay recent thoughts so the feed isn't blank on reconnect
            for thought_event in list(self._last_thoughts):
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(thought_event))

            # Run receive + broadcast concurrently for this client
            receive_task = asyncio.create_task(self._receive_loop(websocket))
            try:
                await receive_task
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug("UI: ws error: %s", e)
            finally:
                self._clients.discard(websocket)
                receive_task.cancel()
                logger.info("UI: client disconnected (%d remaining)", len(self._clients))

        return app

    async def _start_deepgram(self, websocket) -> object | None:
        """
        Open a Deepgram live-transcription session for one browser client.
        Compatible with deepgram-sdk v7+ which uses an async context-manager API.
        Returns a DGSession handle with .send(bytes) and .finish() methods.
        """
        try:
            from deepgram import AsyncDeepgramClient
            from deepgram.listen.v1.types import ListenV1Results
        except ImportError:
            logger.warning("deepgram-sdk not installed — mic disabled. Run 'uv sync' to install.")
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    json.dumps({"type": "transcript_error", "msg": "deepgram-sdk not installed"})
                )
            return None

        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not api_key:
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    json.dumps({"type": "transcript_error", "msg": "DEEPGRAM_API_KEY not set"})
                )
            return None

        client = AsyncDeepgramClient()  # picks up DEEPGRAM_API_KEY from env
        audio_queue: asyncio.Queue = asyncio.Queue()

        class DGSession:
            """Thin wrapper so _receive_loop can call .send() and .finish()."""

            def __init__(self) -> None:
                self._task: asyncio.Task | None = None
                # Raw audio bytes for the in-progress utterance. _receive_loop
                # appends incoming chunks here; _listen drains it on each final
                # transcript to publish the full utterance to the auditory bus.
                self.audio_chunks: list[bytes] = []

            async def send(self, data: bytes) -> None:
                await audio_queue.put(data)

            async def finish(self) -> None:
                await audio_queue.put(None)  # sentinel → closes the connection
                if self._task:
                    try:
                        await asyncio.wait_for(asyncio.shield(self._task), timeout=2.0)
                    except Exception:
                        self._task.cancel()

        session = DGSession()

        # Accumulate diarized words across chunks for one utterance so we can
        # publish to the auditory bus on final. (Raw audio bytes are buffered on
        # the DGSession handle as `.audio_chunks`, written by _receive_loop.)
        _diarized_words: list[dict] = []
        _utterance_start_s: float | None = None

        async def _publish_utterance(
            transcript: str, audio_bytes: bytes, words: list[dict]
        ) -> None:
            """Mirror what streaming_mic publishes so the auditory cortex gets
            the same events regardless of whether voice mode is active."""
            if not (self._bus and audio_bytes):
                return
            duration_s = len(audio_bytes) / (16000 * 2)  # 16kHz, int16 = 2 bytes/sample
            try:
                await self._bus.publish_dict(
                    "auditory.raw_audio",
                    {
                        "audio_bytes": audio_bytes,
                        "sample_rate": 16000,
                        "duration_s": duration_s,
                        "channels": 1,
                        "dtype": "int16",
                    },
                    source="ui",
                )
                await self._bus.publish_dict(
                    "auditory.diarized_audio",
                    {
                        "audio_bytes": audio_bytes,
                        "sample_rate": 16000,
                        "duration_s": duration_s,
                        "dtype": "int16",
                        "diarized_words": words,
                        "transcript": transcript,
                    },
                    source="ui",
                )
                logger.debug(
                    "UI: published utterance to auditory bus (%d bytes, %d words)",
                    len(audio_bytes),
                    len(words),
                )
            except Exception as e:
                logger.debug("UI: auditory publish failed: %s", e)

        async def _run_session() -> None:
            nonlocal _diarized_words, _utterance_start_s
            try:
                async with client.listen.v1.connect(
                    model="nova-3",
                    language="en-US",
                    smart_format=True,
                    punctuate=True,
                    interim_results=True,
                    endpointing=500,  # ms of silence before finalising — below ~400
                    # natural intra-sentence pauses split one sentence into several
                    # independently-punctuated finals ("I was thinking. About the.
                    # Project."). Turn latency is governed by the browser's
                    # AUTO_SEND_DELAY_MS, not this, so low values bought nothing.
                    utterance_end_ms=1000,  # also fire on utterance boundary
                    diarize=True,  # enable speaker diarization for auditory cortex
                    # The browser sends raw PCM16 mono @16kHz (AudioWorklet tap).
                    # Raw frames are stateless, so a session can be closed between
                    # turns and reopened mid-capture — unlike WebM/Opus, where a
                    # fresh connection can never decode a headerless mid-stream
                    # chunk (the old failure: mic dead after the first turn).
                    encoding="linear16",
                    sample_rate=16000,
                    channels=1,
                ) as conn:
                    logger.info("UI: Deepgram live session started for client")

                    async def _listen() -> None:
                        nonlocal _diarized_words
                        async for msg in conn:
                            if isinstance(msg, ListenV1Results):
                                try:
                                    alt = msg.channel.alternatives[0]
                                    if alt.transcript:
                                        await websocket.send_text(
                                            json.dumps(
                                                {
                                                    "type": "transcript",
                                                    "text": alt.transcript,
                                                    "is_final": msg.is_final,
                                                }
                                            )
                                        )
                                    if msg.is_final and alt.transcript:
                                        # Harvest diarized words and flush the
                                        # audio buffer to the auditory bus.
                                        words = []
                                        for w in getattr(alt, "words", []) or []:
                                            words.append(
                                                {
                                                    "word": getattr(w, "word", ""),
                                                    "start": getattr(w, "start", 0.0),
                                                    "end": getattr(w, "end", 0.0),
                                                    "speaker": getattr(w, "speaker", 0),
                                                }
                                            )
                                        audio_bytes = b"".join(session.audio_chunks)
                                        session.audio_chunks.clear()
                                        _diarized_words = []
                                        asyncio.create_task(
                                            _publish_utterance(alt.transcript, audio_bytes, words)
                                        )
                                except Exception as e:
                                    logger.debug("Deepgram transcript handler: %s", e)

                    listen_task = asyncio.create_task(_listen())
                    dg_closed_early = False
                    try:
                        while True:
                            chunk = await audio_queue.get()
                            if chunk is None:
                                break
                            # If _listen() finished, Deepgram closed the connection
                            # from their end (timeout, server-side error, etc.).
                            # Break immediately so _run_session exits and the
                            # browser's transcript_error handler can restart.
                            if listen_task.done():
                                logger.info("UI: Deepgram closed connection from their end")
                                dg_closed_early = True
                                break
                            await conn.send_media(chunk)
                    finally:
                        listen_task.cancel()
                        logger.info("UI: Deepgram live session closed")
                    # Notify the browser if Deepgram closed from their end (not
                    # from a voice_stop) so it can reopen a fresh session.
                    if dg_closed_early:
                        with contextlib.suppress(Exception):
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "transcript_error",
                                        "msg": "Deepgram closed connection",
                                    }
                                )
                            )
            except Exception as e:
                logger.warning("Deepgram session error — voice input unavailable: %s", e)
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "transcript_error", "msg": str(e)})
                    )

        session._task = asyncio.create_task(_run_session())
        # Give the connection a moment to establish before we declare success
        await asyncio.sleep(0.3)
        if session._task.done():
            return None  # connection failed immediately
        return session

    async def _start_stt(self, websocket) -> object | None:
        """Provider dispatcher for the browser-capture STT session.
        stt_provider=openai uses Realtime transcription (no per-word diarization
        — multi-speaker attribution degrades to the local fingerprint path);
        anything else, or an OpenAI startup failure, lands on Deepgram."""
        from brain.settings import settings as _settings

        if str(_settings.get("stt_provider", "deepgram")).lower() == "openai" and os.environ.get(
            "OPENAI_API_KEY"
        ):
            s = await self._start_openai_stt(websocket)
            if s is not None:
                return s
            logger.warning("UI: OpenAI STT failed to start — falling back to Deepgram")
        return await self._start_deepgram(websocket)

    async def _start_openai_stt(self, websocket) -> object | None:
        """OpenAI Realtime transcription session for one browser client.
        Same handle contract as _start_deepgram (.send/.finish/.audio_chunks).
        The browser sends 16 kHz PCM16; Realtime expects 24 kHz, so chunks are
        linearly upsampled on the way in."""
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return None
        import base64

        from brain.pns import PNS
        from brain.settings import settings as _settings

        model = str(_settings.get("openai_stt_model", "gpt-4o-transcribe"))
        language = os.environ.get("BRAIN_STT_LANGUAGE", "en").strip() or "en"
        audio_queue: asyncio.Queue = asyncio.Queue()

        class OAISession:
            def __init__(self) -> None:
                self._task: asyncio.Task | None = None
                self.audio_chunks: list[bytes] = []

            async def send(self, data: bytes) -> None:
                await audio_queue.put(data)

            async def finish(self) -> None:
                await audio_queue.put(None)
                if self._task:
                    try:
                        await asyncio.wait_for(asyncio.shield(self._task), timeout=2.0)
                    except Exception:
                        self._task.cancel()

        session = OAISession()

        async def _publish_utterance(transcript: str, audio_bytes: bytes) -> None:
            """Mirror the Deepgram path's bus events; empty word list (no
            diarization), so prosody/fingerprinting still run on raw audio."""
            if not (self._bus and audio_bytes):
                return
            duration_s = len(audio_bytes) / (16000 * 2)
            with contextlib.suppress(Exception):
                await self._bus.publish_dict(
                    "auditory.raw_audio",
                    {
                        "audio_bytes": audio_bytes,
                        "sample_rate": 16000,
                        "duration_s": duration_s,
                        "channels": 1,
                        "dtype": "int16",
                    },
                    source="ui",
                )
                await self._bus.publish_dict(
                    "auditory.diarized_audio",
                    {
                        "audio_bytes": audio_bytes,
                        "sample_rate": 16000,
                        "duration_s": duration_s,
                        "dtype": "int16",
                        "diarized_words": [],
                        "transcript": transcript,
                    },
                    source="ui",
                )

        async def _run_session() -> None:
            try:
                import websockets

                url = "wss://api.openai.com/v1/realtime?intent=transcription"
                async with websockets.connect(
                    url,
                    additional_headers=[
                        ("Authorization", f"Bearer {api_key}"),
                        ("OpenAI-Beta", "realtime=v1"),
                    ],
                    max_size=None,
                ) as conn:
                    await conn.send(
                        json.dumps(
                            {
                                "type": "transcription_session.update",
                                "session": {
                                    "input_audio_format": "pcm16",
                                    "input_audio_transcription": {
                                        "model": model,
                                        "language": language,
                                    },
                                    "turn_detection": {
                                        "type": "server_vad",
                                        "silence_duration_ms": 600,
                                    },
                                },
                            }
                        )
                    )
                    logger.info("UI: OpenAI Realtime STT session started (model=%s)", model)
                    interim = ""

                    async def _listen() -> None:
                        nonlocal interim
                        async for raw in conn:
                            try:
                                ev = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            etype = ev.get("type", "")
                            if etype == "conversation.item.input_audio_transcription.delta":
                                interim += ev.get("delta", "")
                                if interim.strip():
                                    with contextlib.suppress(Exception):
                                        await websocket.send_text(
                                            json.dumps(
                                                {
                                                    "type": "transcript",
                                                    "text": interim,
                                                    "is_final": False,
                                                }
                                            )
                                        )
                            elif etype == "conversation.item.input_audio_transcription.completed":
                                interim = ""
                                text = str(ev.get("transcript", "")).strip()
                                if text:
                                    with contextlib.suppress(Exception):
                                        await websocket.send_text(
                                            json.dumps(
                                                {
                                                    "type": "transcript",
                                                    "text": text,
                                                    "is_final": True,
                                                }
                                            )
                                        )
                                    audio_bytes = b"".join(session.audio_chunks)
                                    session.audio_chunks.clear()
                                    asyncio.create_task(_publish_utterance(text, audio_bytes))
                            elif etype == "error":
                                logger.warning("UI: OpenAI STT error event: %s", ev.get("error"))

                    listen_task = asyncio.create_task(_listen())
                    closed_early = False
                    try:
                        while True:
                            chunk = await audio_queue.get()
                            if chunk is None:
                                break
                            if listen_task.done():
                                closed_early = True
                                break
                            up = PNS._pcm_resample(chunk, 16000, 24000)
                            await conn.send(
                                json.dumps(
                                    {
                                        "type": "input_audio_buffer.append",
                                        "audio": base64.b64encode(up).decode(),
                                    }
                                )
                            )
                    finally:
                        listen_task.cancel()
                        logger.info("UI: OpenAI Realtime STT session closed")
                    if closed_early:
                        with contextlib.suppress(Exception):
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "transcript_error",
                                        "msg": "OpenAI STT closed connection",
                                    }
                                )
                            )
            except Exception as e:
                logger.warning("OpenAI STT session error — voice input unavailable: %s", e)
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "transcript_error", "msg": str(e)})
                    )

        session._task = asyncio.create_task(_run_session())
        await asyncio.sleep(0.3)
        if session._task.done():
            return None
        return session

    async def _receive_loop(self, websocket) -> None:
        dg_conn = None  # per-client Deepgram live connection
        dg_last_attempt = 0.0  # monotonic ts of the last connect attempt (backoff)
        loop = asyncio.get_running_loop()
        while True:
            try:
                msg = await websocket.receive()
            except Exception:
                break
            if msg.get("type") == "websocket.disconnect":
                break
            try:
                if "text" in msg and msg["text"]:
                    data = json.loads(msg["text"])
                    t = data.get("type")
                    if t == "user_message" and self._on_user_message:
                        text = data.get("text", "").strip()
                        if text:
                            await self._on_user_message(text)
                    elif t == "mic_toggle" and self._on_mic_toggle:
                        self._on_mic_toggle()
                        # Echo new state back so the button updates. (The session
                        # also broadcasts the settled state once any async flush
                        # completes — this echo just makes the click feel instant.)
                        with contextlib.suppress(Exception):
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "mic_state",
                                        "status": self._mic_status(),
                                    }
                                )
                            )
                    elif t == "mic_ptt" and self._on_mic_ptt:
                        # Push-to-talk: {down:true} on Space keydown (go live),
                        # {down:false} on keyup (flush held phrase + re-mute).
                        # State is broadcast by the session once it settles, so
                        # we don't echo here (release involves an async flush).
                        self._on_mic_ptt(bool(data.get("down", False)))
                    elif t == "set_voice" and self._on_voice_change:
                        vid = data.get("voice_id", "").strip()
                        if vid:
                            self._on_voice_change(vid)
                    elif t == "tts_mute" and self._on_tts_mute:
                        # Voice-narration mute. Skips TTS synthesis server-side so
                        # muting saves ElevenLabs credits (not just client audio).
                        self._on_tts_mute(bool(data.get("muted", False)))
                    elif t == "eval_mode" and self._on_eval_mode:
                        intensive = bool(data.get("intensive", False))
                        self._on_eval_mode(intensive)
                    elif t == "interrupt" and self._on_interrupt:
                        self._on_interrupt()
                    elif t == "voice_start":
                        # If a previous session died silently (task finished without
                        # a voice_stop), clear it so we get a fresh connection.
                        if dg_conn is not None and dg_conn._task.done():
                            dg_conn = None
                        if dg_conn is None:
                            dg_conn = await self._start_stt(websocket)
                    elif t == "voice_stop":
                        if dg_conn is not None:
                            with contextlib.suppress(Exception):
                                await dg_conn.finish()
                            dg_conn = None

                elif "bytes" in msg and msg["bytes"]:
                    # The browser only streams audio while it believes a voice
                    # session is open, so a missing/dead connection here means
                    # Deepgram failed to start or died mid-capture (e.g. their
                    # 1011 idle timeout). Self-heal: reopen, with a 2s backoff
                    # so a hard failure (bad key) doesn't hammer the API. The
                    # old code dropped these frames silently — the mic looked
                    # alive ("Listening…") while nothing was transcribed.
                    if dg_conn is not None and dg_conn._task is not None and dg_conn._task.done():
                        logger.warning("UI: Deepgram session died mid-capture — reopening")
                        dg_conn = None
                    if dg_conn is None and loop.time() - dg_last_attempt >= 2.0:
                        dg_last_attempt = loop.time()
                        dg_conn = await self._start_stt(websocket)
                        if dg_conn is None:
                            logger.warning(
                                "UI: mic audio arriving but Deepgram is unavailable — dropping"
                            )
                    if dg_conn is not None:
                        dg_conn.audio_chunks.append(msg["bytes"])
                        await dg_conn.send(msg["bytes"])

            except Exception as e:
                # A handler error must not kill the receive loop — that would
                # silently disable ALL input (text and voice) for this client.
                logger.warning(
                    "UI: ws message handler error (%s: %s) — continuing",
                    type(e).__name__,
                    e,
                )

        if dg_conn is not None:
            with contextlib.suppress(Exception):
                await dg_conn.finish()

    @property
    def has_listeners(self) -> bool:
        """True if at least one browser client is connected to receive events/audio."""
        return bool(self._clients)

    def attach_tts_queue(self, pns) -> None:
        """Wire PNS TTS queue into this server's broadcast loop.

        Called by session_setup after PNS and UIServer are both ready.
        pns._tts_ws_queue is set here; the _tts_broadcast_loop drains it.
        """
        import asyncio

        pns._tts_ws_queue = asyncio.Queue(maxsize=256)
        asyncio.create_task(self._tts_broadcast_loop(pns._tts_ws_queue, pns))

    async def _tts_broadcast_loop(self, queue: asyncio.Queue, pns) -> None:
        """Drain TTS PCM chunks and broadcast as binary WebSocket frames.

        Frame format: 1-byte type prefix (0x01=audio, 0xFF=interrupt) + PCM bytes.
        Browser detects interrupts by the 0xFF frame and stops playback immediately.
        """
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
            except TimeoutError:
                continue

            if chunk is None:
                # Sentinel: TTS stream ended — send tts_end event
                if self._clients:
                    payload = json.dumps({"type": "tts_end"})
                    dead = set()
                    for client in list(self._clients):
                        with contextlib.suppress(Exception):
                            await client.send_text(payload)
                continue

            if chunk == b"\xff":
                # Interrupt sentinel
                if self._clients:
                    payload = json.dumps({"type": "tts_interrupt"})
                    dead = set()
                    for client in list(self._clients):
                        with contextlib.suppress(Exception):
                            await client.send_text(payload)
                continue

            # Binary PCM chunk — prefix with 0x01 so browser can distinguish
            frame = b"\x01" + chunk
            if self._clients:
                dead = set()
                for client in list(self._clients):
                    try:
                        await client.send_bytes(frame)
                    except Exception:
                        dead.add(client)
                self._clients -= dead

    async def _handle_agent_event(self, event: dict) -> None:
        """Route one agent-lane event to the Agents view (never the main feed).

        Pairs turn_start/turn_end per session into a recent-history buffer — the
        same shape the main chat keeps in ``_chat_history`` — and broadcasts every
        agent event re-wrapped as a single ``agent_event`` type so the main chat's
        per-type handlers can't pick it up. The buffer is replayed on reconnect so
        the Agents view survives a page refresh, just like the main chat."""
        sid = event.get("route_sid") or ""
        etype = event.get("type")
        if etype == "turn_start" and event.get("user_input"):
            self._agent_pending[sid] = {
                "route_sid": sid,
                "agent_id": event.get("agent_id", ""),
                "end_user_id": event.get("end_user_id", ""),
                "turn_id": event.get("turn_id"),
                "user_input": event["user_input"],
                "ts": event.get("ts"),
            }
        elif etype == "turn_end" and event.get("response"):
            pending = self._agent_pending.pop(sid, None)
            if pending is not None:
                self._agent_history.append(
                    {
                        **pending,
                        "response": event["response"],
                        "elapsed_s": event.get("elapsed_s"),
                    }
                )
                if len(self._agent_history) > 50:
                    self._agent_history.pop(0)

        if self._clients:
            payload = json.dumps({"type": "agent_event", "event": event})
            dead = set()
            for client in list(self._clients):
                try:
                    await client.send_text(payload)
                except Exception:
                    dead.add(client)
            self._clients -= dead

    async def _broadcast_loop(self) -> None:
        """Drain emitter queue and broadcast to all connected clients."""
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                # Agent-lane events (engine-API/partner turns) never touch the main
                # feed: they're re-wrapped as a single "agent_event" type the main
                # chat handlers don't match, and routed to the Agents view instead.
                if event.get("channel") == "agent":
                    await self._handle_agent_event(event)
                    continue
                # Cache latest neuromod + hormonal + emotion for new clients
                if event.get("type") == "neuromod":
                    self._last_neuromod = {k: v for k, v in event.items() if k != "type"}
                elif event.get("type") == "hormonal":
                    self._last_hormonal = {k: v for k, v in event.items() if k != "type"}
                elif event.get("type") == "emotion" and event.get("emotion"):
                    self._last_emotion = event["emotion"]
                elif event.get("type") == "stream_thought" and event.get("thought"):
                    if not event.get("proactive"):
                        self._last_thoughts.append(event)
                        if len(self._last_thoughts) > 10:
                            self._last_thoughts.pop(0)
                elif event.get("type") == "turn_start" and event.get("user_input"):
                    self._pending_turn = {
                        "turn_id": event.get("turn_id"),
                        "user_input": event["user_input"],
                    }
                elif (
                    event.get("type") == "turn_end" and event.get("response") and self._pending_turn
                ):
                    self._chat_history.append(
                        {
                            **self._pending_turn,
                            "response": event["response"],
                            "elapsed_s": event.get("elapsed_s"),
                            "llm_calls": event.get("llm_calls", 0),
                        }
                    )
                    self._pending_turn = None
                    if len(self._chat_history) > 50:
                        self._chat_history.pop(0)

                if self._clients:
                    payload = json.dumps(event)
                    dead = set()
                    for client in list(self._clients):
                        try:
                            await client.send_text(payload)
                        except Exception:
                            dead.add(client)
                    self._clients -= dead
            except TimeoutError:
                await asyncio.sleep(0.01)

    async def start(self, host: str | None = None, port: int | None = None) -> None:
        import uvicorn

        # Railway sets PORT env var; default to 0.0.0.0 in production
        _port = port or int(os.environ.get("PORT", "8765"))
        # Bind all interfaces only when hosted (Railway); localhost otherwise.
        _host = host or (
            "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"  # nosec B104
        )

        self._app = self._build_app()

        # Start broadcast loop as a background task
        asyncio.create_task(self._broadcast_loop())

        config = uvicorn.Config(
            self._app,
            host=_host,
            port=_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        logger.info("UI server starting at http://%s:%d", _host, _port)
        print(f"\nBrain UI: http://{host}:{port}\n")
        await server.serve()
