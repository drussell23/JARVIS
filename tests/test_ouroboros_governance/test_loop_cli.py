"""Tests for governed loop CLI commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)


_FIXED_TS = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)


def _make_context(
    *,
    op_id: str = "op-test-001",
    description: str = "Add tests",
    target_files: Tuple[str, ...] = ("tests/test_foo.py",),
) -> OperationContext:
    return OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
        _timestamp=_FIXED_TS,
    )


def _mock_service(
    terminal_phase: OperationPhase = OperationPhase.COMPLETE,
) -> MagicMock:
    from backend.core.ouroboros.governance.governed_loop_service import (
        OperationResult,
        ServiceState,
    )

    service = MagicMock()
    service.state = ServiceState.ACTIVE
    service.submit = AsyncMock(
        return_value=OperationResult(
            op_id="op-test-001",
            terminal_phase=terminal_phase,
            provider_used="gcp-jprime",
            total_duration_s=5.2,
            reason_code=terminal_phase.name.lower(),
            trigger_source="cli",
        )
    )
    service._approval_provider = MagicMock()
    service._approval_provider.approve = AsyncMock(
        return_value=MagicMock(status=MagicMock(name="APPROVED"))
    )
    service._approval_provider.reject = AsyncMock(
        return_value=MagicMock(status=MagicMock(name="REJECTED"))
    )
    return service


@pytest.mark.asyncio
class TestSelfModifyCommand:
    """Tests for the self-modify CLI command logic."""

    async def test_self_modify_succeeds(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_self_modify

        service = _mock_service()
        result = await handle_self_modify(
            service=service,
            target="tests/test_foo.py",
            goal="Add edge case tests",
        )
        assert result.terminal_phase is OperationPhase.COMPLETE
        service.submit.assert_called_once()

    async def test_self_modify_returns_result_on_cancel(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_self_modify

        service = _mock_service(terminal_phase=OperationPhase.CANCELLED)
        result = await handle_self_modify(
            service=service,
            target="tests/test_foo.py",
            goal="Fix test",
        )
        assert result.terminal_phase is OperationPhase.CANCELLED

    async def test_self_modify_with_no_service_raises(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_self_modify

        with pytest.raises(RuntimeError, match="not_active"):
            await handle_self_modify(
                service=None,
                target="tests/test_foo.py",
                goal="Fix test",
            )


@pytest.mark.asyncio
class TestApproveCommand:
    """Tests for the approve CLI command logic."""

    async def test_approve_calls_provider(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_approve

        service = _mock_service()
        result = await handle_approve(
            service=service,
            op_id="op-test-001",
            approver="derek",
        )
        service._approval_provider.approve.assert_called_once_with(
            "op-test-001", "derek"
        )

    async def test_approve_with_no_service_raises(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_approve

        with pytest.raises(RuntimeError, match="not_active"):
            await handle_approve(service=None, op_id="op-001", approver="derek")


@pytest.mark.asyncio
class TestRejectCommand:
    """Tests for the reject CLI command logic."""

    async def test_reject_calls_provider(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_reject

        service = _mock_service()
        result = await handle_reject(
            service=service,
            op_id="op-test-001",
            approver="derek",
            reason="Too risky",
        )
        service._approval_provider.reject.assert_called_once_with(
            "op-test-001", "derek", "Too risky"
        )

    async def test_reject_with_no_service_raises(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_reject

        with pytest.raises(RuntimeError, match="not_active"):
            await handle_reject(
                service=None,
                op_id="op-test-001",
                approver="derek",
                reason="Too risky",
            )
