# Scope: per-persona isolation for the criticality controller

**Status:** proposed, not implemented. Found 2026-07-19 while auditing the cognitive dials.

---

## What's wrong, in plain terms

Each personality is supposed to have its own temperament. One of the things that varies is
how strongly its mood shifts its own thresholds for acting — how *reactive* it is. There's
a control loop that measures how excitable the brain is being right now and gently trims
that reactivity up or down to keep it in a healthy band.

The loop currently keeps that trim setting in one place shared by the whole process. But a
single process serves several personalities. So the trim one personality's loop computes is
immediately used by all the others, and the excitability measurement it's reacting to is
itself an average across all of them mixed together.

The practical effect: a personality that has just had a burst of intense turns leaves every
other personality in the process more (or less) reactive than its own temperament says it
should be. Nobody's settings are wrong; they're just being overwritten by a neighbour.

---

## Mechanism

**Write.** [session_turn.py:1701](brain/session_turn.py:1701) runs the controller each turn
when `flock_dynamics` is on (default **1**, graduated 2026-06).
[criticality.py:145](brain/observability/criticality.py:145) then does:

```python
settings.update({"modulation_gain": self._gain})
```

`Settings` is a process-global singleton ([settings.py:1345](brain/settings.py:1345)) and
settings are scoped per *tenant*, not per persona. There is no persona dimension anywhere
in that object.

**Read.** [neuron.py:65](brain/neuron.py:65), inside `SwitchNeuron.effective_threshold()`:

```python
gain = float(_settings.get("modulation_gain", 1.0))
```

Every switch, every turn, process-wide.

**Two distinct defects, and both need fixing — fixing only one leaves the bug:**

1. **Application leak.** The gain is published globally, so persona A's trim lands on
   persona B's thresholds.
2. **Measurement mixing.** `FlockCriticality` smooths σ over a rolling window, and there is
   one instance per *session* ([session_turn.py:1707](brain/session_turn.py:1707)), not per
   persona. Since one session serves many personas (agent lanes bind per turn; the
   round-robin DMN rotates the bound persona per tick), that window averages unrelated
   personas' firing paths. The controller is closing a loop on a blended signal.

**Scope is genuinely small.** I checked every runtime `settings.update()` call site. The
only other ones are 8 in `runpod_manager.py`, all writing RunPod host/readiness — genuinely
process-global infrastructure state, correctly global. This is one bug, not a class.

---

## The fix

**Reuse the idiom already in the codebase.** `Wiring` solved exactly this problem: it keeps
`_by_persona: dict[str, ...]`, resolves the bound persona on access
([wiring.py:95-122](brain/wiring.py:95)), and lazily initialises per persona. Mirror that
rather than inventing a second mechanism. `brain/persona_key.py` already provides
`active_or_home_persona()` and `persona_slug()`.

### 1. Per-persona controller state — `brain/observability/criticality.py`

Key the controller's mutable state by persona slug instead of holding one set on the
instance: the rolling σ window, the EMA'd gain, and the avalanche tracker. Resolve on access
via `active_or_home_persona()`, initialising a fresh window on first sight of a persona.

Replace the `settings.update(...)` write with a module-level per-persona holder:

```python
_gain_by_persona: dict[str, float] = {}

def current_gain() -> float:
    """Bound persona's controller gain; falls back to the static setting.

    The fallback is load-bearing, not defensive: with flock_dynamics OFF the
    Emotionality dial's modulation_gain IS the lever, and personas that turn the
    controller off must keep it.
    """
```

### 2. Read the bound persona's gain — `brain/neuron.py:65`

One line: `gain = current_gain()` in place of the settings read, keeping the existing
try/except identity fallback.

### 3. Kill switch

`criticality_persona_scoped: 1` in `DEFAULTS`, per house convention (bare noun, defaults on,
kill switch not enable switch). Off ⇒ byte-identical to today, including the global write.

---

## Risks

**Hot path.** `effective_threshold()` runs for every switch evaluation, roughly 10-20 times
per turn. The change adds a contextvar read plus a dict lookup — negligible at that volume,
but it should be measured rather than assumed, and the resolution must be wrapped so a
contextvar miss degrades to the static setting rather than raising inside threshold
computation.

**Flag-off personas must not regress.** With `flock_dynamics` off, `modulation_gain` remains
a real dial ([settings-ui.js:160](brain/ui/settings-ui.js:160) documents this deliberately).
The fallback path covers it; it needs an explicit test, because this is the easy thing to
break while fixing the leak.

**Behaviour will change for existing multi-persona tenants**, and that is the point — but it
means a persona whose thresholds were being dragged by a noisy neighbour will start behaving
differently after deploy. Worth watching `flock_criticality` decision records for a shift in
per-persona gain spread.

**Both defects confirmed — settled, no longer an open question.** There is exactly one
`BrainSession` per process ([run.py:104](brain/run.py:104)), and its own docstring at
[session_turn.py:124](brain/session_turn.py:124) states that binding a persona for a turn is
how "one process serves many personas." `self._flock_criticality` is a single instance on
that session, so it does mix personas' firing paths in one σ window. Defect (2) is real and
both halves need fixing.

(I first tried to settle this from logged `flock_criticality` records and found zero — the
kind is absent from `LEDGER_TYPES` in
[learning_ledger.py:27](brain/observability/learning_ledger.py:27), so it only ever reaches
the eval log and never the Learning surface. Minor and separable, but worth adding while in
the file: this controller moves real thresholds and is currently unreadable, which is the
same blind spot eligibility credit had.)

---

## Tests

- **The regression itself:** bind persona A, drive several high-arousal turns through the
  controller, then bind persona B and assert B's `effective_threshold` for a given switch is
  unchanged from its pre-A value. Fails today.
- **Window isolation:** interleave A and B turns with opposite σ characteristics and assert
  each persona's smoothed σ tracks only its own.
- **Flag-off fallback:** with `flock_dynamics=0`, the Emotionality dial's `modulation_gain`
  still moves `effective_threshold`.
- **Neutral when off:** `criticality_persona_scoped=0` reproduces current behaviour exactly.
- **Unbound context:** no bound persona ⇒ falls back to the static setting, never raises.

---

## Estimate

Contained. One substantive file (`criticality.py`), a one-line change in `neuron.py`, one
settings key, one new test file. Roughly 100-150 lines changed plus tests. No migration, no
persisted-state change, no schema change — the gain was never persisted (it is memory-only
by design, per the `control()` docstring).

The prerequisite check above (does one session really mix personas?) is 10 minutes against
the ledger and determines whether this is the full fix or just half of it.
