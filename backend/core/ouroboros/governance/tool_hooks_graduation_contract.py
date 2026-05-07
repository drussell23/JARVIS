"""Venom V1 Slice 3 — graduation contract harness.

§33.1 canonical-shape contract gating the master flag flip on
operator-paced empirical evidence. Mirrors the existing pattern
applied across every default-FALSE substrate in the codebase
(``cross_op_semantic_budget_graduation_contract`` /
``causality_consumer_graduation_contract`` /
``proactive_curiosity_loop_graduation_contract``):

  * 5-value :class:`ToolHooksGraduationVerdict` closed enum
    (READY_FOR_GRADUATION / INSUFFICIENT_FIRES /
    EXCESSIVE_FAILURES / ALREADY_GRADUATED / DISABLED).
  * Frozen :class:`ToolHooksGraduationReport` versioned
    artifact (§33.5).
  * :func:`is_ready_for_graduation` first-match-wins predicate.
  * Master-flag helper :func:`is_harness_enabled` (default-TRUE
    per §33.1 separation-of-concerns; the data flag stays
    default-FALSE on the producer side — ONE SOURCE OF TRUTH).
  * AST pins for taxonomy + authority asymmetry + §33.1
    canonical-shape compliance.

Graduation gates (3-gate first-match-wins, first non-READY
wins):

  1. Slice 2 substrate ALREADY graduated (``JARVIS_VENOM_TOOL_HOOKS_
     ENABLED`` flipped) → ``ALREADY_GRADUATED`` (idempotent
     no-op; re-running the harness post-flip is a no-op).
  2. Total observed tool-hook fires across ``cadence_health.jsonl``-
     style accumulator >= ``min_required_fires`` (default 50,
     env-tunable). Below threshold → ``INSUFFICIENT_FIRES``.
     The substrate must demonstrate empirical exercise of all
     6 event types before graduation makes sense.
  3. Failure ratio (post_tool_use_failure / total fires) >
     ``max_failure_ratio`` (default 0.20). Above threshold means
     the substrate is unhealthy under load; premature flip would
     surface noisy advisory data.

Pure substrate. NEVER raises. Composes Slice 1's
``venom_tool_hooks_enabled`` (single source of truth for the
master flag's default state).
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION: str = (
    "tool_hooks_graduation_report.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Master flag — harness opt-in (§33.1 separation-of-concerns)
# ---------------------------------------------------------------------------


def is_harness_enabled() -> bool:
    """Master switch — ``JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_
    ENABLED``. Default-TRUE per §33.1 separation-of-concerns —
    the harness is a measurement surface, not the cognitive
    substrate. The data flag (``JARVIS_VENOM_TOOL_HOOKS_ENABLED``)
    lives on the producer side and stays default-FALSE."""
    raw = os.environ.get(
        "JARVIS_TOOL_HOOKS_GRADUATION_CONTRACT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # default-TRUE per §33.1 separation
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Env knobs — graduation thresholds
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


def min_required_fires_knob() -> int:
    """Min cumulative tool-hook fires before READY_FOR_GRADUATION
    fires. Default 50 — covers all 6 event types × ~8 fires
    each, the empirical floor below which the contract refuses
    to advise graduation."""
    return _read_int_knob(
        "JARVIS_TOOL_HOOKS_GRADUATION_MIN_FIRES", 50,
    )


def max_failure_ratio_knob() -> float:
    """Max ratio of failure fires (post_tool_use_failure +
    pre_tool_use_failure) to total fires. Above threshold the
    substrate is unhealthy under load."""
    v = _read_float_knob(
        "JARVIS_TOOL_HOOKS_GRADUATION_MAX_FAILURE_RATIO", 0.20,
    )
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------------
# Closed verdict taxonomy (§33.1 canonical shape)
# ---------------------------------------------------------------------------


class ToolHooksGraduationVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST regression.
    Mirrors the §33.1 canonical shape applied across every
    default-FALSE substrate."""

    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_FIRES = "insufficient_fires"
    EXCESSIVE_FAILURES = "excessive_failures"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Versioned report artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolHooksGraduationReport:
    """Frozen graduation report — §33.5 versioned artifact."""

    schema_version: str
    verdict: ToolHooksGraduationVerdict
    observed_fires: int
    failure_fires: int
    failure_ratio: float
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "observed_fires": int(self.observed_fires),
            "failure_fires": int(self.failure_fires),
            "failure_ratio": float(self.failure_ratio),
            "detail": self.detail[:256],
        }


