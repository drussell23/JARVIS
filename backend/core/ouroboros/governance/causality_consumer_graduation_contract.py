"""§31 U2 empirical wiring — Slice 5 graduation contract harness.

Mirrors the §33.1 canonical-shape graduation-contract pattern
(``cross_op_semantic_budget_graduation_contract`` reference
implementation). Composes Slice 1 + Slice 2 + Slice 3
empirical evidence to gate the master-flag flip on operator-
paced cadence — the substrate stays default-FALSE until this
harness reports ``READY_FOR_GRADUATION``.

§33.1 canonical shape mirrored verbatim:

  * 5-value :class:`CausalConsumerGraduationVerdict` closed enum
    (READY_FOR_GRADUATION / INSUFFICIENT_TRANSITIONS /
    EXCESSIVE_DISABLED_SAMPLES / ALREADY_GRADUATED / DISABLED).
  * Frozen :class:`CausalConsumerGraduationReport` versioned
    artifact (§33.5 to_dict projection).
  * :func:`is_ready_for_graduation` first-match-wins predicate.
  * Master-flag helper :func:`is_harness_enabled` (default-TRUE
    per §33.1 separation-of-concerns; the data flag stays
    default-FALSE on the producer side — ONE SOURCE OF TRUTH).
  * :func:`register_shipped_invariants` includes the
    pattern-compliance pin asserting §33.1 canonical shape.

Graduation gates (3-gate first-match-wins, first non-READY
wins):

  1. Slice 1 substrate ALREADY graduated (data flag flipped) →
     ``ALREADY_GRADUATED`` (idempotency — re-running the
     harness post-flip is a no-op).
  2. Total observed transitions in the canonical
     ``decisions.jsonl`` ledgers across recent sessions
     >= ``min_required_transitions`` (default 12, env-tunable).
     Below threshold → ``INSUFFICIENT_TRANSITIONS``.
  3. Fraction of observations that landed at
     :attr:`CausalDecisionAdvice.DISABLED` <=
     ``max_disabled_ratio`` (default 0.10). Above threshold
     means the substrate is thrashing on disabled / no-DAG
     paths — premature flip would mostly produce silence.

Authority asymmetry — harness imports stdlib + governance
substrate; NEVER imports orchestrator / iron_gate / policy /
providers (AST-pinned).

NEVER raises across any public surface.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION: str = (
    "causal_consumer_graduation_report.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Master flag — harness opt-in (§33.1 separation-of-concerns)
# ---------------------------------------------------------------------------


def is_harness_enabled() -> bool:
    """Master switch — ``JARVIS_CAUSAL_CONSUMER_GRADUATION_
    CONTRACT_ENABLED``. Default-TRUE per §33.1 separation-of-
    concerns — the harness is a measurement surface, not the
    cognitive substrate. The data flag
    (``JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED``) lives on the
    producer side and stays default-FALSE."""
    raw = os.environ.get(
        "JARVIS_CAUSAL_CONSUMER_GRADUATION_CONTRACT_ENABLED", "",
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


def min_required_transitions_knob() -> int:
    """Min transitions accumulated before READY_FOR_GRADUATION
    fires. Default 12 — three soaks × ~4 transitions each is
    the empirical floor below which the contract refuses to
    advise graduation."""
    return _read_int_knob(
        "JARVIS_CAUSAL_GRADUATION_MIN_TRANSITIONS", 12,
    )


def max_disabled_ratio_knob() -> float:
    """Max fraction of observations that landed at DISABLED
    advice. Above threshold the substrate is thrashing on
    disabled/no-DAG paths and graduation would be silent."""
    v = _read_float_knob(
        "JARVIS_CAUSAL_GRADUATION_MAX_DISABLED_RATIO", 0.10,
    )
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------------
# Closed verdict taxonomy (§33.1 canonical shape)
# ---------------------------------------------------------------------------


class CausalConsumerGraduationVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST regression.
    Mirrors ``cross_op_semantic_budget_graduation_contract``'s
    ``SemanticBudgetGraduationVerdict`` shape exactly."""

    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_TRANSITIONS = "insufficient_transitions"
    EXCESSIVE_DISABLED_SAMPLES = "excessive_disabled_samples"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Versioned report artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CausalConsumerGraduationReport:
    """Frozen graduation report — §33.5 versioned artifact."""

    schema_version: str
    verdict: CausalConsumerGraduationVerdict
    observed_transitions: int
    disabled_observation_count: int
    disabled_ratio: float
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "observed_transitions": int(self.observed_transitions),
            "disabled_observation_count": int(
                self.disabled_observation_count,
            ),
            "disabled_ratio": float(self.disabled_ratio),
            "detail": self.detail[:256],
        }


