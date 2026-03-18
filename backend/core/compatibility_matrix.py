"""backend/core/compatibility_matrix.py — P3-3 upgrade compatibility matrix.

Machine-readable N/N-1/N+1 compatibility matrix across JARVIS, Prime, and
Reactor Core.

Design:
* ``ComponentVersion`` — (component, major, minor, patch).
* ``CompatibilityRule`` — describes which version pair (a, b) is compatible,
  expressed as inclusive min/max bounds on each side.
* ``CompatibilityMatrix`` — loaded from a list of rules; answers
  ``is_compatible(cv_a, cv_b)`` and ``check_all(versions)``.
* ``DEFAULT_RULES`` — the canonical matrix for the three JARVIS components.

Adding a new rule is the only required step when releasing a new version;
no code changes needed.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "Component",
    "ComponentVersion",
    "CompatibilityRule",
    "CompatibilityMatrix",
    "DEFAULT_RULES",
    "get_compatibility_matrix",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Component names — canonical identifiers
# ---------------------------------------------------------------------------


class Component(str, enum.Enum):
    JARVIS = "jarvis"
    PRIME = "prime"
    REACTOR = "reactor"


# ---------------------------------------------------------------------------
# ComponentVersion — version triple
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentVersion:
    """A semantic version triple for one component.

    Comparison operators use (major, minor, patch) tuple order.
    """

    component: str
    major: int
    minor: int
    patch: int = 0

    @classmethod
    def parse(cls, component: str, version_str: str) -> "ComponentVersion":
        """Parse ``"major.minor.patch"`` or ``"major.minor"`` into a ComponentVersion."""
        parts = version_str.split(".")
        if len(parts) < 2:
            raise ValueError(f"Cannot parse version string: {version_str!r}")
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
        return cls(component=component, major=major, minor=minor, patch=patch)

    def __str__(self) -> str:
        return f"{self.component}@{self.major}.{self.minor}.{self.patch}"

    def _tuple(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __lt__(self, other: "ComponentVersion") -> bool:   # type: ignore[override]
        return self._tuple() < other._tuple()

    def __le__(self, other: "ComponentVersion") -> bool:   # type: ignore[override]
        return self._tuple() <= other._tuple()

    def __gt__(self, other: "ComponentVersion") -> bool:   # type: ignore[override]
        return self._tuple() > other._tuple()

    def __ge__(self, other: "ComponentVersion") -> bool:   # type: ignore[override]
        return self._tuple() >= other._tuple()


# ---------------------------------------------------------------------------
# CompatibilityRule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompatibilityRule:
    """One compatibility pairing between two components.

    A pair (cv_a, cv_b) is covered by this rule when:
        min_a <= cv_a <= max_a  AND  min_b <= cv_b <= max_b

    ``None`` on a bound means "unbounded" (no minimum / no maximum).
    """

    component_a: str                          # e.g. Component.JARVIS.value
    component_b: str                          # e.g. Component.PRIME.value
    min_a: Optional[Tuple[int, int, int]] = None
    max_a: Optional[Tuple[int, int, int]] = None
    min_b: Optional[Tuple[int, int, int]] = None
    max_b: Optional[Tuple[int, int, int]] = None

    def covers(self, cv_a: ComponentVersion, cv_b: ComponentVersion) -> bool:
        """Return True if (cv_a, cv_b) falls within this rule's version bounds."""
        if {cv_a.component, cv_b.component} != {self.component_a, self.component_b}:
            return False
        # Orient so a_ver matches component_a
        if cv_a.component == self.component_a:
            a_ver, b_ver = cv_a, cv_b
        else:
            a_ver, b_ver = cv_b, cv_a

        at = a_ver._tuple()
        bt = b_ver._tuple()

        if self.min_a is not None and at < self.min_a:
            return False
        if self.max_a is not None and at > self.max_a:
            return False
        if self.min_b is not None and bt < self.min_b:
            return False
        if self.max_b is not None and bt > self.max_b:
            return False
        return True


# ---------------------------------------------------------------------------
# Default compatibility matrix (N/N-1/N+1 support)
# ---------------------------------------------------------------------------

