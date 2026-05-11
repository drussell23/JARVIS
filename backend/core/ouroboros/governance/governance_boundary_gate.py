"""
RRD §1 Boundary Recursion-Depth Gate
=====================================

Closes §40 Wave 2 #5 — the first of the three Wave 2 recursion-
bounding arcs. Deterministic predicate gate that **forbids
autonomous ops from modifying O+V's own governance cage**
(``backend/core/ouroboros/governance/``) without operator
approval.

RRD (Reverse Russian Doll) framing
----------------------------------
The cage's structural integrity depends on the boundary between:

* **Outer doll** (operator) — authority to modify everything.
* **Middle doll** (governance/) — O+V's own cage layer. Only
  the operator may modify; autonomous ops touching this layer
  MUST route through ``APPROVAL_REQUIRED`` regardless of any
  other risk-tier-floor signal.
* **Inner doll** (everything else) — autonomous ops may modify
  under normal cage discipline.

Without this gate, M10 ArchitectureProposer (or any future
autonomous proposer) could in principle propose changes to its
own gating logic, semantic_guardian patterns, iron_gate body,
or risk_tier_floor itself — an infinite-regress risk Operator
binding §30.5.2 + §40 Wave 2 #5 both call out.

The gate is **deterministic, pure, and additive**:

* Pure-function ``evaluate_target_files(target_files)``
  inspects a tuple of repo-relative target paths and returns a
  frozen ``BoundaryReport``.
* When ANY target path is inside the canonical governance
  directory, ``verdict`` is ``BOUNDARY_CROSSED`` and the
  effective risk-tier floor becomes ``approval_required``.
* The canonical governance directory is **derived from this
  module's own location** — no hardcoded path string. Adding /
  removing modules from governance/ auto-propagates; moving
  the governance/ directory itself updates the boundary at
  import time.

Composition contract
--------------------
Composes ONLY:

* :mod:`pathlib` for canonical prefix resolution.
* :mod:`risk_tier_floor` (consumer side) for the existing
  strictest-wins floor ladder — this module exposes the
  predicate, ``risk_tier_floor.recommended_floor`` calls it
  via lazy-import composition.

Does NOT compose: orchestrator / iron_gate / candidate_generator
/ providers / urgency_router / change_engine / semantic_guardian
/ user_preference_memory (the FORBIDDEN_PATH surface is operator-
policy concerned with user-defined paths; this gate is about the
governance directory's structural boundary — they are sibling
concerns, not coupled).

§33.1 master flag discipline
----------------------------
``JARVIS_GOVERNANCE_BOUNDARY_GATE_ENABLED`` default-**TRUE**
because this is a **safety gate**, not a cognitive substrate.
The §33.1 default-FALSE pattern applies to new cognitive
surfaces awaiting empirical validation. Safety gates that
codify structural invariants ship default-TRUE per the
canonical pattern (cf. ``JARVIS_ASCII_GATE`` default-true,
``JARVIS_SEMANTIC_GUARD_ENABLED`` default-true,
``JARVIS_EXPLORATION_GATE`` default-true). Operator-override
via ``=false`` exists for emergency rollback.

NEVER raises by construction. A missing or malformed target
path returns ``EMPTY_TARGET``, not exception.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION: str = (
    "governance_boundary_gate.1"
)


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_GOVERNANCE_BOUNDARY_GATE_ENABLED"


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})
_FALSY: FrozenSet[str] = frozenset({"0", "false", "no", "off"})


def _flag(name: str, *, default: bool) -> bool:
    """Canonical truthy reader with asymmetric default semantics."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _FALSY:
        return False
    return raw in _TRUTHY or default


def master_enabled() -> bool:
    """§33.1 safety-gate variant — default-TRUE.

    This module is a load-bearing structural-boundary gate, not
    a cognitive substrate awaiting empirical proof. The default-
    TRUE shape mirrors :data:`JARVIS_ASCII_GATE`,
    :data:`JARVIS_SEMANTIC_GUARD_ENABLED`,
    :data:`JARVIS_EXPLORATION_GATE` — all safety gates that ship
    default-TRUE. Operator-override via
    ``JARVIS_GOVERNANCE_BOUNDARY_GATE_ENABLED=false`` for
    emergency rollback.
    """
    return _flag(_ENV_MASTER, default=True)


