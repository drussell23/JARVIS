"""tests/governance/self_dev/test_pipeline_flow.py

Tests for GovernedLoopService vertical integration extensions.
"""
import pytest

from backend.core.ouroboros.governance.governed_loop_service import (
    ReadyToCommitPayload,
)


def test_ready_to_commit_payload_exists():
    """ReadyToCommitPayload dataclass should be importable."""
    payload = ReadyToCommitPayload(
        op_id="op-123",
        changed_files=("file.py",),
        provider_id="prime",
        model_id="j-prime-v1",
        routing_reason="primary_healthy",
        verification_summary="sandbox: 5/5, post-apply: 5/5",
        rollback_status="clean",
        suggested_commit_message="fix(governed): test fix [op:op-123]",
    )
    assert payload.op_id == "op-123"
    assert payload.rollback_status == "clean"


def test_ready_to_commit_payload_is_frozen():
    """ReadyToCommitPayload should be immutable."""
    payload = ReadyToCommitPayload(
        op_id="op-123",
        changed_files=("file.py",),
        provider_id="prime",
        model_id="j-prime-v1",
        routing_reason="primary_healthy",
        verification_summary="all pass",
        rollback_status="clean",
        suggested_commit_message="fix: test",
    )
    with pytest.raises(AttributeError):
        payload.op_id = "changed"  # type: ignore[misc]
