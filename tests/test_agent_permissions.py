"""
effective_permissions — the bounded "org ceiling ∩ agent narrowing" resolver.

The invariant under test: an agent can only ever NARROW within the org bounds,
never widen past them, and an empty agent dict reproduces the org exactly.
"""

from __future__ import annotations

from brain.agents import effective_permissions


def _org():
    return {
        "ralph_max_total_attempts": 12,
        "cloud_daily_usd_budget": 5.0,
        "motor_max_jobs_per_session": 30,
        "motor_enable_shell": 1,
        "motor_enable_network": 1,
        "motor_enable_cloud_actions": 1,
        "motor_user_cloud": "full",
        "motor_self_cloud": "ro",
        "motor_allowed_dirs": "/data/projects\n/data/scratch",
        "motor_read_only_dirs": "",
        "motor_allowed_commands": "git\nls\ncat",
        "motor_user_connectors": "",
        "motor_self_connectors": "slack\ngmail",
    }


def test_empty_agent_reproduces_org():
    org = _org()
    eff = effective_permissions(org, {})
    assert eff["ralph_max_total_attempts"] == 12
    assert eff["cloud_daily_usd_budget"] == 5.0
    assert eff["motor_enable_shell"] == 1
    assert eff["motor_user_cloud"] == "full"
    assert eff["motor_allowed_dirs"] == "/data/projects\n/data/scratch"
    assert eff["motor_allowed_commands"] == "git\nls\ncat"


def test_numeric_cap_takes_the_tighter():
    org = _org()
    # agent asks for less → honored
    assert effective_permissions(org, {"cloud_daily_usd_budget": 2.0})["cloud_daily_usd_budget"] == 2.0
    # agent asks for MORE → clamped to org (cannot widen)
    assert effective_permissions(org, {"cloud_daily_usd_budget": 100.0})["cloud_daily_usd_budget"] == 5.0
    # int caps stay ints
    eff = effective_permissions(org, {"ralph_max_total_attempts": 3})
    assert eff["ralph_max_total_attempts"] == 3 and isinstance(eff["ralph_max_total_attempts"], int)


def test_capability_flag_is_and():
    org = _org()
    # agent can turn a capability OFF
    assert effective_permissions(org, {"motor_enable_shell": 0})["motor_enable_shell"] == 0
    # agent CANNOT turn one ON that the org disabled
    org2 = {**org, "motor_enable_network": 0}
    assert effective_permissions(org2, {"motor_enable_network": 1})["motor_enable_network"] == 0


def test_cloud_level_most_restrictive():
    org = _org()
    assert effective_permissions(org, {"motor_user_cloud": "ro"})["motor_user_cloud"] == "ro"
    assert effective_permissions(org, {"motor_user_cloud": "off"})["motor_user_cloud"] == "off"
    # agent cannot escalate self cloud from ro to full
    assert effective_permissions(org, {"motor_self_cloud": "full"})["motor_self_cloud"] == "ro"


def test_dirs_must_be_inside_org_roots():
    org = _org()
    # a sub-dir of an org root is kept
    eff = effective_permissions(org, {"motor_allowed_dirs": "/data/projects/agentA"})
    assert eff["motor_allowed_dirs"] == "/data/projects/agentA"
    # a dir OUTSIDE every org root is dropped (cannot escape the ceiling)
    eff2 = effective_permissions(org, {"motor_allowed_dirs": "/etc\n/data/scratch"})
    assert eff2["motor_allowed_dirs"] == "/data/scratch"


def test_command_and_connector_intersection():
    org = _org()
    # commands: agent narrows to a subset of the org list
    eff = effective_permissions(org, {"motor_allowed_commands": "git\nrm"})
    assert eff["motor_allowed_commands"] == "git"  # rm not in org list → dropped
    # connectors: org empty (= all) so agent set passes; then a tighter agent set
    eff_u = effective_permissions(org, {"motor_user_connectors": "slack"})
    assert eff_u["motor_user_connectors"] == "slack"
    # self connectors: org limits to slack/gmail; agent asking for slack+jira keeps slack
    eff_s = effective_permissions(org, {"motor_self_connectors": "slack\njira"})
    assert eff_s["motor_self_connectors"] == "slack"
