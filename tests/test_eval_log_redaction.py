"""The eval log redacts engine-lane text at write.

eval/turns.jsonl is process-wide and append-only, so a partner customer's verbatim
prompt and response written there could never be reached by right-to-erasure. No
eval reader needs the text from the FILE (scorer, emotion judge and baseline all
read the in-memory trace), so an engine-API turn (api_session_id set) is logged
with `sha256:<16>/<len>` digests instead — and so are its later patches (the
baseline response arrives via patch_turn). The owner lane is unchanged, and
BRAIN_EVAL_LOG_AGENT_TEXT=verbatim is the operator's opt-out.
"""

from __future__ import annotations

import json
import re

import pytest

from brain.observability.timeline import TurnTrace
from eval.turn_logger import EvalLogger, agent_text_policy, digest

_DIGEST = re.compile(r"^sha256:[0-9a-f]{16}/\d+$")


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_EVAL_LOG", raising=False)
    monkeypatch.delenv("BRAIN_EVAL_LOG_AGENT_TEXT", raising=False)
    return EvalLogger(tmp_path / "turns.jsonl"), tmp_path / "turns.jsonl"


def _lines(path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_engine_turn_text_is_digested(log):
    logger, path = log
    tr = TurnTrace(turn_id="t1", session_id="s", user_input="my secret", api_session_id="api-1")
    tr.response = "the answer"
    logger.log_turn(tr)
    rec = _lines(path)[0]
    assert _DIGEST.match(rec["user_input"]) and rec["user_input"].endswith("/9")
    assert _DIGEST.match(rec["response"])
    assert rec["text_redacted"] is True
    assert "my secret" not in path.read_text() and "the answer" not in path.read_text()
    assert rec["user_input"] == digest("my secret")  # stable, dedupable


def test_owner_turn_stays_verbatim(log):
    logger, path = log
    tr = TurnTrace(turn_id="t2", session_id="s", user_input="hello owner")
    tr.response = "hi"
    logger.log_turn(tr)
    rec = _lines(path)[0]
    assert rec["user_input"] == "hello owner" and rec["response"] == "hi"
    assert "text_redacted" not in rec


def test_patches_for_an_engine_turn_are_redacted_too(log):
    logger, path = log
    logger.log_turn(TurnTrace(turn_id="t1", session_id="s", user_input="x", api_session_id="a"))
    logger.log_turn(TurnTrace(turn_id="t2", session_id="s", user_input="y"))
    logger.patch_turn("t1", baseline_response="BASELINE TEXT", baseline_model="haiku")
    logger.patch_turn("t2", baseline_response="OWNER BASELINE", baseline_model="haiku")
    p1, p2 = _lines(path)[2:]
    assert _DIGEST.match(p1["baseline_response"]) and p1["baseline_model"] == "haiku"
    assert p2["baseline_response"] == "OWNER BASELINE"


def test_verbatim_escape_hatch(log, monkeypatch):
    monkeypatch.setenv("BRAIN_EVAL_LOG_AGENT_TEXT", "verbatim")
    assert agent_text_policy() == "verbatim"
    logger, path = log
    logger.log_turn(TurnTrace(turn_id="t1", session_id="s", user_input="x", api_session_id="a"))
    assert _lines(path)[0]["user_input"] == "x"


def test_policy_default_is_redacted():
    assert agent_text_policy() == "redacted_at_write"
    assert digest("") == ""
