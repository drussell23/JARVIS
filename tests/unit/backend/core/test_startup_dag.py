"""Tests for StartupDAG - dependency-ordered startup execution."""
import pytest


class TestStartupDAG:
    def test_build_simple_dag(self):
        from backend.core.startup_dag import StartupDAG
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        # A depends on nothing, B depends on A
        registry.register(ComponentDefinition(
            name="comp-a",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
        ))
        registry.register(ComponentDefinition(
            name="comp-b",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-a"],
        ))

        dag = StartupDAG(registry)
        tiers = dag.build()

        # comp-a should be in tier 0, comp-b in tier 1
        assert len(tiers) == 2
        assert "comp-a" in tiers[0]
        assert "comp-b" in tiers[1]

    def test_parallel_components_same_tier(self):
        from backend.core.startup_dag import StartupDAG
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        # A, B, C all independent
        for name in ["comp-a", "comp-b", "comp-c"]:
            registry.register(ComponentDefinition(
                name=name,
                criticality=Criticality.OPTIONAL,
                process_type=ProcessType.IN_PROCESS,
            ))

        dag = StartupDAG(registry)
        tiers = dag.build()

        # All should be in tier 0
        assert len(tiers) == 1
        assert len(tiers[0]) == 3

    def test_cycle_detection(self):
        from backend.core.startup_dag import StartupDAG, CycleDetectedError
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        # A -> B -> C -> A (cycle)
        registry.register(ComponentDefinition(
            name="comp-a",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-c"],
        ))
        registry.register(ComponentDefinition(
            name="comp-b",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-a"],
        ))
        registry.register(ComponentDefinition(
            name="comp-c",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-b"],
        ))

        dag = StartupDAG(registry)
        with pytest.raises(CycleDetectedError) as exc_info:
            dag.build()

        assert "cycle" in str(exc_info.value).lower()

    def test_soft_dependency_handling(self):
        from backend.core.startup_dag import StartupDAG
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType,
            Dependency, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        registry.register(ComponentDefinition(
            name="gcp-prewarm",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.EXTERNAL_SERVICE,
        ))
        registry.register(ComponentDefinition(
            name="jarvis-prime",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.SUBPROCESS,
            dependencies=[
                Dependency("gcp-prewarm", soft=True),
            ],
        ))

        dag = StartupDAG(registry)
        tiers = dag.build()

        # gcp-prewarm tier 0, jarvis-prime tier 1 (still ordered despite soft)
        assert "gcp-prewarm" in tiers[0]
        assert "jarvis-prime" in tiers[1]
