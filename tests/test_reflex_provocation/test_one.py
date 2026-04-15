"""Deliberate failing test to provoke TestFailureSensor → IMMEDIATE route.

Purpose: Exercise the full GENERATE → Venom tool loop → APPLY → VERIFY → L2
pipeline end-to-end during a battle test session. TestWatcher polls this
directory only (via JARVIS_INTENT_TEST_DIR), parses the FAILED line, emits
a test_failure signal with urgency=high, and UrgencyRouter routes IMMEDIATE.

This module is deliberately sized (~2KB) so that a well-scoped fix preserves
>50% of the file content, passing the candidate_generator "suspicious
shrinkage" validator. A single-line fix on a tiny file gets rejected as a
hallucinated truncation; bulking the module with genuine passing tests keeps
the body stable across edits.

Remove after the battle test exercises the reflex path end-to-end.
"""
from __future__ import annotations


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
    assert add(0, 0) == 0
    assert add(5, 0) == 5
    assert add(0, 5) == 5


def test_add_positive_and_negative() -> None:
    assert add(2, 3) == 5
    assert add(-2, -3) == -5
    assert add(-2, 3) == 1


def test_sub_basic() -> None:
    assert sub(5, 3) == 2
    assert sub(0, 0) == 0
    assert sub(-1, -1) == 0


def test_mul_basic() -> None:
    assert mul(2, 3) == 6
    assert mul(-2, 3) == -6
    assert mul(0, 100) == 0


def test_is_even_basic() -> None:
    assert is_even(0) is True
    assert is_even(2) is True
    assert is_even(1) is False
    assert is_even(-4) is True


def test_clamp_within_range() -> None:
    assert clamp(5, 0, 10) == 5
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10


def test_clamp_out_of_range() -> None:
    assert clamp(-5, 0, 10) == 0
    assert clamp(42, 0, 10) == 10


def test_add_expects_wrong_sum() -> None:
    """Deliberately broken: expected to fail until Ouroboros fixes it.

    The correct assertion is ``add(2, 3) == 5``. The wrong literal 999
    triggers the TestFailureSensor pathway so the governance loop routes
    IMMEDIATE, generates a candidate, and exercises APPLY → VERIFY → L2.
    """
    assert add(2, 3) == 999
