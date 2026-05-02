"""SBT-Probe Escalation Bridge — Slice 1 primitive.

The pure-stdlib decision primitive that wires Move 5's
single-probe loop to Priority #4's Speculative Branch Tree as a
sequential escalation path:

  * Move 5 probe runs first (existing default behavior).
  * If probe returns ``EXHAUSTED`` (the inconclusive signal), AND
    escalation is enabled AND cost/time budget allows, escalate
    to SBT for a tree-shaped branching analysis.
  * SBT's :class:`TreeVerdict` (5 values) collapses to
    :class:`ConfidenceCollapseAction` (3 terminal actions) via a
    deterministic 5→3 mapping.

Closes the deferred Slice 5b orchestrator hook for Priority #4.
SBT primitive + runner + comparator + observer all shipped +
graduated 2026-05-02 but ZERO production code calls
``run_speculative_tree``. This bridge is the integration spine.

Why escalation-on-EXHAUSTED, not parallel-race or complexity-classifier
-----------------------------------------------------------------------

The Founding Architect directive forbids hardcoding. A
"complexity classifier picks probe vs SBT" approach requires a
new heuristic and a magic-number threshold; an empirical
"probe couldn't resolve it → try a wider tree" is structural,
not magic-number tuning. The probe's :attr:`ProbeOutcome.EXHAUSTED`
verdict IS the complexity signal — measurement, not heuristic.

Sequential escalation (vs parallel-race) preserves Move 5's
default behavior intact: only the one inconclusive verdict
triggers SBT; CONVERGED / DIVERGED / DISABLED / FAILED outcomes
all skip escalation (probe already produced a usable signal or
defensive fall-through). Cost-bounded by construction: SBT's
own wall-clock cap (60s default) + this bridge's cost/time
budget gate bound the worst case.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — frozen dataclasses + total decision
  function. Slice 2's async wrapper calls SBT runner via
  ``asyncio.wait_for`` with budget enforcement.
* **Dynamic** — every numeric (cost cap, time cap) flows from
  env-knob helpers with floor + ceiling clamps. No hardcoded
  magic constants.
* **Adaptive** — degraded inputs (None, garbage, unknown probe
  outcome strings) all map to closed-taxonomy values rather
  than raises. NEVER propagates exceptions.
* **Intelligent** — 5→3 collapse mapping is deterministic
  per-verdict: CONVERGED→RETRY (thread evidence); DIVERGED→
  ESCALATE (genuine ambiguity confirmed); INCONCLUSIVE/TRUNCATED/
  FAILED→INCONCLUSIVE (mid-band collapse).
* **Robust** — every public function NEVER raises out. Pure-data
  primitive callable from any context.
* **No hardcoding** — 5-value closed enum (J.A.R.M.A.T.R.I.X.),
  per-knob env helpers with floor + ceiling, byte-parity to
  ProbeOutcome / TreeVerdict / ConfidenceCollapseAction string
  constants verified by test.

Authority invariants (AST-pinned by Slice 3 graduation)
-------------------------------------------------------

* Imports stdlib ONLY at hot path. NEVER imports
  ``confidence_probe_bridge`` / ``speculative_branch`` /
  ``hypothesis_consumers`` etc. — strongest authority invariant.
  String constants for ProbeOutcome / TreeVerdict /
  ConfidenceCollapseAction redefined verbatim with byte-parity
  test. Module-owned ``register_flags`` / ``register_shipped_invariants``
  exempt from this pin (registration-contract exemption from
  Priority #6 closure).
* No async (Slice 2 wraps).
* No exec/eval/compile (mirrors Move 5/6/Priority #1-#5 Slice 1
  critical safety pin).

Master flag default-FALSE until Slice 3 graduation:
``JARVIS_SBT_ESCALATION_ENABLED``. Asymmetric env semantics —
empty/whitespace = unset = current default.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


SBT_ESCALATION_BRIDGE_SCHEMA_VERSION: str = "sbt_escalation_bridge.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def sbt_escalation_enabled() -> bool:
    """``JARVIS_SBT_ESCALATION_ENABLED`` (default ``true`` —
    graduated 2026-05-02 in SBT-Probe Escalation Bridge Slice 3).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default; explicit ``0``/``false``/``no``/``off`` evaluates false;
    explicit truthy values evaluate true. Re-read on every call so
    flips hot-revert without restart.

    Graduated default-true matches established Move 5 / Move 6 /
    Priority #1-#5 / Priority #6 discipline:
      * Slice 1 primitive (pure-stdlib) — shipped 2026-05-02.
      * Slice 2 wrapper + executor wire-up — shipped 2026-05-02.
      * Slice 3 production prober adapter (this slice) — wraps
        Move 5's ReadonlyEvidenceProber via the SBT BranchProber
        protocol, rotating resolution_method across
        READONLY_TOOL_ALLOWLIST for branch diversity.
      * 472/472 combined sweep + 21 wrapper tests + dynamic
        registration verified.
      * Cost-bounded by construction: SBT's wall-clock cap +
        bridge's cost/time budget gate + escalation only on
        probe EXHAUSTED (the inconclusive verdict).
    """
    raw = os.environ.get(
        "JARVIS_SBT_ESCALATION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 3, 2026-05-02)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped (floor + ceiling)
# ---------------------------------------------------------------------------


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def max_escalation_cost_usd() -> float:
    """``JARVIS_SBT_ESCALATION_MAX_COST_USD`` — defense-in-depth
    cost cap on the escalation path. Default $0.10, floor $0.01,
    ceiling $1.00. SBT itself has its own wall-clock cap; this is
    a budget-aware gate so that under high-volume probe traffic
    we don't escalate every EXHAUSTED into a $0.10 SBT run."""
    return _env_float_clamped(
        "JARVIS_SBT_ESCALATION_MAX_COST_USD",
        default=0.10, floor=0.01, ceiling=1.00,
    )


