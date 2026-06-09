"""
Tests for FrontalCluster.set_capabilities() — ensures that the entity's
actual tool capabilities are surfaced into the drafter prompt so drafters
can answer "what tools do you have?" accurately instead of confabulating.
"""

from __future__ import annotations

from brain.clusters.frontal import FrontalCluster


def _make_frontal_skeleton():
    """Build a FrontalCluster skeleton bypassing __init__ — we only need
    the prompt-building methods and the _capabilities_summary attribute."""
    cluster = FrontalCluster.__new__(FrontalCluster)
    cluster._capabilities_summary = ""
    return cluster


def test_capabilities_starts_empty():
    f = _make_frontal_skeleton()
    assert f._capabilities_summary == ""


def test_set_capabilities_stores_string():
    f = _make_frontal_skeleton()
    f.set_capabilities("can read files, can ask Claude")
    assert f._capabilities_summary == "can read files, can ask Claude"


def test_set_capabilities_strips_whitespace():
    f = _make_frontal_skeleton()
    f.set_capabilities("  \n  capable\n  ")
    assert f._capabilities_summary == "capable"


def test_set_capabilities_none_becomes_empty():
    f = _make_frontal_skeleton()
    f.set_capabilities(None)  # type: ignore[arg-type]
    assert f._capabilities_summary == ""


def test_cached_context_includes_capabilities_when_set():
    """Capabilities now live in the per-session cached context block (sent as a
    dedicated cached system block) rather than the volatile drafter prompt — so
    they still reach the drafter, just billed at cache-read rates after turn 1."""
    f = _make_frontal_skeleton()
    f.set_capabilities("Tool use ENABLED. Can read files. Can invoke Claude Code.")
    ctx = f._build_cached_context(memory={})
    assert "Your capabilities this session" in ctx
    assert "Tool use ENABLED" in ctx
    assert "Claude Code" in ctx


def test_drafter_prompt_no_longer_carries_capabilities():
    """The volatile drafter prompt must NOT carry capabilities — they belong in
    the cached block, otherwise they'd be re-sent uncached every turn."""
    f = _make_frontal_skeleton()
    f.set_capabilities("Tool use ENABLED. Can read files.")
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={
            "response_type": "chitchat",
            "target_length": "brief",
            "tone": "warm",
            "key_points": [],
            "drafter_count": 1,
        },
    )
    assert "Your capabilities this session" not in prompt


def test_cached_context_omits_capabilities_section_when_empty():
    f = _make_frontal_skeleton()
    # default: _capabilities_summary == "" and no core memory
    ctx = f._build_cached_context(memory={})
    assert "Your capabilities this session" not in ctx


def test_cached_context_full_user_model_not_truncated():
    """The user model must be sent in full — the old 400-char truncation in the
    drafter prompt was the bug this caching change fixes."""
    f = _make_frontal_skeleton()
    long_user = "User model fact. " * 60  # ~1000 chars, well past the old 400 cap
    ctx = f._build_cached_context(memory={"core": {"user": long_user, "self": "I am an entity."}})
    assert long_user.strip() in ctx
    assert "I am an entity." in ctx


def test_cached_context_nonce_is_session_stable():
    """The fence nonce must be reused across calls so the cached block is
    byte-stable across turns (a fresh nonce each turn would defeat the cache)."""
    f = _make_frontal_skeleton()
    f.set_capabilities("Tool use ENABLED")
    ctx1 = f._build_cached_context(memory={"core": {"user": "u", "self": "s"}})
    ctx2 = f._build_cached_context(memory={"core": {"user": "u", "self": "s"}})
    assert ctx1 == ctx2


def test_cached_context_capabilities_appears_before_models():
    """Capabilities should lead the cached block so it's prominent."""
    f = _make_frontal_skeleton()
    f.set_capabilities("Tool use ENABLED")
    ctx = f._build_cached_context(memory={"core": {"user": "user facts", "self": "self facts"}})
    cap_pos = ctx.find("Your capabilities this session")
    self_pos = ctx.find("Entity self-model")
    assert cap_pos >= 0 and self_pos >= 0
    assert cap_pos < self_pos


# ── user-model placement: cached in companion mode, per-turn in engine mode ──────

_INSTR = {
    "response_type": "chitchat",
    "target_length": "brief",
    "tone": "warm",
    "key_points": [],
    "drafter_count": 1,
}
_CORE = {"self": "I am an entity.", "user": "USER_PROFILE_MARKER"}


def test_user_model_cached_in_companion_mode():
    """No end_user_id (companion) → the user-model stays in the cached block."""
    f = _make_frontal_skeleton()
    ctx = f._build_cached_context({"core": _CORE}, features={})
    assert "User model" in ctx and "USER_PROFILE_MARKER" in ctx
    assert "I am an entity." in ctx  # identity cached too


def test_user_model_not_cached_in_engine_mode():
    """A turn carrying end_user_id (engine) → user-model leaves the cached block,
    so the cached prefix is process-stable and shared across customers; identity
    (and any catalog) remain cached."""
    f = _make_frontal_skeleton()
    ctx = f._build_cached_context({"core": _CORE}, features={"end_user_id": "cust-1"})
    assert "USER_PROFILE_MARKER" not in ctx
    assert "I am an entity." in ctx  # identity is still cached


def test_user_model_in_drafter_prompt_in_engine_mode():
    """In engine mode the per-customer user-model rides the per-turn message."""
    f = _make_frontal_skeleton()
    prompt = f._build_drafter_prompt(
        features={"end_user_id": "cust-1", "raw_text": "hi"},
        memory={"core": _CORE},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction=_INSTR,
    )
    assert "User model" in prompt and "USER_PROFILE_MARKER" in prompt


def test_user_model_not_in_drafter_prompt_in_companion_mode():
    """Companion mode keeps the user-model cached, so it must NOT be re-sent in the
    volatile per-turn prompt."""
    f = _make_frontal_skeleton()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={"core": _CORE},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction=_INSTR,
    )
    assert "USER_PROFILE_MARKER" not in prompt


def test_engine_user_model_preferred_over_process_user_model():
    """When the customer has their own model, it wins over the process-level one."""
    f = _make_frontal_skeleton()
    prompt = f._build_drafter_prompt(
        features={
            "end_user_id": "cust-1",
            "engine_user_model": "CUSTOMER_SPECIFIC_PROFILE",
            "raw_text": "hi",
        },
        memory={"core": {"user": "PROCESS_LEVEL_USER"}},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction=_INSTR,
    )
    assert "CUSTOMER_SPECIFIC_PROFILE" in prompt
    assert "PROCESS_LEVEL_USER" not in prompt


def test_engine_falls_back_to_process_user_model_when_customer_empty():
    """A customer with no profile yet falls back to the process-level user.md."""
    f = _make_frontal_skeleton()
    prompt = f._build_drafter_prompt(
        features={"end_user_id": "cust-1", "raw_text": "hi"},  # no engine_user_model
        memory={"core": {"user": "PROCESS_LEVEL_USER"}},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction=_INSTR,
    )
    assert "PROCESS_LEVEL_USER" in prompt
