"""
Enterprise-Grade Browser Stability System v1.0.0
=================================================

Provides proactive crash prevention and intelligent recovery for Chrome/Chromium
browser processes. Addresses the root cause of "window terminated unexpectedly 
(reason: 'crashed', code: '5')" errors - GPU process OOM and Metal API instability.

This module is designed to be imported cleanly WITHOUT pulling in the monolithic
unified_supervisor.py (65K+ lines). It provides:

1. PROACTIVE MONITORING: Detect memory pressure BEFORE crashes occur
2. INTELLIGENT RESTART: Graceful Chrome restart with stability flags
3. CIRCUIT BREAKER: Prevent cascade failures during browser instability
4. GPU PROCESS ISOLATION: macOS Metal API bypass for stability
5. RESOURCE LIMITS: Context/page limits to prevent OOM

Architecture:
    BrowserStabilityManager (Singleton)
    ├── MemoryPressureMonitor (proactive detection)
    ├── StabilizedChromeLauncher (crash-proof Chrome startup)  
    ├── BrowserCircuitBreaker (failure prevention)
    └── CrashRecoveryCoordinator (intelligent recovery)

Exit Code Reference:
    5:   GPU process crash or OOM (most common - this module's target)
    6:   Renderer process crash
    11:  Page unresponsive / SIGSEGV
    15:  Browser terminated by signal (SIGTERM)
    137: OOM killed by system (128 + SIGKILL)
    139: Segmentation fault (128 + SIGSEGV)
    -5:  SIGTRAP - debugger breakpoint/code signing issue (macOS)
    -11: SIGSEGV - segmentation fault

Usage:
    from backend.core.browser_stability import (
        BrowserStabilityManager,
        get_stability_manager,
        ensure_stable_chrome,
    )
    
    # Get singleton manager
    manager = get_stability_manager()
    
    # Check if safe to perform browser operation
    if await manager.is_safe_for_browser_operation():
        # Perform operation
        pass
    else:
        # Back off - system under pressure
        pass
    
    # Ensure Chrome is launched with stability flags
    port = await ensure_stable_chrome()

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BrowserStabilityConfig:
    """
    Configuration for browser stability management.
    
    All values can be overridden via environment variables for zero-hardcoding.
    """
    # Memory thresholds (in percentages and MB)
    system_memory_warning_pct: float = field(
        default_factory=lambda: float(os.getenv("BROWSER_MEMORY_WARNING_PCT", "80"))
    )
    system_memory_critical_pct: float = field(
        default_factory=lambda: float(os.getenv("BROWSER_MEMORY_CRITICAL_PCT", "90"))
    )
    chrome_memory_warning_mb: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_CHROME_WARNING_MB", "4096"))
    )
    chrome_memory_critical_mb: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_CHROME_CRITICAL_MB", "6144"))
    )
    chrome_preemptive_restart_mb: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_PREEMPTIVE_RESTART_MB", "3072"))
    )
    
    # Chrome stability settings
    cdp_port_range_start: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_CDP_PORT_START", "9222"))
    )
    cdp_port_range_end: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_CDP_PORT_END", "9232"))
    )
    
    # Circuit breaker settings
    circuit_failure_threshold: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_CIRCUIT_FAILURES", "5"))
    )
    circuit_recovery_timeout_sec: float = field(
        default_factory=lambda: float(os.getenv("BROWSER_CIRCUIT_RECOVERY_SEC", "30.0"))
    )
    
    # Crash recovery settings
    max_restarts_per_hour: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_MAX_RESTARTS_HOUR", "5"))
    )
    restart_cooldown_sec: float = field(
        default_factory=lambda: float(os.getenv("BROWSER_RESTART_COOLDOWN_SEC", "10.0"))
    )
    
    # Resource limits
    max_contexts: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_MAX_CONTEXTS", "5"))
    )
    max_pages_per_context: int = field(
        default_factory=lambda: int(os.getenv("BROWSER_MAX_PAGES_PER_CONTEXT", "10"))
    )


# =============================================================================
# CRASH CODE DEFINITIONS
# =============================================================================

class CrashSeverity(Enum):
    """Severity levels for browser crashes."""
    LOW = auto()       # Recoverable, transient
    MEDIUM = auto()    # May need restart
    HIGH = auto()      # Needs restart + investigation
    CRITICAL = auto()  # System-level issue


@dataclass
class CrashInfo:
    """Information about a browser crash."""
    code: int
    meaning: str
    severity: CrashSeverity
    recoverable: bool
    recommended_action: str


# Comprehensive crash code mapping
CRASH_CODE_INFO: Dict[int, CrashInfo] = {
    # Chromium internal exit codes
    5: CrashInfo(5, "GPU process crash or OOM", CrashSeverity.HIGH, True, 
                 "Restart Chrome with --disable-gpu and memory limits"),
    6: CrashInfo(6, "Renderer process crash", CrashSeverity.MEDIUM, True,
                 "Close problematic tabs, restart browser"),
    11: CrashInfo(11, "Segmentation fault (SIGSEGV)", CrashSeverity.HIGH, True,
                  "Restart Chrome with minimal flags"),
    15: CrashInfo(15, "SIGTERM - terminated by signal", CrashSeverity.LOW, True,
                  "Normal shutdown, no action needed"),
    137: CrashInfo(137, "OOM killed by system (SIGKILL)", CrashSeverity.CRITICAL, True,
                   "Free system memory, restart with lower limits"),
    139: CrashInfo(139, "SIGSEGV (128 + 11)", CrashSeverity.HIGH, True,
                   "Restart Chrome with minimal flags"),
    # macOS signal codes (negative)
    -1: CrashInfo(-1, "SIGHUP - terminal hangup", CrashSeverity.LOW, True, "Reconnect"),
    -2: CrashInfo(-2, "SIGINT - keyboard interrupt", CrashSeverity.LOW, True, "User cancel"),
    -5: CrashInfo(-5, "SIGTRAP - code signing issue", CrashSeverity.HIGH, False,
                  "Check code signing, may need to re-download Chrome"),
    -6: CrashInfo(-6, "SIGABRT - abort signal", CrashSeverity.HIGH, True,
                  "Check for assertion failures"),
    -9: CrashInfo(-9, "SIGKILL - killed by system", CrashSeverity.CRITICAL, True,
                  "System OOM kill, free memory"),
    -10: CrashInfo(-10, "SIGBUS - bus error", CrashSeverity.HIGH, False,
                   "Memory access error, check hardware"),
    -11: CrashInfo(-11, "SIGSEGV - segmentation fault", CrashSeverity.HIGH, True,
                   "Restart Chrome with minimal flags"),
    -15: CrashInfo(-15, "SIGTERM - terminated by signal", CrashSeverity.LOW, True,
                   "Normal shutdown"),
}


def get_crash_info(code: int) -> CrashInfo:
    """Get crash information for an exit code."""
    return CRASH_CODE_INFO.get(code, CrashInfo(
        code, f"Unknown exit code {code}", CrashSeverity.MEDIUM, True,
        "Restart browser and monitor"
    ))


# =============================================================================
# CHROME STABILITY FLAGS - OFFICIALLY SUPPORTED ONLY
# =============================================================================
# 
# ROOT CAUSE ANALYSIS for Chrome GPU crashes (code 5):
# 1. GPU process runs out of memory due to Metal API overhead on macOS
# 2. CompositorTileWorker threads crash under memory pressure
# 3. V8 JavaScript heap exhaustion
#
# CURE (not band-aid): Use ONLY officially supported Chrome flags that address
# these root causes through proper resource management, not by disabling
# security features or using deprecated/unsupported flags.
#
# References:
# - https://peter.sh/experiments/chromium-command-line-switches/
# - Chromium source: chrome/common/chrome_switches.cc
# =============================================================================

# Flags that are OFFICIALLY SUPPORTED across Chrome versions
_SUPPORTED_UNIVERSAL_FLAGS = [
    # Resource management (addresses root cause: memory pressure)
    "--disable-dev-shm-usage",              # Use /tmp instead of /dev/shm (prevents SIGBUS)
    "--disable-extensions",                  # Extensions consume memory
    "--disable-background-networking",       # Reduce background memory usage
    "--disable-sync",                        # Disable sync to reduce memory
    "--disable-default-apps",                # Don't load default apps
    
    # Startup optimization
    "--no-first-run",                        # Skip first run tasks
    "--no-default-browser-check",            # Skip browser check
    
    # Audio/video (reduces GPU memory pressure)
    "--mute-audio",                          # Mute to save resources
    "--autoplay-policy=no-user-gesture-required",  # Prevent autoplay blocking
    
    # Telemetry
    "--metrics-recording-only",              # Don't send metrics
    "--disable-breakpad",                    # Disable crash reporter
    
    # Memory limits for V8 (addresses root cause: heap exhaustion)
    "--js-flags=--max-old-space-size=2048",  # Limit V8 heap to 2GB
    "--js-flags=--expose-gc",                # Allow manual GC for memory management
]

# macOS-specific SUPPORTED flags (addresses Metal API issues properly)
_SUPPORTED_MACOS_FLAGS = [
    # GPU control (proper way to address Metal crashes)
    "--disable-gpu",                         # Disable hardware GPU acceleration entirely
    "--disable-gpu-compositing",             # Disable GPU-based compositing
    "--disable-accelerated-2d-canvas",       # Disable GPU canvas (major memory user)
    "--disable-accelerated-video-decode",    # Disable GPU video decode
    
    # Feature flags (supported syntax for disabling features)
    "--disable-features=VizDisplayCompositor,Vulkan,SkiaRenderer",
    
    # Renderer settings
    "--force-color-profile=srgb",            # Consistent color handling
    "--disable-partial-raster",              # Disable partial GPU raster
    "--disable-skia-runtime-opts",           # Disable Skia optimizations that may crash
]

# Linux-specific SUPPORTED flags
_SUPPORTED_LINUX_FLAGS = [
    "--disable-gpu",                         # Disable GPU (safe on servers/containers)
    "--no-sandbox",                          # Required when running as root (Docker)
    "--disable-setuid-sandbox",              # For containers
]

# Windows-specific SUPPORTED flags  
_SUPPORTED_WINDOWS_FLAGS = [
    "--disable-gpu",                         # Disable GPU for stability
    "--disable-gpu-compositing",
]


def get_chrome_stability_flags() -> List[str]:
    """
    Get platform-specific Chrome stability flags - ONLY officially supported flags.
    
    This function returns flags that:
    1. Are documented in Chromium source code
    2. Don't produce "unsupported flag" warnings
    3. Address root causes of GPU crashes (memory pressure, not security bypasses)
    
    Root cause cure for code 5 (GPU crash):
    - Disable GPU entirely (--disable-gpu) rather than trying to sandbox hack
    - Limit V8 memory to prevent heap exhaustion
    - Disable features that cause Metal API crashes on macOS
    """
    flags = list(_SUPPORTED_UNIVERSAL_FLAGS)  # Copy to avoid mutation
    
    if sys.platform == "darwin":
        flags.extend(_SUPPORTED_MACOS_FLAGS)
        logger.debug(
            f"[BrowserStability] Using {len(flags)} macOS-specific stability flags "
            "(GPU disabled, Metal features disabled)"
        )
    elif sys.platform == "linux":
        flags.extend(_SUPPORTED_LINUX_FLAGS)
        logger.debug(
            f"[BrowserStability] Using {len(flags)} Linux-specific stability flags"
        )
    elif sys.platform == "win32":
        flags.extend(_SUPPORTED_WINDOWS_FLAGS)
        logger.debug(
            f"[BrowserStability] Using {len(flags)} Windows-specific stability flags"
        )
    
    return flags


def get_minimal_stability_flags() -> List[str]:
    """
    Get minimal set of stability flags for maximum compatibility.
    
    Use this when the full flag set causes issues. This is the absolute
    minimum needed to prevent GPU crashes.
    """
    minimal = [
        "--disable-gpu",                     # The key fix for code 5 crashes
        "--disable-dev-shm-usage",           # Prevents SIGBUS on shared memory
        "--no-first-run",
        "--disable-extensions",
        "--js-flags=--max-old-space-size=2048",
    ]
    
    # Add platform-specific minimal flags
    if sys.platform == "linux":
        # Only add no-sandbox if running as root
        if os.geteuid() == 0:
            minimal.append("--no-sandbox")
    
    return minimal


def validate_chrome_flags(flags: List[str]) -> Tuple[List[str], List[str]]:
    """
    Validate Chrome flags against known supported flags.
    
    Returns:
        Tuple of (supported_flags, unsupported_flags)
    """
    # Known unsupported/deprecated flags to avoid
    unsupported_patterns = [
        "--disable-gpu-sandbox",      # Unsupported, causes warning
        "--disable-metal",            # Not a real flag
        "--use-gl=swiftshader",       # SwiftShader may not be installed
        "--in-process-gpu",           # Deprecated
        "--disable-web-security",     # Security risk, not stability
        "--no-zygote",                # Linux-specific, may cause issues
        "--enable-features=",         # Syntax issues common
    ]
    
    supported = []
    unsupported = []
    
    for flag in flags:
        is_unsupported = any(pattern in flag for pattern in unsupported_patterns)
        if is_unsupported:
            unsupported.append(flag)
            logger.warning(f"[BrowserStability] Removing unsupported flag: {flag}")
        else:
            supported.append(flag)
    
    return supported, unsupported


def get_chrome_binary_path() -> Optional[str]:
    """Find the Chrome/Chromium binary on this system."""
    platform_paths = {
        "darwin": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Arc.app/Contents/MacOS/Arc",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ],
        "linux": [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ],
        "win32": [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        ],
    }
    
    paths = platform_paths.get(sys.platform, [])
    for path in paths:
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return expanded
    
    return None


# =============================================================================
# MEMORY PRESSURE MONITOR
# =============================================================================

class MemoryPressureLevel(Enum):
    """Memory pressure levels."""
    NORMAL = auto()
    WARNING = auto()
    CRITICAL = auto()


@dataclass
class MemoryStatus:
    """Current memory status."""
    level: MemoryPressureLevel
    system_percent: float
    system_available_mb: float
    chrome_total_mb: float
    chrome_process_count: int
    recommendation: str
    timestamp: datetime = field(default_factory=datetime.now)


class MemoryPressureMonitor:
    """
    Proactive memory pressure monitoring for browser operations.
    
    This is the CURE for crash code 5 - we detect memory pressure BEFORE
    the crash occurs and take preventive action.
    """
    
    def __init__(self, config: BrowserStabilityConfig):
        self._config = config
        self._lock = asyncio.Lock()
        self._last_check: Optional[MemoryStatus] = None
        self._check_interval = 5.0  # seconds
        
    async def check_pressure(self) -> MemoryStatus:
        """
        Check current memory pressure level.
        
        Returns detailed status including recommendation for action.
        """
        async with self._lock:
            try:
                import psutil
                
                # System memory
                mem = psutil.virtual_memory()
                system_percent = mem.percent
                system_available_mb = mem.available / (1024 * 1024)
                
                # Chrome-specific memory
                chrome_memory_mb = 0.0
                chrome_process_count = 0
                
                for proc in psutil.process_iter(['name', 'memory_info']):
                    try:
                        name = proc.info.get('name', '').lower()
                        if any(browser in name for browser in ['chrome', 'chromium', 'arc', 'brave']):
                            mem_info = proc.info.get('memory_info')
                            if mem_info:
                                chrome_memory_mb += mem_info.rss / (1024 * 1024)
                                chrome_process_count += 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                        continue
                
                # Determine pressure level
                if (system_percent >= self._config.system_memory_critical_pct or
                    chrome_memory_mb >= self._config.chrome_memory_critical_mb):
                    level = MemoryPressureLevel.CRITICAL
                    recommendation = (
                        f"CRITICAL: System at {system_percent:.1f}% memory, "
                        f"Chrome using {chrome_memory_mb:.0f}MB. "
                        "Abort browser operations immediately."
                    )
                elif (system_percent >= self._config.system_memory_warning_pct or
                      chrome_memory_mb >= self._config.chrome_memory_warning_mb):
                    level = MemoryPressureLevel.WARNING
                    recommendation = (
                        f"WARNING: System at {system_percent:.1f}% memory, "
                        f"Chrome using {chrome_memory_mb:.0f}MB. "
                        "Proceed with caution, consider closing tabs."
                    )
                else:
                    level = MemoryPressureLevel.NORMAL
                    recommendation = (
                        f"NORMAL: System at {system_percent:.1f}% memory, "
                        f"Chrome using {chrome_memory_mb:.0f}MB. "
                        "Safe to proceed."
                    )
                
                status = MemoryStatus(
                    level=level,
                    system_percent=system_percent,
                    system_available_mb=system_available_mb,
                    chrome_total_mb=chrome_memory_mb,
                    chrome_process_count=chrome_process_count,
                    recommendation=recommendation,
                )
                
                self._last_check = status
                return status
                
            except ImportError:
                # psutil not available
                return MemoryStatus(
                    level=MemoryPressureLevel.NORMAL,
                    system_percent=0.0,
                    system_available_mb=0.0,
                    chrome_total_mb=0.0,
                    chrome_process_count=0,
                    recommendation="Memory monitoring unavailable (psutil not installed)",
                )
            except Exception as e:
                logger.warning(f"[BrowserStability] Memory check failed: {e}")
                return MemoryStatus(
                    level=MemoryPressureLevel.NORMAL,
                    system_percent=0.0,
                    system_available_mb=0.0,
                    chrome_total_mb=0.0,
                    chrome_process_count=0,
                    recommendation=f"Memory check failed: {e}",
                )
    
    def should_preemptively_restart(self) -> bool:
        """Check if Chrome should be preemptively restarted to prevent crash."""
        if self._last_check is None:
            return False
        return self._last_check.chrome_total_mb >= self._config.chrome_preemptive_restart_mb


# =============================================================================
# BROWSER CIRCUIT BREAKER
# =============================================================================

class BrowserCircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


class BrowserCircuitBreaker:
    """
    Circuit breaker specifically for browser operations.
    
    Uses the atomic circuit breaker pattern but with browser-specific
    crash code awareness and recovery strategies.
    """
    
    def __init__(self, config: BrowserStabilityConfig):
        self._config = config
        self._state = BrowserCircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[datetime] = None
        self._crash_history: List[Tuple[datetime, int, str]] = []  # (time, code, reason)
        self._lock = asyncio.Lock()
        
    @property
    def state(self) -> BrowserCircuitBreakerState:
        return self._state
        
    async def can_execute(self) -> bool:
        """Check if browser operations are allowed."""
        async with self._lock:
            if self._state == BrowserCircuitBreakerState.CLOSED:
                return True
            
            if self._state == BrowserCircuitBreakerState.OPEN:
                # Check if recovery timeout elapsed
                if self._last_failure_time:
                    elapsed = (datetime.now() - self._last_failure_time).total_seconds()
                    if elapsed >= self._config.circuit_recovery_timeout_sec:
                        self._state = BrowserCircuitBreakerState.HALF_OPEN
                        self._success_count = 0
                        logger.info(
                            f"[BrowserCircuitBreaker] Transitioning to HALF_OPEN after {elapsed:.1f}s"
                        )
                        return True
                return False
            
            if self._state == BrowserCircuitBreakerState.HALF_OPEN:
                return True  # Allow test request
            
            return False
    
    async def record_success(self) -> None:
        """Record a successful browser operation."""
        async with self._lock:
            self._success_count += 1
            
            if self._state == BrowserCircuitBreakerState.HALF_OPEN:
                if self._success_count >= 2:  # Need 2 successes to close
                    self._state = BrowserCircuitBreakerState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    logger.info("[BrowserCircuitBreaker] Recovered to CLOSED state")
            
            elif self._state == BrowserCircuitBreakerState.CLOSED:
                # Success resets failure count
                self._failure_count = 0
    
    async def record_failure(self, reason: str, crash_code: Optional[str] = None) -> None:
        """Record a browser failure/crash."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = datetime.now()
            
            code = int(crash_code) if crash_code and crash_code.isdigit() else 0
            self._crash_history.append((datetime.now(), code, reason))
            
            # Keep history bounded
            if len(self._crash_history) > 100:
                self._crash_history = self._crash_history[-50:]
            
            crash_info = get_crash_info(code)
            
            if self._state == BrowserCircuitBreakerState.HALF_OPEN:
                # Any failure in HALF_OPEN immediately opens circuit
                self._state = BrowserCircuitBreakerState.OPEN
                self._success_count = 0
                logger.warning(
                    f"[BrowserCircuitBreaker] HALF_OPEN test failed: {reason} (code {code}: {crash_info.meaning})"
                )
            
            elif self._state == BrowserCircuitBreakerState.CLOSED:
                # Check if threshold reached
                if self._failure_count >= self._config.circuit_failure_threshold:
                    self._state = BrowserCircuitBreakerState.OPEN
                    logger.warning(
                        f"[BrowserCircuitBreaker] Circuit OPEN after {self._failure_count} failures. "
                        f"Last crash: {crash_info.meaning}"
                    )
                    
                    # Log recommended action
                    logger.info(f"[BrowserCircuitBreaker] Recommended action: {crash_info.recommended_action}")
    
    async def reset(self) -> None:
        """Manually reset the circuit breaker."""
        async with self._lock:
            self._state = BrowserCircuitBreakerState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            logger.info("[BrowserCircuitBreaker] Manually reset to CLOSED")
    
    def get_crash_statistics(self) -> Dict[str, Any]:
        """Get crash statistics for diagnostics."""
        # Count crashes by code in last hour
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_crashes = [c for c in self._crash_history if c[0] > one_hour_ago]
        
        crash_by_code: Dict[int, int] = {}
        for _, code, _ in recent_crashes:
            crash_by_code[code] = crash_by_code.get(code, 0) + 1
        
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time.isoformat() if self._last_failure_time else None,
            "crashes_last_hour": len(recent_crashes),
            "crashes_by_code": crash_by_code,
            "total_crash_history": len(self._crash_history),
        }


