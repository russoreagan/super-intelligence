"""
Flock-dynamics criticality layer (flock_dynamics).

Murmuration finding, returned to its home domain: a network of locally-coupled
units is maximally responsive — a salient input propagates undamped while noise
stays local — when it sits near a *critical point*. Cavagna et al. (2010) bridged
flock criticality to neural-assembly criticality explicitly; this module brings
that to the brain's switch network.

Two halves, both gated by `flock_dynamics` and both driven from the per-turn
firing path (`TurnTrace.fired_path`):

  (2) OBSERVABLE — `branching_ratio()` estimates σ, the mean number of
      direct descendants each propagating firing triggered, reconstructed from
      the flat firing path via the wiring graph. σ≈1 is critical; σ<1 is
      sub-critical (activity dies out — sluggish/literal); σ>1 is super-critical
      (cascading/incoherent). `FlockCriticality` smooths σ over a rolling window
      (per-turn σ is noisy small-N) and tracks the avalanche-size distribution.

  (3) CONTROL — `FlockCriticality.control()` reads arousal, sets an
      arousal-modulated setpoint σ* (low arousal → sub-critical and quiet; high
      arousal → toward — but never above — critical), and nudges the global
      `modulation_gain` to reduce the σ−σ* error. Conservative: slow setpoint,
      EMA-smoothed gain, hard clamp. Arousal is the knob; measured σ is the
      feedback. Never steers super-critical.

Flag-off: nothing here is called, and `modulation_gain` keeps its static value.
"""

from __future__ import annotations

from collections import deque

from brain.settings import settings


def branching_ratio(
    fired_path: list[dict], wiring, min_nodes: int = 4
) -> float | None:
    """Estimate the branching ratio σ for one turn's firing path.

    σ = (fired wiring-edges whose source AND target both fired) / (fired nodes
    that have ≥1 outgoing wiring edge). The denominator excludes terminal nodes,
    which can't have descendants and would otherwise bias σ downward.

    Returns None when too few propagating nodes fired to estimate σ reliably
    (small-N guard) — the caller skips the turn rather than smoothing in noise.
    """
    fired = {e.get("name") for e in (fired_path or []) if e.get("name")}
    fired.discard(None)
    if not fired or wiring is None:
        return None
    internal = [n for n in fired if wiring.has_outgoing(n)]
    if len(internal) < max(1, int(min_nodes)):
        return None
    descendants = 0
    for n in internal:
        try:
            descendants += len(wiring.successors(n) & fired)
        except Exception:
            continue
    return descendants / len(internal)


class FlockCriticality:
    """Per-session rolling window of σ + avalanche sizes, plus the closed-loop
    controller that drives `modulation_gain`. One instance per brain session;
    `observe()` then `control()` are called once per turn at end-of-turn.
    """

    def __init__(self) -> None:
        window = max(2, int(settings.get("flock_sigma_window", 12)))
        self._sigmas: deque[float] = deque(maxlen=window)
        self._avalanches: deque[int] = deque(maxlen=window)
        # Controller state — seeded from the current static gain so flag-on does
        # not jump on turn one. Source of truth is settings; this mirrors it.
        self._gain: float = float(settings.get("modulation_gain", 1.0))

    # ── (2) observable ────────────────────────────────────────────────────────

    def observe(self, fired_path: list[dict], wiring) -> dict:
        """Measure this turn: push σ + avalanche size into the window. Pure
        measurement — does not touch gain. Returns a telemetry dict for the
        TurnTrace (σ may be None when the small-N guard trips)."""
        min_nodes = int(settings.get("flock_sigma_min_nodes", 4))
        sigma = branching_ratio(fired_path, wiring, min_nodes)
        avalanche = sum(1 for e in (fired_path or []) if e.get("name"))
        if sigma is not None:
            self._sigmas.append(sigma)
        self._avalanches.append(avalanche)
        return {
            "sigma": round(sigma, 4) if sigma is not None else None,
            "avalanche": avalanche,
            "sigma_smoothed": self.smoothed_sigma(),
            "heavy_tail": self.heavy_tail_metric(),
        }

    def smoothed_sigma(self) -> float | None:
        """Window-mean σ — the multi-turn estimate the controller steers on.
        None until at least one turn produced a defined σ."""
        if not self._sigmas:
            return None
        return round(sum(self._sigmas) / len(self._sigmas), 4)

    def heavy_tail_metric(self) -> float | None:
        """Lightweight heavy-tailedness proxy for the avalanche-size
        distribution: coefficient of variation (std/mean). A critical/power-law
        regime has a heavy tail (CV ≳ 1); a sub-critical/exponential regime is
        tight (CV ≪ 1). Heuristic, not a formal power-law fit."""
        n = len(self._avalanches)
        if n < 2:
            return None
        mean = sum(self._avalanches) / n
        if mean <= 0:
            return None
        var = sum((x - mean) ** 2 for x in self._avalanches) / n
        return round((var ** 0.5) / mean, 4)

    # ── (3) closed-loop control ────────────────────────────────────────────────

    def setpoint(self, arousal: float) -> float:
        """Arousal-modulated criticality target σ*. Low arousal → sub-critical
        (efficient, quiet at rest); high arousal → toward critical. Hard-capped
        at the high target — the system NEVER deliberately steers super-critical."""
        lo = float(settings.get("flock_sigma_target_low", 0.90))
        hi = float(settings.get("flock_sigma_target_high", 1.00))
        a = max(0.0, min(1.0, float(arousal)))
        return min(hi, lo + (hi - lo) * a)

    def control(self, arousal: float) -> dict:
        """Drive `modulation_gain` toward the σ* implied by arousal, using the
        window-smoothed σ as feedback. Conservative P-controller with EMA
        smoothing and a hard clamp. Writes the gain into settings (memory-only,
        never persisted) so SwitchNeuron.effective_threshold picks it up next
        turn. Holds gain steady until σ is estimable."""
        sigma_star = self.setpoint(arousal)
        sm = self.smoothed_sigma()
        kp = float(settings.get("flock_gain_kp", -0.30))
        alpha = float(settings.get("flock_gain_ema_alpha", 0.25))
        gmin = float(settings.get("flock_gain_min", 0.50))
        gmax = float(settings.get("flock_gain_max", 1.80))
        if sm is not None:
            # Incremental P-step, damped by EMA alpha (small, smooth corrections).
            err = sm - sigma_star
            self._gain = self._gain + alpha * kp * err
            self._gain = max(gmin, min(gmax, self._gain))
            settings.update({"modulation_gain": self._gain})
        return {
            "sigma_star": round(sigma_star, 4),
            "sigma_smoothed": sm,
            "gain": round(self._gain, 4),
        }
