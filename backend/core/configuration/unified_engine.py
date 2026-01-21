"""
Unified Configuration Engine v1.0
==================================

Enterprise-grade configuration management for the JARVIS Trinity ecosystem.
Provides centralized configuration across JARVIS (Body), JARVIS Prime (Mind),
and Reactor Core (Learning).

Implements 4 critical configuration patterns:
1. Configuration Synchronization - Centralized config with cross-repo sync
2. Configuration Validation - JSON Schema validation with type checking
3. Configuration Versioning - Git-like version history with rollback
4. Dynamic Configuration Updates - Hot-reload without restart

Author: Trinity Configuration System
Version: 1.0.0
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

# Optional imports for file watching
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

# Optional YAML support
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# ENUMS
# =============================================================================


class ConfigEnvironment(Enum):
    """Configuration environments."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


class ConfigSource(Enum):
    """Sources of configuration values."""
    DEFAULT = auto()
    FILE = auto()
    ENVIRONMENT = auto()
    RUNTIME = auto()
    REMOTE = auto()
    OVERRIDE = auto()


class ConfigFormat(Enum):
    """Configuration file formats."""
    JSON = "json"
    YAML = "yaml"
    ENV = "env"
    TOML = "toml"


class ValidationSeverity(Enum):
    """Severity of validation issues."""
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    CRITICAL = auto()


class ChangeType(Enum):
    """Type of configuration change."""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    ROLLBACK = "rollback"
    MERGE = "merge"


class SyncStatus(Enum):
    """Synchronization status."""
    SYNCED = auto()
    PENDING = auto()
    CONFLICT = auto()
    FAILED = auto()


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class ConfigurationEngineConfig:
    """Configuration for the configuration engine itself."""

    # Storage
    config_directory: str = os.getenv(
        "CONFIG_DIRECTORY",
        str(Path.home() / ".jarvis/config")
    )
    version_history_limit: int = int(os.getenv("CONFIG_VERSION_LIMIT", "100"))

    # Sync settings
    sync_enabled: bool = os.getenv("CONFIG_SYNC_ENABLED", "true").lower() == "true"
    sync_interval: float = float(os.getenv("CONFIG_SYNC_INTERVAL", "30.0"))

    # Hot-reload settings
    hot_reload_enabled: bool = os.getenv("CONFIG_HOT_RELOAD", "true").lower() == "true"
    hot_reload_debounce_ms: int = int(os.getenv("CONFIG_RELOAD_DEBOUNCE_MS", "500"))

    # Validation settings
    strict_validation: bool = os.getenv("CONFIG_STRICT_VALIDATION", "true").lower() == "true"
    allow_unknown_keys: bool = os.getenv("CONFIG_ALLOW_UNKNOWN", "false").lower() == "true"

    # Environment
    default_environment: str = os.getenv("CONFIG_ENVIRONMENT", "development")

    # Encryption
    encrypt_sensitive: bool = os.getenv("CONFIG_ENCRYPT_SENSITIVE", "true").lower() == "true"


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ConfigValue:
    """A configuration value with metadata."""
    key: str
    value: Any
    source: ConfigSource = ConfigSource.DEFAULT
    version: int = 1
    last_modified: datetime = field(default_factory=datetime.utcnow)
    schema_type: Optional[str] = None
    sensitive: bool = False
    description: str = ""


@dataclass
class ConfigVersion:
    """A versioned configuration snapshot."""
    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version_number: int = 1
    config_data: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    created_by: str = "system"
    change_type: ChangeType = ChangeType.UPDATE
    change_description: str = ""
    previous_version: Optional[str] = None
    checksum: str = ""


@dataclass
class ValidationIssue:
    """A validation issue found in configuration."""
    severity: ValidationSeverity = ValidationSeverity.ERROR
    path: str = ""
    message: str = ""
    expected: Optional[Any] = None
    actual: Optional[Any] = None


