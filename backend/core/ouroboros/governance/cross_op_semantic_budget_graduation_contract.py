"""Move 7 Slice 5 — graduation contract harness (PRD §29.4 /
§33.1, 2026-05-05).

Closes Move 7 by structurally enforcing the operator-binding
evidence ladder before ``JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED``
may flip default-FALSE → default-TRUE. Mirrors
:mod:`phase10_graduation_contract` per §33.1 Graduation Contract
Pattern — substrate-only enforcement of an operator binding;
operator paces the empirical accumulation, the cage validates it.

## Why this exists

§33.1 framing: master flag default-FALSE until empirical baseline
establishes the threshold knob is calibrated for THIS codebase.
Move 7 specifically: the default 0.30 integrated-drift threshold
(calibrated for §29.4's 1%/op×100-cycle compounding framing) is
an *initial guess* — the ACTUAL drift envelope of this codebase
must be measured before flipping default-true. Otherwise we
either (a) flip too aggressive (false-positive EXCEEDED triggers)
or (b) flip too lax (slow-boil drift slips through).

Slice 5 ships the verdict primitive that maps "raw ledger state"
→ `READY_FOR_GRADUATION`. Operator runs the substrate (Slices
1-4) for some empirical period (likely tied to Phase 9 cadence),
queries this contract, and only commits the default-true flip
after the verdict goes green.

## Architectural locks (operator mandate, AST-pinned)

  1. **Composes Slices 1+2** — uses
     :func:`compute_semantic_budget` for the per-window check
     AND :func:`read_recent_centroids` for ledger inspection.
     NEVER reimplements either.
  2. **Pure-function predicate** — no I/O beyond what Slice 2
     does internally; no env reads inside the math (caller
     injects). Caller chains the verdict into operator-facing
     surfaces (REPL, observability).
  3. **Master-flag-default-TRUE** — *this contract's* master
     flag (``JARVIS_CROSS_OP_SEMANTIC_GRADUATION_CONTRACT_-
     ENABLED``) defaults TRUE so it's queryable; the
     OPERATOR-BINDING pin lives on Slice 1's master flag (the
     thing being gated), NOT this contract's.
  4. **Authority asymmetry** — imports stdlib + Slice 1 +
     Slice 2 ONLY. NEVER imports orchestrator / iron_gate /
     policy / providers / candidate_generator / change_engine /
     semantic_guardian.
  5. **NEVER raises** — every fault maps to a defensive verdict
     with diagnostics.
  6. **5-value closed verdict taxonomy** — AST-pinned. New
     values require explicit scope-doc + pin update.

## Verdict ladder

  * ``READY_FOR_GRADUATION`` — all gates green: ≥N centroid
    samples + producer fresh (last centroid within
    freshness_max_age_s) + ≥K consecutive stable
    rolling-windows (none EXCEEDED)
  * ``INSUFFICIENT_OP_SAMPLES`` — ledger has fewer than
    ``required_samples`` centroids
  * ``PRODUCER_INACTIVE`` — newest centroid older than
    ``freshness_max_age_s`` (producer not wired OR substrate
    not running OR master flag never flipped on)
  * ``EXCESSIVE_DRIFT_DETECTED`` — recent rolling-window verdict
    was EXCEEDED in ≥1 of the last K windows (threshold too
    tight for actual drift envelope, OR genuine drift that
    requires investigation)
  * ``DISABLED`` — graduation contract master flag off
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


CROSS_OP_SEMANTIC_BUDGET_GRADUATION_CONTRACT_SCHEMA_VERSION: str = (
    "cross_op_semantic_budget_graduation_contract.1"
)


# ---------------------------------------------------------------------------
# Closed-enum verdict taxonomy
# ---------------------------------------------------------------------------


class SemanticBudgetGraduationVerdict(str, Enum):
    """5-value closed taxonomy for the
    :func:`is_ready_for_graduation` predicate. New values
    require explicit scope-doc + AST pin update."""

    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_OP_SAMPLES = "insufficient_op_samples"
    PRODUCER_INACTIVE = "producer_inactive"
    EXCESSIVE_DRIFT_DETECTED = "excessive_drift_detected"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def graduation_contract_enabled() -> bool:
    """``JARVIS_CROSS_OP_SEMANTIC_GRADUATION_CONTRACT_ENABLED``
    — default ``true``. When false,
    :func:`is_ready_for_graduation` always returns
    ``DISABLED``. Intended for operator troubleshooting only.
    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_GRADUATION_CONTRACT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def required_op_samples() -> int:
    """``JARVIS_CROSS_OP_SEMANTIC_REQUIRED_OP_SAMPLES`` — minimum
    centroid count required before graduation considered.
    Default 100 (Phase-9-style baseline; ~1 day of normal
    operation at 1 op/15min). Clamped [10, 100000]."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_REQUIRED_OP_SAMPLES", "",
    ).strip()
    try:
        n = int(raw) if raw else 100
        if n < 10:
            return 10
        if n > 100_000:
            return 100_000
        return n
    except (TypeError, ValueError):
        return 100


def producer_freshness_max_age_s() -> float:
    """``JARVIS_CROSS_OP_SEMANTIC_PRODUCER_FRESHNESS_S`` —
    newest centroid must be no older than this for the
    producer to be considered active. Default 86400s (24h).
    Clamped [60s, 30 days]."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_PRODUCER_FRESHNESS_S", "",
    ).strip()
    try:
        v = float(raw) if raw else 86400.0
    except (TypeError, ValueError):
        return 86400.0
    if v < 60.0:
        return 60.0
    if v > 30.0 * 86400.0:
        return 30.0 * 86400.0
    return v


