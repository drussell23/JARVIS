"""Tests that SAFE_AUTO approval provenance is recorded in ledger data."""
import pytest
from pathlib import Path
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationLedger, OperationState


@pytest.mark.asyncio
async def test_safe_auto_provenance_in_planned_entry(tmp_path):
    """SAFE_AUTO provenance is stored in PLANNED entry data — no new enum."""
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    op_id = "op-auto"

    await ledger.append(LedgerEntry(
        op_id=op_id,
        state=OperationState.PLANNED,
        data={
            "approval_mode": "auto",
            "policy_version": "v0.1.0",
            "risk_tier": "SAFE_AUTO",
            "rule_reason": "blast_radius=1 + no security surface",
        }
    ))

    history = await ledger.get_history(op_id)
    assert history[0].state == OperationState.PLANNED
    assert history[0].data["approval_mode"] == "auto"
    assert history[0].data["risk_tier"] == "SAFE_AUTO"


def test_safe_auto_uses_only_existing_states():
    """No invented OperationState values — only existing enum members."""
    known_states = {s.value for s in OperationState}
    assert "planned" in known_states
    assert "approved" not in known_states
