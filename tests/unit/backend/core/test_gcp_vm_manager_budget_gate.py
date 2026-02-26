from unittest.mock import AsyncMock

import pytest

from backend.core.gcp_vm_manager import GCPVMManager, VMManagerConfig


def _manager() -> GCPVMManager:
    manager = GCPVMManager(config=VMManagerConfig())
    manager.initialized = True
    return manager


@pytest.mark.asyncio
async def test_budget_gate_fails_closed_when_cost_tracker_missing():
    manager = _manager()
    manager.cost_tracker = None

    allowed, reason, details = await manager._enforce_budget_gate(
        operation="unit_test:create",
    )

    assert allowed is False
    assert reason == "cost_tracker_unavailable"
    assert details["fail_mode"] == "closed"


@pytest.mark.asyncio
async def test_create_vm_mandatorily_uses_budget_gate(monkeypatch):
    manager = _manager()
    monkeypatch.setattr(manager.config, "is_valid_for_vm_operations", lambda: (True, ""))

    gate = AsyncMock(return_value=(False, "budget_blocked", {"budget_percent_used": 100.0}))
    monkeypatch.setattr(manager, "_enforce_budget_gate", gate)
    monkeypatch.setattr(
        manager,
        "check_quotas_before_creation",
        AsyncMock(side_effect=AssertionError("quota check should not run when budget blocks")),
    )

    vm = await manager.create_vm(["inference"], "unit_test")

    assert vm is None
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_instance_mandatorily_uses_budget_gate(monkeypatch):
    manager = _manager()

    gate = AsyncMock(return_value=(False, "budget_blocked", {}))
    monkeypatch.setattr(manager, "_enforce_budget_gate", gate)

    class _InstancesClient:
        def start(self, *args, **kwargs):
            raise AssertionError("start should not be called when budget blocks")

    manager.instances_client = _InstancesClient()

    ok, err = await manager._start_instance("jarvis-prime-node")

    assert ok is False
    assert "BUDGET_EXCEEDED" in (err or "")
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_static_vm_mandatorily_uses_budget_gate(monkeypatch):
    manager = _manager()

    gate = AsyncMock(return_value=(False, "budget_blocked", {}))
    monkeypatch.setattr(manager, "_enforce_budget_gate", gate)
    monkeypatch.setattr(
        manager,
        "_get_static_ip_address",
        AsyncMock(side_effect=AssertionError("static IP lookup should not run when budget blocks")),
    )

    ok, err = await manager._create_static_vm("jarvis-prime-node", "jarvis-ip", 8000)

    assert ok is False
    assert "BUDGET_EXCEEDED" in (err or "")
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_static_vm_ready_mandatorily_uses_budget_gate(monkeypatch):
    manager = _manager()
    manager.config.static_ip_name = "jarvis-ip"
    monkeypatch.setattr(manager.config, "is_valid_for_vm_operations", lambda: (True, ""))

    gate = AsyncMock(return_value=(False, "budget_blocked", {}))
    monkeypatch.setattr(manager, "_enforce_budget_gate", gate)
    monkeypatch.setattr(
        manager,
        "_get_static_ip_address",
        AsyncMock(side_effect=AssertionError("ensure should stop before static IP lookup")),
    )

    ok, ip, msg = await manager.ensure_static_vm_ready()

    assert ok is False
    assert ip is None
    assert "BUDGET_EXCEEDED" in msg
    gate.assert_awaited_once()
