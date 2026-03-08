"""Tests for single pipeline_deadline owner at submit()."""
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    ServiceState,
)
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)


def test_governed_loop_config_has_pipeline_timeout_s():
    """GovernedLoopConfig has pipeline_timeout_s with default 600."""
    config = GovernedLoopConfig(project_root=Path("/tmp"))
    assert config.pipeline_timeout_s == 600.0


def test_governed_loop_config_pipeline_timeout_from_env(monkeypatch):
    """pipeline_timeout_s reads from JARVIS_PIPELINE_TIMEOUT_S env var."""
    monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "300")
    config = GovernedLoopConfig.from_env(project_root=Path("/tmp"))
    assert config.pipeline_timeout_s == 300.0


@pytest.mark.asyncio
async def test_submit_stamps_pipeline_deadline_in_ctx():
    """submit() stamps pipeline_deadline on ctx before passing to orchestrator."""
    config = GovernedLoopConfig(
        project_root=Path("/tmp"),
        pipeline_timeout_s=300.0,
    )
    stack = MagicMock()
    prime_client = MagicMock()
    svc = GovernedLoopService(stack=stack, prime_client=prime_client, config=config)
    svc._state = ServiceState.ACTIVE

    captured_ctx = []

    async def fake_orchestrator_run(ctx):
        captured_ctx.append(ctx)
        return ctx.advance(OperationPhase.CANCELLED)

    mock_orchestrator = MagicMock()
    mock_orchestrator.run = fake_orchestrator_run
    svc._orchestrator = mock_orchestrator

    ctx = OperationContext.create(target_files=("foo.py",), description="test")
    assert ctx.pipeline_deadline is None  # not stamped yet

    before = datetime.now(tz=timezone.utc)
    await svc.submit(ctx, trigger_source="test")
    after = datetime.now(tz=timezone.utc)

    assert len(captured_ctx) == 1
    dl = captured_ctx[0].pipeline_deadline
    assert dl is not None
    assert before + timedelta(seconds=299) < dl < after + timedelta(seconds=301)
