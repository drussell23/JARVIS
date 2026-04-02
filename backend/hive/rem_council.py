"""
REM Council Runner

Orchestrates three review modules (health, graduation, manifesto) sequentially
within a shared call budget. Unused calls from earlier modules carry forward
to later ones, and budget exhaustion causes remaining modules to be skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# RESULT DATACLASS
# ============================================================================


@dataclass
class RemSessionResult:
    """Aggregated result of a single REM council session."""

    threads_created: List[str] = field(default_factory=list)
    calls_used: int = 0
    calls_budget: int = 0
    should_escalate: bool = False
    escalation_thread_id: Optional[str] = None
    modules_completed: List[str] = field(default_factory=list)
    modules_skipped: List[str] = field(default_factory=list)


# ============================================================================
# REVIEW MODULE PROTOCOL
# ============================================================================


class ReviewModule(Protocol):
    """Protocol that each review module must satisfy.

    Returns:
        Tuple of (thread_ids, calls_used, should_escalate, escalation_thread_id).
    """

    async def run(
        self, budget: int
    ) -> Tuple[List[str], int, bool, Optional[str]]: ...


# ============================================================================
# REM COUNCIL
# ============================================================================


class RemCouncil:
    """Runs review modules sequentially within a shared call budget.

    Parameters:
        health_scanner: Module that audits runtime health.
        graduation_auditor: Module that reviews graduation criteria.
        manifesto_reviewer: Module that checks manifesto alignment.
        max_calls: Total call budget for the session (default 50).
    """

    def __init__(
        self,
        health_scanner: ReviewModule,
        graduation_auditor: ReviewModule,
        manifesto_reviewer: ReviewModule,
        max_calls: int = 50,
    ) -> None:
        self._modules: List[Tuple[str, ReviewModule]] = [
            ("health", health_scanner),
            ("graduation", graduation_auditor),
            ("manifesto", manifesto_reviewer),
        ]
        self._max_calls = max_calls

    async def run_session(self) -> RemSessionResult:
        """Execute all review modules sequentially, respecting the call budget.

        Budget splitting:
            - Each module starts with ``per_module = max_calls // len(modules)``.
            - Remaining budget (``max_calls - total_calls_used``) is available to
              each subsequent module, so unused calls carry forward.
            - A module is skipped when remaining budget <= 0.
            - The first module that signals escalation sets the session-level
              escalation flag and thread ID.
            - Exceptions in a module are logged; the module is marked completed
              (not skipped) and execution continues.
        """
        result = RemSessionResult(calls_budget=self._max_calls)
        per_module = self._max_calls // len(self._modules)
        total_used = 0

        for name, module in self._modules:
            remaining = self._max_calls - total_used
            budget = min(per_module, remaining) if remaining > per_module else remaining

            if remaining <= 0:
                result.modules_skipped.append(name)
                continue

            try:
                thread_ids, calls_used, should_escalate, escalation_id = (
                    await module.run(budget)
                )
            except Exception:
                logger.warning(
                    "Module %r raised an exception; marking completed and continuing",
                    name,
                    exc_info=True,
                )
                result.modules_completed.append(name)
                continue

            total_used += calls_used
            result.threads_created.extend(thread_ids)
            result.modules_completed.append(name)

            if should_escalate and not result.should_escalate:
                result.should_escalate = True
                result.escalation_thread_id = escalation_id

        result.calls_used = total_used
        return result
