"""Provider Jitter Index + Adaptive Hysteresis — the anti-Flapping-Trap engine.

Mandated bulletproof: mock DW state transitions. Assert (1) a single
ClientPayloadError mid-probe increments the Jitter Index, (2) the required
consecutive-pass threshold dynamically scales up, (3) the Sentinel refuses to
flip to HEALTHY after only 2 passes while jitter is elevated, and (4) once
stability is maintained (jitter decays out of the window), the threshold decays
to baseline and HEALTHY is written atomically to SQLite.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.core.ouroboros.governance.provider_jitter import (
    is_transient_class,
    jitter_index,
    record_jitter_event,
    required_consecutive_passes,
)
from backend.core.ouroboros.governance.provider_state import (
    STATE_HEALTHY,
    get_provider_state,
    is_healthy,
)
from backend.core.ouroboros.governance.sentinel_daemon import run_sentinel


def _db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


# ---------------------------------------------------------------------------
# (1) A transient error increments the Jitter Index
# ---------------------------------------------------------------------------


async def test_client_payload_error_increments_jitter() -> None:
    conn = _db()
    assert jitter_index(conn, "doubleword", now=1000.0) == 0
    assert is_transient_class("dispatch_error:ClientPayloadError") is True

    record_jitter_event(conn, "doubleword", "ClientPayloadError", ts=990.0)
    assert jitter_index(conn, "doubleword", now=1000.0) == 1

    record_jitter_event(conn, "doubleword", "StreamRuptureError", ts=995.0)
    assert jitter_index(conn, "doubleword", now=1000.0) == 2


# ---------------------------------------------------------------------------
# (2) The required threshold dynamically scales with jitter
# ---------------------------------------------------------------------------


async def test_required_passes_scales_and_caps() -> None:
    conn = _db()
    now = 10_000.0
    # No jitter → baseline 2.
    assert required_consecutive_passes(conn, base=2, cap=5, now=now) == 2
    # Each recent flap raises the bar.
    record_jitter_event(conn, "doubleword", "ClientPayloadError", ts=now - 10)
    assert required_consecutive_passes(conn, base=2, cap=5, now=now) == 3
    record_jitter_event(conn, "doubleword", "ClientPayloadError", ts=now - 20)
    assert required_consecutive_passes(conn, base=2, cap=5, now=now) == 4
    # ...clamped to the ceiling.
    for i in range(5):
        record_jitter_event(conn, "doubleword", "StreamRuptureError", ts=now - 30 - i)
    assert required_consecutive_passes(conn, base=2, cap=5, now=now) == 5


async def test_jitter_decays_out_of_the_window() -> None:
    conn = _db()
    record_jitter_event(conn, "doubleword", "ClientPayloadError", ts=1000.0)
    # Within the 30-min window → counts.
    assert jitter_index(conn, "doubleword", window_s=1800, now=1500.0) == 1
    # Past the window → decayed to 0 (no timer, just the rolling window).
    assert jitter_index(conn, "doubleword", window_s=1800, now=3000.0) == 0
    assert required_consecutive_passes(conn, base=2, now=3000.0) == 2


# ---------------------------------------------------------------------------
# (3) Sentinel REFUSES HEALTHY at 2 passes while jitter is elevated
# ---------------------------------------------------------------------------


async def test_sentinel_refuses_healthy_while_jitter_elevated() -> None:
    conn = _db()
    JNOW = 50_000.0
    # Two fresh flaps → jitter 2 → required = 2 + 2 = 4.
    record_jitter_event(conn, "doubleword", "ClientPayloadError", ts=JNOW - 10)
    record_jitter_event(conn, "doubleword", "StreamRuptureError", ts=JNOW - 20)
    assert required_consecutive_passes(conn, base=2, now=JNOW) == 4

    # Exactly THREE consecutive healthy passes, then down — the streak tops out
    # at 3, one short of the jitter-elevated requirement of 4.
    seq = iter([True, True, True])

    async def three_then_down() -> bool:
        return next(seq, False)

    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        clock["t"] += max(1.0, s)

    result = await run_sentinel(
        conn, three_then_down, now_fn=lambda: clock["t"], sleep_fn=sleep,
        stability=2, adaptive_hysteresis=True, jitter_now_fn=lambda: JNOW,
        max_wait_s=1000.0,
    )
    # 3 passes < required 4 → REFUSED to flip while jitter elevated.
    assert result.recovered is False
    assert is_healthy(conn, "doubleword") is False

    # Contrast: the SAME 3-pass pattern with NO jitter (required=baseline 2) flips.
    clean = _db()
    seq2 = iter([True, True, True])

    async def three_clean() -> bool:
        return next(seq2, False)

    clock2 = {"t": 0.0}

    async def sleep2(s: float) -> None:
        clock2["t"] += max(1.0, s)

    r2 = await run_sentinel(
        clean, three_clean, now_fn=lambda: clock2["t"], sleep_fn=sleep2,
        stability=2, adaptive_hysteresis=True, jitter_now_fn=lambda: JNOW,
        max_wait_s=1000.0,
    )
    assert r2.recovered is True and r2.probes == 2   # baseline → flips at 2


# ---------------------------------------------------------------------------
# (4) Once jitter decays, threshold → baseline and HEALTHY is written atomically
# ---------------------------------------------------------------------------


async def test_sentinel_flips_healthy_after_jitter_decays() -> None:
    conn = _db()
    # An OLD flap, already outside the 30-min window at probe time → jitter 0.
    record_jitter_event(conn, "doubleword", "ClientPayloadError", ts=0.0)
    JNOW = 100_000.0   # far past the window → the old flap does not count
    assert required_consecutive_passes(conn, base=2, now=JNOW) == 2

    async def always_healthy() -> bool:
        return True

    clock = {"t": 0.0}

    async def sleep(s: float) -> None:
        clock["t"] += max(1.0, s)

    terminated = {"n": 0}

    result = await run_sentinel(
        conn, always_healthy, now_fn=lambda: clock["t"], sleep_fn=sleep,
        stability=2, adaptive_hysteresis=True, jitter_now_fn=lambda: JNOW,
        terminate=lambda: terminated.__setitem__("n", terminated["n"] + 1),
        max_wait_s=10_000.0,
    )
    # Baseline 2 passes suffice now → HEALTHY written + self-terminated.
    assert result.recovered is True
    assert result.probes == 2
    assert terminated["n"] == 1
    st = get_provider_state(conn, "doubleword")
    assert st["state"] == STATE_HEALTHY
    assert "jitter-adaptive" in st["reason"]
    assert is_healthy(conn, "doubleword") is True