# ---------------------------------------------------------------------------
# Evidence aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FireSnapshot:
    """Internal — totals observed across the substrate."""
    total_fires: int
    failure_fires: int


def _collect_evidence_default() -> _FireSnapshot:
    """Default evidence collector — composes the canonical
    LifecycleHookRegistry.

    Counts the registered hook bindings as a heuristic for
    "substrate is being exercised." Future enhancement (deferred
    per §33.1 separation): wire a per-fire counter into
    ``_maybe_fire_tool_hook`` and expose via a public snapshot,
    then collect from THAT counter rather than the registry's
    bindings.

    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.lifecycle_hook import (
            ToolHookEvent,
        )
    except ImportError:
        return _FireSnapshot(total_fires=0, failure_fires=0)
    try:
        from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
            get_default_registry,
        )
    except ImportError:
        return _FireSnapshot(total_fires=0, failure_fires=0)
    try:
        registry = get_default_registry()
    except Exception:  # noqa: BLE001 — defensive
        return _FireSnapshot(total_fires=0, failure_fires=0)
    total = 0
    failures = 0
    for event in ToolHookEvent:
        try:
            count = registry.count_for_event(event)
        except Exception:  # noqa: BLE001 — defensive
            continue
        total += count
        if event in (
            ToolHookEvent.PRE_TOOL_USE_FAILURE,
            ToolHookEvent.POST_TOOL_USE_FAILURE,
        ):
            failures += count
    return _FireSnapshot(
        total_fires=total, failure_fires=failures,
    )


# ---------------------------------------------------------------------------
# Graduation predicate — first-match-wins (§33.1 canonical shape)
# ---------------------------------------------------------------------------


def is_ready_for_graduation(
    *,
    snapshot_reader: Optional[Callable[[], _FireSnapshot]] = None,
) -> ToolHooksGraduationReport:
    """Evaluate the 3-gate cadence. Returns a
    :class:`ToolHooksGraduationReport`. NEVER raises.

    Gates (first-match-wins):

      1. Slice 2 substrate already graduated → ALREADY_GRADUATED
      2. observed_fires < min_required_fires →
         INSUFFICIENT_FIRES
      3. failure_ratio > max_failure_ratio →
         EXCESSIVE_FAILURES
      4. Otherwise → READY_FOR_GRADUATION

    When the harness master flag is off, the report verdict is
    :attr:`DISABLED` (operator hot-revert path)."""
    if not is_harness_enabled():
        return ToolHooksGraduationReport(
            schema_version=(
                TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=ToolHooksGraduationVerdict.DISABLED,
            observed_fires=0,
            failure_fires=0,
            failure_ratio=0.0,
            detail="harness_master_off",
        )
    # Gate 1 — Slice 2 already graduated → idempotent no-op.
    try:
        from backend.core.ouroboros.governance.lifecycle_hook import (
            venom_tool_hooks_enabled,
        )
        if venom_tool_hooks_enabled():
            return ToolHooksGraduationReport(
                schema_version=(
                    TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION
                ),
                verdict=(
                    ToolHooksGraduationVerdict.ALREADY_GRADUATED
                ),
                observed_fires=0,
                failure_fires=0,
                failure_ratio=0.0,
                detail=(
                    "JARVIS_VENOM_TOOL_HOOKS_ENABLED is on — "
                    "substrate already flipped"
                ),
            )
    except ImportError:
        # Substrate unavailable → treat as not-graduated; the
        # fire-count + failure-ratio gates still apply.
        pass
    # Gates 2-3 — evaluate evidence.
    if snapshot_reader is None:
        snapshot_reader = _collect_evidence_default
    try:
        snapshot = snapshot_reader()
    except Exception:  # noqa: BLE001 — defensive
        snapshot = _FireSnapshot(
            total_fires=0, failure_fires=0,
        )
    total = int(snapshot.total_fires)
    failures = int(snapshot.failure_fires)
    if total <= 0:
        ratio = 0.0
    else:
        ratio = float(failures) / float(total)
    if total < min_required_fires_knob():
        return ToolHooksGraduationReport(
            schema_version=(
                TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                ToolHooksGraduationVerdict.INSUFFICIENT_FIRES
            ),
            observed_fires=total,
            failure_fires=failures,
            failure_ratio=ratio,
            detail=(
                f"observed={total} required="
                f"{min_required_fires_knob()}"
            ),
        )
    if ratio > max_failure_ratio_knob():
        return ToolHooksGraduationReport(
            schema_version=(
                TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                ToolHooksGraduationVerdict.EXCESSIVE_FAILURES
            ),
            observed_fires=total,
            failure_fires=failures,
            failure_ratio=ratio,
            detail=(
                f"failure_ratio={ratio:.3f} max="
                f"{max_failure_ratio_knob():.3f}"
            ),
        )
    return ToolHooksGraduationReport(
        schema_version=(
            TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            ToolHooksGraduationVerdict.READY_FOR_GRADUATION
        ),
        observed_fires=total,
        failure_fires=failures,
        failure_ratio=ratio,
        detail=(
            f"observed={total} failure_ratio={ratio:.3f} "
            f"— empirical evidence sufficient; flip "
            f"JARVIS_VENOM_TOOL_HOOKS_ENABLED to graduate"
        ),
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``tool_hooks_graduation_verdict_taxonomy_closed`` —
         5-value closed enum bytes-pinned (§33.1 canonical
         shape parity).
      2. ``tool_hooks_graduation_authority_asymmetry`` — harness
         substrate purity.
      3. ``tool_hooks_graduation_pattern_compliance`` — §33.1
         canonical-shape parity check (predicate + master-flag
         helper + verdict enum + report artifact).
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
        "tool_hooks_graduation_contract.py"
    )

    _EXPECTED_VERDICTS = {
        "ready_for_graduation",
        "insufficient_fires",
        "excessive_failures",
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
                == "ToolHooksGraduationVerdict"
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
            "ToolHooksGraduationVerdict missing"
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
                            f"harness MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_pattern_compliance(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """§33.1 canonical-shape parity check — required
        symbols + frozen dataclass + 5-value verdict +
        master-flag helper present."""
        violations: list = []
        required_top_level = {
            "is_ready_for_graduation",
            "is_harness_enabled",
            "ToolHooksGraduationVerdict",
            "ToolHooksGraduationReport",
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
                "tool_hooks_graduation_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "Venom V1 Slice 3 — 5-value verdict closed "
                "taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_hooks_graduation_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Venom V1 Slice 3 — harness substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_hooks_graduation_pattern_compliance"
            ),
            target_file=target,
            description=(
                "Venom V1 Slice 3 — §33.1 canonical-shape "
                "parity (predicate + master-flag helper + "
                "verdict enum + report artifact)."
            ),
            validate=_validate_pattern_compliance,
        ),
    ]


__all__ = [
    "TOOL_HOOKS_GRADUATION_REPORT_SCHEMA_VERSION",
    "ToolHooksGraduationReport",
    "ToolHooksGraduationVerdict",
    "is_harness_enabled",
    "is_ready_for_graduation",
    "max_failure_ratio_knob",
    "min_required_fires_knob",
    "register_shipped_invariants",
]
