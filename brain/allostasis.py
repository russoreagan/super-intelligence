"""
Allostatic learning: chemistry baselines and reactivity that learn from stable signals.

The mood (``Neuromodulators`` / ``HormonalState``) keeps decaying to its baseline
within minutes, as before. What this module adds is the slow layer biology has and
the brain lacked: the BASELINE and the REACTIVITY themselves adapt to sustained
conditions — never per turn, only at sleep, only from signals that persist, always
bounded around the persona's temperament (its ``anchor``: chemistry.json resting).

Four mechanisms, each recreating a behavior rather than copying a mechanism:

1. Reward-rate DA. Tonic dopamine tracks the long-run average reward rate (Niv,
   Daw, Joel & Dayan 2007). A slow average of GROUNDED outcome reward — job and
   tool results, world-checked predictions, the user's own reactions, external
   grades; never the persona grading its own drafts — shifts the DA baseline:
   work that keeps landing settles the persona a little brighter.
2. Per-trigger reactivity. Repeated, predictable, manageable situations get a
   smaller response (habituation); unpredictable or uncontrollable ones a larger
   one (sensitization) (Grissom & Bhatnagar 2009; Maier & Seligman 2016). Keyed by
   a content-free trigger (intent × tone), so a new kind of situation starts at
   full reactivity, and an absent trigger recovers toward 1.0 over weeks. Threat
   channels never habituate below a floor: familiarity must not dull real danger.
3. Chronic load. Sustained, uncontrollable stress accumulates (allostatic load,
   McEwen & Wingfield 2003), raises the GABA/CORT baselines a little, and decays
   back once the stress stops. Stress the persona can act on adds little.
4. Per-relationship bonding. Each relationship (persona × person) carries its own
   small OXT/5-HT up, CORT down offset, grown across distinct days from the bond
   model and fading with long absence (Young & Wang 2004). Applied when that
   person's chemistry binds; erased with the person.

Rules every mechanism obeys: at most one baseline update per (source, UTC day) and a
few per persona per day (spacing; a burst or a replayed batch cannot move it),
exactly once per turn (the sleep stage ledger), bounded offsets, a kill switch
each (``allostatic_learning`` is the master), and a manual temperament edit resets
the learned offsets (a new anchor). State lives in ``allostasis.json`` in the
persona's state root, so persona purge removes it and clones never carry it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

NEURO = ("DA", "ACh", "GABA", "Glu", "NE")
HORMONES = ("5HT", "CORT", "OXT", "AEA")
THREAT_CHANNELS = ("GABA", "NE")
STATE_FILE = "allostasis.json"
OWNER = "owner"

# Grounded reward: emitted by something outside the persona's own self-grading.
GROUNDED_REASONS = frozenset(
    {"job_success", "job_failure", "tool_success", "tool_failure", "world_prediction"}
)
GROUNDED_SIGNALS = frozenset({"external_user", "external_grader"})

_DAY = 86400.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}

_DEFAULTS = {
    "allostasis_min_session_turns": 3,
    "allostasis_max_updates_per_day": 3,
    "reward_rate_alpha": 0.05,
    "reward_rate_da_gain": 0.10,
    "load_tau_days": 10.0,
    "load_gain": 0.01,
    "load_cap": 0.05,
    "load_max": 10.0,
    "habituation_step": 0.02,
    "sensitization_step": 0.02,
    "reactivity_gain_min": 0.6,
    "reactivity_gain_max": 1.5,
    "reactivity_threat_floor": 0.7,
    "reactivity_recovery_days": 21.0,
    "reactivity_max_steps_per_day": 1,
    "reactivity_max_keys": 64,
    "bond_rate": 0.2,
    "bond_fade_days": 60.0,
    "bond_oxt": 0.10,
    "bond_5ht": 0.05,
    "bond_cort": -0.03,
}


# ── settings ──────────────────────────────────────────────────────────────────


def _s(key: str):
    from brain.settings import settings

    v = settings.get(key, _DEFAULTS.get(key))
    return _DEFAULTS.get(key) if v is None else v


def _f(key: str) -> float:
    try:
        return float(_s(key))
    except (TypeError, ValueError):
        return float(_DEFAULTS[key])


def enabled(mechanism: str | None = None) -> bool:
    """The master switch, and (with ``mechanism``) that mechanism's own switch."""
    from brain.settings import settings

    try:
        if not int(settings.get("allostatic_learning", 1) or 0):
            return False
        return mechanism is None or bool(int(settings.get(mechanism, 1) or 0))
    except (TypeError, ValueError):
        return True


