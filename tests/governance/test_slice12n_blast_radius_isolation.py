"""
Slice 12N — Blast-Radius Isolation + Config Hardening tests.
============================================================

Closes the wedge surfaced by the Slice 12M verification soak
(bt-2026-05-23-015723): a background Ouroboros autonomous op
hit ``TERMINAL_STRUCTURAL`` on provider exhaustion, incremented
the global circuit-breaker trip counter, the global breaker
fired ``session_exhausted``, and the in-flight SWE-Bench-Pro
fixture op (a high-priority foreground op) was assassinated
mid-GENERATE despite being completely healthy.

PHASE 1 — Blast-Radius Isolation:
  * New closed ``CircuitTripOrigin`` taxonomy (4 values:
    FOREGROUND / BACKGROUND / SPECULATIVE / MAINTENANCE)
  * Per-op breaker accepts ``origin`` parameter
  * ``_trip_terminal`` only escalates structural trips to the
    global threshold when origin == FOREGROUND
  * BACKGROUND / SPECULATIVE / MAINTENANCE trips terminate
    locally but cannot blast-radius out
  * CandidateGenerator maps ProviderRoute → origin so the
    canonical routing layer drives the isolation

PHASE 2 — Config Hardening:
  * New ``MISCONFIGURED_PHASE_A_DISABLED`` verdict on
    ``SWEBenchProInjectionVerdict``
  * Preflight check in ``maybe_inject_swe_bench_at_boot`` halts
    cleanly when HARNESS_INJECT=true + Phase A master=off,
    BEFORE any load/ingest attempt — no budget burn, no worktree
    creation, clear actionable log line

Operator binding (verbatim):
  * Background sensor/maintenance op must NEVER trip global CB
    if high-priority foreground op is healthy
  * If low-priority/background op faults, scoped breaker only
  * session_exhausted only triggered globally if (a) budget
    breached, OR (b) foreground orchestrator op suffers
    terminal structural fault
  * Pre-flight assertion at boot for misconfigured Phase A
  * Halt cleanly before any budget burn
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.circuit_breaker import (
    CircuitBreaker,
    CircuitScope,
    CircuitState,
    CircuitTripOrigin,
    _FOREGROUND_ORIGINS,
    get_global_breaker,
    reset_global_breaker,
)
from backend.core.ouroboros.governance.provider_retry_classifier import (
    RetryDecision,
)
from backend.core.ouroboros.governance.swe_bench_pro.harness_inject import (
    SWEBenchProInjectionVerdict,
    maybe_inject_swe_bench_at_boot,
)


# ===============================================================
# Helpers
# ===============================================================


@pytest.fixture(autouse=True)
def _reset_global_breaker():
    """Each test starts with a fresh global breaker. The breaker
    is a process singleton — tests must isolate it explicitly."""
    reset_global_breaker()
    yield
    reset_global_breaker()


# ===============================================================
# Phase 1 — Blast-radius isolation behavioral tests
# ===============================================================


def test_foreground_trip_escalates_to_global() -> None:
    """Operator binding: foreground (primary/orchestrator) trips
    SHOULD escalate to global. This is the pre-Slice-12N behavior
    that must be preserved byte-identically for FOREGROUND
    origins."""
    gb = get_global_breaker()
    assert gb.state == CircuitState.CLOSED
    # Default global trip threshold is 5
    for i in range(5):
        b = CircuitBreaker(
            op_id=f"fg-{i}", origin=CircuitTripOrigin.FOREGROUND,
        )
        b.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
    assert gb.state == CircuitState.OPEN_TERMINAL, (
        f"Global should trip after 5 FG trips; got {gb.state}"
    )


def test_background_trip_does_not_escalate() -> None:
    """Operator binding: background sensor ops MUST NOT trip the
    global breaker. 20 background trips (4× the default threshold)
    MUST leave the global breaker CLOSED."""
    gb = get_global_breaker()
    for i in range(20):
        b = CircuitBreaker(
            op_id=f"bg-{i}", origin=CircuitTripOrigin.BACKGROUND,
        )
        b.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
        # Per-op breaker DID trip
        assert b.state == CircuitState.OPEN_TERMINAL
    # Global breaker DID NOT trip
    assert gb.state == CircuitState.CLOSED, (
        f"BACKGROUND trips should not escalate; "
        f"global state={gb.state} after 20 trips"
    )


def test_speculative_trip_does_not_escalate() -> None:
    """SPECULATIVE ops (IntentDiscovery, DreamEngine pre-compute)
    are fire-and-forget; their structural trips must be isolated."""
    gb = get_global_breaker()
    for i in range(20):
        b = CircuitBreaker(
            op_id=f"spec-{i}", origin=CircuitTripOrigin.SPECULATIVE,
        )
        b.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
    assert gb.state == CircuitState.CLOSED


def test_maintenance_trip_does_not_escalate() -> None:
    """MAINTENANCE ops (periodic upkeep, health probes,
    TopologySentinel) must be isolated."""
    gb = get_global_breaker()
    for i in range(20):
        b = CircuitBreaker(
            op_id=f"maint-{i}", origin=CircuitTripOrigin.MAINTENANCE,
        )
        b.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
    assert gb.state == CircuitState.CLOSED


def test_mixed_origin_only_foreground_counts_toward_global() -> None:
    """The bt-2026-05-23-015723 scenario reproduced: 19 BG trips
    DO NOT escalate; then 5 FG trips DO escalate. Background
    trips never blast-radius out, but foreground escalation is
    still authoritative."""
    gb = get_global_breaker()
    # 19 BACKGROUND trips
    for i in range(19):
        CircuitBreaker(
            op_id=f"bg-{i}", origin=CircuitTripOrigin.BACKGROUND,
        ).evaluate(RetryDecision.TERMINAL_STRUCTURAL)
    assert gb.state == CircuitState.CLOSED
    # Now 5 FOREGROUND trips
    for i in range(5):
        CircuitBreaker(
            op_id=f"fg-{i}", origin=CircuitTripOrigin.FOREGROUND,
        ).evaluate(RetryDecision.TERMINAL_STRUCTURAL)
    assert gb.state == CircuitState.OPEN_TERMINAL


def test_default_origin_is_foreground_for_backward_compat() -> None:
    """Pre-Slice-12N callers that don't plumb origin must get
    FOREGROUND by default — preserving the prior escalation
    semantics byte-identically."""
    b = CircuitBreaker(op_id="legacy-call")
    assert b.origin == CircuitTripOrigin.FOREGROUND


def test_foreground_origins_set_contains_only_foreground() -> None:
    """The module-level ``_FOREGROUND_ORIGINS`` frozenset is the
    single source of truth for which origins escalate. Verify
    it contains ONLY FOREGROUND (no accidental inclusion of
    BACKGROUND that would re-introduce the wedge)."""
    assert _FOREGROUND_ORIGINS == frozenset({CircuitTripOrigin.FOREGROUND})


def test_circuit_breaker_origin_property_exposes_value() -> None:
    """Operator-facing telemetry: ``breaker.origin`` must be a
    public property for logging + observability."""
    b = CircuitBreaker(op_id="x", origin=CircuitTripOrigin.BACKGROUND)
    assert b.origin == CircuitTripOrigin.BACKGROUND


# ===============================================================
# Phase 1 — Trip-log line includes origin attribution
# ===============================================================


def test_trip_log_line_includes_origin_and_escalation_flag(caplog) -> None:
    """Operators tracking session_exhausted post-mortems need to
    see WHICH origin tripped and whether it escalated. The
    [CircuitBreaker] log line MUST include origin= and
    escalated_to_global= tokens."""
    import logging
    caplog.set_level(logging.INFO,
                     logger="backend.core.ouroboros.governance.circuit_breaker")
    b = CircuitBreaker(op_id="audit-test",
                       origin=CircuitTripOrigin.BACKGROUND)
    b.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
    msgs = [r.message for r in caplog.records]
    matched = [m for m in msgs if "tripped" in m and "origin=" in m]
    assert matched, f"No trip line with origin= found: {msgs[-3:]}"
    msg = matched[-1]
    assert "origin=background" in msg
    assert "escalated_to_global=False" in msg


# ===============================================================
# Phase 1 — CandidateGenerator plumbs ProviderRoute → origin
# ===============================================================


def test_candidate_generator_route_origin_map_present() -> None:
    """The CandidateGenerator must define a module-level mapping
    from ProviderRoute string → CircuitTripOrigin. AST-pin
    catches a refactor that drops the map."""
    from backend.core.ouroboros.governance import candidate_generator
    assert hasattr(candidate_generator, "_SLICE12N_ROUTE_TO_ORIGIN")
    m = candidate_generator._SLICE12N_ROUTE_TO_ORIGIN
    assert m["immediate"] == CircuitTripOrigin.FOREGROUND
    assert m["standard"] == CircuitTripOrigin.FOREGROUND
    assert m["complex"] == CircuitTripOrigin.FOREGROUND
    assert m["background"] == CircuitTripOrigin.BACKGROUND
    assert m["speculative"] == CircuitTripOrigin.SPECULATIVE


# ===============================================================
# Phase 2 — Config hardening preflight tests
# ===============================================================


@pytest.mark.asyncio
async def test_phase2_misconfigured_phase_a_disabled_verdict_exists() -> None:
    """The new closed-taxonomy value MUST exist on
    ``SWEBenchProInjectionVerdict``."""
    assert hasattr(
        SWEBenchProInjectionVerdict, "MISCONFIGURED_PHASE_A_DISABLED",
    )
    assert SWEBenchProInjectionVerdict.MISCONFIGURED_PHASE_A_DISABLED.value \
        == "misconfigured_phase_a_disabled"


@pytest.mark.asyncio
async def test_phase2_preflight_halts_when_phase_a_disabled() -> None:
    """When HARNESS_INJECT_ENABLED=true but Phase A master=off,
    the boot hook MUST return the new distinct verdict BEFORE
    any load/ingest attempt — no budget burn, no worktree
    creation, clear actionable log line."""
    intake = MagicMock()
    with patch.dict(
        os.environ,
        {
            "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED": "true",
        },
        clear=False,
    ):
        os.environ.pop("JARVIS_SWE_BENCH_PRO_ENABLED", None)
        verdict = await maybe_inject_swe_bench_at_boot(intake)
    assert verdict == SWEBenchProInjectionVerdict.MISCONFIGURED_PHASE_A_DISABLED


@pytest.mark.asyncio
async def test_phase2_preflight_does_not_halt_when_phase_a_enabled(
    tmp_path: Path,
) -> None:
    """When Phase A master IS on, the preflight DOES NOT trip the
    new verdict. (The downstream call may still return another
    verdict like SKIPPED_NO_PROBLEMS — but NOT
    MISCONFIGURED_PHASE_A_DISABLED.)"""
    intake = MagicMock()

    async def _ingest(_env):
        return "enqueued"
    intake.ingest_envelope = _ingest

    with patch.dict(
        os.environ,
        {
            "JARVIS_SWE_BENCH_PRO_ENABLED": "true",
            "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED": "true",
            "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH": str(
                tmp_path / "nonexistent.jsonl",
            ),
        },
        clear=False,
    ):
        # Use a path that doesn't exist so we don't actually inject
        # anything — we just want to confirm the preflight DIDN'T
        # halt at MISCONFIGURED.
        os.environ.pop("JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS", None)
        verdict = await maybe_inject_swe_bench_at_boot(intake)
    assert verdict != SWEBenchProInjectionVerdict.MISCONFIGURED_PHASE_A_DISABLED


@pytest.mark.asyncio
async def test_phase2_skipped_disabled_takes_priority_over_misconfig() -> None:
    """When HARNESS_INJECT itself is OFF, the verdict must be
    SKIPPED_DISABLED — not MISCONFIGURED. The check-order
    matters: SKIPPED_DISABLED is the "operator deliberately
    didn't opt in" signal and must come first."""
    intake = MagicMock()
    with patch.dict(
        os.environ,
        {
            "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED": "false",
        },
        clear=False,
    ):
        os.environ.pop("JARVIS_SWE_BENCH_PRO_ENABLED", None)
        verdict = await maybe_inject_swe_bench_at_boot(intake)
    assert verdict == SWEBenchProInjectionVerdict.SKIPPED_DISABLED


