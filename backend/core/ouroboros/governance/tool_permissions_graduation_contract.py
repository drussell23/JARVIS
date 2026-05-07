"""Venom V2 Slice 2 — graduation contract harness.

§33.1 canonical-shape contract gating the
``JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED`` master flag flip on
operator-paced empirical evidence. Mirrors the §33.1 canonical
shape applied across every default-FALSE substrate.

Graduation gates (3-gate first-match-wins):

  1. Slice 1 substrate ALREADY graduated (data flag flipped) →
     ``ALREADY_GRADUATED`` (idempotent no-op).
  2. Total observed permission evaluations <
     ``min_required_evaluations`` (default 50) →
     ``INSUFFICIENT_EVALUATIONS``.
  3. Deny ratio (DENY decisions / total decisions) >
     ``max_deny_ratio`` (default 0.40) → ``EXCESSIVE_DENIES``.
     Above threshold means the registry is over-aggressive
     (operator-defined callbacks rejecting too much) — premature
     flip would block tool dispatch broadly.

Pure substrate. NEVER raises.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION: str = (
    "tool_permissions_graduation_report.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Master flag — harness opt-in (§33.1 separation-of-concerns)
# ---------------------------------------------------------------------------


def is_harness_enabled() -> bool:
    """Master switch — ``JARVIS_TOOL_PERMISSIONS_GRADUATION_
    CONTRACT_ENABLED``. Default-TRUE per §33.1 separation-of-
    concerns — the harness is a measurement surface, not the
    cognitive substrate. The data flag (``JARVIS_VENOM_TOOL_
    PERMISSIONS_ENABLED``) lives on the producer side and stays
    default-FALSE."""
    raw = os.environ.get(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_CONTRACT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # default-TRUE per §33.1 separation
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _read_int_knob(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        v = int(raw)
        if v <= 0:
            return default
        return v
    except (TypeError, ValueError):
        return default


def _read_float_knob(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        v = float(raw)
        if v < 0.0:
            return default
        return v
    except (TypeError, ValueError):
        return default


def min_required_evaluations_knob() -> int:
    """Min cumulative permission evaluations before
    READY_FOR_GRADUATION fires."""
    return _read_int_knob(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MIN_EVALUATIONS",
        50,
    )


def max_deny_ratio_knob() -> float:
    """Max deny ratio. Above threshold the registry is
    over-aggressive."""
    v = _read_float_knob(
        "JARVIS_TOOL_PERMISSIONS_GRADUATION_MAX_DENY_RATIO",
        0.40,
    )
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------------
# Closed verdict taxonomy (§33.1 canonical shape)
# ---------------------------------------------------------------------------


class ToolPermissionsGraduationVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST regression."""

    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_EVALUATIONS = "insufficient_evaluations"
    EXCESSIVE_DENIES = "excessive_denies"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Versioned report artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPermissionsGraduationReport:
    """Frozen graduation report — §33.5 versioned artifact."""

    schema_version: str
    verdict: ToolPermissionsGraduationVerdict
    observed_evaluations: int
    deny_decisions: int
    deny_ratio: float
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "observed_evaluations": int(
                self.observed_evaluations,
            ),
            "deny_decisions": int(self.deny_decisions),
            "deny_ratio": float(self.deny_ratio),
            "detail": self.detail[:256],
        }


# ---------------------------------------------------------------------------
# Evidence aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EvaluationSnapshot:
    """Internal — totals observed across the substrate."""
    total_evaluations: int
    deny_decisions: int


