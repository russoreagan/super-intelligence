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

3. **People OR days, for an org whose traffic is its own.** Distinct-people
   corroboration cannot fire in an org where one person (the owner, often arriving as
   a single end user through their own app) produces nearly all the traffic: every
   lesson stays at support 1 forever. Such an org can choose
   organizations.lesson_corroboration = 'people_or_days' (brain/org_settings.py): a
   lesson still promotes on 3 distinct people, AND also once it has been re-derived on
   ``promote_days`` distinct UTC days, whoever it came from — a superset of 'people'. Days, not passes: a consolidation that replays the
   same batch several times in one day corroborates nothing. Each occasion is
   recorded as "<token>:<day>", so erasure still cascades through it.

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
_DEFAULT_PROMOTE_DAYS = 3

# How a lesson earns "established". PEOPLE: 3 distinct people only. PEOPLE_OR_DAYS:
# 3 distinct people OR 3 distinct days (a superset). "occasions" was the first name
# for PEOPLE_OR_DAYS and is still accepted wherever a mode is read.
PEOPLE = "people"
PEOPLE_OR_DAYS = "people_or_days"
OCCASIONS = PEOPLE_OR_DAYS  # former name, kept so older imports resolve
CORROBORATION_MODES = (PEOPLE, PEOPLE_OR_DAYS)
_MODE_ALIASES = {"occasions": PEOPLE_OR_DAYS}


def normalize_mode(mode: object) -> str:
    """The canonical corroboration mode for any accepted spelling; PEOPLE otherwise."""
    m = str(mode or "").strip().lower()
    m = _MODE_ALIASES.get(m, m)
    return m if m in CORROBORATION_MODES else PEOPLE


_PROVISIONAL = "provisional"
_ESTABLISHED = "established"

_DAY_FMT = "%Y-%m-%d"


