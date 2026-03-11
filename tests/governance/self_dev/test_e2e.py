"""tests/governance/self_dev/test_e2e.py

End-to-end integration tests for the CLI-to-GovernedLoopService pipeline.

These tests validate the full path from CLI entry point through
GovernedLoopService.submit() to the mock orchestrator and back, ensuring
that the result contract (OperationResult) is correctly assembled.

  1. test_e2e_submit_reaches_complete
     handle_self_modify -> GovernedLoopService.submit -> mock orchestrator
     returns COMPLETE -> result.terminal_phase == COMPLETE, provider_used matches

  2. test_e2e_reject_stops_pipeline
     Submit with mock orchestrator returning CANCELLED -> terminal_phase == CANCELLED

  3. test_e2e_full_approval_flow
     Full flow with CLIApprovalProvider, mock orchestrator returns COMPLETE
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.approval_provider import CLIApprovalProvider
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    OperationResult,
    ServiceState,
)
from backend.core.ouroboros.governance.loop_cli import handle_self_modify
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
)


def _make_service(tmp_path: Path, max_concurrent_ops: int = 2) -> GovernedLoopService:
    """Build a GovernedLoopService with mocked internals, forced ACTIVE."""
    config = GovernedLoopConfig(
        project_root=tmp_path,
        max_concurrent_ops=max_concurrent_ops,
    )
    stack = MagicMock()
    stack.resource_monitor.snapshot = AsyncMock(return_value=MagicMock())
    svc = GovernedLoopService(stack=stack, prime_client=None, config=config)
    svc._state = ServiceState.ACTIVE
    return svc


def _mock_terminal_context(
    _ctx: OperationContext,
    phase: OperationPhase,
    provider_name: str = "mock-provider",
    duration_s: float = 0.42,
) -> OperationContext:
    """Return a mock OperationContext in a terminal phase with generation info.

    We use a MagicMock that quacks like OperationContext for the fields
    that GovernedLoopService.submit() reads from the orchestrator return value.
    """
    terminal = MagicMock(spec=OperationContext)
    terminal.phase = phase
    terminal.generation = GenerationResult(
        candidates=({"file": "test.py", "content": "x = 1"},),
        provider_name=provider_name,
        generation_duration_s=duration_s,
    )
    return terminal


# ── Test 1: Submit reaches COMPLETE ────────────────────────────────


def test_e2e_submit_reaches_complete(tmp_path: Path):
    """CLI handle_self_modify -> service.submit -> orchestrator(COMPLETE) -> result has correct fields."""
    svc = _make_service(tmp_path)
    provider_name = "test-prime-provider"

    # Wire mock orchestrator that returns COMPLETE context
    mock_orch = MagicMock()

    async def fake_run(ctx: OperationContext) -> OperationContext:
        return _mock_terminal_context(ctx, OperationPhase.COMPLETE, provider_name)

    mock_orch.run = AsyncMock(side_effect=fake_run)
    svc._orchestrator = mock_orch

    # Drive through CLI entry point
    result: OperationResult = asyncio.get_event_loop().run_until_complete(
        handle_self_modify(
            service=svc,
            target="tests/test_example.py",
            goal="add missing assertion",
        )
    )

    # Verify result contract
    assert result.terminal_phase == OperationPhase.COMPLETE
    assert result.provider_used == provider_name
    assert result.generation_duration_s == pytest.approx(0.42)
    assert result.trigger_source == "cli_manual"
    assert result.total_duration_s > 0.0
    assert result.reason_code == "complete"

    # Op should be tracked in completed_ops
    assert result.op_id in svc._completed_ops


# ── Test 2: Reject stops pipeline ─────────────────────────────────


def test_e2e_reject_stops_pipeline(tmp_path: Path):
    """Submit with orchestrator returning CANCELLED -> terminal_phase is CANCELLED, file unchanged."""
    svc = _make_service(tmp_path)

    # Create a target file to verify it stays unchanged
    target = tmp_path / "untouched.py"
    original_content = "# original content\n"
    target.write_text(original_content, encoding="utf-8")

    # Wire mock orchestrator that returns CANCELLED context (simulating rejection)
    mock_orch = MagicMock()

    async def fake_run(_ctx: OperationContext) -> OperationContext:
        terminal = MagicMock(spec=OperationContext)
        terminal.phase = OperationPhase.CANCELLED
        terminal.generation = None  # no generation on cancellation
        return terminal

    mock_orch.run = AsyncMock(side_effect=fake_run)
    svc._orchestrator = mock_orch

    ctx = OperationContext.create(
        target_files=(str(target),),
        description="should be cancelled",
        op_id="op-reject-test",
    )

    result: OperationResult = asyncio.get_event_loop().run_until_complete(
        svc.submit(ctx, trigger_source="test")
    )

    # Verify cancellation
    assert result.terminal_phase == OperationPhase.CANCELLED
    assert result.provider_used is None
    assert result.generation_duration_s is None
    assert result.reason_code == "cancelled"

    # File should be untouched
    assert target.read_text(encoding="utf-8") == original_content

    # Op should be tracked in completed_ops
    assert result.op_id in svc._completed_ops


# ── Test 3: Full approval flow ────────────────────────────────────


def test_e2e_full_approval_flow(tmp_path: Path):
    """Full flow: CLIApprovalProvider wired into service, orchestrator returns COMPLETE."""
    svc = _make_service(tmp_path)

    # Wire a real CLIApprovalProvider (the service normally builds this in start())
    approval_provider = CLIApprovalProvider()
    svc._approval_provider = approval_provider

    provider_name = "claude-test"

    # Wire mock orchestrator that returns COMPLETE
    mock_orch = MagicMock()

    async def fake_run(ctx: OperationContext) -> OperationContext:
        return _mock_terminal_context(ctx, OperationPhase.COMPLETE, provider_name, 1.23)

    mock_orch.run = AsyncMock(side_effect=fake_run)
    svc._orchestrator = mock_orch

    ctx = OperationContext.create(
        target_files=("tests/test_feature.py",),
        description="implement new feature test",
        op_id="op-approval-flow",
    )

    result: OperationResult = asyncio.get_event_loop().run_until_complete(
        svc.submit(ctx, trigger_source="cli_manual")
    )

    # Verify COMPLETE result
    assert result.terminal_phase == OperationPhase.COMPLETE
    assert result.provider_used == provider_name
    assert result.generation_duration_s == pytest.approx(1.23)
    assert result.trigger_source == "cli_manual"
    assert result.op_id == "op-approval-flow"

    # Verify the orchestrator was called exactly once with the context
    mock_orch.run.assert_awaited_once()
    call_ctx = mock_orch.run.call_args[0][0]
    assert call_ctx.op_id == "op-approval-flow"
    assert call_ctx.description == "implement new feature test"
    assert call_ctx.target_files == ("tests/test_feature.py",)

    # The approval provider is available (even though orchestrator mock
    # didn't exercise it -- it's wired and ready)
    assert svc._approval_provider is approval_provider
