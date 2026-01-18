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
        await registry.register_service("reactor-core", os.getpid(), 8090)

        # Step 4: Verify discovery
        jarvis = await registry.discover_service("jarvis-core")
        reactor = await registry.discover_service("reactor-core")

        assert jarvis is not None
        assert reactor is not None
        assert jarvis.port == 5001
        assert reactor.port == 8090

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
# Trinity State Manager Tests (Category 2: State Management)
# =============================================================================

class TestTrinityStateManager:
    """Test Trinity State Manager - All 8 state management gaps."""

    @pytest.mark.asyncio
    async def test_state_manager_initialization(self):
        """Test state manager initializes correctly."""
        from backend.core.trinity_state_manager import (
            TrinityStateManager,
            StateManagerConfig
        )

        config = StateManagerConfig()
        manager = await TrinityStateManager.create(config, "test-node")

        try:
            assert manager._started is True
            assert manager.node_id == "test-node"

            metrics = manager.get_metrics()
            assert "node_id" in metrics
            assert "state_entries" in metrics
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_state_set_and_get(self):
        """Test basic state operations."""
        from backend.core.trinity_state_manager import (
            TrinityStateManager,
            StateManagerConfig,
            StateNamespace
        )

        config = StateManagerConfig()
        manager = await TrinityStateManager.create(config, "test-node")

        try:
            # Set a value
            entry = await manager.set("test_key", "test_value", StateNamespace.SHARED)
            assert entry.key == "test_key"
            assert entry.value == "test_value"
            assert entry.version == 1

            # Get the value
            value = await manager.get("test_key", StateNamespace.SHARED)
            assert value == "test_value"

            # Update the value
            entry2 = await manager.set("test_key", "updated_value", StateNamespace.SHARED)
            assert entry2.version == 2

            # Get updated value
            value2 = await manager.get("test_key", StateNamespace.SHARED)
            assert value2 == "updated_value"
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_vector_clock(self):
        """Test vector clock for conflict resolution."""
        from backend.core.trinity_state_manager import VectorClock

        clock1 = VectorClock()
        clock1 = clock1.increment("node_a")
        clock1 = clock1.increment("node_a")

        clock2 = VectorClock()
        clock2 = clock2.increment("node_b")

        # clock1 and clock2 are concurrent (neither happened-before)
        assert clock1.is_concurrent(clock2) is True

        # Merge clocks
        merged = clock1.merge(clock2)
        assert merged.clocks["node_a"] == 2
        assert merged.clocks["node_b"] == 1

    @pytest.mark.asyncio
    async def test_gcounter_crdt(self):
        """Test grow-only counter CRDT."""
        from backend.core.trinity_state_manager import GCounter

        counter1 = GCounter()
        counter1 = counter1.increment("node_a", 5)
        counter1 = counter1.increment("node_a", 3)

        counter2 = GCounter()
        counter2 = counter2.increment("node_b", 4)

        # Merge counters
        merged = counter1.merge(counter2)
        assert merged.value() == 12  # 8 (node_a) + 4 (node_b)

    @pytest.mark.asyncio
    async def test_state_versioning(self):
        """Test state versioning and history (Gap 3)."""
        from backend.core.trinity_state_manager import (
            TrinityStateManager,
            StateManagerConfig,
            StateNamespace
        )

        config = StateManagerConfig()
        config.enable_versioning = True
        manager = await TrinityStateManager.create(config, "test-node")

        try:
            # Create multiple versions
            await manager.set("versioned_key", "v1", StateNamespace.SHARED)
            await manager.set("versioned_key", "v2", StateNamespace.SHARED)
            await manager.set("versioned_key", "v3", StateNamespace.SHARED)

            # Get history
            history = await manager.get_history("versioned_key", StateNamespace.SHARED)
            assert len(history) >= 3

            # Rollback to version 1
            entry = await manager.rollback("versioned_key", 1, StateNamespace.SHARED)
            assert entry is not None
            assert entry.value == "v1"
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_state_partitioning(self):
        """Test state partitioning by namespace (Gap 6)."""
        from backend.core.trinity_state_manager import (
            TrinityStateManager,
            StateManagerConfig,
            StateNamespace
        )

        config = StateManagerConfig()
        manager = await TrinityStateManager.create(config, "test-node")

        try:
            # Set values in different namespaces
            await manager.set("key1", "jarvis_value", StateNamespace.JARVIS_BODY)
            await manager.set("key1", "prime_value", StateNamespace.JARVIS_PRIME)
            await manager.set("key1", "reactor_value", StateNamespace.REACTOR_CORE)

            # Values should be independent
            v1 = await manager.get("key1", StateNamespace.JARVIS_BODY)
            v2 = await manager.get("key1", StateNamespace.JARVIS_PRIME)
            v3 = await manager.get("key1", StateNamespace.REACTOR_CORE)

            assert v1 == "jarvis_value"
            assert v2 == "prime_value"
            assert v3 == "reactor_value"
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_compression_engine(self):
        """Test state compression (Gap 7)."""
        from backend.core.trinity_state_manager import (
            CompressionEngine,
            CompressionType,
            StateManagerConfig
        )

        config = StateManagerConfig()
        config.compression_threshold = 100  # Low threshold for testing

        engine = CompressionEngine(config)

        # Create compressible data
        data = b"x" * 1000  # 1KB of repetitive data

        compressed, comp_type = engine.compress(data)

        # Should be compressed
        assert comp_type in (CompressionType.ZLIB, CompressionType.LZ4)
        assert len(compressed) < len(data)

        # Decompress
        decompressed = engine.decompress(compressed, comp_type)
        assert decompressed == data

    @pytest.mark.asyncio
    async def test_access_control(self):
        """Test role-based access control (Gap 8)."""
        from backend.core.trinity_state_manager import (
            AccessController,
            AccessLevel,
            StateNamespace,
            StateManagerConfig
        )

        config = StateManagerConfig()
        controller = AccessController(config)

        # Create read-only token
        token = controller.create_token(
            "test-node",
            {StateNamespace.SHARED},
            AccessLevel.READ
        )

        assert token.can_read(StateNamespace.SHARED) is True
        assert token.can_write(StateNamespace.SHARED) is False

        # Create admin token
        admin_token = controller.create_token(
            "admin-node",
            {StateNamespace.SYSTEM},
            AccessLevel.ADMIN
        )

        assert admin_token.can_admin(StateNamespace.SYSTEM) is True


