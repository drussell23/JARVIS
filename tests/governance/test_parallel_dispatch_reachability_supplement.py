"""Wave 3 (6) Slice 5a — reachability supplement (operator-authorized 2026-04-23).

Purpose
-------
Live battle-test runs (S1 `bt-2026-04-24-021024`, S2 `bt-2026-04-24-030628`,
S3 `bt-2026-04-24-044547`) cannot route the forced-reach multi-file seed
through the post-GENERATE parallel_dispatch seam because BacklogSensor's
source-type mapping routes its emissions BACKGROUND by default — even with
F3 stamping urgency=critical on the envelope. BG route disqualifies
parallel_dispatch regardless of candidate shape; additionally, the
candidate came back as 1-file in S3 (`PlanGenerator Skipping plan ...
trivial_op: 1 file(s)`) — double disqualification.

The eligibility logic is well-tested in isolation by:
- `test_parallel_dispatch_eligibility.py`   (FanoutEligibility matrix)
- `test_parallel_dispatch_graph_build.py`   (ExecutionGraph construction)
- `test_parallel_dispatch_enforce.py`       (enforce_evaluate_fanout on stubs)
- `test_parallel_dispatch_shadow_wiring.py` (phase_dispatcher shadow hook)

What they do NOT prove under a realistic dispatch_pipeline walk is:

    post-GENERATE seam wiring in phase_dispatcher.py correctly invokes
    enforce_evaluate_fanout exactly once, given master+enforce flags on
    AND the GENERATE runner emitted a multi-file generation artifact.

This supplement exercises exactly that path:

    dispatch_pipeline
      → GENERATE runner (stub, emits multi-file generation)
      → post-runner merge_artifacts (pctx.generation populated)
      → post-GENERATE hook (dispatch_phase == GENERATE + pctx.generation)
      → enforce_evaluate_fanout(op_id, pctx.generation, scheduler)
      → [ParallelDispatch] eligibility log + [ParallelDispatch enforce_submit_start] log
      → pctx.extras["parallel_dispatch_fanout_result"] = FanoutResult(COMPLETED)

Additive evidence, not a substitute for live fan-out. Ledger tag:
`reachability_supplement=test_harness`. Classification rule: this test
green == proof the post-GENERATE seam wiring is intact; does NOT count
toward Wave 3 (6) Slice 5a graduation cadence (which requires live
[ParallelDispatch] markers from real sensor-driven ops).

See `memory/project_wave3_item6_graduation_matrix.md` for the
graduation ledger this supplement is tagged against.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision as MemoryFanoutDecision,
    MemoryPressureGate,
    PressureLevel,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    FanoutOutcome,
    FanoutResult,
)
from backend.core.ouroboros.governance.phase_dispatcher import (
    PhaseContext,
    PhaseRunnerRegistry,
    dispatch_pipeline,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.posture import Posture


# ---------------------------------------------------------------------------
# Forced-reach seed targets — match tests/fixtures/wave3_forced_reach_seed.json
# exactly so this supplement is the test-harness analogue of the live seed.
# ---------------------------------------------------------------------------

_SEED_TARGETS: Tuple[str, ...] = (
    "backend/core/ouroboros/architect/__init__.py",
    "backend/core/tui/__init__.py",
    "backend/core/umf/__init__.py",
)


def _seed_multi_file_candidate() -> Dict[str, Any]:
    """Candidate dict mirroring the forced-reach seed's 3-file shape."""
    return {
        "files": [
            {
                "file_path": path,
                "full_content": f'"""Module for {path}."""\n',
                "rationale": f"seed docstring for {path}",
            }
            for path in _SEED_TARGETS
        ],
    }


# ---------------------------------------------------------------------------
# Stub GENERATE runner — emits the multi-file generation artifact.
# ---------------------------------------------------------------------------


