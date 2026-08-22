"""
Tripwires for the DMN monologue prompt.

These exist because the same bug has now shipped twice. On 2026-05-27 (23f9dae) the
monologue prompt overflowed its context window; the fix raised num_ctx and recorded
"Worst-case token budget: 8559 / 10240 (1681 headroom)" in the commit message. Four
days later an unrelated RunPod fix (46d7a2c) set num_ctx to 8192, silently invalidating
that budget. Twelve weeks of blocks accreting later the prompt reached 38-48 KB against
a ~25 KB window, ollama truncated it from the front, and the model returned improvised
JSON — {"task","conclusion","reasoning","recommendations"} and friends — so the idle
loop produced no thoughts at all and queued no work.

The lesson is that a budget which lives only in a commit message is not a budget. These
tests put it somewhere that fails on the pull request which would break it again.

Every budget assertion here pins the RunPod window explicitly: the local variant gets
num_ctx=16384 (model_router.py), so a test that reads the local number would pass
vacuously against the deployment that actually breaks.
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

import brain.open_threads as ot
from brain.dmn import DefaultModeNetwork
from brain.dmn_prompts import MONOLOGUE_SCHEMA, MONOLOGUE_SYSTEM
from brain.sequence_predictor import SequencePredictor

# ── Budget, derived not guessed ─────────────────────────────────────────────
# The RunPod DMN cells run at num_ctx=8192 (brain/model_router.py, `local` bucket,
# is_runpod branch) and the monologue reserves num_predict=512 for its answer, which
# comes out of the same window. English prose + JSON + markdown on this model runs
# ~3.5 chars/token; we use that conservatively to convert.
RUNPOD_NUM_CTX = 8192
MONOLOGUE_NUM_PREDICT = 512
CHARS_PER_TOKEN = 3.5
PROMPT_BUDGET_CHARS = int((RUNPOD_NUM_CTX - MONOLOGUE_NUM_PREDICT) * CHARS_PER_TOKEN)

# The part of the budget spent before a single tick-specific character is added: the
# system prompt plus anything statically injected into it.
#
# This is a RATCHET, not an aspiration. It is set just above today's actual floor so it
# fails on the next change that grows the static cost — which is the only moment a
# tripwire is useful. Tighten it whenever the floor comes down (condensing the guidance
# body is the outstanding lever); never raise it to make a red test go green without
# first establishing what grew and why.
MONOLOGUE_STATIC_BUDGET_CHARS = 17_000


def _make_dmn():
    """Minimal DMN suitable for prompt assembly — mirrors tests/test_dmn_resilience.py."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._seq_predictor = SequencePredictor()
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._bus.neuromod = MagicMock()
    dmn._bus.neuromod.snapshot = MagicMock(return_value={})
    dmn._bus.neuromod.add = MagicMock()
    dmn._bus.hormonal = MagicMock()
    dmn._bus.hormonal.snapshot = MagicMock(return_value={})
    dmn._bus.hormonal.add = MagicMock()
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    dmn._hippocampus = None
    dmn._parietal = None
    dmn._obs = None
    dmn._running = True
    dmn._last_context = "Recent: hello world"
    dmn._thought_count = 0
    dmn._recent_thoughts = deque(maxlen=10)
    dmn._recent_angles = deque(maxlen=8)
    dmn._suppressed_count = 0
    dmn._session_id = "test"
    dmn._last_emotion = "neutral"
    dmn._last_speaker_name = None
    dmn._last_affection_score = 0
    dmn._last_familiarity = "new"
    dmn._last_projects = ""
    dmn._session_thought_buf = []
    dmn._session_thought_limit = 200
    dmn._candidate_q = deque(maxlen=8)
    dmn._self_task_q = deque(maxlen=4)
    dmn._skill_selector = None
    dmn._monologue_cell = MagicMock()
    dmn._monologue_cell.reset_turn = MagicMock()
    dmn._monologue_cell.call = AsyncMock(return_value='{"thought": "ok"}')
    dmn._simulation_cell = MagicMock()
    dmn._simulation_cell.reset_turn = MagicMock()
    dmn._simulation_cell.call = AsyncMock(return_value="{}")
    dmn.predicted_next = None
    dmn.last_was_question = False
    dmn.last_assistant_message = ""
    dmn.anticipations = []
    dmn.prefetched = []
    dmn._ensure_runtime_state()
    dmn._tick_idle_s = dmn._effective_idle_seconds()
    dmn._tick_idle_phase = dmn._idle_phase(dmn._tick_idle_s)
    return dmn


