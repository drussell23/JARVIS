# backend/core/ouroboros/governance/runtime_contracts.py
"""
Runtime Contract Checker -- N/N-1 Schema Validation at Runtime
================================================================

Extends the boot-time :class:`ContractGate` with runtime checks.
Before any autonomous write, verifies that proposed changes don't
break the contract between the current schema (N) and the previous
schema (N-1).

Rules:
- Same major, same or +1 minor: COMPATIBLE
- Same major, exactly N-1 minor: COMPATIBLE (backward compat)
- Different major: INCOMPATIBLE
- More than 1 minor version back: INCOMPATIBLE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from backend.core.ouroboros.governance.contract_gate import ContractVersion

logger = logging.getLogger("Ouroboros.RuntimeContracts")


@dataclass(frozen=True)
class ContractViolation:
    """A single contract violation."""

    field: str
    reason: str


@dataclass(frozen=True)
class ContractCheckResult:
    """Result of a runtime contract compatibility check."""

    compatible: bool
    violations: List[ContractViolation] = field(default_factory=list)


class RuntimeContractChecker:
    """Runtime N/N-1 schema compatibility checker."""

    def __init__(self, current_version: ContractVersion) -> None:
        self._current = current_version

    def check_compatibility(
        self, proposed_version: Optional[ContractVersion] = None
    ) -> ContractCheckResult:
        """Check if a proposed version is compatible with current."""
        if proposed_version is None:
            return ContractCheckResult(compatible=True)

        violations: List[ContractViolation] = []

        # Major version must match
        if proposed_version.major != self._current.major:
            violations.append(
                ContractViolation(
                    field="major_version",
                    reason=(
                        f"Major version mismatch: current={self._current.major}, "
                        f"proposed={proposed_version.major}"
                    ),
                )
            )
            return ContractCheckResult(compatible=False, violations=violations)

        # Minor version: allow N (same), N+x (forward), N-1 (one back)
        minor_delta = self._current.minor - proposed_version.minor
        if minor_delta > 1:
            violations.append(
                ContractViolation(
                    field="minor_version",
                    reason=(
                        f"Minor version too old: current={self._current.minor}, "
                        f"proposed={proposed_version.minor} (max N-1 allowed)"
                    ),
                )
            )
            return ContractCheckResult(compatible=False, violations=violations)

        return ContractCheckResult(compatible=True)

    def check_before_write(
        self, proposed_version: Optional[ContractVersion] = None
    ) -> bool:
        """Convenience method: returns True if write is safe."""
        result = self.check_compatibility(proposed_version)
        if not result.compatible:
            logger.warning(
                "Runtime contract check failed: %s",
                "; ".join(v.reason for v in result.violations),
            )
        return result.compatible
