"""Tests for B+ saga type additions: SAGA_PARTIAL_PROMOTE + SagaLedgerArtifact."""
import dataclasses
import time

from backend.core.ouroboros.governance.saga.saga_types import (
    SagaLedgerArtifact,
    SagaTerminalState,
)


def test_partial_promote_terminal_state_exists():
    assert hasattr(SagaTerminalState, "SAGA_PARTIAL_PROMOTE")
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE.value == "saga_partial_promote"


def test_partial_promote_is_distinct_from_stuck():
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE != SagaTerminalState.SAGA_STUCK


def test_saga_ledger_artifact_is_frozen():
    artifact = SagaLedgerArtifact(
        saga_id="test-saga",
        op_id="test-op",
        event="prepare",
        repo="jarvis",
        original_ref="main",
        original_sha="abc123",
        base_sha="abc123",
        saga_branch="ouroboros/saga-test/jarvis",
        promoted_sha="",
        promote_order_index=-1,
        rollback_reason="",
        partial_promote_boundary_repo="",
        kept_forensics_branches=False,
        skipped_no_diff=False,
        timestamp_ns=time.monotonic_ns(),
    )
    assert dataclasses.is_dataclass(artifact)
    # Frozen: assignment should raise
    try:
        artifact.saga_id = "changed"  # type: ignore[misc]
        assert False, "Should have raised FrozenInstanceError"
    except (dataclasses.FrozenInstanceError, AttributeError):
        pass


def test_saga_ledger_artifact_serializes_to_dict():
    artifact = SagaLedgerArtifact(
        saga_id="s1",
        op_id="o1",
        event="apply_repo",
        repo="prime",
        original_ref="main",
        original_sha="aaa",
        base_sha="aaa",
        saga_branch="ouroboros/saga-o1/prime",
        promoted_sha="bbb",
        promote_order_index=1,
        rollback_reason="",
        partial_promote_boundary_repo="",
        kept_forensics_branches=True,
        skipped_no_diff=False,
        timestamp_ns=12345,
    )
    d = dataclasses.asdict(artifact)
    assert d["saga_id"] == "s1"
    assert d["promoted_sha"] == "bbb"
    assert d["kept_forensics_branches"] is True
    assert d["promote_order_index"] == 1
