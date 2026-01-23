"""
Enterprise Process Manager v108.0 - Robust Process Lifecycle Management
=========================================================================

This module provides enterprise-grade process management that fixes critical
issues in the Trinity orchestration system:

1. PORT CONFLICT RESOLUTION: Validates process health, not just port occupancy
2. PROCESS OUTPUT CAPTURE: Streams stderr during startup for error visibility
3. INTELLIGENT HEALTH VALIDATION: Separates "process alive" from "service healthy"
4. GRACEFUL DEGRADATION: Components can run in degraded mode without killing startup

Key Fixes:
- Port validation now checks BOTH occupancy AND health
- Stderr is captured and logged during startup (errors no longer silent)
- Process death detected immediately via poll(), not timeout
- Socket state (LISTEN vs TIME_WAIT) properly handled
- PID validation ensures we're talking to the right process

Author: JARVIS Development Team
Version: 108.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class ProcessState(str, Enum):
    """Process lifecycle states."""
    NOT_STARTED = "not_started"
    STARTING = "starting"
    RUNNING = "running"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    CRASHED = "crashed"


@dataclass
class PortValidationResult:
    """
    Result of comprehensive port validation.

    Goes beyond simple "is port in use" to answer:
    - Is the port occupied?
    - Is the process on that port healthy?
    - Is it the expected process (PID match)?
    - Is the socket in LISTEN state (vs TIME_WAIT)?
    """
    port: int
    is_occupied: bool = False
    is_healthy: bool = False
    is_expected_process: bool = False
    socket_state: str = "unknown"  # LISTEN, TIME_WAIT, CLOSE_WAIT, etc.
    pid: Optional[int] = None
    process_name: Optional[str] = None
    error: Optional[str] = None
    recommendation: str = "proceed"  # "proceed", "wait", "kill_and_retry", "skip"

    @property
    def can_proceed(self) -> bool:
        """Can we proceed with startup on this port?"""
        # Not occupied - proceed
        if not self.is_occupied:
            return True
        # Occupied by expected healthy process - skip startup (already running)
        if self.is_expected_process and self.is_healthy:
            return False  # Service already running, skip
        # Occupied but unhealthy - need cleanup
        return False

    @property
    def needs_cleanup(self) -> bool:
        """Does the port need cleanup before we can use it?"""
        if not self.is_occupied:
            return False
        # TIME_WAIT - wait for OS to release
        if self.socket_state == "TIME_WAIT":
            return True
        # Occupied by stale/unhealthy process - kill it
        if self.is_occupied and not self.is_healthy:
            return True
        return False


@dataclass
class ProcessStartupResult:
    """Result of process startup attempt."""
    success: bool = False
    pid: Optional[int] = None
    port: Optional[int] = None
    state: ProcessState = ProcessState.NOT_STARTED
    startup_duration: float = 0.0
    stderr_output: str = ""
    stdout_output: str = ""
    error: Optional[str] = None
    exit_code: Optional[int] = None


class EnterpriseProcessManager:
    """
    Enterprise-grade process manager with comprehensive validation.

    Key improvements over basic subprocess management:
    1. Port validation with health checks (not just occupancy)
    2. Real-time stderr capture during startup
    3. Socket state awareness (TIME_WAIT handling)
    4. PID-based process validation
    5. Graceful cleanup with SIGTERM -> SIGKILL escalation
    """

    def __init__(
        self,
        health_check_timeout: float = 10.0,
        port_validation_timeout: float = 5.0,
        stderr_capture_timeout: float = 5.0,
        cleanup_wait_time: float = 2.0,
    ):
        self.health_check_timeout = health_check_timeout
        self.port_validation_timeout = port_validation_timeout
        self.stderr_capture_timeout = stderr_capture_timeout
        self.cleanup_wait_time = cleanup_wait_time

        # HTTP session for health checks (separate from main orchestrator)
        self._http_session: Optional[aiohttp.ClientSession] = None

        # Track managed processes
        self._managed_processes: Dict[str, asyncio.subprocess.Process] = {}
        self._startup_times: Dict[str, float] = {}

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session for health checks."""
        if self._http_session is None or self._http_session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,
                limit_per_host=5,
                keepalive_timeout=30.0,
                enable_cleanup_closed=True,
            )
            self._http_session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.health_check_timeout),
            )
        return self._http_session

    async def close(self) -> None:
        """Clean up resources."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    # =========================================================================
    # Port Validation (Fixed: Checks Health, Not Just Occupancy)
    # =========================================================================

    async def validate_port(
        self,
        port: int,
        expected_service: Optional[str] = None,
        health_endpoint: str = "/health",
    ) -> PortValidationResult:
        """
        Comprehensive port validation.

        This fixes the critical issue where port occupancy was assumed to mean
        the service is healthy. Now we check:
        1. Is the port occupied? (socket check)
        2. What's the socket state? (LISTEN vs TIME_WAIT)
        3. What PID owns the port? (lsof)
        4. Is that process healthy? (HTTP health check)

        Returns:
            PortValidationResult with full details for decision making
        """
        result = PortValidationResult(port=port)

        # Step 1: Check socket occupancy and state
        socket_info = await self._check_socket_state(port)
        result.is_occupied = socket_info.get("occupied", False)
        result.socket_state = socket_info.get("state", "unknown")

        if not result.is_occupied:
            result.recommendation = "proceed"
            return result

        # Step 2: Get PID of process on port
        pid_info = await self._get_port_pid(port)
        result.pid = pid_info.get("pid")
        result.process_name = pid_info.get("name")

        if result.socket_state == "TIME_WAIT":
            # Port in TIME_WAIT - need to wait for OS to release
            result.recommendation = "wait"
            result.error = f"Port {port} in TIME_WAIT state, will be released shortly"
            logger.warning(f"[PortValidation] Port {port} in TIME_WAIT - recommending wait")
            return result

        # Step 3: Check if process is responding to health checks
        if result.pid:
            health_status = await self._check_port_health(port, health_endpoint)
            result.is_healthy = health_status.get("healthy", False)

            # Check if this is the expected service
            if expected_service and health_status.get("service_name"):
                result.is_expected_process = (
                    health_status.get("service_name", "").lower() == expected_service.lower()
                )

        # Step 4: Determine recommendation
        if result.is_healthy:
            if result.is_expected_process:
                result.recommendation = "skip"  # Already running healthy
                logger.info(
                    f"[PortValidation] Port {port}: {expected_service} already running healthy"
                )
            else:
                result.recommendation = "kill_and_retry"
                result.error = f"Port {port} occupied by different service"
                logger.warning(
                    f"[PortValidation] Port {port} occupied by {result.process_name}, "
                    f"not {expected_service}"
                )
        else:
            result.recommendation = "kill_and_retry"
            result.error = f"Port {port} occupied but process unhealthy (PID: {result.pid})"
            logger.warning(
                f"[PortValidation] Port {port}: process {result.pid} unhealthy - will clean up"
            )

        return result

    async def _check_socket_state(self, port: int) -> Dict[str, Any]:
        """
        Check socket state for a port.

        Returns dict with:
        - occupied: bool
        - state: LISTEN, TIME_WAIT, CLOSE_WAIT, etc.
        """
        try:
            # Try to bind to check if available
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.bind(("0.0.0.0", port))
                sock.close()
                return {"occupied": False, "state": "available"}
            except socket.error:
                sock.close()

            # Port occupied - check state with netstat/lsof
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-P", "-n",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            output = stdout.decode() if stdout else ""

            # Parse state from lsof output
            state = "LISTEN"
            if "TIME_WAIT" in output:
                state = "TIME_WAIT"
            elif "CLOSE_WAIT" in output:
                state = "CLOSE_WAIT"
            elif "ESTABLISHED" in output:
                state = "ESTABLISHED"
            elif "(LISTEN)" in output:
                state = "LISTEN"

            return {"occupied": True, "state": state}

        except Exception as e:
            logger.debug(f"[PortValidation] Socket state check error: {e}")
            return {"occupied": True, "state": "unknown"}

    async def _get_port_pid(self, port: int) -> Dict[str, Any]:
        """Get PID and process name for a port."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-ti", f":{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if not stdout:
                return {"pid": None, "name": None}

            pids = stdout.decode().strip().split('\n')
            if pids and pids[0]:
                pid = int(pids[0])

                # Get process name
                try:
                    name_proc = await asyncio.create_subprocess_exec(
                        "ps", "-p", str(pid), "-o", "comm=",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    name_stdout, _ = await asyncio.wait_for(name_proc.communicate(), timeout=2.0)
                    name = name_stdout.decode().strip() if name_stdout else None
                except Exception:
                    name = None

                return {"pid": pid, "name": name}

        except Exception as e:
            logger.debug(f"[PortValidation] Get PID error: {e}")

        return {"pid": None, "name": None}

    async def _check_port_health(self, port: int, health_endpoint: str) -> Dict[str, Any]:
        """
        Check if service on port is healthy via HTTP.

        Returns dict with:
        - healthy: bool
        - service_name: str (if available from health response)
        - status: str (health status)
        """
        try:
            session = await self._get_http_session()
            url = f"http://localhost:{port}{health_endpoint}"

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.health_check_timeout)
            ) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        return {
                            "healthy": data.get("status") == "healthy" or data.get("healthy", False),
                            "service_name": data.get("service", data.get("name", "")),
                            "status": data.get("status", "unknown"),
                        }
                    except Exception:
                        # Non-JSON 200 response - still consider healthy
                        return {"healthy": True, "service_name": None, "status": "ok"}
                else:
                    return {"healthy": False, "service_name": None, "status": f"http_{response.status}"}

        except asyncio.TimeoutError:
            return {"healthy": False, "service_name": None, "status": "timeout"}
        except Exception as e:
            return {"healthy": False, "service_name": None, "status": f"error: {str(e)[:50]}"}

    # =========================================================================
    # Process Cleanup (Enhanced with Socket State Awareness)
    # =========================================================================

    async def cleanup_port(
        self,
        port: int,
        force: bool = False,
        wait_for_time_wait: bool = True,
        max_wait: float = 30.0,
    ) -> bool:
        """
        Clean up a port for reuse.

        Handles:
        1. LISTEN state: Kill process with SIGTERM, escalate to SIGKILL
        2. TIME_WAIT state: Wait for OS to release (up to max_wait)
        3. Process not responding: Force kill

        Args:
            port: Port to clean up
            force: If True, use SIGKILL immediately
            wait_for_time_wait: If True, wait for TIME_WAIT to clear
            max_wait: Maximum time to wait for cleanup

        Returns:
            True if port is now available
        """
        start_time = time.time()
        validation = await self.validate_port(port)

        if not validation.is_occupied:
            return True

        if validation.socket_state == "TIME_WAIT":
            if not wait_for_time_wait:
                logger.warning(f"[Cleanup] Port {port} in TIME_WAIT, not waiting")
                return False

            logger.info(f"[Cleanup] Port {port} in TIME_WAIT, waiting for release...")
            while time.time() - start_time < max_wait:
                await asyncio.sleep(1.0)
                check = await self._check_socket_state(port)
                if not check.get("occupied", True):
                    logger.info(f"[Cleanup] Port {port} released from TIME_WAIT")
                    return True
            logger.warning(f"[Cleanup] Port {port} TIME_WAIT timeout after {max_wait}s")
            return False

        # Kill the process
        if validation.pid:
            success = await self._kill_process(
                validation.pid,
                force=force,
                wait_timeout=self.cleanup_wait_time
            )
            if success:
                # Verify port is now available
                await asyncio.sleep(0.5)
                check = await self._check_socket_state(port)
                if not check.get("occupied", True):
                    logger.info(f"[Cleanup] Port {port} successfully cleaned up (killed PID {validation.pid})")
                    return True

                # May be in TIME_WAIT now
                if check.get("state") == "TIME_WAIT" and wait_for_time_wait:
                    return await self.cleanup_port(
                        port,
                        force=force,
                        wait_for_time_wait=True,
                        max_wait=max_wait - (time.time() - start_time)
                    )

        return False

    async def _kill_process(
        self,
        pid: int,
        force: bool = False,
        wait_timeout: float = 5.0,
    ) -> bool:
        """
        Kill a process gracefully.

        First tries SIGTERM, then SIGKILL if process doesn't exit.
        """
        # Safety: Never kill current process or parent
        current_pid = os.getpid()
        parent_pid = os.getppid()
        if pid in (current_pid, parent_pid):
            logger.error(f"[Kill] Refusing to kill self ({current_pid}) or parent ({parent_pid})")
            return False

        try:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.kill(pid, sig)
            logger.info(f"[Kill] Sent {sig.name} to PID {pid}")

            # Wait for process to exit
            start = time.time()
            while time.time() - start < wait_timeout:
                try:
                    os.kill(pid, 0)  # Check if process exists
                    await asyncio.sleep(0.1)
                except ProcessLookupError:
                    # Process exited
                    return True

            if not force:
                # Escalate to SIGKILL
                logger.warning(f"[Kill] PID {pid} didn't exit, escalating to SIGKILL")
                return await self._kill_process(pid, force=True, wait_timeout=wait_timeout)

            return False

        except ProcessLookupError:
            # Process already dead
            return True
        except PermissionError:
            logger.error(f"[Kill] Permission denied for PID {pid}")
            return False
        except Exception as e:
            logger.error(f"[Kill] Error killing PID {pid}: {e}")
            return False

    # =========================================================================
    # Process Startup (Enhanced with stderr capture)
    # =========================================================================

    async def start_process(
        self,
        name: str,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        port: int = 0,
        health_endpoint: str = "/health",
        startup_timeout: float = 60.0,
        capture_output: bool = True,
    ) -> ProcessStartupResult:
        """
        Start a process with comprehensive monitoring.

        Key improvements:
        1. Captures stderr during startup (errors are visible)
        2. Polls process state continuously (immediate crash detection)
        3. Streams output in real-time
        4. Returns detailed result with stderr for debugging

        Args:
            name: Service name
            command: Command to execute
            cwd: Working directory
            env: Environment variables
            port: Expected port (for health checks)
            health_endpoint: Health check endpoint
            startup_timeout: Max startup time
            capture_output: Whether to capture stdout/stderr

        Returns:
            ProcessStartupResult with all details
        """
        result = ProcessStartupResult(state=ProcessState.STARTING)
        start_time = time.time()

        logger.info(f"[ProcessManager] Starting {name}: {' '.join(command)}")

        try:
            # Merge environment
            full_env = os.environ.copy()
            if env:
                full_env.update(env)

            # Start process with output capture
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd) if cwd else None,
                stdout=asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,  # ALWAYS capture stderr
                env=full_env,
            )

            result.pid = process.pid
            result.port = port
            result.state = ProcessState.RUNNING

            self._managed_processes[name] = process
            self._startup_times[name] = start_time

            logger.info(f"[ProcessManager] {name} started with PID {result.pid}")

            # Start stderr reader task
            stderr_lines: List[str] = []
            stderr_task = asyncio.create_task(
                self._read_stderr_during_startup(process, name, stderr_lines)
            )

            # Wait for health or failure
            check_interval = 1.0

            while time.time() - start_time < startup_timeout:
                # Check if process died
                if process.returncode is not None:
                    result.exit_code = process.returncode
                    result.state = ProcessState.CRASHED
                    result.error = f"Process died with exit code {result.exit_code}"

                    # Get remaining stderr
                    stderr_task.cancel()
                    try:
                        await stderr_task
                    except asyncio.CancelledError:
                        pass

                    result.stderr_output = "\n".join(stderr_lines)

                    logger.error(
                        f"[ProcessManager] {name} crashed during startup:\n"
                        f"  Exit code: {result.exit_code}\n"
                        f"  Stderr: {result.stderr_output[:500]}"
                    )
                    return result

                # Check health if port specified
                if port > 0:
                    try:
                        session = await self._get_http_session()
                        url = f"http://localhost:{port}{health_endpoint}"
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(total=5.0)
                        ) as response:
                            if response.status == 200:
                                try:
                                    data = await response.json()
                                    status = data.get("status", "unknown")
                                    if status == "healthy":
                                        result.success = True
                                        result.state = ProcessState.HEALTHY
                                        result.startup_duration = time.time() - start_time
                                        stderr_task.cancel()
                                        logger.info(
                                            f"[ProcessManager] {name} healthy after "
                                            f"{result.startup_duration:.1f}s"
                                        )
                                        return result
                                    elif status in ("starting", "initializing"):
                                        # Still starting - continue waiting
                                        pass
                                except Exception:
                                    pass  # Non-JSON response, keep waiting
                    except Exception:
                        pass  # Health check failed, keep waiting

                await asyncio.sleep(check_interval)
                check_interval = min(check_interval * 1.1, 5.0)

            # Timeout
            result.state = ProcessState.DEGRADED
            result.error = f"Startup timeout after {startup_timeout}s"
            result.startup_duration = time.time() - start_time
            result.success = False  # Not healthy, but process is running

            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            result.stderr_output = "\n".join(stderr_lines)

            logger.warning(
                f"[ProcessManager] {name} startup timeout. Process running but not healthy.\n"
                f"  Recent stderr: {result.stderr_output[-500:] if result.stderr_output else 'none'}"
            )
            return result

        except Exception as e:
            result.state = ProcessState.FAILED
            result.error = str(e)
            logger.error(f"[ProcessManager] Failed to start {name}: {e}")
            return result

    async def _read_stderr_during_startup(
        self,
        process: asyncio.subprocess.Process,
        name: str,
        lines_buffer: List[str],
        max_lines: int = 100,
    ) -> None:
        """
        Read stderr during startup and log errors.

        This fixes the critical issue where startup errors were silently lost.
        """
        if not process.stderr:
            return

        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break

                decoded = line.decode().rstrip()
                if decoded:
                    lines_buffer.append(decoded)
                    if len(lines_buffer) > max_lines:
                        lines_buffer.pop(0)

                    # Log errors/warnings immediately
                    lower = decoded.lower()
                    if "error" in lower or "exception" in lower or "traceback" in lower:
                        logger.error(f"[{name}:stderr] {decoded}")
                    elif "warning" in lower:
                        logger.warning(f"[{name}:stderr] {decoded}")
                    else:
                        logger.debug(f"[{name}:stderr] {decoded}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[{name}] stderr reader error: {e}")

    # =========================================================================
    # Process Monitoring
    # =========================================================================

    def is_process_running(self, name: str) -> bool:
        """Check if a managed process is still running."""
        process = self._managed_processes.get(name)
        if not process:
            return False
        return process.returncode is None

    def get_process_exit_code(self, name: str) -> Optional[int]:
        """Get exit code if process has exited."""
        process = self._managed_processes.get(name)
        if process:
            return process.returncode
        return None

    async def stop_process(
        self,
        name: str,
        graceful_timeout: float = 10.0,
    ) -> bool:
        """Stop a managed process gracefully."""
        process = self._managed_processes.get(name)
        if not process:
            return True

        if process.returncode is not None:
            # Already stopped
            return True

        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=graceful_timeout)
                logger.info(f"[ProcessManager] {name} stopped gracefully")
                return True
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning(f"[ProcessManager] {name} killed after timeout")
                return True
        except Exception as e:
            logger.error(f"[ProcessManager] Error stopping {name}: {e}")
            return False


# =============================================================================
# Global Instance
# =============================================================================

_global_process_manager: Optional[EnterpriseProcessManager] = None


def get_process_manager() -> EnterpriseProcessManager:
    """Get the global process manager instance."""
    global _global_process_manager
    if _global_process_manager is None:
        _global_process_manager = EnterpriseProcessManager()
    return _global_process_manager
