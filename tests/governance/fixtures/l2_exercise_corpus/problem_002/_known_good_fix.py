"""Known-good fix for problem_002 MaxPriorityQueue.

Reference solution used by the Phase 1.5.B spine to sanity-check
that the test suite IS solvable when the multi-site negation is
applied coherently.

Bug summary:
  * ``heapq`` is a MIN-heap; storing positive priorities makes
    ``pop()`` return the LOWEST priority first — opposite of the
    max-priority contract.

Coherent fix:
  1. Negate ``priority`` at push-time so the min-heap pops in
     descending original-priority order.
  2. Re-negate at peek-time so the caller sees the ORIGINAL
     priority (not the internal -priority storage form).
  3. Pop already returns just the item (no priority), so its
     internal storage is consistent without further change.

Tie-break: ``self._counter`` increments per push, so the heap's
tuple comparison naturally orders earlier insertions BEFORE later
ones when priorities tie.  The counter is positive and NOT
negated — this is correct because for ties (same negated
priority) we want the smaller counter to win the heap comparison
(FIFO).

This file is NOT loaded by the canonical ``list_corpus_problems``
walker (underscore-prefixed → skipped per Phase 1.5.A convention).
"""
from __future__ import annotations

import heapq
from typing import Any, List, Tuple


class MaxPriorityQueue:
    """A max-priority queue with stable (FIFO) tie-breaking."""

    def __init__(self) -> None:
        self._heap: List[Tuple[Any, int, Any]] = []
        self._counter: int = 0

    def push(self, priority: Any, item: Any) -> None:
        # FIX: negate priority so min-heap pops the most-negative
        # (= highest original priority) first.  Counter is NOT
        # negated — for ties, smaller counter (earlier insertion)
        # wins the heap comparison, giving FIFO.
        heapq.heappush(self._heap, (-priority, self._counter, item))
        self._counter += 1

    def pop(self) -> Any:
        if not self._heap:
            raise IndexError("pop from empty MaxPriorityQueue")
        _neg_priority, _counter, item = heapq.heappop(self._heap)
        return item

    def peek(self) -> Tuple[Any, Any]:
        if not self._heap:
            raise IndexError("peek on empty MaxPriorityQueue")
        neg_priority, _counter, item = self._heap[0]
        # FIX: re-negate so the caller sees the ORIGINAL priority.
        return (-neg_priority, item)

    def __len__(self) -> int:
        return len(self._heap)
