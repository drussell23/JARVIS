"""
Plugin Manifest — Declarative packaging for Ouroboros extensions.

Gap 6: No distribution mechanism for Ouroboros capabilities.
This module defines the manifest format for packaging and installing
sensors, providers, tools, and hooks as distributable plugins.

Manifest format (YAML):
  name: my-plugin
  version: 1.0.0
  description: Custom sensor for monitoring X
  author: derek
  components:
    sensors:
      - module: my_plugin.sensors.custom_sensor
        class: CustomSensor
        config:
          poll_interval_s: 3600
    tools:
      - module: my_plugin.tools.custom_tool
        name: custom_search
        description: Search X for Y
    hooks:
      - event: PostToolUse
        matcher: Edit
        command: ./scripts/lint.sh

Boundary Principle:
  Deterministic: YAML parsing, module loading, component registration.
  Agentic: Plugin content (sensor logic, tool behavior) is arbitrary code.
"""
from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(
    os.environ.get(
        "JARVIS_PLUGIN_DIR",
        str(Path.home() / ".jarvis" / "plugins"),
    )
)


@dataclass
class SensorSpec:
    """Specification for a plugin-provided sensor."""
    module: str                # Python import path
    class_name: str            # Class name within the module
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """Specification for a plugin-provided tool."""
    module: str
    name: str
    description: str
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookSpec:
    """Specification for a plugin-provided hook."""
    event: str                 # PreToolUse, PostToolUse, etc.
    matcher: str               # Tool name pattern
    command: str               # Shell command to execute


@dataclass
class PluginManifest:
    """Parsed plugin manifest."""
    name: str
    version: str
    description: str
    author: str = ""
    sensors: List[SensorSpec] = field(default_factory=list)
    tools: List[ToolSpec] = field(default_factory=list)
    hooks: List[HookSpec] = field(default_factory=list)
    enabled: bool = True


class PluginManager:
    """Manages Ouroboros plugins — load, register, and unload.

    Scans the plugin directory for manifest files, parses them,
    and registers components with the governance pipeline.
    """

    def __init__(self, plugin_dir: Path = _PLUGIN_DIR) -> None:
        self._plugin_dir = plugin_dir
        self._plugins: Dict[str, PluginManifest] = {}

    def discover_plugins(self) -> List[PluginManifest]:
        """Scan plugin directory for manifest files."""
        if not self._plugin_dir.exists():
            return []

        manifests = []
        for manifest_path in self._plugin_dir.glob("*/plugin.yaml"):
            try:
                manifest = self._parse_manifest(manifest_path)
                if manifest:
                    manifests.append(manifest)
                    self._plugins[manifest.name] = manifest
            except Exception as exc:
                logger.warning(
                    "[PluginManager] Failed to parse %s: %s",
                    manifest_path, exc,
                )

        # Also check for plugin.yml
        for manifest_path in self._plugin_dir.glob("*/plugin.yml"):
            if manifest_path.with_suffix(".yaml").exists():
                continue  # Already processed
            try:
                manifest = self._parse_manifest(manifest_path)
                if manifest:
                    manifests.append(manifest)
                    self._plugins[manifest.name] = manifest
            except Exception:
                pass

        logger.info(
            "[PluginManager] Discovered %d plugins in %s",
            len(manifests), self._plugin_dir,
        )
        return manifests

    def load_sensors(
        self, router: Any, repo: str = "jarvis",
    ) -> List[Any]:
        """Load and instantiate all plugin-provided sensors."""
        sensors = []
        for plugin in self._plugins.values():
            if not plugin.enabled:
                continue
            for spec in plugin.sensors:
                try:
                    mod = importlib.import_module(spec.module)
                    cls = getattr(mod, spec.class_name)
                    sensor = cls(
                        repo=repo,
                        router=router,
                        **spec.config,
                    )
                    sensors.append(sensor)
                    logger.info(
                        "[PluginManager] Loaded sensor %s.%s from plugin %s",
                        spec.module, spec.class_name, plugin.name,
                    )
                except Exception as exc:
                    logger.warning(
                        "[PluginManager] Failed to load sensor %s: %s",
                        spec.class_name, exc,
                    )
        return sensors

    def load_tools(self) -> Dict[str, Any]:
        """Load plugin-provided tools for the tool executor."""
        tools = {}
        for plugin in self._plugins.values():
            if not plugin.enabled:
                continue
            for spec in plugin.tools:
                try:
                    mod = importlib.import_module(spec.module)
                    tool_fn = getattr(mod, spec.name)
                    tools[spec.name] = {
                        "function": tool_fn,
                        "description": spec.description,
                        "plugin": plugin.name,
                        "config": spec.config,
                    }
                    logger.info(
                        "[PluginManager] Loaded tool %s from plugin %s",
                        spec.name, plugin.name,
                    )
                except Exception as exc:
                    logger.warning(
                        "[PluginManager] Failed to load tool %s: %s",
                        spec.name, exc,
                    )
        return tools

    def get_hooks(self) -> List[HookSpec]:
        """Get all plugin-provided hooks."""
        hooks = []
        for plugin in self._plugins.values():
            if not plugin.enabled:
                continue
            hooks.extend(plugin.hooks)
        return hooks

    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all discovered plugins with metadata."""
        return [
            {
                "name": p.name,
                "version": p.version,
                "description": p.description,
                "author": p.author,
                "enabled": p.enabled,
                "sensors": len(p.sensors),
                "tools": len(p.tools),
                "hooks": len(p.hooks),
            }
            for p in self._plugins.values()
        ]

    def _parse_manifest(self, path: Path) -> Optional[PluginManifest]:
        """Parse a plugin.yaml manifest file."""
        try:
            import yaml
        except ImportError:
            # Fallback to ruamel.yaml
            try:
                from ruamel.yaml import YAML
                yaml_parser = YAML()
                data = yaml_parser.load(path.read_text())
            except ImportError:
                logger.debug("[PluginManager] No YAML parser available")
                return None
        else:
            data = yaml.safe_load(path.read_text())

        if not isinstance(data, dict):
            return None

        sensors = []
        for s in data.get("components", {}).get("sensors", []):
            sensors.append(SensorSpec(
                module=s["module"],
                class_name=s.get("class", s.get("class_name", "")),
                config=s.get("config", {}),
            ))

        tools = []
        for t in data.get("components", {}).get("tools", []):
            tools.append(ToolSpec(
                module=t["module"],
                name=t["name"],
                description=t.get("description", ""),
                config=t.get("config", {}),
            ))

        hooks = []
        for h in data.get("components", {}).get("hooks", []):
            hooks.append(HookSpec(
                event=h["event"],
                matcher=h.get("matcher", ""),
                command=h["command"],
            ))

        return PluginManifest(
            name=data.get("name", path.parent.name),
            version=data.get("version", "0.0.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            sensors=sensors,
            tools=tools,
            hooks=hooks,
            enabled=data.get("enabled", True),
        )
