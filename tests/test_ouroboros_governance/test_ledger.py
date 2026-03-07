"""Tests for the Ouroboros Operation Ledger — append-only state log."""

import pytest

from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id


# ---------------------------------------------------------------------------
# TestLedgerAppend
# ---------------------------------------------------------------------------


class TestLedgerAppend:
    """Tests for OperationLedger.append() behaviour."""

    @pytest.mark.asyncio
    async def test_append_creates_entry(self, tmp_ledger_dir):
        """Appending a PLANNED entry, then get_history returns exactly 1 entry."""
        ledger = OperationLedger(storage_dir=tmp_ledger_dir)
        op_id = generate_operation_id()

        entry = LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={"intent": "test"})
        result = await ledger.append(entry)

        assert result is True
        history = await ledger.get_history(op_id)
        assert len(history) == 1
        assert history[0].op_id == op_id
        assert history[0].state is OperationState.PLANNED
        assert history[0].data == {"intent": "test"}

    @pytest.mark.asyncio
    async def test_append_preserves_ordering(self, tmp_ledger_dir):
        """Append PLANNED, VALIDATING, APPLIED in order; verify the sequence."""
        ledger = OperationLedger(storage_dir=tmp_ledger_dir)
        op_id = generate_operation_id()

        states = [OperationState.PLANNED, OperationState.VALIDATING, OperationState.APPLIED]
        for state in states:
            entry = LedgerEntry(op_id=op_id, state=state, data={})
            await ledger.append(entry)

        history = await ledger.get_history(op_id)
        assert len(history) == 3
        assert [e.state for e in history] == states

    @pytest.mark.asyncio
    async def test_append_is_durable(self, tmp_ledger_dir):
        """Append entries, create a NEW OperationLedger from the same dir, history persists."""
        ledger1 = OperationLedger(storage_dir=tmp_ledger_dir)
        op_id = generate_operation_id()

        entry = LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={"key": "value"})
        await ledger1.append(entry)

        # Create an entirely new ledger instance from the same directory
        ledger2 = OperationLedger(storage_dir=tmp_ledger_dir)
        history = await ledger2.get_history(op_id)
        assert len(history) == 1
        assert history[0].op_id == op_id
        assert history[0].state is OperationState.PLANNED
        assert history[0].data == {"key": "value"}

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_skip(self, tmp_ledger_dir):
        """Same (op_id, state) appended twice; history has only 1 entry."""
        ledger = OperationLedger(storage_dir=tmp_ledger_dir)
        op_id = generate_operation_id()

        entry = LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={"x": 1})
        first_result = await ledger.append(entry)
        second_result = await ledger.append(entry)

        assert first_result is True
        assert second_result is False
        history = await ledger.get_history(op_id)
        assert len(history) == 1


# ---------------------------------------------------------------------------
# TestLedgerQuery
# ---------------------------------------------------------------------------


class TestLedgerQuery:
    """Tests for OperationLedger query methods."""

    @pytest.mark.asyncio
    async def test_get_latest_state(self, tmp_ledger_dir):
        """Append PLANNED then APPLIED; latest state is APPLIED."""
        ledger = OperationLedger(storage_dir=tmp_ledger_dir)
        op_id = generate_operation_id()

        await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
        await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.APPLIED, data={}))

        latest = await ledger.get_latest_state(op_id)
        assert latest is OperationState.APPLIED

    @pytest.mark.asyncio
    async def test_get_latest_state_unknown_op(self, tmp_ledger_dir):
        """Unknown op_id returns None."""
        ledger = OperationLedger(storage_dir=tmp_ledger_dir)
        latest = await ledger.get_latest_state("op-nonexistent-fake")
        assert latest is None
