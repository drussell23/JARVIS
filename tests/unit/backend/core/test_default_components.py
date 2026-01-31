"""
Tests for default_components module.

Tests the predefined component definitions for JARVIS system.
"""
import pytest
from typing import List

from backend.core.component_registry import (
    ComponentDefinition,
    ComponentRegistry,
    Criticality,
    ProcessType,
    HealthCheckType,
    FallbackStrategy,
    Dependency,
)
from backend.core.default_components import (
    CROSS_REPO_COMPONENTS,
    JARVIS_CORE_COMPONENTS,
    register_default_components,
    get_all_default_components,
)


class TestCrossRepoComponents:
    """Tests for CROSS_REPO_COMPONENTS list."""

    def test_cross_repo_components_is_list(self):
        """CROSS_REPO_COMPONENTS should be a list."""
        assert isinstance(CROSS_REPO_COMPONENTS, list)

    def test_cross_repo_components_not_empty(self):
        """CROSS_REPO_COMPONENTS should contain components."""
        assert len(CROSS_REPO_COMPONENTS) > 0

    def test_all_cross_repo_are_component_definitions(self):
        """All items in CROSS_REPO_COMPONENTS should be ComponentDefinition."""
        for component in CROSS_REPO_COMPONENTS:
            assert isinstance(component, ComponentDefinition)

    def test_gcp_prewarm_component(self):
        """gcp-prewarm component should be correctly defined."""
        gcp_prewarm = next(
            (c for c in CROSS_REPO_COMPONENTS if c.name == "gcp-prewarm"), None
        )
        assert gcp_prewarm is not None
        assert gcp_prewarm.criticality == Criticality.OPTIONAL
        assert gcp_prewarm.process_type == ProcessType.EXTERNAL_SERVICE
        assert "gcp-vm-ready" in gcp_prewarm.provides_capabilities
        assert gcp_prewarm.dependencies == []
        assert gcp_prewarm.startup_timeout == 30.0

    def test_jarvis_prime_component(self):
        """jarvis-prime component should be correctly defined."""
        jarvis_prime = next(
            (c for c in CROSS_REPO_COMPONENTS if c.name == "jarvis-prime"), None
        )
        assert jarvis_prime is not None
        assert jarvis_prime.criticality == Criticality.DEGRADED_OK
        assert jarvis_prime.process_type == ProcessType.SUBPROCESS
        assert jarvis_prime.repo_path == "${JARVIS_PRIME_PATH}"
        assert "local-inference" in jarvis_prime.provides_capabilities
        assert "llm" in jarvis_prime.provides_capabilities
        assert "embeddings" in jarvis_prime.provides_capabilities
        assert jarvis_prime.health_check_type == HealthCheckType.HTTP
        assert jarvis_prime.health_endpoint == "http://localhost:${JARVIS_PRIME_PORT}/health"
        assert jarvis_prime.startup_timeout == 120.0
        assert jarvis_prime.fallback_strategy == FallbackStrategy.RETRY_THEN_CONTINUE
        assert jarvis_prime.fallback_for_capabilities == {"inference": "claude-api", "embeddings": "openai-api"}
        assert jarvis_prime.disable_env_var == "JARVIS_PRIME_ENABLED"
        assert jarvis_prime.conservative_skip_priority == 80

    def test_jarvis_prime_dependencies(self):
        """jarvis-prime should have correct dependencies."""
        jarvis_prime = next(
            (c for c in CROSS_REPO_COMPONENTS if c.name == "jarvis-prime"), None
        )
        assert jarvis_prime is not None
        # Should have jarvis-core as hard dependency
        assert "jarvis-core" in jarvis_prime.dependencies
        # Should have gcp-prewarm as soft dependency
        soft_deps = [d for d in jarvis_prime.dependencies if isinstance(d, Dependency)]
        assert any(d.component == "gcp-prewarm" and d.soft is True for d in soft_deps)

    def test_reactor_core_component(self):
        """reactor-core component should be correctly defined."""
        reactor_core = next(
            (c for c in CROSS_REPO_COMPONENTS if c.name == "reactor-core"), None
        )
        assert reactor_core is not None
        assert reactor_core.criticality == Criticality.OPTIONAL
        assert reactor_core.process_type == ProcessType.SUBPROCESS
        assert reactor_core.repo_path == "${REACTOR_CORE_PATH}"
        assert "training" in reactor_core.provides_capabilities
        assert "fine-tuning" in reactor_core.provides_capabilities
        assert "jarvis-core" in reactor_core.dependencies
        assert "jarvis-prime" in reactor_core.dependencies
        assert reactor_core.health_check_type == HealthCheckType.HTTP
        assert reactor_core.health_endpoint == "http://localhost:${REACTOR_PORT}/health"
        assert reactor_core.startup_timeout == 90.0
        assert reactor_core.fallback_strategy == FallbackStrategy.CONTINUE
        assert reactor_core.disable_env_var == "REACTOR_ENABLED"
        assert reactor_core.conservative_skip_priority == 10


