"""Unified Graduation Dashboard (PRD §35, 2026-05-07).

Single operator-facing surface aggregating ALL graduation gates
across the codebase into one queryable snapshot:

  * 32-flag CADENCE_POLICY ledger (Phase 9 wall-clock soak
    evidence — :mod:`graduation_ledger`)
  * 8 §33.1 graduation contracts:
      - :mod:`phase10_graduation_contract` (Phase 10 PURGE)
      - :mod:`cross_op_semantic_budget_graduation_contract`
        (Move 7)
      - :mod:`proactive_curiosity_loop_graduation_contract`
        (Move 8)
      - :mod:`causality_consumer_graduation_contract`
      - :mod:`tool_confidence_indicator_graduation_contract`
      - :mod:`tool_hooks_graduation_contract`
      - :mod:`tool_permissions_graduation_contract`
      - :mod:`verification.multi_prior_graduation_contract`
        (Move 6.5)

## Why this exists

Today the operator queries 9 separate surfaces to answer the
question "what is graduation-ready RIGHT NOW?". This dashboard
gives ONE query that aggregates them. It does NOT bypass any
contract; it is a pure read-only composer.

Per the operator binding (§29.4 line 3611): "no workarounds, no
brute force, no shortcut solutions". The dashboard composes
existing canonical contracts via lazy-import — it NEVER
re-implements any predicate. It NEVER mutates ledger state. It
NEVER fabricates evidence. When a contract requires injected
evidence the dashboard cannot provide, the row carries an
explicit `dashboard_diagnostic` so the operator knows the row
is partial.

## Architectural locks (operator mandate, AST-pinned)

  1. **Master flag default-FALSE** per §33.1. Bytes-pinned via
     AST regression — a premature ``return True`` flip fires
     the synthetic regression test.
  2. **Authority asymmetry** — imports stdlib + meta/ +
     adaptation/graduation_ledger + 8 graduation contracts
     ONLY. NEVER imports orchestrator / iron_gate / policy /
     providers / candidate_generator / change_engine /
     semantic_guardian (the substrate cage applies recursively
     to aggregators).
  3. **Composes canonical contracts** — each row's verdict
     comes from invoking the canonical predicate via lazy-
     import. NO parallel reasoning about graduation readiness
     anywhere in this module. AST-pinned.
  4. **Read-only** — dashboard observes; never mutates ledger
     state. Forbids ``record_*`` / ``write_*`` / ``set_*`` /
     ``update_*`` calls into any composed contract surface.
     AST-pinned.
  5. **Verdict taxonomy 5-values** — closed enum
     :class:`UnifiedGraduationVerdict`. New values require
     explicit scope-doc + pin update.

## Verdict ladder (unified)

  * ``READY`` — composed contract reports its READY value
    (READY_FOR_GRADUATION / READY_FOR_PURGE) AND, for ledger-
    backed flags, ``eligible_flags()`` includes it.
  * ``EVIDENCE_GATHERING`` — substrate is wired and producing
    evidence but threshold not yet reached (insufficient op
    samples / insufficient emissions / insufficient
    transitions / clean count below required).
  * ``EVIDENCE_INSUFFICIENT`` — substrate is unwired or
    inactive (producer inactive / missing queue evidence /
    missing recovery evidence). Distinct from
    EVIDENCE_GATHERING because the fix is to wire the
    producer, not to wait for more time.
  * ``EVIDENCE_FAILED`` — observed signal says do NOT graduate
    (excessive drift / excessive throttles / excessive false
    positives / excessive denies / excessive failures /
    excessive disabled samples / excessive non-actionable
    rate / runner-class failures in ledger).
  * ``DISABLED`` — contract harness master flag off, OR (for
    ledger-backed flags) ledger master flag off.

## Versioning

Every artifact carries ``schema_version`` per §33.5. Bumping
the contract requires explicit scope-doc + pin update.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


UNIFIED_GRADUATION_DASHBOARD_SCHEMA_VERSION: str = (
    "unified_graduation_dashboard.1"
)


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def is_dashboard_enabled() -> bool:
    """Master flag — ``JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED``
    (default ``false``).

    Default-FALSE per §33.1 — the dashboard is queryable in shadow
    mode (the function still returns a snapshot when called via
    the ``aggregate_dashboard`` API), but the REPL surface and
    audit-ledger writes are gated. Flip default-true after
    operator validates the aggregation matches per-contract
    queries on a live session."""
    return os.environ.get(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "",
    ).strip().lower() in _TRUTHY


def audit_ledger_path() -> Path:
    """JSONL audit ledger path — env-overridable via
    ``JARVIS_UNIFIED_GRADUATION_DASHBOARD_LEDGER_PATH``."""
    raw = os.environ.get(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_LEDGER_PATH", "",
    ).strip()
    if raw:
        return Path(raw).expanduser()
    return Path(".jarvis/unified_graduation_dashboard.jsonl")


# ---------------------------------------------------------------------------
# Closed verdict taxonomy (5 values, AST-pinned)
# ---------------------------------------------------------------------------


class UnifiedGraduationVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST regression.

    Maps the 8 distinct contract verdicts + ledger states into
    one operator-facing taxonomy so the dashboard renders
    uniformly. Mapping per :func:`_normalize_contract_verdict`
    + :func:`_normalize_ledger_state`."""

    READY = "ready"
    EVIDENCE_GATHERING = "evidence_gathering"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    EVIDENCE_FAILED = "evidence_failed"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Verdict mapping tables
