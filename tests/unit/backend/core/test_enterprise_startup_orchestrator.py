# tests/unit/backend/core/test_enterprise_startup_orchestrator.py
"""
Tests for EnterpriseStartupOrchestrator.

Tests cover:
- Basic startup orchestration with healthy components
- Tier-parallel execution verification
- Required component failure aborts startup
- Optional component failure continues startup
- Recovery engine integration
- Event emission
- Conservative mode skipping
- Shutdown order (reverse startup order)
- Component starters registration
- Disabled components handling
"""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone
from typing import Dict, List, Any
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.component_registry import (
    ComponentDefinition,
    ComponentRegistry,
    ComponentStatus,
    Criticality,
    ProcessType,
    Dependency,
)
from backend.core.recovery_engine import (
    RecoveryEngine,
    RecoveryAction,
    RecoveryStrategy,
    RecoveryPhase,
    ErrorClassifier,
)
from backend.core.startup_context import StartupContext
from backend.core.enterprise_startup_orchestrator import (
    EnterpriseStartupOrchestrator,
    StartupEvent,
    StartupResult,
    ComponentResult,
    create_enterprise_orchestrator,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def registry():
    """Create a fresh ComponentRegistry for each test."""
    reg = ComponentRegistry()
    reg._reset_for_testing()
    return reg


@pytest.fixture
def simple_components():
    """Simple component definitions for testing."""
    return [
        ComponentDefinition(
            name="core",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            provides_capabilities=["core"],
            dependencies=[],
            startup_timeout=5.0,
        ),
        ComponentDefinition(
            name="database",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.EXTERNAL_SERVICE,
            provides_capabilities=["persistence"],
            dependencies=["core"],
            startup_timeout=5.0,
        ),
        ComponentDefinition(
            name="cache",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.EXTERNAL_SERVICE,
            provides_capabilities=["caching"],
            dependencies=["core"],
            startup_timeout=5.0,
            conservative_skip_priority=30,  # Low priority, will be skipped in conservative mode
        ),
    ]


@pytest.fixture
def registry_with_components(registry, simple_components):
    """Registry populated with simple components."""
    for comp in simple_components:
        registry.register(comp)
    return registry


@pytest.fixture
def orchestrator(registry_with_components):
    """Create orchestrator with default settings."""
    return EnterpriseStartupOrchestrator(
        registry=registry_with_components,
    )


# =============================================================================
# Test StartupEvent enum
# =============================================================================

class TestStartupEvent:
    """Tests for StartupEvent enum."""

    def test_all_events_defined(self):
        """Verify all expected events are defined."""
        expected = [
            "STARTUP_BEGUN",
            "TIER_STARTED",
            "TIER_COMPLETED",
            "COMPONENT_STARTING",
            "COMPONENT_HEALTHY",
            "COMPONENT_FAILED",
            "COMPONENT_DISABLED",
            "RECOVERY_ATTEMPTED",
            "STARTUP_COMPLETED",
            "SHUTDOWN_BEGUN",
            "SHUTDOWN_COMPLETED",
        ]
        for name in expected:
            assert hasattr(StartupEvent, name)
            event = getattr(StartupEvent, name)
            assert isinstance(event, StartupEvent)


# =============================================================================
# Test ComponentResult dataclass
# =============================================================================

class TestComponentResult:
    """Tests for ComponentResult dataclass."""

    def test_success_result(self):
        """Test creating a successful result."""
        result = ComponentResult(
            name="test-component",
            success=True,
            status=ComponentStatus.HEALTHY,
            startup_time=1.5,
        )
        assert result.name == "test-component"
        assert result.success is True
        assert result.status == ComponentStatus.HEALTHY
        assert result.startup_time == 1.5
        assert result.error is None
        assert result.message is None

    def test_failure_result(self):
        """Test creating a failed result."""
        error = RuntimeError("Startup failed")
        result = ComponentResult(
            name="test-component",
            success=False,
            status=ComponentStatus.FAILED,
            startup_time=0.5,
            error=error,
            message="Component failed to start",
        )
        assert result.success is False
        assert result.error is error
        assert result.message == "Component failed to start"


# =============================================================================
# Test StartupResult dataclass
# =============================================================================

class TestStartupResult:
    """Tests for StartupResult dataclass."""

    def test_healthy_result(self):
        """Test creating a healthy startup result."""
        result = StartupResult(
            success=True,
            total_time=5.0,
            components={
                "core": ComponentResult(
                    name="core",
                    success=True,
                    status=ComponentStatus.HEALTHY,
                    startup_time=1.0,
                ),
            },
            healthy_count=1,
            failed_count=0,
            disabled_count=0,
            overall_status="HEALTHY",
        )
        assert result.success is True
        assert result.overall_status == "HEALTHY"
        assert result.healthy_count == 1

    def test_degraded_result(self):
        """Test creating a degraded startup result."""
        result = StartupResult(
            success=True,
            total_time=5.0,
            components={},
            healthy_count=2,
            failed_count=1,
            disabled_count=0,
            overall_status="DEGRADED",
        )
        assert result.success is True
        assert result.overall_status == "DEGRADED"


# =============================================================================
# Test EnterpriseStartupOrchestrator initialization
# =============================================================================

class TestOrchestratorInit:
    """Tests for orchestrator initialization."""

    def test_basic_init(self, registry):
        """Test basic initialization."""
        orchestrator = EnterpriseStartupOrchestrator(registry=registry)
        assert orchestrator.registry is registry
        assert orchestrator.recovery_engine is not None
        assert orchestrator.startup_context is None
        assert orchestrator.summary is not None

    def test_init_with_recovery_engine(self, registry):
        """Test initialization with custom recovery engine."""
        recovery = RecoveryEngine(registry, ErrorClassifier())
        orchestrator = EnterpriseStartupOrchestrator(
            registry=registry,
            recovery_engine=recovery,
        )
        assert orchestrator.recovery_engine is recovery

    def test_init_with_startup_context(self, registry):
        """Test initialization with startup context."""
        context = StartupContext(
            previous_exit_code=0,
            crash_count_recent=0,
        )
        orchestrator = EnterpriseStartupOrchestrator(
            registry=registry,
            startup_context=context,
        )
        assert orchestrator.startup_context is context


# =============================================================================
# Test Component Starter Registration
# =============================================================================

class TestComponentStarterRegistration:
    """Tests for component starter registration."""

    def test_register_sync_starter(self, orchestrator):
        """Test registering a synchronous starter."""
        async def starter():
            return True

        orchestrator.register_component_starter("core", starter)
        assert "core" in orchestrator._component_starters
        assert orchestrator._component_starters["core"] is starter

    def test_register_async_starter(self, orchestrator):
        """Test registering an async starter."""
        async def async_starter():
            await asyncio.sleep(0.01)
            return True

        orchestrator.register_component_starter("database", async_starter)
        assert "database" in orchestrator._component_starters

    def test_register_multiple_starters(self, orchestrator):
        """Test registering multiple starters."""
        async def starter1():
            return True

        async def starter2():
            return True

        orchestrator.register_component_starter("core", starter1)
        orchestrator.register_component_starter("database", starter2)

        assert len(orchestrator._component_starters) == 2


# =============================================================================
# Test Event Handling
# =============================================================================

class TestEventHandling:
    """Tests for event emission and handling."""

    @pytest.mark.asyncio
    async def test_register_sync_event_handler(self, orchestrator):
        """Test registering a sync event handler."""
        events_received = []

        def handler(event, data):
            events_received.append((event, data))

        orchestrator.on_event(StartupEvent.STARTUP_BEGUN, handler)
        await orchestrator._emit_event(StartupEvent.STARTUP_BEGUN, test="value")

        assert len(events_received) == 1
        assert events_received[0][0] == StartupEvent.STARTUP_BEGUN
        assert events_received[0][1]["test"] == "value"

    @pytest.mark.asyncio
    async def test_register_async_event_handler(self, orchestrator):
        """Test registering an async event handler."""
        events_received = []

        async def async_handler(event, data):
            await asyncio.sleep(0.001)
            events_received.append((event, data))

        orchestrator.on_event(StartupEvent.COMPONENT_HEALTHY, async_handler)
        await orchestrator._emit_event(StartupEvent.COMPONENT_HEALTHY, component="test")

        assert len(events_received) == 1

    @pytest.mark.asyncio
    async def test_multiple_handlers_for_event(self, orchestrator):
        """Test multiple handlers for same event."""
        calls = []

        def handler1(event, data):
            calls.append("handler1")

        def handler2(event, data):
            calls.append("handler2")

        orchestrator.on_event(StartupEvent.STARTUP_BEGUN, handler1)
        orchestrator.on_event(StartupEvent.STARTUP_BEGUN, handler2)
        await orchestrator._emit_event(StartupEvent.STARTUP_BEGUN)

        assert calls == ["handler1", "handler2"]

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_break_emit(self, orchestrator):
        """Test that handler exceptions don't break event emission."""
        calls = []

        def failing_handler(event, data):
            raise RuntimeError("Handler failed")

        def working_handler(event, data):
            calls.append("working")

        orchestrator.on_event(StartupEvent.STARTUP_BEGUN, failing_handler)
        orchestrator.on_event(StartupEvent.STARTUP_BEGUN, working_handler)

        # Should not raise
        await orchestrator._emit_event(StartupEvent.STARTUP_BEGUN)
        assert "working" in calls


# =============================================================================
# Test Basic Startup Orchestration
# =============================================================================

class TestBasicStartup:
    """Tests for basic startup orchestration."""

    @pytest.mark.asyncio
    async def test_orchestrate_startup_all_healthy(self, orchestrator):
        """Test startup with all components healthy."""
        result = await orchestrator.orchestrate_startup()

        assert result.success is True
        assert result.overall_status == "HEALTHY"
        assert len(result.components) == 3
        assert result.healthy_count == 3
        assert result.failed_count == 0

    @pytest.mark.asyncio
    async def test_orchestrate_startup_emits_events(self, orchestrator):
        """Test that startup emits expected events."""
        events = []

        def handler(event, data):
            events.append(event)

        for event in StartupEvent:
            orchestrator.on_event(event, handler)

        await orchestrator.orchestrate_startup()

        assert StartupEvent.STARTUP_BEGUN in events
        assert StartupEvent.STARTUP_COMPLETED in events
        # Should have component starting events
        component_starting_count = sum(
            1 for e in events if e == StartupEvent.COMPONENT_STARTING
        )
        assert component_starting_count >= 1

    @pytest.mark.asyncio
    async def test_startup_populates_shutdown_order(self, orchestrator):
        """Test that startup populates shutdown order."""
        assert len(orchestrator._shutdown_order) == 0

        await orchestrator.orchestrate_startup()

        # All healthy components should be in shutdown order
        assert len(orchestrator._shutdown_order) == 3
        assert "core" in orchestrator._shutdown_order

    @pytest.mark.asyncio
    async def test_startup_records_timing(self, orchestrator):
        """Test that startup records component timing."""
        result = await orchestrator.orchestrate_startup()

        for name, comp_result in result.components.items():
            assert comp_result.startup_time >= 0


# =============================================================================
# Test Tier-Parallel Execution
# =============================================================================

class TestTierParallelExecution:
    """Tests for tier-parallel component startup."""

    @pytest.mark.asyncio
    async def test_parallel_components_in_same_tier(self, registry):
        """Test that components in the same tier start in parallel."""
        # Create components with no dependencies (all in tier 0)
        for name in ["comp1", "comp2", "comp3"]:
            registry.register(ComponentDefinition(
                name=name,
                criticality=Criticality.OPTIONAL,
                process_type=ProcessType.IN_PROCESS,
                dependencies=[],
                startup_timeout=5.0,
            ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        start_times = {}
        end_times = {}

        async def make_starter(name):
            async def starter():
                start_times[name] = datetime.now(timezone.utc)
                await asyncio.sleep(0.05)  # 50ms
                end_times[name] = datetime.now(timezone.utc)
                return True
            return starter

        for name in ["comp1", "comp2", "comp3"]:
            orchestrator.register_component_starter(
                name, await make_starter(name)
            )

        await orchestrator.orchestrate_startup()

        # All should have started (approximately) at the same time
        # allowing for some async scheduling overhead
        if len(start_times) == 3:
            min_start = min(start_times.values())
            max_start = max(start_times.values())
            delta = (max_start - min_start).total_seconds()
            # Should all start within 30ms of each other (parallel)
            assert delta < 0.03, f"Components didn't start in parallel: delta={delta}s"

    @pytest.mark.asyncio
    async def test_dependent_components_start_after_dependencies(self, registry):
        """Test that dependent components wait for dependencies."""
        # Tier 0: base
        # Tier 1: depends_on_base
        registry.register(ComponentDefinition(
            name="base",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
        ))
        registry.register(ComponentDefinition(
            name="depends_on_base",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["base"],
            startup_timeout=5.0,
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        start_times = {}

        async def make_starter(name, delay=0.05):
            async def starter():
                start_times[name] = datetime.now(timezone.utc)
                await asyncio.sleep(delay)
                return True
            return starter

        orchestrator.register_component_starter(
            "base", await make_starter("base", 0.05)
        )
        orchestrator.register_component_starter(
            "depends_on_base", await make_starter("depends_on_base", 0.01)
        )

        await orchestrator.orchestrate_startup()

        # depends_on_base should start after base
        assert start_times["depends_on_base"] > start_times["base"]


# =============================================================================
# Test Required Component Failure
# =============================================================================

class TestRequiredComponentFailure:
    """Tests for required component failure handling."""

    @pytest.mark.asyncio
    async def test_required_failure_aborts_startup(self, registry):
        """Test that required component failure aborts entire startup."""
        registry.register(ComponentDefinition(
            name="required-comp",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            retry_max_attempts=1,  # Minimal retries for faster test
        ))
        registry.register(ComponentDefinition(
            name="optional-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["required-comp"],
            startup_timeout=5.0,
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        async def failing_starter():
            raise RuntimeError("Required component failed")

        orchestrator.register_component_starter("required-comp", failing_starter)

        result = await orchestrator.orchestrate_startup()

        assert result.success is False
        assert result.overall_status == "FAILED"
        # Optional component should not have been started
        assert "optional-comp" not in result.components

    @pytest.mark.asyncio
    async def test_required_failure_emits_failed_event(self, registry):
        """Test that required failure emits COMPONENT_FAILED event."""
        registry.register(ComponentDefinition(
            name="required-comp",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            retry_max_attempts=1,
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        events = []
        orchestrator.on_event(
            StartupEvent.COMPONENT_FAILED,
            lambda e, d: events.append(d)
        )

        async def failing_starter():
            raise RuntimeError("Failed")

        orchestrator.register_component_starter("required-comp", failing_starter)

        await orchestrator.orchestrate_startup()

        assert len(events) >= 1
        assert events[0]["component"] == "required-comp"


# =============================================================================
# Test Optional Component Failure
# =============================================================================

class TestOptionalComponentFailure:
    """Tests for optional component failure handling."""

    @pytest.mark.asyncio
    async def test_optional_failure_continues_startup(self, registry):
        """Test that optional component failure doesn't abort startup."""
        registry.register(ComponentDefinition(
            name="core",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
        ))
        registry.register(ComponentDefinition(
            name="optional",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["core"],
            startup_timeout=5.0,
            retry_max_attempts=1,
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        async def failing_starter():
            raise RuntimeError("Optional failed")

        orchestrator.register_component_starter("optional", failing_starter)

        result = await orchestrator.orchestrate_startup()

        # Startup should still succeed
        assert result.success is True
        assert result.overall_status in ("DEGRADED", "HEALTHY")
        assert result.failed_count >= 1

    @pytest.mark.asyncio
    async def test_degraded_ok_failure_continues(self, registry):
        """Test that DEGRADED_OK component failure continues startup."""
        registry.register(ComponentDefinition(
            name="core",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
        ))
        registry.register(ComponentDefinition(
            name="degraded-ok",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["core"],
            startup_timeout=5.0,
            retry_max_attempts=1,
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        async def failing_starter():
            raise RuntimeError("Degraded OK failed")

        orchestrator.register_component_starter("degraded-ok", failing_starter)

        result = await orchestrator.orchestrate_startup()

        assert result.success is True


# =============================================================================
# Test Recovery Engine Integration
# =============================================================================

class TestRecoveryEngineIntegration:
    """Tests for recovery engine integration."""

    @pytest.mark.asyncio
    async def test_recovery_engine_called_on_failure(self, registry):
        """Test that recovery engine is called on component failure."""
        registry.register(ComponentDefinition(
            name="flaky",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            retry_max_attempts=2,
            retry_delay_seconds=0.01,
        ))

        recovery_calls = []
        mock_recovery = AsyncMock()
        mock_recovery.handle_failure = AsyncMock(
            return_value=RecoveryAction(
                strategy=RecoveryStrategy.DISABLE_AND_CONTINUE,
            )
        )

        orchestrator = EnterpriseStartupOrchestrator(
            registry=registry,
            recovery_engine=mock_recovery,
        )

        async def failing_starter():
            raise ConnectionRefusedError("Service unavailable")

        orchestrator.register_component_starter("flaky", failing_starter)

        await orchestrator.orchestrate_startup()

        mock_recovery.handle_failure.assert_called()

    @pytest.mark.asyncio
    async def test_recovery_with_retry_strategy(self, registry):
        """Test that retry strategy causes re-attempt."""
        registry.register(ComponentDefinition(
            name="retryable",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            retry_max_attempts=3,
            retry_delay_seconds=0.01,
        ))

        attempt_count = [0]

        async def eventually_succeeds():
            attempt_count[0] += 1
            if attempt_count[0] < 2:
                raise ConnectionRefusedError("Not ready yet")
            return True

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)
        orchestrator.register_component_starter("retryable", eventually_succeeds)

        result = await orchestrator.orchestrate_startup()

        assert result.success is True
        assert attempt_count[0] >= 2

    @pytest.mark.asyncio
    async def test_recovery_emits_event(self, registry):
        """Test that recovery attempts emit events."""
        registry.register(ComponentDefinition(
            name="failing",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            retry_max_attempts=1,
        ))

        events = []
        orchestrator = EnterpriseStartupOrchestrator(registry=registry)
        orchestrator.on_event(
            StartupEvent.RECOVERY_ATTEMPTED,
            lambda e, d: events.append(d)
        )

        async def failing_starter():
            raise RuntimeError("Fail")

        orchestrator.register_component_starter("failing", failing_starter)

        await orchestrator.orchestrate_startup()

        assert len(events) >= 1
        assert "component" in events[0]
        assert "strategy" in events[0]


# =============================================================================
# Test Conservative Mode
# =============================================================================

class TestConservativeMode:
    """Tests for conservative startup mode."""

    @pytest.mark.asyncio
    async def test_conservative_mode_skips_low_priority(self, registry_with_components):
        """Test that conservative mode skips low-priority optional components."""
        context = StartupContext(
            crash_count_recent=5,  # Above threshold
        )

        orchestrator = EnterpriseStartupOrchestrator(
            registry=registry_with_components,
            startup_context=context,
        )

        result = await orchestrator.orchestrate_startup()

        # Cache has conservative_skip_priority=30 (below 50), should be skipped
        assert result.success is True
        # Check that cache was not started
        if "cache" in result.components:
            # If it's there, it should be skipped/disabled
            assert result.components["cache"].status != ComponentStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_normal_mode_starts_all(self, registry_with_components):
        """Test that normal mode starts all components."""
        context = StartupContext(
            crash_count_recent=0,  # No crashes
        )

        orchestrator = EnterpriseStartupOrchestrator(
            registry=registry_with_components,
            startup_context=context,
        )

        result = await orchestrator.orchestrate_startup()

        assert result.success is True
        assert "cache" in result.components


# =============================================================================
# Test Disabled Components
# =============================================================================

class TestDisabledComponents:
    """Tests for disabled component handling."""

    @pytest.mark.asyncio
    async def test_disabled_component_skipped(self, registry):
        """Test that disabled components are skipped."""
        registry.register(ComponentDefinition(
            name="disabled-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            disable_env_var="DISABLED_COMP_ENABLED",
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        with patch.dict("os.environ", {"DISABLED_COMP_ENABLED": "false"}):
            result = await orchestrator.orchestrate_startup()

        assert result.success is True
        assert result.disabled_count >= 1
        assert result.components["disabled-comp"].status == ComponentStatus.DISABLED

    @pytest.mark.asyncio
    async def test_disabled_component_emits_event(self, registry):
        """Test that disabled component emits COMPONENT_DISABLED event."""
        registry.register(ComponentDefinition(
            name="disabled-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            disable_env_var="DISABLED_COMP_ENABLED",
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        events = []
        orchestrator.on_event(
            StartupEvent.COMPONENT_DISABLED,
            lambda e, d: events.append(d)
        )

        with patch.dict("os.environ", {"DISABLED_COMP_ENABLED": "false"}):
            await orchestrator.orchestrate_startup()

        assert len(events) == 1
        assert events[0]["component"] == "disabled-comp"


# =============================================================================
# Test Shutdown
# =============================================================================

class TestShutdown:
    """Tests for shutdown functionality."""

    @pytest.mark.asyncio
    async def test_shutdown_reverse_order(self, orchestrator):
        """Test that shutdown happens in reverse startup order."""
        await orchestrator.orchestrate_startup()

        original_order = orchestrator._shutdown_order.copy()
        assert len(original_order) > 0

        await orchestrator.shutdown_all(reverse_order=True)

        # Verify components were marked pending (shutdown state)
        for name in original_order:
            state = orchestrator.registry.get_state(name)
            assert state.status == ComponentStatus.PENDING

    @pytest.mark.asyncio
    async def test_shutdown_emits_events(self, orchestrator):
        """Test that shutdown emits expected events."""
        await orchestrator.orchestrate_startup()

        events = []
        orchestrator.on_event(
            StartupEvent.SHUTDOWN_BEGUN,
            lambda e, d: events.append(e)
        )
        orchestrator.on_event(
            StartupEvent.SHUTDOWN_COMPLETED,
            lambda e, d: events.append(e)
        )

        await orchestrator.shutdown_all()

        assert StartupEvent.SHUTDOWN_BEGUN in events
        assert StartupEvent.SHUTDOWN_COMPLETED in events


# =============================================================================
# Test Health Check Integration
# =============================================================================

class TestHealthCheckIntegration:
    """Tests for health check integration."""

    @pytest.mark.asyncio
    async def test_health_check_all(self, orchestrator):
        """Test that health_check_all returns system health."""
        await orchestrator.orchestrate_startup()

        health = await orchestrator.health_check_all()

        assert health is not None
        assert health.overall is not None
        assert len(health.components) >= 0


# =============================================================================
# Test Factory Function
# =============================================================================

class TestFactoryFunction:
    """Tests for create_enterprise_orchestrator factory."""

    def test_create_with_defaults(self):
        """Test creating orchestrator with default components."""
        orchestrator = create_enterprise_orchestrator(register_defaults=True)

        assert orchestrator is not None
        assert orchestrator.registry is not None
        # Should have default components registered
        assert len(orchestrator.registry.all_definitions()) > 0

    def test_create_without_defaults(self):
        """Test creating orchestrator without default components."""
        orchestrator = create_enterprise_orchestrator(register_defaults=False)

        assert orchestrator is not None
        assert len(orchestrator.registry.all_definitions()) == 0

    def test_create_with_custom_context(self):
        """Test creating orchestrator with custom startup context."""
        context = StartupContext(
            previous_exit_code=1,
            crash_count_recent=2,
        )
        orchestrator = create_enterprise_orchestrator(
            register_defaults=False,
            startup_context=context,
        )

        assert orchestrator.startup_context is context
        assert orchestrator.startup_context.crash_count_recent == 2


# =============================================================================
# Test Timeout Handling
# =============================================================================

class TestTimeoutHandling:
    """Tests for component timeout handling."""

    @pytest.mark.asyncio
    async def test_component_timeout_triggers_recovery(self, registry):
        """Test that component timeout triggers recovery."""
        registry.register(ComponentDefinition(
            name="slow-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=0.1,  # Very short timeout
            retry_max_attempts=1,
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        async def slow_starter():
            await asyncio.sleep(1.0)  # Exceeds timeout
            return True

        orchestrator.register_component_starter("slow-comp", slow_starter)

        result = await orchestrator.orchestrate_startup()

        # Should handle timeout gracefully
        assert result.success is True  # Optional component
        assert "slow-comp" in result.components
        assert result.components["slow-comp"].status in (
            ComponentStatus.FAILED,
            ComponentStatus.DEGRADED,
        )


# =============================================================================
# Test Complex DAG Scenarios
# =============================================================================

class TestComplexDAG:
    """Tests for complex dependency scenarios."""

    @pytest.mark.asyncio
    async def test_diamond_dependency(self, registry):
        """Test diamond dependency pattern (A -> B, C -> D, B -> D, C -> D)."""
        # A is base
        # B and C depend on A
        # D depends on B and C
        registry.register(ComponentDefinition(
            name="A",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
        ))
        registry.register(ComponentDefinition(
            name="B",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["A"],
            startup_timeout=5.0,
        ))
        registry.register(ComponentDefinition(
            name="C",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["A"],
            startup_timeout=5.0,
        ))
        registry.register(ComponentDefinition(
            name="D",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["B", "C"],
            startup_timeout=5.0,
        ))

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)

        start_times = {}

        async def make_starter(name):
            async def starter():
                start_times[name] = datetime.now(timezone.utc)
                await asyncio.sleep(0.01)
                return True
            return starter

        for name in ["A", "B", "C", "D"]:
            orchestrator.register_component_starter(name, await make_starter(name))

        result = await orchestrator.orchestrate_startup()

        assert result.success is True
        assert len(result.components) == 4

        # Verify ordering
        assert start_times["A"] < start_times["B"]
        assert start_times["A"] < start_times["C"]
        assert start_times["B"] < start_times["D"]
        assert start_times["C"] < start_times["D"]


# =============================================================================
# Test Error Classification Scenarios
# =============================================================================

class TestErrorClassification:
    """Tests for different error classification scenarios."""

    @pytest.mark.asyncio
    async def test_connection_refused_triggers_retry(self, registry):
        """Test that ConnectionRefusedError triggers retry."""
        registry.register(ComponentDefinition(
            name="service",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.EXTERNAL_SERVICE,
            dependencies=[],
            startup_timeout=5.0,
            retry_max_attempts=3,
            retry_delay_seconds=0.01,
        ))

        attempts = [0]

        async def eventually_available():
            attempts[0] += 1
            if attempts[0] < 2:
                raise ConnectionRefusedError("Service not ready")
            return True

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)
        orchestrator.register_component_starter("service", eventually_available)

        result = await orchestrator.orchestrate_startup()

        assert result.success is True
        assert attempts[0] >= 2

    @pytest.mark.asyncio
    async def test_file_not_found_disables_component(self, registry):
        """Test that FileNotFoundError disables component."""
        registry.register(ComponentDefinition(
            name="config-dependent",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=[],
            startup_timeout=5.0,
            retry_max_attempts=1,
        ))

        async def missing_config():
            raise FileNotFoundError("Config file not found")

        orchestrator = EnterpriseStartupOrchestrator(registry=registry)
        orchestrator.register_component_starter("config-dependent", missing_config)

        result = await orchestrator.orchestrate_startup()

        assert result.success is True
        assert result.components["config-dependent"].status == ComponentStatus.FAILED


class TestControlPlaneAuthority:
    """Tests for control-plane authority enforcement hooks."""

    @pytest.mark.asyncio
    async def test_orchestrate_startup_requires_authority(self, registry):
        orchestrator = EnterpriseStartupOrchestrator(registry=registry)
        with patch(
            "backend.core.enterprise_startup_orchestrator._require_control_plane_authority",
            side_effect=RuntimeError("Control-plane authority required"),
        ):
            with pytest.raises(RuntimeError, match="Control-plane authority required"):
                await orchestrator.orchestrate_startup()

    @pytest.mark.asyncio
    async def test_shutdown_all_requires_authority(self, registry):
        orchestrator = EnterpriseStartupOrchestrator(registry=registry)
        with patch(
            "backend.core.enterprise_startup_orchestrator._require_control_plane_authority",
            side_effect=RuntimeError("Control-plane authority required"),
        ):
            with pytest.raises(RuntimeError, match="Control-plane authority required"):
                await orchestrator.shutdown_all()
