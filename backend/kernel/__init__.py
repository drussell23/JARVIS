"""
JARVIS Kernel Package v1.0.0
============================

Enterprise-grade modular kernel for the JARVIS AI Operating System.

This package provides a clean, modular architecture extracted from the 
monolithic unified_supervisor.py (65K+ lines). The goal is to enable:

1. CLEAN IMPORTS: Other modules can import specific functionality without
   loading the entire 65K-line monolith
2. TESTABILITY: Each module can be tested in isolation
3. MAINTAINABILITY: Clear separation of concerns
4. GRADUAL MIGRATION: Components are extracted incrementally

Package Structure:
    kernel/
    ├── __init__.py          - Package exports
    ├── config.py            - SystemKernelConfig and configuration
    ├── signals.py           - Signal handling and protection
    ├── process.py           - Process lifecycle management
    ├── health.py            - Health monitoring and readiness
    ├── ipc.py               - Inter-process communication (Unix sockets)
    ├── circuit_breaker.py   - Circuit breaker patterns
    ├── resource_managers/   - Resource management (Docker, GCP, etc.)
    │   ├── __init__.py
    │   ├── base.py
    │   ├── docker.py
    │   └── gcp.py
    └── trinity/             - Trinity cross-repo coordination
        ├── __init__.py
        ├── launcher.py
        └── coordinator.py

Usage:
    from backend.kernel import (
        SystemKernelConfig,
        get_kernel_instance,
        SignalProtector,
        ProcessManager,
    )

    # Get the kernel singleton
    kernel = get_kernel_instance()
    
    # Access configuration
    config = kernel.config
    
    # Check health
    health = await kernel.get_health()

Migration Strategy:
    1. New code should import from backend.kernel instead of unified_supervisor
    2. Existing code can continue to import from unified_supervisor (which will
       eventually become a thin facade over backend.kernel)
    3. Gradually move functionality from unified_supervisor to kernel modules

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

logger = logging.getLogger(__name__)

# Version
__version__ = "1.0.0"

# =============================================================================
# LAZY IMPORTS (avoid circular dependencies and heavy loading)
# =============================================================================

# These will be lazily imported to avoid loading the entire monolith
_kernel_instance: Optional[Any] = None
_config_class: Optional[type] = None
_signal_protector: Optional[type] = None


def get_kernel_instance():
    """
    Get the singleton kernel instance.
    
    This provides a clean interface to access the kernel without
    importing the entire unified_supervisor module.
    
    Returns:
        The JarvisSystemKernel instance, or None if not running.
    """
    global _kernel_instance
    
    # Try to get from existing kernel
    if _kernel_instance is not None:
        return _kernel_instance
    
    # Try to import from unified_supervisor (legacy path)
    try:
        from unified_supervisor import get_kernel
        _kernel_instance = get_kernel()
        return _kernel_instance
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"[kernel] Failed to get kernel instance: {e}")
    
    return None


def get_kernel_config():
    """
    Get the kernel configuration.
    
    Returns:
        SystemKernelConfig instance with current configuration.
    """
    # Try to get from running kernel
    kernel = get_kernel_instance()
    if kernel is not None and hasattr(kernel, 'config'):
        return kernel.config
    
    # Create default config
    try:
        from backend.kernel.config import SystemKernelConfig
        return SystemKernelConfig()
    except ImportError:
        # Fallback to unified_supervisor
        try:
            from unified_supervisor import SystemKernelConfig
            return SystemKernelConfig()
        except ImportError:
            return None


def get_active_cdp_port() -> Optional[int]:
    """
    Get the active Chrome CDP port.
    
    This is a convenience function used by browser automation code.
    
    Returns:
        The active CDP port, or None if not available.
    """
    # First try the modular browser_stability module
    try:
        from backend.core.browser_stability import get_active_cdp_port as _get_cdp
        return _get_cdp()
    except ImportError:
        pass
    
    # Fallback to unified_supervisor
    try:
        from unified_supervisor import get_active_cdp_port as _get_cdp
        return _get_cdp()
    except ImportError:
        pass
    
    return None


def get_stabilized_chrome_launcher():
    """
    Get the stabilized Chrome launcher for crash-resistant browser automation.
    
    Returns:
        StabilizedChromeLauncher instance.
    """
    # First try the modular browser_stability module
    try:
        from backend.core.browser_stability import get_stability_manager
        return get_stability_manager().chrome_launcher
    except ImportError:
        pass
    
    # Fallback to unified_supervisor
    try:
        from unified_supervisor import get_stabilized_chrome_launcher as _get_launcher
        return _get_launcher()
    except ImportError:
        pass
    
    return None


# =============================================================================
# MODULE EXPORTS (lazy loaded)
# =============================================================================

def __getattr__(name: str) -> Any:
    """
    Lazy attribute loading for clean imports.
    
    Allows: `from backend.kernel import SystemKernelConfig`
    without loading everything upfront.
    """
    # Configuration
    if name == "SystemKernelConfig":
        try:
            from backend.kernel.config import SystemKernelConfig
            return SystemKernelConfig
        except ImportError:
            from unified_supervisor import SystemKernelConfig
            return SystemKernelConfig
    
    # Signal handling
    if name == "SignalProtector":
        try:
            from backend.kernel.signals import SignalProtector
            return SignalProtector
        except ImportError:
            return None
    
    # Process management
    if name == "ProcessManager":
        try:
            from backend.kernel.process import ProcessManager
            return ProcessManager
        except ImportError:
            return None
    
    # Health monitoring
    if name == "HealthMonitor":
        try:
            from backend.kernel.health import HealthMonitor
            return HealthMonitor
        except ImportError:
            return None
    
    # Circuit breaker
    if name in ("CircuitBreaker", "CircuitBreakerState"):
        try:
            from backend.kernel.circuit_breaker import CircuitBreaker, CircuitBreakerState
            return CircuitBreaker if name == "CircuitBreaker" else CircuitBreakerState
        except ImportError:
            try:
                from unified_supervisor import CircuitBreaker, CircuitBreakerState
                return CircuitBreaker if name == "CircuitBreaker" else CircuitBreakerState
            except ImportError:
                return None
    
    raise AttributeError(f"module 'backend.kernel' has no attribute '{name}'")


# =============================================================================
# PUBLIC API
# =============================================================================

__all__ = [
    # Version
    "__version__",
    
    # Instance accessors
    "get_kernel_instance",
    "get_kernel_config",
    "get_active_cdp_port",
    "get_stabilized_chrome_launcher",
    
    # Classes (lazy loaded)
    "SystemKernelConfig",
    "SignalProtector",
    "ProcessManager",
    "HealthMonitor",
    "CircuitBreaker",
    "CircuitBreakerState",
]

logger.debug(f"[kernel] Package initialized (v{__version__})")
