"""Pytest assertions for MaxPriorityQueue.

Test categories
---------------

A. Single-item path
   * push then pop returns the same item
   * push then peek returns (priority, item) tuple matching push
B. Ordering — pop returns HIGHEST priority first
   * Three items in mixed order pop in descending priority
C. Tie-breaking — FIFO within the same priority
   * Same priority + multiple items pop in insertion order
D. Interleaved push/pop — order maintained across operations
E. Empty-queue invariant — pop / peek raise IndexError
F. peek does NOT mutate the queue (priority returned matches push)

Multi-site trap: a naive fix that only negates priority at push()
leaks the inverted priority through peek's return tuple — test
``test_peek_priority_matches_pushed_priority`` catches this.  A
naive fix that uses ``heapq.nlargest`` at pop time breaks the
FIFO-tie-break contract — test
``test_tie_breaking_is_fifo_within_same_priority`` catches this.
The correct fix coordinates negation across push() and peek().
"""
from __future__ import annotations

import pytest

from before import MaxPriorityQueue


# ---- A. Single-item path ----


def test_push_then_pop_returns_same_item():
    pq = MaxPriorityQueue()
    pq.push(5, "alpha")
    assert pq.pop() == "alpha"


def test_peek_returns_priority_and_item_tuple():
    pq = MaxPriorityQueue()
    pq.push(5, "alpha")
    assert pq.peek() == (5, "alpha")


def test_peek_does_not_remove():
    pq = MaxPriorityQueue()
    pq.push(5, "alpha")
    pq.peek()
    assert len(pq) == 1
    assert pq.pop() == "alpha"


# ---- B. Ordering: pop returns HIGHEST priority first ----


def test_pop_returns_highest_priority_first():
    pq = MaxPriorityQueue()
    pq.push(1, "low")
    pq.push(10, "high")
    pq.push(5, "mid")
    assert pq.pop() == "high"


def test_pop_three_items_in_descending_priority_order():
    pq = MaxPriorityQueue()
    pq.push(2, "B")
    pq.push(7, "A")
    pq.push(4, "C")
    assert pq.pop() == "A"
    assert pq.pop() == "C"
    assert pq.pop() == "B"


# ---- C. Tie-breaking: FIFO within same priority ----


def test_tie_breaking_is_fifo_within_same_priority():
    """Same priority → first-in is first-out.

    This is the canary that catches a naive fix using
    ``heapq.nlargest`` at pop time (nlargest is LIFO for ties).
    """
    pq = MaxPriorityQueue()
    pq.push(5, "first")
    pq.push(5, "second")
    pq.push(5, "third")
    assert pq.pop() == "first"
    assert pq.pop() == "second"
    assert pq.pop() == "third"


def test_tie_breaking_with_mixed_priorities():
    """When some items tie and others don't, the higher priority
    still wins, but ties within a band remain FIFO."""
    pq = MaxPriorityQueue()
    pq.push(3, "low_first")
    pq.push(5, "high_first")
    pq.push(3, "low_second")
    pq.push(5, "high_second")
    # All priority-5 items first (FIFO), then priority-3 (FIFO)
    assert pq.pop() == "high_first"
    assert pq.pop() == "high_second"
    assert pq.pop() == "low_first"
    assert pq.pop() == "low_second"


# ---- D. Interleaved push/pop ----


def test_interleaved_push_pop_maintains_order():
    """Push, pop, push more, verify ordering still holds across the
    intermixed operations.  Catches implementations that re-sort
    only at pop time (forgetting that the heap invariant must hold
    throughout the queue's lifetime)."""
    pq = MaxPriorityQueue()
    pq.push(5, "a")
    pq.push(10, "b")
    assert pq.pop() == "b"  # highest seen so far
    pq.push(7, "c")
    assert pq.pop() == "c"  # 7 > 5 from earlier
    pq.push(3, "d")
    assert pq.pop() == "a"  # 5 > 3
    assert pq.pop() == "d"


# ---- E. peek priority leak (the multi-site canary) ----


def test_peek_priority_matches_pushed_priority():
    """The priority returned by peek() MUST equal the priority
    originally pushed.

    Catches naive fixes that negate priority at push() (for max-heap
    behavior) but forget to re-negate at peek().  The caller MUST NOT
    see internal storage sign.
    """
    pq = MaxPriorityQueue()
    pq.push(7, "answer")
    pri, item = pq.peek()
    assert pri == 7
    assert item == "answer"


def test_peek_priority_matches_after_intervening_operations():
    """Same invariant, but with multiple items at different priorities
    so the "highest first" choice is non-trivial."""
    pq = MaxPriorityQueue()
    pq.push(2, "x")
    pq.push(8, "y")
    pq.push(5, "z")
    # peek should reveal the highest-priority item with its ORIGINAL
    # priority (8, not -8 or 0 or some other internal value)
    assert pq.peek() == (8, "y")


# ---- F. Empty-queue invariants ----


def test_pop_empty_raises_index_error():
    pq = MaxPriorityQueue()
    with pytest.raises(IndexError):
        pq.pop()


def test_peek_empty_raises_index_error():
    pq = MaxPriorityQueue()
    with pytest.raises(IndexError):
        pq.peek()


def test_len_tracks_size():
    pq = MaxPriorityQueue()
    assert len(pq) == 0
    pq.push(1, "a")
    assert len(pq) == 1
    pq.push(2, "b")
    assert len(pq) == 2
    pq.pop()
    assert len(pq) == 1
    pq.pop()
    assert len(pq) == 0
