"""Headless Sentinel daemon — SQLite IPC Single-Writer handoff.

Mandated bulletproof: the Sentinel passes the 2-stage check, writes HEALTHY to
the SQLite provider_state table, and self-terminates — while executing ZERO git
/ os.system / subprocess mutation calls (it holds no such privilege).

Plus: provider_state upsert/read CRUD; the orchestrator poll (is_healthy); and
the daemon staying DEGRADED (no HEALTHY write, no terminate) when DW never
recovers within the wait budget.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess

import pytest

from backend.core.ouroboros.governance.provider_state import (
    STATE_DEGRADED,
    STATE_HEALTHY,
    get_provider_state,
    is_healthy,
    mark_degraded,
    mark_healthy,
)
from backend.core.ouroboros.governance.sentinel_daemon import run_sentinel


def _db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


# ---------------------------------------------------------------------------
# provider_state IPC CRUD
# ---------------------------------------------------------------------------


async def test_provider_state_upsert_and_read() -> None:
    conn = _db()
    assert get_provider_state(conn, "doubleword") is None
    mark_degraded(conn, "doubleword", reason="outage", ts=1000.0)
    st = get_provider_state(conn, "doubleword")
    assert st["state"] == STATE_DEGRADED and st["updated_ts"] == 1000.0
    assert is_healthy(conn, "doubleword") is False

    # Upsert (same PK) flips the state in place — one row per provider.
    mark_healthy(conn, "doubleword", reason="recovered", ts=2000.0)
    st2 = get_provider_state(conn, "doubleword")
    assert st2["state"] == STATE_HEALTHY and st2["updated_ts"] == 2000.0
    assert is_healthy(conn, "doubleword") is True
    n = conn.execute("SELECT COUNT(*) FROM provider_state").fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------------
# The mandated case: HEALTHY write + self-terminate + ZERO git/os mutation
# ---------------------------------------------------------------------------


async def test_sentinel_writes_healthy_terminates_and_makes_no_mutation(monkeypatch) -> None:
    # Hard guard: ANY git/os.system/subprocess call fails the test outright.
    monkeypatch.setattr(os, "system", lambda *a, **k: pytest.fail("os.system called by Sentinel"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess.run called by Sentinel"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess.Popen called by Sentinel"))
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: pytest.fail("subprocess.call called by Sentinel"))
    if hasattr(os, "popen"):
        monkeypatch.setattr(os, "popen", lambda *a, **k: pytest.fail("os.popen called by Sentinel"))

    conn = _db()
    probes = {"n": 0}

    async def staged_probe() -> bool:
        # Simulates the 2-stage check passing — HEALTHY every probe.
        probes["n"] += 1
        return True

    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    async def sleep(s: float) -> None:
        clock["t"] += s

    terminated = {"called": False}

    def terminate() -> None:
        terminated["called"] = True

    result = await run_sentinel(
        conn, staged_probe, now_fn=now, sleep_fn=sleep, terminate=terminate,
        stability=2, max_wait_s=100_000.0,
    )

    # HEALTHY written to SQLite with a timestamp (the ONLY mutation).
    assert result.recovered is True
    st = get_provider_state(conn, "doubleword")
    assert st["state"] == STATE_HEALTHY
    assert st["updated_ts"] is not None
    assert "staged_2pass_ok" in st["reason"]

    # Self-terminated after the write.
    assert terminated["called"] is True

    # It required 2 consecutive healthy probes (stability gate).
    assert probes["n"] == 2
    # Zero git/os/subprocess mutation — proven by the fail-guards never firing.


async def test_sentinel_stays_degraded_when_never_recovers(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no launch on non-recovery"))
    conn = _db()

    async def always_down() -> bool:
        return False

    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        clock["t"] += max(1.0, s)   # ensure the wait budget is consumed

    terminated = {"called": False}

    result = await run_sentinel(
        conn, always_down, now_fn=lambda: clock["t"], sleep_fn=sleep,
        terminate=lambda: terminated.__setitem__("called", True),
        stability=2, max_wait_s=500.0,
    )
    assert result.recovered is False
    assert terminated["called"] is False           # never handed off
    assert is_healthy(conn, "doubleword") is False  # stayed DEGRADED
    assert get_provider_state(conn, "doubleword")["state"] == STATE_DEGRADED


async def test_flapping_recovery_requires_consecutive_stability(monkeypatch) -> None:
    """A single healthy blip does NOT trip the handoff — needs `stability` in a
    row (defends against a lone lucky probe on a still-thrashing lane)."""
    conn = _db()
    seq = iter([True, False, True, True])   # blip, drop, then stable x2

    async def flapping() -> bool:
        try:
            return next(seq)
        except StopIteration:
            return True

    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        clock["t"] += s

    healthy_at = {"probe": None}

    result = await run_sentinel(
        conn, flapping, now_fn=lambda: clock["t"], sleep_fn=sleep,
        stability=2, max_wait_s=100_000.0,
    )
    assert result.recovered is True
    # Probes: T(streak1), F(reset), T(streak1), T(streak2 → write) = 4 probes.
    assert result.probes == 4
