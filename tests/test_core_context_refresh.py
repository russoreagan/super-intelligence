"""The hippocampus caches core context (self.md + user.md) per persona so the prompt
prefix stays byte-stable. A write to self.md — sleep consolidation learning something —
must invalidate that cache, or the new self-model never reaches a prompt until restart."""

from __future__ import annotations

from brain.clusters.hippocampus import HippocampusCluster
from brain.second_brain import store
from brain.second_brain.store import bind_persona


class _Schema:
    def __init__(self):
        self.self_md = {"": "v1-home", "the_sage": "v1-sage"}

    def load_core_context(self):
        return {"self": self.self_md[store.active_persona() or ""], "user": ""}


def _hip():
    h = HippocampusCluster.__new__(HippocampusCluster)
    h._schema = _Schema()
    h._core_context = h._schema.load_core_context()
    return h


def test_a_self_md_write_reaches_the_next_prompt():
    h = _hip()
    with bind_persona("the_sage"):
        assert h._active_core_context()["self"] == "v1-sage"
    assert h._active_core_context()["self"] == "v1-home"

    h._schema.self_md = {"": "v2-home", "the_sage": "v2-sage"}
    with bind_persona("the_sage"):
        assert h._active_core_context()["self"] == "v1-sage", "no write yet — cached"
    store._note_core_write("self.md")
    with bind_persona("the_sage"):
        # The bound refresh must not overwrite the process default with the sage.
        assert h._active_core_context()["self"] == "v2-sage"
    assert h._active_core_context()["self"] == "v2-home"


def test_ledger_writes_do_not_bust_the_prompt_cache():
    h = _hip()
    h._active_core_context()
    h._schema.self_md[""] = "v2-home"
    store._note_core_write("open_questions__x.md")
    assert h._active_core_context()["self"] == "v1-home"