def _captured_user_message(dmn) -> str:
    """Run the monologue and return the user message it actually sent."""
    asyncio.run(dmn._run_monologue("t1", {}))
    (messages,), _kwargs = dmn._monologue_cell.call.call_args
    return messages[0]["content"]


# ── The response contract must be read last ─────────────────────────────────


def test_schema_is_last_thing_the_model_reads():
    """The response contract goes at the tail of the user message, nowhere else.

    Two independent reasons, both learned the hard way. Ollama truncates an over-long
    prompt from the FRONT, so tail content survives any overflow — the schema went from
    first casualty to last. And the router appends loaded skill markdown AFTER the
    system prompt, so a contract carried there is followed by procedure docs that end in
    their own output formats, which the model copies.

    If this fails because someone appended a new block to prompt_parts: move it above
    the schema append, don't move the schema.
    """
    dmn = _make_dmn()
    content = _captured_user_message(dmn)
    assert content.rstrip().endswith("}")
    assert content.rstrip().endswith(MONOLOGUE_SCHEMA.rstrip())


def test_schema_is_not_in_the_system_prompt():
    """The guidance and the contract are separate constants and must stay separate."""
    assert "Return JSON only:" not in MONOLOGUE_SYSTEM
    assert "Return JSON only:" in MONOLOGUE_SCHEMA
    assert '"thought"' in MONOLOGUE_SCHEMA


# ── Size ────────────────────────────────────────────────────────────────────


def test_monologue_static_floor_fits_budget():
    """What the prompt costs before the tick starts must leave room for the tick.

    This is the assertion that 23f9dae's commit-message budget should have been.
    """
    static = len(MONOLOGUE_SYSTEM) + len(MONOLOGUE_SCHEMA)
    assert static <= MONOLOGUE_STATIC_BUDGET_CHARS, (
        f"monologue static floor is {static} chars, over the "
        f"{MONOLOGUE_STATIC_BUDGET_CHARS} budget. Raising the constant is a decision, "
        "not a fix — check what grew first."
    )


# ── No procedure docs on the monologue cell ─────────────────────────────────


def test_monologue_cell_injects_no_static_skills():
    """The monologue carries no baseline skill documents.

    It used to carry logic-check + emotional: 6179 chars of Claude-Code procedure docs
    that end in their own output contracts ("## Output Format: Premises / Inference /
    Conclusion", "**Output:** Per-stakeholder map: …"). The router appends them after
    the system prompt, so they were the last thing the model read before generating,
    and it copied them instead of the schema. Skill-as-lens still works where it was
    designed to — rumination runs on its own cell, and _apply_monologue_skills still
    layers a relevant skill on high-drive idle ticks.
    """
    dmn = _make_dmn()
    dmn._judge_cell = MagicMock()
    selector = MagicMock()
    selector.tier1_names = [
        "logic-check",
        "communication-clarity-audit",
        "ethics-bias-check",
        "emotional",
    ]
    dmn.set_skill_selector(selector)
    assert dmn._monologue_cell.skills == []
    assert dmn._monologue_baseline_skills == []
    # The judge still gets the full set — it evaluates spoken candidates.
    assert len(dmn._judge_cell.skills) == 4


def test_low_drive_tick_resets_to_the_empty_baseline():
    """Per-tick skill variation must fall back to no skills, not to the old pair."""
    dmn = _make_dmn()
    dmn._judge_cell = MagicMock()
    selector = MagicMock()
    selector.tier1_names = ["logic-check", "emotional"]
    dmn.set_skill_selector(selector)
    dmn._monologue_cell.skills = ["logic-check", "emotional"]  # simulate a prior tick
    dmn._skill_selector = None  # no selector → reset path only
    asyncio.run(dmn._apply_monologue_skills("t1", {}, 0.0))
    assert dmn._monologue_cell.skills == []


# ── The shared context blob is decomposed ───────────────────────────────────


def test_frameworks_catalog_never_reaches_the_shared_context():
    """The reasoning-tool list is monologue-only.

    It used to be concatenated into the shared context string, so simulation,
    prefetcher and anticipator — none of which are told to use a framework — each paid
    4026 chars for it on every tick, at the same 8192-token window.
    """
    dmn = _make_dmn()
    dmn._conversation_text = "User: hello\nBrain: hi"
    dmn._last_self_schema = "I am a test persona."
    assert "aesthetic-coherence-check" not in dmn._last_context
    assert "Thinking frameworks" not in dmn._last_context


