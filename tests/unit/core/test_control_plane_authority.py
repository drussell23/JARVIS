"""Tests for v270.2 control-plane authority hierarchy.

Validates that:
1. Dead orchestrators are marked as deprecated
2. Active orchestrators have the expected authority functions
3. startup_state_machine is the canonical component status source
4. supervisor_gcp_controller singleton can be reset for in-process restarts
5. backend/main.py health endpoints use startup_state_machine (not broken imports)
"""

import importlib
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_module(dotted_name: str):
    """Import a module by dotted name, returning None on failure."""
    try:
        return importlib.import_module(dotted_name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. Deprecated orchestrators have deprecation notice
# ---------------------------------------------------------------------------

class TestDeprecatedOrchestrators:
    """Verify dead orchestrators carry deprecation markers."""

    def test_intelligent_startup_orchestrator_deprecated(self):
        mod = _import_module("backend.core.intelligent_startup_orchestrator")
        assert mod is not None, "Module should be importable (even if deprecated)"
        docstring = mod.__doc__ or ""
        assert "DEPRECATED" in docstring

    def test_trinity_startup_orchestrator_deprecated(self):
        mod = _import_module("backend.core.trinity_startup_orchestrator")
        assert mod is not None, "Module should be importable (even if deprecated)"
        docstring = mod.__doc__ or ""
        assert "DEPRECATED" in docstring

    def test_deprecated_modules_mention_canonical_authority(self):
        """Deprecation notices should point to the canonical authority chain."""
        for modname in (
            "backend.core.intelligent_startup_orchestrator",
            "backend.core.trinity_startup_orchestrator",
        ):
            mod = _import_module(modname)
            if mod is None:
                continue
            doc = mod.__doc__ or ""
            assert "startup_state_machine" in doc or "cross_repo_startup_orchestrator" in doc, (
                f"{modname} deprecation notice should reference canonical authority"
            )


# ---------------------------------------------------------------------------
# 2. Active orchestrators export expected symbols
# ---------------------------------------------------------------------------

class TestActiveOrchestrators:
    """Verify active orchestrators have the required API surface."""

    def test_startup_state_machine_exports(self):
        mod = _import_module("backend.core.startup_state_machine")
        assert mod is not None
        # Core classes
        assert hasattr(mod, "StartupStateMachine")
        assert hasattr(mod, "StartupPhase")
        assert hasattr(mod, "ComponentStatus")
        assert hasattr(mod, "ComponentInfo")
        assert hasattr(mod, "CyclicDependencyError")
        # Singleton access
        assert callable(getattr(mod, "get_startup_state_machine", None))
        assert callable(getattr(mod, "get_startup_state_machine_sync", None))
        # In-process restart support
        assert callable(getattr(mod, "reset_startup_state_machine", None))

    def test_supervisor_gcp_controller_exports(self):
        mod = _import_module("backend.core.supervisor_gcp_controller")
        assert mod is not None
        assert hasattr(mod, "SupervisorAwareGCPController")
        assert callable(getattr(mod, "get_supervisor_gcp_controller", None))
        # v270.2: Must have reset support
        assert callable(getattr(mod, "reset_supervisor_gcp_controller", None))

    def test_infrastructure_orchestrator_exists(self):
        """InfrastructureOrchestrator should be importable."""
        mod = _import_module("backend.core.infrastructure_orchestrator")
        assert mod is not None
        assert hasattr(mod, "InfrastructureOrchestrator")


# ---------------------------------------------------------------------------
# 3. startup_state_machine is the canonical status source
# ---------------------------------------------------------------------------

class TestStartupStateMachineAsCanonical:
    """Verify startup_state_machine provides the canonical component status API."""

    def test_standard_components_registered(self):
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        # The DAG should have the standard JARVIS startup phases
        expected = {
            "clean_slate", "loading_experience", "preflight",
            "resources", "backend",
        }
        assert expected.issubset(set(sm.components.keys())), (
            f"Missing critical phases: {expected - set(sm.components.keys())}"
        )

    def test_compute_waves_produces_valid_order(self):
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        waves = sm.compute_waves()
        assert len(waves) > 0, "Must produce at least one wave"
        # First wave should contain 'clean_slate' (no dependencies)
        assert "clean_slate" in waves[0]

    def test_update_component_sync_tracking(self):
        from backend.core.startup_state_machine import (
            StartupStateMachine, ComponentStatus,
        )
        sm = StartupStateMachine()
        sm.update_component_sync("backend", "loading")
        assert sm.components["backend"].status == ComponentStatus.LOADING
        sm.update_component_sync("backend", "ready")
        assert sm.components["backend"].status == ComponentStatus.READY

    def test_get_component_summary_shape(self):
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        summary = sm.get_component_summary()
        assert "phase" in summary
        assert "components" in summary
        assert isinstance(summary["components"], dict)


# ---------------------------------------------------------------------------
# 4. supervisor_gcp_controller singleton reset
# ---------------------------------------------------------------------------

class TestGCPControllerSingletonReset:
    """Verify the singleton reset prevents stale state across restarts."""

    def test_reset_clears_singleton(self):
        from backend.core.supervisor_gcp_controller import (
            get_supervisor_gcp_controller,
            reset_supervisor_gcp_controller,
        )
        # Create the singleton
        ctrl1 = get_supervisor_gcp_controller()
        assert ctrl1 is not None
        # Mutate some state
        ctrl1._vms_created_today = 99
        ctrl1._spend_today = 42.0

        # Reset
        reset_supervisor_gcp_controller()

        # New singleton should be fresh
        ctrl2 = get_supervisor_gcp_controller()
        assert ctrl2 is not ctrl1, "Reset must create a new instance"
        assert ctrl2._vms_created_today == 0
        assert ctrl2._spend_today == 0.0

        # Clean up
        reset_supervisor_gcp_controller()

    def test_reset_clears_stall_tracking(self):
        from backend.core.supervisor_gcp_controller import (
            get_supervisor_gcp_controller,
            reset_supervisor_gcp_controller,
        )
        ctrl = get_supervisor_gcp_controller()
        ctrl.record_stall()
        ctrl.mark_gcp_unavailable("test")
        assert ctrl._stall_count == 1
        assert ctrl._gcp_marked_unavailable is True

        reset_supervisor_gcp_controller()

        fresh = get_supervisor_gcp_controller()
        assert fresh._stall_count == 0
        assert fresh._gcp_marked_unavailable is False

        # Clean up
        reset_supervisor_gcp_controller()


# ---------------------------------------------------------------------------
# 5. No broken imports referencing nonexistent functions
# ---------------------------------------------------------------------------

class TestNoBrokenOrchestratorImports:
    """Verify there are no imports of functions that don't exist."""

    def test_get_health_monitor_not_in_advanced_startup_orchestrator(self):
        """The old get_health_monitor import was always broken — verify it's gone."""
        mod = _import_module("backend.core.advanced_startup_orchestrator")
        if mod is None:
            pytest.skip("advanced_startup_orchestrator not importable")
        assert not hasattr(mod, "get_health_monitor"), (
            "get_health_monitor should not exist in advanced_startup_orchestrator"
        )

    def test_startup_state_machine_sync_accessor_exists(self):
        """backend/main.py now uses get_startup_state_machine_sync — verify it works."""
        from backend.core.startup_state_machine import get_startup_state_machine_sync
        # May return None if not initialized, but must not raise
        result = get_startup_state_machine_sync()
        # Not initialized in test context → None is acceptable
        assert result is None or hasattr(result, "get_component_summary")


# ---------------------------------------------------------------------------
# 6. Authority hierarchy consistency
# ---------------------------------------------------------------------------

class TestAuthorityHierarchy:
    """Cross-check that the authority chain is self-consistent."""

    def test_startup_state_machine_has_all_supervisor_phases(self):
        """The DAG should track all phases the supervisor expects."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        # These are the phases the supervisor feeds via _update_component_status()
        supervisor_phases = {
            "backend", "intelligence", "trinity",
            "enterprise_services", "ghost_display", "agi_os",
            "visual_pipeline", "frontend",
        }
        registered = set(sm.components.keys())
        missing = supervisor_phases - registered
        assert not missing, (
            f"StartupStateMachine missing phases the supervisor expects: {missing}"
        )

    def test_dag_is_acyclic(self):
        """The standard component graph must not have cycles."""
        from backend.core.startup_state_machine import (
            StartupStateMachine, CyclicDependencyError,
        )
        sm = StartupStateMachine()
        try:
            waves = sm.compute_waves()
            assert len(waves) > 0
        except CyclicDependencyError:
            pytest.fail("Standard component DAG has a cycle — this is a critical bug")

    def test_critical_components_are_in_early_waves(self):
        """Critical components (clean_slate, preflight, backend) must be in early waves."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        waves = sm.compute_waves()
        # Flatten first 3 waves
        early = set()
        for wave in waves[:5]:
            early.update(wave)
        for name in ("clean_slate", "preflight", "resources", "backend"):
            assert name in early, f"Critical component '{name}' not in first 5 waves"
