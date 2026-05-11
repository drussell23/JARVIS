"""M10 ArchitectureProposer — graduation contract harness.

§33.1 canonical-shape contract gating the
``JARVIS_M10_ARCH_PROPOSER_ENABLED`` master flag flip on
operator-paced empirical evidence. Closes the §40.5 Wave 1 #4
commitment — the M10 substrate (Slices 1-5) shipped in v2.17 on
2026-05-04 but the §33.1 gating predicate the operator binding
§30.5.2 mandates was never wired ("flips only after a 30+ proposal-
acceptance audit").

Composition contract — thin composer over canonical M10 substrate,
zero parallel state, zero hardcoded phase strings:

* :class:`m10.primitives.M10ProposalPhase` — the canonical 16-value
  closed FSM. Terminal phases ``GRADUATED`` / ``FAILED`` /
  ``REJECTED`` / ``EXPIRED`` / ``PUSH_FAILED`` are referenced by
  enum value (no hardcoded literals — AST-pinned).
* :func:`m10.proposal_store.aggregate_phase_histogram` — canonical
  pure-read accessor that dedupes to most-recent-state-per-
  proposal_id. Single source of truth for the audit denominator.
* :func:`m10.primitives.m10_arch_proposer_enabled` — substrate
  master flag accessor. Reused for Gate 1 (already-graduated
  idempotency) — no parallel env read.

Graduation gates (5-gate first-match-wins, §33.1 canonical shape)::

  0. Harness master off  → DISABLED
  1. Substrate master    → ALREADY_GRADUATED
     ``JARVIS_M10_ARCH_PROPOSER_ENABLED=true``
  2. < ``min_required_acceptances`` GRADUATED proposals
                         → INSUFFICIENT_PROPOSALS
  3. rejection_ratio > ``max_rejection_ratio``
                         → EXCESSIVE_REJECTIONS
  4. all pass            → READY_FOR_GRADUATION

Default thresholds reflect the operator binding §30.5.2:

* ``min_required_acceptances = 30`` — verbatim from binding
* ``max_rejection_ratio = 0.50`` — safe ceiling; M10's surface area
  (PR-gated approval lifecycle, OrangePRReviewer) means high
  rejection ratio signals either over-aggressive cage OR over-
  ambitious proposer; either condition warrants operator review
  before flipping the default-FALSE switch

Both knobs are env-tunable via ``JARVIS_M10_GRADUATION_*`` — no
hardcoding. Bounds are defensively clamped.

§33.5 versioned artifact: :class:`M10GraduationReport` with
``schema_version`` + symmetric ``to_dict()`` projection.

Authority asymmetry (AST-pinned): the harness imports stdlib +
``m10.primitives`` + ``m10.proposal_store`` ONLY. It does NOT
import orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / graduation_orchestrator (the archived legacy
module — explicitly forbidden because the H1-H6 design lessons
were lifted via §32.4 verbatim into ``m10/primitives.py`` and we
MUST NOT re-couple to the archived code).

Pure substrate — NEVER raises. The harness is read-only by
construction; a malformed ledger row, missing canonical accessor,
or failed env lookup degrades to ``INSUFFICIENT_PROPOSALS``, not
exception.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


M10_GRADUATION_REPORT_SCHEMA_VERSION: str = (
    "m10_graduation_report.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ===========================================================================
# Master flag — harness opt-in (§33.1 separation-of-concerns)
# ===========================================================================


def is_harness_enabled() -> bool:
    """Master switch — ``JARVIS_M10_GRADUATION_CONTRACT_ENABLED``.

    Default-**TRUE** per §33.1 separation-of-concerns: the harness
    is a measurement surface, NOT the cognitive substrate. The
    data flag (``JARVIS_M10_ARCH_PROPOSER_ENABLED``) lives on the
    producer side (``m10/primitives.py``) and stays default-FALSE
    until this contract returns ``READY_FOR_GRADUATION`` AND the
    operator manually flips it per §30.5.2.
    """
    raw = os.environ.get(
        "JARVIS_M10_GRADUATION_CONTRACT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # default-TRUE per §33.1 separation
    return raw in _TRUTHY


# ===========================================================================
# Env knobs — operator-tunable thresholds (no hardcoding)
# ===========================================================================


_ENV_MIN_REQUIRED = (
    "JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES"
)
_ENV_MAX_REJECTION_RATIO = (
    "JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO"
)

# §30.5.2 operator binding — "30+ proposal-acceptance audit"
_DEFAULT_MIN_REQUIRED = 30
# Safe ceiling: above this the cage / proposer balance needs
# operator review before graduating the substrate.
_DEFAULT_MAX_REJECTION_RATIO = 0.50


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


def min_required_acceptances_knob() -> int:
    """Per §30.5.2 binding — minimum GRADUATED proposals required
    before READY_FOR_GRADUATION fires. Default 30. Operator
    override via ``JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES``;
    invalid / non-positive values fall back to default."""
    return _read_int_knob(_ENV_MIN_REQUIRED, _DEFAULT_MIN_REQUIRED)


def max_rejection_ratio_knob() -> float:
    """Maximum rejection ratio tolerated. Above this the
    proposer/cage balance is suspect — operator review required
    before flipping the substrate flag. Default 0.50; bounded
    [0.0, 1.0]. Operator override via
    ``JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO``."""
    v = _read_float_knob(
        _ENV_MAX_REJECTION_RATIO, _DEFAULT_MAX_REJECTION_RATIO,
    )
    if v > 1.0:
        return 1.0
    return v


# ===========================================================================
# Closed 5-value verdict taxonomy (§33.1 canonical shape)
# ===========================================================================


class M10GraduationVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST regression.

    Adding / removing a verdict requires updating the regression
    pin AND every consumer that switches on this taxonomy. The
    5-value structure mirrors the canonical exemplar shape from
    ``tool_permissions_graduation_contract.M10GraduationVerdict``
    et al. so future arcs inherit the cognitive load-out by
    convention.
    """

    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_PROPOSALS = "insufficient_proposals"
    EXCESSIVE_REJECTIONS = "excessive_rejections"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"


