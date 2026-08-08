"""
Second brain store — episodic (LanceDB or Supabase pgvector) + schema (Markdown or Supabase).
ONLY imported by brain/clusters/hippocampus.py. No other cluster touches this file.

Design: encode every substantive turn. Storage is cheap relative to the cost of deciding
what to keep; retrieval is the intelligence. The hippocampus indexes, not gatekeeps.

Storage is NOT free, and nothing here bounds it. There is no retention policy, no TTL, and
no age-based eviction — the only deletion path is erasure on request (api_purge_end_user).
Growth is linear in substantive turns per persona per tenant, forever. See docs/SYSTEMS.md
Appendix A; a retention policy is an open product decision, not an oversight to patch here.

Backend selection: BRAIN_STORAGE_BACKEND=local (default) | supabase
When supabase, brain.second_brain.supabase_client must have user_id + persona set.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SECOND_BRAIN_ROOT = Path(
    os.environ.get("SECOND_BRAIN_PATH", str(Path(__file__).parent.parent.parent / "second_brain"))
)
EPISODES_DIR = SECOND_BRAIN_ROOT / "episodes"
SCHEMA_DIR = SECOND_BRAIN_ROOT / "schema"

_STORAGE_BACKEND = os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower()

# Must match brain.model_router.EMBEDDING_DIM. nomic-embed-text and
# gemini-embedding-001 both produce 768-dim vectors.
EMBEDDING_DIM = 768


def _persona_key(persona: str) -> str:
    """Canonical persona key for the schema/episode stores. Slugifies so the hosted
    path (provisioner injects the RAW display name, e.g. 'The Visionary') and the
    local path (already slugified to 'the_visionary') read/write the SAME store.
    Idempotent on an already-slugged name; empty falls back to 'default'."""
    from brain.persona_key import persona_slug

    return persona_slug(persona, "default")


# Per-turn persona override (multi-persona Path B). When a turn binds a persona, all
# persona resolution — memory store scope, mandate catalog, agent_id derivation —
# follows it, so ONE process can serve many personas by binding per turn (mirrors the
# per-customer chemistry contextvar). Empty/unbound → the process persona, exactly as
# before, so the deployed single-persona path is byte-for-byte unchanged.
_active_persona_var: ContextVar[str] = ContextVar("brain_active_persona", default="")


@contextlib.contextmanager
def bind_persona(persona: str):
    """Bind ``persona`` as the active persona for the duration of the block (and any
    awaits in the same task). No-op for an empty persona. Resets on exit."""
    if not (persona or "").strip():
        yield
        return
    token = _active_persona_var.set(persona)
    try:
        yield
    finally:
        _active_persona_var.reset(token)


def active_persona() -> str:
    """The per-turn bound persona, or '' if none is bound."""
    return _active_persona_var.get()


def _resolve_persona(explicit: str) -> str:
    """Persona for store scoping. In multitenant mode an unresolved persona must
    never fall back to 'default' — that bucket is shared across every tenant whose
    provisioning failed the same way, i.e. silent cross-tenant contamination."""
    raw = explicit or _active_persona_var.get() or os.environ.get("BRAIN_PERSONA_NAME", "")
    if not raw.strip() and os.environ.get("BRAIN_MULTITENANT"):
        raise RuntimeError(
            "BRAIN_PERSONA_NAME is not set in multitenant mode — refusing the "
            "persona='default' fallback (it cross-contaminates tenants). The "
            "provisioner must inject BRAIN_PERSONA_NAME."
        )
    return raw or "default"


def _sql_quote(value: str) -> str:
    """Escape a string for interpolation into a LanceDB (DataFusion SQL) filter.
    Doubles single quotes and strips characters that can't appear in our tags or
    session ids anyway — these filters are built from user-influenced strings."""
    return str(value).replace("'", "''").replace("\\", "")


def _signature_cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity over the union of two cognitive-signature dicts.
    Missing keys count as 0. Returns 0.0 if either side has no magnitude."""
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    dot = sum(float(a.get(k, 0.0)) * float(b.get(k, 0.0)) for k in keys)
    na = sum(float(a.get(k, 0.0)) ** 2 for k in keys) ** 0.5
    nb = sum(float(b.get(k, 0.0)) ** 2 for k in keys) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class Episode:
    session_id: str
    turn_id: str
    ts: float
    user_input: str
    entity_response: str  # final emitted response (cognitive artifact)
    topic_tags: list[str]
    emotion_state: str  # entity's emotion at time of episode
    user_emotion: str  # estimated user emotion
    entities: list[str]
    neuromod_snapshot: dict[str, float]
    surprise_score: float  # from predict-and-surprise gating
    vector: list[float] | None = None  # embedding (populated by hippocampus)
    # Cognitive signature: the activation profile (chemistry + problem-STRUCTURE
    # flags) at encode time, deliberately content-free so it transfers across
    # domains. Matched by structural recall when a novel situation arrives.
    # Built by hippocampus._build_cog_signature; see brain/clusters/hippocampus.py.
    cog_signature: dict[str, float] = field(default_factory=dict)
    # Engine mode: which of the partner's customers this episode belongs to and
    # which assignment (mandates table) was active. "" / None in companion mode.
    end_user_id: str = ""
    mandate_id: str = ""