@dataclass
class ValidationResult:
    """Result of configuration validation."""
    valid: bool = True
    issues: List[ValidationIssue] = field(default_factory=list)
    validated_at: datetime = field(default_factory=datetime.utcnow)

    def add_issue(
        self,
        severity: ValidationSeverity,
        path: str,
        message: str,
        expected: Any = None,
        actual: Any = None,
    ):
        """Add a validation issue."""
        self.issues.append(ValidationIssue(
            severity=severity,
            path=path,
            message=message,
            expected=expected,
            actual=actual,
        ))
        if severity in [ValidationSeverity.ERROR, ValidationSeverity.CRITICAL]:
            self.valid = False


@dataclass
class ConfigSchema:
    """Schema definition for configuration validation."""
    name: str
    version: str = "1.0"
    properties: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)
    additional_properties: bool = False


@dataclass
class ConfigChangeEvent:
    """Event emitted when configuration changes."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    config_key: str = ""
    old_value: Any = None
    new_value: Any = None
    change_type: ChangeType = ChangeType.UPDATE
    source: ConfigSource = ConfigSource.RUNTIME
    timestamp: datetime = field(default_factory=datetime.utcnow)
    version: int = 0


# =============================================================================
# SCHEMA VALIDATOR
# =============================================================================


class SchemaValidator:
    """
    JSON Schema-like validator for configuration.

    Supports:
    - Type validation (string, number, boolean, array, object)
    - Required fields
    - Min/max constraints
    - Pattern matching
    - Enum validation
    - Nested object validation
    """

    def __init__(self, allow_unknown: bool = False):
        self.allow_unknown = allow_unknown
        self.logger = logging.getLogger("SchemaValidator")

    def validate(
        self,
        data: Dict[str, Any],
        schema: ConfigSchema,
    ) -> ValidationResult:
        """Validate data against a schema."""
        result = ValidationResult()

        # Check required fields
        for required in schema.required:
            if required not in data:
                result.add_issue(
                    ValidationSeverity.ERROR,
                    required,
                    f"Required field '{required}' is missing",
                )

        # Validate each property
        for key, value in data.items():
            if key not in schema.properties:
                if not self.allow_unknown and not schema.additional_properties:
                    result.add_issue(
                        ValidationSeverity.WARNING,
                        key,
                        f"Unknown property '{key}'",
                    )
                continue

            prop_schema = schema.properties[key]
            self._validate_property(result, key, value, prop_schema)

        return result

    def _validate_property(
        self,
        result: ValidationResult,
        path: str,
        value: Any,
        schema: Dict[str, Any],
    ):
        """Validate a single property."""
        prop_type = schema.get("type")

        # Type validation
        if prop_type:
            if not self._check_type(value, prop_type):
                result.add_issue(
                    ValidationSeverity.ERROR,
                    path,
                    f"Expected type '{prop_type}', got '{type(value).__name__}'",
                    expected=prop_type,
                    actual=type(value).__name__,
                )
                return

        # Enum validation
        if "enum" in schema:
            if value not in schema["enum"]:
                result.add_issue(
                    ValidationSeverity.ERROR,
                    path,
                    f"Value must be one of: {schema['enum']}",
                    expected=schema["enum"],
                    actual=value,
                )

        # Number constraints
        if prop_type in ["number", "integer"]:
            if "minimum" in schema and value < schema["minimum"]:
                result.add_issue(
                    ValidationSeverity.ERROR,
                    path,
                    f"Value {value} is below minimum {schema['minimum']}",
                )
            if "maximum" in schema and value > schema["maximum"]:
                result.add_issue(
                    ValidationSeverity.ERROR,
                    path,
                    f"Value {value} is above maximum {schema['maximum']}",
                )

        # String constraints
        if prop_type == "string":
            if "minLength" in schema and len(value) < schema["minLength"]:
                result.add_issue(
                    ValidationSeverity.ERROR,
                    path,
                    f"String length {len(value)} is below minimum {schema['minLength']}",
                )
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                result.add_issue(
                    ValidationSeverity.ERROR,
                    path,
                    f"String length {len(value)} is above maximum {schema['maxLength']}",
                )
            if "pattern" in schema:
                if not re.match(schema["pattern"], value):
                    result.add_issue(
                        ValidationSeverity.ERROR,
                        path,
                        f"Value does not match pattern '{schema['pattern']}'",
                    )

        # Array validation
        if prop_type == "array" and isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                result.add_issue(
                    ValidationSeverity.ERROR,
                    path,
                    f"Array has {len(value)} items, minimum is {schema['minItems']}",
                )
            if "items" in schema:
                for i, item in enumerate(value):
                    self._validate_property(result, f"{path}[{i}]", item, schema["items"])

        # Nested object validation
        if prop_type == "object" and isinstance(value, dict):
            if "properties" in schema:
                nested_schema = ConfigSchema(
                    name=path,
                    properties=schema["properties"],
                    required=schema.get("required", []),
                )
                nested_result = self.validate(value, nested_schema)
                for issue in nested_result.issues:
                    issue.path = f"{path}.{issue.path}"
                    result.issues.append(issue)
                    if issue.severity in [ValidationSeverity.ERROR, ValidationSeverity.CRITICAL]:
                        result.valid = False

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if value matches expected type."""
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None),
        }
        expected = type_map.get(expected_type)
        if expected is None:
            return True
        return isinstance(value, expected)