# ── state ─────────────────────────────────────────────────────────────────────


def _slug(persona: str | None) -> str:
    from brain.persona_key import active_or_home_persona, persona_slug

    return persona_slug(persona if persona else (active_or_home_persona() or ""))


def state_path(persona: str | None = None) -> Path:
    from brain.persona_key import persona_state_root

    return persona_state_root(_slug(persona)) / STATE_FILE


def _empty() -> dict:
    return {
        "version": 1,
        "reward": {"r_bar": 0.0, "n": 0},
        "load": {"L": 0.0, "ts": 0.0},
        "gains": {},
        "bonds": {},
        "slots": {"day": "", "total": 0, "sources": []},
        "history": [],
    }


def load_state(persona: str | None = None) -> dict:
    p = state_path(persona)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _empty()
    hit = _cache.get(str(p))
    if hit and hit[0] == mtime:
        return json.loads(json.dumps(hit[1]))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        st = {**_empty(), **(data if isinstance(data, dict) else {})}
    except Exception as e:
        logger.warning("[allostasis] could not read %s: %s — starting fresh", p, e)
        st = _empty()
    _cache[str(p)] = (mtime, st)
    return json.loads(json.dumps(st))


def save_state(persona: str | None, st: dict) -> None:
    p = state_path(persona)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        _cache.pop(str(p), None)
    except Exception as e:
        logger.warning("[allostasis] could not save %s: %s", p, e)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _anchor(persona: str) -> dict[str, float]:
    """The persona's temperament: chemistry.json resting (the table profile, or a
    manual edit). {} when unknown — the caller then leaves baselines alone."""
    try:
        from brain import persona_chem

        path = persona_chem._path(persona)  # noqa: SLF001 - read-only, never seed
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            resting = persona_chem._only_channels(data.get("resting") or {})  # noqa: SLF001
            if resting:
                return resting
        return dict(persona_chem._seed_resting(persona))  # noqa: SLF001 - computed, not written
    except Exception:
        return {}


def has_learned(persona: str | None = None) -> bool:
    """Whether this persona has any learned state at all. Until it does, nothing
    here touches live chemistry (a fresh persona behaves exactly as before)."""
    return state_path(persona).is_file()


# ── runtime reads ─────────────────────────────────────────────────────────────

_KEY_RE = re.compile(r"[^a-z0-9_]+")


def trigger_key(features: dict) -> str:
    """Content-free situation class for reactivity learning: intent × tone. Small
    vocabularies on both sides, so a persona holds a few dozen keys at most."""

    def norm(v) -> str:
        return _KEY_RE.sub("_", str(v or "").lower()).strip("_")[:24] or "none"

    tone = features.get("user_tone_toward_ai") or features.get("user_register") or ""
    return f"{norm(features.get('intent'))}|{norm(tone)}"


def _recovered(g: float, last_ts: float, now: float) -> float:
    days = max(0.0, (now - last_ts) / _DAY) if last_ts else 0.0
    tau = max(0.5, _f("reactivity_recovery_days"))
    return 1.0 + (g - 1.0) * math.exp(-days / tau)


def gain(persona: str | None, key: str, channel: str, now: float | None = None) -> float:
    """Learned reactivity gain for (trigger, channel); 1.0 when nothing learned."""
    if not key or not enabled("reactivity_habituation"):
        return 1.0
    entry = (load_state(persona).get("gains") or {}).get(key)
    if not entry:
        return 1.0
    c = (entry.get("ch") or {}).get(channel)
    if not c:
        return 1.0
    return _recovered(float(c.get("g", 1.0)), float(entry.get("last_ts", 0.0)), now or time.time())


def _load_now(st: dict, now: float) -> float:
    ld = st.get("load") or {}
    L, ts = float(ld.get("L", 0.0)), float(ld.get("ts", 0.0))
    if L <= 0 or not ts:
        return max(0.0, L)
    return L * math.exp(-max(0.0, now - ts) / _DAY / max(0.5, _f("load_tau_days")))


