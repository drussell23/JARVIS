# tests/unit/supervisor/test_cross_repo_startup_orchestrator.py
"""
Tests for ProcessOrchestrator.startup_lock_context() method.

TDD approach for Pillar 1: Lock-Guarded Single-Owner Startup.
"""
import pytest
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class TestStartupLockContext:
    """Tests for ProcessOrchestrator.startup_lock_context() async context manager."""

    @pytest.mark.asyncio
    async def test_startup_lock_context_acquires_lock(self):
        """Lock is acquired in __aenter__ and released in __aexit__."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        # Mock the internal lock methods
        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock) as mock_acquire:
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock) as mock_release:
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        mock_acquire.return_value = True

                        async with orchestrator.startup_lock_context(spawn_processes=False) as ctx:
                            mock_acquire.assert_called_once()
                            assert ctx is orchestrator

                        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_lock_context_spawn_processes_false(self):
        """When spawn_processes=False, orchestrator does not spawn processes."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock):
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock) as mock_init:
                        async with orchestrator.startup_lock_context(spawn_processes=False):
                            # Verify spawn_processes flag is stored
                            assert orchestrator._spawn_processes is False

                        # verify _initialize_cross_repo_state was called with spawn_processes=False
                        mock_init.assert_called_once_with(spawn_processes=False)

    @pytest.mark.asyncio
    async def test_startup_lock_context_spawn_processes_true(self):
        """When spawn_processes=True (default), orchestrator allows spawning."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock):
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock) as mock_init:
                        async with orchestrator.startup_lock_context(spawn_processes=True):
                            assert orchestrator._spawn_processes is True

                        mock_init.assert_called_once_with(spawn_processes=True)

    @pytest.mark.asyncio
    async def test_startup_lock_context_failure_raises_error(self):
        """Lock acquisition failure raises StartupLockError."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            StartupLockError,
        )

        orchestrator = ProcessOrchestrator()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=False):
            with pytest.raises(StartupLockError, match="Failed to acquire"):
                async with orchestrator.startup_lock_context(spawn_processes=False):
                    pass

    @pytest.mark.asyncio
    async def test_startup_lock_released_on_exception(self):
        """Lock is released even when an exception occurs in the context body."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock) as mock_release:
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        with pytest.raises(ValueError, match="test error"):
                            async with orchestrator.startup_lock_context(spawn_processes=False):
                                raise ValueError("test error")

                        # Lock should still be released
                        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_lock_context_calls_hardware_enforcement(self):
        """startup_lock_context calls _enforce_hardware_environment."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock):
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock) as mock_hw:
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        async with orchestrator.startup_lock_context(spawn_processes=False):
                            pass

                        mock_hw.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_lock_context_starts_gcp_prewarm_when_enabled(self):
        """startup_lock_context starts GCP prewarm when _gcp_prewarm_enabled is True."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        orchestrator._gcp_prewarm_enabled = True

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock):
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        with patch.object(orchestrator, '_start_gcp_prewarm', new_callable=AsyncMock) as mock_gcp:
                            async with orchestrator.startup_lock_context(spawn_processes=False):
                                pass

                            mock_gcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_lock_context_skips_gcp_prewarm_when_disabled(self):
        """startup_lock_context skips GCP prewarm when _gcp_prewarm_enabled is False."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        orchestrator._gcp_prewarm_enabled = False

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock):
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        with patch.object(orchestrator, '_start_gcp_prewarm', new_callable=AsyncMock) as mock_gcp:
                            async with orchestrator.startup_lock_context(spawn_processes=False):
                                pass

                            mock_gcp.assert_not_called()