class TestJarvisCoreComponents:
    """Tests for JARVIS_CORE_COMPONENTS list."""

    def test_jarvis_core_components_is_list(self):
        """JARVIS_CORE_COMPONENTS should be a list."""
        assert isinstance(JARVIS_CORE_COMPONENTS, list)

    def test_jarvis_core_components_not_empty(self):
        """JARVIS_CORE_COMPONENTS should contain components."""
        assert len(JARVIS_CORE_COMPONENTS) > 0

    def test_all_jarvis_core_are_component_definitions(self):
        """All items in JARVIS_CORE_COMPONENTS should be ComponentDefinition."""
        for component in JARVIS_CORE_COMPONENTS:
            assert isinstance(component, ComponentDefinition)

    def test_jarvis_core_component(self):
        """jarvis-core component should be correctly defined."""
        jarvis_core = next(
            (c for c in JARVIS_CORE_COMPONENTS if c.name == "jarvis-core"), None
        )
        assert jarvis_core is not None
        assert jarvis_core.criticality == Criticality.REQUIRED
        assert jarvis_core.process_type == ProcessType.IN_PROCESS
        assert "core" in jarvis_core.provides_capabilities
        assert "api" in jarvis_core.provides_capabilities
        assert jarvis_core.dependencies == []
        assert jarvis_core.startup_timeout == 30.0

    def test_redis_component(self):
        """redis component should be correctly defined."""
        redis = next(
            (c for c in JARVIS_CORE_COMPONENTS if c.name == "redis"), None
        )
        assert redis is not None
        assert redis.criticality == Criticality.OPTIONAL
        assert redis.process_type == ProcessType.EXTERNAL_SERVICE
        assert "cache" in redis.provides_capabilities
        assert "pubsub" in redis.provides_capabilities
        assert redis.dependencies == []
        assert redis.health_check_type == HealthCheckType.TCP
        assert redis.health_endpoint == "localhost:6379"
        assert redis.startup_timeout == 10.0
        assert redis.fallback_strategy == FallbackStrategy.CONTINUE
        assert redis.disable_env_var == "REDIS_ENABLED"

    def test_cloud_sql_component(self):
        """cloud-sql component should be correctly defined."""
        cloud_sql = next(
            (c for c in JARVIS_CORE_COMPONENTS if c.name == "cloud-sql"), None
        )
        assert cloud_sql is not None
        assert cloud_sql.criticality == Criticality.DEGRADED_OK
        assert cloud_sql.process_type == ProcessType.EXTERNAL_SERVICE
        assert "database" in cloud_sql.provides_capabilities
        assert "persistence" in cloud_sql.provides_capabilities
        assert cloud_sql.dependencies == []
        assert cloud_sql.health_check_type == HealthCheckType.TCP
        assert cloud_sql.startup_timeout == 30.0
        assert cloud_sql.fallback_strategy == FallbackStrategy.RETRY_THEN_CONTINUE
        assert cloud_sql.disable_env_var == "CLOUD_SQL_ENABLED"

    def test_voice_unlock_component(self):
        """voice-unlock component should be correctly defined."""
        voice_unlock = next(
            (c for c in JARVIS_CORE_COMPONENTS if c.name == "voice-unlock"), None
        )
        assert voice_unlock is not None
        assert voice_unlock.criticality == Criticality.DEGRADED_OK
        assert voice_unlock.process_type == ProcessType.IN_PROCESS
        assert "voice-auth" in voice_unlock.provides_capabilities
        assert "biometrics" in voice_unlock.provides_capabilities
        assert "jarvis-core" in voice_unlock.dependencies
        assert voice_unlock.startup_timeout == 45.0
        assert voice_unlock.fallback_strategy == FallbackStrategy.CONTINUE
        assert voice_unlock.disable_env_var == "VOICE_UNLOCK_ENABLED"