# ===========================================================================
# Versioned report artifact (§33.5)
# ===========================================================================


@dataclass(frozen=True)
class M10GraduationReport:
    """Frozen graduation report — §33.5 versioned artifact.

    Carries the audit numerator/denominator + verdict + detail
    string so a downstream consumer (REPL renderer / SSE payload /
    operator decision) can render the exact graduation arithmetic
    without recomputing from the ledger. ``to_dict`` projection
    is the canonical observable shape.
    """

    schema_version: str
    verdict: M10GraduationVerdict
    observed_accepted: int          # GRADUATED count
    observed_rejected: int          # FAILED + REJECTED + EXPIRED + PUSH_FAILED
    rejection_ratio: float          # rejected / (accepted + rejected)
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "observed_accepted": int(self.observed_accepted),
            "observed_rejected": int(self.observed_rejected),
            "rejection_ratio": float(self.rejection_ratio),
            "detail": self.detail[:256],
        }


# ===========================================================================
# Evidence aggregator — composes canonical M10 proposal store
# ===========================================================================


@dataclass(frozen=True)
class _AcceptanceSnapshot:
    """Internal projection — totals observed across the canonical
    M10 proposal ledger. Built by composing
    ``aggregate_phase_histogram`` (the canonical pure-read
    accessor which dedupes to most-recent-state-per-proposal_id).
    """
    accepted: int
    rejected: int


def _canonical_terminal_phase_values() -> (
    "tuple[set[str], set[str]]"
):
    """Return (accept_phase_values, reject_phase_values) by
    composing the canonical :class:`M10ProposalPhase` enum.
    NEVER raises — returns empty sets if substrate unavailable.

    No hardcoded phase strings — operator binding "no hardcoding"
    enforced structurally: adding a new terminal phase to the
    canonical enum requires this function to be updated, and the
    AST pin asserts both the import and the enum reference.
    """
    try:
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
    except ImportError:
        return (set(), set())
    accept = {M10ProposalPhase.GRADUATED.value}
    reject = {
        M10ProposalPhase.FAILED.value,
        M10ProposalPhase.REJECTED.value,
        M10ProposalPhase.EXPIRED.value,
        M10ProposalPhase.PUSH_FAILED.value,
    }
    return (accept, reject)


