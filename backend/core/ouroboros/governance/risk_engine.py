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
from typing import Any, List, Mapping, Optional


# ---------------------------------------------------------------------------
# Policy version -- bump on every rule change
# ---------------------------------------------------------------------------

POLICY_VERSION: str = "v0.2.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RiskTier(Enum):
    """Classification tier for an autonomous operation.

    Five graduated severity levels (Green → Yellow → Orange → Red →
    Order-2):

    - **SAFE_AUTO** (Green): Auto-apply silently. Read-only exploration,
      test additions, doc fixes, trivial dependency bumps.
    - **NOTIFY_APPLY** (Yellow): Auto-apply but surface prominently in
      the CLI stream. Behavioral code changes, new files, config changes.
    - **APPROVAL_REQUIRED** (Orange): Block and ask the operator.
      Breaking API changes, deleting files/tests, security-adjacent,
      cost threshold crossings.
    - **BLOCKED** (Red): Unconditionally forbidden. Supervisor, security
      surface, hard invariant violations.
    - **ORDER_2_GOVERNANCE** (RR Pass B Slice 2): the candidate touches
      a governance-code path enumerated in
      ``.jarvis/order2_manifest.yaml``. Strictly above ``BLOCKED`` —
      ``BLOCKED`` says "this op will not run autonomously, but a human
      can override at the REPL." ``ORDER_2_GOVERNANCE`` says "this op
      cannot run **even with operator REPL approval** — it requires the
      Pass B Slice 6 amendment protocol." Auto-apply forbidden at every
      nominal tier (including ``SAFE_AUTO``); REPL ``approve`` does not
      clear; op routes to a dedicated ``order2_review`` queue with
      operator-driven SLO. See `memory/project_reverse_russian_doll_pass_b.md`
      §4 for design.
    """

    SAFE_AUTO = auto()
    NOTIFY_APPLY = auto()
    APPROVAL_REQUIRED = auto()
    BLOCKED = auto()
    ORDER_2_GOVERNANCE = auto()


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
    source: str = ""
    # Slice 20 — an OPTIONAL provenance CLAIM (a pointer: {kind, goal_id})
    # attached by a high-fidelity sensor. Never trusted as-is: the
    # self-protection gate re-derives every authority-bearing fact from
    # ground truth (the live operator-signed roadmap + its HMAC) via
    # delegated_provenance.verify_provenance_claim. Absent/None ⇒
    # byte-identical pre-Slice-20 classification.
    provenance: Optional[Mapping[str, Any]] = None


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

    # ------------------------------------------------------------------
    # Immune-surface sentinel strings — category descriptors, NOT a hand-
    # maintained per-file list.
    #
    # Root-cause history: the previous incarnation enumerated individual
    # governance modules (``.../risk_engine``, ``.../orchestrator``,
    # ``.../semantic_guardian`` …). That list went stale the moment the live
    # enforcement code was extracted into the ``phase_runners/`` package —
    # ``gate_runner.py``, ``slice4b_runner.py``, ``generate_runner.py`` were
    # never added, so a candidate editing the *shipping* GATE code escaped the
    # self-modification block entirely. A per-file list re-rots at every
    # refactor by construction.
    #
    # The fix is to name the immune surface by PACKAGE, not by file: the whole
    # of ``ouroboros/governance/`` IS the cage, so a single substring covers
    # every current module, every future module, and every refactor. This is
    # the §51/RRD invariant made literal — the immune system's scope is defined
    # structurally (the package boundary) and therefore scales with the shell.
    # ------------------------------------------------------------------
    _KERNEL_SENTINELS_BASE: tuple = (
        "unified_supervisor",
    )
    _SELF_MOD_SENTINELS_BASE: tuple = (
        # The governance package in its entirety — the cage. Covers
        # orchestrator, risk_engine, semantic_guardian, phase_runners/*,
        # gate_runner, slice4b_runner, generate_runner, intake/*, and anything
        # added under governance/ hereafter. Refactor-proof by construction.
        "ouroboros/governance/",
        # Kernel-adjacent lifecycle modules that live OUTSIDE the governance
        # package but are still O+V's own body.
        "ouroboros/daemon",
        "ouroboros/vital_scan",
        "ouroboros/spinal_cord",
        "ouroboros/rem_sleep",
        "ouroboros/rem_epoch",
    )
    _SECURITY_SENTINELS_BASE: tuple = (
        "auth/",
        "credential",
        "secret",
        "token",
        ".env",
    )

    # The ONLY operation source permitted to modify the cage autonomously, and
    # only at APPROVAL_REQUIRED (human-in-the-loop) — the sanctioned Order-2 /
    # "Neurosurgeon" path (M10 ArchitectureProposer). This tuple is IMMUTABLE
    # and deliberately NOT env-configurable: an env switch that added a
    # sanctioned source would be a cage-weakening surface, exactly the class of
    # off-switch the change-engine phantom-gate lesson retired. Widening the
    # PROTECTION (sentinels) via env is safe and allowed below; widening the
    # PERMISSION is not.
    _SANCTIONED_SELF_MOD_SOURCES: tuple = ("architecture",)
    # Sources treated as lowest-trust (curiosity / roaming): self-mod is BLOCKED
    # and reason-coded under the historical ``exploration_*`` family.
    _UNTRUSTED_SELF_MOD_SOURCES: tuple = ("exploration", "roadmap")

    # Env knobs — ADDITIVE ONLY. Each ADDS sentinels to the baseline; none can
    # remove a baseline sentinel or lower a floor. Comma-separated substrings.
    _ENV_EXTRA_KERNEL: str = "JARVIS_RISK_KERNEL_EXTRA_SENTINELS"
    _ENV_EXTRA_SELF_MOD: str = "JARVIS_RISK_SELF_MOD_EXTRA_SENTINELS"
    _ENV_EXTRA_SECURITY: str = "JARVIS_RISK_SECURITY_EXTRA_SENTINELS"

    # Blast-radius threshold stricter than the default for exploration ops.
    _EXPLORATION_BLAST_RADIUS_THRESHOLD: int = 3

    # Backward-compatible alias — external readers (and older tests) referenced
    # ``_EXPLORATION_SELF_MOD_SENTINELS`` directly. Kept as the baseline view;
    # the live gate consults ``_self_mod_sentinels()`` (baseline + env).
    _EXPLORATION_SELF_MOD_SENTINELS: tuple = _SELF_MOD_SENTINELS_BASE
    _EXPLORATION_KERNEL_SENTINELS: tuple = _KERNEL_SENTINELS_BASE
    _EXPLORATION_SECURITY_SENTINELS: tuple = _SECURITY_SENTINELS_BASE

    # ------------------------------------------------------------------
    # Sentinel derivation (baseline ∪ env-additive) + matcher — DRY helpers
    # consumed by the unconditional self-protection gate.
    # ------------------------------------------------------------------

    @staticmethod
    def _env_extra_sentinels(env_name: str) -> tuple:
        """Parse comma-separated ADDITIVE sentinels from *env_name*. Never
        raises; empty/garbage → empty tuple. Additive-only by contract."""
        try:
            raw = (os.environ.get(env_name, "") or "").strip()
            if not raw:
                return ()
            return tuple(s.strip() for s in raw.split(",") if s.strip())
        except Exception:  # noqa: BLE001 — a config-parse fault must never
            # narrow the cage; degrade to the safe baseline.
            return ()

    def _kernel_sentinels(self) -> tuple:
        return self._KERNEL_SENTINELS_BASE + self._env_extra_sentinels(
            self._ENV_EXTRA_KERNEL
        )

    def _self_mod_sentinels(self) -> tuple:
        return self._SELF_MOD_SENTINELS_BASE + self._env_extra_sentinels(
            self._ENV_EXTRA_SELF_MOD
        )

    def _security_sentinels(self) -> tuple:
        return self._SECURITY_SENTINELS_BASE + self._env_extra_sentinels(
            self._ENV_EXTRA_SECURITY
        )

    @staticmethod
    def _matches_any(file_strs: List[str], sentinels: tuple) -> bool:
        """True iff any affected file path contains any sentinel substring."""
        return any(
            sentinel in fpath for fpath in file_strs for sentinel in sentinels
        )

    def _self_protection_verdict(self, file_strs, source, provenance=None):
        """UNCONDITIONAL, source-independent self-protection.

        The immune system must classify a modification of ITSELF at a floor no
        sensor source can lower. This is path-based rather than dependent on the
        profile-builder correctly setting ``touches_supervisor`` /
        ``touches_security_surface`` — the orchestrator's main classification
        path (``_build_profile``) never sets ``source`` and governance files do
        not trip those boolean heuristics, so before this gate a governance
        self-edit fell straight through to the general rules and could land
        SAFE_AUTO. This substring gate is the reliable self-protection.

        Returns a :class:`RiskClassification` when the op touches the immune
        surface, else ``None`` (fall through to the ordinary rules).
        """
        # Kernel — unconditionally BLOCKED for EVERY source.
        if self._matches_any(file_strs, self._kernel_sentinels()):
            return RiskClassification(
                tier=RiskTier.BLOCKED,
                reason_code=self._selfmod_reason(source, "touches_kernel"),
            )
        # Security surface — unconditionally BLOCKED for EVERY source.
        if self._matches_any(file_strs, self._security_sentinels()):
            return RiskClassification(
                tier=RiskTier.BLOCKED,
                reason_code=self._selfmod_reason(source, "touches_security"),
            )
        # Governance / kernel-adjacent self-modification.
        if self._matches_any(file_strs, self._self_mod_sentinels()):
            if source in self._SANCTIONED_SELF_MOD_SOURCES:
                # The one sanctioned Order-2 path — human-gated, not blocked.
                return RiskClassification(
                    tier=RiskTier.APPROVAL_REQUIRED,
                    reason_code="architecture_self_modification",
                )
            # ── Slice 20 — Delegated Provenance ──
            # A signal may carry a provenance CLAIM (pointer to a goal inside
            # the operator-signed .jarvis/roadmap.yaml). The claim itself is
            # untrusted; verify_provenance_claim re-derives everything from
            # ground truth (live file + HMAC compare_digest + goal existence
            # + freshness + per-file scope binding). A VERIFIED claim raises
            # the verdict from BLOCKED to APPROVAL_REQUIRED — the same
            # human-gated ceiling as the architecture path, NEVER auto-apply.
            # This branch sits AFTER the kernel/security unconditional blocks
            # (structurally unreachable for those surfaces) and any failure —
            # malformed, expired, hallucinated goal, out-of-scope target,
            # verifier fault, feature off — falls through to the exact
            # pre-Slice-20 blocks below (fail-closed).
            if provenance is not None:
                try:
                    from backend.core.ouroboros.governance.delegated_provenance import (  # noqa: E501
                        verify_provenance_claim,
                    )
                    _pv = verify_provenance_claim(
                        provenance, source=source, file_strs=file_strs,
                    )
                except Exception:  # noqa: BLE001 — verifier import/fault ⇒ refuse
                    _pv = None
                if _pv is not None and _pv.valid:
                    return RiskClassification(
                        tier=RiskTier.APPROVAL_REQUIRED,
                        reason_code="delegated_provenance_self_modification",
                    )
            if source in self._UNTRUSTED_SELF_MOD_SOURCES:
                return RiskClassification(
                    tier=RiskTier.BLOCKED,
                    reason_code="exploration_self_modification",
                )
            # THE HOLE THIS FIX CLOSES: every other source (TestFailure,
            # Backlog, OpportunityMiner, ai_miner, postmortem, the empty
            # default the orchestrator uses, …) editing the cage. Unsanctioned
            # autonomous self-modification is BLOCKED — a human may still do it
            # manually or route it through the architecture/M10 path.
            return RiskClassification(
                tier=RiskTier.BLOCKED,
                reason_code="self_modification_unsanctioned_source",
            )
        return None

    @staticmethod
    def _selfmod_reason(source: str, base: str) -> str:
        """Source-appropriate reason code, preserving the historical
        ``exploration_*`` / ``architecture_*`` families for the two sources
        that had them, and a bare ``base`` for all others."""
        if source in ("exploration", "roadmap"):
            return f"exploration_{base}"
        if source == "architecture":
            return f"architecture_{base}"
        return base

    def classify(self, profile: OperationProfile) -> RiskClassification:
        """Classify an operation profile into a risk tier.

        Rules are evaluated in strict priority order; **first match wins**.

        UNCONDITIONAL self-protection (evaluated FIRST, for EVERY source):
        S1. Files touching the kernel (unified_supervisor)   -> BLOCKED
        S2. Files touching the security surface              -> BLOCKED
        S3. Files touching the governance package / body:
              - source == "architecture" (sanctioned Order-2) -> APPROVAL_REQUIRED
              - source in {exploration, roadmap} (untrusted)  -> BLOCKED
              - any other source (incl. the empty default)    -> BLOCKED

        Source-specific stricter rules (after self-protection):
        E4. exploration/roadmap + blast_radius > 3          -> APPROVAL_REQUIRED
        A3. architecture + crosses_repo_boundary            -> APPROVAL_REQUIRED

        General rules (applied to all sources, as fallthrough):
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
        file_strs = [str(f) for f in profile.files_affected]
        _source = (profile.source or "").strip()

        # ══════════════════════════════════════════════════════════════════
        # UNCONDITIONAL SELF-PROTECTION (source-independent) — evaluated FIRST,
        # for EVERY source. Kernel + security → BLOCKED always; governance/
        # kernel-adjacent self-modification → BLOCKED for every source except
        # the single sanctioned Order-2 ``architecture`` path (APPROVAL_REQUIRED).
        #
        # This closes the hole where the self-mod block was source-gated to
        # exploration/roadmap/architecture only: every OTHER source (and the
        # empty default the orchestrator's main path emits) bypassed it and a
        # cage self-edit could land SAFE_AUTO. First match wins, so this floor
        # cannot be lowered by any downstream rule.
        # ══════════════════════════════════════════════════════════════════
        _self_protect = self._self_protection_verdict(
            file_strs, _source,
            provenance=getattr(profile, "provenance", None),
        )
        if _self_protect is not None:
            return _self_protect

        # Exploration/roadmap — remaining stricter rule (E4) after the
        # self-protection gate above has already handled kernel/security/self-mod.
        if _source in self._UNTRUSTED_SELF_MOD_SOURCES:
            # Rule E4: Stricter blast-radius cap for exploration ops
            if profile.blast_radius > self._EXPLORATION_BLAST_RADIUS_THRESHOLD:
                return RiskClassification(
                    tier=RiskTier.APPROVAL_REQUIRED,
                    reason_code="exploration_blast_radius_exceeded",
                )
            # Remaining exploration changes fall through to the standard rules.

        # Architecture — remaining rule (A3 cross-repo) after self-protection.
        if _source in self._SANCTIONED_SELF_MOD_SOURCES:
            if getattr(profile, 'crosses_repo_boundary', False):
                return RiskClassification(
                    tier=RiskTier.APPROVAL_REQUIRED,
                    reason_code="architecture_cross_repo",
                )

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

        # Rule 10: New files or behavioral modifications → NOTIFY_APPLY (Yellow)
        # These are auto-applied but surfaced prominently in the CLI.
        if profile.change_type is ChangeType.CREATE:
            return RiskClassification(
                tier=RiskTier.NOTIFY_APPLY,
                reason_code="new_file_created",
            )

        # Rule 11: Multi-file modifications → NOTIFY_APPLY
        if len(profile.files_affected) > 1:
            return RiskClassification(
                tier=RiskTier.NOTIFY_APPLY,
                reason_code="multi_file_change",
            )

        # Rule 12: Core orchestration path modifications → NOTIFY_APPLY
        if profile.is_core_orchestration_path:
            return RiskClassification(
                tier=RiskTier.NOTIFY_APPLY,
                reason_code="core_path_modification",
            )

        # Rule 13: All checks passed -- safe to auto-execute silently
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
