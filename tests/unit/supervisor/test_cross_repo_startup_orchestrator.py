# tests/unit/supervisor/test_cross_repo_startup_orchestrator.py
"""
Tests for ProcessOrchestrator.startup_lock_context() method.

TDD approach for Pillar 1: Lock-Guarded Single-Owner Startup.
"""
import pytest
import json
import os
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
