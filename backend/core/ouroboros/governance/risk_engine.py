"""
Deterministic Risk Engine
=========================

Classifies every autonomous Ouroboros operation into one of three risk tiers
using *only* deterministic rules.  No LLM calls.  No heuristics.

Risk Tiers
----------
- **SAFE_AUTO** -- operation may proceed without human approval.
- **APPROVAL_REQUIRED** -- operation must be reviewed by a human operator.
- **BLOCKED** -- operation is unconditionally forbidden (hard invariant).

Rules are evaluated in strict priority order; first match wins.  The full
rule chain and its version are captured in :class:`RiskClassification` so
that every decision is auditable and reproducible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# Policy version -- bump on every rule change
# ---------------------------------------------------------------------------

POLICY_VERSION: str = "v0.1.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RiskTier(Enum):
    """Classification tier for an autonomous operation."""

    SAFE_AUTO = auto()
    APPROVAL_REQUIRED = auto()
    BLOCKED = auto()


class ChangeType(Enum):
    """Kind of filesystem mutation the operation performs."""

    CREATE = auto()
    MODIFY = auto()
    DELETE = auto()
    RENAME = auto()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HardInvariantViolation(Exception):
    """Raised when an operation violates a non-negotiable hard invariant.

    Hard invariants are constraints that can *never* be overridden -- not by
    operator approval, not by policy relaxation.  They exist to protect the
    system's foundational safety properties.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationProfile:
    """Immutable description of a proposed autonomous operation.

    Every field that feeds into risk classification is captured here so that
    decisions are fully reproducible given the same profile.

    Parameters
    ----------
    files_affected:
        Paths of every file the operation will touch.
    change_type:
        The kind of mutation (create / modify / delete / rename).
    blast_radius:
        Estimated number of downstream components affected by the change.
    crosses_repo_boundary:
        ``True`` when the change spans more than one repository.
    touches_security_surface:
        ``True`` when the change touches authentication, authorization,
        encryption, secrets, or credential management code.
    touches_supervisor:
        ``True`` when the change modifies ``unified_supervisor.py`` or any
        other supervisor-lifecycle file.
    test_scope_confidence:
        Float in ``[0, 1]`` estimating how well existing tests cover the
        blast radius of this change.
    is_dependency_change:
        ``True`` when the change modifies dependency manifests
        (requirements.txt, pyproject.toml, package.json, etc.).
    is_core_orchestration_path:
        ``True`` when the change targets a core orchestration module
        (router, controller, engine, orchestrator).
    """

    files_affected: List[Path]
    change_type: ChangeType
    blast_radius: int
    crosses_repo_boundary: bool
    touches_security_surface: bool
    touches_supervisor: bool
    test_scope_confidence: float
    is_dependency_change: bool = False
    is_core_orchestration_path: bool = False


@dataclass(frozen=True)
class RiskClassification:
    """Immutable result of a risk-engine evaluation.

    Parameters
    ----------
    tier:
        The assigned :class:`RiskTier`.
    reason_code:
        Machine-readable label identifying which rule triggered.
    policy_version:
        Version of the policy ruleset that produced this classification.
    """

    tier: RiskTier
    reason_code: str
    policy_version: str = POLICY_VERSION


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------