class _StubGenerateRunner(PhaseRunner):
    """Minimal runner: emit a multi-file GenerationResult and terminate.

    The post-GENERATE hook in phase_dispatcher.dispatch_pipeline fires
    AFTER the runner returns + artifacts are merged into pctx.generation.
    By returning next_phase=None we force the dispatcher to terminate
    after exactly one phase iteration, so the hook fires exactly once.
    """

    phase = OperationPhase.GENERATE

    async def run(self, ctx: OperationContext) -> PhaseResult:
        generation = GenerationResult(
            candidates=(_seed_multi_file_candidate(),),
            provider_name="test-stub",
            generation_duration_s=0.0,
            model_id="test-stub-model",
            is_noop=False,
            tool_execution_records=(),
        )
        # next_phase=None → dispatcher terminates after post-GENERATE hook.
        return PhaseResult(
            next_ctx=ctx,
            next_phase=None,
            status="ok",
            reason="stub_terminal",
            artifacts={"generation": generation},
        )


def _stub_generate_factory(
    orch: Any, serpent: Any, pctx: PhaseContext, ctx: OperationContext,
) -> PhaseRunner:
    return _StubGenerateRunner()


# ---------------------------------------------------------------------------
# Scheduler + memory gate + posture fixtures — reuse enforce-test patterns.
# ---------------------------------------------------------------------------


class _FakeScheduler:
    """Async SubagentScheduler contract stub.

    Same shape as tests/governance/test_parallel_dispatch_enforce.py's
    _FakeScheduler. Kept local to this file rather than extracted to a
    conftest because the supplement is self-contained by design.
    """

    def __init__(
        self,
        *,
        terminal_phase: GraphExecutionPhase = GraphExecutionPhase.COMPLETED,
        unit_tallies: Tuple[int, int, int] = (3, 0, 0),
    ) -> None:
        self.terminal_phase = terminal_phase
        self.unit_tallies = unit_tallies
        self.submitted_graphs: list = []
        self.wait_calls: list = []

    async def submit(self, graph: ExecutionGraph) -> bool:
        self.submitted_graphs.append(graph)
        return True

    async def wait_for_graph(
        self, graph_id: str, timeout_s: Optional[float] = None,
    ) -> GraphExecutionState:
        self.wait_calls.append((graph_id, timeout_s))
        graph = self.submitted_graphs[-1]
        n_c, n_f, n_x = self.unit_tallies
        completed = tuple(u.unit_id for u in graph.units[:n_c])
        failed = tuple(u.unit_id for u in graph.units[n_c:n_c + n_f])
        cancelled = tuple(
            u.unit_id for u in graph.units[n_c + n_f:n_c + n_f + n_x]
        )
        return GraphExecutionState(
            graph=graph,
            phase=self.terminal_phase,
            completed_units=completed,
            failed_units=failed,
            cancelled_units=cancelled,
            last_error="",
        )


def _ok_gate() -> MemoryPressureGate:
    """MemoryPressureGate that always allows the requested concurrency."""
    gate = MagicMock(spec=MemoryPressureGate)

    def _cf(n: int) -> MemoryFanoutDecision:
        return MemoryFanoutDecision(
            allowed=True,
            n_requested=n,
            n_allowed=n,
            level=PressureLevel.OK,
            free_pct=60.0,
            reason_code="mock_ok",
            source="test",
        )

    gate.can_fanout.side_effect = _cf
    return gate


def _maintain_posture():
    def _fn() -> Tuple[Optional[Posture], Optional[float]]:
        return Posture.MAINTAIN, 0.9
    return _fn


# ---------------------------------------------------------------------------
# Orchestrator stub — only fields dispatch_pipeline reads on the post-GENERATE
# hot path (specifically: orchestrator._subagent_scheduler).
# ---------------------------------------------------------------------------


