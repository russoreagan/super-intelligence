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
from dataclasses import dataclass
from pathlib import Path

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

    def recall_recent(self, limit: int = 6) -> list[dict]:
        """Return the most recent episodes by timestamp (for session bridging at boot)."""
        if not self._ensure_ready():
            return []
        try:
            import pyarrow.compute as pc  # noqa: F401 (pyarrow already required by lancedb)
            tbl = self._table.to_arrow()
            sorted_tbl = tbl.sort_by([("ts", "descending")])
            rows = sorted_tbl.slice(0, limit).to_pylist()
            episodes = []
            for r in rows:
                ep = dict(r)
                ep["topic_tags"] = json.loads(ep.get("topic_tags", "[]"))
                ep["entities"] = json.loads(ep.get("entities", "[]"))
                ep["neuromod_snapshot"] = json.loads(ep.get("neuromod_snapshot", "{}"))
                episodes.append(ep)
            return episodes
        except Exception as e:
            logger.error("[Episode DB] Recent recall failed: %s", e)
            return []

    def recall(self, query_vector: list[float], limit: int = 5,
               exclude_tags: list[str] | None = None) -> list[dict]:
        """Vector search over all episodes, optionally excluding those that
        contain any of the given tags. Used by the main recall path so that
        deferred questions (which have their own search path) don't compete
        with conversation memories for top-k slots."""
        if not self._ensure_ready():
            return []
        try:
            q = self._table.search(query_vector).limit(limit)
            if exclude_tags:
                for tag in exclude_tags:
                    q = q.where(f"topic_tags NOT LIKE '%{tag}%'")
            results = q.to_list()
            return self._parse_rows(results)
        except Exception as e:
            logger.error("[Episode DB] Memory search failed: %s", e)
            return []

    def recall_by_tag(self, query_vector: list[float], tag: str,
                      limit: int = 3) -> list[dict]:
        """Vector search scoped to episodes that contain the given tag.
        Used to give deferred questions their own retrieval budget, separate
        from conversation memories."""
        if not self._ensure_ready():
            return []
        try:
            results = (
                self._table
                .search(query_vector)
                .where(f"topic_tags LIKE '%{tag}%'")
                .limit(limit)
                .to_list()
            )
            return self._parse_rows(results)
        except Exception as e:
            logger.error("[Episode DB] Tag-scoped recall failed (tag=%r): %s", tag, e)
            return []

    def _parse_rows(self, rows: list[dict]) -> list[dict]:
        episodes = []
        for r in rows:
            ep = dict(r)
            ep["topic_tags"] = json.loads(ep.get("topic_tags", "[]"))
            ep["entities"] = json.loads(ep.get("entities", "[]"))
            ep["neuromod_snapshot"] = json.loads(ep.get("neuromod_snapshot", "{}"))
            episodes.append(ep)
        return episodes

    _SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    def recall_by_session(self, session_id: str) -> list[dict]:
        if not self._ensure_ready():
            return []
        if not self._SESSION_ID_RE.match(session_id):
            logger.warning("[Episode DB] [Security] Blocked unsafe session ID in memory query: %r", session_id)
            return []
        try:
            # Safety: session_id is validated by _SESSION_ID_RE above; that regex
            # must stay in place to prevent SQL injection through this f-string.
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
        """Pre-load self.md + user.md + open_questions.md at session boot."""
        self_content = self.read("self.md")
        oq_content = self.read("open_questions.md")
        # Combine into a single self key so the DMN sees open questions alongside
        # the self-model without requiring changes to update_context() call sites.
        combined_self = self_content
        if oq_content:
            combined_self = f"{self_content}\n\n{oq_content}"
        return {
            "self": combined_self,
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
                       "## Emotional profile\n\n"
                       "## Relationship\n"
                       "- Familiarity: new (conversations so far: ~0)\n\n"
                       "## Affection score\n"
                       "- Score: 0\n")

    _SPEAKER_SLUG_RE = re.compile(r"[^a-z0-9]+")

    def speaker_filename(self, name: str) -> str:
        """Convert a speaker name to a safe per-speaker schema filename."""
        slug = self._SPEAKER_SLUG_RE.sub("_", name.lower().strip()).strip("_")[:32]
        slug = slug or "unknown"
        return f"user_{slug}.md"

    def ensure_speaker_schema(self, name: str) -> str:
        """Ensure a per-speaker schema file exists. Returns the filename."""
        filename = self.speaker_filename(name)
        if not (SCHEMA_DIR / filename).exists():
            self.write(filename, f"# User: {name}\n\n"
                       "## Known facts\n\n"
                       "## Preferences\n\n"
                       "## Emotional profile\n\n"
                       "## Relationship\n"
                       "- Familiarity: new\n\n"
                       "## Affection score\n"
                       "- Score: 0\n")
        return filename

    def load_speaker_context(self, name: str) -> str:
        """Read (and if needed create) the schema file for a named speaker."""
        filename = self.ensure_speaker_schema(name)
        return self.read(filename)

    async def migrate_placeholder(self, placeholder_filename: str, target_filename: str) -> None:
        """Append facts from a placeholder schema (e.g. user_spk_0.md) into the real one,
        then delete the placeholder. Called when an unknown speaker is enrolled."""
        if not self._validate_filename(placeholder_filename):
            return
        if not self._validate_filename(target_filename):
            return
        placeholder_path = SCHEMA_DIR / placeholder_filename
        if not placeholder_path.exists():
            return
        async with self._lock:
            src = placeholder_path.read_text()
            dst = (SCHEMA_DIR / target_filename).read_text() if (SCHEMA_DIR / target_filename).exists() else ""
            # Extract bullet-point facts from the placeholder (skip header/section lines)
            facts = [ln.strip() for ln in src.splitlines()
                     if ln.strip().startswith("- ") and ln.strip() not in dst]
            if facts:
                self._atomic_write(SCHEMA_DIR / target_filename,
                                   dst + "\n" + "\n".join(facts))
            placeholder_path.unlink(missing_ok=True)
            logger.info("[Schema] Migrated placeholder %s → %s (%d facts)",
                        placeholder_filename, target_filename, len(facts))

    def primary_user_name(self) -> str:
        """Extract the primary user's name from user.md Known facts, or from the header."""
        content = self.read("user.md")
        for line in content.splitlines():
            # Prefer "User's name is X" fact over the header title
            m = re.match(r"-\s+User['']s name is (.+)", line.strip())
            if m:
                return m.group(1).strip()
        # Fall back to the file header: "# User: X"
        m = re.match(r"#\s+User:\s+(.+)", content.strip().splitlines()[0] if content else "")
        if m:
            name = m.group(1).strip()
            return name if name.lower() != "user" else ""
        return ""
