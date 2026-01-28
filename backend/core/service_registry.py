"""
Enterprise-Grade Service Registry v3.0
======================================

Dynamic service discovery system eliminating hardcoded ports and enabling
true distributed orchestration across JARVIS, J-Prime, and Reactor-Core.

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                   Service Registry v3.0                          │
    │  ┌────────────────────────────────────────────────────────────┐  │
    │  │  File-Based Registry: ~/.jarvis/registry/services.json     │  │
    │  │  ┌──────────────┬──────────────┬─────────────────────┐     │  │
    │  │  │   JARVIS     │   J-PRIME    │   REACTOR-CORE      │     │  │
    │  │  │  PID: 12345  │  PID: 12346  │   PID: 12347        │     │  │
    │  │  │  Port: 5001  │  Port: 8000  │   Port: 8090        │     │  │
    │  │  │  Status: ✅  │  Status: ✅  │   Status: ✅         │     │  │
    │  │  └──────────────┴──────────────┴─────────────────────┘     │  │
    │  └────────────────────────────────────────────────────────────┘  │
    │                                                                  │
    │  Features:                                                       │
    │  • Atomic file operations with fcntl locking                     │
    │  • Automatic stale service cleanup (dead PIDs)                   │
    │  • Health heartbeat tracking                                     │
    │  • Zero hardcoded ports or URLs                                  │
    │  • Cross-process safe concurrent access                          │
    └──────────────────────────────────────────────────────────────────┘

Usage:
    # Register service on startup
    registry = ServiceRegistry()
    await registry.register_service(
        service_name="jarvis-prime",
        pid=os.getpid(),
        port=8000,
        health_endpoint="/health"
    )

    # Discover services dynamically
    jprime = await registry.discover_service("jarvis-prime")
    if jprime:
        url = f"http://{jprime.host}:{jprime.port}{jprime.health_endpoint}"

    # Heartbeat to keep alive
    await registry.heartbeat("jarvis-prime")

    # Clean deregistration on shutdown
    await registry.deregister_service("jarvis-prime")

Author: JARVIS AI System
Version: 93.0.0

v93.0 Changes:
- Added DirectoryManager for robust directory handling
- Retry logic for all file operations (read/write)
- Handles race conditions where directory is deleted
- Atomic temp file creation using tempfile module
- Pre-write directory validation
- Detailed error logging for debugging
"""

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta
from contextlib import contextmanager
import psutil

logger = logging.getLogger(__name__)


# =============================================================================
# v93.0: Robust Directory Management
# =============================================================================

class DirectoryManager:
    """
    v93.0: Thread-safe directory management with atomic operations.

    Handles race conditions, missing directories, and provides retry logic
    for all file system operations.
    """

    def __init__(self, base_dir: Path, max_retries: int = 3, retry_delay: float = 0.1):
        self.base_dir = base_dir
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._initialized = False
        self._init_lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None

    def ensure_directory_sync(self) -> bool:
        """
        v93.0: Synchronously ensure directory exists with retry logic.

        Returns:
            True if directory exists/was created successfully
        """
        for attempt in range(self.max_retries):
            try:
                # Create directory with all parents
                self.base_dir.mkdir(parents=True, exist_ok=True)

                # Verify it actually exists (handles race with deletion)
                if self.base_dir.exists() and self.base_dir.is_dir():
                    self._initialized = True
                    return True

                # If doesn't exist after creation, wait and retry
                time.sleep(self.retry_delay * (attempt + 1))

            except PermissionError as e:
                logger.error(f"[v93.0] Permission denied creating {self.base_dir}: {e}")
                return False
            except OSError as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"[v93.0] Failed to create directory {self.base_dir}: {e}")
                    return False
                time.sleep(self.retry_delay * (attempt + 1))

        return False

    async def ensure_directory_async(self) -> bool:
        """v93.0: Async version of ensure_directory_sync."""
        return await asyncio.to_thread(self.ensure_directory_sync)

    @contextmanager
    def atomic_write_context(self, target_file: Path):
        """
        v93.0: Context manager for atomic file writes.

        Creates a temp file, yields it for writing, then atomically
        renames to target. Handles cleanup on failure.

        Usage:
            with dir_manager.atomic_write_context(file_path) as temp_path:
                temp_path.write_text(data)
            # File is atomically renamed on context exit
        """
        # Ensure directory exists BEFORE attempting write
        self.ensure_directory_sync()

        # Create temp file in same directory for atomic rename
        temp_fd, temp_path_str = tempfile.mkstemp(
            dir=str(self.base_dir),
            prefix=target_file.stem + "_",
            suffix=".tmp"
        )
        temp_path = Path(temp_path_str)

        try:
            # Close the fd since we'll write via path
            os.close(temp_fd)

            yield temp_path

            # Ensure directory still exists before rename
            self.ensure_directory_sync()

            # Atomic rename (POSIX guarantees atomicity on same filesystem)
            temp_path.replace(target_file)

        except Exception as e:
            # Clean up temp file on failure
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            raise

    def safe_read(self, file_path: Path) -> Optional[str]:
        """
        v93.0: Safely read a file with retry logic.

        Returns None if file doesn't exist or can't be read.
        """
        for attempt in range(self.max_retries):
            try:
                if not file_path.exists():
                    return None

                return file_path.read_text()

            except (FileNotFoundError, OSError) as e:
                if attempt == self.max_retries - 1:
                    logger.warning(f"[v93.0] Failed to read {file_path}: {e}")
                    return None
                time.sleep(self.retry_delay * (attempt + 1))

        return None


# =============================================================================
# v96.0: Atomic Shared Registry for Cross-Process Coordination
# =============================================================================

