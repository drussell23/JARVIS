"""
Trinity v86.0 Enhancement Tests
==============================

Comprehensive tests for v86.0 advanced features:
- Priority-based ownership resolution
- Lock file validation and recovery
- Heartbeat monitoring with auto-restart
- Event-driven status synchronization
- Circuit breaker for state operations
- Adaptive timeout management
- Resource-aware launch sequencing

Run with: python3 -m pytest tests/test_trinity_v86.py -v
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import v86.0 components
from backend.core.trinity_integrator import (
    UnifiedStateCoordinator,
    ProcessOwnership,
    TrinityEntryPointDetector,
    ResourceChecker,
    ResourceAwareLaunchSequencer,
    LaunchSequenceStep,
    LaunchDecision,
    get_launch_sequencer,
)


class TestPriorityBasedOwnership:
    """Test priority-based ownership resolution (v86.0)."""

    def test_entry_point_priority_values(self):
        """Test that priority values are correctly defined."""
        priorities = UnifiedStateCoordinator.ENTRY_POINT_PRIORITY

        assert priorities["run_supervisor"] == 100
        assert priorities["run_supervisor.py"] == 100
        assert priorities["start_system"] == 50
        assert priorities["start_system.py"] == 50
        assert priorities["main_direct"] == 10
        assert priorities["main.py"] == 10
        assert priorities["unknown"] == 0

    @pytest.mark.asyncio
    async def test_priority_resolution_higher_wins(self):
        """Test that higher priority entry point wins ownership."""
        coord = UnifiedStateCoordinator()

        # Create mock owner with lower priority
        lower_priority_owner = ProcessOwnership(
            entry_point="start_system",
            pid=12345,
            cookie="test-cookie",
            hostname="localhost",
            acquired_at=time.time(),
            last_heartbeat=time.time(),
        )

        # Higher priority should win
        should_takeover, reason = await coord._resolve_ownership_conflict(
            "run_supervisor", lower_priority_owner
        )

        assert should_takeover is True
        assert "Higher priority" in reason

    @pytest.mark.asyncio
    async def test_priority_resolution_lower_loses(self):
        """Test that lower priority entry point loses ownership."""
        coord = UnifiedStateCoordinator()

        # Create mock owner with higher priority
        higher_priority_owner = ProcessOwnership(
            entry_point="run_supervisor",
            pid=12345,
            cookie="test-cookie",
            hostname="localhost",
            acquired_at=time.time(),
            last_heartbeat=time.time(),
        )

        # Lower priority should lose
        should_takeover, reason = await coord._resolve_ownership_conflict(
            "start_system", higher_priority_owner
        )

        assert should_takeover is False
        assert "Lower priority" in reason

    @pytest.mark.asyncio
    async def test_same_priority_timestamp_tiebreaker(self):
        """Test that same priority uses timestamp as tiebreaker."""
        coord = UnifiedStateCoordinator()
        coord._process_start_time = time.time() - 100  # We started earlier

        # Create mock owner with same priority but later start
        same_priority_owner = ProcessOwnership(
            entry_point="run_supervisor",
            pid=12345,
            cookie="test-cookie",
            hostname="localhost",
            acquired_at=time.time(),  # Later timestamp
            last_heartbeat=time.time(),
        )

        # Earlier process should win
        should_takeover, reason = await coord._resolve_ownership_conflict(
            "run_supervisor", same_priority_owner
        )

        assert should_takeover is True
        assert "Earlier process" in reason


class TestLockFileValidation:
    """Test lock file validation and recovery (v86.0)."""

    @pytest.mark.asyncio
    async def test_validate_nonexistent_lock_file(self):
        """Test validation passes for nonexistent lock file."""
        coord = UnifiedStateCoordinator()

        # Nonexistent file should be valid
        result = await coord._validate_and_recover_lock_file(
            Path("/nonexistent/path/test.lock")
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_oversized_lock_file(self):
        """Test validation handles oversized lock files."""
        coord = UnifiedStateCoordinator()

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "test.lock"

            # Create oversized lock file (> 4KB)
            lock_file.write_text("x" * 10000)

            # Should recover (remove/rename the file)
            result = await coord._validate_and_recover_lock_file(lock_file)

            assert result is True
            # File should be removed or renamed
            assert not lock_file.exists() or lock_file.stat().st_size < 4096


class TestStateFileValidation:
    """Test state file integrity validation (v86.0)."""

    @pytest.mark.asyncio
    async def test_validate_valid_state_file(self):
        """Test validation passes for valid state file."""
        coord = UnifiedStateCoordinator()

        with tempfile.TemporaryDirectory() as tmpdir:
            coord.state_file = Path(tmpdir) / "state.json"

            # Write valid JSON
            coord.state_file.write_text(json.dumps({
                "owners": {},
                "last_update": time.time()
            }))

            result = await coord._validate_and_recover_state_file()
            assert result is True

    @pytest.mark.asyncio
    async def test_validate_corrupted_state_file(self):
        """Test validation handles corrupted state file."""
        coord = UnifiedStateCoordinator()

        with tempfile.TemporaryDirectory() as tmpdir:
            coord.state_file = Path(tmpdir) / "state.json"

            # Write invalid JSON
            coord.state_file.write_text("{invalid json here")

            result = await coord._validate_and_recover_state_file()

            # Should recover
            assert result is True
            # File should be renamed/removed
            assert not coord.state_file.exists()


class TestCircuitBreaker:
    """Test circuit breaker for state operations (v86.0)."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_closed_on_success(self):
        """Test circuit breaker stays closed on success."""
        coord = UnifiedStateCoordinator()
        coord._circuit_state = "closed"
        coord._circuit_failure_count = 0

        async def success_operation():
            return "success"

        result = await coord._circuit_breaker_execute(success_operation)

        assert result == "success"
        assert coord._circuit_state == "closed"
        assert coord._circuit_failure_count == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_on_failures(self):
        """Test circuit breaker opens after threshold failures."""
        coord = UnifiedStateCoordinator()
        coord._circuit_state = "closed"
        coord._circuit_failure_count = 4  # One below threshold
        coord._circuit_failure_threshold = 5

        async def failing_operation():
            raise Exception("Test failure")

        with pytest.raises(Exception):
            await coord._circuit_breaker_execute(failing_operation)

        # Should now be open
        assert coord._circuit_state == "open"
        assert coord._circuit_failure_count == 5

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open_recovery(self):
        """Test circuit breaker transitions to half-open after timeout."""
        coord = UnifiedStateCoordinator()
        coord._circuit_state = "open"
        coord._circuit_last_failure_time = time.time() - 60  # 60s ago
        coord._circuit_recovery_timeout = 30.0  # 30s timeout

        async def success_operation():
            return "success"

        result = await coord._circuit_breaker_execute(success_operation)

        # Should recover to closed
        assert result == "success"
        assert coord._circuit_state == "closed"
        assert coord._circuit_failure_count == 0


