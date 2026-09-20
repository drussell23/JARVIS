"""Boot recovery reaches a verdict ONCE.

On 2026-09-20 the same eleven ops were "recovered", quarantined and escalated
as ``manual_intervention_required`` on every boot. Nine were still on disk and
all nine had one shape::

    ... failed (change_engine_failed) -> ... -> applied (reason=noop)

Two independent defects made that loop permanent:

1. ``ledger.append`` deduplicates on ``op_id:state`` and returns False without
   writing. ``get_history`` had just loaded the op's keys, so the FAILED
   verdict collided with the EARLIER failed attempt and the APPLIED recovery
   marker collided with the entry it was marking. Both were dropped, silently;
   the latest state stayed APPLIED; next boot, the same op again.
2. A no-op records APPLIED {reason, provider} with no ``target_file`` — there
   was no file. Boot recovery read that as an orphaned apply with missing
   provenance, so every CORRECT no-op became an alarm on the following boot.

Each test boots TWICE over the same directory with a FRESH ledger object: a
single instance keeps its dedup set in memory and cannot see the defect.
"""

from __future__ import annotations

import hashlib
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)


async def _boot(tmp_path):
    """One daemon boot: new process, new ledger object, same directory."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    comm = MagicMock()
    comm.emit_decision = AsyncMock()
    stack = MagicMock()
    stack.ledger, stack.comm, stack.approval_store = ledger, comm, None
    svc = GovernedLoopService.__new__(GovernedLoopService)
    svc._stack = stack
    svc._config = GovernedLoopConfig(project_root=tmp_path)
    svc.report_external_outcome = AsyncMock()
    await svc._reconcile_on_boot()
    return ledger, comm


async def _seed(tmp_path, op_id, entries):
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    for state, data in entries:
        await ledger.append(LedgerEntry(op_id=op_id, state=state, data=data))


def _lines(tmp_path, op_id):
    return (tmp_path / "ledger" / f"{op_id}.jsonl").read_text().count("\n")


@pytest.mark.asyncio
async def test_a_noop_is_not_an_orphan(tmp_path):
    """The exact shape of all nine recurring orphans."""
    op = "op-noop"
    await _seed(tmp_path, op, [
        (OperationState.PLANNED, {"target_file": "backend/x.py"}),
        (OperationState.FAILED, {"reason": "change_engine_failed"}),
        (OperationState.APPLIED, {"reason": "noop", "provider": "local"}),
    ])
    for _ in range(3):
        _ledger, comm = await _boot(tmp_path)
        comm.emit_decision.assert_not_called()


@pytest.mark.asyncio
async def test_a_noop_is_examined_once_not_every_boot(tmp_path):
    op = "op-noop-once"
    await _seed(tmp_path, op, [
        (OperationState.FAILED, {"reason": "change_engine_failed"}),
        (OperationState.APPLIED, {"reason": "noop"}),
    ])
    ledger, _ = await _boot(tmp_path)
    assert (await ledger.get_history(op))[-1].data.get("recovery_attempted") is True
    settled = _lines(tmp_path, op)
    await _boot(tmp_path)
    await _boot(tmp_path)
    assert _lines(tmp_path, op) == settled, "re-examined an op it had settled"


@pytest.mark.asyncio
async def test_missing_provenance_escalates_exactly_once(tmp_path):
    """A REAL orphan — a file was named, the rollback hash was not kept —
    still escalates. Once. The earlier FAILED in its history is what used to
    swallow the verdict."""
    op = "op-unvouched"
    await _seed(tmp_path, op, [
        (OperationState.FAILED, {"reason": "change_engine_failed"}),
        (OperationState.APPLIED, {"target_file": str(tmp_path / "t.py")}),
    ])
    ledger, comm = await _boot(tmp_path)
    assert comm.emit_decision.await_count == 1
    assert await ledger.get_latest_state(op) == OperationState.FAILED

    _ledger2, comm2 = await _boot(tmp_path)
    comm2.emit_decision.assert_not_called()


@pytest.mark.asyncio
async def test_an_intact_apply_is_settled_once(tmp_path):
    """Its verdict is written as APPLIED — a state that is ALWAYS already in
    the history — so it could never persist at all."""
    target = tmp_path / "t.py"
    target.write_bytes(b"x = 2\n")
    op = "op-intact"
    await _seed(tmp_path, op, [
        (OperationState.APPLIED, {
            "target_file": str(target),
            "rollback_hash": hashlib.sha256(b"x = 1\n").hexdigest(),
            "applied_hash": hashlib.sha256(b"x = 2\n").hexdigest(),
        }),
    ])
    await _boot(tmp_path)
    settled = _lines(tmp_path, op)
    await _boot(tmp_path)
    assert _lines(tmp_path, op) == settled
    assert target.read_bytes() == b"x = 2\n"


@pytest.mark.asyncio
async def test_already_reverted_still_rolls_back(tmp_path):
    """The pre-existing contract, unchanged."""
    target = tmp_path / "t.py"
    target.write_bytes(b"x = 1\n")
    op = "op-reverted"
    await _seed(tmp_path, op, [
        (OperationState.APPLIED, {
            "target_file": str(target),
            "rollback_hash": hashlib.sha256(b"x = 1\n").hexdigest(),
        }),
    ])
    ledger, comm = await _boot(tmp_path)
    assert await ledger.get_latest_state(op) == OperationState.ROLLED_BACK
    comm.emit_decision.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_verdict_is_reported_not_swallowed(tmp_path, caplog):
    op = "op-refused"
    await _seed(tmp_path, op, [
        (OperationState.APPLIED, {"target_file": str(tmp_path / "t.py")}),
    ])
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    ledger.append = AsyncMock(return_value=False)
    comm = MagicMock()
    comm.emit_decision = AsyncMock()
    stack = MagicMock()
    stack.ledger, stack.comm, stack.approval_store = ledger, comm, None
    svc = GovernedLoopService.__new__(GovernedLoopService)
    svc._stack = stack
    svc._config = GovernedLoopConfig(project_root=tmp_path)
    svc.report_external_outcome = AsyncMock()

    caplog.set_level(logging.WARNING)
    await svc._reconcile_on_boot()
    assert any("REFUSED the verdict" in r.getMessage() for r in caplog.records)
