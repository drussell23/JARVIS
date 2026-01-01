"""
JARVIS Backend Module - v8.0 Hyper-Speed Edition
=================================================

Core backend services for the JARVIS AI Assistant.

v8.0 LAZY LOADING: All heavy modules are JIT-loaded on first access,
reducing import time from 300ms+ to <50ms.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Only import types for type checking, not at runtime
if TYPE_CHECKING:
    from .voice_unlock import (
        get_voice_unlock_system,
        initialize_voice_unlock,
        cleanup_voice_unlock,
        get_voice_unlock_status,
    )
    from .process_cleanup_manager import ProcessCleanupManager
    from .resource_manager import get_resource_manager

logger = logging.getLogger(__name__)

# Version info
__version__ = "13.4.0"
__author__ = "JARVIS Team"

# =============================================================================
# LAZY LOADING SYSTEM v8.0 - JIT Module Loading
# =============================================================================
# These modules are loaded on first access, not at package import time.
# This reduces backend/__init__.py import time from 300ms+ to <50ms.
# =============================================================================

_lazy_modules = {
    # Voice Unlock module (heavy - loads ML models)
    "get_voice_unlock_system": (".voice_unlock", "get_voice_unlock_system"),
    "initialize_voice_unlock": (".voice_unlock", "initialize_voice_unlock"),
    "cleanup_voice_unlock": (".voice_unlock", "cleanup_voice_unlock"),
    "get_voice_unlock_status": (".voice_unlock", "get_voice_unlock_status"),
    "check_voice_dependencies": (".voice_unlock", "check_dependencies"),

    # Process Cleanup Manager
    "ProcessCleanupManager": (".process_cleanup_manager", "ProcessCleanupManager"),

    # Resource Manager
    "get_resource_manager": (".resource_manager", "get_resource_manager"),

    # Vision (lazy vision engine)
    "lazy_vision_engine": (".vision", "lazy_vision_engine"),
}

_loaded_modules = {}
_availability_cache = {}


def __getattr__(name: str):
    """
    Lazy import handler - imports modules only when accessed.

    This is the core of the v8.0 Hyper-Speed optimization.
    Instead of loading all modules at import time, we defer
    until the exact moment the module is needed.
    """
    if name in _lazy_modules:
        if name not in _loaded_modules:
            module_path, attr_name = _lazy_modules[name]
            try:
                import importlib
                module = importlib.import_module(module_path, package=__name__)
                _loaded_modules[name] = getattr(module, attr_name)
                logger.debug(f"JIT loaded: {name}")
            except ImportError as e:
                logger.debug(f"Module not available: {name} ({e})")
                _loaded_modules[name] = None
        return _loaded_modules[name]

    # Handle availability flags
    if name == "VOICE_UNLOCK_AVAILABLE":
        return _check_availability("voice_unlock")
    if name == "VISION_AVAILABLE":
        return _check_availability("vision")
    if name == "CLEANUP_AVAILABLE":
        return _check_availability("process_cleanup_manager")
    if name == "RESOURCE_MANAGER_AVAILABLE":
        return _check_availability("resource_manager")

    raise AttributeError(f"module 'backend' has no attribute '{name}'")


def _check_availability(module_name: str) -> bool:
    """Check if a module is available without fully loading it."""
    if module_name in _availability_cache:
        return _availability_cache[module_name]

    try:
        import importlib.util
        spec = importlib.util.find_spec(f".{module_name}", package=__name__)
        available = spec is not None
    except (ImportError, ModuleNotFoundError):
        available = False

    _availability_cache[module_name] = available
    return available


# Global resource manager instance (lazy)
_resource_manager = None


def get_backend_status():
    """Get status of all backend modules"""
    status = {
        'version': __version__,
        'modules': {
            'voice_unlock': VOICE_UNLOCK_AVAILABLE,
            'vision': VISION_AVAILABLE,
            'cleanup': CLEANUP_AVAILABLE,
            'resource_manager': RESOURCE_MANAGER_AVAILABLE
        }
    }
    
    if VOICE_UNLOCK_AVAILABLE:
        status['voice_unlock'] = get_voice_unlock_status()
        
    if RESOURCE_MANAGER_AVAILABLE and _resource_manager:
        status['resources'] = _resource_manager.get_status()
        
    return status


async def initialize_backend():
    """Initialize all backend services with 30% memory target (4.8GB on 16GB systems)"""
    global _resource_manager
    logger.info("Initializing JARVIS backend services...")
    logger.info("Memory target: 30% of system RAM - ultra-aggressive optimization enabled")
    
    # Initialize resource manager first (CRITICAL for 30% memory target)
    if RESOURCE_MANAGER_AVAILABLE:
        try:
            _resource_manager = get_resource_manager()
            logger.info("Resource manager initialized - enforcing 30% memory limit")
            
            # Get initial memory status
            status = _resource_manager.get_status()
            logger.info(f"Initial memory: {status['memory_percent']:.1f}% of system RAM")
            
            # Prepare for proximity + voice unlock if we're using it
            if VOICE_UNLOCK_AVAILABLE:
                logger.info("Preparing memory allocation for Proximity + Voice Unlock")
                _resource_manager.request_voice_unlock_resources()
        except Exception as e:
            logger.error(f"Failed to initialize resource manager: {e}")
    
    # Initialize voice unlock if available (with proximity detection)
    if VOICE_UNLOCK_AVAILABLE:
        try:
            await initialize_voice_unlock()
            logger.info("Voice Unlock initialized with proximity detection")
            logger.info("Apple Watch proximity: 3m unlock, 10m auto-lock")
        except Exception as e:
            logger.error(f"Failed to initialize Voice Unlock: {e}")
            
    logger.info("Backend services initialized with memory optimization")


async def cleanup_backend():
    """Cleanup all backend services"""
    logger.info("Cleaning up JARVIS backend services...")
    
    # Cleanup voice unlock if available
    if VOICE_UNLOCK_AVAILABLE:
        try:
            await cleanup_voice_unlock()
        except Exception as e:
            logger.error(f"Error cleaning up Voice Unlock: {e}")
            
    logger.info("Backend services cleaned up")


__all__ = [
    'get_backend_status',
    'initialize_backend',
    'cleanup_backend',
    'VOICE_UNLOCK_AVAILABLE',
    'VISION_AVAILABLE',
    'CLEANUP_AVAILABLE'
]