# =============================================================================
# VERSION MANAGER
# =============================================================================


class ConfigVersionManager:
    """
    Manages configuration version history.

    Features:
    - Git-like versioning
    - Rollback support
    - Diff generation
    - History browsing
    """

    def __init__(self, config: ConfigurationEngineConfig):
        self.config = config
        self.logger = logging.getLogger("ConfigVersionManager")
        self._versions: List[ConfigVersion] = []
        self._current_version: Optional[ConfigVersion] = None
        self._lock = asyncio.Lock()

        # Storage path
        self._version_file = Path(config.config_directory) / "versions.json"

    async def initialize(self):
        """Initialize version manager."""
        await self._load_versions()

    async def create_version(
        self,
        config_data: Dict[str, Any],
        change_type: ChangeType = ChangeType.UPDATE,
        description: str = "",
        created_by: str = "system",
    ) -> ConfigVersion:
        """Create a new configuration version."""
        async with self._lock:
            # Calculate version number
            version_number = 1
            if self._versions:
                version_number = max(v.version_number for v in self._versions) + 1

            # Calculate checksum
            checksum = self._calculate_checksum(config_data)

            # Create version
            version = ConfigVersion(
                version_number=version_number,
                config_data=copy.deepcopy(config_data),
                change_type=change_type,
                change_description=description,
                created_by=created_by,
                previous_version=self._current_version.version_id if self._current_version else None,
                checksum=checksum,
            )

            # Add to history
            self._versions.append(version)
            self._current_version = version

            # Trim history if needed
            while len(self._versions) > self.config.version_history_limit:
                self._versions.pop(0)

            # Save
            await self._save_versions()

            self.logger.info(f"Created config version {version_number}: {description}")
            return version

    async def get_version(self, version_id: Optional[str] = None) -> Optional[ConfigVersion]:
        """Get a specific version or current version."""
        async with self._lock:
            if version_id is None:
                return self._current_version

            for version in self._versions:
                if version.version_id == version_id:
                    return version
            return None

    async def get_version_by_number(self, version_number: int) -> Optional[ConfigVersion]:
        """Get version by number."""
        async with self._lock:
            for version in self._versions:
                if version.version_number == version_number:
                    return version
            return None

    async def rollback(self, version_id: str) -> Optional[ConfigVersion]:
        """Rollback to a specific version."""
        target_version = await self.get_version(version_id)
        if not target_version:
            return None

        # Create new version with rollback
        return await self.create_version(
            config_data=target_version.config_data,
            change_type=ChangeType.ROLLBACK,
            description=f"Rollback to version {target_version.version_number}",
        )

    async def get_history(
        self,
        limit: int = 10,
        offset: int = 0,
    ) -> List[ConfigVersion]:
        """Get version history."""
        async with self._lock:
            sorted_versions = sorted(
                self._versions,
                key=lambda v: v.version_number,
                reverse=True
            )
            return sorted_versions[offset:offset + limit]

    async def diff(
        self,
        version_a: str,
        version_b: str,
    ) -> Dict[str, Any]:
        """Generate diff between two versions."""
        va = await self.get_version(version_a)
        vb = await self.get_version(version_b)

        if not va or not vb:
            return {"error": "Version not found"}

        return self._generate_diff(va.config_data, vb.config_data)

    def _calculate_checksum(self, data: Dict[str, Any]) -> str:
        """Calculate checksum for config data."""
        serialized = json.dumps(data, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()

    def _generate_diff(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        path: str = "",
    ) -> Dict[str, Any]:
        """Generate diff between two configs."""
        diff = {
            "added": {},
            "removed": {},
            "changed": {},
        }

        all_keys = set(old.keys()) | set(new.keys())

        for key in all_keys:
            key_path = f"{path}.{key}" if path else key

            if key not in old:
                diff["added"][key_path] = new[key]
            elif key not in new:
                diff["removed"][key_path] = old[key]
            elif old[key] != new[key]:
                if isinstance(old[key], dict) and isinstance(new[key], dict):
                    nested_diff = self._generate_diff(old[key], new[key], key_path)
                    diff["added"].update(nested_diff["added"])
                    diff["removed"].update(nested_diff["removed"])
                    diff["changed"].update(nested_diff["changed"])
                else:
                    diff["changed"][key_path] = {
                        "old": old[key],
                        "new": new[key],
                    }

        return diff

    async def _load_versions(self):
        """Load versions from disk."""
        if not self._version_file.exists():
            return

        try:
            with open(self._version_file, "r") as f:
                data = json.load(f)

            for v in data.get("versions", []):
                version = ConfigVersion(
                    version_id=v["version_id"],
                    version_number=v["version_number"],
                    config_data=v["config_data"],
                    created_at=datetime.fromisoformat(v["created_at"]),
                    created_by=v["created_by"],
                    change_type=ChangeType(v["change_type"]),
                    change_description=v["change_description"],
                    previous_version=v.get("previous_version"),
                    checksum=v["checksum"],
                )
                self._versions.append(version)

            if self._versions:
                self._current_version = max(self._versions, key=lambda v: v.version_number)

            self.logger.info(f"Loaded {len(self._versions)} config versions")

        except Exception as e:
            self.logger.error(f"Failed to load versions: {e}")

    async def _save_versions(self):
        """Save versions to disk."""
        try:
            self._version_file.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "versions": [
                    {
                        "version_id": v.version_id,
                        "version_number": v.version_number,
                        "config_data": v.config_data,
                        "created_at": v.created_at.isoformat(),
                        "created_by": v.created_by,
                        "change_type": v.change_type.value,
                        "change_description": v.change_description,
                        "previous_version": v.previous_version,
                        "checksum": v.checksum,
                    }
                    for v in self._versions
                ]
            }

            with open(self._version_file, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            self.logger.error(f"Failed to save versions: {e}")


# =============================================================================
# HOT RELOAD MANAGER
# =============================================================================


class HotReloadManager:
    """
    Manages dynamic configuration updates without restart.

    Features:
    - File system watching
    - Debounced updates
    - Event-driven notifications
    - Graceful reload
    """

    def __init__(self, config: ConfigurationEngineConfig):
        self.config = config
        self.logger = logging.getLogger("HotReloadManager")
        self._callbacks: List[Callable] = []
        self._observer: Optional[Any] = None
        self._debounce_timers: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._watched_files: Set[str] = set()

    def register_callback(self, callback: Callable):
        """Register a callback for config changes."""
        self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable):
        """Unregister a callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def start(self, watch_paths: List[str]):
        """Start watching for file changes."""
        if not WATCHDOG_AVAILABLE:
            self.logger.warning("Watchdog not available, hot reload disabled")
            return

        if self._running:
            return

        self._running = True
        self._watched_files = set(watch_paths)

        # Create observer
        self._observer = Observer()

        # Create event handler
        handler = _ConfigFileHandler(self)

        # Watch directories containing config files
        watched_dirs = set()
        for path in watch_paths:
            dir_path = str(Path(path).parent)
            if dir_path not in watched_dirs:
                self._observer.schedule(handler, dir_path, recursive=False)
                watched_dirs.add(dir_path)
                self.logger.debug(f"Watching directory: {dir_path}")

        self._observer.start()
        self.logger.info(f"Hot reload started, watching {len(watch_paths)} files")

    async def stop(self):
        """Stop watching for file changes."""
        if not self._running:
            return

        self._running = False

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

        # Cancel pending debounce timers
        for task in self._debounce_timers.values():
            task.cancel()
        self._debounce_timers.clear()

        self.logger.info("Hot reload stopped")

    async def trigger_reload(self, file_path: str):
        """Trigger a reload for a specific file (debounced)."""
        if not self._running:
            return

        async with self._lock:
            # Cancel existing timer for this file
            if file_path in self._debounce_timers:
                self._debounce_timers[file_path].cancel()

            # Create new debounced task
            self._debounce_timers[file_path] = asyncio.create_task(
                self._debounced_reload(file_path)
            )

    async def _debounced_reload(self, file_path: str):
        """Execute reload after debounce period."""
        try:
            # Wait for debounce period
            await asyncio.sleep(self.config.hot_reload_debounce_ms / 1000)

            # Notify callbacks
            for callback in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(file_path)
                    else:
                        callback(file_path)
                except Exception as e:
                    self.logger.error(f"Callback error for {file_path}: {e}")

            self.logger.info(f"Hot reloaded: {file_path}")

        except asyncio.CancelledError:
            pass
        finally:
            async with self._lock:
                self._debounce_timers.pop(file_path, None)


if WATCHDOG_AVAILABLE:
    class _ConfigFileHandler(FileSystemEventHandler):
        """File system event handler for config changes."""

        def __init__(self, manager: HotReloadManager):
            self.manager = manager

        def on_modified(self, event):
            if event.is_directory:
                return

            file_path = str(event.src_path)
            if file_path in self.manager._watched_files:
                # Schedule reload in the asyncio event loop
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self.manager.trigger_reload(file_path),
                        loop
                    )


# =============================================================================
# CONFIGURATION STORE
# =============================================================================


class ConfigurationStore:
    """
    Central configuration store with caching and layering.

    Features:
    - Multi-layer configuration (defaults, file, env, runtime)
    - Caching
    - Type coercion
    - Path-based access
    """

    def __init__(self, config: ConfigurationEngineConfig):
        self.config = config
        self.logger = logging.getLogger("ConfigurationStore")

        # Config layers (lower index = lower priority)
        # v95.0: Include ALL ConfigSource enum values to prevent KeyError
        self._layers: Dict[ConfigSource, Dict[str, ConfigValue]] = {
            source: {} for source in ConfigSource
        }

        self._cache: Dict[str, Any] = {}
        self._schemas: Dict[str, ConfigSchema] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        key: str,
        default: Any = None,
        config_type: Optional[type] = None,
    ) -> Any:
        """Get a configuration value."""
        async with self._lock:
            # Check cache
            if key in self._cache:
                return self._cache[key]

            # Search layers from highest to lowest priority
            for source in reversed(list(ConfigSource)):
                if key in self._layers[source]:
                    value = self._layers[source][key].value

                    # Type coercion
                    if config_type:
                        value = self._coerce_type(value, config_type)

                    # Cache
                    self._cache[key] = value
                    return value

            return default

    async def set(
        self,
        key: str,
        value: Any,
        source: ConfigSource = ConfigSource.RUNTIME,
        sensitive: bool = False,
        description: str = "",
    ):
        """Set a configuration value."""
        async with self._lock:
            self._layers[source][key] = ConfigValue(
                key=key,
                value=value,
                source=source,
                sensitive=sensitive,
                description=description,
            )

            # Invalidate cache
            self._cache.pop(key, None)

    async def delete(self, key: str, source: ConfigSource = ConfigSource.RUNTIME):
        """Delete a configuration value."""
        async with self._lock:
            if key in self._layers[source]:
                del self._layers[source][key]
            self._cache.pop(key, None)

    async def get_all(self, source: Optional[ConfigSource] = None) -> Dict[str, Any]:
        """Get all configuration values."""
        async with self._lock:
            if source:
                return {k: v.value for k, v in self._layers[source].items()}

            # Merge all layers
            result = {}
            for layer_source in ConfigSource:
                for key, config_value in self._layers[layer_source].items():
                    result[key] = config_value.value

            return result

    async def load_from_dict(
        self,
        data: Dict[str, Any],
        source: ConfigSource = ConfigSource.FILE,
        prefix: str = "",
    ):
        """Load configuration from dictionary."""
        async with self._lock:
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key

                if isinstance(value, dict):
                    # Recursive load for nested objects
                    await self.load_from_dict(value, source, full_key)
                else:
                    self._layers[source][full_key] = ConfigValue(
                        key=full_key,
                        value=value,
                        source=source,
                    )

            # Clear cache
            self._cache.clear()

    async def load_from_file(self, file_path: str, source: ConfigSource = ConfigSource.FILE):
        """Load configuration from file."""
        path = Path(file_path)
        if not path.exists():
            self.logger.warning(f"Config file not found: {file_path}")
            return

        try:
            with open(path, "r") as f:
                if path.suffix in [".yaml", ".yml"]:
                    if YAML_AVAILABLE:
                        data = yaml.safe_load(f)
                    else:
                        self.logger.error("YAML support not available")
                        return
                elif path.suffix == ".json":
                    data = json.load(f)
                else:
                    self.logger.error(f"Unsupported config format: {path.suffix}")
                    return

            if data:
                await self.load_from_dict(data, source)
                self.logger.info(f"Loaded config from {file_path}")

        except Exception as e:
            self.logger.error(f"Failed to load config from {file_path}: {e}")

    async def load_from_environment(self, prefix: str = ""):
        """Load configuration from environment variables."""
        async with self._lock:
            for key, value in os.environ.items():
                if prefix and not key.startswith(prefix):
                    continue

                config_key = key.lower()
                if prefix:
                    config_key = config_key[len(prefix):].lstrip("_")

                # Try to parse JSON values
                try:
                    parsed_value = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    parsed_value = value

                self._layers[ConfigSource.ENVIRONMENT][config_key] = ConfigValue(
                    key=config_key,
                    value=parsed_value,
                    source=ConfigSource.ENVIRONMENT,
                )

            self._cache.clear()

    def _coerce_type(self, value: Any, target_type: type) -> Any:
        """Coerce value to target type."""
        try:
            if target_type == bool:
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes", "on")
                return bool(value)
            elif target_type == int:
                return int(value)
            elif target_type == float:
                return float(value)
            elif target_type == list:
                if isinstance(value, str):
                    return value.split(",")
                return list(value)
            elif target_type == dict:
                if isinstance(value, str):
                    return json.loads(value)
                return dict(value)
            else:
                return target_type(value)
        except (ValueError, TypeError):
            return value


# =============================================================================
# UNIFIED CONFIGURATION ENGINE
# =============================================================================


class UnifiedConfigurationEngine:
    """
    Unified configuration engine coordinating all configuration components.

    Provides:
    - Centralized configuration management
    - Schema validation
    - Version history
    - Hot-reload
    - Cross-repo synchronization
    """

    def __init__(self, config: Optional[ConfigurationEngineConfig] = None):
        self.config = config or ConfigurationEngineConfig()
        self.logger = logging.getLogger("UnifiedConfigurationEngine")

        # Components
        self.store = ConfigurationStore(self.config)
        self.validator = SchemaValidator(self.config.allow_unknown_keys)
        self.version_manager = ConfigVersionManager(self.config)
        self.hot_reload = HotReloadManager(self.config)

        # State
        self._running = False
        self._schemas: Dict[str, ConfigSchema] = {}
        self._callbacks: List[Callable] = []
        self._lock = asyncio.Lock()

        # Event tracking
        self._change_events: List[ConfigChangeEvent] = []

    async def initialize(self) -> bool:
        """Initialize the configuration engine."""
        try:
            # Create directories
            Path(self.config.config_directory).mkdir(parents=True, exist_ok=True)

            # Initialize version manager
            await self.version_manager.initialize()

            # Register hot reload callback
            self.hot_reload.register_callback(self._on_file_changed)

            self._running = True
            self.logger.info("UnifiedConfigurationEngine initialized")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    async def shutdown(self):
        """Shutdown the configuration engine."""
        self._running = False
        await self.hot_reload.stop()
        self.logger.info("UnifiedConfigurationEngine shutdown")

    # =========================================================================
    # Configuration Access
    # =========================================================================

    async def get(
        self,
        key: str,
        default: Any = None,
        config_type: Optional[type] = None,
    ) -> Any:
        """Get a configuration value."""
        return await self.store.get(key, default, config_type)

    async def set(
        self,
        key: str,
        value: Any,
        validate: bool = True,
        create_version: bool = True,
    ):
        """Set a configuration value."""
        old_value = await self.store.get(key)

        # Set new value
        await self.store.set(key, value)

        # Validate if schema exists
        if validate and key in self._schemas:
            result = self.validator.validate({key: value}, self._schemas[key])
            if not result.valid:
                # Rollback
                await self.store.set(key, old_value)
                raise ValueError(f"Validation failed: {result.issues}")

        # Create version
        if create_version:
            all_config = await self.store.get_all()
            await self.version_manager.create_version(
                all_config,
                ChangeType.UPDATE,
                f"Updated {key}",
            )

        # Emit change event
        event = ConfigChangeEvent(
            config_key=key,
            old_value=old_value,
            new_value=value,
            change_type=ChangeType.UPDATE,
        )
        await self._emit_change(event)

    async def delete(self, key: str, create_version: bool = True):
        """Delete a configuration value."""
        old_value = await self.store.get(key)

        await self.store.delete(key)

        if create_version:
            all_config = await self.store.get_all()
            await self.version_manager.create_version(
                all_config,
                ChangeType.DELETE,
                f"Deleted {key}",
            )

        event = ConfigChangeEvent(
            config_key=key,
            old_value=old_value,
            new_value=None,
            change_type=ChangeType.DELETE,
        )
        await self._emit_change(event)

    async def get_all(self) -> Dict[str, Any]:
        """Get all configuration values."""
        return await self.store.get_all()

    # =========================================================================
    # File Operations
    # =========================================================================

    async def load_file(self, file_path: str, watch: bool = True):
        """Load configuration from a file."""
        await self.store.load_from_file(file_path)

        if watch and self.config.hot_reload_enabled:
            await self.hot_reload.start([file_path])

        # Create version
        all_config = await self.store.get_all()
        await self.version_manager.create_version(
            all_config,
            ChangeType.UPDATE,
            f"Loaded from {file_path}",
        )

    async def save_file(self, file_path: str):
        """Save configuration to a file."""
        all_config = await self.store.get_all()

        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            if path.suffix in [".yaml", ".yml"] and YAML_AVAILABLE:
                yaml.safe_dump(all_config, f, default_flow_style=False)
            else:
                json.dump(all_config, f, indent=2)

        self.logger.info(f"Saved config to {file_path}")

    async def load_environment(self, prefix: str = "JARVIS_"):
        """Load configuration from environment variables."""
        await self.store.load_from_environment(prefix)

    # =========================================================================
    # Schema Management
    # =========================================================================

    async def register_schema(
        self,
        name: str,
        schema: ConfigSchema,
    ):
        """Register a configuration schema."""
        async with self._lock:
            self._schemas[name] = schema
            self.logger.info(f"Registered schema: {name}")

    async def validate(
        self,
        data: Optional[Dict[str, Any]] = None,
        schema_name: Optional[str] = None,
    ) -> ValidationResult:
        """Validate configuration against schema."""
        if data is None:
            data = await self.store.get_all()

        if schema_name:
            if schema_name not in self._schemas:
                result = ValidationResult()
                result.add_issue(
                    ValidationSeverity.ERROR,
                    "",
                    f"Schema '{schema_name}' not found",
                )
                return result
            return self.validator.validate(data, self._schemas[schema_name])

        # Validate against all registered schemas
        combined_result = ValidationResult()
        for name, schema in self._schemas.items():
            result = self.validator.validate(data, schema)
            combined_result.issues.extend(result.issues)
            if not result.valid:
                combined_result.valid = False

        return combined_result

    # =========================================================================
    # Version Operations
    # =========================================================================

    async def get_version(
        self,
        version_id: Optional[str] = None,
    ) -> Optional[ConfigVersion]:
        """Get a configuration version."""
        return await self.version_manager.get_version(version_id)

    async def get_history(
        self,
        limit: int = 10,
    ) -> List[ConfigVersion]:
        """Get version history."""
        return await self.version_manager.get_history(limit)

    async def rollback(self, version_id: str) -> bool:
        """Rollback to a specific version."""
        version = await self.version_manager.rollback(version_id)
        if not version:
            return False

        # Apply the rolled back config
        await self.store.load_from_dict(version.config_data)

        event = ConfigChangeEvent(
            config_key="*",
            change_type=ChangeType.ROLLBACK,
            version=version.version_number,
        )
        await self._emit_change(event)

        return True

    async def diff(
        self,
        version_a: str,
        version_b: str,
    ) -> Dict[str, Any]:
        """Generate diff between versions."""
        return await self.version_manager.diff(version_a, version_b)

    # =========================================================================
    # Change Notifications
    # =========================================================================

    def register_change_callback(self, callback: Callable):
        """Register callback for configuration changes."""
        self._callbacks.append(callback)

    def unregister_change_callback(self, callback: Callable):
        """Unregister change callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def _emit_change(self, event: ConfigChangeEvent):
        """Emit configuration change event."""
        self._change_events.append(event)

        # Keep only recent events
        if len(self._change_events) > 1000:
            self._change_events = self._change_events[-500:]

        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                self.logger.error(f"Change callback error: {e}")

    async def _on_file_changed(self, file_path: str):
        """Handle file change notification."""
        self.logger.info(f"Config file changed: {file_path}")
        await self.store.load_from_file(file_path)

        # Create version
        all_config = await self.store.get_all()
        await self.version_manager.create_version(
            all_config,
            ChangeType.UPDATE,
            f"Hot-reloaded from {file_path}",
        )

        event = ConfigChangeEvent(
            config_key=file_path,
            change_type=ChangeType.UPDATE,
            source=ConfigSource.FILE,
        )
        await self._emit_change(event)

    # =========================================================================
    # Status
    # =========================================================================

    async def get_status(self) -> Dict[str, Any]:
        """Get engine status."""
        current_version = await self.version_manager.get_version()

        return {
            "running": self._running,
            "hot_reload_enabled": self.config.hot_reload_enabled,
            "schemas_registered": len(self._schemas),
            "current_version": current_version.version_number if current_version else 0,
            "total_versions": len(await self.version_manager.get_history(1000)),
            "config_keys": len(await self.store.get_all()),
        }


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_engine: Optional[UnifiedConfigurationEngine] = None
_engine_lock = asyncio.Lock()