# ---------------------------------------------------------------------------
#
# Per-contract verdict-string → unified verdict. All 8 contracts
# follow the canonical §33.1 5-value shape (READY / INSUFFICIENT /
# EXCESSIVE / ALREADY_GRADUATED / DISABLED), plus phase10's queue
# variants. Mapping is exhaustive: any verdict string not in this
# table is treated as ``EVIDENCE_INSUFFICIENT`` with a diagnostic
# (so a future contract verdict added without pin update is
# visible at runtime, not silently absorbed).

_READY_VERDICTS: FrozenSet[str] = frozenset({
    "ready_for_graduation",
    "ready_for_purge",
    "already_graduated",  # already-graduated rolls up to READY
})

_GATHERING_VERDICTS: FrozenSet[str] = frozenset({
    "insufficient_op_samples",
    "insufficient_emissions",
    "insufficient_observations",
    "insufficient_evaluations",
    "insufficient_fires",
    "insufficient_transitions",
    "insufficient_sessions",
})

_INSUFFICIENT_VERDICTS: FrozenSet[str] = frozenset({
    "producer_inactive",
    "missing_queue_evidence",
    "missing_recovery_evidence",
})

_FAILED_VERDICTS: FrozenSet[str] = frozenset({
    "excessive_drift_detected",
    "excessive_throttles",
    "excessive_false_positives",
    "excessive_failures",
    "excessive_denies",
    "excessive_non_actionable_rate",
    "excessive_disabled_samples",
})

_DISABLED_VERDICTS: FrozenSet[str] = frozenset({
    "disabled",
})


def _normalize_contract_verdict(
    verdict_str: str,
) -> Tuple[UnifiedGraduationVerdict, str]:
    """Map a contract's verdict string → unified verdict +
    diagnostic. NEVER raises."""
    s = (verdict_str or "").strip().lower()
    if s in _READY_VERDICTS:
        return (UnifiedGraduationVerdict.READY, s)
    if s in _GATHERING_VERDICTS:
        return (UnifiedGraduationVerdict.EVIDENCE_GATHERING, s)
    if s in _INSUFFICIENT_VERDICTS:
        return (UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT, s)
    if s in _FAILED_VERDICTS:
        return (UnifiedGraduationVerdict.EVIDENCE_FAILED, s)
    if s in _DISABLED_VERDICTS:
        return (UnifiedGraduationVerdict.DISABLED, s)
    return (
        UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
        f"unknown_verdict:{s[:60]}",
    )


def _normalize_ledger_state(
    progress: Mapping[str, int],
    *,
    is_eligible: bool,
    ledger_master_on: bool,
) -> Tuple[UnifiedGraduationVerdict, str]:
    """Map ledger progress dict → unified verdict + diagnostic.

    Composes :meth:`GraduationLedger.is_eligible` for the READY
    path; the diagnostic always reflects raw counts so the
    operator sees the empirical state."""
    if not ledger_master_on:
        return (
            UnifiedGraduationVerdict.DISABLED,
            "ledger_master_off",
        )
    clean = int(progress.get("clean", 0))
    runner = int(progress.get("runner", 0))
    required = int(progress.get("required", 3))
    diag = f"clean={clean}/{required} runner={runner}"
    if is_eligible:
        return (UnifiedGraduationVerdict.READY, diag)
    if runner > 0:
        return (UnifiedGraduationVerdict.EVIDENCE_FAILED, diag)
    if clean < required:
        return (UnifiedGraduationVerdict.EVIDENCE_GATHERING, diag)
    # Defensive: clean >= required AND runner == 0 should be
    # eligible; if we got here the eligibility check disagreed
    # — surface as INSUFFICIENT with diagnostic so the
    # operator can investigate.
    return (
        UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
        f"{diag} eligibility_mismatch",
    )


