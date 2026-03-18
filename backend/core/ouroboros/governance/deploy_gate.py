"""backend/core/ouroboros/governance/deploy_gate.py — P3-2 safe deploy strategy.

Provides a contract-preflight + canary go/no-go gate that must pass before any
rolling deploy is committed.  Thin wrapper: delegates metrics to
``CanaryController`` and SLO evaluation to ``SLOHealthModel``.

Design:
* ``DeployContract`` — immutable deploy descriptor (service, versions, rollback ref).
* ``PreflightCheck`` — one named, callable synchronous predicate.
* ``ContractPreflightResult`` — outcome of running all preflight checks.
* ``DeployGate`` — orchestrates preflight + canary evaluation; triggers rollback.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .canary_controller import CanaryController, CanaryState, PromotionResult

logger = logging.getLogger("Ouroboros.DeployGate")


# ---------------------------------------------------------------------------
# DeployContract — immutable deploy descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployContract:
    """Immutable deploy descriptor.

    Parameters
    ----------
    service:
        Logical service name (e.g. ``"jarvis"`` / ``"prime"`` / ``"reactor"``).
    from_version:
        Version string being replaced (e.g. ``"2.3.1"``).
    to_version:
        Version string being deployed.
    rollback_ref:
        Git SHA or tag to roll back to on failure.
    domain_slice_prefix:
        Canary domain slice this deploy governs.  Empty string = no canary gate.
    """

    service: str
    from_version: str
    to_version: str
    rollback_ref: str
    domain_slice_prefix: str = ""


# ---------------------------------------------------------------------------
# PreflightCheck — callable safety predicate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightCheck:
    """A named preflight predicate.

    ``check_fn`` must be a zero-argument synchronous callable that returns
    ``(passed: bool, message: str)``.  To keep contracts serialisable, prefer
    passing a plain function reference rather than a lambda.
    """

    name: str
    check_fn: Callable[[], Tuple[bool, str]]


# ---------------------------------------------------------------------------
# ContractPreflightResult — outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractPreflightResult:
    """Result of running all preflight checks for a DeployContract."""

    passed: bool
    failed_checks: Tuple[str, ...]   # Names of failed checks
    warnings: Tuple[str, ...]        # Non-blocking advisory messages


# ---------------------------------------------------------------------------
# DeployGate
# ---------------------------------------------------------------------------


class DeployGate:
    """Orchestrates preflight + canary go/no-go for a rolling deploy.

    Parameters
    ----------
    canary_controller:
        Shared CanaryController instance.  If None, canary gate is skipped.
    slo_model:
        Optional SLOHealthModel.  If provided, its ``status()`` is evaluated
        as an additional preflight check.
    """

    def __init__(
        self,
        canary_controller: Optional[CanaryController] = None,
        slo_model=None,  # SLOHealthModel — avoid circular import
    ) -> None:
        self._canary = canary_controller
        self._slo = slo_model

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_preflight(
        self,
        contract: DeployContract,
        extra_checks: Optional[List[PreflightCheck]] = None,
    ) -> ContractPreflightResult:
        """Run all preflight checks for *contract*.

        Built-in checks (run before *extra_checks*):
        1. Rollback ref is non-empty.
        2. Versions are non-empty and differ.
        3. SLO status (if SLO model is registered).

        Returns a ``ContractPreflightResult`` with ``passed=True`` only if
        every check succeeds.
        """
        failed: List[str] = []
        warnings: List[str] = []

        # Built-in: rollback ref
        if not contract.rollback_ref:
            failed.append("rollback_ref_missing")

        # Built-in: version sanity
        if not contract.from_version or not contract.to_version:
            failed.append("version_strings_empty")
        elif contract.from_version == contract.to_version:
            warnings.append(f"from_version == to_version == {contract.to_version!r}")

        # Built-in: SLO gate
        if self._slo is not None:
            from backend.core.slo_budget import SLOStatus
            slo_status = self._slo.status()
            if slo_status == SLOStatus.UNHEALTHY:
                failed.append(f"slo_unhealthy:{slo_status.value}")
            elif slo_status == SLOStatus.DEGRADED:
                warnings.append(f"slo_degraded:{slo_status.value}")

        # Extra caller-supplied checks
        for check in (extra_checks or []):
            try:
                ok, msg = check.check_fn()
                if not ok:
                    failed.append(f"{check.name}:{msg}")
                elif msg:
                    warnings.append(f"{check.name}:{msg}")
            except Exception as exc:
                failed.append(f"{check.name}:exception:{exc}")

        passed = len(failed) == 0
        result = ContractPreflightResult(
            passed=passed,
            failed_checks=tuple(failed),
            warnings=tuple(warnings),
        )
        if passed:
            logger.info(
                "[DeployGate] preflight PASSED for %s %s→%s",
                contract.service, contract.from_version, contract.to_version,
            )
        else:
            logger.error(
                "[DeployGate] preflight FAILED for %s %s→%s: %s",
                contract.service, contract.from_version, contract.to_version,
                ", ".join(failed),
            )
        return result

    def evaluate_canary(self, domain_slice_prefix: str) -> PromotionResult:
        """Check whether a domain slice's canary metrics allow promotion.

        Returns a ``PromotionResult`` from the underlying ``CanaryController``.
        If no controller is configured, returns a synthetic passing result
        so that callers without canary infra are not blocked.
        """
        if self._canary is None:
            return PromotionResult(promoted=True, reason="no canary controller configured")
        return self._canary.check_promotion(domain_slice_prefix)

    def is_go_for_deploy(
        self,
        contract: DeployContract,
        extra_checks: Optional[List[PreflightCheck]] = None,
    ) -> bool:
        """Convenience: run preflight AND canary evaluation, return True iff both pass.

        Logs the final go/no-go decision.
        """
        preflight = self.run_preflight(contract, extra_checks)
        if not preflight.passed:
            logger.error(
                "[DeployGate] NO-GO for %s: preflight failed (%s)",
                contract.service, ", ".join(preflight.failed_checks),
            )
            return False

        if contract.domain_slice_prefix:
            canary = self.evaluate_canary(contract.domain_slice_prefix)
            if not canary.promoted:
                logger.warning(
                    "[DeployGate] NO-GO for %s: canary gate not met (%s)",
                    contract.service, canary.reason,
                )
                return False

        logger.info("[DeployGate] GO for %s %s→%s", contract.service,
                    contract.from_version, contract.to_version)
        return True

    def trigger_rollback(self, contract: DeployContract, reason: str) -> None:
        """Record a rollback event and suspend the domain slice.

        Actual git-revert mechanics belong to the deploy orchestrator; this
        method only handles canary state + logging so the governance pipeline
        has a consistent rollback hook.
        """
        logger.error(
            "[DeployGate] ROLLBACK triggered for %s → %s — reason: %s",
            contract.service, contract.rollback_ref, reason,
        )
        if self._canary and contract.domain_slice_prefix:
            s = self._canary.get_slice(contract.domain_slice_prefix)
            if s is not None:
                s.state = CanaryState.SUSPENDED
                logger.warning(
                    "[DeployGate] Canary slice suspended: %s",
                    contract.domain_slice_prefix,
                )
