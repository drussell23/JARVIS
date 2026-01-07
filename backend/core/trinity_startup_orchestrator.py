"""
Trinity Unified Startup Orchestrator v79.0
==========================================

Coordinates startup across all three Trinity repos:
- JARVIS (Body) - Main AI agent
- JARVIS-Prime (Mind) - Local LLM inference
- Reactor-Core (Nerves) - Training pipeline

Single command startup: `python3 run_supervisor.py`

FEATURES:
    - Parallel initialization with dependency resolution
    - Health check verification before marking ready
    - Graceful degradation if optional components fail
    - Bidirectional health monitoring
    - Process lifecycle management
    - Startup progress broadcasting

ARCHITECTURE:
    ┌─────────────────────────────────────────────────────────────────┐
    │                 TRINITY STARTUP ORCHESTRATOR                     │
    │                                                                  │
    │  Phase 1: Environment Setup                                      │
    │  ├── Load unified configuration                                  │
    │  ├── Validate directories and permissions                        │
    │  └── Export environment variables                                │
    │                                                                  │
    │  Phase 2: Parallel Component Launch                              │
    │  ├── Start JARVIS-Prime (Mind) → Port 8002                      │
    │  ├── Start Reactor-Core (Nerves) → Port 8003                    │
    │  └── Wait for health checks to pass                              │
    │                                                                  │
    │  Phase 3: Integration Verification                               │
    │  ├── Verify Trinity heartbeats                                   │
    │  ├── Test cross-component communication                          │
    │  └── Start bidirectional health monitoring                       │
    │                                                                  │
    │  Phase 4: JARVIS Main Startup                                    │
    │  └── Continue with existing JARVIS startup                       │
    └─────────────────────────────────────────────────────────────────┘

USAGE:
    from backend.core.trinity_startup_orchestrator import TrinityStartupOrchestrator

    orchestrator = TrinityStartupOrchestrator()
    success = await orchestrator.start_trinity_ecosystem()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# IMPORTS FROM UNIFIED CONFIG
# =============================================================================

try:
    from backend.core.trinity_config import (
        get_config,
        TrinityConfig,
        ComponentType,
        ComponentHealth,
        sleep_with_jitter,
    )
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False
    logger.warning("[TrinityStartup] Unified config not available, using defaults")


# =============================================================================
# ENUMS AND DATA STRUCTURES
# =============================================================================


class StartupPhase(Enum):
    """Startup phases for the Trinity ecosystem."""
    NOT_STARTED = auto()
    ENVIRONMENT_SETUP = auto()
    COMPONENT_LAUNCH = auto()
    HEALTH_VERIFICATION = auto()
    INTEGRATION_CHECK = auto()
    MONITORING_START = auto()
    READY = auto()
    FAILED = auto()


class ComponentStatus(Enum):
    """Status of individual components."""
    NOT_STARTED = auto()
    STARTING = auto()
    HEALTH_CHECK_PENDING = auto()
    HEALTHY = auto()
    DEGRADED = auto()
    FAILED = auto()
    STOPPED = auto()


@dataclass
class ComponentInfo:
    """Information about a Trinity component."""
    component_type: ComponentType
    name: str
    repo_path: Path
    startup_script: str
    port: int
    health_endpoint: str = "/health"
    required: bool = False  # If True, failure blocks startup
    startup_timeout: float = 60.0
    health_check_timeout: float = 10.0
    status: ComponentStatus = ComponentStatus.NOT_STARTED
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    start_time: Optional[float] = None
    error_message: Optional[str] = None

    @property
    def health_url(self) -> str:
        return f"http://localhost:{self.port}{self.health_endpoint}"


@dataclass
class StartupState:
    """Overall startup state."""
    phase: StartupPhase = StartupPhase.NOT_STARTED
    started_at: float = field(default_factory=time.time)
    components: Dict[ComponentType, ComponentInfo] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase.name,
            "started_at": self.started_at,
            "elapsed_seconds": time.time() - self.started_at,
            "components": {
                k.value: {
                    "name": v.name,
                    "status": v.status.name,
                    "port": v.port,
                    "pid": v.pid,
                }
                for k, v in self.components.items()
            },
            "errors": self.errors,
            "warnings": self.warnings,
        }


# =============================================================================
# TRINITY STARTUP ORCHESTRATOR
# =============================================================================


class TrinityStartupOrchestrator:
    """
    Coordinates startup of the entire Trinity ecosystem.

    Ensures all three repos (JARVIS, JARVIS-Prime, Reactor-Core) are
    started in the correct order with proper health verification.
    """

    def __init__(
        self,
        config: Optional[TrinityConfig] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config or (get_config() if _CONFIG_AVAILABLE else None)
        self.logger = logger or logging.getLogger(__name__)
        self.state = StartupState()
        self._shutdown_event = asyncio.Event()
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._progress_callbacks: List[Callable] = []

        # Initialize component info
        self._init_components()

    def _init_components(self) -> None:
        """Initialize component configurations."""
        # Detect repo paths
        jarvis_path = Path(__file__).parent.parent.parent.parent  # JARVIS-AI-Agent
        prime_path = jarvis_path.parent / "jarvis-prime"
        reactor_path = jarvis_path.parent / "reactor-core"

        # Get ports from config or defaults
        prime_port = int(os.getenv("JARVIS_PRIME_PORT", "8002"))
        reactor_port = int(os.getenv("REACTOR_CORE_PORT", "8003"))

        self.state.components = {
            ComponentType.JARVIS_PRIME: ComponentInfo(
                component_type=ComponentType.JARVIS_PRIME,
                name="JARVIS-Prime (Mind)",
                repo_path=prime_path,
                startup_script="run_server.py",
                port=prime_port,
                health_endpoint="/health",
                required=False,  # JARVIS can run without Prime (uses cloud)
                startup_timeout=120.0,  # Model loading can take time
            ),
            ComponentType.REACTOR_CORE: ComponentInfo(
                component_type=ComponentType.REACTOR_CORE,
                name="Reactor-Core (Nerves)",
                repo_path=reactor_path,
                startup_script="reactor_core/orchestration/trinity_orchestrator.py",
                port=reactor_port,
                health_endpoint="/health",
                required=False,  # Optional component
                startup_timeout=30.0,
            ),
        }

    def add_progress_callback(self, callback: Callable[[StartupState], None]) -> None:
        """Add a callback to receive progress updates."""
        self._progress_callbacks.append(callback)

    def _notify_progress(self) -> None:
        """Notify all progress callbacks."""
        for callback in self._progress_callbacks:
            try:
                callback(self.state)
            except Exception as e:
                self.logger.debug(f"Progress callback error: {e}")

    async def start_trinity_ecosystem(self) -> bool:
        """
        Start the entire Trinity ecosystem.

        Returns:
            True if all required components started successfully
        """
        self.logger.info("=" * 60)
        self.logger.info("TRINITY STARTUP ORCHESTRATOR v79.0")
        self.logger.info("=" * 60)

        try:
            # Phase 1: Environment Setup
            self.state.phase = StartupPhase.ENVIRONMENT_SETUP
            self._notify_progress()

            if not await self._setup_environment():
                self.state.phase = StartupPhase.FAILED
                return False

            # Phase 2: Parallel Component Launch
            self.state.phase = StartupPhase.COMPONENT_LAUNCH
            self._notify_progress()

            if not await self._launch_components():
                # Check if any required components failed
                for comp in self.state.components.values():
                    if comp.required and comp.status == ComponentStatus.FAILED:
                        self.state.phase = StartupPhase.FAILED
                        return False

            # Phase 3: Health Verification
            self.state.phase = StartupPhase.HEALTH_VERIFICATION
            self._notify_progress()

            await self._verify_component_health()

            # Phase 4: Integration Check
            self.state.phase = StartupPhase.INTEGRATION_CHECK
            self._notify_progress()

            await self._verify_integration()

            # Phase 5: Start Monitoring
            self.state.phase = StartupPhase.MONITORING_START
            self._notify_progress()

            self._health_monitor_task = asyncio.create_task(
                self._bidirectional_health_monitor()
            )

            # Phase 6: Ready
            self.state.phase = StartupPhase.READY
            self._notify_progress()

            self._log_startup_summary()
            return True

        except Exception as e:
            self.logger.error(f"Trinity startup failed: {e}")
            self.state.errors.append(str(e))
            self.state.phase = StartupPhase.FAILED
            return False

    async def _setup_environment(self) -> bool:
        """Setup environment for Trinity components."""
        self.logger.info("[Trinity] Phase 1: Environment Setup")

        try:
            # Ensure Trinity directories exist
            if self.config:
                trinity_dir = self.config.trinity_dir
            else:
                trinity_dir = Path.home() / ".jarvis" / "trinity"

            directories = [
                trinity_dir,
                trinity_dir / "commands",
                trinity_dir / "heartbeats",
                trinity_dir / "components",
                trinity_dir / "responses",
                trinity_dir / "dlq",
            ]

            for directory in directories:
                directory.mkdir(parents=True, exist_ok=True)
                self.logger.debug(f"  ✓ Directory: {directory}")

            # Export environment variables for child processes
            env_vars = {
                "TRINITY_ENABLED": "true",
                "TRINITY_DIR": str(trinity_dir),
            }

            if self.config:
                env_vars.update({
                    "JARVIS_PRIME_PORT": str(self.config.jarvis_prime_endpoint.port),
                    "REACTOR_CORE_PORT": str(self.config.reactor_core_endpoint.port),
                    "TRINITY_HEARTBEAT_INTERVAL": str(self.config.health.heartbeat_interval),
                })

            os.environ.update(env_vars)
            self.logger.info("  ✓ Environment configured")

            return True

        except Exception as e:
            self.logger.error(f"Environment setup failed: {e}")
            self.state.errors.append(f"Environment setup: {e}")
            return False

    async def _launch_components(self) -> bool:
        """Launch Trinity components in parallel."""
        self.logger.info("[Trinity] Phase 2: Launching Components")

        launch_tasks = []

        for comp_type, comp_info in self.state.components.items():
            if comp_info.repo_path.exists():
                task = asyncio.create_task(
                    self._launch_component(comp_info)
                )
                launch_tasks.append(task)
            else:
                self.logger.warning(f"  ⚠ {comp_info.name}: Repo not found at {comp_info.repo_path}")
                comp_info.status = ComponentStatus.FAILED
                comp_info.error_message = f"Repo not found: {comp_info.repo_path}"
                self.state.warnings.append(f"{comp_info.name} repo not found")

        if launch_tasks:
            # Wait for all launches with timeout
            results = await asyncio.gather(*launch_tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    self.logger.error(f"Component launch error: {result}")

        return True

    async def _launch_component(self, comp_info: ComponentInfo) -> bool:
        """Launch a single component."""
        self.logger.info(f"  → Starting {comp_info.name}...")
        comp_info.status = ComponentStatus.STARTING
        comp_info.start_time = time.time()

        try:
            # Check if already running on port
            if await self._check_port_in_use(comp_info.port):
                self.logger.info(f"    ✓ {comp_info.name} already running on port {comp_info.port}")
                comp_info.status = ComponentStatus.HEALTH_CHECK_PENDING
                return True

            # Find startup script
            script_path = comp_info.repo_path / comp_info.startup_script

            if not script_path.exists():
                # Try alternative locations
                alt_paths = [
                    comp_info.repo_path / "jarvis_prime" / "run_server.py",
                    comp_info.repo_path / "run_server.py",
                    comp_info.repo_path / "main.py",
                ]
                for alt_path in alt_paths:
                    if alt_path.exists():
                        script_path = alt_path
                        break

            if not script_path.exists():
                self.logger.warning(f"    ⚠ {comp_info.name}: Startup script not found")
                comp_info.status = ComponentStatus.FAILED
                comp_info.error_message = "Startup script not found"
                return False

            # Find venv Python
            venv_python = comp_info.repo_path / "venv" / "bin" / "python3"
            if not venv_python.exists():
                venv_python = sys.executable

            # Set environment for subprocess
            env = os.environ.copy()
            env["PYTHONPATH"] = str(comp_info.repo_path)

            # Launch process
            comp_info.process = subprocess.Popen(
                [str(venv_python), str(script_path)],
                cwd=str(comp_info.repo_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # Prevent signal propagation
            )

            comp_info.pid = comp_info.process.pid
            self.logger.info(f"    ✓ {comp_info.name} started (PID: {comp_info.pid})")
            comp_info.status = ComponentStatus.HEALTH_CHECK_PENDING

            return True

        except Exception as e:
            self.logger.error(f"    ✗ {comp_info.name} launch failed: {e}")
            comp_info.status = ComponentStatus.FAILED
            comp_info.error_message = str(e)
            return False

    async def _check_port_in_use(self, port: int) -> bool:
        """Check if a port is already in use."""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex(('localhost', port))
                return result == 0
        except Exception:
            return False

    async def _verify_component_health(self) -> None:
        """Verify health of all launched components."""
        self.logger.info("[Trinity] Phase 3: Health Verification")

        for comp_type, comp_info in self.state.components.items():
            if comp_info.status != ComponentStatus.HEALTH_CHECK_PENDING:
                continue

            healthy = await self._wait_for_health(comp_info)

            if healthy:
                comp_info.status = ComponentStatus.HEALTHY
                self.logger.info(f"  ✓ {comp_info.name}: Healthy")
            else:
                comp_info.status = ComponentStatus.DEGRADED
                self.logger.warning(f"  ⚠ {comp_info.name}: Health check failed (degraded)")
                self.state.warnings.append(f"{comp_info.name} health check failed")

    async def _wait_for_health(self, comp_info: ComponentInfo) -> bool:
        """Wait for a component to become healthy."""
        import aiohttp

        deadline = time.time() + comp_info.startup_timeout
        check_interval = 2.0

        while time.time() < deadline:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        comp_info.health_url,
                        timeout=aiohttp.ClientTimeout(total=comp_info.health_check_timeout)
                    ) as response:
                        if response.status == 200:
                            return True
            except Exception:
                pass

            # Check if process died
            if comp_info.process and comp_info.process.poll() is not None:
                self.logger.warning(f"{comp_info.name} process exited unexpectedly")
                return False

            await sleep_with_jitter(check_interval) if _CONFIG_AVAILABLE else asyncio.sleep(check_interval)

        return False

    async def _verify_integration(self) -> None:
        """Verify Trinity components can communicate."""
        self.logger.info("[Trinity] Phase 4: Integration Verification")

        # Check if Trinity heartbeat files are being created
        if self.config:
            trinity_dir = self.config.trinity_dir
        else:
            trinity_dir = Path.home() / ".jarvis" / "trinity"

        components_dir = trinity_dir / "components"

        # Wait briefly for heartbeats
        await asyncio.sleep(2.0)

        heartbeat_files = list(components_dir.glob("*.json"))
        if heartbeat_files:
            self.logger.info(f"  ✓ Found {len(heartbeat_files)} component heartbeat(s)")
            for hb_file in heartbeat_files:
                self.logger.debug(f"    - {hb_file.name}")
        else:
            self.logger.warning("  ⚠ No component heartbeats found yet")
            self.state.warnings.append("No Trinity heartbeats detected")

    async def _bidirectional_health_monitor(self) -> None:
        """
        Monitor health of all Trinity components bidirectionally.

        - Checks each component's health endpoint
        - Updates component status based on health
        - Triggers alerts/callbacks on status changes
        """
        self.logger.info("[Trinity] Starting bidirectional health monitor")

        check_interval = 10.0  # seconds
        if self.config:
            check_interval = self.config.health.health_check_interval

        while not self._shutdown_event.is_set():
            try:
                for comp_type, comp_info in self.state.components.items():
                    if comp_info.status in (ComponentStatus.STOPPED, ComponentStatus.NOT_STARTED):
                        continue

                    old_status = comp_info.status
                    healthy = await self._check_component_health(comp_info)

                    if healthy:
                        comp_info.status = ComponentStatus.HEALTHY
                    elif comp_info.status == ComponentStatus.HEALTHY:
                        comp_info.status = ComponentStatus.DEGRADED
                        self.logger.warning(f"[HealthMonitor] {comp_info.name} degraded")

                    # Notify on status change
                    if old_status != comp_info.status:
                        self._notify_progress()

                await sleep_with_jitter(check_interval) if _CONFIG_AVAILABLE else asyncio.sleep(check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[HealthMonitor] Error: {e}")
                await asyncio.sleep(5.0)

    async def _check_component_health(self, comp_info: ComponentInfo) -> bool:
        """Check health of a single component."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    comp_info.health_url,
                    timeout=aiohttp.ClientTimeout(total=5.0)
                ) as response:
                    return response.status == 200
        except Exception:
            return False

    def _log_startup_summary(self) -> None:
        """Log startup summary."""
        elapsed = time.time() - self.state.started_at

        self.logger.info("=" * 60)
        self.logger.info("TRINITY STARTUP COMPLETE")
        self.logger.info("=" * 60)
        self.logger.info(f"  Total time: {elapsed:.1f}s")

        for comp_type, comp_info in self.state.components.items():
            status_icon = "✓" if comp_info.status == ComponentStatus.HEALTHY else "⚠"
            self.logger.info(
                f"  {status_icon} {comp_info.name}: {comp_info.status.name} "
                f"(port {comp_info.port})"
            )

        if self.state.warnings:
            self.logger.info(f"  Warnings: {len(self.state.warnings)}")
            for warning in self.state.warnings:
                self.logger.info(f"    - {warning}")

        self.logger.info("=" * 60)

    async def stop_trinity_ecosystem(self) -> None:
        """Stop all Trinity components gracefully."""
        self.logger.info("[Trinity] Stopping Trinity ecosystem...")

        # Signal shutdown
        self._shutdown_event.set()

        # Cancel health monitor
        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        # Stop components in reverse order
        for comp_type, comp_info in reversed(list(self.state.components.items())):
            await self._stop_component(comp_info)

        self.logger.info("[Trinity] Trinity ecosystem stopped")

    async def _stop_component(self, comp_info: ComponentInfo) -> None:
        """Stop a single component gracefully."""
        if not comp_info.process:
            return

        self.logger.info(f"  → Stopping {comp_info.name}...")

        try:
            # Try SIGTERM first
            comp_info.process.terminate()

            # Wait for graceful shutdown
            try:
                comp_info.process.wait(timeout=10.0)
                self.logger.info(f"    ✓ {comp_info.name} stopped gracefully")
            except subprocess.TimeoutExpired:
                # Force kill
                comp_info.process.kill()
                comp_info.process.wait(timeout=5.0)
                self.logger.warning(f"    ⚠ {comp_info.name} killed forcefully")

        except Exception as e:
            self.logger.error(f"    ✗ Error stopping {comp_info.name}: {e}")

        finally:
            comp_info.status = ComponentStatus.STOPPED
            comp_info.process = None

    def get_state(self) -> Dict[str, Any]:
        """Get current startup state as dictionary."""
        return self.state.to_dict()


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_orchestrator: Optional[TrinityStartupOrchestrator] = None


def get_trinity_orchestrator() -> TrinityStartupOrchestrator:
    """Get the singleton Trinity startup orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = TrinityStartupOrchestrator()
    return _orchestrator


async def start_trinity() -> bool:
    """Convenience function to start Trinity ecosystem."""
    orchestrator = get_trinity_orchestrator()
    return await orchestrator.start_trinity_ecosystem()


async def stop_trinity() -> None:
    """Convenience function to stop Trinity ecosystem."""
    if _orchestrator:
        await _orchestrator.stop_trinity_ecosystem()
