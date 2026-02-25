"""Tests for v272.0 Phase 9: Startup transactions and ownership declarations.

Validates:
1. StartupTransaction — artifact registration, phase commit, abort cleanup
2. PhaseArtifact — frozen dataclass integrity
3. abort_with_cleanup — LIFO order, timeout handling, idempotency, committed-phase skip
4. OwnershipDeclaration — frozen dataclass, registry structure
5. check_caller_authorized — authorized, unauthorized, unknown concept
6. Integration — modules importable, singletons consistent
"""

import asyncio
import importlib
import os
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_module(dotted_name: str):
    try:
        return importlib.import_module(dotted_name)
    except Exception:
        return None


# ===========================================================================
# 1. StartupTransaction Module
# ===========================================================================

class TestStartupTransactionModule:
    """Verify startup_transaction.py module structure."""

    def test_module_imports(self):
        mod = _import_module("backend.core.startup_transaction")
        assert mod is not None, "startup_transaction must be importable"

    def test_singleton_consistent(self):
        from backend.core.startup_transaction import get_startup_transaction
        a = get_startup_transaction()
        b = get_startup_transaction()
        assert a is b

    def test_reset_clears_state(self):
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        txn.register_phase_artifact("test", "env_var", "test", lambda: None)
        txn.mark_phase_committed("test")
        assert txn.artifact_count == 1
        assert txn.phase_count == 1

        txn.reset()
        assert txn.artifact_count == 0
        assert txn.phase_count == 0
        assert txn.is_aborted is False


# ===========================================================================
# 2. PhaseArtifact
# ===========================================================================

class TestPhaseArtifact:
    """Verify PhaseArtifact frozen dataclass."""

    def test_frozen(self):
        from backend.core.startup_transaction import PhaseArtifact
        a = PhaseArtifact(
            phase="test", artifact_type="env_var",
            description="test", cleanup_fn=lambda: None,
        )
        with pytest.raises(AttributeError):
            a.phase = "hacked"  # type: ignore

    def test_fields_populated(self):
        from backend.core.startup_transaction import PhaseArtifact
        fn = lambda: None
        a = PhaseArtifact(
            phase="preflight", artifact_type="lock",
            description="Startup lock", cleanup_fn=fn,
        )
        assert a.phase == "preflight"
        assert a.artifact_type == "lock"
        assert a.description == "Startup lock"
        assert a.cleanup_fn is fn
        assert a.registered_at > 0


# ===========================================================================
# 3. Artifact Registration
# ===========================================================================

class TestArtifactRegistration:
    """Verify registration, commit, and query."""

    def test_register_returns_index(self):
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        idx0 = txn.register_phase_artifact("p1", "task", "t1", lambda: None)
        idx1 = txn.register_phase_artifact("p1", "task", "t2", lambda: None)
        assert idx0 == 0
        assert idx1 == 1

    @pytest.mark.asyncio
    async def test_register_after_abort_returns_negative(self):
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        await txn.abort_with_cleanup("test_abort")
        idx = txn.register_phase_artifact("p1", "task", "t1", lambda: None)
        assert idx == -1

    def test_commit_records_artifact_count(self):
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "t1", lambda: None)
        txn.register_phase_artifact("p1", "task", "t2", lambda: None)
        txn.register_phase_artifact("p2", "task", "t3", lambda: None)
        txn.mark_phase_committed("p1")

        assert txn.is_committed("p1") is True
        assert txn.is_committed("p2") is False

    def test_uncommitted_artifacts(self):
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "committed", lambda: None)
        txn.mark_phase_committed("p1")
        txn.register_phase_artifact("p2", "task", "uncommitted", lambda: None)

        uncommitted = txn.get_uncommitted_artifacts()
        assert len(uncommitted) == 1
        assert uncommitted[0].description == "uncommitted"


# ===========================================================================
# 4. Abort Cleanup
# ===========================================================================

