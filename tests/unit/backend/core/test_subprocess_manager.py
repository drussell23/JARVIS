"""
Tests for SubprocessManager - Lifecycle management for cross-repo subprocesses.

Tests cover:
- ProcessState enum values
- ProcessHandle dataclass and properties
- ProcessConfig dataclass
- SubprocessManager.start() creates subprocess
- SubprocessManager.stop() with graceful shutdown
- SubprocessManager.stop() with SIGKILL escalation (mock)
- SubprocessManager.restart() with exponential backoff
- SubprocessManager.shutdown_all() in reverse order
- _resolve_repo_path() expands env vars
- _build_child_env() includes required vars
- _detect_log_level() classification
- is_running() check
- Output streaming (mock streams)
"""
import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from backend.core.subprocess_manager import (
    ProcessState,
    ProcessHandle,
    ProcessConfig,
    SubprocessManager,
    get_subprocess_manager,
)
from backend.core.component_registry import (
    ComponentRegistry,
    ComponentDefinition,
    Criticality,
    ProcessType,
)
from backend.core.recovery_engine import (
    RecoveryEngine,
    RecoveryPhase,
    ErrorClassifier,
)


class TestProcessStateEnum:
    """Test ProcessState enum values."""

    def test_pending_value(self):
        assert ProcessState.PENDING.value == "pending"

    def test_starting_value(self):
        assert ProcessState.STARTING.value == "starting"

    def test_running_value(self):
        assert ProcessState.RUNNING.value == "running"

    def test_stopping_value(self):
        assert ProcessState.STOPPING.value == "stopping"

    def test_stopped_value(self):
        assert ProcessState.STOPPED.value == "stopped"

    def test_crashed_value(self):
        assert ProcessState.CRASHED.value == "crashed"

    def test_all_enum_members(self):
        """Verify all expected enum members exist."""
        members = [e.name for e in ProcessState]
        assert "PENDING" in members
        assert "STARTING" in members
        assert "RUNNING" in members
        assert "STOPPING" in members
        assert "STOPPED" in members
        assert "CRASHED" in members


class TestProcessHandle:
    """Test ProcessHandle dataclass and properties."""

    @pytest.fixture
    def mock_process(self):
        """Create a mock asyncio subprocess."""
        process = MagicMock()
        process.returncode = None  # Process is running
        process.pid = 12345
        return process

    @pytest.fixture
    def mock_component(self):
        """Create a mock component definition."""
        return ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )

    def test_create_handle(self, mock_process, mock_component):
        """Test creating a ProcessHandle."""
        handle = ProcessHandle(
            name="test-component",
            process=mock_process,
            component=mock_component,
        )
        assert handle.name == "test-component"
        assert handle.process == mock_process
        assert handle.component == mock_component
        assert handle.state == ProcessState.PENDING
        assert handle.started_at is None
        assert handle.pid is None
        assert handle.restart_count == 0
        assert handle.last_health_check is None

    def test_handle_with_all_fields(self, mock_process, mock_component):
        """Test ProcessHandle with all fields set."""
        now = datetime.now()
        handle = ProcessHandle(
            name="test-component",
            process=mock_process,
            component=mock_component,
            state=ProcessState.RUNNING,
            started_at=now,
            pid=12345,
            restart_count=2,
            last_health_check=now,
        )
        assert handle.state == ProcessState.RUNNING
        assert handle.started_at == now
        assert handle.pid == 12345
        assert handle.restart_count == 2
        assert handle.last_health_check == now

    def test_is_alive_when_running(self, mock_process, mock_component):
        """Test is_alive returns True when process is running."""
        mock_process.returncode = None
        handle = ProcessHandle(
            name="test-component",
            process=mock_process,
            component=mock_component,
        )
        assert handle.is_alive is True

    def test_is_alive_when_stopped(self, mock_process, mock_component):
        """Test is_alive returns False when process has stopped."""
        mock_process.returncode = 0
        handle = ProcessHandle(
            name="test-component",
            process=mock_process,
            component=mock_component,
        )
        assert handle.is_alive is False

    def test_is_alive_when_crashed(self, mock_process, mock_component):
        """Test is_alive returns False when process crashed."""
        mock_process.returncode = 1
        handle = ProcessHandle(
            name="test-component",
            process=mock_process,
            component=mock_component,
        )
        assert handle.is_alive is False