class TestRegisterDefaultComponents:
    """Tests for register_default_components function."""

    @pytest.fixture
    def fresh_registry(self):
        """Create a fresh registry for testing."""
        registry = ComponentRegistry()
        return registry

    def test_register_default_components_populates_registry(self, fresh_registry):
        """register_default_components should populate the registry with all components."""
        register_default_components(fresh_registry)

        # All JARVIS_CORE_COMPONENTS should be registered
        for component in JARVIS_CORE_COMPONENTS:
            assert fresh_registry.has(component.name)

        # All CROSS_REPO_COMPONENTS should be registered
        for component in CROSS_REPO_COMPONENTS:
            assert fresh_registry.has(component.name)

    def test_register_default_components_correct_count(self, fresh_registry):
        """Registry should have correct number of components after registration."""
        register_default_components(fresh_registry)
        total_expected = len(JARVIS_CORE_COMPONENTS) + len(CROSS_REPO_COMPONENTS)
        assert len(fresh_registry.all_definitions()) == total_expected

    def test_registered_components_have_capabilities(self, fresh_registry):
        """Registered components should have their capabilities indexed."""
        register_default_components(fresh_registry)

        # Check some key capabilities
        assert fresh_registry.get_provider("core") == "jarvis-core"
        assert fresh_registry.get_provider("cache") == "redis"
        assert fresh_registry.get_provider("database") == "cloud-sql"
        assert fresh_registry.get_provider("local-inference") == "jarvis-prime"

    def test_can_retrieve_registered_definitions(self, fresh_registry):
        """Should be able to retrieve component definitions after registration."""
        register_default_components(fresh_registry)

        jarvis_core = fresh_registry.get("jarvis-core")
        assert jarvis_core.criticality == Criticality.REQUIRED

        redis = fresh_registry.get("redis")
        assert redis.criticality == Criticality.OPTIONAL


class TestGetAllDefaultComponents:
    """Tests for get_all_default_components function."""

    def test_returns_list(self):
        """get_all_default_components should return a list."""
        result = get_all_default_components()
        assert isinstance(result, list)

    def test_returns_all_components(self):
        """get_all_default_components should return all components from both lists."""
        result = get_all_default_components()
        expected_count = len(JARVIS_CORE_COMPONENTS) + len(CROSS_REPO_COMPONENTS)
        assert len(result) == expected_count

    def test_contains_jarvis_core_components(self):
        """Result should contain all JARVIS_CORE_COMPONENTS."""
        result = get_all_default_components()
        for component in JARVIS_CORE_COMPONENTS:
            assert component in result

    def test_contains_cross_repo_components(self):
        """Result should contain all CROSS_REPO_COMPONENTS."""
        result = get_all_default_components()
        for component in CROSS_REPO_COMPONENTS:
            assert component in result

    def test_all_items_are_component_definitions(self):
        """All items in result should be ComponentDefinition instances."""
        result = get_all_default_components()
        for component in result:
            assert isinstance(component, ComponentDefinition)


class TestComponentCriticalities:
    """Tests for correct criticality assignments across all components."""

    def test_jarvis_core_is_required(self):
        """jarvis-core should be REQUIRED as system cannot function without it."""
        jarvis_core = next(
            (c for c in get_all_default_components() if c.name == "jarvis-core"), None
        )
        assert jarvis_core is not None
        assert jarvis_core.criticality == Criticality.REQUIRED

    def test_degraded_ok_components(self):
        """Components that allow degraded operation should have DEGRADED_OK criticality."""
        degraded_ok_names = ["jarvis-prime", "cloud-sql", "voice-unlock"]
        all_components = get_all_default_components()
        for name in degraded_ok_names:
            component = next((c for c in all_components if c.name == name), None)
            assert component is not None, f"Component {name} not found"
            assert component.criticality == Criticality.DEGRADED_OK, \
                f"{name} should be DEGRADED_OK, got {component.criticality}"

    def test_optional_components(self):
        """Optional components should have OPTIONAL criticality."""
        optional_names = ["gcp-prewarm", "redis", "reactor-core"]
        all_components = get_all_default_components()
        for name in optional_names:
            component = next((c for c in all_components if c.name == name), None)
            assert component is not None, f"Component {name} not found"
            assert component.criticality == Criticality.OPTIONAL, \
                f"{name} should be OPTIONAL, got {component.criticality}"