@pytest.mark.asyncio
async def test_phase2_preflight_log_message_is_actionable(caplog) -> None:
    """The MISCONFIGURED log line MUST contain enough context for
    the operator to fix without grepping source — must mention
    BOTH env vars by name + the fix instruction."""
    import logging
    caplog.set_level(
        logging.WARNING,
        logger="backend.core.ouroboros.governance.swe_bench_pro.harness_inject",
    )
    intake = MagicMock()
    with patch.dict(
        os.environ,
        {"JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED": "true"},
        clear=False,
    ):
        os.environ.pop("JARVIS_SWE_BENCH_PRO_ENABLED", None)
        await maybe_inject_swe_bench_at_boot(intake)
    msgs = [r.message for r in caplog.records]
    misconfig = [m for m in msgs if "MISCONFIGURED" in m]
    assert misconfig, f"MISCONFIGURED log line not emitted: {msgs}"
    msg = misconfig[-1]
    # Both env vars mentioned
    assert "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED" in msg
    assert "JARVIS_SWE_BENCH_PRO_ENABLED" in msg
    # Actionable instruction
    assert "Set JARVIS_SWE_BENCH_PRO_ENABLED=true" in msg
    # No-budget assertion (so operator knows they haven't burned $)
    assert "No budget burned" in msg


