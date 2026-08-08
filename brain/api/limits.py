"""
Every hard limit the engine API enforces, in one place.

These were previously either absent (no body cap, no bound on /v1/extract input or
schema, no charset or length on end_user_id, no cap on pinned skills) or present in
code but written down nowhere, so an integrator could only discover them by hitting
one. The developer guide's limits table is generated from `as_dict()` and a test
asserts the two agree, so the documented numbers cannot drift from the enforced ones.

Constants live here rather than at their enforcement sites precisely so the endpoint
that REPORTS them (GET /v1/capabilities) and the code that ENFORCES them read the
same value.
"""

from __future__ import annotations

import os

from brain.ids import END_USER_ID_HELP, END_USER_ID_RE

# Request body ceiling. The gateway buffers a body to forward it, so this bounds an
# allocation in the one process every tenant shares. Audio crosses as base64 inside
# JSON, so raising audio limits means raising this too.
MAX_BODY_BYTES = int(os.environ.get("BRAIN_MAX_BODY_BYTES", str(10 * 1024 * 1024)))

# POST /v1/extract. Only the OUTPUT was bounded (max_tokens=1024); a caller could
# send an unbounded document and an unbounded schema to a metered cloud model.
EXTRACT_MAX_INPUT_CHARS = 100_000
EXTRACT_MAX_SCHEMA_BYTES = 16_384

# Session skill pins. Accepted as any list of strings, so a 10k-element list was
# validated and persisted.
MAX_PINNED_SKILLS = 32

# POST .../grade — provenance string, clamped before it reaches the eval log.
GRADE_SOURCE_MAX = 64

# Largest WebSocket frame relayed upstream.
MAX_WS_FRAME_BYTES = int(os.environ.get("BRAIN_MAX_WS_FRAME_BYTES", str(8 * 1024 * 1024)))


def as_dict() -> dict:
    """The limits block served by GET /v1/capabilities and rendered into the guide."""
    return {
        "max_body_bytes": MAX_BODY_BYTES,
        "max_ws_frame_bytes": MAX_WS_FRAME_BYTES,
        "extract_max_input_chars": EXTRACT_MAX_INPUT_CHARS,
        "extract_max_schema_bytes": EXTRACT_MAX_SCHEMA_BYTES,
        "max_pinned_skills": MAX_PINNED_SKILLS,
        "grade_source_max_chars": GRADE_SOURCE_MAX,
        "end_user_id_pattern": END_USER_ID_RE.pattern,
        "end_user_id_help": END_USER_ID_HELP,
    }
