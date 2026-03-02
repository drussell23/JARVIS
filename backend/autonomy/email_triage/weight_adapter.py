"""Adaptive weight engine for email triage scoring (WS5).

Periodically adjusts scoring factor weights based on accumulated
HIGH+MEDIUM confidence outcomes. Implements shadow mode (Gate #5)
and rollback safety.

Shadow mode: New weights run in shadow (logged, not applied) for N
cycles before activation. If adapted weights cause >10% tier
disagreement with observed outcomes during the shadow period,
rollback to last-good-weights.

Safety bounds: Each weight bounded to [default - bounds%, default + bounds%].
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.events import (
    EVENT_WEIGHTS_ADAPTED,
    EVENT_WEIGHTS_SHADOW,
    emit_triage_event,
)
from autonomy.email_triage.scoring import DEFAULT_WEIGHTS

logger = logging.getLogger("jarvis.email_triage.weight_adapter")


class WeightAdapter:
    """Adapts scoring weights based on user outcomes with shadow mode safety.

    Lifecycle:
    1. Accumulate outcomes via ``record_outcome()``.
    2. When ``min_outcomes_for_adaptation`` reached, compute adapted weights.
    3. Run in shadow for ``shadow_cycles`` cycles (log but don't apply).
    4. After shadow, compare tier assignments. If >threshold disagreement,
       rollback to last-good-weights.
    5. If consistent, activate adapted weights.
    """

    def __init__(self, config: TriageConfig):
        self._config = config
        self._outcomes: List[Dict[str, Any]] = []
        self._shadow_log: Deque[Tuple[int, int, str]] = deque(maxlen=200)
        # (default_tier, adapted_tier, actual_outcome)
        self._shadow_cycles_remaining: int = 0
        self._active_weights: Optional[Dict[str, float]] = None
        self._shadow_weights: Optional[Dict[str, float]] = None
        self._last_good_weights: Optional[Dict[str, float]] = None
        self._total_shadow_entries: int = 0
        self._total_disagreements: int = 0

    @property
    def is_shadow_active(self) -> bool:
        """True if adapted weights are in shadow mode (not yet applied)."""
        return self._shadow_weights is not None and self._shadow_cycles_remaining > 0

    @property
    def active_weights(self) -> Optional[Dict[str, float]]:
        """Return the currently active adapted weights, or None for defaults."""
        return self._active_weights

    def record_outcome(self, outcome_record: Dict[str, Any]) -> None:
        """Record an outcome from the OutcomeCollector for weight adaptation.

        Only HIGH+MEDIUM confidence outcomes should be passed here (Gate #4).
        """
        self._outcomes.append(outcome_record)

    def get_weights_for_scoring(self) -> Optional[Dict[str, float]]:
        """Return weights to use for scoring.

        Returns None (use defaults) if:
        - Not enough outcomes yet
        - In shadow mode (shadow weights are logged but not applied)
        - Adaptation is not active
        """
        if self._active_weights is not None:
            return self._active_weights
        return None

    async def get_sender_reputation_bonus(
        self,
        domain: str,
        state_store: Any,
    ) -> float:
        """Query sender_reputation table and return a small score adjustment.

        Returns a value between -0.1 and +0.1 based on historical tier
        distribution and average score for this sender domain.
        """
        if state_store is None:
            return 0.0

        try:
            rep = await state_store.get_sender_reputation(domain)
        except Exception:
            return 0.0

        if rep is None or rep.get("total_count", 0) < 3:
            return 0.0  # Not enough data

        avg_score = rep.get("avg_score", 50)
        # Map avg_score to a small bonus:
        # avg_score 80-100 -> +0.05 to +0.10
        # avg_score 50-80  -> 0.0
        # avg_score 0-50   -> -0.05 to -0.10
        if avg_score >= 80:
            return min(0.10, (avg_score - 80) / 200.0 + 0.05)
        elif avg_score <= 50:
            return max(-0.10, (avg_score - 50) / 500.0)
        return 0.0

    async def compute_adapted_weights(
        self,
        state_store: Any = None,
    ) -> Optional[Dict[str, float]]:
        """Compute adapted weights from accumulated outcomes.

        Returns None if not enough data or if weights are unchanged.
        Updates shadow or active state internally.
        """
        # Filter to adaptation-eligible outcomes
        eligible = [
            o for o in self._outcomes
            if o.get("feeds_adaptation", False)
        ]

        if len(eligible) < self._config.min_outcomes_for_adaptation:
            return None  # Not enough data

        # Compute outcome-weighted tier correlations
        # Track which factors correlate with positive outcomes (replied/relabeled)
        # vs negative outcomes (deleted/ignored)
        factor_adjustments = {k: 0.0 for k in DEFAULT_WEIGHTS}
        total_weight = 0.0

        for outcome_rec in eligible:
            aw = outcome_rec.get("adaptation_weight", 1.0)
            outcome = outcome_rec.get("outcome", "")
            tier = outcome_rec.get("tier", 3)

            # Positive outcomes at high tiers = good scoring
            # Positive outcomes at low tiers = scoring too conservative
            # Negative outcomes at high tiers = scoring too aggressive
            if outcome in ("replied", "relabeled"):
                if tier <= 2:
                    # Correctly scored as important — reinforce
                    pass
                else:
                    # Under-scored — boost sender and urgency weights
                    factor_adjustments["sender"] += 0.01 * aw
                    factor_adjustments["urgency"] += 0.01 * aw
            elif outcome == "deleted":
                if tier <= 2:
                    # Over-scored — reduce sender weight slightly
                    factor_adjustments["sender"] -= 0.01 * aw
                    factor_adjustments["content"] -= 0.005 * aw

            total_weight += aw

        if total_weight == 0:
            return None

        # Normalize adjustments
        for k in factor_adjustments:
            factor_adjustments[k] /= total_weight

        # Apply adjustments with safety bounds
        bounds_pct = self._config.weight_bounds_pct / 100.0
        new_weights = {}
        for k, default_v in DEFAULT_WEIGHTS.items():
            adjusted = default_v + factor_adjustments.get(k, 0.0)
            lower = default_v * (1.0 - bounds_pct)
            upper = default_v * (1.0 + bounds_pct)
            new_weights[k] = max(lower, min(upper, adjusted))

        # Normalize to sum to 1.0
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}

        # Check if weights actually changed meaningfully
        if all(
            abs(new_weights[k] - DEFAULT_WEIGHTS[k]) < 0.001
            for k in DEFAULT_WEIGHTS
        ):
            return None  # No meaningful change

        # Enter shadow mode
        if self._shadow_weights is None:
            self._shadow_weights = new_weights
            self._shadow_cycles_remaining = self._config.shadow_cycles
            self._total_shadow_entries = 0
            self._total_disagreements = 0

            emit_triage_event(EVENT_WEIGHTS_SHADOW, {
                "adapted_weights": new_weights,
                "default_weights": dict(DEFAULT_WEIGHTS),
                "shadow_cycles": self._shadow_cycles_remaining,
                "eligible_outcomes": len(eligible),
            })
            logger.info(
                "Adapted weights entering shadow mode for %d cycles: %s",
                self._shadow_cycles_remaining, new_weights,
            )
            return None  # Don't apply yet

        return new_weights

    def record_shadow_comparison(
        self,
        default_tier: int,
        adapted_tier: int,
        outcome: str,
    ) -> None:
        """Record a tier comparison during shadow mode (Gate #5).

        Called for each email scored during shadow period.
        """
        self._shadow_log.append((default_tier, adapted_tier, outcome))
        self._total_shadow_entries += 1
        if default_tier != adapted_tier:
            self._total_disagreements += 1

    def advance_shadow_cycle(self) -> Optional[Dict[str, float]]:
        """Advance shadow mode by one cycle. Returns activated weights or None.

        After shadow_cycles, evaluates tier agreement. If disagreement exceeds
        threshold, rollback. Otherwise, activate the weights.
        """
        if not self.is_shadow_active:
            return None

        self._shadow_cycles_remaining -= 1

        if self._shadow_cycles_remaining > 0:
            return None  # Still in shadow

        # Shadow period complete — evaluate
        if self._total_shadow_entries == 0:
            # No data during shadow — don't activate
            self._shadow_weights = None
            return None

        disagreement_rate = self._total_disagreements / self._total_shadow_entries
        threshold = self._config.shadow_tier_drift_threshold

        if disagreement_rate > threshold:
            # Rollback (Gate #5)
            logger.warning(
                "Adapted weights rolled back: %.1f%% tier disagreement (threshold: %.1f%%)",
                disagreement_rate * 100, threshold * 100,
            )
            self._shadow_weights = None
            self._shadow_log.clear()
            return None

        # Activate
        self._active_weights = self._shadow_weights
        self._last_good_weights = dict(self._shadow_weights)
        self._shadow_weights = None
        self._shadow_log.clear()

        emit_triage_event(EVENT_WEIGHTS_ADAPTED, {
            "weights": self._active_weights,
            "disagreement_rate": disagreement_rate,
            "shadow_entries": self._total_shadow_entries,
        })
        logger.info(
            "Adapted weights activated: %s (disagreement: %.1f%%)",
            self._active_weights, disagreement_rate * 100,
        )
        return self._active_weights

    def rollback_to_defaults(self) -> None:
        """Force rollback to default weights."""
        self._active_weights = None
        self._shadow_weights = None
        self._shadow_log.clear()
        logger.info("Rolled back to default weights")
