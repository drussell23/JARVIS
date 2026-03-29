"""
PlanValidator — 10 deterministic structural rules for ArchitecturalPlan.

Zero model calls. All checks are pure structural / graph-theoretic logic.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import List

from backend.core.ouroboros.architect.plan import ArchitecturalPlan


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of a single validation run.

    Attributes
    ----------
    passed:
        ``True`` iff all rules were satisfied.
    reasons:
        Human-readable explanation for every rule violation found.
        Empty when ``passed`` is ``True``.
    """

    passed: bool
    reasons: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class PlanValidator:
    """Deterministic structural validator for :class:`ArchitecturalPlan`.

    Rules (checked in order; ALL violations are collected before returning):

    1. At least one step (empty plan fails).
    2. Step count does not exceed *max_steps*.
    3. No duplicate ``step_index`` values.
    4. Step indices form the contiguous range ``0 .. N-1`` with no gaps.
    5. All ``depends_on`` values reference valid step indices.
    6. The dependency graph is acyclic (verified via Kahn's algorithm).
    7. Every step has at least one ``target_path``.
    8. No ``target_path`` (or ``ancillary_path``) contains ``".."``
       (prevents repo-root escapes).
    9. ``repos_affected`` equals the union of all step ``repo`` fields.
    10. Every ``AcceptanceCheck.run_after_step`` that is set references a
        valid step index.
    """

    def __init__(self, max_steps: int = 10) -> None:
        self._max_steps = max_steps

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self, plan: ArchitecturalPlan) -> ValidationResult:
        """Run all 10 rules against *plan* and return a :class:`ValidationResult`."""
        reasons: list[str] = []

        steps = plan.steps
        n = len(steps)

        # ------------------------------------------------------------------
        # Rule 1 — at least one step
        # ------------------------------------------------------------------
        if n == 0:
            reasons.append(
                "Plan has no steps; at least one step is required."
            )
            # None of the remaining structural rules make sense on an empty
            # step list, but we still run the non-step rules (9, 10).
            self._check_repos_affected(plan, set(), reasons)
            self._check_acceptance_checks(plan, set(), reasons)
            return ValidationResult(passed=False, reasons=reasons)

        # ------------------------------------------------------------------
        # Rule 2 — step count <= max_steps
        # ------------------------------------------------------------------
        if n > self._max_steps:
            reasons.append(
                f"Plan has {n} steps, which exceeds max_steps={self._max_steps}."
            )

        # ------------------------------------------------------------------
        # Rule 3 — no duplicate step indices
        # ------------------------------------------------------------------
        seen_indices: set[int] = set()
        duplicates: set[int] = set()
        for step in steps:
            if step.step_index in seen_indices:
                duplicates.add(step.step_index)
            seen_indices.add(step.step_index)

        if duplicates:
            reasons.append(
                f"Duplicate step indices detected: {sorted(duplicates)}."
            )

        # ------------------------------------------------------------------
        # Rule 4 — indices form 0..N-1 with no gaps
        # ------------------------------------------------------------------
        expected = set(range(n))
        if seen_indices != expected:
            missing = sorted(expected - seen_indices)
            extra = sorted(seen_indices - expected)
            parts: list[str] = []
            if missing:
                parts.append(f"missing {missing}")
            if extra:
                parts.append(f"unexpected {extra}")
            reasons.append(
                f"Step indices must form the contiguous range 0..{n - 1}; "
                + ", ".join(parts)
                + "."
            )

        # Build a lookup map for subsequent rules (using the actual indices,
        # not necessarily 0..N-1 so that rule 5/6 can still report useful
        # errors even if rule 4 already fired).
        valid_indices: set[int] = seen_indices

        # ------------------------------------------------------------------
        # Rule 5 — all depends_on values reference valid step indices
        # ------------------------------------------------------------------
        bad_refs: list[tuple[int, int]] = []  # (step_index, bad_dep)
        for step in steps:
            for dep in step.depends_on:
                if dep not in valid_indices:
                    bad_refs.append((step.step_index, dep))

        if bad_refs:
            detail = ", ".join(
                f"step {s} depends_on {d}" for s, d in bad_refs
            )
            reasons.append(
                f"depends_on references non-existent step indices: {detail}."
            )

        # ------------------------------------------------------------------
        # Rule 6 — DAG is acyclic (Kahn's topological sort)
        # ------------------------------------------------------------------
        # Only run if there are no invalid depends_on references; otherwise
        # the graph is malformed and Kahn's would give a false-positive.
        if not bad_refs:
            cycle_reason = self._check_acyclic(steps, valid_indices)
            if cycle_reason:
                reasons.append(cycle_reason)

        # ------------------------------------------------------------------
        # Rule 7 — every step has at least one target_path
        # ------------------------------------------------------------------
        empty_target_steps = [
            step.step_index for step in steps if not step.target_paths
        ]
        if empty_target_steps:
            reasons.append(
                f"Steps {empty_target_steps} have empty target_paths; "
                "at least one target path is required per step."
            )

        # ------------------------------------------------------------------
        # Rule 8 — no ".." in target_paths or ancillary_paths
        # ------------------------------------------------------------------
        escape_violations: list[str] = []
        for step in steps:
            for path in (*step.target_paths, *step.ancillary_paths):
                if ".." in path.split("/") or path.startswith("../"):
                    escape_violations.append(
                        f"step {step.step_index}: '{path}'"
                    )

        if escape_violations:
            reasons.append(
                "Paths must be repo-relative and must not contain '..': "
                + "; ".join(escape_violations)
                + "."
            )

        # ------------------------------------------------------------------
        # Rule 9 — repos_affected matches union of step repos
        # ------------------------------------------------------------------
        step_repos = {step.repo for step in steps}
        self._check_repos_affected(plan, step_repos, reasons)

        # ------------------------------------------------------------------
        # Rule 10 — AcceptanceCheck.run_after_step references are valid
        # ------------------------------------------------------------------
        self._check_acceptance_checks(plan, valid_indices, reasons)

        return ValidationResult(passed=len(reasons) == 0, reasons=reasons)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_acyclic(
        steps: tuple,
        valid_indices: set[int],
    ) -> str | None:
        """Return an error string if the dependency graph contains a cycle.

        Uses Kahn's topological sort: build in-degree counts and adjacency
        list, process nodes with in-degree 0 iteratively, then check whether
        all nodes were visited.
        """
        # Map step_index → PlanStep for fast look-up
        step_map = {s.step_index: s for s in steps}

        # in-degree count
        in_degree: dict[int, int] = {idx: 0 for idx in valid_indices}
        # adjacency: prereq → list of steps that depend on it
        adj: dict[int, list[int]] = defaultdict(list)

        for step in steps:
            for dep in step.depends_on:
                in_degree[step.step_index] += 1
                adj[dep].append(step.step_index)

        queue: deque[int] = deque(
            idx for idx, deg in in_degree.items() if deg == 0
        )
        visited = 0

        while queue:
            node = queue.popleft()
            visited += 1
            for neighbour in adj[node]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        if visited != len(valid_indices):
            # Find which nodes are still in a cycle for a useful message
            cycle_nodes = sorted(
                idx for idx, deg in in_degree.items() if deg > 0
            )
            return (
                f"Dependency graph contains a cycle involving step(s) "
                f"{cycle_nodes}; the plan DAG must be acyclic."
            )
        return None

    @staticmethod
    def _check_repos_affected(
        plan: ArchitecturalPlan,
        step_repos: set[str],
        reasons: list[str],
    ) -> None:
        """Rule 9: repos_affected must equal the union of step repos."""
        declared = set(plan.repos_affected)

        undeclared = step_repos - declared  # in steps but not declared
        unused = declared - step_repos       # declared but not used in any step

        parts: list[str] = []
        if undeclared:
            parts.append(f"repos used in steps but missing from repos_affected: {sorted(undeclared)}")
        if unused:
            parts.append(f"repos declared in repos_affected but absent from steps: {sorted(unused)}")

        if parts:
            reasons.append(
                "repos_affected mismatch — " + "; ".join(parts) + "."
            )

    @staticmethod
    def _check_acceptance_checks(
        plan: ArchitecturalPlan,
        valid_indices: set[int],
        reasons: list[str],
    ) -> None:
        """Rule 10: every AcceptanceCheck.run_after_step must be a valid index."""
        bad: list[tuple[str, int]] = []
        for chk in plan.acceptance_checks:
            if chk.run_after_step is not None and chk.run_after_step not in valid_indices:
                bad.append((chk.check_id, chk.run_after_step))

        if bad:
            detail = ", ".join(
                f"check '{cid}' references step {s}" for cid, s in bad
            )
            reasons.append(
                f"AcceptanceCheck run_after_step references non-existent "
                f"step indices: {detail}."
            )