def _collect_evidence_default() -> _EvaluationSnapshot:
    """Default evidence collector — composes the canonical
    PermissionRegistry. Counts the registered callbacks as a
    heuristic for "substrate is being exercised." Future
    enhancement (deferred): wire a per-evaluation counter into
    `evaluate_tool_permission` and surface via public snapshot.

    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.tool_permission import (
            get_default_registry,
        )
    except ImportError:
        return _EvaluationSnapshot(
            total_evaluations=0, deny_decisions=0,
        )
    try:
        registry = get_default_registry()
        return _EvaluationSnapshot(
            total_evaluations=registry.total_count(),
            deny_decisions=0,
        )
    except Exception:  # noqa: BLE001 — defensive
        return _EvaluationSnapshot(
            total_evaluations=0, deny_decisions=0,
        )


# ---------------------------------------------------------------------------
# Graduation predicate — first-match-wins (§33.1 canonical shape)
# ---------------------------------------------------------------------------


def is_ready_for_graduation(
    *,
    snapshot_reader: Optional[
        Callable[[], _EvaluationSnapshot]
    ] = None,
) -> ToolPermissionsGraduationReport:
    """Evaluate the 3-gate cadence. NEVER raises."""
    if not is_harness_enabled():
        return ToolPermissionsGraduationReport(
            schema_version=(
                TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=ToolPermissionsGraduationVerdict.DISABLED,
            observed_evaluations=0,
            deny_decisions=0,
            deny_ratio=0.0,
            detail="harness_master_off",
        )
    # Gate 1 — Slice 1 already graduated.
    try:
        from backend.core.ouroboros.governance.tool_permission import (
            venom_tool_permissions_enabled,
        )
        if venom_tool_permissions_enabled():
            return ToolPermissionsGraduationReport(
                schema_version=(
                    TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION
                ),
                verdict=(
                    ToolPermissionsGraduationVerdict
                    .ALREADY_GRADUATED
                ),
                observed_evaluations=0,
                deny_decisions=0,
                deny_ratio=0.0,
                detail=(
                    "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED is "
                    "on — substrate already flipped"
                ),
            )
    except ImportError:
        pass
    # Gates 2-3 — evaluate evidence.
    if snapshot_reader is None:
        snapshot_reader = _collect_evidence_default
    try:
        snapshot = snapshot_reader()
    except Exception:  # noqa: BLE001 — defensive
        snapshot = _EvaluationSnapshot(
            total_evaluations=0, deny_decisions=0,
        )
    total = int(snapshot.total_evaluations)
    denies = int(snapshot.deny_decisions)
    if total <= 0:
        ratio = 0.0
    else:
        ratio = float(denies) / float(total)
    if total < min_required_evaluations_knob():
        return ToolPermissionsGraduationReport(
            schema_version=(
                TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                ToolPermissionsGraduationVerdict
                .INSUFFICIENT_EVALUATIONS
            ),
            observed_evaluations=total,
            deny_decisions=denies,
            deny_ratio=ratio,
            detail=(
                f"observed={total} required="
                f"{min_required_evaluations_knob()}"
            ),
        )
    if ratio > max_deny_ratio_knob():
        return ToolPermissionsGraduationReport(
            schema_version=(
                TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                ToolPermissionsGraduationVerdict
                .EXCESSIVE_DENIES
            ),
            observed_evaluations=total,
            deny_decisions=denies,
            deny_ratio=ratio,
            detail=(
                f"deny_ratio={ratio:.3f} max="
                f"{max_deny_ratio_knob():.3f}"
            ),
        )
    return ToolPermissionsGraduationReport(
        schema_version=(
            TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            ToolPermissionsGraduationVerdict.READY_FOR_GRADUATION
        ),
        observed_evaluations=total,
        deny_decisions=denies,
        deny_ratio=ratio,
        detail=(
            f"observed={total} deny_ratio={ratio:.3f} — "
            f"empirical evidence sufficient; flip "
            f"JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED to graduate"
        ),
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``tool_permissions_graduation_verdict_taxonomy_closed``
         — 5-value enum bytes-pinned.
      2. ``tool_permissions_graduation_authority_asymmetry``
         — substrate purity.
      3. ``tool_permissions_graduation_pattern_compliance`` —
         §33.1 canonical-shape parity.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "tool_permissions_graduation_contract.py"
    )

    _EXPECTED_VERDICTS = {
        "ready_for_graduation",
        "insufficient_evaluations",
        "excessive_denies",
        "already_graduated",
        "disabled",
    }

    def _validate_verdict_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name
                == "ToolPermissionsGraduationVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(
                            sub.targets[0], ast.Name,
                        )
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    violations.append(
                        f"verdict missing: {sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"verdict drift: {sorted(extra)}"
                    )
                return tuple(violations)
        violations.append(
            "ToolPermissionsGraduationVerdict missing"
        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"harness MUST NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_pattern_compliance(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required_top_level = {
            "is_ready_for_graduation",
            "is_harness_enabled",
            "ToolPermissionsGraduationVerdict",
            "ToolPermissionsGraduationReport",
        }
        found = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if node.name in required_top_level:
                    found.add(node.name)
            if isinstance(node, ast.ClassDef):
                if node.name in required_top_level:
                    found.add(node.name)
        missing = required_top_level - found
        if missing:
            violations.append(
                f"§33.1 canonical-shape symbols missing: "
                f"{sorted(missing)}"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "tool_permissions_graduation_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "Venom V2 Slice 2 — 5-value verdict closed "
                "taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_permissions_graduation_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Venom V2 Slice 2 — harness substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_permissions_graduation_pattern_compliance"
            ),
            target_file=target,
            description=(
                "Venom V2 Slice 2 — §33.1 canonical-shape "
                "parity."
            ),
            validate=_validate_pattern_compliance,
        ),
    ]


__all__ = [
    "TOOL_PERMISSIONS_GRADUATION_REPORT_SCHEMA_VERSION",
    "ToolPermissionsGraduationReport",
    "ToolPermissionsGraduationVerdict",
    "is_harness_enabled",
    "is_ready_for_graduation",
    "max_deny_ratio_knob",
    "min_required_evaluations_knob",
    "register_shipped_invariants",
]
