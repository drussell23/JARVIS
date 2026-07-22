"""Predictive DW Outage Forecaster — EMA/Bayesian TTR + AIMD slow-start.

Mandated bulletproof (async):
  1. The forecaster computes a DYNAMICALLY-adjusting TTR-driven sleep interval
     from historical outages (mocked SQLite).
  2. The sleep is strictly bounded by the ceiling guardrail (a huge predicted
     TTR can never blow past it, and the cold start never sleeps indefinitely).
  3. On simulated recovery the swarm semaphore INITIALIZES at 1 and scales up
     progressively (TCP-style slow-start), capped at the ceiling.

Plus: cold-start returns the prior (never 0/NaN), Bayesian shrinkage toward the
prior with sparse data, and the end-to-end watch loop records the outage
lifecycle and returns a ramped AIMD controller.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.core.ouroboros.governance.dw_outage_forecaster import (
    AIMDController,
    forecast_ttr,
    predict_sleep_s,
    record_outage_start,
    record_recovery,
    watch_for_recovery,
)


def _db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


def _seed(conn: sqlite3.Connection, ttrs) -> None:
    """Seed completed outages with the given TTRs (monotonic start stamps)."""
    for i, ttr in enumerate(ttrs):
        start = 1000.0 + i * 10_000.0
        record_outage_start(conn, start)
        record_recovery(conn, start, start + ttr)


# ---------------------------------------------------------------------------
# (1) Dynamically-adjusting forecast
# ---------------------------------------------------------------------------


async def test_forecast_reflects_history_and_sleep_adjusts_dynamically() -> None:
    conn = _db()
    _seed(conn, [200.0, 200.0, 200.0])  # steady ~200s outages
    ttr = forecast_ttr(conn, prior_mean=120.0, warmup_k=3)
    # EMA≈200 shrunk toward prior 120 by conf=3/6=0.5 → ≈160.
    assert 150.0 <= ttr <= 200.0

    # The sleep interval SHRINKS as the outage runs longer (dynamic, not static).
    early = predict_sleep_s(conn, elapsed_outage_s=0.0, floor_s=30.0, ceiling_s=600.0)
    late = predict_sleep_s(conn, elapsed_outage_s=ttr - 10.0, floor_s=30.0, ceiling_s=600.0)
    assert early > late
    assert late == 30.0   # past ~expected recovery → floor cadence


async def test_cold_start_returns_prior_not_zero() -> None:
    conn = _db()  # no history
    ttr = forecast_ttr(conn, prior_mean=120.0)
    assert ttr == 120.0
    # And the sleep is a sane bounded value, never 0 or infinite.
    s = predict_sleep_s(conn, 0.0, floor_s=30.0, ceiling_s=600.0)
    assert 30.0 <= s <= 600.0


async def test_bayesian_shrinkage_tames_single_anomaly() -> None:
    conn = _db()
    _seed(conn, [5000.0])   # one wild outage
    # With n=1, conf=1/(1+3)=0.25 → mostly the prior, not the anomaly.
    ttr = forecast_ttr(conn, prior_mean=120.0, warmup_k=3)
    assert ttr < 1500.0, ttr   # the lone 5000s sample did NOT dominate


# ---------------------------------------------------------------------------
# (2) Ceiling guardrail
# ---------------------------------------------------------------------------


async def test_sleep_bounded_by_ceiling() -> None:
    conn = _db()
    _seed(conn, [5000.0, 5000.0, 5000.0, 5000.0, 5000.0])  # long, consistent outages
    ttr = forecast_ttr(conn, prior_mean=120.0)
    assert ttr > 1000.0   # forecast is large...
    # ...but the sleep is CLAMPED at the ceiling — never indefinite.
    s = predict_sleep_s(conn, elapsed_outage_s=0.0, floor_s=30.0, ceiling_s=600.0)
    assert s == 600.0


async def test_sleep_never_below_floor() -> None:
    conn = _db()
    _seed(conn, [40.0, 40.0])   # very short outages
    s = predict_sleep_s(conn, elapsed_outage_s=1000.0, floor_s=30.0, ceiling_s=600.0)
    assert s == 30.0   # way past expected recovery → floor, never sub-floor spam


# ---------------------------------------------------------------------------
# (3) AIMD slow-start
# ---------------------------------------------------------------------------


async def test_aimd_slow_start_initializes_at_one_and_ramps() -> None:
    aimd = AIMDController(max_limit=8, floor=1)
    assert aimd.limit == 1                     # slow-start begins at ONE

    ramp = [aimd.on_success() for _ in range(5)]
    assert ramp == [2, 3, 4, 5, 6]             # additive increase, +1 each

    # Multiplicative decrease on a failure (a recovering server hiccup).
    assert aimd.on_failure() == 3              # 6 // 2

    # Additive increase never exceeds the ceiling.
    for _ in range(20):
        aimd.on_success()
    assert aimd.limit == 8                      # capped at max_limit

    # Failures never fall below the floor.
    for _ in range(20):
        aimd.on_failure()
    assert aimd.limit == 1


async def test_aimd_defaults_ceiling_to_swarm_concurrency() -> None:
    aimd = AIMDController()   # no max_limit → swarm_concurrency()
    assert aimd.limit == 1
    assert aimd.max_limit >= 1


# ---------------------------------------------------------------------------
# End-to-end watch loop (deterministic clock / sleep / probe)
# ---------------------------------------------------------------------------


async def test_watch_loop_records_lifecycle_and_returns_ramped_aimd() -> None:
    conn = _db()
    _seed(conn, [100.0, 100.0])

    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    async def sleep(s: float) -> None:
        clock["t"] += s   # advance the virtual clock by the predicted interval

    probes = {"n": 0}

    async def probe() -> bool:
        probes["n"] += 1
        return probes["n"] >= 3   # down for 2 probes, then recovers

    outcome = await watch_for_recovery(
        conn, probe, now_fn=now, sleep_fn=sleep, max_probes=10,
    )
    assert outcome.recovered is True
    assert outcome.probes == 3
    assert outcome.ttr_s is not None and outcome.ttr_s > 0
    # Slow-start: the returned controller is at limit 1 (start) bumped once on
    # the confirming success → ready to ramp, not slammed at max.
    assert outcome.aimd.limit <= 2

    # The outage was persisted → future forecasts can learn from it.
    n = conn.execute(
        "SELECT COUNT(*) FROM dw_outage_history WHERE ttr_s IS NOT NULL"
    ).fetchone()[0]
    assert n == 3   # 2 seeded + this one


async def test_watch_loop_gives_up_after_max_probes_if_never_recovers() -> None:
    conn = _db()
    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        clock["t"] += s

    async def probe() -> bool:
        return False   # never recovers

    outcome = await watch_for_recovery(
        conn, probe, now_fn=lambda: clock["t"], sleep_fn=sleep, max_probes=4,
    )
    assert outcome.recovered is False
    assert outcome.probes == 4
