"""
Goal Decomposition Engine
==========================

Decomposes high-level goals into actionable sub-operations submitted
via the intake router.

Algorithm:
  1. If reasoning chain is active, expand the goal into sub-intents
  2. For each sub-intent, query TheOracle semantic search for target files
  3. Build IntentEnvelopes with shared correlation_id (saga)
  4. Submit to intake router with causal ordering

All goal-decomposed operations default to ``requires_human_ack=True`` —
only the AUTONOMOUS trust tier may auto-approve them.

Environment:
  JARVIS_GOAL_MAX_SUBTASKS  -- max sub-tasks per goal (default: 8)
  JARVIS_GOAL_MIN_CONFIDENCE -- min Oracle similarity to include file (default: 0.3)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.operation_id import generate_operation_id

logger = logging.getLogger("Ouroboros.GoalDecomposer")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SubTask:
    """A single decomposed sub-task."""

    intent: str
    target_files: Tuple[str, ...]
    confidence: float
    repo: str = "jarvis"


@dataclass
class GoalDecompositionResult:
    """Outcome of goal decomposition."""

    original_goal: str
    sub_tasks: List[SubTask]
    correlation_id: str
    submitted_count: int = 0
    skipped_count: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sub_tasks)


# ---------------------------------------------------------------------------
# GoalDecomposer
# ---------------------------------------------------------------------------


class GoalDecomposer:
    """Decomposes high-level goals into IntentEnvelopes via Oracle + reasoning chain.

    Parameters
    ----------
    oracle:
        TheOracle instance for semantic file search.
    intake_router:
        UnifiedIntakeRouter for submitting envelopes.
    reasoning_chain:
        Optional ReasoningChainOrchestrator for intent expansion.
    """

    def __init__(
        self,
        oracle: Any,
        intake_router: Any,
        reasoning_chain: Any = None,
    ):
        self._oracle = oracle
        self._router = intake_router
        self._chain = reasoning_chain
        self._max_subtasks = _env_int("JARVIS_GOAL_MAX_SUBTASKS", 8)
        self._min_confidence = _env_float("JARVIS_GOAL_MIN_CONFIDENCE", 0.3)

    async def decompose(
        self,
        goal: str,
        repo: str = "jarvis",
        requires_human_ack: bool = True,
    ) -> GoalDecompositionResult:
        """Decompose a goal into sub-operations and submit them.

        Parameters
        ----------
        goal:
            High-level goal description (e.g. "improve authentication security").
        repo:
            Target repository for the goal.
        requires_human_ack:
            Whether sub-tasks require human approval before execution.

        Returns
        -------
        GoalDecompositionResult
            Contains sub-tasks, submission counts, and any errors.
        """
        correlation_id = generate_operation_id("goal")

        # Step 1: Expand goal into sub-intents
        sub_intents = await self._expand_intents(goal)

        # Step 2: For each sub-intent, find target files via Oracle
        sub_tasks: List[SubTask] = []
        for intent_text in sub_intents[: self._max_subtasks]:
            files, confidence = await self._find_target_files(intent_text, repo)
            if files:
                sub_tasks.append(
                    SubTask(
                        intent=intent_text,
                        target_files=files,
                        confidence=confidence,
                        repo=repo,
                    )
                )

        result = GoalDecompositionResult(
            original_goal=goal,
            sub_tasks=sub_tasks,
            correlation_id=correlation_id,
        )

        if not sub_tasks:
            # If no files found, create a single sub-task with the repo root
            sub_tasks.append(
                SubTask(
                    intent=goal,
                    target_files=(".",),
                    confidence=0.5,
                    repo=repo,
                )
            )
            result.sub_tasks = sub_tasks

        # Step 3: Submit envelopes
        for task in sub_tasks:
            try:
                envelope = make_envelope(
                    source="ai_miner",
                    description=task.intent,
                    target_files=task.target_files,
                    repo=task.repo,
                    confidence=task.confidence,
                    urgency="normal",
                    evidence={
                        "goal": goal,
                        "correlation_id": correlation_id,
                        "signature": f"goal:{correlation_id}:{task.intent[:40]}",
                    },
                    requires_human_ack=requires_human_ack,
                    causal_id=correlation_id,
                )
                status = await self._router.ingest(envelope)
                if status in ("enqueued", "pending_ack"):
                    result.submitted_count += 1
                else:
                    result.skipped_count += 1
            except Exception as exc:
                result.errors.append(f"Failed to submit '{task.intent[:40]}': {exc}")
                result.skipped_count += 1

        logger.info(
            "Goal decomposed: %r -> %d sub-tasks (%d submitted, %d skipped)",
            goal[:60],
            result.total,
            result.submitted_count,
            result.skipped_count,
        )
        return result

    async def _expand_intents(self, goal: str) -> List[str]:
        """Use reasoning chain to expand goal into sub-intents.

        Falls back to the original goal as a single intent if the chain
        is unavailable or fails.
        """
        if self._chain is None:
            return [goal]

        try:
            config = getattr(self._chain, "_config", None)
            if config and not config.is_active():
                return [goal]

            result = await self._chain.process(
                command=goal,
                context={"mode": "goal_decomposition"},
                trace_id=generate_operation_id("chain"),
                deadline=None,
            )

            if result and result.handled and result.expanded_intents:
                logger.debug(
                    "Reasoning chain expanded goal into %d intents",
                    len(result.expanded_intents),
                )
                return result.expanded_intents

        except Exception as exc:
            logger.warning("Reasoning chain expansion failed: %s", exc)

        return [goal]

    async def _find_target_files(
        self, intent: str, repo: str
    ) -> Tuple[Tuple[str, ...], float]:
        """Use Oracle semantic search to find relevant files for an intent.

        Returns (target_files_tuple, average_confidence).
        """
        if self._oracle is None:
            return ((".",), 0.5)

        try:
            # Check if Oracle has semantic search capability
            if not hasattr(self._oracle, "semantic_search"):
                return ((".",), 0.5)

            results = await self._oracle.semantic_search(intent, k=5)
            if not results:
                return ((".",), 0.5)

            # Filter by minimum confidence and extract file paths
            files: List[str] = []
            total_score = 0.0
            for file_key, score in results:
                if score >= self._min_confidence:
                    # file_key format: "repo:file_path" — extract just the path
                    parts = file_key.split(":", 1)
                    file_path = parts[1] if len(parts) > 1 else parts[0]
                    files.append(file_path)
                    total_score += score

            if not files:
                return ((".",), 0.5)

            avg_confidence = total_score / len(files) if files else 0.5
            return (tuple(files), avg_confidence)

        except Exception as exc:
            logger.warning("Oracle semantic search failed: %s", exc)
            return ((".",), 0.5)