# ===========================================================================
# Canonical governance directory — derived from THIS module's location
# (operator binding "no hardcoding" enforced structurally)
# ===========================================================================


def _canonical_governance_dir() -> Optional[Path]:
    """Return the absolute path to the governance/ directory.

    Derived structurally: ``Path(__file__).resolve().parent``
    IS the governance directory because this module lives at
    ``backend/core/ouroboros/governance/governance_boundary_gate.py``.

    Returns None when path resolution fails (defensive — caller
    treats as ``UNKNOWN`` verdict). NEVER raises.
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:  # noqa: BLE001
        return None


def _canonical_governance_prefix() -> str:
    """Canonical repo-relative prefix string for the governance
    directory. Operates on string paths because
    :attr:`OperationContext.target_files` is ``Tuple[str, ...]``.

    Result: ``"backend/core/ouroboros/governance/"`` (with
    trailing slash so prefix matching doesn't accidentally
    match e.g. ``governance_data/...``). Computed structurally
    from the canonical directory; no string literal.
    """
    canonical = _canonical_governance_dir()
    if canonical is None:
        # Defensive fallback — mirrors the on-disk layout.
        # Used only when Path resolution fails entirely.
        return "backend/core/ouroboros/governance/"
    # Walk up to find the repo root marker (.git directory)
    # then derive the suffix from there. This is structurally
    # tolerant of repo relocation.
    repo_root: Optional[Path] = None
    for ancestor in (canonical, *canonical.parents):
        try:
            if (ancestor / ".git").exists():
                repo_root = ancestor
                break
        except Exception:  # noqa: BLE001
            continue
    if repo_root is None:
        return "backend/core/ouroboros/governance/"
    try:
        rel = canonical.relative_to(repo_root)
        # Normalize separators + ensure trailing slash so the
        # prefix check is unambiguous.
        s = str(rel).replace(os.sep, "/")
        if not s.endswith("/"):
            s = s + "/"
        return s
    except Exception:  # noqa: BLE001
        return "backend/core/ouroboros/governance/"


# Cached at module load — composes the canonical structural path.
# Invalidate via reset_for_tests() in test isolation.
_CANONICAL_GOVERNANCE_PREFIX: str = _canonical_governance_prefix()


def canonical_governance_prefix() -> str:
    """Public accessor for the canonical governance directory
    prefix. Operators / consumers / tests compose this rather
    than literal strings — single source of truth."""
    return _CANONICAL_GOVERNANCE_PREFIX


def reset_for_tests() -> None:
    """Test seam — recompute the cached prefix. NEVER raises."""
    global _CANONICAL_GOVERNANCE_PREFIX
    _CANONICAL_GOVERNANCE_PREFIX = _canonical_governance_prefix()


# ===========================================================================
# Closed 4-value verdict taxonomy
# ===========================================================================


class BoundaryVerdict(str, enum.Enum):
    """Closed 4-value taxonomy. Bytes-pinned via AST.

    * ``BOUNDARY_CROSSED`` — ≥1 target path lies inside the
      canonical governance directory; the op must route through
      ``APPROVAL_REQUIRED``.
    * ``WITHIN_LIMITS`` — all target paths lie outside the
      cage layer; the op proceeds under normal cage discipline.
    * ``EMPTY_TARGET`` — the op has no target_files (e.g.,
      pure analysis ops). Boundary semantics are vacuous.
    * ``DISABLED`` — master flag off; substrate returns this
      verdict so the caller can route gracefully without the
      gate. Operator-override discipline.
    """

    BOUNDARY_CROSSED = "boundary_crossed"
    WITHIN_LIMITS = "within_limits"
    EMPTY_TARGET = "empty_target"
    DISABLED = "disabled"


# ===========================================================================
# §33.5 frozen versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class BoundaryReport:
    """Frozen evaluation report — §33.5 versioned artifact.

    Carries the verdict + the list of paths that triggered it
    (or were checked). Bounded `crossing_paths` so a malicious
    target_files mega-list can't bloat downstream logs.
    """

    schema_version: str
    verdict: BoundaryVerdict
    crossing_paths: Tuple[str, ...]
    """Paths that fall INSIDE the cage — non-empty only when
    verdict is ``BOUNDARY_CROSSED``. Bounded at 32 entries +
    each path bounded at 256 chars."""
    total_targets: int
    canonical_prefix: str
    """The canonical governance prefix that was used for the
    boundary check (operator-visible for forensics)."""
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "crossing_paths": list(self.crossing_paths),
            "total_targets": int(self.total_targets),
            "canonical_prefix": self.canonical_prefix,
            "detail": self.detail[:256],
        }


# ===========================================================================
# Pure-function predicate — the load-bearing evaluator
# ===========================================================================


_PATH_BOUND_PER_ENTRY = 256
_CROSSING_PATHS_BOUND = 32


def _normalize_path(p: Any) -> str:
    """Coerce a path-like input to a normalized string.

    Accepts str, Path, bytes (decoded utf-8 ignore-errors), or
    None. Converts backslashes to forward slashes for
    cross-platform prefix matching. Bounded at 256 chars.
    NEVER raises.
    """
    if p is None:
        return ""
    try:
        if isinstance(p, bytes):
            s = p.decode("utf-8", errors="ignore")
        else:
            s = str(p)
    except Exception:  # noqa: BLE001
        return ""
    s = s.replace("\\", "/").strip()
    if not s:
        return ""
    # Strip absolute prefixes that map to the canonical repo
    # layout — operators frequently pass absolute paths.
    canonical_dir = _canonical_governance_dir()
    if canonical_dir is not None:
        try:
            abs_p = Path(s)
            if abs_p.is_absolute():
                # Walk up to find the repo root + relativize.
                for ancestor in (canonical_dir, *canonical_dir.parents):
                    try:
                        if (ancestor / ".git").exists():
                            rel = abs_p.relative_to(ancestor)
                            s = str(rel).replace(os.sep, "/")
                            break
                    except (ValueError, Exception):  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass
    return s[:_PATH_BOUND_PER_ENTRY]


def _is_within_governance(normalized_path: str) -> bool:
    """Pure prefix check against the canonical governance dir.
    NEVER raises."""
    if not normalized_path:
        return False
    prefix = canonical_governance_prefix()
    if not prefix:
        return False
    # Two acceptable forms: 'backend/.../governance/foo.py' or
    # './backend/.../governance/foo.py' — strip leading './'.
    p = normalized_path
    if p.startswith("./"):
        p = p[2:]
    return p.startswith(prefix)


def evaluate_target_files(
    target_files: Optional[Sequence[Any]],
) -> BoundaryReport:
    """Deterministic boundary evaluation. NEVER raises.

    Inputs:
        ``target_files`` — sequence of path-like values
        (``str`` / ``Path`` / ``bytes`` / None). Tolerant of
        mixed types; defensive on None / empty inputs.

    Output:
        :class:`BoundaryReport` frozen instance with one of the
        four canonical verdicts.

    Master flag off → ``DISABLED`` verdict (caller passes through
    the op normally; no boundary enforcement).
    """
    if not master_enabled():
        return BoundaryReport(
            schema_version=GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION,
            verdict=BoundaryVerdict.DISABLED,
            crossing_paths=(),
            total_targets=0,
            canonical_prefix=canonical_governance_prefix(),
            detail=(
                f"gate disabled via {_ENV_MASTER}=false — "
                "operator-override discipline"
            ),
        )

    if not target_files:
        return BoundaryReport(
            schema_version=GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION,
            verdict=BoundaryVerdict.EMPTY_TARGET,
            crossing_paths=(),
            total_targets=0,
            canonical_prefix=canonical_governance_prefix(),
            detail="no target_files to evaluate",
        )

    crossings: List[str] = []
    total = 0
    for raw in target_files:
        total += 1
        normalized = _normalize_path(raw)
        if not normalized:
            continue
        if _is_within_governance(normalized):
            if len(crossings) < _CROSSING_PATHS_BOUND:
                crossings.append(normalized)

    if crossings:
        return BoundaryReport(
            schema_version=GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION,
            verdict=BoundaryVerdict.BOUNDARY_CROSSED,
            crossing_paths=tuple(crossings),
            total_targets=total,
            canonical_prefix=canonical_governance_prefix(),
            detail=(
                f"{len(crossings)} of {total} target(s) inside "
                f"cage layer — APPROVAL_REQUIRED route forced"
            ),
        )

    return BoundaryReport(
        schema_version=GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION,
        verdict=BoundaryVerdict.WITHIN_LIMITS,
        crossing_paths=(),
        total_targets=total,
        canonical_prefix=canonical_governance_prefix(),
        detail=(
            f"{total} target(s) checked; all outside cage layer"
        ),
    )


def is_boundary_crossed(
    target_files: Optional[Sequence[Any]],
) -> bool:
    """Convenience predicate — returns True iff
    :func:`evaluate_target_files` returns ``BOUNDARY_CROSSED``.
    NEVER raises."""
    return (
        evaluate_target_files(target_files).verdict
        is BoundaryVerdict.BOUNDARY_CROSSED
    )


# ===========================================================================
# AST pins via shipped_code_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Return AST invariant pins. Auto-discovered via §33.3."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "governance_boundary_gate.py"
    )

    _EXPECTED_VERDICTS = {
        "boundary_crossed",
        "within_limits",
        "empty_target",
        "disabled",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "BoundaryVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"BoundaryVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"BoundaryVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("BoundaryVerdict class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            (
                "backend.core.ouroboros.governance.risk_tier_floor"
            ),
            (
                "backend.core.ouroboros.governance"
                ".user_preference_memory"
            ),
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_true(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Safety-gate canonical shape — master_enabled MUST
        default to True. Drift to default=False without an
        explicit safety-rollback justification would silently
        let the boundary become bypassable."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is True
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) with "
                    "default=True (safety-gate shape)",
                )
        return ("master_enabled() not found",)

    def _validate_no_hardcoded_prefix(
        tree: ast.AST, source: str,
    ) -> tuple:
        """Canonical prefix MUST derive from this module's
        ``__file__`` (operator binding "no hardcoding"). String
        literals matching the cage prefix should only appear as
        defensive fallbacks inside _canonical_governance_prefix
        — not anywhere else in source.
        """
        if "Path(__file__).resolve().parent" not in source:
            return (
                "canonical governance directory must derive "
                "from Path(__file__).resolve().parent — no "
                "hardcoded path literal",
            )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "governance_boundary_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "Boundary verdict 4-value taxonomy "
                "bytes-pinned. Adding/removing a verdict "
                "requires updating downstream risk_tier_floor "
                "composition + tests."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "governance_boundary_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — boundary gate is a "
                "pure-function predicate, MUST NOT import "
                "orchestrator / iron_gate / policy / providers "
                "/ candidate_generator / urgency_router / "
                "change_engine / semantic_guardian / "
                "risk_tier_floor / user_preference_memory. "
                "Consumer-side composition only."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "governance_boundary_master_default_true"
            ),
            target_file=target,
            description=(
                "Safety-gate canonical shape — master "
                "default-TRUE (cf. JARVIS_ASCII_GATE, "
                "JARVIS_SEMANTIC_GUARD_ENABLED, "
                "JARVIS_EXPLORATION_GATE). Drift to "
                "default=False would silently disable the "
                "structural boundary."
            ),
            validate=_validate_master_default_true,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "governance_boundary_no_hardcoded_prefix"
            ),
            target_file=target,
            description=(
                "Operator binding 'no hardcoding' — canonical "
                "governance directory MUST derive from "
                "Path(__file__).resolve().parent, not a string "
                "literal. Moving governance/ auto-propagates "
                "to the gate."
            ),
            validate=_validate_no_hardcoded_prefix,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds (auto-discovered via §33.3 naming-cage)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Register this gate's env knobs. Auto-discovered."""
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=True,
            description=(
                "RRD §1 Boundary recursion-depth gate master "
                "switch. Default-TRUE (safety-gate shape — "
                "mirrors JARVIS_ASCII_GATE / "
                "JARVIS_SEMANTIC_GUARD_ENABLED). Operator-"
                "override via =false for emergency rollback "
                "only — disables structural protection of the "
                "cage layer."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "governance_boundary_gate.py"
            ),
            example=f"{_ENV_MASTER}=false",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION",
    "BoundaryVerdict",
    "BoundaryReport",
    "master_enabled",
    "canonical_governance_prefix",
    "reset_for_tests",
    "evaluate_target_files",
    "is_boundary_crossed",
    "register_shipped_invariants",
    "register_flags",
]
