"""Contract Gate -- Schema Version Compatibility Enforcement.

Enforces N/N-1 schema version compatibility across JARVIS/Prime/Reactor
at boot and before cross-repo operations.  Pure deterministic logic --
no LLM calls, no network I/O.

Rules:
    - Major version must match exactly.
    - Minor version difference > 1 is incompatible.
    - Patch version differences are always compatible.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_SERVICES: Tuple[str, ...] = ("jarvis", "prime", "reactor")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractVersion:
    """Semantic version triplet for a service's contract schema."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class CompatibilityResult:
    """Outcome of a pairwise version compatibility check."""

    compatible: bool
    reason: str = ""


@dataclass(frozen=True)
class BootCheckResult:
    """Outcome of the boot-time compatibility gate across all services."""

    autonomy_allowed: bool
    interactive_allowed: bool
    details: str = ""
    incompatible_pairs: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Contract Gate
# ---------------------------------------------------------------------------


class ContractGate:
    """Enforces N/N-1 schema version compatibility.

    Usage::

        gate = ContractGate()

        # Pairwise check
        result = gate.check_compatibility(local_ver, remote_ver)

        # Boot-time gate (all services)
        boot = await gate.boot_check({
            "jarvis": ContractVersion(2, 1, 0),
            "prime":  ContractVersion(2, 0, 3),
            "reactor": ContractVersion(2, 1, 1),
        })
        if not boot.autonomy_allowed:
            # fall back to interactive mode
            ...
    """

    # ---- pairwise --------------------------------------------------------

    def check_compatibility(
        self,
        local: ContractVersion,
        remote: ContractVersion,
    ) -> CompatibilityResult:
        """Check whether *local* and *remote* versions are compatible.

        Returns a :class:`CompatibilityResult` with ``compatible=True``
        when the versions satisfy the N/N-1 rule, or ``compatible=False``
        with a human-readable *reason* otherwise.
        """
        if local.major != remote.major:
            return CompatibilityResult(
                compatible=False,
                reason=(
                    f"major version mismatch: "
                    f"{local.major} vs {remote.major}"
                ),
            )

        if abs(local.minor - remote.minor) > 1:
            return CompatibilityResult(
                compatible=False,
                reason=(
                    f"minor version gap too large: "
                    f"{local.minor} vs {remote.minor}"
                ),
            )

        # Patch differences are always compatible.
        return CompatibilityResult(compatible=True)

    # ---- boot gate -------------------------------------------------------

    async def boot_check(
        self,
        versions: Dict[str, ContractVersion],
    ) -> BootCheckResult:
        """Run the boot-time compatibility gate.

        All :data:`REQUIRED_SERVICES` must be present in *versions*,
        and every pairwise combination must be compatible.

        Returns a :class:`BootCheckResult`.  ``interactive_allowed``
        is always ``True`` -- even incompatible services can run in
        interactive (human-supervised) mode.
        """
        missing = [s for s in REQUIRED_SERVICES if s not in versions]
        if missing:
            detail = (
                f"missing required services: {', '.join(sorted(missing))}"
            )
            logger.warning("Contract gate FAIL: %s", detail)
            return BootCheckResult(
                autonomy_allowed=False,
                interactive_allowed=True,
                details=detail,
                incompatible_pairs=[],
            )

        incompatible_pairs: List[str] = []
        for svc_a, svc_b in itertools.combinations(versions, 2):
            result = self.check_compatibility(versions[svc_a], versions[svc_b])
            if not result.compatible:
                pair_label = f"{svc_a}<->{svc_b}"
                incompatible_pairs.append(pair_label)
                logger.warning(
                    "Contract gate: %s incompatible -- %s",
                    pair_label,
                    result.reason,
                )

        if incompatible_pairs:
            detail = (
                f"incompatible pairs: {', '.join(incompatible_pairs)}"
            )
            logger.warning("Contract gate FAIL: %s", detail)
            return BootCheckResult(
                autonomy_allowed=False,
                interactive_allowed=True,
                details=detail,
                incompatible_pairs=incompatible_pairs,
            )

        logger.info(
            "Contract gate PASS: all %d services compatible",
            len(versions),
        )
        return BootCheckResult(
            autonomy_allowed=True,
            interactive_allowed=True,
        )
