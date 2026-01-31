"""
Coordinated Shutdown Manager - Phased Shutdown Orchestration.
==============================================================

Provides a systematic, phased shutdown process for the Trinity architecture
that ensures graceful termination with state preservation.

Key Features:
1. Phased shutdown (Announce → Drain → Save → Cleanup → Terminate → Verify)
2. Process Group (PGID) tracking for clean termination
3. Priority-based hook execution
4. Timeout handling with escalation
5. State persistence before shutdown
6. Cross-component coordination via IPC

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  CoordinatedShutdownManager                                         │
    │  ├── ShutdownPhaseExecutor (runs hooks per phase)                   │
    │  ├── ProcessGroupManager (PGID tracking and termination)            │
    │  ├── StatePreserver (persists critical state before shutdown)       │
    │  └── ShutdownCoordinator (cross-component shutdown signaling)       │
    └─────────────────────────────────────────────────────────────────────┘

Shutdown Phases:
    ANNOUNCE   → Notify all components shutdown is starting
    DRAIN      → Complete in-flight requests (grace period)
    SAVE       → Persist critical state to disk
    CLEANUP    → Close connections, release resources
    TERMINATE  → Send signals to processes (SIGTERM → SIGKILL)
    VERIFY     → Confirm all processes terminated

Author: JARVIS Trinity v81.0 - Coordinated Shutdown Orchestration
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import psutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

# =============================================================================
# v151.0: SHUTDOWN DIAGNOSTICS - Deep Forensic Logging
# =============================================================================
# Import diagnostic tools for tracing exactly what triggers shutdown
try:
    from backend.core.shutdown_diagnostics import (
        log_shutdown_trigger,
        log_state_change,
        log_signal_received,
        capture_system_state,
    )
    _DIAGNOSTICS_AVAILABLE = True
except ImportError:
    _DIAGNOSTICS_AVAILABLE = False
    logger.debug("[v151.0] Shutdown diagnostics not available - using stubs")


def _log_shutdown_trigger_safe(source: str, message: str, extra_data: Optional[Dict[str, Any]] = None) -> None:
    """Safe wrapper for shutdown trigger logging."""
    if _DIAGNOSTICS_AVAILABLE:
        log_shutdown_trigger(source, message, extra_data)
    else:
        logger.warning(f"[DIAG] Shutdown trigger: [{source}] {message}")


def _log_state_change_safe(component: str, old_state: str, new_state: str, reason: str = "") -> None:
    """Safe wrapper for state change logging."""
    if _DIAGNOSTICS_AVAILABLE:
        log_state_change(component, old_state, new_state, reason)


def _log_signal_received_safe(signum: int, handler_name: str) -> None:
    """Safe wrapper for signal logging."""
    if _DIAGNOSTICS_AVAILABLE:
        log_signal_received(signum, handler_name)


# =============================================================================
# Configuration
# =============================================================================

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# =============================================================================
# Types and Enums
# =============================================================================

class ShutdownPhase(enum.IntEnum):
    """Shutdown phases in execution order."""
    ANNOUNCE = 1    # Notify all components
    DRAIN = 2       # Complete in-flight requests
    SAVE = 3        # Persist critical state
    CLEANUP = 4     # Close connections
    TERMINATE = 5   # Send signals to processes
    VERIFY = 6      # Confirm termination


class ShutdownReason(enum.Enum):
    """Reason for shutdown."""
    USER_REQUEST = "user_request"
    SIGNAL_RECEIVED = "signal_received"
    HEALTH_CRITICAL = "health_critical"
    UPDATE_REQUIRED = "update_required"
    ERROR_RECOVERY = "error_recovery"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    SCHEDULED = "scheduled"


class ProcessState(enum.Enum):
    """State of a managed process."""
    RUNNING = "running"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ZOMBIE = "zombie"
    UNKNOWN = "unknown"


@dataclass
class ShutdownHook:
    """A hook to be executed during shutdown."""
    name: str
    phase: ShutdownPhase
    priority: int  # Lower = earlier execution
    callback: Callable[[], Awaitable[None]]
    timeout: float = 10.0
    critical: bool = False  # If True, failure aborts shutdown

    def __lt__(self, other: "ShutdownHook") -> bool:
        return self.priority < other.priority


@dataclass
class ManagedProcess:
    """A process managed by the shutdown manager."""
    name: str
    pid: int
    pgid: Optional[int] = None
    state: ProcessState = ProcessState.RUNNING
    component_type: Optional[str] = None  # jarvis_body, jarvis_prime, reactor_core
    started_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        """How long this process has been running."""
        return time.time() - self.started_at


@dataclass
class ShutdownResult:
    """Result of a shutdown operation."""
    success: bool
    phase_reached: ShutdownPhase
    elapsed_seconds: float
    processes_terminated: int
    hooks_executed: int
    hooks_failed: int
    errors: List[str] = field(default_factory=list)
    state_saved: bool = False


@dataclass
class PhaseResult:
    """Result of a single shutdown phase."""
    phase: ShutdownPhase
    success: bool
    elapsed_seconds: float
    hooks_executed: int
    hooks_failed: int
    errors: List[str] = field(default_factory=list)


# =============================================================================
# Process Group Manager
# =============================================================================

class ProcessGroupManager:
    """
    Manages process groups (PGIDs) for clean termination.

    Uses PGID to ensure all child processes are terminated together.
    """

    def __init__(self):
        self._processes: Dict[int, ManagedProcess] = {}
        self._lock = asyncio.Lock()

    async def register_process(
        self,
        name: str,
        pid: int,
        component_type: Optional[str] = None,
        track_pgid: bool = True,
    ) -> ManagedProcess:
        """Register a process for shutdown management."""
        async with self._lock:
            pgid = None
            if track_pgid:
                try:
                    pgid = os.getpgid(pid)
                except (ProcessLookupError, PermissionError):
                    logger.warning(
                        f"[ShutdownManager] Could not get PGID for {name} (PID {pid})"
                    )

            process = ManagedProcess(
                name=name,
                pid=pid,
                pgid=pgid,
                component_type=component_type,
            )
            self._processes[pid] = process

            logger.debug(
                f"[ShutdownManager] Registered process: {name} "
                f"(PID={pid}, PGID={pgid})"
            )
            return process

    async def unregister_process(self, pid: int) -> None:
        """Unregister a process."""
        async with self._lock:
            if pid in self._processes:
                process = self._processes.pop(pid)
                logger.debug(f"[ShutdownManager] Unregistered: {process.name}")

    def get_process(self, pid: int) -> Optional[ManagedProcess]:
        """Get a managed process by PID."""
        return self._processes.get(pid)

    def get_all_processes(self) -> List[ManagedProcess]:
        """Get all managed processes."""
        return list(self._processes.values())

    def get_by_component(self, component_type: str) -> List[ManagedProcess]:
        """Get processes by component type."""
        return [
            p for p in self._processes.values()
            if p.component_type == component_type
        ]

    async def terminate_process(
        self,
        pid: int,
        use_pgid: bool = True,
        graceful_timeout: float = 5.0,
        force_timeout: float = 3.0,
    ) -> bool:
        """
        Terminate a process with graceful escalation.

        1. Send SIGTERM to process (or PGID)
        2. Wait for graceful_timeout
        3. Send SIGKILL if still running
        4. Wait for force_timeout
        5. Return success status
        """
        process = self._processes.get(pid)
        if not process:
            return True  # Already gone

        target_id = pid
        use_pg = use_pgid and process.pgid is not None

        if use_pg:
            target_id = process.pgid
            logger.debug(
                f"[ShutdownManager] Terminating {process.name} via PGID {target_id}"
            )
        else:
            logger.debug(
                f"[ShutdownManager] Terminating {process.name} via PID {pid}"
            )

        # Mark as stopping
        process.state = ProcessState.STOPPING

        # Try graceful termination
        try:
            if use_pg:
                os.killpg(target_id, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            process.state = ProcessState.STOPPED
            return True
        except PermissionError:
            logger.warning(
                f"[ShutdownManager] Permission denied for SIGTERM to {process.name}"
            )

        # Wait for graceful termination
        start_time = time.time()
        while time.time() - start_time < graceful_timeout:
            await asyncio.sleep(0.2)
            if not self._process_exists(pid):
                process.state = ProcessState.STOPPED
                logger.info(f"[ShutdownManager] {process.name} terminated gracefully")
                return True

        # Force termination
        logger.warning(
            f"[ShutdownManager] {process.name} didn't respond to SIGTERM, sending SIGKILL"
        )

        try:
            if use_pg:
                os.killpg(target_id, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            process.state = ProcessState.STOPPED
            return True
        except PermissionError:
            logger.error(
                f"[ShutdownManager] Permission denied for SIGKILL to {process.name}"
            )
            return False

        # Wait for forced termination
        start_time = time.time()
        while time.time() - start_time < force_timeout:
            await asyncio.sleep(0.2)
            if not self._process_exists(pid):
                process.state = ProcessState.STOPPED
                logger.info(f"[ShutdownManager] {process.name} killed")
                return True

        # Still running - zombie or unkillable
        process.state = ProcessState.ZOMBIE
        logger.error(f"[ShutdownManager] Failed to kill {process.name}")
        return False

    def _process_exists(self, pid: int) -> bool:
        """Check if a process exists."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Exists but can't signal

    async def terminate_all(
        self,
        order: Optional[List[str]] = None,
        parallel: bool = False,
        **kwargs,
    ) -> Tuple[int, int]:
        """
        Terminate all managed processes.

        Args:
            order: Component types in termination order (e.g., ["reactor_core", "jarvis_prime", "jarvis_body"])
            parallel: If True, terminate processes in parallel
            **kwargs: Passed to terminate_process

        Returns:
            Tuple of (terminated_count, failed_count)
        """
        terminated = 0
        failed = 0

        if order:
            # Terminate in specified order
            for component_type in order:
                processes = self.get_by_component(component_type)
                for process in processes:
                    if await self.terminate_process(process.pid, **kwargs):
                        terminated += 1
                    else:
                        failed += 1
        elif parallel:
            # Terminate all in parallel
            tasks = [
                self.terminate_process(p.pid, **kwargs)
                for p in self._processes.values()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if result is True:
                    terminated += 1
                else:
                    failed += 1
        else:
            # Terminate sequentially
            for process in list(self._processes.values()):
                if await self.terminate_process(process.pid, **kwargs):
                    terminated += 1
                else:
                    failed += 1

        return (terminated, failed)


# =============================================================================
# Coordinated Shutdown Manager
# =============================================================================

class CoordinatedShutdownManager:
    """
    Orchestrates phased shutdown for the Trinity architecture.

    Features:
    - Phase-based execution with configurable hooks
    - Process group management for clean termination
    - State preservation before shutdown
    - Cross-component coordination via IPC
    - Timeout handling with escalation
    """

    # Default phase timeouts (can be overridden via environment)
    DEFAULT_PHASE_TIMEOUTS = {
        ShutdownPhase.ANNOUNCE: 5.0,
        ShutdownPhase.DRAIN: 30.0,
        ShutdownPhase.SAVE: 15.0,
        ShutdownPhase.CLEANUP: 10.0,
        ShutdownPhase.TERMINATE: 20.0,
        ShutdownPhase.VERIFY: 5.0,
    }

    # Termination order for Trinity components
    TERMINATION_ORDER = ["reactor_core", "jarvis_prime", "jarvis_body"]

    def __init__(
        self,
        ipc_bus: Optional[Any] = None,  # TrinityIPCBus
        state_dir: Optional[Path] = None,
    ):
        """
        Initialize the shutdown manager.

        Args:
            ipc_bus: Optional IPC bus for cross-component signaling
            state_dir: Directory for state preservation
        """
        self.ipc_bus = ipc_bus
        self.state_dir = state_dir or Path(os.environ.get(
            "JARVIS_STATE_DIR",
            str(Path.home() / ".jarvis" / "state")
        ))
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Process management
        self.process_manager = ProcessGroupManager()

        # Hooks organized by phase
        self._hooks: Dict[ShutdownPhase, List[ShutdownHook]] = {
            phase: [] for phase in ShutdownPhase
        }

        # Phase timeouts from environment
        self._phase_timeouts = {
            phase: _env_float(
                f"SHUTDOWN_PHASE_{phase.name}_TIMEOUT",
                self.DEFAULT_PHASE_TIMEOUTS[phase]
            )
            for phase in ShutdownPhase
        }

        # State
        self._shutdown_in_progress = False
        self._shutdown_lock = asyncio.Lock()
        self._current_phase: Optional[ShutdownPhase] = None
        self._shutdown_reason: Optional[ShutdownReason] = None

        # Callbacks
        self._on_shutdown_start: List[Callable[[ShutdownReason], None]] = []
        self._on_phase_complete: List[Callable[[ShutdownPhase, PhaseResult], None]] = []
        self._on_shutdown_complete: List[Callable[[ShutdownResult], None]] = []

        # v93.0: Register default IPC cleanup hook to prevent semaphore leaks
        self._register_default_cleanup_hooks()

        logger.info(
            f"[ShutdownManager] Initialized with state_dir={self.state_dir}"
        )

    def _register_default_cleanup_hooks(self) -> None:
        """
        v93.0: Register default cleanup hooks.

        These hooks ensure proper cleanup of system resources like IPC semaphores
        and shared memory that can leak if not explicitly cleaned up.
        """
        # IPC Semaphore/Shared Memory Cleanup Hook
        async def ipc_cleanup_hook() -> None:
            """Clean up IPC resources (semaphores, shared memory) to prevent leaks."""
            import subprocess

            cleaned_semaphores = 0
            cleaned_shm = 0
            current_user = os.getenv("USER", "")

            if not current_user:
                logger.debug("[ShutdownManager] Cannot determine USER for IPC cleanup")
                return

            try:
                # Clean up semaphores
                result = subprocess.run(
                    ["ipcs", "-s"],
                    capture_output=True,
                    text=True,
                    timeout=5.0
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if current_user in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                sem_id = parts[1]
                                try:
                                    subprocess.run(
                                        ["ipcrm", "-s", sem_id],
                                        timeout=2.0,
                                        capture_output=True
                                    )
                                    cleaned_semaphores += 1
                                except Exception:
                                    pass

                # Clean up shared memory
                result = subprocess.run(
                    ["ipcs", "-m"],
                    capture_output=True,
                    text=True,
                    timeout=5.0
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if current_user in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                shm_id = parts[1]
                                try:
                                    subprocess.run(
                                        ["ipcrm", "-m", shm_id],
                                        timeout=2.0,
                                        capture_output=True
                                    )
                                    cleaned_shm += 1
                                except Exception:
                                    pass

                if cleaned_semaphores > 0 or cleaned_shm > 0:
                    logger.info(
                        f"[ShutdownManager] IPC cleanup: {cleaned_semaphores} semaphores, "
                        f"{cleaned_shm} shared memory segments"
                    )

            except Exception as e:
                logger.debug(f"[ShutdownManager] IPC cleanup error: {e}")

        # Register the IPC cleanup hook with high priority (runs early in CLEANUP phase)
        self.register_hook(
            name="ipc_cleanup",
            phase=ShutdownPhase.CLEANUP,
            callback=ipc_cleanup_hook,
            priority=10,  # Low number = high priority = runs early
            timeout=10.0,
            critical=False,  # Don't fail shutdown if this fails
        )

    # =========================================================================
    # Hook Registration
    # =========================================================================

    def register_hook(
        self,
        name: str,
        phase: ShutdownPhase,
        callback: Callable[[], Awaitable[None]],
        priority: int = 100,
        timeout: float = 10.0,
        critical: bool = False,
    ) -> None:
        """
        Register a shutdown hook.

        Args:
            name: Hook name for logging
            phase: Phase to execute in
            callback: Async function to call
            priority: Execution priority (lower = earlier)
            timeout: Maximum execution time
            critical: If True, failure aborts shutdown
        """
        hook = ShutdownHook(
            name=name,
            phase=phase,
            priority=priority,
            callback=callback,
            timeout=timeout,
            critical=critical,
        )
        self._hooks[phase].append(hook)
        self._hooks[phase].sort()  # Keep sorted by priority

        logger.debug(
            f"[ShutdownManager] Registered hook '{name}' for phase {phase.name} "
            f"(priority={priority}, critical={critical})"
        )

    def unregister_hook(self, name: str, phase: Optional[ShutdownPhase] = None) -> None:
        """Unregister a hook by name."""
        phases = [phase] if phase else list(ShutdownPhase)
        for p in phases:
            self._hooks[p] = [h for h in self._hooks[p] if h.name != name]

    # =========================================================================
    # Process Registration
    # =========================================================================

    async def register_process(
        self,
        name: str,
        pid: int,
        component_type: Optional[str] = None,
    ) -> ManagedProcess:
        """Register a process for managed shutdown."""
        return await self.process_manager.register_process(
            name=name,
            pid=pid,
            component_type=component_type,
        )

    async def unregister_process(self, pid: int) -> None:
        """Unregister a process."""
        await self.process_manager.unregister_process(pid)

    # =========================================================================
    # Shutdown Execution
    # =========================================================================

    async def initiate_shutdown(
        self,
        reason: ShutdownReason = ShutdownReason.USER_REQUEST,
        timeout: Optional[float] = None,
        force: bool = False,
    ) -> ShutdownResult:
        """
        Initiate coordinated shutdown.

        Args:
            reason: Reason for shutdown
            timeout: Overall timeout (uses sum of phase timeouts if None)
            force: If True, skip draining and proceed immediately

        Returns:
            ShutdownResult with details
        """
        # ═══════════════════════════════════════════════════════════════════════════
        # v151.0: DIAGNOSTIC LOGGING - Capture EXACTLY what triggered shutdown
        # ═══════════════════════════════════════════════════════════════════════════
        import traceback
        import threading

        caller_stack = "".join(traceback.format_stack())
        caller_thread = threading.current_thread()

        _log_shutdown_trigger_safe(
            "CoordinatedShutdownManager.initiate_shutdown",
            f"Shutdown initiated - reason={reason.value}, force={force}",
            {
                "reason_value": reason.value,
                "force": force,
                "timeout": timeout,
                "thread_name": caller_thread.name,
                "thread_id": caller_thread.ident,
                "stack_trace": caller_stack,
            }
        )

        logger.warning(
            f"[v151.0] 🔬 SHUTDOWN TRIGGER DETECTED:\n"
            f"    Reason: {reason.value}\n"
            f"    Force: {force}\n"
            f"    Thread: {caller_thread.name}\n"
            f"    Stack trace:\n{caller_stack}"
        )
        # ═══════════════════════════════════════════════════════════════════════════

        async with self._shutdown_lock:
            if self._shutdown_in_progress and not force:
                logger.warning("[ShutdownManager] Shutdown already in progress")
                return ShutdownResult(
                    success=False,
                    phase_reached=self._current_phase or ShutdownPhase.ANNOUNCE,
                    elapsed_seconds=0,
                    processes_terminated=0,
                    hooks_executed=0,
                    hooks_failed=0,
                    errors=["Shutdown already in progress"],
                )

            self._shutdown_in_progress = True
            self._shutdown_reason = reason

            _log_state_change_safe(
                "CoordinatedShutdownManager",
                "idle",
                "shutting_down",
                f"reason={reason.value}"
            )

            # v95.13: Trigger global shutdown signal IMMEDIATELY
            # This ensures ALL components (OrphanDetector, recovery coordinator, etc.)
            # know shutdown is happening, regardless of which shutdown path is taken
            try:
                from backend.core.resilience.graceful_shutdown import initiate_global_shutdown
                initiate_global_shutdown(
                    reason=f"coordinated_{reason.value}",
                    initiator="CoordinatedShutdownManager"
                )
            except ImportError:
                logger.debug("[v95.13] Global shutdown signal not available")
            except Exception as e:
                logger.warning(f"[v95.13] Failed to trigger global shutdown: {e}")

            start_time = time.time()
            total_hooks_executed = 0
            total_hooks_failed = 0
            all_errors: List[str] = []
            state_saved = False

            logger.info(
                f"[ShutdownManager] Initiating shutdown (reason={reason.value})"
            )

            # Notify start callbacks
            for callback in self._on_shutdown_start:
                try:
                    callback(reason)
                except Exception as e:
                    logger.warning(f"[ShutdownManager] Start callback error: {e}")

            # Execute phases
            phases = list(ShutdownPhase)
            if force:
                # Skip DRAIN phase for forced shutdown
                phases = [p for p in phases if p != ShutdownPhase.DRAIN]

            last_successful_phase = ShutdownPhase.ANNOUNCE
            processes_terminated = 0

            for phase in phases:
                self._current_phase = phase
                phase_timeout = self._phase_timeouts[phase]

                logger.info(
                    f"[ShutdownManager] Executing phase {phase.name} "
                    f"(timeout={phase_timeout}s)"
                )

                try:
                    result = await asyncio.wait_for(
                        self._execute_phase(phase),
                        timeout=phase_timeout,
                    )

                    total_hooks_executed += result.hooks_executed
                    total_hooks_failed += result.hooks_failed
                    all_errors.extend(result.errors)

                    # Track state saved
                    if phase == ShutdownPhase.SAVE and result.success:
                        state_saved = True

                    # Notify phase complete callbacks
                    for callback in self._on_phase_complete:
                        try:
                            callback(phase, result)
                        except Exception as e:
                            logger.warning(
                                f"[ShutdownManager] Phase callback error: {e}"
                            )

                    if result.success:
                        last_successful_phase = phase
                    else:
                        # Check if any critical hooks failed
                        if any("critical" in e.lower() for e in result.errors):
                            logger.error(
                                f"[ShutdownManager] Critical failure in phase {phase.name}"
                            )
                            break

                except asyncio.TimeoutError:
                    logger.warning(
                        f"[ShutdownManager] Phase {phase.name} timed out"
                    )
                    all_errors.append(f"Phase {phase.name} timed out")

            # Execute termination
            if ShutdownPhase.TERMINATE in phases:
                terminated, failed = await self.process_manager.terminate_all(
                    order=self.TERMINATION_ORDER,
                )
                processes_terminated = terminated
                if failed > 0:
                    all_errors.append(f"Failed to terminate {failed} processes")

            elapsed = time.time() - start_time

            result = ShutdownResult(
                success=last_successful_phase == phases[-1],
                phase_reached=last_successful_phase,
                elapsed_seconds=elapsed,
                processes_terminated=processes_terminated,
                hooks_executed=total_hooks_executed,
                hooks_failed=total_hooks_failed,
                errors=all_errors,
                state_saved=state_saved,
            )

            # Notify complete callbacks
            for callback in self._on_shutdown_complete:
                try:
                    callback(result)
                except Exception as e:
                    logger.warning(
                        f"[ShutdownManager] Complete callback error: {e}"
                    )

            logger.info(
                f"[ShutdownManager] Shutdown complete: "
                f"success={result.success}, "
                f"phase_reached={result.phase_reached.name}, "
                f"elapsed={result.elapsed_seconds:.2f}s, "
                f"processes_terminated={result.processes_terminated}"
            )

            self._shutdown_in_progress = False
            self._current_phase = None

            return result

    async def _execute_phase(self, phase: ShutdownPhase) -> PhaseResult:
        """Execute a single shutdown phase."""
        start_time = time.time()
        hooks_executed = 0
        hooks_failed = 0
        errors: List[str] = []

        hooks = self._hooks[phase]
        logger.debug(
            f"[ShutdownManager] Phase {phase.name}: {len(hooks)} hooks to execute"
        )

        # Execute built-in actions for certain phases
        if phase == ShutdownPhase.ANNOUNCE:
            await self._announce_shutdown()
        elif phase == ShutdownPhase.SAVE:
            await self._save_state()

        # Execute registered hooks
        for hook in hooks:
            try:
                logger.debug(
                    f"[ShutdownManager] Executing hook '{hook.name}' "
                    f"(priority={hook.priority})"
                )

                await asyncio.wait_for(hook.callback(), timeout=hook.timeout)
                hooks_executed += 1

            except asyncio.TimeoutError:
                hooks_failed += 1
                error_msg = f"Hook '{hook.name}' timed out after {hook.timeout}s"
                errors.append(error_msg)
                logger.warning(f"[ShutdownManager] {error_msg}")

                if hook.critical:
                    errors.append(f"Critical hook '{hook.name}' failed")
                    break

            except Exception as e:
                hooks_failed += 1
                error_msg = f"Hook '{hook.name}' failed: {e}"
                errors.append(error_msg)
                logger.warning(f"[ShutdownManager] {error_msg}")

                if hook.critical:
                    errors.append(f"Critical hook '{hook.name}' failed")
                    break

        elapsed = time.time() - start_time
        success = hooks_failed == 0 or not any(
            h.critical for h in hooks if hooks_failed > 0
        )

        return PhaseResult(
            phase=phase,
            success=success,
            elapsed_seconds=elapsed,
            hooks_executed=hooks_executed,
            hooks_failed=hooks_failed,
            errors=errors,
        )

    async def _announce_shutdown(self) -> None:
        """
        Announce shutdown to all components via IPC with robust error handling.

        Uses proper TrinityCommand parameters and broadcasts to all Trinity components
        with configurable timeout and retry logic.
        """
        if self.ipc_bus is None:
            logger.debug("[ShutdownManager] No IPC bus configured - skipping announcement")
            return

        try:
            from backend.core.trinity_ipc import (
                TrinityCommand,
                ComponentType,
                CommandPriority,
            )
            import uuid

            # Generate correlation ID for tracing across all components
            correlation_id = f"shutdown_{uuid.uuid4().hex[:8]}"

            # Broadcast shutdown announcement to each Trinity component
            # Using CRITICAL priority to ensure immediate processing
            targets = [
                ComponentType.JARVIS_PRIME,
                ComponentType.REACTOR_CORE,
                ComponentType.CODING_COUNCIL,
            ]

            announcement_tasks = []
            for target in targets:
                command = TrinityCommand(
                    command_id=str(uuid.uuid4()),
                    source=ComponentType.JARVIS_BODY,
                    target=target,
                    action="shutdown_announce",
                    payload={
                        "reason": self._shutdown_reason.value if self._shutdown_reason else "unknown",
                        "timestamp": time.time(),
                        "initiator": "jarvis_body",
                        "correlation_id": correlation_id,
                        "phases": [phase.name for phase in ShutdownPhase],
                    },
                    priority=CommandPriority.CRITICAL,
                    timeout_seconds=5.0,  # Short timeout for announcement
                    correlation_id=correlation_id,
                )
                announcement_tasks.append(self.ipc_bus.enqueue_command(command))

            # Execute announcements in parallel with timeout protection
            if announcement_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*announcement_tasks, return_exceptions=True),
                        timeout=3.0,  # Don't let announcement block shutdown
                    )
                    logger.info(
                        f"[ShutdownManager] Shutdown announced via IPC to {len(targets)} components "
                        f"(correlation_id={correlation_id})"
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[ShutdownManager] Shutdown announcement timed out - proceeding anyway"
                    )

        except ImportError as e:
            logger.warning(f"[ShutdownManager] Trinity IPC not available: {e}")
        except Exception as e:
            # Don't let announcement failures block shutdown
            logger.warning(f"[ShutdownManager] Failed to announce shutdown: {e}")

    async def _save_state(self) -> None:
        """Save critical state before shutdown."""
        state_file = self.state_dir / "shutdown_state.json"

        import json

        state = {
            "timestamp": time.time(),
            "reason": self._shutdown_reason.value if self._shutdown_reason else "unknown",
            "processes": [
                {
                    "name": p.name,
                    "pid": p.pid,
                    "component_type": p.component_type,
                    "state": p.state.value,
                }
                for p in self.process_manager.get_all_processes()
            ],
        }

        try:
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)
            logger.info(f"[ShutdownManager] State saved to {state_file}")
        except Exception as e:
            logger.warning(f"[ShutdownManager] Failed to save state: {e}")

    # =========================================================================
    # Callbacks
    # =========================================================================

    def on_shutdown_start(
        self,
        callback: Callable[[ShutdownReason], None],
    ) -> None:
        """Register callback for shutdown start."""
        self._on_shutdown_start.append(callback)

    def on_phase_complete(
        self,
        callback: Callable[[ShutdownPhase, PhaseResult], None],
    ) -> None:
        """Register callback for phase completion."""
        self._on_phase_complete.append(callback)

    def on_shutdown_complete(
        self,
        callback: Callable[[ShutdownResult], None],
    ) -> None:
        """Register callback for shutdown completion."""
        self._on_shutdown_complete.append(callback)

    # =========================================================================
    # Status and Introspection
    # =========================================================================

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._shutdown_in_progress

    @property
    def current_phase(self) -> Optional[ShutdownPhase]:
        """Get current shutdown phase."""
        return self._current_phase

    def get_status(self) -> Dict[str, Any]:
        """Get shutdown manager status."""
        return {
            "shutting_down": self._shutdown_in_progress,
            "current_phase": self._current_phase.name if self._current_phase else None,
            "reason": self._shutdown_reason.value if self._shutdown_reason else None,
            "hooks": {
                phase.name: len(hooks)
                for phase, hooks in self._hooks.items()
            },
            "processes": [
                {
                    "name": p.name,
                    "pid": p.pid,
                    "state": p.state.value,
                    "component": p.component_type,
                }
                for p in self.process_manager.get_all_processes()
            ],
            "phase_timeouts": {
                phase.name: timeout
                for phase, timeout in self._phase_timeouts.items()
            },
        }