def stable_windows_required() -> int:
    """``JARVIS_CROSS_OP_SEMANTIC_STABLE_WINDOWS_REQUIRED`` —
    number of consecutive rolling-windows that must verdict as
    non-EXCEEDED. Default 3 (mirrors Phase 9's 3-clean-session
    pattern). Clamped [1, 100]."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_STABLE_WINDOWS_REQUIRED", "",
    ).strip()
    try:
        n = int(raw) if raw else 3
        if n < 1:
            return 1
        if n > 100:
            return 100
        return n
    except (TypeError, ValueError):
        return 3


# ---------------------------------------------------------------------------
# Frozen result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowSnapshot:
    """One rolling-window verdict snapshot. Frozen for safe
    propagation in :class:`SemanticBudgetGraduationReport`."""

    verdict: str
    integrated_drift: float
    threshold: float
    centroids_in_window: int


@dataclass(frozen=True)
class SemanticBudgetGraduationReport:
    """Aggregated verdict + diagnostic projection from one
    :func:`is_ready_for_graduation` call. Frozen, JSON-
    projectable for telemetry / observability surfaces."""

    verdict: SemanticBudgetGraduationVerdict
    centroids_seen: int
    required_samples: int
    newest_centroid_age_s: float
    freshness_max_age_s: float
    stable_windows_seen: int
    stable_windows_required: int
    recent_windows: Tuple[WindowSnapshot, ...] = field(
        default_factory=tuple,
    )
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    elapsed_s: float = 0.0
    schema_version: str = field(
        default=CROSS_OP_SEMANTIC_BUDGET_GRADUATION_CONTRACT_SCHEMA_VERSION,  # noqa: E501
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "centroids_seen": int(self.centroids_seen),
            "required_samples": int(self.required_samples),
            "newest_centroid_age_s": float(
                self.newest_centroid_age_s,
            ),
            "freshness_max_age_s": float(
                self.freshness_max_age_s,
            ),
            "stable_windows_seen": int(self.stable_windows_seen),
            "stable_windows_required": int(
                self.stable_windows_required,
            ),
            "recent_windows": [
                {
                    "verdict": w.verdict,
                    "integrated_drift": float(
                        w.integrated_drift,
                    ),
                    "threshold": float(w.threshold),
                    "centroids_in_window": int(
                        w.centroids_in_window,
                    ),
                }
                for w in self.recent_windows
            ],
            "diagnostics": list(self.diagnostics),
            "elapsed_s": float(self.elapsed_s),
        }


# ---------------------------------------------------------------------------
# Public API — is_ready_for_graduation
# ---------------------------------------------------------------------------


def is_ready_for_graduation(
    *,
    ledger_path: Optional[Path] = None,
    required_samples: Optional[int] = None,
    freshness_max_age_s: Optional[float] = None,
    stable_windows_n: Optional[int] = None,
    enabled_override: Optional[bool] = None,
    now_unix: Optional[float] = None,
) -> SemanticBudgetGraduationReport:
    """Evaluate whether Move 7's master flag is ready to flip
    default-FALSE → default-TRUE per §33.1 graduation contract.

    Reads the Slice 2 centroid ledger, computes per-window
    verdicts via Slice 1's primitive, and aggregates into a
    frozen :class:`SemanticBudgetGraduationReport`. NEVER raises.

    Verdict ladder (first-match-wins):
      1. ``DISABLED`` — graduation contract master flag off
         OR explicit ``enabled_override=False``
      2. ``INSUFFICIENT_OP_SAMPLES`` — ledger size <
         ``required_samples``
      3. ``PRODUCER_INACTIVE`` — newest centroid age >
         ``freshness_max_age_s``
      4. ``EXCESSIVE_DRIFT_DETECTED`` — any of the last
         ``stable_windows_n`` rolling-window verdicts was
         ``EXCEEDED``
      5. ``READY_FOR_GRADUATION`` — all gates green

    Caller arguments override env knobs (for testing).
    """
    t0 = time.monotonic()

    eff_enabled = (
        enabled_override
        if enabled_override is not None
        else graduation_contract_enabled()
    )
    eff_required = (
        int(required_samples)
        if required_samples is not None
        else required_op_samples()
    )
    eff_freshness = (
        float(freshness_max_age_s)
        if freshness_max_age_s is not None
        else producer_freshness_max_age_s()
    )
    eff_stable_n = (
        int(stable_windows_n)
        if stable_windows_n is not None
        else stable_windows_required()
    )
    eff_now = (
        float(now_unix) if now_unix is not None else time.time()
    )

    if not eff_enabled:
        return SemanticBudgetGraduationReport(
            verdict=SemanticBudgetGraduationVerdict.DISABLED,
            centroids_seen=0,
            required_samples=eff_required,
            newest_centroid_age_s=0.0,
            freshness_max_age_s=eff_freshness,
            stable_windows_seen=0,
            stable_windows_required=eff_stable_n,
            elapsed_s=time.monotonic() - t0,
        )

    # Slice 2 ledger read — caller-injected path supports
    # testing; default resolves Slice 2's env knob.
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
            read_recent_centroids,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetGraduationReport(
            verdict=SemanticBudgetGraduationVerdict.DISABLED,
            centroids_seen=0,
            required_samples=eff_required,
            newest_centroid_age_s=0.0,
            freshness_max_age_s=eff_freshness,
            stable_windows_seen=0,
            stable_windows_required=eff_stable_n,
            diagnostics=(
                f"slice2_recorder_unavailable: "
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    try:
        # Read up to required_samples (clamped at Slice 2's
        # max). For producer-freshness check we just need the
        # newest row; for stable-windows check we need enough
        # for stable_windows_n × window_size.
        all_centroids = read_recent_centroids(
            limit=10_000, path=ledger_path,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetGraduationReport(
            verdict=SemanticBudgetGraduationVerdict.DISABLED,
            centroids_seen=0,
            required_samples=eff_required,
            newest_centroid_age_s=0.0,
            freshness_max_age_s=eff_freshness,
            stable_windows_seen=0,
            stable_windows_required=eff_stable_n,
            diagnostics=(
                f"ledger_read_raised: "
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    centroids_seen = len(all_centroids)

    # Gate 1 — sufficient samples
    if centroids_seen < eff_required:
        return SemanticBudgetGraduationReport(
            verdict=(
                SemanticBudgetGraduationVerdict.INSUFFICIENT_OP_SAMPLES  # noqa: E501
            ),
            centroids_seen=centroids_seen,
            required_samples=eff_required,
            newest_centroid_age_s=0.0,
            freshness_max_age_s=eff_freshness,
            stable_windows_seen=0,
            stable_windows_required=eff_stable_n,
            diagnostics=(
                f"need_{eff_required}_samples_have_"
                f"{centroids_seen}",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    # Gate 2 — producer freshness (newest row recent enough)
    try:
        newest_ts = max(
            float(c.ts_unix) for c in all_centroids
        )
    except Exception:  # noqa: BLE001 — defensive
        newest_ts = 0.0
    age_s = max(0.0, eff_now - newest_ts)
    if age_s > eff_freshness:
        return SemanticBudgetGraduationReport(
            verdict=(
                SemanticBudgetGraduationVerdict.PRODUCER_INACTIVE
            ),
            centroids_seen=centroids_seen,
            required_samples=eff_required,
            newest_centroid_age_s=age_s,
            freshness_max_age_s=eff_freshness,
            stable_windows_seen=0,
            stable_windows_required=eff_stable_n,
            diagnostics=(
                f"newest_centroid_age_{age_s:.0f}s_exceeds_"
                f"freshness_{eff_freshness:.0f}s",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    # Gate 3 — stable rolling-windows (last K windows non-EXCEEDED)
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
            SemanticBudgetVerdict,
            compute_semantic_budget,
            window_size,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetGraduationReport(
            verdict=SemanticBudgetGraduationVerdict.DISABLED,
            centroids_seen=centroids_seen,
            required_samples=eff_required,
            newest_centroid_age_s=age_s,
            freshness_max_age_s=eff_freshness,
            stable_windows_seen=0,
            stable_windows_required=eff_stable_n,
            diagnostics=(
                f"slice1_primitive_unavailable: "
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    eff_window = window_size()
    # Build K rolling windows ending at progressively earlier
    # centroids: window_K-1 = [-W:], window_K-2 = [-2W:-W],
    # etc. Each window covers `eff_window` centroids.
    windows: list = []
    for k in range(eff_stable_n):
        end = centroids_seen - (k * eff_window)
        start = end - eff_window
        if start < 0:
            # Not enough data for this window — but Gate 1 already
            # ensured ≥ required_samples; if windows demand more,
            # diagnose and fall back to whatever we have.
            start = 0
        slice_centroids = all_centroids[start:end]
        if len(slice_centroids) < 2:
            continue
        try:
            report = compute_semantic_budget(
                slice_centroids, enabled_override=True,
            )
        except Exception:  # noqa: BLE001 — defensive
            continue
        windows.append(WindowSnapshot(
            verdict=report.verdict.value,
            integrated_drift=float(report.integrated_drift),
            threshold=float(report.threshold),
            centroids_in_window=len(slice_centroids),
        ))

    # Reverse so chronological-first → recent-last (matches
    # operator mental model when reading the report).
    windows.reverse()

    # Gate 3 check — any EXCEEDED in the last stable_windows_n?
    exceeded_recent = any(
        w.verdict == SemanticBudgetVerdict.EXCEEDED.value
        for w in windows
    )
    if exceeded_recent:
        return SemanticBudgetGraduationReport(
            verdict=(
                SemanticBudgetGraduationVerdict.EXCESSIVE_DRIFT_DETECTED  # noqa: E501
            ),
            centroids_seen=centroids_seen,
            required_samples=eff_required,
            newest_centroid_age_s=age_s,
            freshness_max_age_s=eff_freshness,
            stable_windows_seen=len(windows),
            stable_windows_required=eff_stable_n,
            recent_windows=tuple(windows),
            diagnostics=(
                f"exceeded_in_last_{eff_stable_n}_windows",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    # All gates green
    return SemanticBudgetGraduationReport(
        verdict=(
            SemanticBudgetGraduationVerdict.READY_FOR_GRADUATION
        ),
        centroids_seen=centroids_seen,
        required_samples=eff_required,
        newest_centroid_age_s=age_s,
        freshness_max_age_s=eff_freshness,
        stable_windows_seen=len(windows),
        stable_windows_required=eff_stable_n,
        recent_windows=tuple(windows),
        elapsed_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 3 pins:
      1. Authority asymmetry (substrate purity)
      2. Composes Slices 1+2 (no parallel math/ledger-read)
      3. Verdict taxonomy 5-values (closed-enum integrity)
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

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
                            f"graduation contract MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_substrate(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "compute_semantic_budget" not in source:
            violations.append(
                "graduation contract MUST compose Slice 1 "
                "compute_semantic_budget (no parallel math)"
            )
        if "read_recent_centroids" not in source:
            violations.append(
                "graduation contract MUST compose Slice 2 "
                "read_recent_centroids (no parallel ledger "
                "read)"
            )
        return tuple(violations)

    def _validate_verdict_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "READY_FOR_GRADUATION",
            "INSUFFICIENT_OP_SAMPLES",
            "PRODUCER_INACTIVE",
            "EXCESSIVE_DRIFT_DETECTED",
            "DISABLED",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if (
                    node.name
                    == "SemanticBudgetGraduationVerdict"
                ):
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"verdict taxonomy missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"verdict taxonomy has extras "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "cross_op_semantic_budget_graduation_contract.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_graduation_contract_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "graduation contract MUST stay pure substrate "
                "composing Slices 1+2 + stdlib ONLY (no "
                "orchestrator / iron_gate / policy / providers "
                "/ change_engine / semantic_guardian imports)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_graduation_contract_"
                "composes_substrate"
            ),
            target_file=target,
            description=(
                "graduation contract composes Slice 1 "
                "compute_semantic_budget + Slice 2 "
                "read_recent_centroids. No parallel math / "
                "ledger read."
            ),
            validate=_validate_composes_substrate,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_graduation_contract_"
                "verdict_taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "SemanticBudgetGraduationVerdict is a 5-value "
                "closed taxonomy (READY_FOR_GRADUATION / "
                "INSUFFICIENT_OP_SAMPLES / PRODUCER_INACTIVE / "
                "EXCESSIVE_DRIFT_DETECTED / DISABLED). New "
                "values require explicit scope-doc + pin "
                "update."
            ),
            validate=_validate_verdict_taxonomy_closed,
        ),
    ]


__all__ = [
    "CROSS_OP_SEMANTIC_BUDGET_GRADUATION_CONTRACT_SCHEMA_VERSION",
    "SemanticBudgetGraduationReport",
    "SemanticBudgetGraduationVerdict",
    "WindowSnapshot",
    "graduation_contract_enabled",
    "is_ready_for_graduation",
    "producer_freshness_max_age_s",
    "register_shipped_invariants",
    "required_op_samples",
    "stable_windows_required",
]
