"""
PhaseCost — frozen value types for per-phase cost drill-down.
==============================================================

Slice 1 of the Per-Phase Cost Drill-Down arc. Closes the CC-parity
gap *"why did this op cost $0.80? cost breakdown exists but isn't
drillable per-phase."*

Scope
-----

* **Pure value types.** This module defines :class:`PhaseCostEntry`
  (one charge) and :class:`PhaseCostBreakdown` (aggregated rollup)
  as frozen dataclasses with ``project()`` helpers. No enforcement,
  no mutation, no I/O.
* **Rendering helpers.** :func:`render_phase_cost_breakdown` emits
  a terse operator-readable summary — reused by REPL and SSE.
* **Schema versioning.** Breakdown carries ``schema_version``
  (``phase_cost.v1``) so IDE consumers can feature-detect.

What this module is NOT
-----------------------

* A ledger / store. :class:`~cost_governor.CostGovernor` owns the
  per-op ledger; Slice 2 extends it.
* A persistence layer. Slice 3 wires ``summary.json`` through
  ``session_recorder`` + ``session_record``.
* A REPL dispatcher. Slice 4 ships :mod:`cost_repl`.

Authority boundary
------------------

* §1 read-only — frozen dataclasses + projection helpers only.
* §7 fail-closed — missing / malformed data surfaces as empty
  dicts / ``0.0`` totals, never exceptions.
* §8 observable — every breakdown is JSON-safe via ``project()``.
* No imports from orchestrator / policy_engine / iron_gate /
  risk_tier_floor / semantic_guardian / tool_executor /
  candidate_generator / change_engine. Grep-pinned at graduation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

PHASE_COST_SCHEMA_VERSION: str = "phase_cost.v1"


# Canonical phase ordering — matches OperationPhase enum in op_context.py.
# Kept as a module-level constant so consumers don't import the FSM enum.
CANONICAL_PHASE_ORDER: Tuple[str, ...] = (
    "CLASSIFY",
    "ROUTE",
    "CONTEXT_EXPANSION",
    "PLAN",
    "GENERATE",
    "GENERATE_RETRY",
    "VALIDATE",
    "VALIDATE_RETRY",
    "GATE",
    "APPROVE",
    "APPLY",
    "VERIFY",
    "VISUAL_VERIFY",
    "COMPLETE",
    "CANCELLED",
    "EXPIRED",
    "POSTMORTEM",
)


# ---------------------------------------------------------------------------
# PhaseCostEntry — one charge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseCostEntry:
    """Immutable record of one cost charge.

    Entries stream through the governor during the op lifecycle;
    consumers aggregate them into a :class:`PhaseCostBreakdown`.
    """
    op_id: str
    phase: str
    provider: str
    amount_usd: float
    timestamp_mono: float = 0.0

    def project(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "phase": self.phase,
            "provider": self.provider,
            "amount_usd": self.amount_usd,
            "timestamp_mono": self.timestamp_mono,
        }


# ---------------------------------------------------------------------------
# PhaseCostBreakdown — rollup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseCostBreakdown:
    """Aggregated cost rollup for one op.

    Fields::

        op_id                     str
        total_usd                 float — sum across every charge
        by_phase                  {phase: usd}
        by_provider               {provider: usd}
        by_phase_provider         {phase: {provider: usd}}
        call_count                int — total number of charges
        unknown_phase_usd         float — charges with no phase tag
                                  (legacy pre-Slice-2 path or providers
                                  that charge outside a phase context).
    """

    op_id: str
    total_usd: float
    by_phase: Mapping[str, float] = field(default_factory=dict)
    by_provider: Mapping[str, float] = field(default_factory=dict)
    by_phase_provider: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict,
    )
    call_count: int = 0
    unknown_phase_usd: float = 0.0
    schema_version: str = PHASE_COST_SCHEMA_VERSION

    def project(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "total_usd": self.total_usd,
            "by_phase": {k: v for k, v in self.by_phase.items()},
            "by_provider": {k: v for k, v in self.by_provider.items()},
            "by_phase_provider": {
                phase: dict(providers)
                for phase, providers in self.by_phase_provider.items()
            },
            "call_count": self.call_count,
            "unknown_phase_usd": self.unknown_phase_usd,
        }

    @property
    def has_data(self) -> bool:
        return self.total_usd > 0 or self.call_count > 0

    def top_phase(self) -> Optional[Tuple[str, float]]:
        """Return the most expensive ``(phase, usd)`` tuple, or
        ``None`` when no phase data exists."""
        if not self.by_phase:
            return None
        phase, usd = max(self.by_phase.items(), key=lambda kv: kv[1])
        return (phase, usd) if usd > 0 else None


# ---------------------------------------------------------------------------
# Aggregation helpers — pure functions
# ---------------------------------------------------------------------------


def aggregate_entries(
    op_id: str, entries: List[PhaseCostEntry],
) -> PhaseCostBreakdown:
    """Roll a list of :class:`PhaseCostEntry` into a breakdown."""
    total = 0.0
    by_phase: Dict[str, float] = {}
    by_provider: Dict[str, float] = {}
    by_phase_provider: Dict[str, Dict[str, float]] = {}
    unknown_phase = 0.0
    count = 0
    for entry in entries:
        if entry.op_id and entry.op_id != op_id:
            continue
        amount = float(entry.amount_usd or 0.0)
        if amount <= 0.0:
            continue
        total += amount
        count += 1
        phase = (entry.phase or "").strip()
        provider = (entry.provider or "unknown").strip() or "unknown"
        by_provider[provider] = by_provider.get(provider, 0.0) + amount
        if not phase:
            unknown_phase += amount
            continue
        by_phase[phase] = by_phase.get(phase, 0.0) + amount
        by_phase_provider.setdefault(phase, {})
        by_phase_provider[phase][provider] = (
            by_phase_provider[phase].get(provider, 0.0) + amount
        )
    return PhaseCostBreakdown(
        op_id=op_id,
        total_usd=round(total, 6),
        by_phase={k: round(v, 6) for k, v in by_phase.items()},
        by_provider={k: round(v, 6) for k, v in by_provider.items()},
        by_phase_provider={
            phase: {p: round(v, 6) for p, v in providers.items()}
            for phase, providers in by_phase_provider.items()
        },
        call_count=count,
        unknown_phase_usd=round(unknown_phase, 6),
    )


def breakdown_from_mappings(
    op_id: str,
    phase_totals: Mapping[str, float],
    phase_by_provider: Mapping[str, Mapping[str, float]],
    *,
    call_count: int = 0,
    unknown_phase_usd: float = 0.0,
) -> PhaseCostBreakdown:
    """Build a breakdown directly from governor-held rolling sums.

    Used by :mod:`cost_governor` to project its ``_OpCostEntry``
    state without re-iterating raw entry history.
    """
    total = sum(
        float(v) for v in phase_totals.values() if v and v > 0
    ) + float(unknown_phase_usd or 0.0)
    by_provider: Dict[str, float] = {}
    for providers in phase_by_provider.values():
        for provider, usd in providers.items():
            if not usd or usd <= 0:
                continue
            by_provider[provider] = by_provider.get(provider, 0.0) + float(usd)
    return PhaseCostBreakdown(
        op_id=op_id,
        total_usd=round(total, 6),
        by_phase={
            k: round(float(v), 6)
            for k, v in phase_totals.items()
            if v and v > 0
        },
        by_provider={k: round(v, 6) for k, v in by_provider.items()},
        by_phase_provider={
            phase: {
                p: round(float(v), 6) for p, v in providers.items()
                if v and v > 0
            }
            for phase, providers in phase_by_provider.items()
            if providers
        },
        call_count=int(call_count),
        unknown_phase_usd=round(float(unknown_phase_usd or 0.0), 6),
    )


# ---------------------------------------------------------------------------
# Rendering — terse operator-readable summary
# ---------------------------------------------------------------------------


def _phase_sort_key(phase: str) -> Tuple[int, str]:
    """Sort phases in canonical pipeline order; unknown phases last."""
    try:
        return (CANONICAL_PHASE_ORDER.index(phase), phase)
    except ValueError:
        return (len(CANONICAL_PHASE_ORDER), phase)


def render_phase_cost_breakdown(
    breakdown: PhaseCostBreakdown,
    *,
    include_provider_detail: bool = True,
    currency_fmt: str = "${:.4f}",
) -> str:
    """REPL-friendly rendering of a :class:`PhaseCostBreakdown`.

    Example::

        op-019abc...
          total: $0.8234  (calls=12)
          by phase:
            CLASSIFY      $0.0012
            GENERATE      $0.4512  [claude $0.4421, doubleword $0.0091]
            VALIDATE      $0.2210
            VERIFY        $0.1500
          by provider:
            claude        $0.6123
            doubleword    $0.2111
    """
    lines: List[str] = [f"  {breakdown.op_id}"]
    if not breakdown.has_data:
        lines.append("    (no cost data recorded)")
        return "\n".join(lines)
    lines.append(
        f"    total: {currency_fmt.format(breakdown.total_usd)}"
        f"  (calls={breakdown.call_count})"
    )
    if breakdown.by_phase:
        lines.append("    by phase:")
        phases_sorted = sorted(
            breakdown.by_phase.keys(), key=_phase_sort_key,
        )
        for phase in phases_sorted:
            usd = breakdown.by_phase[phase]
            detail = ""
            if (
                include_provider_detail
                and phase in breakdown.by_phase_provider
            ):
                parts = [
                    f"{p} {currency_fmt.format(v)}"
                    for p, v in sorted(
                        breakdown.by_phase_provider[phase].items(),
                        key=lambda kv: (-kv[1], kv[0]),
                    )
                ]
                if parts:
                    detail = "  [" + ", ".join(parts) + "]"
            lines.append(
                f"      {phase:<18} {currency_fmt.format(usd)}{detail}"
            )
    if breakdown.by_provider:
        lines.append("    by provider:")
        for provider, usd in sorted(
            breakdown.by_provider.items(), key=lambda kv: (-kv[1], kv[0]),
        ):
            lines.append(
                f"      {provider:<18} {currency_fmt.format(usd)}"
            )
    if breakdown.unknown_phase_usd > 0:
        lines.append(
            f"    untagged (no phase): "
            f"{currency_fmt.format(breakdown.unknown_phase_usd)}"
        )
    top = breakdown.top_phase()
    if top is not None:
        phase, usd = top
        pct = (
            usd / breakdown.total_usd * 100.0
            if breakdown.total_usd > 0 else 0.0
        )
        lines.append(
            f"    top phase: {phase} "
            f"({currency_fmt.format(usd)}, {pct:.1f}% of total)"
        )
    return "\n".join(lines)


__all__ = [
    "CANONICAL_PHASE_ORDER",
    "PHASE_COST_SCHEMA_VERSION",
    "PhaseCostBreakdown",
    "PhaseCostEntry",
    "aggregate_entries",
    "breakdown_from_mappings",
    "render_phase_cost_breakdown",
]
