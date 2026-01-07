"""
Intelligent Port Manager - Async, Parallel, and Dynamic Port Coordination
==========================================================================

Fixes the ROOT CAUSE of port conflicts by:
1. Early detection of unkillable processes (system, zombie, protected)
2. Parallel cleanup strategies instead of sequential timeouts
3. Immediate port fallback when process is detected as unkillable
4. Smart process classification to avoid wasting time on futile cleanup

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  IntelligentPortManager                                             │
    │  ├── ProcessClassifier (identify killable vs protected)             │
    │  ├── ParallelCleanupExecutor (concurrent cleanup strategies)        │
    │  ├── EarlyFallbackDecider (switch to fallback fast)                 │
    │  └── PortStateMonitor (async port availability tracking)            │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS v11.1 - Intelligent Port Management
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)


# =============================================================================
# Process Classification
# =============================================================================

class ProcessType(enum.Enum):
    """Classification of process killability."""
    KILLABLE = "killable"                    # Normal process, can be killed
    SYSTEM_PROTECTED = "system_protected"    # Kernel/system process, cannot kill
    ZOMBIE = "zombie"                        # Already dead, needs reaping
    PERMISSION_DENIED = "permission_denied"  # Requires elevated privileges
    OUR_PROCESS = "our_process"              # Our own managed process
    RELATED_PROCESS = "related_process"      # Parent/child/sibling
    HEALTHY_JARVIS = "healthy_jarvis"        # Existing healthy JARVIS Prime
    UNRESPONSIVE = "unresponsive"            # Process exists but not responding
    UNKNOWN = "unknown"                      # Cannot determine


@dataclass
class ProcessInfo:
    """Detailed information about a process on a port."""
    pid: int
    port: int
    process_type: ProcessType
    name: str = "unknown"
    cmdline: str = ""
    status: str = ""
    parent_pid: Optional[int] = None
    is_jarvis_prime: bool = False
    is_healthy: bool = False
    detection_time_ms: float = 0.0
    error: Optional[str] = None

    @property
    def is_killable(self) -> bool:
        """Check if this process can be safely killed."""
        return self.process_type in (
            ProcessType.KILLABLE,
            ProcessType.UNRESPONSIVE,
        )

    @property
    def should_adopt(self) -> bool:
        """Check if we should adopt this process instead of killing."""
        return self.process_type == ProcessType.HEALTHY_JARVIS

    @property
    def needs_fallback(self) -> bool:
        """Check if we should immediately use fallback port."""
        return self.process_type in (
            ProcessType.SYSTEM_PROTECTED,
            ProcessType.PERMISSION_DENIED,
            ProcessType.RELATED_PROCESS,
        )


@dataclass
class CleanupResult:
    """Result of a port cleanup attempt."""
    success: bool
    method: str
    elapsed_ms: float
    port_freed: bool = False
    adopted_process: bool = False
    used_fallback: bool = False
    fallback_port: Optional[int] = None
    error: Optional[str] = None


# =============================================================================
# Intelligent Port Manager
# =============================================================================

class IntelligentPortManager:
    """
    Intelligent, async, parallel port management system.

    Key innovations over previous implementation:
    1. Classifies processes BEFORE attempting cleanup (no wasted time)
    2. Runs cleanup strategies in PARALLEL with first-success wins
    3. Switches to fallback port IMMEDIATELY when process is unkillable
    4. Uses adaptive timeouts based on process classification
    5. Supports instance adoption for existing healthy JARVIS Prime
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        primary_port: int = 8002,
        fallback_port_start: int = 8003,
        fallback_port_end: int = 8020,
        max_cleanup_time_seconds: float = 15.0,  # v78.1: Increased from 10s to 15s
        adopt_existing_instances: bool = True,
        health_probe_timeout: float = 5.0,  # v78.1: Increased from 2s to 5s for slow startup
        health_probe_retries: int = 3,  # v78.1: Added retry count
    ):
        self.host = host
        self.primary_port = primary_port
        self.fallback_port_start = fallback_port_start
        self.fallback_port_end = fallback_port_end
        self.max_cleanup_time = max_cleanup_time_seconds
        self.adopt_existing = adopt_existing_instances
        self.health_probe_timeout = health_probe_timeout
        self.health_probe_retries = health_probe_retries  # v78.1: Store retry count

        # State tracking
        self._current_port = primary_port
        self._cleanup_in_progress = asyncio.Lock()
        self._known_unkillable_pids: Set[int] = set()

    @property
    def effective_port(self) -> int:
        """Get the currently effective port (may be fallback)."""
        return self._current_port

    # =========================================================================
    # Main Entry Point
    # =========================================================================

    async def ensure_port_available(self) -> Tuple[int, Optional[ProcessInfo]]:
        """
        Ensure a port is available for JARVIS Prime, returning the port to use.

        This is the main entry point that orchestrates all cleanup strategies.

        Returns:
            Tuple of (available_port, adopted_process_info or None)

        Raises:
            RuntimeError: If no port can be made available
        """
        async with self._cleanup_in_progress:
            start_time = time.perf_counter()

            # Step 1: Check primary port
            logger.info(f"[PortManager] Checking port {self.primary_port}...")

            pid = await self._get_pid_on_port(self.primary_port)
            if pid is None:
                logger.info(f"[PortManager] Port {self.primary_port} is available")
                self._current_port = self.primary_port
                return (self.primary_port, None)

            # Step 2: Classify the process (FAST - under 500ms)
            logger.info(f"[PortManager] Port {self.primary_port} in use by PID {pid}, classifying...")
            process_info = await self._classify_process(pid, self.primary_port)

            logger.info(
                f"[PortManager] Process classification: {process_info.process_type.value} "
                f"(name={process_info.name}, is_jarvis={process_info.is_jarvis_prime}, "
                f"detection_time={process_info.detection_time_ms:.1f}ms)"
            )

            # Step 3: Handle based on classification
            result = await self._handle_classified_process(process_info)

            elapsed = (time.perf_counter() - start_time) * 1000

            if result.success:
                if result.adopted_process:
                    logger.info(
                        f"[PortManager] ✅ Adopted existing JARVIS Prime on port {self._current_port} "
                        f"(took {elapsed:.0f}ms)"
                    )
                    return (self._current_port, process_info)
                elif result.used_fallback:
                    logger.info(
                        f"[PortManager] ✅ Using fallback port {result.fallback_port} "
                        f"(took {elapsed:.0f}ms)"
                    )
                    self._current_port = result.fallback_port
                    return (result.fallback_port, None)
                else:
                    logger.info(
                        f"[PortManager] ✅ Port {self.primary_port} freed via {result.method} "
                        f"(took {elapsed:.0f}ms)"
                    )
                    self._current_port = self.primary_port
                    return (self.primary_port, None)

            # All cleanup failed - raise error
            raise RuntimeError(
                f"Cannot obtain port for JARVIS Prime after {elapsed:.0f}ms. "
                f"Port {self.primary_port} held by PID {pid} ({process_info.process_type.value}). "
                f"Error: {result.error}"
            )

    # =========================================================================
    # Process Classification (FAST)
    # =========================================================================

    async def _classify_process(self, pid: int, port: int) -> ProcessInfo:
        """
        Classify a process to determine the best cleanup strategy.

        This is designed to be FAST (< 500ms) to avoid wasting time
        before deciding on the right approach.
        """
        start_time = time.perf_counter()

        info = ProcessInfo(
            pid=pid,
            port=port,
            process_type=ProcessType.UNKNOWN,
        )

        # Quick check 1: Is this our own PID?
        current_pid = os.getpid()
        if pid == current_pid:
            info.process_type = ProcessType.OUR_PROCESS
            info.detection_time_ms = (time.perf_counter() - start_time) * 1000
            return info

        # Quick check 2: Is this a known unkillable PID?
        if pid in self._known_unkillable_pids:
            info.process_type = ProcessType.SYSTEM_PROTECTED
            info.detection_time_ms = (time.perf_counter() - start_time) * 1000
            return info

        # Check 3: Low PID heuristic (< 500 often system on macOS)
        if pid < 500:
            logger.debug(f"[PortManager] PID {pid} is low - likely system process")
            # Don't immediately classify as system - verify first

        # Use psutil for detailed classification
        if PSUTIL_AVAILABLE:
            try:
                proc = psutil.Process(pid)

                info.name = proc.name()
                info.status = proc.status()

                try:
                    info.cmdline = " ".join(proc.cmdline())
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    info.cmdline = ""

                try:
                    parent = proc.parent()
                    info.parent_pid = parent.pid if parent else None
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

                # Classification logic
                if info.status == psutil.STATUS_ZOMBIE:
                    info.process_type = ProcessType.ZOMBIE

                elif await self._is_ancestor_or_sibling(pid):
                    info.process_type = ProcessType.RELATED_PROCESS

                elif self._looks_like_jarvis_prime(info.cmdline, info.name):
                    info.is_jarvis_prime = True
                    # Check if it's healthy
                    if await self._probe_jarvis_health(port):
                        info.process_type = ProcessType.HEALTHY_JARVIS
                        info.is_healthy = True
                    else:
                        info.process_type = ProcessType.UNRESPONSIVE

                elif self._looks_like_system_process(info):
                    info.process_type = ProcessType.SYSTEM_PROTECTED
                    self._known_unkillable_pids.add(pid)

                else:
                    info.process_type = ProcessType.KILLABLE

            except psutil.NoSuchProcess:
                # Process already gone
                info.process_type = ProcessType.KILLABLE

            except psutil.AccessDenied:
                info.process_type = ProcessType.PERMISSION_DENIED

            except Exception as e:
                logger.debug(f"[PortManager] psutil classification error: {e}")
                info.error = str(e)

        else:
            # Fallback without psutil - use lsof/ps
            info = await self._classify_via_shell(pid, port, info)

        info.detection_time_ms = (time.perf_counter() - start_time) * 1000
        return info

    def _looks_like_jarvis_prime(self, cmdline: str, name: str) -> bool:
        """Check if process looks like JARVIS Prime."""
        cmdline_lower = cmdline.lower()
        name_lower = name.lower()

        return any([
            "jarvis_prime" in cmdline_lower,
            "jarvis-prime" in cmdline_lower,
            "jarvis.prime" in cmdline_lower,
            "8002" in cmdline_lower and "python" in name_lower,
            "llama" in cmdline_lower and "server" in cmdline_lower,
        ])

    def _looks_like_system_process(self, info: ProcessInfo) -> bool:
        """Check if process looks like a protected system process."""
        name_lower = info.name.lower()

        # macOS system processes
        system_names = {
            "kernel_task", "launchd", "windowserver", "loginwindow",
            "coreaudiod", "coreservicesd", "mds", "mds_stores",
            "mdworker", "hidd", "opendirectoryd", "configd",
            "securityd", "trustd", "apsd", "powerd", "diskmanagementd",
        }

        if name_lower in system_names:
            return True

        # Parent is launchd (PID 1) - might be system daemon
        if info.parent_pid == 1:
            # Additional check: system paths
            if info.cmdline:
                system_paths = [
                    "/System/Library",
                    "/usr/libexec",
                    "/Library/Apple",
                ]
                return any(path in info.cmdline for path in system_paths)

        return False

    async def _classify_via_shell(
        self,
        pid: int,
        port: int,
        info: ProcessInfo
    ) -> ProcessInfo:
        """Fallback classification using shell commands."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "comm=,ppid=,stat=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            if stdout:
                parts = stdout.decode().strip().split()
                if len(parts) >= 1:
                    info.name = parts[0]
                if len(parts) >= 2:
                    try:
                        info.parent_pid = int(parts[1])
                    except ValueError:
                        pass
                if len(parts) >= 3:
                    info.status = parts[2]

                # Check for zombie
                if "Z" in info.status:
                    info.process_type = ProcessType.ZOMBIE
                # Check for JARVIS Prime
                elif self._looks_like_jarvis_prime("", info.name):
                    info.is_jarvis_prime = True
                    if await self._probe_jarvis_health(port):
                        info.process_type = ProcessType.HEALTHY_JARVIS
                    else:
                        info.process_type = ProcessType.UNRESPONSIVE
                else:
                    info.process_type = ProcessType.KILLABLE

        except Exception as e:
            logger.debug(f"[PortManager] Shell classification failed: {e}")
            info.process_type = ProcessType.UNKNOWN

        return info

    async def _is_ancestor_or_sibling(self, pid: int) -> bool:
        """Check if PID is an ancestor or sibling process."""
        if not PSUTIL_AVAILABLE:
            return False

        try:
            current = psutil.Process(os.getpid())
            target = psutil.Process(pid)

            # Check if target is ancestor
            parent = current.parent()
            depth = 0
            while parent and depth < 20:
                if parent.pid == pid:
                    return True
                parent = parent.parent()
                depth += 1

            # Check if target is our child
            for child in current.children(recursive=True):
                if child.pid == pid:
                    return True

            # Check if same parent (sibling)
            if current.parent():
                for sibling in current.parent().children():
                    if sibling.pid == pid:
                        return True

            return False

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    async def _probe_jarvis_health(self, port: int) -> bool:
        """
        Health probe for JARVIS Prime with retry logic.

        v78.1: Added configurable retries with exponential backoff for slow startups.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("[PortManager] aiohttp not available for health probe")
            return False

        url = f"http://{self.host}:{port}/health"
        per_request_timeout = self.health_probe_timeout / max(self.health_probe_retries, 1)

        last_error: Optional[str] = None

        for attempt in range(self.health_probe_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=per_request_timeout)

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            status = data.get("status", "").lower()

                            if status in ("ok", "healthy", "running"):
                                if attempt > 0:
                                    logger.debug(
                                        f"[PortManager] Health probe succeeded on attempt {attempt + 1}/{self.health_probe_retries}"
                                    )
                                return True
                            else:
                                last_error = f"unhealthy status: {status}"
                        else:
                            last_error = f"HTTP {resp.status}"

            except asyncio.TimeoutError:
                last_error = f"timeout after {per_request_timeout:.1f}s"
            except aiohttp.ClientConnectorError as e:
                last_error = f"connection refused: {e}"
            except Exception as e:
                last_error = f"unexpected error: {type(e).__name__}: {e}"

            # Exponential backoff between retries (0.2s, 0.4s, 0.8s...)
            if attempt < self.health_probe_retries - 1:
                backoff = 0.2 * (2 ** attempt)
                logger.debug(
                    f"[PortManager] Health probe attempt {attempt + 1}/{self.health_probe_retries} "
                    f"failed ({last_error}), retrying in {backoff:.1f}s..."
                )
                await asyncio.sleep(backoff)

        logger.debug(
            f"[PortManager] Health probe failed after {self.health_probe_retries} attempts. "
            f"Last error: {last_error}"
        )
        return False

    # =========================================================================
    # Cleanup Strategy Execution (PARALLEL)
    # =========================================================================

    async def _handle_classified_process(self, info: ProcessInfo) -> CleanupResult:
        """
        Handle a classified process with the appropriate strategy.

        Uses PARALLEL execution where possible for maximum speed.
        """
        # Strategy 1: Adopt healthy JARVIS Prime
        if info.should_adopt and self.adopt_existing:
            return CleanupResult(
                success=True,
                method="adoption",
                elapsed_ms=info.detection_time_ms,
                adopted_process=True,
            )

        # Strategy 2: Immediate fallback for unkillable processes
        if info.needs_fallback:
            logger.info(
                f"[PortManager] Process {info.pid} is {info.process_type.value} - "
                f"skipping cleanup, using fallback port"
            )
            return await self._use_fallback_port(info)

        # Strategy 3: Reap zombie processes
        if info.process_type == ProcessType.ZOMBIE:
            return await self._reap_zombie(info)

        # Strategy 4: Parallel cleanup for killable processes
        if info.is_killable:
            return await self._parallel_cleanup(info)

        # Strategy 5: Handle our own process (restart scenario)
        if info.process_type == ProcessType.OUR_PROCESS:
            logger.warning(
                f"[PortManager] Port {info.port} bound by our own process - "
                f"this indicates a restart scenario"
            )
            return await self._use_fallback_port(info)

        # Unknown - try cleanup then fallback
        result = await self._parallel_cleanup(info)
        if not result.success:
            return await self._use_fallback_port(info)
        return result

    async def _parallel_cleanup(self, info: ProcessInfo) -> CleanupResult:
        """
        Execute cleanup strategies in PARALLEL.

        This is the key innovation: instead of sequential SIGINT → SIGTERM → SIGKILL,
        we start multiple strategies concurrently and use the first one that works.

        v78.1: Enhanced with comprehensive diagnostic logging.
        """
        start_time = time.perf_counter()

        # v78.1: Track strategy results for diagnostics
        strategy_results: Dict[str, Optional[CleanupResult]] = {}

        logger.info(
            f"[PortManager] Starting parallel cleanup for PID {info.pid} on port {info.port} "
            f"(max_time={self.max_cleanup_time}s, type={info.process_type.value})"
        )

        # Create tasks for different cleanup strategies
        tasks = [
            asyncio.create_task(
                self._graceful_http_shutdown(info),
                name="http_shutdown"
            ),
            asyncio.create_task(
                self._signal_cascade(info),
                name="signal_cascade"
            ),
        ]

        # Also start port monitoring to detect when port becomes free
        port_free_event = asyncio.Event()
        monitor_task = asyncio.create_task(
            self._monitor_port_release(info.port, port_free_event),
            name="port_monitor"
        )
        tasks.append(monitor_task)

        try:
            # Wait for ANY task to complete successfully or timeout
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.max_cleanup_time,
                return_when=asyncio.FIRST_COMPLETED,
            )

            elapsed_so_far = (time.perf_counter() - start_time) * 1000

            # v78.1: Log which strategies completed vs timed out
            completed_names = [t.get_name() for t in done]
            pending_names = [t.get_name() for t in pending]
            logger.debug(
                f"[PortManager] After {elapsed_so_far:.0f}ms - "
                f"completed: {completed_names}, pending: {pending_names}"
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                strategy_results[task.get_name()] = None  # Timed out

            # Check results
            for task in done:
                task_name = task.get_name()

                if task_name == "port_monitor" and port_free_event.is_set():
                    elapsed = (time.perf_counter() - start_time) * 1000
                    logger.info(
                        f"[PortManager] ✅ Port {info.port} released (detected by monitor) "
                        f"after {elapsed:.0f}ms"
                    )
                    return CleanupResult(
                        success=True,
                        method="port_released",
                        elapsed_ms=elapsed,
                        port_freed=True,
                    )

                try:
                    result = task.result()
                    strategy_results[task_name] = result

                    if isinstance(result, CleanupResult):
                        if result.success:
                            logger.info(
                                f"[PortManager] ✅ Strategy '{task_name}' succeeded "
                                f"in {result.elapsed_ms:.0f}ms"
                            )
                            return result
                        else:
                            logger.debug(
                                f"[PortManager] Strategy '{task_name}' failed "
                                f"after {result.elapsed_ms:.0f}ms: {result.error or 'no error info'}"
                            )
                except asyncio.CancelledError:
                    strategy_results[task_name] = None
                except Exception as e:
                    logger.debug(
                        f"[PortManager] Strategy '{task_name}' raised exception: {e}"
                    )
                    strategy_results[task_name] = None

            # Timeout - check if port is free anyway
            pid = await self._get_pid_on_port(info.port)
            if pid is None:
                elapsed = (time.perf_counter() - start_time) * 1000
                logger.info(
                    f"[PortManager] ✅ Port {info.port} freed (post-timeout check) "
                    f"after {elapsed:.0f}ms"
                )
                return CleanupResult(
                    success=True,
                    method="timeout_but_free",
                    elapsed_ms=elapsed,
                    port_freed=True,
                )

            # v78.1: Detailed failure diagnostics
            elapsed = (time.perf_counter() - start_time) * 1000
            diagnostics = []
            for strategy, result in strategy_results.items():
                if result is None:
                    diagnostics.append(f"{strategy}=TIMEOUT")
                elif result.success:
                    diagnostics.append(f"{strategy}=OK({result.elapsed_ms:.0f}ms)")
                else:
                    diagnostics.append(
                        f"{strategy}=FAILED({result.elapsed_ms:.0f}ms, {result.error or 'unknown'})"
                    )

            logger.warning(
                f"[PortManager] ⚠️ Parallel cleanup failed after {elapsed:.0f}ms. "
                f"PID {info.pid} still holding port {info.port}. "
                f"Strategy results: {', '.join(diagnostics)}. "
                f"Using fallback port..."
            )
            return await self._use_fallback_port(info)

        except Exception as e:
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.error(
                f"[PortManager] ❌ Parallel cleanup error after {elapsed:.0f}ms: "
                f"{type(e).__name__}: {e}"
            )
            return await self._use_fallback_port(info)

        finally:
            # Ensure all tasks are cancelled
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _graceful_http_shutdown(self, info: ProcessInfo) -> CleanupResult:
        """Attempt graceful HTTP shutdown for JARVIS Prime instances."""
        if not info.is_jarvis_prime:
            return CleanupResult(success=False, method="http_shutdown", elapsed_ms=0)

        start_time = time.perf_counter()

        try:
            import aiohttp

            shutdown_url = f"http://{self.host}:{info.port}/admin/shutdown"
            timeout = aiohttp.ClientTimeout(total=3.0)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(shutdown_url) as resp:
                    if resp.status == 200:
                        # Wait for port to free
                        await asyncio.sleep(0.5)
                        pid = await self._get_pid_on_port(info.port)

                        elapsed = (time.perf_counter() - start_time) * 1000

                        if pid is None:
                            return CleanupResult(
                                success=True,
                                method="http_shutdown",
                                elapsed_ms=elapsed,
                                port_freed=True,
                            )

        except Exception:
            pass

        elapsed = (time.perf_counter() - start_time) * 1000
        return CleanupResult(success=False, method="http_shutdown", elapsed_ms=elapsed)

    async def _signal_cascade(self, info: ProcessInfo) -> CleanupResult:
        """
        Send signals with FAST timeouts.

        Unlike the old approach, we use very short timeouts and move on quickly.
        """
        start_time = time.perf_counter()
        pid = info.pid

        signals = [
            (signal.SIGTERM, 2.0, "SIGTERM"),
            (signal.SIGKILL, 1.0, "SIGKILL"),
        ]

        for sig, wait_time, sig_name in signals:
            try:
                os.kill(pid, sig)
                logger.debug(f"[PortManager] Sent {sig_name} to PID {pid}")

                # Wait with early exit on port free
                wait_start = time.perf_counter()
                while (time.perf_counter() - wait_start) < wait_time:
                    await asyncio.sleep(0.2)

                    # Check if port is free
                    current_pid = await self._get_pid_on_port(info.port)
                    if current_pid is None:
                        elapsed = (time.perf_counter() - start_time) * 1000
                        return CleanupResult(
                            success=True,
                            method=sig_name,
                            elapsed_ms=elapsed,
                            port_freed=True,
                        )

                    # Check if process is gone
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        # Process is gone - wait briefly for port release
                        await asyncio.sleep(0.3)
                        current_pid = await self._get_pid_on_port(info.port)

                        elapsed = (time.perf_counter() - start_time) * 1000
                        return CleanupResult(
                            success=current_pid is None,
                            method=sig_name,
                            elapsed_ms=elapsed,
                            port_freed=current_pid is None,
                        )

            except PermissionError:
                logger.warning(f"[PortManager] Permission denied for {sig_name} on PID {pid}")
                self._known_unkillable_pids.add(pid)
                break

            except ProcessLookupError:
                # Process already gone
                elapsed = (time.perf_counter() - start_time) * 1000
                return CleanupResult(
                    success=True,
                    method=f"{sig_name}_already_gone",
                    elapsed_ms=elapsed,
                    port_freed=True,
                )

        elapsed = (time.perf_counter() - start_time) * 1000
        return CleanupResult(success=False, method="signal_cascade", elapsed_ms=elapsed)

    async def _reap_zombie(self, info: ProcessInfo) -> CleanupResult:
        """Reap a zombie process."""
        start_time = time.perf_counter()

        try:
            os.waitpid(info.pid, os.WNOHANG)
            await asyncio.sleep(0.3)

            pid = await self._get_pid_on_port(info.port)
            elapsed = (time.perf_counter() - start_time) * 1000

            if pid is None:
                return CleanupResult(
                    success=True,
                    method="zombie_reap",
                    elapsed_ms=elapsed,
                    port_freed=True,
                )

        except (OSError, ChildProcessError) as e:
            logger.debug(f"[PortManager] Zombie reap failed: {e}")

        # Zombie couldn't be reaped - use fallback
        return await self._use_fallback_port(info)

    async def _monitor_port_release(self, port: int, event: asyncio.Event) -> None:
        """Monitor port for release (runs in background)."""
        while True:
            await asyncio.sleep(0.2)
            pid = await self._get_pid_on_port(port)
            if pid is None:
                event.set()
                return

    # =========================================================================
    # Fallback Port Selection
    # =========================================================================

    async def _use_fallback_port(self, info: ProcessInfo) -> CleanupResult:
        """Find and use a fallback port."""
        start_time = time.perf_counter()

        logger.info(
            f"[PortManager] Searching for fallback port in range "
            f"{self.fallback_port_start}-{self.fallback_port_end}..."
        )

        for port in range(self.fallback_port_start, self.fallback_port_end + 1):
            # Check if port is in use
            pid = await self._get_pid_on_port(port)
            if pid is not None:
                continue

            # Verify with socket bind
            if await self._verify_port_available(port):
                elapsed = (time.perf_counter() - start_time) * 1000

                logger.info(f"[PortManager] ✅ Found available fallback port: {port}")

                return CleanupResult(
                    success=True,
                    method="fallback_port",
                    elapsed_ms=elapsed,
                    used_fallback=True,
                    fallback_port=port,
                )

        elapsed = (time.perf_counter() - start_time) * 1000

        return CleanupResult(
            success=False,
            method="fallback_port",
            elapsed_ms=elapsed,
            error=f"No available ports in range {self.fallback_port_start}-{self.fallback_port_end}",
        )

    async def _verify_port_available(self, port: int) -> bool:
        """Verify port is available by attempting to bind."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(1.0)
            sock.bind((self.host, port))
            sock.close()
            return True
        except OSError:
            return False

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def _get_pid_on_port(self, port: int) -> Optional[int]:
        """Get PID of process using a port."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-t", "-i", f":{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            if stdout:
                pids = stdout.decode().strip().split("\n")
                if pids and pids[0]:
                    return int(pids[0])

            return None

        except asyncio.TimeoutError:
            return None
        except Exception:
            return None


# =============================================================================
# Module-level convenience functions
# =============================================================================

_manager_instance: Optional[IntelligentPortManager] = None


def get_intelligent_port_manager(**kwargs) -> IntelligentPortManager:
    """Get or create the global port manager instance."""
    global _manager_instance

    if _manager_instance is None:
        _manager_instance = IntelligentPortManager(**kwargs)

    return _manager_instance


async def ensure_port_available(port: int = 8002, **kwargs) -> Tuple[int, Optional[ProcessInfo]]:
    """Convenience function to ensure a port is available."""
    manager = get_intelligent_port_manager(primary_port=port, **kwargs)
    return await manager.ensure_port_available()