# =============================================================================
# STABILIZED CHROME LAUNCHER
# =============================================================================

class StabilizedChromeLauncher:
    """
    Launches Chrome with crash-prevention flags and manages the process lifecycle.
    
    This is the ROOT CAUSE FIX for code 5 crashes - we ensure Chrome starts
    with all necessary stability flags to prevent GPU process crashes.
    """
    
    def __init__(self, config: BrowserStabilityConfig):
        self._config = config
        self._chrome_process: Optional[asyncio.subprocess.Process] = None
        self._chrome_pid: Optional[int] = None
        self._cdp_port: Optional[int] = None
        self._lock = asyncio.Lock()
        self._flags = get_chrome_stability_flags()
        self._chrome_binary = get_chrome_binary_path()
        self._started_at: Optional[float] = None
        self._restart_count = 0
        self._restart_history: List[datetime] = []
        
        logger.info(
            f"[StabilizedChromeLauncher] Initialized with {len(self._flags)} stability flags, "
            f"binary: {self._chrome_binary or 'not found'}"
        )
    
    def _find_available_cdp_port(self) -> int:
        """Find an available port in the CDP port range."""
        import socket
        
        for port in range(self._config.cdp_port_range_start, self._config.cdp_port_range_end + 1):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    result = s.connect_ex(('localhost', port))
                    if result != 0:  # Port not in use
                        return port
            except Exception:
                continue
        
        # Default if all ports taken
        return self._config.cdp_port_range_start
    
    async def is_chrome_running(self) -> bool:
        """Check if our Chrome instance is running."""
        if self._chrome_process is None:
            return False
        return self._chrome_process.returncode is None

    async def _is_chrome_process_running(self) -> bool:
        """v3.4: Check if ANY Chrome process is running on the system.

        Used to detect the macOS single-instance handoff case: our launched
        process exits with code 0 after handing off to an existing Chrome
        instance. The existing instance IS running, we just don't own it.
        """
        try:
            import psutil
            for proc in psutil.process_iter(['name']):
                try:
                    name = (proc.info.get('name') or '').lower()
                    if 'google chrome' in name or name == 'chrome':
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return False
        except ImportError:
            # psutil not available — fall back to pgrep
            try:
                result = await asyncio.create_subprocess_exec(
                    'pgrep', '-x', 'Google Chrome',
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await result.wait()
                return result.returncode == 0
            except Exception:
                return False
        except Exception:
            return False

    def get_cdp_port(self) -> Optional[int]:
        """Get the active CDP port."""
        return self._cdp_port
    
    async def _kill_existing_chrome_on_port(self, port: int) -> bool:
        """Kill any existing Chrome process using the CDP port."""
        try:
            import psutil
            
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    if cmdline and f'--remote-debugging-port={port}' in ' '.join(cmdline):
                        logger.info(f"[StabilizedChromeLauncher] Killing existing Chrome on port {port} (PID {proc.pid})")
                        proc.terminate()
                        await asyncio.sleep(1.0)
                        if proc.is_running():
                            proc.kill()
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return False
        except ImportError:
            return False
        except Exception as e:
            logger.warning(f"[StabilizedChromeLauncher] Failed to kill existing Chrome: {e}")
            return False
    
    async def launch_stabilized_chrome(
        self,
        url: Optional[str] = None,
        incognito: bool = False,
        kill_existing: bool = True,
        headless: bool = False,
    ) -> bool:
        """
        Launch Chrome with stability flags to prevent code 5 crashes.
        
        This is the CURE - we ensure Chrome starts with GPU disabled,
        memory limited, and Metal bypassed (on macOS).
        """
        async with self._lock:
            if self._chrome_binary is None:
                logger.error("[StabilizedChromeLauncher] Chrome binary not found")
                return False
            
            # Check restart limits
            one_hour_ago = datetime.now() - timedelta(hours=1)
            recent_restarts = [r for r in self._restart_history if r > one_hour_ago]
            if len(recent_restarts) >= self._config.max_restarts_per_hour:
                logger.error(
                    f"[StabilizedChromeLauncher] Max restarts ({self._config.max_restarts_per_hour}) "
                    "reached in past hour. Backing off."
                )
                return False
            
            # Find available CDP port
            self._cdp_port = self._find_available_cdp_port()
            
            # Kill existing Chrome on the port if requested
            if kill_existing:
                await self._kill_existing_chrome_on_port(self._cdp_port)
            
            # Build command
            cmd = [self._chrome_binary]
            cmd.extend(self._flags)
            cmd.append(f"--remote-debugging-port={self._cdp_port}")
            
            if headless:
                cmd.append("--headless=new")
            
            if incognito:
                cmd.append("--incognito")
            
            if url:
                cmd.append(url)
            
            logger.info(
                f"[StabilizedChromeLauncher] Launching Chrome with {len(self._flags)} stability flags, "
                f"CDP port {self._cdp_port}, headless={headless}"
            )
            
            try:
                # Launch Chrome process
                self._chrome_process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,  # Don't kill on parent exit
                )
                
                self._chrome_pid = self._chrome_process.pid
                self._started_at = time.time()
                self._restart_count += 1
                self._restart_history.append(datetime.now())
                
                # Wait a bit for Chrome to start
                await asyncio.sleep(2.0)

                # Check if still running
                if self._chrome_process.returncode is not None:
                    exit_code = self._chrome_process.returncode

                    # v3.4: Exit code 0 is NEVER a crash. On macOS, Chrome uses a
                    # single-instance model: when Chrome is already running, a new
                    # Chrome process sends its arguments (URL, flags) to the existing
                    # instance via Mach IPC and exits with code 0 (successful handoff).
                    # Treating this as a crash caused the caller to fall back to
                    # AppleScript, opening a SECOND window (the "double window" bug).
                    if exit_code == 0:
                        chrome_running = await self._is_chrome_process_running()
                        if chrome_running:
                            logger.info(
                                "[StabilizedChromeLauncher] Chrome process exited with code 0 "
                                "(handed off to existing instance). Chrome is running."
                            )
                            # Update our tracking — we don't own the process anymore,
                            # but Chrome IS running and our URL/flags were delivered.
                            self._chrome_process = None
                            self._chrome_pid = None
                            return True
                        else:
                            logger.warning(
                                "[StabilizedChromeLauncher] Chrome exited with code 0 "
                                "but no Chrome process found — genuine launch failure"
                            )
                            return False

                    crash_info = get_crash_info(exit_code)
                    logger.error(
                        f"[StabilizedChromeLauncher] Chrome crashed immediately with code {exit_code}: "
                        f"{crash_info.meaning}"
                    )
                    return False
                
                logger.info(
                    f"[StabilizedChromeLauncher] ✅ Chrome launched successfully "
                    f"(PID={self._chrome_pid}, CDP port={self._cdp_port})"
                )
                return True
                
            except Exception as e:
                logger.error(f"[StabilizedChromeLauncher] Failed to launch Chrome: {e}")
                return False
    
    async def restart_chrome(
        self,
        url: Optional[str] = None,
        incognito: bool = False,
    ) -> bool:
        """
        Restart Chrome with stability flags.
        
        Called automatically after crashes or manually for recovery.
        """
        # Cleanup existing process
        await self.shutdown()
        
        # Wait before restart
        await asyncio.sleep(self._config.restart_cooldown_sec)
        
        return await self.launch_stabilized_chrome(
            url=url,
            incognito=incognito,
            kill_existing=True,
        )
    
    async def preemptive_restart_if_needed(self, memory_threshold_mb: float) -> bool:
        """
        Preemptively restart Chrome if memory exceeds threshold.
        
        This is PROACTIVE crash prevention - we restart Chrome BEFORE
        it crashes rather than waiting for code 5.
        """
        try:
            import psutil
            
            # Calculate Chrome memory usage
            chrome_memory_mb = 0.0
            for proc in psutil.process_iter(['name', 'memory_info']):
                try:
                    name = proc.info.get('name', '').lower()
                    if 'chrome' in name or 'chromium' in name:
                        mem_info = proc.info.get('memory_info')
                        if mem_info:
                            chrome_memory_mb += mem_info.rss / (1024 * 1024)
                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                    continue
            
            if chrome_memory_mb > memory_threshold_mb:
                logger.warning(
                    f"[StabilizedChromeLauncher] Chrome using {chrome_memory_mb:.0f}MB "
                    f"(threshold: {memory_threshold_mb:.0f}MB). Preemptively restarting..."
                )
                return await self.restart_chrome()
            
            return True  # No restart needed
            
        except ImportError:
            return True  # psutil not available
        except Exception as e:
            logger.debug(f"[StabilizedChromeLauncher] Memory check failed: {e}")
            return True
    
    async def shutdown(self) -> None:
        """Gracefully shutdown Chrome."""
        if self._chrome_process is None:
            return
        
        try:
            if self._chrome_process.returncode is None:
                self._chrome_process.terminate()
                try:
                    await asyncio.wait_for(self._chrome_process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._chrome_process.kill()
                    await self._chrome_process.wait()
        except Exception as e:
            logger.debug(f"[StabilizedChromeLauncher] Shutdown error: {e}")
        
        self._chrome_process = None
        self._chrome_pid = None
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get launcher statistics."""
        uptime = time.time() - self._started_at if self._started_at else 0
        return {
            "running": self._chrome_process is not None and self._chrome_process.returncode is None,
            "pid": self._chrome_pid,
            "cdp_port": self._cdp_port,
            "uptime_seconds": uptime,
            "restart_count": self._restart_count,
            "recent_restarts": len([r for r in self._restart_history if r > datetime.now() - timedelta(hours=1)]),
            "flags_count": len(self._flags),
            "platform": sys.platform,
        }


# =============================================================================
# BROWSER STABILITY MANAGER (SINGLETON)
# =============================================================================

class BrowserStabilityManager:
    """
    Central manager for browser stability operations.
    
    Coordinates memory monitoring, circuit breaker, and Chrome launcher
    to provide comprehensive crash prevention and recovery.
    
    This is the main entry point for all browser stability operations.
    """
    
    _instance: Optional["BrowserStabilityManager"] = None
    _lock = threading.Lock()
    
    def __new__(cls, config: Optional[BrowserStabilityConfig] = None) -> "BrowserStabilityManager":
        """Singleton pattern with thread-safe initialization."""
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instance = instance
            return cls._instance
    
    def __init__(self, config: Optional[BrowserStabilityConfig] = None):
        """Initialize the stability manager."""
        if self._initialized:
            return
        
        self._config = config or BrowserStabilityConfig()
        self._memory_monitor = MemoryPressureMonitor(self._config)
        self._circuit_breaker = BrowserCircuitBreaker(self._config)
        self._chrome_launcher = StabilizedChromeLauncher(self._config)
        self._initialized = True
        
        logger.info("[BrowserStabilityManager] Initialized - proactive crash prevention active")
    
    @property
    def circuit_breaker(self) -> BrowserCircuitBreaker:
        return self._circuit_breaker
    
    @property
    def chrome_launcher(self) -> StabilizedChromeLauncher:
        return self._chrome_launcher
    
    @property
    def memory_monitor(self) -> MemoryPressureMonitor:
        return self._memory_monitor
    
    async def is_safe_for_browser_operation(self) -> bool:
        """
        Check if it's safe to perform a browser operation.
        
        This is the PROACTIVE check that should be called BEFORE any
        browser operation to prevent crashes.
        """
        # Check circuit breaker
        if not await self._circuit_breaker.can_execute():
            logger.warning("[BrowserStabilityManager] Circuit breaker OPEN - browser operations blocked")
            return False
        
        # Check memory pressure
        status = await self._memory_monitor.check_pressure()
        
        if status.level == MemoryPressureLevel.CRITICAL:
            logger.warning(f"[BrowserStabilityManager] {status.recommendation}")
            return False
        
        if status.level == MemoryPressureLevel.WARNING:
            logger.info(f"[BrowserStabilityManager] {status.recommendation}")
            # Allow but log warning
        
        return True
    
    async def ensure_stable_chrome(self, headless: bool = False) -> Optional[int]:
        """
        Ensure Chrome is running with stability flags.
        
        Returns the CDP port if successful, None if failed.
        """
        # Check if already running
        if await self._chrome_launcher.is_chrome_running():
            # Check if preemptive restart needed
            await self._chrome_launcher.preemptive_restart_if_needed(
                self._config.chrome_preemptive_restart_mb
            )
            return self._chrome_launcher.get_cdp_port()
        
        # Launch new Chrome
        success = await self._chrome_launcher.launch_stabilized_chrome(
            headless=headless
        )
        
        if success:
            return self._chrome_launcher.get_cdp_port()
        return None
    
    async def record_crash(
        self,
        crash_reason: str,
        crash_code: str,
        source: str = "unknown",
        error_message: str = "",
    ) -> None:
        """
        Record a browser crash and update circuit breaker.
        
        This should be called whenever a browser crash is detected.
        """
        await self._circuit_breaker.record_failure(crash_reason, crash_code)
        
        crash_info = get_crash_info(int(crash_code) if crash_code.isdigit() else 0)
        
        logger.error(
            f"[BrowserStabilityManager] Browser crash recorded: "
            f"source={source}, code={crash_code} ({crash_info.meaning}), "
            f"reason={crash_reason}"
        )
        
        # Attempt recovery for recoverable crashes
        if crash_info.recoverable and crash_info.severity in (CrashSeverity.HIGH, CrashSeverity.CRITICAL):
            logger.info(f"[BrowserStabilityManager] Attempting recovery: {crash_info.recommended_action}")
            await self._chrome_launcher.restart_chrome()
    
    async def record_success(self) -> None:
        """Record a successful browser operation."""
        await self._circuit_breaker.record_success()
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status."""
        return {
            "memory": self._memory_monitor._last_check.__dict__ if self._memory_monitor._last_check else None,
            "circuit_breaker": self._circuit_breaker.get_crash_statistics(),
            "chrome_launcher": self._chrome_launcher.get_statistics(),
        }
    
    async def shutdown(self) -> None:
        """Shutdown the stability manager and Chrome."""
        await self._chrome_launcher.shutdown()
        logger.info("[BrowserStabilityManager] Shutdown complete")


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_stability_manager: Optional[BrowserStabilityManager] = None


def get_stability_manager(config: Optional[BrowserStabilityConfig] = None) -> BrowserStabilityManager:
    """Get the singleton BrowserStabilityManager instance."""
    global _stability_manager
    if _stability_manager is None:
        _stability_manager = BrowserStabilityManager(config)
    return _stability_manager


async def ensure_stable_chrome(headless: bool = False) -> Optional[int]:
    """
    Convenience function to ensure Chrome is running with stability flags.
    
    Returns the CDP port if successful, None if failed.
    """
    manager = get_stability_manager()
    return await manager.ensure_stable_chrome(headless=headless)


async def is_safe_for_browser_operation() -> bool:
    """
    Convenience function to check if browser operations are safe.
    
    Call this BEFORE any browser operation to prevent crashes.
    """
    manager = get_stability_manager()
    return await manager.is_safe_for_browser_operation()


def get_active_cdp_port() -> Optional[int]:
    """Get the active CDP port from the stability manager."""
    manager = get_stability_manager()
    return manager.chrome_launcher.get_cdp_port()


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

logger.info(
    f"[BrowserStability] Module loaded - platform: {sys.platform}, "
    f"stability flags available: {len(get_chrome_stability_flags())}"
)
