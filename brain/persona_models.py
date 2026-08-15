"""
Read-side views of a persona's evolving model of itself and the people it talks to.

One module because two surfaces serve the same data — the owner UI
(brain/ui/server.py `/self-model`, `/user-model`) and the owner-gated engine API
(brain/api/server.py `GET /v1/personas/{persona}/...`) — and duplicated gather
logic is how those surfaces drift apart. Everything here is a pure filesystem
read routed through the same persona resolution the writers use (SchemaStore /
persona_state_root / persona_chem), so what these functions return is exactly
what the brain will act on next turn.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Lines present in a freshly-seeded speaker template — a profile that contains
# nothing beyond these hasn't actually learned anything yet.
_TEMPLATE_LINES = {"- (learning…)", "- Familiarity: new", "- Score: 0"}

# Same fields the turn path reads back (brain/metacognition.py); parsed here from
# a body we already hold rather than re-reading the file per speaker.
_SCORE_RE = re.compile(r"- Score:\s*(-?\d+)")
_FAMILIARITY_RE = re.compile(r"- Familiarity:\s*(\w+)")


def read_self_model(persona_name: str) -> str:
    """The persona's self.md — its self-authored identity document."""
    from brain.second_brain.store import SchemaStore

    try:
        return SchemaStore(persona=persona_name).read("self.md")
    except Exception as e:
        logger.warning("[persona_models] self-model read failed for %r: %s", persona_name, e)
        return ""


def read_user_model(persona_name: str) -> dict:
    """The persona's model of the people it talks to.

    Sleep consolidation routes facts by speaker: turns with an identified
    speaker — every voice-identified companion AND every engine turn
    (speaker_name = end_user_id) — land in a per-person user_<slug>.md; only
    speakerless turns land in user.md. So the full picture is user.md PLUS the
    per-person files, not user.md alone. Untouched templates (created on first
    sighting, nothing learned yet) are filtered out.
    """
    from brain.second_brain.store import SchemaStore

    files = SchemaStore(persona=persona_name).read_all()
    content = files.get("user.md", "")
    speakers: list[dict] = []
    for fname in sorted(files):
        if not (fname.startswith("user_") and fname.endswith(".md")):
            continue
        body = files[fname] or ""
        learned = [
            ln.strip()
            for ln in body.splitlines()
            if ln.strip().startswith("- ") and ln.strip() not in _TEMPLATE_LINES
        ]
        if not learned:
            continue  # untouched template — created on first sighting
        first = body.strip().splitlines()[0] if body.strip() else ""
        m = re.match(r"#\s*User:\s*(.+)", first)
        name = (m.group(1).strip() if m else "") or fname[len("user_") : -len(".md")]
        m_score = _SCORE_RE.search(body)
        m_fam = _FAMILIARITY_RE.search(body)
        speakers.append(
            {
                "file": fname,
                "name": name,
                "content": body,
                "affection": int(m_score.group(1)) if m_score else 0,
                "familiarity": m_fam.group(1).lower() if m_fam else "new",
            }
        )
    return {"content": content, "speakers": speakers}


def read_chemistry(persona_name: str) -> dict | None:
    """The persona's chemistry state: resting/current channels plus the
    per-end-user pairs (engine mode seeds a ChemPair per customer; see
    brain/client_chem.py). Returns None for an unknown/unseedable persona.

    Pair snapshots are read straight from the FileChemStore files — the same
    records the registry restores from — so `snapshot` is the customer's last
    persisted mood and `last_seen` its timestamp. Write cadence is throttled,
    so a pair can lag the turn that moved it.
    """
    from brain import persona_chem
    from brain.persona_key import persona_state_root

    state = persona_chem.load(persona_name)
    if state is None:
        return None
    pairs: list[dict] = []
    try:
        pair_dir = persona_state_root(persona_name) / "client_chem"
        if pair_dir.is_dir():
            for path in sorted(pair_dir.glob("*.json")):
                try:
                    rec = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError, ValueError) as exc:
                    logger.warning("[persona_models] unreadable pair %s: %s", path.name, exc)
                    continue
                key = str(rec.get("key", ""))
                if not key:
                    continue
                # Keys are "<persona>:<end_user_id>" and end_user_ids cannot
                # contain ':' (brain/ids.py), so the rightmost split is exact.
                pairs.append(
                    {
                        "end_user_id": key.rsplit(":", 1)[-1],
                        "snapshot": rec.get("snapshot"),
                        "last_seen": rec.get("last_seen"),
                    }
                )
    except Exception as e:
        logger.warning("[persona_models] pair scan failed for %r: %s", persona_name, e)
    return {**state, "pairs": pairs}
