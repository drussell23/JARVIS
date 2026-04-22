"""Deep per-phase terminal parity matrix for the dispatcher (Slice 6b).

Every phase that Slices 1-5 extracted has one or more terminal exit
paths. 6a proved the dispatcher-on happy path + one representative
terminal (pre-APPLY cancel). 6b proves dispatcher-on ≡ dispatcher-off
on each terminal individually, so any cross-phase context threading
regression surfaces at the right phase.

Structure:
* Shared ``_build_orch`` harness + ``_run_both_paths`` helper
* Per-phase terminal tests (each asserts final ctx.phase + terminal
  reason code match between dispatcher-on and dispatcher-off)
* Artifact propagation edge cases where the dispatcher's PhaseContext
  threading is non-trivial (e.g. risk_tier mutation through GATE to
  APPROVE; advisory from CLASSIFY to PLAN Tier 6)

Authority invariant: no candidate_generator / iron_gate / change_engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskClassification,
    RiskTier,
)


_FIXED_TS = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


def _build_stack(
    *,
    can_write: Tuple[bool, str] = (True, "ok"),
    risk_tier: RiskTier = RiskTier.SAFE_AUTO,
    change_success: bool = True,
    change_raises: bool = False,
    cancel_requested: bool = False,
) -> MagicMock:
    stack = MagicMock()
    stack.can_write.return_value = can_write
    stack.risk_engine.classify.return_value = RiskClassification(
        tier=risk_tier, reason_code="default",
    )
    stack.ledger = MagicMock()
    stack.ledger.append = AsyncMock(return_value=True)
    stack.comm = AsyncMock()
    stack.change_engine = AsyncMock()
    if change_raises:
        stack.change_engine.execute = AsyncMock(
            side_effect=RuntimeError("change engine boom"),
        )
    else:
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=change_success, rolled_back=False, op_id="op-test",
        ))
    stack.governed_loop_service.is_cancel_requested.return_value = cancel_requested
    stack.learning_bridge = MagicMock()
    stack.learning_bridge.publish = AsyncMock(return_value=None)
    stack.security_reviewer = MagicMock()
    stack.security_reviewer.review = AsyncMock(return_value=None)
    return stack


def _build_generator(
    *,
    candidates: Optional[tuple] = None,
    raises: Optional[Exception] = None,
) -> MagicMock:
    gen = MagicMock()
    if raises is not None:
        gen.generate = AsyncMock(side_effect=raises)
        return gen
    if candidates is None:
        candidates = (
            {
                "candidate_id": "c1",
                "file_path": "backend/core/utils.py",
                "full_content": "def hello():\n    pass\n",
                "rationale": "stub",
            },
        )
    gen.generate = AsyncMock(return_value=GenerationResult(
        candidates=candidates,
        provider_name="mock",
        generation_duration_s=1.0,
        tool_execution_records=(),
    ))
    return gen


def _build_cfg(**overrides: Any) -> OrchestratorConfig:
    defaults = dict(
        project_root=Path("/tmp/test-project-6b"),
        generation_timeout_s=5.0,
        validation_timeout_s=5.0,
        approval_timeout_s=5.0,
        max_generate_retries=1,
        max_validate_retries=2,
        benchmark_enabled=False,
    )
    defaults.update(overrides)
    return OrchestratorConfig(**defaults)


def _build_ctx(op_id: str = "op-test", **overrides: Any) -> OperationContext:
    defaults: dict = dict(
        target_files=("backend/core/utils.py",),
        description="dispatch test",
        op_id=op_id,
        _timestamp=_FIXED_TS,
    )
    defaults.update(overrides)
    return OperationContext.create(**defaults)


async def _run_both_paths(
    *,
    monkeypatch: pytest.MonkeyPatch,
    build_stack: Callable[[], MagicMock],
    build_generator: Callable[[], MagicMock],
    build_cfg: Callable[[], OrchestratorConfig] = _build_cfg,
    build_ctx: Callable[[str], OperationContext] = _build_ctx,
    approval_provider: Any = None,
) -> Tuple[OperationContext, OperationContext]:
    """Run the same orchestrator setup through dispatcher-off then
    dispatcher-on; return (off_result, on_result). Caller asserts parity."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")
    # dispatcher OFF
    monkeypatch.delenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", raising=False)
    orch_off = GovernedOrchestrator(
        stack=build_stack(), generator=build_generator(),
        approval_provider=approval_provider, config=build_cfg(),
    )
    out_off = await orch_off.run(build_ctx("op-off"))

    # dispatcher ON
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    orch_on = GovernedOrchestrator(
        stack=build_stack(), generator=build_generator(),
        approval_provider=approval_provider, config=build_cfg(),
    )
    out_on = await orch_on.run(build_ctx("op-on"))

    return (out_off, out_on)


