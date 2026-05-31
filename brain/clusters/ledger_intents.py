"""
Conversational intents that touch the open-threads ledger (B5), as pure
functions so they're cheap and fully unit-testable:

  - detect_manual_project: the user hands the brain a project / open thread
    ("work on X", "add this to your open threads", "new project: X").
  - classify_confirmation: the user is answering a surfaced *uncertain
    conclusion* — affirm / reject / correct.

Detection is deliberately conservative: a clear lead phrase is required so a
casual mention doesn't get logged as a project, and the caller confirms the
write in its reply so the user can catch a misfire.
"""

from __future__ import annotations

import re

# Explicit project-assignment lead phrases. The captured group is the project body.
_PROJECT_PATTERNS = [
    re.compile(r"\badd (?:this|that|it) to your open threads\b[:,]?\s*(.*)", re.I),
    re.compile(r"\badd (?:a |an )?(?:new )?(?:project|thread|open thread)\b[:,-]?\s*(.+)", re.I),
    re.compile(r"\bnew project\b[:,-]?\s*(.+)", re.I),
    # "work on X" only as an imperative directed at the assistant — at the start
    # of the message or after you/please/let's/can you — so "I did some work on my
    # car" (work as a noun) doesn't match.
    re.compile(
        r"(?:^|\b(?:you|please|let'?s|can you|could you|would you)\s+)"
        r"(?:start |begin )?work(?:ing)? on\b[:,-]?\s*(.+)",
        re.I,
    ),
    re.compile(r"\bi(?:'d| would) like you to (?:work on|take on|look into)\b\s*(.+)", re.I),
    re.compile(r"\bi want you to (?:work on|take on|start|build|investigate|review)\b\s*(.+)", re.I),
]

_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|correct|exactly|right|agreed?|that'?s right|spot on|"
    r"makes sense|i agree|confirmed?)\b",
    re.I,
)
_REJECT = re.compile(
    r"\b(no|nope|not really|not quite|that'?s wrong|incorrect|disagree|"
    r"i don'?t think so|that'?s not right)\b",
    re.I,
)


def detect_manual_project(user_input: str) -> dict | None:
    """Return {"title", "task"} if the user is assigning a project, else None."""
    if not user_input or not user_input.strip():
        return None
    text = user_input.strip()
    for pat in _PROJECT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        body = (m.group(1) or "").strip().rstrip(".")
        if len(body) < 3:
            continue
        # Title = a short slug from the first clause; task = the full body.
        title = re.split(r"[.\n]|(?:\s+(?:so that|because|which)\b)", body, maxsplit=1)[0].strip()
        title = title[:60] or body[:60]
        return {"title": title, "task": body}
    return None


def classify_confirmation(user_input: str) -> str:
    """Classify a reply to a surfaced uncertain conclusion.

    Returns "affirm" | "reject" | "correct". Only call this when a pending
    conclusion is actually awaiting an answer — otherwise it over-reads ordinary
    replies. A reject/affirm keyword wins; anything else substantive is a
    correction.
    """
    if not user_input or not user_input.strip():
        return "correct"
    # Check reject first: "no, that's actually …" should not match the affirm
    # word inside it.
    if _REJECT.search(user_input):
        return "reject"
    if _AFFIRM.search(user_input):
        return "affirm"
    return "correct"
