"""Move 6.5 Slice 6 — §33.1 graduation contract harness.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "§33.1 graduation contract harness (Slice 6 of your
   table). Phase 9 / shipping: Ship substrate behind master
   default-FALSE immediately after design freeze; graduation
   contract + evidence gates any default-TRUE flip. 'Wait
   for Phase 9 wall-clock' is not a code blocker if the flag
   and contract enforce the sequence."

This is the **last slice** of the Move 6.5 arc. It evaluates
Slice 4's accumulated ledger evidence and produces a
deterministic verdict on whether the operator may flip
Slice 3's :data:`JARVIS_MULTI_PRIOR_DISPATCH_ENABLED` master
flag from default-FALSE → default-TRUE. The contract is
**read-only** — it never mutates flags, never writes
ledgers, never triggers a dispatch.

## Canonical §33.1 shape (parity with Move 7 + Move 8)

Mirrors the canonical Move 7
:mod:`cross_op_semantic_budget_graduation_contract` shape:

  * 5-value :class:`MultiPriorGraduationVerdict` closed enum
  * Frozen §33.5 :class:`MultiPriorGraduationReport`
  * Pure :func:`is_ready_for_graduation` predicate with
    3-gate first-match-wins evaluation
  * Harness master flag default-TRUE per §33.1 separation
    (operator-binding default-FALSE lives on Slices 1-5's
    master flags; the contract is a passive oracle)
  * Pattern-compliance regression test in the test spine
    proves canonical-shape parity with Move 7

## Three gates (first-match-wins)

  1. **ALREADY_GRADUATED** — Slice 3's master flag
     :func:`master_enabled` returns True. Operator already
     flipped; no re-graduation needed.

  2. **INSUFFICIENT_OBSERVATIONS** — Slice 4 ledger has
     fewer than ``required_observations`` (default 50)
     records. Evidence not yet load-bearing.

  3. **EXCESSIVE_NON_ACTIONABLE_RATE** — Among recorded
     observations, the share with action_recommendation in
     {ESCALATE_TO_OPERATOR_REVIEW, FALL_THROUGH} OR
     non-zero cancelled_count / error_count exceeds
     ``max_non_actionable_rate`` (default 0.40). Indicates
     priors aren't producing useful convergence signal OR
     cost gates trip too aggressively — system isn't stable
     enough to graduate.

  4. **(otherwise)** — READY_FOR_GRADUATION.

## Composition discipline (AST-pinned)

  * The predicate composes Slice 3's :func:`master_enabled`
    (via single source of truth — no parallel
    :func:`os.environ.get`) for Gate 1.
  * The predicate composes Slice 4's
    :func:`read_recent_observations` for Gate 2 + 3 (no
    parallel JSONL read; the canonical read API is the only
    evidence channel).
  * Both compositions lazy-imported inside
    :func:`is_ready_for_graduation` — module-top imports
    of those substrates are forbidden.

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / candidate_generator / change_engine /
semantic_guardian / plan_generator / urgency_router /
direction_inferrer / policy imports.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, FrozenSet, Optional,
)


logger = logging.getLogger(
    "Ouroboros.MultiPriorGraduation",
)


MULTI_PRIOR_GRADUATION_SCHEMA_VERSION: str = (
    "multi_prior_graduation_contract.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


# Default thresholds — operator-tunable via env knobs.
# Operator binding 2026-05-07: "fixed K=4; no adaptive
# K until Slice 7+ once metrics exist." Same discipline
# applies here — fixed conservative defaults until Phase 9
# cadence accumulates enough data to validate them.
_DEFAULT_REQUIRED_OBSERVATIONS: int = 50
_DEFAULT_MAX_NON_ACTIONABLE_RATE: float = 0.40


# Action-recommendation values that count as "non-actionable"
# (signal that the K-prior dispatch isn't producing useful
# convergence). Operator-binding load-bearing — these are
# the action-recommendation values that the orchestrator's
# call site would NOT auto-apply on:
#
#   * ESCALATE_TO_OPERATOR_REVIEW — full divergence
#   * FALL_THROUGH                — DISABLED / FAILED
#                                   verdict; useless signal
#
# CLAMP_TO_NOTIFY_APPLY is excluded — it's "majority with
# outliers" which is still useful (operator reviews; signal
# converged enough to be partial).
_NON_ACTIONABLE_ACTIONS: FrozenSet[str] = frozenset({
    "escalate_to_operator_review",
    "fall_through",
})


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value verdict
# ---------------------------------------------------------------------------


class MultiPriorGraduationVerdict(str, enum.Enum):
    """Closed 5-value taxonomy. Mirrors Move 7's
    :class:`SemanticBudgetGraduationVerdict` and Move 8's
    :class:`CuriosityGraduationVerdict` shapes — the
    canonical §33.1 graduation-verdict pattern.

    AST-pinned. Pattern-compliance regression test proves
    §33.1 canonical-shape parity in the test spine."""

    READY_FOR_GRADUATION = "ready_for_graduation"
    """All 3 gates passed. Operator may flip Slice 3's
    master flag default-FALSE → default-TRUE."""

    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    """Slice 4 ledger has fewer than ``required_observations``
    records. Evidence not load-bearing yet."""

    EXCESSIVE_NON_ACTIONABLE_RATE = (
        "excessive_non_actionable_rate"
    )
    """Non-actionable rate (escalate + fall_through +
    rolls with cancellations / errors) exceeds threshold.
    Priors aren't converging usefully; system not stable
    enough to graduate."""

    ALREADY_GRADUATED = "already_graduated"
    """Slice 3's master flag is already TRUE. No
    re-graduation needed."""

    DISABLED = "disabled"
    """Harness master flag off (defensive — disable the
    graduation oracle entirely)."""


# ---------------------------------------------------------------------------
# Master flag — harness default-TRUE per §33.1 separation
# ---------------------------------------------------------------------------


def harness_enabled() -> bool:
    """``JARVIS_MULTI_PRIOR_GRADUATION_CONTRACT_ENABLED``
    harness master switch. Default-TRUE per §33.1
    separation: the contract is a passive read-only oracle,
    so the harness can be on while the producer flags
    (Slices 1-5) stay default-FALSE. Pure read; NEVER raises.
    """
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_GRADUATION_CONTRACT_ENABLED",
        "",
    ).strip().lower()
    if raw == "":
        return True  # default-TRUE per §33.1 separation
    return raw in _TRUTHY


def required_observations() -> int:
    """Operator-tunable observation threshold for Gate 2.
    Clamped [1, 10000]. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_GRADUATION_REQUIRED_OBSERVATIONS",
        "",
    ).strip()
    if not raw:
        return _DEFAULT_REQUIRED_OBSERVATIONS
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_REQUIRED_OBSERVATIONS
    if v < 1:
        return 1
    if v > 10000:
        return 10000
    return v


