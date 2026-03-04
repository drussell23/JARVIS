"""
Proactive Resource Guard v1.0 - OOM Prevention Through Preemptive Memory Management
=====================================================================================

This module prevents Out-of-Memory (OOM) kills by proactively managing memory
BEFORE heavy operations like ML model loading. Instead of reactive cleanup
(which fails if the process is killed), this guard ensures sufficient memory
exists before starting resource-intensive operations.

ROOT CAUSE FIX:
    The supervisor was being killed by macOS OOM killer (`zsh: killed`) because:
    1. Multiple heavy components (SentenceTransformer, LLM models) loaded simultaneously
    2. No memory check before loading - just load and hope for the best
    3. Cleanup code never runs when OOM killer strikes first

SOLUTION:
    1. Pre-flight memory check before each heavy component
    2. Memory budget allocation system (reserve before use)
    3. Intelligent deferral when memory is low
    4. Integration with DEFCON resource governor for system-wide coordination
    5. Emergency unload capability to free memory on demand

Usage:
    from backend.core.proactive_resource_guard import get_proactive_resource_guard
    
    guard = get_proactive_resource_guard()
    
    # Check if we have enough memory before loading
    if await guard.request_memory_budget("sentence_transformer", estimated_mb=800):
        # Safe to load
        model = SentenceTransformer(...)
    else:
        # Memory too low - defer or use cloud fallback
        logger.warning("Deferring model load due to memory pressure")

    # When done with the resource
    guard.release_budget("sentence_transformer")

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

# Default memory thresholds (can be overridden via environment variables)
DEFAULT_MIN_AVAILABLE_GB = float(os.getenv("JARVIS_MIN_AVAILABLE_MEMORY_GB", "2.0"))
DEFAULT_LITE_MODE_THRESHOLD_GB = float(os.getenv("JARVIS_LITE_MODE_THRESHOLD_GB", "4.0"))
DEFAULT_CRITICAL_THRESHOLD_GB = float(os.getenv("JARVIS_CRITICAL_MEMORY_THRESHOLD_GB", "1.0"))
DEFAULT_MEMORY_CHECK_INTERVAL = float(os.getenv("JARVIS_MEMORY_CHECK_INTERVAL", "5.0"))

# Known memory requirements for common components (in MB)
COMPONENT_MEMORY_ESTIMATES = {
    "sentence_transformer": 800,      # SentenceTransformer model (~500MB-1GB)
    "local_llm_3b": 4000,             # 3B parameter model (~4GB)
    "local_llm_7b": 8000,             # 7B parameter model (~8GB)
    "speech_recognition": 500,        # Whisper/SpeechBrain models
    "speaker_verification": 300,      # ECAPA-TDNN
    "vision_model": 400,              # OCR/Computer vision
    "neural_mesh_full": 1000,         # Full neural mesh coordination
    "trinity_integrator": 200,        # Cross-repo coordination
    "default": 500,                   # Default estimate for unknown components
}


class MemoryState(Enum):
    """Memory availability states."""
    HEALTHY = "healthy"           # > lite_mode_threshold: Full operations allowed
    CONSTRAINED = "constrained"   # min_available < x < lite_mode: Lite mode
    CRITICAL = "critical"         # < min_available: Only essential operations
    EMERGENCY = "emergency"       # < critical_threshold: Emergency unload needed


@dataclass
class MemoryBudget:
    """Tracks a memory allocation for a component."""
    component: str
    estimated_mb: int
    allocated_at: float
    priority: int = 50  # 0-100, higher = more important to keep
    can_unload: bool = True  # Whether this can be force-unloaded
    unload_callback: Optional[Callable[[], None]] = None


@dataclass
class MemoryStatus:
    """Current memory status snapshot."""
    total_gb: float
    available_gb: float
    used_gb: float
    percent_used: float
    state: MemoryState
    budgets_allocated_mb: int
    components_loaded: List[str]
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# PROACTIVE RESOURCE GUARD
# =============================================================================

class ProactiveResourceGuard:
    """
    Proactive memory management to prevent OOM kills.
    
    This guard:
    1. Tracks memory budgets for each heavy component
    2. Blocks loading if insufficient memory available
    3. Provides emergency unload capability
    4. Integrates with DEFCON resource governor
    5. Supports cross-repo coordination via file-based signaling
    """
    
    _instance: Optional["ProactiveResourceGuard"] = None
    _instance_lock = threading.Lock()
    
    def __new__(cls) -> "ProactiveResourceGuard":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        
        # Configuration
        self._min_available_gb = DEFAULT_MIN_AVAILABLE_GB
        self._lite_mode_threshold_gb = DEFAULT_LITE_MODE_THRESHOLD_GB
        self._critical_threshold_gb = DEFAULT_CRITICAL_THRESHOLD_GB
        self._check_interval = DEFAULT_MEMORY_CHECK_INTERVAL
        
        # Budget tracking
        self._budgets: Dict[str, MemoryBudget] = {}
        self._budget_lock = threading.RLock()
        
        # Async lock for coordination
        self._async_lock: Optional[asyncio.Lock] = None
        
        # Memory pressure callbacks
        self._pressure_callbacks: List[Callable[[MemoryState], None]] = []
        
        # State tracking
        self._last_state = MemoryState.HEALTHY
        self._state_change_count = 0
        self._gc_trigger_count = 0
        
        # Cross-repo signaling
        self._signal_file = os.path.expanduser("~/.jarvis/memory_pressure.json")
        
        # Statistics
        self._requests_granted = 0
        self._requests_denied = 0
        self._emergency_unloads = 0
        
        self._initialized = True
        logger.info("[ProactiveResourceGuard] Initialized (min_available=%.1fGB, lite_threshold=%.1fGB)",
                   self._min_available_gb, self._lite_mode_threshold_gb)
    
    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create async lock (needs event loop context)."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock
    
    def get_memory_info(self) -> Tuple[float, float, float]:
        """
        Get current memory information.
        
        Returns:
            Tuple of (total_gb, available_gb, used_gb)
        """
        try:
            import psutil
            mem = psutil.virtual_memory()
            total_gb = mem.total / (1024**3)
            available_gb = mem.available / (1024**3)
            used_gb = mem.used / (1024**3)
            return (total_gb, available_gb, used_gb)
        except ImportError:
            logger.warning("[ProactiveResourceGuard] psutil not available, assuming 16GB system")
            return (16.0, 8.0, 8.0)  # Conservative estimate
        except Exception as e:
            logger.error(f"[ProactiveResourceGuard] Error getting memory info: {e}")
            return (16.0, 4.0, 12.0)  # Assume constrained
    
    def get_memory_state(self) -> MemoryState:
        """Determine current memory state based on available memory."""
        _, available_gb, _ = self.get_memory_info()
        
        if available_gb < self._critical_threshold_gb:
            return MemoryState.EMERGENCY
        elif available_gb < self._min_available_gb:
            return MemoryState.CRITICAL
        elif available_gb < self._lite_mode_threshold_gb:
            return MemoryState.CONSTRAINED
        else:
            return MemoryState.HEALTHY
    
    def get_status(self) -> MemoryStatus:
        """Get comprehensive memory status."""
        total_gb, available_gb, used_gb = self.get_memory_info()
        
        with self._budget_lock:
            budgets_mb = sum(b.estimated_mb for b in self._budgets.values())
            components = list(self._budgets.keys())
        
        return MemoryStatus(
            total_gb=total_gb,
            available_gb=available_gb,
            used_gb=used_gb,
            percent_used=(used_gb / total_gb * 100) if total_gb > 0 else 0,
            state=self.get_memory_state(),
            budgets_allocated_mb=budgets_mb,
            components_loaded=components,
        )
    
    def _check_state_change(self) -> None:
        """Check for memory state changes and trigger callbacks."""
        current_state = self.get_memory_state()
        
        if current_state != self._last_state:
            self._state_change_count += 1
            logger.info(
                f"[ProactiveResourceGuard] Memory state changed: {self._last_state.value} → {current_state.value}"
            )
            
            # Trigger callbacks
            for callback in self._pressure_callbacks:
                try:
                    callback(current_state)
                except Exception as e:
                    logger.error(f"[ProactiveResourceGuard] Callback error: {e}")
            
            # Write cross-repo signal
            self._write_pressure_signal(current_state)
            
            self._last_state = current_state
    
    def _write_pressure_signal(self, state: MemoryState) -> None:
        """Write memory pressure signal for cross-repo coordination."""
        try:
            import json
            os.makedirs(os.path.dirname(self._signal_file), exist_ok=True)
            with open(self._signal_file, 'w') as f:
                json.dump({
                    "state": state.value,
                    "timestamp": time.time(),
                    "pid": os.getpid(),
                }, f)
        except Exception as e:
            logger.debug(f"[ProactiveResourceGuard] Could not write signal file: {e}")
    
    def _trigger_gc(self) -> int:
        """Trigger garbage collection and return bytes freed."""
        self._gc_trigger_count += 1
        
        # Get memory before
        _, before_gb, _ = self.get_memory_info()
        
        # Run GC
        gc.collect()
        
        # Get memory after
        _, after_gb, _ = self.get_memory_info()
        
        freed_mb = (after_gb - before_gb) * 1024
        if freed_mb > 10:
            logger.info(f"[ProactiveResourceGuard] GC freed {freed_mb:.0f}MB")
        
        return int(freed_mb)
    
    def get_estimated_memory(self, component: str) -> int:
        """Get estimated memory requirement for a component (in MB)."""
        return COMPONENT_MEMORY_ESTIMATES.get(
            component, 
            COMPONENT_MEMORY_ESTIMATES["default"]
        )
    
    async def request_memory_budget(
        self,
        component: str,
        estimated_mb: Optional[int] = None,
        priority: int = 50,
        timeout: float = 30.0,
        can_unload: bool = True,
        unload_callback: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        Request a memory budget allocation before loading a component.

        This is the main entry point. Call this BEFORE loading any heavy
        component (ML models, large data structures, etc.).

        .. deprecated::
            Use :func:`MemoryBudgetBroker.request` instead.

        Args:
            component: Unique identifier for the component
            estimated_mb: Estimated memory usage (uses lookup if not provided)
            priority: 0-100, higher = more important to keep during pressure
            timeout: Max seconds to wait for memory to become available
            can_unload: Whether this component can be force-unloaded
            unload_callback: Function to call to unload this component

        Returns:
            True if budget allocated (safe to load), False if denied
        """
        # Memory Control Plane: delegate to broker if available
        try:
            import warnings
            warnings.warn(
                "ProactiveResourceGuard.request_memory_budget() is deprecated. "
                "Use MemoryBudgetBroker.request() instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            from backend.core.memory_budget_broker import get_memory_budget_broker
            broker = get_memory_budget_broker()
            if broker is not None:
                from backend.core.memory_types import BudgetPriority, StartupPhase
                try:
                    grant = await broker.try_request(
                        component=f"legacy:{component}@v1",
                        bytes_requested=int((estimated_mb if estimated_mb is not None else self.get_estimated_memory(component)) * 1024 * 1024),
                        priority=BudgetPriority.RUNTIME_INTERACTIVE,
                        phase=broker.current_phase,
                    )
                    return grant is not None
                except Exception:
                    pass  # Fall through to legacy logic
        except ImportError:
            pass

        if estimated_mb is None:
            estimated_mb = self.get_estimated_memory(component)
        
        async with self._get_async_lock():
            return await self._request_budget_internal(
                component, estimated_mb, priority, timeout, can_unload, unload_callback
            )
    
    async def _request_budget_internal(
        self,
        component: str,
        estimated_mb: int,
        priority: int,
        timeout: float,
        can_unload: bool,
        unload_callback: Optional[Callable[[], None]],
    ) -> bool:
        """Internal budget request logic."""
        start_time = time.time()
        required_gb = estimated_mb / 1024
        
        # Check if already allocated
        with self._budget_lock:
            if component in self._budgets:
                logger.debug(f"[ProactiveResourceGuard] {component} already has budget")
                return True
        
        # Wait for sufficient memory
        while time.time() - start_time < timeout:
            _, available_gb, _ = self.get_memory_info()
            
            # Check state
            state = self.get_memory_state()
            self._check_state_change()
            
            # Emergency state - deny all but essential
            if state == MemoryState.EMERGENCY:
                if priority < 90:
                    logger.warning(
                        f"[ProactiveResourceGuard] ❌ Denied {component} - EMERGENCY memory state "
                        f"(available: {available_gb:.1f}GB, needed: {required_gb:.1f}GB)"
                    )
                    self._requests_denied += 1
                    return False
            
            # Critical state - deny medium priority
            if state == MemoryState.CRITICAL:
                if priority < 70:
                    logger.warning(
                        f"[ProactiveResourceGuard] ❌ Denied {component} - CRITICAL memory state"
                    )
                    self._requests_denied += 1
                    return False
            
            # Check if we have enough available
            remaining_after_load = available_gb - required_gb
            if remaining_after_load >= self._min_available_gb:
                # Allocate budget
                with self._budget_lock:
                    self._budgets[component] = MemoryBudget(
                        component=component,
                        estimated_mb=estimated_mb,
                        allocated_at=time.time(),
                        priority=priority,
                        can_unload=can_unload,
                        unload_callback=unload_callback,
                    )
                
                logger.info(
                    f"[ProactiveResourceGuard] ✅ Granted budget for {component} "
                    f"({estimated_mb}MB, remaining: {remaining_after_load:.1f}GB)"
                )
                self._requests_granted += 1
                return True
            
            # Not enough memory - try GC first
            self._trigger_gc()
            
            # Still not enough - wait and retry
            await asyncio.sleep(1.0)
        
        # Timeout
        logger.warning(
            f"[ProactiveResourceGuard] ❌ Timeout waiting for memory for {component}"
        )
        self._requests_denied += 1
        return False
    
    def request_memory_budget_sync(
        self,
        component: str,
        estimated_mb: Optional[int] = None,
        priority: int = 50,
    ) -> bool:
        """
        Synchronous version of request_memory_budget.
        
        Use for non-async contexts. Does not wait for memory to become available.
        """
        if estimated_mb is None:
            estimated_mb = self.get_estimated_memory(component)
        
        required_gb = estimated_mb / 1024
        _, available_gb, _ = self.get_memory_info()
        state = self.get_memory_state()
        
        # Emergency/Critical - deny low priority
        if state in (MemoryState.EMERGENCY, MemoryState.CRITICAL) and priority < 70:
            self._requests_denied += 1
            return False
        
        # Check available memory
        remaining_after_load = available_gb - required_gb
        if remaining_after_load >= self._min_available_gb:
            with self._budget_lock:
                self._budgets[component] = MemoryBudget(
                    component=component,
                    estimated_mb=estimated_mb,
                    allocated_at=time.time(),
                    priority=priority,
                    can_unload=True,
                    unload_callback=None,
                )
            self._requests_granted += 1
            return True
        
        # Try GC
        self._trigger_gc()
        
        # Check again
        _, available_gb, _ = self.get_memory_info()
        remaining_after_load = available_gb - required_gb
        if remaining_after_load >= self._min_available_gb:
            with self._budget_lock:
                self._budgets[component] = MemoryBudget(
                    component=component,
                    estimated_mb=estimated_mb,
                    allocated_at=time.time(),
                    priority=priority,
                    can_unload=True,
                    unload_callback=None,
                )
            self._requests_granted += 1
            return True
        
        self._requests_denied += 1
        return False
    
    def release_budget(self, component: str) -> bool:
        """
        Release a memory budget when component is unloaded.
        
        Call this when you're done with a component to allow other
        components to use that memory.
        """
        with self._budget_lock:
            if component in self._budgets:
                budget = self._budgets.pop(component)
                logger.debug(
                    f"[ProactiveResourceGuard] Released budget for {component} ({budget.estimated_mb}MB)"
                )
                return True
        return False
    
    def emergency_unload(self, target_free_mb: int = 1000) -> int:
        """
        Emergency unload of components to free memory.
        
        Unloads lowest-priority components first until target memory is freed
        or all unloadable components are unloaded.
        
        Args:
            target_free_mb: Target amount of memory to free
            
        Returns:
            Estimated MB freed
        """
        logger.warning(f"[ProactiveResourceGuard] ⚠️ Emergency unload triggered (target: {target_free_mb}MB)")
        self._emergency_unloads += 1
        
        freed_mb = 0
        
        with self._budget_lock:
            # Sort by priority (lowest first)
            unloadable = [
                (name, budget) for name, budget in self._budgets.items()
                if budget.can_unload and budget.unload_callback is not None
            ]
            unloadable.sort(key=lambda x: x[1].priority)
            
            for name, budget in unloadable:
                if freed_mb >= target_free_mb:
                    break
                
                try:
                    logger.info(f"[ProactiveResourceGuard] Unloading {name} (priority: {budget.priority})")
                    budget.unload_callback()
                    del self._budgets[name]
                    freed_mb += budget.estimated_mb
                except Exception as e:
                    logger.error(f"[ProactiveResourceGuard] Error unloading {name}: {e}")
        
        # Force GC after unloading
        gc.collect()
        
        logger.info(f"[ProactiveResourceGuard] Emergency unload complete: ~{freed_mb}MB freed")
        return freed_mb
    
    def register_pressure_callback(self, callback: Callable[[MemoryState], None]) -> None:
        """Register a callback to be notified of memory pressure changes."""
        self._pressure_callbacks.append(callback)
    
    def should_use_lite_mode(self) -> bool:
        """Check if system should operate in lite mode due to memory constraints."""
        state = self.get_memory_state()
        return state in (MemoryState.CONSTRAINED, MemoryState.CRITICAL, MemoryState.EMERGENCY)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get guard statistics."""
        status = self.get_status()
        return {
            "memory_state": status.state.value,
            "available_gb": round(status.available_gb, 2),
            "used_gb": round(status.used_gb, 2),
            "percent_used": round(status.percent_used, 1),
            "budgets_allocated_mb": status.budgets_allocated_mb,
            "components_loaded": status.components_loaded,
            "requests_granted": self._requests_granted,
            "requests_denied": self._requests_denied,
            "emergency_unloads": self._emergency_unloads,
            "gc_triggers": self._gc_trigger_count,
            "state_changes": self._state_change_count,
            "lite_mode_active": self.should_use_lite_mode(),
        }
    
    @asynccontextmanager
    async def memory_budget(
        self,
        component: str,
        estimated_mb: Optional[int] = None,
        priority: int = 50,
    ):
        """
        Context manager for automatic budget allocation and release.
        
        Usage:
            async with guard.memory_budget("my_model", estimated_mb=500):
                model = load_model()
                # Use model
            # Budget automatically released
        """
        granted = await self.request_memory_budget(component, estimated_mb, priority)
        if not granted:
            raise MemoryError(f"Could not allocate memory budget for {component}")
        
        try:
            yield granted
        finally:
            self.release_budget(component)
    
    @contextmanager
    def memory_budget_sync(
        self,
        component: str,
        estimated_mb: Optional[int] = None,
        priority: int = 50,
    ):
        """Synchronous context manager for memory budgets."""
        granted = self.request_memory_budget_sync(component, estimated_mb, priority)
        if not granted:
            raise MemoryError(f"Could not allocate memory budget for {component}")
        
        try:
            yield granted
        finally:
            self.release_budget(component)


# =============================================================================
# GLOBAL ACCESS
# =============================================================================

_guard_instance: Optional[ProactiveResourceGuard] = None


def get_proactive_resource_guard() -> ProactiveResourceGuard:
    """Get the global ProactiveResourceGuard singleton."""
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = ProactiveResourceGuard()
    return _guard_instance


def check_memory_before_load(component: str, estimated_mb: Optional[int] = None) -> bool:
    """
    Quick synchronous check if it's safe to load a component.
    
    Convenience function for simple cases.
    """
    guard = get_proactive_resource_guard()
    return guard.request_memory_budget_sync(component, estimated_mb)


def should_use_lite_mode() -> bool:
    """Check if system should use lite mode due to memory constraints."""
    guard = get_proactive_resource_guard()
    return guard.should_use_lite_mode()


# =============================================================================
# INTEGRATION WITH DEFCON RESOURCE GOVERNOR
# =============================================================================

def integrate_with_defcon_governor() -> bool:
    """
    Integrate with the existing DEFCON resource governor.
    
    This ensures memory pressure detected by either system triggers
    appropriate responses in both.
    """
    try:
        from backend.core.resource_governor import get_resource_governor
        
        governor = get_resource_governor()
        guard = get_proactive_resource_guard()
        
        def on_defcon_change(defcon_level: str):
            """Callback when DEFCON level changes."""
            if defcon_level == "RED":
                # Emergency - trigger unload
                guard.emergency_unload(target_free_mb=2000)
            elif defcon_level == "YELLOW":
                # Elevated - trigger GC
                guard._trigger_gc()
        
        # Register with governor if possible
        if hasattr(governor, "register_level_callback"):
            governor.register_level_callback(on_defcon_change)
            logger.info("[ProactiveResourceGuard] Integrated with DEFCON resource governor")
            return True
        
    except ImportError:
        logger.debug("[ProactiveResourceGuard] DEFCON resource governor not available")
    except Exception as e:
        logger.debug(f"[ProactiveResourceGuard] Could not integrate with governor: {e}")
    
    return False


def check_vm_region_availability(required_mb: int = 500) -> Tuple[bool, str]:
    """
    Check if VM regions have sufficient space for allocation.
    
    This provides an additional safety check beyond memory pressure -
    it validates that the VM address space is healthy and not approaching
    the commpage boundary where SIGBUS crashes occur.
    
    Args:
        required_mb: Minimum MB required for the operation
        
    Returns:
        (is_safe, reason)
    """
    guard = get_proactive_resource_guard()
    total_gb, available_gb, _ = guard.get_memory_info()
    
    # Check basic availability
    available_mb = available_gb * 1024
    if available_mb < required_mb + 500:  # Need buffer above requirement
        return False, f"Insufficient memory: {available_mb:.0f}MB available, need {required_mb + 500}MB"
    
    # Check if we're approaching critical thresholds
    state = guard.get_memory_state()
    if state == MemoryState.EMERGENCY:
        return False, f"Memory state is EMERGENCY - VM regions exhausted"
    elif state == MemoryState.CRITICAL:
        return False, f"Memory state is CRITICAL - risk of SIGBUS"
    
    # Check memory percentage (high percentage suggests VM fragmentation)
    percent_used = ((total_gb - available_gb) / total_gb) * 100 if total_gb > 0 else 100
    if percent_used > 90:
        return False, f"Memory {percent_used:.1f}% used - high risk of VM region exhaustion"
    
    return True, f"VM regions healthy: {available_mb:.0f}MB available ({percent_used:.1f}% used)"


def integrate_with_fault_guard() -> bool:
    """
    Integrate ProactiveResourceGuard with MemoryFaultGuard.
    
    This creates bidirectional coordination:
    - MemoryFaultGuard signals PRG on memory faults
    - PRG triggers emergency unload when fault detected
    
    Returns:
        True if integration successful
    """
    try:
        from backend.core.memory_fault_guard import get_memory_fault_guard, FaultEvent
        
        guard = get_proactive_resource_guard()
        fault_guard = get_memory_fault_guard()
        
        def on_memory_fault(event: FaultEvent):
            """Callback when memory fault is detected."""
            logger.warning(f"[ProactiveResourceGuard] Memory fault detected: {event.fault_type.value}")
            # Trigger emergency unload to try to recover
            try:
                freed_mb = guard.emergency_unload(target_free_mb=1000)
                logger.info(f"[ProactiveResourceGuard] Freed {freed_mb}MB in response to fault")
            except Exception as e:
                logger.error(f"[ProactiveResourceGuard] Emergency unload failed: {e}")
        
        fault_guard.register_fault_callback(on_memory_fault)
        logger.info("[ProactiveResourceGuard] Integrated with MemoryFaultGuard")
        return True
        
    except ImportError:
        logger.debug("[ProactiveResourceGuard] MemoryFaultGuard not available")
    except Exception as e:
        logger.debug(f"[ProactiveResourceGuard] Could not integrate with fault guard: {e}")
    
    return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "ProactiveResourceGuard",
    "MemoryState",
    "MemoryStatus",
    "MemoryBudget",
    "get_proactive_resource_guard",
    "check_memory_before_load",
    "should_use_lite_mode",
    "integrate_with_defcon_governor",
    "check_vm_region_availability",
    "integrate_with_fault_guard",
    "COMPONENT_MEMORY_ESTIMATES",
]

