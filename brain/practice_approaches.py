"""The persona's own practice: self.md's "How I approach sessions".

Two writers, one section:

  • the idle mind (brain/dmn.py) adds lines — a practice thread that concludes
    lands here, settled when confident, "[trying]" when uncertain;
  • sleep (brain/sleep.py) judges them — after cross-learning has reflected on what
    worked with each person, review() weighs every line against the de-identified
    lessons in the hypothesis store and settles, narrows or drops it.

The self-model rides every turn, so this section is how what the persona works out
about its practice reaches its sessions. Everything here is de-identified: lines
are scrubbed before they are written, and the evidence review() reads is the
gate-admitted hypothesis store. Nothing names or describes a person.
"""

from __future__ import annotations

import logging
import re

from brain.settings import settings

logger = logging.getLogger(__name__)

SECTION = "How I approach sessions"
PREFACE = (
    "_What I've settled on from experience, and what I'm trying. My own practice: "
    "never attribute any of it to anyone. [trying] = still testing it._"
)
TRYING_MAX = 3
MERGE_OVERLAP = 0.6
_LINE_MAX = 240

_SECTION_RE = re.compile(
    r"(?ms)^##[ \t]+" + re.escape(SECTION) + r"[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)"
)

Entry = tuple[bool, str]  # (trying, text)


def enabled() -> bool:
    """Practice thinking runs only with cross_learning on, the practice seed on, and
    a consolidated org (fails closed on an unread org)."""
    try:
        if not settings.get("cross_learning", 0):
            return False
        if int(settings.get("dmn_practice_seed_every", 5) or 0) <= 0:
            return False
        from brain import org_settings

        return not org_settings.is_isolated()
    except Exception:
        return False


def cap() -> int:
    try:
        return max(1, int(settings.get("dmn_practice_approaches_max", 8) or 8))
    except (TypeError, ValueError):
        return 8


def clean_line(text: str) -> str:
    return " ".join((text or "").split())[:_LINE_MAX]


def parse(body: str) -> list[Entry]:
    """Section body → [(trying, text)] in order, skipping the preface."""
    out: list[Entry] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        text = line[2:].strip()
        trying = text.lower().startswith("[trying]")
        if trying:
            text = text[len("[trying]") :].strip()
        if text:
            out.append((trying, text))
    return out


def render(entries: list[Entry]) -> str:
    lines = [PREFACE]
    lines += [f"- [trying] {t}" if trying else f"- {t}" for trying, t in entries]
    return "\n".join(lines)


def read(self_md: str) -> list[Entry]:
    m = _SECTION_RE.search(self_md or "")
    return parse(m.group(1) if m else "")


def enforce_caps(entries: list[Entry], limit: int) -> list[Entry]:
    """At most min(TRYING_MAX, limit) [trying] lines and `limit` lines in all; the
    oldest of the over-full kind goes first."""
    entries = list(entries)
    while sum(1 for tr, _ in entries if tr) > min(TRYING_MAX, limit):
        entries.pop(next(i for i, (tr, _) in enumerate(entries) if tr))
    while len(entries) > limit:
        entries.pop(next((i for i, (tr, _) in enumerate(entries) if not tr), 0))
    return entries


def merge(entries: list[Entry], text: str, *, trying: bool, limit: int) -> list[Entry]:
    """Add one line. A near-copy of an existing line replaces it in place, and a
    settled line is never demoted back to [trying]."""
    from brain.dmn_dedup import _content_word_overlap as content_word_overlap

    entries = list(entries)
    for i, (was_trying, existing) in enumerate(entries):
        if content_word_overlap(existing, text) >= MERGE_OVERLAP:
            entries[i] = (was_trying and trying, text)
            break
    else:
        entries.append((trying, text))
    return enforce_caps(entries, limit)


async def write(schema, entries: list[Entry]) -> bool:
    """Replace the section body. False when there is no self-model to write into."""
    if not (schema.read("self.md") or ""):
        return False
    await schema.upsert_section("self.md", SECTION, render(entries))
    return True


