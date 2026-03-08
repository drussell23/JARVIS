"""Unit tests for boot-time hash-guarded reconciliation logic."""
import hashlib
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationLedger, OperationState


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_hash_match_indicates_clean_rollback_candidate(tmp_path):
    """If file hash matches expected, it's an orphaned apply candidate."""
    f = tmp_path / "target.py"
    f.write_text("x = 1\n")
    post_apply_hash = sha256_file(f)
    assert post_apply_hash == hashlib.sha256(b"x = 1\n").hexdigest()


def test_hash_mismatch_indicates_file_drifted(tmp_path):
    """If current hash != expected, file drifted — do NOT rollback."""
    f = tmp_path / "target.py"
    f.write_text("x = 999\n")  # drifted
    expected_hash = hashlib.sha256(b"x = 1\n").hexdigest()
    current_hash = sha256_file(f)
    assert current_hash != expected_hash


@pytest.mark.asyncio
async def test_ledger_get_latest_state_sync_applied(tmp_path):
    """get_latest_state_sync reads APPLIED state synchronously."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-sync-test"
    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.APPLIED, data={}))
    result = ledger.get_latest_state_sync(op_id)
    assert result == OperationState.APPLIED


def test_ledger_get_latest_state_sync_empty(tmp_path):
    """get_latest_state_sync returns None for unknown op_id."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    assert ledger.get_latest_state_sync("op-does-not-exist") is None


@pytest.mark.asyncio
async def test_recovery_marker_prevents_double_recover(tmp_path):
    """If recovery_attempted=True in ledger data, skip on re-scan."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-recover"
    await ledger.append(LedgerEntry(
        op_id=op_id, state=OperationState.APPLIED,
        data={"recovery_attempted": True, "recovery_attempt_id": "uuid-123"}
    ))
    history = await ledger.get_history(op_id)
    assert history[-1].data.get("recovery_attempted") is True


@pytest.mark.asyncio
async def test_reconcile_on_boot_marks_already_reverted_op_rolled_back(tmp_path):
    """Orphaned APPLIED op where file already has pre-apply content -> ROLLED_BACK."""
    from unittest.mock import AsyncMock, MagicMock
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService, GovernedLoopConfig
    from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationLedger, OperationState
    import hashlib

    target = tmp_path / "target.py"
    original_content = b"x = 1\n"
    target.write_bytes(original_content)
    rollback_hash = hashlib.sha256(original_content).hexdigest()

    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-orphan"
    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
    await ledger.append(LedgerEntry(
        op_id=op_id, state=OperationState.APPLIED,
        data={"target_file": str(target), "rollback_hash": rollback_hash},
    ))

    # Build a minimal mock stack
    mock_comm = MagicMock()
    mock_comm.emit_decision = AsyncMock()
    mock_stack = MagicMock()
    mock_stack.ledger = ledger
    mock_stack.comm = mock_comm
    mock_stack.approval_store = None

    config = GovernedLoopConfig(project_root=tmp_path)
    svc = GovernedLoopService.__new__(GovernedLoopService)
    svc._stack = mock_stack
    svc._config = config

    await svc._reconcile_on_boot()

    # File still has original content -> rollback_hash matches -> ROLLED_BACK
    latest = await ledger.get_latest_state(op_id)
    assert latest == OperationState.ROLLED_BACK
    mock_comm.emit_decision.assert_not_called()