class RiskEngine:
    """Deterministic, rule-based risk classifier.

    Thresholds are read from environment variables at construction time so
    that operators can tune policy without code changes.  All env vars fall
    back to strict defaults.

    Environment Variables
    ---------------------
    OUROBOROS_BLAST_RADIUS_THRESHOLD : int (default 5)
        Maximum blast radius before APPROVAL_REQUIRED.
    OUROBOROS_MAX_FILES_THRESHOLD : int (default 2)
        Maximum number of files before APPROVAL_REQUIRED.
    OUROBOROS_TEST_CONFIDENCE_THRESHOLD : float (default 0.75)
        Minimum test-scope confidence before APPROVAL_REQUIRED.
    """

    def __init__(self) -> None:
        self._blast_radius_threshold: int = int(
            os.environ.get("OUROBOROS_BLAST_RADIUS_THRESHOLD", "5")
        )
        self._max_files_threshold: int = int(
            os.environ.get("OUROBOROS_MAX_FILES_THRESHOLD", "2")
        )
        self._test_confidence_threshold: float = float(
            os.environ.get("OUROBOROS_TEST_CONFIDENCE_THRESHOLD", "0.75")
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify(self, profile: OperationProfile) -> RiskClassification:
        """Classify an operation profile into a risk tier.

        Rules are evaluated in strict priority order; **first match wins**.

        1. ``touches_supervisor``        -> BLOCKED
        2. ``touches_security_surface``  -> BLOCKED
        3. ``crosses_repo_boundary``     -> APPROVAL_REQUIRED
        4. ``change_type == DELETE``      -> APPROVAL_REQUIRED
        5. ``is_dependency_change``       -> APPROVAL_REQUIRED
        6. core path + structural change -> APPROVAL_REQUIRED
        7. blast_radius > threshold      -> APPROVAL_REQUIRED
        8. len(files) > max_files        -> APPROVAL_REQUIRED
        9. test_confidence < threshold   -> APPROVAL_REQUIRED
        10. Otherwise                    -> SAFE_AUTO

        Parameters
        ----------
        profile:
            The :class:`OperationProfile` to evaluate.

        Returns
        -------
        RiskClassification
            The deterministic classification including tier, reason code,
            and policy version.
        """
        # Rule 1: Supervisor is unconditionally off-limits
        if profile.touches_supervisor:
            return RiskClassification(
                tier=RiskTier.BLOCKED,
                reason_code="touches_supervisor",
            )

        # Rule 2: Security surface is unconditionally off-limits
        if profile.touches_security_surface:
            return RiskClassification(
                tier=RiskTier.BLOCKED,
                reason_code="touches_security_surface",
            )

        # Rule 3: Cross-repo changes require human review
        if profile.crosses_repo_boundary:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="crosses_repo_boundary",
            )

        # Rule 4: Deletions always require human review
        if profile.change_type is ChangeType.DELETE:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="delete_operation",
            )

        # Rule 5: Dependency changes require human review
        if profile.is_dependency_change:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="dependency_change",
            )

        # Rule 6: Structural changes to core orchestration paths
        if profile.is_core_orchestration_path and profile.change_type in (
            ChangeType.CREATE,
            ChangeType.DELETE,
            ChangeType.RENAME,
        ):
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="core_path_structural_change",
            )

        # Rule 7: Blast radius exceeds threshold
        if profile.blast_radius > self._blast_radius_threshold:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="blast_radius_exceeded",
            )

        # Rule 8: Too many files affected
        if len(profile.files_affected) > self._max_files_threshold:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="too_many_files",
            )

        # Rule 9: Insufficient test coverage confidence
        if profile.test_scope_confidence < self._test_confidence_threshold:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="low_test_confidence",
            )

        # Rule 10: All checks passed -- safe to auto-execute
        return RiskClassification(
            tier=RiskTier.SAFE_AUTO,
            reason_code="all_checks_passed",
        )

    # ------------------------------------------------------------------
    # Hard Invariant Enforcement
    # ------------------------------------------------------------------

    def enforce_invariants(
        self,
        profile: OperationProfile,
        contract_regression_delta: int,
        security_risk_delta: int,
        operator_load_delta: int,
    ) -> None:
        """Enforce non-negotiable hard invariants.

        These invariants can **never** be overridden.  If any are violated,
        :class:`HardInvariantViolation` is raised and the operation must be
        aborted unconditionally.

        Hard Invariants
        ---------------
        1. **No contract regression** -- ``contract_regression_delta`` must
           be ``<= 0``.  An increase means the change breaks an existing
           contract.
        2. **No security risk increase** -- ``security_risk_delta`` must be
           ``<= 0``.  An increase means the change enlarges the attack
           surface.

        Parameters
        ----------
        profile:
            The operation being evaluated (for context in error messages).
        contract_regression_delta:
            Change in contract-compliance score.  Positive = regression.
        security_risk_delta:
            Change in security-risk score.  Positive = risk increase.
        operator_load_delta:
            Change in operator cognitive load (reserved for future use).

        Raises
        ------
        HardInvariantViolation
            If any hard invariant is violated.
        """
        if contract_regression_delta > 0:
            raise HardInvariantViolation(
                f"Contract regression detected (delta={contract_regression_delta}). "
                f"Files affected: {[str(f) for f in profile.files_affected]}. "
                f"Policy {POLICY_VERSION} forbids any contract regression."
            )

        if security_risk_delta > 0:
            raise HardInvariantViolation(
                f"Security risk increase detected (delta={security_risk_delta}). "
                f"Files affected: {[str(f) for f in profile.files_affected]}. "
                f"Policy {POLICY_VERSION} forbids any security risk increase."
            )
