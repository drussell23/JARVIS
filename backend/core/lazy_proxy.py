"""
JARVIS Hyper-Speed Startup: LazyProxy Metaprogramming Engine
=============================================================

Advanced metaprogramming wrapper that defers the cost of heavy imports
(Torch, Transformers, TensorFlow, etc.) until the exact millisecond of use.

This is the core of the "Just-In-Time Intelligence" startup architecture.
Instead of loading everything at startup, we create lightweight proxy objects
that only load the real module when actually accessed.

v1.0.0 - Hyper-Speed Startup Edition

Features:
- Zero-cost proxy creation (no imports until first use)
- Transparent attribute forwarding (works like the real object)
- Thread-safe lazy loading with locking
- Memory-efficient (proxy ~100 bytes vs heavy module MBs)
- Supports module-level and class-level lazy loading
- Automatic logging of JIT load events for profiling
- Preload hints for background loading during idle time

Usage:
    # Instead of:
    from backend.heavy_module import HeavyClass
    obj = HeavyClass()  # Blocks for 2-3 seconds

    # Use:
    from backend.core.lazy_proxy import LazyProxy
    obj = LazyProxy("backend.heavy_module", "HeavyClass")
    # Returns immediately! Only loads when you actually use it:
    obj.do_something()  # Now it loads (JIT)

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │  LazyProxy (lightweight wrapper ~100 bytes)                 │
    │  ├── _module_name: str (stored, not loaded)                 │
    │  ├── _class_name: Optional[str] (for class instantiation)   │
    │  ├── _instance: None (until first use)                      │
    │  └── _load_lock: threading.Lock (thread-safe loading)       │
    └─────────────────────────────────────────────────────────────┘
                              │
                              │ __getattr__ called
                              ▼
    ┌─────────────────────────────────────────────────────────────┐
    │  _load() - Just-In-Time Loading                             │
    │  1. Acquire lock (thread-safe)                              │
    │  2. importlib.import_module(module_name)                    │
    │  3. getattr(module, class_name) if class specified          │
    │  4. Instantiate class if needed                             │
    │  5. Cache in _instance                                      │
    │  6. Return real attribute                                   │
    └─────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 1.0.0
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Type, Union

# Get logger - but don't force logging module to fully initialize
logger = logging.getLogger("jarvis.core.lazy_proxy")

# Track all lazy proxies for diagnostics
_LAZY_PROXY_REGISTRY: Dict[str, "LazyProxy"] = {}
_LOAD_TIMES: Dict[str, float] = {}
_LOAD_ORDER: List[str] = []


class LazyProxy:
    """
    Advanced Metaprogramming Wrapper for Just-In-Time Module Loading.

    Creates a lightweight proxy object that defers actual module import
    until the first attribute access. This dramatically speeds up startup
    by avoiding upfront loading of heavy dependencies.

    Thread-safe: Uses locking to prevent race conditions during loading.

    Args:
        module_name: Full module path (e.g., "backend.neural_mesh.agents.visual_monitor_agent")
        class_name: Optional class to instantiate from the module
        init_args: Optional args to pass to class __init__
        init_kwargs: Optional kwargs to pass to class __init__
        preload_hint: If True, can be preloaded in background during idle
        singleton: If True, reuse same instance across all proxies

    Examples:
        # Lazy module import
        torch = LazyProxy("torch")
        # torch is NOT loaded yet - just a ~100 byte proxy

        # Later when you actually use it:
        tensor = torch.zeros(10)  # NOW torch loads (JIT)

        # Lazy class instantiation
        agent = LazyProxy(
            "backend.neural_mesh.agents.visual_monitor_agent",
            "VisualMonitorAgent",
            init_kwargs={"config": my_config}
        )
        # VisualMonitorAgent is NOT loaded yet

        # Later when you use it:
        await agent.start()  # NOW the class loads and instantiates
    """

    # Class-level registry for singletons
    _singletons: Dict[str, Any] = {}
    _singleton_lock = threading.Lock()

    # Slots for memory efficiency (no __dict__)
    __slots__ = (
        '_module_name',
        '_class_name',
        '_init_args',
        '_init_kwargs',
        '_instance',
        '_load_lock',
        '_loaded',
        '_preload_hint',
        '_singleton',
        '_load_time',
        '_proxy_id',
    )

    def __init__(
        self,
        module_name: str,
        class_name: Optional[str] = None,
        init_args: Optional[tuple] = None,
        init_kwargs: Optional[Dict[str, Any]] = None,
        preload_hint: bool = False,
        singleton: bool = False,
    ):
        # Use object.__setattr__ to bypass our __setattr__ override
        object.__setattr__(self, '_module_name', module_name)
        object.__setattr__(self, '_class_name', class_name)
        object.__setattr__(self, '_init_args', init_args or ())
        object.__setattr__(self, '_init_kwargs', init_kwargs or {})
        object.__setattr__(self, '_instance', None)
        object.__setattr__(self, '_load_lock', threading.Lock())
        object.__setattr__(self, '_loaded', False)
        object.__setattr__(self, '_preload_hint', preload_hint)
        object.__setattr__(self, '_singleton', singleton)
        object.__setattr__(self, '_load_time', 0.0)

        # Generate unique proxy ID
        proxy_id = f"{module_name}"
        if class_name:
            proxy_id += f".{class_name}"
        object.__setattr__(self, '_proxy_id', proxy_id)

        # Register in global registry
        _LAZY_PROXY_REGISTRY[proxy_id] = self

    def _load(self) -> Any:
        """
        Perform the actual Just-In-Time loading.

        Thread-safe: Uses double-checked locking pattern.

        Returns:
            The loaded module or instantiated class
        """
        # Fast path - already loaded
        if self._loaded:
            return self._instance

        with self._load_lock:
            # Double-check after acquiring lock
            if self._loaded:
                return self._instance

            start_time = time.perf_counter()

            try:
                # Check singleton cache first
                if self._singleton:
                    with LazyProxy._singleton_lock:
                        if self._proxy_id in LazyProxy._singletons:
                            instance = LazyProxy._singletons[self._proxy_id]
                            object.__setattr__(self, '_instance', instance)
                            object.__setattr__(self, '_loaded', True)
                            return instance

                # Log the JIT load event
                if self._class_name:
                    logger.info(
                        f"⚡ [JIT] Loading: {self._module_name}.{self._class_name}"
                    )
                else:
                    logger.info(f"⚡ [JIT] Loading module: {self._module_name}")

                # Import the module
                module = importlib.import_module(self._module_name)

                if self._class_name:
                    # Get the class from the module
                    cls = getattr(module, self._class_name)

                    # Instantiate the class
                    instance = cls(*self._init_args, **self._init_kwargs)
                else:
                    # Just return the module
                    instance = module

                # Store in singleton cache if needed
                if self._singleton:
                    with LazyProxy._singleton_lock:
                        LazyProxy._singletons[self._proxy_id] = instance

                # Cache the instance
                object.__setattr__(self, '_instance', instance)
                object.__setattr__(self, '_loaded', True)

                # Record load time
                load_time = time.perf_counter() - start_time
                object.__setattr__(self, '_load_time', load_time)
                _LOAD_TIMES[self._proxy_id] = load_time
                _LOAD_ORDER.append(self._proxy_id)

                logger.info(
                    f"✅ [JIT] Loaded {self._proxy_id} in {load_time*1000:.1f}ms"
                )

                return instance

            except Exception as e:
                logger.error(f"❌ [JIT] Failed to load {self._proxy_id}: {e}")
                raise

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access to the loaded instance."""
        # Load on first attribute access
        instance = self._load()
        return getattr(instance, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Forward attribute setting to the loaded instance."""
        if name.startswith('_'):
            # Internal attributes - use object's setattr
            object.__setattr__(self, name, value)
        else:
            # Forward to loaded instance
            instance = self._load()
            setattr(instance, name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Forward calls to the loaded instance."""
        instance = self._load()
        return instance(*args, **kwargs)

    def __repr__(self) -> str:
        """Return a representation showing proxy status."""
        status = "loaded" if self._loaded else "pending"
        if self._class_name:
            return f"<LazyProxy({self._module_name}.{self._class_name}) [{status}]>"
        return f"<LazyProxy({self._module_name}) [{status}]>"

    def __bool__(self) -> bool:
        """Proxies are always truthy (they represent something)."""
        return True

    @property
    def is_loaded(self) -> bool:
        """Check if the proxy has been loaded."""
        return self._loaded

    @property
    def load_time_ms(self) -> float:
        """Get load time in milliseconds."""
        return self._load_time * 1000


class LazyModuleLoader:
    """
    PEP 562 compatible lazy module loader for entire packages.

    Use this in __init__.py files to make all submodules lazy:

    # backend/heavy_package/__init__.py
    from backend.core.lazy_proxy import LazyModuleLoader

    _loader = LazyModuleLoader(__name__, {
        "heavy_model": "HeavyModel",
        "heavy_processor": "HeavyProcessor",
    })

    def __getattr__(name):
        return _loader.get(name)
    """

    __slots__ = ('_package_name', '_exports', '_cache')

    def __init__(self, package_name: str, exports: Dict[str, str]):
        """
        Args:
            package_name: The package name (use __name__)
            exports: Mapping of attribute names to class names within submodules
        """
        self._package_name = package_name
        self._exports = exports
        self._cache: Dict[str, Any] = {}

    def get(self, name: str) -> Any:
        """Get an attribute, loading lazily if needed."""
        if name in self._cache:
            return self._cache[name]

        if name not in self._exports:
            raise AttributeError(
                f"module '{self._package_name}' has no attribute '{name}'"
            )

        class_name = self._exports[name]
        module_path = f"{self._package_name}.{name}"

        logger.info(f"⚡ [JIT] Lazy loading: {module_path}.{class_name}")

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)

        self._cache[name] = cls
        return cls


