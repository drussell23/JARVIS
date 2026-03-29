"""
ArchitecturalPlan and PlanStep schemas
=======================================

Core plan data structures for the Architecture Reasoning Agent.

Design principles:
- All schemas are frozen dataclasses to ensure immutability after creation.
- ``compute_plan_hash`` hashes only structure+scope fields, deliberately
  excluding provenance fields (model_used, created_at, snapshot_hash) so
  that the same logical plan produced by different models or at different
  times yields the same hash.  This enables deduplication and re-use
  of plan hashes across the system.
- ``ArchitecturalPlan.create()`` is the sole constructor.  It automatically
  derives ``plan_id`` (uuid4 hex prefix), ``plan_hash``, and the
  ``file_allowlist`` (union of all target/ancillary/test paths) so callers
  never have to compute these themselves.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StepIntentKind(enum.Enum):
    """What kind of filesystem change a PlanStep intends to perform."""

    CREATE_FILE = "create_file"
    MODIFY_FILE = "modify_file"
    DELETE_FILE = "delete_file"


class CheckKind(enum.Enum):
    """How an AcceptanceCheck verifies correctness after a step."""

    EXIT_CODE = "exit_code"
    REGEX_STDOUT = "regex_stdout"
    IMPORT_CHECK = "import_check"


# ---------------------------------------------------------------------------
# PlanStep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """A single atomic step within an ArchitecturalPlan.

    Parameters
    ----------
    step_index:
        Zero-based ordinal within the plan.  Steps are executed in index order
        unless a dependency in ``depends_on`` forces earlier completion.
    description:
        Human-readable explanation of what this step achieves.
    intent_kind:
        The high-level file-system intent (CREATE / MODIFY / DELETE).
    target_paths:
        Primary paths that will be created, modified, or deleted.  Must be
        non-empty.
    repo:
        The repository that owns the paths, matching a key from
        ``RepoRegistry``.
    ancillary_paths:
        Supporting paths that are read or lightly touched but are not the
        primary target (e.g., a config file updated alongside a new module).
    interface_contracts:
        Free-text descriptions of public API contracts that must remain stable
        (used by the plan validator for drift detection).
    tests_required:
        Paths of test files that must exist and pass after this step.
    risk_tier_hint:
        Advisory risk label consumed by the governance gate.  Defaults to
        ``"safe_auto"`` (no human approval required).
    depends_on:
        Indices of steps that must complete before this step may start.
    """

    step_index: int
    description: str
    intent_kind: StepIntentKind
    target_paths: Tuple[str, ...]
    repo: str
    ancillary_paths: Tuple[str, ...] = ()
    interface_contracts: Tuple[str, ...] = ()
    tests_required: Tuple[str, ...] = ()
    risk_tier_hint: str = "safe_auto"
    depends_on: Tuple[int, ...] = ()


# ---------------------------------------------------------------------------
# AcceptanceCheck
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceCheck:
    """A verifiable acceptance criterion for an ArchitecturalPlan.

    Parameters
    ----------
    check_id:
        Unique identifier for this check within the plan (e.g. ``"chk-001"``).
    check_kind:
        The mechanism used to evaluate pass/fail.
    command:
        Shell command (or import path for IMPORT_CHECK) to execute.
    expected:
        For EXIT_CODE: the numeric exit code as a string (e.g. ``"0"``).
        For REGEX_STDOUT: a regex pattern that must match stdout.
        For IMPORT_CHECK: unused (leave empty).
    cwd:
        Working directory for command execution.  Defaults to ``"."``.
    timeout_s:
        Maximum seconds before the check is considered failed.
    run_after_step:
        If set, this check is run immediately after the referenced step index
        rather than at the end of the plan.
    sandbox_required:
        Whether the check must run inside an isolated sandbox environment.
    """

    check_id: str
    check_kind: CheckKind
    command: str
    expected: str = ""
    cwd: str = "."
    timeout_s: float = 120.0
    run_after_step: Optional[int] = None
    sandbox_required: bool = True


# ---------------------------------------------------------------------------
# Plan hash
# ---------------------------------------------------------------------------


def compute_plan_hash(
    title: str,
    description: str,
    repos_affected: Tuple[str, ...],
    non_goals: Tuple[str, ...],
    steps: Tuple[PlanStep, ...],
    acceptance_checks: Tuple[AcceptanceCheck, ...],
) -> str:
    """Return a 64-character hex SHA-256 hash of the plan's structure and scope.

    Provenance fields (``model_used``, ``created_at``, ``snapshot_hash``,
    ``plan_id``) are intentionally excluded so that the same logical plan
    produced by different models or at different points in time yields the
    same hash.

    The payload is a canonical JSON object serialised with ``sort_keys=True``
    and compact separators to guarantee byte-for-byte reproducibility across
    Python versions and platforms.
    """
    payload = {
        "title": title,
        "description": description,
        "repos_affected": sorted(repos_affected),
        "non_goals": list(non_goals),
        "steps": [
            {
                "step_index": s.step_index,
                "description": s.description,
                "intent_kind": s.intent_kind.value,
                "target_paths": list(s.target_paths),
                "repo": s.repo,
                "ancillary_paths": list(s.ancillary_paths),
                "interface_contracts": list(s.interface_contracts),
                "tests_required": list(s.tests_required),
                "risk_tier_hint": s.risk_tier_hint,
                "depends_on": list(s.depends_on),
            }
            for s in steps
        ],
        "acceptance_checks": [
            {
                "check_id": c.check_id,
                "check_kind": c.check_kind.value,
                "command": c.command,
                "expected": c.expected,
                "cwd": c.cwd,
                "timeout_s": c.timeout_s,
                "run_after_step": c.run_after_step,
                "sandbox_required": c.sandbox_required,
            }
            for c in acceptance_checks
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# ArchitecturalPlan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchitecturalPlan:
    """A fully specified, immutable architectural plan produced by the agent.

    Use :meth:`create` to construct instances — it auto-derives ``plan_id``,
    ``plan_hash``, and ``file_allowlist``.

    Parameters
    ----------
    plan_id:
        First 16 hex characters of a UUID4.  Unique per plan instance.
    plan_hash:
        SHA-256 of the plan's structure and scope (excludes provenance).
    parent_hypothesis_id:
        UUID of the ``FeatureHypothesis`` that triggered this plan.
    parent_hypothesis_fingerprint:
        Fingerprint of the parent hypothesis (for drift detection).
    title:
        Short human-readable name for the plan.
    description:
        Detailed description of what the plan achieves and why.
    repos_affected:
        Repository keys from ``RepoRegistry`` that this plan touches.
    non_goals:
        Explicit scope boundaries — what the plan deliberately does NOT do.
    steps:
        Ordered sequence of atomic :class:`PlanStep` objects.
    file_allowlist:
        Union of all ``target_paths``, ``ancillary_paths``, and
        ``tests_required`` across all steps.  Auto-computed by :meth:`create`.
    acceptance_checks:
        Verifiable criteria that must pass before the plan is considered done.
    model_used:
        Identifier of the model that generated this plan (provenance).
    created_at:
        Unix timestamp when the plan was created (provenance).
    snapshot_hash:
        Hash of the codebase snapshot the plan was generated against
        (provenance).
    """

    plan_id: str
    plan_hash: str
    parent_hypothesis_id: str
    parent_hypothesis_fingerprint: str
    title: str
    description: str
    repos_affected: Tuple[str, ...]
    non_goals: Tuple[str, ...]
    steps: Tuple[PlanStep, ...]
    file_allowlist: FrozenSet[str]
    acceptance_checks: Tuple[AcceptanceCheck, ...]
    model_used: str
    created_at: float
    snapshot_hash: str

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        parent_hypothesis_id: str,
        parent_hypothesis_fingerprint: str,
        title: str,
        description: str,
        repos_affected: Tuple[str, ...],
        non_goals: Tuple[str, ...],
        steps: Tuple[PlanStep, ...],
        acceptance_checks: Tuple[AcceptanceCheck, ...],
        model_used: str,
        created_at: Optional[float] = None,
        snapshot_hash: str = "",
    ) -> "ArchitecturalPlan":
        """Construct an :class:`ArchitecturalPlan` with derived fields.

        Automatically computes:

        * ``plan_id`` — first 16 hex chars of a fresh UUID4.
        * ``plan_hash`` — SHA-256 of structure+scope (excludes provenance).
        * ``file_allowlist`` — union of all target, ancillary, and test paths
          across every step.
        """
        plan_hash = compute_plan_hash(
            title=title,
            description=description,
            repos_affected=repos_affected,
            non_goals=non_goals,
            steps=steps,
            acceptance_checks=acceptance_checks,
        )

        allowlist: set[str] = set()
        for step in steps:
            allowlist.update(step.target_paths)
            allowlist.update(step.ancillary_paths)
            allowlist.update(step.tests_required)

        return cls(
            plan_id=uuid.uuid4().hex[:16],
            plan_hash=plan_hash,
            parent_hypothesis_id=parent_hypothesis_id,
            parent_hypothesis_fingerprint=parent_hypothesis_fingerprint,
            title=title,
            description=description,
            repos_affected=repos_affected,
            non_goals=non_goals,
            steps=steps,
            file_allowlist=frozenset(allowlist),
            acceptance_checks=acceptance_checks,
            model_used=model_used,
            created_at=created_at if created_at is not None else time.time(),
            snapshot_hash=snapshot_hash,
        )
