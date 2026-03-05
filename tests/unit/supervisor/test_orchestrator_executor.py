"""Tests for ProcessOrchestrator VerdictExecutor adapter methods (Task 14).

These tests validate that the VerdictExecutor interface methods added to
ProcessOrchestrator can be imported and behave correctly for basic
scenarios (unknown subsystems, stale identity, etc.).

The tests avoid full ProcessOrchestrator initialization by using
``__new__`` and manually setting only the attributes needed for each test.
"""
import asyncio
import os
import signal
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.root_authority_types import ProcessIdentity, ExecutionResult


class TestOrchestratorExecutorMethodsExist:
    """Verify the VerdictExecutor methods are present on ProcessOrchestrator."""

    def test_verdict_executor_methods_exist(self):
        """ProcessOrchestrator has all VerdictExecutor methods."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        assert hasattr(ProcessOrchestrator, "execute_drain")
        assert hasattr(ProcessOrchestrator, "execute_term")
        assert hasattr(ProcessOrchestrator, "execute_group_kill")
        assert hasattr(ProcessOrchestrator, "execute_restart")
        assert hasattr(ProcessOrchestrator, "get_current_identity")
        assert hasattr(ProcessOrchestrator, "set_verdict_executor_mode")

    def test_set_verdict_executor_mode(self):
        """set_verdict_executor_mode toggles the flag."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch.processes = {}
        orch.set_verdict_executor_mode(True)
        assert orch._verdict_executor_mode is True
        orch.set_verdict_executor_mode(False)
        assert orch._verdict_executor_mode is False


class TestGetCurrentIdentity:
    """Tests for get_current_identity."""

    def test_returns_none_for_unknown_subsystem(self):
        """get_current_identity returns None for unknown subsystem."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch.processes = {}
        result = orch.get_current_identity("nonexistent")
        assert result is None

    def test_returns_none_when_process_is_none(self):
        """get_current_identity returns None when ManagedProcess.process is None."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
            ServiceDefinition,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = None
        mp.definition = MagicMock(name="test-svc")
        orch.processes = {"test-svc": mp}
        result = orch.get_current_identity("test-svc")
        assert result is None

    def test_returns_identity_for_running_process(self):
        """get_current_identity returns a ProcessIdentity for a running process."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 12345
        mp.start_time = 1000.0
        mp.definition = MagicMock()
        mp.definition.name = "test-svc"
        orch.processes = {"test-svc": mp}

        with patch.dict(os.environ, {"JARVIS_ROOT_SESSION_ID": "test-session"}):
            result = orch.get_current_identity("test-svc")

        assert result is not None
        assert isinstance(result, ProcessIdentity)
        assert result.pid == 12345
        assert result.session_id == "test-session"


class TestExecuteDrain:
    """Tests for execute_drain."""

    @pytest.mark.asyncio
    async def test_not_found(self):
        """execute_drain returns not_found for unknown subsystem."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch.processes = {}
        identity = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        result = await orch.execute_drain("nonexistent", identity, 5.0)
        assert not result.accepted
        assert result.error_code == "subsystem_not_found"

    @pytest.mark.asyncio
    async def test_stale_identity(self):
        """execute_drain rejects stale identity."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 999
        mp.start_time = 1000.0
        mp.port = 8080
        mp.definition = MagicMock()
        mp.definition.name = "test-svc"
        orch.processes = {"test-svc": mp}

        stale = ProcessIdentity(pid=1, start_time_ns=0, session_id="old", exec_fingerprint="old")
        result = await orch.execute_drain("test-svc", stale, 5.0)
        assert not result.accepted
        assert result.error_code == "stale_identity"

    @pytest.mark.asyncio
    async def test_no_port(self):
        """execute_drain returns error when port is None."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 999
        mp.start_time = 1000.0
        mp.port = None
        mp.definition = MagicMock()
        mp.definition.name = "test-svc"
        orch.processes = {"test-svc": mp}

        # Build a matching identity
        identity = orch._build_process_identity(mp)
        result = await orch.execute_drain("test-svc", identity, 5.0)
        assert result.accepted
        assert not result.executed
        assert result.error_code == "no_port"


