"""Tests for ApprovalStore CAS with ledger terminal check."""
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.approval_store import ApprovalState, ApprovalStore
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationLedger, OperationState


async def test_decide_returns_superseded_when_ledger_terminal(tmp_path):
    """decide_with_ledger() with ledger in terminal state returns 'superseded'."""
    store = ApprovalStore(store_path=tmp_path / "approvals" / "pending.json")
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-cas"

    store.create(op_id, policy_version="v0.1.0")

    # Force ledger to terminal state (FAILED — OperationState has no CANCELLED)
    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.FAILED, data={}))

    outcome = store.decide_with_ledger(op_id, ApprovalState.APPROVED, ledger=ledger)
    assert outcome == "superseded"


async def test_decide_returns_ok_when_ledger_not_terminal(tmp_path):
    """decide_with_ledger returns 'ok' when ledger is not terminal."""
    store = ApprovalStore(store_path=tmp_path / "approvals" / "pending.json")
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-ok"

    store.create(op_id, policy_version="v0.1.0")

    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.GATING, data={}))

    outcome = store.decide_with_ledger(op_id, ApprovalState.APPROVED, ledger=ledger)
    assert outcome == "ok"


def test_concurrent_decide_exactly_one_wins(tmp_path):
    """Two concurrent decide() calls: exactly one returns APPROVED, one SUPERSEDED."""
    store = ApprovalStore(store_path=tmp_path / "approvals" / "pending.json")
    op_id = "op-concurrent"
    store.create(op_id, policy_version="v0.1.0")

    results = []

    def approve():
        rec = store.decide(op_id, ApprovalState.APPROVED)
        results.append(rec.state)

    t1 = threading.Thread(target=approve)
    t2 = threading.Thread(target=approve)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one must be APPROVED; the other SUPERSEDED (already decided)
    assert results.count(ApprovalState.APPROVED) == 1
    assert results.count(ApprovalState.SUPERSEDED) == 1
