"""Canonical runtime module resolution for the backend app entrypoint."""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType
from typing import Any, Optional


_MAIN_ALIASES = ("backend.main", "main")
_MAIN_LOOKUP_ORDER = ("backend.main", "main", "__main__")


def _is_backend_main_module(module: Optional[ModuleType]) -> bool:
    """Return True when *module* is the backend main entrypoint."""
    if module is None:
        return False
    module_file = os.path.abspath(getattr(module, "__file__", "") or "")
    if not module_file:
        return False
    normalized = module_file.replace("\\", "/")
    return normalized.endswith("/backend/main.py")


def _register_main_aliases(module: ModuleType) -> ModuleType:
    """Make both legacy and canonical names point at the same module."""
    for alias in _MAIN_ALIASES:
        existing = sys.modules.get(alias)
        if existing is None or existing is module or not _is_backend_main_module(existing):
            sys.modules[alias] = module
    return module


def get_main_module(*, strict: bool = False) -> Optional[ModuleType]:
    """Resolve the canonical backend main module without split identity."""
    for module_name in _MAIN_LOOKUP_ORDER:
        module = sys.modules.get(module_name)
        if _is_backend_main_module(module):
            return _register_main_aliases(module)

    for module_name in _MAIN_ALIASES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if _is_backend_main_module(module):
            return _register_main_aliases(module)

    if strict:
        raise ImportError(
            "Backend main module unavailable "
            "(tried backend.main, main, __main__ with backend/main.py identity)"
        )
    return None


def get_main_attr(name: str, *, default: Any = None, strict: bool = False) -> Any:
    """Return an attribute from the canonical backend main module."""
    module = get_main_module(strict=strict)
    if module is None:
        return default
    return getattr(module, name, default)


def get_main_app(*, strict: bool = False) -> Any:
    """Return the FastAPI app object from the canonical backend main module."""
    module = get_main_module(strict=strict)
    if module is None:
        return None
    app = getattr(module, "app", None)
    if app is None and strict:
        raise RuntimeError("Canonical backend main module does not expose app")
    return app
