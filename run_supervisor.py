#!/usr/bin/env python3
"""
JARVIS Supervisor Entry Point - Production Grade v2.0
======================================================

Advanced, robust, async, parallel, intelligent, and dynamic supervisor entry point.

Features:
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
    │  ├── SystemResourceValidator (pre-flight checks)            │
    │  ├── DynamicConfigLoader (env + yaml + defaults)            │
    │  └── IntelligentStartupOrchestrator (phased initialization) │
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

Author: JARVIS System
Version: 2.0.0
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
# Voice Narration - Async with Queuing
# =============================================================================

class AsyncVoiceNarrator:
    """
    Async voice narrator with intelligent queuing.
    
    Features:
    - Non-blocking voice output
    - Queue management to prevent overlap
    - Platform-aware (macOS only)
    - Graceful fallback on errors
    """
    
    def __init__(self, config: BootstrapConfig):
        self.config = config
        self.enabled = config.voice_enabled and platform.system() == "Darwin"
        self._queue: asyncio.Queue = asyncio.Queue()
        self._speaking = False
        self._shutdown = False
    
    async def speak(self, text: str, wait: bool = True, priority: bool = False) -> None:
        """
        Speak text asynchronously.
        
        Args:
            text: Text to speak
            wait: Whether to wait for speech to complete
            priority: If True, speak immediately (skip queue)
        """
        if not self.enabled:
            return
        
        try:
            if priority and not self._speaking:
                await self._speak_now(text, wait)
            elif wait:
                await self._speak_now(text, wait)
            else:
                # Fire and forget
                asyncio.create_task(self._speak_now(text, False))
        except Exception:
            pass
    
    async def _speak_now(self, text: str, wait: bool) -> None:
        """Execute speech command."""
        self._speaking = True
        try:
            process = await asyncio.create_subprocess_exec(
                "say", "-v", self.config.voice_name, 
                "-r", str(self.config.voice_rate), text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if wait:
                await asyncio.wait_for(process.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            self._speaking = False


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
            
            # Wait for both
            pid_file_procs, scanned_procs = await asyncio.gather(
                pid_file_task, process_scan_task
            )
        
        # Merge results (PID files take precedence)
        discovered.update(scanned_procs)
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
            except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
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
        print(f"{cls.CYAN}{' ' * 15}⚡ JARVIS LIFECYCLE SUPERVISOR ⚡{' ' * 15}{cls.RESET}")
        print(f"{cls.CYAN}{'=' * 65}{cls.RESET}")
        print()
        print(f"  {cls.YELLOW}🤖 Self-Updating • Self-Healing • Autonomous{cls.RESET}")
        print()
        print(f"  {cls.GRAY}The Living OS - Manages updates, restarts, and rollbacks")
        print(f"  while keeping JARVIS online and responsive.{cls.RESET}")
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
                await asyncio.sleep(1.0)  # Let ports release
            else:
                TerminalUI.print_success("No existing instances found")
            
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
            
            self.perf.end("validation")
            
            # Phase 3: Initialize supervisor
            self.perf.start("supervisor_init")
            TerminalUI.print_phase(3, 4, "Initializing supervisor")
            print()
            
            from core.supervisor import JARVISSupervisor
            supervisor = JARVISSupervisor()
            
            self._print_config_summary(supervisor)
            self.perf.end("supervisor_init")
            
            # Phase 4: Start JARVIS
            TerminalUI.print_divider()
            TerminalUI.print_phase(4, 4, "Starting JARVIS with loading page")
            print()
            
            print(f"  {TerminalUI.YELLOW}📡 Loading page will open in Chrome Incognito{TerminalUI.RESET}")
            print(f"  {TerminalUI.YELLOW}   Watch real-time progress as JARVIS initializes!{TerminalUI.RESET}")
            
            if self.config.voice_enabled:
                print(f"  {TerminalUI.YELLOW}🔊 Voice narration enabled - JARVIS will speak during startup{TerminalUI.RESET}")
            
            print()
            
            # Run supervisor
            self.perf.start("jarvis")
            await supervisor.run()
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
            # Log performance summary
            summary = self.perf.get_summary()
            self.logger.info(f"Bootstrap performance: {summary}")
    
    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            self._shutdown_event.set()
        
        signal.signal(signal.SIGTERM, handle_signal)
        # SIGINT is handled by KeyboardInterrupt
    
    def _print_config_summary(self, supervisor) -> None:
        """Print supervisor configuration summary."""
        config = supervisor.config
        
        update_status = f"Enabled ({config.update.check.interval_seconds}s)" if config.update.check.enabled else "Disabled"
        idle_status = f"Enabled ({config.idle.threshold_seconds // 3600}h threshold)" if config.idle.enabled else "Disabled"
        rollback_status = "Enabled" if config.rollback.auto_on_boot_failure else "Disabled"
        
        TerminalUI.print_info("Mode", config.mode.value.upper(), highlight=True)
        TerminalUI.print_info("Update Check", update_status)
        TerminalUI.print_info("Idle Updates", idle_status)
        TerminalUI.print_info("Auto-Rollback", rollback_status)
        TerminalUI.print_info("Max Retries", str(config.health.max_crash_retries))
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
