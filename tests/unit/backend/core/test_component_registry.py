"""Tests for ComponentRegistry - the single source of truth for components."""
import pytest
import os


class TestCriticality:
    def test_criticality_values(self):
        from backend.core.component_registry import Criticality
        assert Criticality.REQUIRED.value == "required"
        assert Criticality.DEGRADED_OK.value == "degraded_ok"
        assert Criticality.OPTIONAL.value == "optional"


class TestProcessType:
    def test_process_type_values(self):
        from backend.core.component_registry import ProcessType
        assert ProcessType.IN_PROCESS.value == "in_process"
        assert ProcessType.SUBPROCESS.value == "subprocess"
        assert ProcessType.EXTERNAL_SERVICE.value == "external"


class TestComponentStatus:
    def test_status_values(self):
        from backend.core.component_registry import ComponentStatus
        assert ComponentStatus.PENDING.value == "pending"
        assert ComponentStatus.STARTING.value == "starting"
        assert ComponentStatus.HEALTHY.value == "healthy"
        assert ComponentStatus.DEGRADED.value == "degraded"
        assert ComponentStatus.FAILED.value == "failed"
        assert ComponentStatus.DISABLED.value == "disabled"


class TestDependency:
    def test_hard_dependency(self):
        from backend.core.component_registry import Dependency
        dep = Dependency(component="jarvis-core")
        assert dep.component == "jarvis-core"
        assert dep.soft == False

    def test_soft_dependency(self):
        from backend.core.component_registry import Dependency
        dep = Dependency(component="gcp-prewarm", soft=True)
        assert dep.soft == True


class TestComponentDefinition:
    def test_minimal_definition(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        assert defn.name == "test-component"
        assert defn.criticality == Criticality.OPTIONAL
        assert defn.dependencies == []
        assert defn.startup_timeout == 60.0

    def test_full_definition(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType,
            HealthCheckType, FallbackStrategy, Dependency
        )
        defn = ComponentDefinition(
            name="jarvis-prime",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.SUBPROCESS,
            dependencies=[
                "jarvis-core",
                Dependency("gcp-prewarm", soft=True),
            ],
            provides_capabilities=["local-inference", "llm"],
            health_check_type=HealthCheckType.HTTP,
            health_endpoint="http://localhost:8000/health",
            startup_timeout=120.0,
            retry_max_attempts=3,
            fallback_for_capabilities={"inference": "claude-api"},
            disable_env_var="JARVIS_PRIME_ENABLED",
        )
        assert defn.name == "jarvis-prime"
        assert len(defn.dependencies) == 2
        assert defn.provides_capabilities == ["local-inference", "llm"]

    def test_effective_criticality_no_override(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        assert defn.effective_criticality == Criticality.OPTIONAL

    def test_effective_criticality_with_env_override(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            criticality_override_env="TEST_REQUIRED",
        )
        os.environ["TEST_REQUIRED"] = "true"
        try:
            assert defn.effective_criticality == Criticality.REQUIRED
        finally:
            del os.environ["TEST_REQUIRED"]

    def test_is_disabled_by_env_not_set(self):
        """Component is enabled by default when env var not set."""
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            disable_env_var="TEST_ENABLED",
        )
        # Ensure env var is not set
        os.environ.pop("TEST_ENABLED", None)
        assert defn.is_disabled_by_env() == False

    def test_is_disabled_by_env_false(self):
        """Component is disabled when env var is 'false'."""
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            disable_env_var="TEST_ENABLED",
        )
        os.environ["TEST_ENABLED"] = "false"
        try:
            assert defn.is_disabled_by_env() == True
        finally:
            del os.environ["TEST_ENABLED"]

    def test_is_disabled_by_env_true(self):
        """Component is enabled when env var is 'true'."""
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            disable_env_var="TEST_ENABLED",
        )
        os.environ["TEST_ENABLED"] = "true"
        try:
            assert defn.is_disabled_by_env() == False
        finally:
            del os.environ["TEST_ENABLED"]

    def test_is_disabled_by_env_no_var_configured(self):
        """Component is enabled when no disable_env_var configured."""
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        assert defn.is_disabled_by_env() == False


