"""tests/governance/autonomy/test_selection_strategies.py

TDD tests for SelectionEngine — standalone selection strategies for L2 curriculum selection.

Covers:
- ScoredItem dataclass fields
- Tournament selection (count, uniqueness, statistical bias toward high scores)
- Roulette selection (count, zero scores, negative scores)
- Rank selection (count, statistical bias toward high rank)
- Elitist selection (top-N, determinism)
- select() dispatch and default strategy
- Edge cases: empty list, n > len(items), all same score
"""
from __future__ import annotations

import random
from collections import Counter

import pytest

from backend.core.ouroboros.governance.autonomy.selection_strategies import (
    ScoredItem,
    SelectionEngine,
    SelectionStrategy,
)


# ---------------------------------------------------------------------------
# ScoredItem tests
# ---------------------------------------------------------------------------


class TestScoredItem:
    def test_scored_item_fields(self) -> None:
        item = ScoredItem(item_id="abc", score=3.14, metadata={"key": "val"})
        assert item.item_id == "abc"
        assert item.score == 3.14
        assert item.metadata == {"key": "val"}

    def test_scored_item_defaults(self) -> None:
        item = ScoredItem(item_id="x", score=0.0)
        assert item.metadata == {}


# ---------------------------------------------------------------------------
# Tournament selection
# ---------------------------------------------------------------------------


class TestTournamentSelection:
    def test_tournament_returns_n_items(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(10)]
        engine = SelectionEngine()
        result = engine.tournament_select(items, n=3)
        assert len(result) == 3

    def test_tournament_no_duplicates(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(10)]
        engine = SelectionEngine()
        result = engine.tournament_select(items, n=5)
        ids = [r.item_id for r in result]
        assert len(ids) == len(set(ids)), "tournament_select returned duplicate item_ids"

    def test_tournament_favors_high_scores(self) -> None:
        """Over many runs, the highest-scored item should appear most frequently."""
        random.seed(42)
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(100)]
        engine = SelectionEngine()

        winner_counts: Counter[str] = Counter()
        for _ in range(500):
            result = engine.tournament_select(items, n=5)
            for r in result:
                winner_counts[r.item_id] += 1

        # The top item (id="99", score=99.0) should be most common
        top_id = winner_counts.most_common(1)[0][0]
        assert int(top_id) >= 90, (
            f"Expected top item to have high score, got id={top_id}"
        )


# ---------------------------------------------------------------------------
# Roulette selection
# ---------------------------------------------------------------------------


class TestRouletteSelection:
    def test_roulette_returns_n_items(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i + 1)) for i in range(10)]
        engine = SelectionEngine()
        result = engine.roulette_select(items, n=3)
        assert len(result) == 3

    def test_roulette_handles_zero_scores(self) -> None:
        items = [ScoredItem(item_id=str(i), score=0.0) for i in range(5)]
        engine = SelectionEngine()
        result = engine.roulette_select(items, n=3)
        assert len(result) == 3

    def test_roulette_handles_negative_scores(self) -> None:
        items = [
            ScoredItem(item_id="a", score=-5.0),
            ScoredItem(item_id="b", score=-2.0),
            ScoredItem(item_id="c", score=1.0),
        ]
        engine = SelectionEngine()
        result = engine.roulette_select(items, n=2)
        assert len(result) == 2

    def test_roulette_favors_high_scores(self) -> None:
        """Statistical test: high-score items should be selected more often."""
        random.seed(123)
        items = [
            ScoredItem(item_id="low", score=1.0),
            ScoredItem(item_id="high", score=100.0),
        ]
        engine = SelectionEngine()

        counts: Counter[str] = Counter()
        for _ in range(1000):
            result = engine.roulette_select(items, n=1)
            counts[result[0].item_id] += 1

        assert counts["high"] > counts["low"], (
            f"Expected 'high' to dominate, got high={counts['high']}, low={counts['low']}"
        )


# ---------------------------------------------------------------------------
# Rank selection
# ---------------------------------------------------------------------------


