# tests/unit/core/test_gcp_lifecycle_adapter.py
"""Tests for GCP lifecycle side-effect adapter."""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.gcp_lifecycle_adapter import GCPLifecycleAdapter


class MockGCPVMManager:
    """Mock GCP VM manager for testing adapter dispatch."""
    def __init__(self):
        self.create_calls = []
        self.stop_calls = []
        self.health_calls = []
        self._fail_next = False

    async def create_vm(self, op_id: str, **kwargs):
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("Simulated GCP API error")
        self.create_calls.append({"op_id": op_id, **kwargs})
        return {"instance": "vm-123", "ip": "10.0.0.1"}

    async def stop_vm(self, op_id: str, **kwargs):
        self.stop_calls.append({"op_id": op_id, **kwargs})
        return {"stopped": True}

    async def switch_routing(self, direction: str, op_id: str, **kwargs):
        return {"direction": direction}

    async def query_vm_state(self, op_id: str, **kwargs):
        return "not_found"


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


@pytest.fixture
def gcp_manager():
    return MockGCPVMManager()


@pytest.fixture
def adapter(journal, gcp_manager):
    return GCPLifecycleAdapter(journal, gcp_manager)


class TestAdapterDispatch:
    @pytest.mark.asyncio
    async def test_budget_approved_triggers_create(self, adapter, gcp_manager):
        result = await adapter.execute(
            "budget_approved", "op_1",
            from_state="triggering", to_state="provisioning",
        )
        assert len(gcp_manager.create_calls) == 1
        assert gcp_manager.create_calls[0]["op_id"] == "op_1"

    @pytest.mark.asyncio
    async def test_cooldown_expired_triggers_stop(self, adapter, gcp_manager):
        result = await adapter.execute(
            "cooldown_expired", "op_2",
            from_state="cooling_down", to_state="stopping",
        )
        assert len(gcp_manager.stop_calls) == 1

    @pytest.mark.asyncio
    async def test_session_shutdown_triggers_stop(self, adapter, gcp_manager):
        result = await adapter.execute(
            "session_shutdown", "op_3",
            from_state="active", to_state="stopping",
        )
        assert len(gcp_manager.stop_calls) == 1

    @pytest.mark.asyncio
    async def test_health_probe_ok_triggers_routing_switch(self, adapter, gcp_manager):
        result = await adapter.execute(
            "health_probe_ok", "op_4",
            from_state="booting", to_state="active",
        )
        assert result.get("direction") == "cloud"

    @pytest.mark.asyncio
    async def test_spot_preempted_triggers_routing_and_release(self, adapter, gcp_manager):
        result = await adapter.execute(
            "spot_preempted", "op_5",
            from_state="active", to_state="triggering",
        )
        assert result.get("direction") == "local"


class TestAdapterErrorHandling:
    @pytest.mark.asyncio
    async def test_gcp_api_error_returns_error_dict(self, adapter, gcp_manager):
        gcp_manager._fail_next = True
        result = await adapter.execute(
            "budget_approved", "op_err",
            from_state="triggering", to_state="provisioning",
        )
        assert "error" in result
        assert "Simulated GCP API error" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_action_returns_noop(self, adapter):
        result = await adapter.execute(
            "unknown_action", "op_noop",
            from_state="idle", to_state="idle",
        )
        assert result.get("status") == "no_op"


class TestOpIdGeneration:
    def test_generate_stable_op_id(self, adapter):
        op_id = adapter.generate_op_id("invincible_node", "budget_approved", epoch=5)
        assert "invincible_node" in op_id
        assert "budget_approved" in op_id
        assert "5" in op_id