def offsets(persona: str | None = None, now: float | None = None, st: dict | None = None) -> dict:
    """The learned shift of the persona baseline from its anchor (bounded)."""
    now = now or time.time()
    st = st if st is not None else load_state(persona)
    out: dict[str, float] = {}
    if enabled("reward_rate_baseline"):
        r = max(-1.0, min(1.0, float((st.get("reward") or {}).get("r_bar", 0.0))))
        out["DA"] = r * _f("reward_rate_da_gain")
    if enabled("chronic_load"):
        lo = min(_f("load_cap"), _f("load_gain") * _load_now(st, now))
        if lo > 0:
            out["GABA"] = lo
            out["CORT"] = lo
    return {k: round(v, 5) for k, v in out.items() if v}


def bond_offsets(
    persona: str | None, person: str, now: float | None = None, st: dict | None = None
) -> dict:
    if not person or not enabled("relationship_bonding"):
        return {}
    now = now or time.time()
    st = st if st is not None else load_state(persona)
    b = (st.get("bonds") or {}).get(person)
    if not b:
        return {}
    away = max(0.0, (now - float(b.get("last_seen", now))) / _DAY)
    fade = math.exp(-away / max(1.0, _f("bond_fade_days")))
    return {
        ch: round(float(b.get(ch, 0.0)) * fade, 5) for ch in ("OXT", "5HT", "CORT") if b.get(ch)
    }


def _clip(profile: dict[str, float]) -> dict[str, float]:
    from brain import persona_chem

    out = {ch: max(0.0, min(1.0, float(v))) for ch, v in profile.items()}
    return persona_chem._floor_resting(out)  # noqa: SLF001 - the one structural envelope


def effective_baseline(persona: str | None = None, now: float | None = None) -> dict | None:
    """anchor + learned offsets, inside the structural envelope; None when off or
    the anchor is unknown (callers then leave the baseline exactly as it was)."""
    if not enabled():
        return None
    slug = _slug(persona)
    anchor = _anchor(slug)
    if not anchor:
        return None
    off = offsets(slug, now)
    return _clip({ch: anchor.get(ch, 0.0) + off.get(ch, 0.0) for ch in anchor})


def pair_baseline(persona: str | None, person: str, now: float | None = None) -> dict | None:
    """The baseline a person's chemistry pair relaxes toward: the persona's learned
    baseline plus that relationship's bond. None when nothing would change."""
    base = effective_baseline(persona, now)
    if base is None:
        return None
    bond = bond_offsets(_slug(persona), person, now)
    return _clip({ch: base.get(ch, 0.0) + bond.get(ch, 0.0) for ch in base})


# ── grounded signals from a turn trace ─────────────────────────────────────────


def grounded_reward(trace) -> float | None:
    """Sum of this turn's GROUNDED reward (DA units), or None when it had none."""
    total, seen = 0.0, False
    for e in getattr(trace, "reward_emissions", None) or []:
        if not isinstance(e, dict):
            continue
        # The persona's own learned-gain correction (hypothalamus "reactivity") is
        # not an outcome: counting it fed habituation back into the DA rate and
        # let sensitization to a hostile trigger sensitize itself further.
        if e.get("reason") == "reactivity":
            continue
        if e.get("reason") in GROUNDED_REASONS or e.get("signal_type") in GROUNDED_SIGNALS:
            with contextlib.suppress(TypeError, ValueError):
                total += float(e.get("delta", 0.0))
                seen = True
    grade = getattr(trace, "external_grade", None)
    if grade is not None:
        with contextlib.suppress(TypeError, ValueError):
            total += 0.2 * max(-1.0, min(1.0, float(grade)))
            seen = True
    return total if seen else None


def _uncontrolled(trace) -> bool:
    """The turn ended worse by a grounded measure (a failed tool or job, a bad
    grade, the user's reaction souring), or met HOSTILITY it did not resolve. A
    tense but predictable situation (a debate, a hard question) is not
    uncontrolled: biology habituates to that, it does not sensitize."""
    r = grounded_reward(trace)
    if r is not None and r < -0.01:
        return True
    hostile = (getattr(trace, "trigger_key", "") or "").split("|")[0] == "hostile"
    return hostile and not (r is not None and r > 0.01)


