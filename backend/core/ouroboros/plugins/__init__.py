"""Ouroboros plugin system — operator-authored extensions.

Subsystems pluggable in V1:

  * ``sensor``  — new intake sources (TodoScanner-shaped)
  * ``gate``    — new SemanticGuardian pattern detectors
  * ``repl``    — new REPL slash commands

Entry points:

  * :class:`PluginRegistry` — discovery, loading, lifecycle
  * :class:`PluginManifest` — validated manifest dataclass
  * :mod:`backend.core.ouroboros.plugins.plugin_base` — abstract bases
    operators subclass to author plugins.

Authority invariant: plugin code is operator-authored + third-party.
Fail-closed defaults (``JARVIS_PLUGINS_ENABLED=0``), per-type sub-gates,
mutation gate for sensors that submit intents, error-isolation so one
broken plugin never blocks others. Plugin-originated intents route
through every existing governance gate — CLASSIFY, risk engine,
SemanticGuardian, tier floor — so the system does not grant plugins
any escape from the standard safety discipline.
"""
from __future__ import annotations

from backend.core.ouroboros.plugins.plugin_base import (
    GatePlugin,
    Plugin,
    PluginContext,
    PluginType,
    ReplCommandPlugin,
    SensorPlugin,
)
from backend.core.ouroboros.plugins.plugin_manifest import (
    PluginManifest,
    PluginManifestError,
    parse_manifest,
)
from backend.core.ouroboros.plugins.plugin_registry import (
    PluginLoadOutcome,
    PluginRegistry,
    plugins_enabled,
    plugins_path,
    register_default_plugins,
)

__all__ = (
    "GatePlugin",
    "Plugin",
    "PluginContext",
    "PluginLoadOutcome",
    "PluginManifest",
    "PluginManifestError",
    "PluginRegistry",
    "PluginType",
    "ReplCommandPlugin",
    "SensorPlugin",
    "parse_manifest",
    "plugins_enabled",
    "plugins_path",
    "register_default_plugins",
)
