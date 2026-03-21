"""GraduationTracker — tracks progress through autonomy graduation gates.

Monitors operation outcomes and determines which autonomy level the system
has earned based on observed reliability:

    Level 2 → 3: 20 proactive proposals, >80% accepted
    Level 3 → 4: 50 consecutive successes, zero rollbacks
    Level 4 → 5: 100 auto-approved simple ops, zero rollbacks
    Level 5 → 6: 7 days sustained, <2% rollback rate

Persists state to disk so progress survives restarts.
Emits telemetry via TelemetryBus for dashboard visibility.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class GraduationState:
    """Persistent state for graduation gate tracking."""
    # Level 2 → 3: proactive proposals
    proactive_proposals_total: int = 0
    proactive_proposals_accepted: int = 0

    # Level 3 → 4: consecutive successes
    consecutive_successes: int = 0
    consecutive_successes_peak: int = 0
    total_rollbacks: int = 0

    # Level 4 → 5: auto-approved operations
    auto_approved_total: int = 0
    auto_approved_rollbacks: int = 0

    # Level 5 → 6: sustained operation
    sustained_start_epoch: float = 0.0
    sustained_total_ops: int = 0
    sustained_rollback_count: int = 0

    # Current earned level
    current_level: int = 2

    # Metadata
    last_updated_epoch: float = field(default_factory=time.time)


class GraduationTracker:
    """Tracks and persists autonomy graduation gate progress.

    Usage:
        tracker = GraduationTracker(persistence_dir=Path("~/.jarvis/ouroboros"))
        tracker.record_operation_outcome(op_id, success=True, ...)
        level = tracker.current_level  # 2, 3, 4, 5, or 6
    """

    # Gate thresholds
    LEVEL_3_PROPOSALS_REQUIRED = 20
    LEVEL_3_ACCEPTANCE_RATE = 0.80
    LEVEL_4_CONSECUTIVE_REQUIRED = 50
    LEVEL_5_AUTO_APPROVED_REQUIRED = 100
    LEVEL_6_SUSTAINED_DAYS = 7
    LEVEL_6_MAX_ROLLBACK_RATE = 0.02

    def __init__(
        self,
        persistence_dir: Optional[Path] = None,
        telemetry_bus: Any = None,
    ) -> None:
        self._dir = persistence_dir or Path.home() / ".jarvis" / "ouroboros"
        self._path = self._dir / "graduation_state.json"
        self._bus = telemetry_bus
        self._state = self._load()

    @property
    def current_level(self) -> int:
        return self._state.current_level

    @property
    def state(self) -> GraduationState:
        return self._state

    def record_operation_outcome(
        self,
        op_id: str,
        success: bool,
        rolled_back: bool = False,
        auto_approved: bool = False,
        proactive: bool = False,
        proposal_accepted: Optional[bool] = None,
    ) -> int:
        """Record an operation outcome and re-evaluate graduation level.

        Returns the (possibly updated) current level.
        """
        s = self._state

        # Track consecutive successes
        if success and not rolled_back:
            s.consecutive_successes += 1
            s.consecutive_successes_peak = max(
                s.consecutive_successes_peak, s.consecutive_successes
            )
        else:
            s.consecutive_successes = 0

        # Track rollbacks
        if rolled_back:
            s.total_rollbacks += 1

        # Track proactive proposals (Level 2 → 3)
        if proactive:
            s.proactive_proposals_total += 1
            if proposal_accepted:
                s.proactive_proposals_accepted += 1

        # Track auto-approved ops (Level 4 → 5)
        if auto_approved:
            s.auto_approved_total += 1
            if rolled_back:
                s.auto_approved_rollbacks += 1

        # Track sustained operation (Level 5 → 6)
        if s.current_level >= 5:
            if s.sustained_start_epoch == 0.0:
                s.sustained_start_epoch = time.time()
            s.sustained_total_ops += 1
            if rolled_back:
                s.sustained_rollback_count += 1

        # Re-evaluate level
        new_level = self._evaluate_level()
        if new_level != s.current_level:
            logger.info(
                "[GraduationTracker] Level change: %d → %d",
                s.current_level, new_level,
            )
            s.current_level = new_level
            self._emit_level_change(op_id, new_level)

        s.last_updated_epoch = time.time()
        self._save()
        return s.current_level

    def health(self) -> Dict[str, Any]:
        s = self._state
        return {
            "current_level": s.current_level,
            "consecutive_successes": s.consecutive_successes,
            "peak_consecutive": s.consecutive_successes_peak,
            "total_rollbacks": s.total_rollbacks,
            "proactive_proposals": f"{s.proactive_proposals_accepted}/{s.proactive_proposals_total}",
            "auto_approved": f"{s.auto_approved_total} ({s.auto_approved_rollbacks} rollbacks)",
        }

    def _evaluate_level(self) -> int:
        """Compute the highest level the system has earned. Never decreases."""
        s = self._state
        level = 2  # minimum

        # Level 3: proactive proposals accepted
        if (
            s.proactive_proposals_total >= self.LEVEL_3_PROPOSALS_REQUIRED
            and s.proactive_proposals_accepted / max(1, s.proactive_proposals_total)
            >= self.LEVEL_3_ACCEPTANCE_RATE
        ):
            level = 3

        # Level 4: consecutive successes
        if level >= 3 and s.consecutive_successes_peak >= self.LEVEL_4_CONSECUTIVE_REQUIRED:
            level = 4

        # Level 5: auto-approved with zero rollbacks
        if (
            level >= 4
            and s.auto_approved_total >= self.LEVEL_5_AUTO_APPROVED_REQUIRED
            and s.auto_approved_rollbacks == 0
        ):
            level = 5

        # Level 6: sustained operation
        if level >= 5 and s.sustained_start_epoch > 0:
            days = (time.time() - s.sustained_start_epoch) / 86400.0
            rollback_rate = (
                s.sustained_rollback_count / max(1, s.sustained_total_ops)
            )
            if days >= self.LEVEL_6_SUSTAINED_DAYS and rollback_rate <= self.LEVEL_6_MAX_ROLLBACK_RATE:
                level = 6

        # Never decrease
        return max(level, s.current_level)

    def _load(self) -> GraduationState:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                return GraduationState(**{
                    k: v for k, v in data.items()
                    if k in GraduationState.__dataclass_fields__
                })
        except Exception as exc:
            logger.warning("[GraduationTracker] Load failed: %s — starting fresh", exc)
        return GraduationState()

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(asdict(self._state), indent=2))
        except Exception as exc:
            logger.warning("[GraduationTracker] Save failed: %s", exc)

    def _emit_level_change(self, op_id: str, new_level: int) -> None:
        if self._bus is None:
            return
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope
            self._bus.emit(TelemetryEnvelope.create(
                event_schema="lifecycle.transition@1.0.0",
                source="graduation_tracker",
                trace_id=op_id,
                span_id="level_change",
                partition_key="lifecycle",
                payload={
                    "from_state": f"LEVEL_{new_level - 1}",
                    "to_state": f"LEVEL_{new_level}",
                    "trigger": "graduation_gate_passed",
                    "reason_code": f"earned_level_{new_level}",
                    "attempt": 0,
                    "restarts_in_window": 0,
                    "elapsed_in_prev_state_ms": 0,
                },
            ))
        except Exception:
            pass