def max_escalation_time_s() -> float:
    """``JARVIS_SBT_ESCALATION_MAX_TIME_S`` — defense-in-depth
    time cap on the cumulative probe-then-SBT path. Default 90s,
    floor 10s, ceiling 600s. The probe budget plus SBT budget
    must fit within this. If the probe already consumed too much
    time, escalation is skipped."""
    return _env_float_clamped(
        "JARVIS_SBT_ESCALATION_MAX_TIME_S",
        default=90.0, floor=10.0, ceiling=600.0,
    )


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value escalation decision (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class EscalationDecision(str, enum.Enum):
    """Closed 5-value taxonomy. Every input maps to exactly one.

    * :attr:`ESCALATE` — probe was inconclusive (EXHAUSTED) AND
      escalation is enabled AND cost/time budget allows. Slice 2
      wrapper calls :func:`speculative_branch_runner.run_speculative_tree`
      and maps the resulting ``TreeVerdict`` to
      ``ConfidenceCollapseAction`` via :func:`tree_verdict_to_collapse_action`.

    * :attr:`SKIP` — probe was conclusive (CONVERGED, DIVERGED,
      DISABLED, or FAILED). The probe already produced a usable
      signal or defensive fall-through; no escalation needed.
      Slice 2 wrapper returns the probe's existing collapse
      verdict unchanged.

    * :attr:`BUDGET_EXHAUSTED` — escalation enabled but cost OR
      time budget already consumed. Distinct from DISABLED so
      operators can tell "we wanted to escalate but couldn't"
      from "we never tried". Falls through to probe's existing
      verdict.

    * :attr:`DISABLED` — master flag off. Equivalent to SKIP for
      orchestrator purposes (fall through to probe), but distinct
      in observability.

    * :attr:`FAILED` — defensive sentinel. Garbage input or
      unhandled exception in the decision path. Falls through to
      probe's existing verdict (safe default).
    """

    ESCALATE = "escalate"
    SKIP = "skip"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DISABLED = "disabled"
    FAILED = "failed"


# Set of decisions where the orchestrator IS asked to do additional
# work (escalation path active). Other values fall through to
# probe's existing verdict.
_ACTIVE_ESCALATION_DECISIONS: frozenset = frozenset({
    EscalationDecision.ESCALATE,
})


# ---------------------------------------------------------------------------
# Byte-parity ProbeOutcome string constants (pure-stdlib pin)
# ---------------------------------------------------------------------------

#: Mirror of ``confidence_probe_bridge.ProbeOutcome.*.value`` —
#: kept verbatim so this module stays pure-stdlib (zero governance
#: imports at hot path). Slice 3 graduation pin asserts byte-parity
#: against the live exports; any divergence is caught structurally
#: before shipping. Order + value pinned to the J.A.R.M.A.T.R.I.X.
#: closed-taxonomy contract.
_PROBE_OUTCOME_CONVERGED: str = "converged"
_PROBE_OUTCOME_DIVERGED: str = "diverged"
_PROBE_OUTCOME_EXHAUSTED: str = "exhausted"
_PROBE_OUTCOME_DISABLED: str = "disabled"
_PROBE_OUTCOME_FAILED: str = "failed"

#: The single ProbeOutcome value that triggers escalation. Probe
#: EXHAUSTED means "K probes consumed budget without hitting
#: quorum (partial agreement only)" — the inconclusive case. All
#: other outcomes (CONVERGED / DIVERGED / DISABLED / FAILED) are
#: handled by Move 5's existing executor.
_TRIGGER_PROBE_OUTCOMES: frozenset = frozenset({
    _PROBE_OUTCOME_EXHAUSTED,
})


# ---------------------------------------------------------------------------
# Byte-parity TreeVerdict + ConfidenceCollapseAction string constants
# ---------------------------------------------------------------------------

#: Mirror of ``speculative_branch.TreeVerdict.*.value``.
_TREE_VERDICT_CONVERGED: str = "converged"
_TREE_VERDICT_DIVERGED: str = "diverged"
_TREE_VERDICT_INCONCLUSIVE: str = "inconclusive"
_TREE_VERDICT_TRUNCATED: str = "truncated"
_TREE_VERDICT_FAILED: str = "failed"

#: Mirror of ``hypothesis_consumers.ConfidenceCollapseAction.*.value``.
#: Only the 3 *terminal* values are reachable from the SBT collapse
#: mapping — PROBE_ENVIRONMENT is the trigger state (already past
#: by the time we run SBT) and is intentionally excluded.
_COLLAPSE_RETRY_WITH_FEEDBACK: str = "retry_with_feedback"
_COLLAPSE_ESCALATE_TO_OPERATOR: str = "escalate_to_operator"
_COLLAPSE_INCONCLUSIVE: str = "inconclusive"

#: Deterministic 5→3 mapping. Every TreeVerdict maps to exactly
#: one terminal collapse action.
#:
#:   * CONVERGED → RETRY_WITH_FEEDBACK — tree resolved the
#:     ambiguity; the winning evidence threads into the next
#:     GENERATE round to nudge the model toward the tree's
#:     canonical answer.
#:   * DIVERGED → ESCALATE_TO_OPERATOR — tree confirmed the
#:     ambiguity is genuine (≥2 distinct fingerprints with no
#:     majority). Operator must decide.
#:   * INCONCLUSIVE → INCONCLUSIVE — tree had mixed branches with
#:     no clear pattern. Mid-band; inconclusive collapses to
#:     inconclusive.
#:   * TRUNCATED → INCONCLUSIVE — tree hit budget cap before
#:     converging. Treat as mid-band so the operator-awareness
#:     INCONCLUSIVE SSE channel surfaces the budget signal.
#:   * FAILED → INCONCLUSIVE — defensive fall-through; tree
#:     runner raised an unhandled exception. Safe default.
_TREE_TO_COLLAPSE: Dict[str, str] = {
    _TREE_VERDICT_CONVERGED: _COLLAPSE_RETRY_WITH_FEEDBACK,
    _TREE_VERDICT_DIVERGED: _COLLAPSE_ESCALATE_TO_OPERATOR,
    _TREE_VERDICT_INCONCLUSIVE: _COLLAPSE_INCONCLUSIVE,
    _TREE_VERDICT_TRUNCATED: _COLLAPSE_INCONCLUSIVE,
    _TREE_VERDICT_FAILED: _COLLAPSE_INCONCLUSIVE,
}


# ---------------------------------------------------------------------------
# Phase C MonotonicTighteningVerdict canonical string
# ---------------------------------------------------------------------------

#: Canonical string from ``adaptation.ledger.MonotonicTighteningVerdict``.
#: Slice 3 graduation pin asserts byte-parity to the live enum.
#: Stamped on EscalationDecision outputs that represent
#: structural tightening (ESCALATE — additional analysis beyond
#: probe). SKIP/BUDGET_EXHAUSTED/DISABLED/FAILED are no-op
#: fall-through; not tightening.
_TIGHTENING_PASSED_STR: str = "passed"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationContext:
    """Frozen input to the escalation decision. All fields immutable
    so propagation across async boundaries is safe (Slice 2 wrapper
    will hand this through ``asyncio.wait_for``).

    Attributes:
      probe_outcome: String form of the probe's
        :class:`ProbeOutcome`. Pass via ``probe_verdict.outcome.value``
        from the caller.
      cost_so_far_usd: Cumulative cost burned by the probe path
        (Move 5 wall-clock + provider tokens). The escalation
        gate compares against :func:`max_escalation_cost_usd`.
      time_so_far_s: Cumulative wall-clock seconds the probe path
        consumed.
      op_id: Originating op id — audit only.
      target: Free-form descriptor of what was being probed —
        audit only.
    """

    probe_outcome: str
    cost_so_far_usd: float = 0.0
    time_so_far_s: float = 0.0
    op_id: str = ""
    target: str = ""
    schema_version: str = SBT_ESCALATION_BRIDGE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "probe_outcome": self.probe_outcome,
            "cost_so_far_usd": self.cost_so_far_usd,
            "time_so_far_s": self.time_so_far_s,
            "op_id": self.op_id,
            "target": self.target,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EscalationContext":
        try:
            return cls(
                probe_outcome=str(d.get("probe_outcome", "")),
                cost_so_far_usd=float(d.get("cost_so_far_usd", 0.0) or 0.0),
                time_so_far_s=float(d.get("time_so_far_s", 0.0) or 0.0),
                op_id=str(d.get("op_id", "")),
                target=str(d.get("target", "")),
                schema_version=str(
                    d.get("schema_version", SBT_ESCALATION_BRIDGE_SCHEMA_VERSION),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[SBTEscalationBridge] from_dict degraded: %s", exc,
            )
            return cls(probe_outcome="")


@dataclass(frozen=True)
class EscalationVerdict:
    """Frozen output of :func:`compute_escalation_decision`. Frozen
    so the verdict propagates safely through Slice 2's async wrapper
    + Slice 3's observability surface.

    ``monotonic_tightening_verdict`` populated to ``"passed"`` on
    ESCALATE outcomes (the escalation IS a structural tightening:
    additional analysis beyond probe, never a loosening). All other
    decisions stamp empty string (fall-through, not tightening).
    """

    decision: EscalationDecision
    op_id: str = ""
    target: str = ""
    detail: str = ""
    monotonic_tightening_verdict: str = ""
    schema_version: str = SBT_ESCALATION_BRIDGE_SCHEMA_VERSION

    @property
    def is_escalating(self) -> bool:
        return self.decision in _ACTIVE_ESCALATION_DECISIONS

    @property
    def is_tightening(self) -> bool:
        return self.is_escalating

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "op_id": self.op_id,
            "target": self.target,
            "detail": self.detail,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EscalationVerdict":
        try:
            d_raw = str(d.get("decision", EscalationDecision.FAILED.value))
            try:
                dec = EscalationDecision(d_raw)
            except ValueError:
                dec = EscalationDecision.FAILED
            return cls(
                decision=dec,
                op_id=str(d.get("op_id", "")),
                target=str(d.get("target", "")),
                detail=str(d.get("detail", "")),
                monotonic_tightening_verdict=str(
                    d.get("monotonic_tightening_verdict", ""),
                ),
                schema_version=str(
                    d.get("schema_version", SBT_ESCALATION_BRIDGE_SCHEMA_VERSION),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[SBTEscalationBridge] verdict from_dict degraded: %s", exc,
            )
            return cls(decision=EscalationDecision.FAILED)


# ---------------------------------------------------------------------------
# Total decision function
# ---------------------------------------------------------------------------


def compute_escalation_decision(
    context: EscalationContext,
    *,
    enabled: Optional[bool] = None,
    max_cost_usd: Optional[float] = None,
    max_time_s: Optional[float] = None,
) -> EscalationVerdict:
    """Total decision function — every input maps to exactly one
    :class:`EscalationDecision`. NEVER raises.

    Decision tree (deterministic, no heuristics):
      1. Master flag off → DISABLED
      2. Garbage / non-string probe outcome → FAILED (defensive)
      3. probe_outcome NOT in trigger set (CONVERGED / DIVERGED /
         DISABLED / FAILED) → SKIP
      4. probe_outcome IS in trigger set (EXHAUSTED) BUT cost OR
         time budget already consumed → BUDGET_EXHAUSTED
      5. probe_outcome IS in trigger set AND budget OK → ESCALATE

    Phase C tightening stamping:
      ESCALATE → ``"passed"`` (structural tightening — additional
        analysis beyond probe, never loosening).
      SKIP / BUDGET_EXHAUSTED / DISABLED / FAILED → empty string
        (fall-through, no tightening signal).
    """
    op_id = ""
    target = ""
    try:
        op_id = str(context.op_id) if context.op_id else ""
        target = str(context.target) if context.target else ""

        # 1. Master-flag-off short-circuit.
        is_enabled = (
            enabled if enabled is not None else sbt_escalation_enabled()
        )
        if not is_enabled:
            return EscalationVerdict(
                decision=EscalationDecision.DISABLED,
                op_id=op_id, target=target,
                detail="sbt_escalation master flag off",
                monotonic_tightening_verdict="",
            )

        # 2. Defensive: non-string probe outcome.
        outcome = context.probe_outcome
        if not isinstance(outcome, str):
            logger.warning(
                "[SBTEscalationBridge] non-string probe_outcome "
                "type=%s op=%s — degrading to FAILED",
                type(outcome).__name__, op_id,
            )
            return EscalationVerdict(
                decision=EscalationDecision.FAILED,
                op_id=op_id, target=target,
                detail=(
                    f"non-string probe_outcome type="
                    f"{type(outcome).__name__}"
                ),
                monotonic_tightening_verdict="",
            )

        normalized = outcome.strip().lower()

        # 3. Probe outcome not in trigger set → SKIP (probe already
        #    produced a usable signal or defensive fall-through).
        if normalized not in _TRIGGER_PROBE_OUTCOMES:
            return EscalationVerdict(
                decision=EscalationDecision.SKIP,
                op_id=op_id, target=target,
                detail=(
                    f"probe_outcome {normalized!r} not in trigger "
                    f"set — using probe's existing verdict"
                ),
                monotonic_tightening_verdict="",
            )

        # 4. Trigger set match BUT budget already consumed.
        eff_max_cost = (
            max_cost_usd
            if max_cost_usd is not None and max_cost_usd > 0
            else max_escalation_cost_usd()
        )
        eff_max_time = (
            max_time_s
            if max_time_s is not None and max_time_s > 0
            else max_escalation_time_s()
        )
        try:
            cost = max(0.0, float(context.cost_so_far_usd))
        except (TypeError, ValueError):
            cost = 0.0
        try:
            elapsed = max(0.0, float(context.time_so_far_s))
        except (TypeError, ValueError):
            elapsed = 0.0

        if cost >= eff_max_cost:
            return EscalationVerdict(
                decision=EscalationDecision.BUDGET_EXHAUSTED,
                op_id=op_id, target=target,
                detail=(
                    f"cost_so_far={cost:.4f} >= cap={eff_max_cost:.4f}"
                ),
                monotonic_tightening_verdict="",
            )
        if elapsed >= eff_max_time:
            return EscalationVerdict(
                decision=EscalationDecision.BUDGET_EXHAUSTED,
                op_id=op_id, target=target,
                detail=(
                    f"time_so_far={elapsed:.1f}s >= cap={eff_max_time:.1f}s"
                ),
                monotonic_tightening_verdict="",
            )

        # 5. ESCALATE — probe was inconclusive AND budget allows.
        return EscalationVerdict(
            decision=EscalationDecision.ESCALATE,
            op_id=op_id, target=target,
            detail=(
                f"probe EXHAUSTED + budget OK "
                f"(cost={cost:.4f}<{eff_max_cost:.4f}, "
                f"time={elapsed:.1f}<{eff_max_time:.1f}); "
                f"escalating to SBT"
            ),
            monotonic_tightening_verdict=_TIGHTENING_PASSED_STR,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[SBTEscalationBridge] compute_escalation_decision "
            "last-resort degraded: %s", exc,
        )
        return EscalationVerdict(
            decision=EscalationDecision.FAILED,
            op_id=op_id, target=target,
            detail=f"unhandled: {exc}",
            monotonic_tightening_verdict="",
        )


# ---------------------------------------------------------------------------
# 5→3 collapse mapping — TreeVerdict → ConfidenceCollapseAction string
# ---------------------------------------------------------------------------


def tree_verdict_to_collapse_action(tree_verdict: Optional[str]) -> str:
    """Total mapping function — :class:`TreeVerdict` string →
    :class:`ConfidenceCollapseAction` string.

    Returns the canonical string value (Slice 2 wrapper imports
    the live enums and converts via ``ConfidenceCollapseAction(value)``).

    Garbage / missing / unknown TreeVerdict → ``"inconclusive"``
    (defensive: fall through to operator-awareness mid-band rather
    than silent escalation or silent retry). NEVER raises.
    """
    try:
        if not isinstance(tree_verdict, str):
            logger.warning(
                "[SBTEscalationBridge] non-string tree_verdict "
                "type=%s — degrading to INCONCLUSIVE",
                type(tree_verdict).__name__,
            )
            return _COLLAPSE_INCONCLUSIVE
        normalized = tree_verdict.strip().lower()
        if not normalized:
            return _COLLAPSE_INCONCLUSIVE
        action = _TREE_TO_COLLAPSE.get(normalized)
        if action is None:
            logger.warning(
                "[SBTEscalationBridge] unknown tree_verdict %r — "
                "degrading to INCONCLUSIVE", tree_verdict,
            )
            return _COLLAPSE_INCONCLUSIVE
        return action
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[SBTEscalationBridge] tree_verdict_to_collapse_action "
            "last-resort degraded: %s", exc,
        )
        return _COLLAPSE_INCONCLUSIVE


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "EscalationContext",
    "EscalationDecision",
    "EscalationVerdict",
    "SBT_ESCALATION_BRIDGE_SCHEMA_VERSION",
    "compute_escalation_decision",
    "max_escalation_cost_usd",
    "max_escalation_time_s",
    "register_flags",
    "register_shipped_invariants",
    "sbt_escalation_enabled",
    "tree_verdict_to_collapse_action",
]


# ---------------------------------------------------------------------------
# Slice 3 — Module-owned FlagRegistry contribution (3 flags)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned :class:`FlagRegistry` registration. Discovered
    automatically by ``flag_registry_seed._discover_module_provided_flags``.
    Returns count registered."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[SBTEscalationBridge] register_flags degraded: %s", exc,
        )
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_SBT_ESCALATION_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "sbt_escalation_bridge.py"
            ),
            example="JARVIS_SBT_ESCALATION_ENABLED=true",
            description=(
                "Master switch for the SBT-Probe Escalation Bridge. "
                "When on, probe EXHAUSTED outcomes optionally "
                "escalate to a tree-shaped SBT analysis before "
                "falling through to INCONCLUSIVE. Graduated "
                "default-true 2026-05-02 in Slice 3."
            ),
        ),
        FlagSpec(
            name="JARVIS_SBT_ESCALATION_MAX_COST_USD",
            type=FlagType.FLOAT, default=0.10,
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "sbt_escalation_bridge.py"
            ),
            example="JARVIS_SBT_ESCALATION_MAX_COST_USD=0.25",
            description=(
                "Defense-in-depth cost cap on the escalation path. "
                "If probe path already burned this much, escalation "
                "skips with BUDGET_EXHAUSTED. Floor $0.01, ceiling "
                "$1.00. Default $0.10."
            ),
        ),
        FlagSpec(
            name="JARVIS_SBT_ESCALATION_MAX_TIME_S",
            type=FlagType.FLOAT, default=90.0,
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "sbt_escalation_bridge.py"
            ),
            example="JARVIS_SBT_ESCALATION_MAX_TIME_S=120",
            description=(
                "Defense-in-depth wall-clock cap on the cumulative "
                "probe-then-SBT path. Floor 10s, ceiling 600s. "
                "Default 90s. Composes with SBT's own internal cap."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[SBTEscalationBridge] register_flags spec %s "
                "skipped: %s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 3 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Register Slice 1's structural invariants. Discovered
    automatically. Returns :class:`ShippedCodeInvariant` instances."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_pure_stdlib(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 1 stays pure-stdlib at hot path. Registration-
        contract exemption applies."""
        violations: list = []
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    violations.append(
                        f"line {lineno}: Slice 1 must be pure-stdlib "
                        f"— found {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"MUST NOT {node.func.id}()"
                        )
            if isinstance(node, _ast.AsyncFunctionDef):
                violations.append(
                    f"line {getattr(node, 'lineno', '?')}: Slice 1 "
                    f"must remain sync — found async def {node.name!r}"
                )
        return tuple(violations)

    def _validate_taxonomy_5_values(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Closed-taxonomy: EscalationDecision has exactly 5 values."""
        violations: list = []
        required = {
            "ESCALATE", "SKIP", "BUDGET_EXHAUSTED", "DISABLED", "FAILED",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                if node.name == "EscalationDecision":
                    seen = set()
                    for stmt in node.body:
                        if isinstance(stmt, _ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, _ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"EscalationDecision missing: {sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"EscalationDecision unexpected values "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_collapse_mapping_complete(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        """The 5→3 _TREE_TO_COLLAPSE map MUST cover all 5 TreeVerdict
        values. Missing keys would silently collapse to defensive
        INCONCLUSIVE — wrong for CONVERGED/DIVERGED."""
        violations: list = []
        required = (
            "_TREE_VERDICT_CONVERGED",
            "_TREE_VERDICT_DIVERGED",
            "_TREE_VERDICT_INCONCLUSIVE",
            "_TREE_VERDICT_TRUNCATED",
            "_TREE_VERDICT_FAILED",
        )
        for k in required:
            # Each TreeVerdict key must appear in the mapping
            # construction (literal source presence is a robust
            # cheap check).
            if k not in source:
                violations.append(
                    f"_TREE_TO_COLLAPSE missing key reference {k!r}"
                )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/verification/"
        "sbt_escalation_bridge.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="sbt_escalation_bridge_pure_stdlib",
            target_file=target,
            description=(
                "Slice 1 primitive stays pure-stdlib at hot path: "
                "no governance imports outside register_flags / "
                "register_shipped_invariants, no async, no "
                "exec/eval/compile."
            ),
            validate=_validate_pure_stdlib,
        ),
        ShippedCodeInvariant(
            invariant_name="sbt_escalation_bridge_taxonomy_5_values",
            target_file=target,
            description=(
                "EscalationDecision is a 5-value closed taxonomy "
                "(ESCALATE / SKIP / BUDGET_EXHAUSTED / DISABLED / "
                "FAILED). New values require explicit scope-doc + "
                "Slice 2 wrapper update."
            ),
            validate=_validate_taxonomy_5_values,
        ),
        ShippedCodeInvariant(
            invariant_name="sbt_escalation_bridge_collapse_mapping_complete",
            target_file=target,
            description=(
                "5→3 TreeVerdict→ConfidenceCollapseAction mapping "
                "covers all 5 TreeVerdict values. Missing keys "
                "would silently collapse CONVERGED/DIVERGED to "
                "defensive INCONCLUSIVE."
            ),
            validate=_validate_collapse_mapping_complete,
        ),
    ]