async def scrub(router, text: str) -> str:
    """De-identify one line (the DeidGate passage scrub sleep uses). "" = refused
    or failed: the caller writes nothing (fail closed)."""
    try:
        from brain.deid_gate import DeidGate

        out = await DeidGate(router).scrub_passage(text, text)
    except Exception as e:
        logger.debug("[Practice] de-id failed: %s", e)
        return ""
    return clean_line(out or "")


# ── Sleep: judge each line against what the people served actually showed ──────

_REVIEW_SYS = (
    "You keep a short list of how you approach your sessions: lines you have settled "
    "on, and lines marked [trying] that you are still testing. You also have lessons "
    "drawn from your sessions with many people, each already stripped of anything "
    "identifying, with how many different people it was seen with. Judge each line "
    "of your practice against that evidence.\n"
    "- settle: a [trying] line the evidence supports, seen with several different "
    "people (one person is not enough).\n"
    "- revise: the evidence says it works only in some situations, or needs a "
    "different form; give the narrower or corrected line in `text`.\n"
    "- drop: the evidence says it falls flat, backfires, or a better approach "
    "replaced it.\n"
    "- keep: the evidence says nothing either way. This is the default; most lines "
    "stay as they are.\n"
    "Never add new lines, and never write about any particular person. Each line is "
    "one plain sentence of practice.\n"
    'Reply with JSON only: {"decisions": [{"n": <line number>, "action": '
    '"keep|settle|revise|drop", "text": "<revised line, for revise only>", '
    '"why": "<short>"}]}'
)


def _evidence(store, persona: str, limit: int = 15) -> list[str]:
    k = store.promote_k
    own = [h for h in store.all() if h.learned_by(persona)]
    ranked = sorted(own, key=lambda h: (h.support, h.last_seen), reverse=True)
    out = []
    for h in ranked[:limit]:
        n = h.support
        who = "seen with 1 person" if n == 1 else f"seen with {n} people"
        tag = "established" if n >= k else "tentative"
        out.append(f"- {h.principle} ({who}; {tag})")
    return out


async def review(router, schema, store, *, persona: str, model_key: str = "sonnet") -> dict:
    """One judgment call over the whole section, against this persona's own
    lessons (Hypothesis.personas). Returns a summary dict for logs
    and tests: {"reviewed": bool, "settled": n, "revised": n, "dropped": n}.
    Fail-safe: any error or unparseable answer leaves the section untouched."""
    summary = {"reviewed": False, "settled": 0, "revised": 0, "dropped": 0}
    current = schema.read("self.md") or ""
    entries = read(current)
    evidence = _evidence(store, persona)
    if not current or not entries or not evidence:
        return summary
    numbered = "\n".join(
        f"{i + 1}. {'[trying] ' if tr else ''}{t}" for i, (tr, t) in enumerate(entries)
    )
    prompt = f"Your practice:\n{numbered}\n\nLessons from the people you serve:\n" + "\n".join(
        evidence
    )
    try:
        raw = await router.call(
            model_key,
            _REVIEW_SYS,
            [{"role": "user", "content": prompt}],
            cluster="sleep",
            cell="approach_review",
        )
    except Exception as e:
        logger.warning("[Practice] review call failed: %s", e)
        return summary
    from brain.deid_gate import _parse_json

    parsed = _parse_json(raw or "")
    decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
    if not isinstance(decisions, list):
        return summary

    out: list[Entry | None] = list(entries)
    for d in decisions:
        if not isinstance(d, dict):
            continue
        try:
            i = int(d.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(entries) or out[i] is None:
            continue
        action = str(d.get("action") or "keep").strip().lower()
        trying, text = entries[i]
        if action == "drop":
            out[i] = None
            summary["dropped"] += 1
        elif action == "settle" and trying:
            out[i] = (False, text)
            summary["settled"] += 1
        elif action == "revise":
            revised = await scrub(router, clean_line(str(d.get("text") or "")))
            if revised:
                out[i] = (trying, revised)
                summary["revised"] += 1
    kept = enforce_caps([e for e in out if e is not None], cap())
    summary["reviewed"] = True
    if kept != entries:
        await schema.upsert_section("self.md", SECTION, render(kept))
        logger.info(
            "[Practice] review: %d settled, %d revised, %d dropped",
            summary["settled"],
            summary["revised"],
            summary["dropped"],
        )
    return summary
