# tests/unit/core/test_lifecycle_engine.py
"""Tests for LifecycleEngine — state machine, DAG, wave execution."""

import asyncio
import os
import pytest


def _import_module():
    try:
        from backend.core.lifecycle_engine import LifecycleEngine
        return LifecycleEngine
    except ImportError:
        return None


class TestLifecycleEngineImport:
    def test_module_imports(self):
        cls = _import_module()
        assert cls is not None

    def test_required_exports(self):
        import backend.core.lifecycle_engine as mod
        assert hasattr(mod, "LifecycleEngine")
        assert hasattr(mod, "ComponentDeclaration")
        assert hasattr(mod, "ComponentLocality")
        assert hasattr(mod, "InvalidTransitionError")
        assert hasattr(mod, "CyclicDependencyError")
        assert hasattr(mod, "VALID_TRANSITIONS")


class TestComponentDeclaration:
    def test_declaration_is_frozen(self):
        from backend.core.lifecycle_engine import ComponentDeclaration, ComponentLocality
        decl = ComponentDeclaration(name="test", locality=ComponentLocality.IN_PROCESS)
        with pytest.raises(AttributeError):
            decl.name = "modified"

    def test_default_values(self):
        from backend.core.lifecycle_engine import ComponentDeclaration, ComponentLocality
        decl = ComponentDeclaration(name="test", locality=ComponentLocality.IN_PROCESS)
        assert decl.dependencies == ()
        assert decl.soft_dependencies == ()
        assert decl.is_critical is False
        assert decl.start_timeout_s == 60.0
        assert decl.heartbeat_ttl_s == 30.0