class TestComponentProcessTypes:
    """Tests for correct process type assignments."""

    def test_in_process_components(self):
        """Components that run in the same process should have IN_PROCESS type."""
        in_process_names = ["jarvis-core", "voice-unlock"]
        all_components = get_all_default_components()
        for name in in_process_names:
            component = next((c for c in all_components if c.name == name), None)
            assert component is not None, f"Component {name} not found"
            assert component.process_type == ProcessType.IN_PROCESS, \
                f"{name} should be IN_PROCESS, got {component.process_type}"

    def test_subprocess_components(self):
        """Components that run as subprocesses should have SUBPROCESS type."""
        subprocess_names = ["jarvis-prime", "reactor-core"]
        all_components = get_all_default_components()
        for name in subprocess_names:
            component = next((c for c in all_components if c.name == name), None)
            assert component is not None, f"Component {name} not found"
            assert component.process_type == ProcessType.SUBPROCESS, \
                f"{name} should be SUBPROCESS, got {component.process_type}"

    def test_external_service_components(self):
        """External services should have EXTERNAL_SERVICE type."""
        external_names = ["gcp-prewarm", "redis", "cloud-sql"]
        all_components = get_all_default_components()
        for name in external_names:
            component = next((c for c in all_components if c.name == name), None)
            assert component is not None, f"Component {name} not found"
            assert component.process_type == ProcessType.EXTERNAL_SERVICE, \
                f"{name} should be EXTERNAL_SERVICE, got {component.process_type}"


class TestComponentDependencies:
    """Tests for correct dependency definitions."""

    def test_jarvis_core_no_dependencies(self):
        """jarvis-core should have no dependencies as it's the foundation."""
        jarvis_core = next(
            (c for c in get_all_default_components() if c.name == "jarvis-core"), None
        )
        assert jarvis_core is not None
        assert jarvis_core.dependencies == []

    def test_components_depending_on_jarvis_core(self):
        """Components depending on jarvis-core should have it in dependencies."""
        dependent_names = ["jarvis-prime", "reactor-core", "voice-unlock"]
        all_components = get_all_default_components()
        for name in dependent_names:
            component = next((c for c in all_components if c.name == name), None)
            assert component is not None, f"Component {name} not found"
            # Check jarvis-core is in dependencies (either as string or Dependency)
            has_jarvis_core = any(
                (d == "jarvis-core" if isinstance(d, str) else d.component == "jarvis-core")
                for d in component.dependencies
            )
            assert has_jarvis_core, f"{name} should depend on jarvis-core"

    def test_reactor_core_depends_on_jarvis_prime(self):
        """reactor-core should depend on jarvis-prime."""
        reactor_core = next(
            (c for c in get_all_default_components() if c.name == "reactor-core"), None
        )
        assert reactor_core is not None
        has_jarvis_prime = any(
            (d == "jarvis-prime" if isinstance(d, str) else d.component == "jarvis-prime")
            for d in reactor_core.dependencies
        )
        assert has_jarvis_prime


class TestComponentCapabilities:
    """Tests for correct capability definitions."""

    def test_jarvis_core_provides_core_api(self):
        """jarvis-core should provide core and api capabilities."""
        jarvis_core = next(
            (c for c in get_all_default_components() if c.name == "jarvis-core"), None
        )
        assert jarvis_core is not None
        assert "core" in jarvis_core.provides_capabilities
        assert "api" in jarvis_core.provides_capabilities

    def test_jarvis_prime_provides_inference(self):
        """jarvis-prime should provide inference-related capabilities."""
        jarvis_prime = next(
            (c for c in get_all_default_components() if c.name == "jarvis-prime"), None
        )
        assert jarvis_prime is not None
        assert "local-inference" in jarvis_prime.provides_capabilities
        assert "llm" in jarvis_prime.provides_capabilities
        assert "embeddings" in jarvis_prime.provides_capabilities

    def test_redis_provides_cache_pubsub(self):
        """redis should provide cache and pubsub capabilities."""
        redis = next(
            (c for c in get_all_default_components() if c.name == "redis"), None
        )
        assert redis is not None
        assert "cache" in redis.provides_capabilities
        assert "pubsub" in redis.provides_capabilities

    def test_cloud_sql_provides_database(self):
        """cloud-sql should provide database and persistence capabilities."""
        cloud_sql = next(
            (c for c in get_all_default_components() if c.name == "cloud-sql"), None
        )
        assert cloud_sql is not None
        assert "database" in cloud_sql.provides_capabilities
        assert "persistence" in cloud_sql.provides_capabilities

    def test_voice_unlock_provides_biometrics(self):
        """voice-unlock should provide voice-auth and biometrics capabilities."""
        voice_unlock = next(
            (c for c in get_all_default_components() if c.name == "voice-unlock"), None
        )
        assert voice_unlock is not None
        assert "voice-auth" in voice_unlock.provides_capabilities
        assert "biometrics" in voice_unlock.provides_capabilities

    def test_no_duplicate_component_names(self):
        """All component names should be unique."""
        all_components = get_all_default_components()
        names = [c.name for c in all_components]
        assert len(names) == len(set(names)), "Duplicate component names found"
