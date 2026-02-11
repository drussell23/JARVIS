"""
Dynamic Component Loader for JARVIS Vision System.
Automatically switches between Rust and Python implementations based on availability.
Periodically checks for Rust components and upgrades when available.
"""

import asyncio
import importlib
import logging
import time
import sys
from typing import Dict, Any, Optional, Callable, Type
from dataclasses import dataclass
from enum import Enum
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class ComponentType(Enum):
    """Types of components that can have Rust/Python implementations."""
    BLOOM_FILTER = "bloom_filter"
    SLIDING_WINDOW = "sliding_window"
    METAL_ACCELERATOR = "metal_accelerator"
    ZERO_COPY_POOL = "zero_copy_pool"
    IMAGE_PROCESSOR = "image_processor"
    MEMORY_POOL = "memory_pool"

class ImplementationType(Enum):
    """Implementation backend type."""
    RUST = "rust"
    PYTHON = "python"
    HYBRID = "hybrid"  # Uses Rust for some operations, Python for others

@dataclass
class ComponentImplementation:
    """Represents a component implementation."""
    type: ComponentType
    implementation: ImplementationType
    module_path: str
    class_name: str
    instance: Optional[Any] = None
    performance_score: float = 1.0  # Higher is better
    last_checked: Optional[datetime] = None
    error_count: int = 0
    is_available: bool = False

