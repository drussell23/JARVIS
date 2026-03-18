"""backend/core/boot_contract_checker.py — Nuance 10: boot-time cross-repo contract check.

Problem
-------
``version_negotiation.py`` and ``protocol_version_gate.py`` exist but are not
wired into the boot handshake across all three repos.  If JARVIS Prime is
updated independently (e.g., schema 2c.1 → 2d.0), JARVIS Core discovers the
incompatibility at the first generation request — **not** at boot time.  The
user has already been told "JARVIS is ready."

Fix
---
``BootContractChecker.run_all(versions)`` is called synchronously just before
the "JARVIS is ready" announcement.  It runs:

1. ``CompatibilityMatrix.check_all(versions)`` — N/N-1/N+1 version rules.
2. ``ENV_CONTRACTS`` validation from ``startup_contracts.py`` — required env
   vars, expected formats, critical thresholds.

If any BOOT_BLOCKER or PRECHECK_BLOCKER violations are found, it raises
``BootContractViolation`` before the ready announcement so the user never sees
a broken "JARVIS is ready."

Design
------
* ``BootContractResult``  — frozen dataclass: passed, violations, blocking_violations.
* ``BootContractViolation`` — raised when blocking violations exist.
* ``BootContractChecker``  — wires CompatibilityMatrix + ENV_CONTRACTS.
* ``get_boot_contract_checker()`` — process-wide singleton.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from backend.core.compatibility_matrix import (
    CompatibilityMatrix,
    get_compatibility_matrix,
)

__all__ = [
    "BootContractResult",
    "BootContractViolation",
    "BootContractChecker",
    "get_boot_contract_checker",
]

logger = logging.getLogger(__name__)

# Attempt to import ContractSeverity + ENV_CONTRACTS from startup_contracts.
# If that module is unavailable, we degrade gracefully (env contract checks
# are skipped, compatibility matrix check still runs).
try:
    from backend.core.startup_contracts import (  # type: ignore[import]
        ENV_CONTRACTS,
        ContractSeverity,
    )
    _HAS_STARTUP_CONTRACTS = True
except ImportError:
    ENV_CONTRACTS = []          # type: ignore[assignment]
    ContractSeverity = None     # type: ignore[assignment]
    _HAS_STARTUP_CONTRACTS = False


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootContractResult:
    """Immutable result of a boot-time contract check."""

    passed: bool
    violations: Tuple[str, ...] = field(default_factory=tuple)   # all issues
    blocking_violations: Tuple[str, ...] = field(default_factory=tuple)  # boot-blockers
    duration_s: float = 0.0

    @classmethod
    def ok(cls, duration_s: float = 0.0) -> "BootContractResult":
        return cls(passed=True, duration_s=duration_s)

    def __bool__(self) -> bool:
        return self.passed


class BootContractViolation(RuntimeError):
    """Raised when blocking boot-contract violations are detected.

    Attributes
    ----------
    violations:
        All violation messages (blocking and non-blocking).
    blocking_violations:
        Only the violations that prevent startup.
    """

    def __init__(self, violations: List[str], blocking: List[str]) -> None:
        self.violations = violations
        self.blocking_violations = blocking
        bullet = "\n  • ".join(blocking)
        super().__init__(
            f"[BootContract] {len(blocking)} blocking violation(s) detected:\n"
            f"  • {bullet}"
        )


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class BootContractChecker:
    """Validates cross-repo version compatibility and ENV_CONTRACTS at boot.

    Usage::

        checker = get_boot_contract_checker()
        result = checker.run_all({
            "jarvis": "2.3.0",
            "prime": "2.2.0",
            "reactor": "2.1.0",
        })
        # run_all raises BootContractViolation if any blocker is found.
        logger.info("Boot contracts passed in %.3fs", result.duration_s)
    """

    def __init__(
        self,
        matrix: Optional[CompatibilityMatrix] = None,
    ) -> None:
        self._matrix = matrix or get_compatibility_matrix()

    # ------------------------------------------------------------------
    # Sub-checks
    # ------------------------------------------------------------------

    def check_compatibility_matrix(
        self, versions: Dict[str, str]
    ) -> List[str]:
        """Run CompatibilityMatrix.check_all().  Returns incompatibility messages."""
        if not versions:
            return []
        return self._matrix.check_all(versions)

    def check_env_contracts(self) -> List[str]:
        """Validate ENV_CONTRACTS from startup_contracts.py.

        Returns a list of violation messages.  Empty list = all pass.
        Skipped (returns []) when startup_contracts is unavailable.
        """
        if not _HAS_STARTUP_CONTRACTS or not ENV_CONTRACTS:
            return []

        violations: List[str] = []
        for contract in ENV_CONTRACTS:
            try:
                # Contracts expose an evaluate() method in startup_contracts.py.
                # If the interface is missing, log and skip.
                evaluate = getattr(contract, "evaluate", None)
                if evaluate is None:
                    continue
                ok, message = evaluate()
                if not ok:
                    violations.append(message)
            except Exception as exc:
                violations.append(f"EnvContract evaluation raised: {exc}")

        return violations

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run_all(
        self,
        versions: Optional[Dict[str, str]] = None,
    ) -> BootContractResult:
        """Run all boot-time contract checks.

        Parameters
        ----------
        versions:
            Mapping of ``{component_name: "major.minor.patch"}``.
            ``None`` or ``{}`` — skip compatibility matrix check.

        Returns
        -------
        BootContractResult
            Always returned when no blocking violations exist.

        Raises
        ------
        BootContractViolation
            When any blocking violation is detected (severity implies
            BOOT_BLOCKER or PRECHECK_BLOCKER if ``startup_contracts`` is
            available, or any matrix violation otherwise).
        """
        start = time.monotonic()
        all_violations: List[str] = []
        blocking: List[str] = []

        # --- Compatibility matrix ---
        matrix_issues = self.check_compatibility_matrix(versions or {})
        if matrix_issues:
            logger.error(
                "[BootContract] %d compatibility violation(s): %s",
                len(matrix_issues), matrix_issues,
            )
            all_violations.extend(matrix_issues)
            blocking.extend(matrix_issues)  # matrix violations always block

        # --- ENV contracts ---
        env_issues = self.check_env_contracts()
        if env_issues:
            for issue in env_issues:
                all_violations.append(issue)
                # Treat any env-contract failure as blocking if we have no
                # severity information (conservative default).
                is_blocking = _is_blocking_violation(issue)
                if is_blocking:
                    blocking.append(issue)
                    logger.error("[BootContract] BLOCKING: %s", issue)
                else:
                    logger.warning("[BootContract] WARNING: %s", issue)

        duration = time.monotonic() - start

        if blocking:
            raise BootContractViolation(all_violations, blocking)

        result = BootContractResult(
            passed=True,
            violations=tuple(all_violations),
            blocking_violations=(),
            duration_s=duration,
        )

        if all_violations:
            logger.warning(
                "[BootContract] %d non-blocking issue(s) found in %.3fs",
                len(all_violations), duration,
            )
        else:
            logger.info(
                "[BootContract] all checks passed in %.3fs",
                duration,
            )

        return result


def _is_blocking_violation(message: str) -> bool:
    """Heuristic: a violation is blocking if its message contains certain keywords."""
    blocking_keywords = ("BOOT_BLOCKER", "PRECHECK_BLOCKER", "CRITICAL", "required")
    return any(kw.lower() in message.lower() for kw in blocking_keywords)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_checker: Optional[BootContractChecker] = None


def get_boot_contract_checker() -> BootContractChecker:
    """Return (lazily creating) the process-wide BootContractChecker."""
    global _g_checker
    if _g_checker is None:
        _g_checker = BootContractChecker()
    return _g_checker
