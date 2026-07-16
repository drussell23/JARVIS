"""
Fault-Injection Matrix — systematic resilience verification (autonomy Gap 1)
============================================================================

The stability problem this session paid down one death at a time: every long
soak died to a NOVEL cause (loop starvation, orphan workers, a watchdog
false-positive, a memory explosion, a severed driver). Reactive whack-a-mole
converges slowly because each fix only reveals the next surprise.

This is the systematic alternative. Instead of running a soak and WAITING for a
death, the matrix INJECTS a controlled, reversible fault from each failure
CLASS the organism has a defense against, and verifies the defense fires and
CONTAINS it. An uncovered cell — a fault class with no proven defense — is the
PREDICTED site of the next novel death, surfaced BEFORE it kills a run.

Design (generalizes the proven ``red_blue_matrix`` shape from the containment
cage to the broader stability surface; composes, never duplicates):
  * ``ResilienceFaultClass`` — the closed taxonomy of stability failure classes
    (the *resilience* axis, complementary to ``failure_mode_memory``'s
    *code-generation* axis; mirrors its enum+schema pattern, reuses no values).
  * A declarative ``FaultScenario`` REGISTRY (data, seeded like
    ``flag_registry_seed`` — the runner iterates data, no hardcoded control
    flow). Each scenario = (inject, verify, revert) over a CLEAN injectable
    seam of the defense it targets — pure decision fns / first-class kwargs /
    documented reset seams; NEVER ``unittest.mock.patch``.
  * The A1-auditor verdict contract: a ``criteria`` dict of named booleans,
    ``DEFENDED`` iff ``all(criteria.values())``, deterministic ``failure_locus``
    = first failing criterion, and ``INCONCLUSIVE`` (never a fake pass) when a
    fault cannot be injected/verified.
  * Revert-ALWAYS: every scenario runs inside a ``finally`` that undoes its
    fault, even on exception — the ``a1_live_fire_chaos_harness`` discipline.
  * The Gap 2 oracle (``capability_firing.firing_verdict``) as the detection
    backbone: proves the organism can SEE an induced dark/dormant capability.

Coverage report: ``fault_class × verdict`` over the full taxonomy. Classes with
no scenario are ``NO_SCENARIO`` (a coverage gap — where the next death hides),
distinct from ``UNDEFENDED`` (fault injected, defense did not respond).

Authority posture: read-only projector over self-contained, reversible
scenarios. Imports NO orchestrator/policy/gate module. NEVER raises out of the
runner (a scenario fault degrades to INCONCLUSIVE, never propagates).
"""
from __future__ import annotations

import enum
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

FAULT_INJECTION_MATRIX_SCHEMA_VERSION = "fault_injection_matrix.1"


# ---------------------------------------------------------------------------
# Taxonomy — the resilience failure surface (closed enum, extensible)
# ---------------------------------------------------------------------------


class ResilienceFaultClass(str, enum.Enum):
    """The stability failure classes the organism defends against. Distinct
    from ``failure_mode_memory.FailureModeKind`` (code-generation axis)."""

    ORPHAN_WORKER = "orphan_worker"                 # ppid-drift subprocess leak
    ORPHAN_CHILD = "orphan_child"                   # registered child not reaped
    WATCHDOG_FALSE_POSITIVE = "watchdog_false_positive"   # suspend forged as wedge
    WATCHDOG_BEAT_DEATH = "watchdog_beat_death"     # silent heartbeat-writer fault
    WATCHDOG_MONITOR_DEATH = "watchdog_monitor_death"     # supervisor restart
    MEMORY_EXHAUSTION = "memory_exhaustion"         # fanout not clamped under pressure
    EMBED_CORRELATED_FAILURE = "embed_correlated_failure"  # breaker abort
    LOOP_STARVATION = "loop_starvation"             # on-loop CPU / GIL block
    PROVIDER_OUTAGE = "provider_outage"             # transport failures uncontained
    COST_RUNAWAY = "cost_runaway"                   # unbounded spend
    SEVERED_CAPABILITY = "severed_capability"       # wired-but-inert (Gap 2)
    TEARDOWN_WEDGE = "teardown_wedge"               # shutdown hang


class FaultVerdict(str, enum.Enum):
    DEFENDED = "defended"              # fault injected, defense fired + contained
    UNDEFENDED = "undefended"         # fault injected, defense did NOT respond
    DEFENSE_FAILED = "defense_failed"  # defense fired but did the wrong thing
    INCONCLUSIVE = "inconclusive"     # could not inject/verify (never fake-pass)