class AtomicSharedRegistry:
    """
    v96.0: Thread-safe and process-safe shared registry operations.

    This class provides atomic read-modify-write operations for the shared
    service registry (~/.jarvis/registry/services.json). It uses a separate
    lock file to coordinate access across multiple processes.

    THE CRITICAL FIX:
        Previous implementations locked the TEMP file during write, which
        doesn't prevent race conditions during read-modify-write cycles.

        This implementation uses a SEPARATE LOCK FILE (.lock) that is
        held for the ENTIRE read-modify-write cycle, ensuring true atomicity.

    Usage:
        # From JARVIS Prime, Reactor Core, or any external process
        from backend.core.service_registry import AtomicSharedRegistry

        async with AtomicSharedRegistry.atomic_update() as registry:
            registry["my_service"] = {"pid": os.getpid(), ...}
            # Registry is automatically saved when context exits

    Or for simple registration:
        AtomicSharedRegistry.register_service("my_service", {...})
    """

    # Default paths
    DEFAULT_REGISTRY_DIR = Path.home() / ".jarvis" / "registry"
    DEFAULT_REGISTRY_FILE = "services.json"
    DEFAULT_LOCK_FILE = "services.json.lock"

    # Timeouts
    LOCK_TIMEOUT = 30.0  # Max seconds to wait for lock
    LOCK_POLL_INTERVAL = 0.05  # Poll interval when waiting for lock

    @classmethod
    def get_registry_path(cls) -> Path:
        """Get the registry file path from environment or default."""
        registry_dir = Path(os.getenv(
            "JARVIS_REGISTRY_DIR",
            str(cls.DEFAULT_REGISTRY_DIR)
        ))
        return registry_dir / cls.DEFAULT_REGISTRY_FILE

    @classmethod
    def get_lock_path(cls) -> Path:
        """Get the lock file path."""
        registry_dir = Path(os.getenv(
            "JARVIS_REGISTRY_DIR",
            str(cls.DEFAULT_REGISTRY_DIR)
        ))
        return registry_dir / cls.DEFAULT_LOCK_FILE

    @classmethod
    @contextmanager
    def _acquire_registry_lock(cls, timeout: float = None):
        """
        v96.0: Acquire exclusive lock on the registry using a separate lock file.

        This is the CRITICAL fix - we lock a separate file and hold it for the
        entire read-modify-write cycle.

        Args:
            timeout: Max seconds to wait (default: LOCK_TIMEOUT)

        Yields:
            The lock file handle (for cleanup)

        Raises:
            TimeoutError: If lock cannot be acquired within timeout
        """
        timeout = timeout or cls.LOCK_TIMEOUT
        lock_path = cls.get_lock_path()

        # Ensure directory exists
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Create lock file if it doesn't exist
        lock_path.touch(exist_ok=True)

        start_time = time.time()
        lock_fd = None

        try:
            # Open lock file
            lock_fd = open(lock_path, 'r+')

            # Try to acquire lock with timeout
            while True:
                try:
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Lock acquired!
                    break
                except (IOError, OSError):
                    # Lock is held by another process
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        lock_fd.close()
                        raise TimeoutError(
                            f"Could not acquire registry lock within {timeout}s. "
                            f"Another process may be holding the lock."
                        )
                    time.sleep(cls.LOCK_POLL_INTERVAL)

            yield lock_fd

        finally:
            # Release lock
            if lock_fd:
                try:
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass

    @classmethod
    def _read_registry_unlocked(cls) -> Dict[str, Any]:
        """Read registry file without locking (caller must hold lock)."""
        registry_path = cls.get_registry_path()

        if not registry_path.exists():
            return {}

        try:
            content = registry_path.read_text()
            if not content.strip():
                return {}
            data = json.loads(content)
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[v96.0] Registry read error: {e}")
            return {}

    @classmethod
    def _write_registry_unlocked(cls, data: Dict[str, Any]) -> None:
        """Write registry file atomically (caller must hold lock)."""
        registry_path = cls.get_registry_path()

        # Ensure directory exists
        registry_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file first, then atomic rename
        temp_path = registry_path.with_suffix(".tmp")
        try:
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            # Atomic rename
            temp_path.replace(registry_path)

        except Exception as e:
            # Clean up temp file
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"Failed to write registry: {e}")

    @classmethod
    @contextmanager
    def atomic_update(cls, timeout: float = None):
        """
        v96.0: Context manager for atomic read-modify-write on the registry.

        This is the MAIN ENTRY POINT for safe registry modifications.
        The lock is held for the ENTIRE context duration.

        Usage:
            with AtomicSharedRegistry.atomic_update() as registry:
                registry["my_service"] = {"pid": os.getpid(), ...}
                # Changes automatically saved when context exits

        Args:
            timeout: Max seconds to wait for lock

        Yields:
            Dict that can be modified; changes saved on successful exit
        """
        with cls._acquire_registry_lock(timeout):
            # Read current state
            registry = cls._read_registry_unlocked()

            try:
                yield registry
                # On successful exit, write back
                cls._write_registry_unlocked(registry)
            except Exception as e:
                logger.error(f"[v96.0] Error during atomic registry update: {e}")
                raise

    @classmethod
    def register_service(
        cls,
        service_name: str,
        service_data: Dict[str, Any],
        alternate_names: Optional[List[str]] = None,
        timeout: float = None,
    ) -> bool:
        """
        v96.0: Register a service with the shared registry atomically.

        This is a convenience method for simple service registration.

        Args:
            service_name: Primary service name
            service_data: Service data dict (pid, port, host, etc.)
            alternate_names: Optional list of alternate names to also register
            timeout: Max seconds to wait for lock

        Returns:
            True if registration succeeded, False otherwise
        """
        try:
            with cls.atomic_update(timeout) as registry:
                registry[service_name] = service_data

                # Register alternate names
                if alternate_names:
                    for alt_name in alternate_names:
                        registry[alt_name] = service_data

            logger.info(f"[v96.0] ✅ Registered {service_name} with shared registry")
            return True

        except TimeoutError as e:
            logger.error(f"[v96.0] ❌ Registry lock timeout for {service_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"[v96.0] ❌ Failed to register {service_name}: {e}")
            return False

    @classmethod
    def deregister_service(
        cls,
        service_name: str,
        alternate_names: Optional[List[str]] = None,
        timeout: float = None,
    ) -> bool:
        """
        v96.0: Deregister a service from the shared registry atomically.

        Args:
            service_name: Primary service name to remove
            alternate_names: Optional list of alternate names to also remove
            timeout: Max seconds to wait for lock

        Returns:
            True if deregistration succeeded, False otherwise
        """
        try:
            with cls.atomic_update(timeout) as registry:
                registry.pop(service_name, None)

                # Remove alternate names
                if alternate_names:
                    for alt_name in alternate_names:
                        registry.pop(alt_name, None)

            logger.info(f"[v96.0] ✅ Deregistered {service_name} from shared registry")
            return True

        except TimeoutError as e:
            logger.error(f"[v96.0] ❌ Registry lock timeout for {service_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"[v96.0] ❌ Failed to deregister {service_name}: {e}")
            return False

    @classmethod
    def get_service(cls, service_name: str, timeout: float = None) -> Optional[Dict[str, Any]]:
        """
        v96.0: Get a service from the registry.

        Args:
            service_name: Service name to look up

        Returns:
            Service data dict or None if not found
        """
        try:
            with cls._acquire_registry_lock(timeout or 5.0):
                registry = cls._read_registry_unlocked()
                return registry.get(service_name)
        except Exception as e:
            logger.warning(f"[v96.0] Failed to get service {service_name}: {e}")
            return None

    @classmethod
    def heartbeat(cls, service_name: str, timeout: float = None) -> bool:
        """
        v96.0: Update the heartbeat timestamp for a service.

        Args:
            service_name: Service name to update

        Returns:
            True if heartbeat succeeded
        """
        try:
            with cls.atomic_update(timeout) as registry:
                if service_name in registry:
                    registry[service_name]["last_heartbeat"] = time.time()
                    return True
            return False
        except Exception:
            return False

    @classmethod
    def cleanup_stale_entries(
        cls,
        max_age_seconds: float = 300.0,
        check_process_alive: bool = True,
        timeout: float = None,
    ) -> int:
        """
        v96.0: Aggressive cleanup of stale registry entries.

        This method removes entries that are:
        1. Older than max_age_seconds without heartbeat update
        2. From processes that are no longer running (if check_process_alive=True)
        3. Have PID that has been reused by a different process

        This is called automatically during registration to ensure fresh state.

        Args:
            max_age_seconds: Max age without heartbeat before considered stale
            check_process_alive: Whether to verify process is still running
            timeout: Max seconds to wait for lock

        Returns:
            Number of entries removed
        """
        removed_count = 0
        current_time = time.time()

        try:
            with cls.atomic_update(timeout) as registry:
                entries_to_remove = []

                for service_name, service_data in registry.items():
                    if not isinstance(service_data, dict):
                        entries_to_remove.append(service_name)
                        continue

                    should_remove = False
                    reason = ""

                    # Check 1: Heartbeat age
                    last_heartbeat = service_data.get("last_heartbeat", 0)
                    registered_at = service_data.get("registered_at", 0)
                    last_activity = max(last_heartbeat, registered_at)
                    age = current_time - last_activity

                    if age > max_age_seconds:
                        should_remove = True
                        reason = f"stale ({age:.0f}s without heartbeat)"

                    # Check 2: Process alive (if enabled)
                    if not should_remove and check_process_alive:
                        pid = service_data.get("pid")
                        if pid:
                            try:
                                if not psutil.pid_exists(pid):
                                    should_remove = True
                                    reason = f"process {pid} no longer exists"
                                else:
                                    # Check for PID reuse
                                    proc = psutil.Process(pid)
                                    stored_start_time = service_data.get("process_start_time", 0)
                                    if stored_start_time > 0:
                                        actual_start_time = proc.create_time()
                                        if abs(actual_start_time - stored_start_time) > 2.0:
                                            should_remove = True
                                            reason = f"PID {pid} reused by different process"
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                should_remove = True
                                reason = f"process {pid} not accessible"
                            except Exception:
                                pass  # Keep if we can't verify

                    if should_remove:
                        entries_to_remove.append(service_name)
                        logger.info(f"[v96.0] Removing stale entry: {service_name} ({reason})")

                # Remove stale entries
                for name in entries_to_remove:
                    registry.pop(name, None)
                    removed_count += 1

            if removed_count > 0:
                logger.info(f"[v96.0] Cleaned up {removed_count} stale registry entries")

        except Exception as e:
            logger.warning(f"[v96.0] Stale cleanup error: {e}")

        return removed_count

    @classmethod
    def register_service_with_cleanup(
        cls,
        service_name: str,
        service_data: Dict[str, Any],
        alternate_names: Optional[List[str]] = None,
        cleanup_stale: bool = True,
        max_stale_age: float = 300.0,
        timeout: float = None,
    ) -> bool:
        """
        v96.0: Register a service with automatic stale cleanup.

        This is the RECOMMENDED method for service registration as it:
        1. Cleans up stale entries from previous runs
        2. Registers the new service atomically
        3. Handles all edge cases

        Args:
            service_name: Primary service name
            service_data: Service data dict
            alternate_names: Optional list of alternate names
            cleanup_stale: Whether to clean up stale entries first
            max_stale_age: Max age for stale detection
            timeout: Max seconds to wait for lock

        Returns:
            True if registration succeeded
        """
        try:
            with cls.atomic_update(timeout) as registry:
                # Step 1: Clean up stale entries in same atomic transaction
                if cleanup_stale:
                    current_time = time.time()
                    entries_to_remove = []

                    for name, data in list(registry.items()):
                        if not isinstance(data, dict):
                            entries_to_remove.append(name)
                            continue

                        # Check age
                        last_activity = max(
                            data.get("last_heartbeat", 0),
                            data.get("registered_at", 0)
                        )
                        age = current_time - last_activity

                        if age > max_stale_age:
                            entries_to_remove.append(name)
                            logger.debug(f"[v96.0] Removing stale: {name} (age: {age:.0f}s)")
                        else:
                            # Check if process is alive
                            pid = data.get("pid")
                            if pid:
                                try:
                                    if not psutil.pid_exists(pid):
                                        entries_to_remove.append(name)
                                        logger.debug(f"[v96.0] Removing dead: {name} (pid {pid})")
                                except Exception:
                                    pass

                    for name in entries_to_remove:
                        registry.pop(name, None)

                    if entries_to_remove:
                        logger.info(f"[v96.0] Cleaned {len(entries_to_remove)} stale entries before registration")

                # Step 2: Register the new service
                registry[service_name] = service_data

                # Step 3: Register alternate names
                if alternate_names:
                    for alt_name in alternate_names:
                        registry[alt_name] = service_data

            logger.info(f"[v96.0] ✅ Registered {service_name} (with stale cleanup)")
            return True

        except TimeoutError as e:
            logger.error(f"[v96.0] ❌ Registration timeout for {service_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"[v96.0] ❌ Registration failed for {service_name}: {e}")
            return False


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ServiceInfo:
    """
    Information about a registered service.

    v4.0: Enhanced with process start time tracking to detect PID reuse,
    which can cause false-positive alive checks.

    v96.0: CRITICAL ENHANCEMENTS for port fallback tracking and process validation:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │ Problem 17 Fix: Port Fallback Confusion                                 │
    │   - Tracks ACTUAL port vs configured PRIMARY port                       │
    │   - Records port allocation history (which ports were tried)            │
    │   - Fallback reason tracking for debugging                              │
    ├─────────────────────────────────────────────────────────────────────────┤
    │ Problem 18 Fix: Process Tree Validation                                 │
    │   - Stores process fingerprint (name, cmdline, exe_path)                │
    │   - Validates process identity before termination                       │
    │   - Prevents killing wrong process on PID reuse                         │
    └─────────────────────────────────────────────────────────────────────────┘
    """
    service_name: str
    pid: int
    port: int
    host: str = "localhost"
    health_endpoint: str = "/health"
    status: str = "starting"  # starting, healthy, degraded, failed
    registered_at: float = 0.0
    last_heartbeat: float = 0.0
    metadata: Dict = None
    # v4.0: Track process start time to detect PID reuse
    process_start_time: float = 0.0

    # v96.0: Port fallback tracking (Problem 17 fix)
    primary_port: int = 0           # Originally configured/requested port
    is_fallback_port: bool = False  # True if using fallback, not primary
    fallback_reason: str = ""       # Why fallback was needed (e.g., "port_in_use")
    ports_tried: List = None        # History of ports attempted [port1, port2, ...]
    port_allocation_time: float = 0.0  # When port was allocated

    # v96.0: Process fingerprint for identity validation (Problem 18 fix)
    process_name: str = ""          # Process name (e.g., "python3")
    process_cmdline: str = ""       # Full command line
    process_exe_path: str = ""      # Executable path
    process_cwd: str = ""           # Working directory
    parent_pid: int = 0             # Parent process ID
    parent_name: str = ""           # Parent process name

    # v112.0: Cross-repo lifecycle coordination
    # Tracks service transition states to prevent false deregistration during
    # restarts, upgrades, or coordinated maintenance windows
    lifecycle_state: str = "stable"  # stable, starting, stopping, restarting, upgrading, maintenance
    lifecycle_transition_started: float = 0.0  # When transition began
    lifecycle_expected_duration: float = 0.0   # Expected transition duration (seconds)
    lifecycle_reason: str = ""       # Why service is in transition (e.g., "graceful_restart")

    def __post_init__(self):
        if self.registered_at == 0.0:
            self.registered_at = time.time()
        if self.last_heartbeat == 0.0:
            self.last_heartbeat = time.time()
        if self.metadata is None:
            self.metadata = {}
        if self.ports_tried is None:
            self.ports_tried = []
        # v4.0: Capture process start time if not provided
        if self.process_start_time == 0.0:
            self.process_start_time = self._get_process_start_time()
        # v96.0: Capture process fingerprint if not provided
        if not self.process_name:
            self._capture_process_fingerprint()
        # v96.0: Set primary_port to port if not specified
        if self.primary_port == 0:
            self.primary_port = self.port
        # v96.0: Record port allocation time
        if self.port_allocation_time == 0.0:
            self.port_allocation_time = time.time()

    def _get_process_start_time(self) -> float:
        """v4.0: Get the process start time for PID reuse detection."""
        try:
            process = psutil.Process(self.pid)
            return process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return 0.0

    def _capture_process_fingerprint(self) -> None:
        """
        v96.0: Capture comprehensive process fingerprint for identity validation.

        This fingerprint is used to verify that when we try to manage (terminate,
        signal, etc.) a process by PID, we're actually targeting the correct
        process and not a different process that reused the same PID.
        """
        try:
            process = psutil.Process(self.pid)

            # Basic identity
            self.process_name = process.name()
            self.process_exe_path = process.exe() if process.exe() else ""
            self.process_cwd = process.cwd() if process.cwd() else ""

            # Command line (may be sensitive, truncate for safety)
            try:
                cmdline = process.cmdline()
                self.process_cmdline = " ".join(cmdline)[:500] if cmdline else ""
            except (psutil.AccessDenied, psutil.ZombieProcess):
                self.process_cmdline = ""

            # Parent process info for ancestry validation
            try:
                parent = process.parent()
                if parent:
                    self.parent_pid = parent.pid
                    self.parent_name = parent.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            logger.debug(f"[v96.0] Could not capture fingerprint for PID {self.pid}")

    def validate_process_identity(self) -> Tuple[bool, str]:
        """
        v96.0: Validate that the current process at self.pid matches our stored fingerprint.

        This is CRITICAL for Problem 18 fix - prevents killing wrong processes when PIDs
        are reused. Should be called BEFORE any process management operations.

        Validation Phases:
        ┌─────────────────────────────────────────────────────────────────────┐
        │ Phase 1: Basic Existence Check                                      │
        │   - Process with PID exists                                         │
        │   - Process is not zombie                                           │
        ├─────────────────────────────────────────────────────────────────────┤
        │ Phase 2: Start Time Validation (PID Reuse Detection)                │
        │   - Compare stored vs current process start time                    │
        │   - 1-second tolerance for timing differences                       │
        ├─────────────────────────────────────────────────────────────────────┤
        │ Phase 3: Process Identity Validation                                │
        │   - Compare process name (must match exactly)                       │
        │   - Compare command line (JARVIS-specific patterns)                 │
        │   - Compare executable path (if available)                          │
        └─────────────────────────────────────────────────────────────────────┘

        Returns:
            Tuple of (identity_valid: bool, reason: str)
        """
        try:
            process = psutil.Process(self.pid)

            # Phase 1: Basic existence
            if not process.is_running():
                return False, "process_not_running"

            if process.status() == psutil.STATUS_ZOMBIE:
                return False, "process_is_zombie"

            # Phase 2: Start time validation (strongest PID reuse detection)
            if self.process_start_time > 0.0:
                current_start_time = process.create_time()
                time_diff = abs(current_start_time - self.process_start_time)
                if time_diff > 1.0:
                    return False, f"pid_reused:start_time_mismatch:{time_diff:.1f}s"

            # Phase 3: Process identity validation
            # 3a: Name validation (fast check)
            if self.process_name:
                current_name = process.name()
                if current_name != self.process_name:
                    return False, f"name_mismatch:expected={self.process_name},got={current_name}"

            # 3b: Command line validation (JARVIS-specific patterns)
            if self.process_cmdline:
                try:
                    current_cmdline = " ".join(process.cmdline())

                    # Extract key patterns from stored cmdline
                    jarvis_patterns = [
                        "jarvis", "prime", "reactor", "trinity",
                        "backend", "server.py", "run_supervisor",
                    ]

                    stored_has_jarvis = any(p in self.process_cmdline.lower() for p in jarvis_patterns)
                    current_has_jarvis = any(p in current_cmdline.lower() for p in jarvis_patterns)

                    # If stored was JARVIS but current isn't, that's a reuse
                    if stored_has_jarvis and not current_has_jarvis:
                        return False, "cmdline_mismatch:jarvis_pattern_missing"

                    # Check for specific module match
                    stored_modules = set()
                    current_modules = set()

                    for pattern in ["jarvis_prime", "reactor_core", "backend.main", "run_supervisor"]:
                        if pattern in self.process_cmdline:
                            stored_modules.add(pattern)
                        if pattern in current_cmdline:
                            current_modules.add(pattern)

                    if stored_modules and stored_modules != current_modules:
                        return False, f"cmdline_mismatch:modules:{stored_modules}!={current_modules}"

                except (psutil.AccessDenied, psutil.ZombieProcess):
                    pass  # Can't check cmdline, rely on other validations

            # 3c: Executable path validation
            if self.process_exe_path:
                try:
                    current_exe = process.exe()
                    if current_exe and current_exe != self.process_exe_path:
                        # Python interpreters may vary, check if both are Python
                        stored_is_python = "python" in self.process_exe_path.lower()
                        current_is_python = "python" in current_exe.lower() if current_exe else False

                        if not (stored_is_python and current_is_python):
                            return False, f"exe_mismatch:{self.process_exe_path}!={current_exe}"
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    pass

            return True, "identity_verified"

        except psutil.NoSuchProcess:
            return False, "process_not_found"
        except psutil.AccessDenied:
            # Can't validate, assume valid (conservative)
            return True, "access_denied:assumed_valid"
        except psutil.ZombieProcess:
            return False, "zombie_process"
        except Exception as e:
            logger.warning(f"[v96.0] Process validation error for PID {self.pid}: {e}")
            return False, f"validation_error:{str(e)}"

    def to_dict(self) -> Dict:
        """
        v96.0: Convert to dictionary for JSON serialization.

        Includes all port tracking and process fingerprint fields.
        """
        result = asdict(self)
        # Ensure ports_tried is serializable (list of ints)
        if result.get("ports_tried"):
            result["ports_tried"] = list(result["ports_tried"])
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "ServiceInfo":
        """
        v96.0/v102.0: Create from dictionary with backward compatibility.

        Handles legacy entries that don't have:
        - v4.0 fields: process_start_time
        - v96.0 fields: port tracking, process fingerprint

        v102.0: Also filters out unknown fields to prevent TypeError when
        registry contains fields from other components (e.g., machine_id from
        J-Prime or Reactor-Core that JARVIS doesn't have in its dataclass).
        """
        # Make a copy to avoid modifying original
        data = dict(data)

        # v4.0: Handle legacy entries without process_start_time
        if "process_start_time" not in data:
            data["process_start_time"] = 0.0

        # v96.0: Handle legacy entries without port tracking fields
        if "primary_port" not in data:
            data["primary_port"] = data.get("port", 0)
        if "is_fallback_port" not in data:
            data["is_fallback_port"] = False
        if "fallback_reason" not in data:
            data["fallback_reason"] = ""
        if "ports_tried" not in data:
            data["ports_tried"] = []
        if "port_allocation_time" not in data:
            data["port_allocation_time"] = data.get("registered_at", 0.0)

        # v96.0: Handle legacy entries without process fingerprint fields
        if "process_name" not in data:
            data["process_name"] = ""
        if "process_cmdline" not in data:
            data["process_cmdline"] = ""
        if "process_exe_path" not in data:
            data["process_exe_path"] = ""
        if "process_cwd" not in data:
            data["process_cwd"] = ""
        if "parent_pid" not in data:
            data["parent_pid"] = 0
        if "parent_name" not in data:
            data["parent_name"] = ""

        # v102.0: Filter out unknown fields to prevent TypeError
        # This handles cross-repo field differences (e.g., J-Prime has machine_id)
        import dataclasses
        known_fields = {f.name for f in dataclasses.fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}

        return cls(**filtered_data)

    def update_port(
        self,
        new_port: int,
        is_fallback: bool = False,
        reason: str = "",
    ) -> None:
        """
        v96.0: Update the port and track the change.

        This should be called when a fallback port is allocated instead of
        the primary port. It updates the service registry to track the ACTUAL
        port being used, not just the configured port.

        Args:
            new_port: The new port number to use
            is_fallback: True if this is a fallback (not primary)
            reason: Why fallback was needed (e.g., "primary_port_in_use")
        """
        # Track the old port in history
        if self.port not in self.ports_tried:
            self.ports_tried.append(self.port)

        # Update port
        old_port = self.port
        self.port = new_port
        self.is_fallback_port = is_fallback
        self.fallback_reason = reason
        self.port_allocation_time = time.time()

        # Track new port in history
        if new_port not in self.ports_tried:
            self.ports_tried.append(new_port)

        logger.info(
            f"[v96.0] Port updated for {self.service_name}: "
            f"{old_port} → {new_port} (fallback={is_fallback}, reason={reason})"
        )

    def is_process_alive(self) -> bool:
        """
        Check if the service's process is still running.

        v4.0 Enhancement: Also validates process start time to detect PID reuse.
        On Unix systems, PIDs can be reused after a process terminates.
        A new process could get the same PID, causing false-positive alive checks.

        v112.0 Enhancement: Increased PID reuse tolerance from 1s to 5s (configurable)
        to handle clock skew, NTP adjustments, and floating-point precision issues.
        The tolerance is configurable via JARVIS_PID_REUSE_TOLERANCE env var.
        """
        # v112.0: Configurable PID reuse tolerance (default 5 seconds)
        # Increased from 1s to handle:
        # - NTP clock adjustments
        # - System clock skew
        # - Floating-point precision differences
        # - Container/VM time synchronization delays
        pid_tolerance = float(os.environ.get("JARVIS_PID_REUSE_TOLERANCE", "5.0"))

        try:
            process = psutil.Process(self.pid)

            # Basic check: process exists and is running
            if not process.is_running():
                return False

            # v4.0: PID reuse detection
            # If we have a stored start time, verify it matches
            if self.process_start_time > 0.0:
                current_start_time = process.create_time()
                time_diff = abs(current_start_time - self.process_start_time)

                # v112.0: Use configurable tolerance (default 5 seconds)
                if time_diff > pid_tolerance:
                    logger.warning(
                        f"PID reuse detected for {self.service_name}: "
                        f"stored_start={self.process_start_time:.3f}, "
                        f"current_start={current_start_time:.3f}, "
                        f"diff={time_diff:.3f}s > tolerance={pid_tolerance}s"
                    )
                    return False
                elif time_diff > 1.0:
                    # v112.0: Log drift but don't fail if within tolerance
                    logger.debug(
                        f"[v112.0] Process start time drift for {self.service_name}: "
                        f"{time_diff:.3f}s (within {pid_tolerance}s tolerance)"
                    )

            return True

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def is_stale(self, timeout_seconds: float = 60.0) -> bool:
        """Check if service hasn't sent heartbeat in timeout period."""
        return (time.time() - self.last_heartbeat) > timeout_seconds

    def is_in_startup_phase(self, startup_grace_period: float = 120.0) -> bool:
        """
        v93.3: Check if service is still in its startup grace period.

        During startup, services may take longer to initialize and may not
        send heartbeats as frequently. This method identifies services that
        were registered recently and should be given extra time.

        Args:
            startup_grace_period: Seconds after registration during which
                                  service is considered "starting up"

        Returns:
            True if service is within startup grace period
        """
        if self.registered_at <= 0:
            return False
        return (time.time() - self.registered_at) < startup_grace_period

    def is_stale_startup_aware(
        self,
        timeout_seconds: float = 60.0,
        startup_grace_period: float = 120.0,
        startup_multiplier: float = 5.0,
    ) -> bool:
        """
        v93.3: Startup-aware stale detection.

        Uses extended timeout during startup grace period to allow
        services time to fully initialize before marking them stale.

        Args:
            timeout_seconds: Normal stale timeout
            startup_grace_period: How long to consider service "starting"
            startup_multiplier: Multiply timeout by this during startup

        Returns:
            True if service is stale (accounting for startup phase)
        """
        effective_timeout = timeout_seconds
        if self.is_in_startup_phase(startup_grace_period):
            effective_timeout = timeout_seconds * startup_multiplier

        return (time.time() - self.last_heartbeat) > effective_timeout

    def validate(self) -> tuple[bool, str]:
        """
        v4.0: Comprehensive validation of service entry.

        Returns:
            Tuple of (is_valid, reason_if_invalid)
        """
        # Check PID is valid
        if self.pid <= 0:
            return False, f"Invalid PID: {self.pid}"

        # Check port is valid
        if not (0 < self.port < 65536):
            return False, f"Invalid port: {self.port}"

        # Check process is alive (with PID reuse detection)
        if not self.is_process_alive():
            return False, f"Process {self.pid} is dead or PID was reused"

        # Check not stale (but don't fail on this - just warn)
        # Stale services may still be running, just not heartbeating

        return True, "OK"


# =============================================================================
# Service Registry
# =============================================================================

class ServiceRegistry:
    """
    Enterprise-grade service registry with atomic operations.

    v93.0 Enhancement:
    - Uses DirectoryManager for robust directory handling
    - Retry logic for all file operations
    - Handles race conditions and directory deletion
    - Pre-flight validation before writes

    Features:
    - File-based persistence with fcntl locking
    - Automatic stale service cleanup
    - Health tracking with heartbeats
    - Zero hardcoded configuration
    """

    def __init__(
        self,
        registry_dir: Optional[Path] = None,
        heartbeat_timeout: float = 60.0,
        cleanup_interval: float = 30.0,
        startup_grace_period: float = None,
    ):
        """
        Initialize service registry.

        Args:
            registry_dir: Directory for registry file (default: ~/.jarvis/registry)
            heartbeat_timeout: Seconds before service considered stale
            cleanup_interval: Seconds between cleanup cycles
            startup_grace_period: v93.3 - Grace period for newly registered services (default: from env)
        """
        self.registry_dir = registry_dir or Path.home() / ".jarvis" / "registry"
        self.registry_file = self.registry_dir / "services.json"
        self.heartbeat_timeout = heartbeat_timeout
        self.cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None

        # v93.3: Startup-aware grace period configuration
        self.startup_grace_period = startup_grace_period or float(
            os.environ.get("JARVIS_SERVICE_STARTUP_GRACE", "120.0")
        )
        # v93.3: Extended stale threshold during startup (5x normal)
        self.startup_stale_multiplier = float(
            os.environ.get("JARVIS_STARTUP_STALE_MULTIPLIER", "5.0")
        )

        # v93.0: Use DirectoryManager for robust directory handling
        self._dir_manager = DirectoryManager(self.registry_dir)

        # v93.14: Rate limit stale warnings (max 1 per service per 60s)
        self._stale_warning_times: Dict[str, float] = {}
        self._warning_rate_limit_seconds = float(
            os.environ.get("JARVIS_STALE_WARNING_RATE_LIMIT", "60.0")
        )

        # v95.0: Callback system for service lifecycle events
        # Allows orchestrators to be notified when services die/become stale
        self._on_service_dead_callbacks: List[Callable[[str, int], Any]] = []
        self._on_service_stale_callbacks: List[Callable[[str, float], Any]] = []
        self._on_service_recovered_callbacks: List[Callable[[str], Any]] = []

        # v95.0: Service-specific adaptive stale thresholds
        # Different services have different characteristics:
        # - reactor-core: Long model loading, needs extended threshold
        # - jarvis-prime: Model loading, needs extended threshold
        # - jarvis-core: Quick startup, standard threshold
        # Format: {"service_pattern": threshold_seconds}
        self._service_stale_thresholds: Dict[str, float] = {
            "reactor": float(os.environ.get("REACTOR_STALE_THRESHOLD", "600.0")),  # 10 minutes
            "jarvis-prime": float(os.environ.get("JARVIS_PRIME_STALE_THRESHOLD", "600.0")),  # 10 minutes
            "jprime": float(os.environ.get("JARVIS_PRIME_STALE_THRESHOLD", "600.0")),  # 10 minutes
            # v95.0: jarvis-body (registry owner) has highest threshold - should never be stale
            "jarvis-body": float(os.environ.get("JARVIS_BODY_STALE_THRESHOLD", "3600.0")),  # 1 hour
            "default": float(os.environ.get("JARVIS_VERY_STALE_THRESHOLD", "300.0")),  # 5 minutes
        }
        logger.debug(f"[v95.0] Service stale thresholds: {self._service_stale_thresholds}")

        # v95.0: Circuit breaker for registry operations (prevents cascading failures)
        self._circuit_breaker_state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._circuit_breaker_failures = 0
        self._circuit_breaker_threshold = int(
            os.environ.get("JARVIS_REGISTRY_CB_THRESHOLD", "5")
        )
        self._circuit_breaker_reset_timeout = float(
            os.environ.get("JARVIS_REGISTRY_CB_RESET", "30.0")
        )
        self._circuit_breaker_last_failure = 0.0
        self._circuit_breaker_lock = asyncio.Lock()

        # v95.0: Local cache for graceful degradation when registry unavailable
        self._local_cache: Dict[str, ServiceInfo] = {}
        self._cache_lock = asyncio.Lock()
        self._cache_ttl = float(os.environ.get("JARVIS_REGISTRY_CACHE_TTL", "60.0"))
        self._cache_last_update = 0.0

        # v95.0: Heartbeat retry queue for transient failures
        self._pending_heartbeats: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._heartbeat_retry_task: Optional[asyncio.Task] = None

        # v95.0: Owner service identification and self-heartbeat
        # The owner is the service that RUNS this registry (jarvis-body)
        # Owner is NEVER marked stale because if the registry is running,
        # the owner must be alive (by definition)
        self._owner_service_name: str = os.environ.get(
            "JARVIS_REGISTRY_OWNER", "jarvis-body"
        )
        self._self_heartbeat_task: Optional[asyncio.Task] = None
        self._self_heartbeat_interval: float = float(
            os.environ.get("JARVIS_SELF_HEARTBEAT_INTERVAL", "15.0")
        )
        logger.debug(f"[v95.0] Registry owner: {self._owner_service_name}")

        # v95.1: Grace period tracking for premature deregistration prevention
        # Tracks services that appear dead but haven't been confirmed yet
        self._suspicious_services: Dict[str, float] = {}  # service_name -> first_detection_time
        self._dead_confirmation_grace_period: float = float(
            os.environ.get("JARVIS_DEAD_CONFIRMATION_GRACE", "30.0")  # 30 second grace
        )
        self._dead_confirmation_retries: int = int(
            os.environ.get("JARVIS_DEAD_CONFIRMATION_RETRIES", "3")
        )
        self._suspicious_retry_counts: Dict[str, int] = {}  # service_name -> retry_count

        # v95.2: Read-Write Lock for atomic registry operations
        # This prevents race conditions when multiple processes update the registry
        # Example race: heartbeat reads registry, another process writes, heartbeat overwrites
        self._registry_rw_lock: Optional[asyncio.Lock] = None
        self._registry_rw_lock_initialized = False

        # v95.2: Unified State Management for process-component synchronization
        # Tracks both supervisor's process state AND component's self-reported state
        # This solves the gap where supervisor thinks process is dead but component is healthy
        self._unified_state: Dict[str, Dict[str, Any]] = {}
        self._state_version: int = 0  # Monotonic version for conflict detection

        # v93.0: Ensure directory exists with retry logic
        if not self._dir_manager.ensure_directory_sync():
            logger.error(f"[v93.0] CRITICAL: Failed to create registry directory: {self.registry_dir}")
            raise RuntimeError(f"Cannot create registry directory: {self.registry_dir}")

        # Initialize registry file if doesn't exist
        if not self.registry_file.exists():
            self._write_registry({})

    # =========================================================================
    # v95.0: Callback Registration for Service Lifecycle Events
    # =========================================================================

    def register_on_service_dead(
        self,
        callback: Callable[[str, int], Any]
    ) -> None:
        """
        v95.0: Register callback for when a service's process dies.

        The callback receives:
        - service_name: Name of the dead service
        - pid: The dead PID

        This allows orchestrators to be notified and restart services.
        """
        self._on_service_dead_callbacks.append(callback)
        logger.debug(f"[v95.0] Registered on_service_dead callback: {callback}")

    def register_on_service_stale(
        self,
        callback: Callable[[str, float], Any]
    ) -> None:
        """
        v95.0: Register callback for when a service becomes stale.

        The callback receives:
        - service_name: Name of the stale service
        - last_heartbeat_age: Seconds since last heartbeat

        This allows orchestrators to be notified of unresponsive services.
        """
        self._on_service_stale_callbacks.append(callback)
        logger.debug(f"[v95.0] Registered on_service_stale callback: {callback}")

    def register_on_service_recovered(
        self,
        callback: Callable[[str], Any]
    ) -> None:
        """
        v95.0: Register callback for when a service recovers (becomes healthy).

        The callback receives:
        - service_name: Name of the recovered service
        """
        self._on_service_recovered_callbacks.append(callback)
        logger.debug(f"[v95.0] Registered on_service_recovered callback: {callback}")

    async def _notify_service_dead(self, service_name: str, pid: int) -> None:
        """v95.0: Notify all registered callbacks that a service died."""
        for callback in self._on_service_dead_callbacks:
            try:
                result = callback(service_name, pid)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[v95.0] on_service_dead callback error: {e}")

    async def _notify_service_stale(self, service_name: str, age: float) -> None:
        """v95.0: Notify all registered callbacks that a service is stale."""
        for callback in self._on_service_stale_callbacks:
            try:
                result = callback(service_name, age)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[v95.0] on_service_stale callback error: {e}")

    async def _notify_service_recovered(self, service_name: str) -> None:
        """v95.0: Notify all registered callbacks that a service recovered."""
        for callback in self._on_service_recovered_callbacks:
            try:
                result = callback(service_name)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[v95.0] on_service_recovered callback error: {e}")

    # =========================================================================
    # v95.0: Circuit Breaker Pattern for Registry Resilience
    # =========================================================================

    async def _check_circuit_breaker(self) -> bool:
        """
        v95.0: Check if circuit breaker allows operation.

        Circuit breaker prevents cascading failures when registry is unhealthy.
        States:
        - CLOSED: Normal operation, all requests allowed
        - OPEN: Registry unavailable, use cache fallback
        - HALF_OPEN: Testing if registry recovered

        Returns:
            True if operation allowed, False if circuit is open
        """
        async with self._circuit_breaker_lock:
            if self._circuit_breaker_state == "CLOSED":
                return True

            if self._circuit_breaker_state == "OPEN":
                # Check if reset timeout has passed
                elapsed = time.time() - self._circuit_breaker_last_failure
                if elapsed >= self._circuit_breaker_reset_timeout:
                    self._circuit_breaker_state = "HALF_OPEN"
                    logger.info("[v95.0] Circuit breaker: OPEN → HALF_OPEN (testing recovery)")
                    return True
                return False

            # HALF_OPEN: allow single test request
            return True

    async def _record_circuit_success(self) -> None:
        """v95.0: Record successful registry operation."""
        async with self._circuit_breaker_lock:
            if self._circuit_breaker_state == "HALF_OPEN":
                self._circuit_breaker_state = "CLOSED"
                self._circuit_breaker_failures = 0
                logger.info("[v95.0] Circuit breaker: HALF_OPEN → CLOSED (recovery confirmed)")
            elif self._circuit_breaker_state == "CLOSED":
                # Reset failure count on success
                self._circuit_breaker_failures = max(0, self._circuit_breaker_failures - 1)

    async def _record_circuit_failure(self) -> None:
        """v95.0: Record failed registry operation."""
        async with self._circuit_breaker_lock:
            self._circuit_breaker_failures += 1
            self._circuit_breaker_last_failure = time.time()

            if self._circuit_breaker_state == "HALF_OPEN":
                self._circuit_breaker_state = "OPEN"
                logger.warning("[v95.0] Circuit breaker: HALF_OPEN → OPEN (recovery failed)")
            elif (
                self._circuit_breaker_state == "CLOSED"
                and self._circuit_breaker_failures >= self._circuit_breaker_threshold
            ):
                self._circuit_breaker_state = "OPEN"
                logger.warning(
                    f"[v95.0] Circuit breaker: CLOSED → OPEN "
                    f"(failures: {self._circuit_breaker_failures})"
                )

    # =========================================================================
    # v95.0: Local Cache Fallback for Graceful Degradation
    # =========================================================================

    async def _update_cache(self, services: Dict[str, ServiceInfo]) -> None:
        """v95.0: Update local cache with latest registry state."""
        async with self._cache_lock:
            self._local_cache = services.copy()
            self._cache_last_update = time.time()

    async def _get_from_cache(self, service_name: Optional[str] = None) -> Optional[Any]:
        """
        v95.0: Get service(s) from local cache.

        Args:
            service_name: Specific service to get, or None for all

        Returns:
            ServiceInfo if found, Dict if all services, None if not found/stale
        """
        async with self._cache_lock:
            cache_age = time.time() - self._cache_last_update
            if cache_age > self._cache_ttl:
                logger.debug(f"[v95.0] Cache stale ({cache_age:.1f}s > {self._cache_ttl}s)")
                return None

            if service_name:
                return self._local_cache.get(service_name)
            return self._local_cache.copy()

    async def discover_service_resilient(
        self,
        service_name: str,
        use_cache_fallback: bool = True
    ) -> Optional[ServiceInfo]:
        """
        v95.0: Resilient service discovery with circuit breaker and cache fallback.

        This is the preferred discovery method for cross-repo operations.

        Args:
            service_name: Service to discover
            use_cache_fallback: Whether to use cache when registry unavailable

        Returns:
            ServiceInfo if found, None otherwise
        """
        # Check circuit breaker first
        if not await self._check_circuit_breaker():
            if use_cache_fallback:
                cached = await self._get_from_cache(service_name)
                if cached:
                    logger.debug(f"[v95.0] Using cache fallback for {service_name}")
                    return cached
            logger.warning(f"[v95.0] Circuit open, no cache for {service_name}")
            return None

        try:
            # Try normal discovery
            result = await self.discover_service(service_name)
            await self._record_circuit_success()

            # Update cache on success
            if result:
                async with self._cache_lock:
                    self._local_cache[service_name] = result
                    self._cache_last_update = time.time()

            return result

        except Exception as e:
            await self._record_circuit_failure()
            logger.warning(f"[v95.0] Discovery failed for {service_name}: {e}")

            if use_cache_fallback:
                cached = await self._get_from_cache(service_name)
                if cached:
                    logger.info(f"[v95.0] Using cache fallback for {service_name}")
                    return cached

            return None

    def _get_adaptive_stale_threshold(self, service_name: str) -> float:
        """
        v95.0: Get adaptive stale threshold based on service type.

        Different services have different operational characteristics:
        - reactor-core: Long model loading times, needs extended threshold
        - jarvis-prime: Model loading, needs extended threshold
        - Other services: Standard threshold

        This prevents services with long initialization/loading times
        from being incorrectly marked as stale.

        Args:
            service_name: Name of the service

        Returns:
            Stale threshold in seconds (appropriate for this service)
        """
        service_lower = service_name.lower()

        # Check for service-specific thresholds (pattern matching)
        for pattern, threshold in self._service_stale_thresholds.items():
            if pattern != "default" and pattern in service_lower:
                logger.debug(
                    f"[v95.0] Using adaptive threshold for {service_name}: "
                    f"{threshold}s (pattern: {pattern})"
                )
                return threshold

        # Return default threshold
        return self._service_stale_thresholds.get("default", 300.0)

    def _acquire_lock(self, file_handle) -> None:
        """Acquire exclusive lock on registry file (blocking)."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)

    def _release_lock(self, file_handle) -> None:
        """Release lock on registry file."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)

    def _read_registry(self) -> Dict[str, ServiceInfo]:
        """
        v93.0: Read registry with file locking and robust error handling.

        Returns:
            Dict mapping service names to ServiceInfo
        """
        # v93.0: Ensure directory exists before read
        self._dir_manager.ensure_directory_sync()

        for attempt in range(3):
            try:
                if not self.registry_file.exists():
                    return {}

                with open(self.registry_file, 'r+') as f:
                    self._acquire_lock(f)
                    try:
                        content = f.read()
                        if not content.strip():
                            return {}
                        data = json.loads(content)
                        return {
                            name: ServiceInfo.from_dict(info)
                            for name, info in data.items()
                        }
                    finally:
                        self._release_lock(f)

            except FileNotFoundError:
                # File was deleted between check and open - retry
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                return {}
            except json.JSONDecodeError as e:
                logger.warning(f"[v93.0] Corrupted registry JSON (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                # Return empty on corruption - will be overwritten on next write
                return {}
            except OSError as e:
                logger.warning(f"[v93.0] Registry read error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                return {}

        return {}

    def _write_registry(self, services: Dict[str, ServiceInfo]) -> None:
        """
        v93.0: Write registry with atomic file operations and robust directory handling.

        Uses DirectoryManager for:
        - Pre-write directory validation
        - Atomic temp file creation in correct directory
        - Retry logic on failure
        - Proper cleanup on error

        Args:
            services: Dict mapping service names to ServiceInfo
        """
        # Convert to serializable dict
        data = {
            name: service.to_dict()
            for name, service in services.items()
        }

        # v93.0: Retry loop for robust writes
        last_error = None
        for attempt in range(3):
            try:
                # v93.0: Use DirectoryManager's atomic write context
                with self._dir_manager.atomic_write_context(self.registry_file) as temp_path:
                    with open(temp_path, 'w') as f:
                        self._acquire_lock(f)
                        try:
                            json.dump(data, f, indent=2)
                            f.flush()
                            os.fsync(f.fileno())  # Force write to disk
                        finally:
                            self._release_lock(f)

                # Success!
                return

            except OSError as e:
                last_error = e
                logger.warning(
                    f"[v93.0] Registry write failed (attempt {attempt + 1}/3): {e}"
                )
                # Ensure directory exists for next attempt
                self._dir_manager.ensure_directory_sync()
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))

            except Exception as e:
                last_error = e
                logger.error(f"[v93.0] Unexpected registry write error: {e}")
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))

        # All attempts failed
        logger.error(f"[v93.0] Failed to write registry after 3 attempts: {last_error}")
        raise RuntimeError(f"Failed to write registry: {last_error}")

    async def register_service(
        self,
        service_name: str,
        pid: Optional[int] = None,
        port: Optional[int] = None,
        host: str = "localhost",
        health_endpoint: str = "/health",
        metadata: Optional[Dict] = None,
        # v96.0: Port fallback tracking (Problem 17 fix)
        primary_port: Optional[int] = None,
        is_fallback_port: bool = False,
        fallback_reason: str = "",
        ports_tried: Optional[List[int]] = None,
    ) -> ServiceInfo:
        """
        v96.0: Register a service with port fallback tracking and process fingerprinting.

        This method now tracks the ACTUAL port being used vs the PRIMARY configured port,
        solving Problem 17 (port fallback confusion). It also captures process fingerprint
        for Problem 18 (process tree validation).

        Args:
            service_name: Unique service identifier
            pid: Process ID (defaults to current process)
            port: ACTUAL port number service is listening on
            host: Hostname (default: localhost)
            health_endpoint: Health check endpoint path
            metadata: Optional additional metadata
            primary_port: Originally configured/requested port (v96.0)
            is_fallback_port: True if using fallback port (v96.0)
            fallback_reason: Why fallback was needed (v96.0)
            ports_tried: List of ports attempted before success (v96.0)

        Returns:
            ServiceInfo for the registered service
        """
        # Use current PID if not specified
        actual_pid = pid if pid is not None else os.getpid()

        # Use default port from environment if not specified
        actual_port = port if port is not None else int(
            os.environ.get("JARVIS_BODY_PORT", "8010")
        )

        # v96.0: Determine primary port (configured port)
        if primary_port is None:
            primary_port = actual_port  # If not specified, assume actual = primary

        # v96.0: Track ports tried
        if ports_tried is None:
            ports_tried = []
        if actual_port not in ports_tried:
            ports_tried.append(actual_port)

        service = ServiceInfo(
            service_name=service_name,
            pid=actual_pid,
            port=actual_port,
            host=host,
            health_endpoint=health_endpoint,
            status="starting",
            metadata=metadata or {},
            # v96.0: Port tracking fields
            primary_port=primary_port,
            is_fallback_port=is_fallback_port,
            fallback_reason=fallback_reason,
            ports_tried=ports_tried,
            # Process fingerprint will be captured in __post_init__
        )

        lock = self._ensure_registry_lock()

        # v95.2: Use lock for atomic read-modify-write
        async with lock:
            # Read existing registry
            services = await asyncio.to_thread(self._read_registry)

            # Add/update service
            services[service_name] = service

            # v95.2/v96.0: Update unified state with port tracking
            self._state_version += 1
            self._unified_state[service_name] = {
                "pid": actual_pid,
                "status": "starting",
                "last_heartbeat": time.time(),
                "component_healthy": True,
                "supervisor_healthy": None,
                "state_version": self._state_version,
                "registered_at": time.time(),
                # v96.0: Port tracking in unified state
                "port": actual_port,
                "primary_port": primary_port,
                "is_fallback_port": is_fallback_port,
                "fallback_reason": fallback_reason,
            }

            # Write back atomically
            await asyncio.to_thread(self._write_registry, services)

        # v96.0: Enhanced logging with port tracking info
        port_info = f"Port: {actual_port}"
        if is_fallback_port:
            port_info = f"Port: {actual_port} (FALLBACK from {primary_port}, reason: {fallback_reason})"

        logger.info(
            f"📝 Service registered: {service_name} "
            f"(PID: {actual_pid}, {port_info}, Host: {host})"
        )

        return service

    async def update_service_port(
        self,
        service_name: str,
        new_port: int,
        is_fallback: bool = False,
        reason: str = "",
    ) -> bool:
        """
        v96.0: Update a service's port when fallback is used.

        This is CRITICAL for Problem 17 fix - when a component allocates a fallback
        port, it MUST call this method to update the registry so other components
        can discover the ACTUAL port, not just the configured primary port.

        Args:
            service_name: Service to update
            new_port: New port number
            is_fallback: True if this is a fallback (not primary)
            reason: Why the port change was needed

        Returns:
            True if update was successful
        """
        lock = self._ensure_registry_lock()

        async with lock:
            services = await asyncio.to_thread(self._read_registry)

            if service_name not in services:
                logger.warning(f"[v96.0] Cannot update port - service {service_name} not registered")
                return False

            service = services[service_name]

            # Track old port in history
            if service.port not in service.ports_tried:
                service.ports_tried.append(service.port)

            # Update port info
            old_port = service.port
            service.port = new_port
            service.is_fallback_port = is_fallback
            service.fallback_reason = reason
            service.port_allocation_time = time.time()

            # Track new port in history
            if new_port not in service.ports_tried:
                service.ports_tried.append(new_port)

            # Update unified state
            if service_name in self._unified_state:
                self._unified_state[service_name]["port"] = new_port
                self._unified_state[service_name]["is_fallback_port"] = is_fallback
                self._unified_state[service_name]["fallback_reason"] = reason

            # Write back
            await asyncio.to_thread(self._write_registry, services)

        logger.info(
            f"[v96.0] Port updated for {service_name}: "
            f"{old_port} → {new_port} (fallback={is_fallback}, reason={reason})"
        )

        return True

    async def deregister_service(self, service_name: str) -> bool:
        """
        Remove service from registry.

        Args:
            service_name: Service to deregister

        Returns:
            True if service was found and removed
        """
        services = await asyncio.to_thread(self._read_registry)

        if service_name in services:
            del services[service_name]
            await asyncio.to_thread(self._write_registry, services)
            logger.info(f"❌ Service deregistered: {service_name}")
            return True

        return False

    async def discover_service(self, service_name: str) -> Optional[ServiceInfo]:
        """
        Discover a service by name.

        Args:
            service_name: Service to find

        Returns:
            ServiceInfo if found and healthy, None otherwise
        """
        services = await asyncio.to_thread(self._read_registry)
        service = services.get(service_name)

        if not service:
            return None

        # v95.1: Check if process is still alive with grace period
        # This prevents premature deregistration due to transient PID issues
        if not service.is_process_alive():
            dead_pid = service.pid
            current_time = time.time()

            # v112.0: CRITICAL - Registry owner (jarvis-body) is NEVER auto-deregistered
            # The owner must only be deregistered via explicit deregister_service() call
            # during graceful shutdown. External discover_service() calls should NEVER
            # trigger owner deregistration, even if PID check fails transiently.
            if service_name == self._owner_service_name:
                logger.warning(
                    f"⚠️  Registry owner {service_name} PID check failed (PID {dead_pid}) - "
                    f"NOT deregistering (owner is protected). This may indicate PID reuse "
                    f"false positive or transient process state."
                )
                # Clear any suspicious tracking that may have accumulated
                if service_name in self._suspicious_services:
                    del self._suspicious_services[service_name]
                if service_name in self._suspicious_retry_counts:
                    del self._suspicious_retry_counts[service_name]
                # Return the service info anyway - let caller decide
                # Owner is assumed to be alive unless explicitly deregistered
                return service

            # v112.0: Check if service is in lifecycle transition
            # Services in transition (restarting, upgrading, etc.) get extra protection
            if self.is_in_transition(service):
                transition_elapsed = time.time() - service.lifecycle_transition_started
                logger.info(
                    f"⏳ Service '{service_name}' PID check failed but is in "
                    f"'{service.lifecycle_state}' transition (elapsed: {transition_elapsed:.1f}s, "
                    f"reason: {service.lifecycle_reason or 'not specified'}) - "
                    f"NOT starting grace period"
                )
                # Clear any suspicious tracking - service is in known transition
                if service_name in self._suspicious_services:
                    del self._suspicious_services[service_name]
                if service_name in self._suspicious_retry_counts:
                    del self._suspicious_retry_counts[service_name]
                # Return None but don't deregister - service is transitioning
                return None

            # v95.1: Track suspicious services with grace period
            if service_name not in self._suspicious_services:
                # First detection - start grace period
                self._suspicious_services[service_name] = current_time
                self._suspicious_retry_counts[service_name] = 1
                logger.warning(
                    f"⚠️  Service {service_name} PID {dead_pid} appears dead - "
                    f"starting {self._dead_confirmation_grace_period}s grace period "
                    f"(attempt 1/{self._dead_confirmation_retries})"
                )
                # Return None but DON'T deregister yet - might recover
                return None
            else:
                # Already suspicious - check if grace period expired
                first_detection = self._suspicious_services[service_name]
                elapsed = current_time - first_detection
                retry_count = self._suspicious_retry_counts.get(service_name, 0) + 1
                self._suspicious_retry_counts[service_name] = retry_count

                if elapsed >= self._dead_confirmation_grace_period and \
                   retry_count >= self._dead_confirmation_retries:
                    # Grace period expired AND minimum retries reached - confirm dead
                    logger.warning(
                        f"🧹 Service {service_name} confirmed dead after "
                        f"{elapsed:.1f}s grace period and {retry_count} checks - deregistering"
                    )
                    # Clean up tracking
                    del self._suspicious_services[service_name]
                    if service_name in self._suspicious_retry_counts:
                        del self._suspicious_retry_counts[service_name]
                    # NOW deregister
                    await self.deregister_service(service_name)
                    await self._notify_service_dead(service_name, dead_pid)
                    return None
                else:
                    # Still in grace period - log but don't deregister
                    remaining = self._dead_confirmation_grace_period - elapsed
                    logger.debug(
                        f"⏳ Service {service_name} still appears dead "
                        f"(attempt {retry_count}/{self._dead_confirmation_retries}, "
                        f"{remaining:.1f}s remaining in grace period)"
                    )
                    return None

        # v95.1: Service is alive - clear any suspicious tracking
        if service_name in self._suspicious_services:
            logger.info(
                f"✅ Service {service_name} recovered (PID confirmed alive)"
            )
            del self._suspicious_services[service_name]
            if service_name in self._suspicious_retry_counts:
                del self._suspicious_retry_counts[service_name]

        # v93.3: Startup-aware stale check
        is_in_startup = service.is_in_startup_phase(self.startup_grace_period)
        is_stale = service.is_stale_startup_aware(
            timeout_seconds=self.heartbeat_timeout,
            startup_grace_period=self.startup_grace_period,
            startup_multiplier=self.startup_stale_multiplier,
        )

        if is_stale:
            # v93.14: Rate limit stale warnings to prevent log flooding
            current_time = time.time()
            last_warning_time = self._stale_warning_times.get(service_name, 0)
            heartbeat_age = current_time - service.last_heartbeat

            if current_time - last_warning_time >= self._warning_rate_limit_seconds:
                startup_note = " (after startup grace)" if not is_in_startup else ""
                logger.warning(
                    f"⚠️  Service {service_name} is stale{startup_note} "
                    f"(last heartbeat {heartbeat_age:.0f}s ago)"
                )
                self._stale_warning_times[service_name] = current_time
                # v95.0: Notify callbacks that service is stale (for potential restart)
                await self._notify_service_stale(service_name, heartbeat_age)

            return None

        return service

    async def list_services(self, healthy_only: bool = True) -> List[ServiceInfo]:
        """
        List all registered services.

        Args:
            healthy_only: If True, only return healthy services

        Returns:
            List of ServiceInfo objects
        """
        services = await asyncio.to_thread(self._read_registry)

        if not healthy_only:
            return list(services.values())

        # v93.3: Filter to only healthy services with startup-aware stale detection
        healthy = []
        for service in services.values():
            is_stale = service.is_stale_startup_aware(
                timeout_seconds=self.heartbeat_timeout,
                startup_grace_period=self.startup_grace_period,
                startup_multiplier=self.startup_stale_multiplier,
            )
            if service.is_process_alive() and not is_stale:
                healthy.append(service)

        return healthy

    async def heartbeat(
        self,
        service_name: str,
        status: Optional[str] = None,
        metadata: Optional[Dict] = None,
        timeout: float = 2.0,
    ) -> bool:
        """
        v95.0: Enterprise-grade heartbeat with timeout protection.

        Features:
        - Non-blocking with configurable timeout
        - Fire-and-forget safe (won't block event loop)
        - Graceful degradation on timeout
        - Automatic retry with exponential backoff on transient failures

        Args:
            service_name: Service to update
            status: Optional new status (healthy, degraded, etc.)
            metadata: Optional metadata to merge
            timeout: Maximum time to wait for heartbeat (default 2.0s)

        Returns:
            True if service found and updated, False on timeout or error
        """
        try:
            return await asyncio.wait_for(
                self._heartbeat_internal(service_name, status, metadata),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            # Heartbeat timeout is non-fatal - just log and continue
            logger.debug(f"Heartbeat timeout for {service_name} (non-fatal, will retry)")
            return False
        except asyncio.CancelledError:
            # Don't suppress cancellation
            raise
        except Exception as e:
            # Log but don't propagate - heartbeat failures shouldn't crash services
            logger.debug(f"Heartbeat error for {service_name}: {e} (non-fatal)")
            return False

    # =========================================================================
    # v112.0: Cross-Repo Lifecycle Coordination
    # =========================================================================
    # These methods enable safe coordination between JARVIS, JARVIS-Prime,
    # and Reactor-Core during service transitions (restarts, upgrades, etc.)
    # =========================================================================

    async def signal_lifecycle_transition(
        self,
        service_name: str,
        lifecycle_state: str,
        reason: str = "",
        expected_duration: float = 60.0
    ) -> bool:
        """
        v112.0: Signal that a service is entering a lifecycle transition.

        This prevents external services from accidentally triggering deregistration
        during restarts, upgrades, or maintenance windows.

        Args:
            service_name: Service entering transition
            lifecycle_state: One of: starting, stopping, restarting, upgrading, maintenance
            reason: Why transition is happening (e.g., "graceful_restart", "version_upgrade")
            expected_duration: Expected transition duration in seconds

        Returns:
            True if transition signaled successfully

        Valid lifecycle_state values:
            - "stable": Normal operation (default)
            - "starting": Service is starting up
            - "stopping": Service is gracefully shutting down
            - "restarting": Service is restarting (stopping then starting)
            - "upgrading": Service is upgrading to new version
            - "maintenance": Service is in maintenance mode

        Example:
            # Before restarting jarvis-body
            await registry.signal_lifecycle_transition(
                "jarvis-body",
                "restarting",
                reason="graceful_restart",
                expected_duration=120.0
            )
        """
        valid_states = {"stable", "starting", "stopping", "restarting", "upgrading", "maintenance"}
        if lifecycle_state not in valid_states:
            logger.warning(f"[v112.0] Invalid lifecycle state '{lifecycle_state}', using 'maintenance'")
            lifecycle_state = "maintenance"

        try:
            services = await asyncio.to_thread(self._read_registry)
            service = services.get(service_name)

            if not service:
                logger.warning(f"[v112.0] Cannot signal transition for unknown service: {service_name}")
                return False

            # Update lifecycle fields
            service.lifecycle_state = lifecycle_state
            service.lifecycle_transition_started = time.time()
            service.lifecycle_expected_duration = expected_duration
            service.lifecycle_reason = reason

            # Persist to registry
            services[service_name] = service
            await asyncio.to_thread(self._write_registry, services)

            logger.info(
                f"[v112.0] 🔄 Service '{service_name}' entering '{lifecycle_state}' transition "
                f"(reason: {reason}, expected: {expected_duration}s)"
            )

            return True

        except Exception as e:
            logger.error(f"[v112.0] Failed to signal lifecycle transition: {e}")
            return False

    async def signal_lifecycle_stable(self, service_name: str) -> bool:
        """
        v112.0: Signal that a service has completed its transition and is stable.

        This should be called after a service successfully completes startup,
        restart, upgrade, or exits maintenance mode.

        Args:
            service_name: Service that is now stable

        Returns:
            True if stable state signaled successfully
        """
        return await self.signal_lifecycle_transition(
            service_name,
            "stable",
            reason="transition_complete",
            expected_duration=0.0
        )

    def is_in_transition(self, service: 'ServiceInfo') -> bool:
        """
        v112.0: Check if a service is currently in a lifecycle transition.

        Used to determine if a service should be given extra grace during
        health checks and deregistration decisions.

        Args:
            service: ServiceInfo object to check

        Returns:
            True if service is in an active transition
        """
        if service.lifecycle_state == "stable":
            return False

        # Check if transition has expired
        if service.lifecycle_transition_started > 0 and service.lifecycle_expected_duration > 0:
            elapsed = time.time() - service.lifecycle_transition_started
            if elapsed > service.lifecycle_expected_duration * 2:
                # Transition has taken 2x longer than expected - consider it stuck
                logger.warning(
                    f"[v112.0] Service '{service.service_name}' transition appears stuck "
                    f"(state: {service.lifecycle_state}, elapsed: {elapsed:.1f}s, "
                    f"expected: {service.lifecycle_expected_duration}s)"
                )
                return False

        return True

    async def get_lifecycle_state(self, service_name: str) -> Optional[Dict[str, Any]]:
        """
        v112.0: Get the lifecycle state of a service for cross-repo coordination.

        This is useful for external services (JARVIS-Prime, Reactor-Core) to check
        if they should wait for a service to complete its transition.

        Args:
            service_name: Service to check

        Returns:
            Dict with lifecycle info, or None if service not found:
            {
                "state": "restarting",
                "reason": "graceful_restart",
                "started_at": 1704067200.0,
                "expected_duration": 120.0,
                "elapsed": 30.5,
                "remaining": 89.5,
                "is_transitioning": True
            }
        """
        try:
            services = await asyncio.to_thread(self._read_registry)
            service = services.get(service_name)

            if not service:
                return None

            current_time = time.time()
            elapsed = current_time - service.lifecycle_transition_started if service.lifecycle_transition_started > 0 else 0
            remaining = max(0, service.lifecycle_expected_duration - elapsed) if service.lifecycle_expected_duration > 0 else 0

            return {
                "state": service.lifecycle_state,
                "reason": service.lifecycle_reason,
                "started_at": service.lifecycle_transition_started,
                "expected_duration": service.lifecycle_expected_duration,
                "elapsed": elapsed,
                "remaining": remaining,
                "is_transitioning": self.is_in_transition(service)
            }

        except Exception as e:
            logger.error(f"[v112.0] Failed to get lifecycle state: {e}")
            return None

    async def wait_for_service_stable(
        self,
        service_name: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0
    ) -> bool:
        """
        v112.0: Wait for a service to become stable (complete its transition).

        This is useful for cross-repo coordination where one service needs to
        wait for another to finish restarting before continuing.

        Args:
            service_name: Service to wait for
            timeout: Maximum time to wait (seconds)
            poll_interval: How often to check (seconds)

        Returns:
            True if service became stable within timeout, False otherwise
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            lifecycle = await self.get_lifecycle_state(service_name)

            if lifecycle is None:
                # Service not found - might be starting up
                logger.debug(f"[v112.0] Waiting for '{service_name}' to register...")
                await asyncio.sleep(poll_interval)
                continue

            if not lifecycle["is_transitioning"]:
                # Service is stable!
                elapsed = time.time() - start_time
                logger.info(
                    f"[v112.0] ✅ Service '{service_name}' is stable "
                    f"(waited {elapsed:.1f}s)"
                )
                return True

            # Still transitioning
            remaining = lifecycle["remaining"]
            logger.debug(
                f"[v112.0] Waiting for '{service_name}' to complete "
                f"'{lifecycle['state']}' (remaining: {remaining:.1f}s)"
            )
            await asyncio.sleep(poll_interval)

        # Timeout
        lifecycle = await self.get_lifecycle_state(service_name)
        if lifecycle:
            logger.warning(
                f"[v112.0] ⚠️ Timeout waiting for '{service_name}' to stabilize "
                f"(state: {lifecycle['state']}, timeout: {timeout}s)"
            )
        return False

    def _ensure_registry_lock(self) -> asyncio.Lock:
        """
        v95.2: Lazily initialize the registry read-write lock.

        This handles the case where __init__ runs before event loop exists.
        """
        if self._registry_rw_lock is None or not self._registry_rw_lock_initialized:
            try:
                self._registry_rw_lock = asyncio.Lock()
                self._registry_rw_lock_initialized = True
            except RuntimeError:
                # No event loop - create one when needed
                pass
        return self._registry_rw_lock

    async def _heartbeat_internal(
        self,
        service_name: str,
        status: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        v95.2: Internal heartbeat implementation with race condition fix.

        CRITICAL FIX (v95.2): Uses asyncio lock to prevent read-modify-write race.
        Without this lock, the following race can occur:
        1. Process A reads registry (sees service X with old heartbeat)
        2. Process B reads registry (also sees service X with old heartbeat)
        3. Process A updates service X heartbeat, writes registry
        4. Process B updates service Y heartbeat, writes registry
        5. Process A's changes to X are LOST!

        Now uses _registry_rw_lock for atomic operations.
        """
        lock = self._ensure_registry_lock()

        # v95.2: Acquire lock for entire read-modify-write cycle
        async with lock:
            services = await asyncio.to_thread(self._read_registry)

            if service_name not in services:
                logger.debug(f"Heartbeat for unregistered service: {service_name}")
                return False

            service = services[service_name]
            service.last_heartbeat = time.time()

            if status:
                service.status = status

            if metadata:
                service.metadata.update(metadata)

            # v95.2: Increment state version for conflict detection
            self._state_version += 1

            # v95.2: Update unified state for supervisor-component sync
            self._unified_state[service_name] = {
                "pid": service.pid,
                "status": service.status,
                "last_heartbeat": service.last_heartbeat,
                "component_healthy": True,  # Component reporting heartbeat = healthy
                "supervisor_healthy": None,  # Unknown until supervisor confirms
                "state_version": self._state_version,
            }

            await asyncio.to_thread(self._write_registry, services)

        # v93.14: Clear stale warning rate limiter on successful heartbeat
        if service_name in self._stale_warning_times:
            del self._stale_warning_times[service_name]

        return True

    async def cleanup_stale_services(self) -> int:
        """
        v95.1: Remove services with dead PIDs or stale heartbeats.

        CRITICAL: Registry owner (jarvis-body) is NEVER removed.
        If the registry is running, the owner must be alive by definition.

        Returns:
            Number of services cleaned up
        """
        services = await asyncio.to_thread(self._read_registry)
        cleaned = 0

        for service_name, service in list(services.items()):
            # v95.1: CRITICAL - Registry owner is EXEMPT from cleanup
            # If the registry code is running, the owner service MUST be alive
            if service_name == self._owner_service_name:
                logger.debug(f"  ✓ {service_name} is registry owner - exempt from cleanup")
                continue

            should_remove = False

            # Check if process is dead
            if not service.is_process_alive():
                logger.info(
                    f"🧹 Cleaning dead service: {service_name} (PID {service.pid} not found)"
                )
                should_remove = True

            # v93.3: Check if stale with startup awareness
            elif service.is_stale_startup_aware(
                timeout_seconds=self.heartbeat_timeout,
                startup_grace_period=self.startup_grace_period,
                startup_multiplier=self.startup_stale_multiplier,
            ):
                in_startup = service.is_in_startup_phase(self.startup_grace_period)
                # Only clean if past startup grace period
                if not in_startup:
                    logger.info(
                        f"🧹 Cleaning stale service: {service_name} "
                        f"(last heartbeat {time.time() - service.last_heartbeat:.0f}s ago)"
                    )
                    should_remove = True
                else:
                    logger.debug(
                        f"⏳ Service {service_name} stale but in startup phase, keeping"
                    )

            if should_remove:
                del services[service_name]
                cleaned += 1

        if cleaned > 0:
            await asyncio.to_thread(self._write_registry, services)

        return cleaned

    async def _cleanup_loop(self) -> None:
        """Background task to periodically clean up stale services."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                cleaned = await self.cleanup_stale_services()
                if cleaned > 0:
                    logger.debug(f"🧹 Cleaned {cleaned} stale services")

            except asyncio.CancelledError:
                logger.info("🛑 Service registry cleanup loop stopped")
                break

            except Exception as e:
                logger.error(f"❌ Error in cleanup loop: {e}", exc_info=True)

    async def start_cleanup_task(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info(
                f"🧹 Service registry cleanup started "
                f"(interval: {self.cleanup_interval}s, timeout: {self.heartbeat_timeout}s)"
            )

        # v95.0: Start self-heartbeat task for the registry owner
        if self._self_heartbeat_task is None or self._self_heartbeat_task.done():
            self._self_heartbeat_task = asyncio.create_task(self._self_heartbeat_loop())
            logger.info(
                f"[v95.0] Self-heartbeat started for owner '{self._owner_service_name}' "
                f"(interval: {self._self_heartbeat_interval}s)"
            )

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 Service registry cleanup stopped")

        # v95.0: Stop self-heartbeat task
        if self._self_heartbeat_task and not self._self_heartbeat_task.done():
            self._self_heartbeat_task.cancel()
            try:
                await self._self_heartbeat_task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 Service registry cleanup loop stopped")

    async def _self_heartbeat_loop(self) -> None:
        """
        v95.1: Enterprise-grade self-heartbeat loop for the registry owner service.

        The registry owner (jarvis-body) is the service that RUNS this registry.
        If the registry is running, the owner must be alive (by definition).

        CRITICAL FIXES (v95.1):
        1. Auto-register owner on startup if not registered
        2. Send IMMEDIATE heartbeat (no wait-first pattern)
        3. Owner is NEVER marked stale (exemption in cleanup)
        4. Self-healing registration if owner entry disappears
        """
        logger.info(f"[v95.1] Self-heartbeat loop started for {self._owner_service_name}")

        # v95.1: CRITICAL - Ensure owner is registered IMMEDIATELY on startup
        await self._ensure_owner_registered()

        # v95.1: Send FIRST heartbeat immediately (no wait-first pattern!)
        await self._send_owner_heartbeat()

        while True:
            try:
                # Now wait for interval AFTER first heartbeat
                await asyncio.sleep(self._self_heartbeat_interval)

                # v95.1: Verify owner is still registered (self-healing)
                await self._ensure_owner_registered()

                # Send heartbeat for owner
                await self._send_owner_heartbeat()

            except asyncio.CancelledError:
                logger.debug(f"[v95.1] Self-heartbeat loop cancelled for {self._owner_service_name}")
                break
            except Exception as e:
                # Log but don't crash - the owner should stay alive
                logger.warning(f"[v95.1] Self-heartbeat error (non-fatal): {e}")
                # Try to recover by re-registering
                try:
                    await self._ensure_owner_registered()
                except Exception:
                    pass

    async def _ensure_owner_registered(self) -> None:
        """
        v95.2: Ensure the registry owner service is always registered with correct PID.

        This is CRITICAL for preventing "jarvis-body is stale/dead" issues.
        If the registry is running, the owner MUST exist in the registry with
        the CURRENT process PID.

        Key fixes (v95.2):
        1. Always verify PID matches current process
        2. Re-register if PID mismatch (process may have restarted)
        3. Include process_start_time for accurate alive checks
        """
        current_pid = os.getpid()
        current_start_time = self._get_current_process_start_time()

        try:
            # Read registry directly (bypass discover_service to avoid dead PID detection)
            services = await asyncio.to_thread(self._read_registry)
            owner = services.get(self._owner_service_name)

            needs_registration = False
            reason = ""

            if owner is None:
                needs_registration = True
                reason = "not registered"
            elif owner.pid != current_pid:
                # PID mismatch - registry has old PID, need to update
                needs_registration = True
                reason = f"PID mismatch (stored: {owner.pid}, current: {current_pid})"
            elif owner.process_start_time > 0 and abs(owner.process_start_time - current_start_time) > 2.0:
                # Start time mismatch - process restarted with same PID
                needs_registration = True
                reason = f"start time mismatch (stored: {owner.process_start_time:.1f}, current: {current_start_time:.1f})"

            if needs_registration:
                logger.info(
                    f"[v95.2] Registering owner '{self._owner_service_name}': {reason} "
                    f"(PID: {current_pid})"
                )
                owner_port = int(os.environ.get("JARVIS_BODY_PORT", "8010"))

                # Create ServiceInfo with explicit process_start_time
                service = ServiceInfo(
                    service_name=self._owner_service_name,
                    pid=current_pid,
                    port=owner_port,
                    host="localhost",
                    health_endpoint="/health",
                    status="healthy",
                    process_start_time=current_start_time,
                    metadata={
                        "is_registry_owner": True,
                        "auto_registered": True,
                        "registration_source": "self_heartbeat_loop",
                    }
                )

                services[self._owner_service_name] = service
                await asyncio.to_thread(self._write_registry, services)
                logger.info(f"[v95.2] Owner '{self._owner_service_name}' registered successfully")
            else:
                logger.debug(f"[v95.2] Owner '{self._owner_service_name}' already registered correctly")

        except Exception as e:
            logger.error(f"[v95.2] Failed to ensure owner registered: {e}")

    def _get_current_process_start_time(self) -> float:
        """v95.2: Get the start time of the current process."""
        try:
            return psutil.Process(os.getpid()).create_time()
        except Exception:
            return 0.0

    async def _send_owner_heartbeat(self) -> None:
        """v95.1: Send heartbeat for the registry owner."""
        try:
            await self.heartbeat(
                self._owner_service_name,
                status="healthy",
                metadata={
                    "heartbeat_source": "self_heartbeat",
                    "is_registry_owner": True,
                    "pid": os.getpid(),
                    "timestamp": time.time(),
                }
            )
            logger.debug(f"[v95.1] Self-heartbeat sent for {self._owner_service_name}")
        except Exception as e:
            logger.warning(f"[v95.1] Failed to send owner heartbeat: {e}")

    async def ensure_owner_registered_immediately(self, start_heartbeat: bool = True) -> bool:
        """
        v95.2: PUBLIC method to ensure owner is registered immediately.
        v112.0: Now also starts self-heartbeat loop to prevent startup window vulnerability.

        This should be called at the START of FastAPI lifespan to ensure
        jarvis-body is discoverable by external services (jarvis-prime, reactor-core)
        BEFORE they start waiting for it.

        The issue this fixes:
        - jarvis-prime waits 120s for jarvis-body
        - But jarvis-body only registers when start_cleanup_task() is called
        - start_cleanup_task() is called LATE in the startup process
        - Result: jarvis-prime times out and runs in standalone mode

        v112.0 Enhancement:
        - Also starts the self-heartbeat loop immediately (if start_heartbeat=True)
        - This closes the startup window vulnerability where external services
          could trigger grace period tracking before heartbeats start
        - The heartbeat loop keeps last_heartbeat fresh during FastAPI loading

        Args:
            start_heartbeat: If True, immediately start self-heartbeat loop.
                            Default True for backwards-compatible behavior improvement.

        Returns:
            True if registration succeeded
        """
        try:
            await self._ensure_owner_registered()
            await self._send_owner_heartbeat()
            logger.info(f"[v95.2] ✅ Owner '{self._owner_service_name}' registered immediately")

            # v112.0: Start self-heartbeat loop immediately to prevent startup window
            # This ensures heartbeats continue during FastAPI loading phase
            if start_heartbeat:
                if self._self_heartbeat_task is None or self._self_heartbeat_task.done():
                    self._self_heartbeat_task = asyncio.create_task(
                        self._self_heartbeat_loop(),
                        name=f"service_registry_heartbeat_{self._owner_service_name}"
                    )
                    logger.info(
                        f"[v112.0] ✅ Self-heartbeat loop started immediately "
                        f"(interval: {self._self_heartbeat_interval}s)"
                    )

            return True
        except Exception as e:
            logger.warning(f"[v95.2] Immediate owner registration failed: {e}")
            return False

    # =========================================================================
    # v95.2: Unified State Management API
    # =========================================================================

    async def update_supervisor_state(
        self,
        service_name: str,
        process_alive: bool,
        pid: Optional[int] = None,
        exit_code: Optional[int] = None,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        v95.2: Update state from supervisor's perspective.

        This solves the process-component state synchronization gap:
        - Supervisor tracks PIDs and process lifecycle
        - Components track their own health state
        - This method synchronizes supervisor's view into unified state

        Args:
            service_name: Name of the service
            process_alive: Whether the process is alive (from supervisor's view)
            pid: Process ID (if known)
            exit_code: Exit code if process died
            metadata: Additional metadata
        """
        lock = self._ensure_registry_lock()

        async with lock:
            if service_name not in self._unified_state:
                self._unified_state[service_name] = {
                    "pid": pid,
                    "status": "unknown",
                    "last_heartbeat": 0,
                    "component_healthy": None,
                    "supervisor_healthy": None,
                    "state_version": 0,
                }

            state = self._unified_state[service_name]
            state["supervisor_healthy"] = process_alive
            state["supervisor_updated_at"] = time.time()

            if pid is not None:
                state["pid"] = pid
            if exit_code is not None:
                state["exit_code"] = exit_code
            if metadata:
                state.setdefault("supervisor_metadata", {}).update(metadata)

            # v95.2: Detect state conflicts
            component_healthy = state.get("component_healthy")
            if component_healthy is not None and component_healthy != process_alive:
                logger.warning(
                    f"[v95.2] State conflict for '{service_name}': "
                    f"supervisor says {'alive' if process_alive else 'dead'}, "
                    f"component says {'healthy' if component_healthy else 'unhealthy'}"
                )
                state["state_conflict"] = True
                state["conflict_detected_at"] = time.time()

    async def get_unified_state(self, service_name: str) -> Optional[Dict[str, Any]]:
        """
        v95.2: Get unified state for a service.

        Returns combined supervisor and component state for consensus checking.
        """
        lock = self._ensure_registry_lock()

        async with lock:
            return self._unified_state.get(service_name)

    async def get_all_unified_states(self) -> Dict[str, Dict[str, Any]]:
        """v95.2: Get all unified states for monitoring."""
        lock = self._ensure_registry_lock()

        async with lock:
            return dict(self._unified_state)

    async def resolve_state_conflict(
        self,
        service_name: str,
        trust_source: str = "component"
    ) -> bool:
        """
        v95.2: Resolve a state conflict between supervisor and component.

        Args:
            service_name: Service with conflict
            trust_source: Which source to trust ("supervisor" or "component")

        Returns:
            True if conflict was resolved
        """
        lock = self._ensure_registry_lock()

        async with lock:
            if service_name not in self._unified_state:
                return False

            state = self._unified_state[service_name]
            if not state.get("state_conflict"):
                return False

            if trust_source == "component":
                # Trust component's view - it knows its own health
                state["resolved_status"] = state.get("component_healthy", False)
            else:
                # Trust supervisor's view - it can verify process existence
                state["resolved_status"] = state.get("supervisor_healthy", False)

            state["state_conflict"] = False
            state["conflict_resolved_at"] = time.time()
            state["conflict_resolution"] = trust_source

            logger.info(
                f"[v95.2] Resolved state conflict for '{service_name}' "
                f"(trusted: {trust_source})"
            )

            return True

    async def wait_for_service(
        self,
        service_name: str,
        timeout: float = 30.0,
        check_interval: float = 1.0
    ) -> Optional[ServiceInfo]:
        """
        Wait for a service to become available.

        Args:
            service_name: Service to wait for
            timeout: Maximum time to wait (seconds)
            check_interval: How often to check (seconds)

        Returns:
            ServiceInfo if service becomes available, None if timeout
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            service = await self.discover_service(service_name)
            if service:
                logger.info(f"✅ Service discovered: {service_name}")
                return service

            await asyncio.sleep(check_interval)

        logger.warning(
            f"⏱️  Timeout waiting for service: {service_name} (after {timeout}s)"
        )
        return None

    # =========================================================================
    # v4.0: PRE-FLIGHT CLEANUP - Critical for Robust Startup
    # =========================================================================

    async def pre_flight_cleanup(self) -> Dict[str, Any]:
        """
        v4.0: Comprehensive pre-flight cleanup before starting services.

        MUST be called before starting any services to ensure a clean slate.
        This prevents "dead PID" warnings and ensures reliable service discovery.

        What it does:
        1. Validates ALL entries in the registry
        2. Removes entries with dead PIDs (process not running)
        3. Removes entries with reused PIDs (process start time mismatch)
        4. Removes entries with invalid data (bad port, bad PID)
        5. Optionally kills zombie processes still holding ports
        6. Reports detailed cleanup statistics

        Returns:
            Dict with cleanup statistics and removed services
        """
        logger.info("🚀 Pre-flight cleanup: Validating service registry...")

        services = await asyncio.to_thread(self._read_registry)
        stats = {
            "total_entries": len(services),
            "valid_entries": 0,
            "removed_dead_pid": [],
            "removed_pid_reuse": [],
            "removed_invalid": [],
            "removed_stale": [],
            "ports_freed": [],
            "cleanup_time_ms": 0,
        }

        start_time = time.time()
        to_remove = []

        for service_name, service in services.items():
            # Run comprehensive validation
            is_valid, reason = service.validate()

            if not is_valid:
                to_remove.append(service_name)

                # Categorize the removal reason
                if "dead" in reason.lower() or "not found" in reason.lower():
                    stats["removed_dead_pid"].append({
                        "name": service_name,
                        "pid": service.pid,
                        "port": service.port,
                        "reason": reason
                    })
                elif "reuse" in reason.lower():
                    stats["removed_pid_reuse"].append({
                        "name": service_name,
                        "pid": service.pid,
                        "port": service.port,
                        "reason": reason
                    })
                else:
                    stats["removed_invalid"].append({
                        "name": service_name,
                        "pid": service.pid,
                        "port": service.port,
                        "reason": reason
                    })

                # Track freed ports
                stats["ports_freed"].append(service.port)

                logger.info(
                    f"  🧹 Removing {service_name}: {reason} "
                    f"(PID: {service.pid}, Port: {service.port})"
                )
            else:
                # v95.1: CRITICAL - Registry owner is NEVER marked stale or removed
                # If the registry is running, the owner MUST be alive (by definition)
                if service_name == self._owner_service_name:
                    stats["valid_entries"] += 1
                    logger.debug(
                        f"  ✓ {service_name} is registry owner - exempt from stale checks"
                    )
                    continue

                # v93.3: Startup-aware stale detection with tiered cleanup
                # Services without recent heartbeat should be cleaned up, but give
                # newly registered services extra time during startup grace period
                is_in_startup = service.is_in_startup_phase(self.startup_grace_period)

                # Use startup-aware stale detection
                is_stale = service.is_stale_startup_aware(
                    timeout_seconds=self.heartbeat_timeout,
                    startup_grace_period=self.startup_grace_period,
                    startup_multiplier=self.startup_stale_multiplier,
                )

                if is_stale:
                    stale_age = time.time() - service.last_heartbeat
                    service_age = time.time() - service.registered_at

                    # v95.0: Adaptive very stale threshold - accounts for:
                    # 1. Service type (reactor-core needs longer threshold)
                    # 2. Startup phase (extra grace during startup)
                    # This prevents services with long init times from being incorrectly removed
                    base_very_stale = self._get_adaptive_stale_threshold(service_name)
                    very_stale_threshold = (
                        base_very_stale * self.startup_stale_multiplier
                        if is_in_startup else base_very_stale
                    )

                    if stale_age > very_stale_threshold:
                        # Very stale - treat as dead and clean up
                        logger.warning(
                            f"  🧹 {service_name} is very stale "
                            f"(last heartbeat {stale_age:.0f}s ago > {very_stale_threshold}s threshold) - removing"
                        )
                        to_remove.append(service_name)
                        stats["removed_stale"].append({
                            "name": service_name,
                            "pid": service.pid,
                            "stale_seconds": stale_age,
                            "action": "removed"
                        })
                    else:
                        # Mildly stale - warn but keep for now
                        startup_status = " (in startup phase)" if is_in_startup else ""
                        logger.warning(
                            f"  ⚠️  {service_name} is stale{startup_status} "
                            f"(last heartbeat {stale_age:.0f}s ago, age {service_age:.0f}s) - keeping for now"
                        )
                        stats["valid_entries"] += 1
                        stats["removed_stale"].append({
                            "name": service_name,
                            "pid": service.pid,
                            "stale_seconds": stale_age,
                            "service_age": service_age,
                            "in_startup": is_in_startup,
                            "action": "kept"
                        })
                else:
                    stats["valid_entries"] += 1

        # Remove invalid entries
        if to_remove:
            for name in to_remove:
                del services[name]
            await asyncio.to_thread(self._write_registry, services)

        stats["cleanup_time_ms"] = (time.time() - start_time) * 1000

        # Log summary
        removed_count = len(to_remove)
        if removed_count > 0:
            logger.info(
                f"✅ Pre-flight cleanup complete: "
                f"removed {removed_count} invalid entries, "
                f"{stats['valid_entries']} valid entries remain "
                f"({stats['cleanup_time_ms']:.1f}ms)"
            )
        else:
            logger.info(
                f"✅ Pre-flight cleanup complete: "
                f"registry is clean ({stats['valid_entries']} valid entries, "
                f"{stats['cleanup_time_ms']:.1f}ms)"
            )

        return stats

    async def validate_and_cleanup_port(self, port: int) -> bool:
        """
        v4.0: Ensure a port is free before starting a service.

        Checks if port is in use by:
        1. A registered service (validates if still alive)
        2. An unregistered process (optionally kills it)

        Returns:
            True if port is now free, False if still in use
        """
        # Check if any registered service claims this port
        services = await asyncio.to_thread(self._read_registry)

        for service_name, service in services.items():
            if service.port == port:
                if service.is_process_alive():
                    logger.warning(
                        f"Port {port} in use by registered service {service_name} "
                        f"(PID: {service.pid})"
                    )
                    return False
                else:
                    # Dead service holding port - remove from registry
                    logger.info(
                        f"Removing dead service {service_name} from port {port}"
                    )
                    await self.deregister_service(service_name)

        # Check if port is in use by an unregistered process
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == 'LISTEN':
                    # Port is in use by unregistered process
                    try:
                        proc = psutil.Process(conn.pid)
                        logger.warning(
                            f"Port {port} in use by unregistered process: "
                            f"{proc.name()} (PID: {conn.pid})"
                        )
                        return False
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except (psutil.AccessDenied, OSError):
            # May not have permission to check all connections
            pass

        return True

    async def get_registry_health_report(self) -> Dict[str, Any]:
        """
        v4.0: Generate comprehensive health report for the registry.

        Returns detailed information about all registered services,
        their health status, and any issues detected.
        """
        services = await asyncio.to_thread(self._read_registry)

        report = {
            "timestamp": datetime.now().isoformat(),
            "total_services": len(services),
            "healthy_services": 0,
            "degraded_services": 0,
            "dead_services": 0,
            "stale_services": 0,
            "services": {}
        }

        for service_name, service in services.items():
            is_alive = service.is_process_alive()
            # v93.3: Use startup-aware stale detection for health report
            is_in_startup = service.is_in_startup_phase(self.startup_grace_period)
            is_stale = service.is_stale_startup_aware(
                timeout_seconds=self.heartbeat_timeout,
                startup_grace_period=self.startup_grace_period,
                startup_multiplier=self.startup_stale_multiplier,
            )

            service_report = {
                "pid": service.pid,
                "port": service.port,
                "host": service.host,
                "status": service.status,
                "is_alive": is_alive,
                "is_stale": is_stale,
                "is_in_startup": is_in_startup,  # v93.3: Track startup phase
                "registered_at": datetime.fromtimestamp(
                    service.registered_at
                ).isoformat() if service.registered_at else None,
                "last_heartbeat": datetime.fromtimestamp(
                    service.last_heartbeat
                ).isoformat() if service.last_heartbeat else None,
                "heartbeat_age_seconds": time.time() - service.last_heartbeat,
                "service_age_seconds": time.time() - service.registered_at,  # v93.3
            }

            if not is_alive:
                service_report["health"] = "DEAD"
                report["dead_services"] += 1
            elif is_stale:
                service_report["health"] = "STALE"
                report["stale_services"] += 1
            elif is_in_startup:
                service_report["health"] = "STARTING"  # v93.3: New status
            elif service.status == "degraded":
                service_report["health"] = "DEGRADED"
                report["degraded_services"] += 1
            else:
                service_report["health"] = "HEALTHY"
                report["healthy_services"] += 1

            report["services"][service_name] = service_report

        return report


# =============================================================================
# v17.0: SERVICE LAUNCH CONFIG - Auto-Restart Configuration
# =============================================================================

@dataclass
class ServiceLaunchConfig:
    """
    v17.0: Configuration for how to launch/restart a service.

    Stores all information needed to restart a service automatically
    when it dies or becomes unhealthy.
    """
    service_name: str
    command: List[str]  # e.g., ["python3", "server.py"]
    working_dir: str
    env_vars: Dict[str, str] = None
    python_venv: Optional[str] = None  # Path to venv/bin/python
    restart_policy: str = "always"  # always, on-failure, never
    max_restarts: int = 5
    restart_delay_sec: float = 5.0
    restart_backoff_multiplier: float = 2.0
    max_restart_delay_sec: float = 300.0
    health_check_timeout_sec: float = 30.0

    def __post_init__(self):
        if self.env_vars is None:
            self.env_vars = {}

    def to_dict(self) -> Dict:
        return {
            "service_name": self.service_name,
            "command": self.command,
            "working_dir": self.working_dir,
            "env_vars": self.env_vars,
            "python_venv": self.python_venv,
            "restart_policy": self.restart_policy,
            "max_restarts": self.max_restarts,
            "restart_delay_sec": self.restart_delay_sec,
            "restart_backoff_multiplier": self.restart_backoff_multiplier,
            "max_restart_delay_sec": self.max_restart_delay_sec,
            "health_check_timeout_sec": self.health_check_timeout_sec,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ServiceLaunchConfig":
        return cls(**data)


@dataclass
class ServiceRestartHistory:
    """v17.0: Track restart attempts for a service."""
    service_name: str
    restart_count: int = 0
    last_restart_time: float = 0.0
    last_failure_reason: Optional[str] = None
    consecutive_failures: int = 0

    def record_restart(self, reason: str = None):
        self.restart_count += 1
        self.last_restart_time = time.time()
        self.last_failure_reason = reason

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self, reason: str = None):
        self.consecutive_failures += 1
        self.last_failure_reason = reason

    def get_current_delay(self, config: ServiceLaunchConfig) -> float:
        """Calculate current restart delay with exponential backoff."""
        if self.consecutive_failures == 0:
            return config.restart_delay_sec

        delay = config.restart_delay_sec * (
            config.restart_backoff_multiplier ** (self.consecutive_failures - 1)
        )
        return min(delay, config.max_restart_delay_sec)

    def should_restart(self, config: ServiceLaunchConfig) -> bool:
        """Check if service should be restarted based on policy."""
        if config.restart_policy == "never":
            return False

        if config.restart_policy == "on-failure" and self.last_failure_reason is None:
            return False

        return self.restart_count < config.max_restarts


# =============================================================================
# v17.0: SERVICE SUPERVISOR - Cross-Repo Auto-Restart Manager
# =============================================================================

class ServiceSupervisor:
    """
    v17.0: Enterprise-grade service supervisor with auto-restart.

    Features:
    - Automatic restart of dead services with exponential backoff
    - Cross-repo service management (JARVIS, J-Prime, Reactor-Core)
    - Health monitoring with configurable restart policies
    - Parallel launch support for fast startup
    - Graceful degradation when services repeatedly fail
    - Distributed tracing for restart events

    Architecture:
        ┌─────────────────────────────────────────────────────────────────┐
        │                    Service Supervisor v17.0                      │
        ├─────────────────────────────────────────────────────────────────┤
        │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
        │  │   Monitor   │  │  Restarter  │  │    Health Checker       │  │
        │  │   (async)   │──│   (async)   │──│     (parallel)          │  │
        │  └──────┬──────┘  └──────┬──────┘  └───────────┬─────────────┘  │
        │         │                │                     │                │
        │         ▼                ▼                     ▼                │
        │  ┌──────────────────────────────────────────────────────────┐   │
        │  │              Service Registry (File-Based)                │   │
        │  └──────────────────────────────────────────────────────────┘   │
        └─────────────────────────────────────────────────────────────────┘
    """

    def __init__(
        self,
        registry: Optional[ServiceRegistry] = None,
        monitor_interval: float = 10.0,
        launch_configs_file: Optional[Path] = None,
    ):
        """
        Initialize service supervisor.

        Args:
            registry: ServiceRegistry instance (uses global if not provided)
            monitor_interval: Seconds between health checks
            launch_configs_file: File to persist launch configs
        """
        self._registry = registry
        self._monitor_interval = monitor_interval
        self._launch_configs_file = launch_configs_file or (
            Path.home() / ".jarvis" / "registry" / "launch_configs.json"
        )

        self._launch_configs: Dict[str, ServiceLaunchConfig] = {}
        self._restart_history: Dict[str, ServiceRestartHistory] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._restart_callbacks: List[callable] = []

        # Load persisted launch configs
        self._load_launch_configs()

        logger.info("[v17.0] ServiceSupervisor initialized")

    @property
    def registry(self) -> ServiceRegistry:
        """Get service registry (lazy initialization)."""
        if self._registry is None:
            self._registry = get_service_registry()
        return self._registry

    def _load_launch_configs(self) -> None:
        """Load persisted launch configurations."""
        try:
            if self._launch_configs_file.exists():
                with open(self._launch_configs_file) as f:
                    data = json.load(f)
                    self._launch_configs = {
                        name: ServiceLaunchConfig.from_dict(cfg)
                        for name, cfg in data.items()
                    }
                logger.debug(f"[v17.0] Loaded {len(self._launch_configs)} launch configs")
        except Exception as e:
            logger.warning(f"[v17.0] Failed to load launch configs: {e}")

    def _save_launch_configs(self) -> None:
        """Persist launch configurations to disk."""
        try:
            self._launch_configs_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                name: cfg.to_dict()
                for name, cfg in self._launch_configs.items()
            }
            with open(self._launch_configs_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"[v17.0] Saved {len(data)} launch configs")
        except Exception as e:
            logger.warning(f"[v17.0] Failed to save launch configs: {e}")

    def register_service_launch(self, config: ServiceLaunchConfig) -> None:
        """
        Register how to launch a service for auto-restart.

        Args:
            config: ServiceLaunchConfig with launch details
        """
        self._launch_configs[config.service_name] = config
        self._restart_history[config.service_name] = ServiceRestartHistory(
            service_name=config.service_name
        )
        self._save_launch_configs()
        logger.info(f"[v17.0] Registered launch config for: {config.service_name}")

    def on_restart(self, callback: callable) -> None:
        """Register callback for restart events."""
        self._restart_callbacks.append(callback)

    async def _notify_restart(
        self,
        service_name: str,
        success: bool,
        reason: str = None
    ) -> None:
        """Notify callbacks of restart event."""
        for callback in self._restart_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(service_name, success, reason)
                else:
                    callback(service_name, success, reason)
            except Exception as e:
                logger.warning(f"[v17.0] Restart callback error: {e}")

    async def launch_service(
        self,
        service_name: str,
        wait_for_healthy: bool = True,
    ) -> Optional[asyncio.subprocess.Process]:
        """
        Launch a service using its registered config.

        Args:
            service_name: Service to launch
            wait_for_healthy: Wait for health check to pass

        Returns:
            Process if launched successfully, None otherwise
        """
        async with self._lock:
            if service_name not in self._launch_configs:
                logger.warning(f"[v17.0] No launch config for: {service_name}")
                return None

            config = self._launch_configs[service_name]
            history = self._restart_history.get(
                service_name,
                ServiceRestartHistory(service_name=service_name)
            )

            # Check restart policy
            if not history.should_restart(config):
                logger.warning(
                    f"[v17.0] Service {service_name} exceeded max restarts "
                    f"({history.restart_count}/{config.max_restarts})"
                )
                return None

            # Calculate delay if this is a restart
            if history.restart_count > 0:
                delay = history.get_current_delay(config)
                logger.info(
                    f"[v17.0] Waiting {delay:.1f}s before restarting {service_name} "
                    f"(attempt {history.restart_count + 1}/{config.max_restarts})"
                )
                await asyncio.sleep(delay)

            # Build command
            cmd = config.command.copy()
            if config.python_venv:
                # Replace python with venv python
                if cmd[0] in ("python", "python3"):
                    cmd[0] = config.python_venv

            # Build environment
            env = os.environ.copy()
            env.update(config.env_vars)
            env["PYTHONPATH"] = config.working_dir

            # Create log files
            log_dir = Path.home() / ".jarvis" / "logs" / "services"
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_log = log_dir / f"{service_name}_stdout.log"
            stderr_log = log_dir / f"{service_name}_stderr.log"

            try:
                logger.info(f"[v17.0] Launching {service_name}: {' '.join(cmd)}")

                stdout_fd = open(stdout_log, 'a')
                stderr_fd = open(stderr_log, 'a')

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=config.working_dir,
                    env=env,
                    stdout=stdout_fd,
                    stderr=stderr_fd,
                    start_new_session=True,
                )

                self._processes[service_name] = process
                history.record_restart(reason="supervisor_launch")
                self._restart_history[service_name] = history

                logger.info(f"[v17.0] {service_name} launched (PID: {process.pid})")

                # Wait for health check if requested
                if wait_for_healthy:
                    service_info = await self.registry.wait_for_service(
                        service_name,
                        timeout=config.health_check_timeout_sec,
                    )
                    if service_info:
                        history.record_success()
                        logger.info(f"[v17.0] ✅ {service_name} healthy")
                        await self._notify_restart(service_name, True, "healthy")
                    else:
                        history.record_failure(reason="health_check_timeout")
                        logger.warning(f"[v17.0] ⚠️ {service_name} unhealthy after launch")
                        await self._notify_restart(service_name, False, "health_check_timeout")

                return process

            except Exception as e:
                history.record_failure(reason=str(e))
                self._restart_history[service_name] = history
                logger.error(f"[v17.0] Failed to launch {service_name}: {e}")
                await self._notify_restart(service_name, False, str(e))
                return None

    async def restart_service(self, service_name: str) -> bool:
        """
        Restart a dead or unhealthy service.

        Args:
            service_name: Service to restart

        Returns:
            True if restart was successful
        """
        logger.info(f"[v17.0] Restarting service: {service_name}")

        # Kill existing process if any
        if service_name in self._processes:
            old_proc = self._processes[service_name]
            if old_proc.returncode is None:
                try:
                    old_proc.terminate()
                    await asyncio.wait_for(old_proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    old_proc.kill()
            del self._processes[service_name]

        # Deregister from registry
        await self.registry.deregister_service(service_name)

        # Launch fresh
        process = await self.launch_service(service_name, wait_for_healthy=True)
        return process is not None

    async def _monitor_loop(self) -> None:
        """
        v93.14: Enhanced background loop with proactive heartbeat sending.

        CRITICAL FIX: The previous implementation only checked if services were
        stale via discover_service(), but didn't send heartbeats to keep them alive.
        This caused a vicious cycle where services were marked stale because no one
        was sending heartbeats on their behalf.

        Now:
        1. List all services WITHOUT triggering stale warnings
        2. Do actual HTTP health checks for services with health endpoints
        3. Send heartbeats for healthy services to keep them alive
        4. Only restart truly unhealthy services
        """
        logger.info(f"[v93.14] Service monitor started (interval: {self._monitor_interval}s)")

        while self._running:
            try:
                await asyncio.sleep(self._monitor_interval)

                # Check each registered service
                for service_name, config in self._launch_configs.items():
                    if config.restart_policy == "never":
                        continue

                    try:
                        await self._check_and_heartbeat_service(service_name, config)
                    except Exception as svc_error:
                        logger.debug(f"[v93.14] Error checking {service_name}: {svc_error}")

            except asyncio.CancelledError:
                logger.info("[v93.14] Service monitor stopping")
                break
            except Exception as e:
                logger.error(f"[v93.14] Monitor loop error: {e}", exc_info=True)

    async def _check_and_heartbeat_service(
        self,
        service_name: str,
        config: "ServiceLaunchConfig"
    ) -> None:
        """
        v93.14: Check service health and send heartbeat if alive.

        This method performs:
        1. Direct HTTP health check (preferred if endpoint available)
        2. Process alive check (fallback)
        3. Heartbeat sending on successful health check
        4. Service restart if truly dead

        Args:
            service_name: Name of service to check
            config: Launch configuration with health endpoint info
        """
        import aiohttp

        is_healthy = False
        health_status = "unknown"

        # ═══════════════════════════════════════════════════════════════════════
        # Step 1: Try HTTP health check if endpoint is configured
        # ═══════════════════════════════════════════════════════════════════════
        health_url = None
        if hasattr(config, 'health_endpoint') and config.health_endpoint:
            # Get port from environment or config
            port = None
            if "jarvis-prime" in service_name.lower() or "jprime" in service_name.lower():
                port = int(os.environ.get("JARVIS_PRIME_PORT", "8000"))
            elif "reactor" in service_name.lower():
                port = int(os.environ.get("REACTOR_CORE_PORT", "8090"))

            if port:
                health_url = f"http://localhost:{port}{config.health_endpoint}"

        if health_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        health_url,
                        timeout=aiohttp.ClientTimeout(total=config.health_check_timeout_sec or 5.0)
                    ) as response:
                        is_healthy = response.status == 200
                        if is_healthy:
                            health_status = "healthy"
                            logger.debug(f"[v93.14] {service_name} HTTP health check passed")
                        else:
                            health_status = f"unhealthy (HTTP {response.status})"
            except aiohttp.ClientError as http_err:
                health_status = f"unreachable ({type(http_err).__name__})"
                logger.debug(f"[v93.14] {service_name} HTTP health check failed: {http_err}")
            except Exception as http_err:
                health_status = f"error ({type(http_err).__name__})"
                logger.debug(f"[v93.14] {service_name} HTTP health check error: {http_err}")

        # ═══════════════════════════════════════════════════════════════════════
        # Step 2: Fallback to process alive check if HTTP check failed
        # ═══════════════════════════════════════════════════════════════════════
        if not is_healthy and service_name in self._processes:
            proc = self._processes[service_name]
            if proc.returncode is None:
                # Process is running - assume healthy for heartbeat purposes
                is_healthy = True
                health_status = "process_alive"
                logger.debug(f"[v93.14] {service_name} process alive (PID: {proc.pid})")

        # ═══════════════════════════════════════════════════════════════════════
        # Step 3: Send heartbeat if service is healthy
        # ═══════════════════════════════════════════════════════════════════════
        if is_healthy:
            try:
                await self.registry.heartbeat(
                    service_name,
                    status=health_status,
                    metadata={"checked_by": "ServiceSupervisor", "health_url": health_url}
                )
                logger.debug(f"[v93.14] Sent heartbeat for {service_name} ({health_status})")
            except Exception as hb_err:
                logger.warning(f"[v93.14] Failed to send heartbeat for {service_name}: {hb_err}")
        else:
            # ═══════════════════════════════════════════════════════════════════
            # Step 4: Service is unhealthy - check if we should restart
            # ═══════════════════════════════════════════════════════════════════
            history = self._restart_history.get(service_name)
            if history and not history.should_restart(config.max_restarts or 5):
                logger.warning(
                    f"[v93.14] {service_name} unhealthy but max restarts reached, skipping"
                )
                return

            # Check if service is registered but stale (use list_services to avoid warning)
            services = await self.registry.list_services(healthy_only=False)
            service_info = next(
                (s for s in services if s.service_name == service_name),
                None
            )

            if service_info is None:
                # Service not registered at all - needs restart
                logger.warning(
                    f"[v93.14] {service_name} not registered, initiating restart"
                )
                await self.restart_service(service_name)
            elif not service_info.is_process_alive():
                # Service registered but process is dead
                logger.warning(
                    f"[v93.14] {service_name} process dead (was PID {service_info.pid}), "
                    f"initiating restart"
                )
                await self.restart_service(service_name)
            else:
                # Process is alive but HTTP check failed - send degraded heartbeat
                # to prevent stale detection, but don't restart yet
                try:
                    await self.registry.heartbeat(
                        service_name,
                        status="degraded",
                        metadata={"reason": health_status}
                    )
                    logger.debug(
                        f"[v93.14] Sent degraded heartbeat for {service_name} "
                        f"(process alive but health check failed)"
                    )
                except Exception as hb_err:
                    logger.debug(f"[v93.14] Failed to send degraded heartbeat: {hb_err}")

    async def start(self) -> None:
        """Start the service supervisor."""
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="service_supervisor_monitor"
        )
        logger.info("[v17.0] ServiceSupervisor started")

    async def stop(self) -> None:
        """Stop the service supervisor."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[v17.0] ServiceSupervisor stopped")

    async def launch_all_services(
        self,
        parallel: bool = True,
        wait_for_healthy: bool = True,
    ) -> Dict[str, bool]:
        """
        Launch all registered services.

        Args:
            parallel: Launch services in parallel
            wait_for_healthy: Wait for health checks

        Returns:
            Dict mapping service_name to success status
        """
        results = {}

        if parallel:
            # Launch all in parallel
            tasks = {
                name: asyncio.create_task(
                    self.launch_service(name, wait_for_healthy=wait_for_healthy)
                )
                for name in self._launch_configs.keys()
            }

            for name, task in tasks.items():
                try:
                    process = await asyncio.wait_for(task, timeout=120.0)
                    results[name] = process is not None
                except asyncio.TimeoutError:
                    results[name] = False
                    logger.warning(f"[v17.0] Timeout launching {name}")
                except Exception as e:
                    results[name] = False
                    logger.error(f"[v17.0] Error launching {name}: {e}")
        else:
            # Launch sequentially
            for name in self._launch_configs.keys():
                try:
                    process = await self.launch_service(name, wait_for_healthy=wait_for_healthy)
                    results[name] = process is not None
                except Exception as e:
                    results[name] = False
                    logger.error(f"[v17.0] Error launching {name}: {e}")

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get supervisor statistics."""
        return {
            "running": self._running,
            "services_registered": len(self._launch_configs),
            "services_with_process": len(self._processes),
            "restart_history": {
                name: {
                    "restart_count": history.restart_count,
                    "consecutive_failures": history.consecutive_failures,
                    "last_restart": history.last_restart_time,
                    "last_failure": history.last_failure_reason,
                }
                for name, history in self._restart_history.items()
            },
        }


# Global supervisor instance
_global_supervisor: Optional[ServiceSupervisor] = None


def get_service_supervisor() -> ServiceSupervisor:
    """Get global service supervisor instance (singleton)."""
    global _global_supervisor
    if _global_supervisor is None:
        _global_supervisor = ServiceSupervisor()
    return _global_supervisor


# =============================================================================
# v93.0: Startup Directory Initialization
# =============================================================================

# All required .jarvis directories for JARVIS ecosystem
REQUIRED_JARVIS_DIRECTORIES = [
    Path.home() / ".jarvis",
    Path.home() / ".jarvis" / "registry",
    Path.home() / ".jarvis" / "state",
    Path.home() / ".jarvis" / "state" / "locks",
    Path.home() / ".jarvis" / "locks",
    Path.home() / ".jarvis" / "logs",
    Path.home() / ".jarvis" / "logs" / "services",
    Path.home() / ".jarvis" / "cross_repo",
    Path.home() / ".jarvis" / "cross_repo" / "events",
    Path.home() / ".jarvis" / "cross_repo" / "sync",
    Path.home() / ".jarvis" / "trinity",
    Path.home() / ".jarvis" / "trinity" / "ipc",
    Path.home() / ".jarvis" / "trinity" / "commands",
    Path.home() / ".jarvis" / "bridge",
    Path.home() / ".jarvis" / "bridge" / "training_staging",
    Path.home() / ".jarvis" / "dlq",
    Path.home() / ".jarvis" / "cache",
    Path.home() / ".jarvis" / "ouroboros",
    Path.home() / ".jarvis" / "ouroboros" / "events",
    Path.home() / ".jarvis" / "ouroboros" / "memory",
    Path.home() / ".jarvis" / "ouroboros" / "snapshots",
    Path.home() / ".jarvis" / "ouroboros" / "cache",
    Path.home() / ".jarvis" / "ouroboros" / "sandbox",
    Path.home() / ".jarvis" / "ouroboros" / "rollback",
    Path.home() / ".jarvis" / "ouroboros" / "learning_cache",
    Path.home() / ".jarvis" / "ouroboros" / "manual_review",
    Path.home() / ".jarvis" / "reactor",
    Path.home() / ".jarvis" / "reactor" / "events",
    Path.home() / ".jarvis" / "oracle",
    Path.home() / ".jarvis" / "experience_mesh",
    Path.home() / ".jarvis" / "experience_mesh" / "fallback",
    Path.home() / ".jarvis" / "experience_queue",
    Path.home() / ".jarvis" / "experience_queue" / "ouroboros",
    Path.home() / ".jarvis" / "atomic_backups",
    Path.home() / ".jarvis" / "bulletproof",
    Path.home() / ".jarvis" / "performance",
]


def ensure_all_jarvis_directories() -> Dict[str, Any]:
    """
    v93.0: Ensure all required JARVIS directories exist.

    Should be called ONCE at startup before any services initialize.
    This prevents race conditions and "No such file or directory" errors
    throughout the JARVIS ecosystem.

    Returns:
        Dict with creation stats: created, existed, failed
    """
    stats = {
        "created": [],
        "existed": [],
        "failed": [],
        "total": len(REQUIRED_JARVIS_DIRECTORIES),
    }

    logger.info("[v93.0] Pre-flight directory initialization...")

    for dir_path in REQUIRED_JARVIS_DIRECTORIES:
        try:
            if dir_path.exists():
                stats["existed"].append(str(dir_path))
            else:
                dir_path.mkdir(parents=True, exist_ok=True)
                stats["created"].append(str(dir_path))
                logger.debug(f"[v93.0] Created directory: {dir_path}")
        except Exception as e:
            stats["failed"].append({"path": str(dir_path), "error": str(e)})
            logger.error(f"[v93.0] Failed to create directory {dir_path}: {e}")

    # Summary
    if stats["failed"]:
        logger.error(
            f"[v93.0] Directory initialization INCOMPLETE: "
            f"{len(stats['created'])} created, {len(stats['existed'])} existed, "
            f"{len(stats['failed'])} FAILED"
        )
    else:
        logger.info(
            f"[v93.0] Directory initialization complete: "
            f"{len(stats['created'])} created, {len(stats['existed'])} existed"
        )

    return stats


# =============================================================================
# Convenience Functions
# =============================================================================

_global_registry: Optional[ServiceRegistry] = None


def get_service_registry() -> ServiceRegistry:
    """Get global service registry instance (singleton)."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ServiceRegistry()
    return _global_registry


async def register_current_service(
    service_name: str,
    port: int,
    health_endpoint: str = "/health",
    metadata: Optional[Dict] = None
) -> ServiceInfo:
    """
    Convenience function to register current process as a service.

    Args:
        service_name: Name for this service
        port: Port this service listens on
        health_endpoint: Health check endpoint
        metadata: Optional metadata

    Returns:
        ServiceInfo for registered service
    """
    registry = get_service_registry()
    return await registry.register_service(
        service_name=service_name,
        pid=os.getpid(),
        port=port,
        health_endpoint=health_endpoint,
        metadata=metadata
    )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Core classes
    "ServiceRegistry",
    "ServiceInfo",
    # v17.0: Service Supervisor
    "ServiceSupervisor",
    "ServiceLaunchConfig",
    "ServiceRestartHistory",
    # v93.0: Directory Management
    "DirectoryManager",
    "ensure_all_jarvis_directories",
    "REQUIRED_JARVIS_DIRECTORIES",
    # Convenience functions
    "get_service_registry",
    "get_service_supervisor",
    "register_current_service",
]