class TestRankSelection:
    def test_rank_returns_n_items(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(10)]
        engine = SelectionEngine()
        result = engine.rank_select(items, n=3)
        assert len(result) == 3

    def test_rank_favors_high_rank(self) -> None:
        """Statistical test over many runs: highest-ranked item appears most."""
        random.seed(7)
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(10)]
        engine = SelectionEngine()

        counts: Counter[str] = Counter()
        for _ in range(2000):
            result = engine.rank_select(items, n=1)
            counts[result[0].item_id] += 1

        # The top item (id="9") should be the most frequently selected
        top_id = counts.most_common(1)[0][0]
        assert top_id == "9", f"Expected id='9' to be most common, got {top_id}"


# ---------------------------------------------------------------------------
# Elitist selection
# ---------------------------------------------------------------------------


class TestElitistSelection:
    def test_elitist_returns_top_n(self) -> None:
        items = [
            ScoredItem(item_id="a", score=1.0),
            ScoredItem(item_id="b", score=5.0),
            ScoredItem(item_id="c", score=3.0),
            ScoredItem(item_id="d", score=4.0),
            ScoredItem(item_id="e", score=2.0),
        ]
        engine = SelectionEngine()
        result = engine.elitist_select(items, n=3)
        ids = [r.item_id for r in result]
        assert ids == ["b", "d", "c"]

    def test_elitist_deterministic(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i % 7)) for i in range(20)]
        engine = SelectionEngine()
        result1 = engine.elitist_select(items, n=5)
        result2 = engine.elitist_select(items, n=5)
        assert [r.item_id for r in result1] == [r.item_id for r in result2]


# ---------------------------------------------------------------------------
# select() dispatch
# ---------------------------------------------------------------------------


class TestSelectDispatch:
    def test_select_dispatches_to_strategy(self) -> None:
        items = [
            ScoredItem(item_id="a", score=1.0),
            ScoredItem(item_id="b", score=5.0),
            ScoredItem(item_id="c", score=3.0),
        ]
        engine = SelectionEngine()
        result = engine.select(items, n=2, strategy=SelectionStrategy.ELITIST)
        # Elitist is deterministic, so we can check exact output
        ids = [r.item_id for r in result]
        assert ids == ["b", "c"]

    def test_select_with_default_strategy(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(10)]
        engine = SelectionEngine(default_strategy=SelectionStrategy.ELITIST)
        result = engine.select(items, n=3)
        ids = [r.item_id for r in result]
        assert ids == ["9", "8", "7"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_items_returns_empty(self) -> None:
        engine = SelectionEngine()
        for strategy in SelectionStrategy:
            result = engine.select([], n=5, strategy=strategy)
            assert result == [], f"Expected empty for {strategy}"

    def test_n_greater_than_items(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(3)]
        engine = SelectionEngine()
        for strategy in SelectionStrategy:
            result = engine.select(items, n=5, strategy=strategy)
            assert len(result) <= 3, f"Got more than 3 items for {strategy}"
            assert len(result) == 3, f"Expected all 3 items for {strategy}"

    def test_all_same_score(self) -> None:
        items = [ScoredItem(item_id=str(i), score=1.0) for i in range(10)]
        engine = SelectionEngine()
        for strategy in SelectionStrategy:
            result = engine.select(items, n=4, strategy=strategy)
            assert len(result) == 4, f"Expected 4 items for {strategy}"

    def test_single_item(self) -> None:
        items = [ScoredItem(item_id="only", score=42.0)]
        engine = SelectionEngine()
        for strategy in SelectionStrategy:
            result = engine.select(items, n=1, strategy=strategy)
            assert len(result) == 1
            assert result[0].item_id == "only"

    def test_n_zero_returns_empty(self) -> None:
        items = [ScoredItem(item_id=str(i), score=float(i)) for i in range(5)]
        engine = SelectionEngine()
        for strategy in SelectionStrategy:
            result = engine.select(items, n=0, strategy=strategy)
            assert result == [], f"Expected empty for n=0 with {strategy}"
