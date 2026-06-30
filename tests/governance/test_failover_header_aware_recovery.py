"""Tests for header-aware DW-recovery (Task CR5).

When DW is rate-limited (429), the failover SERVING recovery probe must suspend
until the provider's OWN ``Retry-After`` / ``x-ratelimit-reset`` deadline instead
of a blind forecast interval -- then fall through to the EXISTING semantic deep
probe (untouched) which gates handback on real generation success.

ADDITIVE + DEFAULT-OFF: with ``JARVIS_FAILOVER_HEADER_AWARE_RECOVERY_ENABLED``
unset, ``_probe_interval`` is byte-identical (the new branch is skipped) and the
DoublewordInfraError ``ratelimit_reset_ts`` field defaults None. All boundaries
are injected fakes -> ZERO real GCP / network. The jitter backoff + deep probe
are REUSED, not rebuilt.
"""
from __future__ import annotations

import time
from email.utils import formatdate

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    AWAKEN_REASON_DATA_PLANE,
    AWAKEN_REASON_RATE_LIMIT,
    FailoverLifecycleController,
)
from backend.core.ouroboros.governance.doubleword_provider import (
    DoublewordInfraError,
    _parse_retry_after_headers,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    monkeypatch.delenv("JARVIS_FAILOVER_HEADER_AWARE_RECOVERY_ENABLED", raising=False)
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _make_ctrl(clock, **kw):
    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        is_degrading_fn=lambda: False,
        flare_fn=lambda payload: None,
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


# ---------------------------------------------------------------------------
# Change 1 -- header parse helper
# ---------------------------------------------------------------------------

def test_parse_retry_after_seconds():
    now = time.time()
    ts = _parse_retry_after_headers({"Retry-After": "30"})
    assert ts is not None
    assert now + 28 <= ts <= now + 32


def test_parse_retry_after_http_date():
    now = time.time()
    # HTTP-date 60s in the future.
    http_date = formatdate(now + 60, usegmt=True)
    ts = _parse_retry_after_headers({"Retry-After": http_date})
    assert ts is not None
    # HTTP-date has 1s resolution.
    assert now + 57 <= ts <= now + 63


def test_parse_x_ratelimit_reset_unix_ts():
    now = time.time()
    target = now + 45
    ts = _parse_retry_after_headers({"x-ratelimit-reset": str(target)})
    assert ts is not None
    assert abs(ts - target) <= 1.0


def test_parse_x_ratelimit_reset_small_delta():
    now = time.time()
    # A small value (< 1e6) is treated as delta-seconds, not an absolute epoch.
    ts = _parse_retry_after_headers({"x-ratelimit-reset": "20"})
    assert ts is not None
    assert now + 18 <= ts <= now + 22


def test_parse_soonest_future_wins():
    now = time.time()
    ts = _parse_retry_after_headers(
        {"Retry-After": "90", "x-ratelimit-reset": str(now + 10)}
    )
    assert ts is not None
    # Soonest sane future deadline (the x-ratelimit-reset @ now+10) wins.
    assert now + 8 <= ts <= now + 12


def test_parse_garbage_and_empty_return_none():
    assert _parse_retry_after_headers({}) is None
    assert _parse_retry_after_headers(None) is None
    assert _parse_retry_after_headers({"Retry-After": "not-a-number"}) is None
    assert _parse_retry_after_headers({"Retry-After": ""}) is None
    assert _parse_retry_after_headers({"x-ratelimit-reset": "abc"}) is None


def test_parse_past_values_return_none():
    now = time.time()
    # Past delta-seconds is impossible; past absolute reset must be dropped.
    assert _parse_retry_after_headers({"x-ratelimit-reset": str(now - 100)}) is None
    # Retry-After negative delta -> not a future deadline.
    assert _parse_retry_after_headers({"Retry-After": "-5"}) is None


def test_infra_error_carries_reset_ts_field():
    # Additive field defaults None for existing callers.
    err_default = DoublewordInfraError("boom", status_code=429)
    assert err_default.ratelimit_reset_ts is None
    # Explicit value is preserved.
    err = DoublewordInfraError("boom", status_code=429, ratelimit_reset_ts=12345.0)
    assert err.ratelimit_reset_ts == 12345.0


# ---------------------------------------------------------------------------
# Change 2a -- note_rate_limited anchor
# ---------------------------------------------------------------------------

def test_note_rate_limited_sets_deadline(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    fixed_now = 5000.0
    monkeypatch.setattr(fl.time, "time", lambda: fixed_now)

    assert ctrl._rate_limit_reset_ts is None

    # Future deadline -> anchored.
    ctrl.note_rate_limited(fixed_now + 60)
    assert ctrl._rate_limit_reset_ts == fixed_now + 60

    # Past deadline -> ignored (anchor unchanged).
    ctrl.note_rate_limited(fixed_now - 10)
    assert ctrl._rate_limit_reset_ts == fixed_now + 60

    # None -> ignored.
    ctrl.note_rate_limited(None)
    assert ctrl._rate_limit_reset_ts == fixed_now + 60


# ---------------------------------------------------------------------------
# Change 2b -- header-aware _probe_interval branch
# ---------------------------------------------------------------------------

def test_probe_interval_header_aware(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    monkeypatch.setenv("JARVIS_FAILOVER_HEADER_AWARE_RECOVERY_ENABLED", "true")
    fixed_now = 5000.0
    monkeypatch.setattr(fl.time, "time", lambda: fixed_now)

    ctrl._awaken_reason = AWAKEN_REASON_RATE_LIMIT
    ctrl._rate_limit_reset_ts = fixed_now + 50

    interval = ctrl._probe_interval(now=clock.t)
    assert abs(interval - 50.0) <= 0.5


def test_probe_interval_flag_off_falls_through(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    # Flag OFF (default) -> header-aware branch skipped -> forecast interval.
    fixed_now = 5000.0
    monkeypatch.setattr(fl.time, "time", lambda: fixed_now)
    ctrl._awaken_reason = AWAKEN_REASON_RATE_LIMIT
    ctrl._rate_limit_reset_ts = fixed_now + 50

    interval = ctrl._probe_interval(now=clock.t)
    # Must NOT return the ~50s header deadline; uses the normal jitter interval.
    assert not (abs(interval - 50.0) <= 0.5)
    assert interval >= 0.0


def test_probe_interval_deadline_passed_clears_and_resumes(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    monkeypatch.setenv("JARVIS_FAILOVER_HEADER_AWARE_RECOVERY_ENABLED", "true")
    fixed_now = 5000.0
    monkeypatch.setattr(fl.time, "time", lambda: fixed_now)
    ctrl._awaken_reason = AWAKEN_REASON_RATE_LIMIT
    # Deadline already in the past.
    ctrl._rate_limit_reset_ts = fixed_now - 5

    interval = ctrl._probe_interval(now=clock.t)
    # Anchor cleared -> resume normal probing (NOT a negative/zero header sleep).
    assert ctrl._rate_limit_reset_ts is None
    assert interval >= 0.0


def test_data_plane_uses_jitter_unchanged(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    monkeypatch.setenv("JARVIS_FAILOVER_HEADER_AWARE_RECOVERY_ENABLED", "true")
    fixed_now = 5000.0
    monkeypatch.setattr(fl.time, "time", lambda: fixed_now)
    # Non-rate-limit reason -> header-aware path skipped even with flag ON.
    ctrl._awaken_reason = AWAKEN_REASON_DATA_PLANE
    ctrl._rate_limit_reset_ts = fixed_now + 50

    interval = ctrl._probe_interval(now=clock.t)
    assert not (abs(interval - 50.0) <= 0.5)
    assert ctrl._rate_limit_reset_ts == fixed_now + 50  # untouched


# ---------------------------------------------------------------------------
# Change 2c -- _enter_awakening reason override
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enter_awakening_rate_limit_anchor_sets_reason(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    monkeypatch.setattr(fl.time, "time", lambda: clock.t)
    ctrl._rate_limit_reset_ts = clock.t + 60

    # Even a data-plane trigger is overridden to RATE_LIMIT when the anchor lives.
    await ctrl._enter_awakening(now=clock.t, trigger="reactive_outage", route="dw")
    assert ctrl._awaken_reason == AWAKEN_REASON_RATE_LIMIT