class CoverageVerdict(str, enum.Enum):
    DEFENDED = "defended"          # ≥1 scenario, all DEFENDED
    BROKEN = "broken"             # ≥1 scenario UNDEFENDED/DEFENSE_FAILED
    INCONCLUSIVE = "inconclusive"  # scenarios only INCONCLUSIVE
    NO_SCENARIO = "no_scenario"   # NO scenario — the predicted next-death site


# ---------------------------------------------------------------------------
# Scenario + result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaultScenario:
    """One reversible fault-injection scenario. ``inject`` induces the fault and
    returns an opaque handle; ``verify`` returns a ``{criterion: bool}`` dict
    (the defense fired + contained); ``revert`` undoes the fault (always run)."""
    id: str
    fault_class: ResilienceFaultClass
    defense: str                       # the module/mechanism under test
    description: str
    inject: Callable[[], Any]
    verify: Callable[[Any], Dict[str, bool]]
    revert: Callable[[Any], None] = lambda _h: None
    severity: str = "high"


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    fault_class: str
    defense: str
    verdict: str
    criteria: Dict[str, bool] = field(default_factory=dict)
    failure_locus: Optional[str] = None
    detail: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "fault_class": self.fault_class,
            "defense": self.defense,
            "verdict": self.verdict,
            "criteria": dict(self.criteria),
            "failure_locus": self.failure_locus,
            "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


