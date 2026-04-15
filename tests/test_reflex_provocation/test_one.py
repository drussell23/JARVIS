# [Ouroboros] Modified by Ouroboros (op=op-019d8f19-) at 2026-04-15 03:10 UTC
# Reason: Stable test failure: tests/test_reflex_provocation/test_one.py::test_add_expects_wrong_sum (streak=2): 

from __future__ import annotations
"""Reflex provocation tests for the TestFailureSensor pipeline."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b


def sub(a: int, b: int) -> int:
    """Return the difference of two integers."""
    return a - b


def mul(a: int, b: int) -> int:
    """Return the product of two integers."""
    return a * b


def is_even(n: int) -> bool:
    """Return True when n is an even integer."""
    return n % 2 == 0


def clamp(value: int, low: int, high: int) -> int:
    """Clamp value to the inclusive range [low, high]."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def test_add_zero_identity() -> None:
    """Verify that adding zero does not change a value."""
    assert add(0, 0) == 0
    assert add(5, 0) == 5
    assert add(0, 5) == 5


def test_add_positive_and_negative() -> None:
    """Verify addition with positive and negative integers."""
    assert add(2, 3) == 5
    assert add(-2, -3) == -5
    assert add(-2, 3) == 1


def test_sub_basic() -> None:
    """Verify basic subtraction behaviour."""
    assert sub(5, 3) == 2
    assert sub(0, 0) == 0
    assert sub(-1, -1) == 0


def test_mul_basic() -> None:
    """Verify basic multiplication behaviour."""
    assert mul(2, 3) == 6
    assert mul(-2, 3) == -6
    assert mul(0, 100) == 0


def test_is_even_basic() -> None:
    """Verify even/odd detection for representative integers."""
    assert is_even(0) is True
    assert is_even(2) is True
    assert is_even(1) is False
    assert is_even(-4) is True


def test_clamp_within_range() -> None:
    """Verify that values already inside the range pass through unchanged."""
    assert clamp(5, 0, 10) == 5
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10


def test_clamp_out_of_range() -> None:
    """Verify that values outside the range are clamped to the nearest bound."""
    assert clamp(-5, 0, 10) == 0
    assert clamp(42, 0, 10) == 10


def test_add_expects_wrong_sum() -> None:
    """Verify that add(2, 3) returns the correct sum of 5."""
    assert add(2, 3) == 5
