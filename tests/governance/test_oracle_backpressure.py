"""Phase 1 — Adaptive Backpressure for the Oracle cold index.

Cures the ControlPlaneStarvation cold-boot wedge: AIMD throttle on the index batch
loop so background indexing yields cores to the FSM control plane.
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.oracle as O


def _throttle(max_batch=50, min_batch=4, lag=50.0):
    return O._AdaptiveIndexThrottle(max_batch=max_batch, min_batch=min_batch, lag_threshold_ms=lag)


# --------------------------------------------------------------------------- AIMD
def test_decreases_multiplicatively_on_lag():
    t = _throttle(max_batch=50)
    assert t.batch == 50
    assert t.update(200.0) == 25     # > threshold → halve
    assert t.update(200.0) == 12
    assert t.update(200.0) == 6


def test_increases_additively_when_responsive():
    t = _throttle(max_batch=64)
    t.update(500.0)  # drop to 32
    b1 = t.batch
    nb = t.update(5.0)               # below threshold → +max//8 (=8)
    assert nb == b1 + 8


def test_floor_never_below_min():
    t = _throttle(max_batch=50, min_batch=4)
    for _ in range(20):
        t.update(999.0)
    assert t.batch == 4              # clamped at the floor


def test_ceiling_never_above_max():
    t = _throttle(max_batch=50)
    for _ in range(50):
        t.update(0.0)
    assert t.batch == 50            # clamped at the ceiling


def test_backoff_proportional_and_capped():
    t = _throttle(lag=50.0)
    assert t.backoff_s(40.0) == 0.0                 # below threshold → no backoff
    assert t.backoff_s(150.0) == pytest.approx(0.1)  # (150-50)/1000
    assert t.backoff_s(10_000.0) == 0.5             # capped


def test_aimd_recovery_cycle():
    t = _throttle(max_batch=64, min_batch=4)
    t.update(300.0); t.update(300.0)               # decrease under load
    low = t.batch
    for _ in range(20):
        t.update(1.0)                               # sustained calm → recover to max
    assert t.batch == 64 and low < 64


# --------------------------------------------------------------------------- probe + flag
def test_lag_probe_nonnegative():
    # the probe is pure timing — doesn't touch self; call it unbound with self=None
    lag = asyncio.run(O.TheOracle._measure_loop_lag_ms(None, probe_s=0.01))
    assert isinstance(lag, float) and lag >= 0.0


def test_backpressure_enabled_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_BACKPRESSURE_ENABLED", raising=False)
    assert O._oracle_backpressure_enabled() is True


def test_backpressure_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_BACKPRESSURE_ENABLED", "0")
    assert O._oracle_backpressure_enabled() is False


def test_lag_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_BACKPRESSURE_LAG_MS", "120")
    assert O._oracle_backpressure_lag_ms() == 120.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