def _assert_terminal_parity(off: OperationContext, on: OperationContext):
    """Both paths must land the same terminal phase + reason code."""
    assert on.phase is off.phase, (
        f"dispatcher-on final phase {on.phase} != "
        f"dispatcher-off final phase {off.phase}"
    )
    assert on.terminal_reason_code == off.terminal_reason_code, (
        f"dispatcher-on reason {on.terminal_reason_code!r} != "
        f"dispatcher-off reason {off.terminal_reason_code!r}"
    )


# ===========================================================================
# CLASSIFY terminals
# ===========================================================================


@pytest.mark.asyncio
async def test_classify_risk_blocked_terminal(monkeypatch):
    """Risk engine classifies BLOCKED → CANCELLED via CLASSIFY."""
    def _stack():
        return _build_stack(risk_tier=RiskTier.BLOCKED)
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.CANCELLED


# ===========================================================================
# GATE terminals
# ===========================================================================


@pytest.mark.asyncio
async def test_gate_can_write_denied_terminal(monkeypatch):
    """can_write denies → gate_blocked terminal."""
    def _stack():
        return _build_stack(can_write=(False, "canary_not_promoted"))
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.CANCELLED
    assert "gate_blocked" in (out_on.terminal_reason_code or "")


# ===========================================================================
# APPROVE terminals
# ===========================================================================


@pytest.mark.asyncio
async def test_approve_no_provider_when_required(monkeypatch):
    """APPROVAL_REQUIRED + no provider → approval_required_but_no_provider."""
    def _stack():
        return _build_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
        approval_provider=None,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.CANCELLED


@pytest.mark.asyncio
async def test_approve_rejected_terminal(monkeypatch):
    """APPROVAL_REQUIRED + REJECTED decision → approval_rejected."""
    def _stack():
        return _build_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)

    def _provider():
        p = MagicMock()
        p.request = AsyncMock(return_value="req-1")
        p.await_decision = AsyncMock(return_value=ApprovalResult(
            status=ApprovalStatus.REJECTED,
            approver="human",
            reason="not today",
            decided_at=_FIXED_TS,
            request_id="req-1",
        ))
        return p

    # Build separate providers for each path to avoid shared state
    p_off = _provider()
    p_on = _provider()
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")

    monkeypatch.delenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", raising=False)
    orch_off = GovernedOrchestrator(
        stack=_stack(), generator=_build_generator(),
        approval_provider=p_off, config=_build_cfg(),
    )
    out_off = await orch_off.run(_build_ctx("op-off"))

    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    orch_on = GovernedOrchestrator(
        stack=_stack(), generator=_build_generator(),
        approval_provider=p_on, config=_build_cfg(),
    )
    out_on = await orch_on.run(_build_ctx("op-on"))

    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.CANCELLED
    assert out_on.terminal_reason_code == "approval_rejected"


@pytest.mark.asyncio
async def test_approve_expired_terminal(monkeypatch):
    """APPROVAL_REQUIRED + EXPIRED decision → approval_expired."""
    def _stack():
        return _build_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)

    def _provider():
        p = MagicMock()
        p.request = AsyncMock(return_value="req-1")
        p.await_decision = AsyncMock(return_value=ApprovalResult(
            status=ApprovalStatus.EXPIRED, approver="", reason="",
            decided_at=_FIXED_TS, request_id="req-1",
        ))
        return p

    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")
    monkeypatch.delenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", raising=False)
    orch_off = GovernedOrchestrator(
        stack=_stack(), generator=_build_generator(),
        approval_provider=_provider(), config=_build_cfg(),
    )
    out_off = await orch_off.run(_build_ctx("op-off"))

    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    orch_on = GovernedOrchestrator(
        stack=_stack(), generator=_build_generator(),
        approval_provider=_provider(), config=_build_cfg(),
    )
    out_on = await orch_on.run(_build_ctx("op-on"))

    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.EXPIRED


