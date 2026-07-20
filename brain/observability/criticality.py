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
from dataclasses import dataclass, field

from brain.settings import settings

# ── Per-persona published gain ────────────────────────────────────────────────
#
# The controller's output used to be written straight into the process-global
# settings singleton, and every SwitchNeuron read it from there. Settings are scoped
# per TENANT, not per persona, and one process serves many personas (see the
# BrainSession docstring in session_turn.py) — so one persona's trim was landing on
# every other persona's firing thresholds. This map replaces that write; it is the
# same idea, just keyed by who it belongs to.
#
# Memory-only and never persisted, exactly as the settings write was.
_gain_by_persona: dict[str, float] = {}


def _bound_persona() -> str:
    """Slug of the persona bound to the current turn, or "" when none is."""
    try:
        from brain.persona_key import active_or_home_persona, persona_slug

        return persona_slug(active_or_home_persona())
    except Exception:
        return ""


def _scoped() -> bool:
    return bool(settings.get("criticality_persona_scoped", 1))


def current_gain() -> float:
    """Modulation gain in force for the bound persona.

    Falls back to the static `modulation_gain` setting whenever the controller has
    not published one — which covers three real cases, not just defensiveness:
    the persona has not completed a turn yet, no persona is bound (idle/boot), and
    `flock_dynamics` is OFF. That last one matters: with the controller off, the
    Emotionality dial's `modulation_gain` IS the lever, and it has to keep working.

    Called from SwitchNeuron.effective_threshold on every switch evaluation, so it
    stays a contextvar read plus a dict lookup and never raises.
    """
    static = float(settings.get("modulation_gain", 1.0))
    if not _scoped():
        return static
    try:
        g = _gain_by_persona.get(_bound_persona())
    except Exception:
        return static
    return float(g) if g is not None else static


def reset_gains() -> None:
    """Drop every published gain. Tests only."""
    _gain_by_persona.clear()


@dataclass
class _PersonaWindow:
    """One persona's rolling measurement window and controller gain."""

    sigmas: deque[float] = field(default_factory=deque)
    avalanches: deque[int] = field(default_factory=deque)
    gain: float = 1.0


def branching_ratio(fired_path: list[dict], wiring, min_nodes: int = 4) -> float | None:
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
    """Rolling window of σ + avalanche sizes PER PERSONA, plus the closed-loop
    controller that drives each persona's modulation gain. One instance per brain
    session; `observe()` then `control()` are called once per turn at end-of-turn.

    The window is per persona rather than per session because a session serves many
    personas. Sharing one window averaged unrelated personas' firing paths together,
    so the loop was closing on a signal that belonged to none of them. Resolution
    mirrors `Wiring._by_persona` — lazily created, keyed on the persona bound to the
    current turn.
    """

    def __init__(self) -> None:
        self._by_persona: dict[str, _PersonaWindow] = {}

    def _state(self) -> _PersonaWindow:
        """This turn's persona window, created on first sight.

        With `criticality_persona_scoped` off every persona resolves to the same ""
        key, which restores the single shared window this class used to have.
        """
        key = _bound_persona() if _scoped() else ""
        st = self._by_persona.get(key)
        if st is None:
            window = max(2, int(settings.get("flock_sigma_window", 12)))
            st = _PersonaWindow(
                sigmas=deque(maxlen=window),
                avalanches=deque(maxlen=window),
                # Seeded from the static gain so turning the controller on does not
                # jump on a persona's first turn.
                gain=float(settings.get("modulation_gain", 1.0)),
            )
            self._by_persona[key] = st
        return st

    # ── (2) observable ────────────────────────────────────────────────────────

    def observe(self, fired_path: list[dict], wiring) -> dict:
        """Measure this turn: push σ + avalanche size into the window. Pure
        measurement — does not touch gain. Returns a telemetry dict for the
        TurnTrace (σ may be None when the small-N guard trips)."""
        st = self._state()
        min_nodes = int(settings.get("flock_sigma_min_nodes", 4))
        sigma = branching_ratio(fired_path, wiring, min_nodes)
        avalanche = sum(1 for e in (fired_path or []) if e.get("name"))
        if sigma is not None:
            st.sigmas.append(sigma)
        st.avalanches.append(avalanche)
        return {
            "sigma": round(sigma, 4) if sigma is not None else None,
            "avalanche": avalanche,
            "sigma_smoothed": self.smoothed_sigma(),
            "heavy_tail": self.heavy_tail_metric(),
        }

    def smoothed_sigma(self) -> float | None:
        """Window-mean σ — the multi-turn estimate the controller steers on.
        None until at least one turn produced a defined σ."""
        sigmas = self._state().sigmas
        if not sigmas:
            return None
        return round(sum(sigmas) / len(sigmas), 4)

    def heavy_tail_metric(self) -> float | None:
        """Lightweight heavy-tailedness proxy for the avalanche-size
        distribution: coefficient of variation (std/mean). A critical/power-law
        regime has a heavy tail (CV ≳ 1); a sub-critical/exponential regime is
        tight (CV ≪ 1). Heuristic, not a formal power-law fit."""
        avalanches = self._state().avalanches
        n = len(avalanches)
        if n < 2:
            return None
        mean = sum(avalanches) / n
        if mean <= 0:
            return None
        var = sum((x - mean) ** 2 for x in avalanches) / n
        return round((var**0.5) / mean, 4)

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
        """Drive the bound persona's modulation gain toward the σ* implied by
        arousal, using that persona's window-smoothed σ as feedback. Conservative
        P-controller with EMA smoothing and a hard clamp. Publishes the gain for
        SwitchNeuron.effective_threshold to pick up next turn (memory-only, never
        persisted). Holds gain steady until σ is estimable.

        Publishes to the per-persona map rather than to settings. The settings
        singleton has no persona dimension, so writing there handed this persona's
        trim to every other persona sharing the process.
        """
        st = self._state()
        sigma_star = self.setpoint(arousal)
        sm = self.smoothed_sigma()
        kp = float(settings.get("flock_gain_kp", -0.30))
        alpha = float(settings.get("flock_gain_ema_alpha", 0.25))
        gmin = float(settings.get("flock_gain_min", 0.50))
        gmax = float(settings.get("flock_gain_max", 1.80))
        if sm is not None:
            # Incremental P-step, damped by EMA alpha (small, smooth corrections).
            err = sm - sigma_star
            st.gain = st.gain + alpha * kp * err
            st.gain = max(gmin, min(gmax, st.gain))
            if _scoped():
                _gain_by_persona[_bound_persona()] = st.gain
            else:
                settings.update({"modulation_gain": st.gain})
        return {
            "sigma_star": round(sigma_star, 4),
            "sigma_smoothed": sm,
            "gain": round(st.gain, 4),
        }
