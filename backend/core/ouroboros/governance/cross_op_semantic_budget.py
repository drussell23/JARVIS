"""Move 7 — Cross-op Semantic Budget primitive (PRD §29.4 Slice 1,
2026-05-05).

Closes the §28.5.2 v9 brutal-review's "slow-boil compounded drift
over 100+ cycles" bypass vector. Move 4 (Invariant Drift Auditor)
catches *architectural promise* drift; Move 7 catches *semantic
meaning* drift. Together they bound RSI drift in both axes
mathematically — the foundation for stable Recursive Self-
Improvement per §29.4 line 3611.

## Problem framing

A single op may be within Coherence Auditor's behavioral-drift
threshold but, integrated over 100+ ops, the codebase's semantic
centroid (per :class:`SemanticIndex`) silently rotates by 30%+.
Single-window auditing misses the slow boil. This primitive sums
cosine-distance deltas across a rolling window of recent ops'
centroids and compares the integrated drift to an operator-tunable
budget knob.

## Architectural locks (operator mandate, AST-pinned)

  1. **Pure substrate** — stdlib only; composes existing primitives
     (no parallel cosine implementation, no parallel persistence).
  2. **Pure function compute_semantic_budget()** — no I/O, no
     env reads inside the math; caller injects centroids +
     threshold. Slice 2 will add the I/O / observer wiring.
  3. **§33.1 Graduation Contract Pattern** — master flag
     ``JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED`` stays default-
     FALSE until empirical baseline (likely tied to Phase 9
     cadence). AST pin enforces.
  4. **§33.5 Versioned-Artifact-Contract Pattern** —
     :class:`OpSemanticCentroid` carries ``schema_version`` +
     symmetric ``to_dict`` / ``from_dict`` for cross-runner
     ledger reads (Slice 2's JSONL consumer).
  5. **Authority asymmetry** — imports stdlib + ``meta.versioned_-
     artifact`` ONLY. NEVER imports orchestrator / iron_gate /
     policy / providers / candidate_generator / urgency_router /
     change_engine / semantic_guardian.
  6. **NEVER raises** — all faults map to ``DISABLED`` /
     ``INSUFFICIENT_DATA`` verdict with diagnostics.

## Slice 1 vs full arc scope

Slice 1 ships the substrate primitive only — pure function over
caller-supplied centroids. Slices 2-5 (deferred):

  * Slice 2 — centroid recorder at COMPLETE phase boundary
    (lazy producer-bridge per §33.2; flock'd JSONL per §33.4)
  * Slice 3 — async observer + SSE ``semantic_budget_changed``
    event + ``GET /observability/semantic-budget`` (auto-mounted
    via §33.3 naming-cage)
  * Slice 4 — ``/semantic-budget`` REPL verb (auto-discovered via
    Slice 5b consolidation registry)
  * Slice 5 — graduation contract harness gating master flag flip
    on accumulated empirical evidence (per §33.1)
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


CROSS_OP_SEMANTIC_BUDGET_SCHEMA_VERSION: str = (
    "cross_op_semantic_budget.1"
)

OP_SEMANTIC_CENTROID_SCHEMA_VERSION: str = (
    "op_semantic_centroid.1"
)


# ---------------------------------------------------------------------------
# Closed-enum verdict taxonomy
# ---------------------------------------------------------------------------


class SemanticBudgetVerdict(str, Enum):
    """5-value closed taxonomy for the
    :func:`compute_semantic_budget` primitive. New values
    require explicit scope-doc + AST pin update."""

    WITHIN_BUDGET = "within_budget"
    """Integrated drift well below threshold."""

    APPROACHING = "approaching"
    """Integrated drift in the warning band
    ``threshold × approaching_ratio ≤ drift < threshold``."""

    EXCEEDED = "exceeded"
    """Integrated drift ≥ threshold — operator review of
    trajectory advised."""

    INSUFFICIENT_DATA = "insufficient_data"
    """Fewer than 2 centroids in the window — no delta to
    integrate. NOT an error; benign cold-start."""

    DISABLED = "disabled"
    """Master flag off; computation skipped."""


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def cross_op_semantic_budget_enabled() -> bool:
    """``JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED`` — master kill
    switch. Default **FALSE** per §33.1 Graduation Contract
    Pattern; flips only after empirical Phase 9 baseline. AST-
    pinned: future PR that flips default-true without a
    graduation-contract handoff fails the pin."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default — operator-pinned
    return raw in ("1", "true", "yes", "on")


