"""
JARVIS Core - Advanced Architecture for Scale & Memory Efficiency

LAZY LOADING: Heavy modules (task_router with nltk, jarvis_core) are imported
on-demand to prevent 3+ second startup delays from nltk/scipy imports.
"""

__all__ = [
    "ModelManager",
    "ModelTier",
    "ModelInfo",
    "MemoryController",
    "MemoryPressure",
    "MemorySnapshot",
    "TaskRouter",
    "TaskType",
    "TaskAnalysis",
    "JARVISCore",
    "JARVISAssistant",
    # v16.0: Optional Dependency Manager
    "OptionalDependencyManager",
    "get_dependency_manager",
    "is_torch_available",
    "is_coreml_available",
    "is_mlx_available",
    "is_transformers_available",
    "is_gpu_available",
    "get_ml_capabilities",
    "log_dependency_status",
]

__version__ = "2.0.0"

# =============================================================================
# LAZY IMPORT SYSTEM - Avoids 3+ second nltk/scipy import on module load
# =============================================================================
# These modules are imported on first access, not at package import time.
# This is critical for fast startup (<2s) as nltk alone takes 2.8 seconds.
# =============================================================================

_lazy_modules = {
    "ModelManager": (".model_manager", "ModelManager"),
    "ModelTier": (".model_manager", "ModelTier"),
    "ModelInfo": (".model_manager", "ModelInfo"),
    "MemoryController": (".memory_controller", "MemoryController"),
    "MemoryPressure": (".memory_controller", "MemoryPressure"),
    "MemorySnapshot": (".memory_controller", "MemorySnapshot"),
    "TaskRouter": (".task_router", "TaskRouter"),
    "TaskType": (".task_router", "TaskType"),
    "TaskAnalysis": (".task_router", "TaskAnalysis"),
    "JARVISCore": (".jarvis_core", "JARVISCore"),
    "JARVISAssistant": (".jarvis_core", "JARVISAssistant"),
    # v16.0: Optional Dependency Manager
    "OptionalDependencyManager": (".optional_dependencies", "OptionalDependencyManager"),
    "get_dependency_manager": (".optional_dependencies", "get_dependency_manager"),
    "is_torch_available": (".optional_dependencies", "is_torch_available"),
    "is_coreml_available": (".optional_dependencies", "is_coreml_available"),
    "is_mlx_available": (".optional_dependencies", "is_mlx_available"),
    "is_transformers_available": (".optional_dependencies", "is_transformers_available"),
    "is_gpu_available": (".optional_dependencies", "is_gpu_available"),
    "get_ml_capabilities": (".optional_dependencies", "get_ml_capabilities"),
    "log_dependency_status": (".optional_dependencies", "log_dependency_status"),
}

_loaded_modules = {}


def __getattr__(name: str):
    """Lazy import handler - imports modules only when accessed."""
    if name in _lazy_modules:
        if name not in _loaded_modules:
            module_path, attr_name = _lazy_modules[name]
            import importlib
            module = importlib.import_module(module_path, package=__name__)
            _loaded_modules[name] = getattr(module, attr_name)
        return _loaded_modules[name]
    raise AttributeError(f"module 'core' has no attribute '{name}'")