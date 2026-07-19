"""
fragment_pool — Tier 1 structural-plasticity: the receptor/safety contract for
learned-attached capability fragments.

A "fragment" is a curated, already-screened skill (brain/skills_registry.live_skills)
that learning may ATTACH to an existing host cell as a per-persona wiring edge
``fragment.<skill_id> → <host_node>`` (brain/wiring.py). The system never authors
fragments — it only learns the WIRING. This module holds the two things that are NOT
learned and NOT stored on the edge: which hosts may receive a fragment (the safety
allowlist) and which receptor class a fragment presents.

SAFETY BY CONSTRUCTION. An attachment is admissible only to a NON-safety host cell:
  - HOST_RECEPTORS is an explicit allowlist — only names in it can ever be hosts.
  - SAFETY_NODES is a defense-in-depth denylist (temporal integrator inhibitor / motor
    tool planner / threat→GABA transform), so a careless future edit to HOST_RECEPTORS
    still cannot admit an inhibitor/motor/transform node.
  - is_admissible() additionally rejects any host that the node registry classifies as
    something other than a "cell" (e.g. a switch), so only object-backed integrator cells
    are ever hosts.

Fragments are CONTENT, not compute: the "fragment" node kind is NON-object-backed
(node_registry.resolve → None), so a fragment never counts as an UNWIRED object. The
consumer resolves a fragment's BODY via the skill selector (native_skill_body) and injects
it behind the existing untrusted-content fence.
"""

from __future__ import annotations

# Receptor classes. v1 was uniform ("draft_slot" for every approved partner skill); the
# stance library refines that — the refinement the original comment here anticipated:
#   DRAFT_SLOT  — procedural/operational skill fragments (partner pool), the original class.
#   INFO_SLOT   — information-posture stances (brain/skills/stance-*.md): how this turn
#                 should treat information need (answer from known, verify premise, ask…).
#   METHOD_SLOT — humanity reasoning leaves used as method stances (how to attack the
#                 problem). Same skill ids as the humanity library; the class is assigned
#                 by the stance machinery at injection time, not derivable from the id.
# Each class carries its OWN per-host budget (settings: fragment_max_per_host /
# stance_info_max_per_host / stance_method_max_per_host) so stances never contend with
# procedural skills for slots.
DRAFT_SLOT = "draft_slot"
INFO_SLOT = "info_slot"
METHOD_SLOT = "method_slot"

# Info-posture stances are first-party files named stance-*; the prefix IS the receptor
# signal (a stance id is minted by us, never by a partner — skills_registry ids are
# lowercase [a-z0-9_-] so a partner CAN mint "stance-x", which is why is_admissible gates
# INFO_SLOT to drafter hosts only and the injector additionally requires kind=="stance"
# from the index entry before rendering stance framing).
STANCE_PREFIX = "stance-"

FRAGMENT_KIND = "fragment"
FRAGMENT_PREFIX = "fragment."

# The drafters are the competitive substrate where EXPLORATION runs (parallel drafts,
# critic-scored). This covers the fixed 5 (A–E) AND the Tier 2 reserve slots (F.. up to the
# pool ceiling): a recruited reserve is a drafter and may carry/explore fragments. Listing the
# reserve slots here is harmless while they are dormant — they never fire until recruited (an
# executive→drafter_X edge). The other four frontal cells are admissible hosts too (they carry
# ESTABLISHED attachments) but do NOT explore HERE — there is no within-turn competition to
# differentiate a candidate on a cell that emits one opinion per turn. The two JUDGE hosts
# (critic, empathy_critic) instead explore in SHADOW off the live path, on cross-turn paired
# accuracy — see brain/judge_attachment.py, which also argues why executive/stoic_reframer
# are excluded from that producer.
_MAX_DRAFTER_SLOTS = 16  # A..P — a generous ceiling above node_reserve_pool
_ALL_DRAFTERS: frozenset[str] = frozenset(
    f"frontal.drafter_{chr(65 + i)}" for i in range(_MAX_DRAFTER_SLOTS)
)
EXPLORE_HOSTS: frozenset[str] = _ALL_DRAFTERS

# The safety ALLOWLIST: only these non-safety frontal cells may ever be attachment hosts.
# Every one is a kind="cell" integrator (see brain/clusters/frontal.py) — never a
# switch/inhibitor/motor node.
# The approach stage's single stance-credit anchor. Crediting fragment.<sid> edges to
# whichever interchangeable approach cell carried the winner would smear one stance's
# signal across meaningless hosts (3× dilution, and the pair-ledger residuals compound
# the damage) — so ALL approach-stage stance weight lives on this one bookkeeping node.
# It is NOT a firing cell: registered as a non-object "subsystem", exempted by name from
# the classify=="cell" check below, and it accepts ONLY the two stance classes.
APPROACH_ANCHOR = "frontal.approach_stage"

