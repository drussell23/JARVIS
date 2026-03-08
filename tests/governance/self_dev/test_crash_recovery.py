"""tests/governance/self_dev/test_crash_recovery.py

Tests for boot-time crash recovery via the OperationLedger:
  1. Orphaned APPLIED op is detectable via get_latest_state
  2. Completed (APPLIED then ROLLED_BACK) op → latest is ROLLED_BACK
  3. Stale PENDING approvals are expired by expire_stale
  4. Ledger survives restart (new OperationLedger instance reads persisted data)
  5. Empty ledger returns no history
"""
import asyncio
import json
import time
from pathlib import Path

from backend.core.ouroboros.governance.approval_store import (
    ApprovalState,
    ApprovalStore,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)


# ── 1. Orphaned APPLIED op is detectable ──────────────────────────

def test_orphaned_applied_op_detectable(tmp_path: Path):
    """Ledger with APPLIED state → get_latest_state returns APPLIED."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-orphan"

    asyncio.get_event_loop().run_until_complete(
        ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
    )
    asyncio.get_event_loop().run_until_complete(
        ledger.append(LedgerEntry(op_id=op_id, state=OperationState.APPLIED, data={}))
    )

    latest = asyncio.get_event_loop().run_until_complete(
        ledger.get_latest_state(op_id)
    )
    assert latest == OperationState.APPLIED


# ── 2. Completed op → latest is ROLLED_BACK ──────────────────────

def test_completed_op_no_recovery_needed(tmp_path: Path):
    """Ledger with APPLIED then ROLLED_BACK → latest is ROLLED_BACK."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-complete"

    asyncio.get_event_loop().run_until_complete(
        ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
    )
    asyncio.get_event_loop().run_until_complete(
        ledger.append(LedgerEntry(op_id=op_id, state=OperationState.APPLIED, data={}))
    )
    asyncio.get_event_loop().run_until_complete(
        ledger.append(
            LedgerEntry(
                op_id=op_id,
                state=OperationState.ROLLED_BACK,
                data={"reason": "verify_failed"},
            )
        )
    )

    latest = asyncio.get_event_loop().run_until_complete(
        ledger.get_latest_state(op_id)
    )
    assert latest == OperationState.ROLLED_BACK


# ── 3. Stale PENDING approvals expire ─────────────────────────────

def test_stale_pending_approvals_expire(tmp_path: Path):
    """ApprovalStore create + backdate + expire_stale → EXPIRED."""
    store = ApprovalStore(store_path=tmp_path / "approvals" / "pending.json")
    store.create("op-stale", policy_version="v0.1.0")

    # Backdate the created_at to simulate staleness
    data = json.loads(store._path.read_text(encoding="utf-8"))
    data["op-stale"]["created_at"] = time.time() - 7200  # 2 hours ago
    store._atomic_write(data)

    expired = store.expire_stale(timeout_seconds=1800.0)
    assert "op-stale" in expired

    record = store.get("op-stale")
    assert record is not None
    assert record.state == ApprovalState.EXPIRED


# ── 4. Ledger survives restart ─────────────────────────────────────

def test_ledger_survives_restart(tmp_path: Path):
    """Write 2 entries, create new OperationLedger, get_history returns both."""
    storage = tmp_path / "ledger"
    op_id = "op-persist"

    ledger1 = OperationLedger(storage_dir=storage)
    asyncio.get_event_loop().run_until_complete(
        ledger1.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={"step": 1}))
    )
    asyncio.get_event_loop().run_until_complete(
        ledger1.append(LedgerEntry(op_id=op_id, state=OperationState.APPLIED, data={"step": 2}))
    )

    # Simulate restart: create a brand-new OperationLedger on the same dir
    ledger2 = OperationLedger(storage_dir=storage)
    history = asyncio.get_event_loop().run_until_complete(
        ledger2.get_history(op_id)
    )

    assert len(history) == 2
    assert history[0].state == OperationState.PLANNED
    assert history[1].state == OperationState.APPLIED


# ── 5. Empty ledger — no recovery needed ──────────────────────────

def test_empty_ledger_no_recovery(tmp_path: Path):
    """get_history on a nonexistent op_id returns an empty list."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    history = asyncio.get_event_loop().run_until_complete(
        ledger.get_history("op-does-not-exist")
    )
    assert history == []