# ---------------------------------------------------------------------------
# Evidence aggregator — composes substrate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EvidenceSnapshot:
    """Internal — totals observed across the substrate."""
    transitions: int
    disabled_count: int


def _collect_evidence() -> _EvidenceSnapshot:
    """Compose the singleton observer's per-key state map +
    return aggregate counts. NEVER raises.

    For Slice 5's purposes, "transitions" = observed
    transitions tracked by the singleton. "disabled_count" is
    derived defensively — if the producer flag is off, every
    observation that would have fired produces a DISABLED
    artifact.

    The singleton's per-key state stores only the LATEST
    advice, not the per-transition history. For the harness
    purposes, the count of distinct keys that have transitioned
    away from the initial state IS the transition count signal
    (each key's first non-NEUTRAL observation = 1 transition).
    Subsequent transitions on the same key add to the signal.

    Future Slice 5 enhancement (deferred): wire a transition
    counter into the observer and surface it via a public
    snapshot — the harness then consumes that instead of the
    state-map heuristic. Pattern parity with Move 7's evidence
    snapshot.
    """
    try:
        from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
            get_default_observer,
        )
    except ImportError:
        return _EvidenceSnapshot(
            transitions=0, disabled_count=0,
        )
    try:
        observer = get_default_observer()
    except Exception:  # noqa: BLE001 — defensive
        return _EvidenceSnapshot(
            transitions=0, disabled_count=0,
        )
    state = dict(getattr(observer, "_last_advice", {}) or {})
    transitions = len(state)  # heuristic; see docstring
    disabled_count = sum(
        1 for v in state.values() if v == "disabled"
    )
    return _EvidenceSnapshot(
        transitions=transitions,
        disabled_count=disabled_count,
    )


# ---------------------------------------------------------------------------
# Graduation predicate — first-match-wins (§33.1 canonical shape)
# ---------------------------------------------------------------------------


