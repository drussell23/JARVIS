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

from backend.core.portable_sockets import allow_time_wait_rebind
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
        v112.0: CRITICAL FIX - Intelligent port validation with blocking detection.

        Previous bugs fixed:
        1. ESTABLISHED/CLOSED client connections incorrectly flagged as blocking
        2. TIME_WAIT connections blocked when SO_REUSEADDR would allow binding
        3. No distinction between "occupied" and "actually blocking"

        This now properly handles:
        1. Is the port occupied? (socket check with SO_REUSEADDR)
        2. Does it ACTUALLY block binding? (LISTEN vs client connections)
        3. What's the socket state? (LISTEN vs TIME_WAIT vs ESTABLISHED vs CLOSED)
        4. What PID owns the port? (lsof with server socket detection)
        5. Is that process healthy? (HTTP health check)

        Returns:
            PortValidationResult with full details for decision making
        """
        result = PortValidationResult(port=port)

        # Step 1: Check socket state with intelligent blocking detection
        socket_info = await self._check_socket_state(port)
        result.is_occupied = socket_info.get("occupied", False)
        result.socket_state = socket_info.get("state", "unknown")
        is_blocking = socket_info.get("blocking", True)  # Default to True for safety
        details = socket_info.get("details", "")

        # v112.0: CRITICAL - Only consider port "occupied" if it ACTUALLY blocks binding
        if not result.is_occupied or not is_blocking:
            if result.is_occupied and not is_blocking:
                # Port has connections but they don't block binding
                logger.info(
                    f"[v112.0] Port {port}: {result.socket_state} connections exist but "
                    f"don't block binding (non-blocking state). Proceeding. Details: {details}"
                )
                result.is_occupied = False  # Override - not truly blocking
            result.recommendation = "proceed"
            return result

        # Port is ACTUALLY blocking (LISTEN socket or unknown state)
        logger.debug(
            f"[v112.0] Port {port}: blocking state detected ({result.socket_state}). "
            f"Details: {details}"
        )

        # Step 2: Get PID of process on port (server socket only)
        pid_info = await self._get_port_pid(port)
        result.pid = pid_info.get("pid")
        result.process_name = pid_info.get("name")

        # v112.0: TIME_WAIT handling - should have been handled by SO_REUSEADDR
        # If we're here, it means bind still failed even with SO_REUSEADDR
        if result.socket_state == "TIME_WAIT":
            # This is unusual - SO_REUSEADDR should allow binding
            # Wait briefly and recommend retry
            result.recommendation = "wait"
            result.error = (
                f"Port {port} in TIME_WAIT state even with SO_REUSEADDR. "
                f"Unusual - may be kernel-level issue. Wait for natural release (~60s)."
            )
            logger.warning(f"[v112.0] Port {port}: TIME_WAIT persists with SO_REUSEADDR")
            return result

        # v112.0: CLOSE_WAIT means the app hasn't closed the socket
        if result.socket_state == "CLOSE_WAIT":
            result.recommendation = "kill_and_retry"
            result.error = (
                f"Port {port} has CLOSE_WAIT sockets - application hasn't closed connection. "
                f"PID {result.pid} ({result.process_name}) may be stuck."
            )
            logger.warning(f"[v112.0] Port {port}: CLOSE_WAIT - process may be stuck")
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
                    f"[v112.0] Port {port}: {expected_service} already running healthy"
                )
            else:
                result.recommendation = "kill_and_retry"
                result.error = f"Port {port} occupied by different service ({result.process_name})"
                logger.warning(
                    f"[v112.0] Port {port} occupied by {result.process_name}, "
                    f"not {expected_service} - need cleanup"
                )
        else:
            result.recommendation = "kill_and_retry"
            result.error = f"Port {port} has LISTEN socket but process unhealthy (PID: {result.pid})"
            logger.warning(
                f"[v112.0] Port {port}: server PID {result.pid} unhealthy - will clean up"
            )

        return result

    async def _check_socket_state(self, port: int) -> Dict[str, Any]:
        """
        v112.0: CRITICAL FIX - Intelligent socket state detection.

        Previous bugs fixed:
        1. Missing SO_REUSEADDR - caused bind to fail on TIME_WAIT client connections
        2. CLOSED state not recognized - defaulted to LISTEN incorrectly
        3. Client connections flagged as blocking - lsof shows client->server but that
           doesn't block server binding

        This now properly distinguishes:
        - Server LISTEN sockets (ACTUALLY blocks binding)
        - Client connections (don't block server binding on same port)
        - TIME_WAIT/CLOSE_WAIT (may block without SO_REUSEADDR)

        Returns dict with:
        - occupied: bool (True only if LISTEN socket exists)
        - state: LISTEN, TIME_WAIT, CLOSE_WAIT, ESTABLISHED, CLOSED, etc.
        - blocking: bool (True if state actually blocks server binding)
        - details: Additional context for debugging
        """
        result = {
            "occupied": False,
            "state": "available",
            "blocking": False,
            "details": "",
        }

        try:
            # v112.0: CRITICAL - Use SO_REUSEADDR for accurate bind test
            # Without this, TIME_WAIT/CLOSED client sockets incorrectly block bind
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            allow_time_wait_rebind(sock)
            sock.settimeout(1.0)
            try:
                sock.bind(("0.0.0.0", port))
                sock.close()
                return result  # Port available
            except socket.error as bind_err:
                sock.close()
                result["details"] = str(bind_err)

            # Bind failed - analyze WHY using lsof
            # v112.0: Parse lsof output to find ONLY server LISTEN sockets
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-P", "-n",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            output = stdout.decode() if stdout else ""

            if not output.strip():
                # No lsof output but bind failed - likely OS-level issue
                result["occupied"] = True
                result["state"] = "unknown_bind_failure"
                result["blocking"] = True
                return result

            # v112.0: CRITICAL - Parse lsof to find server LISTEN vs client connections
            # Format: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
            # Example server: Python 1234 user 5u IPv4 0x123 0t0 TCP *:8000 (LISTEN)
            # Example client: Python 1234 user 6u IPv4 0x456 0t0 TCP 127.0.0.1:54321->127.0.0.1:8000 (ESTABLISHED)

            has_listen = False
            has_time_wait = False
            has_close_wait = False
            has_established = False
            has_closed = False
            server_pids = []  # PIDs with LISTEN sockets

            for line in output.strip().split('\n')[1:]:  # Skip header
                if not line.strip():
                    continue

                # Check if this is a SERVER socket (listening on this port)
                # Server sockets show as *:PORT or 0.0.0.0:PORT or IP:PORT (LISTEN)
                is_server_socket = False
                parts = line.split()
                if len(parts) >= 9:
                    name_col = parts[-1] if parts[-1].startswith('(') else parts[-2] + ' ' + parts[-1]
                    node_col = ' '.join(parts[8:])

                    # v112.0: Check if this is a LISTEN socket on OUR port
                    if '(LISTEN)' in name_col:
                        # Verify it's listening on this port, not connecting TO this port
                        # Server: *:8000 (LISTEN) or 0.0.0.0:8000 (LISTEN)
                        # NOT: 127.0.0.1:54321->127.0.0.1:8000 (which is client)
                        if f":{port}" in node_col and '->' not in node_col:
                            has_listen = True
                            is_server_socket = True
                            try:
                                server_pids.append(int(parts[1]))
                            except (ValueError, IndexError):
                                pass

                # Parse state for all connections
                if '(LISTEN)' in line:
                    if is_server_socket:
                        has_listen = True
                elif '(TIME_WAIT)' in line:
                    has_time_wait = True
                elif '(CLOSE_WAIT)' in line:
                    has_close_wait = True
                elif '(ESTABLISHED)' in line:
                    has_established = True
                elif '(CLOSED)' in line:
                    has_closed = True

            # v112.0: Determine the ACTUAL state and whether it blocks binding
            if has_listen:
                result["occupied"] = True
                result["state"] = "LISTEN"
                result["blocking"] = True  # LISTEN always blocks
                result["details"] = f"Server LISTEN socket on port {port}, PIDs: {server_pids}"
            elif has_time_wait:
                # TIME_WAIT doesn't block with SO_REUSEADDR, but bind failed
                # This means something else is wrong
                result["occupied"] = True
                result["state"] = "TIME_WAIT"
                result["blocking"] = False  # Shouldn't block with SO_REUSEADDR
                result["details"] = "TIME_WAIT sockets present (should clear in ~60s)"
            elif has_close_wait:
                result["occupied"] = True
                result["state"] = "CLOSE_WAIT"
                result["blocking"] = False  # CLOSE_WAIT is waiting for app to close
                result["details"] = "CLOSE_WAIT - application hasn't closed connection"
            elif has_established:
                # ESTABLISHED connections don't block new LISTEN sockets
                result["occupied"] = False  # v112.0: CRITICAL - not blocking!
                result["state"] = "ESTABLISHED"
                result["blocking"] = False
                result["details"] = "Only client ESTABLISHED connections (non-blocking)"
            elif has_closed:
                # CLOSED connections definitely don't block
                result["occupied"] = False  # v112.0: CRITICAL - not blocking!
                result["state"] = "CLOSED"
                result["blocking"] = False
                result["details"] = "Only CLOSED connections (non-blocking)"
            else:
                # Unknown state but bind failed
                result["occupied"] = True
                result["state"] = "unknown"
                result["blocking"] = True
                result["details"] = f"Unknown state, bind failed, lsof: {output[:200]}"

            logger.debug(
                f"[v112.0] Socket state for port {port}: "
                f"occupied={result['occupied']}, state={result['state']}, "
                f"blocking={result['blocking']}, details={result['details'][:100]}"
            )

            return result

        except asyncio.TimeoutError:
            logger.warning(f"[v112.0] Socket state check timeout for port {port}")
            return {"occupied": True, "state": "timeout", "blocking": True, "details": "lsof timeout"}
        except Exception as e:
            logger.debug(f"[PortValidation] Socket state check error: {e}")
            return {"occupied": True, "state": "error", "blocking": True, "details": str(e)}

    async def _get_port_pid(self, port: int) -> Dict[str, Any]:
        """
        Get PID and process name for a port.

        v109.0: CRITICAL FIX - Only get PIDs in LISTEN state.
        v109.8: ENHANCED - Multi-layer fallback for comprehensive PID detection.

        Detection layers:
        1. lsof -sTCP:LISTEN (primary - safe, only listeners)
        2. lsof without LISTEN filter (fallback for CLOSE_WAIT, FIN_WAIT, etc.)
        3. psutil.net_connections (final fallback - catches orphaned sockets)

        This prevents the supervisor from detecting itself as a port conflict
        while still catching zombie processes in non-LISTEN states.
        """
        current_pid = os.getpid()
        parent_pid = os.getppid()

        # Layer 1: Try LISTEN filter first (safest, avoids self-detection)
        pid, name = await self._get_port_pid_lsof(port, listen_only=True)
        if pid and pid not in (current_pid, parent_pid):
            logger.debug(f"[v109.8] Port {port}: Found LISTEN PID {pid} ({name})")
            return {"pid": pid, "name": name}

        # Layer 2: Fallback - Check ALL socket states (CLOSE_WAIT, FIN_WAIT, etc.)
        # This catches zombie connections that aren't in LISTEN state
        pid, name = await self._get_port_pid_lsof(port, listen_only=False)
        if pid and pid not in (current_pid, parent_pid):
            logger.info(
                f"[v109.8] Port {port}: Found non-LISTEN PID {pid} ({name}) via lsof fallback"
            )
            return {"pid": pid, "name": name}

        # Layer 3: Final fallback - Use psutil for comprehensive detection
        pid, name = await self._get_port_pid_psutil(port)
        if pid and pid not in (current_pid, parent_pid):
            logger.info(
                f"[v109.8] Port {port}: Found PID {pid} ({name}) via psutil fallback"
            )
            return {"pid": pid, "name": name}

        logger.debug(f"[v109.8] Port {port}: No owning PID found via any method")
        return {"pid": None, "name": None}

    async def _get_port_pid_lsof(
        self, port: int, listen_only: bool = True
    ) -> tuple[Optional[int], Optional[str]]:
        """
        v109.8: Get PID using lsof with configurable LISTEN filter.

        Args:
            port: Port number to check
            listen_only: If True, only return LISTEN PIDs (safer). If False, return any.

        Returns:
            Tuple of (pid, process_name) or (None, None)
        """
        try:
            # Build command based on filter mode
            if listen_only:
                cmd = ["lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t"]
            else:
                # Get all TCP states but prioritize server-side states
                cmd = ["lsof", "-i", f":{port}", "-t"]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if not stdout:
                return None, None

            pids = stdout.decode().strip().split('\n')
            current_pid = os.getpid()
            parent_pid = os.getppid()

            for pid_str in pids:
                if not pid_str.strip():
                    continue
                try:
                    pid = int(pid_str.strip())
                except ValueError:
                    continue

                # Skip self and parent
                if pid in (current_pid, parent_pid):
                    continue

                # Get process name
                name = await self._get_process_name(pid)
                return pid, name

            return None, None

        except Exception as e:
            logger.debug(f"[v109.8] lsof error for port {port}: {e}")
            return None, None

    async def _get_port_pid_psutil(self, port: int) -> tuple[Optional[int], Optional[str]]:
        """
        v109.8: Get PID using psutil.net_connections as final fallback.

        This catches cases where lsof fails but psutil can still see the socket.
        Particularly useful for:
        - Orphaned sockets from crashed processes
        - UDP ports (lsof TCP filters miss these)
        - Race conditions where process is exiting
        """
        try:
            import psutil

            current_pid = os.getpid()
            parent_pid = os.getppid()

            # Check all network connections
            for conn in psutil.net_connections(kind='inet'):
                # Check both local and foreign addresses
                if conn.laddr and conn.laddr.port == port:
                    if conn.pid and conn.pid not in (current_pid, parent_pid):
                        try:
                            proc = psutil.Process(conn.pid)
                            return conn.pid, proc.name()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue

            return None, None

        except ImportError:
            logger.debug("[v109.8] psutil not available for fallback PID detection")
            return None, None
        except Exception as e:
            logger.debug(f"[v109.8] psutil error for port {port}: {e}")
            return None, None

    async def _get_process_name(self, pid: int) -> Optional[str]:
        """Get process name for a PID."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "comm=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return stdout.decode().strip() if stdout else None
        except Exception:
            return None

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

        v109.8: Enhanced with multi-layer cleanup and fallback strategies.

        Handles:
        1. LISTEN state: Kill process with SIGTERM, escalate to SIGKILL
        2. TIME_WAIT state: Wait for OS to release (up to max_wait)
        3. CLOSE_WAIT/FIN_WAIT: Find and kill holding process
        4. Orphaned sockets: Use psutil fallback
        5. Process not responding: Force kill

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

        current_pid = os.getpid()
        parent_pid = os.getppid()

        # v109.8: If primary detection found no PID, try fallback methods
        pid_to_kill = validation.pid
        if pid_to_kill is None:
            logger.info(
                f"[v109.8] Port {port}: No PID from primary detection, trying fallbacks..."
            )
            pid_to_kill = await self._find_port_pid_comprehensive(port)

        # v109.0: CRITICAL - Multi-layer PID validation before kill
        if pid_to_kill:
            # Layer 1: Basic self-protection (also in _kill_process)
            if pid_to_kill in (current_pid, parent_pid):
                logger.error(
                    f"[v109.0] SAFETY: Refusing to clean up port {port} - "
                    f"PID {pid_to_kill} is self ({current_pid}) or parent ({parent_pid})"
                )
                return False

            # Layer 2: Verify PID is actually the LISTENER (not an ESTABLISHED connection)
            # v109.8: Skip this check for non-LISTEN states (we're explicitly cleaning orphans)
            if validation.socket_state == "LISTEN":
                listener_check = await self._verify_pid_is_listener(port, pid_to_kill)
                if not listener_check:
                    logger.warning(
                        f"[v109.0] SAFETY: PID {pid_to_kill} is not the LISTEN owner of port {port} "
                        f"- skipping cleanup to avoid killing wrong process"
                    )
                    return False

            # Layer 3: Process name sanity check
            # v109.3: CRITICAL FIX - Only protect SYSTEM processes and the SUPERVISOR
            # NOT jarvis-prime or other JARVIS services (which can be safely killed if stale)
            process_name = validation.process_name
            if not process_name:
                process_name = await self._get_process_name(pid_to_kill)

            system_processes = {"systemd", "launchd", "init", "kernel", "bash", "zsh"}
            if process_name and process_name.lower() in system_processes:
                logger.error(
                    f"[v109.3] SAFETY: PID {pid_to_kill} is a system process "
                    f"({process_name}) - refusing to kill"
                )
                return False

            # v109.3: For Python processes, only protect the SUPERVISOR, not other JARVIS services
            if process_name and process_name.lower() in ("python3", "python"):
                cmdline = await self._get_process_cmdline(pid_to_kill)
                if cmdline:
                    cmdline_lower = cmdline.lower()
                    # Only protect "run_supervisor.py" - NOT jarvis-prime or other services
                    # The check for current_pid/parent_pid already protects us from killing ourselves
                    if "run_supervisor" in cmdline_lower and "jarvis_prime" not in cmdline_lower:
                        logger.warning(
                            f"[v109.3] SAFETY: PID {pid_to_kill} appears to be a supervisor process "
                            f"(run_supervisor in cmdline) - refusing to kill. Cmdline: {cmdline[:100]}"
                        )
                        return False
                    # Log what we're about to kill for debugging
                    logger.info(
                        f"[v109.3] Port cleanup: Will kill PID {pid_to_kill} "
                        f"(cmdline: {cmdline[:100]}...)"
                    )

            # All safety checks passed - proceed with kill
            success = await self._kill_process(
                pid_to_kill,
                force=force,
                wait_timeout=self.cleanup_wait_time
            )
            if success:
                # Verify port is now available
                await asyncio.sleep(0.5)
                check = await self._check_socket_state(port)
                if not check.get("occupied", True):
                    logger.info(f"[Cleanup] Port {port} successfully cleaned up (killed PID {pid_to_kill})")
                    return True

                # May be in TIME_WAIT now
                if check.get("state") == "TIME_WAIT" and wait_for_time_wait:
                    return await self.cleanup_port(
                        port,
                        force=force,
                        wait_for_time_wait=True,
                        max_wait=max_wait - (time.time() - start_time)
                    )

        # v109.8: Final fallback - Port is occupied but we can't find a PID
        # This can happen with kernel-held sockets or race conditions
        if pid_to_kill is None:
            logger.warning(
                f"[v109.8] Port {port}: Occupied ({validation.socket_state}) but no PID found. "
                f"Attempting wait strategy..."
            )
            # Wait for socket to naturally close (works for CLOSE_WAIT, FIN_WAIT)
            remaining_wait = max_wait - (time.time() - start_time)
            if remaining_wait > 0:
                return await self._wait_for_port_release(port, remaining_wait)

        return False

    async def _find_port_pid_comprehensive(self, port: int) -> Optional[int]:
        """
        v109.8: Comprehensive PID search using multiple methods.

        This is a fallback when primary detection fails.
        """
        current_pid = os.getpid()
        parent_pid = os.getppid()

        # Method 1: lsof without LISTEN filter (catches CLOSE_WAIT, FIN_WAIT, etc.)
        pid, _ = await self._get_port_pid_lsof(port, listen_only=False)
        if pid and pid not in (current_pid, parent_pid):
            logger.debug(f"[v109.8] Found PID {pid} via lsof fallback for port {port}")
            return pid

        # Method 2: psutil (catches edge cases lsof misses)
        pid, _ = await self._get_port_pid_psutil(port)
        if pid and pid not in (current_pid, parent_pid):
            logger.debug(f"[v109.8] Found PID {pid} via psutil for port {port}")
            return pid

        # Method 3: fuser command (alternative to lsof, works on some systems)
        try:
            proc = await asyncio.create_subprocess_exec(
                "fuser", f"{port}/tcp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            # fuser outputs to stderr on some systems
            output = (stdout or stderr or b"").decode().strip()
            if output:
                for pid_str in output.split():
                    try:
                        pid = int(pid_str.strip())
                        if pid not in (current_pid, parent_pid):
                            logger.debug(f"[v109.8] Found PID {pid} via fuser for port {port}")
                            return pid
                    except ValueError:
                        continue
        except Exception as e:
            logger.debug(f"[v109.8] fuser fallback failed: {e}")

        return None

    async def _wait_for_port_release(self, port: int, max_wait: float) -> bool:
        """
        v109.8: Wait for a port to be released naturally.

        Used when we can't find a PID but socket is occupied.
        Works for CLOSE_WAIT, FIN_WAIT states that will clear automatically.
        """
        logger.info(f"[v109.8] Waiting up to {max_wait:.1f}s for port {port} to release...")
        start = time.time()
        check_interval = 0.5

        while time.time() - start < max_wait:
            check = await self._check_socket_state(port)
            if not check.get("occupied", True):
                logger.info(f"[v109.8] Port {port} released after {time.time() - start:.1f}s")
                return True

            state = check.get("state", "unknown")
            logger.debug(f"[v109.8] Port {port} still {state}, waiting...")

            await asyncio.sleep(check_interval)
            check_interval = min(check_interval * 1.5, 3.0)  # Exponential backoff

        logger.warning(f"[v109.8] Port {port} did not release within {max_wait:.1f}s")
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

    async def _verify_pid_is_listener(self, port: int, expected_pid: int) -> bool:
        """
        v109.0: Verify that a PID is actually the LISTENING process on a port.

        This prevents killing the wrong process when lsof returns multiple PIDs
        (e.g., the supervisor making health check connections).

        Args:
            port: The port to check
            expected_pid: The PID we expect to be the listener

        Returns:
            True if expected_pid is the LISTENING process on this port
        """
        try:
            # Use lsof with explicit LISTEN filter
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if not stdout:
                return False

            listener_pids = []
            for p in stdout.decode().strip().split('\n'):
                if p.strip():
                    try:
                        listener_pids.append(int(p.strip()))
                    except ValueError:
                        continue

            is_listener = expected_pid in listener_pids
            logger.debug(
                f"[v109.0] _verify_pid_is_listener: port={port}, expected_pid={expected_pid}, "
                f"listener_pids={listener_pids}, is_listener={is_listener}"
            )
            return is_listener

        except Exception as e:
            logger.debug(f"[v109.0] _verify_pid_is_listener error: {e}")
            return False

    async def _get_process_cmdline(self, pid: int) -> Optional[str]:
        """
        v109.0: Get the command line of a process for safety validation.

        Args:
            pid: The process ID to check

        Returns:
            The command line string, or None if unavailable
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "command=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return stdout.decode().strip() if stdout else None
        except Exception as e:
            logger.debug(f"[v109.0] _get_process_cmdline error for PID {pid}: {e}")
            return None

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
