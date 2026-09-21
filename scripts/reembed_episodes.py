"""
Re-embed stored episodes into the current embedding space, and repair rows that
were never embedded.

WHY. Two 768-dimensional models wrote into `episodes.vector`: Google's
gemini-embedding-001 until 2026-09-13, then Ollama's nomic-embed-text once the
gateway's CPU embed sidecar started working. Vectors from different models are not
comparable — cosine distance between them is noise — so every search silently
missed the half of memory the other model had written. Migration 044 records the
model per row and makes the search functions compare only same-model rows, which
makes the split visible and safe; this script closes it.

It also repairs rows with no vector at all. A failed embed used to be stored as a
768-wide zero vector, which pgvector can never return from a cosine search (the
distance is NaN); 044 turned those into NULL, and they are re-embedded here.

WHAT IT TOUCHES BY DEFAULT. Only rows a person would miss: conversation turns,
engine/agent runs, sleep insights and any conclusion the DMN did not reach by
itself (user-confirmed ones included). The DMN's own idle output — idle thoughts,
deferred questions and self-reached conclusions, about ten times the volume of real
turns — is skipped, because `dmn_idle_retention_days` ages it out anyway. What
counts as idle is decided by store.is_dmn_idle_episode, the same predicate
retention uses, so this script and the prune can never disagree. Pass
--include-idle to convert idle output too.

USAGE
    # see what would change, touching nothing
    python scripts/reembed_episodes.py --dry-run

    # convert everything that is not an idle thought
    python scripts/reembed_episodes.py

    # one org, idle thoughts included, against a specific embedding host
    python scripts/reembed_episodes.py --org <uuid> --include-idle \
        --embed-host http://127.0.0.1:11434

ENVIRONMENT
    SUPABASE_URL, SUPABASE_SERVICE_KEY   required (service role: this rewrites
                                         rows across orgs)
    OLLAMA_EMBED_HOST / OLLAMA_HOST      default embedding host, overridden by
                                         --embed-host

The embedding host must serve the SAME model the brains use (nomic-embed-text);
the script refuses to run if the host returns a different dimension. It is safe to
re-run: rows already in the current space are not selected.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

# Idle-thought markers and the model name come from the app itself, so this script
# cannot drift from what the brains write and prune.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.model_router import EMBEDDING_DIM, OLLAMA_EMBED_MODEL  # noqa: E402
from brain.second_brain.store import is_dmn_idle_episode  # noqa: E402

PAGE = 200


def _client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.")
    from supabase import create_client

    return create_client(url, key)


def _embed(host: str, text: str, threads: int) -> list[float] | None:
    body: dict = {"model": OLLAMA_EMBED_MODEL, "prompt": text[:8192]}
    if threads > 0:
        body["options"] = {"num_thread": threads}
    r = httpx.post(f"{host}/api/embeddings", json=body, timeout=60)
    r.raise_for_status()
    vec = r.json().get("embedding")
    if not vec:
        return None
    if len(vec) != EMBEDDING_DIM:
        sys.exit(
            f"{host} returned {len(vec)}-dim vectors, expected {EMBEDDING_DIM}. "
            f"Is it serving {OLLAMA_EMBED_MODEL}?"
        )
    return vec


def _select(sb, org: str | None, include_idle: bool, after_id: int, limit: int):
    """Rows not in the current embedding space, oldest id first.

    Covers both cases in one predicate: a vector from another model, and no vector
    at all (embed_model is null). Paging by id keeps the scan stable while rows are
    being rewritten underneath it."""
    q = (
        sb.table("episodes")
        .select("id,org_id,persona,user_input,entity_response,embed_model,topic_tags")
        .or_(f"embed_model.is.null,embed_model.neq.{OLLAMA_EMBED_MODEL}")
        .gt("id", after_id)
        .order("id")
        .limit(limit)
    )
    if org:
        q = q.eq("org_id", org)
    rows = q.execute().data or []
    if not include_idle:
        # Filter on topic_tags, matching what retention prunes. Filtering on the
        # `user_input` literal used to drop EVERY conclusion — including
        # user-confirmed ones and successful agent runs — leaving exactly the rows
        # a person would miss stranded in the old embedding space forever.
        rows = [r for r in rows if not is_dmn_idle_episode(r.get("topic_tags"))]
    return rows


def _served_models(host: str) -> list[str] | None:
    """Model names the host reports (Ollama /api/tags), or None if it won't say."""
    try:
        r = httpx.get(f"{host}/api/tags", timeout=15)
        r.raise_for_status()
        return [str(m.get("name") or m.get("model") or "") for m in (r.json().get("models") or [])]
    except Exception:
        return None