class DynamicComponentLoader:
    """
    Manages dynamic loading and switching between Rust and Python components.
    Automatically upgrades to Rust when available.
    """
    
    def __init__(self, check_interval: int = 60):
        """
        Initialize the dynamic loader.

        Args:
            check_interval: Seconds between Rust availability checks
        """
        self.check_interval = check_interval
        self.components: Dict[ComponentType, Dict[ImplementationType, ComponentImplementation]] = {}
        self.active_components: Dict[ComponentType, ComponentImplementation] = {}
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._component_callbacks: Dict[ComponentType, list] = {}
        self.self_healer: Optional[Any] = None  # Will be set during start()

        # Track self-healing attempts to prevent infinite loops
        self._self_heal_attempts = 0
        self._max_self_heal_attempts = 3
        self._last_self_heal_time: Optional[datetime] = None
        self._self_heal_cooldown = timedelta(minutes=5)  # Don't retry for 5 minutes

        # Register all known components
        self._register_components()
        
    def _register_components(self):
        """Register all known component implementations."""
        # Bloom Filter implementations
        self.register_component(
            ComponentType.BLOOM_FILTER,
            ImplementationType.RUST,
            "jarvis_rust_core.bloom_filter",
            "PyRustBloomFilter",
            performance_score=10.0
        )
        self.register_component(
            ComponentType.BLOOM_FILTER,
            ImplementationType.PYTHON,
            "vision.bloom_filter",
            "PythonBloomFilter",
            performance_score=1.0
        )
        
        # Sliding Window implementations
        self.register_component(
            ComponentType.SLIDING_WINDOW,
            ImplementationType.RUST,
            "jarvis_rust_core.sliding_window",
            "PySlidingWindow",
            performance_score=5.0
        )
        self.register_component(
            ComponentType.SLIDING_WINDOW,
            ImplementationType.PYTHON,
            "vision.sliding_window",
            "SlidingWindow",
            performance_score=1.0
        )
        
        # Metal Accelerator (Rust only for macOS)
        if sys.platform == "darwin":
            self.register_component(
                ComponentType.METAL_ACCELERATOR,
                ImplementationType.RUST,
                "jarvis_rust_core.metal_accelerator",
                "PyMetalAccelerator",
                performance_score=2.0
            )
        
        # Zero-copy Pool implementations
        self.register_component(
            ComponentType.ZERO_COPY_POOL,
            ImplementationType.RUST,
            "jarvis_rust_core.zero_copy",
            "PyZeroCopyPool",
            performance_score=3.0
        )
        self.register_component(
            ComponentType.ZERO_COPY_POOL,
            ImplementationType.PYTHON,
            "vision.memory.zero_copy_fallback",
            "PythonZeroCopyPool",
            performance_score=1.0
        )
        
        # Memory Pool implementations
        self.register_component(
            ComponentType.MEMORY_POOL,
            ImplementationType.RUST,
            "jarvis_rust_core",
            "RustAdvancedMemoryPool",
            performance_score=3.0
        )
        self.register_component(
            ComponentType.MEMORY_POOL,
            ImplementationType.PYTHON,
            "vision.memory.python_memory_pool",
            "PythonMemoryPool",
            performance_score=1.0
        )
        
    def register_component(
        self,
        component_type: ComponentType,
        impl_type: ImplementationType,
        module_path: str,
        class_name: str,
        performance_score: float = 1.0
    ):
        """Register a component implementation."""
        if component_type not in self.components:
            self.components[component_type] = {}
            
        self.components[component_type][impl_type] = ComponentImplementation(
            type=component_type,
            implementation=impl_type,
            module_path=module_path,
            class_name=class_name,
            performance_score=performance_score
        )

    def _load_canonical_rust_loader(self):
        """Load canonical Rust loader module (backend.vision.jarvis_rust_core)."""
        try:
            from . import jarvis_rust_core as canonical_loader
            return canonical_loader
        except Exception:
            try:
                import backend.vision.jarvis_rust_core as canonical_loader
                return canonical_loader
            except Exception:
                return None

    def _resolve_module(self, impl: ComponentImplementation) -> Optional[Any]:
        """
        Resolve implementation module/object.

        Rust implementations are resolved through the canonical loader to avoid
        accepting the local Python stub as a real Rust backend.
        """
        if impl.implementation == ImplementationType.RUST and impl.module_path.startswith("jarvis_rust_core"):
            canonical = self._load_canonical_rust_loader()
            if canonical is None:
                return None

            if not getattr(canonical, "RUST_AVAILABLE", False):
                return None

            rust_module = getattr(canonical, "jrc", None)
            if rust_module is None:
                return None

            if impl.module_path == "jarvis_rust_core":
                return rust_module

            target = rust_module
            for part in impl.module_path.split(".")[1:]:
                target = getattr(target, part, None)
                if target is None:
                    return None
            return target

        return importlib.import_module(impl.module_path)
        
    async def start(self):
        """Start the dynamic component loader."""
        async with self._lock:
            if self._running:
                return
                
            self._running = True
            
            # Initialize self-healer if available
            try:
                from .rust_self_healer import get_self_healer
                self.self_healer = get_self_healer()
                await self.self_healer.start()
                logger.info("Self-healer integration enabled")
            except ImportError:
                logger.warning("Self-healer not available, manual intervention required for Rust issues")
            except Exception as e:
                logger.error(f"Failed to initialize self-healer: {e}")
            
            # Initial component check
            await self._check_all_components()
            
            # Start periodic checking task
            self._check_task = asyncio.create_task(self._periodic_check())
            
            logger.info("Dynamic component loader started")
            
    async def stop(self):
        """Stop the dynamic component loader."""
        async with self._lock:
            self._running = False
            
            if self._check_task:
                self._check_task.cancel()
                try:
                    await self._check_task
                except asyncio.CancelledError:
                    pass
            
            # Stop self-healer if running
            if self.self_healer:
                try:
                    await self.self_healer.stop()
                except Exception as e:
                    logger.error(f"Error stopping self-healer: {e}")
                    
            logger.info("Dynamic component loader stopped")
            
    async def _periodic_check(self):
        """Periodically check for component availability."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                
                if self._running:
                    changes = await self._check_all_components()
                    
                    if changes:
                        logger.info(f"Component availability changed: {changes}")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic component check: {e}")
                
    async def _check_all_components(self) -> Dict[ComponentType, str]:
        """
        Check availability of all registered components.
        Returns dict of components that changed availability.
        """
        changes = {}
        
        # First check if any Rust components are unavailable
        rust_unavailable = False
        for comp_type, implementations in self.components.items():
            rust_impl = implementations.get(ImplementationType.RUST)
            if rust_impl and not rust_impl.is_available:
                rust_unavailable = True
                break
        
        # If Rust is unavailable, try self-healing (with safeguards against infinite loops)
        if rust_unavailable and self.self_healer:
            # Check if we've exceeded max attempts
            if self._self_heal_attempts >= self._max_self_heal_attempts:
                if self._last_self_heal_time is None or \
                   (datetime.now() - self._last_self_heal_time) > self._self_heal_cooldown:
                    # Reset after cooldown period
                    logger.info(f"Self-heal cooldown expired, resetting attempt counter")
                    self._self_heal_attempts = 0
                else:
                    # Still in cooldown, skip self-healing
                    cooldown_remaining = self._self_heal_cooldown - (datetime.now() - self._last_self_heal_time)
                    logger.debug(f"Skipping self-heal (max attempts reached, cooldown: {cooldown_remaining.seconds}s remaining)")
                    rust_unavailable = False  # Prevent further attempts

            if rust_unavailable and self._self_heal_attempts < self._max_self_heal_attempts:
                self._self_heal_attempts += 1
                self._last_self_heal_time = datetime.now()
                logger.info(f"Rust components unavailable, attempting self-healing (attempt {self._self_heal_attempts}/{self._max_self_heal_attempts})...")

                healed = await self.self_healer.diagnose_and_fix()
                if healed:
                    logger.info("✅ Self-healing successful, rechecking components...")
                    # Give a moment for the system to stabilize
                    await asyncio.sleep(1)
                    # Reset counter on success
                    self._self_heal_attempts = 0
                else:
                    logger.warning(f"Self-healing attempt {self._self_heal_attempts} did not resolve issues")
        
        for comp_type, implementations in self.components.items():
            for impl_type, impl in implementations.items():
                old_available = impl.is_available
                impl.is_available = await self._check_component_availability(impl)
                impl.last_checked = datetime.now()
                
                # If availability changed and this is Rust becoming available
                if old_available != impl.is_available:
                    if impl.is_available and impl_type == ImplementationType.RUST:
                        # Try to upgrade to Rust
                        if await self._try_upgrade_component(comp_type):
                            changes[comp_type] = "upgraded_to_rust"
                    elif not impl.is_available and impl_type == ImplementationType.RUST:
                        # Rust became unavailable, ensure we have Python fallback
                        if await self._ensure_fallback(comp_type):
                            changes[comp_type] = "fell_back_to_python"
                            
        return changes
        
    async def _check_component_availability(self, impl: ComponentImplementation) -> bool:
        """Check if a specific component implementation is available."""
        try:
            module = self._resolve_module(impl)
            if module is None:
                return False
            
            # Check if the class exists
            if hasattr(module, impl.class_name):
                # Try to instantiate (with minimal args)
                cls = getattr(module, impl.class_name)
                
                # For some components, we need to check if they can be instantiated
                if impl.type == ComponentType.BLOOM_FILTER:
                    # Try creating a small bloom filter
                    test_instance = cls(1.0, 7)  # 1MB, 7 hash functions
                    del test_instance
                    
                impl.error_count = 0
                return True
                
        except Exception as e:
            impl.error_count += 1
            if impl.error_count <= 3:  # Only log first few errors
                logger.debug(f"Component {impl.module_path}.{impl.class_name} not available: {e}")
                
        return False
        
    async def _try_upgrade_component(self, comp_type: ComponentType) -> bool:
        """Try to upgrade a component to Rust implementation."""
        rust_impl = self.components.get(comp_type, {}).get(ImplementationType.RUST)
        
        if not rust_impl or not rust_impl.is_available:
            return False
            
        try:
            # Load the Rust component
            instance = await self._load_component(rust_impl)
            
            if instance:
                # Store old component for cleanup
                old_component = self.active_components.get(comp_type)
                
                # Switch to Rust
                self.active_components[comp_type] = rust_impl
                rust_impl.instance = instance
                
                # Cleanup old component
                if old_component and old_component.instance:
                    await self._cleanup_component(old_component)
                    
                # Notify callbacks
                await self._notify_component_change(comp_type, rust_impl)
                
                logger.info(f"Upgraded {comp_type.value} to Rust implementation")
                return True
                
        except Exception as e:
            logger.error(f"Failed to upgrade {comp_type.value} to Rust: {e}")
            rust_impl.error_count += 1
            
        return False
        
    async def _ensure_fallback(self, comp_type: ComponentType) -> bool:
        """Ensure a Python fallback is active when Rust is unavailable."""
        python_impl = self.components.get(comp_type, {}).get(ImplementationType.PYTHON)
        
        if not python_impl:
            logger.warning(f"No Python fallback for {comp_type.value}")
            return False
            
        # Check if Python implementation is available
        if not python_impl.is_available:
            python_impl.is_available = await self._check_component_availability(python_impl)
            
        if not python_impl.is_available:
            logger.error(f"Python fallback for {comp_type.value} is also unavailable!")
            return False
            
        try:
            # Load Python component
            instance = await self._load_component(python_impl)
            
            if instance:
                # Store old component for cleanup
                old_component = self.active_components.get(comp_type)
                
                # Switch to Python
                self.active_components[comp_type] = python_impl
                python_impl.instance = instance
                
                # Cleanup old component
                if old_component and old_component.instance:
                    await self._cleanup_component(old_component)
                    
                # Notify callbacks
                await self._notify_component_change(comp_type, python_impl)
                
                logger.info(f"Fell back to Python implementation for {comp_type.value}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to fall back {comp_type.value} to Python: {e}")
            python_impl.error_count += 1
            
        return False
        
    async def _load_component(self, impl: ComponentImplementation) -> Optional[Any]:
        """Load and instantiate a component."""
        try:
            module = self._resolve_module(impl)
            if module is None:
                return None
            cls = getattr(module, impl.class_name)
            
            # Create instance with appropriate parameters
            if impl.type == ComponentType.BLOOM_FILTER:
                return cls(10.0, 7)  # 10MB, 7 hash functions
            elif impl.type == ComponentType.SLIDING_WINDOW:
                return cls(window_size=30, overlap_threshold=0.9)
            elif impl.type == ComponentType.ZERO_COPY_POOL:
                return cls(max_memory_mb=1024)
            elif impl.type == ComponentType.MEMORY_POOL:
                return cls()
            elif impl.type == ComponentType.METAL_ACCELERATOR:
                return cls()
            else:
                return cls()
                
        except Exception as e:
            logger.error(f"Failed to load component {impl.module_path}.{impl.class_name}: {e}")
            return None
            
    async def _cleanup_component(self, impl: ComponentImplementation):
        """Cleanup a component instance."""
        if not impl.instance:
            return
            
        try:
            # Call cleanup method if available
            if hasattr(impl.instance, 'cleanup'):
                if asyncio.iscoroutinefunction(impl.instance.cleanup):
                    await impl.instance.cleanup()
                else:
                    impl.instance.cleanup()
            elif hasattr(impl.instance, 'close'):
                if asyncio.iscoroutinefunction(impl.instance.close):
                    await impl.instance.close()
                else:
                    impl.instance.close()
                    
        except Exception as e:
            logger.error(f"Error cleaning up component: {e}")
            
        impl.instance = None
        
    def get_component(self, comp_type: ComponentType) -> Optional[Any]:
        """
        Get the active component instance.
        Returns None if no component is available.
        """
        active = self.active_components.get(comp_type)
        return active.instance if active else None
        
    def get_active_implementation(self, comp_type: ComponentType) -> Optional[ImplementationType]:
        """Get the active implementation type for a component."""
        active = self.active_components.get(comp_type)
        return active.implementation if active else None
        
    def register_change_callback(self, comp_type: ComponentType, callback: Callable):
        """Register a callback for component changes."""
        if comp_type not in self._component_callbacks:
            self._component_callbacks[comp_type] = []
        self._component_callbacks[comp_type].append(callback)
        
    async def _notify_component_change(self, comp_type: ComponentType, new_impl: ComponentImplementation):
        """Notify callbacks of component change."""
        callbacks = self._component_callbacks.get(comp_type, [])
        
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(comp_type, new_impl)
                else:
                    callback(comp_type, new_impl)
            except Exception as e:
                logger.error(f"Error in component change callback: {e}")
                
    def get_status(self) -> Dict[str, Any]:
        """Get current status of all components."""
        status = {
            "running": self._running,
            "check_interval": self.check_interval,
            "components": {}
        }
        
        for comp_type, implementations in self.components.items():
            comp_status = {
                "active": None,
                "implementations": {}
            }
            
            # Active implementation
            active = self.active_components.get(comp_type)
            if active:
                comp_status["active"] = {
                    "type": active.implementation.value,
                    "performance_score": active.performance_score,
                    "available": active.is_available
                }
                
            # All implementations
            for impl_type, impl in implementations.items():
                comp_status["implementations"][impl_type.value] = {
                    "available": impl.is_available,
                    "last_checked": impl.last_checked.isoformat() if impl.last_checked else None,
                    "error_count": impl.error_count,
                    "performance_score": impl.performance_score
                }
                
            status["components"][comp_type.value] = comp_status
            
        return status
        
    async def force_check(self) -> Dict[ComponentType, str]:
        """Force an immediate check of all components."""
        logger.info("Forcing component availability check...")
        return await self._check_all_components()
        
    async def prefer_implementation(self, comp_type: ComponentType, impl_type: ImplementationType) -> bool:
        """
        Manually prefer a specific implementation if available.
        Returns True if successfully switched.
        """
        impl = self.components.get(comp_type, {}).get(impl_type)
        
        if not impl:
            logger.error(f"Implementation {impl_type.value} not registered for {comp_type.value}")
            return False
            
        # Check availability
        if not impl.is_available:
            impl.is_available = await self._check_component_availability(impl)
            
        if not impl.is_available:
            logger.error(f"Implementation {impl_type.value} for {comp_type.value} is not available")
            return False
            
        # Load the component
        try:
            instance = await self._load_component(impl)
            
            if instance:
                old_component = self.active_components.get(comp_type)
                
                self.active_components[comp_type] = impl
                impl.instance = instance
                
                if old_component and old_component.instance:
                    await self._cleanup_component(old_component)
                    
                await self._notify_component_change(comp_type, impl)
                
                logger.info(f"Manually switched {comp_type.value} to {impl_type.value}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to switch to {impl_type.value}: {e}")
            
        return False


# Global instance
_component_loader: Optional[DynamicComponentLoader] = None

def get_component_loader() -> DynamicComponentLoader:
    """Get the global component loader instance."""
    global _component_loader
    if _component_loader is None:
        _component_loader = DynamicComponentLoader()
    return _component_loader

async def initialize_dynamic_components():
    """Initialize the dynamic component system."""
    loader = get_component_loader()
    await loader.start()
    return loader

# Convenience functions for getting components
def get_bloom_filter():
    """Get the active bloom filter implementation."""
    return get_component_loader().get_component(ComponentType.BLOOM_FILTER)

def get_sliding_window():
    """Get the active sliding window implementation."""
    return get_component_loader().get_component(ComponentType.SLIDING_WINDOW)

def get_memory_pool():
    """Get the active memory pool implementation."""
    return get_component_loader().get_component(ComponentType.MEMORY_POOL)

def get_zero_copy_pool():
    """Get the active zero-copy pool implementation."""
    return get_component_loader().get_component(ComponentType.ZERO_COPY_POOL)

def get_metal_accelerator():
    """Get the active Metal accelerator implementation (macOS only)."""
    if sys.platform == "darwin":
        return get_component_loader().get_component(ComponentType.METAL_ACCELERATOR)
    return None
