"""
Environment Configuration Utilities
====================================

Generic layer for type-safe environment variable parsing.
Config modules like startup_timeouts.py use these primitives.

This module provides a consistent, robust way to read environment variables
with proper type conversion, validation, and sensible defaults. It is designed
for extraction to jarvis-common in Phase 2.

Key Functions:
    - get_env_str: Basic string environment variable
    - get_env_optional_str: Optional string (distinguishes unset from empty)
    - get_env_int: Integer with optional bounds validation
    - get_env_float: Float with optional bounds validation
    - get_env_bool: Boolean with case-insensitive parsing
    - get_env_list: List with configurable separator

Base Class:
    - EnvConfig: Dataclass-based configuration with automatic env key mapping

Behavior:
    - On parse/validation error: log warning, return default (never raise)
    - All parsing is case-insensitive where applicable
    - Designed for jarvis-common extraction in Phase 2

Usage:
    from backend.utils.env_config import (
        get_env_str,
        get_env_int,
        get_env_bool,
        EnvConfig,
    )

    # Simple env var access
    port = get_env_int("PORT", 8080, min_val=1, max_val=65535)
    debug = get_env_bool("DEBUG", default=False)

    # Dataclass-based configuration
    @dataclass
    class AppConfig(EnvConfig):
        port: int = 8080
        debug: bool = False

    config = AppConfig.from_env()  # Reads JARVIS_PORT, JARVIS_DEBUG
"""

from __future__ import annotations

import logging
import os
from dataclasses import MISSING, dataclass, fields
from typing import Any, ClassVar, Type, TypeVar, get_type_hints

logger = logging.getLogger(__name__)

# Type variable for EnvConfig subclasses
T = TypeVar("T", bound="EnvConfig")


# =============================================================================
# String Functions
# =============================================================================


def get_env_str(key: str, default: str = "") -> str:
    """
    Get string environment variable.

    Args:
        key: The environment variable name
        default: Value to return if not set (default: "")

    Returns:
        The environment variable value, or default if not set

    Example:
        host = get_env_str("HOST", "localhost")
    """
    return os.environ.get(key, default)


def get_env_optional_str(key: str) -> str | None:
    """
    Get optional string environment variable.

    Returns None if unset, "" if set but empty.
    Use when the distinction between unset and empty matters.

    Args:
        key: The environment variable name

    Returns:
        None if unset, actual value (including "") if set

    Example:
        # Unset vs empty matters for optional API keys
        api_key = get_env_optional_str("API_KEY")
        if api_key is None:
            # Not configured at all
            pass
        elif api_key == "":
            # Explicitly set to empty (disable feature)
            pass
        else:
            # Use the API key
            pass
    """
    return os.environ.get(key)  # Returns None if not present


# =============================================================================
# Numeric Functions
# =============================================================================


def get_env_int(
    key: str,
    default: int,
    *,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """
    Get integer environment variable with optional bounds validation.

    On parse error: logs warning, returns default.
    On bounds violation: logs warning, clamps to bounds.

    Args:
        key: The environment variable name
        default: Value to return if not set or on parse error
        min_val: Optional minimum value (clamps if below)
        max_val: Optional maximum value (clamps if above)

    Returns:
        The parsed integer, clamped to bounds if specified

    Example:
        timeout = get_env_int("TIMEOUT", 30, min_val=1, max_val=300)
    """
    raw = os.environ.get(key)
    if raw is None:
        return default

    # Attempt to parse
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid integer value for %s: %r (using default: %d)",
            key,
            raw,
            default,
        )
        return default

    # Apply bounds
    if min_val is not None and value < min_val:
        logger.warning(
            "%s=%d is below minimum %d, clamping to %d",
            key,
            value,
            min_val,
            min_val,
        )
        return min_val

    if max_val is not None and value > max_val:
        logger.warning(
            "%s=%d is above maximum %d, clamping to %d",
            key,
            value,
            max_val,
            max_val,
        )
        return max_val

    return value


def get_env_float(
    key: str,
    default: float,
    *,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float:
    """
    Get float environment variable with optional bounds validation.

    On parse error: logs warning, returns default.
    On bounds violation: logs warning, clamps to bounds.

    Args:
        key: The environment variable name
        default: Value to return if not set or on parse error
        min_val: Optional minimum value (clamps if below)
        max_val: Optional maximum value (clamps if above)

    Returns:
        The parsed float, clamped to bounds if specified

    Example:
        threshold = get_env_float("THRESHOLD", 0.85, min_val=0.0, max_val=1.0)
    """
    raw = os.environ.get(key)
    if raw is None:
        return default

    # Attempt to parse
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid float value for %s: %r (using default: %g)",
            key,
            raw,
            default,
        )
        return default

    # Apply bounds
    if min_val is not None and value < min_val:
        logger.warning(
            "%s=%g is below minimum %g, clamping to %g",
            key,
            value,
            min_val,
            min_val,
        )
        return min_val

    if max_val is not None and value > max_val:
        logger.warning(
            "%s=%g is above maximum %g, clamping to %g",
            key,
            value,
            max_val,
            max_val,
        )
        return max_val

    return value


# =============================================================================
# Boolean Function
# =============================================================================

# Case-insensitive truthy values
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off", ""})


