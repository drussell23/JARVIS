"""Tests for CD-1 — lag-aware inter-chunk stream timeout.

Covers:
  * ``lag_compensated_inter_chunk_timeout_s`` helpers in stream_rupture.py
  * ``recent_lag_ms`` fail-soft helper in control_plane_watchdog.py
"""
from __future__ import annotations


def test_lag_compensation_off_returns_base(monkeypatch):
    monkeypatch.delenv("JARVIS_STREAM_LAG_COMPENSATION_ENABLED", raising=False)
    # Reload to pick up env change via the function (env is read at call time).
    from backend.core.ouroboros.governance.stream_rupture import (
        lag_compensated_inter_chunk_timeout_s,
    )
    assert lag_compensated_inter_chunk_timeout_s(base_s=30.0, lag_credit_s=25.0) == 30.0


def test_lag_compensation_credits_lag_capped(monkeypatch):
    monkeypatch.setenv("JARVIS_STREAM_LAG_COMPENSATION_ENABLED", "true")
    from backend.core.ouroboros.governance.stream_rupture import (
        lag_compensated_inter_chunk_timeout_s,
    )
    # Basic credit.
    assert lag_compensated_inter_chunk_timeout_s(base_s=30.0, lag_credit_s=25.0) == 55.0
    # Explicit cap honored.
    assert (
        lag_compensated_inter_chunk_timeout_s(
            base_s=30.0, lag_credit_s=999.0, max_credit_s=60.0
        )
        == 90.0
    )
    # Negative credit ignored (clamped to 0).
    assert lag_compensated_inter_chunk_timeout_s(base_s=30.0, lag_credit_s=-5.0) == 30.0


def test_lag_compensation_env_variants(monkeypatch):
    """Various truthy env values must enable compensation."""
    from backend.core.ouroboros.governance.stream_rupture import (
        stream_lag_compensation_enabled,
    )
    for val in ("1", "true", "yes", "on", "True", "YES"):
        monkeypatch.setenv("JARVIS_STREAM_LAG_COMPENSATION_ENABLED", val)
        assert stream_lag_compensation_enabled(), f"expected ON for {val!r}"
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("JARVIS_STREAM_LAG_COMPENSATION_ENABLED", val)
        assert not stream_lag_compensation_enabled(), f"expected OFF for {val!r}"


def test_lag_compensation_zero_credit(monkeypatch):
    """Zero lag credit returns base_s exactly."""
    monkeypatch.setenv("JARVIS_STREAM_LAG_COMPENSATION_ENABLED", "true")
    from backend.core.ouroboros.governance.stream_rupture import (
        lag_compensated_inter_chunk_timeout_s,
    )
    assert lag_compensated_inter_chunk_timeout_s(base_s=30.0, lag_credit_s=0.0) == 30.0


def test_recent_lag_ms_failsoft():
    """recent_lag_ms must return a non-negative float and never raise."""
    from backend.core.ouroboros.governance.control_plane_watchdog import recent_lag_ms
    v = recent_lag_ms()
    assert isinstance(v, float) and v >= 0.0


def test_recent_lag_ms_no_records():
    """When the ring is empty, recent_lag_ms returns 0.0."""
    from backend.core.ouroboros.governance.control_plane_watchdog import (
        ControlPlaneWatchdog,
        recent_lag_ms,
    )
    import unittest.mock as mock
    empty_watchdog = ControlPlaneWatchdog()
    with mock.patch(
        "backend.core.ouroboros.governance.control_plane_watchdog.get_default_watchdog",
        return_value=empty_watchdog,
    ):
        assert recent_lag_ms() == 0.0


def test_recent_lag_ms_windows_by_timestamp():
    """Only LagRecords within the window contribute to the sum."""
    import time
    import unittest.mock as mock
    from backend.core.ouroboros.governance.control_plane_watchdog import (
        ControlPlaneWatchdog,
        LagRecord,
        recent_lag_ms,
    )

    now = time.monotonic()
    old_record = LagRecord(
        lag_ms=500.0,
        requested_ms=100.0,
        observed_ms=600.0,
        ts_monotonic=now - 60.0,  # 60s old — outside default 10s window
        thread_name="test",
    )
    recent_record = LagRecord(
        lag_ms=200.0,
        requested_ms=100.0,
        observed_ms=300.0,
        ts_monotonic=now - 2.0,  # 2s old — inside window
        thread_name="test",
    )

    watchdog = ControlPlaneWatchdog()
    watchdog._ring.append(old_record)
    watchdog._ring.append(recent_record)

    with mock.patch(
        "backend.core.ouroboros.governance.control_plane_watchdog.get_default_watchdog",
        return_value=watchdog,
    ):
        result = recent_lag_ms(window_s=10.0)
    # Only the recent 200ms record should be counted.
    assert result == 200.0, f"Expected 200.0, got {result}"
