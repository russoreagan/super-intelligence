"""Bounded in-process persona state — an LRU over every per-persona holder.

One shared brain process serves thousands of personas (a marketplace org), but
until now every per-persona structure grew monotonically with the personas
ever bound in the process and was freed only by the hard purge: the DMN's
transient bundles (with their embedding deques), the wiring graph's per-persona
edge maps, the mandate catalogue cache, and (bounded only by count) the
chemistry registries. This module is the one eviction policy for all of them.

  touch(slug)            — stamped by store.bind_persona, the choke point every
                           turn, DMN tick and job replay passes through.
  register(name, evict,  — a holder registers how to evict one persona (persist
           resident)       first, then drop) and which personas it holds.
  sweep(protected)       — evict LRU beyond `persona_resident_cap`, and anything
                           idle longer than `persona_resident_idle_s`; never a
                           protected persona (home, the roster, the bound one,
                           personas with an open API session).

Evictors persist before they drop (the DMN's novelty + routing weights, dirty
wiring edges, chemistry pairs), so an evicted persona is simply re-read on its
next turn. The hard purge uses the holders' non-persisting `forget_*` paths
instead; this module never touches them.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_bound: dict[str, float] = {}
_holders: dict[str, tuple[Callable[[str], object], Callable[[], Iterable[str]]]] = {}
_protectors: dict[str, Callable[[], Iterable[str]]] = {}
_last_sweep_ts: float = 0.0


def _slug(p: str) -> str:
    from brain.persona_key import persona_slug

    return persona_slug(p or "")


def _setting(key: str, default):
    try:
        from brain.settings import settings

        return settings.get(key, default)
    except Exception:
        return default


def enabled() -> bool:
    try:
        return bool(int(_setting("persona_residency_enabled", 1) or 0))
    except (TypeError, ValueError):
        return True


def touch(persona: str, now: float | None = None) -> None:
    """LRU stamp. Cheap: one dict write under a lock; never raises."""
    slug = _slug(persona)
    if not slug:
        return
    with contextlib.suppress(Exception), _lock:
        _last_bound[slug] = float(now if now is not None else time.time())


def last_bound(persona: str) -> float | None:
    with _lock:
        return _last_bound.get(_slug(persona))


def register(
    name: str,
    evict: Callable[[str], object | Awaitable[object]],
    resident: Callable[[], Iterable[str]],
) -> None:
    """A holder: `evict(slug)` persists-then-drops one persona (sync or async);
    `resident()` lists the slugs it currently holds."""
    with _lock:
        _holders[name] = (evict, resident)


def unregister(name: str) -> None:
    with _lock:
        _holders.pop(name, None)
        _protectors.pop(name, None)


def register_protector(name: str, personas: Callable[[], Iterable[str]]) -> None:
    """Personas that must stay resident regardless of age (the DMN roster, the
    personas with an open API session)."""
    with _lock:
        _protectors[name] = personas


def resident_personas() -> set[str]:
    out: set[str] = set()
    with _lock:
        holders = list(_holders.values())
    for _evict, resident in holders:
        with contextlib.suppress(Exception):
            out.update(_slug(p) for p in resident() if _slug(p))
    return out


def _victims(protected: set[str], now: float) -> list[str]:
    try:
        cap = int(_setting("persona_resident_cap", 256) or 0)
    except (TypeError, ValueError):
        cap = 256
    try:
        idle_s = float(_setting("persona_resident_idle_s", 3600) or 0.0)
    except (TypeError, ValueError):
        idle_s = 3600.0
    resident = resident_personas() - {p for p in protected if p}
    with _lock:
        # A resident never seen by bind_persona (loaded by a path that bypasses
        # it) is stamped now: it ages from here rather than being evicted at once.
        for p in resident:
            _last_bound.setdefault(p, now)
        stamps = dict(_last_bound)
    ordered = sorted(resident, key=lambda p: (stamps.get(p, now), p))
    victims: list[str] = []
    if idle_s > 0:
        victims.extend(p for p in ordered if now - stamps.get(p, 0.0) > idle_s)
    if cap > 0:
        remaining = [p for p in ordered if p not in victims]
        over = len(remaining) - cap
        if over > 0:
            victims.extend(remaining[:over])  # oldest first
    return list(dict.fromkeys(victims))


async def sweep(
    *, protected: Iterable[str] = (), now: float | None = None, force: bool = False
) -> list[str]:
    """Evict what the policy says. Throttled by `persona_residency_sweep_s`
    unless `force`. Returns the slugs evicted. Never raises."""
    global _last_sweep_ts
    if not enabled():
        return []
    ref = float(now if now is not None else time.time())
    try:
        every = float(_setting("persona_residency_sweep_s", 60) or 0.0)
    except (TypeError, ValueError):
        every = 60.0
    if not force and ref - _last_sweep_ts < every:
        return []
    _last_sweep_ts = ref
    prot = {_slug(p) for p in protected if _slug(p)}
    with contextlib.suppress(Exception):
        from brain import org_settings

        prot.add(_slug(org_settings.home_persona()))
    with contextlib.suppress(Exception):
        from brain.second_brain.store import active_persona

        prot.add(_slug(active_persona()))
    with _lock:
        protectors = list(_protectors.values())
    for fn in protectors:
        with contextlib.suppress(Exception):
            prot.update(_slug(p) for p in fn() if _slug(p))
    victims = _victims(prot, ref)
    if not victims:
        return []
    with _lock:
        holders = list(_holders.items())
    evicted: list[str] = []
    for slug in victims:
        ok = True
        for name, (evict, _resident) in holders:
            try:
                r = evict(slug)
                if inspect.isawaitable(r):
                    await r
            except Exception as e:
                ok = False
                logger.debug("[residency] %s could not evict %s: %s", name, slug, e)
        if ok:
            evicted.append(slug)
            with _lock:
                _last_bound.pop(slug, None)
    if evicted:
        logger.info(
            "[residency] evicted %d idle persona(s) from memory: %s",
            len(evicted),
            ", ".join(evicted[:8]),
        )
    return evicted


def _reset_for_tests() -> None:
    global _last_sweep_ts
    with _lock:
        _last_bound.clear()
        _holders.clear()
        _protectors.clear()
    _last_sweep_ts = 0.0
