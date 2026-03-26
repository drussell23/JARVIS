"""
DeferredToolRegistry — Lazy/deferred tool loading.

Tools are registered with their module path and factory name but are not
imported until first use.  This keeps startup fast and memory low even when
the catalogue grows to 1000+ entries.

Keyword search is intentionally simple (substring match against name +
description) so it runs in O(n) with no external dependencies.

Env vars:
  JARVIS_TOOL_REGISTRY_EAGER — "1" to import all tools at registration time
                                 (useful for CI validation)
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EAGER_MODE = os.environ.get("JARVIS_TOOL_REGISTRY_EAGER", "0") == "1"


# ---------------------------------------------------------------------------
# DeferredToolEntry
# ---------------------------------------------------------------------------

@dataclass
class DeferredToolEntry:
    """Metadata for a lazily loaded tool.

    Parameters
    ----------
    name:
        Unique identifier (e.g. ``"read_file"``).
    description:
        Short human-readable description used for keyword search.
    module_path:
        Dotted Python module path to ``importlib.import_module()``.
    factory_name:
        Attribute name inside the module — can be a class or a callable.
    loaded:
        Whether the tool has been materialised yet.
    instance:
        Cached instance after first ``load()``.
    """
    name: str
    description: str
    module_path: str
    factory_name: str
    loaded: bool = False
    instance: Optional[Any] = None


# ---------------------------------------------------------------------------
# Built-in tool declarations
# ---------------------------------------------------------------------------

_DEFAULT_TOOLS: List[tuple] = [
    (
        "read_file",
        "Read file contents",
        "backend.core.ouroboros.governance.tool_executor",
        "ReadFileTool",
    ),
    (
        "search_code",
        "Regex search across files",
        "backend.core.ouroboros.governance.tool_executor",
        "SearchCodeTool",
    ),
    (
        "bash",
        "Shell command execution",
        "backend.core.ouroboros.governance.tools.bash_tool",
        "BashTool",
    ),
    (
        "web_search",
        "DuckDuckGo search",
        "backend.core.ouroboros.governance.tools.web_tool",
        "WebTool",
    ),
    (
        "web_fetch",
        "HTTP GET",
        "backend.core.ouroboros.governance.tools.web_tool",
        "WebFetchTool",
    ),
    (
        "run_tests",
        "Pytest execution",
        "backend.core.ouroboros.governance.test_runner",
        "TestRunner",
    ),
    (
        "lsp_check",
        "Type checking",
        "backend.core.ouroboros.governance.lsp_checker",
        "LSPTypeChecker",
    ),
]


# ---------------------------------------------------------------------------
# DeferredToolRegistry
# ---------------------------------------------------------------------------

class DeferredToolRegistry:
    """A thread-safe registry of tools that are imported on first use.

    Example::

        reg = get_tool_registry()
        reg.register("my_tool", "Does something", "my.module", "MyTool")
        results = reg.search("search")
        tool = reg.load("search_code")
    """

    def __init__(self) -> None:
        self._entries: Dict[str, DeferredToolEntry] = {}
        self._lock = threading.Lock()

    # -- registration -------------------------------------------------------

    def register(
        self,
        name: str,
        description: str,
        module_path: str,
        factory_name: str,
    ) -> None:
        """Add a deferred tool to the registry.

        If *name* is already registered the entry is silently overwritten
        (allows hot-reload / plugin overrides).
        """
        entry = DeferredToolEntry(
            name=name,
            description=description,
            module_path=module_path,
            factory_name=factory_name,
        )
        with self._lock:
            self._entries[name] = entry
        logger.debug("[ToolRegistry] Registered tool %r (%s.%s)", name, module_path, factory_name)

        if _EAGER_MODE:
            self.load(name)

    # -- search -------------------------------------------------------------

    def search(self, query: str, max_results: int = 5) -> List[DeferredToolEntry]:
        """Return entries whose name or description contains *query* (case-insensitive).

        Results are ordered: exact name match first, then substring matches
        sorted alphabetically, capped at *max_results*.
        """
        q = query.lower()
        exact: List[DeferredToolEntry] = []
        partial: List[DeferredToolEntry] = []

        with self._lock:
            for entry in self._entries.values():
                haystack = f"{entry.name} {entry.description}".lower()
                if entry.name.lower() == q:
                    exact.append(entry)
                elif q in haystack:
                    partial.append(entry)

        partial.sort(key=lambda e: e.name)
        combined = exact + partial
        return combined[:max_results]

    # -- loading ------------------------------------------------------------

    def load(self, name: str) -> Any:
        """Import the tool module and instantiate the factory, caching the result.

        Raises ``KeyError`` if *name* is not registered.
        Raises ``ImportError`` / ``AttributeError`` if the module or factory
        cannot be resolved.
        """
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                raise KeyError(f"Tool {name!r} is not registered")
            if entry.loaded and entry.instance is not None:
                return entry.instance

        # Import outside lock — module init may be slow
        logger.info("[ToolRegistry] Loading tool %r from %s.%s", name, entry.module_path, entry.factory_name)
        try:
            module = importlib.import_module(entry.module_path)
            factory = getattr(module, entry.factory_name)
            instance = factory() if callable(factory) else factory
        except Exception:
            logger.exception("[ToolRegistry] Failed to load tool %r", name)
            raise

        with self._lock:
            entry.instance = instance
            entry.loaded = True

        return instance

    # -- introspection ------------------------------------------------------

    def is_loaded(self, name: str) -> bool:
        with self._lock:
            entry = self._entries.get(name)
            return entry.loaded if entry is not None else False

    def list_available(self) -> List[str]:
        """All registered tool names, sorted alphabetically."""
        with self._lock:
            return sorted(self._entries.keys())

    def list_loaded(self) -> List[str]:
        """Only the tool names that have been materialised."""
        with self._lock:
            return sorted(n for n, e in self._entries.items() if e.loaded)

    def stats(self) -> Dict[str, int]:
        """Return ``{"total": N, "loaded": M, "unloaded": N-M}``."""
        with self._lock:
            total = len(self._entries)
            loaded = sum(1 for e in self._entries.values() if e.loaded)
        return {"total": total, "loaded": loaded, "unloaded": total - loaded}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[DeferredToolRegistry] = None
_singleton_lock = threading.Lock()


def get_tool_registry() -> DeferredToolRegistry:
    """Return the process-wide DeferredToolRegistry, creating it on first call.

    Built-in tools from ``_DEFAULT_TOOLS`` are pre-registered automatically.
    """
    global _singleton
    if _singleton is not None:
        return _singleton

    with _singleton_lock:
        if _singleton is not None:
            return _singleton

        registry = DeferredToolRegistry()
        for name, desc, mod, factory in _DEFAULT_TOOLS:
            registry.register(name, desc, mod, factory)

        _singleton = registry
        logger.info(
            "[ToolRegistry] Initialised with %d built-in tools",
            len(_DEFAULT_TOOLS),
        )
        return _singleton