# Rule convention: each JARVIS minor release supports N-1 and N+1 Prime/Reactor
# minor releases.  Patch versions are always compatible within a supported minor.
DEFAULT_RULES: List[CompatibilityRule] = [
    # JARVIS 2.x ↔ Prime 2.x  (same minor family)
    CompatibilityRule(
        component_a=Component.JARVIS.value,
        component_b=Component.PRIME.value,
        min_a=(2, 0, 0), max_a=(2, 99, 99),
        min_b=(2, 0, 0), max_b=(2, 99, 99),
    ),
    # JARVIS 2.x ↔ Prime 1.x  (N/N-1: support previous Prime major)
    CompatibilityRule(
        component_a=Component.JARVIS.value,
        component_b=Component.PRIME.value,
        min_a=(2, 0, 0), max_a=(2, 99, 99),
        min_b=(1, 0, 0), max_b=(1, 99, 99),
    ),
    # JARVIS 2.x ↔ Reactor 2.x
    CompatibilityRule(
        component_a=Component.JARVIS.value,
        component_b=Component.REACTOR.value,
        min_a=(2, 0, 0), max_a=(2, 99, 99),
        min_b=(2, 0, 0), max_b=(2, 99, 99),
    ),
    # JARVIS 2.x ↔ Reactor 1.x  (N/N-1)
    CompatibilityRule(
        component_a=Component.JARVIS.value,
        component_b=Component.REACTOR.value,
        min_a=(2, 0, 0), max_a=(2, 99, 99),
        min_b=(1, 0, 0), max_b=(1, 99, 99),
    ),
    # Prime 2.x ↔ Reactor 2.x  (peer services must share same major)
    CompatibilityRule(
        component_a=Component.PRIME.value,
        component_b=Component.REACTOR.value,
        min_a=(2, 0, 0), max_a=(2, 99, 99),
        min_b=(2, 0, 0), max_b=(2, 99, 99),
    ),
]


# ---------------------------------------------------------------------------
# CompatibilityMatrix
# ---------------------------------------------------------------------------


class CompatibilityMatrix:
    """Checks component version pairs against a list of CompatibilityRules.

    Usage::

        matrix = CompatibilityMatrix(DEFAULT_RULES)
        ok, reason = matrix.is_compatible(
            ComponentVersion.parse("jarvis", "2.3.0"),
            ComponentVersion.parse("prime", "2.2.0"),
        )
    """

    def __init__(self, rules: List[CompatibilityRule]) -> None:
        self._rules = list(rules)

    def is_compatible(
        self,
        cv_a: ComponentVersion,
        cv_b: ComponentVersion,
    ) -> Tuple[bool, str]:
        """Check if (cv_a, cv_b) is permitted by any rule.

        Returns ``(True, "")`` on success, or
        ``(False, "<reason>")`` on failure.
        """
        for rule in self._rules:
            if rule.covers(cv_a, cv_b):
                return True, ""

        reason = (
            f"{cv_a} ↔ {cv_b} not covered by any compatibility rule"
        )
        logger.warning("[CompatMatrix] %s", reason)
        return False, reason

    def check_all(
        self, versions: Dict[str, str]
    ) -> List[str]:
        """Check all unique pairs in *versions* for compatibility.

        Parameters
        ----------
        versions:
            Mapping of ``{component_name: "major.minor.patch"}``.

        Returns
        -------
        List of incompatibility reason strings.  Empty list means all good.
        """
        component_versions = [
            ComponentVersion.parse(comp, ver)
            for comp, ver in versions.items()
        ]
        issues: List[str] = []
        seen = set()
        for i, cv_a in enumerate(component_versions):
            for cv_b in component_versions[i + 1:]:
                key = (
                    min(cv_a.component, cv_b.component),
                    max(cv_a.component, cv_b.component),
                )
                if key in seen:
                    continue
                seen.add(key)
                ok, reason = self.is_compatible(cv_a, cv_b)
                if not ok:
                    issues.append(reason)
        return issues


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_matrix: Optional[CompatibilityMatrix] = None


def get_compatibility_matrix() -> CompatibilityMatrix:
    """Return (lazily creating) the process-wide CompatibilityMatrix."""
    global _g_matrix
    if _g_matrix is None:
        _g_matrix = CompatibilityMatrix(DEFAULT_RULES)
    return _g_matrix