class TestProcessConfig:
    """Test ProcessConfig dataclass."""

    def test_create_minimal_config(self):
        """Test creating ProcessConfig with minimal fields."""
        config = ProcessConfig(
            working_dir="/path/to/repo",
            command=["python", "main.py"],
        )
        assert config.working_dir == "/path/to/repo"
        assert config.command == ["python", "main.py"]
        assert config.env == {}
        assert config.stdout_handler is None
        assert config.stderr_handler is None

    def test_create_config_with_env(self):
        """Test ProcessConfig with environment variables."""
        config = ProcessConfig(
            working_dir="/path/to/repo",
            command=["python", "main.py"],
            env={"PYTHONPATH": "/custom/path"},
        )
        assert config.env == {"PYTHONPATH": "/custom/path"}

    def test_create_config_with_handlers(self):
        """Test ProcessConfig with output handlers."""
        stdout_handler = MagicMock()
        stderr_handler = MagicMock()

        config = ProcessConfig(
            working_dir="/path/to/repo",
            command=["python", "main.py"],
            stdout_handler=stdout_handler,
            stderr_handler=stderr_handler,
        )
        assert config.stdout_handler == stdout_handler
        assert config.stderr_handler == stderr_handler


class TestSubprocessManager:
    """Test SubprocessManager class."""

    @pytest.fixture
    def registry(self):
        """Create a clean ComponentRegistry."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return registry

    @pytest.fixture
    def recovery_engine(self, registry):
        """Create a RecoveryEngine."""
        return RecoveryEngine(registry, ErrorClassifier())

    @pytest.fixture
    def manager(self, registry, recovery_engine):
        """Create a SubprocessManager."""
        return SubprocessManager(registry, recovery_engine)

    @pytest.fixture
    def component(self, registry):
        """Create and register a test component."""
        definition = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
            repo_path="${HOME}/repos/test-component",
        )
        registry.register(definition)
        return definition

    def test_init(self, registry, recovery_engine):
        """Test SubprocessManager initialization."""
        manager = SubprocessManager(registry, recovery_engine)
        assert manager.registry == registry
        assert manager.recovery_engine == recovery_engine
        assert manager._handles == {}
        assert manager._output_tasks == {}
        assert manager._monitor_tasks == {}
        assert not manager._shutdown_event.is_set()

    def test_init_without_recovery_engine(self, registry):
        """Test SubprocessManager without recovery engine."""
        manager = SubprocessManager(registry)
        assert manager.registry == registry
        assert manager.recovery_engine is None

    @pytest.mark.asyncio
    async def test_start_creates_subprocess(self, manager, component):
        """Test that start() creates a subprocess."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    handle = await manager.start(component)

        assert handle is not None
        assert handle.name == "test-component"
        assert handle.pid == 12345
        assert handle.state == ProcessState.RUNNING
        assert handle.started_at is not None
        assert "test-component" in manager._handles

    @pytest.mark.asyncio
    async def test_start_returns_existing_handle_if_alive(self, manager, component):
        """Test that start() returns existing handle if process is alive."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    handle1 = await manager.start(component)
                    handle2 = await manager.start(component)

        assert handle1 is handle2

    @pytest.mark.asyncio
    async def test_stop_graceful_shutdown(self, manager, component):
        """Test stop() with graceful shutdown."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

        result = await manager.stop("test-component")

        assert result is True
        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_called()
        assert manager._handles["test-component"].state == ProcessState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_sigkill_escalation(self, manager, component):
        """Test stop() with SIGKILL escalation on timeout."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

        result = await manager.stop("test-component", graceful_timeout=0.1)

        assert result is True
        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_nonexistent_component(self, manager):
        """Test stop() for a component that doesn't exist."""
        result = await manager.stop("nonexistent-component")
        assert result is True  # Should succeed silently

    @pytest.mark.asyncio
    async def test_stop_already_dead_process(self, manager, component):
        """Test stop() when process is already dead."""
        mock_process = AsyncMock()
        mock_process.returncode = 0  # Already stopped
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

        result = await manager.stop("test-component")
        assert result is True

    @pytest.mark.asyncio
    async def test_restart_with_backoff(self, manager, component):
        """Test restart() with exponential backoff."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

                    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                        handle = await manager.restart("test-component")
                        # First restart: 2^1 = 2 seconds (capped at 30)
                        mock_sleep.assert_called()

        assert handle is not None
        assert handle.restart_count == 1

    @pytest.mark.asyncio
    async def test_restart_increments_count(self, manager, component):
        """Test that restart() increments restart_count."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        await manager.restart("test-component")
                        handle = await manager.restart("test-component")

        assert handle.restart_count == 2

    @pytest.mark.asyncio
    async def test_restart_nonexistent_component(self, manager):
        """Test restart() for a component that doesn't exist."""
        result = await manager.restart("nonexistent-component")
        assert result is None

    @pytest.mark.asyncio
    async def test_shutdown_all_reverse_order(self, registry, recovery_engine):
        """Test shutdown_all() stops components in reverse order."""
        manager = SubprocessManager(registry, recovery_engine)

        # Register multiple components
        comp1 = ComponentDefinition(
            name="component-1",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )
        comp2 = ComponentDefinition(
            name="component-2",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )
        registry.register(comp1)
        registry.register(comp2)

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()

        stop_order = []

        async def track_stop(name, *args, **kwargs):
            stop_order.append(name)
            return True

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(comp1)
                    await manager.start(comp2)

        with patch.object(manager, "stop", side_effect=track_stop):
            await manager.shutdown_all(reverse_order=True)

        assert stop_order == ["component-2", "component-1"]

    @pytest.mark.asyncio
    async def test_shutdown_all_sets_event(self, manager):
        """Test that shutdown_all() sets the shutdown event."""
        await manager.shutdown_all()
        assert manager._shutdown_event.is_set()


