"""
Second brain store — episodic (LanceDB) + schema (Markdown).
ONLY imported by brain/clusters/hippocampus.py. No other cluster touches this file.

Design: encode every substantive turn. Storage is free; retrieval is the intelligence.
The hippocampus indexes, not gatekeeps.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SECOND_BRAIN_ROOT = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    str(Path(__file__).parent.parent.parent / "second_brain")
))
EPISODES_DIR = SECOND_BRAIN_ROOT / "episodes"
SCHEMA_DIR = SECOND_BRAIN_ROOT / "schema"

# Must match brain.model_router.EMBEDDING_DIM. nomic-embed-text and
# gemini-embedding-001 both produce 768-dim vectors.
EMBEDDING_DIM = 768


@dataclass
class Episode:
    session_id: str
    turn_id: str
    ts: float
    user_input: str
    entity_response: str          # final emitted response (cognitive artifact)
    topic_tags: list[str]
    emotion_state: str            # entity's emotion at time of episode
    user_emotion: str             # estimated user emotion
    entities: list[str]
    neuromod_snapshot: dict[str, float]
    surprise_score: float         # from predict-and-surprise gating
    vector: list[float] | None = None  # embedding (populated by hippocampus)


class EpisodicStore:
    """LanceDB-backed episodic memory. Encodes all substantive turns."""

    def __init__(self) -> None:
        self._db = None
        self._table = None
        self._ready = False

    def _ensure_ready(self) -> bool:
        if self._ready:
            return True
        try:
            import lancedb
            import pyarrow as pa
            EPISODES_DIR.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(EPISODES_DIR))
            schema = pa.schema([
                pa.field("session_id", pa.string()),
                pa.field("turn_id", pa.string()),
                pa.field("ts", pa.float64()),
                pa.field("user_input", pa.string()),
                pa.field("entity_response", pa.string()),
                pa.field("topic_tags", pa.string()),   # JSON array
                pa.field("emotion_state", pa.string()),
                pa.field("user_emotion", pa.string()),
                pa.field("entities", pa.string()),      # JSON array
                pa.field("neuromod_snapshot", pa.string()),  # JSON
                pa.field("surprise_score", pa.float64()),
                pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
            ])
            if "episodes" in self._db.table_names():
                self._table = self._db.open_table("episodes")
            else:
                self._table = self._db.create_table("episodes", schema=schema)
            self._ready = True
            return True
        except Exception as e:
            logger.warning("[Episode DB] Database unavailable — episodes will not be saved this session. Is lancedb installed? Run 'uv sync'. Error: %s", e)
            return False

    def encode(self, episode: Episode) -> None:
        if not self._ensure_ready():
            return
        try:
            row = {
                "session_id": episode.session_id,
                "turn_id": episode.turn_id,
                "ts": episode.ts,
                "user_input": episode.user_input,
                "entity_response": episode.entity_response,
                "topic_tags": json.dumps(episode.topic_tags),
                "emotion_state": episode.emotion_state,
                "user_emotion": episode.user_emotion,
                "entities": json.dumps(episode.entities),
                "neuromod_snapshot": json.dumps(episode.neuromod_snapshot),
                "surprise_score": episode.surprise_score,
                "vector": episode.vector or ([0.0] * EMBEDDING_DIM),
            }
            self._table.add([row])
        except Exception as e:
            logger.error("[Episode DB] Failed to save episode — this turn's memory will be lost: %s", e)

    def recall(self, query_vector: list[float], limit: int = 5,
               topic_filter: str | None = None) -> list[dict]:
        if not self._ensure_ready():
            return []
        try:
            q = self._table.search(query_vector).limit(limit)
            results = q.to_list()
            episodes = []
            for r in results:
                ep = dict(r)
                ep["topic_tags"] = json.loads(ep.get("topic_tags", "[]"))
                ep["entities"] = json.loads(ep.get("entities", "[]"))
                ep["neuromod_snapshot"] = json.loads(ep.get("neuromod_snapshot", "{}"))
                episodes.append(ep)
            return episodes
        except Exception as e:
            logger.error("[Episode DB] Memory search failed: %s", e)
            return []

    _SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    def recall_by_session(self, session_id: str) -> list[dict]:
        if not self._ensure_ready():
            return []
        if not self._SESSION_ID_RE.match(session_id):
            logger.warning("[Episode DB] [Security] Blocked unsafe session ID in memory query: %r", session_id)
            return []
        try:
            results = self._table.search().where(
                f"session_id = '{session_id}'"
            ).to_list()
            return results
        except Exception as e:
            logger.error("[Episode DB] Session recall failed: %s", e)
            return []


class SchemaStore:
    """
    Markdown-based schema layer. Human-readable, hand-editable.
    One file per topic/entity. Fast grep-based lookup.

    Writes are serialized with an asyncio.Lock and use temp-file-then-rename
    so concurrent encode + sleep-consolidation cannot corrupt the schema.
    Sync write/append remain for boot-time (no event loop) use.
    """

    _FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.md$")

    def __init__(self) -> None:
        SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _validate_filename(self, filename: str) -> bool:
        """Return True if filename is safe; log a warning and return False otherwise."""
        if not self._FILENAME_RE.match(filename):
            logger.warning("[Schema DB] [Security] Blocked unsafe filename (possible path traversal): %r", filename)
            return False
        resolved = (SCHEMA_DIR / filename).resolve()
        if not resolved.is_relative_to(SCHEMA_DIR.resolve()):
            logger.warning("[Schema DB] [Security] Blocked filename that tries to escape the schema directory: %r", filename)
            return False
        return True

    def read(self, filename: str) -> str:
        if not self._validate_filename(filename):
            return ""
        path = SCHEMA_DIR / filename
        if path.exists():
            return path.read_text()
        return ""

    def _atomic_write(self, path: Path, content: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)

    def write(self, filename: str, content: str) -> None:
        """Sync write — only safe at boot / outside event loop."""
        if not self._validate_filename(filename):
            return
        self._atomic_write(SCHEMA_DIR / filename, content)

    def append_fact(self, filename: str, fact: str) -> None:
        """Sync append — only safe at boot / outside event loop."""
        if not self._validate_filename(filename):
            return
        path = SCHEMA_DIR / filename
        existing = path.read_text() if path.exists() else ""
        fact = fact.strip()
        if fact and fact not in existing:
            self._atomic_write(path, existing + f"\n- {fact}")

    async def awrite(self, filename: str, content: str) -> None:
        if not self._validate_filename(filename):
            return
        async with self._lock:
            self._atomic_write(SCHEMA_DIR / filename, content)

    async def aappend_fact(self, filename: str, fact: str) -> None:
        if not self._validate_filename(filename):
            return
        async with self._lock:
            path = SCHEMA_DIR / filename
            existing = path.read_text() if path.exists() else ""
            fact = fact.strip()
            if fact and fact not in existing:
                self._atomic_write(path, existing + f"\n- {fact}")

    def list_files(self) -> list[str]:
        return [p.name for p in SCHEMA_DIR.glob("*.md")]

    def grep(self, keyword: str) -> list[tuple[str, str]]:
        """Return (filename, matching_line) pairs."""
        hits = []
        for path in SCHEMA_DIR.glob("*.md"):
            for line in path.read_text().splitlines():
                if keyword.lower() in line.lower():
                    hits.append((path.name, line.strip()))
        return hits

    def load_core_context(self) -> dict[str, str]:
        """Pre-load self.md + user.md at session boot (extended mind — reliably needed)."""
        return {
            "self": self.read("self.md"),
            "user": self.read("user.md"),
        }

    def ensure_self_schema(self) -> None:
        if not (SCHEMA_DIR / "self.md").exists():
            self.write("self.md", "# Entity Self-Model\n\n"
                       "## Identity\n- Instantiated: " + time.strftime("%Y-%m-%d") + "\n\n"
                       "## Stable preferences\n\n"
                       "## Relational identity\n\n"
                       "## History summary\n\n"
                       "## Current mood signature\n\n"
                       "## Values\n")

    def ensure_user_schema(self, user_name: str = "User") -> None:
        if not (SCHEMA_DIR / "user.md").exists():
            self.write("user.md", f"# User: {user_name}\n\n"
                       "## Known facts\n\n"
                       "## Preferences\n\n"
                       "## Emotional profile\n")
