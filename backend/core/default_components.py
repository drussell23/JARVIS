"""
Default component definitions for JARVIS system.

This module provides predefined ComponentDefinition instances for:
- JARVIS_CORE_COMPONENTS: Core system components (jarvis-core, redis, cloud-sql, voice-unlock)
- CROSS_REPO_COMPONENTS: Cross-repository components (gcp-prewarm, jarvis-prime, reactor-core)

These definitions serve as the canonical source for component configuration,
including criticality levels, dependencies, capabilities, and fallback strategies.
"""
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


# Cross-repository components that interact with other JARVIS repos
CROSS_REPO_COMPONENTS: List[ComponentDefinition] = [
    ComponentDefinition(
        name="gcp-prewarm",
        criticality=Criticality.OPTIONAL,
        process_type=ProcessType.EXTERNAL_SERVICE,
        provides_capabilities=["gcp-vm-ready"],
        dependencies=[],
        startup_timeout=30.0,
    ),
    ComponentDefinition(
        name="jarvis-prime",
        criticality=Criticality.DEGRADED_OK,
        process_type=ProcessType.SUBPROCESS,
        repo_path="${JARVIS_PRIME_PATH}",
        provides_capabilities=["local-inference", "llm", "embeddings"],
        dependencies=[
            "jarvis-core",
            Dependency("gcp-prewarm", soft=True),
        ],
        health_check_type=HealthCheckType.HTTP,
        health_endpoint="http://localhost:${JARVIS_PRIME_PORT}/health",
        startup_timeout=120.0,
        fallback_strategy=FallbackStrategy.RETRY_THEN_CONTINUE,
        fallback_for_capabilities={"inference": "claude-api", "embeddings": "openai-api"},
        disable_env_var="JARVIS_PRIME_ENABLED",
        conservative_skip_priority=80,
    ),
    ComponentDefinition(
        name="reactor-core",
        criticality=Criticality.OPTIONAL,
        process_type=ProcessType.SUBPROCESS,
        repo_path="${REACTOR_CORE_PATH}",
        provides_capabilities=["training", "fine-tuning"],
        dependencies=["jarvis-core", "jarvis-prime"],
        health_check_type=HealthCheckType.HTTP,
        health_endpoint="http://localhost:${REACTOR_PORT}/health",
        startup_timeout=90.0,
        fallback_strategy=FallbackStrategy.CONTINUE,
        disable_env_var="REACTOR_ENABLED",
        conservative_skip_priority=10,
    ),
]


# Core JARVIS components that form the foundation of the system
JARVIS_CORE_COMPONENTS: List[ComponentDefinition] = [
    ComponentDefinition(
        name="jarvis-core",
        criticality=Criticality.REQUIRED,
        process_type=ProcessType.IN_PROCESS,
        provides_capabilities=["core", "api"],
        dependencies=[],
        startup_timeout=30.0,
    ),
    ComponentDefinition(
        name="redis",
        criticality=Criticality.OPTIONAL,
        process_type=ProcessType.EXTERNAL_SERVICE,
        provides_capabilities=["cache", "pubsub"],
        dependencies=[],
        health_check_type=HealthCheckType.TCP,
        health_endpoint="localhost:6379",
        startup_timeout=10.0,
        fallback_strategy=FallbackStrategy.CONTINUE,
        disable_env_var="REDIS_ENABLED",
    ),
    ComponentDefinition(
        name="cloud-sql",
        criticality=Criticality.DEGRADED_OK,
        process_type=ProcessType.EXTERNAL_SERVICE,
        provides_capabilities=["database", "persistence"],
        dependencies=[],
        health_check_type=HealthCheckType.TCP,
        startup_timeout=30.0,
        fallback_strategy=FallbackStrategy.RETRY_THEN_CONTINUE,
        disable_env_var="CLOUD_SQL_ENABLED",
    ),
    ComponentDefinition(
        name="voice-unlock",
        criticality=Criticality.DEGRADED_OK,
        process_type=ProcessType.IN_PROCESS,
        provides_capabilities=["voice-auth", "biometrics"],
        dependencies=["jarvis-core"],
        startup_timeout=45.0,
        fallback_strategy=FallbackStrategy.CONTINUE,
        disable_env_var="VOICE_UNLOCK_ENABLED",
    ),
]


def register_default_components(registry: ComponentRegistry) -> None:
    """
    Register all default components with the registry.

    This function registers both JARVIS_CORE_COMPONENTS and CROSS_REPO_COMPONENTS
    with the provided ComponentRegistry instance. Components are registered in order,
    with core components first followed by cross-repo components.

    Args:
        registry: The ComponentRegistry instance to populate with component definitions.
    """
    for component in JARVIS_CORE_COMPONENTS:
        registry.register(component)
    for component in CROSS_REPO_COMPONENTS:
        registry.register(component)


def get_all_default_components() -> List[ComponentDefinition]:
    """
    Get all default component definitions.

    Returns a combined list of all JARVIS_CORE_COMPONENTS and CROSS_REPO_COMPONENTS.
    This is useful for iteration, reporting, or bulk operations on all components.

    Returns:
        List of all ComponentDefinition instances from both component lists.
    """
    return JARVIS_CORE_COMPONENTS + CROSS_REPO_COMPONENTS
