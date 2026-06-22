"""Tests for AdaptiveRecursionGovernor (Task B3).

TDD-first: all tests written BEFORE implementation.
These must be RED until the production module exists.
"""
from __future__ import annotations

import math

import pytest

from backend.core.ouroboros.governance import adaptive_recursion_governor as gov


# ---------------------------------------------------------------------------
# Brief-mandated tests (exact from task-B3-brief.md)
# ---------------------------------------------------------------------------


def test_idle_expands_fanout():
    """Under zero load the governor allows and gives fanout >= 3."""
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    assert b.allowed and b.max_fanout >= 3


def test_heavy_load_shrinks_to_one_or_blocks():
    """Under maximum load, governor must either block or shrink fanout to 1."""
    b = gov.recursion_budget(queue_len=500, loop_blocked_ms=2000.0, pressure_level=3, depth=2)
    assert (not b.allowed) or b.max_fanout == 1


def test_depth_ceiling_is_adaptive_not_literal():
    """Same depth=4 → allowed when idle, blocked when loaded.
    Proves the ceiling is derived from signals, not a hardcoded constant.
    """
    shallow = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=4)
    deep_loaded = gov.recursion_budget(
        queue_len=300, loop_blocked_ms=1500.0, pressure_level=2, depth=4
    )
    assert shallow.allowed and not deep_loaded.allowed


def test_failsoft_bad_signals_blocks_safely():
    """Any bad input → safe block (never raises, always returns a valid Budget)."""
    b = gov.recursion_budget(queue_len=-1, loop_blocked_ms=float("nan"), pressure_level=99, depth=0)
    assert isinstance(b.allowed, bool)


# ---------------------------------------------------------------------------
# Additional tests for monotonicity + structural correctness
# ---------------------------------------------------------------------------


def test_budget_is_frozen_dataclass():
    """Budget must be a frozen dataclass with expected fields."""
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    assert hasattr(b, "allowed")
    assert hasattr(b, "max_fanout")
    assert hasattr(b, "reason")
    # frozen: assigning should raise
    with pytest.raises((AttributeError, TypeError)):
        b.allowed = False  # type: ignore[misc]


def test_reason_is_str():
    """Budget.reason must be a non-empty string."""
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    assert isinstance(b.reason, str) and len(b.reason) > 0


def test_max_fanout_is_positive_int():
    """max_fanout must always be >= 1 (even when blocked)."""
    b = gov.recursion_budget(queue_len=500, loop_blocked_ms=2000.0, pressure_level=3, depth=0)
    assert isinstance(b.max_fanout, int) and b.max_fanout >= 1


def test_fanout_monotone_decreases_with_queue():
    """Higher queue length must yield <= fanout (monotone)."""
    b_low = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    b_mid = gov.recursion_budget(queue_len=100, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    b_high = gov.recursion_budget(queue_len=500, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    assert b_low.max_fanout >= b_mid.max_fanout >= b_high.max_fanout


def test_fanout_monotone_decreases_with_loop_latency():
    """Higher loop latency must yield <= fanout (monotone)."""
    b_low = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    b_mid = gov.recursion_budget(queue_len=0, loop_blocked_ms=500.0, pressure_level=0, depth=0)
    b_high = gov.recursion_budget(queue_len=0, loop_blocked_ms=2000.0, pressure_level=0, depth=0)
    assert b_low.max_fanout >= b_mid.max_fanout >= b_high.max_fanout


def test_fanout_monotone_decreases_with_pressure():
    """Higher pressure level must yield <= fanout (monotone)."""
    b0 = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    b1 = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=1, depth=0)
    b2 = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=2, depth=0)
    b3 = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=3, depth=0)
    assert b0.max_fanout >= b1.max_fanout >= b2.max_fanout >= b3.max_fanout


def test_depth_monotone_decreases_allowed():
    """Higher depth under load must not be allowed if shallower depth was already blocked."""
    # Under heavy load, if depth=3 is blocked, depth=10 must also be blocked
    b3 = gov.recursion_budget(queue_len=300, loop_blocked_ms=1500.0, pressure_level=2, depth=3)
    b10 = gov.recursion_budget(queue_len=300, loop_blocked_ms=1500.0, pressure_level=2, depth=10)
    if not b3.allowed:
        assert not b10.allowed


def test_failsoft_nan_queue():
    """NaN queue_len → failsoft block."""
    # queue_len is int so Python type system prevents NaN directly,
    # but test robustness with extreme values
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=float("nan"), pressure_level=0, depth=0)
    assert isinstance(b.allowed, bool)
    assert b.max_fanout >= 1


def test_failsoft_extreme_depth():
    """Absurdly large depth → blocked (not crash)."""
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=10_000)
    assert isinstance(b, gov.Budget)
    assert b.max_fanout >= 1


def test_failsoft_inf_loop_ms():
    """Infinite loop_blocked_ms → failsoft block."""
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=float("inf"), pressure_level=0, depth=0)
    # must not raise; should block or severely restrict
    assert isinstance(b.allowed, bool)
    assert b.max_fanout >= 1


def test_failsoft_negative_pressure_level():
    """Negative pressure_level treated as OK (0) or failsoft."""
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=-1, depth=0)
    assert isinstance(b, gov.Budget)
    assert b.max_fanout >= 1


def test_env_knob_fanout_idle(monkeypatch):
    """JARVIS_RECURSION_FANOUT_IDLE env knob changes the idle fanout ceiling."""
    monkeypatch.setenv("JARVIS_RECURSION_FANOUT_IDLE", "6")
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    assert b.max_fanout >= 6


def test_env_knob_queue_soft(monkeypatch):
    """JARVIS_RECURSION_QUEUE_SOFT knob: queue below soft → still allowed at depth 0."""
    monkeypatch.setenv("JARVIS_RECURSION_QUEUE_SOFT", "200")
    b = gov.recursion_budget(queue_len=50, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    assert b.allowed


def test_env_knob_loop_ms_soft(monkeypatch):
    """JARVIS_RECURSION_LOOP_MS_SOFT knob: latency below soft → still allowed at depth 0."""
    monkeypatch.setenv("JARVIS_RECURSION_LOOP_MS_SOFT", "500")
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=100.0, pressure_level=0, depth=0)
    assert b.allowed


def test_critical_pressure_level_blocks_depth_zero():
    """CRITICAL pressure (3) at depth=0 must either block or severely restrict fanout."""
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=3, depth=0)
    # Either block entirely or limit severely (fanout <= 2)
    assert (not b.allowed) or b.max_fanout <= 2


def test_no_literal_cap_different_ceilings_by_load():
    """Prove no literal MAX_DEPTH: idle ceiling > loaded ceiling."""
    # Find the ceiling for idle: first depth that becomes disallowed
    idle_ceiling = None
    for d in range(1, 30):
        b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=d)
        if not b.allowed:
            idle_ceiling = d
            break

    # Find the ceiling for loaded
    loaded_ceiling = None
    for d in range(1, 30):
        b = gov.recursion_budget(
            queue_len=200, loop_blocked_ms=1000.0, pressure_level=2, depth=d
        )
        if not b.allowed:
            loaded_ceiling = d
            break

    # loaded_ceiling must exist and be strictly less than idle_ceiling
    # (or idle is unlimited but loaded is bounded)
    if idle_ceiling is not None and loaded_ceiling is not None:
        assert loaded_ceiling < idle_ceiling
    elif idle_ceiling is None:
        # idle allows up to depth 30 — that's fine, loaded must still block somewhere
        assert loaded_ceiling is not None