class EpisodicStore:
    """Episodic memory. Backend: LanceDB (local) or Supabase pgvector (cloud).

    Set BRAIN_STORAGE_BACKEND=supabase to use Supabase. The supabase_client
    module must have user_id and persona set before any call.
    """

    # Class-level default so instances built via __new__ (e.g. test doubles that
    # bypass __init__) still answer the backend branch as local.
    _use_supabase = False

    def __init__(self, persona: str = "") -> None:
        self._persona = persona
        # Local LanceDB state
        self._db = None
        self._table = None
        self._ready = False
        # Supabase state
        self._use_supabase = _STORAGE_BACKEND == "supabase"

    # ── Supabase helpers ──────────────────────────────────────────────────────

    def _sb(self):
        from brain.second_brain.supabase_client import get_client, get_user_id

        return get_client(), get_user_id()

    def _sb_persona(self) -> str:
        """Active persona key (slugified) — hosted (raw display name) and local
        (slug) converge on the same store. Falls back to the env var."""
        return _persona_key(_resolve_persona(self._persona))

    def _ensure_ready(self) -> bool:
        if self._ready:
            return True
        try:
            import lancedb
            import pyarrow as pa

            EPISODES_DIR.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(EPISODES_DIR))
            schema = pa.schema(
                [
                    pa.field("session_id", pa.string()),
                    pa.field("turn_id", pa.string()),
                    pa.field("ts", pa.float64()),
                    pa.field("user_input", pa.string()),
                    pa.field("entity_response", pa.string()),
                    pa.field("topic_tags", pa.string()),  # JSON array
                    pa.field("emotion_state", pa.string()),
                    pa.field("user_emotion", pa.string()),
                    pa.field("entities", pa.string()),  # JSON array
                    pa.field("neuromod_snapshot", pa.string()),  # JSON
                    pa.field("surprise_score", pa.float64()),
                    pa.field("cog_signature", pa.string()),  # JSON
                    pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
                ]
            )
            if "episodes" in self._db.table_names():
                self._table = self._db.open_table("episodes")
                self._migrate_cog_signature()
            else:
                self._table = self._db.create_table("episodes", schema=schema)
            self._ready = True
            return True
        except Exception as e:
            logger.warning(
                "[Episode DB] Database unavailable — episodes will not be saved this session. Is lancedb installed? Run 'uv sync'. Error: %s",
                e,
            )
            return False

    def _migrate_cog_signature(self) -> None:
        """Add the cog_signature column to a pre-existing table that lacks it.
        Old rows default to an empty JSON object; structural recall simply skips
        episodes whose signature is empty."""
        try:
            names = set(self._table.schema.names)
        except Exception:
            return
        if "cog_signature" in names:
            return
        try:
            # SQL expression evaluated per existing row → literal "{}".
            self._table.add_columns({"cog_signature": "'{}'"})
            logger.info("[Episode DB] Migrated table: added cog_signature column.")
        except Exception as e:
            logger.warning(
                "[Episode DB] Could not add cog_signature column (structural recall "
                "disabled until store is rebuilt): %s",
                e,
            )

    def encode(self, episode: Episode) -> None:
        if self._use_supabase:
            self._sb_encode(episode)
            return
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
                "cog_signature": json.dumps(episode.cog_signature or {}),
                "vector": episode.vector or ([0.0] * EMBEDDING_DIM),
            }
            self._table.add([row])
        except Exception as e:
            logger.error(
                "[Episode DB] Failed to save episode — this turn's memory will be lost: %s", e
            )

    def _sb_encode(self, episode: Episode) -> None:
        try:
            sb, uid = self._sb()
            persona = self._sb_persona()
            vec = episode.vector or ([0.0] * EMBEDDING_DIM)
            sb.table("episodes").insert(
                {
                    "org_id": uid,
                    "persona": persona,
                    "session_id": episode.session_id,
                    "turn_id": episode.turn_id,
                    "ts": episode.ts,
                    "user_input": episode.user_input,
                    "entity_response": episode.entity_response,
                    "topic_tags": episode.topic_tags,
                    "emotion_state": episode.emotion_state,
                    "user_emotion": episode.user_emotion,
                    "entities": episode.entities,
                    "neuromod_snapshot": episode.neuromod_snapshot,
                    "surprise_score": episode.surprise_score,
                    "cog_signature": episode.cog_signature or {},
                    "end_user_id": episode.end_user_id or "",
                    "mandate_id": episode.mandate_id or None,
                    "vector": f"[{','.join(str(v) for v in vec)}]",
                }
            ).execute()
        except Exception as e:
            logger.error("[Episode DB] Supabase encode failed: %s", e)

    def recall_recent(self, limit: int = 6) -> list[dict]:
        """Return the most recent episodes by timestamp (for session bridging at boot)."""
        if self._use_supabase:
            return self._sb_recall_recent(limit)
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

    def _sb_recall_recent(self, limit: int) -> list[dict]:
        try:
            sb, uid = self._sb()
            res = (
                sb.table("episodes")
                .select("*")
                .eq("org_id", uid)
                .eq("persona", self._sb_persona())
                .order("ts", desc=True)
                .limit(limit)
                .execute()
            )
            return self._parse_rows(res.data or [])
        except Exception as e:
            logger.error("[Episode DB] Supabase recall_recent failed: %s", e)
            return []

    def sample_random(self, n: int = 1) -> list[dict]:
        """Return up to n episodes chosen uniformly at random from the whole store."""
        if self._use_supabase:
            return self._sb_sample_random(n)
        import random

        if not self._ensure_ready():
            return []
        try:
            rows = self._table.to_arrow().to_pylist()
            if not rows:
                return []
            picked = random.sample(rows, min(n, len(rows)))
            return self._parse_rows(picked)
        except Exception as e:
            logger.error("[Episode DB] Random sample failed: %s", e)
            return []

    def _sb_sample_random(self, n: int) -> list[dict]:
        try:
            sb, uid = self._sb()
            # Supabase doesn't have ORDER BY RANDOM() directly — use rpc or a large limit+slice
            res = (
                sb.table("episodes")
                .select("*")
                .eq("org_id", uid)
                .eq("persona", self._sb_persona())
                .limit(200)
                .execute()
            )
            rows = res.data or []
            if not rows:
                return []
            import random

            return self._parse_rows(random.sample(rows, min(n, len(rows))))
        except Exception as e:
            logger.error("[Episode DB] Supabase sample_random failed: %s", e)
            return []

    def recall(
        self,
        query_vector: list[float],
        limit: int = 5,
        exclude_tags: list[str] | None = None,
        end_user_id: str | None = None,
    ) -> list[dict]:
        """Vector search over episodes. When ``end_user_id`` is given, results are
        scoped to that end-user — isolating personal/sensitive conversation memory.
        ``None`` searches the whole persona store (owner lane + cross-user learning)."""
        if self._use_supabase:
            return self._sb_recall(query_vector, limit, exclude_tags, end_user_id)
        if not self._ensure_ready():
            return []
        try:
            preds = [f"topic_tags NOT LIKE '%{_sql_quote(t)}%'" for t in (exclude_tags or [])]
            if end_user_id is not None:
                preds.append(f"end_user_id = '{_sql_quote(end_user_id)}'")
            q = self._table.search(query_vector).limit(limit)
            if preds:
                q = q.where(" AND ".join(preds))
            results = q.to_list()
            return self._parse_rows(results)
        except Exception as e:
            logger.error("[Episode DB] Memory search failed: %s", e)
            return []

    def _sb_recall(
        self,
        query_vector: list[float],
        limit: int,
        exclude_tags: list[str] | None,
        end_user_id: str | None = None,
    ) -> list[dict]:
        try:
            sb, uid = self._sb()
            persona = self._sb_persona()
            vec_str = f"[{','.join(str(v) for v in query_vector)}]"
            # Use Supabase RPC for pgvector cosine similarity search
            params = {
                "query_vector": vec_str,
                "org_id_param": uid,
                "persona_param": persona,
                "match_count": limit,
            }
            if exclude_tags:
                params["exclude_tags"] = exclude_tags
            # match_episodes already accepts end_user_param (007); only filters when set.
            if end_user_id is not None:
                params["end_user_param"] = end_user_id
            res = sb.rpc("match_episodes", params).execute()
            return self._parse_rows(res.data or [])
        except Exception as e:
            logger.error("[Episode DB] Supabase recall failed: %s", e)
            return []

    def recall_by_tag(
        self, query_vector: list[float], tag: str, limit: int = 3, end_user_id: str | None = None
    ) -> list[dict]:
        """Vector search scoped to episodes that contain the given tag. ``end_user_id``
        further scopes to one end-user (personal-memory isolation); ``None`` = persona-wide."""
        if self._use_supabase:
            return self._sb_recall_by_tag(query_vector, tag, limit, end_user_id)
        if not self._ensure_ready():
            return []
        try:
            pred = f"topic_tags LIKE '%{_sql_quote(tag)}%'"
            if end_user_id is not None:
                pred += f" AND end_user_id = '{_sql_quote(end_user_id)}'"
            results = self._table.search(query_vector).where(pred).limit(limit).to_list()
            return self._parse_rows(results)
        except Exception as e:
            logger.error("[Episode DB] Tag-scoped recall failed (tag=%r): %s", tag, e)
            return []

    def _sb_recall_by_tag(
        self, query_vector: list[float], tag: str, limit: int, end_user_id: str | None = None
    ) -> list[dict]:
        try:
            sb, uid = self._sb()
            vec_str = f"[{','.join(str(v) for v in query_vector)}]"
            params = {
                "query_vector": vec_str,
                "org_id_param": uid,
                "persona_param": self._sb_persona(),
                "tag_param": tag,
                "match_count": limit,
            }
            if end_user_id is not None:
                params["end_user_param"] = end_user_id
            res = sb.rpc("match_episodes_by_tag", params).execute()
            return self._parse_rows(res.data or [])
        except Exception as e:
            logger.error("[Episode DB] Supabase tag-recall failed: %s", e)
            return []

    def recall_structural(
        self,
        current_sig: dict[str, float],
        approach_tags: list[str] | None = None,
        limit: int = 3,
        exclude_session: str | None = None,
        scan_cap: int = 500,
    ) -> list[dict]:
        """Rank episodes by COGNITIVE-SIGNATURE similarity rather than topic.

        This is the cross-domain transfer path: it ignores text content entirely
        and matches on the activation profile (chemistry + problem-structure
        flags) stored in each episode's cog_signature. Candidates whose
        ``approach:*`` tags overlap the current approach get a small boost.

        Returns parsed episodes (top ``limit`` by score) each annotated with
        ``cog_sim`` (raw signature cosine, [-1, 1]) and ``approach_overlap``
        (int). The caller applies its own minimum-similarity threshold — the
        store always returns the best candidates so the caller can also detect
        the "even the closest match is weak" (anomalous-state) case.
        """
        if not current_sig:
            return []
        if self._use_supabase:
            rows = self._sb_recall_recent(scan_cap)
        else:
            if not self._ensure_ready():
                return []
            try:
                rows = self._parse_rows(self._table.to_arrow().to_pylist())
            except Exception as e:
                logger.error("[Episode DB] Structural scan failed: %s", e)
                return []
        approach_set = {t for t in (approach_tags or []) if t}
        scored = []
        for ep in rows:
            if exclude_session and ep.get("session_id") == exclude_session:
                continue
            sig = ep.get("cog_signature") or {}
            if not sig:
                continue
            sim = _signature_cosine(current_sig, sig)
            overlap = sum(
                1
                for t in ep.get("topic_tags", [])
                if t.startswith("approach:") and t in approach_set
            )
            ep["cog_sim"] = round(sim, 4)
            ep["approach_overlap"] = overlap
            ep["_score"] = sim + 0.05 * overlap
            scored.append(ep)
        scored.sort(key=lambda e: e["_score"], reverse=True)
        return scored[:limit]

    @staticmethod
    def _as_list(v) -> list:
        # LanceDB stores these as JSON strings; Supabase (text[]) returns real lists.
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                return []
        return []

    @staticmethod
    def _as_dict(v) -> dict:
        # LanceDB stores this as a JSON string; Supabase (jsonb) returns a real dict.
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                return {}
        return {}

    def _parse_rows(self, rows: list[dict]) -> list[dict]:
        episodes = []
        for r in rows:
            ep = dict(r)
            ep["topic_tags"] = self._as_list(ep.get("topic_tags"))
            ep["entities"] = self._as_list(ep.get("entities"))
            ep["neuromod_snapshot"] = self._as_dict(ep.get("neuromod_snapshot"))
            ep["cog_signature"] = self._as_dict(ep.get("cog_signature"))
            episodes.append(ep)
        return episodes

    _SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    def recall_by_session(self, session_id: str) -> list[dict]:
        if not self._SESSION_ID_RE.match(session_id):
            logger.warning(
                "[Episode DB] [Security] Blocked unsafe session ID in memory query: %r", session_id
            )
            return []
        if self._use_supabase:
            try:
                sb, uid = self._sb()
                res = (
                    sb.table("episodes")
                    .select("*")
                    .eq("org_id", uid)
                    .eq("persona", self._sb_persona())
                    .eq("session_id", session_id)
                    .execute()
                )
                return self._parse_rows(res.data or [])
            except Exception as e:
                logger.error("[Episode DB] Supabase session recall failed: %s", e)
                return []
        if not self._ensure_ready():
            return []
        try:
            results = (
                self._table.search().where(f"session_id = '{_sql_quote(session_id)}'").to_list()
            )
            return results
        except Exception as e:
            logger.error("[Episode DB] Session recall failed: %s", e)
            return []


