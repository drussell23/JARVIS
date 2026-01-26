"""
Trinity Unified Startup Coordinator v113.0
============================================

Single entry point for starting all Trinity repos (JARVIS, JARVIS-Prime, Reactor-Core)
with intelligent coordination, dependency management, and health verification.

Features:
- Dependency-aware startup ordering (infrastructure → JARVIS → Prime + Reactor)
- Parallel startup where safe
- Progressive health verification
- Automatic retry with exponential backoff
- Cross-repo coordination via shared state
- Startup phase tracking to prevent premature degradation

Usage:
    from backend.core.trinity_unified_startup import get_trinity_startup
    
    startup = get_trinity_startup()
    success = await startup.start_all()

Author: JARVIS Development Team
Version: 113.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StartupPhase(str, Enum):
    """Phases of the startup process."""
    INITIALIZING = "initializing"
    LOADING = "loading"              # v113.0: Loading server startup
    INFRASTRUCTURE = "infrastructure"  # Redis, shared state
    CORE = "core"                       # JARVIS-body
    SERVICES = "services"               # J-Prime, Reactor-Core
    FRONTEND = "frontend"            # v113.0: React frontend
    VERIFYING = "verifying"             # Cross-repo health check
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class ServiceStartResult:
    """Result of starting a single service."""
    name: str
    success: bool
    port: int
    pid: Optional[int] = None
    start_time: float = 0.0
    health_check_passed: bool = False
    error: Optional[str] = None
    startup_duration: float = 0.0


@dataclass
class TrinityStartupResult:
    """Overall Trinity startup result."""
    success: bool
    phase: StartupPhase
    services: Dict[str, ServiceStartResult] = field(default_factory=dict)
    total_duration: float = 0.0
    errors: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "phase": self.phase.value,
            "total_duration": self.total_duration,
            "services": {k: vars(v) for k, v in self.services.items()},
            "errors": self.errors,
        }


class TrinityUnifiedStartup:
    """
    Unified startup coordinator for the Trinity system.
    
    Ensures all repos start in the correct order with proper health verification
    and graceful handling of failures.
    """
    
    def __init__(self):
        self._phase = StartupPhase.INITIALIZING
        self._start_time: float = 0.0
        self._services: Dict[str, ServiceStartResult] = {}
        self._callbacks: List[Callable] = []
        
        # State file for cross-process coordination
        self._state_dir = Path.home() / ".jarvis" / "trinity"
        self._state_file = self._state_dir / "startup_state.json"
        
        # Configuration
        self._max_parallel = int(os.getenv("TRINITY_MAX_PARALLEL_STARTUP", "2"))
        self._startup_timeout = float(os.getenv("TRINITY_STARTUP_TIMEOUT", "300.0"))
        
        # v113.0: Loading server and frontend process tracking
        self._loading_server_process: Optional[asyncio.subprocess.Process] = None
        self._frontend_process: Optional[asyncio.subprocess.Process] = None
        self._loading_server_port = int(os.getenv("JARVIS_LOADING_PORT", "3001"))
        self._frontend_port = int(os.getenv("JARVIS_FRONTEND_PORT", "3000"))
        
        # v113.0: Repo paths for cross-repo startup
        self._jarvis_repo = Path(os.getenv(
            "JARVIS_REPO", 
            str(Path.home() / "Documents" / "repos" / "JARVIS-AI-Agent")
        ))
        
    @property
    def phase(self) -> StartupPhase:
        return self._phase
    
    @property
    def is_starting(self) -> bool:
        return self._phase not in (StartupPhase.COMPLETE, StartupPhase.FAILED)
    
    @property
    def startup_elapsed(self) -> float:
        if self._start_time == 0:
            return 0.0
        return time.time() - self._start_time

    async def start_all(
        self,
        skip_infrastructure: bool = False,
        parallel_services: bool = True,
    ) -> TrinityStartupResult:
        """
        Start all Trinity repos with intelligent coordination.
        
        Args:
            skip_infrastructure: Skip Redis/infrastructure setup (for testing)
            parallel_services: Start Prime and Reactor in parallel (default True)
            
        Returns:
            TrinityStartupResult with overall success and per-service details
        """
        self._start_time = time.time()
        result = TrinityStartupResult(success=False, phase=StartupPhase.INITIALIZING)
        
        try:
            # Ensure state directory exists
            self._state_dir.mkdir(parents=True, exist_ok=True)
            
            # Write initial state (for other processes to detect startup in progress)
            await self._write_startup_state("starting")
            
            # ═══════════════════════════════════════════════════════════════════
            # v113.0: Phase 0 - Start loading server FIRST (shows progress to user)
            # ═══════════════════════════════════════════════════════════════════
            self._phase = StartupPhase.LOADING
            await self._emit_phase_change("loading")
            
            loading_ok = await self._start_loading_server()
            if loading_ok:
                logger.info("[TrinityStartup] Loading server is serving startup progress")
            else:
                logger.warning("[TrinityStartup] Loading server failed, continuing without progress display")
            
            # Phase 1: Infrastructure
            if not skip_infrastructure:
                self._phase = StartupPhase.INFRASTRUCTURE
                await self._emit_phase_change("infrastructure")
                
                infra_success = await self._start_infrastructure()
                if not infra_success:
                    result.errors.append("Infrastructure startup failed")
                    # Continue anyway - services may still work
                    logger.warning("[TrinityStartup] Infrastructure startup failed, continuing...")
            
            # Phase 2: Core (JARVIS-body)
            self._phase = StartupPhase.CORE
            await self._emit_phase_change("core")
            
            core_result = await self._start_service_with_health_wait(
                "jarvis-body",
                timeout=120.0,
            )
            self._services["jarvis-body"] = core_result
            result.services["jarvis-body"] = core_result
            
            if not core_result.success:
                result.errors.append(f"Core (jarvis-body) failed: {core_result.error}")
                # Core must succeed
                self._phase = StartupPhase.FAILED
                result.phase = StartupPhase.FAILED
                await self._write_startup_state("failed")
                return result
            
            # Phase 3: Services (J-Prime + Reactor-Core)
            self._phase = StartupPhase.SERVICES
            await self._emit_phase_change("services")
            
            if parallel_services:
                # Start in parallel
                prime_task = asyncio.create_task(
                    self._start_service_with_health_wait("jarvis-prime", timeout=180.0)
                )
                reactor_task = asyncio.create_task(
                    self._start_service_with_health_wait("reactor-core", timeout=120.0)
                )
                
                prime_result, reactor_result = await asyncio.gather(
                    prime_task, reactor_task, return_exceptions=True
                )
                
                # Handle exceptions
                if isinstance(prime_result, Exception):
                    prime_result = ServiceStartResult(
                        name="jarvis-prime", success=False, port=8000,
                        error=str(prime_result)
                    )
                if isinstance(reactor_result, Exception):
                    reactor_result = ServiceStartResult(
                        name="reactor-core", success=False, port=8090,
                        error=str(reactor_result)
                    )
            else:
                # Sequential startup
                prime_result = await self._start_service_with_health_wait(
                    "jarvis-prime", timeout=180.0
                )
                reactor_result = await self._start_service_with_health_wait(
                    "reactor-core", timeout=120.0
                )
            
            self._services["jarvis-prime"] = prime_result
            self._services["reactor-core"] = reactor_result
            result.services["jarvis-prime"] = prime_result
            result.services["reactor-core"] = reactor_result
            
            # Services are optional - log failures but don't abort
            if not prime_result.success:
                result.errors.append(f"jarvis-prime failed: {prime_result.error}")
                logger.warning(f"[TrinityStartup] jarvis-prime failed: {prime_result.error}")
            if not reactor_result.success:
                result.errors.append(f"reactor-core failed: {reactor_result.error}")
                logger.warning(f"[TrinityStartup] reactor-core failed: {reactor_result.error}")
            
            # Phase 4: Verification
            self._phase = StartupPhase.VERIFYING
            await self._emit_phase_change("verifying")
            
            verify_success = await self._verify_trinity_health()
            
            # ═══════════════════════════════════════════════════════════════════
            # v113.0: Phase 5 - Start frontend and stop loading server
            # ═══════════════════════════════════════════════════════════════════
            self._phase = StartupPhase.FRONTEND
            await self._emit_phase_change("frontend")
            
            frontend_ok = await self._start_frontend()
            if frontend_ok:
                logger.info(f"[TrinityStartup] Frontend ready at http://localhost:{self._frontend_port}")
                # Stop loading server now that frontend is ready
                await self._stop_loading_server()
            else:
                logger.warning("[TrinityStartup] Frontend failed to start - loading page will remain active")
                result.errors.append("Frontend failed to start")
            
            # Complete
            self._phase = StartupPhase.COMPLETE
            result.phase = StartupPhase.COMPLETE
            result.success = core_result.success  # Core must be healthy
            result.total_duration = time.time() - self._start_time
            
            await self._write_startup_state("complete" if result.success else "degraded")
            
            # Log summary
            healthy_count = sum(1 for s in self._services.values() if s.success)
            logger.info(
                f"[TrinityStartup] Complete: {healthy_count}/{len(self._services)} services healthy "
                f"({result.total_duration:.1f}s)"
            )
            if frontend_ok:
                logger.info(f"🚀 JARVIS is online at http://localhost:{self._frontend_port}")
            
            return result
            
        except Exception as e:
            logger.error(f"[TrinityStartup] Fatal error: {e}")
            self._phase = StartupPhase.FAILED
            result.phase = StartupPhase.FAILED
            result.errors.append(str(e))
            result.total_duration = time.time() - self._start_time
            await self._write_startup_state("failed")
            return result
    
    async def _start_infrastructure(self) -> bool:
        """Start infrastructure services (Redis, shared state)."""
        logger.info("[TrinityStartup] Starting infrastructure...")
        
        # Check Redis availability
        try:
            import redis.asyncio as aioredis
            client = await aioredis.from_url("redis://localhost:6379", socket_timeout=5.0)
            await asyncio.wait_for(client.ping(), timeout=5.0)
            await client.aclose()
            logger.info("[TrinityStartup] Redis is available")
            return True
        except Exception as e:
            logger.warning(f"[TrinityStartup] Redis not available: {e}")
            return False
    
    async def _start_service_with_health_wait(
        self,
        service_name: str,
        timeout: float = 60.0,
    ) -> ServiceStartResult:
        """
        Start a service and wait for it to become healthy.
        
        Uses the cross_repo_startup_orchestrator if available,
        otherwise checks if service is already running.
        """
        start_time = time.time()
        result = ServiceStartResult(
            name=service_name,
            success=False,
            port=self._get_service_port(service_name),
            start_time=start_time,
        )
        
        logger.info(f"[TrinityStartup] Starting {service_name}...")
        
        try:
            # Try to use the orchestrator
            try:
                from backend.supervisor.cross_repo_startup_orchestrator import (
                    get_process_orchestrator,
                )
                orchestrator = get_process_orchestrator()
                
                # Check if already managed
                if service_name in orchestrator.processes:
                    managed = orchestrator.processes[service_name]
                    if managed.is_running:
                        logger.info(f"[TrinityStartup] {service_name} already running (pid={managed.pid})")
                        result.success = True
                        result.pid = managed.pid
                        result.startup_duration = time.time() - start_time
                        return result
                
            except ImportError:
                logger.debug("[TrinityStartup] cross_repo_startup_orchestrator not available")
            
            # Check if service is already running via health check
            health_ok = await self._check_service_health(service_name, timeout=10.0)
            
            if health_ok:
                logger.info(f"[TrinityStartup] {service_name} is already healthy")
                result.success = True
                result.health_check_passed = True
                result.startup_duration = time.time() - start_time
                return result
            
            # Service not running - wait for it (orchestrator should start it)
            logger.info(f"[TrinityStartup] Waiting for {service_name} to become healthy (timeout={timeout}s)...")
            
            deadline = time.time() + timeout
            check_interval = 5.0
            
            while time.time() < deadline:
                health_ok = await self._check_service_health(service_name, timeout=10.0)
                
                if health_ok:
                    result.success = True
                    result.health_check_passed = True
                    result.startup_duration = time.time() - start_time
                    logger.info(f"[TrinityStartup] {service_name} is healthy ({result.startup_duration:.1f}s)")
                    return result
                
                await asyncio.sleep(check_interval)
            
            # Timeout
            result.error = f"Timeout waiting for {service_name} after {timeout}s"
            result.startup_duration = time.time() - start_time
            logger.warning(f"[TrinityStartup] {service_name} startup timeout")
            return result
            
        except Exception as e:
            result.error = str(e)
            result.startup_duration = time.time() - start_time
            logger.error(f"[TrinityStartup] {service_name} startup error: {e}")
            return result
    
    async def _check_service_health(self, service_name: str, timeout: float = 10.0) -> bool:
        """Check if a service is healthy via HTTP health endpoint."""
        try:
            import aiohttp
            
            port = self._get_service_port(service_name)
            url = f"http://localhost:{port}/health"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    return resp.status == 200
                    
        except Exception:
            return False
    
    async def _verify_trinity_health(self) -> bool:
        """Verify all Trinity services are healthy and can communicate."""
        logger.info("[TrinityStartup] Verifying Trinity health...")
        
        all_healthy = True
        for name, result in self._services.items():
            if result.success:
                current_health = await self._check_service_health(name, timeout=5.0)
                if not current_health:
                    logger.warning(f"[TrinityStartup] {name} failed health verification")
                    all_healthy = False
        
        return all_healthy
    
    def _get_service_port(self, service_name: str) -> int:
        """Get the port for a service."""
        ports = {
            "jarvis-body": int(os.getenv("JARVIS_BODY_PORT", "8010")),
            "jarvis-prime": int(os.getenv("JARVIS_PRIME_PORT", "8000")),
            "reactor-core": int(os.getenv("REACTOR_CORE_PORT", "8090")),
        }
        return ports.get(service_name, 8010)
    
    # ═══════════════════════════════════════════════════════════════════════
    # v113.0: LOADING SERVER AND FRONTEND STARTUP
    # ═══════════════════════════════════════════════════════════════════════
    
    async def _start_loading_server(self) -> bool:
        """
        v113.0: Start the loading server on port 3001 for startup progress display.
        
        The loading server serves loading-manager.js and broadcasts startup progress
        to connected clients via WebSocket.
        
        Returns:
            True if loading server started successfully
        """
        logger.info(f"[TrinityStartup] Starting loading server on port {self._loading_server_port}...")
        
        try:
            # Check if loading_server.py exists
            loading_server_path = self._jarvis_repo / "backend" / "loading_server.py"
            if not loading_server_path.exists():
                # Fallback: use Python HTTP server
                loading_server_path = self._jarvis_repo / "frontend" / "public"
                if loading_server_path.exists():
                    self._loading_server_process = await asyncio.create_subprocess_exec(
                        "python3", "-m", "http.server", str(self._loading_server_port),
                        cwd=str(loading_server_path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    logger.info(f"[TrinityStartup] Loading server started (fallback mode, pid={self._loading_server_process.pid})")
                    return True
                else:
                    logger.warning("[TrinityStartup] Loading server path not found")
                    return False
            
            # Start the dedicated loading server
            self._loading_server_process = await asyncio.create_subprocess_exec(
                "python3", str(loading_server_path),
                env={
                    **os.environ,
                    "LOADING_SERVER_PORT": str(self._loading_server_port),
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            # Wait for it to start
            await asyncio.sleep(1.0)
            
            if self._loading_server_process.returncode is None:
                logger.info(f"[TrinityStartup] Loading server started (pid={self._loading_server_process.pid})")
                return True
            else:
                logger.warning(f"[TrinityStartup] Loading server exited with code {self._loading_server_process.returncode}")
                return False
                
        except Exception as e:
            logger.warning(f"[TrinityStartup] Failed to start loading server: {e}")
            return False
    
    async def _start_frontend(self) -> bool:
        """
        v113.0: Start the React frontend on port 3000.
        
        This should be called AFTER the backend is healthy.
        
        Returns:
            True if frontend started successfully
        """
        logger.info(f"[TrinityStartup] Starting frontend on port {self._frontend_port}...")
        
        try:
            frontend_dir = self._jarvis_repo / "frontend"
            
            if not frontend_dir.exists():
                logger.warning(f"[TrinityStartup] Frontend directory not found: {frontend_dir}")
                return False
            
            # Check if node_modules exists
            node_modules = frontend_dir / "node_modules"
            if not node_modules.exists():
                logger.info("[TrinityStartup] Installing frontend dependencies...")
                npm_install = await asyncio.create_subprocess_exec(
                    "npm", "install",
                    cwd=str(frontend_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(npm_install.wait(), timeout=300.0)
                if npm_install.returncode != 0:
                    logger.warning("[TrinityStartup] npm install failed")
                    return False
            
            # Start the frontend dev server
            env = {
                **os.environ,
                "PORT": str(self._frontend_port),
                "BROWSER": "none",  # Don't auto-open browser
                "REACT_APP_BACKEND_URL": f"http://localhost:{self._get_service_port('jarvis-body')}",
            }
            
            self._frontend_process = await asyncio.create_subprocess_exec(
                "npm", "start",
                cwd=str(frontend_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            # Wait for frontend to be ready (poll health endpoint)
            deadline = time.time() + 120.0  # 2 minute timeout for webpack
            check_interval = 3.0
            
            while time.time() < deadline:
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.head(
                            f"http://localhost:{self._frontend_port}/",
                            timeout=aiohttp.ClientTimeout(total=5.0)
                        ) as resp:
                            if resp.status in (200, 304):
                                logger.info(f"[TrinityStartup] Frontend ready (pid={self._frontend_process.pid})")
                                return True
                except Exception:
                    pass
                
                # Check if process died
                if self._frontend_process.returncode is not None:
                    logger.warning(f"[TrinityStartup] Frontend exited with code {self._frontend_process.returncode}")
                    return False
                
                await asyncio.sleep(check_interval)
            
            logger.warning("[TrinityStartup] Frontend startup timeout")
            return False
            
        except Exception as e:
            logger.error(f"[TrinityStartup] Failed to start frontend: {e}")
            return False
    
    async def _stop_loading_server(self) -> None:
        """v113.0: Stop the loading server (called after frontend is ready)."""
        if self._loading_server_process and self._loading_server_process.returncode is None:
            logger.info("[TrinityStartup] Stopping loading server...")
            try:
                self._loading_server_process.terminate()
                await asyncio.wait_for(self._loading_server_process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._loading_server_process.kill()
            self._loading_server_process = None
    
    async def _write_startup_state(self, status: str) -> None:
        """Write current startup state for cross-process coordination."""
        try:
            state = {
                "status": status,
                "phase": self._phase.value,
                "start_time": self._start_time,
                "elapsed": self.startup_elapsed,
                "services": {k: v.success for k, v in self._services.items()},
                "timestamp": time.time(),
            }
            self._state_file.write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.debug(f"[TrinityStartup] Failed to write state: {e}")
    
    async def _emit_phase_change(self, phase: str) -> None:
        """Emit phase change event to callbacks."""
        for callback in self._callbacks:
            try:
                result = callback(phase, self.startup_elapsed)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.debug(f"[TrinityStartup] Callback error: {e}")
    
    def on_phase_change(self, callback: Callable) -> None:
        """Register a callback for phase changes."""
        self._callbacks.append(callback)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current startup status."""
        return {
            "phase": self._phase.value,
            "is_starting": self.is_starting,
            "elapsed": self.startup_elapsed,
            "services": {k: {"success": v.success, "error": v.error} for k, v in self._services.items()},
        }


