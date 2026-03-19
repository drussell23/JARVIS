"""v298.0: SupervisorAwareGCPController adapter tests."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_idle_monitor_loop_removed():
    """_idle_monitor_loop must not exist on SupervisorAwareGCPController."""
    from backend.core.supervisor_gcp_controller import SupervisorAwareGCPController
    ctrl = SupervisorAwareGCPController.__new__(SupervisorAwareGCPController)
    assert not hasattr(ctrl, "_idle_monitor_loop"), (
        "_idle_monitor_loop must be removed — VMLifecycleManager owns idle tracking"
    )


def test_gcp_controller_adapter_implements_vm_controller():
    """_GCPControllerAdapter must satisfy VMController Protocol."""
    from backend.core.supervisor_gcp_controller import SupervisorAwareGCPController
    from backend.core.vm_lifecycle_manager import VMController
    assert hasattr(SupervisorAwareGCPController, "_GCPControllerAdapter"), (
        "_GCPControllerAdapter inner class must exist"
    )
    adapter_cls = SupervisorAwareGCPController._GCPControllerAdapter
    # Protocol structural check
    instance = adapter_cls.__new__(adapter_cls)
    assert isinstance(instance, VMController)


@pytest.mark.asyncio
async def test_set_lifecycle_manager_exists():
    """set_lifecycle_manager() method must exist."""
    from backend.core.supervisor_gcp_controller import SupervisorAwareGCPController
    ctrl = SupervisorAwareGCPController.__new__(SupervisorAwareGCPController)
    assert hasattr(ctrl, "set_lifecycle_manager")