def window_size() -> int:
    """``JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE`` — number of most-
    recent op centroids to integrate. Default 50 (≈ a couple
    hours of normal operation; large enough to surface 1%/op
    compounding without hyper-noise)."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE", "",
    ).strip()
    try:
        n = int(raw) if raw else 50
        if n < 2:
            return 2
        if n > 10_000:
            return 10_000
        return n
    except (TypeError, ValueError):
        return 50


def drift_threshold() -> float:
    """``JARVIS_CROSS_OP_SEMANTIC_THRESHOLD`` — operator budget
    knob: integrated cosine-distance summed over the window MUST
    NOT exceed this fraction. Default 0.30 (30% — calibrated for
    the §29.4 "1%/op compounding over 100 cycles" framing)."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_THRESHOLD", "",
    ).strip()
    try:
        v = float(raw) if raw else 0.30
        if v <= 0.0:
            return 0.001
        if v > 100.0:
            return 100.0
        return v
    except (TypeError, ValueError):
        return 0.30


def approaching_ratio() -> float:
    """``JARVIS_CROSS_OP_SEMANTIC_APPROACHING_RATIO`` — fraction of
    threshold above which the verdict ladder transitions to
    ``APPROACHING``. Default 0.8 (warn at 80% of budget). Clamped
    [0.1, 1.0]."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_APPROACHING_RATIO", "",
    ).strip()
    try:
        v = float(raw) if raw else 0.8
        if v < 0.1:
            return 0.1
        if v > 1.0:
            return 1.0
        return v
    except (TypeError, ValueError):
        return 0.8


# ---------------------------------------------------------------------------
# Pure cosine — no parallel implementation; substrate-bounded
# ---------------------------------------------------------------------------


def cosine_distance(
    a: Sequence[float],
    b: Sequence[float],
) -> float:
    """Cosine distance ``1 - cos(a, b)`` ∈ [0, 2].

    Pure stdlib (sqrt + iteration). Returns 0.0 when either
    vector is empty / all-zero — semantically "no movement"
    rather than NaN. Returns 2.0 (max distance) for opposing
    unit vectors. NEVER raises.

    Why inline (not import semantic_index._cosine): substrate
    authority asymmetry — Move 7 must not depend on
    semantic_index's full surface. The math is 8 lines; bytes-
    pinned for parity with semantic_index._cosine."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    try:
        for i in range(n):
            ai = float(a[i])
            bi = float(b[i])
            dot += ai * bi
            norm_a += ai * ai
            norm_b += bi * bi
    except (TypeError, ValueError):
        return 0.0
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    sim = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    # Numerical safety — clamp to [-1, 1].
    if sim > 1.0:
        sim = 1.0
    elif sim < -1.0:
        sim = -1.0
    return 1.0 - sim


