"""
Phase 3.0 Integration Tests
===========================

Integration tests for the Phase 3.0 Architecture Upgrade components:
- Service Registry
- Process Orchestrator
- Training Coordinator
- Reactor Core Interface
- System Hardening

Run with:
    pytest tests/integration/test_phase3_integration.py -v
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# =============================================================================
# Service Registry Tests
# =============================================================================

class TestServiceRegistry:
    """Tests for the Service Registry v3.0."""

    @pytest.fixture
    def temp_registry_dir(self, tmp_path):
        """Create a temporary registry directory."""
        return tmp_path / "registry"

    @pytest.mark.asyncio
    async def test_service_registration(self, temp_registry_dir):
        """Test that services can be registered and discovered."""
        from backend.core.service_registry import ServiceRegistry

        registry = ServiceRegistry(registry_dir=temp_registry_dir)

        # Register a service
        service = await registry.register_service(
            service_name="test-service",
            pid=os.getpid(),
            port=8080,
            health_endpoint="/health"
        )

        assert service.service_name == "test-service"
        assert service.port == 8080
        assert service.pid == os.getpid()

        # Discover the service
        discovered = await registry.discover_service("test-service")

        assert discovered is not None
        assert discovered.service_name == "test-service"
        assert discovered.port == 8080

    @pytest.mark.asyncio
    async def test_service_heartbeat(self, temp_registry_dir):
        """Test service heartbeat functionality."""
        from backend.core.service_registry import ServiceRegistry

        registry = ServiceRegistry(registry_dir=temp_registry_dir)

        # Register a service
        await registry.register_service(
            service_name="heartbeat-test",
            pid=os.getpid(),
            port=8081
        )

        # Send heartbeat
        success = await registry.heartbeat("heartbeat-test", status="healthy")
        assert success is True

        # Check updated status
        service = await registry.discover_service("heartbeat-test")
        assert service.status == "healthy"

    @pytest.mark.asyncio
    async def test_service_deregistration(self, temp_registry_dir):
        """Test service deregistration."""
        from backend.core.service_registry import ServiceRegistry

        registry = ServiceRegistry(registry_dir=temp_registry_dir)

        # Register and then deregister
        await registry.register_service(
            service_name="temp-service",
            pid=os.getpid(),
            port=8082
        )

        success = await registry.deregister_service("temp-service")
        assert success is True

        # Should not be discoverable
        service = await registry.discover_service("temp-service")
        assert service is None

    @pytest.mark.asyncio
    async def test_list_services(self, temp_registry_dir):
        """Test listing all services."""
        from backend.core.service_registry import ServiceRegistry

        registry = ServiceRegistry(registry_dir=temp_registry_dir)

        # Register multiple services
        await registry.register_service("service-1", os.getpid(), 8001)
        await registry.register_service("service-2", os.getpid(), 8002)
        await registry.register_service("service-3", os.getpid(), 8003)

        # List all services
        services = await registry.list_services(healthy_only=False)
        assert len(services) == 3


# =============================================================================
# System Hardening Tests
# =============================================================================

class TestSystemHardening:
    """Tests for the System Hardening module."""

    @pytest.fixture
    def temp_jarvis_home(self, tmp_path):
        """Create a temporary JARVIS home directory."""
        return tmp_path / ".jarvis"

    @pytest.mark.asyncio
    async def test_critical_directory_creation(self, temp_jarvis_home):
        """Test that all critical directories are created."""
        from backend.core.system_hardening import (
            CriticalDirectoryManager,
            HardeningConfig
        )

        config = HardeningConfig(jarvis_home=temp_jarvis_home)
        manager = CriticalDirectoryManager(config)

        results = await manager.initialize_all()

        # All directories should be created successfully
        assert all(results.values()), f"Some directories failed: {results}"

        # Verify directories exist
        assert (temp_jarvis_home / "registry").exists()
        assert (temp_jarvis_home / "bridge" / "training_staging").exists()
        assert (temp_jarvis_home / "trinity" / "events").exists()

    @pytest.mark.asyncio
    async def test_directory_verification(self, temp_jarvis_home):
        """Test directory existence verification."""
        from backend.core.system_hardening import (
            CriticalDirectoryManager,
            HardeningConfig
        )

        config = HardeningConfig(jarvis_home=temp_jarvis_home)
        manager = CriticalDirectoryManager(config)

        # Before initialization
        results_before = manager.verify_all_exist()
        assert not any(results_before.values())

        # After initialization
        await manager.initialize_all()
        results_after = manager.verify_all_exist()
        assert all(results_after.values())

    @pytest.mark.asyncio
    async def test_shutdown_hook_registration(self):
        """Test shutdown hook registration."""
        from backend.core.system_hardening import GracefulShutdownManager, ShutdownPhase

        manager = GracefulShutdownManager()

        callback_called = False

        async def test_callback():
            nonlocal callback_called
            callback_called = True

        manager.register_hook(
            name="test-hook",
            callback=test_callback,
            phase=ShutdownPhase.CLEANUP
        )

        # Execute shutdown
        results = await manager.execute_shutdown()

        assert "test-hook" in results
        assert results["test-hook"] is True
        assert callback_called is True

    @pytest.mark.asyncio
    async def test_system_health(self):
        """Test system health monitoring."""
        from backend.core.system_hardening import get_system_health

        health = await get_system_health()

        assert health.timestamp > 0
        assert 0 <= health.cpu_percent <= 100
        assert 0 <= health.memory_percent <= 100
        assert health.memory_available_gb > 0
        assert health.overall_status in ("healthy", "degraded", "critical")


# =============================================================================
# Training Coordinator Tests
# =============================================================================

class TestTrainingCoordinator:
    """Tests for the Training Coordinator v3.0."""

    @pytest.fixture
    def temp_config(self, tmp_path):
        """Create a temporary configuration."""
        from backend.intelligence.advanced_training_coordinator import AdvancedTrainingConfig

        return AdvancedTrainingConfig(
            dropbox_dir=tmp_path / "dropbox",
            state_db_path=tmp_path / "state.db",
            checkpoint_dir=tmp_path / "checkpoints"
        )

    @pytest.mark.asyncio
    async def test_data_serializer(self, temp_config):
        """Test the DataSerializer component."""
        from backend.intelligence.advanced_training_coordinator import DataSerializer

        serializer = DataSerializer(temp_config)

        # Create test data
        experiences = [
            {"input": "hello", "output": "world", "reward": 1.0}
            for _ in range(100)
        ]

        # Serialize with compression
        data = await serializer.serialize(experiences, compress=True)
        assert isinstance(data, bytes)
        assert len(data) > 0

        # Deserialize
        recovered = await serializer.deserialize(data, compressed=True)
        assert len(recovered) == 100
        assert recovered[0]["input"] == "hello"

        serializer.shutdown()

    @pytest.mark.asyncio
    async def test_dropbox_manager(self, temp_config):
        """Test the DropBoxManager component."""
        from backend.intelligence.advanced_training_coordinator import DropBoxManager

        # Ensure dropbox size threshold is low for testing
        temp_config.dropbox_size_threshold_mb = 0.001

        manager = DropBoxManager(temp_config)

        # Create test experiences
        experiences = [{"data": f"test_{i}"} for i in range(1000)]

        # Prepare dataset (should use dropbox for this size)
        path = await manager.prepare_dataset("test-job-123", experiences)

        assert path is not None
        assert path.exists()

        # Load dataset back
        loaded = await manager.load_dataset(path)
        assert len(loaded) == 1000

        # Cleanup
        success = await manager.cleanup("test-job-123")
        assert success is True
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_state_manager(self, temp_config):
        """Test the TrainingStateManager component."""
        from backend.intelligence.advanced_training_coordinator import TrainingStateManager

        manager = TrainingStateManager(temp_config)

        # Save a job
        await manager.save_job(
            job_id="state-test-job",
            model_type="test_model",
            status="running",
            priority=1,
            metadata={"epochs": 10}
        )

        # Get active jobs
        active = await manager.get_active_jobs()
        assert len(active) == 1
        assert active[0]["job_id"] == "state-test-job"

        # Mark completed
        await manager.mark_completed("state-test-job", success=True)

        # Should no longer be in active jobs
        active = await manager.get_active_jobs()
        assert len(active) == 0


# =============================================================================
# Reactor Core Interface Tests
# =============================================================================

class TestReactorCoreInterface:
    """Tests for the Reactor Core API Interface."""

    @pytest.fixture
    def temp_config(self, tmp_path):
        """Create a temporary configuration."""
        from backend.reactor.reactor_api_interface import ReactorAPIConfig

        return ReactorAPIConfig(
            dropbox_dir=tmp_path / "dropbox"
        )

    @pytest.mark.asyncio
    async def test_dropbox_handler(self, temp_config):
        """Test the DropBoxHandler component."""
        from backend.reactor.reactor_api_interface import DropBoxHandler

        handler = DropBoxHandler(temp_config)

        # Create a test dataset file
        dataset_file = temp_config.dropbox_dir / "test.json"
        test_data = [{"x": 1}, {"x": 2}]
        dataset_file.write_text(json.dumps(test_data))

        # Load dataset
        loaded = await handler.load_dataset(str(dataset_file))
        assert loaded == test_data

        # Cleanup
        success = await handler.cleanup(str(dataset_file))
        assert success is True

    @pytest.mark.asyncio
    async def test_training_job_manager(self, temp_config):
        """Test the TrainingJobManager component."""
        from backend.reactor.reactor_api_interface import TrainingJobManager

        manager = TrainingJobManager(temp_config)

        # Start a training job
        success = await manager.start_training(
            job_id="test-training-job",
            model_type="test",
            experiences=[{"data": "test"}],
            config={},
            epochs=3
        )
        assert success is True

        # Check status
        status = await manager.get_status("test-training-job")
        assert status["job_id"] == "test-training-job"
        assert status["status"] in ("running", "training")

        # Wait for completion
        await asyncio.sleep(2)

        # Check final status
        status = await manager.get_status("test-training-job")
        assert status["status"] == "completed"


# =============================================================================
# Integration Test: Full Pipeline
# =============================================================================

class TestFullPipeline:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_training_pipeline(self, tmp_path):
        """Test the complete training pipeline from submission to completion."""
        from backend.core.system_hardening import CriticalDirectoryManager, HardeningConfig
        from backend.core.service_registry import ServiceRegistry

        # Step 1: Initialize critical directories
        config = HardeningConfig(jarvis_home=tmp_path / ".jarvis")
        dir_manager = CriticalDirectoryManager(config)
        await dir_manager.initialize_all()

        # Step 2: Set up service registry
        registry = ServiceRegistry(registry_dir=tmp_path / ".jarvis" / "registry")

        # Step 3: Register mock services
        await registry.register_service("jarvis-core", os.getpid(), 5001)
        await registry.register_service("reactor-core", os.getpid(), 8003)

        # Step 4: Verify discovery
        jarvis = await registry.discover_service("jarvis-core")
        reactor = await registry.discover_service("reactor-core")

        assert jarvis is not None
        assert reactor is not None
        assert jarvis.port == 5001
        assert reactor.port == 8003

        # Step 5: Send heartbeats
        await registry.heartbeat("jarvis-core", status="healthy")
        await registry.heartbeat("reactor-core", status="healthy")

        # Step 6: List healthy services
        services = await registry.list_services(healthy_only=True)
        assert len(services) == 2

        # Cleanup
        await registry.deregister_service("jarvis-core")
        await registry.deregister_service("reactor-core")


# =============================================================================
# Trinity IPC Hub Tests
# =============================================================================

class TestTrinityIPCHub:
    """Tests for the Trinity IPC Hub v4.0."""

    @pytest.fixture
    def temp_ipc_dir(self, tmp_path):
        """Create a temporary IPC directory."""
        return tmp_path / "trinity" / "ipc"

    @pytest.mark.asyncio
    async def test_ipc_hub_initialization(self, temp_ipc_dir):
        """Test IPC Hub initialization."""
        from backend.core.trinity_ipc_hub import TrinityIPCHub, TrinityIPCConfig

        config = TrinityIPCConfig(ipc_base_dir=temp_ipc_dir)
        hub = TrinityIPCHub(config)

        await hub.start()

        # Verify hub is started
        health = await hub.get_health()
        assert health["status"] == "healthy"

        await hub.stop()

    @pytest.mark.asyncio
    async def test_model_registry(self, temp_ipc_dir):
        """Test model registry (Gap 5)."""
        from backend.core.trinity_ipc_hub import TrinityIPCHub, TrinityIPCConfig

        config = TrinityIPCConfig(ipc_base_dir=temp_ipc_dir)
        hub = TrinityIPCHub(config)
        await hub.start()

        # Register a model
        model = await hub.models.register_model(
            model_id="test-model-v1",
            version="1.0.0",
            model_type="test",
            capabilities=["test_capability"],
            metrics={"accuracy": 0.95}
        )

        assert model.model_id == "test-model-v1"
        assert model.version == "1.0.0"

        # List models
        models = await hub.models.list_models()
        assert len(models) == 1

        # Find best model
        best = await hub.models.find_best_model("test", "accuracy")
        assert best.model_id == "test-model-v1"

        await hub.stop()

    @pytest.mark.asyncio
    async def test_event_bus(self, temp_ipc_dir):
        """Test Pub/Sub event bus (Gap 9)."""
        from backend.core.trinity_ipc_hub import TrinityIPCHub, TrinityIPCConfig

        config = TrinityIPCConfig(ipc_base_dir=temp_ipc_dir)
        hub = TrinityIPCHub(config)
        await hub.start()

        received_events = []

        async def handler(event):
            received_events.append(event)

        # Subscribe to events
        unsubscribe = hub.events.subscribe("test.*", handler)

        # Publish event
        await hub.events.publish("test.event", {"data": "hello"})

        # Wait for event delivery
        await asyncio.sleep(0.1)

        assert len(received_events) == 1
        assert received_events[0].topic == "test.event"

        unsubscribe()
        await hub.stop()

    @pytest.mark.asyncio
    async def test_message_queue(self, temp_ipc_dir):
        """Test reliable message queue (Gap 10)."""
        from backend.core.trinity_ipc_hub import (
            TrinityIPCHub,
            TrinityIPCConfig,
            DeliveryGuarantee
        )

        config = TrinityIPCConfig(ipc_base_dir=temp_ipc_dir)
        hub = TrinityIPCHub(config)
        await hub.start()

        # Enqueue a message
        msg_id = await hub.queue.enqueue(
            "test_queue",
            {"task": "process_data"},
            delivery=DeliveryGuarantee.AT_LEAST_ONCE
        )

        assert msg_id is not None

        # Dequeue the message
        message = await hub.queue.dequeue("test_queue", timeout=1.0)
        assert message is not None

        # Acknowledge
        await hub.queue.ack(message.message_id)

        # Queue should be empty now
        empty_msg = await hub.queue.dequeue("test_queue", timeout=0.1)
        assert empty_msg is None

        await hub.stop()

    @pytest.mark.asyncio
    async def test_training_pipeline(self, temp_ipc_dir):
        """Test training data pipeline (Gap 4)."""
        from backend.core.trinity_ipc_hub import TrinityIPCHub, TrinityIPCConfig

        config = TrinityIPCConfig(ipc_base_dir=temp_ipc_dir)
        hub = TrinityIPCHub(config)
        await hub.start()

        # Submit training interaction
        await hub.pipeline.submit_interaction(
            user_input="Hello, how are you?",
            assistant_response="I'm doing well, thank you!",
            reward=1.0,
            model_type="general"
        )

        # Check pipeline stats
        stats = await hub.pipeline.get_pipeline_stats()
        assert stats["buffer_size"] == 1

        await hub.stop()

    @pytest.mark.asyncio
    async def test_circuit_breaker(self):
        """Test circuit breaker resilience pattern."""
        from backend.core.trinity_ipc_hub import CircuitBreaker, CircuitOpenError

        breaker = CircuitBreaker(threshold=3, timeout=1.0)

        # Initially closed
        assert await breaker.can_execute() is True

        # Record failures to open circuit
        await breaker.record_failure()
        await breaker.record_failure()
        await breaker.record_failure()

        # Circuit should be open
        assert breaker.is_open is True
        assert await breaker.can_execute() is False

        # Wait for timeout
        await asyncio.sleep(1.1)

        # Should be half-open now
        assert await breaker.can_execute() is True


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
