"""LOCAL-ONLY soak fixture — the saga op trigger. DO NOT PUSH TO ORIGIN."""

from __future__ import annotations

from backend.core.ouroboros.governance.saga.saga_apply_strategy import (
    SagaApplyStrategy,
)


def test_topological_sort_is_alphabetical_within_same_depth() -> None:
    s = SagaApplyStrategy.__new__(SagaApplyStrategy)
    repo_scope = ("p1", "p2", "x", "y")
    edges = (("x", "p2"), ("y", "p1"))
    result = s._topological_sort(repo_scope, edges)
    assert result == ["p1", "p2", "x", "y"], (
        f"docstring promises alphabetical within same depth, got {result} "
        "(FIFO deque bug — use a heapq priority queue)"
    )
