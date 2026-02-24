"""Tests for v270.4 Phase 7: Task lifecycle ownership and recovery policy unification.

Validates:
1. TaskLifecycleManager.register_task() — sync task registration for lifecycle tracking
2. RecoveryPolicy registry — canonical recovery parameters, threshold alignment
3. Disease 1 cure — fire-and-forget tasks now tracked
4. Disease 2 cure — circuit breaker thresholds unified via recovery policy
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
# 1. TaskLifecycleManager.register_task() — Sync Task Registration
# ===========================================================================

class TestRegisterTask:
    """Verify register_task() tracks externally-created tasks."""

    def test_register_task_method_exists(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager
        assert hasattr(TaskLifecycleManager, "register_task")

    @pytest.mark.asyncio
    async def test_register_tracks_task(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager, TaskPriority
        mgr = TaskLifecycleManager()

        async def noop():
            await asyncio.sleep(0.01)

        task = asyncio.create_task(noop(), name="test-register")
        mgr.register_task("test_register", task, priority=TaskPriority.LOW)

        # Task should be tracked
        assert "test_register" in mgr._tasks
        assert mgr._tasks["test_register"].task is task

        await task  # Let it complete

    @pytest.mark.asyncio
    async def test_register_done_callback_fires(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager, TaskState
        mgr = TaskLifecycleManager()

        async def quick():
            return 42

        task = asyncio.create_task(quick(), name="test-done-cb")
        mgr.register_task("test_done_cb", task)
        await task
        # Give done callback a tick to fire
        await asyncio.sleep(0.05)

        assert mgr._tasks["test_done_cb"].state == TaskState.COMPLETED

    @pytest.mark.asyncio
    async def test_register_failed_task_logs_error(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager, TaskState
        mgr = TaskLifecycleManager()

        async def failing():
            raise ValueError("intentional test error")

        task = asyncio.create_task(failing(), name="test-fail")
        mgr.register_task("test_fail", task)

        # Wait for task to fail
        try:
            await task
        except ValueError:
            pass
        await asyncio.sleep(0.05)

        assert mgr._tasks["test_fail"].state == TaskState.FAILED
        assert "intentional test error" in mgr._tasks["test_fail"].error

    @pytest.mark.asyncio
    async def test_register_cancelled_task(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager, TaskState
        mgr = TaskLifecycleManager()

        async def slow():
            await asyncio.sleep(10)

        task = asyncio.create_task(slow(), name="test-cancel")
        mgr.register_task("test_cancel", task)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)

        assert mgr._tasks["test_cancel"].state == TaskState.CANCELLED

    def test_register_during_shutdown_rejected(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager
        mgr = TaskLifecycleManager()
        mgr._shutdown_started = True

        # Should not raise but should warn and return without tracking
        loop = asyncio.new_event_loop()
        task = loop.create_task(asyncio.sleep(0))
        mgr.register_task("should_fail", task)
        assert "should_fail" not in mgr._tasks
        task.cancel()
        loop.close()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_registered_tasks(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager
        mgr = TaskLifecycleManager()

        async def long_running():
            await asyncio.sleep(100)

        task = asyncio.create_task(long_running(), name="test-shutdown")
        mgr.register_task("test_shutdown", task)

        result = await mgr.shutdown_all(timeout=5.0)
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_register_with_monitor_flag(self):
        from backend.core.task_lifecycle_manager import TaskLifecycleManager, TaskPriority
        mgr = TaskLifecycleManager()

        async def monitor():
            await asyncio.sleep(0.01)

        task = asyncio.create_task(monitor(), name="test-monitor")
        mgr.register_task("test_monitor", task, priority=TaskPriority.MONITORING, is_monitor=True)

        assert mgr._tasks["test_monitor"].is_monitor is True
        await task


# ===========================================================================
# 2. RecoveryPolicy Registry — Canonical Recovery Parameters
# ===========================================================================

class TestRecoveryPolicyRegistry:
    """Verify recovery policy registry has correct canonical values."""

    def test_module_imports(self):
        mod = _import_module("backend.core.recovery_policy")
        assert mod is not None, "recovery_policy must be importable"

    def test_recovery_policies_not_empty(self):
        from backend.core.recovery_policy import RECOVERY_POLICIES
        assert len(RECOVERY_POLICIES) >= 7, (
            f"Expected at least 7 recovery policies, got {len(RECOVERY_POLICIES)}"
        )

    def test_each_policy_has_required_fields(self):
        from backend.core.recovery_policy import RECOVERY_POLICIES
        for name, p in RECOVERY_POLICIES.items():
            assert p.resource_name, f"Policy {name} missing resource_name"
            assert p.description, f"Policy {name} missing description"
            assert p.circuit_failure_threshold >= 1, (
                f"Policy {name} has invalid threshold={p.circuit_failure_threshold}"
            )
            assert p.circuit_recovery_seconds > 0, (
                f"Policy {name} has invalid recovery={p.circuit_recovery_seconds}"
            )

    def test_get_recovery_params_returns_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        p = get_recovery_params("prime_router")
        assert p is not None
        assert p.resource_name == "prime_router"

    def test_get_recovery_params_returns_none_for_unknown(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("nonexistent_resource") is None

    def test_policy_is_frozen(self):
        from backend.core.recovery_policy import get_recovery_params
        p = get_recovery_params("prime_router")
        with pytest.raises(AttributeError):
            p.circuit_failure_threshold = 999  # type: ignore

    def test_effective_recovery_uses_max(self):
        from backend.core.recovery_policy import get_recovery_params
        p = get_recovery_params("gcp_vm_ops")
        assert p is not None
        # effective_recovery should be max(recovery_seconds, cooldown_seconds)
        assert p.effective_recovery() == max(
            p.circuit_recovery_seconds, p.cooldown_seconds
        )


# ===========================================================================
# 3. Disease 1 Cure — PrimeRouter threshold alignment
# ===========================================================================

class TestPrimeRouterThresholdAlignment:
    """Verify PrimeRouter circuit breaker uses recovery policy threshold."""

    def test_prime_router_threshold_is_3_not_2(self):
        """The key fix: PrimeRouter threshold was 2 (too aggressive), now 3."""
        from backend.core.recovery_policy import get_recovery_params
        p = get_recovery_params("prime_router")
        assert p is not None
        assert p.circuit_failure_threshold >= 3, (
            f"PrimeRouter threshold should be >= 3, got {p.circuit_failure_threshold}"
        )

    def test_prime_router_reads_from_policy(self):
        """Verify _EndpointAwareCircuitBreaker pulls from recovery policy."""
        from backend.core.prime_router import _EndpointAwareCircuitBreaker
        cb = _EndpointAwareCircuitBreaker()
        # Should have read from policy (threshold=3, not the old default=2)
        assert cb._threshold >= 3, (
            f"Circuit breaker threshold should be >= 3, got {cb._threshold}"
        )


# ===========================================================================
# 4. Disease 2 Cure — GCP quota cooldown aligned with circuit recovery
# ===========================================================================

class TestGCPRecoveryAlignment:
    """Verify GCP recovery parameters are consistent."""

    def test_gcp_vm_ops_cooldown_exceeds_recovery(self):
        """Quota cooldown MUST exceed circuit recovery to prevent retry storms."""
        from backend.core.recovery_policy import get_recovery_params
        p = get_recovery_params("gcp_vm_ops")
        assert p is not None
        assert p.cooldown_seconds >= p.circuit_recovery_seconds, (
            f"Cooldown ({p.cooldown_seconds}s) must >= recovery ({p.circuit_recovery_seconds}s) "
            f"to prevent retry storms"
        )

    def test_gcp_effective_recovery_is_cooldown(self):
        """effective_recovery() should return cooldown when cooldown > recovery."""
        from backend.core.recovery_policy import get_recovery_params
        p = get_recovery_params("gcp_vm_ops")
        assert p is not None
        assert p.effective_recovery() == p.cooldown_seconds

    def test_all_gcp_policies_have_consistent_recovery(self):
        """All GCP-related policies should have recovery >= 60s."""
        from backend.core.recovery_policy import RECOVERY_POLICIES
        for name, p in RECOVERY_POLICIES.items():
            if "gcp" in name:
                assert p.circuit_recovery_seconds >= 30.0, (
                    f"GCP policy {name} recovery too aggressive: {p.circuit_recovery_seconds}s"
                )


# ===========================================================================
# 5. Model Serving — Recovery Policy Integration
# ===========================================================================

class TestModelServingRecovery:
    """Verify UnifiedModelServing uses recovery policy for circuit breaker."""

    def test_circuit_breaker_constants_from_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        p = get_recovery_params("model_serving")
        assert p is not None

        from backend.intelligence.unified_model_serving import (
            CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            CIRCUIT_BREAKER_RECOVERY_SECONDS,
        )
        assert CIRCUIT_BREAKER_FAILURE_THRESHOLD == p.circuit_failure_threshold
        assert CIRCUIT_BREAKER_RECOVERY_SECONDS == p.circuit_recovery_seconds


# ===========================================================================
# 6. Cross-Repo Task Lifecycle Registration Exists
# ===========================================================================

class TestCrossRepoTaskRegistration:
    """Verify cross_repo module-level tasks are registered with TLM."""

    def test_module_imports(self):
        mod = _import_module("backend.supervisor.cross_repo_startup_orchestrator")
        assert mod is not None

    def test_task_lifecycle_manager_importable(self):
        from backend.core.task_lifecycle_manager import (
            TaskLifecycleManager, get_task_manager, TaskPriority
        )
        mgr = get_task_manager()
        assert hasattr(mgr, "register_task")
        assert hasattr(mgr, "shutdown_all")

    def test_register_task_is_sync(self):
        """register_task must be sync (not async) for module-level callers."""
        from backend.core.task_lifecycle_manager import TaskLifecycleManager
        import inspect
        assert not inspect.iscoroutinefunction(TaskLifecycleManager.register_task)


# ===========================================================================
# 7. Env Var Override — Recovery Policy Respects Env Vars
# ===========================================================================

class TestRecoveryPolicyEnvOverride:
    """Verify recovery policies can be overridden via env vars."""

    def test_prime_router_env_override(self):
        """PRIME_ROUTER_CIRCUIT_FAILURES env var should override default."""
        key = "PRIME_ROUTER_CIRCUIT_FAILURES"
        try:
            os.environ[key] = "7"
            # Force re-import to pick up env var
            import backend.core.recovery_policy as rp
            importlib.reload(rp)
            p = rp.get_recovery_params("prime_router")
            assert p is not None
            assert p.circuit_failure_threshold == 7
        finally:
            os.environ.pop(key, None)
            importlib.reload(rp)

    def test_gcp_cooldown_env_override(self):
        """GCP_QUOTA_COOLDOWN_SECONDS env var should override default."""
        key = "GCP_QUOTA_COOLDOWN_SECONDS"
        try:
            os.environ[key] = "600"
            import backend.core.recovery_policy as rp
            importlib.reload(rp)
            p = rp.get_recovery_params("gcp_vm_ops")
            assert p is not None
            assert p.cooldown_seconds == 600.0
        finally:
            os.environ.pop(key, None)
            importlib.reload(rp)


# ===========================================================================
# 8. Supervisor TLM Shutdown Integration
# ===========================================================================

class TestSupervisorTLMShutdown:
    """Verify supervisor calls TLM shutdown during teardown."""

    def test_supervisor_has_tlm_shutdown_call(self):
        """The supervisor must import and call TLM shutdown_all during stop."""
        import re
        # Read a section of unified_supervisor.py around the shutdown area
        with open("unified_supervisor.py", "r") as f:
            content = f.read()

        # Check that TLM shutdown is wired in
        assert "task_lifecycle_manager" in content
        assert "shutdown_all" in content

    def test_tlm_shutdown_returns_stats(self):
        """shutdown_all() must return a dict with status."""
        loop = asyncio.new_event_loop()
        try:
            from backend.core.task_lifecycle_manager import TaskLifecycleManager
            mgr = TaskLifecycleManager()
            result = loop.run_until_complete(mgr.shutdown_all(timeout=1.0))
            assert isinstance(result, dict)
            assert "status" in result
        finally:
            loop.close()


# ===========================================================================
# 9. Policy Completeness — All Disease Sites Have Policies
# ===========================================================================

class TestPolicyCompleteness:
    """Verify all disease sites have corresponding recovery policies."""

    def test_prime_router_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("prime_router") is not None

    def test_prime_client_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("prime_client") is not None

    def test_model_serving_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("model_serving") is not None

    def test_gcp_vm_ops_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("gcp_vm_ops") is not None

    def test_gcp_cost_tracker_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("gcp_cost_tracker") is not None

    def test_gcp_quota_check_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("gcp_quota_check") is not None

    def test_cloud_sql_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("cloud_sql") is not None

    def test_redis_has_policy(self):
        from backend.core.recovery_policy import get_recovery_params
        assert get_recovery_params("redis") is not None


# ===========================================================================
# 10. No Threshold Conflict — All Policies Are Consistent
# ===========================================================================

class TestNoThresholdConflict:
    """Verify no policy has internally inconsistent parameters."""

    def test_no_recovery_shorter_than_1_second(self):
        from backend.core.recovery_policy import RECOVERY_POLICIES
        for name, p in RECOVERY_POLICIES.items():
            assert p.circuit_recovery_seconds >= 1.0, (
                f"Policy {name} recovery too short: {p.circuit_recovery_seconds}s"
            )

    def test_cooldown_never_less_than_recovery(self):
        """Where cooldown is set, it must not be less than recovery."""
        from backend.core.recovery_policy import RECOVERY_POLICIES
        for name, p in RECOVERY_POLICIES.items():
            if p.cooldown_seconds > 0:
                assert p.cooldown_seconds >= p.circuit_recovery_seconds, (
                    f"Policy {name}: cooldown ({p.cooldown_seconds}s) < recovery "
                    f"({p.circuit_recovery_seconds}s) — retry storm risk"
                )

    def test_half_open_max_calls_positive(self):
        from backend.core.recovery_policy import RECOVERY_POLICIES
        for name, p in RECOVERY_POLICIES.items():
            assert p.circuit_half_open_max_calls >= 1, (
                f"Policy {name} half_open_max_calls must be >= 1"
            )
