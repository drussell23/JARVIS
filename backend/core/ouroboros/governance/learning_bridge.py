# backend/core/ouroboros/governance/learning_bridge.py
"""
Learning Bridge -- Operation Feedback to LearningMemory
========================================================

Publishes governance operation outcomes to the existing
:class:`LearningMemory` for future consultation.  When the Ouroboros
engine plans a new operation, it can check whether a similar
goal+file+error combination has been tried before (and failed).

Fault isolation: LearningMemory failures are logged but never block
the governance pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

from backend.core.ouroboros.governance.ledger import OperationState

logger = logging.getLogger("Ouroboros.LearningBridge")


_SUCCESS_STATES = {OperationState.APPLIED}


@dataclass
class OperationOutcome:
    """Summary of a governance operation for learning feedback."""

    op_id: str
    goal: str
    target_files: List[str]
    final_state: OperationState
    error_pattern: Optional[str] = None
    solution_pattern: Optional[str] = None

    @property
    def success(self) -> bool:
        """Whether the operation completed successfully."""
        return self.final_state in _SUCCESS_STATES


class _GovernanceImprovementType(Enum):
    """Minimal ImprovementType stand-in for LearningMemory compatibility.

    LearningMemory._hash_request accesses ``request.improvement_type.value``,
    so we provide an enum member with a ``.value`` attribute.
    """

    GOVERNANCE = "governance"


class _GovernanceRequest:
    """Minimal request object for LearningMemory API compatibility.

    LearningMemory._hash_request reads ``target_file``, ``goal``, and
    ``improvement_type.value``.  This adapter provides exactly those
    attributes without depending on the full ImprovementRequest dataclass.
    """

    def __init__(self, goal: str, target_file: str) -> None:
        self.goal = goal
        self.target_file = target_file
        self.improvement_type = _GovernanceImprovementType.GOVERNANCE


class LearningBridge:
    """Bridge between governance outcomes and LearningMemory.

    Parameters
    ----------
    learning_memory:
        An instance of :class:`LearningMemory` (or ``None`` for a no-op
        bridge).  When ``None``, all queries return safe defaults and
        publishes are silently dropped.
    """

    def __init__(self, learning_memory: Optional[Any] = None) -> None:
        self._memory = learning_memory

    async def publish(self, outcome: OperationOutcome) -> None:
        """Publish an operation outcome to LearningMemory.

        Records the outcome as a learning entry so that subsequent
        operations can consult past results before re-attempting the
        same goal+file+error combination.

        Fault-isolated: exceptions from LearningMemory are logged
        but never propagated.
        """
        if self._memory is None:
            return

        try:
            target = outcome.target_files[0] if outcome.target_files else "unknown"
            request = _GovernanceRequest(goal=outcome.goal, target_file=target)
            error_pattern = outcome.error_pattern or "none"

            await self._memory.record_attempt(
                request=request,
                error_pattern=error_pattern,
                solution_pattern=outcome.solution_pattern,
                success=outcome.success,
            )
        except Exception as exc:
            logger.warning(
                "LearningBridge: failed to publish outcome for op=%s: %s",
                outcome.op_id,
                exc,
            )

    async def should_skip(
        self, goal: str, target_file: str, error_pattern: str
    ) -> bool:
        """Check if a goal+file+error has failed too many times.

        Delegates to :meth:`LearningMemory.should_skip_pattern`.
        Returns ``False`` if no memory is configured or on error.
        """
        if self._memory is None:
            return False

        try:
            request = _GovernanceRequest(goal=goal, target_file=target_file)
            return await self._memory.should_skip_pattern(
                request=request, error_pattern=error_pattern
            )
        except Exception as exc:
            logger.warning("LearningBridge: should_skip error: %s", exc)
            return False

    async def get_known_solution(
        self, goal: str, target_file: str, error_pattern: str
    ) -> Optional[str]:
        """Check if a known solution exists for this goal+file+error.

        Delegates to :meth:`LearningMemory.get_known_solution`.
        Returns ``None`` if no memory is configured or on error.
        """
        if self._memory is None:
            return None

        try:
            request = _GovernanceRequest(goal=goal, target_file=target_file)
            return await self._memory.get_known_solution(
                request=request, error_pattern=error_pattern
            )
        except Exception as exc:
            logger.warning("LearningBridge: get_known_solution error: %s", exc)
            return None
