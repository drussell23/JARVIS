"""
Memory Fault Guard - SIGBUS/SIGSEGV Defense System
===================================================

Advanced signal handler and memory protection system to prevent and recover from
catastrophic memory faults (SIGBUS/SIGSEGV) that occur when Python's allocator
hits reserved VM regions during heavy ML model loading.

Features:
- SIGBUS/SIGSEGV signal handlers for graceful degradation
- Pre-emptive VM region validation before large allocations
- Emergency memory reserve for recovery operations
- Cross-repo coordination for cloud offload triggering
- Recovery protocol with cleanup and continuation

The SIGBUS at address 0x00000008ae400000 (MALLOC_SMALL/commpage boundary) is the
final symptom of memory exhaustion. This module adds the missing layer: catching
actual memory faults and triggering coordinated recovery.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │  MemoryFaultGuard (Singleton)                               │
    │  ├── Signal Handlers (SIGBUS=10, SIGSEGV=11)                │
    │  ├── Emergency Reserve (~50MB pre-allocated buffer)         │
    │  ├── Fault Callbacks (notify ProactiveResourceGuard, etc)   │
    │  ├── VM Region Checker (pre-allocation validation)          │
    │  └── Recovery Protocol (graceful degradation sequence)      │
    └─────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.memory_fault_guard import get_memory_fault_guard, install_early_protection

    # Install at earliest possible point (module load time)
    install_early_protection()

    # Later, full initialization with callbacks
    guard = get_memory_fault_guard()
    guard.register_fault_callback(my_callback)

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import ctypes
import faulthandler
import gc
import logging
import os
import platform
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import psutil

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class MemoryFaultConfig:
    """Configuration for memory fault protection."""
    
    # Emergency reserve size (released during recovery to allow cleanup)
    emergency_reserve_mb: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_EMERGENCY_RESERVE_MB", "50"))
    )
    
    # Minimum available memory before refusing new allocations
    min_available_mb: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIN_AVAILABLE_MB", "500"))
    )
    
    # VM region check threshold (percentage of total VM used)
    vm_region_threshold_percent: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VM_THRESHOLD_PERCENT", "85.0"))
    )
    
    # Enable faulthandler for stack traces on crashes
    enable_faulthandler: bool = field(
        default_factory=lambda: os.getenv("JARVIS_ENABLE_FAULTHANDLER", "true").lower() == "true"
    )
    
    # Cross-repo signal file path
    signal_file_path: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "memory_state" / "fault_signal.json"
    )
    
    # Maximum fault callbacks to prevent infinite loops
    max_callbacks: int = 10
    
    # Recovery timeout (seconds)
    recovery_timeout: float = 5.0
    
    @classmethod
    def from_environment(cls) -> "MemoryFaultConfig":
        """Load configuration from environment variables."""
        return cls()


class FaultType(Enum):
    """Type of memory fault detected."""
    SIGBUS = "sigbus"      # Bus error - memory access violation
    SIGSEGV = "sigsegv"    # Segmentation fault - invalid memory access
    OOM = "oom"            # Out of memory (pre-emptive detection)
    VM_EXHAUSTED = "vm_exhausted"  # VM region exhaustion


class FaultSeverity(Enum):
    """Severity of the memory fault."""
    WARNING = "warning"    # Approaching limits, take action
    CRITICAL = "critical"  # Limit reached, must degrade
    FATAL = "fatal"        # Unrecoverable, attempting graceful shutdown


@dataclass
class FaultEvent:
    """Record of a memory fault event."""
    fault_type: FaultType
    severity: FaultSeverity
    timestamp: datetime
    signal_number: Optional[int] = None
    address: Optional[str] = None
    available_mb: Optional[float] = None
    vm_usage_percent: Optional[float] = None
    recovery_attempted: bool = False
    recovery_successful: bool = False
    traceback: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "fault_type": self.fault_type.value,
            "severity": self.severity.value,
            "timestamp": self.timestamp.isoformat(),
            "signal_number": self.signal_number,
            "address": self.address,
            "available_mb": self.available_mb,
            "vm_usage_percent": self.vm_usage_percent,
            "recovery_attempted": self.recovery_attempted,
            "recovery_successful": self.recovery_successful,
            "traceback": self.traceback,
        }


# =============================================================================
# Memory Fault Guard Implementation
# =============================================================================

class MemoryFaultGuard:
    """
    Advanced memory fault defense system.
    
    Provides three layers of protection:
    1. Prevention: Pre-emptive VM region validation before large allocations
    2. Detection: SIGBUS/SIGSEGV signal handlers
    3. Recovery: Emergency reserve release and graceful degradation
    
    This is a singleton - use get_memory_fault_guard() to access.
    """
    
    _instance: Optional["MemoryFaultGuard"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "MemoryFaultGuard":
        """Ensure singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self) -> None:
        """Initialize the memory fault guard."""
        if getattr(self, "_initialized", False):
            return
        
        self.config = MemoryFaultConfig.from_environment()
        
        # Emergency reserve - allocated at startup, released during recovery
        self._emergency_reserve: Optional[bytearray] = None
        self._reserve_released = False
        
        # Fault callbacks - called on memory fault detection
        self._fault_callbacks: List[Callable[[FaultEvent], None]] = []
        self._callback_lock = threading.Lock()
        
        # Fault history for diagnostics
        self._fault_history: List[FaultEvent] = []
        self._max_history = 100
        
        # Original signal handlers (for restoration)
        self._original_sigbus_handler: Optional[Any] = None
        self._original_sigsegv_handler: Optional[Any] = None
        
        # State flags
        self._handlers_installed = False
        self._is_recovering = False
        self._shutdown_triggered = False
        
        # Platform detection
        self._is_macos = platform.system().lower() == "darwin"
        self._is_arm64 = platform.machine().lower() in ("arm64", "aarch64")
        
        self._initialized = True
        logger.info("🛡️ MemoryFaultGuard initialized")
    
    def initialize(self) -> bool:
        """
        Full initialization: allocate reserve, install handlers.
        
        Returns:
            True if initialization successful
        """
        try:
            # Allocate emergency reserve
            self._allocate_emergency_reserve()
            
            # Install signal handlers
            self._install_signal_handlers()
            
            # Enable faulthandler for debugging
            if self.config.enable_faulthandler:
                self._enable_faulthandler()
            
            # Ensure signal file directory exists
            self.config.signal_file_path.parent.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"🛡️ MemoryFaultGuard fully initialized:")
            logger.info(f"   📦 Emergency reserve: {self.config.emergency_reserve_mb} MB")
            logger.info(f"   🚨 Min available: {self.config.min_available_mb} MB")
            logger.info(f"   📊 VM threshold: {self.config.vm_region_threshold_percent}%")
            logger.info(f"   🖥️ Platform: {'macOS ARM64' if self._is_macos and self._is_arm64 else platform.system()}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ MemoryFaultGuard initialization failed: {e}")
            return False
    
    def _allocate_emergency_reserve(self) -> None:
        """Allocate emergency memory reserve."""
        if self._emergency_reserve is not None:
            return
        
        try:
            reserve_bytes = self.config.emergency_reserve_mb * 1024 * 1024
            self._emergency_reserve = bytearray(reserve_bytes)
            # Touch the memory to ensure it's actually allocated
            for i in range(0, len(self._emergency_reserve), 4096):
                self._emergency_reserve[i] = 0
            logger.debug(f"   Allocated {self.config.emergency_reserve_mb}MB emergency reserve")
        except MemoryError as e:
            logger.warning(f"⚠️ Could not allocate full emergency reserve: {e}")
            # Try smaller reserve
            try:
                smaller_reserve = 10 * 1024 * 1024  # 10MB fallback
                self._emergency_reserve = bytearray(smaller_reserve)
                logger.warning(f"   Allocated reduced 10MB emergency reserve")
            except MemoryError:
                logger.error("   Failed to allocate any emergency reserve")
    
    def _install_signal_handlers(self) -> None:
        """Install SIGBUS and SIGSEGV handlers."""
        if self._handlers_installed:
            return
        
        try:
            # Store original handlers for restoration
            self._original_sigbus_handler = signal.getsignal(signal.SIGBUS)
            self._original_sigsegv_handler = signal.getsignal(signal.SIGSEGV)
            
            # Install our handlers
            signal.signal(signal.SIGBUS, self._handle_sigbus)
            signal.signal(signal.SIGSEGV, self._handle_sigsegv)
            
            self._handlers_installed = True
            logger.debug("   Installed SIGBUS/SIGSEGV handlers")
            
        except (ValueError, OSError) as e:
            # Can't install handlers (e.g., not in main thread)
            logger.warning(f"⚠️ Cannot install signal handlers: {e}")
    
    def _enable_faulthandler(self) -> None:
        """Enable Python's faulthandler for debugging."""
        try:
            faulthandler.enable()
            logger.debug("   Enabled faulthandler for stack traces")
        except Exception as e:
            logger.debug(f"   Could not enable faulthandler: {e}")
    
    def _handle_sigbus(self, signum: int, frame: Any) -> None:
        """
        Handle SIGBUS (bus error) signal.
        
        This is called when memory access fails at VM region boundary.
        We attempt recovery before allowing the crash.
        """
        self._handle_memory_fault(
            FaultType.SIGBUS,
            signum,
            frame,
            "SIGBUS: Memory access at reserved VM region boundary"
        )
    
    def _handle_sigsegv(self, signum: int, frame: Any) -> None:
        """
        Handle SIGSEGV (segmentation fault) signal.
        
        This indicates invalid memory access, often due to memory exhaustion.
        """
        self._handle_memory_fault(
            FaultType.SIGSEGV,
            signum,
            frame,
            "SIGSEGV: Invalid memory access"
        )
    
    def _handle_memory_fault(
        self,
        fault_type: FaultType,
        signum: int,
        frame: Any,
        message: str
    ) -> None:
        """
        Central handler for memory faults.
        
        Attempts recovery sequence:
        1. Release emergency reserve to free memory
        2. Trigger garbage collection
        3. Notify callbacks for graceful degradation
        4. Write signal file for cross-repo coordination
        5. If recovery fails, allow original handler or exit
        """
        # Prevent recursive handling
        if self._is_recovering:
            sys.exit(128 + signum)  # Exit with signal code
        
        self._is_recovering = True
        
        # Create fault event
        event = FaultEvent(
            fault_type=fault_type,
            severity=FaultSeverity.FATAL,
            timestamp=datetime.now(),
            signal_number=signum,
            traceback=self._get_traceback(frame),
        )
        
        try:
            # Get current memory state
            mem = psutil.virtual_memory()
            event.available_mb = mem.available / (1024 * 1024)
            event.vm_usage_percent = mem.percent
        except Exception:
            pass
        
        logger.critical(f"🚨 {message}")
        logger.critical(f"   Signal: {signum}")
        logger.critical(f"   Available: {event.available_mb:.0f}MB" if event.available_mb else "   Available: Unknown")
        logger.critical(f"   Attempting recovery...")
        
        # Record event
        self._record_fault_event(event)
        
        # Attempt recovery
        event.recovery_attempted = True
        recovery_success = self._attempt_recovery(event)
        event.recovery_successful = recovery_success
        
        if recovery_success:
            logger.warning("⚠️ Memory fault recovery attempted - system may be unstable")
            logger.warning("   Consider restarting with cloud offload (JARVIS_PREFER_CLOUD_RUN=true)")
            # Don't exit - let program try to continue
            self._is_recovering = False
            return
        
        # Recovery failed - exit gracefully
        logger.critical("❌ Recovery failed - triggering graceful shutdown")
        self._trigger_graceful_shutdown()
        
        # If we get here, exit with signal code
        sys.exit(128 + signum)
    
    def _attempt_recovery(self, event: FaultEvent) -> bool:
        """
        Attempt to recover from memory fault.
        
        Returns:
            True if recovery might have succeeded
        """
        try:
            # Step 1: Release emergency reserve
            if self._emergency_reserve is not None and not self._reserve_released:
                logger.info("   📦 Releasing emergency reserve...")
                self._emergency_reserve = None
                self._reserve_released = True
            
            # Step 2: Force garbage collection
            logger.info("   🗑️ Forcing garbage collection...")
            gc.collect()
            gc.collect()  # Second pass for cyclic references
            
            # Step 3: Notify callbacks (async-safe)
            logger.info("   📢 Notifying fault callbacks...")
            self._notify_callbacks_sync(event)
            
            # Step 4: Write cross-repo signal file
            logger.info("   📝 Writing cross-repo signal...")
            self._write_signal_file(event)
            
            # Check if we recovered enough memory
            mem = psutil.virtual_memory()
            available_mb = mem.available / (1024 * 1024)
            
            if available_mb > self.config.min_available_mb:
                logger.info(f"   ✅ Recovered to {available_mb:.0f}MB available")
                return True
            else:
                logger.warning(f"   ⚠️ Only {available_mb:.0f}MB available after recovery")
                return False
                
        except Exception as e:
            logger.error(f"   ❌ Recovery error: {e}")
            return False
    
    def _notify_callbacks_sync(self, event: FaultEvent) -> None:
        """Notify callbacks synchronously (for signal handler context)."""
        with self._callback_lock:
            callbacks = list(self._fault_callbacks)
        
        for callback in callbacks[:self.config.max_callbacks]:
            try:
                callback(event)
            except Exception as e:
                logger.debug(f"   Callback error: {e}")
    
    def _write_signal_file(self, event: FaultEvent) -> None:
        """Write signal file for cross-repo coordination."""
        try:
            import json
            
            signal_data = {
                "event": event.to_dict(),
                "action": "emergency_offload",
                "source": "jarvis_core",
                "pid": os.getpid(),
            }
            
            self.config.signal_file_path.write_text(json.dumps(signal_data, indent=2))
            logger.debug(f"   Signal file written: {self.config.signal_file_path}")
            
        except Exception as e:
            logger.debug(f"   Could not write signal file: {e}")
    
    def _trigger_graceful_shutdown(self) -> None:
        """Trigger graceful shutdown sequence."""
        if self._shutdown_triggered:
            return
        
        self._shutdown_triggered = True
        
        try:
            # Write shutdown signal
            shutdown_file = self.config.signal_file_path.parent / "shutdown_requested.txt"
            shutdown_file.write_text(f"Memory fault shutdown at {datetime.now().isoformat()}")
            
            # v149.0: Write Trinity Protocol cloud lock for SIGBUS->cloud handover
            self._write_cloud_lock()
        except Exception:
            pass
    
    def _write_cloud_lock(self) -> None:
        """v149.0: Write cloud lock for SIGBUS->cloud handover."""
        try:
            import json
            cloud_lock_file = Path.home() / ".jarvis" / "trinity" / "cloud_lock.json"
            cloud_lock_file.parent.mkdir(parents=True, exist_ok=True)
            
            mem = psutil.virtual_memory()
            lock_data = {
                "locked": True,
                "reason": "SIGBUS_MEMORY_FAULT",
                "timestamp": time.time(),
                "hardware_ram_gb": mem.total / (1024 ** 3),
                "version": "v149.0",
            }
            
            temp_file = cloud_lock_file.with_suffix('.json.tmp')
            temp_file.write_text(json.dumps(lock_data, indent=2))
            temp_file.rename(cloud_lock_file)
            logger.critical("   ☁️ Cloud lock set - next restart forces cloud mode")
        except Exception as e:
            logger.debug(f"   Could not write cloud lock: {e}")
    
    def _get_traceback(self, frame: Any) -> Optional[str]:
        """Get traceback string from frame."""
        try:
            if frame is not None:
                return "".join(traceback.format_stack(frame))
        except Exception:
            pass
        return None
    
    def _record_fault_event(self, event: FaultEvent) -> None:
        """Record fault event in history."""
        self._fault_history.append(event)
        if len(self._fault_history) > self._max_history:
            self._fault_history = self._fault_history[-self._max_history:]
    
    # =========================================================================
    # Public API - Pre-emptive Protection
    # =========================================================================
    
    def check_memory_available(self, required_mb: int) -> Tuple[bool, str]:
        """
        Check if required memory is available before allocation.
        
        Args:
            required_mb: Memory required in MB
            
        Returns:
            (is_available, reason)
        """
        try:
            mem = psutil.virtual_memory()
            available_mb = mem.available / (1024 * 1024)
            
            # Account for emergency reserve
            effective_available = available_mb - self.config.min_available_mb
            
            if effective_available >= required_mb:
                return True, f"Available: {available_mb:.0f}MB (need {required_mb}MB)"
            else:
                return False, f"Insufficient: {available_mb:.0f}MB available, need {required_mb}MB + {self.config.min_available_mb}MB buffer"
                
        except Exception as e:
            return False, f"Cannot check memory: {e}"
    
    def check_vm_region_availability(self) -> Tuple[bool, str]:
        """
        Check if VM regions are healthy (not approaching exhaustion).
        
        Returns:
            (is_healthy, reason)
        """
        try:
            mem = psutil.virtual_memory()
            usage_percent = mem.percent
            
            if usage_percent < self.config.vm_region_threshold_percent:
                return True, f"VM usage: {usage_percent:.1f}% (threshold: {self.config.vm_region_threshold_percent}%)"
            else:
                return False, f"VM usage too high: {usage_percent:.1f}% >= {self.config.vm_region_threshold_percent}%"
                
        except Exception as e:
            return False, f"Cannot check VM regions: {e}"
    
    def should_offload_to_cloud(self) -> Tuple[bool, str]:
        """
        Determine if operations should be offloaded to cloud.
        
        Returns:
            (should_offload, reason)
        """
        # Check if we've had recent faults
        recent_faults = [
            e for e in self._fault_history
            if (datetime.now() - e.timestamp).total_seconds() < 300  # Last 5 minutes
        ]
        
        if recent_faults:
            return True, f"Recent memory faults: {len(recent_faults)} in last 5 minutes"
        
        # Check if reserve was released (indicates previous fault)
        if self._reserve_released:
            return True, "Emergency reserve was released - memory critically low"
        
        # Check current memory state
        available_ok, reason = self.check_memory_available(500)  # Need 500MB buffer
        if not available_ok:
            return True, reason
        
        vm_ok, reason = self.check_vm_region_availability()
        if not vm_ok:
            return True, reason
        
        return False, "Memory healthy - local operation OK"
    
    def register_fault_callback(self, callback: Callable[[FaultEvent], None]) -> None:
        """
        Register a callback to be called on memory fault.
        
        Args:
            callback: Function taking FaultEvent parameter
        """
        with self._callback_lock:
            if len(self._fault_callbacks) < self.config.max_callbacks:
                self._fault_callbacks.append(callback)
                logger.debug(f"   Registered fault callback (total: {len(self._fault_callbacks)})")
            else:
                logger.warning(f"⚠️ Max callbacks ({self.config.max_callbacks}) reached")
    
    def unregister_fault_callback(self, callback: Callable[[FaultEvent], None]) -> None:
        """Unregister a fault callback."""
        with self._callback_lock:
            if callback in self._fault_callbacks:
                self._fault_callbacks.remove(callback)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current guard status."""
        mem = psutil.virtual_memory()
        
        return {
            "initialized": self._initialized,
            "handlers_installed": self._handlers_installed,
            "reserve_allocated_mb": self.config.emergency_reserve_mb if self._emergency_reserve else 0,
            "reserve_released": self._reserve_released,
            "callbacks_registered": len(self._fault_callbacks),
            "fault_count": len(self._fault_history),
            "recent_faults": len([
                e for e in self._fault_history
                if (datetime.now() - e.timestamp).total_seconds() < 300
            ]),
            "memory_available_mb": mem.available / (1024 * 1024),
            "memory_percent": mem.percent,
            "is_recovering": self._is_recovering,
            "platform": "macos_arm64" if self._is_macos and self._is_arm64 else platform.system(),
        }
    
    def cleanup(self) -> None:
        """Cleanup and restore original handlers."""
        if self._handlers_installed:
            try:
                if self._original_sigbus_handler is not None:
                    signal.signal(signal.SIGBUS, self._original_sigbus_handler)
                if self._original_sigsegv_handler is not None:
                    signal.signal(signal.SIGSEGV, self._original_sigsegv_handler)
                self._handlers_installed = False
                logger.debug("   Restored original signal handlers")
            except Exception as e:
                logger.debug(f"   Could not restore handlers: {e}")
        
        # Release reserve
        self._emergency_reserve = None


# =============================================================================
# Module-Level API
# =============================================================================

_guard: Optional[MemoryFaultGuard] = None
_early_protection_installed = False


def get_memory_fault_guard() -> MemoryFaultGuard:
    """Get or create the global MemoryFaultGuard instance."""
    global _guard
    if _guard is None:
        _guard = MemoryFaultGuard()
    return _guard


def install_early_protection() -> bool:
    """
    Install minimal signal protection at module load time.
    
    This should be called as early as possible (before heavy imports)
    to catch crashes during startup.
    
    Returns:
        True if protection was installed
    """
    global _early_protection_installed
    
    if _early_protection_installed:
        return True
    
    try:
        # Just install faulthandler for now (lightweight)
        faulthandler.enable()
        
        # Full initialization happens later via get_memory_fault_guard().initialize()
        _early_protection_installed = True
        return True
        
    except Exception:
        return False


def check_before_ml_load(model_name: str, estimated_mb: int) -> Tuple[bool, str]:
    """
    Check if it's safe to load an ML model.
    
    Args:
        model_name: Name of model being loaded
        estimated_mb: Estimated memory requirement in MB
        
    Returns:
        (is_safe, reason)
    """
    guard = get_memory_fault_guard()
    
    # Check memory available
    mem_ok, mem_reason = guard.check_memory_available(estimated_mb)
    if not mem_ok:
        return False, f"Cannot load {model_name}: {mem_reason}"
    
    # Check VM regions
    vm_ok, vm_reason = guard.check_vm_region_availability()
    if not vm_ok:
        return False, f"Cannot load {model_name}: {vm_reason}"
    
    return True, f"Safe to load {model_name} ({estimated_mb}MB)"


# Install early protection when module is imported
install_early_protection()
