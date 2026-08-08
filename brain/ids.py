"""
Identifier shapes, in one place.

Persona slugs, mandate ids and skill ids each had their own regex and their own
near-identical validator (mandates' and skills' were byte-identical apart from the
exception type). Meanwhile `end_user_id` — the only one of them supplied by an
outside caller — had no validation at all beyond "non-empty string".

Dependency-free on purpose (stdlib `re` only), like brain/persona_key.py, so stores
and the API layer can both import it without dragging the world in.
"""

from __future__ import annotations

import re

# Persona slugs: no dashes, because they become directory names and process keys.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")

# Mandate and skill ids: dashes allowed, they are never path segments.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# End-user ids are PARTNER-CHOSEN and identify their customer, so this is
# deliberately wider than a slug — emails and UUIDs are legitimate and common. It is
# an allowlist rather than a denylist, and every character left out corresponds to a
# real injection site rather than to taste:
#
#   whitespace / newline — the id is interpolated into an LLM prompt and written to
#       tool_log.md (brain/clusters/_executor_common.py), which is read back as model
#       context; a newline lets a caller forge log structure inside that context.
#   ':'                  — Supabase vault names are built by concatenation,
#       'mcp:' || org || ':' || end_user_id || ':' || server_name (migration 012), so
#       a colon makes two different (customer, server) pairs collide on one name.
#   quotes and '%'       — LanceDB predicates are assembled by string concatenation
#       in the local backend (brain/second_brain/store.py).
#   '/' '\' '.'-runs     — the id reaches filename derivation.
#
# 128 chars keeps vault names, log lines and derived filenames bounded.
END_USER_ID_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")

END_USER_ID_HELP = (
    "end_user_id must be 1-128 characters of letters, digits, or . _ @ + - "
    "(no whitespace, quotes, colons or slashes)"
)


def valid_id(value: object, *, pattern: re.Pattern[str], label: str, exc: type[Exception]) -> str:
    """Return the id, or raise `exc` describing the expected shape."""
    s = str(value or "")
    if not pattern.match(s):
        raise exc(f"invalid {label}: {s[:64]!r}")
    return s


def valid_end_user_id(value: object) -> str:
    """Validate a partner-supplied end_user_id. Raises ValueError.

    Rejects rather than sanitises. Normalising (say, stripping colons) would silently
    map two distinct customers onto one identity, which is worse than refusing the
    request — it merges their memory, chemistry and connector tokens."""
    s = str(value or "").strip()
    if not END_USER_ID_RE.match(s):
        raise ValueError(END_USER_ID_HELP)
    return s
