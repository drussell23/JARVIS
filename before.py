"""MaxPriorityQueue — a priority queue where pop() returns the
highest-priority item.

Implementation note (intended): this class is a thin wrapper around
Python's built-in ``heapq`` module.  ``heapq`` implements a MIN-heap,
so a max-heap requires inverting the priority sign at the storage
layer.

Tie-break contract: when two items have equal priority, the one that
was inserted FIRST is popped FIRST (FIFO for ties).  This is the
"stable" ordering expected by callers — e.g. job schedulers that use
priority for severity but rely on insertion order for fairness within
the same severity.

API
---

  * ``push(priority: int|float, item: Any) -> None``
        Insert ``item`` with the given priority.  Higher priority
        means "popped sooner."

  * ``pop() -> Any``
        Remove and return the highest-priority item.  Raises
        :class:`IndexError` on empty queue.

  * ``peek() -> tuple[int|float, Any]``
        Return ``(priority, item)`` of the next-to-be-popped element
        WITHOUT removing it.  The priority returned MUST match what
        the caller originally pushed (i.e., no internal-state leak).
        Raises :class:`IndexError` on empty queue.

  * ``__len__() -> int``
        Current number of items in the queue.
"""
from __future__ import annotations

import heapq
from typing import Any, List, Tuple


class MaxPriorityQueue:
    """A max-priority queue with stable (FIFO) tie-breaking."""

    def __init__(self) -> None:
        # Internal heap storage.  Items stored as
        # ``(priority, counter, item)`` 3-tuples so heapq's tuple
        # comparison breaks ties by counter (and counter is unique
        # per insertion so item objects never need to be comparable).
        self._heap: List[Tuple[Any, int, Any]] = []
        self._counter: int = 0

    def push(self, priority: Any, item: Any) -> None:
        # BUG: stores priority WITHOUT inversion.  heapq is a
        # min-heap, so this pops in ASCENDING priority order
        # instead of the contracted DESCENDING (max-first) order.
        heapq.heappush(self._heap, (priority, self._counter, item))
        self._counter += 1

    def pop(self) -> Any:
        if not self._heap:
            raise IndexError("pop from empty MaxPriorityQueue")
        # Unpacks (priority, counter, item).  Returns the item only.
        _priority, _counter, item = heapq.heappop(self._heap)
        return item

    def peek(self) -> Tuple[Any, Any]:
        if not self._heap:
            raise IndexError("peek on empty MaxPriorityQueue")
        priority, _counter, item = self._heap[0]
        # The priority returned here MUST equal the one originally
        # pushed by the caller.  If push() ever starts inverting
        # priorities for storage, peek() MUST invert them back —
        # this is the multi-site invariant.
        return (priority, item)

    def __len__(self) -> int:
        return len(self._heap)
