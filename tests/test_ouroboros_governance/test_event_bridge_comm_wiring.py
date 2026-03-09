"""Tests that EventBridge is wired as a CommProtocol transport when event_bus is provided."""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock


def _make_config(tmp_path):
    """Build a GovernanceConfig with a temporary ledger dir."""
    from backend.core.ouroboros.governance.integration import GovernanceConfig

    args = argparse.Namespace(skip_governance=False, governance_mode="sandbox")
    base = GovernanceConfig.from_env_and_args(args)
    return GovernanceConfig(
        ledger_dir=tmp_path / "ledger",
        policy_version=base.policy_version,
        policy_hash=base.policy_hash,
        contract_version=base.contract_version,
        contract_hash=base.contract_hash,
        config_digest=base.config_digest,
        initial_mode=base.initial_mode,
        skip_governance=base.skip_governance,
        canary_slices=base.canary_slices,
        gcp_daily_budget=base.gcp_daily_budget,
        startup_timeout_s=base.startup_timeout_s,
        component_budget_s=base.component_budget_s,
    )


async def test_event_bridge_added_to_comm_when_event_bus_provided(tmp_path):
    """EventBridge.send() must be called when comm.emit_intent() fires with event_bus present."""
    from backend.core.ouroboros.governance.integration import create_governance_stack

    config = _make_config(tmp_path)
    mock_event_bus = AsyncMock()
    mock_event_bus.emit = AsyncMock()

    stack = await create_governance_stack(config, event_bus=mock_event_bus)

    await stack.comm.emit_intent(
        op_id="op-test-001",
        goal="Add utility function",
        target_files=["backend/core/utils.py"],
        risk_tier="safe_auto",
        blast_radius=1,
    )

    # EventBridge maps INTENT → IMPROVEMENT_REQUEST and emits to bus
    assert mock_event_bus.emit.called, "EventBridge.emit not called — not wired as transport"


async def test_no_event_bridge_when_event_bus_none(tmp_path):
    """When event_bus=None, GovernanceStack.event_bridge must be None."""
    from backend.core.ouroboros.governance.integration import create_governance_stack

    config = _make_config(tmp_path)
    stack = await create_governance_stack(config, event_bus=None)
    assert stack.event_bridge is None
