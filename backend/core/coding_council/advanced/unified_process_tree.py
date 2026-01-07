"""
v78.0: Unified Process Tree Manager
====================================

Enterprise-grade process hierarchy management for the Trinity architecture.

Features:
- Complete process tree tracking (run_supervisor → start_system → main.py)
- Cascading shutdown (children before parents)
- Orphan detection and automatic cleanup
- Health monitoring for entire process tree
- Process state machine with transitions
- Crash detection and recovery
- Resource usage aggregation across tree

Architecture:
    run_supervisor.py (PID 1000) [SUPERVISOR]
      ├── loading_server.py (PID 1001) [SERVICE]
      ├── jprime_orchestrator (PID 2000) [TRINITY]
      ├── reactor_core_orchestrator (PID 2001) [TRINITY]
      └── backend/main.py (PID 3000) [BACKEND]
          ├── uvicorn worker 1 (PID 3001) [WORKER]
          └── uvicorn worker 2 (PID 3002) [WORKER]

Usage:
    from backend.core.coding_council.advanced.unified_process_tree import (
        get_process_tree,
        ProcessNode,
        ProcessRole,
    )

    tree = await get_process_tree()
    await tree.register_process(pid, parent_pid, "backend", ProcessRole.BACKEND)
    await tree.shutdown_tree(graceful=True)

Author: JARVIS v78.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class ProcessRole(Enum):
    """Role of a process in the Trinity architecture."""
    SUPERVISOR = "supervisor"      # run_supervisor.py
    BACKEND = "backend"            # backend/main.py
    FRONTEND = "frontend"          # frontend server
    TRINITY_JPRIME = "jprime"      # J-Prime orchestrator
    TRINITY_REACTOR = "reactor"    # Reactor-Core orchestrator
    SERVICE = "service"            # Supporting services (loading server, etc.)
    WORKER = "worker"              # Worker processes (uvicorn workers)
    UNKNOWN = "unknown"


class ProcessState(Enum):
    """State of a process."""
    REGISTERED = "registered"      # Process registered but not verified
    STARTING = "starting"          # Process is starting
    RUNNING = "running"            # Process is running and healthy
    DEGRADED = "degraded"          # Process is running but unhealthy
    STOPPING = "stopping"          # Process is being stopped
    STOPPED = "stopped"            # Process has stopped
    CRASHED = "crashed"            # Process crashed unexpectedly
    ORPHANED = "orphaned"          # Process has no parent (orphan)
    ZOMBIE = "zombie"              # Process is a zombie


class ShutdownStrategy(Enum):
    """Strategy for shutting down a process."""
    GRACEFUL = "graceful"          # SIGTERM, wait, then SIGKILL
    IMMEDIATE = "immediate"        # SIGKILL immediately
    CASCADING = "cascading"        # Children first, then parent


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ProcessMetrics:
    """Resource metrics for a process."""
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    memory_percent: float = 0.0
    num_threads: int = 0
    num_fds: int = 0
    io_read_bytes: int = 0
    io_write_bytes: int = 0
    last_updated: float = field(default_factory=time.time)


@dataclass
class ProcessNode:
    """
    Represents a process in the tree.

    Each node tracks:
    - Process identity (PID, name, role)
    - Hierarchy (parent, children)
    - State and health
    - Resource metrics
    - Lifecycle events
    """
    pid: int
    name: str
    role: ProcessRole
    parent_pid: Optional[int] = None
    children: List[int] = field(default_factory=list)
    state: ProcessState = ProcessState.REGISTERED
    critical: bool = True
    start_time: float = field(default_factory=time.time)
    stop_time: Optional[float] = None
    metrics: ProcessMetrics = field(default_factory=ProcessMetrics)
    restart_count: int = 0
    max_restarts: int = 3
    last_heartbeat: float = field(default_factory=time.time)
    heartbeat_timeout: float = 30.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    shutdown_func: Optional[Callable] = None

    @property
    def is_alive(self) -> bool:
        """Check if process is still running."""
        if not PSUTIL_AVAILABLE:
            return self.state == ProcessState.RUNNING
        try:
            proc = psutil.Process(self.pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    @property
    def is_healthy(self) -> bool:
        """Check if process is healthy."""
        if not self.is_alive:
            return False
        # Check heartbeat age
        heartbeat_age = time.time() - self.last_heartbeat
        return heartbeat_age < self.heartbeat_timeout

    @property
    def uptime_seconds(self) -> float:
        """Get process uptime in seconds."""
        if self.stop_time:
            return self.stop_time - self.start_time
        return time.time() - self.start_time

    def update_heartbeat(self):
        """Update the heartbeat timestamp."""
        self.last_heartbeat = time.time()

    def update_metrics(self):
        """Update resource metrics from psutil."""
        if not PSUTIL_AVAILABLE or not self.is_alive:
            return

        try:
            proc = psutil.Process(self.pid)
            with proc.oneshot():
                self.metrics.cpu_percent = proc.cpu_percent()
                mem_info = proc.memory_info()
                self.metrics.memory_mb = mem_info.rss / (1024 * 1024)
                self.metrics.memory_percent = proc.memory_percent()
                self.metrics.num_threads = proc.num_threads()
                try:
                    self.metrics.num_fds = proc.num_fds()
                except (psutil.AccessDenied, AttributeError):
                    pass
                try:
                    io_counters = proc.io_counters()
                    self.metrics.io_read_bytes = io_counters.read_bytes
                    self.metrics.io_write_bytes = io_counters.write_bytes
                except (psutil.AccessDenied, AttributeError):
                    pass
            self.metrics.last_updated = time.time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


@dataclass
class TreeSnapshot:
    """Snapshot of the process tree state."""
    timestamp: datetime
    root_pid: Optional[int]
    total_processes: int
    running_processes: int
    crashed_processes: int
    orphaned_processes: int
    total_cpu_percent: float
    total_memory_mb: float
    processes: Dict[int, Dict[str, Any]]


# =============================================================================
# Unified Process Tree Manager
# =============================================================================

class UnifiedProcessTree:
    """
    Unified Process Tree Manager for Trinity Architecture.

    Tracks the entire process hierarchy from run_supervisor.py down to
    individual worker processes. Provides:

    - Complete tree visualization
    - Cascading shutdown (children before parents)
    - Orphan detection and cleanup
    - Health monitoring across all processes
    - Crash detection and recovery
    - Resource aggregation

    Thread-safe and async-compatible.
    """

    def __init__(self, logger_instance: Optional[logging.Logger] = None):
        self.log = logger_instance or logger
        self._nodes: Dict[int, ProcessNode] = {}
        self._root_pid: Optional[int] = None
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._monitoring_task: Optional[asyncio.Task] = None
        self._state_file = Path.home() / ".jarvis" / "trinity" / "process_tree.json"
        self._callbacks: Dict[str, List[Callable]] = {
            "on_crash": [],
            "on_orphan": [],
            "on_state_change": [],
        }

        # Ensure state directory exists
        self._state_file.parent.mkdir(parents=True, exist_ok=True)

    async def register_process(
        self,
        pid: int,
        name: str,
        role: ProcessRole,
        parent_pid: Optional[int] = None,
        critical: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        shutdown_func: Optional[Callable] = None,
    ) -> ProcessNode:
        """
        Register a process in the tree.

        Args:
            pid: Process ID
            name: Human-readable name
            role: Process role in architecture
            parent_pid: Parent process ID (None for root)
            critical: If True, crash affects parent
            metadata: Additional metadata
            shutdown_func: Function to call on shutdown

        Returns:
            ProcessNode for the registered process
        """
        async with self._lock:
            # Create node
            node = ProcessNode(
                pid=pid,
                name=name,
                role=role,
                parent_pid=parent_pid,
                critical=critical,
                metadata=metadata or {},
                shutdown_func=shutdown_func,
            )

            # Set as root if no parent
            if parent_pid is None:
                self._root_pid = pid
            else:
                # Add to parent's children
                parent = self._nodes.get(parent_pid)
                if parent and pid not in parent.children:
                    parent.children.append(pid)

            self._nodes[pid] = node

            # Update state
            if node.is_alive:
                node.state = ProcessState.RUNNING
            else:
                node.state = ProcessState.REGISTERED

            self.log.info(
                f"[ProcessTree] Registered: {name} (PID {pid}, role={role.value}, "
                f"parent={parent_pid})"
            )

            # Persist state
            await self._persist_state()

            return node

    async def unregister_process(self, pid: int) -> bool:
        """
        Unregister a process from the tree.

        Args:
            pid: Process ID to unregister

        Returns:
            True if process was unregistered
        """
        async with self._lock:
            node = self._nodes.get(pid)
            if not node:
                return False

            # Remove from parent's children
            if node.parent_pid:
                parent = self._nodes.get(node.parent_pid)
                if parent and pid in parent.children:
                    parent.children.remove(pid)

            # Handle children (make them orphans or reassign)
            for child_pid in node.children:
                child = self._nodes.get(child_pid)
                if child:
                    child.parent_pid = node.parent_pid  # Reassign to grandparent
                    child.state = ProcessState.ORPHANED
                    await self._fire_callback("on_orphan", child)

            # Remove node
            del self._nodes[pid]

            if self._root_pid == pid:
                self._root_pid = None

            self.log.info(f"[ProcessTree] Unregistered: {node.name} (PID {pid})")

            await self._persist_state()
            return True

    async def update_state(self, pid: int, state: ProcessState):
        """Update the state of a process."""
        async with self._lock:
            node = self._nodes.get(pid)
            if not node:
                return

            old_state = node.state
            node.state = state

            if state == ProcessState.STOPPED:
                node.stop_time = time.time()

            await self._fire_callback("on_state_change", node, old_state, state)

    async def heartbeat(self, pid: int) -> bool:
        """
        Record a heartbeat for a process.

        Returns:
            True if process exists and heartbeat was recorded
        """
        async with self._lock:
            node = self._nodes.get(pid)
            if not node:
                return False

            node.update_heartbeat()

            # Update state if previously degraded
            if node.state == ProcessState.DEGRADED and node.is_healthy:
                node.state = ProcessState.RUNNING

            return True

    async def get_node(self, pid: int) -> Optional[ProcessNode]:
        """Get a process node by PID."""
        return self._nodes.get(pid)

    async def get_children(self, pid: int) -> List[ProcessNode]:
        """Get all children of a process."""
        node = self._nodes.get(pid)
        if not node:
            return []

        return [
            self._nodes[child_pid]
            for child_pid in node.children
            if child_pid in self._nodes
        ]

    async def get_descendants(self, pid: int) -> List[ProcessNode]:
        """Get all descendants (children, grandchildren, etc.) of a process."""
        descendants = []

        async def _collect(parent_pid: int):
            children = await self.get_children(parent_pid)
            for child in children:
                descendants.append(child)
                await _collect(child.pid)

        await _collect(pid)
        return descendants

    async def get_shutdown_order(self, root_pid: Optional[int] = None) -> List[int]:
        """
        Get optimal shutdown order (children before parents).

        Uses reverse topological sort to ensure children are
        shut down before their parents.
        """
        root = root_pid or self._root_pid
        if not root:
            return []

        # Build shutdown order (deepest first)
        order = []
        visited = set()

        async def _visit(pid: int, depth: int = 0):
            if pid in visited:
                return
            visited.add(pid)

            node = self._nodes.get(pid)
            if not node:
                return

            # Visit children first (deeper in tree)
            for child_pid in node.children:
                await _visit(child_pid, depth + 1)

            order.append((pid, depth))

        await _visit(root)

        # Sort by depth (deepest first), then by order visited
        order.sort(key=lambda x: -x[1])
        return [pid for pid, _ in order]

    async def shutdown_tree(
        self,
        root_pid: Optional[int] = None,
        strategy: ShutdownStrategy = ShutdownStrategy.CASCADING,
        timeout: float = 30.0,
        force_after: float = 10.0,
    ) -> Dict[int, bool]:
        """
        Shutdown the entire process tree.

        Args:
            root_pid: Root of subtree to shutdown (None for entire tree)
            strategy: Shutdown strategy
            timeout: Maximum time for entire shutdown
            force_after: Time before force killing

        Returns:
            Dict mapping PID to success status
        """
        self._shutdown_event.set()
        results: Dict[int, bool] = {}
        start_time = time.time()

        root = root_pid or self._root_pid
        if not root:
            return results

        self.log.info(f"[ProcessTree] Initiating {strategy.value} shutdown...")

        if strategy == ShutdownStrategy.CASCADING:
            # Get optimal shutdown order
            shutdown_order = await self.get_shutdown_order(root)
            self.log.info(f"[ProcessTree] Shutdown order: {shutdown_order}")

            for pid in shutdown_order:
                if time.time() - start_time > timeout:
                    self.log.warning("[ProcessTree] Shutdown timeout exceeded")
                    break

                success = await self._shutdown_process(
                    pid,
                    graceful=True,
                    timeout=force_after
                )
                results[pid] = success

        elif strategy == ShutdownStrategy.GRACEFUL:
            # Send SIGTERM to all, wait, then force
            for pid in list(self._nodes.keys()):
                await self._send_signal(pid, signal.SIGTERM)
                await self.update_state(pid, ProcessState.STOPPING)

            # Wait for graceful shutdown
            await asyncio.sleep(min(force_after, timeout / 2))

            # Force kill remaining
            for pid in list(self._nodes.keys()):
                node = self._nodes.get(pid)
                if node and node.is_alive:
                    success = await self._shutdown_process(pid, graceful=False)
                    results[pid] = success
                else:
                    results[pid] = True

        elif strategy == ShutdownStrategy.IMMEDIATE:
            # Force kill all immediately
            for pid in list(self._nodes.keys()):
                success = await self._shutdown_process(pid, graceful=False)
                results[pid] = success

        self.log.info(f"[ProcessTree] Shutdown complete: {sum(results.values())}/{len(results)} succeeded")
        return results

    async def _shutdown_process(
        self,
        pid: int,
        graceful: bool = True,
        timeout: float = 5.0,
    ) -> bool:
        """Shutdown a single process."""
        node = self._nodes.get(pid)
        if not node:
            return True  # Already gone

        if not node.is_alive:
            await self.update_state(pid, ProcessState.STOPPED)
            return True

        self.log.info(f"[ProcessTree] Stopping {node.name} (PID {pid})...")

        # Call custom shutdown function if provided
        if node.shutdown_func:
            try:
                if asyncio.iscoroutinefunction(node.shutdown_func):
                    await asyncio.wait_for(node.shutdown_func(), timeout=timeout/2)
                else:
                    node.shutdown_func()
            except Exception as e:
                self.log.warning(f"[ProcessTree] Custom shutdown failed: {e}")

        # Send SIGTERM
        if graceful:
            await self._send_signal(pid, signal.SIGTERM)
            await self.update_state(pid, ProcessState.STOPPING)

            # Wait for graceful shutdown
            for _ in range(int(timeout * 10)):
                if not node.is_alive:
                    await self.update_state(pid, ProcessState.STOPPED)
                    return True
                await asyncio.sleep(0.1)

        # Force kill if still alive
        if node.is_alive:
            self.log.warning(f"[ProcessTree] Force killing {node.name} (PID {pid})")
            await self._send_signal(pid, signal.SIGKILL)
            await asyncio.sleep(0.5)

        await self.update_state(pid, ProcessState.STOPPED)
        return not node.is_alive

    async def _send_signal(self, pid: int, sig: signal.Signals) -> bool:
        """Send a signal to a process."""
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError) as e:
            self.log.debug(f"[ProcessTree] Signal {sig} to {pid} failed: {e}")
            return False

    async def start_monitoring(self, interval: float = 5.0):
        """Start background monitoring of all processes."""
        if self._monitoring_task and not self._monitoring_task.done():
            return

        async def _monitor():
            while not self._shutdown_event.is_set():
                try:
                    await self._check_all_processes()
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.log.error(f"[ProcessTree] Monitoring error: {e}")
                    await asyncio.sleep(interval)

        self._monitoring_task = asyncio.create_task(_monitor())
        self.log.info("[ProcessTree] Background monitoring started")

    async def stop_monitoring(self):
        """Stop background monitoring."""
        self._shutdown_event.set()
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

    async def _check_all_processes(self):
        """Check health of all processes."""
        for pid, node in list(self._nodes.items()):
            # Update metrics
            node.update_metrics()

            # Check if process is alive
            if not node.is_alive and node.state == ProcessState.RUNNING:
                node.state = ProcessState.CRASHED
                self.log.warning(f"[ProcessTree] CRASH detected: {node.name} (PID {pid})")
                await self._fire_callback("on_crash", node)

            # v78.1: Auto-heartbeat for processes verified alive via psutil
            # If process is alive (verified by OS), automatically update heartbeat
            # This prevents false DEGRADED states for processes that don't self-report
            elif node.is_alive:
                # Process is alive - update heartbeat if metrics were successfully gathered
                if node.metrics.last_updated > node.last_heartbeat:
                    node.update_heartbeat()
                    # Recover from degraded state if now healthy
                    if node.state == ProcessState.DEGRADED:
                        node.state = ProcessState.RUNNING
                        self.log.info(f"[ProcessTree] RECOVERED: {node.name} (PID {pid}) - now healthy")
                # Only mark as degraded if we can't verify via psutil AND heartbeat timed out
                elif not node.is_healthy and not node.metrics.last_updated:
                    if node.state == ProcessState.RUNNING:
                        node.state = ProcessState.DEGRADED
                        self.log.warning(f"[ProcessTree] DEGRADED: {node.name} (PID {pid}) - no metrics or heartbeat")

            # Check for orphans
            if node.parent_pid and node.parent_pid not in self._nodes:
                if node.state != ProcessState.ORPHANED:
                    node.state = ProcessState.ORPHANED
                    await self._fire_callback("on_orphan", node)

    async def detect_orphans(self) -> List[ProcessNode]:
        """Detect orphaned processes."""
        orphans = []
        for node in self._nodes.values():
            if node.parent_pid and node.parent_pid not in self._nodes:
                node.state = ProcessState.ORPHANED
                orphans.append(node)
        return orphans

    async def cleanup_orphans(self) -> int:
        """Clean up orphaned processes."""
        orphans = await self.detect_orphans()
        cleaned = 0

        for node in orphans:
            if await self._shutdown_process(node.pid, graceful=True):
                await self.unregister_process(node.pid)
                cleaned += 1

        if cleaned > 0:
            self.log.info(f"[ProcessTree] Cleaned up {cleaned} orphan processes")

        return cleaned

    def add_callback(self, event: str, callback: Callable):
        """Add a callback for process events."""
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    async def _fire_callback(self, event: str, *args):
        """Fire callbacks for an event."""
        for callback in self._callbacks.get(event, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(*args)
                else:
                    callback(*args)
            except Exception as e:
                self.log.error(f"[ProcessTree] Callback error: {e}")

    async def get_snapshot(self) -> TreeSnapshot:
        """Get a snapshot of the current tree state."""
        total_cpu = 0.0
        total_mem = 0.0
        running = 0
        crashed = 0
        orphaned = 0
        processes = {}

        for pid, node in self._nodes.items():
            node.update_metrics()
            total_cpu += node.metrics.cpu_percent
            total_mem += node.metrics.memory_mb

            if node.state == ProcessState.RUNNING:
                running += 1
            elif node.state == ProcessState.CRASHED:
                crashed += 1
            elif node.state == ProcessState.ORPHANED:
                orphaned += 1

            processes[pid] = {
                "name": node.name,
                "role": node.role.value,
                "state": node.state.value,
                "parent_pid": node.parent_pid,
                "children": node.children,
                "uptime_seconds": node.uptime_seconds,
                "cpu_percent": node.metrics.cpu_percent,
                "memory_mb": node.metrics.memory_mb,
            }

        return TreeSnapshot(
            timestamp=datetime.now(),
            root_pid=self._root_pid,
            total_processes=len(self._nodes),
            running_processes=running,
            crashed_processes=crashed,
            orphaned_processes=orphaned,
            total_cpu_percent=total_cpu,
            total_memory_mb=total_mem,
            processes=processes,
        )

    async def _persist_state(self):
        """Persist tree state to disk."""
        try:
            snapshot = await self.get_snapshot()
            state = {
                "timestamp": snapshot.timestamp.isoformat(),
                "root_pid": snapshot.root_pid,
                "processes": snapshot.processes,
            }
            self._state_file.write_text(json.dumps(state, indent=2))
        except Exception as e:
            self.log.debug(f"[ProcessTree] Failed to persist state: {e}")

    async def load_state(self) -> bool:
        """Load tree state from disk."""
        try:
            if not self._state_file.exists():
                return False

            state = json.loads(self._state_file.read_text())
            # Validate that processes are still running
            for pid_str, info in state.get("processes", {}).items():
                pid = int(pid_str)
                if PSUTIL_AVAILABLE:
                    try:
                        proc = psutil.Process(pid)
                        if proc.is_running():
                            await self.register_process(
                                pid=pid,
                                name=info["name"],
                                role=ProcessRole(info["role"]),
                                parent_pid=info.get("parent_pid"),
                            )
                    except psutil.NoSuchProcess:
                        pass
            return True
        except Exception as e:
            self.log.debug(f"[ProcessTree] Failed to load state: {e}")
            return False

    def visualize(self) -> str:
        """Generate ASCII visualization of process tree."""
        if not self._root_pid:
            return "Empty process tree"

        lines = []

        def _draw(pid: int, prefix: str = "", is_last: bool = True):
            node = self._nodes.get(pid)
            if not node:
                return

            # Determine node symbol
            state_symbol = {
                ProcessState.RUNNING: "✅",
                ProcessState.DEGRADED: "⚠️",
                ProcessState.CRASHED: "❌",
                ProcessState.STOPPED: "⏹️",
                ProcessState.ORPHANED: "👻",
            }.get(node.state, "❓")

            connector = "└── " if is_last else "├── "
            lines.append(
                f"{prefix}{connector}{state_symbol} {node.name} "
                f"(PID {pid}, {node.role.value})"
            )

            # Draw children
            child_prefix = prefix + ("    " if is_last else "│   ")
            for i, child_pid in enumerate(node.children):
                _draw(child_pid, child_prefix, i == len(node.children) - 1)

        _draw(self._root_pid)
        return "\n".join(lines)


# =============================================================================
# Singleton Instance
# =============================================================================

_process_tree: Optional[UnifiedProcessTree] = None
_tree_lock: Optional[asyncio.Lock] = None  # v78.1: Lazy init for Python 3.9 compat


def _get_tree_lock() -> asyncio.Lock:
    """v78.1: Lazy lock initialization to avoid 'no running event loop' error on import."""
    global _tree_lock
    if _tree_lock is None:
        _tree_lock = asyncio.Lock()
    return _tree_lock


async def get_process_tree() -> UnifiedProcessTree:
    """Get or create the singleton process tree instance."""
    global _process_tree

    async with _get_tree_lock():
        if _process_tree is None:
            _process_tree = UnifiedProcessTree()
            # Try to load previous state
            await _process_tree.load_state()
        return _process_tree


def get_process_tree_sync() -> Optional[UnifiedProcessTree]:
    """Get the process tree synchronously (may be None)."""
    return _process_tree
