"""
Shared hypothesis store — the de-identified pattern store cross-learning writes to.

This is the SHARED side of the privacy design (see
reports/per_client_chemistry_design.md). Only gate-admitted, de-identified
principles land here. Two ideas it implements:

1. **Confidence is a dial, not a gate.** A novel single case enters as a
   *provisional* hypothesis (often the richest signal — the anomaly). As DISTINCT
   customers corroborate the same principle, it promotes provisional → established
   at a k-threshold (k=3 by default — the same "≥3 distinct sources" heritage as
   motor-chunk promotion). Nothing is discarded for being singular.

2. **Distinctness without storing identities.** Corroboration must count DISTINCT
   sources (so one chatty customer can't self-promote a hypothesis), but the shared
   store must not hold plaintext customer ids. So each contributor is recorded as an
   opaque salted-hash TOKEN. Distinct-count and deletion-cascade both work on tokens;
   the plaintext id never enters shared state. The case→hypothesis pointer the design
   keeps silo-side is just the list of hypothesis ids add() returns to the caller.

Deletion / erasure: purge_source(id) removes that source's token from every
hypothesis it touched; a hypothesis with no remaining contributors is retired, and
one that falls below k naturally demotes to provisional — all derived from the token
set, so "delete the silo" stays provably complete.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

_DEFAULT_PROMOTE_K = 3

_PROVISIONAL = "provisional"
_ESTABLISHED = "established"

_norm_re = re.compile(r"[^a-z0-9 ]+")
_ws_re = re.compile(r"\s+")


def _content_key(principle: str) -> str:
    """Normalize a principle to a dedup key: lowercased, punctuation stripped,
    whitespace collapsed. Paraphrase-insensitive dedup (semantic/embedding dedup is
    a future enhancement — slot a key_fn in)."""
    s = _norm_re.sub(" ", (principle or "").lower())
    return _ws_re.sub(" ", s).strip()


@dataclass
class Hypothesis:
    id: str
    principle: str
    contributors: set[str] = field(default_factory=set)  # opaque salted-hash tokens
    first_seen: float = 0.0
    last_seen: float = 0.0

    @property
    def support(self) -> int:
        """Number of DISTINCT corroborating sources."""
        return len(self.contributors)

    def status(self, promote_k: int) -> str:
        return _ESTABLISHED if self.support >= promote_k else _PROVISIONAL

    def confidence(self, promote_k: int) -> float:
        """0→1 confidence: support relative to the promotion threshold."""
        return min(1.0, self.support / max(1, promote_k))


class HypothesisStore:
    def __init__(
        self,
        *,
        promote_k: int = _DEFAULT_PROMOTE_K,
        salt: str = "",
        now_fn=time.time,
    ) -> None:
        self._k = max(1, int(promote_k))
        self._salt = salt
        self._now = now_fn
        self._by_id: dict[str, Hypothesis] = {}
        self._id_by_key: dict[str, str] = {}

    @property
    def promote_k(self) -> int:
        """Distinct-source count at which a hypothesis promotes to established."""
        return self._k

    def _token(self, source_id: str) -> str:
        """Opaque, stable pseudonym for a source — lets us count distinct sources
        and cascade deletion without storing the plaintext id in shared state."""
        return hashlib.sha256(f"{self._salt}:{source_id}".encode()).hexdigest()[:16]

    @staticmethod
    def _hyp_id(content_key: str) -> str:
        # Content-addressed id, not a security digest.
        return hashlib.sha1(content_key.encode(), usedforsecurity=False).hexdigest()[:12]

    def add(self, principle: str, source_id: str) -> Hypothesis:
        """Record a gate-admitted principle, corroborated by ``source_id``. Same
        principle (by content key) from a NEW source promotes it; from a source
        already counted, it's idempotent. Returns the hypothesis — its ``id`` is the
        pointer the caller stores silo-side for later deletion cascade."""
        key = _content_key(principle)
        token = self._token(source_id)
        now = self._now()
        hid = self._id_by_key.get(key)
        if hid is None:
            hid = self._hyp_id(key)
            self._by_id[hid] = Hypothesis(
                id=hid, principle=principle.strip(), first_seen=now, last_seen=now
            )
            self._id_by_key[key] = hid
        hyp = self._by_id[hid]
        hyp.contributors.add(token)
        hyp.last_seen = now
        return hyp

    def purge_source(self, source_id: str, hyp_ids: list[str] | None = None) -> list[str]:
        """Remove a source's contribution (deletion cascade). ``hyp_ids`` is the
        silo-side pointer list (the hypotheses this source touched); if None, scan
        all. Retires any hypothesis left with no contributors. Returns retired ids."""
        token = self._token(source_id)
        targets = hyp_ids if hyp_ids is not None else list(self._by_id.keys())
        retired: list[str] = []
        for hid in targets:
            hyp = self._by_id.get(hid)
            if hyp is None:
                continue
            hyp.contributors.discard(token)
            if hyp.support == 0:
                self._by_id.pop(hid, None)
                self._id_by_key.pop(_content_key(hyp.principle), None)
                retired.append(hid)
        return retired

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, hyp_id: str) -> Hypothesis | None:
        return self._by_id.get(hyp_id)

    def all(self) -> list[Hypothesis]:
        return list(self._by_id.values())

    def established(self) -> list[Hypothesis]:
        return [h for h in self._by_id.values() if h.status(self._k) == _ESTABLISHED]

    def provisional(self) -> list[Hypothesis]:
        return [h for h in self._by_id.values() if h.status(self._k) == _PROVISIONAL]

    # ── persistence ─────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "promote_k": self._k,
            "hypotheses": [
                {
                    "id": h.id,
                    "principle": h.principle,
                    "contributors": sorted(h.contributors),
                    "first_seen": h.first_seen,
                    "last_seen": h.last_seen,
                }
                for h in self._by_id.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict, *, salt: str = "", now_fn=time.time) -> HypothesisStore:
        store = cls(
            promote_k=int(data.get("promote_k", _DEFAULT_PROMOTE_K)), salt=salt, now_fn=now_fn
        )
        for rec in data.get("hypotheses", []):
            hyp = Hypothesis(
                id=rec["id"],
                principle=rec["principle"],
                contributors=set(rec.get("contributors", [])),
                first_seen=rec.get("first_seen", 0.0),
                last_seen=rec.get("last_seen", 0.0),
            )
            store._by_id[hyp.id] = hyp
            store._id_by_key[_content_key(hyp.principle)] = hyp.id
        return store