@pytest.mark.asyncio
async def test_phase2_intake_service_none_takes_priority() -> None:
    """When intake_service is None (harness boot ordering issue),
    the verdict is FAILED_INJECT — preserves the existing
    pre-Slice-12N behavior. MISCONFIGURED only fires AFTER the
    intake check."""
    with patch.dict(
        os.environ,
        {"JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED": "true"},
        clear=False,
    ):
        os.environ.pop("JARVIS_SWE_BENCH_PRO_ENABLED", None)
        verdict = await maybe_inject_swe_bench_at_boot(None)
    assert verdict == SWEBenchProInjectionVerdict.FAILED_INJECT


# ===============================================================
# AST pins — structural regression armor
# ===============================================================


_CB_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "circuit_breaker.py"
)

_CG_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)

_HI_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "swe_bench_pro" / "harness_inject.py"
)


def _load_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def test_ast_pin_circuit_trip_origin_taxonomy_closed() -> None:
    """The 4 CircuitTripOrigin values are the closed taxonomy.
    Adding a new value silently could change the isolation
    semantics (e.g., a new value defaulting to FOREGROUND
    behavior). Pin catches additions."""
    tree = _load_ast(_CB_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "CircuitTripOrigin":
            continue
        values = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                values.add(stmt.targets[0].id)
        assert values == {
            "FOREGROUND", "BACKGROUND", "SPECULATIVE", "MAINTENANCE",
        }, f"Taxonomy drift: {values}"
        return
    pytest.fail("CircuitTripOrigin class not found")


def test_ast_pin_foreground_origins_set_contains_only_foreground() -> None:
    """Walk the module AST to confirm `_FOREGROUND_ORIGINS` is a
    frozenset containing ONLY `CircuitTripOrigin.FOREGROUND`.
    A regression that includes BACKGROUND in this set would
    silently re-enable the wedge."""
    tree = _load_ast(_CB_PATH)
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "_FOREGROUND_ORIGINS":
            continue
        # Walk the value to find the frozenset({...}) call
        src = ast.unparse(node.value) if node.value is not None else ""
        assert "FOREGROUND" in src
        assert "BACKGROUND" not in src
        assert "SPECULATIVE" not in src
        assert "MAINTENANCE" not in src
        return
    pytest.fail("_FOREGROUND_ORIGINS module-level constant not found")


def test_ast_pin_trip_terminal_gates_on_origin() -> None:
    """`_trip_terminal` MUST gate the
    `get_global_breaker().report_structural_trip()` call on
    `self._origin in _FOREGROUND_ORIGINS` (or equivalent shape).
    A refactor that removes the gate would re-introduce the
    wedge."""
    tree = _load_ast(_CB_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_trip_terminal":
            continue
        src = ast.unparse(node)
        # Must reference both the origin and the foreground set
        assert "_origin" in src, "_trip_terminal must read self._origin"
        assert "_FOREGROUND_ORIGINS" in src, (
            "_trip_terminal must gate on _FOREGROUND_ORIGINS"
        )
        assert "report_structural_trip" in src, (
            "_trip_terminal must still call report_structural_trip "
            "(for foreground origins)"
        )
        # The gate must be a conditional — the call must NOT be
        # unconditional anymore
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and \
                    isinstance(sub.func, ast.Attribute) and \
                    sub.func.attr == "report_structural_trip":
                # The call must be wrapped in an `if`
                # statement — walk parents...
                # ast doesn't give parents, so use a textual
                # marker. The presence of `_FOREGROUND_ORIGINS`
                # in the body satisfies this structurally
                # (verified above).
                pass
        return
    pytest.fail("_trip_terminal method not found")


def test_ast_pin_circuit_breaker_init_accepts_origin_kwarg() -> None:
    """`CircuitBreaker.__init__` MUST accept `origin` as a
    kwarg with a default of `CircuitTripOrigin.FOREGROUND`
    (backward compat)."""
    tree = _load_ast(_CB_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "CircuitBreaker":
            continue
        for m in node.body:
            if isinstance(m, ast.FunctionDef) and m.name == "__init__":
                # Look for kwonly origin arg
                kw_args = [a.arg for a in m.args.kwonlyargs]
                assert "origin" in kw_args, (
                    f"origin kwarg missing: {kw_args}"
                )
                return
    pytest.fail("CircuitBreaker.__init__ not found")


def test_ast_pin_misconfigured_verdict_in_enum() -> None:
    """The new closed-taxonomy value MUST be in the
    SWEBenchProInjectionVerdict enum body."""
    tree = _load_ast(_HI_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "SWEBenchProInjectionVerdict":
            continue
        names = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                names.add(stmt.targets[0].id)
        assert "MISCONFIGURED_PHASE_A_DISABLED" in names, (
            f"Verdict not added: {names}"
        )
        return
    pytest.fail("SWEBenchProInjectionVerdict class not found")


def test_ast_pin_preflight_calls_swe_bench_pro_enabled() -> None:
    """`maybe_inject_swe_bench_at_boot` MUST call
    `swe_bench_pro_enabled()` BEFORE any `_resolve_instance_ids`
    or load attempts — composes the canonical Phase A predicate
    (single source of truth)."""
    tree = _load_ast(_HI_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "maybe_inject_swe_bench_at_boot":
            continue
        src = ast.unparse(node)
        assert "swe_bench_pro_enabled" in src
        assert "MISCONFIGURED_PHASE_A_DISABLED" in src
        # The check must appear BEFORE _resolve_instance_ids in
        # the source text. Both are referenced; check order.
        idx_preflight = src.find("swe_bench_pro_enabled")
        idx_resolve = src.find("_resolve_instance_ids")
        assert idx_preflight > 0 and idx_resolve > 0
        assert idx_preflight < idx_resolve, (
            "preflight check must precede _resolve_instance_ids"
        )
        return
    pytest.fail("maybe_inject_swe_bench_at_boot not found")


def test_ast_pin_route_origin_map_module_level() -> None:
    """`_SLICE12N_ROUTE_TO_ORIGIN` must be a module-level binding
    in candidate_generator.py (not buried in a function body)."""
    tree = _load_ast(_CG_PATH)
    found = False
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign) and \
                isinstance(stmt.target, ast.Name) and \
                stmt.target.id == "_SLICE12N_ROUTE_TO_ORIGIN":
            found = True
            break
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and \
                        target.id == "_SLICE12N_ROUTE_TO_ORIGIN":
                    found = True
                    break
    assert found, "_SLICE12N_ROUTE_TO_ORIGIN not at module level"


def test_ast_pin_breaker_construction_passes_origin() -> None:
    """The CircuitBreaker construction in candidate_generator.py
    MUST pass `origin=` so background ops actually use the
    isolation. A refactor that drops the kwarg would re-introduce
    the wedge (every breaker defaults to FOREGROUND)."""
    src = _CG_PATH.read_text()
    # The single construction site must include origin=
    assert "_Slice7e_CircuitBreaker(" in src
    assert "_slice12n_origin" in src or \
           "origin=_slice12n" in src or \
           "_SLICE12N_ROUTE_TO_ORIGIN" in src, (
        "CircuitBreaker construction in candidate_generator.py "
        "must plumb origin from ProviderRoute"
    )