class TestSupervisorAuthorityGate:
    """Tests for split-brain fencing in supervisor authority checks."""

    def test_authority_requires_kernel_lock_owner(self, tmp_path, monkeypatch):
        import backend.supervisor.cross_repo_startup_orchestrator as orchestrator_mod

        authority_state = tmp_path / "locks" / "supervisor_authority.json"
        kernel_lock = tmp_path / "locks" / "kernel.lock"
        kernel_lock.parent.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(orchestrator_mod, "_AUTHORITY_STATE_PATH", authority_state)
        monkeypatch.setattr(orchestrator_mod, "_KERNEL_LOCK_PATH", kernel_lock)

        kernel_lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        epoch = orchestrator_mod.grant_supervisor_authority(
            "unit_test_grant",
            owner_pid=os.getpid(),
            epoch="unit-test-epoch",
        )
        assert epoch == "unit-test-epoch"
        assert orchestrator_mod.check_supervisor_authority(
            "unit_test_action",
            expected_epoch=epoch,
        )

        kernel_lock.write_text(json.dumps({"pid": os.getpid() + 99999}), encoding="utf-8")
        assert not orchestrator_mod.check_supervisor_authority(
            "unit_test_action",
            expected_epoch=epoch,
        )

        orchestrator_mod.revoke_supervisor_authority("unit_test_done")

    def test_bootstrap_owner_allowed_before_grant(self, tmp_path, monkeypatch):
        import backend.supervisor.cross_repo_startup_orchestrator as orchestrator_mod

        authority_state = tmp_path / "locks" / "supervisor_authority.json"
        kernel_lock = tmp_path / "locks" / "kernel.lock"
        kernel_lock.parent.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(orchestrator_mod, "_AUTHORITY_STATE_PATH", authority_state)
        monkeypatch.setattr(orchestrator_mod, "_KERNEL_LOCK_PATH", kernel_lock)

        orchestrator_mod.revoke_supervisor_authority("bootstrap_reset")
        kernel_lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

        assert orchestrator_mod.check_supervisor_authority(
            "bootstrap_action",
            allow_bootstrap_owner=True,
        )
        assert not orchestrator_mod.check_supervisor_authority("bootstrap_action")

    def test_enforce_single_authority_allows_standalone_without_lock(
        self, tmp_path, monkeypatch
    ):
        import backend.supervisor.cross_repo_startup_orchestrator as orchestrator_mod

        authority_state = tmp_path / "locks" / "supervisor_authority.json"
        kernel_lock = tmp_path / "locks" / "kernel.lock"
        kernel_lock.parent.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(orchestrator_mod, "_AUTHORITY_STATE_PATH", authority_state)
        monkeypatch.setattr(orchestrator_mod, "_KERNEL_LOCK_PATH", kernel_lock)

        orchestrator_mod.revoke_supervisor_authority("standalone_reset")
        assert orchestrator_mod.enforce_single_control_plane_authority(
            "standalone_action",
            allow_when_no_kernel_lock=True,
        )

    def test_enforce_single_authority_blocks_when_foreign_lock_owner(
        self, tmp_path, monkeypatch
    ):
        import backend.supervisor.cross_repo_startup_orchestrator as orchestrator_mod

        authority_state = tmp_path / "locks" / "supervisor_authority.json"
        kernel_lock = tmp_path / "locks" / "kernel.lock"
        kernel_lock.parent.mkdir(parents=True, exist_ok=True)
        kernel_lock.write_text(
            json.dumps({"pid": os.getpid() + 99999}),
            encoding="utf-8",
        )

        monkeypatch.setattr(orchestrator_mod, "_AUTHORITY_STATE_PATH", authority_state)
        monkeypatch.setattr(orchestrator_mod, "_KERNEL_LOCK_PATH", kernel_lock)

        orchestrator_mod.revoke_supervisor_authority("foreign_owner_reset")
        with patch.object(orchestrator_mod, "_pid_is_alive", return_value=True):
            assert not orchestrator_mod.enforce_single_control_plane_authority(
                "foreign_owner_action",
                allow_bootstrap_owner=True,
            )


