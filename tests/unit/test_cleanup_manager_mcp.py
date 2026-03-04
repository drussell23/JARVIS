"""Tests for process_cleanup_manager MCP broker integration (Task 5).

Verifies that ``IntelligentMemoryController``, ``EventDrivenCleanupTrigger``,
and ``ProcessCleanupManager`` can register with the MCP broker, read memory
from the broker's cached snapshot instead of raw psutil, derive thresholds
from ``PressurePolicy``, and submit ``CLEANUP`` actions through the
coordinator.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.core.memory_types import (
    ActuatorAction,
    DecisionEnvelope,
    MemorySnapshot,
    PressurePolicy,
    PressureTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_broker(epoch: int = 1) -> MagicMock:
    """Create a mock broker with the attributes accessed by the cleanup manager."""
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.current_epoch = epoch
    broker.current_sequence = 5
    broker.policy = PressurePolicy.for_ram_gb(16.0)
    broker.coordinator = MagicMock()
    broker.coordinator.submit = MagicMock(return_value="dec-abc123")
    broker.latest_snapshot = None  # default: no snapshot yet
    return broker


def _mock_snapshot_obj(
    physical_total: int = 16 * (1024 ** 3),
    physical_free: int = 4 * (1024 ** 3),
    snapshot_id: str = "snap-test-001",
) -> MagicMock:
    """Create a mock BrokerMemorySnapshot with physical fields."""
    snap = MagicMock(spec=MemorySnapshot)
    snap.physical_total = physical_total
    snap.physical_free = physical_free
    snap.snapshot_id = snapshot_id
    snap.pressure_tier = PressureTier.OPTIMAL
    return snap


# ---------------------------------------------------------------------------
# IntelligentMemoryController tests
# ---------------------------------------------------------------------------

class TestIntelligentMemoryControllerDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        if hasattr(ctrl, "_mcp_active"):
            assert ctrl._mcp_active is False

    def test_broker_default_none(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        if hasattr(ctrl, "_broker"):
            assert ctrl._broker is None


class TestIntelligentMemoryControllerRegister:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        broker = _mock_broker()
        ctrl.register_with_broker(broker)
        assert ctrl._mcp_active is True

    def test_stores_broker_reference(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        broker = _mock_broker()
        ctrl.register_with_broker(broker)
        assert ctrl._broker is broker

    def test_recalculates_thresholds_from_policy(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        broker = _mock_broker()
        policy = broker.policy
        ctrl.register_with_broker(broker)

        # After registration, thresholds should match policy tiers
        assert ctrl._moderate_threshold == policy.enter_thresholds[PressureTier.CONSTRAINED]
        assert ctrl._high_threshold == policy.enter_thresholds[PressureTier.CRITICAL]
        assert ctrl._critical_threshold == policy.enter_thresholds[PressureTier.EMERGENCY]


class TestIntelligentMemoryControllerThresholds:
    """Verify _get_hardware_aware_thresholds uses broker policy when active."""

    def test_uses_policy_when_mcp_active(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        broker = _mock_broker()
        ctrl.register_with_broker(broker)

        thresholds = ctrl._get_hardware_aware_thresholds()
        policy = broker.policy
        assert thresholds["moderate"] == policy.enter_thresholds[PressureTier.CONSTRAINED]
        assert thresholds["high"] == policy.enter_thresholds[PressureTier.CRITICAL]
        assert thresholds["critical"] == policy.enter_thresholds[PressureTier.EMERGENCY]

    def test_uses_legacy_when_mcp_inactive(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        # Should not raise when broker is not set
        thresholds = ctrl._get_hardware_aware_thresholds()
        # Should return dict with all three keys
        assert "moderate" in thresholds
        assert "high" in thresholds
        assert "critical" in thresholds

    def test_env_override_takes_precedence_over_broker(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        broker = _mock_broker()
        ctrl.register_with_broker(broker)

        with patch.dict("os.environ", {
            "JARVIS_MEMORY_MODERATE_THRESHOLD": "77.0",
            "JARVIS_MEMORY_HIGH_THRESHOLD": "87.0",
            "JARVIS_MEMORY_CRITICAL_THRESHOLD": "97.0",
        }):
            thresholds = ctrl._get_hardware_aware_thresholds()
            assert thresholds["moderate"] == 77.0
            assert thresholds["high"] == 87.0
            assert thresholds["critical"] == 97.0


class TestDetectTotalRam:
    """Verify _detect_total_ram uses broker when active."""

    def test_uses_broker_static_method_when_active(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        ctrl = IntelligentMemoryController()
        broker = _mock_broker()
        ctrl.register_with_broker(broker)

        with patch(
            "backend.core.memory_budget_broker.MemoryBudgetBroker._detect_total_ram_gb",
            return_value=32.0,
        ):
            result = ctrl._detect_total_ram()
            assert result == 32.0


# ---------------------------------------------------------------------------
# EventDrivenCleanupTrigger tests
# ---------------------------------------------------------------------------

class TestEventDrivenCleanupTriggerBroker:
    """Verify EventDrivenCleanupTrigger broker integration."""

    def test_has_mcp_active_default_false(self):
        from backend.process_cleanup_manager import get_event_trigger
        trigger = get_event_trigger()
        if hasattr(trigger, "_mcp_active"):
            assert trigger._mcp_active is False

    def test_register_with_broker_sets_active(self):
        from backend.process_cleanup_manager import EventDrivenCleanupTrigger
        trigger = EventDrivenCleanupTrigger()
        broker = _mock_broker()
        trigger.register_with_broker(broker)
        assert trigger._mcp_active is True
        assert trigger._broker is broker

    def test_check_memory_pressure_uses_broker_snapshot(self):
        """When broker has a snapshot, _check_memory_pressure should use it."""
        from backend.process_cleanup_manager import EventDrivenCleanupTrigger

        trigger = EventDrivenCleanupTrigger()
        broker = _mock_broker()

        # Set up broker with a snapshot showing 50% usage
        snap = _mock_snapshot_obj(
            physical_total=16 * (1024 ** 3),
            physical_free=8 * (1024 ** 3),  # 50% free -> 50% used
        )
        broker.latest_snapshot = snap
        trigger.register_with_broker(broker)

        # Patch psutil to return 99% -- if broker is used, this should NOT be used
        with patch("backend.process_cleanup_manager.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_psutil.virtual_memory.return_value = mock_mem

            trigger._check_memory_pressure()
            # psutil.virtual_memory should NOT have been called
            mock_psutil.virtual_memory.assert_not_called()

    def test_check_memory_pressure_falls_back_when_no_snapshot(self):
        """When broker has no snapshot yet, should fall back to psutil."""
        from backend.process_cleanup_manager import EventDrivenCleanupTrigger

        trigger = EventDrivenCleanupTrigger()
        broker = _mock_broker()
        broker.latest_snapshot = None
        trigger.register_with_broker(broker)

        with patch("backend.process_cleanup_manager.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 50.0
            mock_psutil.virtual_memory.return_value = mock_mem

            trigger._check_memory_pressure()
            # Should fall back to psutil since no snapshot
            mock_psutil.virtual_memory.assert_called()


# ---------------------------------------------------------------------------
# ProcessCleanupManager tests
# ---------------------------------------------------------------------------

class TestProcessCleanupManagerDefaults:
    """Verify default attribute initialization."""

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_mcp_active_default_false(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        assert mgr._mcp_active is False

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_broker_default_none(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        assert mgr._broker is None


class TestProcessCleanupManagerRegister:
    """Verify register_with_broker propagation."""

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_sets_mcp_active(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        broker = _mock_broker()
        mgr.register_with_broker(broker)
        assert mgr._mcp_active is True
        assert mgr._broker is broker

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_propagates_to_event_trigger(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager

        trigger_mock = MagicMock()
        trigger_mock.register_with_broker = MagicMock()
        mock_et.return_value = trigger_mock

        mgr = ProcessCleanupManager()
        broker = _mock_broker()
        mgr.register_with_broker(broker)

        trigger_mock.register_with_broker.assert_called_once_with(broker)


class TestGetMemoryPercentFromBroker:
    """Verify _get_memory_percent_from_broker helper."""

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_returns_none_when_not_active(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        assert mgr._get_memory_percent_from_broker() is None

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_returns_none_when_no_snapshot(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        broker = _mock_broker()
        broker.latest_snapshot = None
        mgr._broker = broker
        mgr._mcp_active = True
        assert mgr._get_memory_percent_from_broker() is None

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_returns_percent_from_snapshot(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        broker = _mock_broker()
        snap = _mock_snapshot_obj(
            physical_total=16 * (1024 ** 3),
            physical_free=4 * (1024 ** 3),  # 75% used
        )
        broker.latest_snapshot = snap
        mgr._broker = broker
        mgr._mcp_active = True

        pct = mgr._get_memory_percent_from_broker()
        assert pct is not None
        assert abs(pct - 75.0) < 0.1


class TestGetHybridCloudStatus:
    """Verify get_hybrid_cloud_status uses broker snapshot."""

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_uses_broker_snapshot_when_active(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        broker = _mock_broker()
        snap = _mock_snapshot_obj(
            physical_total=16 * (1024 ** 3),
            physical_free=4 * (1024 ** 3),  # 75% used
        )
        broker.latest_snapshot = snap
        mgr._broker = broker
        mgr._mcp_active = True

        with patch("backend.process_cleanup_manager.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_psutil.virtual_memory.return_value = mock_mem

            status = mgr.get_hybrid_cloud_status()
            # Should use broker value (75%), not psutil (99%)
            assert abs(status["memory_percent"] - 75.0) < 0.1


class TestGetSystemSnapshot:
    """Verify get_system_snapshot uses broker snapshot."""

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_uses_broker_snapshot_when_active(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        mgr.swift_monitor = None  # force psutil/broker path
        broker = _mock_broker()
        snap = _mock_snapshot_obj(
            physical_total=16 * (1024 ** 3),
            physical_free=4 * (1024 ** 3),  # 75% used
        )
        broker.latest_snapshot = snap
        mgr._broker = broker
        mgr._mcp_active = True

        with patch("backend.process_cleanup_manager.psutil") as mock_psutil:
            mock_psutil.cpu_percent.return_value = 10.0
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_mem.available = 1 * (1024 ** 3)
            mock_psutil.virtual_memory.return_value = mock_mem

            snapshot = mgr.get_system_snapshot()
            # Memory should come from broker (75%), not psutil (99%)
            assert abs(snapshot["memory_percent"] - 75.0) < 0.1
            # psutil.virtual_memory should NOT be called for memory
            mock_psutil.virtual_memory.assert_not_called()

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_falls_back_to_psutil_when_no_broker(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        mgr.swift_monitor = None

        with patch("backend.process_cleanup_manager.psutil") as mock_psutil:
            mock_psutil.cpu_percent.return_value = 10.0
            mock_mem = MagicMock()
            mock_mem.percent = 55.0
            mock_mem.available = 8 * (1024 ** 3)
            mock_psutil.virtual_memory.return_value = mock_mem

            snapshot = mgr.get_system_snapshot()
            assert abs(snapshot["memory_percent"] - 55.0) < 0.1
            mock_psutil.virtual_memory.assert_called()


# ---------------------------------------------------------------------------
# Coordinator submission tests
# ---------------------------------------------------------------------------

class TestCoordinatorSubmission:
    """Verify CLEANUP actions are submitted through coordinator."""

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    @patch("backend.process_cleanup_manager.get_memory_controller")
    def test_handle_memory_pressure_submits_cleanup(
        self, mock_ctrl_fn, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import (
            CleanupEvent,
            CleanupEventType,
            ProcessCleanupManager,
        )

        mock_controller = MagicMock()
        mock_ctrl_fn.return_value = mock_controller

        mgr = ProcessCleanupManager()
        broker = _mock_broker()
        snap = _mock_snapshot_obj()
        broker.latest_snapshot = snap
        mgr._broker = broker
        mgr._mcp_active = True

        event = CleanupEvent(
            event_type=CleanupEventType.MEMORY_PRESSURE,
            data={
                "memory_percent": 90.0,
                "relief_level": "HIGH",
                "reason": "test",
            },
        )

        # Patch _schedule_memory_relief_with_level to avoid side effects
        mgr._schedule_memory_relief_with_level = MagicMock()

        with patch("backend.process_cleanup_manager.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 85.0
            mock_psutil.virtual_memory.return_value = mock_mem

            mgr._handle_memory_pressure(event)

        broker.coordinator.submit.assert_called_once()
        call_args = broker.coordinator.submit.call_args
        assert call_args[0][0] == ActuatorAction.CLEANUP
        assert isinstance(call_args[0][1], DecisionEnvelope)
        assert call_args[1]["source"] == "process_cleanup_manager" or call_args[0][2] == "process_cleanup_manager"

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    @patch("backend.process_cleanup_manager.get_memory_controller")
    def test_handle_memory_pressure_skips_when_coordinator_rejects(
        self, mock_ctrl_fn, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import (
            CleanupEvent,
            CleanupEventType,
            ProcessCleanupManager,
        )

        mock_controller = MagicMock()
        mock_ctrl_fn.return_value = mock_controller

        mgr = ProcessCleanupManager()
        broker = _mock_broker()
        snap = _mock_snapshot_obj()
        broker.latest_snapshot = snap
        broker.coordinator.submit.return_value = None  # rejected
        mgr._broker = broker
        mgr._mcp_active = True

        event = CleanupEvent(
            event_type=CleanupEventType.MEMORY_PRESSURE,
            data={
                "memory_percent": 90.0,
                "relief_level": "HIGH",
                "reason": "test",
            },
        )

        mgr._schedule_memory_relief_with_level = MagicMock()
        mgr._handle_memory_pressure(event)

        # Should NOT have scheduled relief since coordinator rejected
        mgr._schedule_memory_relief_with_level.assert_not_called()


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_contains_memory_budget_broker(self):
        import backend.process_cleanup_manager as mod
        source = inspect.getsource(mod)
        assert "memory_budget_broker" in source, (
            "process_cleanup_manager must import from memory_budget_broker"
        )

    def test_source_contains_coordinator(self):
        import backend.process_cleanup_manager as mod
        source = inspect.getsource(mod)
        assert "coordinator" in source, (
            "process_cleanup_manager must reference the coordinator"
        )

    def test_source_contains_latest_snapshot(self):
        import backend.process_cleanup_manager as mod
        source = inspect.getsource(mod)
        assert "latest_snapshot" in source, (
            "process_cleanup_manager must read broker.latest_snapshot"
        )

    def test_source_contains_pressure_policy(self):
        import backend.process_cleanup_manager as mod
        source = inspect.getsource(mod)
        assert "PressurePolicy" in source or "pressure_policy" in source or "broker.policy" in source, (
            "process_cleanup_manager must reference PressurePolicy or broker.policy"
        )

    def test_source_contains_decision_envelope(self):
        import backend.process_cleanup_manager as mod
        source = inspect.getsource(mod)
        assert "DecisionEnvelope" in source, (
            "process_cleanup_manager must use DecisionEnvelope"
        )

    def test_source_contains_actuator_action_cleanup(self):
        import backend.process_cleanup_manager as mod
        source = inspect.getsource(mod)
        assert "ActuatorAction.CLEANUP" in source, (
            "process_cleanup_manager must submit ActuatorAction.CLEANUP"
        )

    def test_source_contains_register_with_broker(self):
        from backend.process_cleanup_manager import ProcessCleanupManager
        assert hasattr(ProcessCleanupManager, "register_with_broker"), (
            "ProcessCleanupManager must have register_with_broker method"
        )

    def test_source_contains_mcp_active_guard(self):
        from backend.process_cleanup_manager import ProcessCleanupManager
        source = inspect.getsource(ProcessCleanupManager.get_system_snapshot)
        assert "_mcp_active" in source, (
            "get_system_snapshot must check _mcp_active flag"
        )

    def test_intelligent_memory_controller_has_register(self):
        from backend.process_cleanup_manager import IntelligentMemoryController
        assert hasattr(IntelligentMemoryController, "register_with_broker"), (
            "IntelligentMemoryController must have register_with_broker method"
        )


# ---------------------------------------------------------------------------
# Build decision envelope tests
# ---------------------------------------------------------------------------

class TestBuildDecisionEnvelope:
    """Verify _build_decision_envelope creates correct envelopes."""

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_returns_none_when_not_active(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        assert mgr._build_decision_envelope(PressureTier.CRITICAL) is None

    @patch("backend.process_cleanup_manager.GCPVMSessionManager")
    @patch("backend.process_cleanup_manager.get_health_monitor")
    @patch("backend.process_cleanup_manager.get_event_trigger")
    @patch("backend.process_cleanup_manager.get_port_pool")
    @patch("backend.process_cleanup_manager.get_circuit_breaker")
    def test_builds_envelope_with_broker_state(
        self, mock_cb, mock_pp, mock_et, mock_hm, mock_vm
    ):
        from backend.process_cleanup_manager import ProcessCleanupManager
        mgr = ProcessCleanupManager()
        broker = _mock_broker(epoch=7)
        broker.current_sequence = 42
        snap = _mock_snapshot_obj(snapshot_id="snap-xyz")
        broker.latest_snapshot = snap
        mgr._broker = broker
        mgr._mcp_active = True

        envelope = mgr._build_decision_envelope(PressureTier.CRITICAL)
        assert envelope is not None
        assert isinstance(envelope, DecisionEnvelope)
        assert envelope.epoch == 7
        assert envelope.sequence == 42
        assert envelope.snapshot_id == "snap-xyz"
        assert envelope.pressure_tier == PressureTier.CRITICAL
        assert envelope.policy_version == broker.policy.version


# ---------------------------------------------------------------------------
# Broker latest_snapshot tests
# ---------------------------------------------------------------------------

class TestBrokerLatestSnapshot:
    """Verify the broker caches snapshots via notify_pressure_observers."""

    @pytest.mark.asyncio
    async def test_latest_snapshot_initially_none(self):
        from backend.core.memory_budget_broker import MemoryBudgetBroker
        quantizer = MagicMock()
        broker = MemoryBudgetBroker(quantizer, epoch=1)
        assert broker.latest_snapshot is None

    @pytest.mark.asyncio
    async def test_latest_snapshot_set_after_notify(self):
        from backend.core.memory_budget_broker import MemoryBudgetBroker
        quantizer = MagicMock()
        broker = MemoryBudgetBroker(quantizer, epoch=1)
        snap = _mock_snapshot_obj()
        await broker.notify_pressure_observers(PressureTier.OPTIMAL, snap)
        assert broker.latest_snapshot is snap
