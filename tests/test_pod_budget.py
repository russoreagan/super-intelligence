"""GPU-pod demand gating and the daily uptime ceiling.

Background (the bug these guard): the gateway's pod reconciler gated on
`provisioner.full_count() > 0` — "is a full-tier brain process alive" — which is not
the question "does anything need a GPU". A 5-minute keepalive cron guarantees a live
brain, so that gate was permanently true, `ensure_running()` fired every 60s forever,
and the `pause()` branch beneath it was unreachable. The pod billed 144 hours straight
at $0.44/hr while serving ~90 seconds of inference a day.

tests/test_runpod_lifecycle.py already guards "don't run a pod with no consumer". It
did not catch this, because a consumer WAS alive the whole time — it just never wanted
the GPU. So these tests are about demand and affordability, not liveness.
"""

from __future__ import annotations

import datetime
import json

import pytest

import brain.pod_budget as pb
import brain.provisioner as pv


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Point both shared files at a temp dir — never touch a real tenant volume."""
    monkeypatch.setattr(pb, "_LEDGER", tmp_path / ".pod_budget.json")
    monkeypatch.setattr(pv, "POD_DEMAND_FILE", tmp_path / ".pod_demand")
    monkeypatch.setattr(pv, "_last_pod_demand_write", 0.0)


def _set_budget(monkeypatch, minutes):
    monkeypatch.setattr(pb, "budget_seconds", lambda: float(minutes) * 60.0)


# ── the demand channel (tenant → gateway) ──────────────────────────────────


def test_demand_age_is_none_before_anything_asks():
    # None must be distinguishable from "asked a long time ago": it also covers a
    # fresh deploy where the file does not exist yet.
    assert pv.pod_demand_age_s() is None


def test_note_pod_demand_records_a_fresh_timestamp():
    pv.note_pod_demand()
    age = pv.pod_demand_age_s()
    assert age is not None and age < 5.0


def test_note_pod_demand_is_throttled(monkeypatch):
    monkeypatch.setattr(pv, "POD_DEMAND_THROTTLE_S", 3600.0)
    pv.note_pod_demand()
    first = pv.POD_DEMAND_FILE.stat().st_mtime
    pv.note_pod_demand()  # inside the throttle window — must not rewrite
    assert pv.POD_DEMAND_FILE.stat().st_mtime == first


def test_demand_survives_a_missing_directory(tmp_path, monkeypatch):
    # The tenant volume mount can lag a boot; a failed touch must never raise into
    # an inference call, it may only delay a wake.
    monkeypatch.setattr(pv, "POD_DEMAND_FILE", tmp_path / "nope" / "deeper" / ".pod_demand")
    pv.note_pod_demand()  # creates parents rather than exploding
    assert pv.pod_demand_age_s() is not None


# ── the daily ledger ───────────────────────────────────────────────────────


def test_record_uptime_accumulates():
    assert pb.record_uptime(60) == pytest.approx(60)
    assert pb.record_uptime(30) == pytest.approx(90)
    assert pb.spent_seconds() == pytest.approx(90)


def test_ledger_resets_on_utc_day_rollover():
    pb.record_uptime(600)
    stale = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    pb._LEDGER.write_text(json.dumps({"date": stale, "seconds": 99999}), encoding="utf-8")
    assert pb.spent_seconds() == 0.0  # yesterday's burn is not today's budget


def test_ledger_survives_a_corrupt_file():
    # A half-written ledger must read as 0, not crash the reconciler. Failing OPEN
    # here is deliberate: the reconciler still bills forward from 0 this tick.
    pb._LEDGER.write_text("{not json", encoding="utf-8")
    assert pb.spent_seconds() == 0.0


def test_uptime_is_billed_not_usage(monkeypatch):
    # RunPod bills wall-clock uptime, so the ledger must too. This is the exact
    # distinction the original bug lived in: 144h billed, ~90s used.
    _set_budget(monkeypatch, 10)
    pb.record_uptime(9 * 60)
    assert not pb.exhausted()
    pb.record_uptime(2 * 60)
    assert pb.exhausted()


# ── the setting actually changes behaviour ─────────────────────────────────


def test_budget_zero_is_uncapped(monkeypatch):
    _set_budget(monkeypatch, 0)
    pb.record_uptime(10_000_000)
    assert pb.exhausted() is False
    assert pb.status()["uncapped"] is True


def test_flipping_the_budget_setting_flips_the_decision(monkeypatch):
    """The dial must be load-bearing, not decorative: same uptime, same demand, and
    only `pod_daily_minutes_budget` differs between held and slept."""
    pb.record_uptime(120 * 60)  # two hours used today
    args = {"full_tier_brains": 1, "demand_age_s": 5.0, "grace_s": 600.0}

    _set_budget(monkeypatch, 180)  # ceiling above usage → pod may run
    assert pb.should_hold_pod(**args, over_budget=pb.exhausted()) is True

    _set_budget(monkeypatch, 60)  # ceiling below usage → pod must sleep
    assert pb.should_hold_pod(**args, over_budget=pb.exhausted()) is False


def test_real_setting_is_declared_and_reaches_budget_seconds():
    """A settings.json key with no settings.py declaration is silently dropped and the
    dial becomes decorative. Assert the REAL accessor resolves the REAL declared
    default end-to-end — every other test here stubs budget_seconds(), so without this
    one the whole suite would pass with the setting never declared at all."""
    from brain import settings as settings_mod

    declared = settings_mod.DEFAULTS["pod_daily_minutes_budget"]
    assert declared > 0, "must ship enforcing, not uncapped — a ceiling, not an opt-in"
    assert pb.budget_seconds() == pytest.approx(float(declared) * 60)


# ── the wake/sleep decision ────────────────────────────────────────────────


def test_liveness_alone_does_not_hold_the_pod():
    """THE regression. A live full-tier brain with no demand must NOT hold the pod —
    this exact combination billed 144 idle hours."""
    assert (
        pb.should_hold_pod(full_tier_brains=3, demand_age_s=None, grace_s=600.0, over_budget=False)
        is False
    )


def test_stale_demand_does_not_hold_the_pod():
    assert (
        pb.should_hold_pod(full_tier_brains=1, demand_age_s=601.0, grace_s=600.0, over_budget=False)
        is False
    )


def test_fresh_demand_holds_the_pod():
    assert (
        pb.should_hold_pod(full_tier_brains=1, demand_age_s=30.0, grace_s=600.0, over_budget=False)
        is True
    )


def test_lite_only_host_never_holds_the_pod():
    # A lite brain remaps every local route to cloud; demand from one is not a
    # reason to spin a GPU it can never use.
    assert (
        pb.should_hold_pod(full_tier_brains=0, demand_age_s=1.0, grace_s=600.0, over_budget=False)
        is False
    )


def test_budget_beats_demand():
    # Over budget outranks live demand — otherwise the ceiling isn't a ceiling.
    assert (
        pb.should_hold_pod(full_tier_brains=5, demand_age_s=0.0, grace_s=600.0, over_budget=True)
        is False
    )


# ── the ops surface ────────────────────────────────────────────────────────


def test_status_reports_dollars_for_comparison_with_cloud_spend(monkeypatch):
    monkeypatch.setenv("RUNPOD_COST_PER_HR", "0.44")
    _set_budget(monkeypatch, 180)
    pb.record_uptime(3600)
    st = pb.status()
    assert st["minutes_used"] == pytest.approx(60.0)
    assert st["usd_today"] == pytest.approx(0.44, abs=1e-3)
    assert st["usd_budget"] == pytest.approx(1.32, abs=1e-2)
    assert st["exhausted"] is False


# ── skip accounting: why a runpod cell produced nothing ────────────────────


def test_pod_off_skip_is_counted_and_records_demand(monkeypatch):
    """A runpod cell with the pod asleep must (a) be counted, so a backed-off DMN is
    distinguishable from a broken one, and (b) still register demand — otherwise a
    slept pod is unwakeable: no demand → no wake → no demand."""
    import asyncio

    import brain.model_router as mr

    monkeypatch.setattr(mr, "_RUNPOD_SKIPPED", mr.Counter())
    from brain.settings import settings

    monkeypatch.setattr(settings, "get", lambda k, d=None: "off" if k == "runpod_host" else d)

    router = mr.ModelRouter.__new__(mr.ModelRouter)
    text, tin, tout = asyncio.run(
        mr.ModelRouter._call_local(
            router,
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="",
            local_variant="runpod",
        )
    )

    assert (text, tin, tout) == ("", 0, 0)  # degrades, never falls back to cloud
    assert mr.runpod_skip_counts() == {"pod_off": 1}
    age = pv.pod_demand_age_s()
    assert age is not None and age < 5.0, "the wake request must survive the early return"


def test_eager_warm_respects_the_budget(monkeypatch):
    """The eager warm is a SECOND route to ensure_running(), independent of the
    reconciler. Production sets BRAIN_TIER=full, so it fires on every login — if it
    skipped the ceiling, the ceiling would not be one."""
    import asyncio

    from brain.gateway.server import _safe_pod_ensure

    calls = []

    class _FakePod:
        async def ensure_running(self):
            calls.append(1)

    monkeypatch.setattr(pb, "exhausted", lambda: True)
    asyncio.run(_safe_pod_ensure(_FakePod()))
    assert calls == [], "over budget must not wake the pod, even on a fresh login"

    monkeypatch.setattr(pb, "exhausted", lambda: False)
    asyncio.run(_safe_pod_ensure(_FakePod()))
    assert calls == [1], "within budget the eager warm still works"