class TestAbortCleanup:
    """Verify abort_with_cleanup behavior."""

    @pytest.mark.asyncio
    async def test_abort_calls_cleanup_reverse_order(self):
        from backend.core.startup_transaction import StartupTransaction
        order = []
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "A", lambda: order.append("A"))
        txn.register_phase_artifact("p1", "task", "B", lambda: order.append("B"))

        result = await txn.abort_with_cleanup("test")
        assert order == ["B", "A"]  # LIFO
        assert result["status"] == "aborted"
        assert result["cleaned"] == 2

    @pytest.mark.asyncio
    async def test_abort_skips_committed_phases(self):
        from backend.core.startup_transaction import StartupTransaction
        cleaned = []
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "committed", lambda: cleaned.append("p1"))
        txn.mark_phase_committed("p1")
        txn.register_phase_artifact("p2", "task", "uncommitted", lambda: cleaned.append("p2"))

        result = await txn.abort_with_cleanup("test")
        assert "p2" in cleaned
        assert "p1" not in cleaned  # Committed phase skipped

    @pytest.mark.asyncio
    async def test_abort_idempotent(self):
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "t1", lambda: None)

        r1 = await txn.abort_with_cleanup("first")
        r2 = await txn.abort_with_cleanup("second")
        assert r1["status"] == "aborted"
        assert r2["status"] == "already_aborted"
        assert r2["reason"] == "first"  # Original reason preserved

    @pytest.mark.asyncio
    async def test_abort_handles_sync_cleanup(self):
        from backend.core.startup_transaction import StartupTransaction
        flag = {"cleaned": False}
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "env_var", "test",
                                     lambda: flag.__setitem__("cleaned", True))

        await txn.abort_with_cleanup("test")
        assert flag["cleaned"] is True

    @pytest.mark.asyncio
    async def test_abort_handles_async_cleanup(self):
        from backend.core.startup_transaction import StartupTransaction
        flag = {"cleaned": False}

        async def async_cleanup():
            await asyncio.sleep(0.01)
            flag["cleaned"] = True

        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "service", "test", async_cleanup)

        await txn.abort_with_cleanup("test")
        assert flag["cleaned"] is True

    @pytest.mark.asyncio
    async def test_abort_tolerates_cleanup_exception(self):
        from backend.core.startup_transaction import StartupTransaction
        cleaned = []
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "good1", lambda: cleaned.append("good1"))
        txn.register_phase_artifact("p1", "task", "bad",
                                     lambda: (_ for _ in ()).throw(ValueError("boom")))
        txn.register_phase_artifact("p1", "task", "good2", lambda: cleaned.append("good2"))

        result = await txn.abort_with_cleanup("test")
        # good2 and good1 should still be cleaned (LIFO: good2, bad, good1)
        assert "good2" in cleaned
        assert "good1" in cleaned
        assert result["failed"] >= 1

    @pytest.mark.asyncio
    async def test_abort_clears_startup_complete_env(self, monkeypatch):
        from backend.core.startup_transaction import StartupTransaction
        monkeypatch.setenv("JARVIS_STARTUP_COMPLETE", "true")
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "t1", lambda: None)

        await txn.abort_with_cleanup("test")
        assert os.environ.get("JARVIS_STARTUP_COMPLETE") is None


# ===========================================================================
# 5. OwnershipRegistry Module
# ===========================================================================

class TestOwnershipRegistryModule:
    """Verify ownership_registry.py module structure."""

    def test_module_imports(self):
        mod = _import_module("backend.core.ownership_registry")
        assert mod is not None, "ownership_registry must be importable"

    def test_declarations_dict_exists(self):
        from backend.core.ownership_registry import OWNERSHIP_DECLARATIONS
        assert isinstance(OWNERSHIP_DECLARATIONS, dict)
        assert len(OWNERSHIP_DECLARATIONS) >= 5

    def test_declarations_frozen(self):
        from backend.core.ownership_registry import OWNERSHIP_DECLARATIONS
        decl = list(OWNERSHIP_DECLARATIONS.values())[0]
        with pytest.raises(AttributeError):
            decl.concept = "hacked"  # type: ignore


# ===========================================================================
# 6. Ownership Declarations
# ===========================================================================

class TestOwnershipDeclarations:
    """Verify specific ownership declarations."""

    def test_gcp_vm_lifecycle_declared(self):
        from backend.core.ownership_registry import get_owner
        d = get_owner("gcp_vm_lifecycle")
        assert d is not None
        assert d.owning_module == "backend.core.gcp_vm_manager"
        assert "backend.core.supervisor_gcp_controller" in d.allowed_callers

    def test_startup_mode_declared(self):
        from backend.core.ownership_registry import get_owner
        d = get_owner("startup_mode")
        assert d is not None
        assert d.owning_module == "unified_supervisor"

    def test_prime_routing_declared(self):
        from backend.core.ownership_registry import get_owner
        d = get_owner("prime_routing")
        assert d is not None
        assert d.owning_module == "backend.core.prime_router"

    def test_startup_lifecycle_declared(self):
        from backend.core.ownership_registry import get_owner
        d = get_owner("startup_lifecycle")
        assert d is not None
        assert d.owning_module == "unified_supervisor"

    def test_trinity_gcp_ready_event_declared(self):
        from backend.core.ownership_registry import get_owner
        d = get_owner("trinity_gcp_ready_event")
        assert d is not None
        assert d.owning_module == "backend.supervisor.cross_repo_startup_orchestrator"

    def test_unknown_concept_returns_none(self):
        from backend.core.ownership_registry import get_owner
        assert get_owner("nonexistent_concept") is None