# ---------------------------------------------------------------------------
# Env / master
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_FAULT_INJECTION_MATRIX_ENABLED`` (default true). The matrix runs
    self-contained reversible scenarios; the switch only silences the surface."""
    raw = os.environ.get("JARVIS_FAULT_INJECTION_MATRIX_ENABLED", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Registry (seeded, data-driven — mirrors flag_registry_seed)
# ---------------------------------------------------------------------------

_REGISTRY_LOCK = threading.RLock()
_SCENARIOS: List[FaultScenario] = []
_SEEDED = False


def register_scenario(scenario: FaultScenario) -> None:
    with _REGISTRY_LOCK:
        if any(s.id == scenario.id for s in _SCENARIOS):
            return
        _SCENARIOS.append(scenario)


def ensure_seeded() -> List[FaultScenario]:
    global _SEEDED
    with _REGISTRY_LOCK:
        if not _SEEDED:
            try:
                from backend.core.ouroboros.governance.fault_injection_seed import (
                    seed_scenarios,
                )
                seed_scenarios(register_scenario)
            except Exception:  # noqa: BLE001 — a bad seed never breaks the matrix
                pass
            _SEEDED = True
        return list(_SCENARIOS)


def reset_registry_for_tests() -> None:
    global _SEEDED
    with _REGISTRY_LOCK:
        _SCENARIOS.clear()
        _SEEDED = False


# ---------------------------------------------------------------------------
# Runner — safe (revert-always) envelope + criteria-dict verdict
# ---------------------------------------------------------------------------


def run_scenario(scenario: FaultScenario) -> ScenarioResult:
    """Run one scenario inside a revert-ALWAYS envelope. NEVER raises.

    inject → verify → (finally) revert. DEFENDED iff every criterion holds;
    else DEFENSE_FAILED with the first-failing criterion as the locus;
    INCONCLUSIVE if inject/verify raised (never a fake pass)."""
    t0 = time.monotonic()
    handle: Any = None
    injected = False
    try:
        handle = scenario.inject()
        injected = True
        criteria = scenario.verify(handle)
        if not isinstance(criteria, dict) or not criteria:
            return _result(scenario, FaultVerdict.INCONCLUSIVE, {}, t0,
                           detail="verify returned no criteria")
        passed = all(bool(v) for v in criteria.values())
        if passed:
            return _result(scenario, FaultVerdict.DEFENDED, criteria, t0)
        locus = next((k for k, v in criteria.items() if not v), None)
        return _result(scenario, FaultVerdict.DEFENSE_FAILED, criteria, t0,
                       locus=locus, detail=f"criterion failed: {locus}")
    except Exception as exc:  # noqa: BLE001 — a scenario fault is INCONCLUSIVE
        return _result(scenario, FaultVerdict.INCONCLUSIVE, {}, t0,
                       detail=f"{type(exc).__name__}: {exc}")
    finally:
        if injected:
            try:
                scenario.revert(handle)
            except Exception:  # noqa: BLE001 — revert best-effort, never raise
                pass


def _result(scenario, verdict, criteria, t0, *, locus=None, detail=""):
    return ScenarioResult(
        scenario_id=scenario.id, fault_class=scenario.fault_class.value,
        defense=scenario.defense, verdict=verdict.value, criteria=criteria,
        failure_locus=locus, detail=detail,
        elapsed_ms=(time.monotonic() - t0) * 1000.0,
    )


# ---------------------------------------------------------------------------
# Matrix + coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixReport:
    schema_version: str = FAULT_INJECTION_MATRIX_SCHEMA_VERSION
    enabled: bool = True
    reason_code: str = "ok"
    generated_at_unix: float = 0.0
    results: List[ScenarioResult] = field(default_factory=list)
    coverage: Dict[str, str] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)
    resilience_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        gaps = [c for c, v in self.coverage.items()
                if v in (CoverageVerdict.NO_SCENARIO.value, CoverageVerdict.BROKEN.value)]
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "reason_code": self.reason_code,
            "generated_at_unix": self.generated_at_unix,
            # class × coverage — the matrix. NO_SCENARIO / BROKEN cells are the
            # predicted sites of the next novel death.
            "coverage": self.coverage,
            "coverage_gaps": gaps,
            "scenario_counts": self.counts,
            # fraction of the fault taxonomy with a proven (DEFENDED) defense.
            "resilience_score": self.resilience_score,
            "results": [r.to_dict() for r in self.results],
        }


def run_matrix() -> MatrixReport:
    """Run every registered scenario and compute the coverage matrix over the
    FULL fault taxonomy. NEVER raises."""
    now = time.time()
    try:
        scenarios = ensure_seeded()
        results = [run_scenario(s) for s in scenarios]

        # per-class rollup
        by_class: Dict[str, List[ScenarioResult]] = {}
        for r in results:
            by_class.setdefault(r.fault_class, []).append(r)

        coverage: Dict[str, str] = {}
        for fclass in ResilienceFaultClass:
            rs = by_class.get(fclass.value, [])
            coverage[fclass.value] = _coverage_verdict(rs).value

        counts: Dict[str, int] = {}
        for r in results:
            counts[r.verdict] = counts.get(r.verdict, 0) + 1

        defended = sum(1 for v in coverage.values()
                       if v == CoverageVerdict.DEFENDED.value)
        score = round(defended / len(coverage), 4) if coverage else None

        return MatrixReport(
            enabled=True, reason_code="ok", generated_at_unix=now,
            results=results, coverage=coverage, counts=counts,
            resilience_score=score,
        )
    except Exception:  # noqa: BLE001 — matrix MUST never raise
        return MatrixReport(enabled=True, reason_code="matrix_error",
                            generated_at_unix=now)


def _coverage_verdict(results: List[ScenarioResult]) -> CoverageVerdict:
    if not results:
        return CoverageVerdict.NO_SCENARIO
    verdicts = {r.verdict for r in results}
    if FaultVerdict.UNDEFENDED.value in verdicts or FaultVerdict.DEFENSE_FAILED.value in verdicts:
        return CoverageVerdict.BROKEN
    if verdicts == {FaultVerdict.INCONCLUSIVE.value}:
        return CoverageVerdict.INCONCLUSIVE
    if FaultVerdict.DEFENDED.value in verdicts:
        return CoverageVerdict.DEFENDED
    return CoverageVerdict.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Cached snapshot for the observability GET
# ---------------------------------------------------------------------------

_SNAP_LOCK = threading.RLock()
_LAST: Optional[MatrixReport] = None
_LAST_TS: float = 0.0


def _ttl_s() -> float:
    try:
        return max(1.0, float(
            os.environ.get("JARVIS_FAULT_INJECTION_MATRIX_TTL_S", "").strip() or 300.0
        ))
    except (TypeError, ValueError):
        return 300.0


def snapshot(*, force: bool = False) -> Dict[str, Any]:
    """Public entry for the observability GET — TTL-cached matrix run. Master
    off → minimal ``enabled: false`` body. NEVER raises."""
    if not master_enabled():
        return {
            "schema_version": FAULT_INJECTION_MATRIX_SCHEMA_VERSION,
            "enabled": False, "reason_code": "disabled",
        }
    global _LAST, _LAST_TS
    now = time.time()
    with _SNAP_LOCK:
        if not force and _LAST is not None and (now - _LAST_TS) <= _ttl_s():
            return _LAST.to_dict()
    report = run_matrix()
    with _SNAP_LOCK:
        _LAST = report
        _LAST_TS = now
    return report.to_dict()


def reset_cache_for_tests() -> None:
    global _LAST, _LAST_TS
    with _SNAP_LOCK:
        _LAST = None
        _LAST_TS = 0.0
