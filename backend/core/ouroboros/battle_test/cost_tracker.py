"""CostTracker — monitors cumulative API spend during a battle test session.

Fires an asyncio.Event when the configured budget is exhausted, allowing the
BattleTestHarness to stop the session cleanly alongside shutdown_event and
idle_event (first one to fire wins).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CostTracker:
    """Track per-provider API costs for a single battle test session.

    Parameters
    ----------
    budget_usd:
        Maximum spend allowed for this session (default $0.50).
    persist_path:
        Optional JSON file path.  If supplied the tracker loads existing state
        on construction and writes updated state on :meth:`save`.
    """

    def __init__(
        self,
        budget_usd: float = 0.50,
        persist_path: Optional[Path] = None,
    ) -> None:
        self._budget_usd = budget_usd
        self._persist_path = persist_path
        self._total_spent: float = 0.0
        self._breakdown: Dict[str, float] = defaultdict(float)
        self.budget_event: asyncio.Event = asyncio.Event()

        self._load()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total_spent(self) -> float:
        """Total USD spent so far this session."""
        return self._total_spent

    @property
    def remaining(self) -> float:
        """Remaining budget in USD (never negative)."""
        return max(0.0, self._budget_usd - self._total_spent)

    @property
    def exhausted(self) -> bool:
        """True when total spend has reached or exceeded the budget."""
        return self._total_spent >= self._budget_usd

    @property
    def breakdown(self) -> Dict[str, float]:
        """Per-provider cost breakdown (a snapshot copy)."""
        return dict(self._breakdown)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, provider: str, cost_usd: float) -> None:
        """Record a cost incurred by *provider*.

        Non-positive values are silently ignored.  If recording this cost
        causes the session budget to be exhausted, :attr:`budget_event` is set.

        Parameters
        ----------
        provider:
            Arbitrary string identifying the AI provider (e.g. ``"anthropic"``).
        cost_usd:
            Cost in US dollars.  Values <= 0 are ignored.
        """
        if cost_usd <= 0:
            return

        self._total_spent += cost_usd
        self._breakdown[provider] += cost_usd

        logger.debug(
            "CostTracker: recorded $%.4f for %s | total=$%.4f remaining=$%.4f",
            cost_usd,
            provider,
            self._total_spent,
            self.remaining,
        )

        if self.exhausted and not self.budget_event.is_set():
            logger.warning(
                "CostTracker: budget of $%.2f exhausted (total=$%.4f) — firing budget_event",
                self._budget_usd,
                self._total_spent,
            )
            self.budget_event.set()

    def save(self) -> None:
        """Persist current state to :attr:`_persist_path` as JSON.

        No-op if no path was provided.  Errors are logged but not re-raised so
        the session is never interrupted by an I/O failure.
        """
        if self._persist_path is None:
            return

        state = {
            "budget_usd": self._budget_usd,
            "total_spent": self._total_spent,
            "breakdown": dict(self._breakdown),
        }
        try:
            self._persist_path.write_text(json.dumps(state, indent=2))
            logger.debug("CostTracker: state saved to %s", self._persist_path)
        except OSError as exc:
            logger.error("CostTracker: failed to save state: %s", exc)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load previously persisted state from :attr:`_persist_path`.

        Called once in :meth:`__init__`.  Silently does nothing if the path is
        ``None``, the file does not exist, or the JSON is malformed.
        """
        if self._persist_path is None:
            return

        try:
            raw = self._persist_path.read_text()
            state = json.loads(raw)
            self._total_spent = float(state.get("total_spent", 0.0))
            for provider, cost in state.get("breakdown", {}).items():
                self._breakdown[provider] = float(cost)
            logger.debug(
                "CostTracker: loaded state from %s (total=$%.4f)",
                self._persist_path,
                self._total_spent,
            )
            # Re-fire the event if the loaded state is already exhausted.
            if self.exhausted:
                self.budget_event.set()
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("CostTracker: could not load state from %s: %s", self._persist_path, exc)
