"""
JARVIS Rust Core Python integration.

This module resolves the real Rust extension first, and only loads the local
Python stub when no extension is importable. Stub mode is explicit and
observable via `RUST_AVAILABLE`/`RUST_REASON`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import importlib
import importlib.util
import logging
import sys

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()
_STUB_DIR = _THIS_DIR / "jarvis-rust-core"
_STUB_FILE = (_STUB_DIR / "jarvis_rust_core.py").resolve()
_REQUIRED_SYMBOLS = ("RustRuntimeManager", "RustAdvancedMemoryPool", "RustImageProcessor")
_OPTIONAL_SYMBOLS = ("RustMemoryPool", "quantize_model_weights")


def _module_origin(module: Any) -> str:
    origin = getattr(module, "__file__", None)
    if origin is None:
        return "<builtin>"
    try:
        return str(Path(origin).resolve())
    except Exception:
        return str(origin)


def _is_wrapper_or_stub(module: Any) -> bool:
    """Detect modules that are NOT the real Rust native extension.

    Returns True for:
    - The Python stub (``__rust_available__ = False``)
    - This canonical loader itself (accidental self-import when
      ``backend/vision`` is in ``sys.path``)
    """
    if module is None:
        return False
    if getattr(module, "__rust_available__", None) is False:
        return True
    origin = getattr(module, "__file__", None)
    if origin is None:
        return False
    try:
        resolved = Path(origin).resolve()
        # Detect the Python stub file
        if resolved == _STUB_FILE:
            return True
        # Detect self-import of this canonical loader
        if resolved == _THIS_FILE:
            return True
    except Exception:
        pass
    return False


# Keep old name for backwards compatibility with external callers
_is_stub_module = _is_wrapper_or_stub


def _strip_interfering_paths() -> list[Tuple[int, str]]:
    """Remove sys.path entries that would shadow the real Rust extension.

    Strips both the stub directory (``jarvis-rust-core/``) and the
    canonical loader's own directory (``backend/vision/``) so that
    ``importlib.import_module("jarvis_rust_core")`` resolves to the
    installed native extension in site-packages rather than to a
    Python wrapper.
    """
    blocked = {str(_STUB_DIR.resolve()), str(_THIS_DIR.resolve())}
    removed: list[Tuple[int, str]] = []
    for idx in range(len(sys.path) - 1, -1, -1):
        candidate = sys.path[idx]
        try:
            resolved = str(Path(candidate).resolve())
        except Exception:
            resolved = candidate
        if resolved in blocked:
            removed.append((idx, sys.path.pop(idx)))
    removed.reverse()
    return removed


# Keep old name for backwards compat
_strip_stub_path = _strip_interfering_paths


def _restore_paths(entries: list[Tuple[int, str]]) -> None:
    for idx, value in entries:
        if idx < 0 or idx > len(sys.path):
            sys.path.append(value)
        else:
            sys.path.insert(idx, value)


def _required_symbols_available(module: Any) -> bool:
    return all(hasattr(module, symbol) for symbol in _REQUIRED_SYMBOLS)


def _compute_capabilities(module: Any) -> Dict[str, bool]:
    symbols = list(_REQUIRED_SYMBOLS) + list(_OPTIONAL_SYMBOLS)
    return {symbol: hasattr(module, symbol) for symbol in symbols}


def _load_real_module() -> Tuple[Optional[Any], Optional[str]]:
    existing = sys.modules.get("jarvis_rust_core")
    existing_was_stub = _is_stub_module(existing)
    if existing is not None and not existing_was_stub:
        return existing, None

    if existing_was_stub:
        sys.modules.pop("jarvis_rust_core", None)

    removed_entries = _strip_stub_path()
    try:
        module = importlib.import_module("jarvis_rust_core")
        if _is_stub_module(module):
            return None, f"Loaded stub module from {_module_origin(module)}"
        return module, None
    except ImportError as exc:
        return None, str(exc)
    finally:
        _restore_paths(removed_entries)
        if "jarvis_rust_core" not in sys.modules and existing is not None:
            sys.modules["jarvis_rust_core"] = existing


def _discover_build_artifacts() -> List[Path]:
    """Search well-known build directories for a compiled Rust extension."""
    rust_crate = _STUB_DIR  # jarvis-rust-core/
    extensions = (".abi3.so", ".so", ".dylib", ".pyd")
    candidates: List[Path] = []
    search_dirs = [
        rust_crate / "target" / "maturin",
        rust_crate / "target" / "release",
        rust_crate / "target" / "release" / "deps",
        rust_crate / "target" / "wheels",
    ]
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            name = child.name.lower()
            if not child.is_file():
                continue
            # Match native extension files (e.g. jarvis_rust_core.abi3.so, libjarvis_rust_core.dylib)
            if "jarvis_rust_core" in name and any(name.endswith(ext) for ext in extensions):
                candidates.append(child)
            # Match wheel files that can be pip-installed
            if name.startswith("jarvis_rust_core") and name.endswith(".whl"):
                candidates.append(child)
    # Sort by modification time (newest first) so we pick the freshest build
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates


def _load_from_build_artifact() -> Tuple[Optional[Any], Optional[str]]:
    """
    Attempt to load the Rust module directly from a compiled build artifact.
    This handles the case where ``maturin build`` succeeded but the wheel
    was never installed to site-packages.
    """
    artifacts = _discover_build_artifacts()
    if not artifacts:
        return None, "no build artifacts found"

    for artifact in artifacts:
        name = artifact.name.lower()

        # Wheel files: install on-the-fly via importlib
        if name.endswith(".whl"):
            try:
                import subprocess
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", str(artifact)],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    # Clear any cached stub from sys.modules
                    sys.modules.pop("jarvis_rust_core", None)
                    module = importlib.import_module("jarvis_rust_core")
                    if not _is_stub_module(module):
                        logger.info("Installed wheel and loaded Rust core from %s", artifact)
                        return module, None
            except Exception as exc:
                logger.debug("Failed to install wheel %s: %s", artifact, exc)
            continue

        # Native shared library: load via importlib.util.spec_from_file_location
        if any(name.endswith(ext) for ext in (".abi3.so", ".so", ".dylib", ".pyd")):
            try:
                spec = importlib.util.spec_from_file_location("jarvis_rust_core", str(artifact))
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules["jarvis_rust_core"] = module
                spec.loader.exec_module(module)
                if not _is_stub_module(module) and _required_symbols_available(module):
                    logger.info("Loaded Rust core directly from build artifact: %s", artifact)
                    return module, None
                # Loaded but incomplete — remove and try next
                sys.modules.pop("jarvis_rust_core", None)
            except Exception as exc:
                sys.modules.pop("jarvis_rust_core", None)
                logger.debug("Failed to load artifact %s: %s", artifact, exc)
            continue

    return None, f"tried {len(artifacts)} artifacts, none loaded successfully"


def _load_stub_module() -> Tuple[Optional[Any], Optional[str]]:
    if not _STUB_FILE.exists():
        return None, "Stub file not found"

    inserted = False
    stub_path = str(_STUB_DIR)
    if stub_path not in sys.path:
        sys.path.insert(0, stub_path)
        inserted = True

    try:
        module = importlib.import_module("jarvis_rust_core")
        return module, None
    except ImportError as exc:
        return None, str(exc)
    finally:
        if inserted and sys.path and sys.path[0] == stub_path:
            sys.path.pop(0)


def _load_module() -> Tuple[Optional[Any], bool, str, Dict[str, bool]]:
    # Tier 1: Try the standard import (site-packages or sys.path)
    module, error = _load_real_module()
    if module is not None:
        capabilities = _compute_capabilities(module)
        if _required_symbols_available(module):
            logger.info("Rust core loaded from %s", _module_origin(module))
            return module, True, "loaded_real_module", capabilities
        missing = [symbol for symbol, present in capabilities.items() if symbol in _REQUIRED_SYMBOLS and not present]
        reason = f"real module missing required symbols: {', '.join(missing)}"
        logger.warning("Rust module loaded but incompatible: %s", reason)
        return module, False, reason, capabilities

    # Tier 2: Search build directories for compiled artifacts
    artifact_module, artifact_error = _load_from_build_artifact()
    if artifact_module is not None:
        capabilities = _compute_capabilities(artifact_module)
        if _required_symbols_available(artifact_module):
            logger.info("Rust core loaded from build artifact: %s", _module_origin(artifact_module))
            return artifact_module, True, "loaded_from_build_artifact", capabilities
        missing = [symbol for symbol, present in capabilities.items() if symbol in _REQUIRED_SYMBOLS and not present]
        reason = f"build artifact missing required symbols: {', '.join(missing)}"
        logger.warning("Build artifact loaded but incompatible: %s", reason)
        return artifact_module, False, reason, capabilities

    # Tier 3: Fall back to Python stub
    stub_module, stub_error = _load_stub_module()
    if stub_module is not None:
        reason = "loaded_python_stub"
        combined_error = error or artifact_error or "unknown error"
        logger.warning("Rust extension unavailable (%s); using Python stub from %s", combined_error, _module_origin(stub_module))
        return stub_module, False, reason, _compute_capabilities(stub_module)

    reason = f"rust_unavailable: {error or artifact_error or stub_error or 'unknown import error'}"
    logger.warning("Rust core not available: %s", reason)
    return None, False, reason, {}


jrc, RUST_AVAILABLE, RUST_REASON, RUST_CAPABILITIES = _load_module()
RUST_MODULE_ORIGIN = _module_origin(jrc) if jrc is not None else "unloaded"

# Global references to Rust components
_runtime_manager = None
_memory_pool = None
_advanced_pool = None
_image_processor = None


def initialize_rust_runtime(config: Dict[str, Any]) -> None:
    """
    Initialize Rust runtime components that are present in the loaded module.
    """
    global _runtime_manager, _memory_pool, _advanced_pool, _image_processor

    _runtime_manager = None
    _memory_pool = None
    _advanced_pool = None
    _image_processor = None

    if not RUST_AVAILABLE or jrc is None:
        logger.warning("Rust runtime initialization skipped (%s)", RUST_REASON)
        return

    initialized_components = []

    try:
        if hasattr(jrc, "RustRuntimeManager"):
            _runtime_manager = jrc.RustRuntimeManager(
                worker_threads=config.get("worker_threads", 4),
                enable_cpu_affinity=config.get("enable_cpu_affinity", True),
            )
            initialized_components.append("runtime_manager")

        if hasattr(jrc, "RustMemoryPool"):
            _memory_pool = jrc.RustMemoryPool()
            initialized_components.append("memory_pool")

        if hasattr(jrc, "RustAdvancedMemoryPool"):
            _advanced_pool = jrc.RustAdvancedMemoryPool()
            initialized_components.append("advanced_memory_pool")
            if _memory_pool is None:
                _memory_pool = _advanced_pool

        if hasattr(jrc, "RustImageProcessor"):
            _image_processor = jrc.RustImageProcessor()
            initialized_components.append("image_processor")

        if not initialized_components:
            logger.warning("Rust module loaded but no expected runtime components were initialized")
            return

        logger.info("Rust runtime initialized: %s", ", ".join(initialized_components))

        if _runtime_manager is not None and hasattr(_runtime_manager, "stats"):
            try:
                logger.info("Rust runtime stats: %s", _runtime_manager.stats())
            except Exception as exc:
                logger.debug("Rust runtime stats unavailable: %s", exc)

    except Exception as exc:
        logger.error("Failed to initialize Rust runtime components: %s", exc)
        raise


def get_rust_runtime():
    """Get the global Rust runtime manager."""
    return _runtime_manager


def get_rust_memory_pool():
    """Get the global Rust memory pool."""
    return _memory_pool


def get_rust_advanced_pool():
    """Get the global advanced memory pool with leak detection."""
    return _advanced_pool


def get_rust_image_processor():
    """Get the global Rust image processor."""
    return _image_processor


def is_rust_available() -> bool:
    """Check whether Rust module is loaded and at least one component is initialized."""
    return RUST_AVAILABLE and any(
        component is not None
        for component in (_runtime_manager, _memory_pool, _advanced_pool, _image_processor)
    )


def process_image_with_rust(image_array):
    """
    Process an image with Rust acceleration when available.
    """
    if _image_processor is not None and hasattr(_image_processor, "process_numpy_image"):
        try:
            return _image_processor.process_numpy_image(image_array)
        except Exception as exc:
            logger.warning("Rust image processing failed: %s", exc)

    return image_array


def allocate_rust_buffer(size: int):
    """
    Allocate a buffer using Rust memory pool if available.
    """
    if _memory_pool is not None and hasattr(_memory_pool, "allocate"):
        try:
            return _memory_pool.allocate(size)
        except Exception as exc:
            logger.warning("Rust buffer allocation failed: %s", exc)

    return bytearray(size)


def quantize_weights_with_rust(weights):
    """
    Quantize model weights using Rust if available.
    """
    if (
        not RUST_AVAILABLE
        or jrc is None
        or not RUST_CAPABILITIES.get("quantize_model_weights", False)
    ):
        try:
            return weights.astype("int8")
        except Exception:
            return weights

    try:
        return jrc.quantize_model_weights(weights)
    except Exception as exc:
        logger.warning("Rust quantization failed: %s", exc)
        try:
            return weights.astype("int8")
        except Exception:
            return weights