class TestResolveRepoPath:
    """Test _resolve_repo_path() method."""

    @pytest.fixture
    def manager(self):
        """Create a SubprocessManager."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return SubprocessManager(registry)

    def test_resolve_empty_path(self, manager):
        """Test resolving empty path returns cwd."""
        result = manager._resolve_repo_path("")
        assert result == os.getcwd()

    def test_resolve_path_with_env_var(self, manager):
        """Test resolving path with environment variable."""
        with patch.dict(os.environ, {"HOME": "/home/testuser"}):
            result = manager._resolve_repo_path("${HOME}/repos/test")
        assert result == "/home/testuser/repos/test"

    def test_resolve_path_with_tilde(self, manager):
        """Test resolving path with tilde."""
        result = manager._resolve_repo_path("~/repos/test")
        expected = os.path.expanduser("~/repos/test")
        assert result == expected

    def test_resolve_path_without_env_var(self, manager):
        """Test resolving path without environment variables."""
        result = manager._resolve_repo_path("/absolute/path/to/repo")
        assert result == "/absolute/path/to/repo"


class TestBuildChildEnv:
    """Test _build_child_env() method."""

    @pytest.fixture
    def manager(self):
        """Create a SubprocessManager."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return SubprocessManager(registry)

    @pytest.fixture
    def component(self):
        """Create a test component."""
        return ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )

    def test_build_env_includes_pythonpath(self, manager, component):
        """Test that PYTHONPATH is included if set."""
        with patch.dict(os.environ, {"PYTHONPATH": "/custom/path"}, clear=False):
            env = manager._build_child_env(component)
        assert "PYTHONPATH" in env

    def test_build_env_includes_path(self, manager, component):
        """Test that PATH is included if set."""
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
            env = manager._build_child_env(component)
        assert "PATH" in env

    def test_build_env_includes_home(self, manager, component):
        """Test that HOME is included if set."""
        with patch.dict(os.environ, {"HOME": "/home/user"}, clear=False):
            env = manager._build_child_env(component)
        assert "HOME" in env

    def test_build_env_includes_user(self, manager, component):
        """Test that USER is included if set."""
        with patch.dict(os.environ, {"USER": "testuser"}, clear=False):
            env = manager._build_child_env(component)
        assert "USER" in env

    def test_build_env_includes_component_marker(self, manager, component):
        """Test that component-specific marker is included."""
        env = manager._build_child_env(component)
        assert "TEST_COMPONENT_CHILD_PROCESS" in env
        assert env["TEST_COMPONENT_CHILD_PROCESS"] == "1"

    def test_build_env_normalizes_component_name(self, manager):
        """Test that component name is normalized in env var."""
        component = ComponentDefinition(
            name="jarvis-prime",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )
        env = manager._build_child_env(component)
        assert "JARVIS_PRIME_CHILD_PROCESS" in env