@dataclass
class _StubOrchestrator:
    _subagent_scheduler: Any = None


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def _build_generate_ctx(op_id: str) -> OperationContext:
    """Start in CLASSIFY then advance to GENERATE (the valid transition
    path per PHASE_TRANSITIONS; trivial_op direct-to-GENERATE)."""
    ts = datetime(2026, 4, 23, 22, 0, 0, tzinfo=timezone.utc)
    ctx = OperationContext.create(
        target_files=_SEED_TARGETS,
        description="Wave 3 (6) Slice 5a reachability supplement — forced-reach seed equivalent",
        op_id=op_id,
        _timestamp=ts,
    )
    # Walk CLASSIFY → ROUTE → GENERATE per PHASE_TRANSITIONS (ROUTE
    # has GENERATE as a direct fast-path destination). We skip the
    # CLASSIFY + ROUTE runners entirely because this supplement exists
    # to exercise the post-GENERATE seam, not the upstream classifier
    # or router.
    ctx = ctx.advance(new_phase=OperationPhase.ROUTE, _timestamp=ts)
    ctx = ctx.advance(new_phase=OperationPhase.GENERATE, _timestamp=ts)
    return ctx


@pytest.mark.asyncio
async def test_dispatch_pipeline_post_generate_fires_enforce_evaluate_fanout_once(
    monkeypatch, caplog,
):
    """REACHABILITY SUPPLEMENT — proves the post-GENERATE seam.

    Wiring invariants this asserts:

    1. `[ParallelDispatch]` eligibility log emits exactly once (from
       is_fanout_eligible → FanoutEligibility.log_line).
    2. `[ParallelDispatch enforce_submit_start]` log emits exactly once
       (from enforce_evaluate_fanout's pre-submit log at lines 1398-1406
       of parallel_dispatch.py).
    3. scheduler.submit is called exactly once.
    4. scheduler.wait_for_graph is called exactly once.
    5. The ExecutionGraph built has 3 units (matching the 3 forced-reach
       seed target files).
    6. pctx.extras["parallel_dispatch_fanout_result"] is a FanoutResult
       with outcome=COMPLETED and no error.
    7. Graph concurrency_limit == 3 (posture=MAINTAIN weight=1.0 × n=3;
       memory OK; max_units env default 3).
    """
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    # Explicit gate-override injection: the enforce path consults
    # default gate + posture via module-level singletons unless test
    # monkey-patches them. We patch the parallel_dispatch module
    # getters so the supplement is hermetic (doesn't read real posture
    # store or run the /proc/meminfo probe).
    import backend.core.ouroboros.governance.parallel_dispatch as pd_mod
    monkeypatch.setattr(pd_mod, "get_default_gate", _ok_gate)
    monkeypatch.setattr(pd_mod, "_default_posture_fn", _maintain_posture())

    caplog.set_level(logging.INFO, logger="Ouroboros.ParallelDispatch")

    # Build stubs.
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.COMPLETED,
        unit_tallies=(3, 0, 0),
    )
    orchestrator = _StubOrchestrator(_subagent_scheduler=scheduler)
    registry = PhaseRunnerRegistry()
    registry.register(OperationPhase.GENERATE, _stub_generate_factory)
    pctx = PhaseContext()
    ctx = _build_generate_ctx("op-wave3-slice5a-supplement-001")

    # Drive the pipeline. Terminates after one GENERATE iteration
    # (_StubGenerateRunner returns next_phase=None).
    final_ctx = await dispatch_pipeline(
        orchestrator,
        None,
        ctx,
        registry=registry,
        initial_context=pctx,
    )

    # ---- Assertion 1 + 2: both telemetry lines emitted exactly once ----
    pd_messages = [
        r.message for r in caplog.records
        if "[ParallelDispatch]" in r.message or "enforce_submit_start" in r.message
    ]
    eligibility_lines = [m for m in pd_messages if "[ParallelDispatch] op=" in m]
    submit_lines = [m for m in pd_messages if "enforce_submit_start" in m]
    assert len(eligibility_lines) == 1, (
        f"expected exactly 1 [ParallelDispatch] eligibility log, "
        f"got {len(eligibility_lines)}: {eligibility_lines}"
    )
    assert len(submit_lines) == 1, (
        f"expected exactly 1 [ParallelDispatch enforce_submit_start] log, "
        f"got {len(submit_lines)}: {submit_lines}"
    )
    # Ledger-parseable format: graph_id=graph-... + plan_digest= + concurrency_limit=3 + n_units=3
    submit_line = submit_lines[0]
    assert "graph_id=graph-" in submit_line, submit_line
    assert re.search(r"plan_digest=[0-9a-f]{12}", submit_line), submit_line
    assert "concurrency_limit=3" in submit_line, submit_line
    assert "n_units=3" in submit_line, submit_line

    # ---- Assertion 3 + 4: scheduler was touched exactly once per path ----
    assert len(scheduler.submitted_graphs) == 1, (
        f"expected scheduler.submit() exactly once, "
        f"got {len(scheduler.submitted_graphs)}"
    )
    assert len(scheduler.wait_calls) == 1, (
        f"expected scheduler.wait_for_graph() exactly once, "
        f"got {len(scheduler.wait_calls)}"
    )

    # ---- Assertion 5: graph built with 3 units (one per seed target) ----
    graph = scheduler.submitted_graphs[0]
    assert len(graph.units) == 3, (
        f"expected 3 work units, got {len(graph.units)}"
    )
    unit_targets = sorted(
        file_path
        for u in graph.units
        for file_path in u.target_files
    )
    assert unit_targets == sorted(_SEED_TARGETS), (
        f"expected work units to cover seed targets, "
        f"got {unit_targets} vs {sorted(_SEED_TARGETS)}"
    )

    # ---- Assertion 6: FanoutResult stashed in pctx.extras ----
    fanout_result = pctx.extras.get("parallel_dispatch_fanout_result")
    assert fanout_result is not None, (
        f"expected pctx.extras['parallel_dispatch_fanout_result'] to be "
        f"set by post-GENERATE hook; got extras={pctx.extras}"
    )
    assert isinstance(fanout_result, FanoutResult), (
        f"expected FanoutResult, got {type(fanout_result).__name__}"
    )
    assert fanout_result.outcome == FanoutOutcome.COMPLETED, (
        f"expected outcome=COMPLETED, got {fanout_result.outcome}"
    )
    assert not fanout_result.error, (
        f"expected empty error, got {fanout_result.error!r}"
    )

    # ---- Assertion 7: concurrency_limit matches n=3, OK memory, MAINTAIN ----
    assert graph.concurrency_limit == 3, (
        f"expected concurrency_limit=3 (posture=MAINTAIN w=1.0 × n=3, "
        f"memory OK, max_units default 3), got {graph.concurrency_limit}"
    )

    # ---- Dispatcher terminated after the GENERATE iteration ----
    assert final_ctx is not None


@pytest.mark.asyncio
async def test_dispatch_pipeline_flags_off_skips_post_generate_hook(
    monkeypatch, caplog,
):
    """Negative control: flags off → no [ParallelDispatch] logs,
    scheduler never touched, no fanout_result stashed.

    Proves the hook is properly gated and doesn't fire when master is off.
    Byte-identical to a pre-Wave-3 dispatch_pipeline run.
    """
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="Ouroboros.ParallelDispatch")

    scheduler = _FakeScheduler()
    orchestrator = _StubOrchestrator(_subagent_scheduler=scheduler)
    registry = PhaseRunnerRegistry()
    registry.register(OperationPhase.GENERATE, _stub_generate_factory)
    pctx = PhaseContext()
    ctx = _build_generate_ctx("op-wave3-slice5a-supplement-negctl-001")

    await dispatch_pipeline(
        orchestrator, None, ctx, registry=registry, initial_context=pctx,
    )

    pd_messages = [r.message for r in caplog.records if "[ParallelDispatch]" in r.message]
    assert pd_messages == [], f"expected no ParallelDispatch logs, got {pd_messages}"
    assert scheduler.submitted_graphs == []
    assert scheduler.wait_calls == []
    assert "parallel_dispatch_fanout_result" not in pctx.extras