def _collect_evidence_default() -> _AcceptanceSnapshot:
    """Default evidence collector — composes the canonical
    ``m10.proposal_store.aggregate_phase_histogram`` reader, which
    in turn composes the JSONL ledger via ``flock_critical_section``
    + dedupes to most-recent-state-per-proposal_id.

    Single source of truth for the audit; NEVER raises. Substrate
    unavailable / empty ledger → ``_AcceptanceSnapshot(0, 0)``.
    """
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
            aggregate_phase_histogram,
        )
    except ImportError:
        return _AcceptanceSnapshot(accepted=0, rejected=0)
    try:
        histogram = aggregate_phase_histogram()
    except Exception:  # noqa: BLE001 — defensive
        return _AcceptanceSnapshot(accepted=0, rejected=0)
    if not isinstance(histogram, dict):
        return _AcceptanceSnapshot(accepted=0, rejected=0)
    accept_phases, reject_phases = (
        _canonical_terminal_phase_values()
    )
    accepted = 0
    rejected = 0
    for phase, count in histogram.items():
        try:
            c = int(count)
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        if phase in accept_phases:
            accepted += c
        elif phase in reject_phases:
            rejected += c
        # phases outside the terminal set are ignored — they
        # represent in-flight proposals not yet decided.
    return _AcceptanceSnapshot(
        accepted=accepted, rejected=rejected,
    )


# ===========================================================================
# Graduation predicate — 5-gate first-match-wins (§33.1 canonical shape)
# ===========================================================================


def is_ready_for_graduation(
    *,
    snapshot_reader: Optional[
        Callable[[], _AcceptanceSnapshot]
    ] = None,
) -> M10GraduationReport:
    """Evaluate the §33.1 5-gate cadence. NEVER raises.

    ``snapshot_reader`` is caller-injectable (testing seam). When
    omitted the canonical default reader composes
    :func:`aggregate_phase_histogram`.
    """
    # Gate 0 — harness master off.
    if not is_harness_enabled():
        return M10GraduationReport(
            schema_version=M10_GRADUATION_REPORT_SCHEMA_VERSION,
            verdict=M10GraduationVerdict.DISABLED,
            observed_accepted=0,
            observed_rejected=0,
            rejection_ratio=0.0,
            detail="harness_master_off",
        )

    # Gate 1 — substrate ALREADY graduated (idempotent no-op).
    try:
        from backend.core.ouroboros.governance.m10.primitives import (
            m10_arch_proposer_enabled,
        )
        if m10_arch_proposer_enabled():
            return M10GraduationReport(
                schema_version=(
                    M10_GRADUATION_REPORT_SCHEMA_VERSION
                ),
                verdict=M10GraduationVerdict.ALREADY_GRADUATED,
                observed_accepted=0,
                observed_rejected=0,
                rejection_ratio=0.0,
                detail=(
                    "JARVIS_M10_ARCH_PROPOSER_ENABLED is on — "
                    "substrate already flipped; contract is "
                    "now an idempotent no-op"
                ),
            )
    except ImportError:
        # Substrate unavailable — continue. Gates 2-3 will
        # return INSUFFICIENT_PROPOSALS via empty snapshot.
        pass

    # Gates 2-3 — evaluate evidence.
    if snapshot_reader is None:
        snapshot_reader = _collect_evidence_default
    try:
        snapshot = snapshot_reader()
    except Exception:  # noqa: BLE001 — defensive
        snapshot = _AcceptanceSnapshot(accepted=0, rejected=0)

    accepted = int(snapshot.accepted)
    rejected = int(snapshot.rejected)
    total_terminal = accepted + rejected
    if total_terminal <= 0:
        ratio = 0.0
    else:
        ratio = float(rejected) / float(total_terminal)

    # Gate 2 — insufficient acceptances (§30.5.2 binding).
    min_required = min_required_acceptances_knob()
    if accepted < min_required:
        return M10GraduationReport(
            schema_version=M10_GRADUATION_REPORT_SCHEMA_VERSION,
            verdict=M10GraduationVerdict.INSUFFICIENT_PROPOSALS,
            observed_accepted=accepted,
            observed_rejected=rejected,
            rejection_ratio=ratio,
            detail=(
                f"observed_accepted={accepted} "
                f"required={min_required} — operator binding "
                "§30.5.2 mandates ≥30 proposal-acceptance "
                "audit before flipping the substrate flag"
            ),
        )

    # Gate 3 — rejection ratio too high.
    max_ratio = max_rejection_ratio_knob()
    if ratio > max_ratio:
        return M10GraduationReport(
            schema_version=M10_GRADUATION_REPORT_SCHEMA_VERSION,
            verdict=M10GraduationVerdict.EXCESSIVE_REJECTIONS,
            observed_accepted=accepted,
            observed_rejected=rejected,
            rejection_ratio=ratio,
            detail=(
                f"rejection_ratio={ratio:.3f} max={max_ratio:.3f}"
                f" — proposer/cage balance suspect; operator "
                f"review required before graduation"
            ),
        )

    # Gate 4 — all gates pass; flip is empirically justified.
    return M10GraduationReport(
        schema_version=M10_GRADUATION_REPORT_SCHEMA_VERSION,
        verdict=M10GraduationVerdict.READY_FOR_GRADUATION,
        observed_accepted=accepted,
        observed_rejected=rejected,
        rejection_ratio=ratio,
        detail=(
            f"observed_accepted={accepted} "
            f"rejection_ratio={ratio:.3f} — empirical evidence "
            f"sufficient per §30.5.2; flip "
            f"JARVIS_M10_ARCH_PROPOSER_ENABLED=true to graduate"
        ),
    )