def _same_model(served: str, want: str) -> bool:
    """Ollama reports "nomic-embed-text:latest" for "nomic-embed-text"."""
    return served.split(":", 1)[0] == want.split(":", 1)[0]


def verify_model(host: str, allow_unverified: bool = False) -> str:
    """Confirm the host actually serves OLLAMA_EMBED_MODEL before anything is written.

    A dimension check alone is not identity. Several 768-dim models exist
    (bge-base, all-mpnet, a stale gemini proxy); any of them would pass a
    length check and then be written to every row LABELLED nomic-embed-text.
    That recreates the exact mixed-vector-space corruption migration 044 exists
    to fix, except now invisible, because the labels all agree and nothing
    downstream can tell the rows apart again.

    Returns a short description of what was verified."""
    served = _served_models(host)
    if served is None:
        if not allow_unverified:
            sys.exit(
                f"{host} would not report its models (no /api/tags). Refusing to write "
                f"vectors that cannot be confirmed as {OLLAMA_EMBED_MODEL} — a different "
                f"768-dim model would be silently mislabelled. Re-run with "
                f"--allow-unverified-model only if you are certain."
            )
        return f"UNVERIFIED (host would not list its models; trusting {OLLAMA_EMBED_MODEL})"
    if not any(_same_model(m, OLLAMA_EMBED_MODEL) for m in served):
        sys.exit(
            f"{host} does not serve {OLLAMA_EMBED_MODEL}. It has: "
            f"{', '.join(served) or '(nothing)'}. Point --embed-host at the right host, "
            f"or pull the model there."
        )
    return f"{OLLAMA_EMBED_MODEL} confirmed present on {host}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dry-run", action="store_true", help="count and sample only; write nothing")
    ap.add_argument("--org", help="restrict to one org id")
    ap.add_argument("--include-idle", action="store_true", help="also convert DMN idle thoughts")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    ap.add_argument(
        "--embed-host",
        default=(os.environ.get("OLLAMA_EMBED_HOST") or os.environ.get("OLLAMA_HOST") or "").strip()
        or "http://127.0.0.1:11434",
    )
    ap.add_argument("--threads", type=int, default=2, help="num_thread sent per embed (0 = unset)")
    ap.add_argument(
        "--allow-unverified-model",
        action="store_true",
        help="proceed even if the host will not list its models (it cannot then be "
        "confirmed as serving the expected embedding model)",
    )
    args = ap.parse_args()

    host = args.embed_host.rstrip("/")
    sb = _client()

    # Fail before touching anything if the host is wrong, not serving the model, or
    # serving a DIFFERENT model of the same width. Identity first, then dimension.
    note = verify_model(host, args.allow_unverified_model)
    probe = _embed(host, "probe", args.threads)
    if probe is None:
        sys.exit(f"{host} returned an empty embedding.")
    print(f"host {host}: {note} ({len(probe)}-dim) — ok")

    done = failed = 0
    after_id = 0
    started = time.time()
    while True:
        page = _select(sb, args.org, args.include_idle, after_id, PAGE)
        if not page:
            break
        for row in page:
            after_id = max(after_id, int(row["id"]))
            if args.limit and done >= args.limit:
                break
            text = f"{row.get('user_input') or ''} {row.get('entity_response') or ''}".strip()
            if not text:
                continue
            if args.dry_run:
                if done < 5:
                    was = row.get("embed_model") or "unembedded"
                    print(f"  would re-embed id={row['id']} ({was}): {text[:70]!r}")
                done += 1
                continue
            try:
                vec = _embed(host, text, args.threads)
                if vec is None:
                    failed += 1
                    continue
                sb.table("episodes").update(
                    {
                        "vector": f"[{','.join(str(v) for v in vec)}]",
                        "embed_model": OLLAMA_EMBED_MODEL,
                    }
                ).eq("id", row["id"]).execute()
                done += 1
            except Exception as e:  # keep going: one bad row must not end the run
                failed += 1
                print(f"  ! id={row['id']}: {type(e).__name__}: {e}")
            if done and done % 100 == 0:
                print(f"  {done} re-embedded ({time.time() - started:.0f}s)")
        if args.limit and done >= args.limit:
            break

    verb = "would re-embed" if args.dry_run else "re-embedded"
    print(f"{verb} {done} row(s), {failed} failed, in {time.time() - started:.0f}s")
    if not args.dry_run and done:
        print("Rows now carry embed_model — searches compare only same-model vectors.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
