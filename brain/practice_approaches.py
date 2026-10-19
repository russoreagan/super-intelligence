"""The persona's own practice: self.md's "How I approach sessions".

Two writers, one section:

  • the idle mind (brain/dmn.py) adds lines — a practice thread that concludes
    lands here, settled when confident, "[trying]" when uncertain, but only when
    grounding() finds a lesson from real sessions that the line puts to work;
  • sleep (brain/sleep.py) judges them — after cross-learning has reflected on what
    worked with each person, review() weighs every line against the de-identified
    lessons in the hypothesis store and settles, narrows or drops it. A line no
    lesson supports decays: settled → [trying] → gone.

Grounding exists because a practice thread is any thread opened while a lesson was
in mind, and idle thinking often wanders off the lesson (to its own quiet period,
say). Without it, those musings concluded "confident" straight into a settled line,
and review() defaulted to keeping anything the evidence did not mention.

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
_EVIDENCE_MAX = 40
_LINE_MAX = 240

_SECTION_RE = re.compile(
    r"(?ms)^##[ \t]+" + re.escape(SECTION) + r"[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)"
)

Entry = tuple[bool, str]  # (trying, text)


def enabled() -> bool:
    """Practice thinking runs only with cross_learning on and the practice seed on,
    in a consolidated org, or in an isolated one with per-persona private learning
    (each persona's practice rests on its own private lessons). Fails closed on an
    unread org."""
    try:
        if not settings.get("cross_learning", 0):
            return False
        if int(settings.get("dmn_practice_seed_every", 5) or 0) <= 0:
            return False
        from brain import cross_learning, org_settings

        if not org_settings.is_isolated():
            return True
        return org_settings.is_isolated_known() and cross_learning.private_learning_on()
    except Exception:
        return False


def grounding_on() -> bool:
    """Kill switch for grounding: the admission check in grounding() and review()'s
    decay of lines no lesson supports. 0 = the earlier behavior (every concluded
    practice thread lands; review keeps what the evidence does not mention)."""
    try:
        return bool(int(settings.get("practice_grounding", 1) or 0))
    except (TypeError, ValueError):
        return True


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
    "- settle: a [trying] line the evidence supports, established or seen with several "
    "different people (one person once is not enough).\n"
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


_REVIEW_GROUNDED_SYS = (
    "You keep a short list of how you approach your sessions: lines you have settled "
    "on, and lines marked [trying] that you are still testing. You also have numbered "
    "lessons drawn from your real sessions with many people, each already stripped of "
    "anything identifying, with how many different people it was seen with. A line "
    "earns its place only by putting one of those lessons to work. Judge every line.\n"
    "- basis: the number of the lesson the line puts to work (applies it, narrows it, "
    "or says when or how to use it), or 0 when none does. A reflection on your own "
    "idle or quiet time, your systems or your state of mind is not practice: basis 0.\n"
    "- settle: a [trying] line whose basis lesson is established or was seen with "
    "several different people (one person once is not enough).\n"
    "- revise: the evidence says it works only in some situations, or needs a "
    "different form; give the narrower or corrected line in `text`.\n"
    "- drop: the evidence says it falls flat, backfires, or a better approach "
    "replaced it.\n"
    "- keep: it puts a lesson to work and the evidence does not call for a change.\n"
    "Never add new lines, and never write about any particular person. Each line is "
    "one plain sentence of practice.\n"
    'Reply with JSON only: {"decisions": [{"n": <line number>, "basis": <lesson '
    'number or 0>, "action": "keep|settle|revise|drop", "text": "<revised line, for '
    'revise only>", "why": "<short>"}]}'
)


def _lessons(store, persona: str, limit: int = _EVIDENCE_MAX) -> list:
    """This persona's own lessons, strongest support first."""
    own = [h for h in store.all() if h.learned_by(persona)]
    return sorted(own, key=lambda h: (h.support, h.days, h.last_seen), reverse=True)[:limit]


def _evidence(store, lessons: list) -> list[str]:
    occasions = getattr(store, "corroboration", "people") == "occasions"
    out = []
    for i, h in enumerate(lessons):
        n = h.support
        who = "seen with 1 person" if n == 1 else f"seen with {n} people"
        if occasions:
            d = h.days
            who += ", on 1 day" if d == 1 else f", on {d} days"
        tag = "established" if store.status_of(h) == "established" else "tentative"
        out.append(f"L{i + 1}. {h.principle} ({who}; {tag})")
    return out


# ── Admission: a line must put a lesson from real sessions to work ─────────────

_GROUND_SYS = (
    "You are checking one line someone wants to add to the list of how they approach "
    "their sessions with people. Below it are numbered lessons drawn from their real "
    "sessions. The line belongs on the list only if it is something to do (or avoid) "
    "in a session and it puts one of those lessons to work: applies it, narrows it, "
    "or says when or how to use it. A reflection on their own idle or quiet time, "
    "their systems, or their state of mind is not practice, and neither is a line no "
    "lesson speaks to.\n"
    'Reply with JSON only: {"basis": <number of the lesson it puts to work, or 0>}'
)


async def grounding(router, text: str, store, *, persona: str, model_key: str = "haiku") -> str:
    """The lesson `text` puts to work, or "" when none does. Fail closed: no
    lessons for this persona, no router, a failed call or an unparseable answer all
    return "" (the line is not admitted)."""
    lessons = _lessons(store, persona) if store is not None else []
    if router is None or not lessons or not clean_line(text):
        return ""
    listing = "\n".join(f"{i + 1}. {h.principle}" for i, h in enumerate(lessons))
    try:
        raw = await router.call(
            model_key,
            _GROUND_SYS,
            [{"role": "user", "content": f"Line: {clean_line(text)}\n\nLessons:\n{listing}"}],
            cluster="dmn",
            cell="approach_grounding",
        )
    except Exception as e:
        logger.debug("[Practice] grounding call failed: %s", e)
        return ""
    from brain.deid_gate import _parse_json

    parsed = _parse_json(raw or "")
    try:
        n = int(parsed.get("basis") or 0) if isinstance(parsed, dict) else 0
    except (TypeError, ValueError):
        return ""
    return lessons[n - 1].principle if 1 <= n <= len(lessons) else ""


async def review(router, schema, store, *, persona: str, model_key: str = "sonnet") -> dict:
    """One judgment call over the whole section, against this persona's own
    lessons (Hypothesis.personas). Returns a summary dict for logs and tests:
    {"reviewed": bool, "settled": n, "revised": n, "dropped": n, "demoted": n}.

    With grounding on (`practice_grounding`), every line must name the lesson it
    puts to work. One that names none decays: a settled line goes back to
    [trying], a [trying] line is dropped, so filler is gone within two reviews and
    a real line that lost its footing gets one more round to find it. Settling
    needs a basis lesson seen with more than one person. A line the answer does not
    mention is left alone (a truncated answer must not wipe the section).
    Fail-safe: any error or unparseable answer leaves the section untouched."""
    summary = {"reviewed": False, "settled": 0, "revised": 0, "dropped": 0, "demoted": 0}
    grounded = grounding_on()
    current = schema.read("self.md") or ""
    entries = read(current)
    lessons = _lessons(store, persona, _EVIDENCE_MAX if grounded else 15)
    if not current or not entries or not lessons:
        return summary
    numbered = "\n".join(
        f"{i + 1}. {'[trying] ' if tr else ''}{t}" for i, (tr, t) in enumerate(entries)
    )
    prompt = f"Your practice:\n{numbered}\n\nLessons from the people you serve:\n" + "\n".join(
        _evidence(store, lessons)
    )
    try:
        raw = await router.call(
            model_key,
            _REVIEW_GROUNDED_SYS if grounded else _REVIEW_SYS,
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
    demoted: set[int] = set()
    decided: set[int] = set()
    for d in decisions:
        if not isinstance(d, dict):
            continue
        try:
            i = int(d.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(entries) or i in decided:
            continue
        decided.add(i)
        action = str(d.get("action") or "keep").strip().lower()
        trying, text = entries[i]
        try:
            b = int(d.get("basis") or 0)
        except (TypeError, ValueError):
            b = 0
        basis = lessons[b - 1] if 1 <= b <= len(lessons) else None
        if action == "drop":
            out[i] = None
            summary["dropped"] += 1
        elif grounded and basis is None:
            if trying:
                out[i] = None
                summary["dropped"] += 1
            else:
                out[i] = (True, text)
                demoted.add(i)
                summary["demoted"] += 1
        elif action == "settle" and trying:
            # Several people, or established under the org's rule (an org whose
            # traffic is its own establishes on distinct days, never on people).
            if not grounded or basis.support > 1 or store.status_of(basis) == "established":
                out[i] = (False, text)
                summary["settled"] += 1
        elif action == "revise":
            revised = await scrub(router, clean_line(str(d.get("text") or "")))
            if revised:
                out[i] = (trying, revised)
                summary["revised"] += 1
    # Demoted lines are the first to go when [trying] overflows: they just lost
    # their footing, the others are being tested on purpose.
    trying_cap = min(TRYING_MAX, cap())
    while sum(1 for e in out if e is not None and e[0]) > trying_cap and demoted:
        i = min(demoted)
        demoted.discard(i)
        out[i] = None
        summary["demoted"] -= 1
        summary["dropped"] += 1
    kept = enforce_caps([e for e in out if e is not None], cap())
    summary["reviewed"] = True
    if kept != entries:
        await schema.upsert_section("self.md", SECTION, render(kept))
        logger.info(
            "[Practice] review: %d settled, %d revised, %d demoted, %d dropped",
            summary["settled"],
            summary["revised"],
            summary["demoted"],
            summary["dropped"],
        )
    return summary