class SchemaStore:
    """
    Schema layer: Markdown files (local) or Supabase brain_schemas table (cloud).

    Backend selection: BRAIN_STORAGE_BACKEND=local (default) | supabase
    Writes are serialized with an asyncio.Lock and use temp-file-then-rename
    so concurrent encode + sleep-consolidation cannot corrupt the schema.
    Sync write/append remain for boot-time (no event loop) use.
    """

    _FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.md$")
    # Class-level default so __new__-built instances (test doubles) stay local.
    _use_supabase = False

    def __init__(self, persona: str = "") -> None:
        self._use_supabase = _STORAGE_BACKEND == "supabase"
        self._persona = persona
        if not self._use_supabase:
            SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _sb(self):
        from brain.second_brain.supabase_client import get_client, get_user_id

        return get_client(), get_user_id()

    def _sb_persona(self) -> str:
        return _persona_key(_resolve_persona(self._persona))

    def _validate_filename(self, filename: str) -> bool:
        """Return True if filename is safe; log a warning and return False otherwise."""
        if not self._FILENAME_RE.match(filename):
            logger.warning(
                "[Schema DB] [Security] Blocked unsafe filename (possible path traversal): %r",
                filename,
            )
            return False
        resolved = (SCHEMA_DIR / filename).resolve()
        if not resolved.is_relative_to(SCHEMA_DIR.resolve()):
            logger.warning(
                "[Schema DB] [Security] Blocked filename that tries to escape the schema directory: %r",
                filename,
            )
            return False
        return True

    def read(self, filename: str) -> str:
        if not self._validate_filename(filename):
            return ""
        if self._use_supabase:
            try:
                sb, uid = self._sb()
                res = (
                    sb.table("brain_schemas")
                    .select("content")
                    .eq("org_id", uid)
                    .eq("persona", self._sb_persona())
                    .eq("end_user_id", "")
                    .eq("filename", filename)
                    .maybe_single()
                    .execute()
                )
                # maybe_single() returns None (no response) on zero rows in this
                # supabase-py version — treat a missing row like a missing file.
                return (getattr(res, "data", None) or {}).get("content", "")
            except Exception as e:
                logger.error("[Schema DB] Supabase read failed (%s): %s", filename, e)
                return ""
        path = SCHEMA_DIR / filename
        if path.exists():
            return path.read_text()
        return ""

    def _atomic_write(self, path: Path, content: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)

    def _sb_write(self, filename: str, content: str, persona: str | None = None) -> None:
        """persona must be resolved IN the event-loop task when this runs on an
        executor thread: workers don't inherit the bind_persona contextvar, so
        resolving here would fall back to BRAIN_PERSONA_NAME (the process home
        persona) and silently write a bound persona's file onto the home row."""
        try:
            sb, uid = self._sb()
            sb.table("brain_schemas").upsert(
                {
                    "org_id": uid,
                    "persona": self._sb_persona() if persona is None else persona,
                    "end_user_id": "",  # companion mode; engine-mode callers will thread this
                    "filename": filename,
                    "content": content,
                    "updated_at": "now()",
                },
                # Must name the table's actual unique constraint
                # (org_id, persona, end_user_id, filename) — migration 007. A stale
                # column list here makes every upsert error out (silently, log-only).
                on_conflict="org_id,persona,end_user_id,filename",
            ).execute()
        except Exception as e:
            logger.error("[Schema DB] Supabase write failed (%s): %s", filename, e)

    def write(self, filename: str, content: str) -> None:
        """Sync write — only safe at boot / outside event loop."""
        if not self._validate_filename(filename):
            return
        if self._use_supabase:
            self._sb_write(filename, content)
            return
        self._atomic_write(SCHEMA_DIR / filename, content)

    def append_fact(self, filename: str, fact: str) -> None:
        """Sync append — only safe at boot / outside event loop."""
        if not self._validate_filename(filename):
            return
        fact = fact.strip()
        if not fact:
            return
        existing = self.read(filename)
        if fact not in existing:
            self.write(filename, existing + f"\n- {fact}")

    async def awrite(self, filename: str, content: str) -> None:
        if not self._validate_filename(filename):
            return
        async with self._lock:
            if self._use_supabase:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._sb_write, filename, content, self._sb_persona()
                )
            else:
                self._atomic_write(SCHEMA_DIR / filename, content)

    async def aappend_fact(self, filename: str, fact: str) -> None:
        if not self._validate_filename(filename):
            return
        fact = fact.strip()
        if not fact:
            return
        async with self._lock:
            existing = self.read(filename)
            if fact not in existing:
                new_content = existing + f"\n- {fact}"
                if self._use_supabase:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._sb_write, filename, new_content, self._sb_persona()
                    )
                else:
                    self._atomic_write(SCHEMA_DIR / filename, new_content)

    @staticmethod
    def _replace_section_body(content: str, section: str, new_body: str) -> str:
        """Return content with the body of ## <section> replaced by new_body.
        If the section does not exist, appends it at the end. new_body should
        NOT include the heading line itself. Trailing/leading blank lines in
        new_body are normalized."""
        new_body = new_body.strip("\n")
        # Match the heading line, then any content up to the next ## or EOF.
        # Heading is matched at line start; section name is anchored with $ to
        # avoid partial matches ("## Preferences" must not match "## Preferences (old)").
        pattern = re.compile(
            r"(^##[ \t]+" + re.escape(section) + r"[ \t]*\r?\n)(.*?)(?=^##[ \t]|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        # Substitute via a FUNCTION, not a template string: re parses backslash
        # escapes in a template, and new_body is arbitrary caller content. The DMN's
        # open-threads ledger renders its body with json.dumps(), whose ensure_ascii
        # escapes every non-ASCII char to \uXXXX — so one curly apostrophe or em dash
        # in a thought (i.e. almost any LLM prose) raised "bad escape \u" here. re
        # compiles the template BEFORE scanning for matches, so it raised even when
        # the section was absent, making the `n == 0` append branch below unreachable:
        # the `## Open threads` section was never created at all, and _save_threads
        # swallowed the error as a warning. A literal \1 in a body would likewise
        # splice in a capture group. A function's return value is used verbatim.
        new_content, n = pattern.subn(lambda m: m.group(1) + new_body + "\n\n", content, count=1)
        if n == 0:
            # Section missing — append at end with proper spacing.
            tail = "" if content.endswith("\n") else "\n"
            new_content = content + tail + f"\n## {section}\n{new_body}\n"
        # Collapse any run of 3+ blank lines we may have created.
        new_content = re.sub(r"\n{3,}", "\n\n", new_content)
        return new_content

    def ensure_section(self, filename: str, section: str, default_body: str) -> None:
        """Sync: add the section to filename if it isn't already present."""
        if not self._validate_filename(filename):
            return
        content = self.read(filename)
        if not content:
            return
        if re.search(r"(?m)^##[ \t]+" + re.escape(section) + r"[ \t]*$", content):
            return
        new_content = self._replace_section_body(content, section, default_body)
        self.write(filename, new_content)

    async def upsert_section(self, filename: str, section: str, body: str) -> None:
        """Async upsert: replace (or create) the body of ## <section> in filename."""
        if not self._validate_filename(filename):
            return
        async with self._lock:
            content = self.read(filename)
            if not content:
                return
            new_content = self._replace_section_body(content, section, body)
            if new_content != content:
                if self._use_supabase:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._sb_write, filename, new_content, self._sb_persona()
                    )
                else:
                    self._atomic_write(SCHEMA_DIR / filename, new_content)

    def list_files(self) -> list[str]:
        if self._use_supabase:
            try:
                sb, uid = self._sb()
                res = (
                    sb.table("brain_schemas")
                    .select("filename")
                    .eq("org_id", uid)
                    .eq("persona", self._sb_persona())
                    .eq("end_user_id", "")
                    .execute()
                )
                return [r["filename"] for r in (res.data or [])]
            except Exception as e:
                logger.error("[Schema DB] Supabase list_files failed: %s", e)
                return []
        return [p.name for p in SCHEMA_DIR.glob("*.md")]

    def read_all(self) -> dict[str, str]:
        """filename → content for every schema file in this persona's scope.
        One query on Supabase — use this over list_files()+read() when you need
        the contents of several files (e.g. all per-speaker user models)."""
        if self._use_supabase:
            try:
                sb, uid = self._sb()
                res = (
                    sb.table("brain_schemas")
                    .select("filename,content")
                    .eq("org_id", uid)
                    .eq("persona", self._sb_persona())
                    .eq("end_user_id", "")
                    .execute()
                )
                return {r["filename"]: r.get("content") or "" for r in (res.data or [])}
            except Exception as e:
                logger.error("[Schema DB] Supabase read_all failed: %s", e)
                return {}
        out: dict[str, str] = {}
        for p in SCHEMA_DIR.glob("*.md"):
            try:
                out[p.name] = p.read_text()
            except OSError:
                continue
        return out

    def grep(self, keyword: str) -> list[tuple[str, str]]:
        """Return (filename, matching_line) pairs."""
        if self._use_supabase:
            try:
                sb, uid = self._sb()
                res = (
                    sb.table("brain_schemas")
                    .select("filename,content")
                    .eq("org_id", uid)
                    .eq("persona", self._sb_persona())
                    .eq("end_user_id", "")
                    .ilike("content", f"%{keyword}%")
                    .execute()
                )
                hits = []
                for row in res.data or []:
                    for line in row["content"].splitlines():
                        if keyword.lower() in line.lower():
                            hits.append((row["filename"], line.strip()))
                return hits
            except Exception as e:
                logger.error("[Schema DB] Supabase grep failed: %s", e)
                return []
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
        if not self.read("self.md"):
            self.write(
                "self.md",
                "# Entity Self-Model\n\n"
                "## Identity\n- Instantiated: " + time.strftime("%Y-%m-%d") + "\n\n"
                "## Stable preferences\n\n"
                "## Relational identity\n\n"
                "## History summary\n\n"
                "## Current mood signature\n\n"
                "## Values\n",
            )

    def ensure_user_schema(self, user_name: str = "User") -> None:
        if not self.read("user.md"):
            self.write(
                "user.md",
                f"# User: {user_name}\n\n"
                "## Known facts\n\n"
                "## Preferences\n\n"
                "## Communication style\n"
                "- (learning…)\n\n"
                "## Mood response patterns\n"
                "- (learning…)\n\n"
                "## Emotional profile\n\n"
                "## Relationship\n"
                "- Familiarity: new (conversations so far: ~0)\n\n"
                "## Affection score\n"
                "- Score: 0\n",
            )
        else:
            self.ensure_section("user.md", "Communication style", "- (learning…)")
            self.ensure_section("user.md", "Mood response patterns", "- (learning…)")

    _SPEAKER_SLUG_RE = re.compile(r"[^a-z0-9]+")

    def speaker_filename(self, name: str) -> str:
        """Convert a speaker name to a safe per-speaker schema filename.

        The slug is lossy — it folds every non-alphanumeric run to "_" and truncates
        — so on its own it COLLIDES: "user@a.com" and "user_a_com" produced the same
        file, as did any two ids sharing a 32-char prefix. Two different customers
        then shared one profile document, reading each other's personal facts and
        preferences. The digest is over the raw name, so the filename is unique even
        when the readable part is not; the prefix is kept only so a human browsing
        the directory can still tell whose file this is."""
        raw = name.strip()
        slug = self._SPEAKER_SLUG_RE.sub("_", raw.lower()).strip("_")[:32]
        slug = slug or "unknown"
        digest = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        return f"user_{slug}_{digest}.md"

    def ensure_speaker_schema(self, name: str) -> str:
        """Ensure a per-speaker schema file exists. Returns the filename."""
        filename = self.speaker_filename(name)
        if not self.read(filename):
            self.write(
                filename,
                f"# User: {name}\n\n"
                "## Known facts\n\n"
                "## Preferences\n\n"
                "## Communication style\n"
                "- (learning…)\n\n"
                "## Mood response patterns\n"
                "- (learning…)\n\n"
                "## Emotional profile\n\n"
                "## Relationship\n"
                "- Familiarity: new\n\n"
                "## Affection score\n"
                "- Score: 0\n",
            )
        else:
            self.ensure_section(filename, "Communication style", "- (learning…)")
            self.ensure_section(filename, "Mood response patterns", "- (learning…)")
        return filename

    def load_speaker_context(self, name: str) -> str:
        """Read (and if needed create) the schema file for a named speaker."""
        filename = self.ensure_speaker_schema(name)
        return self.read(filename)

    async def migrate_placeholder(self, placeholder_filename: str, target_filename: str) -> None:
        """Append facts from a placeholder schema into the real one, then delete placeholder."""
        if not self._validate_filename(placeholder_filename):
            return
        if not self._validate_filename(target_filename):
            return
        src = self.read(placeholder_filename)
        if not src:
            return
        async with self._lock:
            dst = self.read(target_filename)
            facts = [
                ln.strip()
                for ln in src.splitlines()
                if ln.strip().startswith("- ") and ln.strip() not in dst
            ]
            if facts:
                new_content = dst + "\n" + "\n".join(facts)
                if self._use_supabase:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._sb_write, target_filename, new_content, self._sb_persona()
                    )
                    # Delete placeholder from Supabase
                    try:
                        sb, uid = self._sb()
                        (
                            sb.table("brain_schemas")
                            .delete()
                            .eq("org_id", uid)
                            .eq("persona", self._sb_persona())
                            .eq("filename", placeholder_filename)
                            .execute()
                        )
                    except Exception as e:
                        logger.warning("[Schema] Supabase placeholder delete failed: %s", e)
                else:
                    self._atomic_write(SCHEMA_DIR / target_filename, new_content)
                    (SCHEMA_DIR / placeholder_filename).unlink(missing_ok=True)
            logger.info(
                "[Schema] Migrated placeholder %s → %s (%d facts)",
                placeholder_filename,
                target_filename,
                len(facts),
            )

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
