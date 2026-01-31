"""
SubprocessManager - Manages lifecycle of cross-repo subprocesses.

This module provides:
- ProcessState: Enum for subprocess states
- ProcessHandle: Handle for a managed subprocess
- ProcessConfig: Configuration for subprocess startup
- SubprocessManager: Main manager class for subprocess lifecycle

Features:
- Async subprocess management
- Output streaming with log level detection
- Health monitoring
- Graceful shutdown with SIGTERM -> SIGKILL escalation
- Restart on crash with exponential backoff
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional

from backend.core.component_registry import (
    ComponentDefinition,
    ComponentRegistry,
)
from backend.core.recovery_engine import (
    RecoveryEngine,
    RecoveryPhase,
)

logger = logging.getLogger("jarvis.subprocess_manager")


class ProcessState(Enum):
    """State of a managed subprocess."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"


@dataclass
class ProcessHandle:
    """
    Handle for a managed subprocess.

    Provides access to the underlying asyncio subprocess and tracks
    lifecycle metadata such as state, start time, and restart count.

    Attributes:
        name: Component name this handle represents
        process: The underlying asyncio subprocess
        component: ComponentDefinition for this subprocess
        state: Current state of the subprocess
        started_at: When the subprocess was started
        pid: Process ID of the subprocess
        restart_count: Number of times this subprocess has been restarted
        last_health_check: When the last health check was performed
    """

    name: str
    process: asyncio.subprocess.Process
    component: ComponentDefinition
    state: ProcessState = ProcessState.PENDING
    started_at: Optional[datetime] = None
    pid: Optional[int] = None
    restart_count: int = 0
    last_health_check: Optional[datetime] = None

    @property
    def is_alive(self) -> bool:
        """Check if the subprocess is still running."""
        return self.process.returncode is None


@dataclass
class ProcessConfig:
    """
    Configuration for subprocess startup.

    Contains all settings needed to start a subprocess including
    working directory, command, environment, and output handlers.

    Attributes:
        working_dir: Working directory for the subprocess
        command: Command and arguments to execute
        env: Environment variables to set
        stdout_handler: Optional callback for stdout lines
        stderr_handler: Optional callback for stderr lines
    """

    working_dir: str
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    stdout_handler: Optional[Callable[[str], None]] = None
    stderr_handler: Optional[Callable[[str], None]] = None


