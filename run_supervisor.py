#!/usr/bin/env python3
"""
JARVIS Supervisor Entry Point - Production Grade v5.0 (Living OS Edition)
===========================================================================

Advanced, robust, async, parallel, intelligent, and dynamic supervisor entry point.
This is the SINGLE COMMAND needed to run JARVIS - it handles everything.

v5.0 Living OS Features:
- 🔥 DEV MODE: Hot reload / live reload - edit code and see changes instantly
- 🔄 Zero-Touch autonomous self-updating without human intervention
- 🛡️ Dead Man's Switch for post-update stability verification
- 🎙️ Unified voice coordination (narrator + announcer working together)
- 📋 Prime Directives (immutable safety constraints)
- 🧠 AGI OS integration for intelligent decision making

v7.0 JARVIS-Prime Integration:
- 🧠 Tier-0 Local Brain: GGUF model inference via llama-cpp-python
- 🐳 Docker/Cloud Run: Serverless deployment to Google Cloud Run
- 🔬 Reactor-Core: Auto-deployment of trained models from reactor-core
- 💰 Cost-Effective: Free local inference, reduces cloud API costs

Dev Mode (Hot Reload):
    When you run `python3 run_supervisor.py`, it:
    1. Starts JARVIS normally
    2. Watches your code for changes (.py, .yaml files)
    3. Automatically restarts JARVIS when you save changes
    4. You never have to manually restart while developing!
    
    This is like "nodemon" for Python - write code, save, see changes.

Core Features:
- Parallel process discovery and termination with cascade strategy
- Async resource validation (memory, disk, ports, network)
- Dynamic configuration with environment variable overrides
- Connection pooling and intelligent health pre-checks
- Parallel component initialization with dependency resolution
- Graceful shutdown orchestration with cleanup tasks
- Performance metrics and timing analysis
- Circuit breaker for repeated failures
- Adaptive startup based on system resources

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │  SupervisorBootstrapper (this file)                         │
    │  ├── ParallelProcessCleaner (async process termination)     │
    │  ├── IntelligentResourceOrchestrator (pre-flight checks)    │
    │  ├── DynamicConfigLoader (env + yaml + defaults)            │
    │  ├── AGIOSBridge (intelligent decision propagation)         │
    │  ├── HotReloadWatcher (file change detection) [DEV MODE]    │
    │  └── IntelligentStartupOrchestrator (phased initialization) │
    └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────────────┐
    │  JARVISSupervisor (core/supervisor/jarvis_supervisor.py)    │
    │  ├── ZeroTouchEngine (autonomous updates)                   │
    │  ├── DeadManSwitch (stability verification)                 │
    │  ├── UpdateEngine (staging, validation, classification)     │
    │  ├── RollbackManager (version history, snapshots)           │
    │  ├── UnifiedStartupVoiceCoordinator (narrator + announcer)  │
    │  └── SupervisorNarrator v5.0 (intelligent voice feedback)   │
    └─────────────────────────────────────────────────────────────┘

Usage:
    # Run supervisor (recommended way to start JARVIS)
    # This is ALL you need - it handles everything including hot reload!
    python run_supervisor.py

    # Disable dev mode / hot reload (production)
    JARVIS_DEV_MODE=false python run_supervisor.py

    # With debug logging
    JARVIS_SUPERVISOR_LOG_LEVEL=DEBUG python run_supervisor.py

    # Disable voice narration
    STARTUP_NARRATOR_VOICE=false python run_supervisor.py

    # Skip resource validation (faster startup)
    SKIP_RESOURCE_CHECK=true python run_supervisor.py

    # Enable Zero-Touch autonomous updates
    JARVIS_ZERO_TOUCH_ENABLED=true python run_supervisor.py

    # Configure Dead Man's Switch probation period
    JARVIS_DMS_PROBATION_SECONDS=60 python run_supervisor.py

    # Configure hot reload check interval (default: 10s)
    JARVIS_RELOAD_CHECK_INTERVAL=5 python run_supervisor.py

    # Configure startup grace period before hot reload activates (default: 120s)
    JARVIS_RELOAD_GRACE_PERIOD=60 python run_supervisor.py

    # ═══════════════════════════════════════════════════════════════════
    # JARVIS-Prime Configuration (v7.0)
    # ═══════════════════════════════════════════════════════════════════

    # Disable JARVIS-Prime (use cloud APIs only)
    JARVIS_PRIME_ENABLED=false python run_supervisor.py

    # Use Docker container for JARVIS-Prime
    JARVIS_PRIME_USE_DOCKER=true python run_supervisor.py

    # Use Cloud Run for JARVIS-Prime
    JARVIS_PRIME_USE_CLOUD_RUN=true \
    JARVIS_PRIME_CLOUD_RUN_URL=https://jarvis-prime-xxx.run.app \
    python run_supervisor.py

    # Custom model path
    JARVIS_PRIME_MODELS_DIR=/path/to/models python run_supervisor.py

    # Enable Reactor-Core auto-deployment
    REACTOR_CORE_ENABLED=true \
    REACTOR_CORE_OUTPUT=/path/to/reactor-core/output \
    python run_supervisor.py

Author: JARVIS System
Version: 7.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Add backend to path
backend_path = Path(__file__).parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

# =============================================================================
# CRITICAL: Python 3.9 Compatibility - MUST run before any Google API imports
# =============================================================================
# This patches importlib.metadata.packages_distributions() which google-api-core needs
try:
    from utils.python39_compat import ensure_python39_compatibility
    ensure_python39_compatibility()
except ImportError:
    # Fallback: manually patch if the module isn't available
    import importlib.metadata as metadata
    if not hasattr(metadata, 'packages_distributions'):
        def packages_distributions():
            return {}
        metadata.packages_distributions = packages_distributions

# =============================================================================
# EARLY SHUTDOWN HOOK REGISTRATION
# =============================================================================
# Register shutdown hook as early as possible to ensure GCP VMs are cleaned up
# even if JARVIS crashes during startup. The hook handles its own idempotency.
# =============================================================================
try:
    from backend.scripts.shutdown_hook import register_handlers as _register_shutdown_handlers
    _register_shutdown_handlers()
except ImportError:
    pass  # Will be registered later by SupervisorBootstrapper

# =============================================================================
# Configuration - Dynamic with Environment Overrides
# =============================================================================

@dataclass
class BootstrapConfig:
    """Dynamic configuration for supervisor bootstrap."""
    
    # Logging
    log_level: str = field(default_factory=lambda: os.getenv("JARVIS_SUPERVISOR_LOG_LEVEL", "INFO"))
    log_format: str = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    
    # Voice narration
    voice_enabled: bool = field(default_factory=lambda: os.getenv("STARTUP_NARRATOR_VOICE", "true").lower() == "true")
    voice_name: str = field(default_factory=lambda: os.getenv("STARTUP_NARRATOR_VOICE_NAME", "Daniel"))
    voice_rate: int = field(default_factory=lambda: int(os.getenv("STARTUP_NARRATOR_RATE", "190")))
    
    # Resource validation
    skip_resource_check: bool = field(default_factory=lambda: os.getenv("SKIP_RESOURCE_CHECK", "false").lower() == "true")
    min_memory_gb: float = field(default_factory=lambda: float(os.getenv("MIN_MEMORY_GB", "2.0")))
    min_disk_gb: float = field(default_factory=lambda: float(os.getenv("MIN_DISK_GB", "1.0")))
    
    # Process cleanup
    cleanup_timeout_sigint: float = field(default_factory=lambda: float(os.getenv("CLEANUP_TIMEOUT_SIGINT", "10.0")))
    cleanup_timeout_sigterm: float = field(default_factory=lambda: float(os.getenv("CLEANUP_TIMEOUT_SIGTERM", "5.0")))
    cleanup_timeout_sigkill: float = field(default_factory=lambda: float(os.getenv("CLEANUP_TIMEOUT_SIGKILL", "2.0")))
    max_parallel_cleanups: int = field(default_factory=lambda: int(os.getenv("MAX_PARALLEL_CLEANUPS", "5")))
    
    # Ports to validate
    required_ports: List[int] = field(default_factory=lambda: [
        int(os.getenv("BACKEND_PORT", "8010")),
        int(os.getenv("FRONTEND_PORT", "3000")),
        int(os.getenv("LOADING_SERVER_PORT", "3001")),
    ])
    
    # Patterns to identify JARVIS processes
    jarvis_patterns: List[str] = field(default_factory=lambda: [
        "start_system.py",
        "main.py",
        "jarvis",
    ])
    
    # PID file locations
    pid_files: List[Path] = field(default_factory=lambda: [
        Path("/tmp/jarvis_master.pid"),
        Path("/tmp/jarvis.pid"),
        Path("/tmp/jarvis_supervisor.pid"),
    ])
    
    # =========================================================================
    # v3.0: Zero-Touch Autonomous Update Settings
    # =========================================================================
    # Default: ENABLED - JARVIS should be a living, self-updating system
    # Disable with: JARVIS_ZERO_TOUCH_ENABLED=false
    zero_touch_enabled: bool = field(default_factory=lambda: os.getenv("JARVIS_ZERO_TOUCH_ENABLED", "true").lower() == "true")
    zero_touch_require_idle: bool = field(default_factory=lambda: os.getenv("JARVIS_ZERO_TOUCH_REQUIRE_IDLE", "true").lower() == "true")
    zero_touch_check_busy: bool = field(default_factory=lambda: os.getenv("JARVIS_ZERO_TOUCH_CHECK_BUSY", "true").lower() == "true")
    zero_touch_auto_security: bool = field(default_factory=lambda: os.getenv("JARVIS_ZERO_TOUCH_AUTO_SECURITY", "true").lower() == "true")
    zero_touch_auto_critical: bool = field(default_factory=lambda: os.getenv("JARVIS_ZERO_TOUCH_AUTO_CRITICAL", "true").lower() == "true")
    zero_touch_auto_minor: bool = field(default_factory=lambda: os.getenv("JARVIS_ZERO_TOUCH_AUTO_MINOR", "true").lower() == "true")
    zero_touch_auto_major: bool = field(default_factory=lambda: os.getenv("JARVIS_ZERO_TOUCH_AUTO_MAJOR", "false").lower() == "true")
    
    # Dead Man's Switch settings
    dms_enabled: bool = field(default_factory=lambda: os.getenv("JARVIS_DMS_ENABLED", "true").lower() == "true")
    dms_probation_seconds: int = field(default_factory=lambda: int(os.getenv("JARVIS_DMS_PROBATION_SECONDS", "30")))
    dms_max_failures: int = field(default_factory=lambda: int(os.getenv("JARVIS_DMS_MAX_FAILURES", "3")))
    
    # AGI OS integration
    agi_os_enabled: bool = field(default_factory=lambda: os.getenv("JARVIS_AGI_OS_ENABLED", "true").lower() == "true")
    agi_os_approval_for_updates: bool = field(default_factory=lambda: os.getenv("JARVIS_AGI_OS_APPROVAL_UPDATES", "false").lower() == "true")

    # =========================================================================
    # JARVIS-Prime Tier-0 Brain Integration
    # =========================================================================
    # Local brain for fast, cost-effective inference without cloud API calls.
    # Can run locally (subprocess) or in Docker/Cloud Run.
    # =========================================================================
    jarvis_prime_enabled: bool = field(default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true")
    jarvis_prime_auto_start: bool = field(default_factory=lambda: os.getenv("JARVIS_PRIME_AUTO_START", "true").lower() == "true")
    jarvis_prime_host: str = field(default_factory=lambda: os.getenv("JARVIS_PRIME_HOST", "127.0.0.1"))
    jarvis_prime_port: int = field(default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8002")))
    jarvis_prime_repo_path: Path = field(default_factory=lambda: Path(os.getenv(
        "JARVIS_PRIME_PATH",
        str(Path.home() / "Documents" / "repos" / "jarvis-prime")
    )))
    jarvis_prime_models_dir: str = field(default_factory=lambda: os.getenv("JARVIS_PRIME_MODELS_DIR", "models"))
    jarvis_prime_startup_timeout: float = field(default_factory=lambda: float(os.getenv("JARVIS_PRIME_STARTUP_TIMEOUT", "60.0")))

    # Docker/Cloud Run mode
    jarvis_prime_use_docker: bool = field(default_factory=lambda: os.getenv("JARVIS_PRIME_USE_DOCKER", "false").lower() == "true")
    jarvis_prime_docker_image: str = field(default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_IMAGE", "jarvis-prime:latest"))
    jarvis_prime_use_cloud_run: bool = field(default_factory=lambda: os.getenv("JARVIS_PRIME_USE_CLOUD_RUN", "false").lower() == "true")
    jarvis_prime_cloud_run_url: str = field(default_factory=lambda: os.getenv("JARVIS_PRIME_CLOUD_RUN_URL", ""))

    # =========================================================================
    # Reactor-Core Integration (Auto-deployment of trained models)
    # =========================================================================
    reactor_core_enabled: bool = field(default_factory=lambda: os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true")
    reactor_core_watch_dir: Optional[str] = field(default_factory=lambda: os.getenv("REACTOR_CORE_OUTPUT"))
    reactor_core_auto_deploy: bool = field(default_factory=lambda: os.getenv("REACTOR_CORE_AUTO_DEPLOY", "true").lower() == "true")

    # =========================================================================
    # Data Flywheel Integration (Self-improving learning loop)
    # =========================================================================
    data_flywheel_enabled: bool = field(default_factory=lambda: os.getenv("DATA_FLYWHEEL_ENABLED", "true").lower() == "true")
    data_flywheel_auto_collect: bool = field(default_factory=lambda: os.getenv("DATA_FLYWHEEL_AUTO_COLLECT", "true").lower() == "true")
    data_flywheel_auto_train: bool = field(default_factory=lambda: os.getenv("DATA_FLYWHEEL_AUTO_TRAIN", "true").lower() == "true")
    data_flywheel_training_schedule: str = field(default_factory=lambda: os.getenv("DATA_FLYWHEEL_TRAINING_SCHEDULE", "03:00"))  # 3 AM daily
    data_flywheel_min_experiences: int = field(default_factory=lambda: int(os.getenv("DATA_FLYWHEEL_MIN_EXPERIENCES", "100")))
    data_flywheel_cooldown_hours: int = field(default_factory=lambda: int(os.getenv("DATA_FLYWHEEL_COOLDOWN_HOURS", "24")))

    # =========================================================================
    # Intelligent Learning Goals (Auto-discovery from interactions)
    # =========================================================================
    learning_goals_enabled: bool = field(default_factory=lambda: os.getenv("LEARNING_GOALS_ENABLED", "true").lower() == "true")
    learning_goals_auto_discover: bool = field(default_factory=lambda: os.getenv("LEARNING_GOALS_AUTO_DISCOVER", "true").lower() == "true")
    learning_goals_max_topics: int = field(default_factory=lambda: int(os.getenv("LEARNING_GOALS_MAX_TOPICS", "20")))

    # =========================================================================
    # v9.0: Intelligence Systems (UAE/SAI/Neural Mesh/MAS)
    # =========================================================================
    # UAE (Unified Awareness Engine) - Screen awareness and computer vision
    uae_enabled: bool = field(default_factory=lambda: os.getenv("UAE_ENABLED", "true").lower() == "true")
    uae_chain_of_thought: bool = field(default_factory=lambda: os.getenv("UAE_CHAIN_OF_THOUGHT", "true").lower() == "true")

    # SAI (Situational Awareness Intelligence) - Window/app tracking
    sai_enabled: bool = field(default_factory=lambda: os.getenv("SAI_ENABLED", "true").lower() == "true")
    sai_yabai_bridge: bool = field(default_factory=lambda: os.getenv("SAI_YABAI_BRIDGE", "true").lower() == "true")

    # Neural Mesh - Distributed intelligence coordination
    neural_mesh_enabled: bool = field(default_factory=lambda: os.getenv("NEURAL_MESH_ENABLED", "true").lower() == "true")
    neural_mesh_sync_interval: float = field(default_factory=lambda: float(os.getenv("NEURAL_MESH_SYNC_INTERVAL", "5.0")))

    # MAS (Multi-Agent System) - Coordinated agent execution
    mas_enabled: bool = field(default_factory=lambda: os.getenv("MAS_ENABLED", "true").lower() == "true")
    mas_max_concurrent_agents: int = field(default_factory=lambda: int(os.getenv("MAS_MAX_CONCURRENT_AGENTS", "5")))

    # CAI (Collective AI Intelligence) - Cross-system insight aggregation
    cai_enabled: bool = field(default_factory=lambda: os.getenv("CAI_ENABLED", "true").lower() == "true")

    # =========================================================================
    # v9.0: Continuous Background Web Scraping
    # =========================================================================
    continuous_scraping_enabled: bool = field(default_factory=lambda: os.getenv("CONTINUOUS_SCRAPING_ENABLED", "true").lower() == "true")
    continuous_scraping_interval_hours: float = field(default_factory=lambda: float(os.getenv("CONTINUOUS_SCRAPING_INTERVAL_HOURS", "4")))
    continuous_scraping_max_pages: int = field(default_factory=lambda: int(os.getenv("CONTINUOUS_SCRAPING_MAX_PAGES", "50")))
    continuous_scraping_topics: str = field(default_factory=lambda: os.getenv("CONTINUOUS_SCRAPING_TOPICS", ""))

    # =========================================================================
    # v9.0: Reactor-Core Integration (Training Pipeline)
    # =========================================================================
    reactor_core_integration_enabled: bool = field(default_factory=lambda: os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true")
    reactor_core_repo_path: str = field(default_factory=lambda: os.getenv("REACTOR_CORE_PATH", str(Path.home() / "Documents" / "repos" / "reactor-core")))


class StartupPhase(Enum):
    """Phases of supervisor startup."""
    INIT = "init"
    CLEANUP = "cleanup"
    VALIDATION = "validation"
    SUPERVISOR_INIT = "supervisor_init"
    JARVIS_START = "jarvis_start"
    COMPLETE = "complete"
    FAILED = "failed"


# =============================================================================
# Logging Setup - Advanced with Performance Tracking
# =============================================================================

class PerformanceLogger:
    """Track and log performance metrics during startup."""
    
    def __init__(self):
        self._timings: Dict[str, float] = {}
        self._start_times: Dict[str, float] = {}
        self._start_time = time.perf_counter()
    
    def start(self, operation: str) -> None:
        """Start timing an operation."""
        self._start_times[operation] = time.perf_counter()
    
    def end(self, operation: str) -> float:
        """End timing an operation and return duration."""
        if operation in self._start_times:
            duration = time.perf_counter() - self._start_times[operation]
            self._timings[operation] = duration
            del self._start_times[operation]
            return duration
        return 0.0
    
    def get_total_time(self) -> float:
        """Get total time since logger creation."""
        return time.perf_counter() - self._start_time
    
    def get_summary(self) -> Dict[str, float]:
        """Get all recorded timings."""
        return {
            **self._timings,
            "total": self.get_total_time()
        }


def setup_logging(config: BootstrapConfig) -> logging.Logger:
    """
    Configure advanced logging for the supervisor.
    
    Features:
    - Configurable log level via environment
    - Reduced noise from libraries
    - Performance-friendly format
    """
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format=config.log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Reduce noise from libraries
    noisy_loggers = [
        "urllib3", "asyncio", "aiohttp", "httpx",
        "httpcore", "charset_normalizer"
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    
    return logging.getLogger("supervisor.bootstrap")


# =============================================================================
# Voice Narration - Unified Orchestrator Integration (v2.0)
# =============================================================================

class AsyncVoiceNarrator:
    """
    Async voice narrator that delegates to UnifiedVoiceOrchestrator.

    v2.0 CHANGE: Instead of spawning direct `say` processes, this now
    delegates to the unified orchestrator to ensure only ONE voice
    speaks at a time across the entire JARVIS system.

    Features:
    - Non-blocking voice output
    - Queue management via unified orchestrator
    - Platform-aware (macOS only)
    - Graceful fallback on errors
    - PREVENTS multiple voices during startup
    """

    def __init__(self, config: BootstrapConfig):
        self.config = config
        self.enabled = config.voice_enabled and platform.system() == "Darwin"
        self._orchestrator = None
        self._started = False

    async def _ensure_orchestrator(self):
        """Ensure the unified orchestrator is initialized and started."""
        if self._orchestrator is None:
            try:
                # CRITICAL: Use the SAME import path as startup_narrator.py
                # to ensure we get the SAME singleton instance.
                # 
                # startup_narrator.py uses: from .unified_voice_orchestrator import ...
                # which resolves to: core.supervisor.unified_voice_orchestrator
                # 
                # If we use "backend.core.supervisor..." here, Python treats it as
                # a DIFFERENT module, creating a SEPARATE singleton!
                from core.supervisor.unified_voice_orchestrator import (
                    get_voice_orchestrator,
                    VoicePriority,
                    VoiceSource,
                )
                self._orchestrator = get_voice_orchestrator()
                self._VoicePriority = VoicePriority
                self._VoiceSource = VoiceSource
            except ImportError as e:
                # Fallback if orchestrator not available
                logging.getLogger(__name__).debug(f"Voice orchestrator not available: {e}")
                self.enabled = False
                return

        if not self._started and self._orchestrator:
            await self._orchestrator.start()
            self._started = True

    async def speak(self, text: str, wait: bool = True, priority: bool = False) -> None:
        """
        Speak text through unified orchestrator.

        Args:
            text: Text to speak
            wait: Whether to wait for speech to complete
            priority: If True, use CRITICAL priority (interrupts current)
        """
        if not self.enabled:
            return

        try:
            await self._ensure_orchestrator()

            if self._orchestrator is None:
                return

            # Map priority to VoicePriority
            voice_priority = (
                self._VoicePriority.CRITICAL if priority
                else self._VoicePriority.MEDIUM
            )

            await self._orchestrator.speak(
                text=text,
                priority=voice_priority,
                source=self._VoiceSource.SYSTEM,
                wait=wait,
            )
        except Exception as e:
            logging.getLogger(__name__).debug(f"Voice error: {e}")


# =============================================================================
# Parallel Process Cleaner - Advanced Termination
# =============================================================================

@dataclass
class ProcessInfo:
    """Information about a discovered process."""
    pid: int
    cmdline: str
    age_seconds: float
    memory_mb: float = 0.0
    source: str = "scan"  # "pid_file" or "scan"


class ParallelProcessCleaner:
    """
    Intelligent parallel process cleaner with cascade termination.
    
    Features:
    - Parallel process discovery using ThreadPoolExecutor
    - Async termination with SIGINT → SIGTERM → SIGKILL cascade
    - Semaphore-controlled parallelism
    - Detailed progress reporting
    - PID file cleanup
    """
    
    def __init__(self, config: BootstrapConfig, logger: logging.Logger, narrator: AsyncVoiceNarrator):
        self.config = config
        self.logger = logger
        self.narrator = narrator
        self._my_pid = os.getpid()
        self._my_parent = os.getppid()
    
    async def discover_and_cleanup(self) -> Tuple[int, List[ProcessInfo]]:
        """
        Discover and cleanup existing JARVIS instances.
        
        Returns:
            Tuple of (terminated_count, discovered_processes)
        """
        perf = PerformanceLogger()
        
        # Phase 1: Parallel discovery
        perf.start("discovery")
        discovered = await self._parallel_discover()
        perf.end("discovery")
        
        if not discovered:
            return 0, []
        
        # Announce cleanup
        await self.narrator.speak("Cleaning up previous session.", wait=False)
        
        # Phase 2: Parallel termination with semaphore
        perf.start("termination")
        terminated = await self._parallel_terminate(discovered)
        perf.end("termination")
        
        # Phase 3: PID file cleanup
        await self._cleanup_pid_files()
        
        self.logger.debug(f"Cleanup performance: {perf.get_summary()}")
        
        # Send log to loading page if available
        try:
            from loading_server import get_progress_reporter
            reporter = get_progress_reporter()
            if terminated > 0:
                await reporter.log(
                    "Supervisor",
                    f"Cleaned up {terminated} existing instance(s)",
                    "success"
                )
        except Exception:
            pass
        
        return terminated, list(discovered.values())
    
    async def _parallel_discover(self) -> Dict[int, ProcessInfo]:
        """Discover processes in parallel using ThreadPoolExecutor."""
        discovered: Dict[int, ProcessInfo] = {}
        
        # Run in thread pool for psutil operations (they can block)
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=4) as executor:
            # Task 1: Check PID files
            pid_file_task = loop.run_in_executor(
                executor, self._discover_from_pid_files
            )
            
            # Task 2: Scan process list
            process_scan_task = loop.run_in_executor(
                executor, self._discover_from_process_list
            )

            # Task 3: Scan ports (new)
            port_scan_task = loop.run_in_executor(
                executor, self._discover_from_ports
            )
            
            # Wait for all
            pid_file_procs, scanned_procs, port_procs = await asyncio.gather(
                pid_file_task, process_scan_task, port_scan_task
            )
        
        # Merge results (PID files take precedence, then ports, then scan)
        discovered.update(scanned_procs)
        discovered.update(port_procs)
        discovered.update(pid_file_procs)
        
        return discovered
    
    def _discover_from_pid_files(self) -> Dict[int, ProcessInfo]:
        """Discover processes from PID files (runs in thread)."""
        try:
            import psutil
        except ImportError:
            return {}
        
        discovered = {}
        
        for pid_file in self.config.pid_files:
            if not pid_file.exists():
                continue
            
            try:
                pid = int(pid_file.read_text().strip())
                if not psutil.pid_exists(pid) or pid in (self._my_pid, self._my_parent):
                    pid_file.unlink(missing_ok=True)
                    continue
                
                proc = psutil.Process(pid)
                cmdline = " ".join(proc.cmdline()).lower()
                
                if any(p in cmdline for p in self.config.jarvis_patterns):
                    discovered[pid] = ProcessInfo(
                        pid=pid,
                        cmdline=cmdline[:100],
                        age_seconds=time.time() - proc.create_time(),
                        memory_mb=proc.memory_info().rss / (1024 * 1024),
                        source="pid_file"
                    )
            except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                try:
                    pid_file.unlink(missing_ok=True)
                except Exception:
                    pass
        
        return discovered
    
    def _discover_from_process_list(self) -> Dict[int, ProcessInfo]:
        """Scan process list for JARVIS processes (runs in thread)."""
        try:
            import psutil
        except ImportError:
            return {}
        
        discovered = {}
        
        for proc in psutil.process_iter(['pid', 'cmdline', 'create_time', 'memory_info']):
            try:
                pid = proc.info['pid']
                if pid in (self._my_pid, self._my_parent):
                    continue
                
                cmdline = " ".join(proc.info.get('cmdline') or []).lower()
                if any(p in cmdline for p in self.config.jarvis_patterns):
                    mem_info = proc.info.get('memory_info')
                    discovered[pid] = ProcessInfo(
                        pid=pid,
                        cmdline=cmdline[:100],
                        age_seconds=time.time() - proc.info['create_time'],
                        memory_mb=mem_info.rss / (1024 * 1024) if mem_info else 0,
                        source="scan"
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        return discovered

    def _discover_from_ports(self) -> Dict[int, ProcessInfo]:
        """Discover processes holding critical ports."""
        try:
            import psutil
        except ImportError:
            return {}

        discovered = {}
        critical_ports = self.config.required_ports

        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port in critical_ports:
                    try:
                        pid = conn.pid
                        if not pid or pid in (self._my_pid, self._my_parent):
                            continue
                            
                        # Don't rediscover if we already found it, but verify it exists
                        if pid in discovered:
                            continue

                        proc = psutil.Process(pid)
                        cmdline = " ".join(proc.cmdline()).lower()
                        mem_info = proc.memory_info()
                        
                        discovered[pid] = ProcessInfo(
                            pid=pid,
                            cmdline=cmdline[:100],
                            age_seconds=time.time() - proc.create_time(),
                            memory_mb=mem_info.rss / (1024 * 1024),
                            source=f"port_{conn.laddr.port}"
                        )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except (psutil.AccessDenied, PermissionError):
            # macOS might require root for net_connections on other users' procs
            pass
            
        return discovered
    
    async def _parallel_terminate(self, processes: Dict[int, ProcessInfo]) -> int:
        """Terminate processes in parallel with semaphore control."""
        import psutil
        
        # Limit parallelism
        semaphore = asyncio.Semaphore(self.config.max_parallel_cleanups)
        
        async def terminate_one(pid: int, info: ProcessInfo) -> bool:
            async with semaphore:
                return await self._terminate_process(pid, info)
        
        # Create termination tasks
        tasks = [
            asyncio.create_task(terminate_one(pid, info))
            for pid, info in processes.items()
        ]
        
        # Wait for all with gather
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Count successes
        terminated = sum(1 for r in results if r is True)
        return terminated
    
    async def _terminate_process(self, pid: int, info: ProcessInfo) -> bool:
        """
        Terminate a single process with cascade strategy.
        
        SIGINT → wait → SIGTERM → wait → SIGKILL
        """
        try:
            import psutil
            
            proc = psutil.Process(pid)
            
            # Phase 1: SIGINT (graceful)
            try:
                os.kill(pid, signal.SIGINT)
                await asyncio.sleep(0.1)  # Brief pause
                proc.wait(timeout=self.config.cleanup_timeout_sigint)
                return True
            except psutil.TimeoutExpired:
                pass
            
            # Phase 2: SIGTERM
            try:
                os.kill(pid, signal.SIGTERM)
                proc.wait(timeout=self.config.cleanup_timeout_sigterm)
                return True
            except psutil.TimeoutExpired:
                pass
            
            # Phase 3: SIGKILL (force)
            os.kill(pid, signal.SIGKILL)
            proc.wait(timeout=self.config.cleanup_timeout_sigkill)
            return True
            
        except (psutil.NoSuchProcess, ProcessLookupError):
            return True  # Already gone
        except Exception as e:
            self.logger.debug(f"Failed to terminate {pid}: {e}")
            return False
    
    async def _cleanup_pid_files(self) -> None:
        """Clean up stale PID files."""
        for pid_file in self.config.pid_files:
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass


# =============================================================================
# System Resource Validator - Pre-flight Checks
# =============================================================================

@dataclass
class ResourceStatus:
    """
    Enhanced status of system resources with intelligent analysis.

    Includes not just resource metrics but also:
    - Recommendations for optimization
    - Actions taken automatically
    - Startup mode decision
    - Cloud activation status
    - ARM64 SIMD availability
    """
    memory_available_gb: float
    memory_total_gb: float
    disk_available_gb: float
    ports_available: List[int]
    ports_in_use: List[int]
    cpu_count: int
    load_average: Optional[Tuple[float, float, float]] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    # New intelligent fields
    recommendations: List[str] = field(default_factory=list)
    actions_taken: List[str] = field(default_factory=list)
    startup_mode: Optional[str] = None  # local_full, cloud_first, cloud_only
    cloud_activated: bool = False
    arm64_simd_available: bool = False
    memory_pressure: float = 0.0  # 0-100%

    @property
    def is_healthy(self) -> bool:
        return len(self.errors) == 0

    @property
    def is_cloud_mode(self) -> bool:
        return self.startup_mode in ("cloud_first", "cloud_only")


class IntelligentResourceOrchestrator:
    """
    Intelligent Resource Orchestrator for JARVIS Startup.

    This is a comprehensive, async, parallel, intelligent, and dynamic resource
    management system that integrates:

    1. MemoryAwareStartup - Intelligent cloud offloading decisions
    2. IntelligentMemoryOptimizer - Active memory optimization
    3. HybridRouter - Resource-aware request routing
    4. GCP Hybrid Cloud - Automatic cloud activation when needed

    Features:
    - Parallel resource checks with intelligent analysis
    - Automatic memory optimization when constrained
    - Dynamic startup mode selection (LOCAL_FULL, CLOUD_FIRST, CLOUD_ONLY)
    - Intelligent port conflict resolution
    - Cost-aware cloud activation recommendations
    - ARM64 SIMD optimization detection
    - Real-time resource monitoring

    Architecture:
        ┌─────────────────────────────────────────────────────────────┐
        │         IntelligentResourceOrchestrator                      │
        │  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐ │
        │  │MemoryAwareStartup│  │MemoryOptimizer │  │ HybridRouter │ │
        │  │  (Cloud Decision)│  │ (Active Optim) │  │  (Routing)   │ │
        │  └────────┬────────┘  └────────┬────────┘  └──────┬───────┘ │
        │           │                    │                   │         │
        │           └────────────────────┴───────────────────┘         │
        │                          ↓                                   │
        │              Unified Resource Decision                       │
        └─────────────────────────────────────────────────────────────┘
    """

    # Thresholds (configurable via environment)
    CLOUD_THRESHOLD_GB = float(os.getenv("JARVIS_CLOUD_THRESHOLD_GB", "6.0"))
    CRITICAL_THRESHOLD_GB = float(os.getenv("JARVIS_CRITICAL_THRESHOLD_GB", "2.0"))
    OPTIMIZE_THRESHOLD_GB = float(os.getenv("JARVIS_OPTIMIZE_THRESHOLD_GB", "4.0"))

    def __init__(self, config: BootstrapConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # Lazy-loaded components
        self._memory_aware_startup = None
        self._memory_optimizer = None
        self._hybrid_router = None

        # State
        self._startup_mode = None
        self._optimization_performed = False
        self._cloud_activated = False
        self._arm64_available = self._check_arm64_simd()

    def _check_arm64_simd(self) -> bool:
        """Check if ARM64 SIMD optimizations are available."""
        try:
            asm_path = Path(__file__).parent / "backend" / "core" / "arm64_simd_asm.s"
            return asm_path.exists() and platform.machine() == "arm64"
        except Exception:
            return False

    async def _get_memory_aware_startup(self):
        """Lazy load MemoryAwareStartup."""
        if self._memory_aware_startup is None:
            try:
                from core.memory_aware_startup import MemoryAwareStartup
                self._memory_aware_startup = MemoryAwareStartup()
            except ImportError as e:
                self.logger.debug(f"MemoryAwareStartup not available: {e}")
        return self._memory_aware_startup

    async def _get_memory_optimizer(self):
        """Lazy load IntelligentMemoryOptimizer."""
        if self._memory_optimizer is None:
            try:
                from memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
                self._memory_optimizer = IntelligentMemoryOptimizer()
            except ImportError as e:
                self.logger.debug(f"IntelligentMemoryOptimizer not available: {e}")
        return self._memory_optimizer

    async def validate_and_optimize(self) -> ResourceStatus:
        """
        Validate system resources AND take intelligent action.

        This goes beyond just checking - it actively optimizes and
        makes decisions about startup mode and cloud activation.

        Returns:
            ResourceStatus with enhanced recommendations and actions taken
        """
        if self.config.skip_resource_check:
            self.logger.debug("Resource check skipped via config")
            return ResourceStatus(
                memory_available_gb=0,
                memory_total_gb=0,
                disk_available_gb=0,
                ports_available=[],
                ports_in_use=[],
                cpu_count=os.cpu_count() or 1,
            )

        # Phase 1: Parallel resource checks
        memory_task = asyncio.create_task(self._check_memory_detailed())
        disk_task = asyncio.create_task(self._check_disk())
        ports_task = asyncio.create_task(self._check_ports_intelligent())
        cpu_task = asyncio.create_task(self._check_cpu())

        memory_result, disk_result, ports_result, cpu_result = await asyncio.gather(
            memory_task, disk_task, ports_task, cpu_task
        )

        # Phase 2: Intelligent analysis and action
        warnings = []
        errors = []
        actions_taken = []
        recommendations = []

        available_gb = memory_result["available_gb"]
        total_gb = memory_result["total_gb"]
        memory_pressure = memory_result["pressure"]

        # === INTELLIGENT MEMORY HANDLING ===
        if available_gb < self.CRITICAL_THRESHOLD_GB:
            # Critical memory - attempt aggressive optimization
            self.logger.warning(f"⚠️  CRITICAL: Only {available_gb:.1f}GB available!")

            optimizer = await self._get_memory_optimizer()
            if optimizer:
                self.logger.info("🧹 Attempting emergency memory optimization...")
                success, report = await optimizer.optimize_for_langchain(aggressive=True)
                if success:
                    freed_mb = report.get("memory_freed_mb", 0)
                    actions_taken.append(f"Emergency optimization freed {freed_mb:.0f}MB")
                    # Re-check memory after optimization
                    memory_result = await self._check_memory_detailed()
                    available_gb = memory_result["available_gb"]

            if available_gb < self.CRITICAL_THRESHOLD_GB:
                errors.append(f"Critical memory: {available_gb:.1f}GB (need {self.CRITICAL_THRESHOLD_GB}GB)")
                recommendations.append("🔴 Consider closing applications or using GCP cloud mode")

        elif available_gb < self.CLOUD_THRESHOLD_GB:
            # Low memory - recommend cloud mode
            warnings.append(f"Low memory: {available_gb:.1f}GB available")

            # Determine startup mode
            startup_manager = await self._get_memory_aware_startup()
            if startup_manager:
                decision = await startup_manager.determine_startup_mode()
                self._startup_mode = decision.mode.value

                if decision.use_cloud_ml:
                    recommendations.append(f"☁️  Cloud-First Mode: GCP will handle ML processing")
                    recommendations.append(f"💰 Estimated cost: ~$0.029/hour (Spot VM)")
                    recommendations.append(f"🚀 Voice unlock will be instant (cloud-powered)")

                    # Offer to activate cloud
                    if decision.gcp_vm_required:
                        recommendations.append("✨ GCP Spot VM will be activated automatically")
                        self._cloud_activated = True
                else:
                    recommendations.append(f"🏠 Local Mode: {decision.reason}")
            else:
                recommendations.append("💡 Tip: Close Chrome tabs or IDEs to free memory")

        elif available_gb < self.OPTIMIZE_THRESHOLD_GB:
            # Moderate memory - try light optimization
            optimizer = await self._get_memory_optimizer()
            if optimizer:
                suggestions = await optimizer.get_optimization_suggestions()
                if suggestions:
                    recommendations.extend([f"💡 {s}" for s in suggestions[:3]])

        else:
            # Plenty of memory - full local mode
            recommendations.append(f"✅ Sufficient memory ({available_gb:.1f}GB) - Full local mode")
            self._startup_mode = "local_full"

            if self._arm64_available:
                recommendations.append("⚡ ARM64 SIMD optimizations available (40-50x faster ML)")

        # === INTELLIGENT PORT HANDLING ===
        ports_available, ports_in_use, port_actions = ports_result
        if port_actions:
            actions_taken.extend(port_actions)
        if ports_in_use:
            warnings.append(f"Ports in use: {ports_in_use} (JARVIS processes found - will be recycled)")

        # === DISK VALIDATION ===
        if disk_result < self.config.min_disk_gb:
            errors.append(f"Insufficient disk: {disk_result:.1f}GB available")
        elif disk_result < self.config.min_disk_gb * 2:
            warnings.append(f"Low disk: {disk_result:.1f}GB available")

        # === CPU ANALYSIS ===
        cpu_count, load_avg = cpu_result
        if load_avg and load_avg[0] > cpu_count * 0.8:
            warnings.append(f"High CPU load: {load_avg[0]:.1f} (cores: {cpu_count})")
            recommendations.append("💡 Consider cloud offloading for CPU-intensive tasks")

        return ResourceStatus(
            memory_available_gb=available_gb,
            memory_total_gb=total_gb,
            disk_available_gb=disk_result,
            ports_available=ports_available,
            ports_in_use=ports_in_use,
            cpu_count=cpu_count,
            load_average=load_avg,
            warnings=warnings,
            errors=errors,
            recommendations=recommendations,
            actions_taken=actions_taken,
            startup_mode=self._startup_mode,
            cloud_activated=self._cloud_activated,
            arm64_simd_available=self._arm64_available,
            memory_pressure=memory_pressure,
        )

    async def _check_memory_detailed(self) -> Dict[str, Any]:
        """Get detailed memory analysis."""
        try:
            import psutil
            mem = psutil.virtual_memory()

            # Calculate memory pressure
            pressure = (mem.used / mem.total) * 100 if mem.total > 0 else 0

            return {
                "available_gb": mem.available / (1024**3),
                "total_gb": mem.total / (1024**3),
                "used_gb": mem.used / (1024**3),
                "pressure": pressure,
                "percent_used": mem.percent,
            }
        except Exception:
            return {
                "available_gb": 0.0,
                "total_gb": 0.0,
                "used_gb": 0.0,
                "pressure": 100.0,
                "percent_used": 100.0,
            }

    async def _check_disk(self) -> float:
        """Check available disk space."""
        try:
            import shutil
            total, used, free = shutil.disk_usage("/")
            return free / (1024**3)
        except Exception:
            return 0.0

    async def _check_ports_intelligent(self) -> Tuple[List[int], List[int], List[str]]:
        """
        Intelligently check and handle port conflicts.

        If a port is in use by a JARVIS process, it will be marked for recycling.
        """
        import socket

        available = []
        in_use = []
        actions = []

        for port in self.config.required_ports:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                result = sock.connect_ex(('localhost', port))
                sock.close()

                if result == 0:
                    in_use.append(port)
                    # Check if it's a JARVIS process (will be recycled by cleanup)
                    is_jarvis = await self._is_jarvis_port(port)
                    if is_jarvis:
                        actions.append(f"Port {port}: JARVIS process detected (will recycle)")
                else:
                    available.append(port)
            except Exception:
                available.append(port)

        return available, in_use, actions

    async def _is_jarvis_port(self, port: int) -> bool:
        """Check if a port is being used by a JARVIS process."""
        try:
            import psutil
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == 'LISTEN':
                    try:
                        proc = psutil.Process(conn.pid)
                        cmdline = " ".join(proc.cmdline()).lower()
                        return any(p in cmdline for p in ["jarvis", "main.py", "start_system"])
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    async def _check_cpu(self) -> Tuple[int, Optional[Tuple[float, float, float]]]:
        """Check CPU info."""
        cpu_count = os.cpu_count() or 1
        load_avg = None

        try:
            if hasattr(os, 'getloadavg'):
                load_avg = os.getloadavg()
        except Exception:
            pass

        return cpu_count, load_avg

    def get_startup_mode(self) -> Optional[str]:
        """Get the determined startup mode."""
        return self._startup_mode

    def is_cloud_activated(self) -> bool:
        """Check if cloud mode was activated."""
        return self._cloud_activated


# Backwards compatibility alias
SystemResourceValidator = IntelligentResourceOrchestrator


# =============================================================================
# Banner and UI - Enhanced Visual Feedback
# =============================================================================

class TerminalUI:
    """Enhanced terminal UI with colors and formatting."""
    
    # ANSI color codes
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    
    @classmethod
    def print_banner(cls) -> None:
        """Print an engaging startup banner."""
        print()
        print(f"{cls.CYAN}{'=' * 65}{cls.RESET}")
        print(f"{cls.CYAN}{' ' * 10}⚡ JARVIS LIFECYCLE SUPERVISOR v3.0 ⚡{' ' * 10}{cls.RESET}")
        print(f"{cls.CYAN}{' ' * 18}Zero-Touch Edition{' ' * 18}{cls.RESET}")
        print(f"{cls.CYAN}{'=' * 65}{cls.RESET}")
        print()
        print(f"  {cls.YELLOW}🤖 Self-Updating • Self-Healing • Autonomous • AGI-Powered{cls.RESET}")
        print()
        print(f"  {cls.GRAY}The Living OS - Manages updates, restarts, and rollbacks")
        print(f"  while keeping JARVIS online and responsive.{cls.RESET}")
        print()
        print(f"  {cls.GRAY}Zero-Touch: Autonomous updates with Dead Man's Switch{cls.RESET}")
        print()
        print(f"{cls.CYAN}{'-' * 65}{cls.RESET}")
        print()

    @classmethod
    def print_phase(cls, number: int, total: int, message: str) -> None:
        """Print a phase indicator."""
        print(f"  {cls.GRAY}[{number}/{total}] {message}...{cls.RESET}")
    
    @classmethod
    def print_success(cls, message: str) -> None:
        """Print a success message."""
        print(f"  {cls.GREEN}✓{cls.RESET} {message}")
    
    @classmethod
    def print_info(cls, key: str, value: str, highlight: bool = False) -> None:
        """Print an info line."""
        value_str = f"{cls.BOLD}{value}{cls.RESET}" if highlight else value
        print(f"  {cls.GREEN}●{cls.RESET} {key}: {value_str}")
    
    @classmethod
    def print_warning(cls, message: str) -> None:
        """Print a warning message."""
        print(f"  {cls.YELLOW}⚠{cls.RESET} {message}")
    
    @classmethod
    def print_error(cls, message: str) -> None:
        """Print an error message."""
        print(f"  {cls.RED}✗{cls.RESET} {message}")
    
    @classmethod
    def print_divider(cls) -> None:
        """Print a divider line."""
        print()
        print(f"{cls.CYAN}{'-' * 65}{cls.RESET}")
        print()
    
    @classmethod
    def print_process_list(cls, processes: List[ProcessInfo]) -> None:
        """Print discovered processes."""
        print(f"  {cls.YELLOW}●{cls.RESET} Found {len(processes)} existing instance(s):")
        for proc in processes:
            age_min = proc.age_seconds / 60
            print(f"    └─ PID {proc.pid} ({age_min:.1f} min, {proc.memory_mb:.0f}MB)")
        print()


# =============================================================================
# Hot Reload Watcher - Intelligent Polyglot File Change Detection v5.0
# =============================================================================

class FileTypeCategory(Enum):
    """Categories of file types for intelligent restart decisions."""
    BACKEND_CODE = "backend_code"       # Python, Rust - requires backend restart
    FRONTEND_CODE = "frontend_code"     # JS, JSX, TS, TSX, CSS, HTML - may need frontend rebuild
    NATIVE_CODE = "native_code"         # Swift, Rust - may need recompilation
    CONFIG = "config"                   # YAML, TOML, JSON - configuration changes
    SCRIPT = "script"                   # Shell scripts - utility scripts
    DOCS = "docs"                       # Markdown, text - documentation (usually no restart)
    BUILD = "build"                     # Cargo.toml, package.json - build configs
    UNKNOWN = "unknown"


@dataclass
class FileTypeInfo:
    """Information about a file type."""
    extension: str
    category: FileTypeCategory
    requires_restart: bool
    restart_target: str  # "backend", "frontend", "native", "all", "none"
    description: str


class IntelligentFileTypeRegistry:
    """
    Dynamically discovers and categorizes file types in the codebase.
    
    Instead of hardcoding patterns, this registry:
    1. Scans the codebase to discover all file types
    2. Categorizes them intelligently
    3. Determines restart requirements for each type
    """
    
    # Known file type mappings (extensible, not exhaustive)
    KNOWN_TYPES: Dict[str, FileTypeInfo] = {
        # Backend code (requires backend restart)
        ".py": FileTypeInfo(".py", FileTypeCategory.BACKEND_CODE, True, "backend", "Python"),
        ".pyx": FileTypeInfo(".pyx", FileTypeCategory.BACKEND_CODE, True, "backend", "Cython"),
        ".pyi": FileTypeInfo(".pyi", FileTypeCategory.BACKEND_CODE, False, "none", "Python type stubs"),
        
        # Rust (native extensions - may need rebuild)
        ".rs": FileTypeInfo(".rs", FileTypeCategory.NATIVE_CODE, True, "backend", "Rust"),
        
        # Swift (native macOS code - may need rebuild)
        ".swift": FileTypeInfo(".swift", FileTypeCategory.NATIVE_CODE, True, "backend", "Swift"),
        
        # Frontend code
        ".js": FileTypeInfo(".js", FileTypeCategory.FRONTEND_CODE, True, "frontend", "JavaScript"),
        ".jsx": FileTypeInfo(".jsx", FileTypeCategory.FRONTEND_CODE, True, "frontend", "React JSX"),
        ".ts": FileTypeInfo(".ts", FileTypeCategory.FRONTEND_CODE, True, "frontend", "TypeScript"),
        ".tsx": FileTypeInfo(".tsx", FileTypeCategory.FRONTEND_CODE, True, "frontend", "React TSX"),
        ".css": FileTypeInfo(".css", FileTypeCategory.FRONTEND_CODE, True, "frontend", "CSS"),
        ".scss": FileTypeInfo(".scss", FileTypeCategory.FRONTEND_CODE, True, "frontend", "SCSS"),
        ".less": FileTypeInfo(".less", FileTypeCategory.FRONTEND_CODE, True, "frontend", "LESS"),
        ".html": FileTypeInfo(".html", FileTypeCategory.FRONTEND_CODE, True, "frontend", "HTML"),
        
        # Configuration files
        ".yaml": FileTypeInfo(".yaml", FileTypeCategory.CONFIG, True, "backend", "YAML config"),
        ".yml": FileTypeInfo(".yml", FileTypeCategory.CONFIG, True, "backend", "YAML config"),
        ".toml": FileTypeInfo(".toml", FileTypeCategory.BUILD, True, "backend", "TOML config"),
        ".json": FileTypeInfo(".json", FileTypeCategory.CONFIG, False, "none", "JSON config"),  # Usually runtime
        ".env": FileTypeInfo(".env", FileTypeCategory.CONFIG, True, "all", "Environment"),
        ".ini": FileTypeInfo(".ini", FileTypeCategory.CONFIG, True, "backend", "INI config"),
        
        # Shell scripts
        ".sh": FileTypeInfo(".sh", FileTypeCategory.SCRIPT, False, "none", "Shell script"),
        ".bash": FileTypeInfo(".bash", FileTypeCategory.SCRIPT, False, "none", "Bash script"),
        ".zsh": FileTypeInfo(".zsh", FileTypeCategory.SCRIPT, False, "none", "Zsh script"),
        
        # Build files (require full rebuild)
        "Cargo.toml": FileTypeInfo("Cargo.toml", FileTypeCategory.BUILD, True, "all", "Rust build"),
        "package.json": FileTypeInfo("package.json", FileTypeCategory.BUILD, True, "frontend", "NPM package"),
        "requirements.txt": FileTypeInfo("requirements.txt", FileTypeCategory.BUILD, True, "all", "Python deps"),
        "pyproject.toml": FileTypeInfo("pyproject.toml", FileTypeCategory.BUILD, True, "all", "Python project"),
        
        # Documentation (no restart needed)
        ".md": FileTypeInfo(".md", FileTypeCategory.DOCS, False, "none", "Markdown"),
        ".txt": FileTypeInfo(".txt", FileTypeCategory.DOCS, False, "none", "Text"),
        ".rst": FileTypeInfo(".rst", FileTypeCategory.DOCS, False, "none", "RST docs"),
        
        # SQL (may need migration)
        ".sql": FileTypeInfo(".sql", FileTypeCategory.CONFIG, False, "none", "SQL"),
    }
    
    def __init__(self, repo_root: Path, logger: logging.Logger):
        self.repo_root = repo_root
        self.logger = logger
        self._discovered_extensions: Set[str] = set()
        self._file_counts: Dict[str, int] = {}
    
    def discover_file_types(self) -> Dict[str, int]:
        """
        Dynamically discover all file types in the codebase.
        Returns a dict of extension -> count.
        """
        self._discovered_extensions.clear()
        self._file_counts.clear()
        
        exclude_dirs = {
            '.git', '__pycache__', 'node_modules', 'venv', 'env',
            '.venv', 'build', 'dist', 'target', '.cursor', '.idea',
            '.vscode', 'coverage', '.pytest_cache', '.mypy_cache',
        }
        
        for root, dirs, files in os.walk(self.repo_root):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith('.')]
            
            for file in files:
                if file.startswith('.'):
                    continue
                
                # Get extension
                if '.' in file:
                    ext = '.' + file.rsplit('.', 1)[-1].lower()
                else:
                    ext = ''
                
                if ext:
                    self._discovered_extensions.add(ext)
                    self._file_counts[ext] = self._file_counts.get(ext, 0) + 1
        
        return self._file_counts
    
    def get_file_info(self, file_path: str) -> FileTypeInfo:
        """Get info about a file type."""
        path = Path(file_path)
        filename = path.name
        
        # Check exact filename match first (e.g., "Cargo.toml")
        if filename in self.KNOWN_TYPES:
            return self.KNOWN_TYPES[filename]
        
        # Check extension
        ext = path.suffix.lower()
        if ext in self.KNOWN_TYPES:
            return self.KNOWN_TYPES[ext]
        
        # Unknown type - return safe default
        return FileTypeInfo(ext, FileTypeCategory.UNKNOWN, False, "none", f"Unknown ({ext})")
    
    def get_watch_patterns(self) -> List[str]:
        """
        Dynamically generate watch patterns based on discovered file types.
        Only includes types that require restart.
        """
        patterns = []
        
        # Discover file types if not already done
        if not self._discovered_extensions:
            self.discover_file_types()
        
        # Add patterns for known restart-requiring types
        for ext in self._discovered_extensions:
            if ext in self.KNOWN_TYPES:
                info = self.KNOWN_TYPES[ext]
                if info.requires_restart:
                    patterns.append(f"**/*{ext}")
            else:
                # For unknown types, be conservative - don't watch by default
                pass
        
        # Always include important config files
        patterns.extend([
            "**/Cargo.toml",
            "**/package.json",
            "**/requirements.txt",
            "**/pyproject.toml",
        ])
        
        return list(set(patterns))  # Deduplicate
    
    def categorize_changes(self, changed_files: List[str]) -> Dict[str, List[str]]:
        """
        Categorize changed files by restart target.
        Returns dict of target -> list of files.
        """
        categorized: Dict[str, List[str]] = {
            "backend": [],
            "frontend": [],
            "native": [],
            "all": [],
            "none": [],
        }
        
        for file_path in changed_files:
            info = self.get_file_info(file_path)
            categorized[info.restart_target].append(file_path)
        
        return categorized
    
    def get_summary(self) -> str:
        """Get a summary of discovered file types."""
        if not self._file_counts:
            self.discover_file_types()
        
        # Sort by count
        sorted_types = sorted(self._file_counts.items(), key=lambda x: -x[1])
        
        lines = ["File types in codebase:"]
        for ext, count in sorted_types[:15]:  # Top 15
            info = self.KNOWN_TYPES.get(ext, None)
            if info:
                restart = "🔄" if info.requires_restart else "📝"
                lines.append(f"  {restart} {ext}: {count} files ({info.description})")
            else:
                lines.append(f"  ❓ {ext}: {count} files")
        
        if len(sorted_types) > 15:
            lines.append(f"  ... and {len(sorted_types) - 15} more types")
        
        return "\n".join(lines)


class HotReloadWatcher:
    """
    v5.0: Intelligent polyglot hot reload watcher.
    
    Features:
    - Dynamic file type discovery (no hardcoding!)
    - Category-based restart decisions (backend vs frontend)
    - Parallel file hash calculation
    - Smart debouncing and cooldown
    - Frontend rebuild support (npm run build)
    - React dev server detection (skip if HMR is active)
    """
    
    def __init__(self, config: BootstrapConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.repo_root = Path(__file__).parent
        self.frontend_dir = self.repo_root / "frontend"
        self.backend_dir = self.repo_root / "backend"
        
        # Configuration from environment
        self.enabled = os.getenv("JARVIS_DEV_MODE", "true").lower() == "true"
        self.grace_period = int(os.getenv("JARVIS_RELOAD_GRACE_PERIOD", "120"))
        self.check_interval = int(os.getenv("JARVIS_RELOAD_CHECK_INTERVAL", "10"))
        self.cooldown_seconds = int(os.getenv("JARVIS_RELOAD_COOLDOWN", "10"))
        self.verbose = os.getenv("JARVIS_RELOAD_VERBOSE", "false").lower() == "true"
        
        # Frontend-specific config
        self.frontend_auto_rebuild = os.getenv("JARVIS_FRONTEND_AUTO_REBUILD", "true").lower() == "true"
        self.frontend_dev_server_port = int(os.getenv("JARVIS_FRONTEND_DEV_PORT", "3000"))
        
        # Intelligent file type registry
        self._type_registry = IntelligentFileTypeRegistry(self.repo_root, logger)
        
        # Exclude patterns (directories and file patterns to skip)
        self.exclude_dirs = {
            '.git', '__pycache__', 'node_modules', 'venv', 'env',
            '.venv', 'build', 'dist', 'target', '.cursor', '.idea',
            '.vscode', 'coverage', '.pytest_cache', '.mypy_cache',
            'logs', 'cache', '.jarvis_cache', 'htmlcov',
        }
        self.exclude_patterns = [
            "*.pyc", "*.pyo", "*.log", "*.tmp", "*.bak",
            "*.swp", "*.swo", "*~", ".DS_Store",
        ]
        
        # State
        self._start_time = time.time()
        self._file_hashes: Dict[str, str] = {}
        self._last_restart_time = 0.0
        self._last_frontend_rebuild_time = 0.0
        self._grace_period_ended = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._restart_callback: Optional[Callable] = None
        self._frontend_callback: Optional[Callable] = None
        self._pending_changes: List[str] = []
        self._pending_frontend_changes: List[str] = []
        self._debounce_task: Optional[asyncio.Task] = None
        self._frontend_debounce_task: Optional[asyncio.Task] = None
        self._react_dev_server_running: Optional[bool] = None
    
    def set_restart_callback(self, callback: Callable) -> None:
        """Set the callback to invoke when a backend restart is needed."""
        self._restart_callback = callback
    
    def set_frontend_callback(self, callback: Callable) -> None:
        """Set the callback to invoke when a frontend rebuild is needed."""
        self._frontend_callback = callback
    
    async def _is_react_dev_server_running(self) -> bool:
        """
        Check if React dev server is running.
        If it is, we don't need to trigger rebuilds - React HMR handles it.
        """
        if self._react_dev_server_running is not None:
            return self._react_dev_server_running
        
        import socket
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', self.frontend_dev_server_port))
            sock.close()
            
            self._react_dev_server_running = (result == 0)
            
            if self._react_dev_server_running:
                self.logger.info(f"🌐 React dev server detected on port {self.frontend_dev_server_port} - HMR active")
            else:
                self.logger.info("📦 React dev server not running - will trigger rebuilds on frontend changes")
            
            return self._react_dev_server_running
        except Exception:
            self._react_dev_server_running = False
            return False
    
    async def _rebuild_frontend(self, changed_files: List[str]) -> bool:
        """
        Trigger frontend rebuild (npm run build).
        Only runs if React dev server is NOT running.
        """
        if await self._is_react_dev_server_running():
            self.logger.info("   🔄 React HMR will handle these changes automatically")
            return True
        
        if not self.frontend_auto_rebuild:
            self.logger.info("   ⚠️ Frontend auto-rebuild disabled (JARVIS_FRONTEND_AUTO_REBUILD=false)")
            return False
        
        self.logger.info("   🔨 Triggering frontend rebuild...")
        
        try:
            # Run npm run build in frontend directory
            process = await asyncio.create_subprocess_exec(
                "npm", "run", "build",
                cwd=str(self.frontend_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CI": "true"}  # Prevent interactive prompts
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
            
            if process.returncode == 0:
                self.logger.info("   ✅ Frontend rebuild completed successfully")
                return True
            else:
                self.logger.error(f"   ❌ Frontend rebuild failed: {stderr.decode()[:200]}")
                return False
                
        except asyncio.TimeoutError:
            self.logger.error("   ❌ Frontend rebuild timed out (120s)")
            return False
        except Exception as e:
            self.logger.error(f"   ❌ Frontend rebuild error: {e}")
            return False
    
    def _should_watch_file(self, file_path: Path) -> bool:
        """Determine if a file should be watched."""
        # Check if in excluded directory
        for part in file_path.parts:
            if part in self.exclude_dirs or part.startswith('.'):
                return False
        
        # Check exclude patterns
        from fnmatch import fnmatch
        for pattern in self.exclude_patterns:
            if fnmatch(file_path.name, pattern):
                return False
        
        # Check if file type requires restart
        info = self._type_registry.get_file_info(str(file_path))
        return info.requires_restart
    
    def _calculate_file_hashes_parallel(self) -> Dict[str, str]:
        """Calculate file hashes in parallel for speed."""
        import hashlib
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def hash_file(file_path: Path) -> Tuple[str, Optional[str]]:
            try:
                with open(file_path, 'rb') as f:
                    return str(file_path.relative_to(self.repo_root)), hashlib.md5(f.read()).hexdigest()
            except Exception:
                return str(file_path), None
        
        files_to_hash = []
        
        # Walk directories and find watchable files
        for root, dirs, files in os.walk(self.repo_root):
            # Filter out excluded directories
            dirs[:] = [d for d in dirs if d not in self.exclude_dirs and not d.startswith('.')]
            
            root_path = Path(root)
            for file in files:
                file_path = root_path / file
                if self._should_watch_file(file_path):
                    files_to_hash.append(file_path)
        
        # Calculate hashes in parallel
        hashes = {}
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
            futures = {executor.submit(hash_file, fp): fp for fp in files_to_hash}
            for future in as_completed(futures):
                rel_path, file_hash = future.result()
                if file_hash:
                    hashes[rel_path] = file_hash
        
        return hashes
    
    def _detect_changes(self) -> Tuple[bool, List[str], Dict[str, List[str]]]:
        """
        Detect which files have changed.
        Returns: (has_changes, changed_files, categorized_changes)
        """
        current = self._calculate_file_hashes_parallel()
        changed = []
        
        for path, hash_val in current.items():
            if path not in self._file_hashes or self._file_hashes[path] != hash_val:
                changed.append(path)
        
        # Check for deleted files
        for path in self._file_hashes:
            if path not in current:
                changed.append(f"[DELETED] {path}")
        
        self._file_hashes = current
        
        # Categorize changes
        categorized = self._type_registry.categorize_changes(changed)
        
        return len(changed) > 0, changed, categorized
    
    def _is_in_grace_period(self) -> bool:
        """Check if we're still in the startup grace period."""
        elapsed = time.time() - self._start_time
        in_grace = elapsed < self.grace_period
        
        if not in_grace and not self._grace_period_ended:
            self._grace_period_ended = True
            self.logger.info(f"⏰ Hot reload grace period ended after {elapsed:.0f}s - now active")
        
        return in_grace
    
    def _is_in_cooldown(self) -> bool:
        """Check if we're in cooldown from a recent restart."""
        return (time.time() - self._last_restart_time) < self.cooldown_seconds
    
    async def start(self) -> None:
        """Start the hot reload watcher."""
        if not self.enabled:
            self.logger.info("🔥 Hot reload disabled (JARVIS_DEV_MODE=false)")
            return
        
        # Discover and log file types
        self._type_registry.discover_file_types()
        
        if self.verbose:
            self.logger.info(self._type_registry.get_summary())
        
        # Initialize file hashes
        self._file_hashes = self._calculate_file_hashes_parallel()
        
        # Count files by category
        backend_count = 0
        frontend_count = 0
        for file_path in self._file_hashes:
            info = self._type_registry.get_file_info(file_path)
            if info.restart_target == "backend" or info.restart_target == "native":
                backend_count += 1
            elif info.restart_target == "frontend":
                frontend_count += 1
        
        # Log summary
        watch_patterns = self._type_registry.get_watch_patterns()
        file_types = sorted(set(p.split('*')[-1] for p in watch_patterns if '*' in p))
        
        self.logger.info(f"🔥 Hot reload watching {len(self._file_hashes)} files")
        self.logger.info(f"   🐍 Backend/Native: {backend_count} files")
        self.logger.info(f"   ⚛️  Frontend: {frontend_count} files")
        self.logger.info(f"   File types: {', '.join(file_types)}")
        self.logger.info(f"   Grace period: {self.grace_period}s, Check interval: {self.check_interval}s")
        
        # Start monitor task
        self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    async def stop(self) -> None:
        """Stop the hot reload watcher."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        if self._debounce_task:
            self._debounce_task.cancel()
        
        if self._frontend_debounce_task:
            self._frontend_debounce_task.cancel()
    
    async def _debounced_restart(self, delay: float = 0.5) -> None:
        """Debounce rapid backend file changes into a single restart."""
        await asyncio.sleep(delay)
        
        if self._pending_changes and self._restart_callback:
            changes = self._pending_changes.copy()
            self._pending_changes.clear()
            
            self._last_restart_time = time.time()
            await self._restart_callback(changes)
    
    async def _debounced_frontend_rebuild(self, delay: float = 1.0) -> None:
        """Debounce rapid frontend file changes into a single rebuild."""
        await asyncio.sleep(delay)
        
        if self._pending_frontend_changes:
            changes = self._pending_frontend_changes.copy()
            self._pending_frontend_changes.clear()
            
            self._last_frontend_rebuild_time = time.time()
            await self._rebuild_frontend(changes)
    
    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        # Check React dev server status on first run
        await self._is_react_dev_server_running()
        
        while True:
            try:
                await asyncio.sleep(self.check_interval)
                
                # Skip during grace period
                if self._is_in_grace_period():
                    continue
                
                # Check for changes
                has_changes, changed_files, categorized = self._detect_changes()
                
                if has_changes:
                    # Log changes by category
                    self.logger.info(f"🔥 Detected {len(changed_files)} file change(s):")
                    
                    for target, files in categorized.items():
                        if files and target != "none":
                            icon = {
                                "backend": "🐍",
                                "frontend": "⚛️",
                                "native": "🦀",
                                "all": "🌐",
                            }.get(target, "📁")
                            self.logger.info(f"   {icon} {target.upper()}: {len(files)} file(s)")
                            if self.verbose:
                                for f in files[:3]:
                                    self.logger.info(f"     └─ {f}")
                                if len(files) > 3:
                                    self.logger.info(f"     └─ ... and {len(files) - 3} more")
                    
                    # Separate backend and frontend changes
                    backend_changes = categorized.get("backend", []) + categorized.get("native", []) + categorized.get("all", [])
                    frontend_changes = categorized.get("frontend", []) + categorized.get("all", [])
                    
                    # Handle backend changes
                    if backend_changes:
                        if self._is_in_cooldown():
                            remaining = self.cooldown_seconds - (time.time() - self._last_restart_time)
                            self.logger.info(f"   ⏳ Backend cooldown ({remaining:.0f}s remaining), deferring")
                            self._pending_changes.extend(backend_changes)
                        else:
                            self._pending_changes.extend(backend_changes)
                            if self._debounce_task:
                                self._debounce_task.cancel()
                            self._debounce_task = asyncio.create_task(self._debounced_restart())
                    
                    # Handle frontend changes
                    if frontend_changes:
                        # Check frontend cooldown
                        frontend_cooldown = (time.time() - self._last_frontend_rebuild_time) < self.cooldown_seconds
                        
                        if frontend_cooldown:
                            remaining = self.cooldown_seconds - (time.time() - self._last_frontend_rebuild_time)
                            self.logger.info(f"   ⏳ Frontend cooldown ({remaining:.0f}s remaining), deferring")
                            self._pending_frontend_changes.extend(frontend_changes)
                        else:
                            self._pending_frontend_changes.extend(frontend_changes)
                            if self._frontend_debounce_task:
                                self._frontend_debounce_task.cancel()
                            self._frontend_debounce_task = asyncio.create_task(self._debounced_frontend_rebuild())
                    
                    # Log if only docs changed
                    if not backend_changes and not frontend_changes:
                        self.logger.info("   📝 Changes don't require restart (docs only)")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Hot reload monitor error: {e}")
                await asyncio.sleep(self.check_interval)


# =============================================================================
# Main Orchestrator - Intelligent Startup Sequence
# =============================================================================

class SupervisorBootstrapper:
    """
    Intelligent orchestrator for supervisor startup.

    Features:
    - Phased startup with dependency resolution
    - Parallel operations where possible
    - Graceful error handling and recovery
    - Performance tracking and reporting
    - Two-tier agentic security (v1.0)
    - Watchdog safety supervision
    """

    def __init__(self):
        self.config = BootstrapConfig()
        self.logger = setup_logging(self.config)
        self.narrator = AsyncVoiceNarrator(self.config)
        self.perf = PerformanceLogger()
        self.phase = StartupPhase.INIT
        self._shutdown_event = asyncio.Event()
        self._loading_server_process: Optional[asyncio.subprocess.Process] = None  # Track for cleanup

        # v5.0: Hot Reload Watcher for dev mode
        self._hot_reload = HotReloadWatcher(self.config, self.logger)
        self._hot_reload.set_restart_callback(self._on_hot_reload_triggered)
        self._supervisor: Optional[Any] = None  # Reference to running supervisor for restart

        # v6.0: Agentic Watchdog and Tiered Router for Two-Tier Security
        self._watchdog = None
        self._tiered_router = None
        self._agentic_runner = None  # v6.0: Unified Agentic Task Runner
        self._vbia_adapter = None    # v6.0: Tiered VBIA Adapter
        self._watchdog_enabled = os.getenv("JARVIS_WATCHDOG_ENABLED", "true").lower() == "true"
        self._tiered_routing_enabled = os.getenv("JARVIS_TIERED_ROUTING", "true").lower() == "true"
        self._agentic_runner_enabled = os.getenv("JARVIS_AGENTIC_RUNNER", "true").lower() == "true"

        # v7.0: JARVIS-Prime Tier-0 Brain Integration
        self._jarvis_prime_orchestrator = None
        self._jarvis_prime_client = None
        self._jarvis_prime_process: Optional[asyncio.subprocess.Process] = None
        self._reactor_core_watcher = None

        # v8.0: Data Flywheel (Self-Improving Learning Loop)
        self._data_flywheel = None
        self._learning_goals_manager = None
        self._training_scheduler_task = None
        self._experience_collection_task = None  # v9.1: Background experience collection

        # CRITICAL: Set CI=true to prevent npm start from hanging interactively
        # if port 3000 is taken. This ensures we fail fast or handle it automatically.
        os.environ["CI"] = "true"

        self._setup_signal_handlers()
    
    async def run(self) -> int:
        """
        Run the complete bootstrap sequence.
        
        Returns:
            Exit code (0 for success, non-zero for failure)
        """
        try:
            # Setup signal handlers
            self._setup_signal_handlers()
            
            # Print banner
            TerminalUI.print_banner()
            
            # Phase 1: Cleanup existing instances
            self.perf.start("cleanup")
            TerminalUI.print_phase(1, 4, "Checking for existing instances")
            
            cleaner = ParallelProcessCleaner(self.config, self.logger, self.narrator)
            terminated, discovered = await cleaner.discover_and_cleanup()
            
            if discovered:
                TerminalUI.print_process_list(discovered)
                TerminalUI.print_success(f"Terminated {terminated} instance(s)")

                # Wait for ports to be fully released with verification
                await self._wait_for_ports_release()
            else:
                TerminalUI.print_success("No existing instances found")

            # ═══════════════════════════════════════════════════════════════════
            # CRITICAL: Signal to start_system.py that cleanup was already done
            # This prevents the duplicate "already running on port 8010" warning
            # ═══════════════════════════════════════════════════════════════════
            os.environ["JARVIS_CLEANUP_DONE"] = "1"
            os.environ["JARVIS_CLEANUP_TIMESTAMP"] = str(time.time())
            self.logger.info("Set JARVIS_CLEANUP_DONE=1 - start_system.py will skip redundant cleanup")

            self.perf.end("cleanup")
            
            # Phase 2: Intelligent Resource Validation & Optimization
            self.perf.start("validation")
            TerminalUI.print_phase(2, 4, "Analyzing system resources")

            orchestrator = IntelligentResourceOrchestrator(self.config, self.logger)
            resources = await orchestrator.validate_and_optimize()

            # Show actions taken (if any)
            if resources.actions_taken:
                for action in resources.actions_taken:
                    print(f"  {TerminalUI.CYAN}⚡ {action}{TerminalUI.RESET}")

            # Show errors (critical)
            if resources.errors:
                for error in resources.errors:
                    TerminalUI.print_error(error)
                await self.narrator.speak("System resources insufficient. Please check the logs.", wait=True)
                return 1

            # Show warnings
            if resources.warnings:
                for warning in resources.warnings:
                    TerminalUI.print_warning(warning)

            # Show recommendations (intelligent)
            if resources.recommendations:
                for rec in resources.recommendations:
                    print(f"  {TerminalUI.CYAN}{rec}{TerminalUI.RESET}")

            # Show startup mode decision
            if resources.startup_mode:
                mode_display = {
                    "local_full": "🏠 Full Local Mode",
                    "cloud_first": "☁️  Cloud-First Mode",
                    "cloud_only": "☁️  Cloud-Only Mode"
                }.get(resources.startup_mode, resources.startup_mode)
                print(f"  {TerminalUI.GREEN}Mode: {mode_display}{TerminalUI.RESET}")

                # Voice announcement for cloud mode
                if resources.is_cloud_mode:
                    await self.narrator.speak(
                        "Using cloud mode for optimal performance.",
                        wait=False
                    )

            # Summary
            if not resources.warnings and not resources.errors:
                TerminalUI.print_success(
                    f"Resources OK ({resources.memory_available_gb:.1f}GB RAM, "
                    f"{resources.disk_available_gb:.1f}GB disk)"
                )

            # ═══════════════════════════════════════════════════════════════════
            # CRITICAL: Propagate Intelligent Decisions to Child Processes
            # ═══════════════════════════════════════════════════════════════════
            # These environment variables are inherited by start_system.py and
            # all JARVIS processes, ensuring the orchestrator's decisions are
            # respected throughout the system.
            # ═══════════════════════════════════════════════════════════════════
            if resources.startup_mode:
                os.environ["JARVIS_STARTUP_MODE"] = resources.startup_mode
                self.logger.info(f"🔄 Propagating JARVIS_STARTUP_MODE={resources.startup_mode}")

            if resources.cloud_activated:
                os.environ["JARVIS_CLOUD_ACTIVATED"] = "true"
                os.environ["JARVIS_PREFER_CLOUD_RUN"] = "true"
                self.logger.info("🔄 Propagating JARVIS_CLOUD_ACTIVATED=true")

            if resources.arm64_simd_available:
                os.environ["JARVIS_ARM64_SIMD"] = "true"
                self.logger.info("🔄 Propagating JARVIS_ARM64_SIMD=true")

            # Propagate memory constraints for adaptive loading
            os.environ["JARVIS_AVAILABLE_RAM_GB"] = str(round(resources.memory_available_gb, 1))
            os.environ["JARVIS_TOTAL_RAM_GB"] = str(round(resources.memory_total_gb, 1))

            # CRITICAL: Optimize Frontend Memory & Timeout based on resources
            # Reduce Node memory to 2GB (from default 4GB) to prevent relief pressure
            os.environ["JARVIS_FRONTEND_MEMORY_MB"] = "2048"
            # Increase timeout to 600s to prevent early shutdown on slow systems
            # (Observed startup time takes up to 4 minutes)
            os.environ["JARVIS_FRONTEND_TIMEOUT"] = "600"
            self.logger.info("🔄 Configured Frontend: 2GB RAM limit, 600s timeout")

            # Propagate warnings for downstream handling
            if resources.warnings:
                os.environ["JARVIS_STARTUP_WARNINGS"] = "|".join(resources.warnings[:5])

            # ═══════════════════════════════════════════════════════════════════
            # v3.0: Propagate Zero-Touch & AGI OS Settings to Child Processes
            # ═══════════════════════════════════════════════════════════════════
            await self._propagate_zero_touch_settings()
            await self._propagate_agi_os_settings()
            
            # ═══════════════════════════════════════════════════════════════════
            # v5.0: Initialize Intelligent Rate Orchestrator (ML Forecasting)
            # ═══════════════════════════════════════════════════════════════════
            # This starts the ML-powered rate limiting system that:
            # - Predicts rate limit breaches BEFORE they happen
            # - Adaptively throttles GCP, CloudSQL, and Claude API calls
            # - Uses time-series forecasting for intelligent request scheduling
            # ═══════════════════════════════════════════════════════════════════
            await self._initialize_rate_orchestrator()

            # ═══════════════════════════════════════════════════════════════════
            # v6.0: Initialize Two-Tier Agentic Security System
            # ═══════════════════════════════════════════════════════════════════
            # This enables the Two-Tier security model:
            # - Tier 1 (JARVIS): Safe APIs, read-only, Gemini Flash
            # - Tier 2 (JARVIS ACCESS): Full Computer Use, strict VBIA, Claude
            # - Watchdog monitors heartbeats and can trigger kill switch
            # ═══════════════════════════════════════════════════════════════════
            await self._initialize_agentic_security()

            # ═══════════════════════════════════════════════════════════════════
            # v7.0: Initialize JARVIS-Prime Tier-0 Local Brain
            # ═══════════════════════════════════════════════════════════════════
            # This starts the local inference server for cost-effective AI:
            # - Tier 0 (JARVIS-Prime): Local GGUF model, fast, free
            # - Supports local subprocess, Docker, or Cloud Run deployment
            # - Auto-integrates with Reactor-Core for model hot-swapping
            # ═══════════════════════════════════════════════════════════════════
            await self._initialize_jarvis_prime()

            # ═══════════════════════════════════════════════════════════════════
            # v9.0: Initialize Intelligence Systems (UAE/SAI/Neural Mesh/MAS)
            # ═══════════════════════════════════════════════════════════════════
            # This enables the full Agentic OS intelligence stack:
            # - UAE (Unified Awareness Engine): Screen awareness, computer vision
            # - SAI (Situational Awareness Intelligence): Window/app tracking
            # - Neural Mesh: Distributed intelligence coordination
            # - MAS (Multi-Agent System): Coordinated agent execution
            # - Continuous background web scraping for self-improvement
            # ═══════════════════════════════════════════════════════════════════
            await self._initialize_intelligence_systems()

            self.perf.end("validation")

            # Phase 3: Initialize supervisor
            self.perf.start("supervisor_init")
            TerminalUI.print_phase(3, 4, "Initializing supervisor")
            print()
            
            from core.supervisor import JARVISSupervisor
            # v3.1: Pass skip_browser_open=True to prevent duplicate browser windows
            # run_supervisor.py handles browser opening in _start_loading_page_ecosystem()
            supervisor = JARVISSupervisor(skip_browser_open=True)
            self._supervisor = supervisor  # Store reference for hot reload
            
            self._print_config_summary(supervisor)
            self.perf.end("supervisor_init")
            
            # Phase 3.5: Start Loading Page (BEFORE JARVIS spawns)
            TerminalUI.print_divider()
            print(f"  {TerminalUI.CYAN}🌐 Starting Loading Page Server...{TerminalUI.RESET}")
            
            await self._start_loading_page_ecosystem()
            
            # Phase 4: Start JARVIS
            TerminalUI.print_divider()
            TerminalUI.print_phase(4, 4, "Launching JARVIS Core")
            print()
            
            print(f"  {TerminalUI.YELLOW}📡 Watch real-time progress in the loading page!{TerminalUI.RESET}")
            
            if self.config.voice_enabled:
                print(f"  {TerminalUI.YELLOW}🔊 Voice narration enabled{TerminalUI.RESET}")
            
            print()
            
            # Run supervisor with startup monitoring
            self.perf.start("jarvis")
            
            # v4.0: Run startup monitoring in parallel with supervisor
            # This ensures completion is broadcast even if jarvis_supervisor's
            # internal monitoring doesn't complete (e.g., due to service initialization delays)
            if self._loading_server_process:
                # Start monitoring as a background task
                monitoring_task = asyncio.create_task(
                    self._monitor_jarvis_startup(max_wait=120.0)
                )
                self.logger.info("🔍 Startup monitoring task started")
            else:
                monitoring_task = None
            
            # v5.0: Start hot reload watcher (dev mode)
            await self._hot_reload.start()
            
            try:
                await supervisor.run()
            finally:
                # Cleanup remote resources (VMs)
                await self.cleanup_resources()
                
                # Stop hot reload watcher
                await self._hot_reload.stop()
                
                # Cancel monitoring if still running
                if monitoring_task and not monitoring_task.done():
                    monitoring_task.cancel()
                    try:
                        await monitoring_task
                    except asyncio.CancelledError:
                        pass
            
            self.perf.end("jarvis")

            return 0

        except KeyboardInterrupt:
            print(f"\n{TerminalUI.YELLOW}👋 Supervisor interrupted by user{TerminalUI.RESET}")
            await self.narrator.speak("Supervisor shutting down. Goodbye.", wait=True)
            return 130

        except Exception as e:
            self.logger.exception(f"Bootstrap failed: {e}")
            TerminalUI.print_error(f"Bootstrap failed: {e}")
            await self.narrator.speak("An error occurred. Please check the logs.", wait=True)
            return 1
        
        finally:
            # ═══════════════════════════════════════════════════════════════════
            # GRACEFUL SHUTDOWN: Give Chrome time to redirect before killing server
            # ═══════════════════════════════════════════════════════════════════
            # When JARVIS starts successfully, the loading page automatically
            # redirects Chrome to the main app (localhost:3000). If we terminate
            # the loading server too quickly, Chrome may show:
            # "window terminated unexpectedly (reason: 'killed', code: '15')"
            #
            # Solution: Wait for Chrome to complete its redirect before shutting down.
            # ═══════════════════════════════════════════════════════════════════
            if self._loading_server_process:
                try:
                    # Check if JARVIS startup was successful (env var set by supervisor)
                    startup_complete = os.environ.get("JARVIS_STARTUP_COMPLETE") == "true"

                    if startup_complete:
                        # Give Chrome 2 seconds to redirect to main app
                        # The loading-manager.js has a 1s fade-out animation before redirect
                        self.logger.debug("Waiting for Chrome to complete redirect...")
                        await asyncio.sleep(2.0)

                    # Send SIGINT first for graceful shutdown (allows cleanup handlers to run)
                    self._loading_server_process.send_signal(signal.SIGINT)
                    try:
                        await asyncio.wait_for(self._loading_server_process.wait(), timeout=3.0)
                        self.logger.info("Loading server gracefully terminated")
                    except asyncio.TimeoutError:
                        # SIGINT didn't work, try SIGTERM
                        self._loading_server_process.terminate()
                        try:
                            await asyncio.wait_for(self._loading_server_process.wait(), timeout=2.0)
                            self.logger.info("Loading server terminated")
                        except asyncio.TimeoutError:
                            # Still not dead, force kill
                            self._loading_server_process.kill()
                            self.logger.warning("Loading server force killed (timeout)")

                except ProcessLookupError:
                    # Process already exited
                    self.logger.debug("Loading server already exited")
                except Exception as e:
                    self.logger.debug(f"Loading server cleanup error: {e}")
            
            # Log performance summary
            summary = self.perf.get_summary()
            self.logger.info(f"Bootstrap performance: {summary}")
    
    async def _start_loading_page_ecosystem(self) -> None:
        """
        Start the loading page ecosystem BEFORE JARVIS spawns.
        
        This method:
        1. Starts the loading_server.py subprocess
        2. Waits for server to be ready (with retries)
        3. Opens Chrome Incognito to the loading page
        4. Sets JARVIS_SUPERVISOR_LOADING=1 so start_system.py knows to skip browser ops
        
        This ensures the user sees the loading page immediately when running
        under supervisor, just like when running start_system.py directly.
        """
        loading_port = self.config.required_ports[2]  # 3001
        loading_url = f"http://localhost:{loading_port}"
        
        try:
            # Step 1: Start loading server subprocess
            loading_server_script = Path(__file__).parent / "loading_server.py"
            
            if not loading_server_script.exists():
                self.logger.warning(f"Loading server script not found: {loading_server_script}")
                print(f"  {TerminalUI.YELLOW}⚠️  Loading server not available - will skip browser{TerminalUI.RESET}")
                return
            
            self.logger.info(f"Starting loading server: {loading_server_script}")
            
            # Start as async subprocess
            self._loading_server_process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(loading_server_script),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            
            print(f"  {TerminalUI.GREEN}✓ Loading server started (PID {self._loading_server_process.pid}){TerminalUI.RESET}")
            
            # Step 2: Wait for server to be ready (intelligent adaptive health check)
            import aiohttp
            
            server_ready = False
            health_url = f"{loading_url}/health"
            
            # Adaptive retry configuration
            initial_delay = 0.1  # Start fast
            max_delay = 1.0      # Cap at 1 second
            max_wait_time = 15.0 # Total wait budget (seconds)
            timeout_per_request = 1.0  # Generous timeout per request
            
            start_time = time.time()
            attempt = 0
            current_delay = initial_delay
            
            # Single session for connection reuse
            connector = aiohttp.TCPConnector(
                limit=1,
                enable_cleanup_closed=True,
                force_close=False,
            )
            
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=timeout_per_request)
            ) as session:
                while (time.time() - start_time) < max_wait_time:
                    attempt += 1
                    try:
                        async with session.get(health_url) as resp:
                            if resp.status == 200:
                                server_ready = True
                                elapsed = time.time() - start_time
                                self.logger.info(
                                    f"Loading server ready after {attempt} attempts ({elapsed:.2f}s)"
                                )
                                break
                            else:
                                self.logger.debug(
                                    f"Health check attempt {attempt}: status {resp.status}"
                                )
                    except aiohttp.ClientConnectorError:
                        # Server not listening yet - this is expected during startup
                        pass
                    except asyncio.TimeoutError:
                        # Request timed out - server might be slow
                        self.logger.debug(f"Health check attempt {attempt}: timeout")
                    except Exception as e:
                        self.logger.debug(f"Health check attempt {attempt}: {type(e).__name__}")
                    
                    # Adaptive backoff: start fast, slow down over time
                    await asyncio.sleep(current_delay)
                    current_delay = min(current_delay * 1.5, max_delay)
                    
                    # Progress indicator every ~2 seconds
                    if attempt % 5 == 0:
                        elapsed = time.time() - start_time
                        print(f"  {TerminalUI.CYAN}⏳ Waiting for loading server... ({elapsed:.1f}s){TerminalUI.RESET}")
            
            if not server_ready:
                elapsed = time.time() - start_time
                self.logger.warning(
                    f"Loading server didn't respond after {attempt} attempts ({elapsed:.1f}s)"
                )
                # Check if process is still running
                if self._loading_server_process.returncode is not None:
                    print(f"  {TerminalUI.RED}✗ Loading server exited unexpectedly (code: {self._loading_server_process.returncode}){TerminalUI.RESET}")
                else:
                    print(f"  {TerminalUI.YELLOW}⚠️  Loading server slow to respond - continuing (may still be starting){TerminalUI.RESET}")
            else:
                print(f"  {TerminalUI.GREEN}✓ Loading server ready at {loading_url}{TerminalUI.RESET}")
            
            # Step 3: Intelligent Chrome window management (v4.0 - Clean Slate)
            # - Close ALL existing JARVIS windows (localhost:3000, :3001, :8010)
            # - Open ONE fresh incognito window
            # - This ensures a clean, predictable single-window experience
            if platform.system() == "Darwin":  # macOS
                try:
                    opened = await self._ensure_single_jarvis_window(loading_url)
                    if opened:
                        print(f"  {TerminalUI.GREEN}✓ Single JARVIS window ready{TerminalUI.RESET}")
                    else:
                        print(f"  {TerminalUI.YELLOW}⚠️  Could not open Chrome automatically{TerminalUI.RESET}")
                        print(f"  {TerminalUI.CYAN}💡 Open manually: {loading_url}{TerminalUI.RESET}")
                except Exception as e:
                    self.logger.debug(f"Failed to open Chrome: {e}")
                    print(f"  {TerminalUI.YELLOW}⚠️  Could not open Chrome automatically{TerminalUI.RESET}")
                    print(f"  {TerminalUI.CYAN}💡 Open manually: {loading_url}{TerminalUI.RESET}")
            
            # Step 4: Set environment variable to signal start_system.py
            os.environ["JARVIS_SUPERVISOR_LOADING"] = "1"
            self.logger.info("Set JARVIS_SUPERVISOR_LOADING=1")
            
            # Voice narration
            await self.narrator.speak("Loading page ready. Starting JARVIS core.", wait=False)
            
            print()  # Blank line for readability
            
        except Exception as e:
            self.logger.exception(f"Failed to start loading page ecosystem: {e}")
            print(f"  {TerminalUI.YELLOW}⚠️  Loading page failed: {e}{TerminalUI.RESET}")
            print(f"  {TerminalUI.CYAN}💡 JARVIS will start without loading page{TerminalUI.RESET}")

    # ═══════════════════════════════════════════════════════════════════════════
    # BROWSER LOCK FILE - Prevents race conditions across all processes
    # ═══════════════════════════════════════════════════════════════════════════
    BROWSER_LOCK_FILE = Path("/tmp/jarvis_browser.lock")
    BROWSER_PID_FILE = Path("/tmp/jarvis_browser_opener.pid")
    
    async def _acquire_browser_lock(self) -> bool:
        """
        Acquire exclusive lock for browser operations.
        
        Uses file-based locking to prevent multiple processes from
        opening browser windows simultaneously.
        
        Returns:
            True if lock acquired, False if another process holds it
        """
        try:
            # Check if lock exists and is recent (within 30 seconds)
            if self.BROWSER_LOCK_FILE.exists():
                lock_age = time.time() - self.BROWSER_LOCK_FILE.stat().st_mtime
                if lock_age < 30:
                    # Check if the PID that created it is still running
                    if self.BROWSER_PID_FILE.exists():
                        try:
                            pid = int(self.BROWSER_PID_FILE.read_text().strip())
                            # Check if process is still alive
                            os.kill(pid, 0)
                            self.logger.debug(f"Browser lock held by PID {pid}")
                            return False
                        except (ProcessLookupError, ValueError):
                            # Process is dead, we can take the lock
                            pass
                    else:
                        self.logger.debug(f"Browser lock exists but no PID file, age={lock_age:.1f}s")
                        return False
            
            # Acquire lock
            self.BROWSER_LOCK_FILE.write_text(str(time.time()))
            self.BROWSER_PID_FILE.write_text(str(os.getpid()))
            self.logger.debug(f"Acquired browser lock (PID {os.getpid()})")
            return True
            
        except Exception as e:
            self.logger.debug(f"Lock acquisition error: {e}")
            return False
    
    def _release_browser_lock(self) -> None:
        """Release the browser lock."""
        try:
            if self.BROWSER_LOCK_FILE.exists():
                self.BROWSER_LOCK_FILE.unlink()
            if self.BROWSER_PID_FILE.exists():
                self.BROWSER_PID_FILE.unlink()
            self.logger.debug("Released browser lock")
        except Exception as e:
            self.logger.debug(f"Lock release error: {e}")

    async def _close_all_jarvis_windows(self) -> int:
        """
        AGGRESSIVELY close ALL Chrome incognito windows + JARVIS-related regular windows.
        
        v5.0: Multi-phase approach with verification
        1. First pass: Close all incognito + JARVIS windows
        2. Verify: Check if any remain
        3. Force: Repeatedly close until none remain
        4. Nuclear: Quit Chrome entirely if still stuck
        
        Returns:
            Number of windows closed
        """
        total_closed = 0
        
        # Phase 1: Check if Chrome is even running
        try:
            check_chrome = '''
            tell application "System Events"
                return (exists process "Google Chrome")
            end tell
            '''
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript", "-e", check_chrome,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            chrome_running = stdout.decode().strip().lower() == "true"
            
            if not chrome_running:
                self.logger.debug("Chrome not running - no windows to close")
                return 0
        except Exception:
            pass
        
        # Phase 2: First pass - close all incognito and JARVIS windows
        try:
            applescript = '''
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
                closed = int(stdout.decode().strip() or "0")
                total_closed += closed
                if closed > 0:
                    self.logger.info(f"🗑️ Phase 1: Closed {closed} window(s)")
            except ValueError:
                pass
        except Exception as e:
            self.logger.debug(f"Phase 1 failed: {e}")
        
        await asyncio.sleep(0.5)
        
        # Phase 3: Force-close any remaining incognito windows (loop until done)
        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                force_script = '''
                tell application "Google Chrome"
                    set incogCount to count of (windows whose mode is "incognito")
                    if incogCount = 0 then
                        return "0:done"
                    end if
                    
                    try
                        close (first window whose mode is "incognito")
                        return "1:closed"
                    on error
                        return "0:error"
                    end try
                end tell
                '''
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/osascript", "-e", force_script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await process.communicate()
                result = stdout.decode().strip()
                
                if "done" in result:
                    break
                elif "closed" in result:
                    total_closed += 1
                    await asyncio.sleep(0.3)
                else:
                    break
            except Exception:
                break
        
        if total_closed > 0:
            self.logger.info(f"🧹 Total: Closed {total_closed} JARVIS window(s)")
            await asyncio.sleep(1.0)  # Let Chrome fully process
        
        return total_closed

    async def _open_fresh_incognito_window(self, url: str) -> bool:
        """
        Open exactly ONE fresh Chrome incognito window with fullscreen mode.
        
        v5.0: Verifies only one window is created after opening.
        
        Args:
            url: The URL to open
            
        Returns:
            True if successful, False otherwise
        """
        # First, count existing incognito windows
        initial_count = 0
        try:
            count_script = '''
            tell application "Google Chrome"
                return count of (windows whose mode is "incognito")
            end tell
            '''
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript", "-e", count_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            initial_count = int(stdout.decode().strip() or "0")
            
            if initial_count > 0:
                self.logger.warning(f"⚠️ {initial_count} incognito windows still exist before opening new one!")
        except Exception:
            pass
        
        # Open the window using AppleScript for precise control
        try:
            applescript = f'''
            tell application "Google Chrome"
                -- Create exactly ONE new incognito window
                set newWindow to make new window with properties {{mode:"incognito"}}
                delay 0.5
                
                -- Navigate to URL
                tell newWindow
                    set URL of active tab to "{url}"
                end tell
                
                -- Bring to front and activate
                set index of newWindow to 1
                activate
            end tell
            
            -- Enter fullscreen mode (Cmd+Ctrl+F)
            delay 1.0
            tell application "System Events"
                tell process "Google Chrome"
                    set frontmost to true
                    delay 0.3
                    -- Use menu bar for more reliable fullscreen
                    try
                        click menu item "Enter Full Screen" of menu "View" of menu bar 1
                    on error
                        -- Fallback to keyboard shortcut
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
            result = stdout.decode().strip().lower()
            
            if result == "true":
                # Verify we have exactly ONE incognito window
                await asyncio.sleep(0.5)
                try:
                    process = await asyncio.create_subprocess_exec(
                        "/usr/bin/osascript", "-e", count_script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout, _ = await process.communicate()
                    final_count = int(stdout.decode().strip() or "0")
                    
                    if final_count > 1:
                        self.logger.warning(f"⚠️ Multiple incognito windows detected ({final_count}), closing extras...")
                        # Close extras
                        for _ in range(final_count - 1):
                            await self._close_one_incognito_window()
                    
                    self.logger.info(f"🌐 Single JARVIS incognito window opened: {url}")
                except Exception:
                    pass
                
                return True
            
        except Exception as e:
            self.logger.debug(f"AppleScript failed: {e}")
        
        # Fallback to command line (less precise)
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-na", "Google Chrome",
                "--args", "--incognito", "--new-window", "--start-fullscreen", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            self.logger.info(f"🌐 Opened Chrome incognito via command line: {url}")
            return True
        except Exception as e:
            self.logger.warning(f"⚠️ Could not open browser: {e}")
            return False
    
    async def _close_one_incognito_window(self) -> bool:
        """Close a single incognito window."""
        try:
            script = '''
            tell application "Google Chrome"
                try
                    close (first window whose mode is "incognito")
                    return true
                on error
                    return false
                end try
            end tell
            '''
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            return stdout.decode().strip().lower() == "true"
        except Exception:
            return False

    async def _ensure_single_jarvis_window(self, url: str) -> bool:
        """
        Intelligent window manager: ensures exactly ONE Chrome window for JARVIS.
        
        Strategy (v5.0 - Lock-Based Clean Slate):
        1. Acquire exclusive browser lock (prevents race conditions)
        2. Close ALL existing JARVIS-related windows
        3. Open ONE fresh incognito window
        4. Release lock
        
        The lock prevents multiple processes from opening browsers simultaneously.
        
        Args:
            url: The URL to open (typically localhost:3001 for loading page)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Step 0: Acquire exclusive browser lock
            if not await self._acquire_browser_lock():
                self.logger.info("🔒 Another process is managing browser - skipping")
                # Wait a bit and check if browser is ready
                await asyncio.sleep(2.0)
                return True  # Assume the other process handled it
            
            try:
                # Step 1: Close all existing JARVIS windows
                closed_count = await self._close_all_jarvis_windows()
                
                if closed_count > 0:
                    self.logger.info(f"🧹 Cleaned up {closed_count} existing JARVIS window(s)")
                    await asyncio.sleep(1.0)  # Let Chrome fully process closures
                
                # Step 2: Open fresh incognito window
                success = await self._open_fresh_incognito_window(url)
                
                if success:
                    self.logger.info(f"✅ Single JARVIS window ready at {url}")
                else:
                    self.logger.warning(f"⚠️ Failed to open browser - please open {url} manually")
                
                return success
                
            finally:
                # Always release lock when done
                self._release_browser_lock()
            
        except Exception as e:
            self.logger.exception(f"Window management error: {e}")
            self._release_browser_lock()
            return False
    
    async def _broadcast_to_loading_page(
        self,
        stage: str,
        message: str,
        progress: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Broadcast progress update to the loading page.
        
        v4.0: Supervisor takes responsibility for completion broadcasting
        when JARVIS_SUPERVISOR_LOADING=1 is set.
        
        Args:
            stage: Current stage name
            message: Human-readable message
            progress: Progress percentage (0-100)
            metadata: Optional metadata dict
            
        Returns:
            True if broadcast succeeded, False otherwise
        """
        if not self._loading_server_process:
            return False
        
        try:
            import aiohttp
            from datetime import datetime
            
            loading_port = self.config.required_ports[2]  # 3001
            url = f"http://localhost:{loading_port}/api/update-progress"
            
            data = {
                "stage": stage,
                "message": message,
                "progress": progress,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {},
            }
            
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2.0)
            ) as session:
                async with session.post(url, json=data) as resp:
                    if resp.status == 200:
                        self.logger.debug(f"📡 Broadcast: {stage} ({progress}%)")
                        return True
                    else:
                        self.logger.debug(f"Broadcast failed: status {resp.status}")
                        return False
                        
        except Exception as e:
            self.logger.debug(f"Broadcast failed: {e}")
            return False
    
    async def _monitor_jarvis_startup(self, max_wait: float = 120.0) -> bool:
        """
        Monitor JARVIS startup and broadcast progress to loading page.
        
        v4.0: This is the CRITICAL missing piece that was causing the 97% hang.
        v4.1: Now checks /health/ready for OPERATIONAL readiness, not just HTTP response.
              This prevents "OFFLINE - SEARCHING FOR BACKEND" by ensuring services
              are actually ready before broadcasting completion.
        v9.0: ADAPTIVE TIMEOUT - Extends wait time when heavy initialization detected
              (Docker startup, Cloud Run initialization, ML model loading, etc.)
        
        Args:
            max_wait: Maximum time to wait for JARVIS to be ready
            
        Returns:
            True if JARVIS is ready, False if timeout
        """
        import aiohttp
        
        backend_port = self.config.required_ports[0]  # 8010
        frontend_port = self.config.required_ports[1]  # 3000
        
        start_time = time.time()
        last_progress = 0
        backend_http_ready = False
        backend_operational = False
        frontend_ready = False
        last_status = None
        
        # v9.0: Adaptive timeout tracking
        adaptive_max_wait = max_wait
        timeout_extended = False
        extension_reasons: List[str] = []
        
        # Check for conditions that require extended timeouts
        docker_needs_start = not self._is_docker_running()
        if docker_needs_start:
            adaptive_max_wait += 60  # +60s for Docker startup
            extension_reasons.append("Docker startup")
        
        # Check if this is a cold start (first run after long idle)
        if os.getenv("JARVIS_COLD_START") == "1":
            adaptive_max_wait += 30  # +30s for cold start
            extension_reasons.append("Cold start")
        
        if extension_reasons:
            self.logger.info(f"⏱️  Adaptive timeout: {adaptive_max_wait:.0f}s (base: {max_wait}s)")
            self.logger.info(f"   Extensions: {', '.join(extension_reasons)}")
        
        self.logger.info("🔍 Monitoring JARVIS startup (v9.0 - adaptive timeout)...")

        while (time.time() - start_time) < adaptive_max_wait:
            elapsed = time.time() - start_time
            
            # Phase 1a: Check backend HTTP health
            if not backend_http_ready:
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=3.0)
                    ) as session:
                        async with session.get(
                            f"http://localhost:{backend_port}/health"
                        ) as resp:
                            if resp.status == 200:
                                backend_http_ready = True
                                self.logger.info("✅ Backend HTTP responding")
                                await self._broadcast_to_loading_page(
                                    "backend_http",
                                    "Backend server online",
                                    70,
                                    {"icon": "⏳", "label": "Backend Starting"}
                                )
                except Exception:
                    pass
            
            # Phase 1b: Check backend OPERATIONAL readiness
            if backend_http_ready and not backend_operational:
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=5.0)
                    ) as session:
                        async with session.get(
                            f"http://localhost:{backend_port}/health/ready"
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                status = data.get("status", "unknown")
                                
                                # Log status changes
                                if status != last_status:
                                    self.logger.info(f"📊 Backend status: {status}")
                                    last_status = status
                                
                                # Check for operational readiness
                                # v10.0: Progressive readiness - accept when ready=True from backend
                                is_ready = (
                                    data.get("ready") == True or
                                    data.get("operational") == True or
                                    status in ["ready", "operational", "degraded", "warming_up", "websocket_ready"]
                                )
                                
                                # Also accept if WebSocket is ready (core functionality)
                                details = data.get("details", {})
                                websocket_ready = details.get("websocket_ready", False)
                                
                                # v10.0: WebSocket ready = immediately interactive
                                # No need to wait for ML models - they warm in background
                                if websocket_ready and not is_ready:
                                    self.logger.info(f"✅ WebSocket ready - accepting as interactive")
                                    is_ready = True
                                
                                # v10.0: Accept any status where ready=True (backend makes decision)
                                # This trusts the backend's progressive readiness logic
                                if data.get("ready") == True and not is_ready:
                                    self.logger.info(f"✅ Backend reports ready={data.get('ready')} status={status}")
                                    is_ready = True
                                
                                if is_ready:
                                    backend_operational = True
                                    self.logger.info("✅ Backend operationally ready")
                                    await self._broadcast_to_loading_page(
                                        "backend_ready",
                                        "Backend services ready",
                                        85,
                                        {"icon": "✅", "label": "Backend Ready", "backend_ready": True}
                                    )
                                
                                # v9.0: Dynamic timeout extension for slow services
                                # If we detect heavy initialization in progress, extend the timeout
                                if not timeout_extended:
                                    ml_warmup = data.get("ml_warmup_info", {})
                                    is_warming = ml_warmup.get("is_warming_up", False)
                                    
                                    # Detect Docker/Cloud Run initialization
                                    cloud_init = data.get("cloud_init", {})
                                    docker_starting = cloud_init.get("docker_starting", False)
                                    cloud_run_init = cloud_init.get("cloud_run_initializing", False)
                                    
                                    if is_warming and elapsed > 60:
                                        # ML models taking long - extend timeout
                                        adaptive_max_wait = max(adaptive_max_wait, start_time + elapsed + 60)
                                        timeout_extended = True
                                        self.logger.info(f"⏱️  Extended timeout for ML warmup: +60s (new max: {adaptive_max_wait - start_time:.0f}s)")
                                    
                                    if docker_starting or cloud_run_init:
                                        # Cloud services initializing - extend timeout
                                        adaptive_max_wait = max(adaptive_max_wait, start_time + elapsed + 90)
                                        timeout_extended = True
                                        service = "Docker" if docker_starting else "Cloud Run"
                                        self.logger.info(f"⏱️  Extended timeout for {service}: +90s (new max: {adaptive_max_wait - start_time:.0f}s)")
                                        
                except Exception as e:
                    # /health/ready might not exist - fallback after 45s if /health works
                    if backend_http_ready and elapsed > 45:
                        backend_operational = True
                        self.logger.warning(f"⚠️ /health/ready unavailable, accepting after {elapsed:.0f}s")
                        await self._broadcast_to_loading_page(
                            "backend_ready",
                            "Backend services ready (fallback)",
                            85,
                            {"icon": "⚠️", "label": "Backend Ready", "backend_ready": True}
                        )
            
            # Phase 2: Check frontend (only after backend is operational)
            if backend_operational and not frontend_ready:
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=2.0)
                    ) as session:
                        async with session.get(
                            f"http://localhost:{frontend_port}"
                        ) as resp:
                            if resp.status in [200, 304]:
                                frontend_ready = True
                                self.logger.info("✅ Frontend ready")
                                await self._broadcast_to_loading_page(
                                    "frontend_ready",
                                    "Frontend interface ready",
                                    95,
                                    {"icon": "✅", "label": "Frontend Ready", "frontend_ready": True}
                                )
                except Exception:
                    pass
            
            # Phase 3: Both OPERATIONALLY ready = complete!
            if backend_operational and frontend_ready:
                self.logger.info("🎉 JARVIS startup complete (all systems operational)!")
                
                # Broadcast 100% completion
                await self._broadcast_to_loading_page(
                    "complete",
                    "JARVIS is online!",
                    100,
                    {
                        "icon": "✅",
                        "label": "Complete",
                        "sublabel": "All systems operational!",
                        "success": True,
                        "backend_ready": True,
                        "frontend_verified": True,
                        "redirect_url": f"http://localhost:{frontend_port}",
                    }
                )
                
                return True
            
            # Broadcast periodic progress updates
            progress = 50 + int((elapsed / max_wait) * 40)  # 50-90%
            if progress > last_progress + 5:  # Update every 5%
                last_progress = progress
                
                if not backend_http_ready:
                    status_msg = f"Starting backend... ({int(elapsed)}s)"
                    stage = "backend_starting"
                elif not backend_operational:
                    status_msg = f"Backend initializing services... ({int(elapsed)}s)"
                    stage = "backend_init"
                elif not frontend_ready:
                    status_msg = f"Starting frontend... ({int(elapsed)}s)"
                    stage = "frontend_starting"
                else:
                    status_msg = "Finalizing..."
                    stage = "finalizing"
                
                await self._broadcast_to_loading_page(
                    stage,
                    status_msg,
                    min(progress, 94),  # Cap at 94% until truly complete
                    {"icon": "⏳", "label": "Starting", "sublabel": status_msg}
                )
            
            await asyncio.sleep(1.5)  # Slightly slower polling
        
        # Timeout - Only broadcast if we have SOME progress
        self.logger.warning(f"⚠️ JARVIS startup monitoring timeout after {adaptive_max_wait}s")
        self.logger.warning(f"   Status: http={backend_http_ready}, operational={backend_operational}, frontend={frontend_ready}")
        if extension_reasons:
            self.logger.warning(f"   Timeout was extended for: {', '.join(extension_reasons)}")
        
        # If backend is operational and frontend is ready, we can complete
        if backend_operational and frontend_ready:
            await self._broadcast_to_loading_page(
                "complete",
                "JARVIS is online!",
                100,
                {
                    "icon": "✅",
                    "label": "Complete",
                    "sublabel": "System ready!",
                    "success": True,
                    "backend_ready": True,
                    "frontend_verified": True,
                    "redirect_url": f"http://localhost:{frontend_port}",
                }
            )
            return True
        
        # If only backend HTTP is responding (not operational), broadcast warning
        if backend_http_ready:
            await self._broadcast_to_loading_page(
                "startup_slow",
                "JARVIS is starting slowly...",
                90,
                {
                    "icon": "⚠️",
                    "label": "Slow Startup",
                    "sublabel": "Services still initializing...",
                    "backend_ready": backend_operational,
                    "frontend_verified": frontend_ready,
                    "warning": "Startup took longer than expected",
                }
            )
        
        # Return true if backend is at least operational
        return backend_operational
    
    def _is_docker_running(self) -> bool:
        """
        v9.0: Check if Docker daemon is currently running.
        Used for adaptive timeout calculation.
        
        Returns:
            True if Docker is running, False otherwise
        """
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=3.0
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            return False
    
    async def _wait_for_ports_release(self, max_wait: float = 5.0) -> bool:
        """
        Wait for all critical ports to be fully released after cleanup.

        This prevents race conditions where start_system.py checks ports
        before they're fully released by the OS.

        Args:
            max_wait: Maximum time to wait in seconds

        Returns:
            True if all ports are free, False if timeout
        """
        import socket

        start_time = time.time()
        check_interval = 0.2

        while time.time() - start_time < max_wait:
            all_free = True

            for port in self.config.required_ports:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.1)
                    result = sock.connect_ex(('localhost', port))
                    sock.close()

                    if result == 0:  # Port is still in use
                        all_free = False
                        break
                except Exception:
                    pass  # Error connecting = port is free

            if all_free:
                self.logger.debug(f"All ports released after {time.time() - start_time:.1f}s")
                return True

            await asyncio.sleep(check_interval)

        self.logger.warning(f"Timeout waiting for ports to release after {max_wait}s")
        return False

    def _setup_signal_handlers(self) -> None:
        """
        Setup signal handlers for graceful shutdown with VM cleanup.
        
        The shutdown hook module handles its own signal registration,
        but we also set the shutdown event so the main loop can exit gracefully.
        """
        # Import and register shutdown hook early (handles atexit + signals)
        try:
            backend_path = Path(__file__).parent / "backend"
            if str(backend_path) not in sys.path:
                sys.path.insert(0, str(backend_path))
            
            from backend.scripts.shutdown_hook import register_handlers
            register_handlers()
            self.logger.debug("✅ Shutdown hook handlers registered")
        except Exception as e:
            self.logger.warning(f"⚠️ Could not register shutdown hook: {e}")
        
        def handle_signal(signum, frame):
            """Handle SIGTERM by setting shutdown event."""
            self._shutdown_event.set()
            self.logger.info(f"🛑 Received signal {signum} - initiating shutdown")

        signal.signal(signal.SIGTERM, handle_signal)
        # SIGINT is handled by KeyboardInterrupt in the main run() method
        
    async def cleanup_resources(self):
        """
        Cleanup remote resources (GCP VMs) and local services on shutdown.

        Uses the enhanced shutdown_hook module which provides:
        - Async-safe cleanup with timeouts
        - Multiple fallback approaches (VM Manager, gcloud CLI)
        - Idempotent execution (safe to call multiple times)
        """
        # Cleanup JARVIS-Prime
        try:
            await self._stop_jarvis_prime()
            self.logger.info("✅ JARVIS-Prime stopped")
        except Exception as e:
            self.logger.warning(f"⚠️ JARVIS-Prime cleanup error: {e}")

        # Cleanup GCP resources
        try:
            from backend.scripts.shutdown_hook import cleanup_remote_resources

            self.logger.info("🧹 Cleaning up remote resources...")
            result = await cleanup_remote_resources(
                timeout=30.0,
                reason="Supervisor shutdown"
            )

            if result.get("success"):
                vms = result.get("vms_cleaned", 0)
                method = result.get("method", "unknown")
                if vms > 0:
                    self.logger.info(f"✅ Cleaned {vms} VM(s) via {method}")
                else:
                    self.logger.info("✅ No VMs to clean up")
            else:
                errors = result.get("errors", [])
                self.logger.warning(f"⚠️ Cleanup completed with issues: {errors}")

        except Exception as e:
            self.logger.error(f"❌ Failed to cleanup remote resources: {e}")
    
    async def _propagate_zero_touch_settings(self) -> None:
        """
        Propagate Zero-Touch settings to child processes via environment variables.
        
        These are read by JARVISSupervisor to configure autonomous update behavior.
        """
        # Zero-Touch master switch
        if self.config.zero_touch_enabled:
            os.environ["JARVIS_ZERO_TOUCH_ENABLED"] = "true"
            self.logger.info("🤖 Zero-Touch autonomous updates ENABLED")
            
            # Propagate individual settings
            os.environ["JARVIS_ZERO_TOUCH_REQUIRE_IDLE"] = str(self.config.zero_touch_require_idle).lower()
            os.environ["JARVIS_ZERO_TOUCH_CHECK_BUSY"] = str(self.config.zero_touch_check_busy).lower()
            os.environ["JARVIS_ZERO_TOUCH_AUTO_SECURITY"] = str(self.config.zero_touch_auto_security).lower()
            os.environ["JARVIS_ZERO_TOUCH_AUTO_CRITICAL"] = str(self.config.zero_touch_auto_critical).lower()
            os.environ["JARVIS_ZERO_TOUCH_AUTO_MINOR"] = str(self.config.zero_touch_auto_minor).lower()
            os.environ["JARVIS_ZERO_TOUCH_AUTO_MAJOR"] = str(self.config.zero_touch_auto_major).lower()
        
        # Dead Man's Switch settings
        if self.config.dms_enabled:
            os.environ["JARVIS_DMS_ENABLED"] = "true"
            os.environ["JARVIS_DMS_PROBATION_SECONDS"] = str(self.config.dms_probation_seconds)
            os.environ["JARVIS_DMS_MAX_FAILURES"] = str(self.config.dms_max_failures)
            self.logger.info(f"🎯 Dead Man's Switch: {self.config.dms_probation_seconds}s probation")
    
    async def _propagate_agi_os_settings(self) -> None:
        """
        Propagate AGI OS settings for intelligent integration.
        
        When enabled, AGI OS can:
        - Use VoiceApprovalManager for update consent
        - Push update events to ProactiveEventStream
        - Leverage IntelligentActionOrchestrator for optimal timing
        """
        if self.config.agi_os_enabled:
            os.environ["JARVIS_AGI_OS_ENABLED"] = "true"
            
            if self.config.agi_os_approval_for_updates:
                os.environ["JARVIS_AGI_OS_APPROVAL_UPDATES"] = "true"
                self.logger.info("🧠 AGI OS: Voice approval for updates ENABLED")
            
            # Check if AGI OS is available
            # Use consistent import path (same as internal backend imports)
            try:
                from agi_os import get_agi_os
                self.logger.info("🧠 AGI OS module available - will integrate with supervisor")
            except ImportError:
                self.logger.debug("AGI OS module not available - supervisor will operate independently")
    
    async def _initialize_rate_orchestrator(self) -> None:
        """
        v5.0: Initialize the Intelligent Rate Orchestrator for ML-powered rate limiting.
        
        This starts the ML forecasting system that:
        - Predicts rate limit breaches BEFORE they happen
        - Adaptively throttles GCP, CloudSQL, and Claude API calls
        - Uses Holt-Winters exponential smoothing for time-series forecasting
        - Implements PID control for smooth throttle adjustments
        - Schedules requests with priority-based queuing
        
        The orchestrator runs background tasks for:
        - Continuous throttle adjustment (every 1 second)
        - Forecast model updates (every 1 minute)
        """
        try:
            try:
                from core.intelligent_rate_orchestrator import (
                    get_rate_orchestrator,
                    ServiceType,
                )
            except ImportError:
                # Fallback to backend-prefixed import
                from core.intelligent_rate_orchestrator import (
                    get_rate_orchestrator,
                    ServiceType,
                )
            
            # Initialize and start the orchestrator
            orchestrator = await get_rate_orchestrator()
            
            # Log initial status
            stats = orchestrator.get_stats()
            service_count = len(stats.get("services", {}))
            
            self.logger.info(f"🎯 Intelligent Rate Orchestrator initialized")
            self.logger.info(f"   • ML Forecasting: Enabled (Holt-Winters time-series)")
            self.logger.info(f"   • Adaptive Throttling: Enabled (PID control)")
            self.logger.info(f"   • Services Configured: {service_count}")
            self.logger.info(f"   • Background Tasks: Adjustment loop, Forecast loop")
            
            # Propagate rate orchestrator availability to child processes
            os.environ["JARVIS_RATE_ORCHESTRATOR_ENABLED"] = "true"
            os.environ["JARVIS_ML_RATE_FORECASTING"] = "true"
            
            # Print status
            print(f"  {TerminalUI.GREEN}✓ Rate Limiting: ML Forecasting + Adaptive Throttling{TerminalUI.RESET}")
            
        except ImportError as e:
            self.logger.warning(f"⚠️ Intelligent Rate Orchestrator not available: {e}")
            self.logger.warning("   Rate limiting will use basic fallback mode")
            os.environ["JARVIS_RATE_ORCHESTRATOR_ENABLED"] = "false"
            print(f"  {TerminalUI.YELLOW}⚠️ Rate Limiting: Basic mode (ML forecasting unavailable){TerminalUI.RESET}")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Rate Orchestrator: {e}")
            os.environ["JARVIS_RATE_ORCHESTRATOR_ENABLED"] = "false"

    async def _initialize_agentic_security(self) -> None:
        """
        v6.0: Initialize the Two-Tier Agentic Security System.

        This starts the safety supervision layer for agentic (Computer Use) execution:
        - AgenticWatchdog: Monitors heartbeats, activity rates, triggers kill switch
        - TieredCommandRouter: Routes commands based on Tier 1 (safe) vs Tier 2 (agentic)
        - VBIA Integration: Strict voice authentication for Tier 2 commands

        Two-Tier Security Model:
        - Tier 1 "JARVIS": Safe APIs, read-only, optional auth, Gemini Flash
        - Tier 2 "JARVIS ACCESS": Full Computer Use, strict VBIA, Claude Sonnet

        Safety Features:
        - Heartbeat monitoring with configurable timeout
        - Activity rate limiting (prevents click storms)
        - Automatic downgrade to passive mode on anomalies
        - Voice announcement of safety events
        - Comprehensive audit logging
        """
        try:
            # Broadcast Two-Tier initialization start
            await self._broadcast_startup_progress(
                stage="two_tier_init",
                message="Initializing Two-Tier Agentic Security System...",
                progress=82,
                metadata={
                    "two_tier": {
                        "overall_status": "initializing",
                        "message": "Starting Two-Tier Security initialization...",
                    }
                }
            )

            # Initialize Watchdog
            if self._watchdog_enabled:
                try:
                    from core.agentic_watchdog import (
                        start_watchdog,
                        WatchdogConfig,
                        AgenticMode,
                    )

                    # Create TTS callback for watchdog announcements
                    async def watchdog_tts(text: str):
                        await self.narrator.speak(text, wait=False)

                    self._watchdog = await start_watchdog(
                        tts_callback=watchdog_tts if self.config.voice_enabled else None
                    )

                    self.logger.info("🛡️ Agentic Watchdog initialized")
                    self.logger.info("   • Kill Switch: Armed (heartbeat timeout, activity spike)")
                    self.logger.info("   • Safety Mode: Active monitoring")

                    os.environ["JARVIS_WATCHDOG_ENABLED"] = "true"
                    print(f"  {TerminalUI.GREEN}✓ Watchdog: Active safety monitoring{TerminalUI.RESET}")

                    # Broadcast Two-Tier Watchdog status to loading page
                    await self._broadcast_startup_progress(
                        stage="two_tier_watchdog",
                        message="Agentic Watchdog initialized - kill switch armed",
                        progress=83,
                        metadata={
                            "two_tier": {
                                "watchdog_ready": True,
                                "watchdog_status": "active",
                                "watchdog_mode": "monitoring",
                                "message": "Watchdog initialized - safety monitoring active",
                            }
                        }
                    )

                except ImportError as e:
                    self.logger.warning(f"⚠️ Agentic Watchdog not available: {e}")
                    os.environ["JARVIS_WATCHDOG_ENABLED"] = "false"
                    print(f"  {TerminalUI.YELLOW}⚠️ Watchdog: Not available{TerminalUI.RESET}")

            # Initialize Tiered VBIA Adapter
            vbia_adapter = None
            if self._tiered_routing_enabled:
                try:
                    from core.tiered_vbia_adapter import (
                        TieredVBIAAdapter,
                        TieredVBIAConfig,
                        get_tiered_vbia_adapter,
                    )

                    vbia_adapter = await get_tiered_vbia_adapter()
                    self.logger.info("🔐 Tiered VBIA Adapter initialized")
                    print(f"  {TerminalUI.GREEN}✓ VBIA Adapter: Tiered authentication ready{TerminalUI.RESET}")

                    # Broadcast Two-Tier VBIA status to loading page
                    await self._broadcast_startup_progress(
                        stage="two_tier_vbia",
                        message="Voice biometric authentication ready",
                        progress=85,
                        metadata={
                            "two_tier": {
                                "vbia_adapter_ready": True,
                                "vbia_tier1_threshold": 0.70,
                                "vbia_tier2_threshold": 0.85,
                                "vbia_liveness_enabled": True,
                                "message": "VBIA Adapter ready - tiered thresholds active",
                            }
                        }
                    )

                except ImportError as e:
                    self.logger.warning(f"⚠️ Tiered VBIA Adapter not available: {e}")
                    print(f"  {TerminalUI.YELLOW}⚠️ VBIA Adapter: Not available (will use fallback){TerminalUI.RESET}")

            # Initialize Tiered Router
            if self._tiered_routing_enabled:
                try:
                    from core.tiered_command_router import (
                        TieredCommandRouter,
                        TieredRouterConfig,
                        set_tiered_router,
                    )

                    # Create TTS callback for router announcements
                    async def router_tts(text: str):
                        await self.narrator.speak(text, wait=False)

                    config = TieredRouterConfig()

                    # Wire up VBIA adapter callbacks
                    vbia_callback = None
                    liveness_callback = None
                    if vbia_adapter:
                        vbia_callback = vbia_adapter.verify_speaker
                        liveness_callback = vbia_adapter.verify_liveness

                    self._tiered_router = TieredCommandRouter(
                        config=config,
                        vbia_callback=vbia_callback,
                        liveness_callback=liveness_callback,
                        tts_callback=router_tts if self.config.voice_enabled else None,
                    )

                    # Register in global registry for API access
                    set_tiered_router(self._tiered_router)

                    self.logger.info("🎯 Two-Tier Command Router initialized")
                    self.logger.info(f"   • Tier 1: {config.tier1_backend} (safe, low-auth)")
                    self.logger.info(f"   • Tier 2: {config.tier2_backend} (agentic, strict-auth)")
                    self.logger.info(f"   • VBIA Thresholds: T1={config.tier1_vbia_threshold:.0%}, T2={config.tier2_vbia_threshold:.0%}")
                    self.logger.info(f"   • VBIA Adapter: {'Connected' if vbia_adapter else 'Fallback mode'}")

                    os.environ["JARVIS_TIERED_ROUTING"] = "true"
                    os.environ["JARVIS_TIER2_BACKEND"] = config.tier2_backend
                    print(f"  {TerminalUI.GREEN}✓ Two-Tier Security: T1=Gemini, T2=Claude+VBIA{TerminalUI.RESET}")

                    # Store VBIA adapter for later use
                    self._vbia_adapter = vbia_adapter

                    # Broadcast Two-Tier Router status to loading page
                    await self._broadcast_startup_progress(
                        stage="two_tier_router",
                        message="Two-Tier command routing ready",
                        progress=87,
                        metadata={
                            "two_tier": {
                                "router_ready": True,
                                "tier1_operational": True,
                                "tier2_operational": True,
                                "message": "Router ready - Tier 1 (Gemini) + Tier 2 (Claude) active",
                            }
                        }
                    )

                    # Broadcast Two-Tier system fully ready
                    await self._broadcast_startup_progress(
                        stage="two_tier_ready",
                        message="Two-Tier Agentic Security System fully operational",
                        progress=89,
                        metadata={
                            "two_tier": {
                                "watchdog_ready": self._watchdog is not None,
                                "router_ready": True,
                                "vbia_adapter_ready": vbia_adapter is not None,
                                "tier1_operational": True,
                                "tier2_operational": True,
                                "watchdog_status": "active" if self._watchdog else "disabled",
                                "watchdog_mode": "monitoring" if self._watchdog else "idle",
                                "overall_status": "operational",
                                "message": "Two-Tier Security fully operational - all components ready",
                            }
                        }
                    )

                except ImportError as e:
                    self.logger.warning(f"⚠️ Tiered Router not available: {e}")
                    os.environ["JARVIS_TIERED_ROUTING"] = "false"
                    print(f"  {TerminalUI.YELLOW}⚠️ Tiered Routing: Not available{TerminalUI.RESET}")

            # Initialize Agentic Task Runner (unified execution engine)
            if self._agentic_runner_enabled:
                try:
                    from core.agentic_task_runner import (
                        AgenticTaskRunner,
                        AgenticRunnerConfig,
                        set_agentic_runner,
                    )

                    # Create TTS callback for runner narration
                    async def runner_tts(text: str):
                        await self.narrator.speak(text, wait=False)

                    runner_config = AgenticRunnerConfig()
                    self._agentic_runner = AgenticTaskRunner(
                        config=runner_config,
                        tts_callback=runner_tts if self.config.voice_enabled else None,
                        watchdog=self._watchdog,
                        logger=self.logger,
                    )

                    # Initialize the runner
                    await self._agentic_runner.initialize()

                    # Set global instance for access by other components
                    set_agentic_runner(self._agentic_runner)

                    # Wire router's execute_tier2 to use the runner
                    if self._tiered_router:
                        self._wire_router_to_runner()

                    self.logger.info("🤖 Agentic Task Runner initialized")
                    self.logger.info(f"   • Watchdog: {'Attached' if self._watchdog else 'Independent'}")
                    self.logger.info(f"   • Ready: {self._agentic_runner.is_ready}")
                    os.environ["JARVIS_AGENTIC_RUNNER"] = "true"
                    print(f"  {TerminalUI.GREEN}✓ Agentic Runner: Computer Use ready{TerminalUI.RESET}")

                except ImportError as e:
                    self.logger.warning(f"⚠️ Agentic Runner not available: {e}")
                    os.environ["JARVIS_AGENTIC_RUNNER"] = "false"
                    print(f"  {TerminalUI.YELLOW}⚠️ Agentic Runner: Not available{TerminalUI.RESET}")

            # Log overall status
            if self._watchdog_enabled or self._tiered_routing_enabled or self._agentic_runner_enabled:
                self.logger.info("✅ Two-Tier Agentic Security System ready")
            else:
                self.logger.info("ℹ️ Agentic security disabled (use JARVIS_WATCHDOG_ENABLED=true to enable)")

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Agentic Security: {e}")
            os.environ["JARVIS_WATCHDOG_ENABLED"] = "false"
            os.environ["JARVIS_TIERED_ROUTING"] = "false"
            os.environ["JARVIS_AGENTIC_RUNNER"] = "false"

    async def _initialize_jarvis_prime(self) -> None:
        """
        v8.0: Initialize JARVIS-Prime Tier-0 Brain with Memory-Aware Hybrid Routing.

        This dynamically decides the optimal mode based on available system memory:
        - RAM ≥ 8GB → Local subprocess mode (FREE, fastest)
        - RAM 4-8GB → Cloud Run mode (pay-per-use, ~$0.02/request)
        - RAM < 4GB → Gemini API fallback (cheapest, ~$0.0001/1K tokens)

        Features:
        - Memory-aware automatic routing (no hardcoding!)
        - Multi-tier fallback chain: Local → Cloud Run → Gemini API
        - Circuit breaker pattern for resilience
        - Real-time memory monitoring with dynamic switching
        - OpenAI-compatible API at /v1/chat/completions
        - Reactor-Core integration for auto-deployment of trained models

        Architecture:
        ┌─────────────────────────────────────────────────────────────────┐
        │  JARVIS-Prime Memory-Aware Hybrid Router (v8.0)                 │
        │  ┌───────────────────────────────────────────────────────────┐  │
        │  │              Memory Pressure Monitor                       │  │
        │  │  RAM ≥ 8GB → LOCAL    RAM 4-8GB → CLOUD    < 4GB → API   │  │
        │  └──────────────────────────┬────────────────────────────────┘  │
        │                             │                                   │
        │  ┌─────────────┐   ┌───────┴──────┐   ┌────────────────────┐   │
        │  │   Local     │   │   Cloud Run  │   │    Gemini API      │   │
        │  │ (Port 8002) │→→→│  (GCR URL)   │→→→│    (Fallback)      │   │
        │  └─────────────┘   └──────────────┘   └────────────────────┘   │
        │         ↓                 ↓                    ↓               │
        │  ┌──────────────────────────────────────────────────────────┐  │
        │  │              CircuitBreaker + Retry Logic                 │  │
        │  └──────────────────────────────────────────────────────────┘  │
        └─────────────────────────────────────────────────────────────────┘
        """
        if not self.config.jarvis_prime_enabled:
            self.logger.info("ℹ️ JARVIS-Prime disabled via configuration")
            os.environ["JARVIS_PRIME_ENABLED"] = "false"
            return

        try:
            # Import memory-aware client
            from core.jarvis_prime_client import (
                JarvisPrimeClient,
                JarvisPrimeConfig,
                get_jarvis_prime_client,
                get_system_memory_status,
                RoutingMode,
            )

            # Get current memory status for decision
            memory_status = await get_system_memory_status()
            available_gb = memory_status["available_gb"]
            recommended_mode = memory_status["recommended_mode"]

            self.logger.info(
                f"🧠 Memory Status: {available_gb:.1f}GB available, "
                f"recommended mode: {recommended_mode}"
            )

            # Broadcast JARVIS-Prime initialization start
            await self._broadcast_startup_progress(
                stage="jarvis_prime_init",
                message=f"Initializing JARVIS-Prime (Mode: {recommended_mode}, RAM: {available_gb:.1f}GB)...",
                progress=75,
                metadata={
                    "jarvis_prime": {
                        "status": "initializing",
                        "mode": recommended_mode,
                        "memory_available_gb": available_gb,
                        "memory_status": memory_status,
                    }
                }
            )

            # Build configuration from environment and current state
            client_config = JarvisPrimeConfig(
                # Local settings
                local_host=self.config.jarvis_prime_host,
                local_port=self.config.jarvis_prime_port,
                # Cloud Run settings
                cloud_run_url=self.config.jarvis_prime_cloud_run_url or os.getenv(
                    "JARVIS_PRIME_CLOUD_RUN_URL",
                    "https://jarvis-prime-dev-888774109345.us-central1.run.app"
                ),
                use_cloud_run=self.config.jarvis_prime_use_cloud_run or bool(
                    self.config.jarvis_prime_cloud_run_url
                ),
            )

            # Create the memory-aware client
            self._jarvis_prime_client = JarvisPrimeClient(client_config)

            # Log the decision
            mode, reason = self._jarvis_prime_client.decide_mode()
            self.logger.info(f"🎯 JARVIS-Prime routing decision: {mode.value} ({reason})")

            # Initialize based on recommended mode
            if mode == RoutingMode.LOCAL:
                # Start local subprocess if not already running
                await self._init_jarvis_prime_local_if_needed()
            elif mode == RoutingMode.CLOUD_RUN:
                # Verify Cloud Run is accessible
                await self._init_jarvis_prime_cloud_run()
            elif mode == RoutingMode.GEMINI_API:
                self.logger.info("📡 Using Gemini API fallback due to low memory")
            else:
                self.logger.warning("⚠️ No JARVIS-Prime backends available")

            # Run health check on the selected mode
            if mode != RoutingMode.DISABLED:
                health = await self._jarvis_prime_client.check_health(mode)
                if health.available:
                    self.logger.info(f"✅ {mode.value} backend healthy (latency: {health.latency_ms:.0f}ms)")
                else:
                    self.logger.warning(f"⚠️ {mode.value} backend not healthy: {health.error}")

            # Initialize Reactor-Core watcher for auto-deployment
            if self.config.reactor_core_enabled and self.config.reactor_core_watch_dir:
                await self._init_reactor_core_watcher()

            # v8.0: Initialize Data Flywheel for self-improving learning
            if self.config.data_flywheel_enabled:
                await self._init_data_flywheel()

            # Start dynamic memory monitoring for automatic mode switching
            await self._jarvis_prime_client.start_monitoring()

            # Register mode change callback
            async def on_mode_change(old_mode, new_mode, reason):
                self.logger.info(
                    f"🔄 JARVIS-Prime mode changed: {old_mode.value} → {new_mode.value} ({reason})"
                )
                os.environ["JARVIS_PRIME_ROUTING_MODE"] = new_mode.value

                # Broadcast the change
                await self._broadcast_startup_progress(
                    stage="jarvis_prime_mode_change",
                    message=f"JARVIS-Prime switched to {new_mode.value}",
                    progress=100,
                    metadata={
                        "jarvis_prime": {
                            "old_mode": old_mode.value,
                            "new_mode": new_mode.value,
                            "reason": reason,
                        }
                    }
                )

            self._jarvis_prime_client.register_mode_change_callback(on_mode_change)

            # Propagate settings to environment
            os.environ["JARVIS_PRIME_ENABLED"] = "true"
            os.environ["JARVIS_PRIME_HOST"] = self.config.jarvis_prime_host
            os.environ["JARVIS_PRIME_PORT"] = str(self.config.jarvis_prime_port)
            os.environ["JARVIS_PRIME_ROUTING_MODE"] = mode.value

            # Broadcast completion
            client_stats = self._jarvis_prime_client.get_stats()
            await self._broadcast_startup_progress(
                stage="jarvis_prime_ready",
                message=f"JARVIS-Prime Tier-0 Brain online ({mode.value})",
                progress=78,
                metadata={
                    "jarvis_prime": {
                        "status": "ready",
                        "mode": mode.value,
                        "reason": reason,
                        "url": f"http://{self.config.jarvis_prime_host}:{self.config.jarvis_prime_port}",
                        "cloud_run_url": client_config.cloud_run_url if mode == RoutingMode.CLOUD_RUN else None,
                        "memory_available_gb": available_gb,
                        "monitoring_active": True,
                        "circuit_breakers": client_stats.get("circuit_breakers", {}),
                    }
                }
            )

            self.logger.info(f"✅ JARVIS-Prime Tier-0 Brain ready (mode: {mode.value})")
            self.logger.info(f"🔄 Dynamic memory monitoring active (interval: 30s)")
            print(f"  {TerminalUI.GREEN}✓ JARVIS-Prime: {mode.value} mode ({available_gb:.1f}GB RAM){TerminalUI.RESET}")

        except ImportError as e:
            self.logger.warning(f"⚠️ JARVIS-Prime client not available: {e}")
            os.environ["JARVIS_PRIME_ENABLED"] = "false"
            print(f"  {TerminalUI.YELLOW}⚠️ JARVIS-Prime: Client not available{TerminalUI.RESET}")

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize JARVIS-Prime: {e}")
            os.environ["JARVIS_PRIME_ENABLED"] = "false"
            print(f"  {TerminalUI.YELLOW}⚠️ JARVIS-Prime: Not available ({e}){TerminalUI.RESET}")

    async def _init_jarvis_prime_local_if_needed(self) -> None:
        """Start JARVIS-Prime local subprocess if not already running."""
        # Check if already running
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(
                    f"http://{self.config.jarvis_prime_host}:{self.config.jarvis_prime_port}/health"
                )
                if resp.status_code == 200:
                    self.logger.info("✅ JARVIS-Prime local already running")
                    return
        except Exception:
            pass  # Not running, need to start

        # Start local subprocess
        await self._init_jarvis_prime_local()

    async def _init_jarvis_prime_local(self) -> None:
        """Start JARVIS-Prime as a local subprocess."""
        repo_path = self.config.jarvis_prime_repo_path

        if not repo_path.exists():
            self.logger.warning(f"⚠️ JARVIS-Prime repo not found: {repo_path}")
            return

        # Check if model exists
        model_path = repo_path / self.config.jarvis_prime_models_dir / "current.gguf"
        if not model_path.exists():
            self.logger.warning(f"⚠️ JARVIS-Prime model not found: {model_path}")
            self.logger.info("   Download with: cd jarvis-prime && python -m jarvis_prime.docker.model_downloader tinyllama-chat")
            return

        # Check for venv or use system Python
        venv_python = repo_path / "venv" / "bin" / "python"
        python_cmd = str(venv_python) if venv_python.exists() else sys.executable

        # Build command
        cmd = [
            python_cmd,
            "run_server.py",
            "--host", self.config.jarvis_prime_host,
            "--port", str(self.config.jarvis_prime_port),
            "--model", str(model_path),
        ]

        self.logger.info(f"🚀 Starting JARVIS-Prime local: {' '.join(cmd)}")

        # Start subprocess
        self._jarvis_prime_process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(repo_path)},
        )

        # Wait for health check
        await self._wait_for_jarvis_prime_health()

        # Start log reader
        asyncio.create_task(self._read_jarvis_prime_logs())

    async def _init_jarvis_prime_docker(self) -> None:
        """Start JARVIS-Prime as a Docker container."""
        container_name = "jarvis-prime"

        # Stop existing container if any
        try:
            stop_proc = await asyncio.create_subprocess_exec(
                "docker", "stop", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(stop_proc.wait(), timeout=10.0)

            rm_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await rm_proc.wait()
        except Exception:
            pass  # Container might not exist

        # Build docker run command
        models_dir = self.config.jarvis_prime_repo_path / self.config.jarvis_prime_models_dir

        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"{self.config.jarvis_prime_port}:8000",
            "-v", f"{models_dir}:/app/models:ro",
            "-e", f"LOG_LEVEL=INFO",
            self.config.jarvis_prime_docker_image,
        ]

        self.logger.info(f"🐳 Starting JARVIS-Prime Docker: {' '.join(cmd)}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Docker start failed: {stderr.decode()}")

        container_id = stdout.decode().strip()[:12]
        self.logger.info(f"✅ Docker container started: {container_id}")

        # Wait for health
        await self._wait_for_jarvis_prime_health()

    async def _init_jarvis_prime_cloud_run(self) -> None:
        """Connect to JARVIS-Prime on Cloud Run."""
        url = self.config.jarvis_prime_cloud_run_url

        self.logger.info(f"☁️ Connecting to JARVIS-Prime Cloud Run: {url}")

        # Just verify connectivity - no process to start
        import aiohttp
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        self.logger.info("✅ Cloud Run JARVIS-Prime is healthy")
                    else:
                        self.logger.warning(f"⚠️ Cloud Run returned status {resp.status}")
            except Exception as e:
                self.logger.warning(f"⚠️ Cloud Run health check failed: {e}")
                self.logger.info("   This is expected if the service hasn't been deployed yet")
                self.logger.info("   Deploy with: terraform apply -var='enable_jarvis_prime=true'")

    async def _wait_for_jarvis_prime_health(self) -> bool:
        """Wait for JARVIS-Prime to become healthy."""
        import aiohttp

        url = f"http://{self.config.jarvis_prime_host}:{self.config.jarvis_prime_port}/health"
        start_time = time.perf_counter()
        timeout = self.config.jarvis_prime_startup_timeout

        self.logger.info(f"⏳ Waiting for JARVIS-Prime at {url}...")

        async with aiohttp.ClientSession() as session:
            while (time.perf_counter() - start_time) < timeout:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            self.logger.info(f"✅ JARVIS-Prime healthy: {data.get('status', 'ok')}")
                            return True
                except Exception:
                    pass

                await asyncio.sleep(1.0)

        self.logger.warning(f"⚠️ JARVIS-Prime health check timed out after {timeout}s")
        return False

    async def _read_jarvis_prime_logs(self) -> None:
        """Read and log JARVIS-Prime subprocess output."""
        if not self._jarvis_prime_process:
            return

        try:
            async for line in self._jarvis_prime_process.stdout:
                text = line.decode().strip()
                if text:
                    self.logger.debug(f"[JarvisPrime] {text}")
        except Exception as e:
            self.logger.debug(f"JARVIS-Prime log reader ended: {e}")

    async def _init_reactor_core_watcher(self) -> None:
        """Initialize Reactor-Core watcher for auto-deployment of trained models."""
        watch_dir = self.config.reactor_core_watch_dir

        if not watch_dir:
            # Use default reactor-core output path
            watch_dir = str(Path.home() / "Documents" / "repos" / "reactor-core" / "output")

        self.logger.info(f"🔬 Initializing Reactor-Core watcher: {watch_dir}")

        # Import and start watcher (non-blocking)
        try:
            from autonomy.reactor_core_watcher import (
                ReactorCoreWatcher,
                ReactorCoreConfig,
                DeploymentResult,
            )

            # Configure watcher
            config = ReactorCoreConfig(
                watch_dir=Path(watch_dir),
                local_models_dir=self.config.jarvis_prime_repo_path / self.config.jarvis_prime_models_dir,
                upload_to_gcs=True,
                deploy_local=True,
                auto_activate=self.config.reactor_core_auto_deploy,
            )

            # Create watcher
            self._reactor_core_watcher = ReactorCoreWatcher(config)

            # Register deployment callback
            async def on_model_deployed(result: DeploymentResult):
                """Callback when a new model is deployed from reactor-core."""
                if result.success:
                    self.logger.info(
                        f"🔥 Reactor-Core deployed: {result.model_name} "
                        f"({result.model_size_mb:.1f}MB, checksum: {result.checksum})"
                    )

                    # Announce deployment
                    if hasattr(self, 'narrator') and self.narrator:
                        await self.narrator.speak(
                            f"New model deployed from Reactor Core: {result.model_name}",
                            wait=False
                        )

                    # Broadcast progress update
                    await self._broadcast_startup_progress(
                        stage="reactor_core_deploy",
                        message=f"New model deployed: {result.model_name}",
                        progress=100,
                        metadata={
                            "reactor_core": {
                                "model_name": result.model_name,
                                "model_size_mb": result.model_size_mb,
                                "checksum": result.checksum,
                                "local_deployed": result.local_deployed,
                                "gcs_uploaded": result.gcs_uploaded,
                                "gcs_path": result.gcs_path,
                                "hot_swap_notified": result.hot_swap_notified,
                            }
                        }
                    )

                    # Force a mode check in case we should switch to local
                    if self._jarvis_prime_client:
                        await self._jarvis_prime_client.force_mode_check()

                else:
                    self.logger.warning(
                        f"⚠️ Reactor-Core deployment failed: {result.model_name} - {result.error}"
                    )

            self._reactor_core_watcher.register_callback(on_model_deployed)

            # Start watching in background
            await self._reactor_core_watcher.start()
            self.logger.info("✅ Reactor-Core watcher started")
            print(f"  {TerminalUI.GREEN}✓ Reactor-Core: Watching for new models{TerminalUI.RESET}")

        except ImportError as e:
            self.logger.warning(f"⚠️ Reactor-Core watcher not available: {e}")
        except Exception as e:
            self.logger.warning(f"⚠️ Reactor-Core watcher failed to start: {e}")

    async def _init_data_flywheel(self) -> None:
        """
        v8.0: Initialize the Unified Data Flywheel for self-improving learning.

        The Data Flywheel connects:
        - JARVIS-AI-Agent (experience recording, observability)
        - reactor-core (Scout web scraping, training, GGUF export)
        - JARVIS-Prime (model deployment, inference)

        Features:
        - Automatic experience collection from JARVIS interactions
        - Intelligent learning goals auto-discovery
        - Scheduled training runs (default: 3 AM daily)
        - Auto-deployment via Reactor-Core Watcher
        """
        if not self.config.data_flywheel_enabled:
            self.logger.info("ℹ️ Data Flywheel disabled via configuration")
            return

        self.logger.info("🔄 Initializing Data Flywheel (self-improving learning loop)...")

        try:
            from autonomy.unified_data_flywheel import (
                UnifiedDataFlywheel,
                FlywheelConfig,
                FlywheelProgress,
                get_data_flywheel,
            )

            # Configure flywheel
            flywheel_config = FlywheelConfig(
                jarvis_repo=Path(__file__).parent,
                jarvis_prime_repo=self.config.jarvis_prime_repo_path,
                reactor_core_repo=Path.home() / "Documents" / "repos" / "reactor-core",
                auto_train_enabled=self.config.data_flywheel_auto_train,
                training_cooldown_hours=self.config.data_flywheel_cooldown_hours,
                min_experiences_for_training=self.config.data_flywheel_min_experiences,
            )

            # Get or create flywheel instance
            self._data_flywheel = UnifiedDataFlywheel(flywheel_config)

            # Register progress callback for status updates
            def on_flywheel_progress(progress: FlywheelProgress):
                self.logger.debug(
                    f"Flywheel: {progress.stage.value} - "
                    f"experiences={progress.experiences_collected}, "
                    f"web_pages={progress.web_pages_scraped}"
                )

            self._data_flywheel.register_progress_callback(on_flywheel_progress)

            # Initialize learning goals manager if enabled
            if self.config.learning_goals_enabled:
                await self._init_learning_goals_manager()

            # Schedule automatic training if enabled
            if self.config.data_flywheel_auto_train:
                self._training_scheduler_task = asyncio.create_task(
                    self._run_training_scheduler()
                )

            # v9.1: Initialize flywheel components eagerly for faster experience collection
            try:
                await self._data_flywheel._init_components()
                self.logger.info("✅ Data Flywheel components initialized")

                # Initialize the SQLite training database connection
                if hasattr(self._data_flywheel, '_init_training_db'):
                    await self._data_flywheel._init_training_db()
                    self.logger.info("✅ Training database connection established")
            except Exception as init_err:
                self.logger.warning(f"⚠️ Flywheel component init error (non-fatal): {init_err}")

            # v9.1: Start background experience collection loop
            if self.config.data_flywheel_auto_collect:
                self._experience_collection_task = asyncio.create_task(
                    self._run_experience_collection_loop()
                )
                self.logger.info("✅ Background experience collection started")

            self.logger.info("✅ Data Flywheel initialized")
            print(f"  {TerminalUI.GREEN}✓ Data Flywheel: Self-improving learning active{TerminalUI.RESET}")

            # Broadcast flywheel ready
            await self._broadcast_startup_progress(
                stage="data_flywheel_ready",
                message="Data Flywheel ready for self-improving learning",
                progress=82,
                metadata={
                    "data_flywheel": {
                        "status": "ready",
                        "auto_train": self.config.data_flywheel_auto_train,
                        "training_schedule": self.config.data_flywheel_training_schedule,
                        "learning_goals_enabled": self.config.learning_goals_enabled,
                    }
                }
            )

        except ImportError as e:
            self.logger.warning(f"⚠️ Data Flywheel not available: {e}")
        except Exception as e:
            self.logger.warning(f"⚠️ Data Flywheel failed to start: {e}")

    async def _init_learning_goals_manager(self) -> None:
        """
        Initialize the intelligent learning goals manager.

        This system automatically discovers topics JARVIS should learn about by:
        - Analyzing user interactions for mentioned technologies/concepts
        - Detecting questions JARVIS couldn't answer well
        - Monitoring for trending topics in the user's domain
        """
        self.logger.info("🎯 Initializing Learning Goals Manager...")

        try:
            # Create learning goals manager
            from dataclasses import dataclass, field
            from typing import List, Dict, Any, Optional
            from datetime import datetime
            import json

            @dataclass
            class LearningGoal:
                """A topic JARVIS should learn about."""
                topic: str
                priority: int = 5  # 1-10 scale
                source: str = "auto"  # auto, user, trending
                urls: List[str] = field(default_factory=list)
                discovered_at: datetime = field(default_factory=datetime.now)
                completed: bool = False

                def to_dict(self) -> Dict[str, Any]:
                    return {
                        "topic": self.topic,
                        "priority": self.priority,
                        "source": self.source,
                        "urls": self.urls,
                        "discovered_at": self.discovered_at.isoformat(),
                        "completed": self.completed,
                    }

            class LearningGoalsManager:
                """Manages and auto-discovers learning goals for JARVIS."""

                def __init__(self, max_topics: int = 20):
                    self.max_topics = max_topics
                    self.goals: List[LearningGoal] = []
                    self.goals_file = Path(__file__).parent / "data" / "learning_goals.json"
                    self._load_goals()

                def _load_goals(self) -> None:
                    """Load goals from file."""
                    if self.goals_file.exists():
                        try:
                            data = json.loads(self.goals_file.read_text())
                            for g in data.get("topics", []):
                                self.goals.append(LearningGoal(
                                    topic=g["topic"],
                                    priority=g.get("priority", 5),
                                    source=g.get("source", "file"),
                                    urls=g.get("urls", []),
                                ))
                        except Exception:
                            pass

                def _save_goals(self) -> None:
                    """Save goals to file."""
                    self.goals_file.parent.mkdir(parents=True, exist_ok=True)
                    data = {"topics": [g.to_dict() for g in self.goals if not g.completed]}
                    self.goals_file.write_text(json.dumps(data, indent=2, default=str))

                def add_goal(self, topic: str, priority: int = 5, source: str = "auto", urls: List[str] = None) -> bool:
                    """Add a learning goal if not already present."""
                    # Check if already exists
                    for g in self.goals:
                        if g.topic.lower() == topic.lower():
                            return False

                    # Enforce max topics
                    if len([g for g in self.goals if not g.completed]) >= self.max_topics:
                        # Remove lowest priority completed goal
                        self.goals = sorted(self.goals, key=lambda x: (x.completed, -x.priority))
                        if self.goals and self.goals[-1].completed:
                            self.goals.pop()

                    self.goals.append(LearningGoal(
                        topic=topic,
                        priority=priority,
                        source=source,
                        urls=urls or [],
                    ))
                    self._save_goals()
                    return True

                def get_pending_goals(self) -> List[LearningGoal]:
                    """Get uncompleted goals sorted by priority."""
                    return sorted(
                        [g for g in self.goals if not g.completed],
                        key=lambda x: -x.priority
                    )

                def mark_completed(self, topic: str) -> None:
                    """Mark a goal as completed."""
                    for g in self.goals:
                        if g.topic.lower() == topic.lower():
                            g.completed = True
                    self._save_goals()

                async def auto_discover_from_logs(self, log_dir: Path) -> List[str]:
                    """Auto-discover learning topics from JARVIS logs."""
                    discovered = []

                    # Tech keywords to look for
                    tech_patterns = [
                        r"(?:learn|study|research|understand)\s+(\w+(?:\s+\w+)?)",
                        r"what\s+is\s+(\w+(?:\s+\w+)?)\??",
                        r"how\s+(?:does|do)\s+(\w+(?:\s+\w+)?)\s+work",
                        r"(\w+(?:\.\w+)?)\s+(?:documentation|docs|tutorial)",
                    ]

                    # Scan recent logs for patterns
                    if log_dir.exists():
                        import re
                        for log_file in sorted(log_dir.glob("*.log"), reverse=True)[:5]:
                            try:
                                content = log_file.read_text()
                                for pattern in tech_patterns:
                                    matches = re.findall(pattern, content, re.IGNORECASE)
                                    for match in matches[:3]:  # Limit per pattern
                                        topic = match.strip()
                                        if len(topic) > 3 and self.add_goal(topic, priority=3, source="auto"):
                                            discovered.append(topic)
                            except Exception:
                                continue

                    return discovered

            # Create and store manager
            self._learning_goals_manager = LearningGoalsManager(
                max_topics=self.config.learning_goals_max_topics
            )

            # Auto-discover from logs if enabled
            if self.config.learning_goals_auto_discover:
                log_dir = Path(__file__).parent / "logs"
                discovered = await self._learning_goals_manager.auto_discover_from_logs(log_dir)
                if discovered:
                    self.logger.info(f"🎯 Auto-discovered {len(discovered)} learning topics: {discovered[:5]}")

            pending = self._learning_goals_manager.get_pending_goals()
            self.logger.info(f"✅ Learning Goals Manager ready ({len(pending)} pending topics)")

        except Exception as e:
            self.logger.warning(f"⚠️ Learning Goals Manager failed: {e}")

    async def _initialize_intelligence_systems(self) -> None:
        """
        v9.0: Initialize Full Agentic OS Intelligence Stack.

        This method initializes all advanced intelligence systems that make JARVIS
        a truly autonomous, self-improving AI agent:

        1. UAE (Unified Awareness Engine) - Screen awareness and computer vision
           - Chain-of-thought reasoning for complex decisions
           - Integration with SAI for spatial understanding
           - Continuous visual monitoring and analysis

        2. SAI (Situational Awareness Intelligence) - Window/app tracking
           - Yabai bridge for macOS window management
           - Cross-space learning and pattern recognition
           - Real-time workspace state tracking

        3. Neural Mesh - Distributed intelligence coordination
           - Inter-system communication and synchronization
           - Shared context propagation across all subsystems
           - Adaptive load balancing for intelligence tasks

        4. MAS (Multi-Agent System) - Coordinated agent execution
           - Parallel task decomposition and execution
           - Agent collaboration and conflict resolution
           - Dynamic agent spawning based on task complexity

        5. CAI (Collective AI Intelligence) - Emergent intelligence aggregation
           - Synthesis of insights from all agents
           - Pattern detection across system boundaries
           - Proactive recommendation generation

        6. Continuous Background Web Scraping - Self-improving knowledge
           - Configurable interval-based scraping (default: every 4 hours)
           - Topic-driven intelligent content discovery
           - Automatic integration with training pipeline

        Architecture:
        ┌─────────────────────────────────────────────────────────────────────┐
        │              Agentic OS Intelligence Stack (v9.0)                    │
        ├─────────────────────────────────────────────────────────────────────┤
        │  ┌──────────────────┐    ┌──────────────────┐    ┌───────────────┐ │
        │  │       UAE        │◄──►│       SAI        │◄──►│  Neural Mesh  │ │
        │  │  (Vision+Chain)  │    │   (Yabai+Apps)   │    │   (Coord.)    │ │
        │  └────────┬─────────┘    └────────┬─────────┘    └───────┬───────┘ │
        │           │                       │                       │         │
        │           └───────────────────────┼───────────────────────┘         │
        │                                   ▼                                 │
        │                    ┌──────────────────────────┐                     │
        │                    │    MAS (Multi-Agent)     │                     │
        │                    │  ┌──────┐ ┌──────┐      │                     │
        │                    │  │Agent1│ │Agent2│ ...  │                     │
        │                    │  └──────┘ └──────┘      │                     │
        │                    └────────────┬─────────────┘                     │
        │                                 ▼                                   │
        │                    ┌──────────────────────────┐                     │
        │                    │    CAI (Collective AI)   │                     │
        │                    │    Intelligence Layer    │                     │
        │                    └────────────┬─────────────┘                     │
        │                                 ▼                                   │
        │  ┌──────────────────────────────────────────────────────────────┐  │
        │  │          Continuous Background Web Scraping                   │  │
        │  │  Topics → Safe Scout → Training Pipeline → Model Deployment  │  │
        │  └──────────────────────────────────────────────────────────────┘  │
        └─────────────────────────────────────────────────────────────────────┘
        """
        self.logger.info("═" * 60)
        self.logger.info("🧠 v9.0: Initializing Full Agentic OS Intelligence Stack...")
        self.logger.info("═" * 60)

        # Track initialization status for all systems
        initialized_systems: Dict[str, bool] = {
            "uae": False,
            "sai": False,
            "neural_mesh": False,
            "mas": False,
            "cai": False,
            "continuous_scraping": False,
            "reactor_core": False,
        }

        # ═══════════════════════════════════════════════════════════════════
        # Step 1: Initialize UAE (Unified Awareness Engine)
        # ═══════════════════════════════════════════════════════════════════
        if self.config.uae_enabled:
            try:
                self.logger.info("🔮 Step 1/7: Initializing UAE (Unified Awareness Engine)...")
                self.logger.info("   • Chain-of-thought reasoning: " + ("Enabled" if self.config.uae_chain_of_thought else "Disabled"))
                self.logger.info("   • Proactive intelligence: Enabled")
                self.logger.info("   • Learning database: Enabled")

                from intelligence.uae_integration import (
                    initialize_uae,
                    get_uae,
                    get_enhanced_uae,
                    shutdown_uae,
                )

                # Initialize UAE with all features enabled
                self._uae_engine = await initialize_uae(
                    vision_analyzer=None,  # Will be injected later by main.py
                    sai_monitoring_interval=5.0,  # 5-second monitoring for real-time awareness
                    enable_auto_start=True,
                    enable_learning_db=True,
                    enable_yabai=self.config.sai_yabai_bridge,  # Connect to Yabai if enabled
                    enable_proactive_intelligence=True,  # Phase 4 proactive communication
                    enable_chain_of_thought=self.config.uae_chain_of_thought,  # LangGraph reasoning
                    enable_unified_orchestrator=True,  # Full UnifiedIntelligenceOrchestrator
                )

                # Store enhanced UAE for chain-of-thought reasoning
                if self.config.uae_chain_of_thought:
                    self._enhanced_uae = get_enhanced_uae()
                    self.logger.info("✅ UAE initialized with LangGraph chain-of-thought reasoning")
                else:
                    self._enhanced_uae = None
                    self.logger.info("✅ UAE initialized (standard mode)")

                initialized_systems["uae"] = True
                os.environ["UAE_ENABLED"] = "true"
                os.environ["UAE_CHAIN_OF_THOUGHT"] = str(self.config.uae_chain_of_thought).lower()

                # Connect UAE to training data flywheel for experience logging
                try:
                    from autonomy.unified_data_flywheel import get_data_flywheel

                    flywheel = get_data_flywheel()
                    if flywheel:
                        # Store flywheel reference for UAE experience logging
                        self._uae_data_flywheel = flywheel

                        # Create callback for logging UAE decisions to training DB
                        async def log_uae_decision(decision_data: Dict[str, Any]) -> None:
                            """Log UAE decisions to training database."""
                            try:
                                experience_context = {
                                    "source": "uae_decision",
                                    "element_id": decision_data.get("element_id"),
                                    "confidence": decision_data.get("confidence", 0.0),
                                    "decision_source": decision_data.get("source", "unknown"),
                                    "chain_of_thought": decision_data.get("reasoning"),
                                    "timestamp": time.time(),
                                }

                                quality_score = min(0.9, decision_data.get("confidence", 0.5) + 0.2)

                                flywheel.add_experience(
                                    source="uae",
                                    input_text=decision_data.get("query", "screen_analysis"),
                                    output_text=str(decision_data.get("result", {})),
                                    context=experience_context,
                                    quality_score=quality_score,
                                )
                            except Exception as flywheel_error:
                                self.logger.debug(f"UAE flywheel logging error: {flywheel_error}")

                        self._uae_decision_callback = log_uae_decision
                        self.logger.info("✅ UAE connected to training data flywheel")
                except Exception as flywheel_error:
                    self.logger.debug(f"UAE flywheel connection skipped: {flywheel_error}")

                print(f"  {TerminalUI.GREEN}✓ UAE: Unified Awareness Engine active{TerminalUI.RESET}")

            except ImportError as e:
                self.logger.warning(f"⚠️ UAE not available: {e}")
                os.environ["UAE_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ UAE: Not available{TerminalUI.RESET}")
            except Exception as e:
                self.logger.error(f"❌ UAE initialization failed: {e}")
                os.environ["UAE_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ UAE: Failed ({e}){TerminalUI.RESET}")

        # ═══════════════════════════════════════════════════════════════════
        # Step 2: Initialize SAI (Situational Awareness Intelligence)
        # ═══════════════════════════════════════════════════════════════════
        if self.config.sai_enabled:
            try:
                self.logger.info("👁️ Step 2/7: Initializing SAI (Situational Awareness Intelligence)...")
                self.logger.info("   • Yabai bridge: " + ("Enabled" if self.config.sai_yabai_bridge else "Disabled"))
                self.logger.info("   • 24/7 workspace monitoring: Enabled")

                from intelligence.yabai_sai_integration import (
                    initialize_bridge,
                    get_bridge,
                    YabaiSAIBridge,
                )
                from intelligence.yabai_spatial_intelligence import (
                    get_yabai_intelligence,
                    YabaiSpatialIntelligence,
                )

                # Initialize Yabai Spatial Intelligence
                if self.config.sai_yabai_bridge:
                    self._yabai_intelligence = await get_yabai_intelligence(
                        learning_db=None,  # Will connect to learning DB from UAE
                        monitoring_interval=5.0,
                        enable_24_7_mode=True,
                    )

                    if self._yabai_intelligence and self._yabai_intelligence.yabai_available:
                        self.logger.info("✅ SAI: Yabai bridge connected (24/7 workspace monitoring)")
                        initialized_systems["sai"] = True
                        os.environ["SAI_YABAI_BRIDGE"] = "true"
                    else:
                        self.logger.warning("⚠️ SAI: Yabai not available on this system")
                        os.environ["SAI_YABAI_BRIDGE"] = "false"
                else:
                    self.logger.info("ℹ️ SAI: Running without Yabai bridge")

                initialized_systems["sai"] = True
                os.environ["SAI_ENABLED"] = "true"

                # Connect SAI to training data flywheel for workspace experience logging
                try:
                    from autonomy.unified_data_flywheel import get_data_flywheel

                    flywheel = get_data_flywheel()
                    if flywheel and self._yabai_intelligence:
                        # Store flywheel reference for SAI experience logging
                        self._sai_data_flywheel = flywheel

                        # Create callback for logging SAI workspace events to training DB
                        async def log_sai_workspace_event(event_data: Dict[str, Any]) -> None:
                            """Log SAI workspace events to training database."""
                            try:
                                experience_context = {
                                    "source": "sai_workspace",
                                    "event_type": event_data.get("event_type"),
                                    "space_id": event_data.get("space_id"),
                                    "app_name": event_data.get("app_name"),
                                    "window_count": event_data.get("window_count", 0),
                                    "timestamp": time.time(),
                                }

                                flywheel.add_experience(
                                    source="sai",
                                    input_text=f"workspace_{event_data.get('event_type', 'unknown')}",
                                    output_text=str(event_data.get("result", {})),
                                    context=experience_context,
                                    quality_score=0.7,
                                )
                            except Exception as flywheel_error:
                                self.logger.debug(f"SAI flywheel logging error: {flywheel_error}")

                        self._sai_event_callback = log_sai_workspace_event
                        self.logger.info("✅ SAI connected to training data flywheel")
                except Exception as flywheel_error:
                    self.logger.debug(f"SAI flywheel connection skipped: {flywheel_error}")

                print(f"  {TerminalUI.GREEN}✓ SAI: Situational Awareness active{TerminalUI.RESET}")

            except ImportError as e:
                self.logger.warning(f"⚠️ SAI not available: {e}")
                os.environ["SAI_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ SAI: Not available{TerminalUI.RESET}")
            except Exception as e:
                self.logger.error(f"❌ SAI initialization failed: {e}")
                os.environ["SAI_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ SAI: Failed ({e}){TerminalUI.RESET}")

        # ═══════════════════════════════════════════════════════════════════
        # Step 3: Initialize Neural Mesh (Distributed Intelligence Coordination)
        # ═══════════════════════════════════════════════════════════════════
        if self.config.neural_mesh_enabled:
            try:
                self.logger.info("🕸️ Step 3/7: Initializing Neural Mesh (Distributed Intelligence)...")
                self.logger.info("   • Sync interval: " + str(self.config.neural_mesh_sync_interval) + "s")

                # Neural Mesh is a new system that coordinates all intelligence subsystems
                # It provides shared context, message passing, and load balancing

                from dataclasses import dataclass, field
                from typing import Dict, Any, List, Callable, Optional
                from collections import defaultdict
                from datetime import datetime
                import weakref

                @dataclass
                class NeuralMeshNode:
                    """A node in the Neural Mesh network."""
                    node_id: str
                    node_type: str  # "uae", "sai", "mas", "cai", "scraper"
                    capabilities: List[str] = field(default_factory=list)
                    status: str = "active"
                    last_heartbeat: float = field(default_factory=time.time)
                    metadata: Dict[str, Any] = field(default_factory=dict)

                class NeuralMesh:
                    """
                    Distributed intelligence coordination network.

                    Provides:
                    - Inter-system message passing
                    - Shared context propagation
                    - Load balancing for intelligence tasks
                    - System health monitoring
                    - Adaptive routing of intelligence queries
                    """

                    def __init__(self, sync_interval: float = 5.0):
                        self._nodes: Dict[str, NeuralMeshNode] = {}
                        self._context: Dict[str, Any] = {}  # Shared context
                        self._message_queues: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
                        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
                        self._sync_interval = sync_interval
                        self._sync_task: Optional[asyncio.Task] = None
                        self._running = False
                        self._logger = logging.getLogger("NeuralMesh")

                    def register_node(self, node: NeuralMeshNode) -> None:
                        """Register a node in the mesh."""
                        self._nodes[node.node_id] = node
                        self._message_queues[node.node_id] = asyncio.Queue()
                        self._logger.debug(f"Node registered: {node.node_id} ({node.node_type})")

                    def unregister_node(self, node_id: str) -> None:
                        """Remove a node from the mesh."""
                        if node_id in self._nodes:
                            del self._nodes[node_id]
                        if node_id in self._message_queues:
                            del self._message_queues[node_id]

                    async def broadcast(self, event_type: str, data: Dict[str, Any], source: str = None) -> None:
                        """Broadcast a message to all subscribed nodes."""
                        message = {
                            "event_type": event_type,
                            "data": data,
                            "source": source,
                            "timestamp": time.time(),
                        }

                        for subscriber in self._subscribers.get(event_type, []):
                            try:
                                if asyncio.iscoroutinefunction(subscriber):
                                    await subscriber(message)
                                else:
                                    subscriber(message)
                            except Exception as e:
                                self._logger.warning(f"Subscriber error for {event_type}: {e}")

                    def subscribe(self, event_type: str, callback: Callable) -> None:
                        """Subscribe to a specific event type."""
                        self._subscribers[event_type].append(callback)

                    def update_context(self, key: str, value: Any) -> None:
                        """Update shared context."""
                        self._context[key] = {
                            "value": value,
                            "updated_at": time.time(),
                        }

                    def get_context(self, key: str) -> Optional[Any]:
                        """Get value from shared context."""
                        if key in self._context:
                            return self._context[key]["value"]
                        return None

                    async def send_to_node(self, node_id: str, message: Dict[str, Any]) -> bool:
                        """Send a message to a specific node."""
                        if node_id in self._message_queues:
                            await self._message_queues[node_id].put(message)
                            return True
                        return False

                    async def receive_from_node(self, node_id: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
                        """Receive a message from a specific node's queue."""
                        if node_id in self._message_queues:
                            try:
                                return await asyncio.wait_for(
                                    self._message_queues[node_id].get(),
                                    timeout=timeout
                                )
                            except asyncio.TimeoutError:
                                return None
                        return None

                    def get_active_nodes(self, node_type: str = None) -> List[NeuralMeshNode]:
                        """Get all active nodes, optionally filtered by type."""
                        nodes = list(self._nodes.values())
                        if node_type:
                            nodes = [n for n in nodes if n.node_type == node_type]
                        return [n for n in nodes if n.status == "active"]

                    def get_node_with_capability(self, capability: str) -> Optional[NeuralMeshNode]:
                        """Find a node with a specific capability."""
                        for node in self._nodes.values():
                            if capability in node.capabilities and node.status == "active":
                                return node
                        return None

                    async def start(self) -> None:
                        """Start the Neural Mesh sync loop."""
                        self._running = True
                        self._sync_task = asyncio.create_task(self._sync_loop())
                        self._logger.info("Neural Mesh sync loop started")

                    async def stop(self) -> None:
                        """Stop the Neural Mesh."""
                        self._running = False
                        if self._sync_task:
                            self._sync_task.cancel()
                            try:
                                await self._sync_task
                            except asyncio.CancelledError:
                                pass

                    async def _sync_loop(self) -> None:
                        """Background sync loop for health checks and context propagation."""
                        while self._running:
                            try:
                                # Check node health
                                current_time = time.time()
                                for node in list(self._nodes.values()):
                                    if current_time - node.last_heartbeat > self._sync_interval * 3:
                                        node.status = "stale"
                                        self._logger.warning(f"Node {node.node_id} is stale")

                                # Broadcast context sync
                                await self.broadcast("context_sync", self._context, source="mesh")

                                await asyncio.sleep(self._sync_interval)

                            except asyncio.CancelledError:
                                break
                            except Exception as e:
                                self._logger.error(f"Sync loop error: {e}")
                                await asyncio.sleep(1)

                    def get_stats(self) -> Dict[str, Any]:
                        """Get mesh statistics."""
                        return {
                            "total_nodes": len(self._nodes),
                            "active_nodes": len([n for n in self._nodes.values() if n.status == "active"]),
                            "node_types": list(set(n.node_type for n in self._nodes.values())),
                            "context_keys": list(self._context.keys()),
                            "subscriber_count": sum(len(subs) for subs in self._subscribers.values()),
                        }

                # Create and start Neural Mesh
                self._neural_mesh = NeuralMesh(sync_interval=self.config.neural_mesh_sync_interval)

                # Register core nodes
                if initialized_systems["uae"]:
                    self._neural_mesh.register_node(NeuralMeshNode(
                        node_id="uae-primary",
                        node_type="uae",
                        capabilities=["vision", "screen_capture", "element_detection", "chain_of_thought"],
                    ))

                if initialized_systems["sai"]:
                    self._neural_mesh.register_node(NeuralMeshNode(
                        node_id="sai-primary",
                        node_type="sai",
                        capabilities=["window_tracking", "app_focus", "workspace_state", "yabai_control"],
                    ))

                # Start the mesh
                await self._neural_mesh.start()

                initialized_systems["neural_mesh"] = True
                os.environ["NEURAL_MESH_ENABLED"] = "true"
                self.logger.info(f"✅ Neural Mesh initialized (sync interval: {self.config.neural_mesh_sync_interval}s)")
                print(f"  {TerminalUI.GREEN}✓ Neural Mesh: Distributed intelligence coordination active{TerminalUI.RESET}")

            except Exception as e:
                self.logger.error(f"❌ Neural Mesh initialization failed: {e}")
                os.environ["NEURAL_MESH_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ Neural Mesh: Failed ({e}){TerminalUI.RESET}")

        # ═══════════════════════════════════════════════════════════════════
        # Step 4: Initialize MAS (Multi-Agent System)
        # ═══════════════════════════════════════════════════════════════════
        if self.config.mas_enabled:
            try:
                self.logger.info("🤖 Step 4/7: Initializing MAS (Multi-Agent System)...")
                self.logger.info("   • Max concurrent agents: " + str(self.config.mas_max_concurrent_agents))
                self.logger.info("   • Dynamic spawning: Enabled")

                from dataclasses import dataclass, field
                from typing import Dict, Any, List, Optional, Callable
                from enum import Enum
                import uuid

                class AgentStatus(Enum):
                    IDLE = "idle"
                    RUNNING = "running"
                    WAITING = "waiting"
                    COMPLETED = "completed"
                    FAILED = "failed"

                @dataclass
                class AgentTask:
                    """A task for an agent to execute."""
                    task_id: str
                    goal: str
                    context: Dict[str, Any] = field(default_factory=dict)
                    priority: int = 5  # 1-10
                    dependencies: List[str] = field(default_factory=list)
                    parent_task_id: Optional[str] = None
                    status: AgentStatus = AgentStatus.IDLE
                    result: Optional[Any] = None
                    error: Optional[str] = None
                    created_at: float = field(default_factory=time.time)
                    started_at: Optional[float] = None
                    completed_at: Optional[float] = None

                @dataclass
                class Agent:
                    """An autonomous agent in the MAS."""
                    agent_id: str
                    agent_type: str
                    capabilities: List[str] = field(default_factory=list)
                    current_task: Optional[AgentTask] = None
                    status: AgentStatus = AgentStatus.IDLE
                    metrics: Dict[str, Any] = field(default_factory=dict)

                class MultiAgentSystem:
                    """
                    Multi-Agent System for coordinated autonomous execution.

                    Provides:
                    - Dynamic agent spawning based on task complexity
                    - Task decomposition and parallel execution
                    - Agent collaboration and result aggregation
                    - Conflict resolution for shared resources
                    - Load balancing across available agents
                    """

                    def __init__(self, max_concurrent_agents: int = 5):
                        self._agents: Dict[str, Agent] = {}
                        self._task_queue: asyncio.Queue = asyncio.Queue()
                        self._completed_tasks: Dict[str, AgentTask] = {}
                        self._max_concurrent = max_concurrent_agents
                        self._running = False
                        self._coordinator_task: Optional[asyncio.Task] = None
                        self._agent_executors: Dict[str, Callable] = {}
                        self._logger = logging.getLogger("MAS")

                    def register_agent_type(self, agent_type: str, executor: Callable) -> None:
                        """Register an agent type with its executor function."""
                        self._agent_executors[agent_type] = executor
                        self._logger.debug(f"Agent type registered: {agent_type}")

                    async def spawn_agent(self, agent_type: str, capabilities: List[str] = None) -> Agent:
                        """Spawn a new agent."""
                        if len(self._agents) >= self._max_concurrent:
                            # Find idle agent to reuse
                            for agent in self._agents.values():
                                if agent.status == AgentStatus.IDLE:
                                    agent.capabilities = capabilities or []
                                    return agent
                            raise RuntimeError(f"Max agents ({self._max_concurrent}) reached")

                        agent = Agent(
                            agent_id=f"agent-{uuid.uuid4().hex[:8]}",
                            agent_type=agent_type,
                            capabilities=capabilities or [],
                        )
                        self._agents[agent.agent_id] = agent
                        self._logger.info(f"Agent spawned: {agent.agent_id} ({agent_type})")
                        return agent

                    async def submit_task(self, task: AgentTask) -> str:
                        """Submit a task for execution."""
                        await self._task_queue.put(task)
                        self._logger.debug(f"Task submitted: {task.task_id}")
                        return task.task_id

                    async def decompose_task(self, goal: str, context: Dict[str, Any] = None) -> List[AgentTask]:
                        """Decompose a complex goal into subtasks."""
                        # This would typically use an LLM to break down the task
                        # For now, return as single task
                        task = AgentTask(
                            task_id=f"task-{uuid.uuid4().hex[:8]}",
                            goal=goal,
                            context=context or {},
                        )
                        return [task]

                    async def execute_task(self, task: AgentTask) -> AgentTask:
                        """Execute a single task."""
                        task.status = AgentStatus.RUNNING
                        task.started_at = time.time()

                        try:
                            # Find appropriate agent type
                            agent_type = self._determine_agent_type(task)

                            # Spawn or reuse agent
                            agent = await self.spawn_agent(agent_type)
                            agent.current_task = task
                            agent.status = AgentStatus.RUNNING

                            # Get executor for this agent type
                            executor = self._agent_executors.get(agent_type)
                            if executor:
                                if asyncio.iscoroutinefunction(executor):
                                    result = await executor(task)
                                else:
                                    result = executor(task)
                                task.result = result
                                task.status = AgentStatus.COMPLETED
                            else:
                                task.error = f"No executor for agent type: {agent_type}"
                                task.status = AgentStatus.FAILED

                        except Exception as e:
                            task.error = str(e)
                            task.status = AgentStatus.FAILED
                            self._logger.error(f"Task {task.task_id} failed: {e}")

                        finally:
                            task.completed_at = time.time()
                            if agent:
                                agent.current_task = None
                                agent.status = AgentStatus.IDLE

                        self._completed_tasks[task.task_id] = task
                        return task

                    def _determine_agent_type(self, task: AgentTask) -> str:
                        """Determine the best agent type for a task."""
                        goal_lower = task.goal.lower()

                        if any(w in goal_lower for w in ["search", "find", "look"]):
                            return "explorer"
                        elif any(w in goal_lower for w in ["write", "create", "generate"]):
                            return "creator"
                        elif any(w in goal_lower for w in ["analyze", "review", "check"]):
                            return "analyzer"
                        elif any(w in goal_lower for w in ["scrape", "fetch", "download"]):
                            return "scraper"
                        else:
                            return "general"

                    async def run_goal(self, goal: str, context: Dict[str, Any] = None) -> List[AgentTask]:
                        """Execute a goal by decomposing and running all subtasks."""
                        tasks = await self.decompose_task(goal, context)
                        results = []

                        # Execute tasks (respecting dependencies)
                        for task in tasks:
                            result = await self.execute_task(task)
                            results.append(result)

                        return results

                    async def start(self) -> None:
                        """Start the MAS coordinator."""
                        self._running = True
                        self._coordinator_task = asyncio.create_task(self._coordinate())
                        self._logger.info("MAS coordinator started")

                    async def stop(self) -> None:
                        """Stop the MAS."""
                        self._running = False
                        if self._coordinator_task:
                            self._coordinator_task.cancel()
                            try:
                                await self._coordinator_task
                            except asyncio.CancelledError:
                                pass

                    async def _coordinate(self) -> None:
                        """Background coordination loop."""
                        while self._running:
                            try:
                                # Process queued tasks
                                try:
                                    task = await asyncio.wait_for(
                                        self._task_queue.get(),
                                        timeout=1.0
                                    )
                                    asyncio.create_task(self.execute_task(task))
                                except asyncio.TimeoutError:
                                    pass

                                # Clean up completed agents
                                for agent in list(self._agents.values()):
                                    if agent.status == AgentStatus.COMPLETED:
                                        agent.status = AgentStatus.IDLE

                            except asyncio.CancelledError:
                                break
                            except Exception as e:
                                self._logger.error(f"Coordinator error: {e}")
                                await asyncio.sleep(1)

                    def get_stats(self) -> Dict[str, Any]:
                        """Get MAS statistics."""
                        return {
                            "total_agents": len(self._agents),
                            "active_agents": len([a for a in self._agents.values() if a.status == AgentStatus.RUNNING]),
                            "idle_agents": len([a for a in self._agents.values() if a.status == AgentStatus.IDLE]),
                            "queued_tasks": self._task_queue.qsize(),
                            "completed_tasks": len(self._completed_tasks),
                            "max_concurrent": self._max_concurrent,
                        }

                # Create and start MAS
                self._mas = MultiAgentSystem(max_concurrent_agents=self.config.mas_max_concurrent_agents)

                # Register default agent executors
                async def general_executor(task: AgentTask) -> Dict[str, Any]:
                    """Default general-purpose agent executor."""
                    return {"status": "completed", "message": f"Processed: {task.goal}"}

                async def scraper_executor(task: AgentTask) -> Dict[str, Any]:
                    """Web scraping agent executor."""
                    # This would integrate with Safe Scout
                    return {"status": "completed", "message": f"Scraped: {task.goal}"}

                self._mas.register_agent_type("general", general_executor)
                self._mas.register_agent_type("explorer", general_executor)
                self._mas.register_agent_type("creator", general_executor)
                self._mas.register_agent_type("analyzer", general_executor)
                self._mas.register_agent_type("scraper", scraper_executor)

                # Start MAS
                await self._mas.start()

                # Register MAS with Neural Mesh if available
                if hasattr(self, '_neural_mesh') and self._neural_mesh:
                    from dataclasses import dataclass, field
                    self._neural_mesh.register_node(NeuralMeshNode(
                        node_id="mas-coordinator",
                        node_type="mas",
                        capabilities=["task_decomposition", "agent_spawning", "parallel_execution"],
                    ))

                initialized_systems["mas"] = True
                os.environ["MAS_ENABLED"] = "true"
                self.logger.info(f"✅ MAS initialized (max agents: {self.config.mas_max_concurrent_agents})")
                print(f"  {TerminalUI.GREEN}✓ MAS: Multi-Agent System active{TerminalUI.RESET}")

            except Exception as e:
                self.logger.error(f"❌ MAS initialization failed: {e}")
                os.environ["MAS_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ MAS: Failed ({e}){TerminalUI.RESET}")

        # ═══════════════════════════════════════════════════════════════════
        # Step 5: Initialize CAI (Collective AI Intelligence)
        # ═══════════════════════════════════════════════════════════════════
        if self.config.cai_enabled:
            try:
                self.logger.info("🧬 Step 5/7: Initializing CAI (Collective AI Intelligence)...")

                from dataclasses import dataclass, field
                from typing import Dict, Any, List, Optional

                @dataclass
                class InsightSource:
                    """Source of an insight."""
                    system: str  # "uae", "sai", "mas", etc.
                    confidence: float
                    timestamp: float
                    data: Dict[str, Any]

                @dataclass
                class CollectiveInsight:
                    """An insight aggregated from multiple sources."""
                    insight_id: str
                    topic: str
                    sources: List[InsightSource] = field(default_factory=list)
                    aggregated_confidence: float = 0.0
                    recommendations: List[str] = field(default_factory=list)
                    created_at: float = field(default_factory=time.time)

                class CollectiveAI:
                    """
                    Collective AI Intelligence - Emergent intelligence from all subsystems.

                    Provides:
                    - Synthesis of insights from UAE, SAI, MAS
                    - Cross-system pattern detection
                    - Proactive recommendation generation
                    - Adaptive learning from system interactions
                    """

                    def __init__(self):
                        self._insights: Dict[str, CollectiveInsight] = {}
                        self._patterns: List[Dict[str, Any]] = []
                        self._recommendation_callbacks: List[Callable] = []
                        self._logger = logging.getLogger("CAI")

                    def add_insight_source(self, topic: str, source: InsightSource) -> None:
                        """Add an insight source for a topic."""
                        if topic not in self._insights:
                            self._insights[topic] = CollectiveInsight(
                                insight_id=f"insight-{uuid.uuid4().hex[:8]}",
                                topic=topic,
                            )

                        self._insights[topic].sources.append(source)
                        self._recalculate_confidence(topic)

                    def _recalculate_confidence(self, topic: str) -> None:
                        """Recalculate aggregated confidence for a topic."""
                        if topic in self._insights:
                            insight = self._insights[topic]
                            if insight.sources:
                                # Weighted average based on source confidence
                                total = sum(s.confidence for s in insight.sources)
                                insight.aggregated_confidence = total / len(insight.sources)

                    def get_insight(self, topic: str) -> Optional[CollectiveInsight]:
                        """Get the collective insight for a topic."""
                        return self._insights.get(topic)

                    def detect_patterns(self) -> List[Dict[str, Any]]:
                        """Detect patterns across all insights."""
                        # Simple pattern detection - look for topics with multiple high-confidence sources
                        patterns = []
                        for insight in self._insights.values():
                            if len(insight.sources) >= 2 and insight.aggregated_confidence > 0.7:
                                patterns.append({
                                    "topic": insight.topic,
                                    "confidence": insight.aggregated_confidence,
                                    "source_count": len(insight.sources),
                                    "systems": list(set(s.system for s in insight.sources)),
                                })
                        self._patterns = patterns
                        return patterns

                    async def generate_recommendations(self) -> List[str]:
                        """Generate proactive recommendations based on patterns."""
                        recommendations = []
                        patterns = self.detect_patterns()

                        for pattern in patterns:
                            if pattern["confidence"] > 0.8:
                                recommendations.append(
                                    f"High-confidence pattern detected in {pattern['topic']} "
                                    f"across {pattern['systems']}"
                                )

                        # Notify callbacks
                        for callback in self._recommendation_callbacks:
                            try:
                                if asyncio.iscoroutinefunction(callback):
                                    await callback(recommendations)
                                else:
                                    callback(recommendations)
                            except Exception as e:
                                self._logger.warning(f"Recommendation callback error: {e}")

                        return recommendations

                    def register_recommendation_callback(self, callback: Callable) -> None:
                        """Register a callback for recommendations."""
                        self._recommendation_callbacks.append(callback)

                    def get_stats(self) -> Dict[str, Any]:
                        """Get CAI statistics."""
                        return {
                            "total_insights": len(self._insights),
                            "total_patterns": len(self._patterns),
                            "high_confidence_insights": len([
                                i for i in self._insights.values()
                                if i.aggregated_confidence > 0.7
                            ]),
                        }

                # Create CAI instance
                self._cai = CollectiveAI()

                # Connect CAI to Neural Mesh for insight aggregation
                if hasattr(self, '_neural_mesh') and self._neural_mesh:
                    async def on_context_sync(message: Dict[str, Any]) -> None:
                        """Handle context sync from Neural Mesh."""
                        for key, value in message.get("data", {}).items():
                            self._cai.add_insight_source(
                                topic=key,
                                source=InsightSource(
                                    system="mesh",
                                    confidence=0.8,
                                    timestamp=time.time(),
                                    data=value,
                                )
                            )

                    self._neural_mesh.subscribe("context_sync", on_context_sync)

                initialized_systems["cai"] = True
                os.environ["CAI_ENABLED"] = "true"
                self.logger.info("✅ CAI initialized (Collective AI Intelligence)")
                print(f"  {TerminalUI.GREEN}✓ CAI: Collective AI Intelligence active{TerminalUI.RESET}")

            except Exception as e:
                self.logger.error(f"❌ CAI initialization failed: {e}")
                os.environ["CAI_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ CAI: Failed ({e}){TerminalUI.RESET}")

        # ═══════════════════════════════════════════════════════════════════
        # Step 6: Initialize Continuous Background Web Scraping
        # ═══════════════════════════════════════════════════════════════════
        if self.config.continuous_scraping_enabled:
            try:
                self.logger.info("🌐 Step 6/7: Initializing Continuous Background Web Scraping...")

                # Start the continuous scraping background task
                self._continuous_scraping_task = asyncio.create_task(
                    self._run_continuous_scraping()
                )

                initialized_systems["continuous_scraping"] = True
                os.environ["CONTINUOUS_SCRAPING_ENABLED"] = "true"
                self.logger.info(
                    f"✅ Continuous scraping initialized "
                    f"(interval: {self.config.continuous_scraping_interval_hours}h, "
                    f"max pages: {self.config.continuous_scraping_max_pages})"
                )
                print(f"  {TerminalUI.GREEN}✓ Web Scraping: Continuous background learning active{TerminalUI.RESET}")

            except Exception as e:
                self.logger.error(f"❌ Continuous scraping initialization failed: {e}")
                os.environ["CONTINUOUS_SCRAPING_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ Web Scraping: Failed ({e}){TerminalUI.RESET}")

        # ═══════════════════════════════════════════════════════════════════
        # Step 7: Initialize Reactor-Core Integration
        # ═══════════════════════════════════════════════════════════════════
        if self.config.reactor_core_integration_enabled:
            try:
                self.logger.info("⚛️ Step 7/7: Initializing Reactor-Core Integration...")

                from autonomy.reactor_core_integration import (
                    get_reactor_core_integration,
                    initialize_reactor_core,
                    initialize_prime_neural_mesh,
                    get_prime_neural_mesh_bridge,
                    ReactorCoreConfig,
                )

                # Get reactor-core integration singleton
                self._reactor_core_integration = get_reactor_core_integration()

                # Configure with paths from supervisor config
                if hasattr(self.config, 'reactor_core_repo_path'):
                    self._reactor_core_integration.config.reactor_core_path = Path(
                        self.config.reactor_core_repo_path
                    )
                if hasattr(self.config, 'jarvis_prime_repo_path'):
                    self._reactor_core_integration.config.jarvis_prime_path = Path(
                        self.config.jarvis_prime_repo_path
                    )

                # Initialize all reactor-core components
                reactor_init_success = await initialize_reactor_core()

                if reactor_init_success:
                    # Connect to Neural Mesh if available
                    if hasattr(self, '_neural_mesh') and self._neural_mesh:
                        # Register reactor-core as a Neural Mesh node
                        self._neural_mesh.register_node(NeuralMeshNode(
                            node_id="reactor_core",
                            node_type="reactor_core",
                            capabilities=["training", "scraping", "model_deployment", "experience_collection"],
                            status="active",
                        ))
                        self.logger.info("✅ Reactor-Core connected to Neural Mesh")

                    # Connect to CAI if available for insight aggregation
                    if hasattr(self, '_cai') and self._cai:
                        async def on_reactor_event(event: Dict[str, Any]) -> None:
                            """Feed reactor-core events into CAI for analysis."""
                            self._cai.add_insight_source(
                                topic=event.get("event_type", "reactor_core"),
                                source=InsightSource(
                                    system="reactor_core",
                                    confidence=0.85,
                                    timestamp=time.time(),
                                    data=event,
                                )
                            )

                        self._reactor_core_integration.register_prime_callback(on_reactor_event)
                        self.logger.info("✅ Reactor-Core connected to CAI for intelligence aggregation")

                    # Connect to Data Flywheel for experience syncing
                    if hasattr(self, '_data_flywheel') and self._data_flywheel:
                        self.logger.info("✅ Reactor-Core connected to Data Flywheel")

                    # Initialize Prime Neural Mesh bridge
                    try:
                        prime_mesh_success = await initialize_prime_neural_mesh()
                        if prime_mesh_success:
                            self._prime_neural_mesh_bridge = get_prime_neural_mesh_bridge()
                            self.logger.info("✅ Prime Neural Mesh bridge initialized")

                            # Connect bridge to CAI if available
                            if hasattr(self, '_cai') and self._cai:
                                async def on_prime_mesh_event(event: Dict[str, Any]) -> None:
                                    """Feed Prime mesh events into CAI for analysis."""
                                    self._cai.add_insight_source(
                                        topic=f"prime_{event.get('event_type', 'unknown')}",
                                        source=InsightSource(
                                            system="prime_neural_mesh",
                                            confidence=0.9,
                                            timestamp=time.time(),
                                            data=event,
                                        )
                                    )

                                self._prime_neural_mesh_bridge.register_callback(on_prime_mesh_event)
                                self.logger.info("✅ Prime Neural Mesh connected to CAI")

                            print(f"  {TerminalUI.GREEN}✓ Prime Neural Mesh: JARVIS-Prime bridge active{TerminalUI.RESET}")
                        else:
                            self.logger.warning("⚠️ Prime Neural Mesh bridge initialization incomplete")
                    except Exception as prime_mesh_error:
                        self.logger.warning(f"⚠️ Prime Neural Mesh initialization skipped: {prime_mesh_error}")

                    initialized_systems["reactor_core"] = True
                    os.environ["REACTOR_CORE_ENABLED"] = "true"

                    # Get status for logging
                    reactor_status = self._reactor_core_integration.get_status()
                    active_components = [k for k, v in reactor_status["components"].items() if v]

                    self.logger.info(
                        f"✅ Reactor-Core Integration initialized "
                        f"(components: {', '.join(active_components)})"
                    )
                    print(f"  {TerminalUI.GREEN}✓ Reactor-Core: Training pipeline integration active{TerminalUI.RESET}")
                else:
                    self.logger.warning("⚠️ Reactor-Core initialization returned false")
                    os.environ["REACTOR_CORE_ENABLED"] = "false"
                    print(f"  {TerminalUI.YELLOW}⚠️ Reactor-Core: Partial initialization{TerminalUI.RESET}")

            except ImportError as e:
                self.logger.warning(f"⚠️ Reactor-Core not available: {e}")
                os.environ["REACTOR_CORE_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ Reactor-Core: Not available{TerminalUI.RESET}")
            except Exception as e:
                self.logger.error(f"❌ Reactor-Core initialization failed: {e}")
                os.environ["REACTOR_CORE_ENABLED"] = "false"
                print(f"  {TerminalUI.YELLOW}⚠️ Reactor-Core: Failed ({e}){TerminalUI.RESET}")

        # ═══════════════════════════════════════════════════════════════════
        # Broadcast Intelligence Systems Status
        # ═══════════════════════════════════════════════════════════════════
        active_systems = [k for k, v in initialized_systems.items() if v]

        await self._broadcast_startup_progress(
            stage="intelligence_systems_ready",
            message=f"Intelligence stack online: {', '.join(active_systems)}",
            progress=85,
            metadata={
                "intelligence_systems": {
                    "uae": initialized_systems["uae"],
                    "sai": initialized_systems["sai"],
                    "neural_mesh": initialized_systems["neural_mesh"],
                    "mas": initialized_systems["mas"],
                    "cai": initialized_systems["cai"],
                    "continuous_scraping": initialized_systems["continuous_scraping"],
                    "reactor_core": initialized_systems["reactor_core"],
                    "active_count": len(active_systems),
                }
            }
        )

        self.logger.info("═" * 60)
        self.logger.info(f"✅ Intelligence Stack: {len(active_systems)}/7 systems active")
        self.logger.info("═" * 60)

    async def _run_continuous_scraping(self) -> None:
        """
        Background task for continuous web scraping.

        Runs at configurable intervals (default: every 4 hours) to:
        - Discover new documentation and learning content
        - Scrape and process web pages via Safe Scout
        - Feed new data into the training pipeline
        """
        interval_seconds = self.config.continuous_scraping_interval_hours * 3600
        self.logger.info(f"📅 Continuous scraping started (interval: {self.config.continuous_scraping_interval_hours}h)")

        while True:
            try:
                # Wait for the configured interval
                await asyncio.sleep(interval_seconds)

                self.logger.info("🌐 Starting continuous web scraping cycle...")

                # Check if flywheel is available and not busy
                if hasattr(self, '_data_flywheel') and self._data_flywheel:
                    if self._data_flywheel.is_running:
                        self.logger.debug("Flywheel busy, skipping scraping cycle")
                        continue

                    # Parse topics from config (comma-separated)
                    topics = []
                    if self.config.continuous_scraping_topics:
                        topics = [t.strip() for t in self.config.continuous_scraping_topics.split(",") if t.strip()]

                    # If no explicit topics, use learning goals
                    if not topics and hasattr(self, '_learning_goals_manager') and self._learning_goals_manager:
                        pending_goals = self._learning_goals_manager.get_pending_goals()
                        topics = [g.topic for g in pending_goals[:5]]  # Top 5 priority topics

                    if topics:
                        self.logger.info(f"📚 Scraping topics: {topics}")

                        # Announce scraping start
                        if hasattr(self, 'narrator') and self.narrator:
                            await self.narrator.speak(
                                f"Starting background web research on {len(topics)} topics.",
                                wait=False
                            )

                        # Run flywheel with web scraping only (no training)
                        try:
                            result = await self._data_flywheel.run_web_scraping_only(
                                topics=topics,
                                max_pages=self.config.continuous_scraping_max_pages,
                            )

                            if result.success:
                                self.logger.info(
                                    f"✅ Scraping complete: {result.progress.web_pages_scraped} pages, "
                                    f"{result.progress.experiences_collected} experiences"
                                )

                                # Update CAI with scraping insights
                                if hasattr(self, '_cai') and self._cai:
                                    from dataclasses import dataclass, field
                                    self._cai.add_insight_source(
                                        topic="web_scraping",
                                        source=InsightSource(
                                            system="scraper",
                                            confidence=0.7,
                                            timestamp=time.time(),
                                            data={
                                                "pages_scraped": result.progress.web_pages_scraped,
                                                "topics": topics,
                                            }
                                        )
                                    )
                            else:
                                self.logger.warning(f"⚠️ Scraping failed: {result.error}")

                        except AttributeError:
                            # Flywheel might not have run_web_scraping_only method
                            # Fall back to full cycle without training
                            result = await self._data_flywheel.run_full_cycle(
                                include_web_scraping=True,
                                include_training=False,
                            )
                    else:
                        self.logger.debug("No topics configured for scraping")

                else:
                    self.logger.debug("Data Flywheel not available for scraping")

            except asyncio.CancelledError:
                self.logger.info("Continuous scraping stopped")
                break
            except Exception as e:
                self.logger.error(f"Continuous scraping error: {e}")
                # Wait 30 minutes before retrying on error
                await asyncio.sleep(1800)

    async def _stop_intelligence_systems(self) -> None:
        """Stop all intelligence systems gracefully."""
        self.logger.info("🛑 Stopping Intelligence Systems...")

        # Stop continuous scraping
        if hasattr(self, '_continuous_scraping_task') and self._continuous_scraping_task:
            self._continuous_scraping_task.cancel()
            try:
                await self._continuous_scraping_task
            except asyncio.CancelledError:
                pass
            self._continuous_scraping_task = None

        # Stop MAS
        if hasattr(self, '_mas') and self._mas:
            await self._mas.stop()
            self._mas = None

        # Stop Neural Mesh
        if hasattr(self, '_neural_mesh') and self._neural_mesh:
            await self._neural_mesh.stop()
            self._neural_mesh = None

        # Shutdown UAE
        if hasattr(self, '_uae_engine') and self._uae_engine:
            try:
                from intelligence.uae_integration import shutdown_uae
                await shutdown_uae()
            except Exception as e:
                self.logger.warning(f"UAE shutdown error: {e}")
            self._uae_engine = None
            self._enhanced_uae = None

        # Clear CAI
        self._cai = None

        # Shutdown Prime Neural Mesh Bridge
        if hasattr(self, '_prime_neural_mesh_bridge') and self._prime_neural_mesh_bridge:
            try:
                await self._prime_neural_mesh_bridge.shutdown()
            except Exception as e:
                self.logger.warning(f"Prime Neural Mesh shutdown error: {e}")
            self._prime_neural_mesh_bridge = None

        # Shutdown Reactor-Core Integration
        if hasattr(self, '_reactor_core_integration') and self._reactor_core_integration:
            try:
                from autonomy.reactor_core_integration import shutdown_reactor_core
                await shutdown_reactor_core()
            except Exception as e:
                self.logger.warning(f"Reactor-Core shutdown error: {e}")
            self._reactor_core_integration = None

        self.logger.info("✅ Intelligence Systems stopped")

    async def _run_training_scheduler(self) -> None:
        """
        Background task that schedules training runs.

        Default: Runs at 3 AM daily when:
        - Enough experiences have been collected
        - Cooldown period has passed
        """
        self.logger.info(f"📅 Training scheduler started (schedule: {self.config.data_flywheel_training_schedule})")

        while True:
            try:
                # Parse schedule time
                schedule_hour, schedule_minute = map(int, self.config.data_flywheel_training_schedule.split(":"))

                # Calculate seconds until next scheduled time
                now = datetime.now()
                target = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)

                if target <= now:
                    # Already passed today, schedule for tomorrow
                    from datetime import timedelta
                    target += timedelta(days=1)

                sleep_seconds = (target - now).total_seconds()
                self.logger.debug(f"Training scheduled in {sleep_seconds / 3600:.1f} hours")

                # Sleep until scheduled time
                await asyncio.sleep(sleep_seconds)

                # Check if flywheel is ready
                if self._data_flywheel and not self._data_flywheel.is_running:
                    self.logger.info("🚀 Starting scheduled training run...")

                    # Announce training
                    if hasattr(self, 'narrator') and self.narrator:
                        await self.narrator.speak(
                            "Starting scheduled model training. This may take a while.",
                            wait=False
                        )

                    # Run flywheel cycle
                    result = await self._data_flywheel.run_full_cycle(
                        include_web_scraping=True,
                        include_training=True,
                    )

                    if result.success:
                        self.logger.info(
                            f"✅ Training completed: {result.progress.dataset_examples} examples, "
                            f"model: {result.model_path or 'not trained'}"
                        )
                        if hasattr(self, 'narrator') and self.narrator:
                            await self.narrator.speak(
                                f"Training complete. Processed {result.progress.dataset_examples} examples.",
                                wait=False
                            )
                    else:
                        self.logger.warning(f"⚠️ Training failed: {result.error}")

                else:
                    self.logger.debug("Flywheel busy or not ready, skipping scheduled run")

            except asyncio.CancelledError:
                self.logger.info("Training scheduler stopped")
                break
            except Exception as e:
                self.logger.error(f"Training scheduler error: {e}")
                await asyncio.sleep(3600)  # Wait an hour before retrying

    async def _run_experience_collection_loop(self) -> None:
        """
        v9.1: Background task for continuous experience collection.

        This loop:
        - Periodically collects experiences from JARVIS interactions
        - Syncs with reactor-core JARVISConnector
        - Monitors experience quality and quantity
        - Triggers learning goal discovery when patterns emerge

        Runs every 5 minutes to ensure fresh training data.
        """
        collection_interval = 300  # 5 minutes
        self.logger.info(f"🔄 Experience collection loop started (interval: {collection_interval}s)")

        while True:
            try:
                await asyncio.sleep(collection_interval)

                if not self._data_flywheel or self._data_flywheel.is_running:
                    continue

                # Collect recent experiences
                try:
                    if self._data_flywheel._jarvis_connector:
                        experiences = await asyncio.get_event_loop().run_in_executor(
                            None,
                            self._data_flywheel._jarvis_connector.collect_recent_experiences,
                            1  # Last 1 hour
                        )

                        if experiences:
                            self.logger.debug(f"Collected {len(experiences)} recent experiences")

                            # Store in training database
                            if hasattr(self._data_flywheel, 'add_experience'):
                                for exp in experiences[:10]:  # Limit batch size
                                    self._data_flywheel.add_experience(
                                        source="auto_collection",
                                        input_text=exp.get("input", ""),
                                        output_text=exp.get("output", ""),
                                        context=exp.get("context", {}),
                                        quality_score=exp.get("quality", 0.5),
                                    )

                except Exception as collect_err:
                    self.logger.debug(f"Experience collection cycle: {collect_err}")

                # Check if we should trigger learning goal discovery
                try:
                    if hasattr(self, '_learning_goals_manager') and self._learning_goals_manager:
                        stats = self._data_flywheel.get_stats() if hasattr(self._data_flywheel, 'get_stats') else {}
                        total_experiences = stats.get("total_experiences", 0)

                        # Discover new goals every 100 experiences
                        if total_experiences > 0 and total_experiences % 100 == 0:
                            await self._learning_goals_manager.auto_discover_topics()
                            self.logger.info(f"Auto-discovered learning topics at {total_experiences} experiences")
                except Exception as goal_err:
                    self.logger.debug(f"Learning goal discovery: {goal_err}")

            except asyncio.CancelledError:
                self.logger.info("Experience collection loop stopped")
                break
            except Exception as e:
                self.logger.warning(f"Experience collection error: {e}")
                await asyncio.sleep(60)  # Wait a minute before retrying

    async def _stop_data_flywheel(self) -> None:
        """Stop the Data Flywheel and related tasks."""
        # Cancel experience collection loop
        if hasattr(self, '_experience_collection_task') and self._experience_collection_task:
            self._experience_collection_task.cancel()
            try:
                await self._experience_collection_task
            except asyncio.CancelledError:
                pass
            self._experience_collection_task = None

        # Cancel training scheduler
        if self._training_scheduler_task:
            self._training_scheduler_task.cancel()
            try:
                await self._training_scheduler_task
            except asyncio.CancelledError:
                pass
            self._training_scheduler_task = None

        # Cancel any running flywheel
        if self._data_flywheel:
            await self._data_flywheel.cancel()
            self._data_flywheel = None

        self._learning_goals_manager = None

    async def _stop_jarvis_prime(self) -> None:
        """Stop JARVIS-Prime subprocess or container."""
        # Stop subprocess
        if self._jarvis_prime_process:
            try:
                self._jarvis_prime_process.terminate()
                await asyncio.wait_for(self._jarvis_prime_process.wait(), timeout=5.0)
            except Exception:
                self._jarvis_prime_process.kill()
            self._jarvis_prime_process = None

        # Stop Docker container
        if self.config.jarvis_prime_use_docker:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "stop", "jarvis-prime",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except Exception:
                pass

        # Stop Data Flywheel and training scheduler
        await self._stop_data_flywheel()

        # Stop reactor-core watcher
        if self._reactor_core_watcher:
            await self._reactor_core_watcher.stop()
            self._reactor_core_watcher = None

    def _wire_router_to_runner(self) -> None:
        """
        Wire the TieredCommandRouter's execute_tier2 to use the AgenticTaskRunner.

        This creates a seamless flow:
        Voice Command -> TieredRouter -> AgenticRunner -> Computer Use
        """
        if not self._tiered_router or not self._agentic_runner:
            return

        # Store reference for the closure
        runner = self._agentic_runner
        vbia_adapter = self._vbia_adapter

        async def execute_tier2_via_runner(command: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
            """Execute Tier 2 command via the unified AgenticTaskRunner."""
            from core.agentic_task_runner import RunnerMode

            try:
                # Execute via runner
                result = await runner.run(
                    goal=command,
                    mode=RunnerMode.AUTONOMOUS,
                    context=context,
                    narrate=True,
                )

                return {
                    "success": result.success,
                    "response": result.final_message,
                    "actions_count": result.actions_count,
                    "duration_ms": result.execution_time_ms,
                    "mode": result.mode,
                    "watchdog_status": result.watchdog_status,
                }

            except Exception as e:
                return {
                    "success": False,
                    "error": str(e),
                    "response": f"Task execution failed: {e}",
                }

        # Monkey-patch the router's execute_tier2 method
        self._tiered_router.execute_tier2 = execute_tier2_via_runner
        self.logger.debug("[Supervisor] Router execute_tier2 wired to AgenticRunner")

    async def _on_hot_reload_triggered(self, changed_files: List[str]) -> None:
        """
        v5.0: Handle hot reload trigger - restart JARVIS with new code.
        
        This is called by the HotReloadWatcher when file changes are detected.
        """
        self.logger.info(f"🔥 Hot reload triggered by {len(changed_files)} file change(s)")
        restart_start_time = time.time()
        
        if not self._supervisor:
            self.logger.warning("No supervisor reference - cannot hot reload")
            return
        
        # Detect file types for announcement
        file_types = []
        for f in changed_files:
            ext = Path(f).suffix.lower()
            if ext in (".py", ".pyx"):
                if "Python" not in file_types:
                    file_types.append("Python")
            elif ext in (".rs",):
                if "Rust" not in file_types:
                    file_types.append("Rust")
            elif ext in (".swift",):
                if "Swift" not in file_types:
                    file_types.append("Swift")
            elif ext in (".js", ".jsx"):
                if "JavaScript" not in file_types:
                    file_types.append("JavaScript")
            elif ext in (".ts", ".tsx"):
                if "TypeScript" not in file_types:
                    file_types.append("TypeScript")
        
        # v5.0: Announce changes via voice coordinator
        try:
            from core.supervisor import get_startup_voice_coordinator
            voice_coordinator = get_startup_voice_coordinator()
            
            # Announce detection
            await voice_coordinator.announce_hot_reload_detected(
                file_count=len(changed_files),
                file_types=file_types,
                target="backend",
            )
            
            # Announce restart
            await voice_coordinator.announce_hot_reload_restarting(target="backend")
            
        except Exception as e:
            self.logger.debug(f"Voice announcement failed (non-fatal): {e}")
        
        try:
            # Clear Python cache for changed modules
            self._clear_python_cache(changed_files)
            
            # Request supervisor restart (this gracefully stops and restarts JARVIS)
            # The supervisor has built-in restart capability
            from core.supervisor import request_restart, RestartSource, RestartUrgency
            
            await request_restart(
                source=RestartSource.DEV_MODE,
                reason=f"Hot reload: {len(changed_files)} file(s) changed",
                urgency=RestartUrgency.NORMAL,
            )
            
            self.logger.info("✅ Restart requested via supervisor")
            
            # v5.0: Announce completion (after restart completes)
            # Note: This runs before restart actually completes, but we still announce
            # The actual restart will trigger a full startup sequence with its own announcements
            
        except Exception as e:
            self.logger.error(f"Hot reload failed: {e}")
            # Fallback: Try direct supervisor restart
            try:
                if hasattr(self._supervisor, '_handle_restart_request'):
                    await self._supervisor._handle_restart_request()
            except Exception as e2:
                self.logger.error(f"Fallback restart also failed: {e2}")
    
    def _clear_python_cache(self, changed_files: List[str]) -> None:
        """Clear Python import cache for changed modules."""
        import shutil
        
        # Clear __pycache__ directories
        for cache_dir in (Path(__file__).parent / "backend").glob("**/__pycache__"):
            try:
                shutil.rmtree(cache_dir)
            except Exception:
                pass
        
        # Clear sys.modules for JARVIS modules
        modules_to_clear = []
        for module_name in list(sys.modules.keys()):
            if 'jarvis' in module_name.lower() or 'backend' in module_name.lower():
                modules_to_clear.append(module_name)
        
        for module_name in modules_to_clear:
            sys.modules.pop(module_name, None)
        
        self.logger.debug(f"Cleared {len(modules_to_clear)} cached modules")
    
    def _print_config_summary(self, supervisor) -> None:
        """Print supervisor configuration summary."""
        config = supervisor.config
        
        update_status = f"Enabled ({config.update.check.interval_seconds}s)" if config.update.check.enabled else "Disabled"
        idle_status = f"Enabled ({config.idle.threshold_seconds // 3600}h threshold)" if config.idle.enabled else "Disabled"
        rollback_status = "Enabled" if config.rollback.auto_on_boot_failure else "Disabled"
        
        # v3.0: Zero-Touch status
        zero_touch_status = "Enabled" if self.config.zero_touch_enabled else "Disabled"
        dms_status = f"Enabled ({self.config.dms_probation_seconds}s)" if self.config.dms_enabled else "Disabled"
        
        TerminalUI.print_info("Mode", config.mode.value.upper(), highlight=True)
        TerminalUI.print_info("Update Check", update_status)
        TerminalUI.print_info("Idle Updates", idle_status)
        TerminalUI.print_info("Auto-Rollback", rollback_status)
        TerminalUI.print_info("Max Retries", str(config.health.max_crash_retries))
        
        # v3.0: Zero-Touch info
        TerminalUI.print_info("Zero-Touch", zero_touch_status, highlight=self.config.zero_touch_enabled)
        TerminalUI.print_info("Dead Man's Switch", dms_status)
        TerminalUI.print_info("AGI OS Integration", "Enabled" if self.config.agi_os_enabled else "Disabled")
        
        TerminalUI.print_info("Loading Page", f"Enabled (port {self.config.required_ports[2]})", highlight=True)
        TerminalUI.print_info("Voice Narration", "Enabled" if self.config.voice_enabled else "Disabled", highlight=True)


# =============================================================================
# Entry Point
# =============================================================================

def parse_args():
    """Parse command-line arguments."""
    import argparse

    parser = argparse.ArgumentParser(
        description="JARVIS Supervisor - Unified System Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start supervisor normally
  python run_supervisor.py

  # Execute single agentic task and exit
  python run_supervisor.py --task "Open Safari and check the weather"

  # Execute task with specific mode
  python run_supervisor.py --task "Organize desktop" --mode autonomous

  # Disable voice narration
  python run_supervisor.py --no-voice
"""
    )

    parser.add_argument(
        "--task", "-t",
        help="Execute a single agentic task and exit"
    )

    parser.add_argument(
        "--mode", "-m",
        choices=["direct", "supervised", "autonomous"],
        default="autonomous",
        help="Execution mode for --task (default: autonomous)"
    )

    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="Disable voice narration"
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Task timeout in seconds (default: 300)"
    )

    return parser.parse_args()


async def run_single_task(
    bootstrapper: SupervisorBootstrapper,
    goal: str,
    mode: str,
    timeout: float,
) -> int:
    """
    Run a single agentic task and return.

    This is used when --task is provided. The supervisor will:
    1. Initialize just the essential components (watchdog, router, runner)
    2. Execute the task
    3. Shutdown and exit
    """
    from core.agentic_task_runner import RunnerMode, get_agentic_runner

    # Wait for agentic runner to be ready
    runner = get_agentic_runner()
    if not runner:
        bootstrapper.logger.error("Agentic runner not initialized")
        return 1

    if not runner.is_ready:
        bootstrapper.logger.info("Waiting for agentic runner to be ready...")
        for _ in range(30):  # Wait up to 30 seconds
            await asyncio.sleep(1)
            if runner.is_ready:
                break
        if not runner.is_ready:
            bootstrapper.logger.error("Agentic runner failed to initialize")
            return 1

    bootstrapper.logger.info(f"Executing task: {goal}")
    bootstrapper.logger.info(f"Mode: {mode}")

    try:
        result = await asyncio.wait_for(
            runner.run(
                goal=goal,
                mode=RunnerMode(mode),
                narrate=bootstrapper.config.voice_enabled,
            ),
            timeout=timeout,
        )

        print("\n" + "=" * 60)
        print("TASK RESULT")
        print("=" * 60)
        print(f"Success: {result.success}")
        print(f"Message: {result.final_message}")
        print(f"Time: {result.execution_time_ms:.0f}ms")
        print(f"Actions: {result.actions_count}")

        if result.learning_insights:
            print("\nInsights:")
            for insight in result.learning_insights:
                print(f"  - {insight}")

        if result.error:
            print(f"\nError: {result.error}")

        return 0 if result.success else 1

    except asyncio.TimeoutError:
        bootstrapper.logger.error(f"Task timed out after {timeout}s")
        return 1
    except Exception as e:
        bootstrapper.logger.error(f"Task execution failed: {e}")
        return 1


async def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Apply command-line settings to environment
    if args.no_voice:
        os.environ["JARVIS_VOICE_ENABLED"] = "false"

    bootstrapper = SupervisorBootstrapper()

    if args.task:
        # Single task mode: Initialize, run task, shutdown
        bootstrapper.logger.info("Running in single-task mode")

        # Initialize agentic security (minimal startup)
        try:
            await bootstrapper._initialize_agentic_security()
        except Exception as e:
            bootstrapper.logger.error(f"Failed to initialize agentic security: {e}")
            return 1

        # Run the task
        exit_code = await run_single_task(
            bootstrapper=bootstrapper,
            goal=args.task,
            mode=args.mode,
            timeout=args.timeout,
        )

        # Cleanup
        try:
            if bootstrapper._agentic_runner:
                await bootstrapper._agentic_runner.shutdown()
            if bootstrapper._watchdog:
                from core.agentic_watchdog import stop_watchdog
                await stop_watchdog()
        except Exception as e:
            bootstrapper.logger.warning(f"Cleanup error: {e}")

        return exit_code
    else:
        # Normal supervisor mode
        return await bootstrapper.run()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
