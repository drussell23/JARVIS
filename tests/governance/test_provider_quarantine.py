"""Tests for provider_quarantine.py — rolling success-rate gradient.

All tests written FIRST (TDD RED phase) before any implementation exists.

Covers:
- Window fills, all False -> is_global_outage True
- Window all False but not yet full -> False (need full window)
- One True in window -> success_rate > 0 -> is_global_outage False (recovery)
- Empty window -> rate 1.0, outage False
- JARVIS_QUARANTINE_WINDOW=3 env-tunable (not hardcoded)
- quarantine_enabled default true
- quarantine_op with fake ctx + monkeypatched callees -> True + both called
- quarantine_op fail-soft (raising append_dlq -> returns False, never raises)
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers to ensure a clean singleton between tests
# ---------------------------------------------------------------------------

def _fresh_module() -> object:
    """Import (or re-import) provider_quarantine with current env."""
    # Remove cached module so env changes are picked up between tests.
    for key in list(sys.modules.keys()):
        if "provider_quarantine" in key:
            del sys.modules[key]

    import importlib
    return importlib.import_module(
        "backend.core.ouroboros.governance.provider_quarantine"
    )


# ---------------------------------------------------------------------------
# 1. Empty window -> success_rate 1.0, is_global_outage False
# ---------------------------------------------------------------------------

def test_empty_window_rate_is_one_and_no_outage(monkeypatch):
    """Empty window: assume healthy (rate=1.0, outage=False)."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    mod = _fresh_module()
    g = mod.ProviderHealthGradient()
    assert g.success_rate("STANDARD") == 1.0
    assert g.is_global_outage("STANDARD") is False


# ---------------------------------------------------------------------------
# 2. Window all-False but not yet full -> is_global_outage False
# ---------------------------------------------------------------------------

def test_partial_window_all_failures_not_outage(monkeypatch):
    """A partial window (< maxlen) of all failures is NOT yet an outage."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    mod = _fresh_module()
    g = mod.ProviderHealthGradient()
    route = "IMMEDIATE"
    # Push 4 failures (one short of window=5)
    for _ in range(4):
        g.record_sweep(route, success=False)
    assert g.success_rate(route) == 0.0
    # Not an outage yet — window not full
    assert g.is_global_outage(route) is False


# ---------------------------------------------------------------------------
# 3. Window fills then all-False -> is_global_outage True
# ---------------------------------------------------------------------------

def test_full_window_all_failures_is_outage(monkeypatch):
    """Once the window is full and every entry is False, declare outage."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    mod = _fresh_module()
    g = mod.ProviderHealthGradient()
    route = "STANDARD"
    for _ in range(5):
        g.record_sweep(route, success=False)
    assert g.success_rate(route) == 0.0
    assert g.is_global_outage(route) is True


# ---------------------------------------------------------------------------
# 4. One True in full window -> success_rate > 0 -> is_global_outage False
# ---------------------------------------------------------------------------

def test_one_success_in_full_window_clears_outage(monkeypatch):
    """A single success inside the window keeps success_rate > 0 -> no outage."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    mod = _fresh_module()
    g = mod.ProviderHealthGradient()
    route = "COMPLEX"
    # 4 failures then one success
    for _ in range(4):
        g.record_sweep(route, success=False)
    g.record_sweep(route, success=True)
    assert g.success_rate(route) > 0.0
    assert g.is_global_outage(route) is False


# ---------------------------------------------------------------------------
# 5. JARVIS_QUARANTINE_WINDOW=3 is honoured (env-tunable)
# ---------------------------------------------------------------------------

def test_env_window_size_3_honoured(monkeypatch):
    """JARVIS_QUARANTINE_WINDOW=3 -> outage after exactly 3 failures, not 5."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "3")
    mod = _fresh_module()
    g = mod.ProviderHealthGradient()
    route = "BACKGROUND"
    # 2 failures: not yet full (window=3)
    g.record_sweep(route, success=False)
    g.record_sweep(route, success=False)
    assert g.is_global_outage(route) is False, "2/3 failures should not be outage"
    # 3rd failure: window full + all false -> outage
    g.record_sweep(route, success=False)
    assert g.is_global_outage(route) is True


# ---------------------------------------------------------------------------
# 6. reset() clears the window
# ---------------------------------------------------------------------------

def test_reset_clears_window(monkeypatch):
    """reset() must wipe the recorded window so the route starts fresh."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "3")
    mod = _fresh_module()
    g = mod.ProviderHealthGradient()
    route = "STANDARD"
    for _ in range(3):
        g.record_sweep(route, success=False)
    assert g.is_global_outage(route) is True
    g.reset(route)
    # After reset: empty window -> rate 1.0, no outage
    assert g.success_rate(route) == 1.0
    assert g.is_global_outage(route) is False


# ---------------------------------------------------------------------------
# 7. success_rate is accurate fraction
# ---------------------------------------------------------------------------

def test_success_rate_fraction(monkeypatch):
    """success_rate returns the fraction of True values in the window."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "4")
    mod = _fresh_module()
    g = mod.ProviderHealthGradient()
    route = "STANDARD"
    g.record_sweep(route, success=True)
    g.record_sweep(route, success=False)
    g.record_sweep(route, success=True)
    g.record_sweep(route, success=False)
    # 2 out of 4 successes
    assert g.success_rate(route) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 8. quarantine_enabled default true