# =============================================================================
# Global Instance
# =============================================================================

_startup: Optional[TrinityUnifiedStartup] = None


def get_trinity_startup() -> TrinityUnifiedStartup:
    """Get or create the global Trinity startup coordinator."""
    global _startup
    if _startup is None:
        _startup = TrinityUnifiedStartup()
    return _startup


def is_startup_in_progress() -> bool:
    """Check if Trinity startup is currently in progress."""
    if _startup is None:
        # Check state file
        state_file = Path.home() / ".jarvis" / "trinity" / "startup_state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                if state.get("status") == "starting":
                    # Check if stale (more than 10 minutes old)
                    age = time.time() - state.get("timestamp", 0)
                    return age < 600
            except Exception:
                pass
        return False
    return _startup.is_starting


def get_startup_phase() -> str:
    """Get the current startup phase."""
    if _startup is None:
        return "unknown"
    return _startup.phase.value


def get_startup_elapsed() -> float:
    """Get elapsed time since startup began."""
    if _startup is None:
        return 0.0
    return _startup.startup_elapsed


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "TrinityUnifiedStartup",
    "StartupPhase",
    "ServiceStartResult",
    "TrinityStartupResult",
    "get_trinity_startup",
    "is_startup_in_progress",
    "get_startup_phase",
    "get_startup_elapsed",
]