# ---------------------------------------------------------------------------
# Versioned artifacts (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DashboardRow:
    """One graduation gate's snapshot. Frozen for safe
    propagation. ``source`` distinguishes ``ledger`` (Phase 9
    cadence) from ``contract`` (§33.1)."""

    schema_version: str = (
        UNIFIED_GRADUATION_DASHBOARD_SCHEMA_VERSION
    )
    name: str = ""
    source: str = ""  # "ledger" | "contract"
    verdict: UnifiedGraduationVerdict = (
        UnifiedGraduationVerdict.DISABLED
    )
    raw_verdict: str = ""
    diagnostic: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """§33.5 symmetric projection. NEVER raises."""
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "source": self.source,
            "verdict": self.verdict.value,
            "raw_verdict": self.raw_verdict,
            "diagnostic": self.diagnostic,
            "elapsed_s": float(self.elapsed_s),
        }


@dataclass(frozen=True)
class DashboardSnapshot:
    """Aggregate snapshot across all gates. Frozen."""

    schema_version: str = (
        UNIFIED_GRADUATION_DASHBOARD_SCHEMA_VERSION
    )
    aggregated_at_unix: float = 0.0
    rows: Tuple[DashboardRow, ...] = field(default_factory=tuple)
    elapsed_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """§33.5 symmetric projection. NEVER raises."""
        return {
            "schema_version": self.schema_version,
            "aggregated_at_unix": float(self.aggregated_at_unix),
            "rows": [r.to_dict() for r in self.rows],
            "elapsed_s": float(self.elapsed_s),
            "summary": self.summary(),
        }

    def summary(self) -> Dict[str, int]:
        """Per-verdict count summary. Useful for `/graduation`
        operator surface header."""
        out: Dict[str, int] = {
            v.value: 0 for v in UnifiedGraduationVerdict
        }
        for r in self.rows:
            out[r.verdict.value] = out.get(r.verdict.value, 0) + 1
        return out

    def ready_rows(self) -> Tuple[DashboardRow, ...]:
        return tuple(
            r for r in self.rows
            if r.verdict == UnifiedGraduationVerdict.READY
        )

    def failed_rows(self) -> Tuple[DashboardRow, ...]:
        return tuple(
            r for r in self.rows
            if r.verdict == UnifiedGraduationVerdict.EVIDENCE_FAILED
        )


# ---------------------------------------------------------------------------
# Per-contract adapters (lazy-import canonical predicates)
# ---------------------------------------------------------------------------
#
# Each adapter:
#   1. Lazy-imports the canonical contract module
#   2. Calls the canonical predicate with safe-default args
#   3. Maps the report's verdict.value → unified verdict
#   4. NEVER raises — every fault routes to a defensive row
#
# Curiosity is the OUTLIER — its predicate requires injected
# evidence (observed_surfaced_emissions, observed_governor_-
# throttles). Dashboard adapter passes 0/0 + a dashboard
# diagnostic noting the partial state. The honest framing:
# until evidence wiring is built, the row reports the contract's
# verdict for the zero-evidence case (typically
# INSUFFICIENT_EMISSIONS) AND the dashboard explicitly notes
# this is the dashboard's degradation, not a real signal.