# ---------------------------------------------------------------------------

def test_quarantine_enabled_default_true(monkeypatch):
    """JARVIS_PROVIDER_QUARANTINE_ENABLED defaults to true."""
    monkeypatch.delenv("JARVIS_PROVIDER_QUARANTINE_ENABLED", raising=False)
    mod = _fresh_module()
    assert mod.quarantine_enabled() is True


def test_quarantine_enabled_respects_false(monkeypatch):
    """JARVIS_PROVIDER_QUARANTINE_ENABLED=false -> False."""
    monkeypatch.setenv("JARVIS_PROVIDER_QUARANTINE_ENABLED", "false")
    mod = _fresh_module()
    assert mod.quarantine_enabled() is False


# ---------------------------------------------------------------------------
# 9. get_provider_health_gradient returns singleton
# ---------------------------------------------------------------------------

def test_get_provider_health_gradient_singleton():
    """get_provider_health_gradient() must return the same object each call."""
    mod = _fresh_module()
    a = mod.get_provider_health_gradient()
    b = mod.get_provider_health_gradient()
    assert a is b


# ---------------------------------------------------------------------------
# 10. quarantine_op: returns True + calls emit_sovereign_yield + append_dlq
# ---------------------------------------------------------------------------

def test_quarantine_op_calls_both_sides_and_returns_true():
    """quarantine_op must call emit_sovereign_yield and append_dlq, then return True."""
    mod = _fresh_module()
    ctx = SimpleNamespace(op_id="op-test-001")

    emit_calls = []
    dlq_calls = []

    def fake_emit(op_id, *, lineage_id, ratio, consecutive_stalls,
                  parent_chars, child_chars, tier, reason):
        emit_calls.append((op_id, tier, reason))

    def fake_append_dlq(envelope, *, reason, path=None):
        dlq_calls.append((envelope, reason))

    with (
        mock.patch(
            "backend.core.ouroboros.governance.provider_quarantine"
            "._import_emit_sovereign_yield",
            return_value=fake_emit,
        ),
        mock.patch(
            "backend.core.ouroboros.governance.provider_quarantine"
            "._import_append_dlq",
            return_value=fake_append_dlq,
        ),
    ):
        result = mod.quarantine_op(ctx, route="STANDARD", telemetry={"lane": "batch"})

    assert result is True
    assert len(emit_calls) == 1
    op_arg, tier_arg, reason_arg = emit_calls[0]
    assert op_arg == "op-test-001"
    assert tier_arg == "provider"
    assert reason_arg == "UPSTREAM QUARANTINE"
    assert len(dlq_calls) == 1
    _, dlq_reason = dlq_calls[0]
    assert dlq_reason == "upstream_quarantine:dw_global_outage"


# ---------------------------------------------------------------------------
# 11. quarantine_op fail-soft: raising append_dlq -> returns False, never raises
# ---------------------------------------------------------------------------

def test_quarantine_op_fail_soft_on_append_dlq_error():
    """If append_dlq raises, quarantine_op must return False without raising."""
    mod = _fresh_module()
    ctx = SimpleNamespace(op_id="op-fail-002")

    def fake_emit(op_id, *, lineage_id, ratio, consecutive_stalls,
                  parent_chars, child_chars, tier, reason):
        pass  # succeeds

    def bad_append_dlq(envelope, *, reason, path=None):
        raise RuntimeError("disk full")

    with (
        mock.patch(
            "backend.core.ouroboros.governance.provider_quarantine"
            "._import_emit_sovereign_yield",
            return_value=fake_emit,
        ),
        mock.patch(
            "backend.core.ouroboros.governance.provider_quarantine"
            "._import_append_dlq",
            return_value=bad_append_dlq,
        ),
    ):
        result = mod.quarantine_op(ctx, route="STANDARD", telemetry={})

    assert result is False


# ---------------------------------------------------------------------------
# 12. quarantine_op: ctx without op_id doesn't crash (fail-soft)
# ---------------------------------------------------------------------------

def test_quarantine_op_no_op_id_attr():
    """quarantine_op is fail-soft even when ctx has no op_id."""
    mod = _fresh_module()
    ctx = SimpleNamespace()  # no op_id

    def fake_emit(op_id, *, lineage_id, ratio, consecutive_stalls,
                  parent_chars, child_chars, tier, reason):
        pass

    def fake_append_dlq(envelope, *, reason, path=None):
        pass

    with (
        mock.patch(
            "backend.core.ouroboros.governance.provider_quarantine"
            "._import_emit_sovereign_yield",
            return_value=fake_emit,
        ),
        mock.patch(
            "backend.core.ouroboros.governance.provider_quarantine"
            "._import_append_dlq",
            return_value=fake_append_dlq,
        ),
    ):
        result = mod.quarantine_op(ctx, route="BACKGROUND", telemetry={})

    assert result is True