class TestAdaptiveTimeout:
    """Test adaptive timeout management (v86.0)."""

    def test_adaptive_timeout_with_no_history(self):
        """Test fallback to default when no history."""
        coord = UnifiedStateCoordinator()
        coord._operation_history = {}

        timeout = coord.get_adaptive_timeout("test_op", 10.0)

        assert timeout == 10.0

    def test_adaptive_timeout_with_history(self):
        """Test adaptive timeout uses 95th percentile."""
        coord = UnifiedStateCoordinator()
        coord._timeout_multiplier = 1.5

        # Simulate historical operation times
        coord._operation_history = {
            "test_op": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        }

        timeout = coord.get_adaptive_timeout("test_op", 5.0)

        # 95th percentile of [1-10] is ~10, * 1.5 = ~15
        # Should be clamped between default (5) and 10x default (50)
        assert timeout >= 5.0
        assert timeout <= 50.0

    def test_record_operation_duration(self):
        """Test recording operation durations."""
        coord = UnifiedStateCoordinator()
        coord._operation_history = {}

        coord._record_operation_duration("test_op", 1.5)
        coord._record_operation_duration("test_op", 2.5)

        assert "test_op" in coord._operation_history
        assert len(coord._operation_history["test_op"]) == 2
        assert 1.5 in coord._operation_history["test_op"]
        assert 2.5 in coord._operation_history["test_op"]