# =============================================================================
# Signal Handler Integration
# =============================================================================

def setup_signal_handlers(
    shutdown_manager: CoordinatedShutdownManager,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """
    Set up signal handlers for graceful shutdown with modern asyncio patterns.

    Handles SIGTERM, SIGINT, and SIGHUP with proper event loop context handling.
    Uses Python 3.10+ compatible APIs (no deprecated loop= parameter).

    Args:
        shutdown_manager: The shutdown manager to trigger on signals
        loop: Optional event loop (auto-detected if not provided)
    """
    import sys
    import functools

    # Get the running loop safely
    try:
        if loop is None:
            loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop - we're being called during setup before loop starts
        # Signal handlers will be set up but won't work until loop runs
        logger.warning(
            "[ShutdownManager] No running event loop - signal handlers may be delayed"
        )
        return

    # Track if shutdown was already triggered to prevent duplicate calls
    # v93.0: Use threading.Event for sync-safe flag and LazyAsyncLock for async-safe locking
    import threading
    _shutdown_triggered = threading.Event()
    _shutdown_lock = LazyAsyncLock()  # v93.0: Lazy lock avoids "no running event loop" issues

    async def _safe_shutdown(reason: ShutdownReason, sig_name: str) -> None:
        """
        v123.5: Execute shutdown with lock to prevent duplicates.

        CRITICAL: Must call os._exit() after shutdown completes.
        sys.exit() raises SystemExit which gets caught by asyncio task handling.
        os._exit() terminates immediately without exception propagation.
        """
        async with _shutdown_lock:
            if _shutdown_triggered.is_set():
                logger.debug(f"[ShutdownManager] Ignoring duplicate signal {sig_name}")
                return
            _shutdown_triggered.set()

        logger.info(f"[ShutdownManager] Initiating shutdown due to {sig_name}")
        try:
            result = await shutdown_manager.initiate_shutdown(reason=reason)

            # v123.5: CRITICAL - Must use os._exit() NOT sys.exit()
            # sys.exit() raises SystemExit which asyncio catches as task exception
            # os._exit() terminates immediately without exception propagation
            if result.success:
                logger.info(f"[ShutdownManager] Shutdown successful, exiting process with os._exit()")
                import os as _os
                # Use signal-appropriate exit codes
                if sig_name == "SIGTERM":
                    _os._exit(143)  # 128 + 15
                elif sig_name == "SIGINT":
                    _os._exit(130)  # 128 + 2
                else:
                    _os._exit(0)
            else:
                logger.error(f"[ShutdownManager] Shutdown incomplete: {result.errors}")
                import os as _os
                _os._exit(1)

        except Exception as e:
            logger.error(f"[ShutdownManager] Shutdown failed: {e}")
            # Force exit on shutdown failure
            import os as _os
            _os._exit(1)

    def handle_signal(signum: int) -> None:
        """Signal handler that schedules async shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info(f"[ShutdownManager] Received signal {sig_name}")

        # Determine shutdown reason based on signal
        reason_map = {
            signal.SIGTERM: ShutdownReason.SIGNAL_RECEIVED,
            signal.SIGINT: ShutdownReason.USER_REQUEST,
            signal.SIGHUP: ShutdownReason.UPDATE_REQUIRED,
        }
        reason = reason_map.get(signum, ShutdownReason.SIGNAL_RECEIVED)

        # Schedule shutdown using modern asyncio API (no deprecated loop= param)
        # Use create_task if we're in a running loop context
        try:
            running_loop = asyncio.get_running_loop()
            running_loop.create_task(
                _safe_shutdown(reason, sig_name),
                name=f"shutdown_signal_{sig_name}",
            )
        except RuntimeError:
            # No running loop - this shouldn't happen in normal operation
            logger.error(
                f"[ShutdownManager] Cannot handle signal {sig_name} - no running event loop"
            )

    # Signals to handle
    # v117.0: SIGHUP is reserved for os.execv() restart in supervisor_singleton.py
    # Only SIGTERM and SIGINT should trigger shutdown
    signals_to_handle = [signal.SIGTERM, signal.SIGINT]
    # NOTE: Do NOT add SIGHUP here - it's handled by supervisor_singleton._handle_sighup
    # for clean restart via os.execv() (fixes EXC_GUARD crash)

    # Register handlers with proper error handling
    for sig in signals_to_handle:
        try:
            # Use functools.partial to properly capture the signal value
            loop.add_signal_handler(
                sig,
                functools.partial(handle_signal, sig),
            )
            logger.debug(f"[ShutdownManager] Registered handler for {sig.name}")
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for all signals
            # Fall back to signal.signal() but note this blocks the event loop
            try:
                def windows_handler(signum, frame, captured_sig=sig):
                    handle_signal(captured_sig)
                signal.signal(sig, windows_handler)
                logger.debug(
                    f"[ShutdownManager] Registered Windows handler for {sig.name}"
                )
            except (ValueError, OSError) as e:
                logger.warning(
                    f"[ShutdownManager] Could not register handler for {sig.name}: {e}"
                )
        except Exception as e:
            logger.warning(
                f"[ShutdownManager] Failed to register handler for {sig.name}: {e}"
            )


# =============================================================================
# Singleton Access
# =============================================================================

_shutdown_manager: Optional[CoordinatedShutdownManager] = None
_manager_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_shutdown_manager(
    ipc_bus: Optional[Any] = None,
    **kwargs,
) -> CoordinatedShutdownManager:
    """Get the singleton shutdown manager."""
    global _shutdown_manager

    async with _manager_lock:
        if _shutdown_manager is None:
            _shutdown_manager = CoordinatedShutdownManager(
                ipc_bus=ipc_bus,
                **kwargs,
            )
        return _shutdown_manager


def get_shutdown_manager_sync(**kwargs) -> CoordinatedShutdownManager:
    """Synchronous version for non-async contexts."""
    global _shutdown_manager

    if _shutdown_manager is None:
        _shutdown_manager = CoordinatedShutdownManager(**kwargs)
    return _shutdown_manager


async def initiate_shutdown(
    reason: ShutdownReason = ShutdownReason.USER_REQUEST,
    **kwargs,
) -> ShutdownResult:
    """Convenience function to initiate shutdown."""
    manager = await get_shutdown_manager()
    return await manager.initiate_shutdown(reason=reason, **kwargs)


# =============================================================================
# Orphan Process Detection and Cleanup
# =============================================================================


@dataclass
class OrphanProcess:
    """Detected orphan process."""
    pid: int
    name: str
    cmdline: str
    started_at: float
    component_type: Optional[str] = None
    reason: str = "unknown"


class OrphanProcessDetector:
    """
    Detects and cleans up orphan JARVIS processes.

    Orphan processes can occur when:
    - Supervisor crashes without cleanup
    - System restart without proper shutdown
    - Zombie processes from failed subprocess management

    Features:
    - Process name pattern matching
    - PID file staleness detection
    - Heartbeat file age analysis
    - Safe termination with verification
    """

    # Process name patterns for JARVIS components
    COMPONENT_PATTERNS = {
        "jarvis_body": ["backend.main", "jarvis_supervisor", "run_supervisor"],
        "jarvis_prime": ["jarvis_prime", "jarvis-prime", "jprime"],
        "reactor_core": ["reactor_core", "reactor-core", "training_pipeline"],
    }

    def __init__(
        self,
        trinity_dir: Optional[Path] = None,
        max_orphan_age_hours: float = 24.0,
        startup_grace_period_seconds: float = 120.0,  # Grace period for new processes
    ):
        self.trinity_dir = trinity_dir or Path(
            os.environ.get("TRINITY_DIR", str(Path.home() / ".jarvis" / "trinity"))
        )
        self.max_orphan_age_hours = max_orphan_age_hours
        self.startup_grace_period_seconds = startup_grace_period_seconds
        self._detected_orphans: List[OrphanProcess] = []
        self._startup_time = time.time()  # Track when this detector was created

    async def detect_orphans(self) -> List[OrphanProcess]:
        """
        v95.3: Detect orphan JARVIS processes with orchestrator shutdown check.

        Returns:
            List of detected orphan processes

        CRITICAL (v95.3): Skips detection if orchestrator shutdown is in progress
        or completed, as processes may be intentionally terminating.
        """
        self._detected_orphans = []

        # v95.13: Check GLOBAL shutdown signal FIRST (most reliable)
        try:
            from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
            if is_global_shutdown_initiated():
                logger.info(
                    "[v95.13] OrphanDetector: Skipping detection - global shutdown initiated"
                )
                return []
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[v95.13] OrphanDetector: Error checking global shutdown: {e}")

        # v95.3: Check if orchestrator shutdown is in progress or completed
        # If so, don't detect orphans - processes are being managed by orchestrator
        try:
            from backend.supervisor.cross_repo_startup_orchestrator import (
                is_orchestrator_shutdown_in_progress,
                is_orchestrator_shutdown_completed,
            )

            if is_orchestrator_shutdown_in_progress() or is_orchestrator_shutdown_completed():
                logger.info(
                    "[v95.3] OrphanDetector: Skipping detection - orchestrator shutdown "
                    f"in progress ({is_orchestrator_shutdown_in_progress()}) or "
                    f"completed ({is_orchestrator_shutdown_completed()})"
                )
                return []
        except ImportError:
            # Orchestrator module not available - proceed with caution
            logger.debug("[v95.3] OrphanDetector: Orchestrator module not available")
        except Exception as e:
            logger.debug(f"[v95.3] OrphanDetector: Error checking orchestrator state: {e}")

        # Method 1: Check PID files for stale PIDs
        await self._check_stale_pid_files()

        # Method 2: Check for processes matching JARVIS patterns
        await self._check_process_patterns()

        # Method 3: Check heartbeat files for dead processes
        await self._check_stale_heartbeats()

        # Deduplicate by PID
        seen_pids = set()
        unique_orphans = []
        for orphan in self._detected_orphans:
            if orphan.pid not in seen_pids:
                seen_pids.add(orphan.pid)
                unique_orphans.append(orphan)

        self._detected_orphans = unique_orphans

        if self._detected_orphans:
            logger.info(
                f"[OrphanDetector] Found {len(self._detected_orphans)} orphan processes"
            )

        return self._detected_orphans

    async def _check_stale_pid_files(self) -> None:
        """Check for PID files pointing to dead processes."""
        pid_dir = self.trinity_dir / "pids"
        if not pid_dir.exists():
            return

        for pid_file in pid_dir.glob("*.pid"):
            try:
                pid = int(pid_file.read_text().strip())
                component = pid_file.stem

                # Check if process is still running
                if self._process_exists(pid):
                    # Process exists, but is it a JARVIS process?
                    cmdline = self._get_process_cmdline(pid)
                    if not self._matches_jarvis_pattern(cmdline):
                        # PID was reused by OS - file is stale
                        pid_file.unlink()
                        logger.debug(
                            f"[OrphanDetector] Removed stale PID file: {pid_file}"
                        )
                else:
                    # Process doesn't exist - file is stale
                    pid_file.unlink()
                    logger.debug(
                        f"[OrphanDetector] Removed stale PID file: {pid_file}"
                    )

            except (ValueError, PermissionError):
                continue

    async def _check_process_patterns(self) -> None:
        """Check for running processes matching JARVIS patterns."""
        try:
            import subprocess

            # Get all Python processes
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )

            if result.returncode != 0:
                return

            current_pid = os.getpid()

            for line in result.stdout.strip().split("\n")[1:]:  # Skip header
                parts = line.split(None, 10)
                if len(parts) < 11:
                    continue

                try:
                    pid = int(parts[1])
                except ValueError:
                    continue

                # Skip current process
                if pid == current_pid:
                    continue

                cmdline = parts[10] if len(parts) > 10 else ""

                # Check if this matches any JARVIS pattern
                component_type = None
                for comp, patterns in self.COMPONENT_PATTERNS.items():
                    if any(p in cmdline.lower() for p in patterns):
                        component_type = comp
                        break

                if component_type:
                    # Check if this process is within startup grace period
                    # (newly started processes haven't had time to establish heartbeats)
                    if self._is_within_startup_grace_period(pid):
                        logger.debug(
                            f"[OrphanDetector] Skipping PID {pid} ({component_type}) - "
                            f"within startup grace period"
                        )
                        continue

                    # Check if this process has a valid heartbeat
                    has_valid_heartbeat = await self._has_valid_heartbeat(
                        component_type, pid
                    )

                    if not has_valid_heartbeat:
                        # Get actual process start time for the orphan record
                        start_time = self._get_process_start_time(pid) or 0.0
                        # This is an orphan
                        orphan = OrphanProcess(
                            pid=pid,
                            name=component_type,
                            cmdline=cmdline[:200],  # Truncate
                            started_at=start_time,
                            component_type=component_type,
                            reason="no_valid_heartbeat",
                        )
                        self._detected_orphans.append(orphan)

        except Exception as e:
            logger.debug(f"[OrphanDetector] Process check failed: {e}")

    async def _check_stale_heartbeats(self) -> None:
        """
        v93.0: Enhanced heartbeat staleness detection with multi-directory support.

        Checks multiple heartbeat directories for backwards compatibility and
        cross-repo coordination.
        """
        # v93.0: Multiple heartbeat directories to check
        heartbeat_dirs = [
            self.trinity_dir / "heartbeats",      # PRIMARY: correct location
            self.trinity_dir / "components",      # LEGACY: old location
            Path.home() / ".jarvis" / "cross_repo",  # Cross-repo heartbeats
        ]

        processed_files: set = set()  # Avoid processing same file twice

        for heartbeat_dir in heartbeat_dirs:
            if not heartbeat_dir.exists():
                continue

            for hb_file in heartbeat_dir.glob("*.json"):
                # Skip if already processed (same component in different dir)
                if hb_file.stem in processed_files:
                    continue
                processed_files.add(hb_file.stem)

                try:
                    import json

                    with open(hb_file) as f:
                        data = json.load(f)

                    # v93.1: Validate JSON structure to prevent "'list' object has no attribute 'get'" errors
                    if not isinstance(data, dict):
                        logger.debug(
                            f"[OrphanDetector] Invalid heartbeat file format in {hb_file}: "
                            f"expected dict, got {type(data).__name__}"
                        )
                        continue

                    pid = data.get("pid")
                    if not pid:
                        continue

                    # Check if process exists
                    if not self._process_exists(pid):
                        # Heartbeat for dead process - file is stale
                        try:
                            hb_file.unlink()
                            logger.debug(
                                f"[OrphanDetector] Removed stale heartbeat: {hb_file}"
                            )
                        except OSError:
                            pass  # File may have been removed by another process
                    else:
                        # Process exists but heartbeat may be old
                        timestamp = data.get("timestamp", 0)
                        age_hours = (time.time() - timestamp) / 3600

                        if age_hours > self.max_orphan_age_hours:
                            # v93.0: Before marking as orphan, verify via HTTP health check
                            component = hb_file.stem

                            # v108.1: Check if process was RECENTLY started (within 120s)
                            # A freshly started process may have a stale heartbeat file from
                            # a previous run - don't mark it as orphan during startup grace period
                            try:
                                proc = psutil.Process(pid)
                                proc_age_seconds = time.time() - proc.create_time()
                                if proc_age_seconds < 120.0:  # 2 minute grace period
                                    logger.info(
                                        f"[OrphanDetector] {component} (PID={pid}) has stale heartbeat "
                                        f"but process is only {proc_age_seconds:.1f}s old - "
                                        f"allowing startup grace period"
                                    )
                                    continue
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass  # Process may have died, continue to check

                            # Try HTTP health check first - process may be healthy
                            if await self._http_health_check(component, pid):
                                logger.debug(
                                    f"[OrphanDetector] {component} has stale heartbeat file "
                                    f"but responds to HTTP health check - not an orphan"
                                )
                                continue

                            # Very old heartbeat and no HTTP response - process may be zombie
                            cmdline = self._get_process_cmdline(pid)

                            orphan = OrphanProcess(
                                pid=pid,
                                name=component,
                                cmdline=cmdline,
                                started_at=timestamp,
                                component_type=component.replace("_", "-"),
                                reason=f"stale_heartbeat_{age_hours:.1f}h",
                            )
                            self._detected_orphans.append(orphan)

                except (json.JSONDecodeError, KeyError, PermissionError):
                    continue

    async def _has_valid_heartbeat(
        self,
        component_type: str,
        pid: int,
    ) -> bool:
        """
        v93.0: Enhanced heartbeat validation with multi-source verification.

        Verification Strategy (Priority Order):
        1. HTTP health check (most reliable - proves service is responsive)
        2. Heartbeat file from multiple directories
        3. Process liveness as final fallback

        This prevents false positives where valid processes are incorrectly
        marked as orphans due to heartbeat directory mismatches.
        """
        # PHASE 1: HTTP Health Check (HIGHEST PRIORITY)
        # If the service responds to HTTP, it's definitely alive and healthy
        if await self._http_health_check(component_type, pid):
            logger.debug(
                f"[OrphanDetector] {component_type} (PID={pid}) validated via HTTP health check"
            )
            return True

        # PHASE 2: Multi-Directory Heartbeat File Check
        # v93.0: Multiple heartbeat directories for backwards compatibility
        heartbeat_dirs = [
            self.trinity_dir / "heartbeats",      # PRIMARY: correct location
            self.trinity_dir / "components",      # LEGACY: old location
            Path.home() / ".jarvis" / "cross_repo",  # Cross-repo heartbeats
        ]

        for heartbeat_dir in heartbeat_dirs:
            heartbeat_file = heartbeat_dir / f"{component_type}.json"

            if not heartbeat_file.exists():
                continue

            try:
                import json

                with open(heartbeat_file) as f:
                    data = json.load(f)

                # v93.1: Validate JSON structure to prevent "'list' object has no attribute 'get'" errors
                if not isinstance(data, dict):
                    logger.debug(
                        f"[OrphanDetector] Invalid heartbeat file format in {heartbeat_file}: "
                        f"expected dict, got {type(data).__name__}"
                    )
                    continue

                # Check PID matches
                file_pid = data.get("pid")
                if file_pid != pid:
                    logger.debug(
                        f"[OrphanDetector] {component_type} heartbeat PID mismatch: "
                        f"file={file_pid}, process={pid} in {heartbeat_dir}"
                    )
                    continue  # Try next directory

                # Check timestamp is recent
                timestamp = data.get("timestamp", 0)
                age_seconds = time.time() - timestamp

                # Valid if heartbeat is less than 5 minutes old
                if age_seconds < 300:
                    logger.debug(
                        f"[OrphanDetector] {component_type} (PID={pid}) has valid heartbeat "
                        f"({age_seconds:.1f}s old) in {heartbeat_dir}"
                    )
                    return True
                else:
                    logger.debug(
                        f"[OrphanDetector] {component_type} heartbeat is stale "
                        f"({age_seconds:.1f}s old) in {heartbeat_dir}"
                    )

            except Exception as e:
                logger.debug(
                    f"[OrphanDetector] Error reading heartbeat {heartbeat_file}: {e}"
                )
                continue

        # PHASE 3: No valid heartbeat found in any directory
        logger.debug(
            f"[OrphanDetector] {component_type} (PID={pid}) has no valid heartbeat in any directory"
        )
        return False

    def _process_exists(self, pid: int) -> bool:
        """Check if a process exists."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Exists but no permission

    def _get_process_cmdline(self, pid: int) -> str:
        """Get command line of a process."""
        try:
            # macOS/Linux
            proc_file = Path(f"/proc/{pid}/cmdline")
            if proc_file.exists():
                return proc_file.read_text().replace("\x00", " ").strip()

            # Fallback using ps
            import subprocess
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode == 0:
                return result.stdout.strip()

        except Exception:
            pass

        return "unknown"

    def _matches_jarvis_pattern(self, cmdline: str) -> bool:
        """Check if command line matches any JARVIS pattern."""
        cmdline_lower = cmdline.lower()
        for patterns in self.COMPONENT_PATTERNS.values():
            if any(p in cmdline_lower for p in patterns):
                return True
        return False

    # v93.0: HTTP port configuration for each component
    # Configurable via environment variables: JARVIS_BODY_PORTS, JARVIS_PRIME_PORTS, etc.
    @staticmethod
    def _get_component_ports() -> Dict[str, List[int]]:
        """Get component HTTP ports from environment or defaults."""
        def parse_ports(env_key: str, defaults: List[int]) -> List[int]:
            env_val = os.environ.get(env_key)
            if env_val:
                try:
                    return [int(p.strip()) for p in env_val.split(",")]
                except ValueError:
                    pass
            return defaults

        return {
            "jarvis_body": parse_ports("JARVIS_BODY_PORTS", [8080, 8000, 5000]),
            "jarvis_prime": parse_ports("JARVIS_PRIME_PORTS", [8091, 8001, 5001]),
            "reactor_core": parse_ports("REACTOR_CORE_PORTS", [8090, 8002, 5002]),
        }

    @property
    def COMPONENT_HTTP_PORTS(self) -> Dict[str, List[int]]:
        """Dynamic port configuration."""
        return self._get_component_ports()

    async def _http_health_check(self, component_type: str, pid: int) -> bool:
        """
        v93.0: Verify component health via HTTP health endpoint.

        This is the most reliable verification method because:
        - If HTTP responds, the service is definitely running AND responsive
        - Heartbeat files can be stale, but HTTP is real-time
        - Works across process boundaries and even network boundaries

        Args:
            component_type: Type of component (jarvis_body, jarvis_prime, reactor_core)
            pid: Process ID (used for logging/verification)

        Returns:
            True if HTTP health check passes, False otherwise
        """
        ports = self.COMPONENT_HTTP_PORTS.get(component_type, [])
        if not ports:
            # Unknown component type - can't do HTTP check
            return False

        try:
            import aiohttp
        except ImportError:
            # aiohttp not available - skip HTTP check
            logger.debug(
                f"[OrphanDetector] aiohttp not available for HTTP health check of {component_type}"
            )
            return False

        for port in ports:
            try:
                timeout = aiohttp.ClientTimeout(total=2.0)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    url = f"http://localhost:{port}/health"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            try:
                                data = await resp.json()
                                # Optional: verify PID matches if returned in health response
                                if "pid" in data and data["pid"] != pid:
                                    logger.debug(
                                        f"[OrphanDetector] HTTP health check PID mismatch: "
                                        f"response_pid={data.get('pid')}, process_pid={pid}"
                                    )
                                    continue

                                logger.debug(
                                    f"[OrphanDetector] {component_type} HTTP health check "
                                    f"passed at port {port}"
                                )
                                return True
                            except Exception:
                                # JSON parse failed but status was 200 - still consider valid
                                logger.debug(
                                    f"[OrphanDetector] {component_type} HTTP health check "
                                    f"passed at port {port} (non-JSON response)"
                                )
                                return True
            except asyncio.TimeoutError:
                logger.debug(
                    f"[OrphanDetector] HTTP health check timeout for {component_type} "
                    f"at port {port}"
                )
                continue
            except aiohttp.ClientError as e:
                logger.debug(
                    f"[OrphanDetector] HTTP health check failed for {component_type} "
                    f"at port {port}: {e}"
                )
                continue
            except Exception as e:
                logger.debug(
                    f"[OrphanDetector] HTTP health check error for {component_type} "
                    f"at port {port}: {e}"
                )
                continue

        return False

    def _get_process_start_time(self, pid: int) -> Optional[float]:
        """
        Get the start time of a process as Unix timestamp.

        Returns None if unable to determine start time.
        """
        try:
            import subprocess
            # Use ps to get process start time (works on macOS and Linux)
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Parse the lstart format (e.g., "Sun Jan 18 14:46:52 2026")
                from datetime import datetime
                try:
                    start_str = result.stdout.strip()
                    start_dt = datetime.strptime(start_str, "%a %b %d %H:%M:%S %Y")
                    return start_dt.timestamp()
                except ValueError:
                    pass

            # Alternative: use etime (elapsed time)
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "etime="],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                etime = result.stdout.strip()
                # Parse elapsed time format: [[DD-]hh:]mm:ss
                try:
                    parts = etime.replace("-", ":").split(":")
                    seconds = 0
                    if len(parts) == 2:  # mm:ss
                        seconds = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:  # hh:mm:ss
                        seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 4:  # DD:hh:mm:ss
                        seconds = int(parts[0]) * 86400 + int(parts[1]) * 3600 + int(parts[2]) * 60 + int(parts[3])
                    return time.time() - seconds
                except (ValueError, IndexError):
                    pass
        except Exception:
            pass
        return None

    def _is_within_startup_grace_period(self, pid: int) -> bool:
        """
        Check if a process is within the startup grace period.

        Returns True if the process started recently and should be
        given time to establish heartbeats.
        """
        # If we just started, give all processes grace period
        time_since_detector_start = time.time() - self._startup_time
        if time_since_detector_start < self.startup_grace_period_seconds:
            return True

        # Check process start time
        start_time = self._get_process_start_time(pid)
        if start_time is None:
            # Can't determine start time - give benefit of doubt
            return True

        process_age = time.time() - start_time
        return process_age < self.startup_grace_period_seconds

    async def cleanup_orphans(
        self,
        orphans: Optional[List[OrphanProcess]] = None,
        force: bool = False,
        timeout: float = 10.0,
    ) -> Tuple[int, int]:
        """
        v95.3: Clean up orphan processes with orchestrator shutdown state check.

        Args:
            orphans: List of orphans to clean (detects if None)
            force: Use SIGKILL immediately instead of SIGTERM
            timeout: Seconds to wait for graceful termination

        Returns:
            Tuple of (terminated_count, failed_count)

        CRITICAL (v95.3): Checks orchestrator shutdown state to prevent
        killing processes during orchestrator-managed shutdown.
        """
        # v95.3: Check if orchestrator shutdown is in progress or completed
        # v95.13: Check GLOBAL shutdown signal FIRST (most reliable)
        # This catches shutdowns initiated before orchestrator state is updated
        try:
            from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
            if is_global_shutdown_initiated():
                logger.info(
                    "[v95.13] OrphanDetector: Skipping cleanup - global shutdown initiated"
                )
                return (0, 0)
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[v95.13] OrphanDetector: Error checking global shutdown: {e}")

        # If so, the orchestrator is managing process termination - don't interfere
        try:
            from backend.supervisor.cross_repo_startup_orchestrator import (
                is_orchestrator_shutdown_in_progress,
                is_orchestrator_shutdown_completed,
            )

            if is_orchestrator_shutdown_in_progress() or is_orchestrator_shutdown_completed():
                logger.info(
                    "[v95.3] OrphanDetector: Skipping cleanup - orchestrator shutdown "
                    f"in progress ({is_orchestrator_shutdown_in_progress()}) or "
                    f"completed ({is_orchestrator_shutdown_completed()})"
                )
                return (0, 0)
        except ImportError:
            # Orchestrator module not available - proceed with caution
            logger.debug("[v95.3] OrphanDetector: Orchestrator module not available")
        except Exception as e:
            logger.debug(f"[v95.3] OrphanDetector: Error checking orchestrator state: {e}")

        if orphans is None:
            orphans = await self.detect_orphans()

        if not orphans:
            return (0, 0)

        terminated = 0
        failed = 0

        for orphan in orphans:
            try:
                logger.info(
                    f"[OrphanDetector] Terminating orphan: "
                    f"PID={orphan.pid}, component={orphan.component_type}, "
                    f"reason={orphan.reason}"
                )

                if force:
                    os.kill(orphan.pid, signal.SIGKILL)
                else:
                    os.kill(orphan.pid, signal.SIGTERM)

                # Wait for termination
                start = time.time()
                while time.time() - start < timeout:
                    if not self._process_exists(orphan.pid):
                        terminated += 1
                        break
                    await asyncio.sleep(0.2)
                else:
                    # Force kill if still running
                    try:
                        os.kill(orphan.pid, signal.SIGKILL)
                        await asyncio.sleep(0.5)
                        if not self._process_exists(orphan.pid):
                            terminated += 1
                        else:
                            failed += 1
                    except ProcessLookupError:
                        terminated += 1

            except ProcessLookupError:
                # Already dead
                terminated += 1
            except PermissionError:
                logger.warning(
                    f"[OrphanDetector] No permission to kill PID {orphan.pid}"
                )
                failed += 1
            except Exception as e:
                logger.warning(
                    f"[OrphanDetector] Failed to kill PID {orphan.pid}: {e}"
                )
                failed += 1

        logger.info(
            f"[OrphanDetector] Cleanup complete: "
            f"terminated={terminated}, failed={failed}"
        )

        return (terminated, failed)


