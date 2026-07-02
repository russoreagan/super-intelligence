"""
Thought-dedup machinery for the Default Mode Network (brain/dmn.py).

The DMN's idle monologue must not resurface the same idea over and over. This
module holds the PURE parts of that novelty defense — the similarity functions
and the tuning constants — while the stateful gate itself (which owns the
recent-thought/embedding/angle/frame deques) stays on DefaultModeNetwork in
brain/dmn.py:

- `_content_word_overlap`: Jaccard overlap on content words (stop-word
  filtered) — the cheap textual pre-filter.
- `_frame_signature`: coarse "shape" of a thought's opening clause, catching
  template collapse ("I should INQUIRE ...") that word/semantic gates miss.
- `_cosine`: cosine similarity over embedding vectors — the semantic gate.
- `DMN_*` constants: window sizes and thresholds for the dedup memory.

Everything here is pure (no DMN state); brain/dmn.py imports and re-exports
these names so existing importers of brain.dmn keep working.
"""

from __future__ import annotations

import os
import re

# English function/stop words — filtered out before Jaccard overlap so that
# common scaffolding ("the user has been...") doesn't make every thought look
# like a duplicate. This is DIFFERENT from voice_bridge.bleed_overlap, which
# is tuned for TTS-bleed detection (needs to catch articles).
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "must",
        "shall",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "up",
        "out",
        "as",
        "into",
        "through",
        "after",
        "before",
        "between",
        "during",
        "under",
        "over",
        "about",
        "against",
        "without",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "itself",
        "he",
        "she",
        "they",
        "them",
        "their",
        "theirs",
        "him",
        "her",
        "his",
        "hers",
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
        "you",
        "your",
        "yours",
        "yourself",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "if",
        "then",
        "than",
        "because",
        "so",
        "not",
        "no",
        "yes",
        "very",
        "just",
        "only",
        "some",
        "any",
        "all",
        "each",
        "much",
        "many",
        "more",
        "most",
        "other",
        "another",
        "such",
        "same",
        "too",
        "again",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "now",
        "still",
        "even",
        "also",
        "like",
        "feel",
        "feels",
        "feeling",
        # Domain-saturated tokens — these appear in nearly every thought and
        # would otherwise dominate the Jaccard score
        "user",
        "thought",
        "thinking",
        "wonder",
        "wondering",
        "notice",
        "noticing",
    }
)


def _content_word_overlap(a: str, b: str) -> float:
    """Jaccard overlap on CONTENT words only.

    Tokens shorter than 3 chars or in _STOP_WORDS are dropped. This is the
    similarity function used to reject near-duplicate thoughts.
    """
    if not a or not b:
        return 0.0

    def content_tokens(s: str) -> set[str]:
        return {
            w for w in re.findall(r"[a-z']+", s.lower()) if len(w) >= 3 and w not in _STOP_WORDS
        }

    ta = content_tokens(a)
    tb = content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Verb families used by the frame-repetition gate. Template collapse swaps the
# topic noun while keeping the opening frame ("I should investigate/explore/
# consider research on X"). Collapsing near-synonym verbs to a single class lets
# us detect that the *shape* of the thought is repeating even when the words differ.
_FRAME_VERB_CLASSES: dict[str, str] = {}
for _cls, _verbs in {
    "INQUIRE": (
        "investigate",
        "explore",
        "consider",
        "look",
        "examine",
        "study",
        "research",
        "analyze",
        "analyse",
        "review",
        "dig",
        "delve",
        "probe",
        "survey",
        "assess",
        "evaluate",
        "understand",
        "learn",
    ),
    "WONDER": ("wonder", "question", "ask", "muse", "ponder", "speculate"),
    "NOTICE": ("notice", "observe", "see", "realize", "realise", "note", "catch", "spot"),
    "RECALL": ("remember", "recall", "recollect", "reflect"),
    "FEEL": ("feel", "sense", "worry", "hope", "fear"),
    "WANT": ("want", "need", "wish", "intend", "plan"),
}.items():
    for _v in _verbs:
        _FRAME_VERB_CLASSES[_v] = _cls


def _frame_signature(text: str) -> str:
    """Return a coarse 'shape' signature of a thought's opening clause.

    Walks the leading tokens, emitting them verbatim until it hits a verb it
    recognizes, which it replaces with that verb's CLASS and stops. So
    "I should investigate recent papers..." and "I should explore studies..."
    both reduce to "i should INQUIRE" — letting the frame-repetition gate catch
    template collapse that the word-overlap and cosine gates miss (they only see
    the swapped topic nouns, never the shared frame). Empty string = no signature.
    """
    words = re.findall(r"[a-z']+", text.lower())[:6]
    sig: list[str] = []
    for w in words:
        if w in _FRAME_VERB_CLASSES:
            sig.append(_FRAME_VERB_CLASSES[w])
            break
        sig.append(w)
        if len(sig) >= 3:  # cap leading function-word run so the sig stays coarse
            break
    return " ".join(sig)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two embedding vectors; 0.0 if either is missing or
    mismatched. Used by the semantic dedup gate (same embedder as the skill
    selector, so vectors are comparable)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    import math as _math

    num = sum(x * y for x, y in zip(a, b, strict=True))
    na = _math.sqrt(sum(x * x for x in a))
    nb = _math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


# How similar a new thought can be to recent ones before we discard it as
# redundant. Word-set Jaccard — 0.35 catches near-duplicates while still
# letting genuinely different thoughts through. (Semantic angle tracking,
# added below, handles same-idea-different-words cases the word check misses.)
DMN_OVERLAP_THRESHOLD = float(os.environ.get("BRAIN_DMN_OVERLAP_THRESHOLD", "0.35"))
# How many recent thoughts/angles to show the LLM as context (variety pressure).
# Larger window = model is told about more prior territory to avoid.
DMN_RECENT_THOUGHTS = int(os.environ.get("BRAIN_DMN_RECENT_THOUGHTS", "10"))
# How many recent thoughts to show the LLM VERBATIM in the prompt. Kept small:
# dumping 10 near-identical priors few-shot-primes the model to continue the
# pattern ("this is my voice") instead of breaking it. The angle list (below)
# carries the broader "territory already covered" signal more cheaply.
DMN_PROMPT_PRIORS = int(os.environ.get("BRAIN_DMN_PROMPT_PRIORS", "3"))
# How many of those recent thoughts to actually COMPARE against for hard dedup.
# Narrower than DMN_RECENT_THOUGHTS so thoughts can recur after a gap — the LLM
# context pressure (above) already discourages literal repeats. Comparing against
# all 10 causes over-suppression on focused topics after just 3-4 thoughts.
DMN_DEDUP_WINDOW = int(os.environ.get("BRAIN_DMN_DEDUP_WINDOW", "4"))
# How many recent thought angles to block (separate from text-overlap window).
DMN_RECENT_ANGLES = int(os.environ.get("BRAIN_DMN_RECENT_ANGLES", "8"))
# Frame-repetition gate: how many recent frame-signatures to track, and how many
# prior matches of the same signature trigger suppression. With max=2 a third
# consecutive thought sharing the same opening shape ("i should INQUIRE") is
# rejected — catching template collapse the semantic gates can't see.
DMN_RECENT_FRAMES = int(os.environ.get("BRAIN_DMN_RECENT_FRAMES", "6"))
DMN_FRAME_REPEAT_MAX = int(os.environ.get("BRAIN_DMN_FRAME_REPEAT_MAX", "2"))
