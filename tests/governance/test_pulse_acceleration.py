"""Proactive Health Gradient & Pulse Acceleration Engine.

Mandated bulletproof: mock DW state transitions. Assert (1) a successful Pass-1
probe triggers PULSE_ACCELERATION (15s interval), (2) 5 rapid accelerated probes
complete the N=5 requirement in record time, (3) a mid-pulse failure instantly
decelerates back to passive cadence, and (4) provider_state is written HEALTHY
with zero .git / process side-effects.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess

import pytest

from backend.core.ouroboros.governance.dw_outage_forecaster import (
    record_probe_ttft,
    ttft_slope,
)
from backend.core.ouroboros.governance.provider_jitter import (
    jitter_index,
    record_jitter_event,
    required_consecutive_passes,
)
from backend.core.ouroboros.governance.provider_state import (
    STATE_HEALTHY,
    get_provider_state,
)
from backend.core.ouroboros.governance.sentinel_daemon import (
    CADENCE_PASSIVE,
    CADENCE_PULSE,
    ProbeSignal,
    run_sentinel,
)


def _db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


# ---------------------------------------------------------------------------
# Health Gradient (ΔTTFT) primitive
# ---------------------------------------------------------------------------


async def test_ttft_slope_negative_when_stabilizing() -> None:
    conn = _db()
    # Falling TTFT across successive probes → negative slope (VRAM stabilizing).
    for i, ttft in enumerate([6.0, 5.0, 4.0, 3.0]):
        record_probe_ttft(conn, "doubleword", ttft, ts=1000.0 + i * 30)
    s = ttft_slope(conn, "doubleword", now=1200.0)
    assert s is not None and s < 0.0

    conn2 = _db()
    for i, ttft in enumerate([3.0, 4.0, 5.0]):   # rising → not stabilizing
        record_probe_ttft(conn2, "doubleword", ttft, ts=1000.0 + i * 30)
    assert ttft_slope(conn2, "doubleword", now=1200.0) > 0.0


# ---------------------------------------------------------------------------
# (1)+(2) Pass-1 triggers PULSE; 5 rapid accelerated probes complete N=5
# ---------------------------------------------------------------------------


async def test_pulse_accelerates_and_completes_n5(monkeypatch) -> None:
    # Zero side-effects guard: any git/subprocess call fails the test.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess.Popen"))
    monkeypatch.setattr(os, "system", lambda *a, **k: pytest.fail("os.system"))

    conn = _db()
    JNOW = 50_000.0
    # 5 recent flaps → required = base(2) + jitter(5) capped at 5.
    for i in range(5):
        record_jitter_event(conn, "doubleword", "no_tokens", ts=JNOW - 100 - i)
    assert required_consecutive_passes(conn, base=2, now=JNOW) == 5

    intervals = []
    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        intervals.append(s)
        clock["t"] += max(0.001, s)

    ttfts = iter([5.0, 4.0, 3.0, 2.0, 1.0])

    async def probe() -> ProbeSignal:
        return ProbeSignal(healthy=True, pass1_ok=True, ttft_s=next(ttfts, 1.0))

    terminated = {"n": 0}

    result = await run_sentinel(
        conn, probe, now_fn=lambda: clock["t"], sleep_fn=sleep,
        stability=2, adaptive_hysteresis=True, jitter_now_fn=lambda: JNOW,
        pulse_enabled=True, pulse_interval_s=15.0,
        terminate=lambda: terminated.__setitem__("n", terminated["n"] + 1),
        max_wait_s=10_000.0,
    )

    # (2) Exactly 5 rapid passes completed the N=5 requirement.
    assert result.recovered is True
    assert result.probes == 5
    assert result.cadence == CADENCE_PULSE

    # (1) After the first Pass-1 wake, the cadence dropped to the 15s burst.
    assert intervals[1] == 15.0 and intervals[2] == 15.0 and intervals[4] == 15.0

    # (4) HEALTHY written; terminate called; zero git/subprocess (guards above).
    assert terminated["n"] == 1
    st = get_provider_state(conn, "doubleword")
    assert st["state"] == STATE_HEALTHY
    assert "pulse" in st["reason"]


# ---------------------------------------------------------------------------
# (3) A mid-pulse failure instantly decelerates to passive cadence
# ---------------------------------------------------------------------------


async def test_mid_pulse_failure_decelerates_and_records_jitter() -> None:
    conn = _db()
    JNOW = 1_000.0
    intervals = []
    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        intervals.append(s)
        clock["t"] += max(0.001, s)

    seq = iter([
        ProbeSignal(healthy=True, pass1_ok=True, ttft_s=3.0),    # → PULSE
        ProbeSignal(healthy=False, pass1_ok=False, error_class="ClientPayloadError"),  # fail mid-pulse
    ])

    async def probe() -> ProbeSignal:
        return next(seq, ProbeSignal(healthy=False, pass1_ok=False, error_class="no_tokens"))

    result = await run_sentinel(
        conn, probe, now_fn=lambda: clock["t"], sleep_fn=sleep,
        stability=2, adaptive_hysteresis=True, jitter_now_fn=lambda: JNOW,
        pulse_enabled=True, pulse_interval_s=15.0, max_wait_s=300.0,
    )

    assert result.recovered is False
    # intervals[1] = 15 (pulse after the Pass-1 wake); intervals[2] = decelerated
    # forecast interval (NOT 15) once the mid-pulse ClientPayloadError landed.
    assert intervals[1] == 15.0
    assert intervals[2] != 15.0 and intervals[2] >= 30.0   # back to forecast floor
    # The Jitter Deceleration Guard recorded the flap.
    assert jitter_index(conn, "doubleword", now=JNOW) >= 1
    assert result.cadence == CADENCE_PASSIVE


async def test_bool_probe_still_supported_no_pulse_signal() -> None:
    # A legacy bool probe: healthy=True twice → flips at baseline 2, passive.
    conn = _db()
    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        clock["t"] += max(0.001, s)

    async def probe() -> bool:
        return True

    result = await run_sentinel(
        conn, probe, now_fn=lambda: clock["t"], sleep_fn=sleep,
        stability=2, adaptive_hysteresis=True, jitter_now_fn=lambda: 1.0,
        pulse_enabled=True, max_wait_s=10_000.0,
    )
    # bool healthy is treated as pass1_ok → pulse engages; flips at 2.
    assert result.recovered is True and result.probes == 2
