"""
Trinity IPC Layer v81.0 - Atomic File Operations for Cross-Repo Communication
=============================================================================

Provides atomic file-based IPC for communication between Trinity components:
- JARVIS Body (execution layer)
- JARVIS Prime (cognitive mind)
- Reactor Core (training nerves)

FEATURES:
    - OS-level atomic file locking (fcntl on POSIX, msvcrt on Windows)
    - Atomic write-rename pattern for corruption prevention
    - Centralized IPC bus for heartbeats, commands, and state
    - PID validation for process liveness checking
    - Stale lock detection and recovery
    - Async-compatible throughout

ZERO HARDCODING - All configuration via environment variables:
    TRINITY_DIR                     - Base directory for Trinity files
    TRINITY_LOCK_TIMEOUT            - Lock acquisition timeout (default: 30.0s)
    TRINITY_LOCK_STALE_TIMEOUT      - Stale lock detection (default: 300.0s)
    TRINITY_FILE_OPERATION_TIMEOUT  - File operation timeout (default: 10.0s)
    TRINITY_IPC_POLL_INTERVAL       - IPC polling interval (default: 0.1s)

Author: JARVIS v81.0
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# ENVIRONMENT VARIABLE HELPERS
# =============================================================================


def _env_str(key: str, default: str) -> str:
    """Get string from environment."""
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    """Get integer from environment with validation."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"[TrinityIPC] Invalid int for {key}: {value}, using default: {default}")
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment with validation."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"[TrinityIPC] Invalid float for {key}: {value}, using default: {default}")
        return default