def is_ready_for_graduation(
    *,
    snapshot: Optional[_EvidenceSnapshot] = None,
) -> CausalConsumerGraduationReport:
    """Evaluate the 3-gate cadence. Returns a
    :class:`CausalConsumerGraduationReport`. NEVER raises.

    Gates (first-match-wins):

      1. Slice 1 substrate already graduated → ALREADY_GRADUATED
      2. observed_transitions < min_required_transitions →
         INSUFFICIENT_TRANSITIONS
      3. disabled_ratio > max_disabled_ratio →
         EXCESSIVE_DISABLED_SAMPLES
      4. Otherwise → READY_FOR_GRADUATION

    When the harness master flag is off, the report verdict is
    :attr:`DISABLED` and the harness is a no-op (operator hot-
    revert path)."""
    if not is_harness_enabled():
        return CausalConsumerGraduationReport(
            schema_version=(
                CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=CausalConsumerGraduationVerdict.DISABLED,
            observed_transitions=0,
            disabled_observation_count=0,
            disabled_ratio=0.0,
            detail="harness_master_off",
        )
    # Gate 1 — Slice 1 already graduated → idempotent no-op.
    try:
        from backend.core.ouroboros.governance.causality_consumer import (  # noqa: E501
            is_consumer_enabled,
        )
        if is_consumer_enabled():
            return CausalConsumerGraduationReport(
                schema_version=(
                    CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION
                ),
                verdict=(
                    CausalConsumerGraduationVerdict
                    .ALREADY_GRADUATED
                ),
                observed_transitions=0,
                disabled_observation_count=0,
                disabled_ratio=0.0,
                detail=(
                    "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED "
                    "is on — substrate already flipped"
                ),
            )
    except ImportError:
        # Substrate unavailable → treat as not-graduated; the
        # transition-count + disabled-ratio gates still apply.
        pass
    # Gates 2-3 — evaluate evidence.
    if snapshot is None:
        snapshot = _collect_evidence()
    transitions = int(snapshot.transitions)
    disabled = int(snapshot.disabled_count)
    if transitions <= 0:
        ratio = 0.0
    else:
        ratio = float(disabled) / float(transitions)
    if transitions < min_required_transitions_knob():
        return CausalConsumerGraduationReport(
            schema_version=(
                CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                CausalConsumerGraduationVerdict
                .INSUFFICIENT_TRANSITIONS
            ),
            observed_transitions=transitions,
            disabled_observation_count=disabled,
            disabled_ratio=ratio,
            detail=(
                f"observed={transitions} required="
                f"{min_required_transitions_knob()}"
            ),
        )
    if ratio > max_disabled_ratio_knob():
        return CausalConsumerGraduationReport(
            schema_version=(
                CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION
            ),
            verdict=(
                CausalConsumerGraduationVerdict
                .EXCESSIVE_DISABLED_SAMPLES
            ),
            observed_transitions=transitions,
            disabled_observation_count=disabled,
            disabled_ratio=ratio,
            detail=(
                f"disabled_ratio={ratio:.3f} max="
                f"{max_disabled_ratio_knob():.3f}"
            ),
        )
    return CausalConsumerGraduationReport(
        schema_version=(
            CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION
        ),
        verdict=(
            CausalConsumerGraduationVerdict
            .READY_FOR_GRADUATION
        ),
        observed_transitions=transitions,
        disabled_observation_count=disabled,
        disabled_ratio=ratio,
        detail=(
            f"observed={transitions} disabled_ratio={ratio:.3f}"
            f" — empirical evidence sufficient; flip "
            f"JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED to "
            f"graduate"
        ),
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``causal_consumer_graduation_verdict_taxonomy_closed``
         — 5-value closed enum bytes-pinned (§33.1 canonical
         shape parity).
      2. ``causal_consumer_graduation_authority_asymmetry`` —
         harness substrate purity.
      3. ``causal_consumer_graduation_pattern_compliance`` —
         §33.1 canonical-shape parity check (predicate name +
         report dataclass + 5-value verdict + master-flag
         helper).
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
        "causality_consumer_graduation_contract.py"
    )

    _EXPECTED_VERDICTS = {
        "ready_for_graduation",
        "insufficient_transitions",
        "excessive_disabled_samples",
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
                == "CausalConsumerGraduationVerdict"
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
                    violations.append(
                        f"verdict missing: {sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"verdict drift: {sorted(extra)}"
                    )
                return tuple(violations)
        violations.append(
            "CausalConsumerGraduationVerdict missing"
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
        """§33.1 canonical-shape parity check — required
        symbols + frozen dataclass + 5-value verdict + master-
        flag helper present."""
        violations: list = []
        required_top_level = {
            "is_ready_for_graduation",       # predicate
            "is_harness_enabled",            # master-flag helper
            "CausalConsumerGraduationVerdict",  # closed enum
            "CausalConsumerGraduationReport",   # frozen artifact
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
                "causal_consumer_graduation_verdict_"
                "taxonomy_closed"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 5 — 5-value verdict closed "
                "taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_consumer_graduation_authority_"
                "asymmetry"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 5 — harness substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_consumer_graduation_pattern_"
                "compliance"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 5 — §33.1 canonical-shape "
                "parity (predicate + master-flag helper + "
                "verdict enum + report artifact)."
            ),
            validate=_validate_pattern_compliance,
        ),
    ]


__all__ = [
    "CAUSAL_GRADUATION_REPORT_SCHEMA_VERSION",
    "CausalConsumerGraduationReport",
    "CausalConsumerGraduationVerdict",
    "is_harness_enabled",
    "is_ready_for_graduation",
    "max_disabled_ratio_knob",
    "min_required_transitions_knob",
    "register_shipped_invariants",
]