def _day(ts: float) -> str:
    """The UTC calendar day of a timestamp — the unit an occasion is counted in."""
    return time.strftime(_DAY_FMT, time.gmtime(float(ts or 0.0)))


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
    # Persona slugs whose customers contributed. The store is org-wide (one file
    # per process, which can serve several personas); a persona's own practice
    # reads only its own lessons. Empty on legacy records.
    personas: set[str] = field(default_factory=set)
    # "<token>:<YYYY-MM-DD>" — the UTC days a source (re)derived this lesson. Read
    # only in occasions mode; carries the token so purge_source cascades into it.
    occasions: set[str] = field(default_factory=set)

    def learned_by(self, persona: str) -> bool:
        return bool(persona) and persona in self.personas

    @property
    def support(self) -> int:
        """Number of DISTINCT corroborating sources."""
        return len(self.contributors)

    @property
    def days(self) -> int:
        """Number of DISTINCT UTC days on which any source (re)derived it."""
        return len({o.rsplit(":", 1)[-1] for o in self.occasions})

    def status(
        self,
        promote_k: int,
        *,
        corroboration: str = PEOPLE,
        promote_days: int = _DEFAULT_PROMOTE_DAYS,
    ) -> str:
        if self.support >= promote_k:
            return _ESTABLISHED
        if normalize_mode(corroboration) == PEOPLE_OR_DAYS and self.days >= max(
            1, int(promote_days)
        ):
            return _ESTABLISHED
        return _PROVISIONAL

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
        corroboration: str = PEOPLE,
        promote_days: int = _DEFAULT_PROMOTE_DAYS,
    ) -> None:
        self._k = max(1, int(promote_k))
        # Not persisted: the org's declaration is read live (org_settings), so a
        # switch takes effect on the next load and is reversible.
        self._corroboration = normalize_mode(corroboration)
        self._days = max(1, int(promote_days))
        self._salt = salt
        self._now = now_fn
        self._by_id: dict[str, Hypothesis] = {}
        self._id_by_key: dict[str, str] = {}

    @property
    def promote_k(self) -> int:
        """Distinct-source count at which a hypothesis promotes to established."""
        return self._k

    @property
    def corroboration(self) -> str:
        """'people' (3 distinct people only) or 'people_or_days' (3 distinct people,
        or 3 distinct days)."""
        return self._corroboration

    @property
    def promote_days(self) -> int:
        return self._days

    def status_of(self, hyp: Hypothesis) -> str:
        """provisional | established under this store's corroboration rule."""
        return hyp.status(self._k, corroboration=self._corroboration, promote_days=self._days)

    def _token(self, source_id: str) -> str:
        """Opaque, stable pseudonym for a source — lets us count distinct sources
        and cascade deletion without storing the plaintext id in shared state."""
        return hashlib.sha256(f"{self._salt}:{source_id}".encode()).hexdigest()[:16]

    @staticmethod
    def _hyp_id(content_key: str) -> str:
        # Content-addressed id, not a security digest.
        return hashlib.sha1(content_key.encode(), usedforsecurity=False).hexdigest()[:12]

    def add(self, principle: str, source_id: str, persona: str = "") -> Hypothesis:
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
        hyp.occasions.add(f"{token}:{_day(now)}")
        if persona:
            hyp.personas.add(persona)
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
            hyp.occasions = {o for o in hyp.occasions if not o.startswith(token + ":")}
            if hyp.support == 0:
                self._by_id.pop(hid, None)
                self._id_by_key.pop(_content_key(hyp.principle), None)
                retired.append(hid)
        return retired

    def merge_source(self, from_id: str, to_id: str) -> list[str]:
        """Fold one source's contributions into another's: the same person reached
        the org under two ids. Every hypothesis ``from_id`` corroborated is
        re-credited to ``to_id`` (a hypothesis both backed now counts them once),
        and its occasions move with it. Returns the touched hypothesis ids."""
        old, new = self._token(from_id), self._token(to_id)
        if old == new:
            return []
        touched: list[str] = []
        for hyp in self._by_id.values():
            if old not in hyp.contributors:
                continue
            hyp.contributors.discard(old)
            hyp.contributors.add(new)
            hyp.occasions = {
                (new + o[len(old) :]) if o.startswith(old + ":") else o for o in hyp.occasions
            }
            touched.append(hyp.id)
        return touched

    def remove(self, hyp_id: str) -> bool:
        """Retire one hypothesis outright (an operator decision, not erasure)."""
        hyp = self._by_id.pop(hyp_id, None)
        if hyp is None:
            return False
        self._id_by_key.pop(_content_key(hyp.principle), None)
        return True

    def set_personas(self, hyp_id: str, personas: set[str]) -> Hypothesis | None:
        """Re-attribute a hypothesis to the personas that learned it. An empty set
        makes it unattributed (legacy semantics: shown to every persona)."""
        hyp = self._by_id.get(hyp_id)
        if hyp is not None:
            hyp.personas = set(personas)
        return hyp

    # ── reads ─────────────────────────────────────────────────────────────────

    def find_by_text(self, principle: str) -> Hypothesis | None:
        """The hypothesis whose content key matches ``principle`` exactly, if any."""
        hid = self._id_by_key.get(_content_key(principle))
        return self._by_id.get(hid) if hid else None

    def get(self, hyp_id: str) -> Hypothesis | None:
        return self._by_id.get(hyp_id)

    def all(self) -> list[Hypothesis]:
        return list(self._by_id.values())

    def established(self) -> list[Hypothesis]:
        return [h for h in self._by_id.values() if self.status_of(h) == _ESTABLISHED]

    def provisional(self) -> list[Hypothesis]:
        return [h for h in self._by_id.values() if self.status_of(h) == _PROVISIONAL]

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
                    "personas": sorted(h.personas),
                    "occasions": sorted(h.occasions),
                }
                for h in self._by_id.values()
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
        *,
        salt: str = "",
        now_fn=time.time,
        corroboration: str = PEOPLE,
        promote_days: int = _DEFAULT_PROMOTE_DAYS,
    ) -> HypothesisStore:
        store = cls(
            promote_k=int(data.get("promote_k", _DEFAULT_PROMOTE_K)),
            salt=salt,
            now_fn=now_fn,
            corroboration=corroboration,
            promote_days=promote_days,
        )
        for rec in data.get("hypotheses", []):
            hyp = Hypothesis(
                id=rec["id"],
                principle=rec["principle"],
                contributors=set(rec.get("contributors", [])),
                first_seen=rec.get("first_seen", 0.0),
                last_seen=rec.get("last_seen", 0.0),
                personas=set(rec.get("personas", [])),
                occasions=set(rec.get("occasions", [])),
            )
            if "occasions" not in rec and len(hyp.contributors) == 1:
                # Records written before occasions existed: with a single source the
                # first and last sighting are both that source's, so they are two
                # honest occasions. With several, which source came when is unknown
                # and nothing is guessed (people-mode support still counts them).
                (tok,) = hyp.contributors
                hyp.occasions = {
                    f"{tok}:{_day(ts)}" for ts in (hyp.first_seen, hyp.last_seen) if ts and ts > 0
                }
            store._by_id[hyp.id] = hyp
            store._id_by_key[_content_key(hyp.principle)] = hyp.id
        return store