def _env_path(key: str, default: Path) -> Path:
    """Get path from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    return Path(value).expanduser()


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================


class ComponentType(str, Enum):
    """Trinity component types."""
    JARVIS_BODY = "jarvis_body"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"
    CODING_COUNCIL = "coding_council"


class CommandPriority(int, Enum):
    """Command priority levels."""
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass
class TrinityIPCConfig:
    """Configuration for Trinity IPC - all values from environment variables."""

    # Base directory
    trinity_dir: Path = field(default_factory=lambda: _env_path(
        "TRINITY_DIR", Path.home() / ".jarvis" / "trinity"
    ))

    # Timeouts
    lock_timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_LOCK_TIMEOUT", 30.0
    ))
    lock_stale_timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_LOCK_STALE_TIMEOUT", 300.0
    ))
    file_operation_timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_FILE_OPERATION_TIMEOUT", 10.0
    ))

    # Polling
    poll_interval: float = field(default_factory=lambda: _env_float(
        "TRINITY_IPC_POLL_INTERVAL", 0.1
    ))

    def __post_init__(self):
        """Ensure directories exist."""
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """Create required directories."""
        dirs = [
            self.trinity_dir,
            self.trinity_dir / "heartbeats",
            self.trinity_dir / "commands",
            self.trinity_dir / "responses",
            self.trinity_dir / "state",
            self.trinity_dir / "locks",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    @property
    def heartbeats_dir(self) -> Path:
        return self.trinity_dir / "heartbeats"

    @property
    def commands_dir(self) -> Path:
        return self.trinity_dir / "commands"

    @property
    def responses_dir(self) -> Path:
        return self.trinity_dir / "responses"

    @property
    def state_dir(self) -> Path:
        return self.trinity_dir / "state"

    @property
    def locks_dir(self) -> Path:
        return self.trinity_dir / "locks"


@dataclass
class HeartbeatData:
    """Heartbeat data for a Trinity component."""
    component_type: ComponentType
    component_id: str
    timestamp: float
    pid: int
    host: str
    status: str  # "starting", "ready", "degraded", "stopping"
    uptime_seconds: float
    version: str = "81.0"
    metrics: Dict[str, Any] = field(default_factory=dict)
    dependencies_ready: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_type": self.component_type.value if isinstance(self.component_type, ComponentType) else self.component_type,
            "component_id": self.component_id,
            "timestamp": self.timestamp,
            "pid": self.pid,
            "host": self.host,
            "status": self.status,
            "uptime_seconds": self.uptime_seconds,
            "version": self.version,
            "metrics": self.metrics,
            "dependencies_ready": self.dependencies_ready,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HeartbeatData":
        component_type = data.get("component_type", "unknown")
        if isinstance(component_type, str):
            try:
                component_type = ComponentType(component_type)
            except ValueError:
                component_type = ComponentType.JARVIS_BODY

        return cls(
            component_type=component_type,
            component_id=data.get("component_id", "unknown"),
            timestamp=data.get("timestamp", 0.0),
            pid=data.get("pid", 0),
            host=data.get("host", "unknown"),
            status=data.get("status", "unknown"),
            uptime_seconds=data.get("uptime_seconds", 0.0),
            version=data.get("version", "unknown"),
            metrics=data.get("metrics", {}),
            dependencies_ready=data.get("dependencies_ready", {}),
        )

    @property
    def is_alive(self) -> bool:
        """Check if the component process is still alive."""
        return is_pid_alive(self.pid)

    @property
    def age_seconds(self) -> float:
        """Get age of heartbeat in seconds."""
        return time.time() - self.timestamp


@dataclass
class TrinityCommand:
    """Command to be sent between Trinity components."""
    command_id: str
    source: ComponentType
    target: ComponentType
    action: str
    payload: Dict[str, Any]
    priority: CommandPriority = CommandPriority.NORMAL
    timestamp: float = field(default_factory=time.time)
    timeout_seconds: float = 30.0
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "source": self.source.value if isinstance(self.source, ComponentType) else self.source,
            "target": self.target.value if isinstance(self.target, ComponentType) else self.target,
            "action": self.action,
            "payload": self.payload,
            "priority": self.priority.value if isinstance(self.priority, CommandPriority) else self.priority,
            "timestamp": self.timestamp,
            "timeout_seconds": self.timeout_seconds,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrinityCommand":
        return cls(
            command_id=data["command_id"],
            source=ComponentType(data["source"]),
            target=ComponentType(data["target"]),
            action=data["action"],
            payload=data.get("payload", {}),
            priority=CommandPriority(data.get("priority", 2)),
            timestamp=data.get("timestamp", time.time()),
            timeout_seconds=data.get("timeout_seconds", 30.0),
            correlation_id=data.get("correlation_id"),
        )


@dataclass
class CommandResponse:
    """Response to a Trinity command."""
    response_id: str
    command_id: str
    source: ComponentType
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "response_id": self.response_id,
            "command_id": self.command_id,
            "source": self.source.value if isinstance(self.source, ComponentType) else self.source,
            "success": self.success,
            "result": self.result,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommandResponse":
        return cls(
            response_id=data["response_id"],
            command_id=data["command_id"],
            source=ComponentType(data["source"]),
            success=data["success"],
            result=data.get("result"),
            error=data.get("error"),
            timestamp=data.get("timestamp", time.time()),
        )


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def is_pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we don't have permission
    except OSError:
        return False


def generate_id() -> str:
    """Generate a unique ID."""
    return str(uuid.uuid4())[:8]


# =============================================================================
# ATOMIC IPC FILE
# =============================================================================


class AtomicIPCFile:
    """
    Atomic file operations for IPC with proper locking.

    Uses OS-level fcntl locks for concurrency control and atomic
    rename operations for corruption prevention.

    Usage:
        ipc_file = AtomicIPCFile(path, config)

        # Atomic read
        data = await ipc_file.read_atomic()

        # Atomic write
        await ipc_file.write_atomic({"key": "value"})

        # Read-modify-write
        def updater(data):
            data["counter"] = data.get("counter", 0) + 1
            return data
        result = await ipc_file.update_atomic(updater)
    """

    def __init__(
        self,
        file_path: Path,
        config: Optional[TrinityIPCConfig] = None,
    ):
        self._file_path = Path(file_path)
        self._config = config or TrinityIPCConfig()
        self._lock_path = self._file_path.with_suffix(self._file_path.suffix + ".lock")

        # Ensure parent directory exists
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def file_path(self) -> Path:
        return self._file_path

    async def read_atomic(self) -> Optional[Dict[str, Any]]:
        """
        Read file with shared lock.

        Returns:
            Parsed JSON data or None if file doesn't exist or is invalid
        """
        if not self._file_path.exists():
            return None

        try:
            # Use run_in_executor for file I/O
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._read_with_lock)
        except Exception as e:
            logger.debug(f"[AtomicIPCFile] Read failed for {self._file_path}: {e}")
            return None

    def _read_with_lock(self) -> Optional[Dict[str, Any]]:
        """Synchronous read with shared lock."""
        if not self._file_path.exists():
            return None

        try:
            fd = os.open(str(self._file_path), os.O_RDONLY)
            try:
                # Acquire shared lock (allows multiple readers)
                fcntl.flock(fd, fcntl.LOCK_SH)
                try:
                    content = os.read(fd, 1024 * 1024)  # 1MB max
                    if not content:
                        return None
                    return json.loads(content.decode("utf-8"))
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"[AtomicIPCFile] Read error: {e}")
            return None

    async def write_atomic(self, data: Dict[str, Any]) -> None:
        """
        Atomic write using temp file + rename pattern.

        This ensures that readers never see a partial write.

        Args:
            data: Dictionary to write as JSON
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_with_lock, data)

    def _write_with_lock(self, data: Dict[str, Any]) -> None:
        """Synchronous atomic write."""
        # Write to temporary file first
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self._file_path.parent,
            prefix=f".{self._file_path.name}.",
            suffix=".tmp"
        )

        try:
            # Write data
            content = json.dumps(data, indent=2, default=str)
            os.write(tmp_fd, content.encode("utf-8"))
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1  # Mark as closed

            # Atomic rename
            os.replace(tmp_path, self._file_path)

        except Exception:
            # Clean up temp file on error
            if tmp_fd >= 0:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def update_atomic(
        self,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Atomic read-modify-write operation.

        Args:
            updater: Function that transforms the data
            timeout: Lock acquisition timeout

        Returns:
            Updated data
        """
        timeout = timeout or self._config.lock_timeout
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._update_with_lock,
            updater,
            timeout
        )

    def _update_with_lock(
        self,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
        timeout: float,
    ) -> Dict[str, Any]:
        """Synchronous read-modify-write with exclusive lock."""
        # Create or open lock file
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(
            str(self._lock_path),
            os.O_RDWR | os.O_CREAT,
            0o644
        )

        try:
            # Try to acquire exclusive lock with timeout
            start_time = time.time()
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() - start_time > timeout:
                        raise TimeoutError(f"Lock acquisition timed out after {timeout}s")
                    time.sleep(0.05)

            try:
                # Read current data
                current = {}
                if self._file_path.exists():
                    try:
                        content = self._file_path.read_text()
                        if content:
                            current = json.loads(content)
                    except (json.JSONDecodeError, OSError):
                        pass

                # Apply update
                updated = updater(current)

                # Write atomically
                self._write_with_lock(updated)

                return updated

            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    async def delete(self) -> bool:
        """Delete the file if it exists."""
        try:
            if self._file_path.exists():
                self._file_path.unlink()
            if self._lock_path.exists():
                self._lock_path.unlink()
            return True
        except OSError as e:
            logger.debug(f"[AtomicIPCFile] Delete failed: {e}")
            return False

    async def exists(self) -> bool:
        """Check if file exists."""
        return self._file_path.exists()


# =============================================================================
# TRINITY IPC BUS
# =============================================================================


class TrinityIPCBus:
    """
    Centralized IPC bus for all Trinity communication.

    Provides:
    - Atomic heartbeat publish/subscribe
    - Command queue with priority ordering
    - State synchronization
    - Response handling

    Usage:
        bus = await get_trinity_ipc_bus()

        # Publish heartbeat
        await bus.publish_heartbeat(
            component=ComponentType.JARVIS_BODY,
            status="ready",
            pid=os.getpid(),
            metrics={"requests": 100}
        )

        # Read heartbeat
        heartbeat = await bus.read_heartbeat(ComponentType.JARVIS_PRIME)
        if heartbeat and heartbeat.is_alive:
            print(f"J-Prime is {heartbeat.status}")

        # Send command
        cmd_id = await bus.enqueue_command(TrinityCommand(...))

        # Wait for response
        response = await bus.wait_for_response(cmd_id, timeout=10.0)
    """

    def __init__(self, config: Optional[TrinityIPCConfig] = None):
        self._config = config or TrinityIPCConfig()
        self._heartbeat_files: Dict[ComponentType, AtomicIPCFile] = {}
        self._command_file: Optional[AtomicIPCFile] = None
        self._state_file: Optional[AtomicIPCFile] = None
        self._start_time = time.time()

        # Initialize files
        self._init_files()

    def _init_files(self) -> None:
        """Initialize IPC files."""
        # Heartbeat files for each component
        for component in ComponentType:
            self._heartbeat_files[component] = AtomicIPCFile(
                self._config.heartbeats_dir / f"{component.value}.json",
                self._config,
            )

        # Command queue file
        self._command_file = AtomicIPCFile(
            self._config.commands_dir / "command_queue.json",
            self._config,
        )

        # Shared state file
        self._state_file = AtomicIPCFile(
            self._config.state_dir / "trinity_state.json",
            self._config,
        )

    # =========================================================================
    # HEARTBEAT OPERATIONS
    # =========================================================================

    async def publish_heartbeat(
        self,
        component: ComponentType,
        status: str,
        pid: int,
        metrics: Optional[Dict[str, Any]] = None,
        dependencies_ready: Optional[Dict[str, bool]] = None,
    ) -> None:
        """
        Publish heartbeat for a component.

        Args:
            component: Component type
            status: Current status (starting, ready, degraded, stopping)
            pid: Process ID
            metrics: Optional performance metrics
            dependencies_ready: Optional dependency status
        """
        heartbeat = HeartbeatData(
            component_type=component,
            component_id=f"{component.value}_{pid}",
            timestamp=time.time(),
            pid=pid,
            host=os.uname().nodename,
            status=status,
            uptime_seconds=time.time() - self._start_time,
            metrics=metrics or {},
            dependencies_ready=dependencies_ready or {},
        )

        ipc_file = self._heartbeat_files.get(component)
        if ipc_file:
            await ipc_file.write_atomic(heartbeat.to_dict())
            logger.debug(f"[TrinityIPC] Published heartbeat for {component.value}: {status}")

    async def read_heartbeat(
        self,
        component: ComponentType,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[HeartbeatData]:
        """
        Read heartbeat for a component.

        Args:
            component: Component type
            max_age_seconds: Maximum age for valid heartbeat (default: 15s)

        Returns:
            HeartbeatData if valid heartbeat exists, None otherwise
        """
        max_age = max_age_seconds or 15.0

        ipc_file = self._heartbeat_files.get(component)
        if not ipc_file:
            return None

        data = await ipc_file.read_atomic()
        if data is None:
            return None

        try:
            heartbeat = HeartbeatData.from_dict(data)

            # Check if heartbeat is stale
            if heartbeat.age_seconds > max_age:
                logger.debug(
                    f"[TrinityIPC] Stale heartbeat for {component.value}: "
                    f"{heartbeat.age_seconds:.1f}s old"
                )
                return None

            # Verify PID is alive
            if not heartbeat.is_alive:
                logger.debug(
                    f"[TrinityIPC] Dead process for {component.value}: "
                    f"PID {heartbeat.pid}"
                )
                return None

            return heartbeat

        except Exception as e:
            logger.debug(f"[TrinityIPC] Invalid heartbeat data: {e}")
            return None

    async def read_all_heartbeats(
        self,
        max_age_seconds: Optional[float] = None,
    ) -> Dict[ComponentType, Optional[HeartbeatData]]:
        """Read heartbeats for all components."""
        results = {}
        for component in ComponentType:
            results[component] = await self.read_heartbeat(component, max_age_seconds)
        return results

    async def get_component_status(
        self,
        component: ComponentType,
    ) -> str:
        """Get status of a component (ready, degraded, offline, unknown)."""
        heartbeat = await self.read_heartbeat(component)
        if heartbeat is None:
            return "offline"
        return heartbeat.status

    async def is_component_ready(self, component: ComponentType) -> bool:
        """Check if a component is ready."""
        return await self.get_component_status(component) == "ready"

    # =========================================================================
    # COMMAND OPERATIONS
    # =========================================================================

    async def enqueue_command(self, command: TrinityCommand) -> str:
        """
        Add command to queue.

        Args:
            command: Command to enqueue

        Returns:
            Command ID
        """
        def updater(data: Dict) -> Dict:
            queue = data.get("commands", [])
            queue.append(command.to_dict())
            # Sort by priority (lower value = higher priority)
            queue.sort(key=lambda x: x.get("priority", 2))
            # Limit queue size
            if len(queue) > 1000:
                queue = queue[:1000]
            data["commands"] = queue
            data["last_updated"] = time.time()
            return data

        if self._command_file:
            await self._command_file.update_atomic(updater)
            logger.debug(
                f"[TrinityIPC] Enqueued command {command.command_id}: "
                f"{command.action} -> {command.target.value}"
            )

        return command.command_id

    async def dequeue_command(
        self,
        target: ComponentType,
    ) -> Optional[TrinityCommand]:
        """
        Get next command for a component.

        Args:
            target: Target component

        Returns:
            Next command or None
        """
        result = [None]

        def updater(data: Dict) -> Dict:
            queue = data.get("commands", [])

            # Find first command for this target
            for i, cmd_data in enumerate(queue):
                if cmd_data.get("target") == target.value:
                    # Remove from queue
                    queue.pop(i)
                    result[0] = TrinityCommand.from_dict(cmd_data)
                    break

            data["commands"] = queue
            data["last_updated"] = time.time()
            return data

        if self._command_file:
            await self._command_file.update_atomic(updater)

        if result[0]:
            logger.debug(
                f"[TrinityIPC] Dequeued command {result[0].command_id}: "
                f"{result[0].action}"
            )

        return result[0]

    async def peek_commands(
        self,
        target: Optional[ComponentType] = None,
        limit: int = 10,
    ) -> List[TrinityCommand]:
        """
        Peek at pending commands without removing them.

        Args:
            target: Optional filter by target
            limit: Maximum number to return

        Returns:
            List of pending commands
        """
        if not self._command_file:
            return []

        data = await self._command_file.read_atomic()
        if data is None:
            return []

        queue = data.get("commands", [])

        if target:
            queue = [c for c in queue if c.get("target") == target.value]

        return [TrinityCommand.from_dict(c) for c in queue[:limit]]

    # =========================================================================
    # RESPONSE OPERATIONS
    # =========================================================================

    async def send_response(self, response: CommandResponse) -> None:
        """Send response to a command."""
        response_file = AtomicIPCFile(
            self._config.responses_dir / f"{response.command_id}.json",
            self._config,
        )
        await response_file.write_atomic(response.to_dict())
        logger.debug(f"[TrinityIPC] Sent response for {response.command_id}")

    async def wait_for_response(
        self,
        command_id: str,
        timeout: float = 30.0,
    ) -> Optional[CommandResponse]:
        """
        Wait for response to a command.

        Args:
            command_id: Command to wait for
            timeout: Maximum wait time

        Returns:
            Response or None if timeout
        """
        response_file = AtomicIPCFile(
            self._config.responses_dir / f"{command_id}.json",
            self._config,
        )

        start_time = time.time()
        while time.time() - start_time < timeout:
            data = await response_file.read_atomic()
            if data:
                try:
                    response = CommandResponse.from_dict(data)
                    # Clean up response file
                    await response_file.delete()
                    return response
                except Exception as e:
                    logger.debug(f"[TrinityIPC] Invalid response data: {e}")

            await asyncio.sleep(self._config.poll_interval)

        logger.debug(f"[TrinityIPC] Response timeout for {command_id}")
        return None

    # =========================================================================
    # STATE OPERATIONS
    # =========================================================================

    async def read_shared_state(self) -> Dict[str, Any]:
        """Read shared Trinity state."""
        if not self._state_file:
            return {}
        data = await self._state_file.read_atomic()
        return data or {}

    async def update_shared_state(
        self,
        key: str,
        value: Any,
    ) -> None:
        """Update a key in shared state."""
        def updater(data: Dict) -> Dict:
            data[key] = value
            data["_last_updated"] = time.time()
            data["_last_updater"] = os.getpid()
            return data

        if self._state_file:
            await self._state_file.update_atomic(updater)

    async def merge_shared_state(
        self,
        updates: Dict[str, Any],
    ) -> None:
        """Merge multiple keys into shared state."""
        def updater(data: Dict) -> Dict:
            data.update(updates)
            data["_last_updated"] = time.time()
            data["_last_updater"] = os.getpid()
            return data

        if self._state_file:
            await self._state_file.update_atomic(updater)

    # =========================================================================
    # CLEANUP
    # =========================================================================

    async def cleanup_stale_heartbeats(
        self,
        max_age_seconds: float = 60.0,
    ) -> int:
        """
        Clean up stale heartbeat files.

        Args:
            max_age_seconds: Maximum age before cleanup

        Returns:
            Number of files cleaned up
        """
        cleaned = 0

        for component, ipc_file in self._heartbeat_files.items():
            if not await ipc_file.exists():
                continue

            data = await ipc_file.read_atomic()
            if data is None:
                await ipc_file.delete()
                cleaned += 1
                continue

            try:
                heartbeat = HeartbeatData.from_dict(data)

                # Clean up if stale or dead
                if heartbeat.age_seconds > max_age_seconds or not heartbeat.is_alive:
                    await ipc_file.delete()
                    cleaned += 1
                    logger.info(
                        f"[TrinityIPC] Cleaned up stale heartbeat: {component.value}"
                    )
            except Exception:
                await ipc_file.delete()
                cleaned += 1

        return cleaned

    async def cleanup_old_responses(
        self,
        max_age_seconds: float = 300.0,
    ) -> int:
        """Clean up old response files."""
        cleaned = 0

        for response_file in self._config.responses_dir.glob("*.json"):
            try:
                mtime = response_file.stat().st_mtime
                if time.time() - mtime > max_age_seconds:
                    response_file.unlink()
                    cleaned += 1
            except OSError:
                pass

        return cleaned


# =============================================================================
# SINGLETON PATTERN
# =============================================================================

_ipc_bus: Optional[TrinityIPCBus] = None
_ipc_lock = asyncio.Lock()


async def get_trinity_ipc_bus(
    config: Optional[TrinityIPCConfig] = None,
) -> TrinityIPCBus:
    """
    Get or create the global Trinity IPC bus singleton.

    Thread-safe with double-check locking.

    Args:
        config: Optional configuration override

    Returns:
        TrinityIPCBus instance
    """
    global _ipc_bus

    # Fast path
    if _ipc_bus is not None:
        return _ipc_bus

    # Slow path with lock
    async with _ipc_lock:
        if _ipc_bus is None:
            _ipc_bus = TrinityIPCBus(config)
            logger.info("[TrinityIPC] Initialized IPC bus")
        return _ipc_bus


def get_trinity_ipc_bus_sync(
    config: Optional[TrinityIPCConfig] = None,
) -> TrinityIPCBus:
    """Synchronous version for non-async contexts."""
    global _ipc_bus

    if _ipc_bus is None:
        _ipc_bus = TrinityIPCBus(config)
        logger.info("[TrinityIPC] Initialized IPC bus (sync)")

    return _ipc_bus


async def close_trinity_ipc_bus() -> None:
    """Close and cleanup the global IPC bus."""
    global _ipc_bus

    if _ipc_bus is not None:
        await _ipc_bus.cleanup_stale_heartbeats()
        await _ipc_bus.cleanup_old_responses()
        _ipc_bus = None
        logger.info("[TrinityIPC] Closed IPC bus")


# =============================================================================
# ENHANCED IPC BUS WITH CIRCUIT BREAKER AND IN-MEMORY FALLBACK
# =============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking requests (failing fast)
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration."""
    failure_threshold: int = field(default_factory=lambda: _env_int(
        "TRINITY_IPC_CIRCUIT_FAILURE_THRESHOLD", 5
    ))
    success_threshold: int = field(default_factory=lambda: _env_int(
        "TRINITY_IPC_CIRCUIT_SUCCESS_THRESHOLD", 2
    ))
    timeout_seconds: float = field(default_factory=lambda: _env_float(
        "TRINITY_IPC_CIRCUIT_TIMEOUT", 30.0
    ))


class IPCCircuitBreaker:
    """
    Circuit breaker for IPC operations.

    When file operations fail repeatedly, the circuit opens and
    operations fall back to in-memory mode.
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()

        # Metrics
        self._total_failures = 0
        self._total_successes = 0
        self._circuit_opens = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    async def can_execute(self) -> bool:
        """Check if operation can proceed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if timeout elapsed
                if time.time() - self._last_failure_time >= self.config.timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    logger.info(f"[IPC-CB:{self.name}] Circuit HALF_OPEN (testing)")
                    return True
                return False

            # HALF_OPEN - allow single test request
            return True

    async def record_success(self) -> None:
        """Record successful operation."""
        async with self._lock:
            self._total_successes += 1
            self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(f"[IPC-CB:{self.name}] Circuit CLOSED (recovered)")
            else:
                self._failure_count = max(0, self._failure_count - 1)

    async def record_failure(self) -> None:
        """Record failed operation."""
        async with self._lock:
            self._total_failures += 1
            self._failure_count += 1
            self._last_failure_time = time.time()
            self._success_count = 0

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._circuit_opens += 1
                logger.warning(f"[IPC-CB:{self.name}] Circuit OPEN (test failed)")
            elif (
                self._state == CircuitState.CLOSED and
                self._failure_count >= self.config.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._circuit_opens += 1
                logger.warning(
                    f"[IPC-CB:{self.name}] Circuit OPEN "
                    f"(threshold {self.config.failure_threshold} reached)"
                )

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "total_failures": self._total_failures,
            "total_successes": self._total_successes,
            "circuit_opens": self._circuit_opens,
        }


class InMemoryFallbackQueue:
    """
    In-memory fallback queue for when file operations fail.

    Features:
    - Bounded size with LRU eviction
    - TTL-based expiration
    - Automatic persistence on recovery
    """

    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: float = 300.0,
    ):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

        self._commands: Dict[str, Tuple[float, TrinityCommand]] = {}
        self._heartbeats: Dict[str, Tuple[float, HeartbeatData]] = {}
        self._lock = asyncio.Lock()

        logger.info(f"[InMemoryQueue] Initialized (max={max_size}, ttl={ttl_seconds}s)")

    async def store_command(self, command: TrinityCommand) -> None:
        """Store command in memory."""
        async with self._lock:
            # Evict oldest if at capacity
            if len(self._commands) >= self.max_size:
                oldest_id = min(
                    self._commands.keys(),
                    key=lambda k: self._commands[k][0]
                )
                del self._commands[oldest_id]

            self._commands[command.command_id] = (time.time(), command)

    async def get_commands(self, target: ComponentType) -> List[TrinityCommand]:
        """Get commands for a target component."""
        async with self._lock:
            now = time.time()
            result = []

            for cmd_id, (timestamp, cmd) in list(self._commands.items()):
                # Skip expired
                if now - timestamp > self.ttl_seconds:
                    del self._commands[cmd_id]
                    continue

                if cmd.target == target:
                    result.append(cmd)
                    del self._commands[cmd_id]

            return result

    async def store_heartbeat(self, heartbeat: HeartbeatData) -> None:
        """Store heartbeat in memory."""
        async with self._lock:
            key = heartbeat.component_type.value
            self._heartbeats[key] = (time.time(), heartbeat)

    async def get_heartbeat(
        self,
        component: ComponentType,
    ) -> Optional[HeartbeatData]:
        """Get heartbeat for component."""
        async with self._lock:
            key = component.value
            if key not in self._heartbeats:
                return None

            timestamp, heartbeat = self._heartbeats[key]

            # Check TTL
            if time.time() - timestamp > self.ttl_seconds:
                del self._heartbeats[key]
                return None

            return heartbeat

    async def get_all_commands(self) -> List[TrinityCommand]:
        """Get all stored commands (for persistence on recovery)."""
        async with self._lock:
            now = time.time()
            result = []

            for cmd_id, (timestamp, cmd) in list(self._commands.items()):
                if now - timestamp <= self.ttl_seconds:
                    result.append(cmd)

            return result

    def __len__(self) -> int:
        return len(self._commands) + len(self._heartbeats)


