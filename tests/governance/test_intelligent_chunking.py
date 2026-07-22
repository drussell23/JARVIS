"""Intelligent Chunk Routing — enterprise-scale (10k-100k line) handling.

Mandated bulletproof, on a mocked ~100,000-line file:
  1. Hierarchical AST Pruning returns a MINIMAL Radius of Relevance —
     aggressively dropping distant sibling classes.
  2. A missing symbol EXPLICITLY blocks whole-file ingestion and degrades to
     the RAG / keyword chunker.
  3. The router logs the attempt outcome to the SQLite telemetry layer, and the
     learned weights come back.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.core.ouroboros.governance.intelligent_chunking import (
    ChunkPlan,
    best_strategy,
    exceeds_ceiling,
    keyword_rag_chunks,
    radius_of_relevance,
    record_strategy_outcome,
    select_extraction_strategy,
    strategy_weights,
)


def _make_massive_file(n_siblings: int = 4600) -> str:
    """~100k-line module: n_siblings distant classes (to be pruned) + the one
    target class holding ``_topological_sort``."""
    parts = ['"""Massive enterprise module."""', "import collections", "import heapq", ""]
    for i in range(n_siblings):
        parts.append(f"class Sibling_{i}:")
        parts.append(f'    """Distant sibling class {i} — must be PRUNED."""')
        for j in range(18):
            parts.append(f"    def method_{j}(self):")
            parts.append(f"        return {i} * {j}")
        parts.append("")
    parts.append("class SagaApplyStrategy:")
    parts.append('    """The target class — the Radius of Relevance."""')
    parts.append("    def _topological_sort(self, repo_scope, edges):")
    parts.append("        graph = collections.defaultdict(list)")
    parts.append("        return sorted(repo_scope)")
    parts.append("")
    return "\n".join(parts)


_MASSIVE = _make_massive_file()


def test_massive_file_exceeds_ceiling() -> None:
    assert _MASSIVE.count("\n") > 90000, "fixture should be ~100k lines"
    assert exceeds_ceiling(_MASSIVE) is True


def test_hierarchical_pruning_drops_distant_siblings() -> None:
    radius = radius_of_relevance(_MASSIVE, "saga_apply_strategy.py", "_topological_sort")
    assert radius is not None
    # The target class + method + imports are present...
    assert "class SagaApplyStrategy" in radius
    assert "_topological_sort" in radius
    assert "import collections" in radius
    # ...but EVERY distant sibling class is dropped (aggressive pruning).
    assert "class Sibling_0" not in radius
    assert "class Sibling_4599" not in radius
    assert "method_0" not in radius
    # The radius is a TINY fraction of the ~100k-line file.
    assert len(radius) < len(_MASSIVE) / 100
    assert radius.count("\n") < 40


def test_missing_symbol_blocks_whole_file_and_routes_to_rag() -> None:
    plan = select_extraction_strategy(
        _MASSIVE, "saga_apply_strategy.py", symbol="does_not_exist_anywhere",
        query="topological sort heapq",
    )
    # Whole-file ingestion of a 100k-line file is FORBIDDEN.
    assert plan.strategy == "rag"
    assert plan.strategy != "whole"
    assert plan.forbade_whole_file is True
    # RAG returned bounded top-k snippets, NOT the whole file.
    assert 0 < len(plan.rag_snippets) <= 6
    assert len(plan.context) < len(_MASSIVE)


def test_resolvable_symbol_routes_to_ast_radius() -> None:
    plan = select_extraction_strategy(
        _MASSIVE, "saga_apply_strategy.py", symbol="_topological_sort",
    )
    assert plan.strategy == "ast"
    assert plan.forbade_whole_file is True
    assert "class SagaApplyStrategy" in plan.context
    assert "class Sibling_0" not in plan.context


def test_small_file_allows_whole() -> None:
    plan = select_extraction_strategy("def f():\n    return 1\n", "x.py", "f")
    assert plan.strategy == "whole"
    assert plan.forbade_whole_file is False


def test_reinforcement_logs_to_sqlite_and_learns() -> None:
    conn = sqlite3.connect(":memory:")

    # Log outcomes: AST succeeds on massive .py, RAG times out.
    assert record_strategy_outcome(
        conn, strategy="ast", file_lines=100000, ext=".py", outcome="promoted",
    ) is True
    record_strategy_outcome(
        conn, strategy="ast", file_lines=100000, ext=".py", outcome="promoted",
    )
    record_strategy_outcome(
        conn, strategy="rag", file_lines=100000, ext=".py", outcome="timeout",
    )

    # The row landed in the SQLite telemetry layer.
    n = conn.execute(
        "SELECT COUNT(*) FROM chunk_strategy_outcomes"
    ).fetchone()[0]
    assert n == 3

    # The router LEARNS — AST has the higher historical success weight.
    weights = strategy_weights(conn, file_lines=100000, ext=".py")
    assert weights["ast"] == 1.0
    assert weights["rag"] == 0.0
    assert best_strategy(conn, file_lines=100000, ext=".py") == "ast"
    conn.close()


def test_rag_never_returns_whole_file() -> None:
    snippets = keyword_rag_chunks(_MASSIVE, "topological sort", k=6)
    assert 0 < len(snippets) <= 6
    joined = "\n".join(snippets)
    assert len(joined) < len(_MASSIVE) / 10  # bounded, not the whole file
