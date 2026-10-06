"""The voice list the app offers: the ElevenLabs account's "My Voices".

"My Voices" in the ElevenLabs web app is every voice on the account except
ElevenLabs' platform defaults: voices the owner created (cloned, designed,
professional clones) plus voices saved from the Voice Library. The API name for
that set is ``voice_type=non-default`` on ``GET /v2/voices``. The owner curates
that list in ElevenLabs (e.g. to only the voices that suit the current model),
so the picker shows it as-is, all pages of it, rather than re-deriving it.

Two exceptions:
  - an eleven_v3* model silently swaps in its own voice for a Professional Voice
    Clone, so those are hidden on v3 (v4 and Flash serve them);
  - an empty My Voices list falls back to the default voices, so the picker is
    never empty.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

VOICES_URL = "https://api.elevenlabs.io/v2/voices"
PAGE_SIZE = 100  # the API maximum
MAX_PAGES = 20  # 2,000 voices; a guard against a pagination loop, not a real limit


async def fetch_voices(api_key: str, voice_type: str, *, client=None) -> list[dict]:
    """Every voice of ``voice_type`` on the account, following next_page_token."""
    import httpx

    own = client is None
    client = client or httpx.AsyncClient(timeout=8)
    voices: list[dict] = []
    token: str | None = None
    try:
        for _ in range(MAX_PAGES):
            params: dict = {
                "voice_type": voice_type,
                "page_size": PAGE_SIZE,
                "sort": "name",
                "sort_direction": "asc",
                "include_total_count": "false",
            }
            if token:
                params["next_page_token"] = token
            resp = await client.get(VOICES_URL, headers={"xi-api-key": api_key}, params=params)
            resp.raise_for_status()
            body = resp.json()
            voices.extend(body.get("voices") or [])
            token = body.get("next_page_token")
            if not body.get("has_more") or not token:
                break
        else:
            logger.warning("[voices] stopped after %d pages of %s voices", MAX_PAGES, voice_type)
    finally:
        if own:
            await client.aclose()
    return voices


def _entry(v: dict) -> dict:
    return {
        "voice_id": v["voice_id"],
        "name": v.get("name") or v["voice_id"],
        "category": v.get("category") or "",
    }


async def picker_voices(api_key: str, model_id: str, *, client=None) -> dict:
    """The /voices payload: ``{voices, model_id, source, message}`` where
    ``source`` is "my_voices" or "default" (the empty-list fallback)."""
    mine = await fetch_voices(api_key, "non-default", client=client)
    message = ""
    hidden_pro = 0
    if model_id.startswith("eleven_v3"):
        kept = [v for v in mine if v.get("category") != "professional"]
        hidden_pro = len(mine) - len(kept)
        mine = kept
        if hidden_pro:
            message = (
                f"Hiding {hidden_pro} Professional Voice Clone(s): {model_id} does not "
                "serve them. Switch to Eleven v4 Turbo to use them."
            )
    if mine:
        return {
            "voices": [_entry(v) for v in mine],
            "model_id": model_id,
            "source": "my_voices",
            "message": message,
        }
    defaults = await fetch_voices(api_key, "default", client=client)
    if not message:
        message = "Your ElevenLabs My Voices list is empty, so the default voices are shown."
    return {
        "voices": [_entry(v) for v in defaults],
        "model_id": model_id,
        "source": "default",
        "message": message,
    }