def lazy_import(
    module_name: str,
    class_name: Optional[str] = None,
    **kwargs: Any
) -> LazyProxy:
    """
    Convenience function to create a LazyProxy.

    Args:
        module_name: Full module path
        class_name: Optional class to instantiate
        **kwargs: Additional args passed to LazyProxy

    Returns:
        LazyProxy instance
    """
    return LazyProxy(module_name, class_name, **kwargs)


async def preload_proxies(
    proxies: Optional[List[LazyProxy]] = None,
    concurrency: int = 4,
) -> Dict[str, float]:
    """
    Preload multiple lazy proxies in parallel during idle time.

    Useful for warming up critical components before user interaction.

    Args:
        proxies: List of proxies to preload (default: all with preload_hint=True)
        concurrency: Max parallel loads

    Returns:
        Dict mapping proxy_id to load time in ms
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    if proxies is None:
        # Get all proxies marked for preload
        proxies = [
            p for p in _LAZY_PROXY_REGISTRY.values()
            if p._preload_hint and not p._loaded
        ]

    if not proxies:
        return {}

    results: Dict[str, float] = {}

    def load_proxy(proxy: LazyProxy) -> Tuple[str, float]:
        try:
            proxy._load()
            return (proxy._proxy_id, proxy._load_time * 1000)
        except Exception as e:
            logger.error(f"Preload failed for {proxy._proxy_id}: {e}")
            return (proxy._proxy_id, -1)

    # Use thread pool for parallel loading
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        loop = asyncio.get_event_loop()
        futures = [
            loop.run_in_executor(executor, load_proxy, proxy)
            for proxy in proxies
        ]
        completed = await asyncio.gather(*futures, return_exceptions=True)

    for result in completed:
        if isinstance(result, tuple):
            proxy_id, load_time = result
            results[proxy_id] = load_time

    return results


def get_lazy_proxy_stats() -> Dict[str, Any]:
    """
    Get statistics about lazy proxy usage.

    Returns:
        Dict with:
        - total_proxies: Number of registered proxies
        - loaded_proxies: Number that have been loaded
        - pending_proxies: Number still pending
        - total_load_time_ms: Sum of all load times
        - load_order: Order in which proxies were loaded
        - slowest_loads: Top 5 slowest loads
    """
    total = len(_LAZY_PROXY_REGISTRY)
    loaded = sum(1 for p in _LAZY_PROXY_REGISTRY.values() if p._loaded)
    pending = total - loaded

    total_time = sum(_LOAD_TIMES.values()) * 1000

    # Get slowest loads
    sorted_times = sorted(
        _LOAD_TIMES.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]
    slowest = {k: v * 1000 for k, v in sorted_times}

    return {
        "total_proxies": total,
        "loaded_proxies": loaded,
        "pending_proxies": pending,
        "total_load_time_ms": total_time,
        "load_order": _LOAD_ORDER.copy(),
        "slowest_loads": slowest,
    }


def reset_lazy_proxy_stats() -> None:
    """Reset all lazy proxy statistics (for testing)."""
    global _LOAD_TIMES, _LOAD_ORDER
    _LOAD_TIMES = {}
    _LOAD_ORDER = []


# =============================================================================
# Pre-built Lazy Proxies for Common Heavy Modules
# =============================================================================
# These can be imported directly for convenience

def get_lazy_torch() -> LazyProxy:
    """Get a lazy proxy for PyTorch (typically 2-3s to load)."""
    return LazyProxy("torch", preload_hint=True)


def get_lazy_transformers() -> LazyProxy:
    """Get a lazy proxy for Hugging Face Transformers (typically 3-5s to load)."""
    return LazyProxy("transformers", preload_hint=True)


def get_lazy_numpy() -> LazyProxy:
    """Get a lazy proxy for NumPy (typically 0.5-1s to load)."""
    return LazyProxy("numpy", preload_hint=True)


def get_lazy_tensorflow() -> LazyProxy:
    """Get a lazy proxy for TensorFlow (typically 3-8s to load)."""
    return LazyProxy("tensorflow", preload_hint=True)


# =============================================================================
# __slots__ Optimized Base Classes for High-Frequency Objects
# =============================================================================

class SlottedDataClass:
    """
    Base class for high-frequency data objects that need memory efficiency.

    Using __slots__ instead of __dict__ reduces memory by 40-50% and
    speeds up attribute access.

    Subclasses should define their own __slots__:

    class FrameData(SlottedDataClass):
        __slots__ = ('id', 'timestamp', 'pixels', 'metadata')
    """
    __slots__ = ()

    def __repr__(self) -> str:
        # Collect all slot values from class hierarchy
        slots = []
        for cls in type(self).__mro__:
            if hasattr(cls, '__slots__'):
                slots.extend(cls.__slots__)

        attrs = []
        for slot in slots:
            if slot and hasattr(self, slot):
                value = getattr(self, slot)
                if isinstance(value, str) and len(value) > 50:
                    value = value[:47] + "..."
                attrs.append(f"{slot}={value!r}")

        return f"{type(self).__name__}({', '.join(attrs)})"


# =============================================================================
# Example Slotted Classes for JARVIS
# =============================================================================

class SlottedMessage(SlottedDataClass):
    """Memory-efficient message class for high-frequency messaging."""
    __slots__ = ('id', 'timestamp', 'content', 'sender', 'metadata')

    def __init__(
        self,
        id: str,
        content: str,
        sender: str = "system",
        metadata: Optional[Dict] = None
    ):
        self.id = id
        self.timestamp = time.time()
        self.content = content
        self.sender = sender
        self.metadata = metadata or {}


class SlottedFrame(SlottedDataClass):
    """Memory-efficient frame class for video processing."""
    __slots__ = ('id', 'timestamp', 'width', 'height', 'data', 'format')

    def __init__(
        self,
        id: int,
        width: int,
        height: int,
        data: bytes,
        format: str = "rgb24"
    ):
        self.id = id
        self.timestamp = time.time()
        self.width = width
        self.height = height
        self.data = data
        self.format = format


class SlottedEvent(SlottedDataClass):
    """Memory-efficient event class for event-driven architecture."""
    __slots__ = ('event_type', 'timestamp', 'source', 'payload', 'priority')

    def __init__(
        self,
        event_type: str,
        source: str,
        payload: Any = None,
        priority: int = 0
    ):
        self.event_type = event_type
        self.timestamp = time.time()
        self.source = source
        self.payload = payload
        self.priority = priority