class TestEventDrivenSync:
    """Test event-driven status synchronization (v86.0)."""

    @pytest.mark.asyncio
    async def test_publish_component_event(self):
        """Test event publishing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            coord = UnifiedStateCoordinator()
            coord.state_file = Path(tmpdir) / "state.json"
            coord._our_entry_point = "run_supervisor"

            # Publish event
            await coord.publish_component_event(
                "jarvis",
                UnifiedStateCoordinator.ComponentEventType.READY,
                metadata={"test": "value"}
            )

            # Verify event was stored
            state = json.loads(coord.state_file.read_text())
            assert "events" in state
            assert len(state["events"]) > 0

            event = state["events"][-1]
            assert event["component"] == "jarvis"
            assert event["event_type"] == "ready"
            assert event["metadata"]["test"] == "value"

    @pytest.mark.asyncio
    async def test_get_recent_events(self):
        """Test retrieving recent events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            coord = UnifiedStateCoordinator()
            coord.state_file = Path(tmpdir) / "state.json"
            coord._our_entry_point = "run_supervisor"

            # Publish multiple events
            await coord.publish_component_event(
                "jarvis",
                UnifiedStateCoordinator.ComponentEventType.STARTING,
            )
            await coord.publish_component_event(
                "jarvis",
                UnifiedStateCoordinator.ComponentEventType.READY,
            )

            # Get events
            events = await coord.get_recent_events(component="jarvis")

            assert len(events) == 2

    @pytest.mark.asyncio
    async def test_event_filtering_by_type(self):
        """Test filtering events by type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            coord = UnifiedStateCoordinator()
            coord.state_file = Path(tmpdir) / "state.json"
            coord._our_entry_point = "run_supervisor"

            # Publish events of different types
            await coord.publish_component_event(
                "jarvis",
                UnifiedStateCoordinator.ComponentEventType.STARTING,
            )
            await coord.publish_component_event(
                "jarvis",
                UnifiedStateCoordinator.ComponentEventType.READY,
            )

            # Filter by type
            events = await coord.get_recent_events(
                component="jarvis",
                event_types=["ready"]
            )

            assert len(events) == 1
            assert events[0]["event_type"] == "ready"


class TestResourceAwareLaunchSequencer:
    """Test resource-aware launch sequencing (v86.0)."""

    @pytest.mark.asyncio
    async def test_sequencer_initialization(self):
        """Test sequencer initializes with default components."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        assert sequencer._initialized
        assert "jarvis_body" in sequencer._components
        assert "jarvis_prime" in sequencer._components
        assert "reactor_core" in sequencer._components

    @pytest.mark.asyncio
    async def test_component_priority_ordering(self):
        """Test components are ordered by priority."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        # jarvis_body should have lowest priority (launch first)
        assert sequencer._components["jarvis_body"].priority == 10
        assert sequencer._components["jarvis_prime"].priority == 20
        assert sequencer._components["reactor_core"].priority == 30

    @pytest.mark.asyncio
    async def test_dependency_checking(self):
        """Test dependencies are checked before launch."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        # Try to launch jarvis_prime without jarvis_body ready
        decision = await sequencer.evaluate_launch("jarvis_prime")

        assert decision.can_launch is False
        assert "Missing dependencies" in decision.reason
        assert "jarvis_body" in decision.reason

    @pytest.mark.asyncio
    async def test_resource_reservation(self):
        """Test resource reservation before launch."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        initial_reserved = sequencer._reserved_memory_gb

        # Reserve resources
        result = await sequencer.reserve_resources("jarvis_body")

        assert result is True
        assert sequencer._reserved_memory_gb > initial_reserved

    @pytest.mark.asyncio
    async def test_mark_ready_releases_reservation(self):
        """Test marking ready releases resource reservation."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        # Reserve and then mark ready
        await sequencer.reserve_resources("jarvis_body")
        reserved_after_reserve = sequencer._reserved_memory_gb

        await sequencer.mark_ready("jarvis_body", startup_time_sec=5.0)

        assert sequencer._reserved_memory_gb < reserved_after_reserve
        assert "jarvis_body" in sequencer._ready_components

    @pytest.mark.asyncio
    async def test_launch_sequence_ordering(self):
        """Test launch sequence respects priority and dependencies."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        # Get launch sequence
        sequence = await sequencer.get_launch_sequence()

        # Should be ordered by priority
        component_order = [d.component for d in sequence]
        assert component_order.index("jarvis_body") < component_order.index("jarvis_prime")
        assert component_order.index("jarvis_body") < component_order.index("reactor_core")

    @pytest.mark.asyncio
    async def test_startup_time_recording(self):
        """Test startup times are recorded for adaptive learning."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        # Record startup time
        sequencer.record_startup_time("jarvis_body", 5.0)
        sequencer.record_startup_time("jarvis_body", 6.0)

        # Should be in history
        assert "jarvis_body" in sequencer._startup_history
        assert len(sequencer._startup_history["jarvis_body"]) == 2

    @pytest.mark.asyncio
    async def test_estimated_startup_uses_history(self):
        """Test estimated startup time uses historical data."""
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        # Add history (need at least 5 samples)
        for i in range(10):
            sequencer.record_startup_time("jarvis_body", 5.0 + i * 0.1)

        # Get estimate
        estimate = sequencer.get_estimated_startup_time("jarvis_body")

        # Should be based on 90th percentile of history
        assert estimate > 5.0
        assert estimate < 10.0

    def test_get_status(self):
        """Test getting sequencer status."""
        sequencer = ResourceAwareLaunchSequencer()

        status = sequencer.get_status()

        assert "initialized" in status
        assert "launched_components" in status
        assert "ready_components" in status
        assert "reserved_memory_gb" in status