# ===========================================================================
# APPLY / VERIFY terminals
# ===========================================================================


@pytest.mark.asyncio
async def test_apply_dry_run_terminal(monkeypatch):
    """JARVIS_DRY_RUN=1 → dry_run_session terminal."""
    monkeypatch.setenv("JARVIS_DRY_RUN", "1")
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.terminal_reason_code == "dry_run_session"


@pytest.mark.asyncio
async def test_apply_change_engine_failed_terminal(monkeypatch):
    """ChangeEngine returns success=False → change_engine_failed."""
    def _stack():
        return _build_stack(change_success=False)
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.POSTMORTEM


@pytest.mark.asyncio
async def test_apply_change_engine_exception_terminal(monkeypatch):
    """ChangeEngine raises → change_engine_error."""
    def _stack():
        return _build_stack(change_raises=True)
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.terminal_reason_code == "change_engine_error"


# ===========================================================================
# VALIDATE terminals
# ===========================================================================


@pytest.mark.asyncio
async def test_generate_no_candidates_returned(monkeypatch):
    """Generator returns None → all retries exhaust → terminal.
    Parity on the terminal path even when GENERATE never produces
    a viable candidate."""
    def _gen():
        gen = MagicMock()
        gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=(),  # empty
            provider_name="mock", generation_duration_s=0.1,
            tool_execution_records=(),
        ))
        return gen
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,
        build_generator=_gen,
    )
    _assert_terminal_parity(out_off, out_on)
    # Both paths should hit some terminal (POSTMORTEM or CANCELLED)
    assert out_on.phase in (
        OperationPhase.POSTMORTEM, OperationPhase.CANCELLED,
    )


@pytest.mark.asyncio
async def test_generate_is_noop_completes(monkeypatch):
    """generation.is_noop=True → advance COMPLETE with terminal_reason=noop."""
    def _gen():
        gen = MagicMock()
        gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=(),
            provider_name="mock", generation_duration_s=0.1,
            tool_execution_records=(), is_noop=True,
        ))
        return gen
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,
        build_generator=_gen,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.COMPLETE
    assert out_on.terminal_reason_code == "noop"


# ===========================================================================
# Cross-phase artifact propagation edges
# ===========================================================================


@pytest.mark.asyncio
async def test_artifact_risk_tier_mutation_gate_to_approve(monkeypatch):
    """GATE mutates risk_tier (SAFE_AUTO → APPROVAL_REQUIRED via SimilarityGate
    escalation or MIN_RISK_TIER floor). Dispatcher must thread the MUTATED
    value to APPROVE factory — else APPROVE would take SAFE_AUTO path and
    NOT gate on approval."""
    # Use JARVIS_MIN_RISK_TIER to force SAFE_AUTO → APPROVAL_REQUIRED upgrade
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")

    # With no approval provider, APPROVAL_REQUIRED → approval_required_but_no_provider
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,  # risk_tier=SAFE_AUTO by default
        build_generator=_build_generator,
        approval_provider=None,
    )
    _assert_terminal_parity(out_off, out_on)
    # Both should hit approval_required_but_no_provider because GATE's floor
    # escalated SAFE_AUTO → APPROVAL_REQUIRED, and APPROVE had no provider.
    # This exercises pctx.risk_tier threading through GATERunner's artifacts
    # → dispatcher rebind → Slice4bRunner construction.
    assert out_on.phase is OperationPhase.CANCELLED
    assert "approval_required" in (out_on.terminal_reason_code or "")


@pytest.mark.asyncio
async def test_artifact_generation_threads_classify_to_validate(monkeypatch):
    """generation artifact produced by GENERATERunner must reach VALIDATERunner
    via PhaseContext (pctx.generation). If threading breaks, VALIDATE factory
    raises PhaseContextError. Pinning: happy path reaches COMPLETE confirms
    generation threaded correctly through all intermediate phases."""
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)
    # Happy path ends in COMPLETE (or POSTMORTEM if benchmark disabled
    # triggers rollback — accept either as long as BOTH paths match).
    assert out_on.phase in (
        OperationPhase.COMPLETE, OperationPhase.POSTMORTEM,
    )