# =============================================================================
# Trinity Observability Tests (v106.0) - 10 Gaps
# =============================================================================

class TestTrinityObservability:
    """Tests for Trinity Observability System v4.0."""

    @pytest.mark.asyncio
    async def test_observability_initialization(self):
        """Test observability system initialization."""
        from backend.core.trinity_observability import (
            TrinityObservability,
            ObservabilityConfig
        )

        config = ObservabilityConfig(
            node_id="test-node",
            service_name="test-service"
        )

        observability = await TrinityObservability.create(config, auto_start=True)
        assert observability._running is True

        metrics = observability.get_metrics()
        assert metrics["running"] is True
        assert metrics["node_id"] == "test-node"
        assert metrics["service_name"] == "test-service"

        await observability.stop()
        assert observability._running is False

    @pytest.mark.asyncio
    async def test_distributed_tracing(self):
        """Test W3C Trace Context tracing."""
        from backend.core.trinity_observability import (
            DistributedTracer,
            ObservabilityConfig,
            SpanContext,
            SpanKind
        )

        config = ObservabilityConfig(node_id="test-tracer")
        tracer = DistributedTracer(config)
        await tracer.start()

        # Test span creation
        async with tracer.start_span("test-operation", kind=SpanKind.SERVER) as span:
            assert span is not None
            assert span.name == "test-operation"
            span.set_attribute("test-key", "test-value")
            span.add_event("test-event")

        # Test context propagation
        headers = tracer.inject_context({})
        assert "traceparent" in headers

        await tracer.stop()

    @pytest.mark.asyncio
    async def test_metrics_collection(self):
        """Test Prometheus-compatible metrics."""
        from backend.core.trinity_observability import (
            MetricsCollector,
            ObservabilityConfig
        )

        config = ObservabilityConfig(node_id="test-metrics")
        collector = MetricsCollector(config)
        await collector.start()

        # Test counter
        counter = collector.counter("test_counter", "Test counter", ["label"])
        counter.inc(5.0, label="test")

        # Test gauge
        gauge = collector.gauge("test_gauge", "Test gauge", ["label"])
        gauge.set(42.0, label="test")

        # Test histogram
        histogram = collector.histogram("test_histogram", "Test histogram", ["label"])
        histogram.observe(0.5, label="test")

        # Get samples
        samples = collector.get_all_samples()
        assert len(samples) > 0

        # Test Prometheus format
        prom_output = collector.to_prometheus_format()
        assert "test_counter" in prom_output

        await collector.stop()

    @pytest.mark.asyncio
    async def test_centralized_logging(self):
        """Test centralized structured logging."""
        from backend.core.trinity_observability import (
            CentralizedLogger,
            ObservabilityConfig,
            LogLevel
        )

        config = ObservabilityConfig(node_id="test-logger")
        logger = CentralizedLogger(config)
        await logger.start()

        # Log messages
        await logger.info("Test info message", key="value")
        await logger.warning("Test warning message")
        await logger.error("Test error message")

        # Check buffer
        assert logger._buffer

        await logger.stop()

    @pytest.mark.asyncio
    async def test_error_aggregation(self):
        """Test Sentry-style error aggregation."""
        from backend.core.trinity_observability import (
            ErrorAggregator,
            ObservabilityConfig
        )

        config = ObservabilityConfig(node_id="test-errors")
        aggregator = ErrorAggregator(config)
        await aggregator.start()

        # Capture an exception
        try:
            raise ValueError("Test error")
        except ValueError as e:
            fingerprint_id = await aggregator.capture_exception(e)
            assert fingerprint_id is not None

        # Check error groups
        groups = await aggregator.get_error_groups()
        assert len(groups) > 0
        assert groups[0]["exception_type"] == "ValueError"

        await aggregator.stop()

    @pytest.mark.asyncio
    async def test_health_dashboard(self):
        """Test unified health dashboard."""
        from backend.core.trinity_observability import (
            HealthDashboard,
            ObservabilityConfig,
            HealthStatus
        )

        config = ObservabilityConfig(node_id="test-health")
        dashboard = HealthDashboard(config)
        await dashboard.start()

        # Run health checks
        health = await dashboard.run_checks()
        assert health is not None
        assert health.service == "jarvis"
        assert health.overall_status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.UNHEALTHY]

        # Check default checks are present
        check_names = [c.name for c in health.checks]
        assert "memory" in check_names
        assert "disk" in check_names
        assert "cpu" in check_names

        await dashboard.stop()

    @pytest.mark.asyncio
    async def test_alert_system(self):
        """Test alert system with deduplication."""
        from backend.core.trinity_observability import (
            AlertManager,
            ObservabilityConfig,
            AlertSeverity,
            AlertState
        )

        config = ObservabilityConfig(node_id="test-alerts")
        manager = AlertManager(config)
        await manager.start()

        # Fire an alert
        alert = await manager.fire_alert(
            name="test-alert",
            severity=AlertSeverity.WARNING,
            message="Test alert message"
        )
        assert alert is not None
        assert alert.state == AlertState.FIRING

        # Check active alerts
        active = await manager.get_active_alerts()
        assert len(active) == 1

        # Resolve alert
        resolved = await manager.resolve_alert(alert.alert_id)
        assert resolved is True

        await manager.stop()

    @pytest.mark.asyncio
    async def test_dependency_graph(self):
        """Test dependency graph visualization."""
        from backend.core.trinity_observability import (
            DependencyGraph,
            ObservabilityConfig
        )

        config = ObservabilityConfig(node_id="test-deps")
        graph = DependencyGraph(config)

        # Add dependencies
        await graph.record_service_dependency("service-a", "service-b")
        await graph.record_service_dependency("service-a", "service-c")

        # Check nodes and edges
        metrics = graph.get_metrics()
        assert metrics["nodes"] == 3
        assert metrics["edges"] == 2

        # Test Mermaid export
        mermaid = await graph.to_mermaid()
        assert "graph TD" in mermaid
        assert "service_a" in mermaid

    @pytest.mark.asyncio
    async def test_request_flow_tracking(self):
        """Test request flow visualization."""
        from backend.core.trinity_observability import (
            RequestFlowTracker,
            ObservabilityConfig
        )

        config = ObservabilityConfig(node_id="test-flows")
        tracker = RequestFlowTracker(config)

        # Start a flow
        flow_id = await tracker.start_flow("test-request")
        assert flow_id is not None

        # Add steps
        await tracker.add_step(flow_id, "step-1", service="service-a")
        await tracker.end_step(flow_id)

        await tracker.add_step(flow_id, "step-2", service="service-b")
        await tracker.end_step(flow_id)

        # End flow
        flow = await tracker.end_flow(flow_id)
        assert flow is not None
        assert flow.status == "success"

        # Check completed flows
        completed = await tracker.get_completed_flows()
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_resource_monitoring(self):
        """Test resource usage monitoring."""
        from backend.core.trinity_observability import (
            ResourceMonitor,
            ObservabilityConfig
        )

        config = ObservabilityConfig(node_id="test-resources")
        monitor = ResourceMonitor(config)
        await monitor.start()

        # Collect a snapshot
        snapshot = await monitor.collect_snapshot()
        assert snapshot is not None
        assert snapshot.cpu_percent >= 0
        assert snapshot.memory_percent >= 0
        assert snapshot.disk_percent >= 0

        # Check history
        history = await monitor.get_history()
        assert len(history) > 0

        await monitor.stop()


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
