"""
One-time migration: copy local second_brain/ files to Supabase.

Usage:
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... USER_ID=<your-supabase-user-uuid> \
    uv run python scripts/migrate_to_supabase.py [--persona the_visionary]

The USER_ID is the UUID from Supabase Auth (auth.users table).
Run this once before switching BRAIN_STORAGE_BACKEND=supabase.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def get_supabase():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def migrate_schema(sb, user_id: str, persona: str, schema_dir: Path) -> None:
    logger.info("Migrating schema files from %s", schema_dir)
    for md_file in schema_dir.glob("*.md"):
        content = md_file.read_text()
        sb.table("brain_schemas").upsert({
            "user_id": user_id,
            "persona": persona,
            "filename": md_file.name,
            "content": content,
            "updated_at": "now()",
        }, on_conflict="user_id,persona,filename").execute()
        logger.info("  schema: %s (%d chars)", md_file.name, len(content))


def migrate_wiring(sb, user_id: str, persona: str, wiring_path: Path) -> None:
    if not wiring_path.exists():
        logger.info("No wiring.json at %s — skipping", wiring_path)
        return
    data = json.loads(wiring_path.read_text())
    rows = [
        {
            "user_id": user_id,
            "persona": persona,
            "source": e["src"],
            "target": e["tgt"],
            "weight": e["w"],
            "polarity": e["pol"],
            "updated_at": "now()",
        }
        for e in data
    ]
    if rows:
        sb.table("wiring_edges").upsert(
            rows, on_conflict="user_id,persona,source,target"
        ).execute()
        logger.info("Migrated %d wiring edges for persona=%s", len(rows), persona)


def migrate_episodes(sb, user_id: str, persona: str, episodes_dir: Path) -> None:
    """Migrate LanceDB episodes to Supabase. Requires lancedb installed."""
    if not episodes_dir.exists():
        logger.info("No episodes dir at %s — skipping", episodes_dir)
        return
    try:
        import lancedb
    except ImportError:
        logger.warning("lancedb not installed — skipping episode migration. uv add lancedb")
        return

    db = lancedb.connect(str(episodes_dir))
    if "episodes" not in db.table_names():
        logger.info("No episodes table found — skipping")
        return

    table = db.open_table("episodes")
    rows = table.to_arrow().to_pylist()
    logger.info("Migrating %d episodes for persona=%s", len(rows), persona)

    batch_size = 50
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        records = []
        for r in batch:
            vec = r.get("vector") or []
            records.append({
                "user_id": user_id,
                "persona": persona,
                "session_id": r.get("session_id"),
                "turn_id": r.get("turn_id"),
                "ts": r.get("ts"),
                "user_input": r.get("user_input"),
                "entity_response": r.get("entity_response"),
                "topic_tags": json.loads(r.get("topic_tags") or "[]"),
                "emotion_state": r.get("emotion_state"),
                "user_emotion": r.get("user_emotion"),
                "entities": json.loads(r.get("entities") or "[]"),
                "neuromod_snapshot": json.loads(r.get("neuromod_snapshot") or "{}"),
                "surprise_score": r.get("surprise_score") or 0.0,
                "vector": f"[{','.join(str(v) for v in (vec if isinstance(vec, list) else vec.tolist()))}]",
            })
        sb.table("episodes").insert(records).execute()
        logger.info("  episodes: inserted batch %d-%d", i, i + len(batch))


def create_user_profile(sb, user_id: str, active_persona: str) -> None:
    sb.table("user_profiles").upsert({
        "id": user_id,
        "active_persona": active_persona,
    }, on_conflict="id").execute()
    logger.info("User profile created/updated for %s", user_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate local second_brain to Supabase")
    parser.add_argument("--persona", default="the_visionary", help="Persona slug to migrate")
    parser.add_argument("--all-personas", action="store_true", help="Migrate all personas")
    args = parser.parse_args()

    user_id = os.environ.get("USER_ID")
    if not user_id:
        logger.error("USER_ID env var required (your Supabase auth.users UUID)")
        sys.exit(1)

    sb = get_supabase()
    second_brain = ROOT / "second_brain"

    personas_to_migrate = []
    if args.all_personas:
        personas_dir = second_brain / "personas"
        if personas_dir.exists():
            personas_to_migrate = [p.name for p in personas_dir.iterdir() if p.is_dir()]
    else:
        personas_to_migrate = [args.persona]

    if not personas_to_migrate:
        logger.error("No personas found to migrate")
        sys.exit(1)

    logger.info("Migrating personas: %s", personas_to_migrate)

    # Create user profile with first persona as active
    create_user_profile(sb, user_id, personas_to_migrate[0])

    for persona in personas_to_migrate:
        logger.info("── Persona: %s ──", persona)
        persona_dir = second_brain / "personas" / persona

        # Schema files
        schema_dir = persona_dir / "schema"
        if schema_dir.exists():
            migrate_schema(sb, user_id, persona, schema_dir)
        else:
            # Fall back to top-level schema (no persona isolation yet)
            migrate_schema(sb, user_id, persona, second_brain / "schema")

        # Wiring
        wiring_path = persona_dir / "wiring.json"
        if not wiring_path.exists():
            wiring_path = second_brain / "wiring.json"
        migrate_wiring(sb, user_id, persona, wiring_path)

        # Episodes
        episodes_dir = persona_dir / "episodes"
        if not episodes_dir.exists():
            episodes_dir = second_brain / "episodes"
        migrate_episodes(sb, user_id, persona, episodes_dir)

    logger.info("Migration complete.")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Set BRAIN_STORAGE_BACKEND=supabase in your .env")
    logger.info("  2. Verify data in Supabase dashboard")
    logger.info("  3. Deploy to Railway")


if __name__ == "__main__":
    main()
