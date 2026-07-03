"""External grading — the one learning signal grounded outside the brain.

The premise-integrity audit measured ~80% of Hebbian reward as self-graded
(critic model, DA swings the brain administered to itself). This module is the
narrow slot where verdicts from OUTSIDE enter: a user thumbs press today, a
validator model doing post-hoc passes tomorrow. Grades land on the TurnTrace
(consumed by HebbianUpdater._composite_outcome at the next consolidation) and
in the eval log via patch_turn, so post-consolidation grades still leave an
auditable record.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def normalize_grade(raw, scale: str = "thumbs") -> float | None:
    """Normalize a raw grade to [-1, +1]. None for unusable input.

    scales: "thumbs" (±1 / bool / any sign), "stars5" (0-5 → [-1, 1]),
    "unit" (already [-1, 1], clamped)."""
    try:
        if raw is None:
            return None
        if isinstance(raw, bool):
            return 1.0 if raw else -1.0
        v = float(raw)
        if scale == "stars5":
            return max(-1.0, min(1.0, (v / 2.5) - 1.0))
        if scale == "thumbs":
            return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)
        return max(-1.0, min(1.0, v))
    except Exception:
        return None


class ExternalGrader:
    """Interface for automated graders (validator models, judge panels).

    Subclasses implement grade(); a runner can then sweep recent turns and
    patch grades post-hoc via EvalLogger.patch_turn — see api_grade_turn in
    session_loops.py for the write path a grade must follow."""

    source = "validator"

    async def grade(self, turn: dict) -> float | None:  # pragma: no cover — interface
        """Return a grade in [-1, +1] for a turn record, or None to abstain."""
        raise NotImplementedError
