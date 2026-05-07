"""§37 Tier 2 #13 Slice 4 — graduation contract harness.

§33.1 canonical-shape contract gating the
``JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED`` master flag flip on
operator-paced empirical evidence. Mirrors the canonical shape
applied across every default-FALSE substrate
(``tool_permissions_graduation_contract`` for Venom V2,
``cross_op_semantic_budget_graduation_contract`` for Move 7,
``proactive_curiosity_loop_graduation_contract`` for Move 8,
etc.).

Graduation gates (3-gate first-match-wins):

  1. Slice 1 substrate ALREADY graduated (data flag flipped) →
     ``ALREADY_GRADUATED`` (idempotent no-op).
  2. Total observed per-tool streams <
     ``min_required_observations`` (default 50) →
     ``INSUFFICIENT_OBSERVATIONS``. Phase 9 cadence accumulates
     evidence; below floor → not yet enough signal to
     calibrate the confidence-band thresholds.
  3. False-positive ratio (streams at MEDIUM/LOW/UNKNOWN /
     total streams) > ``max_false_positive_ratio`` (default
     0.40) → ``EXCESSIVE_FALSE_POSITIVES``. Above threshold
     means too many tool calls landed at the unsafe pole —
     either model genuinely uncertain (calibration good but
     auto-apply-clamping would fire constantly, drowning the
     operator) OR thresholds need tuning. Premature flip
     would cause every soak to clamp to NOTIFY_APPLY.

When all 3 gates pass → ``READY_FOR_GRADUATION``: operator can
flip ``JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED=true`` and
expect (a) Slice 2 SSE publication active, (b) Slice 3 risk-
tier-floor consumer clamping low-confidence ops to
NOTIFY_APPLY, (c) calibrated false-positive rate below 40%.

**Composition** (operator binding 2026-05-07): pure substrate
composing Slice 1's ``get_default_observer()`` + ``master_enabled``
+ ``band_distribution`` + ``ToolConfidenceBand``. Zero parallel
state. NEVER raises. Authority asymmetry — no orchestrator /
iron_gate / providers imports (AST-pinned).
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


logger = logging.getLogger(__name__)


TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION: str = (
    "tool_confidence_graduation_report.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Master flag — harness opt-in (§33.1 separation-of-concerns)
# ---------------------------------------------------------------------------


def is_harness_enabled() -> bool:
    """Master switch — ``JARVIS_TOOL_CONFIDENCE_INDICATOR_
    GRADUATION_CONTRACT_ENABLED``. Default-TRUE per §33.1
    separation-of-concerns: the harness is a measurement
    surface, not the cognitive substrate. The data flag
    (``JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED``) lives on the
    Slice 1 producer side and stays default-FALSE until this
    contract reports READY."""
    raw = os.environ.get(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_GRADUATION_CONTRACT_"
        "ENABLED",
        "",
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


def min_required_observations_knob() -> int:
    """Min cumulative per-tool stream observations before
    READY_FOR_GRADUATION can fire. Default 50; matches Venom
    V2's threshold (peer-pattern consistency)."""
    return _read_int_knob(
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MIN_OBSERVATIONS",
        50,
    )


def max_false_positive_ratio_knob() -> float:
    """Max acceptable ratio of streams at the unsafe pole
    (MEDIUM + LOW + UNKNOWN bands) over total observed streams.
    Above this, premature graduation would cause runaway tier-
    upgrades. Default 0.40 (40%); clamped to [0.0, 1.0]."""
    v = _read_float_knob(
        "JARVIS_TOOL_CONFIDENCE_GRADUATION_MAX_FP_RATIO",
        0.40,
    )
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------------
# Closed verdict taxonomy (§33.1 canonical shape — 5-value)
# ---------------------------------------------------------------------------


class ToolConfidenceGraduationVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST regression.

    Mirrors the canonical 5-value verdict shape applied across
    every §33.1 graduation contract: a positive READY case, two
    not-yet cases (insufficient evidence + over-firing
    diagnostic), and two no-op cases (already graduated +
    harness disabled)."""

    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    EXCESSIVE_FALSE_POSITIVES = "excessive_false_positives"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Versioned report artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolConfidenceGraduationReport:
    """Frozen graduation report — §33.5 versioned artifact."""

    schema_version: str
    verdict: ToolConfidenceGraduationVerdict
    observed_streams: int
    unsafe_streams: int
    false_positive_ratio: float
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "observed_streams": int(self.observed_streams),
            "unsafe_streams": int(self.unsafe_streams),
            "false_positive_ratio": float(
                self.false_positive_ratio,
            ),
            "detail": self.detail[:256],
        }


# ---------------------------------------------------------------------------
# Evidence aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ConfidenceSnapshot:
    """Internal — band totals observed across the substrate."""
    total_streams: int
    unsafe_streams: int  # bands at MEDIUM / LOW / UNKNOWN


def _collect_evidence_default() -> _ConfidenceSnapshot:
    """Default evidence collector — composes the canonical
    Slice 1 ``ToolConfidenceObserver.band_distribution()`` to
    aggregate per-band counts across all observed streams.

    Unsafe pole = MEDIUM + LOW + UNKNOWN (the bands that trigger
    Slice 3 risk-tier-floor clamping). Total = all bands.

    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
            ToolConfidenceBand,
            get_default_observer,
        )
    except ImportError:
        return _ConfidenceSnapshot(
            total_streams=0, unsafe_streams=0,
        )
    try:
        observer = get_default_observer()
        dist = observer.band_distribution()
        total = sum(dist.values())
        unsafe = (
            dist.get(ToolConfidenceBand.MEDIUM, 0)
            + dist.get(ToolConfidenceBand.LOW, 0)
            + dist.get(ToolConfidenceBand.UNKNOWN, 0)
        )
        return _ConfidenceSnapshot(
            total_streams=int(total),
            unsafe_streams=int(unsafe),
        )
    except Exception:  # noqa: BLE001 — defensive
        return _ConfidenceSnapshot(
            total_streams=0, unsafe_streams=0,
        )


# ---------------------------------------------------------------------------
# Graduation predicate — first-match-wins (§33.1 canonical shape)
# ---------------------------------------------------------------------------