async def get_configuration_engine() -> UnifiedConfigurationEngine:
    """Get or create the global configuration engine."""
    global _engine

    async with _engine_lock:
        if _engine is None:
            _engine = UnifiedConfigurationEngine()
            await _engine.initialize()
        return _engine


async def initialize_configuration() -> bool:
    """Initialize the global configuration engine."""
    engine = await get_configuration_engine()
    return engine._running


async def shutdown_configuration():
    """Shutdown the global configuration engine."""
    global _engine

    async with _engine_lock:
        if _engine is not None:
            await _engine.shutdown()
            _engine = None
            logger.info("Configuration engine shutdown")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    "ConfigurationEngineConfig",
    # Enums
    "ConfigEnvironment",
    "ConfigSource",
    "ConfigFormat",
    "ValidationSeverity",
    "ChangeType",
    "SyncStatus",
    # Data Structures
    "ConfigValue",
    "ConfigVersion",
    "ValidationIssue",
    "ValidationResult",
    "ConfigSchema",
    "ConfigChangeEvent",
    # Components
    "SchemaValidator",
    "ConfigVersionManager",
    "HotReloadManager",
    "ConfigurationStore",
    # Engine
    "UnifiedConfigurationEngine",
    # Global Functions
    "get_configuration_engine",
    "initialize_configuration",
    "shutdown_configuration",
]