class TestValidTransitions:
    def test_registered_can_start(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "STARTING" in VALID_TRANSITIONS["REGISTERED"]

    def test_starting_can_handshake_or_fail(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "HANDSHAKING" in VALID_TRANSITIONS["STARTING"]
        assert "FAILED" in VALID_TRANSITIONS["STARTING"]

    def test_ready_can_degrade_drain_fail_lost(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        ready_targets = VALID_TRANSITIONS["READY"]
        assert "DEGRADED" in ready_targets
        assert "DRAINING" in ready_targets
        assert "FAILED" in ready_targets
        assert "LOST" in ready_targets

    def test_failed_can_restart(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "STARTING" in VALID_TRANSITIONS["FAILED"]

    def test_stopped_can_restart(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "STARTING" in VALID_TRANSITIONS["STOPPED"]

    def test_invalid_transition_not_possible(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "READY" not in VALID_TRANSITIONS["REGISTERED"]
        assert "STARTING" not in VALID_TRANSITIONS["READY"]


class TestStateTransitions:
    @pytest.fixture
    async def engine(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.lifecycle_engine import (
            LifecycleEngine, ComponentDeclaration, ComponentLocality,
        )
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        components = (
            ComponentDeclaration(
                name="backend_api",
                locality=ComponentLocality.IN_PROCESS,
                is_critical=True,
            ),
            ComponentDeclaration(
                name="jarvis_prime",
                locality=ComponentLocality.SUBPROCESS,
                dependencies=("backend_api",),
            ),
        )
        engine = LifecycleEngine(journal, components)
        yield engine
        await journal.close()

    async def test_initial_status_is_registered(self, engine):
        assert engine.get_status("backend_api") == "REGISTERED"
        assert engine.get_status("jarvis_prime") == "REGISTERED"

    async def test_valid_transition_succeeds(self, engine):
        await engine.transition_component("backend_api", "STARTING", reason="test")
        assert engine.get_status("backend_api") == "STARTING"

    async def test_invalid_transition_raises(self, engine):
        from backend.core.lifecycle_engine import InvalidTransitionError
        with pytest.raises(InvalidTransitionError):
            await engine.transition_component("backend_api", "READY", reason="skip_handshake")

    async def test_transition_journals_entry(self, engine):
        await engine.transition_component("backend_api", "STARTING", reason="test")
        entries = await engine._journal.replay_from(0, action_filter=["state_transition"])
        targets = [e["target"] for e in entries]
        assert "backend_api" in targets

    async def test_full_lifecycle_path(self, engine):
        comp = "backend_api"
        await engine.transition_component(comp, "STARTING", reason="boot")
        await engine.transition_component(comp, "HANDSHAKING", reason="health_ok")
        await engine.transition_component(comp, "READY", reason="handshake_ok")
        await engine.transition_component(comp, "DRAINING", reason="shutdown")
        await engine.transition_component(comp, "STOPPING", reason="drain_done")
        await engine.transition_component(comp, "STOPPED", reason="terminated")
        assert engine.get_status(comp) == "STOPPED"

    async def test_recovery_from_failed(self, engine):
        comp = "backend_api"
        await engine.transition_component(comp, "STARTING", reason="boot")
        await engine.transition_component(comp, "FAILED", reason="crash")
        await engine.transition_component(comp, "STARTING", reason="retry")
        assert engine.get_status(comp) == "STARTING"

    async def test_get_all_statuses(self, engine):
        statuses = engine.get_all_statuses()
        assert "backend_api" in statuses
        assert "jarvis_prime" in statuses
        assert statuses["backend_api"] == "REGISTERED"


class TestWaveComputation:
    def test_independent_components_same_wave(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS),
        )
        waves = compute_waves(comps)
        assert len(waves) == 1
        names = {c.name for c in waves[0]}
        assert names == {"a", "b"}

    def test_dependency_creates_separate_waves(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("a",)),
        )
        waves = compute_waves(comps)
        assert len(waves) == 2
        assert waves[0][0].name == "a"
        assert waves[1][0].name == "b"

    def test_cycle_detected(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
            CyclicDependencyError,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("b",)),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("a",)),
        )
        with pytest.raises(CyclicDependencyError):
            compute_waves(comps)

    def test_soft_deps_dont_affect_wave_ordering(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS,
                                soft_dependencies=("a",)),
        )
        waves = compute_waves(comps)
        # Soft dep = same wave (no ordering constraint)
        assert len(waves) == 1

    def test_diamond_dependency(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="root", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="left", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("root",)),
            ComponentDeclaration(name="right", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("root",)),
            ComponentDeclaration(name="join", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("left", "right")),
        )
        waves = compute_waves(comps)
        assert len(waves) == 3
        assert waves[0][0].name == "root"
        assert {c.name for c in waves[1]} == {"left", "right"}
        assert waves[2][0].name == "join"


class TestFailurePropagation:
    @pytest.fixture
    async def engine(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.lifecycle_engine import (
            LifecycleEngine, ComponentDeclaration, ComponentLocality,
        )
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        components = (
            ComponentDeclaration(
                name="backend", locality=ComponentLocality.IN_PROCESS,
                is_critical=True,
            ),
            ComponentDeclaration(
                name="prime", locality=ComponentLocality.SUBPROCESS,
                dependencies=("backend",),
            ),
            ComponentDeclaration(
                name="reactor", locality=ComponentLocality.SUBPROCESS,
                soft_dependencies=("prime",),
            ),
        )
        engine = LifecycleEngine(journal, components)
        yield engine
        await journal.close()

    async def test_hard_dep_failure_skips_dependent(self, engine):
        await engine.transition_component("backend", "STARTING", reason="boot")
        await engine.transition_component("backend", "FAILED", reason="crash")
        await engine.propagate_failure("backend", "failed")
        # prime depends on backend (hard) — should be FAILED
        assert engine.get_status("prime") == "FAILED"

    async def test_soft_dep_failure_degrades_dependent(self, engine):
        # Get reactor to READY state first
        await engine.transition_component("reactor", "STARTING", reason="boot")
        await engine.transition_component("reactor", "HANDSHAKING", reason="health")
        await engine.transition_component("reactor", "READY", reason="handshake")

        # Prime fails — reactor soft-depends on prime
        await engine.transition_component("prime", "STARTING", reason="boot")
        await engine.transition_component("prime", "FAILED", reason="crash")
        await engine.propagate_failure("prime", "failed")
        assert engine.get_status("reactor") == "DEGRADED"

    async def test_hard_dep_lost_drains_dependent(self, engine):
        # Get both to READY
        await engine.transition_component("backend", "STARTING", reason="boot")
        await engine.transition_component("backend", "HANDSHAKING", reason="health")
        await engine.transition_component("backend", "READY", reason="ok")
        await engine.transition_component("prime", "STARTING", reason="boot")
        await engine.transition_component("prime", "HANDSHAKING", reason="health")
        await engine.transition_component("prime", "READY", reason="ok")

        # Backend goes LOST
        await engine.transition_component("backend", "LOST", reason="heartbeat_expired")
        await engine.propagate_failure("backend", "lost")
        assert engine.get_status("prime") == "DRAINING"
