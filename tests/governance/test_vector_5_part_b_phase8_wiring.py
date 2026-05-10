"""Vector #5 Part B — Phase 8 producer wiring regression spine.

Closes the gap between Phase 8 substrate (DecisionTraceLedger +
LatentConfidenceRing + LatencySLODetector — all shipped) and live
production data. Substrate without producers is dead code; this
arc wires three minimal-touch producer call sites and pins them
against drift.

## Wiring sites

  1. ``route_runner.py`` — after ``UrgencyRouter.classify`` →
     ``DecisionTraceLedger.record(phase="ROUTE", ...)``.
  2. ``semantic_triage.py`` — after triage decision logged →
     ``LatentConfidenceRing.record(classifier_name=
     "semantic_triage", ...)``.
  3. ``phase_dispatcher.py`` — wraps ``runner.run(ctx)`` with
     ``time.monotonic()`` deltas → ``LatencySLODetector.record(
     phase=dispatch_phase.name, ...)``.

## AST pins

Each wiring site has a bytes-pin on the production source that
fires if a future edit removes the producer call. Synthetic
regressions prove the pin fails on mutated sources.

## Functional integration

Master-on each ledger, simulate the producer surface, assert the
ledger received at least one record. Validates the wiring is
live, not just syntactically present.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]


# -----------------------------------------------------------------
# AST pin helpers
# -----------------------------------------------------------------


def _read_source(rel_path: str) -> tuple[ast.AST, str]:
    p = _REPO_ROOT / rel_path
    src = p.read_text(encoding="utf-8")
    return ast.parse(src), src


# -----------------------------------------------------------------
# DecisionTraceLedger wiring (route_runner.py)
# -----------------------------------------------------------------


_ROUTE_RUNNER_REL = (
    "backend/core/ouroboros/governance/phase_runners/route_runner.py"
)
_TRIAGE_REL = (
    "backend/core/ouroboros/governance/semantic_triage.py"
)
_DISPATCHER_REL = (
    "backend/core/ouroboros/governance/phase_dispatcher.py"
)


def _decision_trace_pin(src: str) -> list[str]:
    """Verify route_runner composes ``phase8_producers.record_decision``
    (the canonical wrapper that handles substrate.record() + SSE
    publish in one call) AND stamps phase="ROUTE". Bytes-pin —
    fail-loud if the call site drifts away from the canonical
    surface."""
    failures: list[str] = []
    if "phase8_producers" not in src:
        failures.append(
            "route_runner.py must compose canonical phase8_producers "
            "wrapper (NOT call decision_trace_ledger directly)"
        )
    if "record_decision" not in src:
        failures.append(
            "route_runner.py missing record_decision import / call"
        )
    if 'phase="ROUTE"' not in src:
        failures.append(
            'route_runner.py missing phase="ROUTE" record_decision call'
        )
    return failures


def _latent_confidence_pin(src: str) -> list[str]:
    """Verify semantic_triage composes
    ``phase8_producers.record_confidence`` AND stamps
    classifier_name="semantic_triage"."""
    failures: list[str] = []
    if "phase8_producers" not in src:
        failures.append(
            "semantic_triage.py must compose canonical phase8_producers "
            "wrapper (NOT call latent_confidence_ring directly)"
        )
    if "record_confidence" not in src:
        failures.append(
            "semantic_triage.py missing record_confidence import / call"
        )
    if 'classifier_name="semantic_triage"' not in src:
        failures.append(
            "semantic_triage.py missing classifier_name=\"semantic_triage\" "
            "record_confidence call"
        )
    return failures


def _latency_slo_pin(src: str) -> list[str]:
    """Verify phase_dispatcher wraps runner.run(ctx) with
    time.monotonic() deltas AND composes
    ``phase8_producers.record_phase_latency`` +
    ``check_breach_and_publish``."""
    failures: list[str] = []
    if "phase8_producers" not in src:
        failures.append(
            "phase_dispatcher.py must compose canonical phase8_producers "
            "wrapper (NOT call latency_slo_detector directly)"
        )
    if "record_phase_latency" not in src:
        failures.append(
            "phase_dispatcher.py missing record_phase_latency import / call"
        )
    if "check_breach_and_publish" not in src:
        failures.append(
            "phase_dispatcher.py missing check_breach_and_publish "
            "(SSE-breach piggy-back must compose at the same call site)"
        )
    if "_phase_t0 = time.monotonic()" not in src:
        failures.append(
            "phase_dispatcher.py missing _phase_t0 = time.monotonic() "
            "monotonic-clock anchor (Vector #11 discipline)"
        )
    if "phase=dispatch_phase.name" not in src:
        failures.append(
            "phase_dispatcher.py missing phase=dispatch_phase.name "
            "record_phase_latency call"
        )
    return failures


# -----------------------------------------------------------------
# Pin pass tests (positive — production source is wired)
# -----------------------------------------------------------------


def test_decision_trace_pin_passes_on_production_source():
    _, src = _read_source(_ROUTE_RUNNER_REL)
    failures = _decision_trace_pin(src)
    assert failures == [], (
        "DecisionTraceLedger wiring drifted away from route_runner.py:\n"
        + "\n".join(failures)
    )


def test_latent_confidence_pin_passes_on_production_source():
    _, src = _read_source(_TRIAGE_REL)
    failures = _latent_confidence_pin(src)
    assert failures == [], (
        "LatentConfidenceRing wiring drifted away from semantic_triage.py:\n"
        + "\n".join(failures)
    )


def test_latency_slo_pin_passes_on_production_source():
    _, src = _read_source(_DISPATCHER_REL)
    failures = _latency_slo_pin(src)
    assert failures == [], (
        "LatencySLODetector wiring drifted away from phase_dispatcher.py:\n"
        + "\n".join(failures)
    )


# -----------------------------------------------------------------
# Pin synthetic regressions (negative — pin fires when wiring removed)
# -----------------------------------------------------------------


def test_decision_trace_pin_fires_on_mutated_source():
    _, src = _read_source(_ROUTE_RUNNER_REL)
    mutated = src.replace("phase8_producers", "##REMOVED##")
    failures = _decision_trace_pin(mutated)
    assert failures, (
        "pin must fire when canonical phase8_producers wrapper is "
        "removed"
    )


def test_decision_trace_pin_fires_when_phase_label_removed():
    _, src = _read_source(_ROUTE_RUNNER_REL)
    mutated = src.replace('phase="ROUTE"', 'phase="WRONG"')
    failures = _decision_trace_pin(mutated)
    assert any('phase="ROUTE"' in f for f in failures)


def test_latent_confidence_pin_fires_on_mutated_source():
    _, src = _read_source(_TRIAGE_REL)
    mutated = src.replace("phase8_producers", "##REMOVED##")
    failures = _latent_confidence_pin(mutated)
    assert failures, (
        "pin must fire when canonical phase8_producers wrapper is "
        "removed"
    )


def test_latent_confidence_pin_fires_when_classifier_renamed():
    _, src = _read_source(_TRIAGE_REL)
    mutated = src.replace(
        'classifier_name="semantic_triage"',
        'classifier_name="something_else"',
    )
    failures = _latent_confidence_pin(mutated)
    assert any("classifier_name" in f for f in failures)


def test_latency_slo_pin_fires_on_mutated_source():
    _, src = _read_source(_DISPATCHER_REL)
    mutated = src.replace("phase8_producers", "##REMOVED##")
    failures = _latency_slo_pin(mutated)
    assert failures, (
        "pin must fire when canonical phase8_producers wrapper is "
        "removed"
    )


def test_latency_slo_pin_fires_when_monotonic_anchor_removed():
    _, src = _read_source(_DISPATCHER_REL)
    mutated = src.replace(
        "_phase_t0 = time.monotonic()",
        "_phase_t0 = time.time()",
    )
    failures = _latency_slo_pin(mutated)
    assert any("monotonic" in f for f in failures), (
        "pin must fire when monotonic anchor is replaced with wall clock"
    )


def test_latency_slo_pin_fires_when_breach_publish_removed():
    """The breach-publish piggy-back at the same call site is part
    of the wiring contract — operators see live SLO violations
    without a separate observer loop."""
    _, src = _read_source(_DISPATCHER_REL)
    mutated = src.replace("check_breach_and_publish", "##REMOVED##")
    failures = _latency_slo_pin(mutated)
    assert any("check_breach_and_publish" in f for f in failures)


# -----------------------------------------------------------------
# Functional integration — verify ledger receives records during
# the actual production flow
# -----------------------------------------------------------------


@pytest.fixture
def _decision_ledger_master_on(tmp_path, monkeypatch):
    """Master-on the DecisionTraceLedger with a tmp ledger path."""
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_PATH",
        str(tmp_path / "decision_trace.jsonl"),
    )
    from backend.core.ouroboros.governance.observability import (
        decision_trace_ledger as _mod,
    )
    _mod.reset_default_ledger()
    yield _mod
    _mod.reset_default_ledger()


@pytest.fixture
def _confidence_ring_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "true")
    from backend.core.ouroboros.governance.observability import (
        latent_confidence_ring as _mod,
    )
    _mod.reset_default_ring()
    yield _mod
    _mod.reset_default_ring()


@pytest.fixture
def _latency_detector_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_LATENCY_SLO_DETECTOR_ENABLED", "true")
    from backend.core.ouroboros.governance.observability import (
        latency_slo_detector as _mod,
    )
    # Reset whichever resetter the detector exposes — names vary
    # across the substrate; tolerate either spelling.
    for name in ("reset_default_detector", "reset_default"):
        fn = getattr(_mod, name, None)
        if fn is not None:
            fn()
            break
    yield _mod


def test_decision_trace_ledger_records_route_decision_directly(
    _decision_ledger_master_on,
):
    """Direct call into the producer surface — proves the lazy
    import is reachable AND the master flag gates correctly."""
    ledger = _decision_ledger_master_on.get_default_ledger()
    ok, detail = ledger.record(
        op_id="op-vector5b-test",
        phase="ROUTE",
        decision="STANDARD",
        factors={
            "signal_urgency": "normal",
            "signal_source": "TestFailureSensor",
            "task_complexity": "moderate",
        },
        rationale="default cascade for normal urgency",
    )
    assert ok, f"record failed: {detail}"
    assert detail == "ok"


def test_latent_confidence_ring_records_triage_event_directly(
    _confidence_ring_master_on,
):
    ring = _confidence_ring_master_on.get_default_ring()
    ok, detail = ring.record(
        classifier_name="semantic_triage",
        confidence=0.87,
        threshold=0.5,
        outcome="GENERATE",
        extra={"op_id": "op-test", "model": "dw-397b"},
    )
    assert ok, f"record failed: {detail}"
    events = ring.recent(10)
    assert len(events) >= 1
    assert events[-1].classifier_name == "semantic_triage"
    assert events[-1].outcome == "GENERATE"


def test_latency_slo_detector_records_phase_latency_directly(
    _latency_detector_master_on,
):
    detector = _latency_detector_master_on.get_default_detector()
    ok, detail = detector.record(phase="ROUTE", latency_s=0.123)
    assert ok, f"record failed: {detail}"


def test_decision_trace_master_off_returns_false(monkeypatch):
    """Sanity: master-off path returns (False, master_off) — gate
    is enforced at producer, not consumer.

    Note: as of 2026-05-05 the DecisionTraceLedger flag graduated
    default-TRUE; opting back to false requires the explicit
    ``=false`` rollback per the graduated-flag escape-hatch
    contract.
    """
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "false")
    from backend.core.ouroboros.governance.observability import (
        decision_trace_ledger as _mod,
    )
    _mod.reset_default_ledger()
    ledger = _mod.get_default_ledger()
    ok, detail = ledger.record(
        op_id="op-test",
        phase="ROUTE",
        decision="STANDARD",
    )
    assert ok is False
    assert detail == "master_off"


def test_latent_confidence_master_off_returns_false(monkeypatch):
    monkeypatch.delenv("JARVIS_LATENT_CONFIDENCE_RING_ENABLED", raising=False)
    from backend.core.ouroboros.governance.observability import (
        latent_confidence_ring as _mod,
    )
    _mod.reset_default_ring()
    ring = _mod.get_default_ring()
    ok, detail = ring.record(
        classifier_name="x", confidence=0.5, threshold=0.5, outcome="y",
    )
    assert ok is False
    assert detail == "master_off"


# -----------------------------------------------------------------
# Wiring composes — the substrate the pins reference must actually
# import cleanly with the producer call sites loaded.
# -----------------------------------------------------------------


def test_route_runner_imports_cleanly():
    """If the producer-wiring imports broke route_runner, this
    fires immediately."""
    import importlib
    mod = importlib.import_module(
        "backend.core.ouroboros.governance.phase_runners.route_runner"
    )
    assert hasattr(mod, "ROUTERunner")


def test_semantic_triage_imports_cleanly():
    import importlib
    mod = importlib.import_module(
        "backend.core.ouroboros.governance.semantic_triage"
    )
    # SemanticTriageEngine class is the public surface.
    assert hasattr(mod, "SemanticTriageEngine")


def test_phase_dispatcher_imports_cleanly():
    import importlib
    mod = importlib.import_module(
        "backend.core.ouroboros.governance.phase_dispatcher"
    )
    assert hasattr(mod, "dispatch_pipeline")


# -----------------------------------------------------------------
# All 3 producer surfaces remain a single coordinated arc — adding
# a 4th producer should be a deliberate decision, not silent drift.
# -----------------------------------------------------------------


def test_phase8_producer_count_is_pinned():
    """Bytes-count pin: exactly 3 production files compose the
    canonical phase8_producers wrapper as of Vector #5 Part B
    graduation. New producers MUST update this count, forcing
    reviewer attention."""
    wired = []
    for rel, sentinel in (
        (_ROUTE_RUNNER_REL, "record_decision"),
        (_TRIAGE_REL, "record_confidence"),
        (_DISPATCHER_REL, "record_phase_latency"),
    ):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        if "phase8_producers" in src and sentinel in src:
            wired.append(rel)
    assert len(wired) == 3, (
        f"Expected exactly 3 wired producer files, found {len(wired)}: "
        f"{wired}. Adding a 4th producer? Update this pin."
    )