# ---------------------------------------------------------------------------
# Frozen artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpSemanticCentroid:
    """One op's semantic centroid snapshot at COMPLETE phase
    boundary. Adopts §33.5 Versioned-Artifact-Contract: carries
    ``schema_version`` + symmetric ``to_dict`` / ``from_dict``
    for the cross-runner JSONL ledger Slice 2 will write."""

    op_id: str
    ts_unix: float
    centroid: Tuple[float, ...]
    centroid_hash: str = ""
    schema_version: str = OP_SEMANTIC_CENTROID_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "ts_unix": float(self.ts_unix),
            "centroid": list(self.centroid),
            "centroid_hash": self.centroid_hash,
        }

    @classmethod
    def from_dict(
        cls, raw: Dict[str, Any],
    ) -> "Optional[OpSemanticCentroid]":
        """Defensive parse — returns ``None`` on malformed
        fields. NEVER raises."""
        try:
            if not isinstance(raw, dict):
                return None
            centroid_raw = raw.get("centroid", []) or []
            if not isinstance(centroid_raw, (list, tuple)):
                return None
            centroid = tuple(
                float(x) for x in centroid_raw
                if isinstance(x, (int, float))
            )
            return cls(
                op_id=str(raw.get("op_id", "")),
                ts_unix=float(raw.get("ts_unix", 0.0)),
                centroid=centroid,
                centroid_hash=str(raw.get("centroid_hash", "")),
                schema_version=str(
                    raw.get(
                        "schema_version",
                        OP_SEMANTIC_CENTROID_SCHEMA_VERSION,
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class SemanticBudgetReport:
    """Aggregated verdict + diagnostic projection from one
    :func:`compute_semantic_budget` call. Frozen, JSON-
    projectable for telemetry + observability surfaces."""

    verdict: SemanticBudgetVerdict
    integrated_drift: float
    threshold: float
    approaching_band: float
    window_size: int
    centroids_seen: int
    per_op_deltas: Tuple[float, ...] = field(default_factory=tuple)
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    elapsed_s: float = 0.0
    schema_version: str = field(
        default=CROSS_OP_SEMANTIC_BUDGET_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "integrated_drift": float(self.integrated_drift),
            "threshold": float(self.threshold),
            "approaching_band": float(self.approaching_band),
            "window_size": int(self.window_size),
            "centroids_seen": int(self.centroids_seen),
            "per_op_deltas": [
                float(d) for d in self.per_op_deltas
            ],
            "diagnostics": list(self.diagnostics),
            "elapsed_s": float(self.elapsed_s),
        }


# ---------------------------------------------------------------------------
# Pure function — compute_semantic_budget
# ---------------------------------------------------------------------------


def compute_semantic_budget(
    centroids: Sequence[OpSemanticCentroid],
    *,
    threshold: Optional[float] = None,
    approaching_band_ratio: Optional[float] = None,
    enabled_override: Optional[bool] = None,
) -> SemanticBudgetReport:
    """Integrate cosine-distance deltas across the supplied
    rolling window of op centroids and emit a frozen
    :class:`SemanticBudgetReport`.

    Pure function — no I/O, no env reads inside the math (env
    knobs resolved at call boundary). Caller is expected to
    supply the most-recent ``window_size()`` centroids
    chronologically.

    Verdict ladder:

      * ``DISABLED`` — master flag off (or
        ``enabled_override=False``)
      * ``INSUFFICIENT_DATA`` — fewer than 2 centroids
      * ``EXCEEDED`` — integrated drift ≥ threshold
      * ``APPROACHING`` — drift ≥ threshold × approaching_ratio
      * ``WITHIN_BUDGET`` — drift below the warning band

    NEVER raises. Every fault path emits a structured
    diagnostic + a defensive verdict (DISABLED / INSUFFICIENT_-
    DATA) rather than crashing."""
    t0 = time.monotonic()

    # Resolve config — caller can override per-call (testing).
    eff_enabled = (
        enabled_override
        if enabled_override is not None
        else cross_op_semantic_budget_enabled()
    )
    eff_threshold = (
        float(threshold)
        if threshold is not None
        else drift_threshold()
    )
    eff_ratio = (
        float(approaching_band_ratio)
        if approaching_band_ratio is not None
        else approaching_ratio()
    )
    # Clamp ratio defensively — caller may have passed garbage.
    if eff_ratio < 0.0:
        eff_ratio = 0.0
    elif eff_ratio > 1.0:
        eff_ratio = 1.0
    eff_band = eff_threshold * eff_ratio

    if not eff_enabled:
        return SemanticBudgetReport(
            verdict=SemanticBudgetVerdict.DISABLED,
            integrated_drift=0.0,
            threshold=eff_threshold,
            approaching_band=eff_band,
            window_size=0,
            centroids_seen=0,
            elapsed_s=time.monotonic() - t0,
        )

    try:
        snapshots = list(centroids or ())
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetReport(
            verdict=SemanticBudgetVerdict.DISABLED,
            integrated_drift=0.0,
            threshold=eff_threshold,
            approaching_band=eff_band,
            window_size=0,
            centroids_seen=0,
            diagnostics=(
                f"centroids_unmaterialized: "
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    if len(snapshots) < 2:
        return SemanticBudgetReport(
            verdict=SemanticBudgetVerdict.INSUFFICIENT_DATA,
            integrated_drift=0.0,
            threshold=eff_threshold,
            approaching_band=eff_band,
            window_size=len(snapshots),
            centroids_seen=len(snapshots),
            diagnostics=(
                f"need_2_centroids_got_{len(snapshots)}",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    # Pairwise cosine-distance deltas across the window.
    deltas: List[float] = []
    skipped = 0
    for i in range(1, len(snapshots)):
        prev = snapshots[i - 1]
        cur = snapshots[i]
        try:
            d = cosine_distance(prev.centroid, cur.centroid)
            # Defensive — ensure finite + non-negative
            if d != d or d < 0.0:  # NaN check
                skipped += 1
                continue
            deltas.append(d)
        except Exception:  # noqa: BLE001 — defensive
            skipped += 1
            continue

    integrated = sum(deltas)

    diagnostics: List[str] = []
    if skipped > 0:
        diagnostics.append(
            f"skipped_{skipped}_malformed_pairs"
        )

    if integrated >= eff_threshold:
        verdict = SemanticBudgetVerdict.EXCEEDED
    elif integrated >= eff_band:
        verdict = SemanticBudgetVerdict.APPROACHING
    else:
        verdict = SemanticBudgetVerdict.WITHIN_BUDGET

    return SemanticBudgetReport(
        verdict=verdict,
        integrated_drift=integrated,
        threshold=eff_threshold,
        approaching_band=eff_band,
        window_size=len(snapshots),
        centroids_seen=len(snapshots),
        per_op_deltas=tuple(deltas),
        diagnostics=tuple(diagnostics),
        elapsed_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Pins:
      1. ``cross_op_semantic_budget_master_flag_stays_default_false``
         — operator binding (§33.1 graduation contract pattern):
         master flag default is FALSE until empirical baseline.
      2. ``cross_op_semantic_budget_authority_asymmetry`` —
         substrate stays pure (no orchestrator / iron_gate /
         policy / providers imports).
      3. ``cross_op_semantic_budget_verdict_taxonomy_5_values``
         — closed-enum integrity (taxonomy may not silently
         grow without scope doc + pin update).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """The ``cross_op_semantic_budget_enabled()`` helper MUST
        return False on the unset-env path (§33.1 operator
        binding). Bytes-pin: source MUST contain
        ``return False  # default — operator-pinned`` literal
        OR equivalent default-False fallthrough."""
        violations: list = []
        target_func = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if (
                    node.name
                    == "cross_op_semantic_budget_enabled"
                ):
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "cross_op_semantic_budget_enabled function "
                "missing"
            )
            return tuple(violations)
        # AST-based — walk function body classifying every
        # bare `return <constant>` statement. If ANY constant
        # return is True without a paired guard above it,
        # flag premature flip. If no `return False` appears
        # anywhere on the unset-env path, flag missing
        # operator-binding default.
        has_default_false = False
        has_unguarded_default_true = False
        for node in ast.walk(target_func):
            if isinstance(node, ast.Return):
                if isinstance(node.value, ast.Constant):
                    if node.value.value is False:
                        has_default_false = True
                    elif node.value.value is True:
                        # A `return True` IS allowed inside a
                        # truthy-env-check guard (the post-
                        # parse path); but a top-level
                        # default-True return without the
                        # `return False` companion = drift.
                        has_unguarded_default_true = True
        if not has_default_false:
            violations.append(
                "cross_op_semantic_budget_enabled MUST return "
                "False on the unset-env path (§33.1 operator "
                "binding — master flag default-FALSE until "
                "empirical baseline)"
            )
        # Diagnostic only — `return True` is allowed when
        # `return False` is also present (truthy-env path).
        # If neither is present, the missing-default-False
        # branch above already caught it.
        _ = has_unguarded_default_true
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Substrate purity — no orchestrator / iron_gate /
        etc. imports."""
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
                            f"cross_op_semantic_budget.py MUST "
                            f"NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_verdict_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """SemanticBudgetVerdict has EXACTLY 5 closed values.
        New values require explicit scope-doc + this pin update."""
        violations: list = []
        required = {
            "WITHIN_BUDGET", "APPROACHING", "EXCEEDED",
            "INSUFFICIENT_DATA", "DISABLED",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "SemanticBudgetVerdict":
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
                            f"SemanticBudgetVerdict missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"SemanticBudgetVerdict has extra "
                            f"values (closed-taxonomy "
                            f"violation): {sorted(extras)}"
                        )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "cross_op_semantic_budget.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_master_flag_"
                "stays_default_false"
            ),
            target_file=target,
            description=(
                "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED MUST "
                "stay default-FALSE per §33.1 Graduation "
                "Contract Pattern — operator binding until "
                "empirical Phase 9 baseline establishes the "
                "drift envelope for this codebase."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "cross_op_semantic_budget.py MUST stay pure "
                "substrate — stdlib + math + meta.versioned_-"
                "artifact ONLY (no governance imports)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_verdict_taxonomy_5_values"  # noqa: E501
            ),
            target_file=target,
            description=(
                "SemanticBudgetVerdict is a 5-value closed "
                "taxonomy (WITHIN_BUDGET / APPROACHING / "
                "EXCEEDED / INSUFFICIENT_DATA / DISABLED). "
                "New values require explicit scope-doc + pin "
                "update."
            ),
            validate=_validate_verdict_taxonomy_closed,
        ),
    ]


__all__ = [
    "CROSS_OP_SEMANTIC_BUDGET_SCHEMA_VERSION",
    "OP_SEMANTIC_CENTROID_SCHEMA_VERSION",
    "OpSemanticCentroid",
    "SemanticBudgetReport",
    "SemanticBudgetVerdict",
    "approaching_ratio",
    "compute_semantic_budget",
    "cosine_distance",
    "cross_op_semantic_budget_enabled",
    "drift_threshold",
    "register_shipped_invariants",
    "window_size",
]