class TestDetectLogLevel:
    """Test _detect_log_level() method."""

    @pytest.fixture
    def manager(self):
        """Create a SubprocessManager."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return SubprocessManager(registry)

    def test_detect_error_with_colon(self, manager):
        """Test detecting ERROR: pattern."""
        assert manager._detect_log_level("ERROR: Something went wrong") == "error"

    def test_detect_error_with_brackets(self, manager):
        """Test detecting [ERROR] pattern."""
        assert manager._detect_log_level("[ERROR] Something went wrong") == "error"

    def test_detect_critical(self, manager):
        """Test detecting CRITICAL: pattern."""
        assert manager._detect_log_level("CRITICAL: Fatal error") == "error"

    def test_detect_warning_with_colon(self, manager):
        """Test detecting WARNING: pattern."""
        assert manager._detect_log_level("WARNING: This is a warning") == "warning"

    def test_detect_warning_with_brackets(self, manager):
        """Test detecting [WARNING] pattern."""
        assert manager._detect_log_level("[WARNING] This is a warning") == "warning"

    def test_detect_warn(self, manager):
        """Test detecting WARN: pattern."""
        assert manager._detect_log_level("WARN: This is a warning") == "warning"

    def test_detect_debug_with_colon(self, manager):
        """Test detecting DEBUG: pattern."""
        assert manager._detect_log_level("DEBUG: Debug message") == "debug"

    def test_detect_debug_with_brackets(self, manager):
        """Test detecting [DEBUG] pattern."""
        assert manager._detect_log_level("[DEBUG] Debug message") == "debug"

    def test_detect_info_default(self, manager):
        """Test that unmatched lines return info."""
        assert manager._detect_log_level("Some regular log message") == "info"

    def test_detect_case_insensitive(self, manager):
        """Test that detection is case insensitive."""
        assert manager._detect_log_level("error: lowercase error") == "error"
        assert manager._detect_log_level("warning: lowercase warning") == "warning"


class TestIsRunning:
    """Test is_running() method."""

    @pytest.fixture
    def registry(self):
        """Create a clean ComponentRegistry."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return registry

    @pytest.fixture
    def manager(self, registry):
        """Create a SubprocessManager."""
        return SubprocessManager(registry)

    @pytest.fixture
    def component(self, registry):
        """Create and register a test component."""
        definition = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )
        registry.register(definition)
        return definition

    def test_is_running_when_not_started(self, manager):
        """Test is_running() returns False when component not started."""
        assert manager.is_running("test-component") is False

    @pytest.mark.asyncio
    async def test_is_running_when_running(self, manager, component):
        """Test is_running() returns True when process is running."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

        assert manager.is_running("test-component") is True

    @pytest.mark.asyncio
    async def test_is_running_when_stopped(self, manager, component):
        """Test is_running() returns False when process stopped."""
        mock_process = AsyncMock()
        mock_process.returncode = 0  # Stopped
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

        assert manager.is_running("test-component") is False


class TestGetHandle:
    """Test get_handle() method."""

    @pytest.fixture
    def registry(self):
        """Create a clean ComponentRegistry."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return registry

    @pytest.fixture
    def manager(self, registry):
        """Create a SubprocessManager."""
        return SubprocessManager(registry)

    @pytest.fixture
    def component(self, registry):
        """Create and register a test component."""
        definition = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )
        registry.register(definition)
        return definition

    def test_get_handle_when_not_started(self, manager):
        """Test get_handle() returns None when component not started."""
        assert manager.get_handle("test-component") is None

    @pytest.mark.asyncio
    async def test_get_handle_when_started(self, manager, component):
        """Test get_handle() returns handle when component started."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(manager, "_stream_output", new_callable=AsyncMock):
                with patch.object(manager, "_monitor_health", new_callable=AsyncMock):
                    await manager.start(component)

        handle = manager.get_handle("test-component")
        assert handle is not None
        assert handle.name == "test-component"


class TestStreamOutput:
    """Test output streaming functionality."""

    @pytest.fixture
    def registry(self):
        """Create a clean ComponentRegistry."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return registry

    @pytest.fixture
    def manager(self, registry):
        """Create a SubprocessManager."""
        return SubprocessManager(registry)

    @pytest.mark.asyncio
    async def test_stream_output_reads_stdout(self, manager, registry):
        """Test that _stream_output reads from stdout."""
        component = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )
        registry.register(component)

        # Create mock streams that return lines then EOF
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(
            side_effect=[b"INFO: Test message\n", b""]
        )
        mock_stderr = AsyncMock()
        mock_stderr.readline = AsyncMock(side_effect=[b""])

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.stdout = mock_stdout
        mock_process.stderr = mock_stderr

        handle = ProcessHandle(
            name="test-component",
            process=mock_process,
            component=component,
            state=ProcessState.RUNNING,
        )

        # Run stream output briefly
        task = asyncio.create_task(manager._stream_output(handle))
        await asyncio.sleep(0.1)
        manager._shutdown_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestMonitorHealth:
    """Test health monitoring functionality."""

    @pytest.fixture
    def registry(self):
        """Create a clean ComponentRegistry."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return registry

    @pytest.fixture
    def recovery_engine(self, registry):
        """Create a RecoveryEngine."""
        return RecoveryEngine(registry, ErrorClassifier())

    @pytest.fixture
    def manager(self, registry, recovery_engine):
        """Create a SubprocessManager."""
        return SubprocessManager(registry, recovery_engine)

    @pytest.mark.asyncio
    async def test_monitor_detects_crash(self, manager, registry):
        """Test that _monitor_health detects crashed process."""
        component = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
        )
        registry.register(component)

        mock_process = MagicMock()
        # Start alive, then crash
        mock_process.returncode = None
        mock_process.pid = 12345

        handle = ProcessHandle(
            name="test-component",
            process=mock_process,
            component=component,
            state=ProcessState.RUNNING,
        )
        manager._handles["test-component"] = handle

        # Simulate crash during monitoring
        async def simulate_crash():
            await asyncio.sleep(0.05)
            mock_process.returncode = 1

        with patch.object(
            manager.recovery_engine, "handle_failure", new_callable=AsyncMock
        ) as mock_failure:
            task = asyncio.create_task(manager._monitor_health(handle))
            await simulate_crash()
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class TestGetSubprocessManager:
    """Test factory function."""

    def test_create_manager(self):
        """Test get_subprocess_manager factory function."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        recovery_engine = RecoveryEngine(registry, ErrorClassifier())

        manager = get_subprocess_manager(registry, recovery_engine)

        assert isinstance(manager, SubprocessManager)
        assert manager.registry == registry
        assert manager.recovery_engine == recovery_engine

    def test_create_manager_without_recovery(self):
        """Test get_subprocess_manager without recovery engine."""
        registry = ComponentRegistry()
        registry._reset_for_testing()

        manager = get_subprocess_manager(registry)

        assert isinstance(manager, SubprocessManager)
        assert manager.recovery_engine is None