def get_env_bool(key: str, default: bool = False) -> bool:
    """
    Get boolean environment variable.

    Case-insensitive parsing:
        True:  "true", "1", "yes", "on"
        False: "false", "0", "no", "off", "" (empty), unset

    On unrecognized value: logs warning, returns default.

    Args:
        key: The environment variable name
        default: Value to return if not set or unrecognized (default: False)

    Returns:
        The parsed boolean value

    Example:
        debug = get_env_bool("DEBUG", default=False)
        verbose = get_env_bool("VERBOSE", default=True)
    """
    raw = os.environ.get(key)
    if raw is None:
        return default

    # Normalize: lowercase and strip whitespace
    normalized = raw.lower().strip()

    if normalized in _TRUE_VALUES:
        return True

    if normalized in _FALSE_VALUES:
        return False

    # Unrecognized value
    logger.warning(
        "Invalid boolean value for %s: %r (using default: %s)",
        key,
        raw,
        default,
    )
    return default


# =============================================================================
# List Function
# =============================================================================


def get_env_list(
    key: str,
    default: list[str] | None = None,
    *,
    separator: str = ",",
    strip: bool = True,
) -> list[str]:
    """
    Get list environment variable.

    Splits by separator, strips whitespace from each element by default.
    Empty elements (from consecutive separators) are filtered out.

    Args:
        key: The environment variable name
        default: Value to return if not set (default: [])
        separator: Character(s) to split on (default: ",")
        strip: Whether to strip whitespace from elements (default: True)

    Returns:
        List of string values, or default if not set

    Example:
        hosts = get_env_list("HOSTS", default=["localhost"])
        # HOSTS="host1, host2, host3" -> ["host1", "host2", "host3"]

        paths = get_env_list("PATH_LIST", separator=":")
        # PATH_LIST="/usr/bin:/usr/local/bin" -> ["/usr/bin", "/usr/local/bin"]
    """
    raw = os.environ.get(key)
    if raw is None:
        return default if default is not None else []

    # Empty string returns empty list
    if not raw:
        return []

    # Split and optionally strip
    parts = raw.split(separator)

    if strip:
        parts = [p.strip() for p in parts]

    # Filter out empty elements
    return [p for p in parts if p]


# =============================================================================
# EnvConfig Base Class
# =============================================================================


@dataclass
class EnvConfig:
    """
    Base class for type-safe environment config sections.

    Convention:
        Field 'my_setting' -> env key '{prefix}MY_SETTING'
        Default prefix: "JARVIS_"
        Override via class attribute: _env_prefix = "CUSTOM_"

    Supported field types:
        - int: Uses get_env_int
        - float: Uses get_env_float
        - bool: Uses get_env_bool
        - str: Uses get_env_str

    Example:
        @dataclass
        class VoiceConfig(EnvConfig):
            sample_rate: int = 16000      # -> JARVIS_SAMPLE_RATE
            base_threshold: float = 0.85  # -> JARVIS_BASE_THRESHOLD
            enabled: bool = True          # -> JARVIS_ENABLED

        config = VoiceConfig.from_env()

        # With custom prefix
        @dataclass
        class AppConfig(EnvConfig):
            _env_prefix: ClassVar[str] = "MYAPP_"
            port: int = 8080  # -> MYAPP_PORT
    """

    _env_prefix: ClassVar[str] = "JARVIS_"

    @classmethod
    def from_env(cls: Type[T]) -> T:
        """
        Create an instance by reading environment variables.

        For each field in the dataclass:
        1. Convert field name to UPPER_SNAKE_CASE
        2. Prepend the prefix (default: JARVIS_)
        3. Read from environment with appropriate type conversion
        4. Use field default if env var not set or invalid

        Returns:
            A new instance with values from environment
        """
        # Get type hints for proper type inference
        hints = get_type_hints(cls)

        # Build kwargs for the dataclass constructor
        kwargs: dict[str, Any] = {}

        for field in fields(cls):
            # Skip ClassVar fields (like _env_prefix)
            if field.name.startswith("_"):
                continue

            # Build env key: prefix + UPPER_SNAKE_CASE
            env_key = cls._env_prefix + field.name.upper()

            # Get the field type
            field_type = hints.get(field.name, str)

            # Determine default value, handling MISSING sentinel
            # We need to resolve the default before type dispatch
            has_default = True
            if field.default is not MISSING:
                raw_default: Any = field.default
            elif field.default_factory is not MISSING:
                raw_default = field.default_factory()
            else:
                # Required field with no default - check if env var is set
                raw_env = os.environ.get(env_key)
                if raw_env is None:
                    raise ValueError(
                        f"EnvConfig field '{field.name}' has no default and "
                        f"env var '{env_key}' is not set"
                    )
                # Mark that we have no default (env var will provide value)
                has_default = False
                raw_default = None  # Placeholder, won't be used

            # Read from environment with appropriate parser
            # Each branch uses explicit casting to satisfy type checkers
            if field_type is int:
                int_default: int = int(raw_default) if has_default else 0
                kwargs[field.name] = get_env_int(env_key, int_default)
            elif field_type is float:
                float_default: float = float(raw_default) if has_default else 0.0
                kwargs[field.name] = get_env_float(env_key, float_default)
            elif field_type is bool:
                bool_default: bool = bool(raw_default) if has_default else False
                kwargs[field.name] = get_env_bool(env_key, bool_default)
            else:
                # Default to string
                str_default: str = str(raw_default) if has_default else ""
                kwargs[field.name] = get_env_str(env_key, str_default)

        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the config to a dictionary.

        Returns:
            Dictionary with field names as keys and current values
        """
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if not field.name.startswith("_")
        }


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # String functions
    "get_env_str",
    "get_env_optional_str",
    # Numeric functions
    "get_env_int",
    "get_env_float",
    # Boolean function
    "get_env_bool",
    # List function
    "get_env_list",
    # Base class
    "EnvConfig",
]
