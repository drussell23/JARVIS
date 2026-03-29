"""
PlanDecomposer — deterministic ArchitecturalPlan -> List[IntentEnvelope] conversion.

Converts each PlanStep into an IntentEnvelope using Kahn's algorithm to produce
a topologically ordered sequence that respects inter-step dependencies.
Within a single dependency tier, steps are ordered by step_index for determinism.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import List

from backend.core.ouroboros.architect.plan import ArchitecturalPlan, PlanStep
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)


class PlanDecomposer:
    """Deterministically decomposes an ArchitecturalPlan into IntentEnvelopes."""

    @staticmethod
    def decompose(plan: ArchitecturalPlan, saga_id: str) -> List[IntentEnvelope]:
        """Convert *plan* into one IntentEnvelope per PlanStep.

        Steps are emitted in topological order (Kahn's algorithm).  Within
        each independent tier the steps are sorted by ``step_index`` for
        determinism.

        Parameters
        ----------
        plan:
            The fully specified immutable plan to decompose.
        saga_id:
            Identifier of the enclosing SagaOrchestrator run; stored in every
            envelope's evidence dict for correlation.

        Returns
        -------
        List[IntentEnvelope]
            Ordered list of envelopes, one per step, safe to submit to the
            Unified Intake Router in sequence.
        """
        ordered_steps = PlanDecomposer._topological_order(plan)
        envelopes: List[IntentEnvelope] = []

        for step in ordered_steps:
            evidence = {
                "saga_id": saga_id,
                "plan_hash": plan.plan_hash,
                "step_index": step.step_index,
                "intent_kind": step.intent_kind.value,
                "analysis_complete": True,
            }
            env = make_envelope(
                source="architecture",
                description=step.description,
                target_files=step.target_paths,
                repo=step.repo,
                confidence=1.0,
                urgency="normal",
                evidence=evidence,
                requires_human_ack=False,
            )
            envelopes.append(env)

        return envelopes

    @staticmethod
    def _topological_order(plan: ArchitecturalPlan) -> List[PlanStep]:
        """Return PlanSteps in topological order using Kahn's algorithm.

        Steps within the same dependency tier are sorted by ``step_index``
        to guarantee a deterministic, reproducible ordering across runs.

        Parameters
        ----------
        plan:
            The plan whose steps should be sorted.

        Returns
        -------
        List[PlanStep]
            Steps ordered so that every dependency appears before the step
            that requires it.

        Raises
        ------
        ValueError
            If the dependency graph contains a cycle.
        """
        # Index steps by step_index for fast lookup.
        step_map: dict[int, PlanStep] = {s.step_index: s for s in plan.steps}

        # Build in-degree map and adjacency list (dependency -> dependants).
        in_degree: dict[int, int] = {idx: 0 for idx in step_map}
        dependants: dict[int, List[int]] = defaultdict(list)

        for step in plan.steps:
            for dep_idx in step.depends_on:
                in_degree[step.step_index] += 1
                dependants[dep_idx].append(step.step_index)

        # Seed the queue with all zero-in-degree steps, sorted for determinism.
        queue: deque[int] = deque(
            sorted(idx for idx, deg in in_degree.items() if deg == 0)
        )

        result: List[PlanStep] = []

        while queue:
            # Pop the lowest step_index from the front (already sorted at insert).
            current_idx = queue.popleft()
            result.append(step_map[current_idx])

            # Reduce in-degree for all steps that depend on current_idx.
            newly_free: List[int] = []
            for child_idx in dependants[current_idx]:
                in_degree[child_idx] -= 1
                if in_degree[child_idx] == 0:
                    newly_free.append(child_idx)

            # Insert newly freed steps in sorted order to maintain determinism
            # within each tier.
            for free_idx in sorted(newly_free):
                # Find the correct insertion point to keep the deque sorted.
                inserted = False
                for i, queued_idx in enumerate(queue):
                    if free_idx < queued_idx:
                        # deque doesn't support O(1) mid-insert; convert briefly.
                        lst = list(queue)
                        lst.insert(i, free_idx)
                        queue = deque(lst)
                        inserted = True
                        break
                if not inserted:
                    queue.append(free_idx)

        if len(result) != len(plan.steps):
            processed = {s.step_index for s in result}
            unprocessed = [idx for idx in step_map if idx not in processed]
            raise ValueError(
                f"Cycle detected in plan dependency graph. "
                f"Steps involved: {sorted(unprocessed)}"
            )

        return result
