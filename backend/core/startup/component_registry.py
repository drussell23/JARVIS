"""
JARVIS Component Registry v149.2
================================

Centralized registry for component lifecycle tracking.

This singleton registry provides:
- Component registration with type classification
- State lifecycle management (PENDING → READY/FAILED/DEGRADED)
- Availability checking for downstream code
- Startup summary generation

Usage:
    from backend.core.startup.component_registry import get_registry
    
    registry = get_registry()
    
    # Register and track a component
    registry.register("redis", ComponentType.OPTIONAL)
    registry.mark_initializing("redis")
    
    try:
        await init_redis()
        registry.mark_ready("redis")
    except Exception as e:
        registry.mark_failed("redis", str(e))
    
    # Check availability before use
    if registry.is_available("redis"):
        await redis_client.get(key)
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .component_contract import (
    ComponentState,
    ComponentStatus,
    ComponentType,
    get_component_type,
    get_failure_level,
    get_success_level,
)

logger = logging.getLogger(__name__)


# =============================================================================
# COMPONENT REGISTRY
# =============================================================================

class ComponentRegistry:
    """
    Singleton registry for tracking component lifecycle.
    
    Thread-safe registry that tracks:
    - Component types (required/optional/degradable)
    - Component states (pending/initializing/ready/failed/degraded)
    - Initialization timing for performance analysis
    - Failure reasons for debugging
    
    The registry enables:
    - Downstream code to check if a component is available
    - Startup summary generation
    - Graceful degradation coordination
    """
    
    _instance: Optional["ComponentRegistry"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "ComponentRegistry":
        """Singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the registry (only runs once due to singleton)."""
        if self._initialized:
            return
        
        self._components: Dict[str, ComponentStatus] = {}
        self._component_lock = threading.RLock()
        self._startup_start_time: Optional[float] = None
        self._startup_end_time: Optional[float] = None
        self._initialized = True
    
    # =========================================================================
    # REGISTRATION
    # =========================================================================
    
    def register(
        self,
        name: str,
        component_type: Optional[ComponentType] = None,
    ) -> ComponentStatus:
        """
        Register a component for tracking.
        
        Args:
            name: Unique component identifier
            component_type: Optional override (uses default if not provided)
            
        Returns:
            The created ComponentStatus
        """
        with self._component_lock:
            if name in self._components:
                return self._components[name]
            
            # Use provided type or look up default
            ctype = component_type or get_component_type(name)
            
            status = ComponentStatus(
                name=name,
                component_type=ctype,
                state=ComponentState.PENDING,
            )
            
            self._components[name] = status
            
            logger.debug(f"[Registry] Registered {name} as {ctype.value}")
            return status
    
    def register_many(self, names: List[str]) -> None:
        """Register multiple components with default types."""
        for name in names:
            self.register(name)
    
    # =========================================================================
    # STATE TRANSITIONS
    # =========================================================================
    
    def mark_initializing(self, name: str) -> None:
        """Mark component as currently initializing."""
        with self._component_lock:
            if name not in self._components:
                self.register(name)
            
            status = self._components[name]
            status.state = ComponentState.INITIALIZING
            status.initialized_at = time.time()
            
            if self._startup_start_time is None:
                self._startup_start_time = time.time()
    
    def mark_ready(self, name: str) -> None:
        """Mark component as fully initialized and ready."""
        with self._component_lock:
            if name not in self._components:
                self.register(name)
            
            status = self._components[name]
            end_time = time.time()
            
            if status.initialized_at:
                status.duration_seconds = end_time - status.initialized_at
            
            status.state = ComponentState.READY
            status.error = None
            
            # Log success at appropriate level
            level = get_success_level(status.component_type)
            logger.log(
                level,
                f"[Registry] ✓ {name} ready "
                f"({status.duration_seconds:.2f}s)" if status.duration_seconds else f"[Registry] ✓ {name} ready"
            )
    
    def mark_failed(
        self,
        name: str,
        error: str,
        error_type: Optional[str] = None,
    ) -> None:
        """
        Mark component as failed.
        
        Logs at the appropriate level based on component type.
        """
        with self._component_lock:
            if name not in self._components:
                self.register(name)
            
            status = self._components[name]
            end_time = time.time()
            
            if status.initialized_at:
                status.duration_seconds = end_time - status.initialized_at
            
            status.state = ComponentState.FAILED
            status.error = error
            status.error_type = error_type
            
            # Log at appropriate level based on component type
            level = get_failure_level(status.component_type)
            type_label = status.component_type.value.upper()
            
            if status.component_type == ComponentType.OPTIONAL:
                logger.log(
                    level,
                    f"[Registry] {name} unavailable ({type_label}): {error}"
                )
            else:
                logger.log(
                    level,
                    f"[Registry] ✗ {name} failed ({type_label}): {error}"
                )
    
    def mark_degraded(
        self,
        name: str,
        reason: str,
    ) -> None:
        """Mark component as operating in degraded mode."""
        with self._component_lock:
            if name not in self._components:
                self.register(name)
            
            status = self._components[name]
            end_time = time.time()
            
            if status.initialized_at:
                status.duration_seconds = end_time - status.initialized_at
            
            status.state = ComponentState.DEGRADED
            status.degradation_reason = reason
            
            logger.warning(f"[Registry] ⚠ {name} degraded: {reason}")
    
    def mark_skipped(self, name: str, reason: str = "intentionally skipped") -> None:
        """Mark component as intentionally skipped."""
        with self._component_lock:
            if name not in self._components:
                self.register(name)
            
            status = self._components[name]
            status.state = ComponentState.SKIPPED
            status.error = reason
            
            logger.debug(f"[Registry] {name} skipped: {reason}")
    
    # =========================================================================
    # QUERIES
    # =========================================================================
    
    def is_available(self, name: str) -> bool:
        """Check if a component is available for use."""
        with self._component_lock:
            if name not in self._components:
                return False
            return self._components[name].is_available
    
    def is_healthy(self, name: str) -> bool:
        """Check if a component is fully healthy."""
        with self._component_lock:
            if name not in self._components:
                return False
            return self._components[name].is_healthy
    
    def get_status(self, name: str) -> Optional[ComponentStatus]:
        """Get the status of a component."""
        with self._component_lock:
            return self._components.get(name)
    
    def get_all_statuses(self) -> Dict[str, ComponentStatus]:
        """Get all component statuses."""
        with self._component_lock:
            return dict(self._components)
    
    def get_available_components(self) -> List[str]:
        """Get names of all available components."""
        with self._component_lock:
            return [
                name for name, status in self._components.items()
                if status.is_available
            ]
    
    def get_failed_components(self) -> List[str]:
        """Get names of all failed components."""
        with self._component_lock:
            return [
                name for name, status in self._components.items()
                if status.is_failed
            ]
    
    def get_required_failures(self) -> List[ComponentStatus]:
        """Get statuses of failed REQUIRED components."""
        with self._component_lock:
            return [
                status for status in self._components.values()
                if status.is_failed and status.component_type == ComponentType.REQUIRED
            ]
    
    # =========================================================================
    # STARTUP SUMMARY
    # =========================================================================
    
    def finish_startup(self) -> None:
        """Mark startup as complete."""
        self._startup_end_time = time.time()
    
    def get_startup_summary(self) -> dict:
        """
        Generate a startup summary for logging.
        
        Returns:
            Dictionary with startup metrics and component statuses
        """
        with self._component_lock:
            total = len(self._components)
            ready = sum(1 for s in self._components.values() if s.state == ComponentState.READY)
            failed = sum(1 for s in self._components.values() if s.state == ComponentState.FAILED)
            degraded = sum(1 for s in self._components.values() if s.state == ComponentState.DEGRADED)
            skipped = sum(1 for s in self._components.values() if s.state == ComponentState.SKIPPED)
            
            startup_duration = None
            if self._startup_start_time and self._startup_end_time:
                startup_duration = self._startup_end_time - self._startup_start_time
            
            # Categorize by type
            required_status = []
            optional_status = []
            degradable_status = []
            
            for status in self._components.values():
                entry = status.to_dict()
                if status.component_type == ComponentType.REQUIRED:
                    required_status.append(entry)
                elif status.component_type == ComponentType.OPTIONAL:
                    optional_status.append(entry)
                else:
                    degradable_status.append(entry)
            
            return {
                "total_components": total,
                "ready": ready,
                "failed": failed,
                "degraded": degraded,
                "skipped": skipped,
                "startup_duration_seconds": startup_duration,
                "required": required_status,
                "degradable": degradable_status,
                "optional": optional_status,
                "has_critical_failures": len(self.get_required_failures()) > 0,
            }
    
    def log_startup_summary(self) -> None:
        """Log a human-readable startup summary."""
        summary = self.get_startup_summary()
        
        # Build summary message
        lines = [
            "=" * 60,
            "JARVIS STARTUP SUMMARY",
            "=" * 60,
            f"Components: {summary['ready']}/{summary['total_components']} ready",
        ]
        
        if summary['degraded'] > 0:
            lines.append(f"Degraded: {summary['degraded']}")
        
        if summary['failed'] > 0:
            lines.append(f"Failed: {summary['failed']}")
        
        if summary['startup_duration_seconds']:
            lines.append(f"Duration: {summary['startup_duration_seconds']:.2f}s")
        
        # List failed required components
        if summary['has_critical_failures']:
            lines.append("")
            lines.append("⚠️  CRITICAL FAILURES:")
            for comp in summary['required']:
                if comp['state'] == 'failed':
                    lines.append(f"  - {comp['name']}: {comp['error']}")
        
        # List degraded components
        degraded = [c for c in summary['degradable'] if c['state'] == 'degraded']
        if degraded:
            lines.append("")
            lines.append("Degraded Components:")
            for comp in degraded:
                lines.append(f"  - {comp['name']}")
        
        # List unavailable optional components
        unavailable = [c for c in summary['optional'] if c['state'] == 'failed']
        if unavailable:
            lines.append("")
            lines.append("Unavailable (optional):")
            for comp in unavailable:
                lines.append(f"  - {comp['name']}")
        
        lines.append("=" * 60)
        
        # Choose log level based on status
        if summary['has_critical_failures']:
            level = logging.ERROR
        elif summary['degraded'] > 0:
            level = logging.WARNING
        else:
            level = logging.INFO
        
        for line in lines:
            logger.log(level, line)
    
    # =========================================================================
    # RESET (for testing)
    # =========================================================================
    
    def reset(self) -> None:
        """Reset the registry (primarily for testing)."""
        with self._component_lock:
            self._components.clear()
            self._startup_start_time = None
            self._startup_end_time = None


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

def get_registry() -> ComponentRegistry:
    """Get the global ComponentRegistry singleton."""
    return ComponentRegistry()


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    "ComponentRegistry",
    "get_registry",
]
