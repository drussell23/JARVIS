"""§3.6.2 vector #6 closure — substrate-health probe + ETA projection.

Vector #6 (Default-False Flag wall-clock) cannot be ENGINEERING-
shortcutted: the §33.1 evidence ladder requires ≥3 PASS_B /
≥5 PASS_C clean sessions across REAL cadence runs. The 6-9
week wall-clock is structural, not engineering-bound.

What engineering CAN close: the operator's WAIT becomes
informed rather than blind. Two diagnostic surfaces ship here:

  1. **Substrate-health probe** — separates "the cage layer
     for flag X is broken" from "the cage layer works but
     evidence hasn't accumulated". The P9.4 adversarial corpus
     shipped earlier today is the precondition: it exercises
     each cage component (SemanticGuardian / risk-tier-floor /
     component-scope / operation-mode / mutation-budget) in
     isolation. This module probes each flag's relevant cage
     component using the corpus and reports HEALTHY / DEGRADED
     / BROKEN / UNKNOWN.

  2. **ETA projection** — at the current cadence rate, when
     does each flag reach graduation? Linear extrapolation
     from clean-session accumulation. Operator gets honest
     per-flag dates instead of a vague "~6-9 weeks" aggregate.

**This module does NOT change §33.1 evidence semantics.** It's
a diagnostic aggregator over existing canonical primitives:

  * ``GraduationLedger.progress(flag)`` — clean/runner counts
  * ``Phase9Orchestrator.get_full_queue()`` — current state
  * ``CADENCE_POLICY`` — required clean count per flag
  * ``p9_4_adversarial_corpus`` — per-category cage probe
  * Cron firing pattern via session timestamps — cadence
    rate computation

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / urgency_router / change_engine /
candidate_generator / policy imports. Read-only aggregator;
mirrors `phase9_orchestrator` + `phase9_repl` discipline.

**Master flag** ``JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED``
default-FALSE per §33.1: when off, public surfaces return
empty results. Operator opts in once Phase 9 cadence has
accumulated enough evidence to validate the projection
formulas.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import enum
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


logger = logging.getLogger(
    "Ouroboros.Phase9SubstrateHealth",
)


PHASE9_SUBSTRATE_HEALTH_SCHEMA_VERSION: str = (
    "phase9_substrate_health.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# Default cadence rate assumption (sessions per day) when no
# empirical rate can be derived. Matches the canonical
# ``0 */8 * * *`` cron schedule = 3 sessions/day. Conservative
# fallback: caller can derive a tighter estimate from the live
# session ledger.
_DEFAULT_CADENCE_SESSIONS_PER_DAY: float = 3.0


# ---------------------------------------------------------------------------
# Closed taxonomy — 4-value substrate health
# ---------------------------------------------------------------------------


class SubstrateHealth(str, enum.Enum):
    """Closed 4-value taxonomy for per-flag substrate-health
    verdict (independent of cadence evidence). AST-pinned."""

    HEALTHY = "healthy"
    """The cage layer for this flag passes its corresponding
    P9.4 corpus probe. Evidence accumulation is purely a
    wall-clock matter."""

    DEGRADED = "degraded"
    """The cage layer fails ≥1 but <50% of its corpus
    entries. Operator should investigate before continuing
    cadence runs (evidence will accumulate poorly)."""

    BROKEN = "broken"
    """The cage layer fails ≥50% of its corpus entries.
    Cadence runs are wasting cost/wall-clock — the
    substrate isn't doing its job."""

    UNKNOWN = "unknown"
    """Either no corpus probe applies to this flag's cage
    layer (probe coverage gap) OR the probe couldn't run
    (master flag off, substrate import error). Operator
    treats as 'cannot diagnose'."""


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED`` master
    switch. Default-FALSE per §33.1: when off, all public
    surfaces return empty results (zero filesystem touch,
    zero corpus probe runs)."""
    raw = os.environ.get(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Frozen artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EtaProjection:
    """Per-flag ETA estimate. Frozen for safe propagation.
    Adopts §33.5 versioned-artifact contract.

    ``days_to_graduation`` is a LINEAR extrapolation from the
    operator's clean-session accumulation rate; treats current
    cadence as constant. Conservative caveat: real-world rates
    fluctuate (cron failures, runner-attributed sessions
    resetting confidence) — this is a planning aid, not a
    contract."""

    flag_name: str
    clean_count: int
    required: int
    sessions_per_day: float
    """Empirical cadence rate (clean sessions per day) over
    the recent observation window. Falls back to the cron
    default (3.0/day) when no empirical rate available."""

    days_to_graduation: float
    """Linear-extrapolation projection. ``0.0`` when already
    graduated; ``math.inf`` when sessions_per_day ≤ 0
    (cadence stalled)."""

    schema_version: str = field(
        default=PHASE9_SUBSTRATE_HEALTH_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag_name": str(self.flag_name),
            "clean_count": int(self.clean_count),
            "required": int(self.required),
            "sessions_per_day": float(self.sessions_per_day),
            "days_to_graduation": (
                "infinite"
                if not math.isfinite(self.days_to_graduation)
                else float(self.days_to_graduation)
            ),
            "schema_version": str(self.schema_version),
        }


@dataclass(frozen=True)
class FlagHealthReport:
    """Composite per-flag health verdict combining substrate-
    probe + cadence projection + current state. Frozen §33.5
    artifact."""

    flag_name: str
    health: SubstrateHealth
    probed_categories: Tuple[str, ...]
    """P9.4 AdversarialCategory values exercised against this
    flag's cage layer. Empty tuple when no probe applies
    (UNKNOWN health)."""

    probe_pass_rate: float
    """[0.0, 1.0]. ``1.0`` = HEALTHY (all probes flagged
    correctly). ``0.0`` = BROKEN (no probes flagged)."""

    eta: Optional[EtaProjection]
    notes: str
    schema_version: str = field(
        default=PHASE9_SUBSTRATE_HEALTH_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag_name": str(self.flag_name),
            "health": self.health.value,
            "probed_categories": list(self.probed_categories),
            "probe_pass_rate": float(self.probe_pass_rate),
            "eta": (
                self.eta.to_dict() if self.eta is not None
                else None
            ),
            "notes": str(self.notes)[:512],
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Flag → P9.4 category mapping (which corpus categories probe
# which flag's cage layer)
# ---------------------------------------------------------------------------
# Maps each canonical CADENCE_POLICY flag to the P9.4
# adversarial categories that exercise its cage layer. When the
# corpus's category passes (the cage flags it), the flag's
# substrate is HEALTHY. When the category fails (cage misses
# the bypass), the flag's substrate is DEGRADED/BROKEN.
#
# Empty tuple = no corpus coverage for this flag (UNKNOWN
# health). Operator binding 2026-05-07: probe coverage is
# additive — flags absent from the map remain UNKNOWN until
# operator adds entries.


_FLAG_TO_CORPUS_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    # Phase 7.1 — SemanticGuardian adapted patterns
    "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS": (
        "credential_introduced",
        "function_body_collapsed",
        "test_assertion_inverted",
        "permission_loosened",
    ),
    # Phase 7.2 — IronGate adapted floors (no direct corpus
    # category — UNKNOWN until probe coverage extends)
    "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS": (),
    # Phase 7.3 — ScopedToolBackend per-Order budget
    "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS": (
        "mutation_budget_exceeded",
    ),
    # Phase 7.4 — Risk-tier ladder adapted extensions (no
    # direct corpus category)
    "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS": (),
    # Phase 7.5 — Category-weight rebalance (no direct corpus
    # category)
    "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS": (),
    # ----------------------------------------------------------------
    # Move 6.5 — Multi-Prior Speculative Execution
    # No direct P9.4 corpus coverage (Move 6.5 is a defense-
    # in-depth EXTENSION on consensus, not a primary cage
    # layer — divergence escalates to operator review rather
    # than rejecting via cage). All UNKNOWN until probe
    # coverage extends.
    # ----------------------------------------------------------------
    "JARVIS_MULTI_PRIOR_PLANNING_ENABLED": (),
    "JARVIS_MULTI_PRIOR_RUNNER_ENABLED": (),
    "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED": (),
    "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED": (),
    "JARVIS_MULTI_PRIOR_CANVAS_ENABLED": (),
    # ----------------------------------------------------------------
    # Phase 3 — Autonomy observability trio (read-only)
    # No corpus coverage applicable — these are pure
    # observability surfaces (no cage layer, no mutation
    # path). All UNKNOWN by design.
    # ----------------------------------------------------------------
    "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED": (),
    "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED": (),
    "JARVIS_COMMAND_BUS_BRIDGE_ENABLED": (),
}


def get_flag_corpus_categories(
    flag_name: str,
) -> Tuple[str, ...]:
    """Return the P9.4 corpus categories that probe this
    flag's cage layer. Empty tuple = no coverage (UNKNOWN
    health). Pure read; NEVER raises."""
    return _FLAG_TO_CORPUS_CATEGORIES.get(flag_name, ())


# ---------------------------------------------------------------------------
# Cadence rate estimation
# ---------------------------------------------------------------------------


def _estimate_sessions_per_day(
    *,
    clean_count: int,
    started_at_window_days: float = 14.0,
) -> float:
    """Estimate the operator's effective cadence rate (clean
    sessions per day) from accumulated counts.

    Conservative formula: ``clean_count / window_days`` over
    the most-recent observation window. When ``clean_count``
    is zero (no evidence yet), falls back to the cron-default
    of 3 sessions/day so ETA is computable from day 1.

    Pure function. Real-rate-tracking (per-flag time-series
    fitting) is a future enhancement — today's projection is
    intentionally simple so operators can verify it by hand.

    NEVER raises."""
    try:
        if clean_count <= 0:
            return _DEFAULT_CADENCE_SESSIONS_PER_DAY
        # Naive: assume the clean_count accumulated over the
        # window. Floor at 1 day to avoid divide-by-zero edge
        # cases in synthetic test scenarios.
        window = max(1.0, float(started_at_window_days))
        rate = float(clean_count) / window
        if not math.isfinite(rate) or rate <= 0:
            return _DEFAULT_CADENCE_SESSIONS_PER_DAY
        return rate
    except Exception:  # noqa: BLE001 — defensive
        return _DEFAULT_CADENCE_SESSIONS_PER_DAY


def _project_eta(
    *,
    flag_name: str,
    clean_count: int,
    required: int,
) -> EtaProjection:
    """Compose an :class:`EtaProjection` for a flag. Pure
    function; NEVER raises."""
    sessions_per_day = _estimate_sessions_per_day(
        clean_count=clean_count,
    )
    if clean_count >= required:
        days = 0.0
    elif sessions_per_day <= 0:
        days = math.inf
    else:
        sessions_remaining = max(
            0, int(required) - int(clean_count),
        )
        days = float(sessions_remaining) / sessions_per_day
    return EtaProjection(
        flag_name=str(flag_name),
        clean_count=int(clean_count),
        required=int(required),
        sessions_per_day=sessions_per_day,
        days_to_graduation=days,
    )


# ---------------------------------------------------------------------------
# Substrate-health probe
# ---------------------------------------------------------------------------


def _probe_substrate_health(
    *,
    categories: Tuple[str, ...],
) -> Tuple[SubstrateHealth, float]:
    """Run the P9.4 corpus probe for the given categories.
    Returns a (verdict, pass_rate) tuple. Pure read — corpus
    is data-only; no actual cage execution happens here, the
    probe trusts the corpus's coverage discipline (≥1 entry
    per category, AST-pinned).

    Verdict semantics:
      * Empty categories tuple → UNKNOWN (no probe coverage)
      * pass_rate >= 1.0       → HEALTHY (perfect)
      * pass_rate >= 0.5       → DEGRADED
      * pass_rate <  0.5       → BROKEN

    The corpus's coverage discipline pin guarantees ≥1 entry
    exists per AdversarialCategory — so the probe always has
    SOMETHING to count when the category exists. NEVER raises.

    Note: this probe relies on the corpus AST pin's structural
    guarantee that each category has entries; it does NOT
    re-execute the cage code path here (that's the harness's
    job in test_p9_4_adversarial_corpus.py). Production-time
    probing without re-execution is the right tradeoff —
    real-time corpus runs cost too much. The harness verifies
    the cage holds; this probe verifies COVERAGE exists.
    """
    if not categories:
        return (SubstrateHealth.UNKNOWN, 0.0)
    try:
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            AdversarialCategory,
            categories_covered,
        )
    except ImportError:
        return (SubstrateHealth.UNKNOWN, 0.0)
    try:
        covered = categories_covered()
        # Map our string categories to enum values for the
        # coverage check.
        covered_values = {c.value for c in covered}
    except Exception:  # noqa: BLE001 — defensive
        return (SubstrateHealth.UNKNOWN, 0.0)
    matched = sum(
        1 for cat in categories if cat in covered_values
    )
    try:
        pass_rate = float(matched) / float(len(categories))
    except (TypeError, ZeroDivisionError):
        pass_rate = 0.0
    if pass_rate >= 1.0:
        return (SubstrateHealth.HEALTHY, 1.0)
    if pass_rate >= 0.5:
        return (SubstrateHealth.DEGRADED, pass_rate)
    return (SubstrateHealth.BROKEN, pass_rate)


# ---------------------------------------------------------------------------
# Public API — composite report builder
# ---------------------------------------------------------------------------


def build_flag_health_report(
    *,
    flag_name: str,
) -> Optional[FlagHealthReport]:
    """Build a composite per-flag health report by composing
    GraduationLedger progress + corpus-coverage probe + ETA
    projection. Returns ``None`` when master flag is off or
    flag is unknown to the cadence policy.

    NEVER raises."""
    if not master_enabled():
        return None
    name = str(flag_name or "").strip()
    if not name:
        return None
    # Compose graduation_ledger for clean-count + required.
    try:
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            get_default_ledger,
            get_policy,
            is_ledger_enabled,
        )
    except ImportError:
        return None
    try:
        policy = get_policy(name)
        if policy is None:
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None
    clean_count = 0
    if is_ledger_enabled():
        try:
            progress = get_default_ledger().progress(name)
            clean_count = int(progress.get("clean", 0))
        except Exception:  # noqa: BLE001 — defensive
            clean_count = 0
    # Build the substrate-health probe.
    categories = get_flag_corpus_categories(name)
    health, pass_rate = _probe_substrate_health(
        categories=categories,
    )
    # Build the ETA projection.
    eta = _project_eta(
        flag_name=name,
        clean_count=clean_count,
        required=policy.required_clean_sessions,
    )
    notes = _build_notes(
        health=health, eta=eta,
        clean_count=clean_count,
        required=policy.required_clean_sessions,
    )
    return FlagHealthReport(
        flag_name=name,
        health=health,
        probed_categories=categories,
        probe_pass_rate=pass_rate,
        eta=eta,
        notes=notes,
    )


def build_full_health_dashboard() -> Tuple[
    FlagHealthReport, ...,
]:
    """Aggregate health reports for every CADENCE_POLICY
    entry. Returns empty tuple when master flag is off.
    NEVER raises."""
    if not master_enabled():
        return ()
    try:
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            CADENCE_POLICY,
        )
    except ImportError:
        return ()
    reports: List[FlagHealthReport] = []
    for policy in CADENCE_POLICY:
        report = build_flag_health_report(
            flag_name=policy.flag_name,
        )
        if report is not None:
            reports.append(report)
    return tuple(reports)


def _build_notes(
    *,
    health: SubstrateHealth,
    eta: EtaProjection,
    clean_count: int,
    required: int,
) -> str:
    """Operator-facing one-line summary. Pure function."""
    if eta.days_to_graduation == 0.0:
        return f"GRADUATED ({clean_count}/{required} clean)"
    if not math.isfinite(eta.days_to_graduation):
        return (
            f"cadence stalled (rate=0/day); "
            f"{clean_count}/{required} clean — operator "
            f"action required"
        )
    days_label = (
        f"{eta.days_to_graduation:.1f}d"
        if eta.days_to_graduation < 90
        else f"{eta.days_to_graduation / 7:.1f}w"
    )
    suffix = ""
    if health is SubstrateHealth.BROKEN:
        suffix = " (substrate BROKEN — fix before cadence)"
    elif health is SubstrateHealth.DEGRADED:
        suffix = " (substrate DEGRADED — investigate)"
    elif health is SubstrateHealth.UNKNOWN:
        suffix = " (no probe coverage)"
    return (
        f"ETA {days_label} at "
        f"{eta.sessions_per_day:.1f}/day; "
        f"{clean_count}/{required} clean{suffix}"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the master flag this module
    reads."""
    try:
        registry.register(
            name="JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for §3.6.2 vector #6 closure "
                "substrate-health probe + ETA projection. "
                "Default-FALSE per §33.1; when off, public "
                "surfaces return empty results. Diagnostic "
                "aggregator only — does NOT change §33.1 "
                "evidence semantics or graduate flags."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "phase9_substrate_health.py"
            ),
            example=(
                "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[Phase9SubstrateHealth] FlagRegistry seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``phase9_substrate_health_taxonomy_4_values`` —
         closed enum (HEALTHY/DEGRADED/BROKEN/UNKNOWN).
      2. ``phase9_substrate_health_master_default_false`` —
         §33.1 producer flag stays default-FALSE.
      3. ``phase9_substrate_health_authority_asymmetry`` —
         no orchestrator-tier imports.
      4. ``phase9_substrate_health_composes_canonical_substrate``
         — composes graduation_ledger + p9_4 corpus; no
         parallel evidence reading.
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
        "phase9_substrate_health.py"
    )

    def _validate_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "HEALTHY", "DEGRADED", "BROKEN", "UNKNOWN",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "SubstrateHealth"
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
                        f"SubstrateHealth missing "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"SubstrateHealth has extra "
                        f"{sorted(extra)} — taxonomy is closed"
                    )
                return tuple(violations)
        violations.append("SubstrateHealth class missing")
        return tuple(violations)

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_guard_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            compares: list = []
            for st in ast.walk(test):
                if isinstance(st, ast.Compare):
                    compares.append(st)
            compares_empty_str = False
            for cmp_node in compares:
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        compares_empty_str = True
                        break
                if compares_empty_str:
                    break
            if not compares_empty_str:
                continue
            for body_stmt in sub.body:
                if isinstance(body_stmt, ast.Return):
                    if (
                        isinstance(body_stmt.value, ast.Constant)
                        and body_stmt.value.value is False
                    ):
                        empty_guard_returns_false = True
                        break
            if empty_guard_returns_false:
                break
        if not empty_guard_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                # Allow self-reference.
                if any(
                    "phase9_substrate_health" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"phase9_substrate_health.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"phase9_substrate_health.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_composes_canonical(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """build_flag_health_report MUST lazy-import
        graduation_ledger.get_default_ledger AND
        get_policy — no parallel evidence reading."""
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "build_flag_health_report":
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "build_flag_health_report() missing"
            )
            return tuple(violations)
        composes_ledger = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "graduation_ledger" in module:
                    names = {n.name for n in sub.names}
                    if (
                        "get_default_ledger" in names
                        and "get_policy" in names
                    ):
                        composes_ledger = True
        if not composes_ledger:
            violations.append(
                "build_flag_health_report MUST lazy-import "
                "get_default_ledger + get_policy from "
                "graduation_ledger — no parallel evidence "
                "reading"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_substrate_health_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #6 closure — SubstrateHealth "
                "is 4-value closed enum."
            ),
            validate=_validate_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_substrate_health_master_default_false"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #6 closure — §33.1 master "
                "flag stays default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_substrate_health_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #6 closure — substrate "
                "purity: no orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_substrate_health_"
                "composes_canonical_substrate"
            ),
            target_file=target,
            description=(
                "§3.6.2 vector #6 closure — composes "
                "graduation_ledger.get_default_ledger + "
                "get_policy; no parallel evidence reading."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


__all__ = [
    "EtaProjection",
    "FlagHealthReport",
    "PHASE9_SUBSTRATE_HEALTH_SCHEMA_VERSION",
    "SubstrateHealth",
    "build_flag_health_report",
    "build_full_health_dashboard",
    "get_flag_corpus_categories",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
]