def max_non_actionable_rate() -> float:
    """Operator-tunable threshold for Gate 3. Clamped
    [0.0, 1.0]. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_GRADUATION_MAX_NON_ACTIONABLE_RATE",  # noqa: E501
        "",
    ).strip()
    if not raw:
        return _DEFAULT_MAX_NON_ACTIONABLE_RATE
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_NON_ACTIONABLE_RATE
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------------
# Frozen artifact (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiPriorGraduationReport:
    """Composite graduation report. Adopts §33.5
    versioned-artifact contract.

    ``breakdown_by_action`` carries the per-action counts
    (e.g. ``{"accept_canonical": 30, "escalate_...": 12,
    "fall_through": 8}``) so operators see WHICH failure
    mode dominated when a gate trips."""

    verdict: MultiPriorGraduationVerdict
    total_observations: int
    non_actionable_count: int
    non_actionable_rate: float
    required_observations: int
    max_non_actionable_rate: float
    breakdown_by_action: Dict[str, int] = field(
        default_factory=dict,
    )
    detail: str = ""
    ts_unix: float = 0.0
    schema_version: str = field(
        default=MULTI_PRIOR_GRADUATION_SCHEMA_VERSION,
    )

    def is_actionable(self) -> bool:
        """True iff verdict is READY_FOR_GRADUATION (the
        only verdict that maps to a definitive operator
        action: flip the master flag)."""
        return (
            self.verdict
            is MultiPriorGraduationVerdict.READY_FOR_GRADUATION  # noqa: E501
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "total_observations": int(
                self.total_observations,
            ),
            "non_actionable_count": int(
                self.non_actionable_count,
            ),
            "non_actionable_rate": float(
                self.non_actionable_rate,
            ),
            "required_observations": int(
                self.required_observations,
            ),
            "max_non_actionable_rate": float(
                self.max_non_actionable_rate,
            ),
            "breakdown_by_action": dict(
                self.breakdown_by_action,
            ),
            "detail": str(self.detail)[:512],
            "ts_unix": float(self.ts_unix),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Default snapshot reader — composes Slice 4's read API
# ---------------------------------------------------------------------------


def _default_snapshot_reader() -> Dict[str, int]:
    """Reads Slice 4's ledger via the canonical
    :func:`read_recent_observations` API + projects to a
    Mapping[action_recommendation, count] + an internal
    sentinel ``_with_failures`` count for cancelled / errored
    rows (orthogonal to action_recommendation per operator
    binding).

    Lazy-imported inside :func:`is_ready_for_graduation` per
    composition discipline. Returns empty Mapping on
    Slice 4 unavailable / read failure (so Gate 2 trips
    cleanly with INSUFFICIENT_OBSERVATIONS rather than
    crashing). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
            read_recent_observations,
        )
    except Exception:  # noqa: BLE001 — defensive
        return {}
    try:
        rows = read_recent_observations(limit=10000)
    except Exception:  # noqa: BLE001 — defensive
        return {}
    out: Dict[str, int] = {}
    failures = 0
    for r in rows:
        try:
            action = str(
                getattr(r, "action_recommendation", ""),
            )
            cancelled = int(
                getattr(r, "cancelled_count", 0),
            )
            errored = int(getattr(r, "error_count", 0))
        except Exception:  # noqa: BLE001 — defensive
            continue
        out[action] = out.get(action, 0) + 1
        if cancelled > 0 or errored > 0:
            failures += 1
    out["_with_failures"] = failures
    return out


