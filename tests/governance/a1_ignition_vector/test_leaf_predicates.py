"""Exhaustive green tests for the isolated A1 ignition target (coverage 1.0).

These are what make the target a LEGITIMATE auto-apply candidate: the chaos
injector requires a green test exercising the function it mutates, and the
OperationAdvisor's coverage signal reads ``test_leaf_predicates.py`` as full
coverage of ``leaf_predicates.py``. A mutation that breaks one of these turns a
test RED (the live-fire signal); the organism's fix must turn it GREEN again.
"""
from __future__ import annotations

from backend.core.ouroboros.a1_ignition_vector.leaf_predicates import (
    clamp01,
    in_closed_range,
    is_even,
    sign,
)


def test_is_even():
    assert is_even(0) is True
    assert is_even(2) is True
    assert is_even(-4) is True
    assert is_even(1) is False
    assert is_even(-3) is False


def test_sign():
    assert sign(5) == 1
    assert sign(0) == 0
    assert sign(-7) == -1


def test_in_closed_range():
    assert in_closed_range(5, 1, 10) is True
    assert in_closed_range(1, 1, 10) is True   # inclusive low
    assert in_closed_range(10, 1, 10) is True  # inclusive high
    assert in_closed_range(0, 1, 10) is False
    assert in_closed_range(11, 1, 10) is False


def test_clamp01():
    assert clamp01(-5) == 0
    assert clamp01(0) == 0
    assert clamp01(1) == 1
    assert clamp01(7) == 1
