"""Standalone selection strategies for scored collections.

Extracted from the deprecated ``genetic.py`` module and adapted for
general-purpose use.  The primary consumer is the L2
:class:`AutonomyFeedbackEngine` which uses these strategies to
prioritise curriculum entries and task candidates for the backlog.

No references to legacy Ouroboros modules.  Pure CPU, no async.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class SelectionStrategy(Enum):
    """Strategy for selecting items from a scored collection."""

    TOURNAMENT = "tournament"
    ROULETTE = "roulette"  # fitness-proportionate
    RANK = "rank"  # rank-based
    ELITIST = "elitist"  # top-N (deterministic)


@dataclass
class ScoredItem:
    """An item with a fitness/priority score for selection."""

    item_id: str
    score: float  # Higher = better
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SelectionEngine:
    """Selects items from scored collections using configurable strategies.

    Used by L2 FeedbackEngine to prioritise curriculum entries and
    task candidates for the backlog.
    """

    def __init__(
        self,
        default_strategy: SelectionStrategy = SelectionStrategy.TOURNAMENT,
    ) -> None:
        self._default_strategy = default_strategy

    # -- public dispatcher ---------------------------------------------------

    def select(
        self,
        items: List[ScoredItem],
        n: int,
        strategy: Optional[SelectionStrategy] = None,
    ) -> List[ScoredItem]:
        """Select *n* items using the specified (or default) strategy.

        Returns up to *n* items (fewer if the collection is smaller).
        Never returns duplicates within a single selection.
        """
        if n <= 0 or not items:
            return []

        effective = strategy if strategy is not None else self._default_strategy

        dispatch = {
            SelectionStrategy.TOURNAMENT: self.tournament_select,
            SelectionStrategy.ROULETTE: self.roulette_select,
            SelectionStrategy.RANK: self.rank_select,
            SelectionStrategy.ELITIST: self.elitist_select,
        }

        handler = dispatch[effective]
        return handler(items, n)

    # -- strategies ----------------------------------------------------------

    def tournament_select(
        self,
        items: List[ScoredItem],
        n: int,
        tournament_size: int = 3,
    ) -> List[ScoredItem]:
        """Tournament selection: pick *tournament_size* random items, take the best.

        Repeat until *n* unique items are collected.  Uses sampling
        without replacement for each tournament round.
        """
        if n <= 0 or not items:
            return []

        n = min(n, len(items))
        ts = min(tournament_size, len(items))

        selected_ids: set[str] = set()
        selected: list[ScoredItem] = []

        # Safety bound: avoid infinite loop if items have duplicate ids
        max_attempts = n * len(items) * 2
        attempts = 0

        while len(selected) < n and attempts < max_attempts:
            attempts += 1
            tournament = random.sample(items, ts)
            winner = max(tournament, key=lambda it: it.score)
            if winner.item_id not in selected_ids:
                selected_ids.add(winner.item_id)
                selected.append(winner)

        # If the tournament couldn't find enough unique winners (e.g.
        # tournament_size == len(items) so the same max always wins),
        # backfill from remaining items in random order.
        if len(selected) < n:
            remaining = [it for it in items if it.item_id not in selected_ids]
            random.shuffle(remaining)
            for it in remaining:
                if len(selected) >= n:
                    break
                if it.item_id not in selected_ids:
                    selected_ids.add(it.item_id)
                    selected.append(it)

        return selected

    def roulette_select(
        self,
        items: List[ScoredItem],
        n: int,
    ) -> List[ScoredItem]:
        """Fitness-proportionate selection (roulette wheel).

        Probability of selection proportional to score.
        Handles zero / negative scores by shifting all values so the
        minimum becomes a small positive number.
        """
        if n <= 0 or not items:
            return []

        n = min(n, len(items))

        # Shift scores so they are all positive
        min_score = min(it.score for it in items)
        shift = (-min_score + 1.0) if min_score <= 0 else 0.0
        weights = [it.score + shift for it in items]

        total = sum(weights)
        if total == 0:
            # All identical after shift (shouldn't happen with +1 shift,
            # but guard anyway) — fall back to uniform random.
            return random.sample(items, n)

        selected_ids: set[str] = set()
        selected: list[ScoredItem] = []

        max_attempts = n * len(items) * 2
        attempts = 0

        while len(selected) < n and attempts < max_attempts:
            attempts += 1
            pick = random.uniform(0, total)
            cumulative = 0.0
            for i, it in enumerate(items):
                cumulative += weights[i]
                if cumulative >= pick:
                    if it.item_id not in selected_ids:
                        selected_ids.add(it.item_id)
                        selected.append(it)
                    break

        return selected

    def rank_select(
        self,
        items: List[ScoredItem],
        n: int,
    ) -> List[ScoredItem]:
        """Rank-based selection.

        Items are ranked by score (ascending). Probability of selection
        is proportional to rank (not raw score), so rank 1 = worst,
        rank len(items) = best.
        """
        if n <= 0 or not items:
            return []

        n = min(n, len(items))

        # Sort ascending by score so highest score gets highest rank
        sorted_items = sorted(items, key=lambda it: it.score)
        ranks = list(range(1, len(sorted_items) + 1))
        total_rank = sum(ranks)

        selected_ids: set[str] = set()
        selected: list[ScoredItem] = []

        max_attempts = n * len(items) * 2
        attempts = 0

        while len(selected) < n and attempts < max_attempts:
            attempts += 1
            pick = random.uniform(0, total_rank)
            cumulative = 0.0
            for i, it in enumerate(sorted_items):
                cumulative += ranks[i]
                if cumulative >= pick:
                    if it.item_id not in selected_ids:
                        selected_ids.add(it.item_id)
                        selected.append(it)
                    break

        return selected

    def elitist_select(
        self,
        items: List[ScoredItem],
        n: int,
    ) -> List[ScoredItem]:
        """Return the top-*n* items by score (deterministic)."""
        if n <= 0 or not items:
            return []

        n = min(n, len(items))
        sorted_items = sorted(items, key=lambda it: it.score, reverse=True)
        return sorted_items[:n]