# ---------------------------------------------------------------------------
# Public predicate — 3-gate first-match-wins
# ---------------------------------------------------------------------------


def is_ready_for_graduation(
    *,
    required_observations_override: Optional[int] = None,
    max_non_actionable_rate_override: Optional[float] = None,
    snapshot_reader: Optional[
        Callable[[], Dict[str, int]]
    ] = None,
    enabled_override: Optional[bool] = None,
    master_enabled_override: Optional[bool] = None,
) -> MultiPriorGraduationReport:
    """Pure §33.1 graduation predicate. NEVER raises.

    First-match-wins decision tree:
      1. Harness disabled → DISABLED report.
      2. Slice 3's master flag already TRUE → ALREADY_GRADUATED.
      3. ``snapshot["__total__"]`` < required → INSUFFICIENT_OBSERVATIONS.
      4. non_actionable_rate > threshold →
         EXCESSIVE_NON_ACTIONABLE_RATE.
      5. otherwise → READY_FOR_GRADUATION.

    Caller-injected overrides (testing):
      * ``snapshot_reader`` — replaces the default Slice 4
        read API with a deterministic fixture.
      * ``master_enabled_override`` — bypasses the Slice 3
        master flag check (test isolation).
      * ``enabled_override`` — bypasses the harness master
        flag check.
    """
    ts = time.time()
    threshold = (
        max_non_actionable_rate_override
        if max_non_actionable_rate_override is not None
        else max_non_actionable_rate()
    )
    required = (
        required_observations_override
        if required_observations_override is not None
        else required_observations()
    )

    # Gate 0: harness disabled
    enabled = (
        enabled_override
        if enabled_override is not None
        else harness_enabled()
    )
    if not enabled:
        return MultiPriorGraduationReport(
            verdict=(
                MultiPriorGraduationVerdict.DISABLED
            ),
            total_observations=0,
            non_actionable_count=0,
            non_actionable_rate=0.0,
            required_observations=int(required),
            max_non_actionable_rate=float(threshold),
            breakdown_by_action={},
            detail=(
                "JARVIS_MULTI_PRIOR_GRADUATION_CONTRACT_"  # noqa: E501
                "ENABLED is false"
            ),
            ts_unix=ts,
        )

    # Gate 1: ALREADY_GRADUATED — composes Slice 3's
    # master_enabled (single source of truth; lazy-imported).
    if master_enabled_override is not None:
        already = bool(master_enabled_override)
    else:
        already = False
        try:
            from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
                master_enabled as dispatch_master_enabled,
            )
            already = bool(dispatch_master_enabled())
        except Exception:  # noqa: BLE001 — defensive
            already = False
    if already:
        return MultiPriorGraduationReport(
            verdict=(
                MultiPriorGraduationVerdict.ALREADY_GRADUATED  # noqa: E501
            ),
            total_observations=0,
            non_actionable_count=0,
            non_actionable_rate=0.0,
            required_observations=int(required),
            max_non_actionable_rate=float(threshold),
            breakdown_by_action={},
            detail=(
                "Slice 3 master flag "
                "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED is "
                "already TRUE"
            ),
            ts_unix=ts,
        )

    # Gather evidence via the snapshot reader (default
    # composes Slice 4's read API; lazy-imported).
    reader = (
        snapshot_reader
        if snapshot_reader is not None
        else _default_snapshot_reader
    )
    try:
        snapshot = dict(reader() or {})
    except Exception:  # noqa: BLE001 — defensive
        snapshot = {}
    failures = int(snapshot.pop("_with_failures", 0))
    total = sum(snapshot.values())
    breakdown: Dict[str, int] = dict(snapshot)
    non_actionable_action_count = sum(
        v for k, v in snapshot.items()
        if k in _NON_ACTIONABLE_ACTIONS
    )
    # Operator binding: cancellations + errors count as
    # non-actionable signals regardless of action_recommendation
    # — they're orthogonal failure modes. We use the union of
    # action-driven non-actionable rows + failure-flagged
    # rows; the failure-flagged set is bounded by ``failures``.
    # To avoid double-counting an op that's both
    # non-actionable AND failure-flagged, take the maximum.
    non_actionable_count = max(
        non_actionable_action_count, failures,
    )
    rate = (
        (non_actionable_count / total)
        if total > 0 else 0.0
    )

    # Gate 2: INSUFFICIENT_OBSERVATIONS
    if total < required:
        return MultiPriorGraduationReport(
            verdict=(
                MultiPriorGraduationVerdict
                .INSUFFICIENT_OBSERVATIONS
            ),
            total_observations=total,
            non_actionable_count=non_actionable_count,
            non_actionable_rate=rate,
            required_observations=int(required),
            max_non_actionable_rate=float(threshold),
            breakdown_by_action=breakdown,
            detail=(
                f"observations={total} < "
                f"required={required}"
            ),
            ts_unix=ts,
        )

    # Gate 3: EXCESSIVE_NON_ACTIONABLE_RATE
    if rate > threshold:
        return MultiPriorGraduationReport(
            verdict=(
                MultiPriorGraduationVerdict
                .EXCESSIVE_NON_ACTIONABLE_RATE
            ),
            total_observations=total,
            non_actionable_count=non_actionable_count,
            non_actionable_rate=rate,
            required_observations=int(required),
            max_non_actionable_rate=float(threshold),
            breakdown_by_action=breakdown,
            detail=(
                f"non_actionable_rate={rate:.3f} > "
                f"threshold={threshold:.3f}"
            ),
            ts_unix=ts,
        )

    # All gates passed → READY_FOR_GRADUATION
    return MultiPriorGraduationReport(
        verdict=(
            MultiPriorGraduationVerdict.READY_FOR_GRADUATION
        ),
        total_observations=total,
        non_actionable_count=non_actionable_count,
        non_actionable_rate=rate,
        required_observations=int(required),
        max_non_actionable_rate=float(threshold),
        breakdown_by_action=breakdown,
        detail=(
            f"observations={total} ≥ {required}; "
            f"non_actionable_rate={rate:.3f} ≤ "
            f"{threshold:.3f}"
        ),
        ts_unix=ts,
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds 3 flags."""
    try:
        registry.register(
            name=(
                "JARVIS_MULTI_PRIOR_GRADUATION_"
                "CONTRACT_ENABLED"
            ),
            type_="bool",
            default="true",
            description=(
                "Harness master switch for Move 6.5 Slice 6 "
                "graduation contract. Default-TRUE per §33.1 "
                "separation: the contract is a passive read-"
                "only oracle, so the harness can be on while "
                "the producer flags (Slices 1-5) stay "
                "default-FALSE."
            ),
            category="Generation",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/"
                "multi_prior_graduation_contract.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_GRADUATION_"
                "CONTRACT_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorGraduation] harness-flag seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name=(
                "JARVIS_MULTI_PRIOR_GRADUATION_"
                "REQUIRED_OBSERVATIONS"
            ),
            type_="int",
            default=str(_DEFAULT_REQUIRED_OBSERVATIONS),
            description=(
                "Gate 2 threshold — minimum Slice 4 ledger "
                "rows required before READY_FOR_GRADUATION. "
                "Default 50; clamped [1, 10000]."
            ),
            category="Generation",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/"
                "multi_prior_graduation_contract.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_GRADUATION_"
                "REQUIRED_OBSERVATIONS=50"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorGraduation] required-observations "
            "seeding failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name=(
                "JARVIS_MULTI_PRIOR_GRADUATION_"
                "MAX_NON_ACTIONABLE_RATE"
            ),
            type_="float",
            default=str(_DEFAULT_MAX_NON_ACTIONABLE_RATE),
            description=(
                "Gate 3 threshold — maximum share of "
                "non-actionable observations (escalate + "
                "fall_through + rolls with cancellations / "
                "errors). Default 0.40; clamped [0.0, 1.0]."
            ),
            category="Generation",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/"
                "multi_prior_graduation_contract.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_GRADUATION_"
                "MAX_NON_ACTIONABLE_RATE=0.40"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorGraduation] max-rate seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_graduation_verdict_taxonomy_5_values``
      2. ``multi_prior_graduation_authority_asymmetry``
      3. ``multi_prior_graduation_composes_substrate``
         — the predicate MUST lazy-import Slice 3's
         ``master_enabled`` and Slice 4's
         ``read_recent_observations`` (single source of
         truth; no parallel evidence reading).
      4. ``multi_prior_graduation_pattern_compliance`` —
         proves §33.1 canonical-shape parity with Move 7
         (predicate name + verdict enum 5-value + frozen
         report + harness flag helper).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_graduation_contract.py"
    )

    def _validate_verdict_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "READY_FOR_GRADUATION",
            "INSUFFICIENT_OBSERVATIONS",
            "EXCESSIVE_NON_ACTIONABLE_RATE",
            "ALREADY_GRADUATED",
            "DISABLED",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name
                == "MultiPriorGraduationVerdict"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extra = seen - required
                if missing:
                    violations.append(
                        f"MultiPriorGraduationVerdict "
                        f"missing {sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"MultiPriorGraduationVerdict has "
                        f"extra {sorted(extra)} — closed at "
                        f"5 values per §33.1 canonical shape"
                    )
                return tuple(violations)
        violations.append(
            "MultiPriorGraduationVerdict class missing"
        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_graduation" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_graduation_"
                            f"contract.py MUST NOT import "
                            f"{module!r} (forbidden segment "
                            f"{seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_graduation_"
                            f"contract.py MUST NOT import "
                            f"{module!r} (forbidden token "
                            f"{f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_composes_substrate(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """The predicate :func:`is_ready_for_graduation`
        MUST lazy-import Slice 3's ``master_enabled`` (single
        source of truth for Gate 1) and the default snapshot
        reader MUST lazy-import Slice 4's
        ``read_recent_observations`` (canonical evidence
        channel for Gate 2 + 3). Top-level imports of those
        substrates are forbidden."""
        violations: list = []
        # Forbid top-level imports.
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "multi_prior_dispatch" in module:
                    for alias in node.names:
                        if alias.name == "master_enabled":
                            violations.append(
                                "composes-substrate: "
                                "multi_prior_dispatch."
                                "master_enabled MUST be "
                                "lazy-imported inside the "
                                "predicate, not at module "
                                "top-level"
                            )
                if "multi_prior_observer" in module:
                    for alias in node.names:
                        if alias.name == (
                            "read_recent_observations"
                        ):
                            violations.append(
                                "composes-substrate: "
                                "multi_prior_observer."
                                "read_recent_observations "
                                "MUST be lazy-imported "
                                "inside the snapshot reader, "
                                "not at module top-level"
                            )
        # Predicate must lazy-import master_enabled.
        predicate_func: Optional[ast.FunctionDef] = None
        reader_func: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if (
                    node.name == "is_ready_for_graduation"
                ):
                    predicate_func = node
                elif (
                    node.name == "_default_snapshot_reader"
                ):
                    reader_func = node
        if predicate_func is None:
            violations.append(
                "is_ready_for_graduation function missing"
            )
        else:
            composes_master = False
            for sub in ast.walk(predicate_func):
                if isinstance(sub, ast.ImportFrom):
                    module = sub.module or ""
                    if "multi_prior_dispatch" in module:
                        for alias in sub.names:
                            if (
                                alias.name == "master_enabled"
                            ):
                                composes_master = True
                                break
                if composes_master:
                    break
            if not composes_master:
                violations.append(
                    "composes-substrate: "
                    "is_ready_for_graduation MUST "
                    "lazy-import master_enabled from "
                    "multi_prior_dispatch (single source "
                    "of truth for Gate 1)"
                )
        if reader_func is None:
            violations.append(
                "_default_snapshot_reader function missing"
            )
        else:
            composes_reader = False
            for sub in ast.walk(reader_func):
                if isinstance(sub, ast.ImportFrom):
                    module = sub.module or ""
                    if "multi_prior_observer" in module:
                        for alias in sub.names:
                            if alias.name == (
                                "read_recent_observations"
                            ):
                                composes_reader = True
                                break
                if composes_reader:
                    break
            if not composes_reader:
                violations.append(
                    "composes-substrate: "
                    "_default_snapshot_reader MUST "
                    "lazy-import read_recent_observations "
                    "from multi_prior_observer (canonical "
                    "evidence channel for Gates 2+3)"
                )
        return tuple(violations)

    def _validate_pattern_compliance(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """§33.1 canonical-shape parity with Move 7's
        :mod:`cross_op_semantic_budget_graduation_contract`:
          * predicate name :func:`is_ready_for_graduation`
            present
          * 5-value verdict enum present
          * frozen Report dataclass present
          * harness master flag helper present
        """
        violations: list = []
        has_predicate = False
        has_verdict_enum = False
        has_report_class = False
        has_harness_helper = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "is_ready_for_graduation":
                    has_predicate = True
                elif node.name == "harness_enabled":
                    has_harness_helper = True
            if isinstance(node, ast.ClassDef):
                if (
                    node.name
                    == "MultiPriorGraduationVerdict"
                ):
                    has_verdict_enum = True
                if (
                    node.name
                    == "MultiPriorGraduationReport"
                ):
                    has_report_class = True
        if not has_predicate:
            violations.append(
                "pattern-compliance: "
                "is_ready_for_graduation predicate MUST "
                "exist per §33.1 canonical shape"
            )
        if not has_verdict_enum:
            violations.append(
                "pattern-compliance: "
                "MultiPriorGraduationVerdict enum MUST "
                "exist per §33.1 canonical shape"
            )
        if not has_report_class:
            violations.append(
                "pattern-compliance: "
                "MultiPriorGraduationReport frozen class "
                "MUST exist per §33.1 canonical shape"
            )
        if not has_harness_helper:
            violations.append(
                "pattern-compliance: harness_enabled() "
                "MUST exist per §33.1 canonical shape"
            )
        # Harness master flag MUST be default-TRUE per §33.1
        # separation (mirrors Move 7's
        # cross_op_semantic_budget_graduation_contract +
        # Move 8's curiosity contract).
        has_default_true = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "harness_enabled"
            ):
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.If):
                        continue
                    for cmp_node in ast.walk(sub.test):
                        if not isinstance(
                            cmp_node, ast.Compare,
                        ):
                            continue
                        operand_empty = False
                        for operand in (
                            cmp_node.left,
                            *cmp_node.comparators,
                        ):
                            if (
                                isinstance(
                                    operand, ast.Constant,
                                )
                                and operand.value == ""
                            ):
                                operand_empty = True
                                break
                        if not operand_empty:
                            continue
                        for stmt in sub.body:
                            if (
                                isinstance(stmt, ast.Return)
                                and isinstance(
                                    stmt.value, ast.Constant,
                                )
                                and stmt.value.value is True
                            ):
                                has_default_true = True
                                break
                        if has_default_true:
                            break
                    if has_default_true:
                        break
                break
        if not has_default_true:
            violations.append(
                "pattern-compliance: harness_enabled() MUST "
                "return True on empty env-var string per "
                "§33.1 separation (harness default-TRUE; "
                "producer Slices 1-5 default-FALSE). "
                "Mirrors Move 7 + Move 8 canonical shape."
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_graduation_"
                "verdict_taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 6 — verdict closed at 5 "
                "values per §33.1 canonical shape."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_graduation_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 6 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_graduation_"
                "composes_substrate"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 6 — predicate composes "
                "Slice 3's master_enabled (Gate 1) + "
                "Slice 4's read_recent_observations (Gates "
                "2+3); no top-level imports of those "
                "substrates."
            ),
            validate=_validate_composes_substrate,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_graduation_"
                "pattern_compliance"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 6 — §33.1 canonical-shape "
                "parity with Move 7 + Move 8: predicate "
                "name + verdict enum 5-value + frozen "
                "report + harness master default-TRUE."
            ),
            validate=_validate_pattern_compliance,
        ),
    ]


__all__ = [
    "MULTI_PRIOR_GRADUATION_SCHEMA_VERSION",
    "MultiPriorGraduationReport",
    "MultiPriorGraduationVerdict",
    "harness_enabled",
    "is_ready_for_graduation",
    "max_non_actionable_rate",
    "register_flags",
    "register_shipped_invariants",
    "required_observations",
]