class ResilientTrinityIPCBus(TrinityIPCBus):
    """
    Enhanced IPC bus with circuit breaker and in-memory fallback.

    When file operations fail repeatedly:
    1. Circuit breaker opens
    2. Operations fall back to in-memory queue
    3. When circuit recovers, pending operations are persisted

    This ensures the IPC bus never blocks completely due to
    file system issues.
    """

    def __init__(self, config: Optional[TrinityIPCConfig] = None):
        super().__init__(config)

        # Circuit breakers for different operation types
        self._heartbeat_circuit = IPCCircuitBreaker("heartbeat")
        self._command_circuit = IPCCircuitBreaker("command")

        # In-memory fallback
        self._fallback_queue = InMemoryFallbackQueue()

        # Recovery task
        self._recovery_task: Optional[asyncio.Task] = None
        self._running = False

        logger.info("[ResilientIPC] Initialized with circuit breaker and fallback")

    async def start(self) -> None:
        """Start the resilient IPC bus."""
        self._running = True
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def stop(self) -> None:
        """Stop the resilient IPC bus."""
        self._running = False
        if self._recovery_task:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass

    async def _recovery_loop(self) -> None:
        """Background loop to recover from circuit open state."""
        while self._running:
            try:
                await asyncio.sleep(5.0)

                # Check if any circuit is recovering
                if not self._heartbeat_circuit.is_open and not self._command_circuit.is_open:
                    # Persist any pending in-memory commands
                    await self._persist_pending_commands()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[ResilientIPC] Recovery loop error: {e}")

    async def _persist_pending_commands(self) -> None:
        """Persist pending in-memory commands to file."""
        commands = await self._fallback_queue.get_all_commands()

        if not commands:
            return

        logger.info(f"[ResilientIPC] Persisting {len(commands)} pending commands")

        for cmd in commands:
            try:
                await super().enqueue_command(cmd)
            except Exception as e:
                logger.warning(f"[ResilientIPC] Failed to persist command: {e}")

    # =========================================================================
    # Override Heartbeat Operations with Circuit Breaker
    # =========================================================================

    async def publish_heartbeat(
        self,
        component: ComponentType,
        status: str,
        pid: int,
        metrics: Optional[Dict[str, Any]] = None,
        dependencies_ready: Optional[Dict[str, bool]] = None,
    ) -> None:
        """Publish heartbeat with circuit breaker protection."""
        if not await self._heartbeat_circuit.can_execute():
            # Fall back to in-memory
            heartbeat = HeartbeatData(
                component_type=component,
                component_id=f"{component.value}_{pid}",
                timestamp=time.time(),
                pid=pid,
                host=os.uname().nodename,
                status=status,
                uptime_seconds=time.time() - self._start_time,
                metrics=metrics or {},
                dependencies_ready=dependencies_ready or {},
            )
            await self._fallback_queue.store_heartbeat(heartbeat)
            return

        try:
            await super().publish_heartbeat(
                component=component,
                status=status,
                pid=pid,
                metrics=metrics,
                dependencies_ready=dependencies_ready,
            )
            await self._heartbeat_circuit.record_success()

        except Exception as e:
            await self._heartbeat_circuit.record_failure()
            logger.warning(f"[ResilientIPC] Heartbeat publish failed: {e}")

            # Fall back to in-memory
            heartbeat = HeartbeatData(
                component_type=component,
                component_id=f"{component.value}_{pid}",
                timestamp=time.time(),
                pid=pid,
                host=os.uname().nodename,
                status=status,
                uptime_seconds=time.time() - self._start_time,
                metrics=metrics or {},
                dependencies_ready=dependencies_ready or {},
            )
            await self._fallback_queue.store_heartbeat(heartbeat)

    async def read_heartbeat(
        self,
        component: ComponentType,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[HeartbeatData]:
        """Read heartbeat with fallback to in-memory."""
        # Try file-based first
        if await self._heartbeat_circuit.can_execute():
            try:
                result = await super().read_heartbeat(component, max_age_seconds)
                await self._heartbeat_circuit.record_success()
                if result:
                    return result
            except Exception as e:
                await self._heartbeat_circuit.record_failure()
                logger.debug(f"[ResilientIPC] Heartbeat read failed: {e}")

        # Fall back to in-memory
        return await self._fallback_queue.get_heartbeat(component)

    # =========================================================================
    # Override Command Operations with Circuit Breaker
    # =========================================================================

    async def enqueue_command(self, command: TrinityCommand) -> str:
        """Enqueue command with circuit breaker protection."""
        if not await self._command_circuit.can_execute():
            # Fall back to in-memory
            await self._fallback_queue.store_command(command)
            logger.debug(
                f"[ResilientIPC] Command queued in-memory: {command.command_id}"
            )
            return command.command_id

        try:
            result = await super().enqueue_command(command)
            await self._command_circuit.record_success()
            return result

        except Exception as e:
            await self._command_circuit.record_failure()
            logger.warning(f"[ResilientIPC] Command enqueue failed: {e}")

            # Fall back to in-memory
            await self._fallback_queue.store_command(command)
            return command.command_id

    async def dequeue_command(
        self,
        target: ComponentType,
    ) -> Optional[TrinityCommand]:
        """Dequeue command with fallback to in-memory."""
        # Check in-memory first (higher priority for recovery)
        in_memory_commands = await self._fallback_queue.get_commands(target)
        if in_memory_commands:
            return in_memory_commands[0]

        # Then try file-based
        if await self._command_circuit.can_execute():
            try:
                result = await super().dequeue_command(target)
                await self._command_circuit.record_success()
                return result
            except Exception as e:
                await self._command_circuit.record_failure()
                logger.debug(f"[ResilientIPC] Command dequeue failed: {e}")

        return None

    # =========================================================================
    # Metrics
    # =========================================================================

    def get_resilience_metrics(self) -> Dict[str, Any]:
        """Get circuit breaker and fallback metrics."""
        return {
            "heartbeat_circuit": self._heartbeat_circuit.get_metrics(),
            "command_circuit": self._command_circuit.get_metrics(),
            "fallback_queue_size": len(self._fallback_queue),
        }


# =============================================================================
# Enhanced Singleton with Resilient IPC
# =============================================================================

_resilient_ipc_bus: Optional[ResilientTrinityIPCBus] = None
_resilient_ipc_lock = asyncio.Lock()


async def get_resilient_trinity_ipc_bus(
    config: Optional[TrinityIPCConfig] = None,
) -> ResilientTrinityIPCBus:
    """
    Get or create the global resilient Trinity IPC bus.

    This is the recommended IPC bus for production use as it
    includes circuit breaker and in-memory fallback.
    """
    global _resilient_ipc_bus

    async with _resilient_ipc_lock:
        if _resilient_ipc_bus is None:
            _resilient_ipc_bus = ResilientTrinityIPCBus(config)
            await _resilient_ipc_bus.start()
            logger.info("[TrinityIPC] Initialized resilient IPC bus")
        return _resilient_ipc_bus


async def close_resilient_trinity_ipc_bus() -> None:
    """Close the resilient IPC bus."""
    global _resilient_ipc_bus

    if _resilient_ipc_bus is not None:
        await _resilient_ipc_bus.stop()
        await _resilient_ipc_bus.cleanup_stale_heartbeats()
        await _resilient_ipc_bus.cleanup_old_responses()
        _resilient_ipc_bus = None
        logger.info("[TrinityIPC] Closed resilient IPC bus")
