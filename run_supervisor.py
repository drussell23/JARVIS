#!/usr/bin/env python3
"""
JARVIS Supervisor Entry Point - Production Grade v3.0 (Zero-Touch Edition)
===========================================================================

Advanced, robust, async, parallel, intelligent, and dynamic supervisor entry point.

v3.0 Zero-Touch Features:
- Autonomous self-updating without human intervention
- Dead Man's Switch for post-update stability verification
- Prime Directives (immutable safety constraints)
- Update classification (security/critical/minor/major)
- Staging area with dry-run validation
- AGI OS integration for intelligent decision making

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
    │  └── IntelligentStartupOrchestrator (phased initialization) │
    └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────────────┐
    │  JARVISSupervisor (core/supervisor/jarvis_supervisor.py)     │
    │  ├── ZeroTouchEngine (autonomous updates)                   │
    │  ├── DeadManSwitch (stability verification)                 │
    │  ├── UpdateEngine (staging, validation, classification)     │
    │  ├── RollbackManager (version history, snapshots)           │
    │  └── SupervisorNarrator v3.0 (intelligent voice feedback)   │
    └─────────────────────────────────────────────────────────────┘

Usage:
    # Run supervisor (recommended way to start JARVIS)
    python run_supervisor.py

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

    # Require idle system before auto-updates
    JARVIS_ZERO_TOUCH_REQUIRE_IDLE=true python run_supervisor.py

Author: JARVIS System
Version: 3.0.0
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
                from backend.core.supervisor.unified_voice_orchestrator import (
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
    """
    
    def __init__(self):
        self.config = BootstrapConfig()
        self.logger = setup_logging(self.config)
        self.narrator = AsyncVoiceNarrator(self.config)
        self.perf = PerformanceLogger()
        self.phase = StartupPhase.INIT
        self._shutdown_event = asyncio.Event()
        self._loading_server_process: Optional[asyncio.subprocess.Process] = None  # Track for cleanup
        
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

            self.perf.end("validation")

            # Phase 3: Initialize supervisor
            self.perf.start("supervisor_init")
            TerminalUI.print_phase(3, 4, "Initializing supervisor")
            print()
            
            from core.supervisor import JARVISSupervisor
            supervisor = JARVISSupervisor()
            
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
            
            try:
                await supervisor.run()
            finally:
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
            
            # Step 3: Open Chrome Incognito window
            if platform.system() == "Darwin":  # macOS
                try:
                    # Use macOS 'open' command to launch Chrome in Incognito mode
                    # This is non-blocking and matches what start_system.py does
                    open_cmd = [
                        "open",
                        "-na",
                        "Google Chrome",
                        "--args",
                        "--incognito",
                        "--new-window",
                        loading_url
                    ]
                    
                    await asyncio.create_subprocess_exec(
                        *open_cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    
                    print(f"  {TerminalUI.GREEN}✓ Chrome Incognito opened to loading page{TerminalUI.RESET}")
                    
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
        
        self.logger.info("🔍 Monitoring JARVIS startup (v4.1 - operational check)...")
        
        while (time.time() - start_time) < max_wait:
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
                                is_ready = (
                                    data.get("ready") == True or
                                    data.get("operational") == True or
                                    status in ["ready", "operational", "degraded"]
                                )
                                
                                # Also accept if WebSocket is ready (core functionality)
                                details = data.get("details", {})
                                websocket_ready = details.get("websocket_ready", False)
                                
                                # v4.2: Accept "warming_up" status after 30s if WebSocket is ready
                                # ML models can continue warming in background - user can interact
                                if status == "warming_up" and elapsed > 30 and websocket_ready:
                                    self.logger.info(f"✅ Backend warming up but functional after {elapsed:.0f}s")
                                    is_ready = True
                                
                                # v4.2: Accept any responding status after 45s with WebSocket
                                if websocket_ready and elapsed > 45:
                                    self.logger.info(f"✅ Backend WebSocket ready after {elapsed:.0f}s")
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
        self.logger.warning(f"⚠️ JARVIS startup monitoring timeout after {max_wait}s")
        self.logger.warning(f"   Status: http={backend_http_ready}, operational={backend_operational}, frontend={frontend_ready}")
        
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
        """Setup signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            self._shutdown_event.set()

        signal.signal(signal.SIGTERM, handle_signal)
        # SIGINT is handled by KeyboardInterrupt
    
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
            try:
                from backend.agi_os import get_agi_os
                self.logger.info("🧠 AGI OS module available - will integrate with supervisor")
            except ImportError:
                self.logger.debug("AGI OS module not available - supervisor will operate independently")
    
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

async def main() -> int:
    """Main entry point."""
    bootstrapper = SupervisorBootstrapper()
    return await bootstrapper.run()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