class TestExecuteTerm:
    """Tests for execute_term."""

    @pytest.mark.asyncio
    async def test_not_found(self):
        """execute_term returns not_found for unknown subsystem."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch.processes = {}
        identity = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        result = await orch.execute_term("nonexistent", identity, 5.0)
        assert not result.accepted
        assert result.error_code == "subsystem_not_found"

    @pytest.mark.asyncio
    async def test_stale_identity(self):
        """execute_term rejects stale identity."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 999
        mp.start_time = 1000.0
        mp.definition = MagicMock()
        mp.definition.name = "test-svc"
        orch.processes = {"test-svc": mp}

        stale = ProcessIdentity(pid=1, start_time_ns=0, session_id="old", exec_fingerprint="old")
        result = await orch.execute_term("test-svc", stale, 5.0)
        assert not result.accepted
        assert result.error_code == "stale_identity"

    @pytest.mark.asyncio
    async def test_process_lookup_error(self):
        """execute_term treats ProcessLookupError as success (already dead)."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 999
        mp.process.send_signal.side_effect = ProcessLookupError
        mp.start_time = 1000.0
        mp.definition = MagicMock()
        mp.definition.name = "test-svc"
        orch.processes = {"test-svc": mp}

        identity = orch._build_process_identity(mp)
        result = await orch.execute_term("test-svc", identity, 5.0)
        assert result.accepted
        assert result.executed
        assert result.result == "success"


class TestExecuteGroupKill:
    """Tests for execute_group_kill."""

    @pytest.mark.asyncio
    async def test_not_found(self):
        """execute_group_kill returns not_found for unknown subsystem."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch.processes = {}
        identity = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        result = await orch.execute_group_kill("nonexistent", identity)
        assert not result.accepted
        assert result.error_code == "subsystem_not_found"

    @pytest.mark.asyncio
    async def test_process_lookup_error(self):
        """execute_group_kill treats ProcessLookupError as success."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 999
        mp.start_time = 1000.0
        mp.definition = MagicMock()
        mp.definition.name = "test-svc"
        orch.processes = {"test-svc": mp}

        identity = orch._build_process_identity(mp)
        with patch("os.getpgid", side_effect=ProcessLookupError):
            result = await orch.execute_group_kill("test-svc", identity)
        assert result.accepted
        assert result.executed
        assert result.result == "success"

    @pytest.mark.asyncio
    async def test_kills_process_group(self):
        """execute_group_kill sends SIGKILL to the process group."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 999
        mp.start_time = 1000.0
        mp.definition = MagicMock()
        mp.definition.name = "test-svc"
        orch.processes = {"test-svc": mp}

        identity = orch._build_process_identity(mp)
        with patch("os.getpgid", return_value=999) as mock_getpgid, \
             patch("os.killpg") as mock_killpg:
            result = await orch.execute_group_kill("test-svc", identity)

        mock_getpgid.assert_called_once_with(999)
        mock_killpg.assert_called_once_with(999, signal.SIGKILL)
        assert result.accepted
        assert result.executed
        assert result.result == "success"


class TestExecuteRestart:
    """Tests for execute_restart."""

    @pytest.mark.asyncio
    async def test_not_found(self):
        """execute_restart returns not_found for unknown subsystem."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch.processes = {}
        result = await orch.execute_restart("nonexistent", 0.0)
        assert not result.accepted
        assert result.error_code == "subsystem_not_found"


class TestBuildProcessIdentity:
    """Tests for the _build_process_identity helper."""

    def test_builds_identity(self):
        """_build_process_identity returns a valid ProcessIdentity."""
        from backend.supervisor.cross_repo_startup_orchestrator import (
            ProcessOrchestrator,
            ManagedProcess,
        )

        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        mp = ManagedProcess.__new__(ManagedProcess)
        mp.process = MagicMock()
        mp.process.pid = 42
        mp.start_time = 500.0
        mp.definition = MagicMock()
        mp.definition.name = "my-service"

        with patch.dict(os.environ, {"JARVIS_ROOT_SESSION_ID": "sess-1"}):
            identity = orch._build_process_identity(mp)

        assert identity.pid == 42
        assert identity.start_time_ns == int(500.0 * 1_000_000_000)
        assert identity.session_id == "sess-1"
        assert identity.exec_fingerprint.startswith("sha256:")
