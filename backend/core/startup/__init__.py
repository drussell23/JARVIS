"""
JARVIS Startup System v149.2
============================

Enterprise-grade startup orchestration with:
- Explicit component classification (required/optional/degradable)
- Centralized lifecycle registry
- Dependency DAG for deterministic ordering
- Startup summary generation

Quick Start:
    from backend.core.startup import (
        ComponentType,
        get_registry,
        get_startup_dag,
    )
    
    # Register and track components
    registry = get_registry()
    registry.register("my_component", ComponentType.OPTIONAL)
    
    # Check dependencies
    dag = get_startup_dag()
    if dag.are_dependencies_ready("my_component"):
        # Initialize...
        registry.mark_ready("my_component")
"""

from .component_contract import (
    ComponentType,
    ComponentState,
    ComponentStatus,
    get_failure_level,
    get_success_level,
    get_component_type,
    is_failure_acceptable,
    should_retry_on_failure,
)

from .component_registry import (
    ComponentRegistry,
    get_registry,
)

from .startup_dag import (
    StartupPhase,
    StartupDAG,
    get_startup_dag,
)


__all__ = [
    # Component Contract
    "ComponentType",
    "ComponentState",
    "ComponentStatus",
    "get_failure_level",
    "get_success_level",
    "get_component_type",
    "is_failure_acceptable",
    "should_retry_on_failure",
    # Component Registry
    "ComponentRegistry",
    "get_registry",
    # Startup DAG
    "StartupPhase",
    "StartupDAG",
    "get_startup_dag",
]