def session_reward(traces: list) -> float | None:
    vals = [r for r in (grounded_reward(t) for t in traces) if r is not None]
    if not vals:
        return None
    return max(-1.0, min(1.0, sum(vals) / (0.1 * max(1, len(traces)))))


def session_stress(traces: list, base: dict) -> float:
    """Mean per-turn elevation of the stress channels above baseline, weighted by
    how uncontrollable the turn was (controllable stress counts 0.3)."""
    if not traces:
        return 0.0
    total = 0.0
    for t in traces:
        nm, hs = getattr(t, "neuromod", None) or {}, getattr(t, "hormonal", None) or {}
        lv = {**hs, **nm}
        e = sum(
            max(0.0, float(lv.get(ch, 0.0)) - float(base.get(ch, 0.0)))
            for ch in ("GABA", "NE", "CORT")
            if ch in lv
        )
        total += e * (1.0 if _uncontrolled(t) else 0.3)
    return total / len(traces)


def _bond_strength(trace) -> float:
    """0..1 from the relationship model's bond (0..100 high-water mark) and live
    affection (-50..+100). Both are on a 0..100 scale; reading bond as 0..1 made
    any acquaintance with bond >= 2 saturate at full strength."""
    bond = max(0.0, float(getattr(trace, "bond", 0.0) or 0.0)) / 100.0
    aff = max(0.0, float(getattr(trace, "affection", 0) or 0)) / 100.0
    return max(0.0, min(1.0, 0.5 * bond + 0.5 * aff))


# ── the sleep pass ─────────────────────────────────────────────────────────────


def _take_slot(st: dict, day: str, source: str) -> bool:
    """Spacing: at most one baseline update per (source, UTC day), and at most
    allostasis_max_updates_per_day per persona per day."""
    slots = st.setdefault("slots", {"day": "", "total": 0, "sources": []})
    if slots.get("day") != day:
        slots.update({"day": day, "total": 0, "sources": []})
    if source in slots["sources"] or slots["total"] >= int(_f("allostasis_max_updates_per_day")):
        return False
    slots["sources"].append(source)
    slots["total"] += 1
    return True


def _habituate(st: dict, trace, day: str, now: float) -> int:
    key = getattr(trace, "trigger_key", "") or ""
    imps = getattr(trace, "appraisal_impulses", None) or {}
    if not key or not imps:
        return 0
    gains = st.setdefault("gains", {})
    entry = gains.setdefault(key, {"ch": {}, "day": "", "steps": 0, "last_ts": 0.0})
    if entry.get("day") != day:
        entry["day"], entry["steps"] = day, 0
    can_step = entry["steps"] < int(_f("reactivity_max_steps_per_day"))
    uncontrolled = _uncontrolled(trace)
    lo, hi = _f("reactivity_gain_min"), _f("reactivity_gain_max")
    h, s = _f("habituation_step"), _f("sensitization_step")
    stepped = 0
    for ch, imp in imps.items():
        try:
            imp = float(imp)
        except (TypeError, ValueError):
            continue
        if ch not in NEURO or abs(imp) < 0.005:
            continue
        c = entry["ch"].setdefault(ch, {"g": 1.0, "exp": imp, "n": 0})
        g = _recovered(float(c.get("g", 1.0)), float(entry.get("last_ts", 0.0)), now)
        exp, n = float(c.get("exp", imp)), int(c.get("n", 0))
        predictable = n >= 2 and abs(imp - exp) <= 0.5 * max(abs(exp), 0.01)
        if can_step and n >= 2:
            if uncontrolled:
                g *= 1.0 + s
            elif predictable:
                g *= 1.0 - h
            else:
                g *= 1.0 + s / 2.0
            stepped = 1
        floor = max(lo, _f("reactivity_threat_floor")) if ch in THREAT_CHANNELS else lo
        c["g"] = round(max(floor, min(hi, g)), 5)
        c["exp"] = round(exp + 0.2 * (imp - exp) if n else imp, 5)
        c["n"] = n + 1
    entry["last_ts"] = now
    entry["steps"] += stepped
    cap = int(_f("reactivity_max_keys"))
    if len(gains) > cap:
        for k in sorted(gains, key=lambda k: gains[k].get("last_ts", 0.0))[: len(gains) - cap]:
            gains.pop(k, None)
    return stepped


