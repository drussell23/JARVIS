"""Plugin manifest schema + parser.

A plugin directory contains exactly one ``manifest.yaml`` OR
``manifest.json`` (YAML preferred, JSON fallback when PyYAML isn't
importable). The manifest declares the plugin's identity + entry point
in a narrow, validated schema. Free-form fields are intentionally
absent — anything the registry doesn't understand is ignored, and
anything it REQUIRES must be explicit.

Required fields:

  * ``name``          — snake_case identifier, unique per host
  * ``type``          — "sensor" | "gate" | "repl" (matches PluginType)
  * ``entry_module``  — Python module path relative to plugin_dir
  * ``entry_class``   — class name within entry_module that subclasses
                        the appropriate Plugin base

Optional fields:

  * ``version``       — semver string, for operator bookkeeping
  * ``description``   — one-line summary
  * ``author``        — free-form string
  * ``tick_interval_s`` — SensorPlugin only; default 60. How often the
                        registry calls ``on_tick``.

Unknown fields are dropped with a DEBUG log. Any required-field
failure raises :class:`PluginManifestError` with a precise, operator-
readable reason.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.Plugins.Manifest")

_ALLOWED_TYPES = frozenset({"sensor", "gate", "repl"})
_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


class PluginManifestError(ValueError):
    """Raised when a manifest fails schema validation. Message names
    the exact field that failed so operators can grep their logs."""


@dataclass(frozen=True)
class PluginManifest:
    """Validated manifest record."""

    name: str
    type: str                       # "sensor" | "gate" | "repl"
    entry_module: str               # e.g. "handler" (resolved relative to plugin_dir)
    entry_class: str                # class name inside entry_module
    plugin_dir: Path                # absolute path
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    tick_interval_s: float = 60.0   # sensor only
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        """Namespace prefix for logs + sensor/command names."""
        return f"plugin.{self.name}"


def _require(raw: Dict[str, Any], key: str, *, label: str) -> Any:
    val = raw.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise PluginManifestError(
            f"{label}: missing required field '{key}'"
        )
    return val


def _valid_name(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    return all(c in _NAME_CHARS for c in name.lower())


def parse_manifest(plugin_dir: Path) -> PluginManifest:
    """Load + validate the manifest at ``plugin_dir``.

    Looks for ``manifest.yaml`` first, then ``manifest.json``. Raises
    :class:`PluginManifestError` on any validation failure.
    """
    plugin_dir = Path(plugin_dir).resolve()
    if not plugin_dir.is_dir():
        raise PluginManifestError(
            f"plugin_dir is not a directory: {plugin_dir}"
        )

    yaml_path = plugin_dir / "manifest.yaml"
    yml_path = plugin_dir / "manifest.yml"
    json_path = plugin_dir / "manifest.json"

    raw: Optional[Dict[str, Any]] = None

    # Prefer YAML, fall back to JSON. YAML is optional — PyYAML may not
    # be installed everywhere. Plugins that want broader portability
    # should ship JSON.
    for yaml_candidate in (yaml_path, yml_path):
        if yaml_candidate.is_file():
            try:
                import yaml  # type: ignore[import-not-found]
                raw = yaml.safe_load(yaml_candidate.read_text(encoding="utf-8"))
                break
            except ImportError:
                logger.debug(
                    "[PluginManifest] PyYAML not importable — skipping %s",
                    yaml_candidate.name,
                )
            except Exception as exc:  # noqa: BLE001
                raise PluginManifestError(
                    f"{yaml_candidate.name}: YAML parse failed: {exc}"
                ) from exc

    if raw is None and json_path.is_file():
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PluginManifestError(
                f"manifest.json: invalid JSON: {exc}"
            ) from exc

    if raw is None:
        raise PluginManifestError(
            f"{plugin_dir.name}: no manifest.yaml or manifest.json"
        )
    if not isinstance(raw, dict):
        raise PluginManifestError(
            f"{plugin_dir.name}: manifest must be a top-level object/dict"
        )

    label = f"{plugin_dir.name}/manifest"

    name = str(_require(raw, "name", label=label)).strip()
    if not _valid_name(name):
        raise PluginManifestError(
            f"{label}: name {name!r} is not valid "
            "(snake_case, ≤64 chars, alphanumeric + underscore/hyphen only)"
        )

    type_raw = str(_require(raw, "type", label=label)).strip().lower()
    if type_raw not in _ALLOWED_TYPES:
        raise PluginManifestError(
            f"{label}: type {type_raw!r} not in "
            f"{{{', '.join(sorted(_ALLOWED_TYPES))}}}"
        )

    entry_module = str(_require(raw, "entry_module", label=label)).strip()
    entry_class = str(_require(raw, "entry_class", label=label)).strip()

    version = str(raw.get("version", "0.0.0")).strip() or "0.0.0"
    description = str(raw.get("description", "")).strip()
    author = str(raw.get("author", "")).strip()

    # tick_interval_s applies to sensor plugins only — parsed loosely
    # so non-sensor manifests can carry the field without harm.
    try:
        tick_interval_s = float(raw.get("tick_interval_s", 60.0))
    except (TypeError, ValueError):
        tick_interval_s = 60.0
    if tick_interval_s <= 0:
        tick_interval_s = 60.0

    # Keep any other declared fields in ``extra`` for plugin code to
    # read if it wants, without contaminating the primary schema.
    known = {
        "name", "type", "entry_module", "entry_class", "version",
        "description", "author", "tick_interval_s",
    }
    extra = {k: v for k, v in raw.items() if k not in known}

    return PluginManifest(
        name=name,
        type=type_raw,
        entry_module=entry_module,
        entry_class=entry_class,
        plugin_dir=plugin_dir,
        version=version,
        description=description,
        author=author,
        tick_interval_s=tick_interval_s,
        extra=extra,
    )


def discover_manifests(roots: Tuple[Path, ...]) -> Tuple[PluginManifest, ...]:
    """Walk each root dir looking for plugin directories (those
    containing a manifest file). Returns parsed manifests sorted by
    name for deterministic load order.

    Errors during parsing are logged at DEBUG + skipped — a broken
    plugin manifest must not break discovery of others.
    """
    found: Dict[str, PluginManifest] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            has_manifest = any(
                (child / m).is_file()
                for m in ("manifest.yaml", "manifest.yml", "manifest.json")
            )
            if not has_manifest:
                continue
            try:
                manifest = parse_manifest(child)
            except PluginManifestError as exc:
                logger.warning(
                    "[PluginManifest] skipped %s: %s", child.name, exc,
                )
                continue
            if manifest.name in found:
                logger.warning(
                    "[PluginManifest] duplicate name %r — keeping first "
                    "(from %s, dropping %s)",
                    manifest.name,
                    found[manifest.name].plugin_dir, manifest.plugin_dir,
                )
                continue
            found[manifest.name] = manifest
    return tuple(sorted(found.values(), key=lambda m: m.name))
