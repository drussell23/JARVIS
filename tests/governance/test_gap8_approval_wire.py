# tests/governance/test_gap8_approval_wire.py
"""CLIApprovalProvider: rejection writes correction to OUROBOROS.md when project_root set."""
import asyncio
import inspect
import pytest
from pathlib import Path
from backend.core.ouroboros.governance.approval_provider import CLIApprovalProvider
from backend.core.ouroboros.governance.op_context import OperationContext


def test_cli_approval_provider_accepts_project_root():
    sig = inspect.signature(CLIApprovalProvider.__init__)
    assert "project_root" in sig.parameters


@pytest.mark.asyncio
async def test_reject_writes_correction_when_project_root_set(tmp_path):
    provider = CLIApprovalProvider(project_root=tmp_path)
    ctx = OperationContext.create(
        op_id="op-abc",
        description="add feature",
        target_files=("backend/foo.py",),
    )
    await provider.request(ctx)
    await provider.reject(request_id="op-abc", approver="human", reason="Don't use global state here")

    md = tmp_path / "OUROBOROS.md"
    assert md.exists()
    content = md.read_text()
    assert "op:op-abc" in content
    assert "Don't use global state here" in content


@pytest.mark.asyncio
async def test_reject_no_correction_when_project_root_none():
    """Without project_root, rejection succeeds but no file is written."""
    provider = CLIApprovalProvider()  # no project_root
    ctx = OperationContext.create(
        op_id="op-xyz",
        description="refactor",
        target_files=("backend/bar.py",),
    )
    await provider.request(ctx)
    result = await provider.reject(request_id="op-xyz", approver="human", reason="some reason")
    from backend.core.ouroboros.governance.approval_provider import ApprovalStatus
    assert result.status == ApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_reject_empty_reason_does_not_crash(tmp_path):
    provider = CLIApprovalProvider(project_root=tmp_path)
    ctx = OperationContext.create(op_id="op-empty", description="x", target_files=("a.py",))
    await provider.request(ctx)
    # Empty reason: should not raise, no file created
    result = await provider.reject(request_id="op-empty", approver="human", reason="  ")
    from backend.core.ouroboros.governance.approval_provider import ApprovalStatus
    assert result.status == ApprovalStatus.REJECTED
    md = tmp_path / "OUROBOROS.md"
    assert not md.exists() or "op:op-empty" not in md.read_text()
