"""The learning-mode switch — one governance event, two directions, audit-logged.

`organizations.learning_mode` (brain/org_settings.py) decides whether a persona is
one learning identity shared across customers (consolidated) or a separate
individual per persona (isolated). Changing it is a governance event with exact
semantics (brain/api/api_guide.md §20 "Learning mode"):

  * owner key or org admin only; `confirm: true` required; audit-logged (who, when,
    from, to, instance_seed); takes effect on the next turn and the next sleep pass
    through the 60 s cache — no restart.
  * consolidated → isolated REQUIRES `instance_seed` ('current' | 'default'): what a
    persona clone starts with. The response lists the personas that hold learned
    state (they become templates) and any persona with more than one end user in
    api_sessions ("multi-owner, cannot be bound").
  * isolated → consolidated is REFUSED (409) while any non-home persona holds learned
    state, unless `force: true` — logged with the persona list. There is no merge.
  * Established-principle injection stops immediately on isolated; hypotheses.json is
    retained but inert. DELETE /v1/org/hypotheses purges it on request.

Both surfaces that can flip the mode — PUT /v1/org/permissions and the console's
Account limits page — route through switch() so they cannot disagree.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path

from brain import org_settings

logger = logging.getLogger(__name__)

SWITCH_FIELDS = ("learning_mode", "instance_seed", "confirm", "force")


class SwitchError(Exception):
    """A refused switch: carries the HTTP status and a flat JSON payload."""

    def __init__(self, status: int, detail: str, **extra) -> None:
        super().__init__(detail)
        self.status = status
        self.payload = {"detail": detail, **extra}


# ── audit log ─────────────────────────────────────────────────────────────────


def audit_log_path() -> Path:
    """The org's governance audit log: <tenant root>/governance_audit.jsonl (the
    home persona root when no tenant root resolves — bare local runs, tests)."""
    from brain.org_permissions import tenant_root
    from brain.persona_key import persona_state_root

    root = tenant_root() or persona_state_root("")
    return Path(root) / "governance_audit.jsonl"


def audit(event: str, actor: dict | None, **fields) -> dict:
    """Append one audit line and mirror it to the process log. Never raises."""
    rec = {
        "ts": time.time(),
        "event": event,
        "actor": {
            "source": str((actor or {}).get("source") or "api"),
            "owner": bool((actor or {}).get("owner")),
            "partner_id": (actor or {}).get("partner_id"),
            "key_id": (actor or {}).get("key_id"),
            "user": (actor or {}).get("user"),
        },
        **fields,
    }
    logger.warning("[governance] %s %s", event, json.dumps(fields, default=str)[:600])
    try:
        path = audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:  # pragma: no cover - disk full etc.
        logger.warning("[governance] audit append failed: %s", e)
    return rec


# ── hypotheses store ──────────────────────────────────────────────────────────


def hypotheses_path() -> Path:
    from brain import cross_learning

    return cross_learning._default_store_path()  # noqa: SLF001 - one definition of the path


def hypotheses_present() -> bool:
    with contextlib.suppress(OSError):
        p = hypotheses_path()
        return p.is_file() and p.stat().st_size > 2
    return False


def purge_hypotheses(actor: dict | None) -> dict:
    """Remove hypotheses.json (the shared, de-identified principle store)."""
    p = hypotheses_path()
    existed = p.is_file()
    for path in (p, p.with_suffix(p.suffix + ".tmp")):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    audit("hypotheses_purged", actor, path=str(p), existed=existed)
    return {"ok": True, "purged": existed, "path": str(p)}


# ── the switch ────────────────────────────────────────────────────────────────


def personas_with_learned_state() -> list[str]:
    """Every non-home persona holding learned state (the switch's template list /
    refusal list). Best-effort per persona."""
    from brain import persona_audit, personas

    out: list[str] = []
    try:
        rows = personas.list_all()
    except Exception as e:
        logger.debug("[governance] persona listing failed: %s", e)
        return out
    for r in rows:
        slug = str(r.get("slug") or "")
        if not slug or org_settings.is_home(slug):
            continue
        with contextlib.suppress(Exception):
            if persona_audit.has_learned_state(slug):
                out.append(slug)
    return out


def describe() -> dict:
    mode, seed = org_settings.refresh()
    return {
        "learning_mode": mode,
        "instance_seed": seed,
        "hypotheses_present": hypotheses_present(),
    }


def _bool(v, field: str) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off", ""):
        return False
    raise SwitchError(400, f"{field} must be a boolean")


def switch(body: dict, actor: dict | None = None) -> dict | None:
    """Apply the learning-mode fields of a governance write. Returns None when the
    body carries none of them, else the switch report. Raises SwitchError (400/409)
    or org_settings.OrgSettingsError (backend / migration)."""
    if not isinstance(body, dict) or not any(k in body for k in SWITCH_FIELDS):
        return None
    mode = body.get("learning_mode")
    seed = body.get("instance_seed")
    confirm = _bool(body.get("confirm"), "confirm")
    force = _bool(body.get("force"), "force")
    if mode is not None and mode not in org_settings.MODES:
        raise SwitchError(400, f"learning_mode must be one of {list(org_settings.MODES)}")
    if seed is not None and seed not in org_settings.SEEDS:
        raise SwitchError(400, f"instance_seed must be one of {list(org_settings.SEEDS)}")
    if mode is None and seed is None:
        raise SwitchError(400, "learning_mode or instance_seed is required with confirm/force")

    cur_mode, cur_seed = org_settings.refresh(force=True)
    if cur_mode == org_settings.UNKNOWN:
        raise org_settings.OrgSettingsError(
            "organizations row unreadable — cannot switch learning_mode safely"
        )

    # Seed-only change (either mode): no confirm needed, still audited.
    if mode is None or mode == cur_mode:
        if seed is None or seed == cur_seed:
            return {
                "learning_mode": cur_mode,
                "instance_seed": cur_seed,
                "changed": [],
                "hypotheses_present": hypotheses_present(),
            }
        _m, new_seed = org_settings.set_instance_seed(seed)
        audit("instance_seed_changed", actor, **{"from": cur_seed, "to": new_seed})
        return {
            "learning_mode": cur_mode,
            "instance_seed": new_seed,
            "changed": ["instance_seed"],
            "hypotheses_present": hypotheses_present(),
        }

    if not confirm:
        raise SwitchError(
            400,
            "confirm: true is required to change learning_mode "
            f"({cur_mode} → {mode}); read the switch semantics first",
        )

    if mode == "isolated":
        if seed is None:
            raise SwitchError(
                400,
                "instance_seed is required when switching to isolated: 'current' "
                "(clones start from what the template learned across everyone it "
                "talked to — per-person memories are never carried, and its "
                "self-description is de-identified first) or 'default' (spec only, "
                "fresh self.md, baseline wiring)",
            )
        templates = personas_with_learned_state()
        from brain import persona_owners

        multi = persona_owners.multi_owner_personas()
        binding_ok = persona_owners.registry_available()
        new_mode, new_seed = org_settings.set_learning_mode("isolated", seed)
        _invalidate_process_caches()
        audit(
            "learning_mode_changed",
            actor,
            **{"from": cur_mode, "to": new_mode},
            instance_seed=new_seed,
            personas_with_learned_state=templates,
            multi_owner_personas=[m["persona"] for m in multi],
        )
        return {
            "learning_mode": new_mode,
            "instance_seed": new_seed,
            "previous": cur_mode,
            "changed": [
                "learning_mode",
                "instance_seed" if new_seed != cur_seed else None,
                "established_principle_injection: off",
                "cross_learning_writes: off",
                "dmn_roster: home persona only (≤ 60 s)",
                "self_authored_skills: off",
                "muscle_memory: home persona only",
                "sleep_scan_all_personas: bounded to the batch",
                "persona_ownership_binding: on for new sessions"
                if binding_ok
                else "persona_ownership_binding: NOT enforceable (apply migration 037)",
            ],
            "personas_with_learned_state": templates,
            "multi_owner_personas": multi,
            "hypotheses_present": hypotheses_present(),
            "hypotheses_note": (
                "hypotheses.json is retained but inert; DELETE /v1/org/hypotheses purges it"
                if hypotheses_present()
                else None
            ),
        }

    # → consolidated
    learned = personas_with_learned_state()
    if learned and not force:
        raise SwitchError(
            409,
            "isolated personas exist: purge or archive first "
            "(or pass force: true — no merge happens; they will now share principles)",
            personas=learned,
        )
    new_mode, new_seed = org_settings.set_learning_mode("consolidated", seed)
    _invalidate_process_caches()
    audit(
        "learning_mode_changed",
        actor,
        **{"from": cur_mode, "to": new_mode},
        instance_seed=new_seed,
        force=bool(force),
        personas_with_learned_state=learned,
    )
    return {
        "learning_mode": new_mode,
        "instance_seed": new_seed,
        "previous": cur_mode,
        "forced": bool(force),
        "changed": [
            "learning_mode",
            "instance_seed" if new_seed != cur_seed else None,
            "established_principle_injection: on",
            "cross_learning_writes: on",
            "dmn_roster: full-tier personas re-enter (≤ 60 s)",
            "persona_ownership_binding: kept, no longer enforced",
        ],
        "personas_with_learned_state": learned,
        "hypotheses_present": hypotheses_present(),
    }


def _invalidate_process_caches() -> None:
    """Best-effort: drop per-process caches so the switch is visible on the next
    turn (agents' answer_only/owning-mandate caches are unaffected; the DMN roster
    and the org row have their own 60 s TTLs)."""
    with contextlib.suppress(Exception):
        org_settings.invalidate()


def actor_from_ctx(ctx: dict | None, source: str = "api") -> dict:
    return {
        "source": source,
        "owner": bool((ctx or {}).get("owner")),
        "partner_id": (ctx or {}).get("partner_id"),
        "key_id": (ctx or {}).get("key_id"),
        "user": (ctx or {}).get("user") or (ctx or {}).get("email"),
    }


__all__ = [
    "SWITCH_FIELDS",
    "SwitchError",
    "actor_from_ctx",
    "audit",
    "audit_log_path",
    "describe",
    "hypotheses_present",
    "purge_hypotheses",
    "switch",
]