# ===========================================================================
# AST pins via shipped_code_invariants (auto-discovered via §33.3)
# ===========================================================================


def register_shipped_invariants() -> list:
    """Return AST invariant pins for this module. Auto-discovered by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Pins:

      1. ``m10_graduation_verdict_taxonomy_closed`` — 5-value
         closed enum bytes-pinned.
      2. ``m10_graduation_authority_asymmetry`` — substrate
         purity (no orchestrator/iron_gate/policy/providers/
         candidate_generator/urgency_router/change_engine/
         semantic_guardian/graduation_orchestrator imports).
      3. ``m10_graduation_pattern_compliance`` — §33.1
         canonical-shape symbols present.
      4. ``m10_graduation_composes_canonical_store`` — must
         compose ``aggregate_phase_histogram`` from the
         canonical proposal_store + ``M10ProposalPhase`` from
         canonical primitives (no hardcoded phase strings, no
         parallel ledger reader).
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
        "m10_arch_proposer_graduation_contract.py"
    )

    _EXPECTED_VERDICTS = {
        "ready_for_graduation",
        "insufficient_proposals",
        "excessive_rejections",
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
                and node.name == "M10GraduationVerdict"
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
                        f"verdict missing: {sorted(missing)}",
                    )
                if extra:
                    violations.append(
                        f"verdict drift: {sorted(extra)}",
                    )
                return tuple(violations)
        violations.append("M10GraduationVerdict missing")
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        # Explicitly include graduation_orchestrator — the archived
        # legacy module from which §32.4 lifted design verbatim.
        # The new contract MUST NOT re-couple to it.
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
            "graduation_orchestrator",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        # Whitelist the m10 sibling modules
                        # (m10.primitives + m10.proposal_store).
                        # These are NOT in the forbidden list,
                        # but defensive check below confirms.
                        if (
                            "m10.primitives" in module
                            or "m10.proposal_store" in module
                        ):
                            continue
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
            "M10GraduationVerdict",
            "M10GraduationReport",
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
                f"{sorted(missing)}",
            )
        return tuple(violations)

    def _validate_composes_canonical_store(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        # Must reference the canonical M10ProposalPhase enum +
        # aggregate_phase_histogram reader. Drift to hardcoded
        # phase strings or a parallel reader would silently
        # diverge from the substrate's source of truth.
        if "M10ProposalPhase" not in source:
            violations.append(
                "must reference canonical "
                "M10ProposalPhase enum (no hardcoded phase "
                "literals)",
            )
        if "aggregate_phase_histogram" not in source:
            violations.append(
                "must compose canonical "
                "aggregate_phase_histogram (no parallel "
                "ledger reader)",
            )
        if "m10_arch_proposer_enabled" not in source:
            violations.append(
                "must compose canonical "
                "m10_arch_proposer_enabled accessor for "
                "Gate 1 (no parallel env read)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "m10_graduation_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "M10 contract — 5-value verdict closed "
                "taxonomy bytes-pinned. Adding/removing a "
                "verdict requires updating this pin AND every "
                "downstream consumer."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "m10_graduation_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "M10 contract — substrate purity. Harness "
                "MUST NOT import orchestrator/iron_gate/"
                "policy/providers/candidate_generator/"
                "urgency_router/change_engine/"
                "semantic_guardian. Explicitly forbids "
                "graduation_orchestrator (archived legacy "
                "module from which §32.4 lifted design only)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "m10_graduation_pattern_compliance"
            ),
            target_file=target,
            description=(
                "M10 contract — §33.1 canonical-shape parity. "
                "Pins the 4 required top-level symbols so "
                "future arcs can inherit the cognitive "
                "load-out by convention."
            ),
            validate=_validate_pattern_compliance,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "m10_graduation_composes_canonical_store"
            ),
            target_file=target,
            description=(
                "M10 contract composes canonical "
                "M10ProposalPhase enum + "
                "aggregate_phase_histogram reader + "
                "m10_arch_proposer_enabled master accessor "
                "— no hardcoded phase literals, no parallel "
                "ledger reader, no parallel env read."
            ),
            validate=_validate_composes_canonical_store,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds (auto-discovered via §33.3 naming-cage)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Register this contract's env knobs into FlagRegistry.

    Auto-discovered zero-edit by
    ``flag_registry_seed._discover_module_provided_flags``.
    """
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "m10_arch_proposer_graduation_contract.py"
    )

    seeds = [
        FlagSpec(
            name="JARVIS_M10_GRADUATION_CONTRACT_ENABLED",
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master switch for the M10 graduation "
                "contract harness — §33.1 separation. "
                "Default TRUE (measurement surface, not "
                "substrate). The cognitive flag "
                "JARVIS_M10_ARCH_PROPOSER_ENABLED stays "
                "default-FALSE on the producer side until "
                "this contract returns READY_FOR_GRADUATION "
                "AND the operator flips it per §30.5.2."
            ),
            category=Category.OBSERVABILITY,
            source_file=src,
            example=(
                "JARVIS_M10_GRADUATION_CONTRACT_ENABLED=false"
            ),
        ),
        FlagSpec(
            name=_ENV_MIN_REQUIRED,
            type=FlagType.INT,
            default=_DEFAULT_MIN_REQUIRED,
            description=(
                "Per §30.5.2 binding — minimum GRADUATED "
                "proposals required before "
                "READY_FOR_GRADUATION. Defaults to 30. "
                "Invalid / non-positive values fall back to "
                "default."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MIN_REQUIRED}=50",
        ),
        FlagSpec(
            name=_ENV_MAX_REJECTION_RATIO,
            type=FlagType.FLOAT,
            default=_DEFAULT_MAX_REJECTION_RATIO,
            description=(
                "Maximum rejection ratio tolerated before "
                "EXCESSIVE_REJECTIONS fires. Default 0.50; "
                "bounded [0.0, 1.0]. Above the threshold the "
                "proposer/cage balance is suspect — operator "
                "review required before graduation."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MAX_REJECTION_RATIO}=0.30",
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
    "M10_GRADUATION_REPORT_SCHEMA_VERSION",
    "M10GraduationReport",
    "M10GraduationVerdict",
    "is_harness_enabled",
    "is_ready_for_graduation",
    "min_required_acceptances_knob",
    "max_rejection_ratio_knob",
    "register_flags",
    "register_shipped_invariants",
]