class TestAuthorityGuardedLifecycle:
    """Tests for public lifecycle APIs guarded by control-plane authority."""

    @pytest.mark.asyncio
    async def test_start_all_services_blocked_without_authority(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.enforce_single_control_plane_authority",
            return_value=False,
        ):
            result = await orchestrator.start_all_services()

        assert result == {"auth_gate_blocked": False}

    @pytest.mark.asyncio
    async def test_restart_service_blocked_without_authority(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.enforce_single_control_plane_authority",
            return_value=False,
        ):
            result = await orchestrator.restart_service("jarvis-prime")

        assert result is False


class TestStartupLockError:
    """Tests for the StartupLockError exception class."""

    def test_startup_lock_error_exists(self):
        """StartupLockError is importable from the module."""
        from backend.supervisor.cross_repo_startup_orchestrator import StartupLockError
        assert issubclass(StartupLockError, Exception)

    def test_startup_lock_error_message(self):
        """StartupLockError can be raised with a message."""
        from backend.supervisor.cross_repo_startup_orchestrator import StartupLockError

        with pytest.raises(StartupLockError, match="test message"):
            raise StartupLockError("test message")


class TestSpawnProcessesFlag:
    """Tests for the _spawn_processes flag behavior."""

    @pytest.mark.asyncio
    async def test_spawn_processes_flag_persists(self):
        """The _spawn_processes flag persists on the orchestrator instance."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock):
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        async with orchestrator.startup_lock_context(spawn_processes=False):
                            # Inside context, flag should be False
                            assert orchestrator._spawn_processes is False

                        # After context, flag should still be False (persists)
                        assert orchestrator._spawn_processes is False


class TestUnifiedSupervisorIntegration:
    """
    Tests for unified_supervisor.py integration with ProcessOrchestrator.

    These tests verify the pattern used in unified_supervisor.py where:
    1. ProcessOrchestrator is created
    2. startup_lock_context(spawn_processes=False) is used
    3. TrinityIntegrator is the sole spawner of processes
    """

    @pytest.mark.asyncio
    async def test_unified_supervisor_pattern_with_trinity(self):
        """
        Test the pattern used in unified_supervisor.py:

        orchestrator = ProcessOrchestrator()
        async with orchestrator.startup_lock_context(spawn_processes=False) as ctx:
            integrator = None
            try:
                integrator = TrinityIntegrator(...)
                await integrator.initialize()
                await integrator.start_components()
            except TimeoutError:
                ...
            finally:
                if integrator is not None:
                    await integrator.stop()
        """
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        # Mock the TrinityIntegrator
        mock_trinity = AsyncMock()
        mock_trinity.initialize = AsyncMock()
        mock_trinity.start_components = AsyncMock(return_value={"jarvis-prime": True})
        mock_trinity.stop = AsyncMock()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock) as mock_release:
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock) as mock_init:
                        async with orchestrator.startup_lock_context(spawn_processes=False) as ctx:
                            # Verify spawn_processes is False
                            assert orchestrator._spawn_processes is False

                            integrator = None
                            try:
                                integrator = mock_trinity
                                await integrator.initialize()
                                results = await integrator.start_components()
                                assert results == {"jarvis-prime": True}
                            finally:
                                if integrator is not None:
                                    await integrator.stop()

                        # Verify _initialize_cross_repo_state was called with spawn_processes=False
                        mock_init.assert_called_once_with(spawn_processes=False)

                # Lock should be released after context exits
                mock_release.assert_called_once()

        # TrinityIntegrator should have been properly stopped
        mock_trinity.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_supervisor_pattern_handles_timeout(self):
        """Test that TimeoutError is handled gracefully and lock is still released."""
        import asyncio
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        # Mock TrinityIntegrator that times out
        mock_trinity = AsyncMock()
        mock_trinity.initialize = AsyncMock()
        mock_trinity.start_components = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_trinity.stop = AsyncMock()

        timeout_caught = False

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock) as mock_release:
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        async with orchestrator.startup_lock_context(spawn_processes=False) as ctx:
                            integrator = None
                            try:
                                integrator = mock_trinity
                                await integrator.initialize()
                                await integrator.start_components()
                            except asyncio.TimeoutError:
                                timeout_caught = True
                            finally:
                                if integrator is not None:
                                    await integrator.stop()

                # Lock should be released even after timeout
                mock_release.assert_called_once()

        assert timeout_caught, "TimeoutError should have been caught"
        mock_trinity.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_supervisor_pattern_stops_integrator_on_error(self):
        """Test that integrator.stop() is called even when an error occurs."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        # Mock TrinityIntegrator that raises error during start_components
        mock_trinity = AsyncMock()
        mock_trinity.initialize = AsyncMock()
        mock_trinity.start_components = AsyncMock(side_effect=RuntimeError("Simulated error"))
        mock_trinity.stop = AsyncMock()

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock) as mock_release:
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        with pytest.raises(RuntimeError, match="Simulated error"):
                            async with orchestrator.startup_lock_context(spawn_processes=False) as ctx:
                                integrator = None
                                try:
                                    integrator = mock_trinity
                                    await integrator.initialize()
                                    await integrator.start_components()
                                finally:
                                    if integrator is not None:
                                        await integrator.stop()

                # Lock should be released even on error
                mock_release.assert_called_once()

        # integrator.stop() should have been called
        mock_trinity.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_supervisor_pattern_integrator_none_before_init(self):
        """Test that if integrator is never assigned, stop() is not called."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()

        stop_called = False

        def mock_stop():
            nonlocal stop_called
            stop_called = True

        with patch.object(orchestrator, '_acquire_startup_lock', new_callable=AsyncMock, return_value=True):
            with patch.object(orchestrator, '_release_startup_lock', new_callable=AsyncMock):
                with patch.object(orchestrator, '_enforce_hardware_environment', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_initialize_cross_repo_state', new_callable=AsyncMock):
                        async with orchestrator.startup_lock_context(spawn_processes=False) as ctx:
                            integrator = None
                            try:
                                # Simulate error before integrator is assigned
                                raise ValueError("Early error")
                            except ValueError:
                                pass  # Handle the error
                            finally:
                                if integrator is not None:
                                    mock_stop()

        assert not stop_called, "stop() should not be called when integrator is None"


class TestFailurePropagationPolicy:
    """Tests for deterministic cross-repo failure propagation policy."""

    @staticmethod
    def _managed(name, depends_on=None, soft_depends_on=None, is_critical=True):
        return SimpleNamespace(
            definition=SimpleNamespace(
                name=name,
                depends_on=depends_on or [],
                soft_depends_on=soft_depends_on or [],
                is_critical=is_critical,
                default_port=8001,
                health_endpoint="/health",
            ),
            status=None,
            is_running=False,
            restart_count=0,
            consecutive_failures=0,
        )

    def test_build_failure_plan_transient_drains_hard_dependents(self):
        from backend.supervisor.cross_repo_startup_orchestrator import (
            FailurePropagationAction,
            ProcessOrchestrator,
        )

        orchestrator = ProcessOrchestrator()
        orchestrator.processes = {
            "reactor-core": self._managed("reactor-core"),
            "jarvis-prime": self._managed("jarvis-prime", depends_on=["reactor-core"]),
            "jarvis-body": self._managed("jarvis-body", depends_on=["jarvis-prime"]),
        }

        plan = orchestrator._build_failure_propagation_plan(
            "reactor-core",
            "process_crash",
            include_source_action=False,
        )
        by_target = {entry.target_service: entry for entry in plan}

        assert by_target["reactor-core"].action == FailurePropagationAction.NONE
        assert by_target["jarvis-prime"].action == FailurePropagationAction.DRAIN
        assert by_target["jarvis-body"].action == FailurePropagationAction.NONE

    def test_build_failure_plan_terminal_isolates_source_and_hard_dependents(self):
        from backend.supervisor.cross_repo_startup_orchestrator import (
            FailurePropagationAction,
            ProcessOrchestrator,
        )

        orchestrator = ProcessOrchestrator()
        orchestrator.processes = {
            "reactor-core": self._managed("reactor-core"),
            "jarvis-prime": self._managed("jarvis-prime", depends_on=["reactor-core"]),
            "jarvis-body": self._managed("jarvis-body", depends_on=["jarvis-prime"]),
        }

        plan = orchestrator._build_failure_propagation_plan(
            "reactor-core",
            "circuit_breaker_open",
            include_source_action=True,
        )
        by_target = {entry.target_service: entry for entry in plan}

        assert by_target["reactor-core"].action == FailurePropagationAction.ISOLATE
        assert by_target["jarvis-prime"].action == FailurePropagationAction.ISOLATE
        assert by_target["jarvis-body"].action == FailurePropagationAction.DEGRADE

    @pytest.mark.asyncio
    async def test_dependency_error_triggers_policy_for_managed_source(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        orchestrator.processes = {"jarvis-prime": self._managed("jarvis-prime")}

        with patch.object(
            orchestrator,
            "_trigger_failure_propagation_policy",
            new_callable=AsyncMock,
        ) as mock_trigger:
            await orchestrator._handle_dependency_error(
                {
                    "source_repo": "jarvis-prime",
                    "message": "dependency timeout",
                    "reason": "dependency_failed",
                }
            )

        mock_trigger.assert_awaited_once_with(
            "jarvis-prime",
            "dependency_failed",
            include_source_action=False,
            trigger="error_handler.dependency_error",
        )

    @pytest.mark.asyncio
    async def test_recovery_path_triggers_policy_before_auto_heal(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        orchestrator.processes = {"jarvis-prime": self._managed("jarvis-prime")}

        with patch.object(
            orchestrator,
            "_trigger_failure_propagation_policy",
            new_callable=AsyncMock,
        ) as mock_trigger:
            with patch.object(orchestrator, "_auto_heal", new_callable=AsyncMock, return_value=True) as mock_heal:
                result = await orchestrator._initiate_intelligent_recovery(
                    "jarvis-prime",
                    "process_dead",
                )

        assert result is True
        mock_trigger.assert_awaited_once_with(
            "jarvis-prime",
            "process_dead",
            include_source_action=False,
            trigger="recovery_coordinator",
        )
        mock_heal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent_restart_suppresses_duplicate_auto_heal(self):
        from backend.supervisor.cross_repo_startup_orchestrator import (
            FailurePropagationAction,
            FailurePropagationDirective,
            ProcessOrchestrator,
            ServiceStatus,
        )

        orchestrator = ProcessOrchestrator()
        managed = self._managed("jarvis-prime")
        managed.status = ServiceStatus.FAILED
        managed.is_running = False
        orchestrator.processes = {"jarvis-prime": managed}

        directive = FailurePropagationDirective(
            source_service="reactor-core",
            target_service="jarvis-prime",
            action=FailurePropagationAction.RESTART,
            reason="process_crash",
        )

        with patch.object(orchestrator, "_auto_heal", new_callable=AsyncMock, return_value=True) as mock_heal:
            first = await orchestrator._apply_failure_directive(directive)
            second = await orchestrator._apply_failure_directive(directive)

        assert first is True
        assert second is True
        mock_heal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent_drain_suppresses_duplicate_begin_drain(self):
        from backend.supervisor.cross_repo_startup_orchestrator import (
            FailurePropagationAction,
            FailurePropagationDirective,
            ProcessOrchestrator,
            ServiceStatus,
        )

        orchestrator = ProcessOrchestrator()
        managed = self._managed("jarvis-prime")
        managed.status = ServiceStatus.HEALTHY
        orchestrator.processes = {"jarvis-prime": managed}

        directive = FailurePropagationDirective(
            source_service="reactor-core",
            target_service="jarvis-prime",
            action=FailurePropagationAction.DRAIN,
            reason="dependency_failed",
        )

        guard = MagicMock()
        guard.begin_drain = AsyncMock()
        guard.get_stats = MagicMock(return_value={"draining_categories": ["jarvis-prime"]})

        with patch(
            "backend.core.resilience.graceful_shutdown.get_operation_guard",
            new=AsyncMock(return_value=guard),
        ):
            first = await orchestrator._apply_failure_directive(directive)
            second = await orchestrator._apply_failure_directive(directive)

        assert first is True
        assert second is True
        guard.begin_drain.assert_awaited_once_with("jarvis-prime")

    @pytest.mark.asyncio
    async def test_idempotent_state_marks_uncertain_on_cancellation(self):
        import asyncio
        from backend.supervisor.cross_repo_startup_orchestrator import (
            FailurePropagationAction,
            FailurePropagationDirective,
            ProcessOrchestrator,
            ServiceStatus,
        )

        orchestrator = ProcessOrchestrator()
        orchestrator._failure_effect_idempotency_inflight_s = 120.0
        managed = self._managed("jarvis-prime")
        managed.status = ServiceStatus.FAILED
        orchestrator.processes = {"jarvis-prime": managed}

        directive = FailurePropagationDirective(
            source_service="reactor-core",
            target_service="jarvis-prime",
            action=FailurePropagationAction.RESTART,
            reason="process_crash",
        )

        with patch.object(orchestrator, "_auto_heal", new_callable=AsyncMock, side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await orchestrator._apply_failure_directive(directive)

        key = orchestrator._failure_effect_idempotency_key(directive)
        assert orchestrator._failure_effect_idempotency_state[key]["status"] == "uncertain"

    @pytest.mark.asyncio
    async def test_failure_policy_journal_restore_reuses_successful_restart(
        self, tmp_path, monkeypatch
    ):
        from backend.supervisor.cross_repo_startup_orchestrator import (
            FailurePropagationAction,
            FailurePropagationDirective,
            ProcessOrchestrator,
            ServiceStatus,
        )

        journal_path = tmp_path / "failure_policy_journal.json"
        monkeypatch.setenv("JARVIS_FAILURE_POLICY_JOURNAL_PATH", str(journal_path))

        directive = FailurePropagationDirective(
            source_service="reactor-core",
            target_service="jarvis-prime",
            action=FailurePropagationAction.RESTART,
            reason="process_crash",
        )

        orchestrator_1 = ProcessOrchestrator()
        managed_1 = self._managed("jarvis-prime")
        managed_1.status = ServiceStatus.FAILED
        managed_1.is_running = False
        orchestrator_1.processes = {"jarvis-prime": managed_1}

        with patch.object(orchestrator_1, "_auto_heal", new_callable=AsyncMock, return_value=True) as heal_1:
            assert await orchestrator_1._apply_failure_directive(directive)
        heal_1.assert_awaited_once()
        assert journal_path.exists()

        orchestrator_2 = ProcessOrchestrator()
        managed_2 = self._managed("jarvis-prime")
        managed_2.status = ServiceStatus.FAILED
        managed_2.is_running = False
        orchestrator_2.processes = {"jarvis-prime": managed_2}
        await orchestrator_2._restore_failure_policy_state_if_needed()

        with patch.object(orchestrator_2, "_auto_heal", new_callable=AsyncMock, return_value=True) as heal_2:
            assert await orchestrator_2._apply_failure_directive(directive)
        heal_2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restore_replays_uncertain_isolate_intent(self, tmp_path, monkeypatch):
        import time
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator, ServiceStatus

        journal_path = tmp_path / "failure_policy_journal.json"
        monkeypatch.setenv("JARVIS_FAILURE_POLICY_JOURNAL_PATH", str(journal_path))

        key = "jarvis-prime|isolate"
        payload = {
            "schema_version": 1,
            "updated_at": time.time(),
            "idempotency_state": {
                key: {
                    "operation_id": "op-test-1",
                    "status": "uncertain",
                    "updated_at": time.time(),
                    "attempts": 1,
                    "source_service": "reactor-core",
                    "target_service": "jarvis-prime",
                    "action": "isolate",
                    "reason": "circuit_breaker_open",
                    "note": "local_timeout_or_cancelled",
                    "result": None,
                }
            },
            "history": [],
            "last_applied": {},
            "degradation_mode": {},
            "crash_circuit_breakers": {},
            "observed_snapshot": {},
        }
        journal_path.write_text(json.dumps(payload), encoding="utf-8")

        orchestrator = ProcessOrchestrator()
        managed = self._managed("jarvis-prime")
        managed.status = ServiceStatus.HEALTHY
        managed.is_running = False
        orchestrator.processes = {"jarvis-prime": managed}

        await orchestrator._restore_failure_policy_state_if_needed()

        assert orchestrator._degradation_mode.get("jarvis-prime") == "isolated"
        assert orchestrator._crash_circuit_breakers.get("jarvis-prime") is True
        assert orchestrator._failure_effect_idempotency_state[key]["status"] == "succeeded"


class TestOwnershipBoundaries:
    """Tests for cross-repo ownership boundary contract enforcement."""

    def test_ownership_contract_passes_for_canonical_definitions(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        issues = orchestrator._validate_ownership_boundaries()
        assert issues == []

    def test_ownership_contract_detects_responsibility_leakage(self):
        from copy import deepcopy
        import backend.supervisor.cross_repo_startup_orchestrator as orchestrator_mod
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        original = deepcopy(orchestrator_mod.ServiceDefinitionRegistry._CANONICAL_DEFINITIONS)
        try:
            orchestrator_mod.ServiceDefinitionRegistry._CANONICAL_DEFINITIONS["jarvis-prime"][
                "responsibility_domains"
            ] = [
                "model_registry",
                "capability_routing",
                "training",  # leakage from reactor-core domain
            ]
            orchestrator_mod.ServiceDefinitionRegistry._cache.clear()

            orchestrator = ProcessOrchestrator()
            issues = orchestrator._validate_ownership_boundaries()
            assert any("out-of-bound domains" in issue for issue in issues)
        finally:
            orchestrator_mod.ServiceDefinitionRegistry._CANONICAL_DEFINITIONS = original
            orchestrator_mod.ServiceDefinitionRegistry._cache.clear()

    @pytest.mark.asyncio
    async def test_initialize_cross_repo_integration_strict_ownership_violation_raises(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        orchestrator._ownership_contract_strict = True

        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator._emit_event",
            new=AsyncMock(),
        ):
            with patch.object(orchestrator, "_initialize_unified_config", new_callable=AsyncMock):
                with patch.object(orchestrator, "_initialize_security_context", new_callable=AsyncMock):
                    with patch.object(orchestrator, "_initialize_unified_logging", new_callable=AsyncMock):
                        with patch.object(orchestrator, "_initialize_error_propagation", new_callable=AsyncMock):
                            with patch.object(orchestrator, "_initialize_state_sync", new_callable=AsyncMock):
                                with patch.object(orchestrator, "_initialize_resource_coordination", new_callable=AsyncMock):
                                    with patch.object(orchestrator, "_initialize_version_compatibility", new_callable=AsyncMock):
                                        with patch.object(orchestrator, "_check_compatibility", new_callable=AsyncMock, return_value=[]):
                                            with patch.object(orchestrator, "_initialize_metrics_collection", new_callable=AsyncMock):
                                                with patch.object(
                                                    orchestrator,
                                                    "_validate_ownership_boundaries",
                                                    return_value=["jarvis-prime declares out-of-bound domains: ['training']"],
                                                ):
                                                    with pytest.raises(RuntimeError, match="ownership contract violation"):
                                                        await orchestrator._initialize_cross_repo_integration()


class TestGlobalAdmissionControl:
    """Tests for global admission/backpressure governance."""

    @pytest.mark.asyncio
    async def test_global_admission_denies_on_memory_pressure(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        orchestrator._global_admission_enabled = True
        orchestrator._global_admission_timeout_s = 0.01
        orchestrator._resource_limits = {
            "memory_mb": 16384,
            "gpu_memory_mb": 8192,
            "network_connections": 1000,
            "cpu_cores": 8,
        }

        with patch.object(
            orchestrator,
            "_collect_global_admission_snapshot",
            new_callable=AsyncMock,
            return_value={
                "memory_percent": 99.0,
                "available_mb": 256,
                "available_gb": 0.25,
                "network_connections": 100,
                "timestamp": 0.0,
            },
        ):
            allowed, reason, lease_id = await orchestrator._acquire_global_admission_lease(
                "jarvis-prime",
                "spawn",
                timeout_s=0.01,
            )

        assert allowed is False
        assert lease_id is None
        assert "memory_percent" in reason or "available_gb" in reason

    @pytest.mark.asyncio
    async def test_global_admission_grant_and_release_updates_allocations(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orchestrator = ProcessOrchestrator()
        orchestrator._global_admission_enabled = True
        orchestrator._resource_limits = {
            "memory_mb": 16384,
            "gpu_memory_mb": 8192,
            "network_connections": 1000,
            "cpu_cores": 8,
        }

        with patch.object(
            orchestrator,
            "_collect_global_admission_snapshot",
            new_callable=AsyncMock,
            return_value={
                "memory_percent": 50.0,
                "available_mb": 12000,
                "available_gb": 12.0,
                "network_connections": 10,
                "timestamp": 0.0,
            },
        ):
            allowed, reason, lease_id = await orchestrator._acquire_global_admission_lease(
                "jarvis-prime",
                "spawn",
                timeout_s=0.01,
            )

        assert allowed is True
        assert reason == "admitted"
        assert lease_id is not None
        assert "jarvis-prime" in orchestrator._resource_allocations
        assert lease_id in orchestrator._global_admission_active_leases

        await orchestrator._release_global_admission_lease(
            lease_id,
            outcome="test_complete",
        )
        assert lease_id not in orchestrator._global_admission_active_leases
        assert "jarvis-prime" not in orchestrator._resource_allocations

    @pytest.mark.asyncio
    async def test_spawn_service_inner_blocks_when_global_admission_denied(self):
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator, ServiceStatus

        orchestrator = ProcessOrchestrator()
        managed = TestFailurePropagationPolicy._managed("jarvis-prime")
        managed.status = ServiceStatus.STARTING

        coordinator = MagicMock()
        coordinator.should_attempt_spawn.return_value = (True, "ok")
        coordinator.mark_spawning.return_value = True
        coordinator.mark_ready.return_value = None
        coordinator.mark_failed.return_value = None
        coordinator.is_service_ready.return_value = False
        coordinator.is_spawn_in_progress.return_value = False
        coordinator.get_spawning_component.return_value = "none"

        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.get_spawn_coordinator",
            return_value=coordinator,
        ):
            with patch.object(
                orchestrator,
                "_acquire_global_admission_lease",
                new_callable=AsyncMock,
                return_value=(False, "memory pressure", None),
            ):
                with patch.object(orchestrator, "_spawn_service_core", new_callable=AsyncMock) as mock_core:
                    result = await orchestrator._spawn_service_inner(managed, managed.definition)

        assert result is False
        assert managed.status == ServiceStatus.DEGRADED
        mock_core.assert_not_awaited()
