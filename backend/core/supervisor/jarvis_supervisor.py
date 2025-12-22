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
from .unified_voice_orchestrator import (
    get_voice_orchestrator,
    UnifiedVoiceOrchestrator,
    SpeechTopic,
    VoicePriority,
)
# v5.0: Unified Startup Voice Coordinator (coordinates narrator + announcer)
from .unified_startup_voice_coordinator import (
    UnifiedStartupVoiceCoordinator,
    get_startup_voice_coordinator,
    CoordinatorState,
)
from .update_notification import (
    UpdateNotificationOrchestrator,
    NotificationChannel,
    NotificationPriority,
    get_notification_orchestrator,
)
from .restart_coordinator import (
    get_restart_coordinator,
    RestartCoordinator,
    RestartRequest,
    RestartSource,
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


# Import the unified progress hub (single source of truth)
def _get_progress_hub():
    """Get the unified progress hub instance."""
    try:
        from backend.core.unified_startup_progress import get_progress_hub
        return get_progress_hub()
    except ImportError:
        return None

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
        skip_browser_open: bool = False,
    ):
        """
        Initialize the JARVIS Supervisor.
        
        Args:
            config: Supervisor configuration (loads from YAML if None)
            jarvis_entry_point: Path to JARVIS entry point (default: start_system.py)
            skip_browser_open: If True, skip opening browser (used when run_supervisor.py already opened it)
        """
        self.config = config or get_supervisor_config()
        self.jarvis_entry_point = jarvis_entry_point or self._find_entry_point()
        self._skip_browser_open = skip_browser_open
        
        self.state = SupervisorState.INITIALIZING
        self.process_info = ProcessInfo()
        self.stats = SupervisorStats()
        
        self._process: Optional[asyncio.subprocess.Process] = None
        self._shutdown_event = asyncio.Event()
        self._update_requested = asyncio.Event()
        self._rollback_requested = asyncio.Event()
        self._restart_requested = asyncio.Event()

        # Restart coordinator for async-safe restart signaling
        self._restart_coordinator: RestartCoordinator = get_restart_coordinator()

        # Callbacks for extensibility
        self._on_state_change: list[Callable[[SupervisorState], None]] = []
        self._on_crash: list[Callable[[int], None]] = []
        self._on_update_available: list[Callable[[], None]] = []
        
        # v2.0: Unified voice orchestrator (single source of truth for ALL voice)
        # This prevents "multiple voices" by ensuring only one `say` process at a time
        self._voice_orchestrator: UnifiedVoiceOrchestrator = get_voice_orchestrator()

        # TTS Narrator for engaging feedback (now delegates to orchestrator)
        self._narrator: SupervisorNarrator = get_narrator()

        # Intelligent startup narrator for phase-aware narration (now delegates to orchestrator)
        self._startup_narrator: IntelligentStartupNarrator = get_startup_narrator()
        
        # v5.0: Unified Startup Voice Coordinator (coordinates narrator + announcer)
        # This ensures both systems work together, sharing context and preventing duplicates
        self._voice_coordinator: Optional[UnifiedStartupVoiceCoordinator] = None

        # Loading page support (uses loading_server.py directly)
        self._progress_reporter: Optional[Any] = None
        self._show_loading_page: bool = True  # Can be disabled via env

        # Unified progress hub (single source of truth for all progress tracking)
        self._progress_hub: Optional[Any] = None

        # Components (lazy loaded)
        self._update_engine: Optional[Any] = None
        self._rollback_manager: Optional[Any] = None
        self._health_monitor: Optional[Any] = None
        self._update_detector: Optional[Any] = None
        self._idle_detector: Optional[Any] = None
        self._notification_orchestrator: Optional[UpdateNotificationOrchestrator] = None
        
        # Dead Man's Switch - Post-update stability verification
        self._dead_man_switch: Optional[DeadManSwitch] = None
        self._is_post_update: bool = False  # Track if this is a post-update boot
        self._pending_update_commit: Optional[str] = None  # Commit we just updated to
        self._previous_commit: Optional[str] = None  # Commit we're replacing
        self._dms_rollback_decision: Optional[RollbackDecision] = None  # Pending rollback from DMS

        # v5.0: Intelligence Component Manager - Orchestrates all intelligence providers
        # Manages: Network Context, Pattern Tracker, Device Monitor, Fusion Engine, Learning Coordinator (RAG+RLHF)
        self._intelligence_manager: Optional[Any] = None

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
            from .rollback_manager import (
            RollbackManager,
            DeadManSwitch,
            ProbationState,
            RollbackDecision,
        )
            self._rollback_manager = RollbackManager(self.config)
            await self._rollback_manager.initialize()
        
        if self._health_monitor is None:
            from .health_monitor import HealthMonitor
            self._health_monitor = HealthMonitor(self.config)
        
        if self._update_detector is None and self.config.update.check.enabled:
            from .update_detector import UpdateDetector
            self._update_detector = UpdateDetector(self.config)
            # v2.0: Initialize baseline for local change awareness
            await self._update_detector.initialize_baseline()
            # Register callback for local change notifications
            self._update_detector.on_local_change(self._on_local_change_detected)
        
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
        
        # Initialize Dead Man's Switch for post-update stability verification
        if self._dead_man_switch is None and self.config.dead_man_switch.enabled:
            self._dead_man_switch = DeadManSwitch(
                config=self.config,
                rollback_manager=self._rollback_manager,
                narrator=self._narrator,
            )
            
            # Register callbacks
            self._dead_man_switch.on_rollback(self._on_dms_rollback)
            self._dead_man_switch.on_stable(self._on_dms_stable)

            logger.info("🎯 Dead Man's Switch initialized")

        # v5.0: Initialize Intelligence Component Manager (async/parallel component orchestration)
        if self._intelligence_manager is None:
            try:
                from intelligence.intelligence_component_manager import get_intelligence_manager

                # Create progress callback for unified progress hub integration
                def intelligence_progress_callback(component_name: str, progress: float):
                    """Report intelligence component initialization progress."""
                    if self._progress_hub:
                        try:
                            # Map component names to friendly display names
                            display_names = {
                                'network_context': 'Network Context Intelligence',
                                'pattern_tracker': 'Unlock Pattern Intelligence',
                                'device_monitor': 'Device State Intelligence',
                                'fusion_engine': 'Multi-Factor Fusion Engine',
                                'learning_coordinator': 'RAG + RLHF Learning System'
                            }
                            display_name = display_names.get(component_name, component_name)

                            # Report to progress hub
                            if progress >= 1.0:
                                asyncio.create_task(
                                    self._progress_hub.component_complete(
                                        f"intelligence_{component_name}",
                                        f"{display_name} ready"
                                    )
                                )
                            else:
                                asyncio.create_task(
                                    self._progress_hub.component_start(
                                        f"intelligence_{component_name}",
                                        display_name
                                    )
                                )
                        except Exception as e:
                            logger.debug(f"Progress hub update error: {e}")

                # Get intelligence manager with progress callback
                self._intelligence_manager = await get_intelligence_manager(
                    progress_callback=intelligence_progress_callback
                )

                # Initialize all intelligence components (async/parallel)
                health_status = await self._intelligence_manager.initialize()

                # Log summary
                summary = self._intelligence_manager.get_summary()
                logger.info(
                    f"🧠 Intelligence system initialized: {summary['ready']}/{summary['total_components']} "
                    f"components ready"
                )

                # Detailed health status
                for component_name, health in health_status.items():
                    status_emoji = {
                        'ready': '✅',
                        'degraded': '⚠️',
                        'failed': '❌',
                        'initializing': '⏳'
                    }.get(health['status'], '❓')

                    logger.info(f"  {status_emoji} {component_name}: {health['status']}")

            except Exception as e:
                logger.warning(f"⚠️ Intelligence Component Manager initialization failed: {e}")
                # Continue without intelligence - graceful degradation
                self._intelligence_manager = None
    
    # Browser lock file - shared with run_supervisor.py
    BROWSER_LOCK_FILE = Path("/tmp/jarvis_browser.lock")
    
    def _is_browser_locked(self) -> bool:
        """Check if another process holds the browser lock."""
        try:
            if self.BROWSER_LOCK_FILE.exists():
                lock_age = time.time() - self.BROWSER_LOCK_FILE.stat().st_mtime
                # If lock is recent (within 30 seconds), someone else is managing browser
                if lock_age < 30:
                    return True
            return False
        except Exception:
            return False
    
    async def _close_all_jarvis_windows(self) -> int:
        """
        Close ALL Chrome incognito windows + JARVIS-related regular windows.
        
        v5.0: Check browser lock first - if locked, skip (another process is handling it)
        
        Returns:
            Number of windows closed
        """
        # Check if browser is being managed by another process
        if self._is_browser_locked():
            logger.info("🔒 Browser management locked by run_supervisor.py - skipping")
            return 0
        
        total_closed = 0
        
        try:
            applescript = '''
            tell application "System Events"
                if not (exists process "Google Chrome") then
                    return 0
                end if
            end tell
            
            tell application "Google Chrome"
                set jarvisPatterns to {"localhost:3000", "localhost:3001", "localhost:8010", "127.0.0.1:3000", "127.0.0.1:3001", "127.0.0.1:8010"}
                set closedCount to 0
                
                set windowCount to count of windows
                repeat with i from windowCount to 1 by -1
                    try
                        set w to window i
                        set shouldClose to false
                        
                        if mode of w is "incognito" then
                            set shouldClose to true
                        else
                            repeat with t in tabs of w
                                set tabURL to URL of t
                                repeat with pattern in jarvisPatterns
                                    if tabURL contains pattern then
                                        set shouldClose to true
                                        exit repeat
                                    end if
                                end repeat
                                if shouldClose then exit repeat
                            end repeat
                        end if
                        
                        if shouldClose then
                            close w
                            set closedCount to closedCount + 1
                            delay 0.2
                        end if
                    end try
                end repeat
                
                return closedCount
            end tell
            '''
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            
            try:
                total_closed = int(stdout.decode().strip() or "0")
            except ValueError:
                pass
                    
        except Exception as e:
            logger.debug(f"Could not close existing windows: {e}")
        
        if total_closed > 0:
            logger.info(f"🗑️ Closed {total_closed} existing JARVIS window(s)")
            await asyncio.sleep(1.0)
        
        return total_closed

    async def _open_loading_page(self) -> bool:
        """
        Open browser to loading page at localhost:3001.
        
        v5.0: Check browser lock first - if locked, skip since
        run_supervisor.py is already managing the browser.
        
        This ensures exactly one browser window for JARVIS.
        """
        loading_url = "http://localhost:3001/"

        # Check if browser is being managed by run_supervisor.py
        if self._is_browser_locked():
            logger.info("🔒 Browser managed by run_supervisor.py - skipping _open_loading_page")
            return True  # Return True since browser IS open, just not by us

        try:
            # Step 1: Close all existing JARVIS windows
            closed_count = await self._close_all_jarvis_windows()
            
            if closed_count > 0:
                await asyncio.sleep(1.0)  # Let Chrome process the closures
                logger.info(f"🧹 Cleaned up {closed_count} existing JARVIS window(s)")
            
            # Step 2: Open fresh incognito window with fullscreen
            applescript = f'''
            tell application "Google Chrome"
                set newWindow to make new window with properties {{mode:"incognito"}}
                delay 0.5
                tell newWindow
                    set URL of active tab to "{loading_url}"
                end tell
                set index of newWindow to 1
                activate
            end tell
            
            -- Enter fullscreen mode (more reliable via menu)
            delay 1.0
            tell application "System Events"
                tell process "Google Chrome"
                    set frontmost to true
                    delay 0.3
                    try
                        click menu item "Enter Full Screen" of menu "View" of menu bar 1
                    on error
                        keystroke "f" using {{command down, control down}}
                    end try
                end tell
            end tell
            
            return true
            '''
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            
            if stdout.decode().strip().lower() == "true":
                logger.info(f"🌐 Opened single JARVIS window: {loading_url}")
                return True
            
            # Fallback to command line
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-na", "Google Chrome",
                "--args", "--incognito", "--new-window", "--start-fullscreen", loading_url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            logger.info(f"🌐 Opened loading page (fallback): {loading_url}")
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
        
        # v5.0: Initialize and start the unified voice coordinator
        # This coordinates both startup_narrator and intelligent_startup_announcer
        try:
            self._voice_coordinator = await get_startup_voice_coordinator()
            await self._voice_coordinator.start_startup()
            logger.info("✅ Unified startup voice coordinator active")
        except Exception as e:
            logger.warning(f"Voice coordinator unavailable, using narrator only: {e}")
            self._voice_coordinator = None
        
        # Start the startup narrator (fallback if coordinator not available)
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

                # Initialize unified progress hub (single source of truth)
                # The hub pre-registers ALL components with their weights upfront
                # to ensure the denominator is fixed and progress increases smoothly.
                self._progress_hub = _get_progress_hub()
                if self._progress_hub:
                    await self._progress_hub.initialize(
                        loading_server_url="http://localhost:3001",
                        required_components=["backend", "frontend", "voice", "vision"]
                    )

                    # v19.7.0: Register narrator callback for automatic milestone announcements
                    # This connects the progress hub to the startup narrator so that
                    # progress milestones (25%, 50%, 75%, 100%) are announced automatically.
                    self._progress_hub.set_narrator_callback(self._startup_narrator.hub_callback)
                    logger.info("📢 Narrator connected to progress hub for auto-announcements")

                    # Mark supervisor as starting (already pre-registered in hub.initialize)
                    await self._progress_hub.component_start("supervisor", "Supervisor initializing...")

                # Report progress with detailed log entry
                await self._progress_reporter.report(
                    "supervisor_init",
                    "Supervisor initializing...",
                    5,
                    log_entry="Supervisor process started and initializing components",
                    log_source="Supervisor",
                    log_type="info"
                )
                # NOTE: Don't announce SUPERVISOR_INIT here - run() already did it via _narrator

                # Open browser to loading page (only on first start AND if not already opened)
                # CRITICAL: Skip if run_supervisor.py already opened the browser
                # v3.1: Use explicit flag (more reliable than env var timing)
                supervisor_already_opened = (
                    self._skip_browser_open or 
                    os.environ.get("JARVIS_SUPERVISOR_LOADING") == "1"
                )
                
                if self.stats.total_starts == 0 and not supervisor_already_opened:
                    await self._open_loading_page()
                    logger.info("🌐 Browser opened by jarvis_supervisor")
                elif supervisor_already_opened:
                    logger.info("📡 Browser already opened by run_supervisor.py, skipping _open_loading_page()")

            except Exception as e:
                logger.warning(f"⚠️ Loading page unavailable: {e}")
                self._progress_reporter = None
                self._progress_hub = None
        
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

        # === SPAWNING PHASE ===
        # Mark spawning as started in the hub (single source of truth)
        if self._progress_hub:
            await self._progress_hub.component_start("spawning", "Starting JARVIS Core...")

        # Get progress from hub for reporter
        spawn_progress = self._progress_hub.get_progress() if self._progress_hub else 10

        # Visual + Voice happen at the SAME moment, BEFORE process creation
        if self._progress_reporter:
            await self._progress_reporter.report(
                "spawning",
                "Starting JARVIS Core...",
                spawn_progress,
                log_entry=f"Spawning JARVIS process: {' '.join(cmd[:2])}...",
                log_source="Supervisor",
                log_type="info"
            )

        # Voice: Announce spawning NOW (aligned with visual)
        await self._startup_narrator.announce_phase(
            StartupPhase.SPAWNING,
            "Starting JARVIS Core...",
            spawn_progress,
            context="start",
        )

        try:
            # Actually create the process (voice + visual already announced)
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
            logger.info(f"✅ JARVIS spawned (PID: {self._process.pid})")
            
            # ═══════════════════════════════════════════════════════════════════
            # DEAD MAN'S SWITCH: Start probation monitoring for post-update boots
            # ═══════════════════════════════════════════════════════════════════
            dms_task = None
            if self._is_post_update and self._dead_man_switch and self.config.dead_man_switch.enabled:
                logger.info("🎯 Starting Dead Man's Switch probation (post-update boot)")
                dms_task = asyncio.create_task(self._run_dead_man_switch())

            # Mark spawning AND supervisor as complete in the hub
            # Supervisor's job is done once the process is spawned
            if self._progress_hub:
                await self._progress_hub.component_complete("spawning", "JARVIS Core started")
                await self._progress_hub.component_complete("supervisor", "Supervisor ready")
                # Also mark early-stage components that don't have explicit tracking
                await self._progress_hub.component_complete("cleanup", "Cleanup complete")
                await self._progress_hub.component_complete("config", "Configuration loaded")

            # NOTE: Do NOT announce "JARVIS online" here - that's premature!
            # The startup narrator will announce completion when ALL systems are ready
            # (backend, frontend, voice, vision) in _monitor_startup_progress()
            
            # Start health monitoring with loading page progress
            if self._health_monitor:
                asyncio.create_task(self._monitor_health())
            
            # Start loading progress monitor - this handles ALL startup narration
            # and will announce "JARVIS online" when truly ready
            if self._progress_reporter:
                asyncio.create_task(self._monitor_startup_progress())
            else:
                # No loading page - announce ready after a brief startup period
                # This fallback ensures we still narrate when running without loading page
                asyncio.create_task(self._announce_ready_fallback())
            
            # Wait for process to exit
            exit_code = await self._process.wait()
            
            self.process_info.last_exit_code = exit_code
            self.process_info.uptime_seconds = (
                datetime.now() - self.process_info.start_time
            ).total_seconds()
            
            logger.info(f"📋 JARVIS exited with code {exit_code} (uptime: {self.process_info.uptime_seconds:.1f}s)")
            
            # ═══════════════════════════════════════════════════════════════════
            # DEAD MAN'S SWITCH: Handle exit during probation
            # ═══════════════════════════════════════════════════════════════════
            if dms_task and not dms_task.done():
                # Cancel the probation loop since JARVIS exited
                dms_task.cancel()
                try:
                    await dms_task
                except asyncio.CancelledError:
                    pass
                
                # If this was a crash during probation, handle it specially
                if self._dead_man_switch and self._dead_man_switch.is_probation_active():
                    if exit_code != 0:  # Crash or error
                        logger.warning(f"💥 JARVIS crashed during Dead Man's Switch probation!")
                        decision = await self._dead_man_switch.handle_crash(exit_code)
                        if decision.should_rollback:
                            logger.info(f"🔄 Dead Man's Switch recommends rollback: {decision.reason}")
                            # Store the decision for the main loop to handle
                            self._dms_rollback_decision = decision
            
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
        Intelligent, robust startup monitoring with parallel health checks.
        
        Features:
        - Parallel health checks for backend/frontend (async)
        - Adaptive timeout based on system state
        - Intelligent retry with exponential backoff
        - Graceful degradation (frontend-optional mode)
        - Real-time progress tracking with visual+voice sync
        - Circuit breaker for failed endpoints
        - Dynamic progress interpolation for smooth UX
        """
        if not self._progress_reporter:
            return
        
        import aiohttp
        from dataclasses import dataclass, field
        from typing import Tuple
        
        # === DYNAMIC CONFIGURATION (no hardcoding) ===
        backend_port = int(os.environ.get("BACKEND_PORT", "8010"))
        frontend_port = int(os.environ.get("FRONTEND_PORT", "3000"))
        base_timeout = float(os.environ.get("STARTUP_TIMEOUT", "180"))  # 3 minutes default
        health_check_timeout = float(os.environ.get("HEALTH_CHECK_TIMEOUT", "3.0"))
        slow_threshold = float(os.environ.get("STARTUP_SLOW_THRESHOLD", "45.0"))
        poll_interval = float(os.environ.get("STARTUP_POLL_INTERVAL", "0.5"))
        # CRITICAL: Default to waiting for frontend since loading page expects to redirect to it
        # Only set FRONTEND_OPTIONAL=true for headless/no-browser mode
        frontend_optional = os.environ.get("FRONTEND_OPTIONAL", "false").lower() == "true"
        
        backend_url = f"http://localhost:{backend_port}"
        frontend_url = f"http://localhost:{frontend_port}"
        
        # === INTELLIGENT STATE TRACKING ===
        @dataclass
        class HealthCheckState:
            """Track health check state with circuit breaker logic."""
            consecutive_failures: int = 0
            last_success: float = 0.0
            is_ready: bool = False
            circuit_open: bool = False
            check_count: int = 0
            
            def record_success(self):
                self.consecutive_failures = 0
                self.last_success = time.time()
                self.is_ready = True
                self.circuit_open = False
                self.check_count += 1
            
            def record_failure(self):
                self.consecutive_failures += 1
                self.check_count += 1
                # Open circuit after 10 consecutive failures
                if self.consecutive_failures >= 10:
                    self.circuit_open = True
            
            def should_check(self) -> bool:
                """Circuit breaker: skip checks if circuit is open."""
                if self.is_ready:
                    return False  # Already ready, no need to check
                if self.circuit_open:
                    # Half-open: try again after 5 seconds
                    return time.time() - self.last_success > 5.0
                return True
        
        backend_state = HealthCheckState()
        frontend_state = HealthCheckState()
        
        # Progress stages with dynamic weighting
        # Include spawning as already completed (announced in _spawn_jarvis)
        stages_completed: set = {"spawning"}  # Start with spawning done
        key_milestones_narrated: set = {"spawning"}  # Already announced in _spawn_jarvis
        
        start_time = time.time()
        slow_startup_announced = False
        last_progress_update = start_time
        
        # Create a shared session for connection pooling
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
        session: Optional[aiohttp.ClientSession] = None
        
        # === PARALLEL HEALTH CHECK FUNCTIONS ===
        async def check_endpoint_smart(
            url: str,
            state: HealthCheckState,
            timeout: float = health_check_timeout
        ) -> bool:
            """Smart health check with circuit breaker and retry logic."""
            nonlocal session
            
            if not state.should_check():
                return state.is_ready
            
            try:
                if session is None or session.closed:
                    session = aiohttp.ClientSession(
                        connector=connector,
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    )
                
                async with session.get(url) as resp:
                    if resp.status == 200:
                        state.record_success()
                        return True
                    else:
                        state.record_failure()
                        return False
            except asyncio.TimeoutError:
                state.record_failure()
                return False
            except aiohttp.ClientError:
                state.record_failure()
                return False
            except Exception as e:
                logger.debug(f"Health check error for {url}: {e}")
                state.record_failure()
                return False
        
        async def check_system_status() -> dict:
            """
            Check detailed system status from backend using /health/startup endpoint.

            This is the SINGLE SOURCE OF TRUTH for backend startup progress.
            The backend's SupervisorProgressBridge updates this endpoint with
            real progress data, and we map it to our progress hub.

            Returns a normalized dict with consistent field names for subsystem tracking.
            """
            try:
                if session is None or session.closed:
                    return {}

                # Primary: Poll /health/startup for detailed progress from progress bridge
                async with session.get(
                    f"{backend_url}/health/startup",
                    timeout=aiohttp.ClientTimeout(total=health_check_timeout)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        components = data.get("components", {})
                        ready_components = set(components.get("ready", []))

                        # Map backend progress to supervisor's expected format
                        return {
                            # Phase and progress from backend
                            "phase": data.get("phase", "UNKNOWN"),
                            "progress": data.get("progress", 0.0),
                            "message": data.get("message", ""),
                            "is_complete": data.get("is_complete", False),

                            # Component status from backend's ready list
                            "database_connected": "database" in ready_components,
                            "voice_ready": "voice" in ready_components,
                            "vision_ready": "vision" in ready_components,
                            "models_ready": "models" in ready_components,
                            "websocket_ready": "websocket" in ready_components,
                            "config_ready": "config" in ready_components,
                            "cleanup_ready": "cleanup" in ready_components,
                            "backend_ready": "backend" in ready_components,

                            # Legacy fields for compatibility
                            "ml_models_ready": "models" in ready_components,
                            "ml_warming_up": data.get("phase") == "MODELS_LOADING",
                            "overall_ready": data.get("full_mode", False),
                        }

            except Exception as e:
                logger.debug(f"System status check failed: {e}")

            # Fallback: Try /health/ready for operational status
            try:
                if session is None or session.closed:
                    return {}

                async with session.get(
                    f"{backend_url}/health/ready",
                    timeout=aiohttp.ClientTimeout(total=health_check_timeout)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        details = data.get("details", {})
                        ml_warmup = details.get("ml_warmup", {})
                        services = data.get("services", {})

                        # Use ACTUAL status from endpoint, no hardcoding!
                        return {
                            "database_connected": details.get("database_connected", False),
                            "voice_ready": details.get("voice_unlock_ready", False) or details.get("speaker_service_ready", False),
                            "vision_ready": details.get("vision_ready", False),
                            "ml_models_ready": details.get("ml_models_ready", False),
                            "ml_warming_up": ml_warmup.get("is_warming_up", False),
                            "websocket_ready": details.get("websocket_ready", False),
                            # CRITICAL: overall_ready is the authoritative readiness flag
                            "overall_ready": data.get("ready", False),
                            "operational": data.get("operational", False),
                            "status": data.get("status", "unknown"),
                            "services_ready": services.get("ready", []),
                            "services_failed": services.get("failed", []),
                        }
            except Exception:
                pass

            return {}
        
        async def parallel_health_check() -> Tuple[bool, bool, dict]:
            """Run backend and frontend health checks in parallel."""
            backend_task = asyncio.create_task(
                check_endpoint_smart(f"{backend_url}/health", backend_state)
            )
            frontend_task = asyncio.create_task(
                check_endpoint_smart(frontend_url, frontend_state)
            )
            
            # Wait for both
            backend_ready, frontend_ready = await asyncio.gather(
                backend_task, frontend_task, return_exceptions=True
            )
            
            # Handle exceptions
            if isinstance(backend_ready, Exception):
                backend_ready = False
            if isinstance(frontend_ready, Exception):
                frontend_ready = False
            
            # Get system status if backend is ready
            system_status = {}
            if backend_ready:
                system_status = await check_system_status()
            
            return backend_ready, frontend_ready, system_status
        
        # === PROGRESS CALCULATION (uses hub as single source of truth) ===
        def get_progress() -> int:
            """
            Get progress from the unified hub (single source of truth).

            The hub pre-registers all components with their weights upfront,
            so the denominator is fixed and progress increases smoothly.
            """
            if self._progress_hub:
                return int(self._progress_hub.get_progress())
            # Fallback if hub not available
            return min(len(stages_completed) * 15, 100)
        
        def get_adaptive_timeout() -> float:
            """Adjust timeout based on what's already loaded."""
            # If backend is ready, we're close - extend timeout
            if backend_state.is_ready:
                return base_timeout * 1.5
            return base_timeout
        
        # === MAIN MONITORING LOOP ===
        try:
            while self.state == SupervisorState.RUNNING and self._progress_reporter:
                elapsed = time.time() - start_time
                adaptive_timeout = get_adaptive_timeout()
                
                # Check for slow startup
                if not slow_startup_announced and elapsed > slow_threshold:
                    slow_startup_announced = True
                    key_milestones_narrated.add("slow")
                    await self._startup_narrator.announce_slow_startup()
                
                # Check for timeout
                if elapsed > adaptive_timeout:
                    # ═══════════════════════════════════════════════════════════════════
                    # INTELLIGENT TIMEOUT HANDLING
                    # ═══════════════════════════════════════════════════════════════════
                    # Timeout is a warning, not necessarily a failure. Let the completion
                    # check below determine if we should complete or continue waiting.
                    #
                    # Key principle: Only FAIL if nothing is working. If services are
                    # still initializing, let them continue.
                    # ═══════════════════════════════════════════════════════════════════

                    if not backend_state.is_ready and not frontend_state.is_ready:
                        # Neither backend nor frontend ready - actual failure
                        logger.warning(f"⚠️ Startup timeout after {elapsed:.1f}s - no services responding")
                        await self._progress_reporter.fail(
                            f"Startup timeout after {int(elapsed)}s",
                            error=f"Backend: not ready, Frontend: not ready"
                        )
                        await self._startup_narrator.announce_error("Startup timeout - no services responding")
                        break

                    # Backend or frontend is responding - don't fail, just log and continue
                    # The completion check below will handle the logic
                    if elapsed > adaptive_timeout * 1.5:
                        # Extended timeout warning
                        logger.warning(
                            f"⏰ Extended startup time: {elapsed:.0f}s "
                            f"(backend={'ready' if backend_state.is_ready else 'starting'}, "
                            f"frontend={'ready' if frontend_state.is_ready else 'starting'})"
                        )
                
                # === PARALLEL HEALTH CHECKS ===
                try:
                    backend_ready, frontend_ready, system_status = await parallel_health_check()
                    
                    # === UPDATE PROGRESS BASED ON STATE ===
                    
                    # Backend
                    if backend_ready and "backend" not in stages_completed:
                        stages_completed.add("backend")

                        # Update unified hub (single source of truth)
                        # NOTE: Component is pre-registered, just mark as complete
                        if self._progress_hub:
                            await self._progress_hub.component_complete("backend", "Backend API online!")

                        progress = get_progress()  # Get from hub after update

                        # Visual + Voice aligned with detailed log
                        await self._progress_reporter.report(
                            "api",
                            "Backend API online!",
                            progress,
                            log_entry=f"Backend API responding on port {backend_port}",
                            log_source="Backend",
                            log_type="success"
                        )

                        if "backend" not in key_milestones_narrated:
                            key_milestones_narrated.add("backend")
                            await self._startup_narrator.announce_phase(
                                StartupPhase.BACKEND_INIT,
                                "Backend API online!",
                                progress,
                                context="complete",
                            )
                    
                    # System subsystems - mark complete based on ACTUAL status from /health/ready
                    # CRITICAL: Default to False (not ready), not True!
                    # Progress should reflect REAL backend state, not assumptions
                    if backend_ready:
                        # Extract status from system_status (from /health/ready endpoint)
                        # Only mark components ready when explicitly confirmed
                        database_ready = system_status.get("database_connected", False) if system_status else False
                        voice_ready_status = system_status.get("voice_ready", False) if system_status else False
                        vision_ready_status = system_status.get("vision_ready", False) if system_status else False
                        ml_models_ready = system_status.get("ml_models_ready", False) if system_status else False
                        ml_warming_up = system_status.get("ml_warming_up", False) if system_status else False

                        # Database: SQLite is connected if backend responds
                        if database_ready and "database" not in stages_completed:
                            stages_completed.add("database")
                            if self._progress_hub:
                                await self._progress_hub.component_complete("database", "Database connected")
                            await self._progress_reporter.report(
                                "database",
                                "Database connected",
                                get_progress(),
                                log_entry="SQLite database connection established",
                                log_source="Backend",
                                log_type="success"
                            )

                        # Voice: Wait for actual voice_unlock confirmation
                        if voice_ready_status and "voice" not in stages_completed:
                            stages_completed.add("voice")
                            if self._progress_hub:
                                await self._progress_hub.component_complete("voice", "Voice system ready")
                            await self._progress_reporter.report(
                                "voice",
                                "Voice system ready",
                                get_progress(),
                                log_entry="Voice recognition and TTS engines initialized",
                                log_source="Backend",
                                log_type="success"
                            )

                        # Vision: Mark complete if vision status is ready
                        if vision_ready_status and "vision" not in stages_completed:
                            stages_completed.add("vision")
                            if self._progress_hub:
                                await self._progress_hub.component_complete("vision", "Vision system ready")
                            await self._progress_reporter.report(
                                "vision",
                                "Vision system ready",
                                get_progress(),
                                log_entry="Vision pipeline and Claude integration active",
                                log_source="Backend",
                                log_type="success"
                            )

                        # WebSocket: Mark complete when backend is ready (WebSocket is always available when backend is up)
                        if "websocket" not in stages_completed:
                            stages_completed.add("websocket")
                            if self._progress_hub:
                                await self._progress_hub.component_complete("websocket", "WebSocket ready")

                        # Models: Only mark complete when ML models are actually ready (not warming up)
                        if ml_models_ready and not ml_warming_up and "models" not in stages_completed:
                            stages_completed.add("models")
                            if self._progress_hub:
                                await self._progress_hub.component_complete("models", "AI models loaded")
                            await self._progress_reporter.report(
                                "models",
                                "AI models loaded",
                                get_progress(),
                                log_entry="ML models fully initialized and ready",
                                log_source="Backend",
                                log_type="success"
                            )
                        elif ml_warming_up and "models" not in stages_completed:
                            # Models are warming up - start the component but don't complete it
                            if self._progress_hub and "models_started" not in stages_completed:
                                stages_completed.add("models_started")
                                await self._progress_hub.component_start("models", "ML models warming up...")

                    # Frontend
                    if frontend_ready and "frontend" not in stages_completed:
                        stages_completed.add("frontend")
                        # Update unified hub (pre-registered, just mark complete)
                        if self._progress_hub:
                            await self._progress_hub.component_complete("frontend", "Frontend ready!")
                        await self._progress_reporter.report(
                            "frontend",
                            "Frontend ready!",
                            get_progress(),
                            log_entry=f"React frontend serving on port {frontend_port}",
                            log_source="Frontend",
                            log_type="success"
                        )
                    
                    # ═══════════════════════════════════════════════════════════════════
                    # COMPLETION CHECK - Based on ACTUAL operational readiness
                    # ═══════════════════════════════════════════════════════════════════
                    # CRITICAL: We must ensure JARVIS is actually functional before
                    # declaring "ready". False positives mislead users and cause confusion.
                    #
                    # Readiness hierarchy:
                    # 1. is_complete from parallel_initializer (all components done)
                    # 2. overall_ready from /health/ready (services operational)
                    # 3. Fallback only after extended wait with degraded services
                    # ═══════════════════════════════════════════════════════════════════

                    backend_fully_complete = system_status.get("is_complete", False) if system_status else False
                    backend_operationally_ready = system_status.get("overall_ready", False) if system_status else False
                    backend_status = system_status.get("status", "unknown") if system_status else "unknown"

                    # Log operational status for debugging
                    if backend_ready and not backend_operationally_ready:
                        logger.debug(f"Backend HTTP OK but not operationally ready (status={backend_status})")

                    # Primary completion: All systems fully initialized
                    ready_for_completion = (
                        backend_ready and
                        backend_fully_complete and
                        (frontend_ready or frontend_optional)
                    )

                    # ═══════════════════════════════════════════════════════════════════
                    # FALLBACK 1: Backend OPERATIONALLY ready (services functional)
                    # This is the key check - /health/ready reports services are working
                    # ═══════════════════════════════════════════════════════════════════
                    if (not ready_for_completion and
                        backend_ready and
                        backend_operationally_ready and
                        (frontend_ready or frontend_optional)):
                        logger.info(f"✅ Backend operationally ready (status={backend_status}) - completing startup")
                        ready_for_completion = True

                    # ═══════════════════════════════════════════════════════════════════
                    # FALLBACK 2: WebSocket ready + HTTP responding (after 45 seconds)
                    # v4.0: More conservative - wait for WebSocket confirmation
                    # The loading page will do its own verification before redirecting
                    # ═══════════════════════════════════════════════════════════════════
                    websocket_ready = system_status.get("websocket_ready", False) if system_status else False
                    if (not ready_for_completion and
                        backend_ready and
                        websocket_ready and
                        (frontend_ready or frontend_optional) and
                        elapsed > 45):
                        logger.info(f"✅ WebSocket + HTTP ready after {elapsed:.1f}s - completing")
                        ready_for_completion = True

                    # ═══════════════════════════════════════════════════════════════════
                    # FALLBACK 3: Complete when backend is responding (no frontend)
                    # v4.0: For headless mode or slow frontend (after 60 seconds)
                    # ═══════════════════════════════════════════════════════════════════
                    if (not ready_for_completion and
                        backend_ready and
                        frontend_optional and
                        elapsed > 60):
                        services_ready = system_status.get("services_ready", []) if system_status else []
                        services_failed = system_status.get("services_failed", []) if system_status else []

                        # v4.0: More relaxed condition - complete if backend responds
                        # Even if ML models are still warming up, user can interact
                        logger.info(f"✅ Backend + Frontend ready after {elapsed:.1f}s - completing")
                        logger.debug(f"   Services: ready={services_ready}, failed={services_failed}")
                        ready_for_completion = True

                    # ═══════════════════════════════════════════════════════════════════
                    # FALLBACK 4: Last resort timeout (after adaptive_timeout * 1.2)
                    # v5.0: INTELLIGENT completion check - DON'T declare ready if:
                    #   - Status is "unknown" (backend isn't reporting properly)
                    #   - No services are ready (system is clearly still initializing)
                    #   - Progress is below 50% (major components missing)
                    # This prevents false "ready" announcements that confuse users
                    # ═══════════════════════════════════════════════════════════════════
                    if (not ready_for_completion and
                        elapsed > adaptive_timeout * 1.2 and
                        backend_ready and
                        (frontend_ready or frontend_optional)):
                        
                        services_ready = system_status.get('services_ready', []) if system_status else []
                        current_progress = get_progress()
                        
                        # v5.0: Intelligent readiness check - system must show REAL progress
                        is_actually_ready = (
                            # Status must not be unknown
                            backend_status != "unknown" and
                            backend_status != "error" and
                            # Either services are ready OR progress indicates readiness
                            (len(services_ready) > 0 or current_progress >= 50)
                        )
                        
                        if is_actually_ready:
                            logger.warning(f"⏰ Extended timeout ({elapsed:.0f}s) - completing with available services")
                            logger.warning(f"   Status: {backend_status}, Services: {services_ready}, Progress: {current_progress}%")
                            ready_for_completion = True
                        else:
                            # System is NOT actually ready - log but DON'T declare complete
                            # This prevents the "JARVIS is ready" announcement when it's clearly not
                            logger.warning(
                                f"⏰ Extended timeout ({elapsed:.0f}s) but system NOT ready - continuing wait"
                            )
                            logger.warning(
                                f"   Status: {backend_status}, Services: {services_ready}, "
                                f"Progress: {current_progress}% (need >50% or services ready)"
                            )
                            # Extend the timeout to give system more time
                            # Will check again on next iteration

                    # ═══════════════════════════════════════════════════════════════════
                    # FALLBACK 5: HARD timeout - system is stuck (after 2x adaptive_timeout)
                    # v5.0: If we've waited 2x the timeout and system is STILL not ready,
                    # mark as partial completion with clear warning to user
                    # ═══════════════════════════════════════════════════════════════════
                    hard_timeout = adaptive_timeout * 2.0
                    if (not ready_for_completion and
                        elapsed > hard_timeout and
                        backend_ready):  # At least HTTP is responding
                        
                        services_ready = system_status.get('services_ready', []) if system_status else []
                        current_progress = get_progress()
                        
                        logger.error(
                            f"⚠️ HARD TIMEOUT ({elapsed:.0f}s) - System stuck in partial state"
                        )
                        logger.error(
                            f"   Status: {backend_status}, Services: {services_ready}, Progress: {current_progress}%"
                        )
                        
                        # v5.0: Use coordinator for accurate partial completion announcement
                        # Coordinator coordinates both narrator and intelligent announcer
                        if self._voice_coordinator:
                            await self._voice_coordinator.announce_complete(
                                services_ready=services_ready,
                                services_failed=system_status.get('services_failed', []) if system_status else [],
                                duration_seconds=elapsed,
                            )
                        else:
                            # Fallback to narrator only
                            await self._startup_narrator.announce_partial_complete(
                                services_ready=services_ready,
                                services_failed=system_status.get('services_failed', []) if system_status else [],
                                progress=current_progress,
                                duration_seconds=elapsed,
                            )
                        
                        # Mark as partial completion - allows user to interact with available features
                        ready_for_completion = True
                        
                        # Report partial completion to loading page
                        await self._progress_reporter.report(
                            "partial_complete",
                            f"Partial startup - {current_progress}% ready",
                            current_progress,
                            metadata={
                                "partial": True,
                                "services_ready": services_ready,
                                "services_failed": system_status.get('services_failed', []) if system_status else [],
                            }
                        )

                    # If completing without frontend, mark it as skipped
                    if ready_for_completion and not frontend_ready and frontend_optional:
                        if "frontend" not in stages_completed:
                            stages_completed.add("frontend")
                            if self._progress_hub:
                                await self._progress_hub.component_skipped("frontend", "Frontend optional - skipped")

                    if ready_for_completion:
                        await asyncio.sleep(0.3)  # Brief pause for visual effect

                        # Add complete stage
                        stages_completed.add("complete")

                        # Mark unified hub as complete (CRITICAL: must happen BEFORE announcements)
                        # This ensures all systems know we're truly ready
                        if self._progress_hub:
                            await self._progress_hub.mark_complete(True, "JARVIS is online!")

                        # Visual: Complete and redirect
                        await self._progress_reporter.complete(
                            "JARVIS is online!",
                            redirect_url=frontend_url,
                        )

                        # Voice: Final announcement (only AFTER hub is marked complete)
                        # This prevents premature "ready" announcements
                        duration = time.time() - start_time
                        
                        # v5.0: Use coordinator for rich, personalized completion
                        # Coordinator uses intelligent_announcer for context-aware messages
                        if self._voice_coordinator:
                            services_ready = system_status.get('services_ready', []) if system_status else []
                            await self._voice_coordinator.announce_complete(
                                services_ready=services_ready,
                                services_failed=[],  # Full completion = no failures
                                duration_seconds=duration,
                            )
                        else:
                            # Fallback to narrator only
                            await self._startup_narrator.announce_complete(
                                duration_seconds=duration,
                            )

                        # ═══════════════════════════════════════════════════════════════════
                        # CRITICAL: Signal to reload manager that startup is complete
                        # This ends the grace period and enables hot-reload functionality
                        # ═══════════════════════════════════════════════════════════════════
                        os.environ["JARVIS_STARTUP_COMPLETE"] = "true"
                        logger.info("🔓 Startup complete signal sent - hot-reload now active")

                        logger.info(f"✅ Startup complete in {duration:.1f}s")
                        break
                    
                except Exception as e:
                    logger.debug(f"Progress check error: {e}")
                
                # Adaptive polling interval
                await asyncio.sleep(poll_interval)
        
        finally:
            # Cleanup
            if session and not session.closed:
                await session.close()
            await self._startup_narrator.stop()

            # Cleanup unified hub session
            if self._progress_hub:
                try:
                    await self._progress_hub.shutdown()
                except Exception:
                    pass
    
    async def _monitor_health(self) -> None:
        """Monitor JARVIS health while running."""
        while self.state == SupervisorState.RUNNING and self._process:
            await asyncio.sleep(self.config.health.check_interval_seconds)
            
            if self._health_monitor and self._process:
                is_healthy = await self._health_monitor.check_health()
                if not is_healthy:
                    logger.warning("⚠️ Health check failed")
                    # Could trigger graceful restart here
    
    async def _announce_ready_fallback(self) -> None:
        """
        Robust fallback announcement when no loading page is available.
        
        Features:
        - Parallel health checks with connection pooling
        - Adaptive polling with exponential backoff
        - Intelligent retry logic
        - Graceful degradation
        """
        import aiohttp
        
        # Dynamic configuration
        backend_port = int(os.environ.get("BACKEND_PORT", "8010"))
        frontend_port = int(os.environ.get("FRONTEND_PORT", "3000"))
        max_wait = float(os.environ.get("STARTUP_TIMEOUT", "180"))
        
        backend_url = f"http://localhost:{backend_port}"
        frontend_url = f"http://localhost:{frontend_port}"
        
        start_time = time.time()
        announced_spawning = False
        announced_backend = False
        consecutive_failures = 0
        poll_interval = 1.0  # Start with 1 second
        
        await self._startup_narrator.start()
        
        # Create session with connection pooling
        connector = aiohttp.TCPConnector(limit=5)
        
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=3.0)
        ) as session:
            while self.state == SupervisorState.RUNNING:
                elapsed = time.time() - start_time
                
                # Adaptive timeout check
                if elapsed > max_wait:
                    logger.warning(f"⚠️ Startup fallback timeout after {elapsed:.1f}s")
                    await self._startup_narrator.announce_error("Startup timeout")
                    break
                
                # Announce spawning once (after brief delay)
                if not announced_spawning and elapsed > 1.5:
                    announced_spawning = True
                    await self._startup_narrator.announce_phase(
                        StartupPhase.SPAWNING,
                        "Starting JARVIS Core...",
                        15,
                        context="start",
                    )
                
                # Parallel health check
                try:
                    backend_task = asyncio.create_task(
                        session.get(f"{backend_url}/health")
                    )
                    frontend_task = asyncio.create_task(
                        session.get(frontend_url)
                    )
                    
                    results = await asyncio.gather(
                        backend_task, frontend_task,
                        return_exceptions=True
                    )
                    
                    backend_resp, frontend_resp = results
                    
                    # Check backend
                    backend_ready = (
                        not isinstance(backend_resp, Exception) and
                        backend_resp.status == 200
                    )
                    
                    # Check frontend
                    frontend_ready = (
                        not isinstance(frontend_resp, Exception) and
                        frontend_resp.status in (200, 304)
                    )
                    
                    # Close responses
                    if not isinstance(backend_resp, Exception):
                        backend_resp.close()
                    if not isinstance(frontend_resp, Exception):
                        frontend_resp.close()
                    
                    # Announce backend ready
                    if backend_ready and not announced_backend:
                        announced_backend = True
                        await self._startup_narrator.announce_phase(
                            StartupPhase.BACKEND_INIT,
                            "Backend API online!",
                            40,
                            context="complete",
                        )
                    
                    # Complete when both ready (or backend ready after extended wait)
                    if backend_ready and (frontend_ready or elapsed > 60):
                        duration = time.time() - start_time
                        await self._startup_narrator.announce_complete(
                            duration_seconds=duration,
                        )
                        # Signal to reload manager that startup is complete
                        os.environ["JARVIS_STARTUP_COMPLETE"] = "true"
                        logger.info(f"✅ Startup complete in {duration:.1f}s (no loading page)")
                        break
                    
                    # Reset failure counter on any success
                    if backend_ready or frontend_ready:
                        consecutive_failures = 0
                        poll_interval = 1.0
                    else:
                        consecutive_failures += 1
                        # Exponential backoff on failures
                        poll_interval = min(poll_interval * 1.2, 5.0)
                    
                except Exception as e:
                    logger.debug(f"Fallback health check error: {e}")
                    consecutive_failures += 1
                    poll_interval = min(poll_interval * 1.2, 5.0)
                
                await asyncio.sleep(poll_interval)
        
        await self._startup_narrator.stop()
    
    async def _handle_crash(self, exit_code: int) -> bool:
        """
        Handle a crash (exit code != 0, 100, 101, 102).
        
        Integrates with Dead Man's Switch for post-update crash handling.
        
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
        
        # ═══════════════════════════════════════════════════════════════════
        # DEAD MAN'S SWITCH: Priority handling for post-update crashes
        # If DMS flagged this crash for rollback, execute it immediately
        # ═══════════════════════════════════════════════════════════════════
        if self._dms_rollback_decision and self._dms_rollback_decision.should_rollback:
            decision = self._dms_rollback_decision
            self._dms_rollback_decision = None  # Clear the decision
            
            logger.warning(f"🎯 Dead Man's Switch executing rollback: {decision.reason}")
            
            self._set_state(SupervisorState.ROLLING_BACK)
            
            # Voice announcement
            if self._narrator:
                await self._narrator.narrate(NarratorEvent.ROLLBACK_STARTING)
            
            if self._rollback_manager:
                # Try reflog first (more reliable for recent changes)
                success = await self._rollback_manager.rollback_using_reflog(steps=1)
                
                if not success:
                    # Fallback to snapshot-based rollback
                    success = await self._rollback_manager.rollback()
                
                if success:
                    self.stats.total_rollbacks += 1
                    logger.info("✅ Dead Man's Switch rollback complete")
                    
                    # Clear post-update state
                    self._is_post_update = False
                    self._pending_update_commit = None
                    
                    # Reset crash count for the reverted version
                    self.process_info.crash_count = 0
                    
                    if self._narrator:
                        await self._narrator.narrate(NarratorEvent.ROLLBACK_COMPLETE)
                    
                    return True
                else:
                    logger.error("❌ Dead Man's Switch rollback FAILED")
                    # Continue with normal crash handling
        
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
    
    async def _handle_update_request(self, zero_touch: bool = False) -> bool:
        """
        Handle an update request (exit code 100 or Zero-Touch trigger).
        
        This method now integrates with:
        - Dead Man's Switch for post-update stability verification
        - Zero-Touch mode for autonomous updates with full validation
        - Prime Directives for immutable core protection
        
        v2.0 Flow:
        1. (Zero-Touch) Run pre-flight checks
        2. (Zero-Touch) Stage and validate update
        3. Captures current commit BEFORE update
        4. Applies update (with Zero-Touch validation if enabled)
        5. Sets flags for post-update probation
        6. On next boot, Dead Man's Switch monitors stability
        
        Args:
            zero_touch: If True, use full Zero-Touch validation pipeline
        
        Returns:
            True if update successful, False if failed
        """
        # Determine if this should be a Zero-Touch update
        is_zero_touch = zero_touch or self.config.is_zero_touch_enabled
        
        self._set_state(SupervisorState.UPDATING)
        mode_str = "Zero-Touch" if is_zero_touch else "Manual"
        logger.info(f"🔄 Update requested ({mode_str} mode)")
        
        # Broadcast maintenance mode to frontend BEFORE JARVIS shuts down
        try:
            from .maintenance_broadcaster import broadcast_maintenance_mode
            await broadcast_maintenance_mode(
                reason="updating",
                message=f"{'Autonomous' if is_zero_touch else 'Manual'} update in progress...",
                estimated_time=45 if is_zero_touch else 30,  # Zero-Touch takes longer (validation)
            )
        except Exception as e:
            logger.debug(f"Maintenance broadcast failed: {e}")
        
        # Announce update starting via TTS
        if is_zero_touch and self.config.zero_touch.announce_before_update:
            # v4.0: Use topic-aware speak for Zero-Touch
            await self._narrator.speak(
                "Starting autonomous update with full validation.",
                wait=True,
                priority=VoicePriority.HIGH,
                topic=SpeechTopic.ZERO_TOUCH,
            )
        else:
            await self._narrator.narrate(NarratorEvent.UPDATE_STARTING, wait=True)
        
        if not self._update_engine:
            logger.error("❌ Update engine not initialized")
            await self._narrator.narrate(NarratorEvent.UPDATE_FAILED)
            return False
        
        try:
            # ═══════════════════════════════════════════════════════════════════
            # ZERO-TOUCH ONLY: Pre-flight safety checks with enhanced narration
            # ═══════════════════════════════════════════════════════════════════
            if is_zero_touch:
                logger.info("🚀 Running Zero-Touch pre-flight checks...")
                
                # v3.0: Narrate pre-flight initiation
                await self._narrator.narrate(NarratorEvent.ZERO_TOUCH_PRE_FLIGHT)
                
                can_update, reason = await self._update_engine.can_auto_update()
                if not can_update:
                    logger.warning(f"⚠️ Zero-Touch blocked: {reason}")
                    
                    # v3.0: Use enhanced blocked narration
                    await self._narrator.narrate_zero_touch_blocked(reason=reason)
                    return False
                
                # v3.0: Pre-flight passed narration
                await self._narrator.narrate(NarratorEvent.ZERO_TOUCH_PRE_FLIGHT_PASSED)
            
            # ═══════════════════════════════════════════════════════════════════
            # DEAD MAN'S SWITCH INTEGRATION: Capture current commit BEFORE update
            # ═══════════════════════════════════════════════════════════════════
            if self._dead_man_switch and self.config.dead_man_switch.enabled:
                self._previous_commit = await self._update_engine.get_current_version()
                logger.info(f"📸 Captured previous commit: {self._previous_commit[:12] if self._previous_commit else 'unknown'}")
            
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
            
            # ═══════════════════════════════════════════════════════════════════
            # PERFORM UPDATE: With or without Zero-Touch validation
            # ═══════════════════════════════════════════════════════════════════
            result = await self._update_engine.apply_update(zero_touch=is_zero_touch)
            
            # Check if result is a boolean or UpdateResult object
            success = result.success if hasattr(result, 'success') else result
            
            if success:
                self.stats.total_updates += 1
                
                # Log Zero-Touch specific info
                classification_str = None
                files_validated = 0
                
                if hasattr(result, 'was_zero_touch') and result.was_zero_touch:
                    logger.info("✅ Zero-Touch update applied successfully")
                    if hasattr(result, 'classification') and result.classification:
                        classification_str = result.classification.value
                        logger.info(f"   Classification: {classification_str}")
                    if hasattr(result, 'validation_report') and result.validation_report:
                        files_validated = result.validation_report.files_checked
                        logger.info(f"   Validated: {files_validated} files")
                else:
                    logger.info("✅ Update applied successfully")
                
                # ═══════════════════════════════════════════════════════════════════
                # DEAD MAN'S SWITCH: Set up post-update probation
                # ═══════════════════════════════════════════════════════════════════
                if self._dead_man_switch and self.config.dead_man_switch.enabled:
                    self._is_post_update = True
                    self._pending_update_commit = await self._update_engine.get_current_version()
                    logger.info(f"🎯 Dead Man's Switch armed for {self._pending_update_commit[:12] if self._pending_update_commit else 'unknown'}")
                    logger.info(f"   Probation period: {self.config.dead_man_switch.probation_seconds}s")
                
                # ═══════════════════════════════════════════════════════════════════
                # v3.0: Enhanced success narration
                # ═══════════════════════════════════════════════════════════════════
                version = self._update_engine.get_progress().message or ""
                if is_zero_touch and self.config.zero_touch.announce_after_update:
                    # Use enhanced Zero-Touch narration
                    await self._narrator.narrate_zero_touch_complete(
                        success=True,
                        new_version=self._pending_update_commit,
                        duration_seconds=result.duration_seconds if hasattr(result, 'duration_seconds') else 0.0,
                    )
                    
                    # If validation was performed, narrate that too
                    if files_validated > 0:
                        await self._narrator.narrate_zero_touch_validation(
                            files_checked=files_validated,
                        )
                else:
                    await self._narrator.narrate(
                        NarratorEvent.UPDATE_COMPLETE,
                        version=version,
                        wait=True,
                    )
                
                # Note: crash count reset moved to _on_dms_stable callback
                # We only reset after probation passes
                return True
            else:
                error_msg = result.error if hasattr(result, 'error') else "Unknown error"
                logger.error(f"❌ Update failed: {error_msg}")
                
                # ═══════════════════════════════════════════════════════════════════
                # v3.0: Enhanced failure narration
                # ═══════════════════════════════════════════════════════════════════
                if is_zero_touch:
                    # Check for validation failures
                    if hasattr(result, 'validation_report') and result.validation_report:
                        vr = result.validation_report
                        await self._narrator.narrate_zero_touch_validation(
                            files_checked=vr.files_checked,
                            syntax_errors=len(vr.syntax_errors),
                            import_errors=len(vr.import_errors),
                            pip_conflicts=len(vr.pip_conflicts),
                        )
                    
                    await self._narrator.narrate_zero_touch_complete(
                        success=False,
                        error=error_msg,
                    )
                else:
                    await self._narrator.narrate(NarratorEvent.UPDATE_FAILED)
                
                self._is_post_update = False
                return False
                
        except Exception as e:
            logger.error(f"❌ Update error: {e}")
            self._is_post_update = False
            if is_zero_touch:
                # v4.0: Use topic-aware speak for error
                await self._narrator.speak(
                    f"Autonomous update error. {str(e)[:50]}",
                    wait=False,
                    priority=VoicePriority.HIGH,
                    topic=SpeechTopic.ERROR,
                )
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
    
    def _on_dms_rollback(self, decision: RollbackDecision) -> None:
        """
        Callback when Dead Man's Switch triggers a rollback.
        
        This is called BEFORE the rollback is executed, allowing us
        to log and take any necessary actions.
        """
        logger.warning(f"🔄 Dead Man's Switch triggered rollback!")
        logger.info(f"   Reason: {decision.reason}")
        logger.info(f"   Confidence: {decision.confidence:.1%}")
        logger.info(f"   Target: {decision.target_commit[:12] if decision.target_commit else 'unknown'}")
        
        # Reset post-update state
        self._is_post_update = False
        self._pending_update_commit = None
        
        # The rollback itself is handled by DeadManSwitch._execute_rollback
    
    def _on_dms_stable(self, commit: str) -> None:
        """
        Callback when Dead Man's Switch confirms version as stable.
        
        This is called AFTER the probation period completes successfully.
        """
        logger.info(f"✅ Dead Man's Switch: Version {commit[:12]} confirmed stable!")
        
        # Clear post-update state
        self._is_post_update = False
        self._pending_update_commit = None
        self._previous_commit = None
        
        # Reset crash count since this version is now proven stable
        self.process_info.crash_count = 0
    
    async def _run_dead_man_switch(self) -> None:
        """
        Background task: Run Dead Man's Switch probation monitoring.
        
        This runs concurrently with JARVIS and monitors its health
        during the post-update probation period.
        
        v3.0: Now uses enhanced narrator for intelligent DMS status updates.
        """
        if not self._dead_man_switch or not self._is_post_update:
            return
        
        logger.info("🎯 Starting Dead Man's Switch probation monitor...")
        
        # Reset narrator context for fresh DMS tracking
        self._narrator.reset_context()
        
        try:
            # Start probation
            await self._dead_man_switch.start_probation(
                update_commit=self._pending_update_commit or "unknown",
                previous_commit=self._previous_commit or "unknown",
            )
            
            # v3.0: Enhanced narration for DMS start
            await self._narrator.narrate_dms_status(
                state="monitoring",
                probation_remaining=float(self.config.dead_man_switch.probation_seconds),
            )
            
            # Run probation loop with status callback for narration
            self._dead_man_switch.on_status_change(self._on_dms_status_change)
            
            status = await self._dead_man_switch.run_probation_loop()
            
            # Handle result with enhanced narration
            if status.state == ProbationState.ROLLING_BACK:
                # Rollback was triggered - need to restart
                logger.warning("🔄 Dead Man's Switch triggered rollback - restarting...")
                
                # v3.0: Enhanced rollback narration
                await self._narrator.narrate_dms_status(state="rollback")
                
                self._restart_requested.set()
                
                # Terminate current process
                if self._process and self._process.returncode is None:
                    self._process.send_signal(signal.SIGTERM)
                    
            elif status.state == ProbationState.COMMITTED:
                logger.info("✅ Dead Man's Switch: Probation passed!")
                
                # v3.0: Enhanced success narration
                await self._narrator.narrate_dms_status(
                    state="committed",
                    health_score=status.health_score,
                )
                
        except asyncio.CancelledError:
            logger.info("🛑 Dead Man's Switch probation cancelled")
        except Exception as e:
            logger.error(f"❌ Dead Man's Switch error: {e}")
    
    def _on_dms_status_change(self, status: "ProbationStatus") -> None:
        """
        Callback for Dead Man's Switch status changes (v3.0).
        
        Used for real-time narration of DMS state changes.
        """
        # Schedule async narration in event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._narrate_dms_status_async(status))
        except Exception as e:
            logger.debug(f"DMS status narration scheduling failed: {e}")
    
    async def _narrate_dms_status_async(self, status: "ProbationStatus") -> None:
        """Async handler for DMS status change narration."""
        state_str = status.state.value if hasattr(status.state, 'value') else str(status.state)
        
        await self._narrator.narrate_dms_status(
            state=state_str,
            health_score=status.health_score,
            consecutive_failures=status.consecutive_failures,
            probation_remaining=status.remaining_seconds,
        )
    
    async def _on_local_change_detected(self, info: "LocalChangeInfo") -> None:
        """
        Callback for local change awareness (v2.0).

        Triggered when local repository changes are detected:
        - New commits made
        - Code pushed to remote
        - Uncommitted changes
        - Branch switches

        Communicates with user via:
        - Voice (TTS) announcements
        - WebSocket broadcasts for frontend UI (UpdateNotificationBadge)
        - Console logging
        - Auto-restart when recommended
        """
        from .update_detector import ChangeType
        from .narrator import NarratorEvent

        if not info.has_changes:
            return

        logger.info(f"📝 Local change detected: {info.summary}")

        # Use the notification orchestrator for multi-channel delivery
        if self._notification_orchestrator:
            await self._notification_orchestrator.notify_local_changes(info)
        else:
            # Fallback: Direct voice announcement if orchestrator not available
            event: Optional[NarratorEvent] = None
            context: dict = {}

            if info.change_type == ChangeType.LOCAL_PUSH:
                event = NarratorEvent.LOCAL_PUSH_DETECTED
                context["summary"] = info.summary
            elif info.change_type == ChangeType.LOCAL_COMMIT:
                event = NarratorEvent.LOCAL_COMMIT_DETECTED
                context["summary"] = info.summary
            elif info.change_type == ChangeType.UNCOMMITTED:
                event = NarratorEvent.CODE_CHANGES_DETECTED
                context["summary"] = f"{info.uncommitted_files} uncommitted files"

            # Announce via narrator
            if event and self._narrator:
                await self._narrator.narrate_event(event, **context)

            # If restart is recommended, announce that too
            if info.restart_recommended and info.restart_reason:
                logger.info(f"🔄 Restart recommended: {info.restart_reason}")
                if self._narrator:
                    await asyncio.sleep(2)  # Brief pause for clarity
                    await self._narrator.narrate_event(
                        NarratorEvent.RESTART_RECOMMENDED,
                        reason=info.restart_reason,
                    )

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
        - LOCAL CHANGE AWARENESS (v2.0): Detects your commits and pushes
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
        """
        Background task: Monitor for idle state and trigger silent/Zero-Touch updates.
        
        v2.0: Enhanced with Zero-Touch autonomous update support.
        When Zero-Touch mode is enabled, this will:
        1. Check system idle state
        2. Query JARVIS busy state
        3. Validate update safety
        4. Auto-apply if all conditions pass
        """
        if not self._idle_detector or not self.config.idle.enabled:
            return
        
        logger.info("😴 Idle detector started")
        
        while not self._shutdown_event.is_set():
            try:
                is_idle = await self._idle_detector.is_system_idle()
                
                if is_idle and self.config.idle.silent_update_enabled:
                    # Check if update is available
                    if self._update_detector:
                        update_info = await self._update_detector.check_for_updates()
                        
                        if update_info and update_info.available:
                            # ═══════════════════════════════════════════════════════════════════
                            # ZERO-TOUCH MODE: Full autonomous update pipeline with enhanced narration
                            # ═══════════════════════════════════════════════════════════════════
                            if self.config.is_zero_touch_enabled:
                                # v3.0: Check and classify the update
                                can_auto, reason = await self._can_zero_touch_update()
                                
                                if can_auto:
                                    logger.info(f"🤖 Zero-Touch: Auto-applying update ({reason})")
                                    
                                    # v3.0: Get update classification for intelligent narration
                                    classification = await self._update_engine.classify_update()
                                    
                                    # Announce before update with classification context
                                    if self.config.zero_touch.announce_before_update:
                                        await self._narrator.narrate(NarratorEvent.ZERO_TOUCH_INITIATED)
                                        
                                        # Classification-specific narration
                                        await self._narrator.narrate_zero_touch_update(
                                            classification=classification.value,
                                            commits=update_info.commits_behind if hasattr(update_info, 'commits_behind') else 0,
                                        )
                                    
                                    self._update_requested.set()
                                else:
                                    logger.debug(f"🤖 Zero-Touch: Skipped - {reason}")
                                    
                                    # v3.0: Narrate why update was deferred
                                    if "busy" in reason.lower():
                                        await self._narrator.narrate(NarratorEvent.UPDATE_DEFERRED)
                            else:
                                # Standard silent update (requires confirmation or already approved)
                                logger.info("😴 System idle with update available - requesting silent update")
                                self._update_requested.set()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Idle detection error: {e}")
            
            await asyncio.sleep(60)  # Check every minute
        
        logger.info("😴 Idle detector stopped")
    
    async def _can_zero_touch_update(self) -> tuple[bool, str]:
        """
        Check if Zero-Touch auto-update can be performed.
        
        This is the gatekeeper for autonomous updates.
        
        Returns:
            Tuple of (can_update, reason)
        """
        if not self._update_engine:
            return False, "Update engine not initialized"
        
        # Delegate to update engine's comprehensive check
        return await self._update_engine.can_auto_update()
    
    def _check_immutable_core(self, files: list[str]) -> tuple[bool, list[str]]:
        """
        Check if update modifies immutable core files.
        
        Prime Directives: The Supervisor is READ-ONLY to JARVIS.
        
        Args:
            files: List of files being modified
            
        Returns:
            Tuple of (is_safe, list of protected files found)
        """
        if not self.config.prime_directives.supervisor_read_only:
            return True, []
        
        import fnmatch
        
        protected = []
        for pattern in self.config.prime_directives.protected_files:
            for file in files:
                if fnmatch.fnmatch(file, pattern):
                    protected.append(f"{file} (matches {pattern})")
        
        return len(protected) == 0, protected

    async def _run_restart_monitor(self) -> None:
        """
        Background task: Monitor RestartCoordinator for restart signals.

        This monitors the async-safe restart coordinator for restart requests
        from components like UpdateNotificationOrchestrator. When a restart
        is signaled, we properly terminate the child process to trigger a
        restart in the main loop.

        This replaces the old sys.exit(102) approach which didn't work
        properly from async tasks.
        """
        logger.info("🔄 Restart monitor started")

        while not self._shutdown_event.is_set():
            try:
                # Wait for restart signal (with timeout to allow shutdown check)
                request = await self._restart_coordinator.wait_for_restart(timeout=5.0)

                if request:
                    logger.info(f"🔄 Restart signal received: {request.reason}")
                    logger.info(f"   Source: {request.source.value}, Urgency: {request.urgency.value}")

                    # Broadcast final notification before restart
                    if self._notification_orchestrator:
                        try:
                            await self._notification_orchestrator._broadcast_restart_notification(
                                message="Restarting now...",
                                reason=request.reason,
                                estimated_time=15,
                            )
                        except Exception as e:
                            logger.debug(f"Restart notification broadcast failed: {e}")

                    # Set the restart requested flag for main loop
                    self._restart_requested.set()

                    # Terminate child process to trigger restart in main loop
                    if self._process and self._process.returncode is None:
                        logger.info(f"📡 Terminating JARVIS (PID: {self._process.pid}) for restart")
                        try:
                            # Send SIGTERM for graceful shutdown
                            self._process.send_signal(signal.SIGTERM)

                            # Give it a moment to exit gracefully
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(asyncio.create_task(self._wait_for_process_exit())),
                                    timeout=10.0
                                )
                            except asyncio.TimeoutError:
                                # Force kill if not exiting
                                logger.warning("⚠️ Process not exiting, sending SIGKILL")
                                self._process.kill()
                        except ProcessLookupError:
                            pass  # Process already exited

                    # Exit monitor loop - restart will be handled by main loop
                    break

            except asyncio.CancelledError:
                logger.info("🛑 Restart monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Restart monitor error: {e}")
                await asyncio.sleep(1)  # Prevent tight loop on errors

        logger.info("🔄 Restart monitor stopped")

    async def _wait_for_process_exit(self) -> None:
        """Wait for the child process to exit."""
        if self._process:
            await self._process.wait()

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

        # v2.0: Start unified voice orchestrator FIRST (single source of truth)
        # This ensures all voice output is coordinated through one system
        await self._voice_orchestrator.start()
        logger.info("🔊 Unified voice orchestrator started")

        # Start narrator (now delegates to orchestrator)
        await self._narrator.start()

        # Announce supervisor online (uses main narrator -> orchestrator)
        await self._narrator.narrate(NarratorEvent.SUPERVISOR_START, wait=True)
        
        # Initialize components
        await self._init_components()
        
        # Setup signal handlers
        self._setup_signal_handlers()
        
        # Initialize restart coordinator
        await self._restart_coordinator.initialize()

        # Start background tasks
        tasks = []
        if self.config.update.check.enabled and self._update_detector:
            tasks.append(asyncio.create_task(self._run_update_detector()))
        if self.config.idle.enabled and self._idle_detector:
            tasks.append(asyncio.create_task(self._run_idle_detector()))

        # Start restart monitor (always enabled - handles async restart signals)
        restart_monitor_task = asyncio.create_task(self._run_restart_monitor())
        tasks.append(restart_monitor_task)
        
        # Dead Man's Switch task (created dynamically after updates)
        dms_task: Optional[asyncio.Task] = None

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
                
                # ═══════════════════════════════════════════════════════════════════
                # DEAD MAN'S SWITCH: Start probation monitoring after post-update boot
                # ═══════════════════════════════════════════════════════════════════
                # Note: The DMS task runs DURING JARVIS execution, not after.
                # We start it here but it monitors concurrently with JARVIS.
                # The exit_code check below happens AFTER JARVIS exits.
                # 
                # For proper integration, we need to start DMS monitoring in
                # _spawn_jarvis or right after the process is created.
                # ═══════════════════════════════════════════════════════════════════

                # Check if restart was triggered by restart coordinator
                # (this takes precedence over exit code handling)
                if self._restart_requested.is_set():
                    self._restart_requested.clear()
                    logger.info("🔄 Restart triggered by coordinator")
                    self._set_state(SupervisorState.RESTARTING)
                    await self._narrator.narrate(NarratorEvent.RESTART_STARTING)
                    # Restart the restart monitor for next cycle
                    if restart_monitor_task.done():
                        restart_monitor_task = asyncio.create_task(self._run_restart_monitor())
                        tasks.append(restart_monitor_task)
                    continue

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

            # Cleanup restart coordinator
            await self._restart_coordinator.cleanup()
            
            # Cleanup Dead Man's Switch
            if self._dead_man_switch:
                try:
                    await self._dead_man_switch.close()
                    logger.debug("🎯 Dead Man's Switch closed")
                except Exception as e:
                    logger.debug(f"Dead Man's Switch cleanup error: {e}")

            # v5.0: Cleanup Intelligence Component Manager
            if self._intelligence_manager:
                try:
                    await self._intelligence_manager.shutdown()
                    logger.info("🧠 Intelligence Component Manager shutdown complete")
                except Exception as e:
                    logger.debug(f"Intelligence Component Manager cleanup error: {e}")

            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # v2.0: Stop unified voice orchestrator LAST (after all other cleanup)
            # This ensures any final messages can still be spoken
            try:
                await self._voice_orchestrator.stop()
                logger.info("🔊 Unified voice orchestrator stopped")
            except Exception as e:
                logger.debug(f"Voice orchestrator cleanup error: {e}")

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