class TestBuildConfig:
    """Test _build_config() method."""

    @pytest.fixture
    def registry(self):
        """Create a clean ComponentRegistry."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return registry

    @pytest.fixture
    def manager(self, registry):
        """Create a SubprocessManager."""
        return SubprocessManager(registry)

    def test_build_config_returns_process_config(self, manager, registry):
        """Test that _build_config returns a ProcessConfig."""
        component = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.SUBPROCESS,
            repo_path="/path/to/repo",
        )
        registry.register(component)

        config = manager._build_config(component)

        assert isinstance(config, ProcessConfig)
        assert config.working_dir == "/path/to/repo"
        assert len(config.command) >= 1


class TestFindPython:
    """Test _find_python() method."""

    @pytest.fixture
    def manager(self):
        """Create a SubprocessManager."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return SubprocessManager(registry)

    def test_find_python_returns_path(self, manager):
        """Test that _find_python returns a valid Python path."""
        python_path = manager._find_python()
        assert python_path is not None
        assert len(python_path) > 0

    def test_find_python_prefers_venv(self, manager):
        """Test that _find_python prefers venv Python if available."""
        venv_python = os.path.join(os.getcwd(), "venv", "bin", "python")

        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = True
            python_path = manager._find_python()

        if os.path.exists(venv_python):
            assert python_path == venv_python

    def test_find_python_falls_back_to_system(self, manager):
        """Test that _find_python falls back to system Python."""
        with patch("os.path.exists", return_value=False):
            python_path = manager._find_python()
        assert python_path == sys.executable


class TestGetStartupCommand:
    """Test _get_startup_command() method."""

    @pytest.fixture
    def manager(self):
        """Create a SubprocessManager."""
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return SubprocessManager(registry)

    def test_get_command_for_jarvis_prime(self, manager):
        """Test startup command for jarvis-prime component."""
        command = manager._get_startup_command("jarvis-prime", "/path/to/repo")
        assert len(command) == 2
        assert "run.py" in command[1]

    def test_get_command_for_reactor_core(self, manager):
        """Test startup command for reactor-core component."""
        command = manager._get_startup_command("reactor-core", "/path/to/repo")
        assert len(command) == 2
        assert "main.py" in command[1]

    def test_get_command_for_unknown_component(self, manager):
        """Test startup command defaults to main.py for unknown components."""
        command = manager._get_startup_command("unknown-component", "/path/to/repo")
        assert len(command) == 2
        assert "main.py" in command[1]
