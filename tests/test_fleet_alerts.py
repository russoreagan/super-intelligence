"""brain/fleet_alerts — content-free health rules for the Fleet console."""

from __future__ import annotations

from brain import fleet_alerts as fa

NOW = 1_800_000_000.0


def test_stuck_jobs_rules():
    jobs = [
        {"job_id": "r1", "state": "running", "updated_at": NOW - 10 * 60},
        {"job_id": "r2", "state": "running", "updated_at": NOW - 45 * 60},
        {"job_id": "a1", "state": "awaiting_approval", "updated_at": NOW - 2 * 86400},
        {"job_id": "d1", "state": "completed", "updated_at": NOW - 5 * 86400},
        {"job_id": "iso", "state": "running", "updated_at": "2020-01-01T00:00:00Z"},
    ]
    ids = {s["job_id"] for s in fa.stuck_jobs(jobs, now=NOW)}
    assert ids == {"r2", "a1", "iso"}


def test_evaluate_rules_and_worst():
    signals = {
        "breaker": {"anthropic": {"kind": "auth"}},
        "dmn": {"dormant": True, "roster": {"size": 300, "cadence_s": 1500}},
        "stuck_jobs": [{"job_id": "x"}],
        "pod_budget": {"exhausted": True},
        "capacity": {"personas": 4600, "max_personas": 5000},
        "partners": [
            {"partner_id": "pf", "over_budget": True},
            {"partner_id": "ok", "over_budget": False},
        ],
        "multi_owner": 2,
        "unmetered_spend": 3,
        "roster_cadence_warn_s": 600,
    }
    codes = {a["code"]: a for a in fa.evaluate(signals, now=NOW)}
    assert set(codes) == {
        "breaker_open",
        "org_dormant",
        "roster_cadence_high",
        "stuck_jobs",
        "pod_budget_exhausted",
        "clone_cap_near",
        "partner_over_budget",
        "multi_owner",
        "unmetered_spend",
    }
    assert (
        codes["breaker_open"]["severity"] == "crit"
        and codes["partner_over_budget"]["subject"] == "pf"
    )
    assert fa.worst(list(codes.values())) == "crit"
    assert fa.worst([]) == "ok"
    assert fa.evaluate({}) == []
    # No alert carries free text beyond the fixed hint vocabulary.
    for a in codes.values():
        assert set(a) == {"code", "severity", "subject", "count", "hint"}


def test_cap_becomes_critical_at_the_cap():
    a = fa.evaluate({"capacity": {"personas": 5000, "max_personas": 5000}})
    assert a[0]["code"] == "clone_cap_near" and a[0]["severity"] == "crit"
    assert fa.evaluate({"capacity": {"personas": 10, "max_personas": 0}}) == []