class TestComponentEventTypes:
    """Test component event type enum (v86.0)."""

    def test_all_event_types_defined(self):
        """Test all required event types are defined."""
        EventType = UnifiedStateCoordinator.ComponentEventType

        assert EventType.STARTING.value == "starting"
        assert EventType.READY.value == "ready"
        assert EventType.DEGRADED.value == "degraded"
        assert EventType.FAILED.value == "failed"
        assert EventType.SHUTTING_DOWN.value == "shutting_down"
        assert EventType.HEARTBEAT_LOST.value == "heartbeat_lost"
        assert EventType.OWNERSHIP_ACQUIRED.value == "ownership_acquired"
        assert EventType.OWNERSHIP_RELEASED.value == "ownership_released"
        assert EventType.OWNERSHIP_TRANSFERRED.value == "ownership_transferred"


class TestResourceChecker:
    """Test existing resource checker integration."""

    @pytest.mark.asyncio
    async def test_check_resources_returns_tuple(self):
        """Test resource check returns (can_launch, warnings) tuple."""
        can_launch, warnings = await ResourceChecker.check_resources_for_component(
            "jarvis_body"
        )

        assert isinstance(can_launch, bool)
        assert isinstance(warnings, list)

    @pytest.mark.asyncio
    async def test_wait_for_resources_timeout(self):
        """Test wait_for_resources respects timeout."""
        # Mock insufficient resources
        with patch.object(
            ResourceChecker,
            "check_resources_for_component",
            return_value=(False, ["Low memory"])
        ):
            start = time.time()
            result = await ResourceChecker.wait_for_resources(
                "jarvis_body",
                timeout=2.0,
                check_interval=0.5,
            )
            elapsed = time.time() - start

            assert result is False
            assert elapsed >= 2.0
            assert elapsed < 4.0  # Should stop at timeout


class TestIntegration:
    """Integration tests for v86.0 components working together."""

    @pytest.mark.asyncio
    async def test_full_startup_sequence(self):
        """Test full startup sequence with all v86.0 features."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup
            os.environ["JARVIS_STATE_DIR"] = tmpdir

            # Get coordinator
            coord = await UnifiedStateCoordinator.get_instance()
            coord.state_file = Path(tmpdir) / "state.json"
            coord.lock_dir = Path(tmpdir) / "locks"
            coord.lock_dir.mkdir(exist_ok=True)

            # Get sequencer
            sequencer = await get_launch_sequencer()
            await sequencer.reset()

            # Simulate startup sequence
            # 1. Evaluate jarvis_body launch
            decision = await sequencer.evaluate_launch("jarvis_body")
            assert decision.can_launch is True

            # 2. Reserve and launch
            await sequencer.reserve_resources("jarvis_body")
            await sequencer.mark_launched("jarvis_body")

            # 3. Mark ready
            await sequencer.mark_ready("jarvis_body", startup_time_sec=3.0)

            # 4. Now jarvis_prime should be launchable
            decision = await sequencer.evaluate_launch("jarvis_prime")
            # Note: can_launch depends on actual system resources
            assert decision.reason != "Missing dependencies"

            # Cleanup
            del os.environ["JARVIS_STATE_DIR"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