def _adapter_phase10() -> DashboardRow:
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
            is_ready_for_purge,
        )
        report = is_ready_for_purge()
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        return DashboardRow(
            name="phase10_purge",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="phase10_purge",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


def _adapter_cross_op_semantic_budget() -> DashboardRow:
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
            is_ready_for_graduation,
        )
        report = is_ready_for_graduation()
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        return DashboardRow(
            name="cross_op_semantic_budget",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="cross_op_semantic_budget",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


def _adapter_proactive_curiosity_loop() -> DashboardRow:
    """Curiosity contract requires injected evidence
    (observed_surfaced_emissions, observed_governor_throttles).
    Dashboard adapter passes 0/0 with explicit diagnostic.
    Future arc may compose firing_telemetry + sensor_governor
    to provide live evidence — until then, this row is
    structurally INSUFFICIENT_EMISSIONS by construction."""
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.proactive_curiosity_loop_graduation_contract import (  # noqa: E501
            is_ready_for_graduation,
        )
        report = is_ready_for_graduation(
            observed_surfaced_emissions=0,
            observed_governor_throttles=0,
        )
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        # Append dashboard-side diagnostic explaining the row
        # reflects the zero-evidence path, not a live measurement.
        diag = (
            f"{diag} | dashboard_note=evidence_reader_not_wired"
        )
        return DashboardRow(
            name="proactive_curiosity_loop",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="proactive_curiosity_loop",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


def _adapter_causality_consumer() -> DashboardRow:
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.causality_consumer_graduation_contract import (  # noqa: E501
            is_ready_for_graduation,
        )
        report = is_ready_for_graduation()
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        return DashboardRow(
            name="causality_consumer",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="causality_consumer",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


def _adapter_tool_confidence_indicator() -> DashboardRow:
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.tool_confidence_indicator_graduation_contract import (  # noqa: E501
            is_ready_for_graduation,
        )
        report = is_ready_for_graduation()
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        return DashboardRow(
            name="tool_confidence_indicator",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="tool_confidence_indicator",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


def _adapter_tool_hooks() -> DashboardRow:
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.tool_hooks_graduation_contract import (  # noqa: E501
            is_ready_for_graduation,
        )
        report = is_ready_for_graduation()
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        return DashboardRow(
            name="tool_hooks",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="tool_hooks",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


def _adapter_tool_permissions() -> DashboardRow:
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.tool_permissions_graduation_contract import (  # noqa: E501
            is_ready_for_graduation,
        )
        report = is_ready_for_graduation()
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        return DashboardRow(
            name="tool_permissions",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="tool_permissions",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


def _adapter_multi_prior() -> DashboardRow:
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
            is_ready_for_graduation,
        )
        report = is_ready_for_graduation()
        raw = getattr(getattr(report, "verdict", None), "value", "")
        verdict, diag = _normalize_contract_verdict(str(raw))
        return DashboardRow(
            name="multi_prior",
            source="contract",
            verdict=verdict,
            raw_verdict=str(raw),
            diagnostic=diag,
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardRow(
            name="multi_prior",
            source="contract",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"adapter_error:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        )


_CONTRACT_ADAPTERS = (
    _adapter_phase10,
    _adapter_cross_op_semantic_budget,
    _adapter_proactive_curiosity_loop,
    _adapter_causality_consumer,
    _adapter_tool_confidence_indicator,
    _adapter_tool_hooks,
    _adapter_tool_permissions,
    _adapter_multi_prior,
)


# ---------------------------------------------------------------------------
# Ledger-flag adapter — composes GraduationLedger.all_progress
# + is_eligible for the 32 CADENCE_POLICY flags
# ---------------------------------------------------------------------------


def _ledger_rows() -> Tuple[DashboardRow, ...]:
    """Compose graduation_ledger.all_progress() for every flag
    in CADENCE_POLICY. NEVER raises."""
    t0 = time.monotonic()
    try:
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            get_default_ledger,
            is_ledger_enabled,
        )
        master = bool(is_ledger_enabled())
        ledger = get_default_ledger()
        progress_map = ledger.all_progress()
        eligible = frozenset(ledger.eligible_flags())
    except Exception as exc:  # noqa: BLE001
        return (DashboardRow(
            name="ledger",
            source="ledger",
            verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
            raw_verdict="",
            diagnostic=f"ledger_read_raised:{type(exc).__name__}",
            elapsed_s=time.monotonic() - t0,
        ),)
    out: List[DashboardRow] = []
    for flag_name in sorted(progress_map):
        progress = progress_map[flag_name]
        verdict, diag = _normalize_ledger_state(
            progress,
            is_eligible=(flag_name in eligible),
            ledger_master_on=master,
        )
        out.append(DashboardRow(
            name=flag_name,
            source="ledger",
            verdict=verdict,
            raw_verdict="",
            diagnostic=diag,
            elapsed_s=0.0,
        ))
    return tuple(out)


# ---------------------------------------------------------------------------
# Public aggregator — single read across all gates
# ---------------------------------------------------------------------------


def aggregate_dashboard(
    *,
    now_unix: Optional[float] = None,
) -> DashboardSnapshot:
    """Aggregate every graduation gate into a single snapshot.

    Pure read; NEVER raises. Composes:
      * 8 §33.1 contract predicates via lazy-import
      * graduation_ledger.all_progress + eligible_flags

    When a per-gate read fails defensively, the row carries an
    ``adapter_error:<exc_type>`` diagnostic and the verdict
    degrades to EVIDENCE_INSUFFICIENT — never propagates the
    exception. The dashboard MUST stay queryable even when one
    contract is broken or unimported.
    """
    t0 = time.monotonic()
    started = float(now_unix) if now_unix is not None else time.time()

    rows: List[DashboardRow] = []

    # Per-contract adapters (8 contracts).
    for adapter in _CONTRACT_ADAPTERS:
        try:
            row = adapter()
        except Exception as exc:  # noqa: BLE001 — defensive
            row = DashboardRow(
                name=adapter.__name__.replace("_adapter_", ""),
                source="contract",
                verdict=UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT,
                raw_verdict="",
                diagnostic=f"outer_exc:{type(exc).__name__}",
                elapsed_s=0.0,
            )
        rows.append(row)

    # Per-flag ledger rows (32 CADENCE_POLICY flags).
    rows.extend(_ledger_rows())

    return DashboardSnapshot(
        aggregated_at_unix=started,
        rows=tuple(rows),
        elapsed_s=time.monotonic() - t0,
    )


def append_audit_record(snapshot: DashboardSnapshot) -> bool:
    """Append a single JSONL record summarizing the snapshot's
    verdict-summary into the audit ledger via §33.4
    flock-protected helper. NEVER raises. Master-flag-gated.

    Returns True on success, False on any failure (master-off
    counts as failure-no-write — the dashboard is queryable
    without writing audit)."""
    if not is_dashboard_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        record = {
            "schema_version": (
                UNIFIED_GRADUATION_DASHBOARD_SCHEMA_VERSION
            ),
            "aggregated_at_unix": float(
                snapshot.aggregated_at_unix
            ),
            "summary": snapshot.summary(),
            "ready_count": len(snapshot.ready_rows()),
            "failed_count": len(snapshot.failed_rows()),
            "elapsed_s": float(snapshot.elapsed_s),
        }
        line = json.dumps(record, sort_keys=True)
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        path = audit_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(flock_append_line(path, line))
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. Master flag default-FALSE (synthetic regression fires
         on premature ``return True`` flip).
      2. Authority asymmetry — substrate cage applies
         recursively; aggregator MUST NOT import orchestrator /
         iron_gate / policy / providers / candidate_generator /
         change_engine / semantic_guardian.
      3. Composes canonical contracts — every adapter MUST
         lazy-import its target predicate (no parallel
         reasoning).
      4. Read-only — aggregator MUST NOT call
         ``record_*`` / ``write_*`` / ``set_*`` / ``update_*``
         on any composed surface.
      5. Verdict taxonomy 5-values (closed-enum integrity).
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
        "unified_graduation_dashboard.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "is_dashboard_enabled"
            ):
                # Must read the env var with default "" (not "true")
                # AND must NOT contain `return True` literal.
                func_src = ast.unparse(node)
                if "return True" in func_src:
                    violations.append(
                        "is_dashboard_enabled MUST NOT "
                        "unconditionally return True (master "
                        "default-FALSE per §33.1)"
                    )
                # The expected env-key default: empty string
                # (so .lower() in _TRUTHY → False unless set).
                expected_key = (
                    "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED"
                )
                if expected_key not in func_src:
                    violations.append(
                        f"is_dashboard_enabled MUST gate on "
                        f"{expected_key!r}"
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
                            f"unified_graduation_dashboard MUST "
                            f"NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_contracts(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Every contract adapter MUST lazy-import its target
        predicate. Walk each `_adapter_*` FunctionDef and ensure
        it contains an ImportFrom referencing a `*_graduation_-
        contract` module."""
        violations: list = []
        required_adapter_count = 8
        adapter_count = 0
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name.startswith("_adapter_")
                and node.name != "_adapter_template"
            ):
                adapter_count += 1
                has_lazy_import = False
                for inner in ast.walk(node):
                    if isinstance(inner, ast.ImportFrom):
                        module = inner.module or ""
                        if "graduation_contract" in module:
                            has_lazy_import = True
                            break
                if not has_lazy_import:
                    violations.append(
                        f"adapter {node.name} MUST lazy-import "
                        f"a *_graduation_contract predicate "
                        f"(no parallel reasoning)"
                    )
        if adapter_count != required_adapter_count:
            violations.append(
                f"expected {required_adapter_count} contract "
                f"adapters, found {adapter_count}"
            )
        return tuple(violations)

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:  # source unused; AST walk is authoritative.
        """Aggregator MUST NOT call record_*/write_*/set_*/
        update_* on imported contract surfaces. Walk all Call
        nodes and inspect attribute-access targets."""
        violations: list = []
        forbidden_prefixes = (
            "record_", "write_", "set_", "update_",
            "mutate_", "delete_", "remove_", "clear_",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    attr = func.attr
                    for p in forbidden_prefixes:
                        if attr.startswith(p):
                            # Allow `path.parent.mkdir` and other
                            # stdlib path mutations — restrict to
                            # contract-related namespaces.
                            target = ast.unparse(func)
                            # Filter: only flag if target name
                            # references a graduation surface.
                            if any(
                                kw in target.lower()
                                for kw in (
                                    "graduation",
                                    "contract",
                                    "ledger",
                                    "verdict",
                                )
                            ):
                                violations.append(
                                    f"read-only violation: "
                                    f"{target}() — aggregator "
                                    f"MUST NOT mutate composed "
                                    f"contract state"
                                )
                                break
        return tuple(violations)

    def _validate_verdict_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "READY",
            "EVIDENCE_GATHERING",
            "EVIDENCE_INSUFFICIENT",
            "EVIDENCE_FAILED",
            "DISABLED",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "UnifiedGraduationVerdict":
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

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "unified_graduation_dashboard_master_"
                "default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_UNIFIED_GRADUATION_"
                "DASHBOARD_ENABLED stays default-FALSE per "
                "§33.1. Synthetic regression fires on "
                "premature `return True` flip."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "unified_graduation_dashboard_authority_"
                "asymmetry"
            ),
            target_file=target,
            description=(
                "Aggregator MUST stay pure substrate "
                "composing graduation contracts + ledger + "
                "stdlib + cross_process_jsonl ONLY. NEVER "
                "imports orchestrator / iron_gate / policy / "
                "providers / candidate_generator / "
                "change_engine / semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "unified_graduation_dashboard_composes_"
                "canonical_contracts"
            ),
            target_file=target,
            description=(
                "Every contract adapter MUST lazy-import its "
                "target *_graduation_contract predicate. No "
                "parallel reasoning about graduation "
                "readiness anywhere in this module. Adapter "
                "count MUST equal 8 (8 §33.1 contracts)."
            ),
            validate=_validate_composes_contracts,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "unified_graduation_dashboard_read_only"
            ),
            target_file=target,
            description=(
                "Aggregator MUST NOT call record_/write_/set_/"
                "update_/mutate_/delete_/remove_/clear_ on any "
                "composed graduation/contract/ledger/verdict "
                "surface. Read-only by construction."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "unified_graduation_dashboard_verdict_"
                "taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "UnifiedGraduationVerdict is a 5-value closed "
                "taxonomy (READY / EVIDENCE_GATHERING / "
                "EVIDENCE_INSUFFICIENT / EVIDENCE_FAILED / "
                "DISABLED). New values require explicit "
                "scope-doc + pin update."
            ),
            validate=_validate_verdict_taxonomy_closed,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Register dashboard flags with the FlagRegistry."""
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED",
            "bool",
            "false",
            (
                "Master flag for the unified graduation "
                "dashboard (§35). Default-FALSE per §33.1; "
                "flip after empirical validation against "
                "per-contract queries on a live session."
            ),
        ),
        (
            "JARVIS_UNIFIED_GRADUATION_DASHBOARD_LEDGER_PATH",
            "path",
            ".jarvis/unified_graduation_dashboard.jsonl",
            (
                "JSONL audit-ledger path for dashboard "
                "snapshot summaries. Append-only, "
                "flock-protected via §33.4."
            ),
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="graduation",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "unified_graduation_dashboard.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001 — defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        return n
    return n


__all__ = [
    "DashboardRow",
    "DashboardSnapshot",
    "UNIFIED_GRADUATION_DASHBOARD_SCHEMA_VERSION",
    "UnifiedGraduationVerdict",
    "aggregate_dashboard",
    "append_audit_record",
    "audit_ledger_path",
    "is_dashboard_enabled",
    "register_flags",
    "register_shipped_invariants",
]
