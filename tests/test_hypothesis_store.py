"""
HypothesisStore — confidence dial, distinct-source promotion, deletion cascade,
and the no-plaintext-ids-in-shared-state guarantee.
"""

from __future__ import annotations

from brain.hypothesis_store import HypothesisStore

_P = "grief can surface at a normally-happy topic"


def _store(k=3):
    return HypothesisStore(promote_k=k, salt="s", now_fn=lambda: 1000.0)


def test_single_case_is_provisional():
    s = _store()
    h = s.add(_P, "alice")
    assert h.support == 1
    assert h.status(3) == "provisional"
    assert s.provisional() and not s.established()


def test_distinct_sources_promote_to_established():
    s = _store(k=3)
    s.add(_P, "alice")
    s.add(_P, "bob")
    h = s.add(_P, "carol")
    assert h.support == 3
    assert h.status(3) == "established"
    assert s.established() and not s.provisional()


def test_same_source_is_idempotent():
    s = _store()
    s.add(_P, "alice")
    h = s.add(_P, "alice")  # same source again
    assert h.support == 1  # one chatty customer can't self-promote


def test_content_key_dedups_paraphrase_of_form():
    s = _store()
    a = s.add("Grief can surface at a normally-happy topic.", "alice")
    b = s.add("grief can surface at a normally-happy topic", "bob")  # case/punct differ
    assert a.id == b.id
    assert b.support == 2


def test_distinct_principles_are_distinct_hypotheses():
    s = _store()
    a = s.add(_P, "alice")
    b = s.add("people mirror the energy you bring", "alice")
    assert a.id != b.id
    assert len(s.all()) == 2


def test_purge_retires_single_source_hypothesis():
    s = _store()
    h = s.add(_P, "alice")
    retired = s.purge_source("alice")
    assert h.id in retired
    assert s.get(h.id) is None


def test_purge_demotes_when_dropping_below_k():
    s = _store(k=3)
    s.add(_P, "alice")
    s.add(_P, "bob")
    h = s.add(_P, "carol")
    assert h.status(3) == "established"
    s.purge_source("carol")
    assert s.get(h.id).support == 2
    assert s.get(h.id).status(3) == "provisional"  # demoted, not retired


def test_no_plaintext_source_ids_in_shared_state():
    s = _store()
    s.add(_P, "sensitive-customer-id-12345")
    blob = s.to_dict()
    serialized = str(blob)
    assert "sensitive-customer-id-12345" not in serialized  # only opaque tokens stored


def test_to_dict_from_dict_roundtrip_preserves_support():
    s = _store(k=3)
    s.add(_P, "alice")
    s.add(_P, "bob")
    restored = HypothesisStore.from_dict(s.to_dict(), salt="s", now_fn=lambda: 1000.0)
    h = restored.all()[0]
    assert h.support == 2
    # a previously-counted source stays idempotent after reload
    restored.add(_P, "alice")
    assert restored.all()[0].support == 2
    # and a genuinely new source still promotes
    restored.add(_P, "carol")
    assert restored.all()[0].status(3) == "established"


def test_confidence_dial():
    s = _store(k=4)
    h = s.add(_P, "alice")
    assert h.confidence(4) == 0.25
    s.add(_P, "bob")
    assert h.confidence(4) == 0.5
