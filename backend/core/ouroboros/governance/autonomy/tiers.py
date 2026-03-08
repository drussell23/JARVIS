"""backend/core/ouroboros/governance/autonomy/tiers.py

Autonomy tier definitions, intelligence snapshots, and per-signal configuration.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Tuple


class AutonomyTier(enum.Enum):
    """Selective autonomy levels for the self-programming pipeline."""
    OBSERVE = "observe"
    SUGGEST = "suggest"
    GOVERNED = "governed"
    AUTONOMOUS = "autonomous"


TIER_ORDER: Tuple[AutonomyTier, ...] = (
    AutonomyTier.OBSERVE,
    AutonomyTier.SUGGEST,
    AutonomyTier.GOVERNED,
    AutonomyTier.AUTONOMOUS,
)


class CognitiveLoad(enum.IntEnum):
    """User cognitive load levels (from CAI)."""
    LOW = 0
    MEDIUM = 1
    HIGH = 2


class WorkContext(enum.Enum):
    """User work context categories (from CAI)."""
    CODING = "coding"
    REVIEWING = "reviewing"
    MEETINGS = "meetings"
    IDLE = "idle"


@dataclass(frozen=True)
class CAISnapshot:
    """Point-in-time context from Context Awareness Intelligence."""
    cognitive_load: CognitiveLoad
    work_context: WorkContext
    safety_level: str  # "SAFE" | "CAUTION" | "UNSAFE"


@dataclass(frozen=True)
class UAESnapshot:
    """Point-in-time context from Unified Awareness Engine."""
    confidence: float  # 0.0 -- 1.0


@dataclass(frozen=True)
class SAISnapshot:
    """Point-in-time context from Situational Awareness Intelligence."""
    ram_percent: float
    system_locked: bool
    anomaly_detected: bool


@dataclass(frozen=True)
class GraduationMetrics:
    """Tracks operational history for trust graduation decisions."""
    observations: int = 0
    false_positives: int = 0
    successful_ops: int = 0
    rollback_count: int = 0
    postmortem_streak: int = 0
    human_confirmations: int = 0


@dataclass(frozen=True)
class SignalAutonomyConfig:
    """Autonomy configuration for a (trigger_source, repo, canary_slice) triple."""
    trigger_source: str
    repo: str
    canary_slice: str
    current_tier: AutonomyTier
    graduation_metrics: GraduationMetrics
    defer_during_cognitive_load: CognitiveLoad = CognitiveLoad.HIGH
    defer_during_work_context: Tuple[WorkContext, ...] = (WorkContext.MEETINGS,)
    require_user_active: bool = False

    @property
    def config_key(self) -> Tuple[str, str, str]:
        """Unique key for this config: (trigger_source, repo, canary_slice)."""
        return (self.trigger_source, self.repo, self.canary_slice)