def test_conversation_snippet_trims_at_a_turn_boundary():
    """Never cut inside a turn — half a sentence attributed to the wrong speaker is
    worse than one fewer turn."""
    dmn = _make_dmn()
    dmn._conversation_text = "\n".join(f"User: message number {i} " + "x" * 80 for i in range(10))
    out = dmn.conversation_snippet(300)
    assert len(out) <= 300
    # Every surviving line is a whole line from the original, and the NEWEST is kept.
    original = dmn._conversation_text.splitlines()
    assert all(line in original for line in out.splitlines())
    assert out.splitlines()[-1] == original[-1]


def test_conversation_digest_is_the_last_user_line():
    dmn = _make_dmn()
    dmn._conversation_text = "User: first thing\nBrain: a reply\nUser: the latest thing"
    assert dmn.conversation_digest() == "User: the latest thing"


# ── Worst case, the combination nobody hand-computes ────────────────────────


def _fill_every_block(dmn):
    """Populate every optional block at its documented cap with realistic filler.

    The bug shipped because each block was individually reasonable and the UNION was
    never exercised — no test, and no person, ever added them all up.
    """
    dmn._conversation_text = "\n".join(f"User: turn {i} " + "word " * 40 for i in range(12))
    dmn._last_self_schema = "self-model line. " * 500  # 8000+, gets capped at read time
    dmn._last_projects = "\n".join(f"- **project {i}**: " + "t" * 120 for i in range(8))
    dmn._recent_thoughts.extend("a prior thought. " * 60 for _ in range(10))
    dmn._recent_angles.extend(f"angle-number-{i}" for i in range(8))
    dmn._recent_conclusions.extend((__import__("time").time(), "c" * 200) for _ in range(5))
    dmn._open_threads = [
        ot.Thread(id=f"t{i}", summary="s" * 200, progress=["p" * 200], status=ot.STATUS_OPEN)
        for i in range(6)
    ]
    dmn._memory_seed = "A memory surfaced: " + "m" * 500
    dmn._event_seed = 'Your self-directed job "x" just failed. Result: ' + "r" * 700
    dmn._sources_fn = lambda: [
        {"goal": "g" * 120, "summary": "s" * 140, "urls": [f"https://ex{i}.com/a"]}
        for i in range(12)
    ]
    selector = MagicMock()
    selector.capability_manifest = MagicMock(
        return_value="Operational capabilities:\n"
        + "\n".join(f"  tool-{i}: " + "d" * 140 for i in range(24))
    )
    dmn._skill_selector = selector
    return dmn


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, tracked deliberately. Stages 1-3 took the worst case from ~48000 to "
        "~29500 chars and the TYPICAL tick now fits with room (production reports zero "
        "context-window warnings). The remaining ~2600 can only come from "
        "MONOLOGUE_SYSTEM, which at 15023 chars is more than half the budget on its own "
        "— and that text is the entity's character, so condensing it is a deliberate, "
        "separately-measured change, not something to do to turn a test green. Remove "
        "this marker when it lands; strict=True means the test fails if it starts "
        "passing, so the marker cannot outlive the gap."
    ),
)
def test_worst_case_prompt_fits_context_window():
    """Every optional block populated at once must still fit.

    This is the test whose absence let the bug ship: 38-48KB against a ~25KB window,
    truncated from the front on every tick. It asserts the real budget, not the current
    number — a tripwire you move to match reality is not a tripwire.
    """
    dmn = _fill_every_block(_make_dmn())
    user = _captured_user_message(dmn)
    total = len(MONOLOGUE_SYSTEM) + len(user)
    assert total <= PROMPT_BUDGET_CHARS, (
        f"worst-case monologue prompt is {total} chars "
        f"(system {len(MONOLOGUE_SYSTEM)} + user {len(user)}), over the "
        f"{PROMPT_BUDGET_CHARS} budget for num_ctx={RUNPOD_NUM_CTX}. Ollama truncates "
        "from the FRONT — do not fix this by raising the window; it has been fixed that "
        "way twice and come back both times."
    )


def test_worst_case_still_ends_with_the_schema():
    """Under maximum pressure the contract is still the last thing read."""
    dmn = _fill_every_block(_make_dmn())
    assert _captured_user_message(dmn).rstrip().endswith(MONOLOGUE_SCHEMA.rstrip())
