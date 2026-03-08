"""backend/core/ouroboros/governance/autonomy/gate.py

Autonomy Gate — multi-system decision function.

Checks CAI (cognitive load, work context), UAE (pattern confidence), and
SAI (resource pressure, system state) before allowing an autonomous operation.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import logging
from typing import Tuple

from .tiers import (
    AutonomyTier,
    CAISnapshot,
    SAISnapshot,
    SignalAutonomyConfig,
    UAESnapshot,
)

logger = logging.getLogger(__name__)

_RAM_PRESSURE_THRESHOLD = 90.0
_UAE_CONFIDENCE_THRESHOLD = 0.6


class AutonomyGate:
    """Multi-system gate for autonomous operations."""

    async def should_proceed(
        self,
        config: SignalAutonomyConfig,
        cai: CAISnapshot,
        uae: UAESnapshot,
        sai: SAISnapshot,
    ) -> Tuple[bool, str]:
        """Decide whether to auto-proceed or defer. Returns (proceed, reason_code)."""
        # 1. Autonomy tier check
        if config.current_tier is AutonomyTier.OBSERVE:
            return False, "tier:observe_only"

        # 2. CAI: Cognitive load
        if cai.cognitive_load >= config.defer_during_cognitive_load:
            return False, "cai:cognitive_load_high"

        # 3. CAI: Work context
        if cai.work_context in config.defer_during_work_context:
            return False, "cai:in_meeting"

        # 4. SAI: Resource pressure
        if sai.ram_percent > _RAM_PRESSURE_THRESHOLD:
            return False, "sai:memory_pressure"

        # 5. SAI: Screen locked
        if sai.system_locked:
            return False, "sai:screen_locked"

        # 6. UAE: Historical pattern confidence
        if uae.confidence < _UAE_CONFIDENCE_THRESHOLD:
            return False, "uae:low_pattern_confidence"

        # 7. Cross-system agreement
        if sai.anomaly_detected and cai.safety_level == "SAFE":
            return False, "disagreement:cai_safe_sai_anomaly"

        return True, "proceed"