class TestComponentState:
    """Tests for ComponentState runtime tracking."""

    def test_initial_state(self):
        """New state starts as PENDING with zero attempts."""
        from backend.core.component_registry import (
            ComponentDefinition, ComponentState, ComponentStatus,
            Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = ComponentState(definition=defn)
        assert state.status == ComponentStatus.PENDING
        assert state.attempt_count == 0
        assert state.started_at is None

    def test_mark_starting(self):
        """mark_starting sets status and increments attempt count."""
        from backend.core.component_registry import (
            ComponentDefinition, ComponentState, ComponentStatus,
            Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = ComponentState(definition=defn)
        state.mark_starting()
        assert state.status == ComponentStatus.STARTING
        assert state.attempt_count == 1
        assert state.started_at is not None
        # Call again to verify increment
        state.mark_starting()
        assert state.attempt_count == 2

    def test_mark_healthy(self):
        """mark_healthy sets status and clears failure reason."""
        from backend.core.component_registry import (
            ComponentDefinition, ComponentState, ComponentStatus,
            Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = ComponentState(definition=defn)
        state.failure_reason = "previous error"
        state.mark_healthy()
        assert state.status == ComponentStatus.HEALTHY
        assert state.healthy_at is not None
        assert state.failure_reason is None

    def test_mark_failed(self):
        """mark_failed sets status and records reason."""
        from backend.core.component_registry import (
            ComponentDefinition, ComponentState, ComponentStatus,
            Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = ComponentState(definition=defn)
        state.mark_failed("Connection timeout")
        assert state.status == ComponentStatus.FAILED
        assert state.failed_at is not None
        assert state.failure_reason == "Connection timeout"

    def test_mark_degraded(self):
        """mark_degraded sets status and records reason."""
        from backend.core.component_registry import (
            ComponentDefinition, ComponentState, ComponentStatus,
            Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = ComponentState(definition=defn)
        state.mark_degraded("Running with fallback")
        assert state.status == ComponentStatus.DEGRADED
        assert state.failure_reason == "Running with fallback"

    def test_mark_disabled(self):
        """mark_disabled sets status and records reason."""
        from backend.core.component_registry import (
            ComponentDefinition, ComponentState, ComponentStatus,
            Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = ComponentState(definition=defn)
        state.mark_disabled("Disabled via env var")
        assert state.status == ComponentStatus.DISABLED
        assert state.failure_reason == "Disabled via env var"


class TestComponentRegistry:
    """Tests for ComponentRegistry - singleton registry for all components."""

    def test_singleton_pattern(self):
        from backend.core.component_registry import get_component_registry
        reg1 = get_component_registry()
        reg2 = get_component_registry()
        assert reg1 is reg2

    def test_register_component(self):
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType, ComponentStatus
        )
        registry = get_component_registry()
        registry._reset_for_testing()

        defn = ComponentDefinition(
            name="test-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        registry.register(defn)

        assert registry.has("test-comp")
        state = registry.get_state("test-comp")
        assert state.status == ComponentStatus.PENDING

    def test_get_by_capability(self):
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType, ComponentStatus
        )
        registry = get_component_registry()
        registry._reset_for_testing()

        defn = ComponentDefinition(
            name="inference-provider",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.SUBPROCESS,
            provides_capabilities=["inference", "llm"],
        )
        registry.register(defn)

        # Mark it healthy so has_capability returns True
        registry.mark_status("inference-provider", ComponentStatus.HEALTHY)

        assert registry.has_capability("inference")
        assert registry.get_provider("inference") == "inference-provider"
        assert not registry.has_capability("vision")

    def test_all_definitions(self):
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )
        registry = get_component_registry()
        registry._reset_for_testing()

        registry.register(ComponentDefinition(
            name="comp1", criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS
        ))
        registry.register(ComponentDefinition(
            name="comp2", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS
        ))

        all_defs = registry.all_definitions()
        assert len(all_defs) == 2