HOST_RECEPTORS: dict[str, frozenset[str]] = {
    # Drafters accept all three classes: they are the competitive substrate where both
    # procedural fragments and stances are explored and contrastively credited. The four
    # single-opinion cells stay DRAFT_SLOT-only — stances attach to drafters first (the
    # proven machinery); widening a judge's receptor set is a later, separate decision.
    **{h: frozenset({DRAFT_SLOT, INFO_SLOT, METHOD_SLOT}) for h in _ALL_DRAFTERS},
    "frontal.critic": frozenset({DRAFT_SLOT}),
    "frontal.stoic_reframer": frozenset({DRAFT_SLOT}),
    "frontal.empathy_critic": frozenset({DRAFT_SLOT}),
    "frontal.executive": frozenset({DRAFT_SLOT}),
    APPROACH_ANCHOR: frozenset({INFO_SLOT, METHOD_SLOT}),
}

# Defense-in-depth DENYLIST: nodes that must NEVER be hosts, even if a future edit adds one
# to HOST_RECEPTORS. Named safety nodes = the temporal integrator inhibitor, the motor tool
# planner, and the threat→GABA transform. (All switches are additionally excluded by the
# classify()=="cell" check in is_admissible.)
SAFETY_NODES: frozenset[str] = frozenset(
    {
        "temporal.integrator_inhibitor",
        "motor_cortex.tool_planner",
        "hypothalamus.threat_to_GABA",
    }
)


def fragment_node_name(skill_id: str) -> str:
    """Canonical graph node name for a fragment. Skill IDs are dot-free
    (^[a-z0-9][a-z0-9_-]{0,63}$), so "fragment.<skill_id>" splits cleanly into
    cluster="fragment", name="<skill_id>" — matching the wiring/registry conventions."""
    return f"{FRAGMENT_PREFIX}{skill_id}"


def is_fragment_node(name: str) -> bool:
    return name.startswith(FRAGMENT_PREFIX)


def skill_id_of(node_name: str) -> str:
    """Inverse of fragment_node_name: the skill id embedded in a fragment node name."""
    return node_name[len(FRAGMENT_PREFIX) :] if is_fragment_node(node_name) else node_name


def fragment_receptor(skill_id: str, kind: str | None = None) -> str:
    """The receptor class a fragment presents.

    Info-posture stances declare themselves by the stance- prefix. Method stances are
    ordinary humanity skill ids — indistinguishable from the id alone — so the stance
    machinery passes kind="method" explicitly at injection/budget time. Everything else
    (the partner pool) presents the original draft_slot."""
    if skill_id.startswith(STANCE_PREFIX):
        return INFO_SLOT
    if kind == "method":
        return METHOD_SLOT
    return DRAFT_SLOT


def is_admissible(skill_id: str, host_node: str, kind: str | None = None) -> bool:
    """True iff fragment `skill_id` may attach to `host_node`:
    host is on the allowlist AND accepts the fragment's receptor AND host is not a named
    safety node AND the node registry does not classify host as a non-cell.

    `kind="method"` classifies a humanity leaf as a method stance (METHOD_SLOT) —
    indistinguishable from the id alone; callers that drew from method_pool() pass it.

    The registry check is belt-and-suspenders: a *registered* switch/subsystem is rejected
    even if wrongly added to HOST_RECEPTORS, while an *unregistered* name (e.g. in a unit
    test that never built the frontal cluster) still passes the classify gate — the static
    allowlist remains the real boundary. The APPROACH_ANCHOR is the one deliberate
    non-cell host: a bookkeeping node for stance credit, exempted by NAME (not by kind)
    so no other subsystem can ride the exemption."""
    if host_node in SAFETY_NODES:
        return False
    accepted = HOST_RECEPTORS.get(host_node)
    if not accepted or fragment_receptor(skill_id, kind) not in accepted:
        return False
    if host_node == APPROACH_ANCHOR:
        return True
    try:
        from brain.node_registry import get_node_registry

        reg_kind = get_node_registry().classify(host_node)
        if reg_kind is not None and reg_kind != "cell":
            return False
    except Exception:
        # Registry unavailable (should not happen in a live brain) — the allowlist +
        # denylist above already exclude every safety node, so fall through as admissible.
        pass
    return True