# =============================================================================
# Enhanced Shutdown Manager with Orphan Detection
# =============================================================================


class EnhancedShutdownManager(CoordinatedShutdownManager):
    """
    Extended shutdown manager with orphan detection.

    Adds:
    - Pre-shutdown orphan cleanup
    - Post-shutdown verification
    - Cross-repo process coordination
    """

    def __init__(
        self,
        ipc_bus: Optional[Any] = None,
        state_dir: Optional[Path] = None,
        detect_orphans_on_start: bool = True,
    ):
        super().__init__(ipc_bus, state_dir)
        self.orphan_detector = OrphanProcessDetector()
        self._detect_orphans_on_start = detect_orphans_on_start

    async def startup_cleanup(self) -> Tuple[int, int]:
        """
        Clean up orphan processes on startup.

        Should be called before starting JARVIS components.

        Returns:
            Tuple of (terminated, failed) counts
        """
        if not self._detect_orphans_on_start:
            return (0, 0)

        logger.info("[EnhancedShutdown] Checking for orphan processes...")
        orphans = await self.orphan_detector.detect_orphans()

        if orphans:
            logger.warning(
                f"[EnhancedShutdown] Found {len(orphans)} orphan processes, cleaning up..."
            )
            return await self.orphan_detector.cleanup_orphans(orphans)

        logger.info("[EnhancedShutdown] No orphan processes found")
        return (0, 0)

    async def initiate_shutdown(
        self,
        reason: ShutdownReason = ShutdownReason.USER_REQUEST,
        timeout: Optional[float] = None,
        force: bool = False,
        verify_cleanup: bool = True,
    ) -> ShutdownResult:
        """
        Initiate shutdown with verification.

        Args:
            reason: Reason for shutdown
            timeout: Overall timeout
            force: Skip draining phase
            verify_cleanup: Run orphan detection after shutdown

        Returns:
            ShutdownResult
        """
        result = await super().initiate_shutdown(reason, timeout, force)

        if verify_cleanup:
            # v90.0: Enhanced orphan cleanup with retry logic and longer delays
            # Give processes more time to fully terminate
            orphan_check_delay = float(os.getenv("SHUTDOWN_ORPHAN_CHECK_DELAY", "3.0"))
            max_orphan_retries = int(os.getenv("SHUTDOWN_ORPHAN_RETRIES", "3"))

            await asyncio.sleep(orphan_check_delay)
            orphans = await self.orphan_detector.detect_orphans()

            retry_count = 0
            while orphans and retry_count < max_orphan_retries:
                retry_count += 1
                logger.info(
                    f"[EnhancedShutdown] {len(orphans)} orphans detected, "
                    f"cleanup attempt {retry_count}/{max_orphan_retries}..."
                )

                terminated, failed = await self.orphan_detector.cleanup_orphans(
                    orphans, force=(retry_count > 1)  # Force after first attempt
                )
                result.processes_terminated += terminated

                # Wait and recheck
                await asyncio.sleep(2.0)
                orphans = await self.orphan_detector.detect_orphans()

            if orphans:
                # v90.0: Log as debug instead of warning if retries exhausted
                # These are likely protected processes or system processes
                orphan_pids = [o.pid for o in orphans]
                logger.debug(
                    f"[EnhancedShutdown] {len(orphans)} processes remain "
                    f"after {max_orphan_retries} cleanup attempts: {orphan_pids}"
                )
            else:
                logger.info("[EnhancedShutdown] All orphan processes cleaned up")

        return result


# =============================================================================
# Convenience Functions
# =============================================================================


async def detect_orphan_processes() -> List[OrphanProcess]:
    """Detect orphan JARVIS processes."""
    detector = OrphanProcessDetector()
    return await detector.detect_orphans()


async def cleanup_orphan_processes(force: bool = False) -> Tuple[int, int]:
    """Detect and cleanup orphan processes."""
    detector = OrphanProcessDetector()
    orphans = await detector.detect_orphans()
    return await detector.cleanup_orphans(orphans, force=force)