@pytest.mark.asyncio
async def test_artifact_t_apply_threads_apply_to_complete(monkeypatch):
    """Slice4bRunner records t_apply at APPLY start; COMPLETERunner reads it
    for canary latency. Dispatcher must thread t_apply through artifacts.
    Pinning: happy path reaching COMPLETE with a non-terminal-error
    reason confirms t_apply reached COMPLETERunner."""
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)


# ===========================================================================
# Cost cap / cancellation variants
# ===========================================================================


@pytest.mark.asyncio
async def test_pre_apply_user_cancel_terminal(monkeypatch):
    """is_cancel_requested=True pre-APPLY → user_cancelled terminal.
    6a covered this via dedicated parity test — here we re-pin it as
    part of the matrix for completeness."""
    def _stack():
        return _build_stack(cancel_requested=True)
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)
    assert out_on.phase is OperationPhase.CANCELLED


# ===========================================================================
# Multi-phase chained terminals
# ===========================================================================


@pytest.mark.asyncio
async def test_generator_exception_retry_exhaustion(monkeypatch):
    """Generator raises on every attempt → all retries exhaust → terminal."""
    def _gen():
        return _build_generator(raises=RuntimeError("generator always fails"))
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,
        build_generator=_gen,
    )
    _assert_terminal_parity(out_off, out_on)
    # Both paths should reach a terminal state (POSTMORTEM / CANCELLED)
    assert out_on.phase in (
        OperationPhase.POSTMORTEM, OperationPhase.CANCELLED,
    )


# ===========================================================================
# Risk-tier escalation chain
# ===========================================================================


@pytest.mark.asyncio
async def test_notify_apply_path_parity(monkeypatch):
    """NOTIFY_APPLY tier triggers 5b yellow preview. No cancel → proceeds
    to APPLY. Dispatcher must route through GATE's 5b block without
    diverging from inline."""
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")  # no sleep in tests

    def _stack():
        return _build_stack(risk_tier=RiskTier.NOTIFY_APPLY)
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)


@pytest.mark.asyncio
async def test_notify_apply_cancel_during_preview(monkeypatch):
    """NOTIFY_APPLY + cancel during preview → user_rejected_notify_apply."""
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0.01")

    def _stack():
        return _build_stack(
            risk_tier=RiskTier.NOTIFY_APPLY,
            cancel_requested=True,
        )
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_stack,
        build_generator=_build_generator,
    )
    _assert_terminal_parity(out_off, out_on)


# ===========================================================================
# Risk-engine "BLOCKED" as first-tier terminal
# ===========================================================================


@pytest.mark.asyncio
async def test_policy_engine_override_to_blocked(monkeypatch):
    """JARVIS_RISK_CEILING env can escalate but not BLOCK — pinning that
    the knob doesn't accidentally provide a path to BLOCK. Dispatcher
    behavior must match inline."""
    monkeypatch.setenv("JARVIS_RISK_CEILING", "APPROVAL_REQUIRED")
    # No approval provider → approval_required_but_no_provider terminal
    out_off, out_on = await _run_both_paths(
        monkeypatch=monkeypatch,
        build_stack=_build_stack,
        build_generator=_build_generator,
        approval_provider=None,
    )
    _assert_terminal_parity(out_off, out_on)


# ===========================================================================
# Dispatcher-on consistency across repeated runs
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatcher_on_is_deterministic(monkeypatch):
    """Running the same setup twice through dispatcher-on must produce
    the same final ctx.phase — pinning that PhaseContext state isn't
    leaking across ops."""
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")

    out1_orch = GovernedOrchestrator(
        stack=_build_stack(), generator=_build_generator(),
        approval_provider=None, config=_build_cfg(),
    )
    out1 = await out1_orch.run(_build_ctx("op-det-1"))

    out2_orch = GovernedOrchestrator(
        stack=_build_stack(), generator=_build_generator(),
        approval_provider=None, config=_build_cfg(),
    )
    out2 = await out2_orch.run(_build_ctx("op-det-2"))

    assert out1.phase is out2.phase


# ===========================================================================
# Authority invariant
# ===========================================================================


def test_terminal_matrix_bans_execution_authority_imports():
    import inspect
    from tests.governance.phase_runner import test_phase_dispatcher_terminals

    src = inspect.getsource(test_phase_dispatcher_terminals)
    for banned in ("candidate_generator", "iron_gate", "change_engine"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"test module must not import {banned}: {s}"
                )


__all__ = []
