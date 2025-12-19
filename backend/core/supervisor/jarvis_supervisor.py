#!/usr/bin/env python3
"""
JARVIS Lifecycle Supervisor
============================

The bulletproof process manager that sits above JARVIS Core.
Handles process spawning, exit code interpretation, restart logic,
and coordination with update/rollback systems.

Architecture:
    Supervisor → spawns → JARVIS Core (start_system.py)
                ↑
    Exit Codes: 0=clean, 1=crash, 100=update, 101=rollback, 102=restart

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Callable, Optional

from .supervisor_config import (
    SupervisorConfig,
    SupervisorMode,
    get_supervisor_config,
)
from .narrator import (
    SupervisorNarrator,
    NarratorEvent,
    get_narrator,
)
from .startup_narrator import (
    IntelligentStartupNarrator,
    StartupPhase,
    NarrationPriority as StartupNarrationPriority,
    get_startup_narrator,
    get_phase_from_stage,
)
from .update_notification import (
    UpdateNotificationOrchestrator,
    NotificationChannel,
    NotificationPriority,
    get_notification_orchestrator,
)

# Import loading server functionality (lazy import to avoid circular deps)
def _get_loading_server():
    """Lazy import of loading_server module."""
    import importlib.util
    from pathlib import Path
    
    # loading_server.py is at project root
    project_root = Path(__file__).parent.parent.parent.parent
    loading_server_path = project_root / "loading_server.py"
    
    spec = importlib.util.spec_from_file_location("loading_server", loading_server_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

logger = logging.getLogger(__name__)


class ExitCode(IntEnum):
    """Exit codes for supervisor-to-process communication."""
    CLEAN_SHUTDOWN = 0
    ERROR_CRASH = 1
    UPDATE_REQUEST = 100
    ROLLBACK_REQUEST = 101
    RESTART_REQUEST = 102
    
    @classmethod
    def from_config(cls, config: SupervisorConfig) -> dict[str, int]:
        """Get exit codes from config."""
        return {
            "clean": config.exit_codes.clean_shutdown,
            "crash": config.exit_codes.error_crash,
            "update": config.exit_codes.update_request,
            "rollback": config.exit_codes.rollback_request,
            "restart": config.exit_codes.restart_request,
        }


class SupervisorState(str, Enum):
    """Supervisor operational states."""
    INITIALIZING = "initializing"
    STARTING = "starting"
    RUNNING = "running"
    UPDATING = "updating"
    ROLLING_BACK = "rolling_back"
    RESTARTING = "restarting"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"


@dataclass
class ProcessInfo:
    """Information about the supervised JARVIS process."""
    pid: Optional[int] = None
    start_time: Optional[datetime] = None
    last_exit_code: Optional[int] = None
    crash_count: int = 0
    last_crash_time: Optional[datetime] = None
    uptime_seconds: float = 0.0
    
    def is_stable(self, window_seconds: int = 60) -> bool:
        """Check if process has been stable (no crash within window)."""
        if self.start_time is None:
            return False
        uptime = (datetime.now() - self.start_time).total_seconds()
        return uptime >= window_seconds


@dataclass
class SupervisorStats:
    """Supervisor statistics for monitoring."""
    total_starts: int = 0
    total_crashes: int = 0
    total_updates: int = 0
    total_rollbacks: int = 0
    supervisor_start_time: datetime = field(default_factory=datetime.now)
    current_version: Optional[str] = None


class JARVISSupervisor:
    """
    Main Lifecycle Supervisor for JARVIS.
    
    Features:
    - Async process spawning with exit code handling
    - Automatic restart on crash with exponential backoff
    - Update coordination via exit code 100
    - Rollback triggering via exit code 101
    - Health monitoring integration
    - Signal forwarding to child process
    - Graceful shutdown orchestration
    
    Example:
        >>> supervisor = JARVISSupervisor()
        >>> await supervisor.run()  # Runs until shutdown
    """
    
    def __init__(
        self,
        config: Optional[SupervisorConfig] = None,
        jarvis_entry_point: Optional[str] = None,
    ):
        """
        Initialize the JARVIS Supervisor.
        
        Args:
            config: Supervisor configuration (loads from YAML if None)
            jarvis_entry_point: Path to JARVIS entry point (default: start_system.py)
        """
        self.config = config or get_supervisor_config()
        self.jarvis_entry_point = jarvis_entry_point or self._find_entry_point()
        
        self.state = SupervisorState.INITIALIZING
        self.process_info = ProcessInfo()
        self.stats = SupervisorStats()
        
        self._process: Optional[asyncio.subprocess.Process] = None
        self._shutdown_event = asyncio.Event()
        self._update_requested = asyncio.Event()
        self._rollback_requested = asyncio.Event()
        
        # Callbacks for extensibility
        self._on_state_change: list[Callable[[SupervisorState], None]] = []
        self._on_crash: list[Callable[[int], None]] = []
        self._on_update_available: list[Callable[[], None]] = []
        
        # TTS Narrator for engaging feedback
        self._narrator: SupervisorNarrator = get_narrator()
        
        # Intelligent startup narrator for phase-aware narration
        self._startup_narrator: IntelligentStartupNarrator = get_startup_narrator()
        
        # Loading page support (uses loading_server.py directly)
        self._progress_reporter: Optional[Any] = None
        self._show_loading_page: bool = True  # Can be disabled via env
        
        # Components (lazy loaded)
        self._update_engine: Optional[Any] = None
        self._rollback_manager: Optional[Any] = None
        self._health_monitor: Optional[Any] = None
        self._update_detector: Optional[Any] = None
        self._idle_detector: Optional[Any] = None
        self._notification_orchestrator: Optional[UpdateNotificationOrchestrator] = None
        
        logger.info(f"🔧 Supervisor initialized (mode: {self.config.mode.value})")
    
    def _find_entry_point(self) -> str:
        """Find the JARVIS entry point script."""
        possible_paths = [
            Path(__file__).parent.parent.parent.parent / "start_system.py",
            Path("start_system.py"),
            Path("backend/start_system.py"),
        ]
        for p in possible_paths:
            if p.exists():
                return str(p.resolve())
        raise FileNotFoundError("Could not find start_system.py")
    
    def _set_state(self, new_state: SupervisorState) -> None:
        """Update state and notify callbacks."""
        old_state = self.state
        self.state = new_state
        logger.info(f"📊 Supervisor state: {old_state.value} → {new_state.value}")
        
        for callback in self._on_state_change:
            try:
                callback(new_state)
            except Exception as e:
                logger.error(f"State change callback error: {e}")
    
    async def _init_components(self) -> None:
        """Initialize supervisor components lazily."""
        if self._update_engine is None:
            from .update_engine import UpdateEngine
            self._update_engine = UpdateEngine(self.config)
        
        if self._rollback_manager is None:
            from .rollback_manager import RollbackManager
            self._rollback_manager = RollbackManager(self.config)
            await self._rollback_manager.initialize()
        
        if self._health_monitor is None:
            from .health_monitor import HealthMonitor
            self._health_monitor = HealthMonitor(self.config)
        
        if self._update_detector is None and self.config.update.check.enabled:
            from .update_detector import UpdateDetector
            self._update_detector = UpdateDetector(self.config)
        
        if self._idle_detector is None and self.config.idle.enabled:
            from .idle_detector import IdleDetector
            self._idle_detector = IdleDetector(self.config)
        
        # Initialize notification orchestrator with all required components
        if self._notification_orchestrator is None:
            self._notification_orchestrator = UpdateNotificationOrchestrator(
                config=self.config,
                narrator=self._narrator,
                update_detector=self._update_detector,
            )
    
    async def _open_loading_page(self) -> bool:
        """
        Open browser to loading page at localhost:3001.
        
        Uses Chrome Incognito for consistent, cache-free experience.
        """
        loading_url = "http://localhost:3001/"
        
        try:
            # Launch Chrome Incognito with loading page
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-na", "Google Chrome",
                "--args", "--incognito", "--new-window", "--start-fullscreen", loading_url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            
            logger.info(f"🌐 Opened loading page: {loading_url}")
            return True
            
        except Exception as e:
            logger.warning(f"⚠️ Could not open browser: {e}")
            logger.info(f"📎 Please manually open: {loading_url}")
            return False
    
    async def _spawn_jarvis(self) -> int:
        """
        Spawn the JARVIS process and wait for exit.
        
        Includes loading page orchestration for visual feedback during startup
        AND intelligent voice narration for audio feedback.
        
        Returns:
            Exit code from the JARVIS process
        """
        self._set_state(SupervisorState.STARTING)
        
        # Start the startup narrator
        await self._startup_narrator.start()
        
        # Start loading page if enabled (first start or after crash)
        show_loading = self._show_loading_page and os.environ.get("JARVIS_NO_LOADING") != "1"
        
        if show_loading:
            try:
                loading_server = _get_loading_server()
                
                # Start loading server in background (if not already running)
                await loading_server.start_loading_server_background()
                
                # Get progress reporter
                self._progress_reporter = loading_server.get_progress_reporter()
                
                # Report and narrate supervisor init
                await self._progress_reporter.report(
                    "supervisor_init",
                    "Supervisor initializing...",
                    5,
                )
                await self._startup_narrator.announce_phase(
                    StartupPhase.SUPERVISOR_INIT,
                    "Supervisor initializing...",
                    5,
                    context="start",
                )
                
                # Open browser to loading page (only on first start)
                if self.stats.total_starts == 0:
                    await self._open_loading_page()
                    
            except Exception as e:
                logger.warning(f"⚠️ Loading page unavailable: {e}")
                self._progress_reporter = None
        
        # Build command
        python_executable = sys.executable
        cmd = [python_executable, self.jarvis_entry_point]
        
        # Add supervisor-specific environment
        env = os.environ.copy()
        env["JARVIS_SUPERVISED"] = "1"
        env["JARVIS_SUPERVISOR_PID"] = str(os.getpid())
        
        # Tell start_system.py that supervisor is handling loading page
        if self._progress_reporter:
            env["JARVIS_SUPERVISOR_LOADING"] = "1"
        
        logger.info(f"🚀 Spawning JARVIS: {' '.join(cmd)}")
        
        # Broadcast spawning stage
        if self._progress_reporter:
            await self._progress_reporter.report(
                "spawning",
                "Starting JARVIS Core...",
                10,
            )
        
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=None,  # Inherit stdout
                stderr=None,  # Inherit stderr
            )
            
            self.process_info.pid = self._process.pid
            self.process_info.start_time = datetime.now()
            self.stats.total_starts += 1
            
            self._set_state(SupervisorState.RUNNING)
            logger.info(f"✅ JARVIS started (PID: {self._process.pid})")
            
            # Announce JARVIS is online (after first successful start)
            if self.stats.total_starts == 1:
                await self._narrator.narrate(NarratorEvent.JARVIS_ONLINE)
            
            # Start health monitoring with loading page progress
            if self._health_monitor:
                asyncio.create_task(self._monitor_health())
            
            # Start loading progress monitor (polls backend health and updates loading page)
            if self._progress_reporter:
                asyncio.create_task(self._monitor_startup_progress())
            
            # Wait for process to exit
            exit_code = await self._process.wait()
            
            self.process_info.last_exit_code = exit_code
            self.process_info.uptime_seconds = (
                datetime.now() - self.process_info.start_time
            ).total_seconds()
            
            logger.info(f"📋 JARVIS exited with code {exit_code} (uptime: {self.process_info.uptime_seconds:.1f}s)")
            
            return exit_code
            
        except Exception as e:
            logger.error(f"❌ Failed to spawn JARVIS: {e}")
            return ExitCode.ERROR_CRASH
        finally:
            self._process = None
            # Close progress reporter on exit
            if self._progress_reporter:
                try:
                    await self._progress_reporter.close()
                except Exception:
                    pass
                self._progress_reporter = None
    
    async def _monitor_startup_progress(self) -> None:
        """
        Monitor JARVIS startup and update loading page progress.
        
        Polls backend health endpoint and broadcasts stages until ready.
        Uses loading_server.py for visual progress and IntelligentStartupNarrator
        for voice narration.
        
        Features:
        - Parallel visual + voice feedback
        - Smart phase detection and narration
        - Slow startup detection with encouraging messages
        - Milestone progress announcements
        """
        if not self._progress_reporter:
            return
        
        import aiohttp
        
        # Use dynamic port configuration from environment
        backend_port = int(os.environ.get("BACKEND_PORT", "8010"))
        frontend_port = int(os.environ.get("FRONTEND_PORT", "3000"))
        
        backend_url = f"http://localhost:{backend_port}"
        frontend_url = f"http://localhost:{frontend_port}"
        
        stages_completed = set()
        stages_narrated = set()
        max_wait_seconds = 120  # 2 minute timeout
        start_time = time.time()
        slow_startup_announced = False
        slow_startup_threshold = 45.0  # Announce if startup takes > 45s
        
        async def check_endpoint(url: str, timeout: float = 2.0) -> bool:
            """Check if an endpoint is responding."""
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        return resp.status == 200
            except Exception:
                return False
        
        # Progress percentages for each stage
        progress_map = {
            "spawning": 15,
            "backend": 40,
            "database": 55,
            "voice": 70,
            "vision": 80,
            "frontend": 90,
        }
        
        # Narrate spawning phase
        await self._startup_narrator.announce_phase(
            StartupPhase.SPAWNING,
            "Starting JARVIS Core...",
            progress_map["spawning"],
            context="start",
        )
        
        while self.state == SupervisorState.RUNNING and self._progress_reporter:
            elapsed = time.time() - start_time
            
            # Check for slow startup
            if not slow_startup_announced and elapsed > slow_startup_threshold:
                slow_startup_announced = True
                await self._startup_narrator.announce_slow_startup()
            
            if elapsed > max_wait_seconds:
                logger.warning("⚠️ Startup monitoring timeout")
                await self._progress_reporter.fail(
                    "Startup timeout - please check logs",
                    error="Timeout after 2 minutes"
                )
                await self._startup_narrator.announce_error("Startup timeout")
                break
            
            try:
                # Check backend health
                backend_ready = await check_endpoint(f"{backend_url}/health")
                
                if backend_ready and "backend" not in stages_completed:
                    stages_completed.add("backend")
                    await self._progress_reporter.report(
                        "api",
                        "Backend API online!",
                        progress_map["backend"],
                    )
                    
                    # Narrate backend ready (only once)
                    if "backend" not in stages_narrated:
                        stages_narrated.add("backend")
                        await self._startup_narrator.announce_phase(
                            StartupPhase.BACKEND_INIT,
                            "Backend API online!",
                            progress_map["backend"],
                            context="complete",
                        )
                        # Also announce progress milestone
                        await self._startup_narrator.announce_progress(progress_map["backend"])
                
                # Once backend is ready, check for full system status
                if backend_ready:
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"{backend_url}/api/system/status",
                                timeout=aiohttp.ClientTimeout(total=3.0)
                            ) as resp:
                                if resp.status == 200:
                                    status = await resp.json()
                                    
                                    # Update stages based on status
                                    if status.get("database_connected") and "database" not in stages_completed:
                                        stages_completed.add("database")
                                        await self._progress_reporter.report(
                                            "database",
                                            "Database connected",
                                            progress_map["database"],
                                        )
                                        
                                        if "database" not in stages_narrated:
                                            stages_narrated.add("database")
                                            await self._startup_narrator.announce_phase(
                                                StartupPhase.DATABASE,
                                                "Database connected",
                                                progress_map["database"],
                                                context="complete",
                                            )
                                            await self._startup_narrator.announce_progress(progress_map["database"])
                                    
                                    if status.get("voice_ready") and "voice" not in stages_completed:
                                        stages_completed.add("voice")
                                        await self._progress_reporter.report(
                                            "voice",
                                            "Voice system ready",
                                            progress_map["voice"],
                                        )
                                        
                                        if "voice" not in stages_narrated:
                                            stages_narrated.add("voice")
                                            await self._startup_narrator.announce_phase(
                                                StartupPhase.VOICE,
                                                "Voice system ready",
                                                progress_map["voice"],
                                                context="complete",
                                            )
                                            await self._startup_narrator.announce_progress(progress_map["voice"])
                                    
                                    if status.get("vision_ready") and "vision" not in stages_completed:
                                        stages_completed.add("vision")
                                        await self._progress_reporter.report(
                                            "vision",
                                            "Vision system ready",
                                            progress_map["vision"],
                                        )
                                        
                                        if "vision" not in stages_narrated:
                                            stages_narrated.add("vision")
                                            await self._startup_narrator.announce_phase(
                                                StartupPhase.VISION,
                                                "Vision system ready",
                                                progress_map["vision"],
                                                context="complete",
                                            )
                    except Exception:
                        pass
                
                # Check frontend
                frontend_ready = await check_endpoint(frontend_url)
                
                if frontend_ready and "frontend" not in stages_completed:
                    stages_completed.add("frontend")
                    await self._progress_reporter.report(
                        "frontend",
                        "Frontend ready!",
                        progress_map["frontend"],
                    )
                    
                    if "frontend" not in stages_narrated:
                        stages_narrated.add("frontend")
                        await self._startup_narrator.announce_phase(
                            StartupPhase.FRONTEND,
                            "Frontend ready!",
                            progress_map["frontend"],
                            context="complete",
                        )
                        await self._startup_narrator.announce_progress(progress_map["frontend"])
                
                # If both backend and frontend are ready, complete!
                if backend_ready and frontend_ready:
                    await asyncio.sleep(0.5)  # Brief pause for visual effect
                    await self._progress_reporter.complete(
                        "JARVIS is online!",
                        redirect_url=frontend_url,
                    )
                    
                    # Narrate completion with duration info
                    duration = time.time() - start_time
                    await self._startup_narrator.announce_complete(
                        duration_seconds=duration,
                    )
                    
                    logger.info("✅ Startup complete, loading page redirecting to main app")
                    break
                
            except Exception as e:
                logger.debug(f"Startup progress check error: {e}")
            
            await asyncio.sleep(1.0)  # Check every second
        
        # Stop the startup narrator
        await self._startup_narrator.stop()
    
    async def _monitor_health(self) -> None:
        """Monitor JARVIS health while running."""
        while self.state == SupervisorState.RUNNING and self._process:
            await asyncio.sleep(self.config.health.check_interval_seconds)
            
            if self._health_monitor and self._process:
                is_healthy = await self._health_monitor.check_health()
                if not is_healthy:
                    logger.warning("⚠️ Health check failed")
                    # Could trigger graceful restart here
    
    async def _handle_crash(self, exit_code: int) -> bool:
        """
        Handle a crash (exit code != 0, 100, 101, 102).
        
        Returns:
            True if should retry, False if should stop
        """
        self.process_info.crash_count += 1
        self.process_info.last_crash_time = datetime.now()
        self.stats.total_crashes += 1
        
        logger.error(f"💥 JARVIS crashed (exit code: {exit_code}, crash #{self.process_info.crash_count})")
        
        # Announce crash detection
        await self._narrator.narrate(NarratorEvent.CRASH_DETECTED)
        
        # Notify callbacks
        for callback in self._on_crash:
            try:
                callback(exit_code)
            except Exception as e:
                logger.error(f"Crash callback error: {e}")
        
        # Check if boot was unstable (crash within stability window)
        if not self.process_info.is_stable(self.config.health.boot_stability_window):
            logger.error(f"🔥 Boot unstable (crashed within {self.config.health.boot_stability_window}s)")
            
            if self.config.rollback.auto_on_boot_failure and self._rollback_manager:
                logger.info("🔄 Triggering automatic rollback...")
                self._set_state(SupervisorState.ROLLING_BACK)
                success = await self._rollback_manager.rollback()
                
                if success:
                    self.stats.total_rollbacks += 1
                    logger.info("✅ Rollback complete, will retry")
                    return True
                else:
                    logger.error("❌ Rollback failed")
                    return False
        
        # Check retry limit
        if self.process_info.crash_count >= self.config.health.max_crash_retries:
            logger.error(f"❌ Max crash retries ({self.config.health.max_crash_retries}) exceeded")
            return False
        
        # Calculate backoff delay
        delay = self.config.health.retry_delay_seconds * (
            self.config.health.backoff_multiplier ** (self.process_info.crash_count - 1)
        )
        logger.info(f"⏳ Waiting {delay:.1f}s before retry...")
        await asyncio.sleep(delay)
        
        return True
    
    async def _handle_update_request(self) -> bool:
        """
        Handle an update request (exit code 100).
        
        Returns:
            True if update successful, False if failed
        """
        self._set_state(SupervisorState.UPDATING)
        logger.info("🔄 Update requested by JARVIS")
        
        # Broadcast maintenance mode to frontend BEFORE JARVIS shuts down
        try:
            from .maintenance_broadcaster import broadcast_maintenance_mode
            await broadcast_maintenance_mode(
                reason="updating",
                message="Downloading updates from repository...",
                estimated_time=30,
            )
        except Exception as e:
            logger.debug(f"Maintenance broadcast failed: {e}")
        
        # Announce update starting via TTS
        await self._narrator.narrate(NarratorEvent.UPDATE_STARTING, wait=True)
        
        if not self._update_engine:
            logger.error("❌ Update engine not initialized")
            await self._narrator.narrate(NarratorEvent.UPDATE_FAILED)
            return False
        
        try:
            # Take version snapshot before update
            if self._rollback_manager:
                await self._rollback_manager.create_snapshot()
            
            # Narrate download phase
            await self._narrator.narrate(NarratorEvent.DOWNLOADING)
            
            # Register progress callback for narration
            async def on_progress(progress):
                if progress.phase.value == "installing":
                    await self._narrator.narrate(NarratorEvent.INSTALLING)
                elif progress.phase.value == "building":
                    await self._narrator.narrate(NarratorEvent.BUILDING)
                elif progress.phase.value == "verifying":
                    await self._narrator.narrate(NarratorEvent.VERIFYING)
            
            # Perform update
            success = await self._update_engine.apply_update()
            
            if success:
                self.stats.total_updates += 1
                logger.info("✅ Update applied successfully")
                
                # Announce success
                version = self._update_engine.get_progress().message or ""
                await self._narrator.narrate(
                    NarratorEvent.UPDATE_COMPLETE,
                    version=version,
                    wait=True,
                )
                
                # Reset crash count on successful update
                self.process_info.crash_count = 0
                return True
            else:
                logger.error("❌ Update failed")
                await self._narrator.narrate(NarratorEvent.UPDATE_FAILED)
                return False
                
        except Exception as e:
            logger.error(f"❌ Update error: {e}")
            return False
    
    async def _handle_rollback_request(self) -> bool:
        """
        Handle a rollback request (exit code 101).
        
        Returns:
            True if rollback successful, False if failed
        """
        self._set_state(SupervisorState.ROLLING_BACK)
        logger.info("🔄 Rollback requested by JARVIS")
        
        # Announce rollback starting
        await self._narrator.narrate(NarratorEvent.ROLLBACK_STARTING, wait=True)
        
        if not self._rollback_manager:
            logger.error("❌ Rollback manager not initialized")
            return False
        
        try:
            success = await self._rollback_manager.rollback()
            
            if success:
                self.stats.total_rollbacks += 1
                logger.info("✅ Rollback completed successfully")
                await self._narrator.narrate(NarratorEvent.ROLLBACK_COMPLETE, wait=True)
                return True
            else:
                logger.error("❌ Rollback failed")
                return False
                
        except Exception as e:
            logger.error(f"❌ Rollback error: {e}")
            return False
    
    async def _run_update_detector(self) -> None:
        """
        Background task: Intelligent update detection and multi-modal notification.
        
        This method runs continuously in the background, checking for updates
        and delivering notifications through multiple channels:
        - Voice (TTS) announcements for immediate awareness
        - WebSocket broadcasts for frontend badge/modal display
        - Console logging for developer visibility
        
        Features:
        - Intelligent deduplication (same update = one notification)
        - Priority-based delivery (security updates are urgent)
        - User activity awareness (configurable interrupt behavior)
        - Rich changelog summaries for meaningful notifications
        """
        if not self._update_detector:
            logger.info("📭 Update detector not enabled")
            return
        
        if not self._notification_orchestrator:
            logger.warning("⚠️ Notification orchestrator not initialized")
            return
        
        logger.info("🔍 Update detector started - checking every "
                   f"{self.config.update.check.interval_seconds}s")
        
        # Track consecutive failures for backoff
        consecutive_failures = 0
        max_failures_before_backoff = 3
        backoff_multiplier = 1.0
        
        while not self._shutdown_event.is_set():
            try:
                # Perform update check via orchestrator (handles notification internally)
                result = await self._notification_orchestrator.check_and_notify()
                
                if result and result.success:
                    # Reset failure counter on success
                    consecutive_failures = 0
                    backoff_multiplier = 1.0
                    
                    # Trigger legacy callbacks for extensibility
                    for callback in self._on_update_available:
                        try:
                            callback()
                        except Exception as e:
                            logger.error(f"Update available callback error: {e}")
                    
                    # If update is available but requires confirmation,
                    # we're done until user acts or reminder interval passes
                    if self.config.update.require_confirmation:
                        logger.debug("📫 Waiting for user action on update")
                
            except asyncio.CancelledError:
                logger.info("🛑 Update detector cancelled")
                break
            except Exception as e:
                consecutive_failures += 1
                logger.warning(f"Update check failed ({consecutive_failures}x): {e}")
                
                # Exponential backoff on repeated failures
                if consecutive_failures >= max_failures_before_backoff:
                    backoff_multiplier = min(backoff_multiplier * 1.5, 4.0)
                    logger.info(f"📉 Backing off update checks (multiplier: {backoff_multiplier:.1f}x)")
            
            # Wait for next check interval (with backoff if needed)
            wait_time = self.config.update.check.interval_seconds * backoff_multiplier
            
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=wait_time,
                )
                # If we get here, shutdown was signaled
                break
            except asyncio.TimeoutError:
                # Normal timeout - continue checking
                pass
        
        logger.info("🔍 Update detector stopped")
    
    async def _run_idle_detector(self) -> None:
        """Background task: Monitor for idle state and trigger silent updates."""
        if not self._idle_detector or not self.config.idle.enabled:
            return
        
        while not self._shutdown_event.is_set():
            try:
                is_idle = await self._idle_detector.is_system_idle()
                
                if is_idle and self.config.idle.silent_update_enabled:
                    # Check if update is available
                    if self._update_detector:
                        update_info = await self._update_detector.check_for_updates()
                        
                        if update_info and update_info.available:
                            logger.info("😴 System idle with update available - requesting silent update")
                            self._update_requested.set()
                
            except Exception as e:
                logger.warning(f"Idle detection error: {e}")
            
            await asyncio.sleep(60)  # Check every minute
    
    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._signal_handler, sig)
        
        logger.info("🛡️ Signal handlers registered")
    
    def _signal_handler(self, sig: signal.Signals) -> None:
        """Handle shutdown signals."""
        logger.info(f"📡 Received {sig.name}")
        self._shutdown_event.set()
        
        # Forward signal to child process
        if self._process and self._process.returncode is None:
            try:
                self._process.send_signal(sig)
                logger.info(f"📡 Forwarded {sig.name} to JARVIS (PID: {self._process.pid})")
            except ProcessLookupError:
                pass
    
    async def run(self) -> None:
        """
        Main supervisor loop.
        
        Runs until shutdown is requested. Handles all exit codes
        and coordinates updates/rollbacks.
        
        Features:
        - Intelligent voice narration during startup
        - Visual loading page with progress
        - Update detection and notification
        - Automatic recovery from crashes
        """
        if not self.config.enabled:
            logger.warning("⚠️ Supervisor is disabled in config")
            return
        
        logger.info("🚀 Starting JARVIS Supervisor")
        self.stats.supervisor_start_time = datetime.now()
        
        # Start narrator
        await self._narrator.start()
        
        # Announce supervisor online (uses main narrator)
        await self._narrator.narrate(NarratorEvent.SUPERVISOR_START, wait=True)
        
        # Initialize components
        await self._init_components()
        
        # Setup signal handlers
        self._setup_signal_handlers()
        
        # Start background tasks
        tasks = []
        if self.config.update.check.enabled and self._update_detector:
            tasks.append(asyncio.create_task(self._run_update_detector()))
        if self.config.idle.enabled and self._idle_detector:
            tasks.append(asyncio.create_task(self._run_idle_detector()))
        
        # Get exit codes from config
        exit_codes = ExitCode.from_config(self.config)
        
        try:
            while not self._shutdown_event.is_set():
                # Check for pending update request from idle detector
                if self._update_requested.is_set():
                    self._update_requested.clear()
                    await self._handle_update_request()
                
                # Spawn JARVIS and wait for exit
                exit_code = await self._spawn_jarvis()
                
                # Handle exit code
                if exit_code == exit_codes["clean"]:
                    logger.info("✅ JARVIS shut down cleanly")
                    break
                    
                elif exit_code == exit_codes["update"]:
                    success = await self._handle_update_request()
                    if not success:
                        logger.warning("⚠️ Update failed, restarting without update")
                    # Always restart after update attempt
                    continue
                    
                elif exit_code == exit_codes["rollback"]:
                    success = await self._handle_rollback_request()
                    if not success:
                        logger.error("❌ Rollback failed, stopping supervisor")
                        break
                    continue
                    
                elif exit_code == exit_codes["restart"]:
                    logger.info("🔄 Restart requested")
                    self._set_state(SupervisorState.RESTARTING)
                    await self._narrator.narrate(NarratorEvent.RESTART_STARTING)
                    continue
                    
                else:
                    # Crash or unknown exit code
                    should_retry = await self._handle_crash(exit_code)
                    if not should_retry:
                        break
        
        finally:
            # Cleanup
            self._set_state(SupervisorState.SHUTTING_DOWN)
            
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            self._set_state(SupervisorState.STOPPED)
            logger.info("👋 Supervisor stopped")
    
    async def request_update(self) -> None:
        """Request an update check and apply."""
        self._update_requested.set()
    
    async def request_shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_event.set()
        
        if self._process and self._process.returncode is None:
            self._process.send_signal(signal.SIGTERM)
    
    def on_state_change(self, callback: Callable[[SupervisorState], None]) -> None:
        """Register a state change callback."""
        self._on_state_change.append(callback)
    
    def on_crash(self, callback: Callable[[int], None]) -> None:
        """Register a crash callback."""
        self._on_crash.append(callback)
    
    def on_update_available(self, callback: Callable[[], None]) -> None:
        """Register an update available callback."""
        self._on_update_available.append(callback)
    
    def get_stats(self) -> dict[str, Any]:
        """Get supervisor statistics."""
        return {
            "state": self.state.value,
            "process": {
                "pid": self.process_info.pid,
                "uptime_seconds": self.process_info.uptime_seconds,
                "crash_count": self.process_info.crash_count,
                "is_stable": self.process_info.is_stable(self.config.health.boot_stability_window),
            },
            "stats": {
                "total_starts": self.stats.total_starts,
                "total_crashes": self.stats.total_crashes,
                "total_updates": self.stats.total_updates,
                "total_rollbacks": self.stats.total_rollbacks,
                "supervisor_uptime_seconds": (
                    datetime.now() - self.stats.supervisor_start_time
                ).total_seconds(),
            },
        }


async def run_supervisor() -> None:
    """Entry point for running the supervisor."""
    supervisor = JARVISSupervisor()
    await supervisor.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    asyncio.run(run_supervisor())