def is_ready_for_graduation(
    *,
    snapshot_reader: Optional[
        Callable[[], _ConfidenceSnapshot]
    ] = None,
) -> ToolConfidenceGraduationReport:
    """Evaluate the 3-gate cadence. NEVER raises.

    Caller-injection (``snapshot_reader``) enables deterministic
    testing without mutating the global observer."""
    if not is_harness_enabled():
        return ToolConfidenceGraduationReport(
            schema_version=(
                TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=ToolConfidenceGraduationVerdict.DISABLED,
            observed_streams=0,
            unsafe_streams=0,
            false_positive_ratio=0.0,
            detail="harness_master_off",
        )
    # Gate 1 — Slice 1 already graduated.
    try:
        from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
            master_enabled,
        )
        if master_enabled():
            return ToolConfidenceGraduationReport(
                schema_version=(
                    TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION
                ),
                verdict=(
                    ToolConfidenceGraduationVerdict
                    .ALREADY_GRADUATED
                ),
                observed_streams=0,
                unsafe_streams=0,
                false_positive_ratio=0.0,
                detail=(
                    "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED is "
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
        snapshot = _ConfidenceSnapshot(
            total_streams=0, unsafe_streams=0,
        )
    total = int(snapshot.total_streams)
    unsafe = int(snapshot.unsafe_streams)
    if total <= 0:
        ratio = 0.0
    else:
        ratio = float(unsafe) / float(total)
    if total < min_required_observations_knob():
        return ToolConfidenceGraduationReport(
            schema_version=(
                TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                ToolConfidenceGraduationVerdict
                .INSUFFICIENT_OBSERVATIONS
            ),
            observed_streams=total,
            unsafe_streams=unsafe,
            false_positive_ratio=ratio,
            detail=(
                f"observed={total} required="
                f"{min_required_observations_knob()}"
            ),
        )
    if ratio > max_false_positive_ratio_knob():
        return ToolConfidenceGraduationReport(
            schema_version=(
                TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                ToolConfidenceGraduationVerdict
                .EXCESSIVE_FALSE_POSITIVES
            ),
            observed_streams=total,
            unsafe_streams=unsafe,
            false_positive_ratio=ratio,
            detail=(
                f"unsafe_ratio={ratio:.3f} max="
                f"{max_false_positive_ratio_knob():.3f} — "
                f"thresholds may need tuning OR model is "
                f"systematically uncertain in this domain"
            ),
        )
    return ToolConfidenceGraduationReport(
        schema_version=(
            TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            ToolConfidenceGraduationVerdict.READY_FOR_GRADUATION
        ),
        observed_streams=total,
        unsafe_streams=unsafe,
        false_positive_ratio=ratio,
        detail=(
            f"observed={total} unsafe_ratio={ratio:.3f} — "
            f"empirical evidence sufficient; flip "
            f"JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED to "
            f"graduate"
        ),
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds — auto-discovered via register_flags()
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered by FlagRegistry. Seeds the 3 knobs this
    module reads. Defensive on registry-shape mismatch."""
    try:
        registry.register(
            name=(
                "JARVIS_TOOL_CONFIDENCE_INDICATOR_GRADUATION_"
                "CONTRACT_ENABLED"
            ),
            type_="bool",
            default="true",
            description=(
                "Master switch for the §37 Tier 2 #13 Slice 4 "
                "graduation contract harness. Default-TRUE per "
                "§33.1 separation-of-concerns; data flag "
                "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED stays "
                "default-FALSE."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_indicator_graduation_contract.py"
            ),
            example=(
                "JARVIS_TOOL_CONFIDENCE_INDICATOR_GRADUATION_"
                "CONTRACT_ENABLED=true"
            ),
        )
        registry.register(
            name=(
                "JARVIS_TOOL_CONFIDENCE_GRADUATION_MIN_"
                "OBSERVATIONS"
            ),
            type_="int",
            default="50",
            description=(
                "Min cumulative per-tool stream observations "
                "before READY_FOR_GRADUATION fires."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_indicator_graduation_contract.py"
            ),
            example=(
                "JARVIS_TOOL_CONFIDENCE_GRADUATION_MIN_"
                "OBSERVATIONS=80"
            ),
        )
        registry.register(
            name=(
                "JARVIS_TOOL_CONFIDENCE_GRADUATION_MAX_FP_RATIO"
            ),
            type_="float",
            default="0.40",
            description=(
                "Max acceptable false-positive ratio (unsafe-"
                "pole streams / total streams) before "
                "EXCESSIVE_FALSE_POSITIVES fires. Default 0.40."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_indicator_graduation_contract.py"
            ),
            example=(
                "JARVIS_TOOL_CONFIDENCE_GRADUATION_MAX_FP_RATIO=0.50"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ToolConfidenceGraduationContract] FlagRegistry "
            "seeding failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``tool_confidence_graduation_verdict_taxonomy_closed``
         — 5-value enum bytes-pinned.
      2. ``tool_confidence_graduation_authority_asymmetry`` —
         substrate purity (no orchestrator / iron_gate / etc.).
      3. ``tool_confidence_graduation_pattern_compliance`` —
         §33.1 canonical-shape parity (required top-level
         symbols present).
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
        "tool_confidence_indicator_graduation_contract.py"
    )

    _EXPECTED_VERDICTS = {
        "ready_for_graduation",
        "insufficient_observations",
        "excessive_false_positives",
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
                == "ToolConfidenceGraduationVerdict"
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
            "ToolConfidenceGraduationVerdict missing"
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
        """§33.1 canonical-shape parity — required top-level
        symbols present. Mirrors the pattern-compliance pin
        applied across every graduation contract (Venom V2,
        Move 7, Move 8)."""
        violations: list = []
        required_top_level = {
            "is_ready_for_graduation",
            "is_harness_enabled",
            "ToolConfidenceGraduationVerdict",
            "ToolConfidenceGraduationReport",
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
                "tool_confidence_graduation_verdict_taxonomy_"
                "closed"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 4 — 5-value verdict closed "
                "taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_graduation_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 4 — harness substrate "
                "purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_graduation_pattern_compliance"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 4 — §33.1 canonical-shape "
                "parity."
            ),
            validate=_validate_pattern_compliance,
        ),
    ]


__all__ = [
    "TOOL_CONFIDENCE_GRADUATION_REPORT_SCHEMA_VERSION",
    "ToolConfidenceGraduationReport",
    "ToolConfidenceGraduationVerdict",
    "is_harness_enabled",
    "is_ready_for_graduation",
    "max_false_positive_ratio_knob",
    "min_required_observations_knob",
    "register_flags",
    "register_shipped_invariants",
]