def _learn_persona(slug: str, traces: list, now: float) -> dict:
    from brain.persona_key import persona_slug

    st = load_state(slug)
    anchor = _anchor(slug)
    base = {ch: anchor.get(ch, 0.0) + offsets(slug, now, st).get(ch, 0.0) for ch in anchor}
    day = _day(now)
    before = offsets(slug, now, st)
    report = {"persona": slug, "sessions_used": 0, "habituation_steps": 0, "bonds_updated": 0}

    # One conversation = one person's API session (engine lane), else their turns in
    # this process session. TurnTrace.session_id alone is the PROCESS session, the
    # same for every lane, so keying on it pooled every customer into one "session"
    # whose spacing slot went to whoever spoke first.
    by_session: dict[tuple[str, str], list] = {}
    for t in traces:
        person = str(getattr(t, "end_user_id", "") or "") or OWNER
        conv = str(getattr(t, "api_session_id", "") or getattr(t, "session_id", "") or "")
        by_session.setdefault((person, conv), []).append(t)
    min_turns = int(_f("allostasis_min_session_turns"))
    for (source, _conv), sess in by_session.items():
        if len(sess) < min_turns:
            continue
        if not _take_slot(st, day, source):
            continue
        report["sessions_used"] += 1
        if enabled("reward_rate_baseline"):
            r = session_reward(sess)
            if r is not None:
                rw = st.setdefault("reward", {"r_bar": 0.0, "n": 0})
                rw["r_bar"] = round(
                    rw.get("r_bar", 0.0) + _f("reward_rate_alpha") * (r - rw.get("r_bar", 0.0)), 5
                )
                rw["n"] = int(rw.get("n", 0)) + 1
        if enabled("chronic_load") and base:
            L = min(_f("load_max"), _load_now(st, now) + session_stress(sess, base))
            st["load"] = {"L": round(L, 5), "ts": now}

    observed = False
    if enabled("reactivity_habituation"):
        for t in traces:
            report["habituation_steps"] += _habituate(st, t, day, now)
            observed = observed or bool(
                getattr(t, "trigger_key", "") and getattr(t, "appraisal_impulses", None)
            )

    if enabled("relationship_bonding"):
        by_person: dict[str, list] = {}
        for t in traces:
            by_person.setdefault(str(getattr(t, "end_user_id", "") or "") or OWNER, []).append(t)
        rate = _f("bond_rate")
        targets = {"OXT": _f("bond_oxt"), "5HT": _f("bond_5ht"), "CORT": _f("bond_cort")}
        bonds = st.setdefault("bonds", {})
        for person, ts in by_person.items():
            if len(ts) < 2:
                continue
            b = bonds.get(person) or {}
            if b.get("day") == day:
                b["last_seen"] = now
                bonds[person] = b
                continue
            strength = max(_bond_strength(t) for t in ts)
            for ch, k in targets.items():
                cur = float(b.get(ch, 0.0))
                b[ch] = round(cur + rate * (k * strength - cur), 5)
            b["day"], b["last_seen"] = day, now
            bonds[person] = b
            report["bonds_updated"] += 1

    after = offsets(slug, now, st)
    report["offsets_before"], report["offsets_after"] = before, after
    if observed or report["sessions_used"] or report["bonds_updated"]:
        st.setdefault("history", []).append(
            {"ts": now, "offsets": after, "r_bar": (st.get("reward") or {}).get("r_bar", 0.0)}
        )
        st["history"] = st["history"][-50:]
        save_state(slug, st)
        with contextlib.suppress(Exception):
            from brain.observability.decisions import decisions
            from brain.second_brain.store import bind_persona

            with bind_persona(persona_slug(slug)):
                decisions.log("allostatic_update", **report)
    return report


def learn(session_id: str, traces: list, now: float | None = None) -> list[dict]:
    """The sleep stage: learn from these (fresh, real) turn traces, per persona.
    Never raises — a learning hiccup must not cost the rest of sleep."""
    if not enabled() or not traces:
        return []
    from brain.persona_key import persona_slug

    now = now or time.time()
    groups: dict[str, list] = {}
    for t in traces:
        slug = persona_slug(getattr(t, "persona_name", "") or "") or _slug(None)
        groups.setdefault(slug, []).append(t)
    out = []
    with _lock:
        for slug, ts in groups.items():
            try:
                out.append(_learn_persona(slug, ts, now))
            except Exception as e:
                logger.warning("[allostasis] learning skipped for %s: %s", slug, e)
    return out


