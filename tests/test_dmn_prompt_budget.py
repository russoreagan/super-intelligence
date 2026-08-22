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