# ===========================================================================
# 7. Caller Authorization
# ===========================================================================

class TestCallerAuthorization:
    """Verify check_caller_authorized behavior."""

    def test_authorized_caller(self):
        from backend.core.ownership_registry import check_caller_authorized
        assert check_caller_authorized("gcp_vm_lifecycle", "unified_supervisor") is True

    def test_authorized_via_suffix_match(self):
        from backend.core.ownership_registry import check_caller_authorized
        assert check_caller_authorized("gcp_vm_lifecycle", "backend.core.gcp_hybrid_prime_router") is True

    def test_authorized_via_basename_match(self):
        from backend.core.ownership_registry import check_caller_authorized
        # Just the file name without package path
        assert check_caller_authorized("gcp_vm_lifecycle", "supervisor_gcp_controller") is True

    def test_unauthorized_caller(self):
        from backend.core.ownership_registry import check_caller_authorized
        assert check_caller_authorized("gcp_vm_lifecycle", "random_unknown_module") is False

    def test_unknown_concept_returns_true(self):
        from backend.core.ownership_registry import check_caller_authorized
        # Unknown concepts are permissive (don't block)
        assert check_caller_authorized("nonexistent", "any_module") is True

    def test_list_concepts(self):
        from backend.core.ownership_registry import list_concepts
        concepts = list_concepts()
        assert "gcp_vm_lifecycle" in concepts
        assert "startup_mode" in concepts
        assert "prime_routing" in concepts
        assert len(concepts) >= 5


# ===========================================================================
# 8. Integration Wiring
# ===========================================================================

class TestIntegrationWiring:
    """Verify wiring in consumer files."""

    def test_supervisor_has_transaction_init(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert "get_startup_transaction" in content
        assert "abort_with_cleanup" in content
        assert "mark_phase_committed" in content

    def test_supervisor_has_abort_on_preflight_fail(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert 'abort_with_cleanup("preflight_failed")' in content

    def test_supervisor_has_abort_on_resources_fail(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert 'abort_with_cleanup("resources_failed")' in content

    def test_supervisor_has_abort_on_backend_fail(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert 'abort_with_cleanup("backend_failed")' in content

    def test_supervisor_registers_startup_complete(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert "JARVIS_STARTUP_COMPLETE" in content
        # Should register it as an artifact
        assert '"finalization"' in content or "'finalization'" in content

    def test_gcp_vm_manager_has_ownership_check(self):
        with open("backend/core/gcp_vm_manager.py", "r") as f:
            content = f.read()
        assert "check_caller_authorized" in content
        assert "gcp_vm_lifecycle" in content


# ===========================================================================
# 9. Transaction Edge Cases
# ===========================================================================

class TestTransactionEdgeCases:
    """Verify edge case handling."""

    def test_empty_abort(self):
        """Abort with no artifacts should not crash."""
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(txn.abort_with_cleanup("empty"))
            assert result["status"] == "aborted"
            assert result["cleaned"] == 0
        finally:
            loop.close()

    def test_all_committed_abort(self):
        """Abort when all phases committed should clean nothing."""
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "task", "t1", lambda: None)
        txn.mark_phase_committed("p1")

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(txn.abort_with_cleanup("test"))
            assert result["cleaned"] == 0
        finally:
            loop.close()

    def test_phase_commit_record(self):
        from backend.core.startup_transaction import StartupTransaction
        txn = StartupTransaction()
        txn.register_phase_artifact("p1", "a", "d1", lambda: None)
        txn.register_phase_artifact("p1", "b", "d2", lambda: None)
        txn.register_phase_artifact("p2", "c", "d3", lambda: None)
        txn.mark_phase_committed("p1")

        # phase_count should be 1 (only p1 committed)
        assert txn.phase_count == 1
        # artifact_count should be 3 (total registered)
        assert txn.artifact_count == 3