# ── applying learned baselines to live chemistry ──────────────────────────────


def apply_to_pair(pair, persona: str | None, person: str) -> bool:
    """Set a chemistry pair's baselines to (persona learned baseline + bond)."""
    if pair is None or not enabled() or not has_learned(persona):
        return False
    base = pair_baseline(persona, person)
    if not base:
        return False
    pair.apply_profile(base, None)
    return True


def apply_to_registry(reg, persona: str | None) -> int:
    n = 0
    for eu, pair in list(getattr(reg, "_live", {}).items()):
        with contextlib.suppress(Exception):
            n += int(apply_to_pair(pair, persona, eu or OWNER))
    return n


def apply_live(bus, home: str, persona_registries: dict | None = None) -> int:
    """After sleep (and at boot): move every live pair to its learned baseline —
    the home resting pair (owner lane + idle loop), the home persona's customer
    pairs, and each bound persona's. Levels are untouched; only where they relax to."""
    if not enabled() or bus is None:
        return 0
    n = int(apply_to_pair(getattr(bus, "resting_chem", None), home, OWNER))
    reg = getattr(bus, "_chem_registry", None)
    if reg is not None:
        n += apply_to_registry(reg, home)
    for slug, r in (persona_registries or {}).items():
        n += apply_to_registry(r, slug)
    return n


# ── edits, erasure, views ─────────────────────────────────────────────────────


def reset_offsets(persona: str | None, reason: str = "anchor_changed") -> None:
    """A manual temperament edit is a new anchor: learned baseline offsets restart
    from zero. Reactivity gains and relationship bonds are kept (they are not the
    temperament)."""
    with _lock:
        st = load_state(persona)
        if not (st.get("reward") or {}).get("r_bar") and not (st.get("load") or {}).get("L"):
            return
        st["reward"] = {"r_bar": 0.0, "n": 0}
        st["load"] = {"L": 0.0, "ts": 0.0}
        st.setdefault("history", []).append({"ts": time.time(), "reset": reason})
        save_state(persona, st)


def _all_state_paths() -> list[Path]:
    from brain.persona_key import persona_state_root

    base = persona_state_root("")
    dirs = [base]
    sib = base.parent if base.parent.name == "personas" else base / "personas"
    with contextlib.suppress(OSError):
        dirs += [d for d in sib.iterdir() if d.is_dir() and d != base]
    return [d / STATE_FILE for d in dirs if (d / STATE_FILE).is_file()]


def forget_person(end_user_id: str) -> int:
    """Erasure: drop this person's relationship bond from every persona."""
    if not end_user_id:
        return 0
    n = 0
    with _lock:
        for p in _all_state_paths():
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (st.get("bonds") or {}).pop(end_user_id, None) is not None:
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
                os.replace(tmp, p)
                _cache.pop(str(p), None)
                n += 1
    return n


def summary(persona: str | None = None, now: float | None = None) -> dict:
    """Content-free view for the chemistry endpoint and the console: anchor,
    learned offsets, effective baseline, reward rate, load, the most-adapted
    triggers, and how many relationships carry a bond (never who)."""
    now = now or time.time()
    slug = _slug(persona)
    st = load_state(slug)
    gains = []
    for key, entry in (st.get("gains") or {}).items():
        for ch, c in (entry.get("ch") or {}).items():
            g = _recovered(float(c.get("g", 1.0)), float(entry.get("last_ts", 0.0)), now)
            if abs(g - 1.0) >= 0.01:
                gains.append({"trigger": key, "channel": ch, "gain": round(g, 3)})
    gains.sort(key=lambda x: abs(x["gain"] - 1.0), reverse=True)
    return {
        "enabled": enabled(),
        "anchor": _anchor(slug),
        "offsets": offsets(slug, now, st),
        "effective": effective_baseline(slug, now),
        "reward_rate": (st.get("reward") or {}).get("r_bar", 0.0),
        "load": round(_load_now(st, now), 4),
        "reactivity": gains[:10],
        "bonds": len(st.get("bonds") or {}),
    }
