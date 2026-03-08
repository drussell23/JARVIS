"""backend/core/ouroboros/governance/autonomy/graduator.py

Trust Graduator — tier promotion and demotion engine.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict, Optional, Tuple

from .tiers import (
    AutonomyTier,
    GraduationMetrics,
    SignalAutonomyConfig,
    TIER_ORDER,
)

logger = logging.getLogger(__name__)

# Graduation criteria
_MIN_OBSERVATIONS = 20
_MAX_FALSE_POSITIVES = 0
_MIN_HUMAN_CONFIRMATIONS = 5
_MIN_SUCCESSFUL_OPS_GOVERNED = 30
_MAX_ROLLBACK_RATE = 0.05
_MIN_SUCCESSFUL_OPS_AUTONOMOUS = 50
_MAX_ROLLBACKS_AUTONOMOUS = 0

# Demotion targets
_DEMOTION_MAP: Dict[str, AutonomyTier] = {
    "rollback": AutonomyTier.GOVERNED,
    "postmortem_streak": AutonomyTier.SUGGEST,
    "anomaly": AutonomyTier.OBSERVE,
    "break_glass": AutonomyTier.OBSERVE,
}

ConfigKey = Tuple[str, str, str]


class TrustGraduator:
    """Manages autonomy tier promotions and demotions."""

    def __init__(self) -> None:
        self._configs: Dict[ConfigKey, SignalAutonomyConfig] = {}

    def register(self, config: SignalAutonomyConfig) -> None:
        """Register or replace a signal autonomy config."""
        self._configs[config.config_key] = config

    def get_config(self, trigger_source: str, repo: str, canary_slice: str) -> Optional[SignalAutonomyConfig]:
        """Get config for a triple, or None if not registered."""
        return self._configs.get((trigger_source, repo, canary_slice))

    def all_configs(self) -> Tuple[SignalAutonomyConfig, ...]:
        """Return all registered configs."""
        return tuple(self._configs.values())

    def check_graduation(self, trigger_source: str, repo: str, canary_slice: str) -> Optional[AutonomyTier]:
        """Check if a triple qualifies for promotion. Returns new tier or None."""
        config = self._configs.get((trigger_source, repo, canary_slice))
        if config is None:
            return None

        tier = config.current_tier
        metrics = config.graduation_metrics

        if tier == AutonomyTier.OBSERVE:
            if (
                metrics.observations >= _MIN_OBSERVATIONS
                and metrics.false_positives <= _MAX_FALSE_POSITIVES
                and metrics.human_confirmations >= _MIN_HUMAN_CONFIRMATIONS
            ):
                return AutonomyTier.SUGGEST
        elif tier == AutonomyTier.SUGGEST:
            if metrics.successful_ops >= _MIN_SUCCESSFUL_OPS_GOVERNED:
                rollback_rate = (
                    metrics.rollback_count / metrics.successful_ops
                    if metrics.successful_ops > 0 else 1.0
                )
                if rollback_rate <= _MAX_ROLLBACK_RATE:
                    return AutonomyTier.GOVERNED
        elif tier == AutonomyTier.GOVERNED:
            if (
                metrics.successful_ops >= _MIN_SUCCESSFUL_OPS_AUTONOMOUS
                and metrics.rollback_count <= _MAX_ROLLBACKS_AUTONOMOUS
            ):
                return AutonomyTier.AUTONOMOUS

        return None

    def promote(self, trigger_source: str, repo: str, canary_slice: str, new_tier: AutonomyTier) -> SignalAutonomyConfig:
        """Apply a promotion. Raises ValueError if new_tier is not higher."""
        key = (trigger_source, repo, canary_slice)
        config = self._configs[key]
        current_idx = TIER_ORDER.index(config.current_tier)
        new_idx = TIER_ORDER.index(new_tier)
        if new_idx <= current_idx:
            raise ValueError(
                f"Cannot promote {key} from {config.current_tier.value} to {new_tier.value}"
            )
        updated = replace(config, current_tier=new_tier)
        self._configs[key] = updated
        logger.info("Trust graduation: %s %s -> %s", key, config.current_tier.value, new_tier.value)
        return updated

    def demote(self, trigger_source: str, repo: str, canary_slice: str, reason: str) -> AutonomyTier:
        """Demote a config based on trigger reason. Returns new tier."""
        key = (trigger_source, repo, canary_slice)
        config = self._configs[key]
        target_tier = _DEMOTION_MAP.get(reason, AutonomyTier.OBSERVE)

        # Never promote via demotion
        current_idx = TIER_ORDER.index(config.current_tier)
        target_idx = TIER_ORDER.index(target_tier)
        if target_idx >= current_idx:
            target_tier = TIER_ORDER[max(0, current_idx - 1)]

        updated = replace(config, current_tier=target_tier, graduation_metrics=GraduationMetrics())
        self._configs[key] = updated
        logger.info("Trust demotion: %s %s -> %s (reason=%s)", key, config.current_tier.value, target_tier.value, reason)
        return target_tier

    def break_glass_reset(self) -> None:
        """Demote ALL configs to OBSERVE."""
        for key, config in self._configs.items():
            self._configs[key] = replace(config, current_tier=AutonomyTier.OBSERVE, graduation_metrics=GraduationMetrics())
        logger.warning("Break-glass reset: all autonomy tiers demoted to OBSERVE")