class SubprocessManager:
    """
    Manages lifecycle of cross-repo subprocesses.

    Provides async subprocess management with:
    - Output streaming with log level detection
    - Health monitoring
    - Graceful shutdown with SIGTERM -> SIGKILL escalation
    - Restart on crash with exponential backoff

    Attributes:
        registry: ComponentRegistry for component lookups
        recovery_engine: Optional RecoveryEngine for failure handling
    """

    def __init__(
        self,
        registry: ComponentRegistry,
        recovery_engine: Optional[RecoveryEngine] = None,
    ):
        """
        Initialize the SubprocessManager.

        Args:
            registry: ComponentRegistry containing component definitions
            recovery_engine: Optional RecoveryEngine for handling failures
        """
        self.registry = registry
        self.recovery_engine = recovery_engine
        self._handles: Dict[str, ProcessHandle] = {}
        self._output_tasks: Dict[str, asyncio.Task] = {}
        self._monitor_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown_event = asyncio.Event()

    async def start(self, component: ComponentDefinition) -> ProcessHandle:
        """
        Start a subprocess for a component.

        Resolves repo path, builds environment, and starts the process.
        If the component is already running, returns the existing handle.

        Args:
            component: ComponentDefinition to start

        Returns:
            ProcessHandle for the started subprocess
        """
        # Return existing handle if process is alive
        if component.name in self._handles:
            existing = self._handles[component.name]
            if existing.is_alive:
                logger.debug(f"Subprocess {component.name} already running")
                return existing

        # Build configuration
        config = self._build_config(component)

        logger.info(f"Starting subprocess {component.name} in {config.working_dir}")
        logger.debug(f"Command: {' '.join(config.command)}")

        # Create the subprocess
        process = await asyncio.create_subprocess_exec(
            *config.command,
            cwd=config.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **config.env},
        )

        # Create the handle
        handle = ProcessHandle(
            name=component.name,
            process=process,
            component=component,
            state=ProcessState.RUNNING,
            started_at=datetime.now(),
            pid=process.pid,
        )

        self._handles[component.name] = handle

        # Start output streaming task
        self._output_tasks[component.name] = asyncio.create_task(
            self._stream_output(handle)
        )

        # Start health monitoring task
        self._monitor_tasks[component.name] = asyncio.create_task(
            self._monitor_health(handle)
        )

        logger.info(f"Started subprocess {component.name} (PID {process.pid})")
        return handle

    async def stop(
        self,
        component_name: str,
        graceful_timeout: float = 10.0,
    ) -> bool:
        """
        Stop a subprocess gracefully.

        First sends SIGTERM and waits for graceful shutdown. If the process
        doesn't stop within the timeout, sends SIGKILL.

        Args:
            component_name: Name of the component to stop
            graceful_timeout: Seconds to wait before SIGKILL

        Returns:
            True if stop was successful
        """
        if component_name not in self._handles:
            return True

        handle = self._handles[component_name]
        if not handle.is_alive:
            return True

        handle.state = ProcessState.STOPPING
        logger.info(f"Stopping subprocess {component_name} (PID {handle.pid})")

        # Cancel monitoring task
        if component_name in self._monitor_tasks:
            self._monitor_tasks[component_name].cancel()
            try:
                await self._monitor_tasks[component_name]
            except asyncio.CancelledError:
                pass

        # Send SIGTERM
        try:
            handle.process.terminate()

            try:
                await asyncio.wait_for(
                    handle.process.wait(),
                    timeout=graceful_timeout,
                )
                logger.info(f"Subprocess {component_name} stopped gracefully")
            except asyncio.TimeoutError:
                # Force kill
                logger.warning(
                    f"Subprocess {component_name} did not stop, sending SIGKILL"
                )
                handle.process.kill()
                await handle.process.wait()
                logger.info(f"Subprocess {component_name} killed")
        except ProcessLookupError:
            # Process already dead
            logger.debug(f"Subprocess {component_name} already dead")

        handle.state = ProcessState.STOPPED

        # Cancel output streaming task
        if component_name in self._output_tasks:
            self._output_tasks[component_name].cancel()
            try:
                await self._output_tasks[component_name]
            except asyncio.CancelledError:
                pass

        return True

    async def restart(self, component_name: str) -> Optional[ProcessHandle]:
        """
        Restart a subprocess with exponential backoff.

        Stops the current process (if running), waits with exponential
        backoff based on restart count, then starts a new process.

        Args:
            component_name: Name of the component to restart

        Returns:
            ProcessHandle for the restarted subprocess, or None if not found
        """
        if component_name not in self._handles:
            return None

        handle = self._handles[component_name]
        handle.restart_count += 1

        logger.info(
            f"Restarting subprocess {component_name} "
            f"(attempt {handle.restart_count})"
        )

        # Stop current process
        await self.stop(component_name)

        # Exponential backoff: 2^restart_count, capped at 30 seconds
        delay = min(2 ** handle.restart_count, 30)
        logger.debug(f"Waiting {delay}s before restart")
        await asyncio.sleep(delay)

        # Start new process
        return await self.start(handle.component)

    async def shutdown_all(self, reverse_order: bool = True) -> None:
        """
        Shutdown all managed subprocesses.

        Sets the shutdown event to stop all monitoring, then stops
        all subprocesses in order.

        Args:
            reverse_order: If True, stop in reverse start order
        """
        logger.info("Shutting down all subprocesses")
        self._shutdown_event.set()

        names = list(self._handles.keys())
        if reverse_order:
            names = list(reversed(names))

        for name in names:
            await self.stop(name)

        logger.info("All subprocesses shut down")

    def get_handle(self, name: str) -> Optional[ProcessHandle]:
        """
        Get handle for a subprocess.

        Args:
            name: Component name to look up

        Returns:
            ProcessHandle if found, None otherwise
        """
        return self._handles.get(name)

    def is_running(self, name: str) -> bool:
        """
        Check if a subprocess is running.

        Args:
            name: Component name to check

        Returns:
            True if subprocess is running
        """
        handle = self._handles.get(name)
        return handle is not None and handle.is_alive

    def _build_config(self, component: ComponentDefinition) -> ProcessConfig:
        """
        Build process configuration from component definition.

        Args:
            component: ComponentDefinition to build config for

        Returns:
            ProcessConfig with working directory, command, and environment
        """
        repo_path = self._resolve_repo_path(component.repo_path or "")

        # Get startup command
        command = self._get_startup_command(component.name, repo_path)

        # Build environment
        env = self._build_child_env(component)

        return ProcessConfig(
            working_dir=repo_path,
            command=command,
            env=env,
        )

    def _resolve_repo_path(self, path_template: str) -> str:
        """
        Resolve repo path, expanding environment variables.

        Handles ${VAR} patterns and tilde expansion.

        Args:
            path_template: Path template to resolve

        Returns:
            Resolved absolute path
        """
        if not path_template:
            return os.getcwd()

        # Expand ${VAR} patterns
        result = path_template
        for key, value in os.environ.items():
            result = result.replace(f"${{{key}}}", value)

        # Expand tilde
        return os.path.expanduser(result)

    def _get_startup_command(self, name: str, repo_path: str) -> List[str]:
        """
        Get startup command for a component.

        Maps component names to their entry point scripts.

        Args:
            name: Component name
            repo_path: Path to the component's repository

        Returns:
            Command list to execute
        """
        python = self._find_python()

        # Map component names to their entry points
        entry_points = {
            "jarvis-prime": "run.py",
            "reactor-core": "main.py",
        }

        entry = entry_points.get(name, "main.py")
        return [python, os.path.join(repo_path, entry)]

    def _find_python(self) -> str:
        """
        Find Python executable.

        Prefers venv Python if available, falls back to system Python.

        Returns:
            Path to Python executable
        """
        # Check for venv first
        venv_python = os.path.join(os.getcwd(), "venv", "bin", "python")
        if os.path.exists(venv_python):
            return venv_python

        # Fall back to system python
        return sys.executable

    def _build_child_env(self, component: ComponentDefinition) -> Dict[str, str]:
        """
        Build environment for child process.

        Passes through relevant parent environment variables and adds
        component-specific markers.

        Args:
            component: ComponentDefinition to build env for

        Returns:
            Environment dictionary for the child process
        """
        env = {}

        # Pass through relevant parent env vars
        for key in ["PYTHONPATH", "PATH", "HOME", "USER"]:
            if key in os.environ:
                env[key] = os.environ[key]

        # Add component-specific marker
        # Normalize name: replace hyphens with underscores, uppercase
        env_key = f"{component.name.upper().replace('-', '_')}_CHILD_PROCESS"
        env[env_key] = "1"

        return env

    async def _stream_output(self, handle: ProcessHandle) -> None:
        """
        Stream stdout/stderr with log level detection.

        Reads lines from process output streams and logs them with
        appropriate log levels based on content analysis.

        Args:
            handle: ProcessHandle to stream output for
        """
        try:

            async def read_stream(
                stream: asyncio.StreamReader, is_stderr: bool
            ) -> None:
                while not self._shutdown_event.is_set():
                    try:
                        line = await asyncio.wait_for(
                            stream.readline(),
                            timeout=1.0,
                        )
                        if not line:
                            break

                        text = line.decode("utf-8", errors="replace").rstrip()
                        level = self._detect_log_level(text)

                        log_func = getattr(logger, level, logger.info)
                        log_func(f"[{handle.name}] {text}")
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break

            await asyncio.gather(
                read_stream(handle.process.stdout, False),
                read_stream(handle.process.stderr, True),
            )
        except asyncio.CancelledError:
            pass

    def _detect_log_level(self, line: str) -> str:
        """
        Detect log level from line content.

        Analyzes the line for log level patterns and returns the
        appropriate level string.

        Args:
            line: Log line to analyze

        Returns:
            Log level string: "error", "warning", "debug", or "info"
        """
        upper = line.upper()

        if any(p in upper for p in ["ERROR:", "[ERROR]", "CRITICAL:"]):
            return "error"
        if any(p in upper for p in ["WARNING:", "[WARNING]", "WARN:"]):
            return "warning"
        if any(p in upper for p in ["DEBUG:", "[DEBUG]"]):
            return "debug"

        return "info"

    async def _monitor_health(self, handle: ProcessHandle) -> None:
        """
        Monitor subprocess health.

        Periodically checks if the subprocess is still alive and updates
        the handle state accordingly. If the process crashes, triggers
        recovery if a recovery engine is configured.

        Args:
            handle: ProcessHandle to monitor
        """
        try:
            while not self._shutdown_event.is_set() and handle.is_alive:
                await asyncio.sleep(5.0)  # Check every 5 seconds

                if not handle.is_alive:
                    handle.state = ProcessState.CRASHED
                    logger.error(
                        f"Subprocess {handle.name} crashed "
                        f"(exit code: {handle.process.returncode})"
                    )

                    # Trigger recovery if configured
                    if self.recovery_engine:
                        error = RuntimeError(
                            f"Process crashed with code {handle.process.returncode}"
                        )
                        await self.recovery_engine.handle_failure(
                            handle.name, error, RecoveryPhase.RUNTIME
                        )
                    break

                handle.last_health_check = datetime.now()
        except asyncio.CancelledError:
            pass


def get_subprocess_manager(
    registry: ComponentRegistry,
    recovery_engine: Optional[RecoveryEngine] = None,
) -> SubprocessManager:
    """
    Factory function for SubprocessManager.

    Creates a new SubprocessManager with the given registry and
    optional recovery engine.

    Args:
        registry: ComponentRegistry for component lookups
        recovery_engine: Optional RecoveryEngine for failure handling

    Returns:
        Configured SubprocessManager instance
    """
    return SubprocessManager(registry, recovery_engine)
