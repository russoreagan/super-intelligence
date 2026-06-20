"""
Transactional email via Resend — reuses the mail service already wired up for
thegaim.app (same Resend account + verified `thegaim.app` sender domain).

Config (env):
  RESEND_API_KEY   Resend API key (shared with thegaim.app). Required to send.
  EMAIL_FROM       Sender address. Defaults to noreply@thegaim.app.

Best-effort by design: if RESEND_API_KEY is unset (e.g. local dev) we log the
email instead of sending, mirroring thegaim.app's dev behaviour, so callers can
treat "sent" and "would-have-sent" the same and never block on mail delivery.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_DEFAULT_FROM = "noreply@thegaim.app"


def _from_address() -> str:
    return os.environ.get("EMAIL_FROM", "").strip() or _DEFAULT_FROM


async def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one email via Resend. Returns True on send (or dev-mode log), False
    only on an actual delivery error. Never raises."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        # Dev / unconfigured: log instead of sending (matches thegaim.app).
        logger.info("[mail] RESEND_API_KEY unset — would send to=%s subject=%r", to, subject)
        return True

    payload: dict[str, object] = {
        "from": _from_address(),
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                _RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as e:
        logger.error("[mail] Resend request failed: %s", e)
        return False
    if r.status_code >= 300:
        logger.error("[mail] Resend rejected (status=%s): %s", r.status_code, r.text[:200])
        return False
    logger.info("[mail] sent to=%s subject=%r", to, subject)
    return True
