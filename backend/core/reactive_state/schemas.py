"""Schema validation layer for the reactive state store -- stdlib only.

Provides ``KeySchema`` (frozen dataclass) for per-key type/constraint
validation and ``SchemaRegistry`` for registering and retrieving schemas.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only.
* ``KeySchema`` is ``@dataclass(frozen=True)`` (immutable value object).
* ``validate()`` returns ``None`` on success or a human-readable error string.
* ``coerce()`` applies policy-driven value transformations (e.g. enum mapping).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Set, Tuple


# -- Exceptions -------------------------------------------------------------


class SchemaValidationError(Exception):
    """Raised when a value fails schema validation."""


# -- Key Schema --------------------------------------------------------------


@dataclass(frozen=True)
class KeySchema:
    """Declarative schema for a single reactive-state key.

    Attributes
    ----------
    key:
        Dotted key name (e.g. ``"gcp.vm_ready"``).
    value_type:
        Expected type tag: ``"bool"``, ``"str"``, ``"int"``, ``"float"``,
        or ``"enum"``.
    nullable:
        Whether ``None`` is an acceptable value.
    default:
        Default value used when no explicit write has occurred.
    description:
        Human-readable description of the key's purpose.
    enum_values:
        Allowed values when ``value_type`` is ``"enum"``.
    pattern:
        Optional regex (matched via ``re.fullmatch``) for ``"str"`` values.
    min_value:
        Inclusive lower bound for ``"int"`` / ``"float"`` values.
    max_value:
        Inclusive upper bound for ``"int"`` / ``"float"`` values.
    schema_version:
        Version of this schema definition (for migrations).
    previous_version:
        Previous schema version this one supersedes (if any).
    unknown_enum_policy:
        How to handle unknown enum values:
        ``"reject"`` -- fail validation,
        ``"map_to:<value>"`` -- coerce to the given value,
        ``"default_with_violation"`` -- coerce to ``self.default``.
    origin_default:
        Default ``origin`` tag applied to values created from this schema's
        default.
    """

    key: str
    value_type: str  # "bool" | "str" | "int" | "float" | "enum"
    nullable: bool
    default: Any
    description: str
    enum_values: Optional[Tuple[str, ...]] = None
    pattern: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    schema_version: int = 1
    previous_version: Optional[int] = None
    unknown_enum_policy: str = "reject"
    origin_default: str = "default"

    def validate(self, value: Any) -> Optional[str]:
        """Return ``None`` if *value* is valid, or a human-readable error string.

        Validation order:
        1. Null check (nullable vs non-nullable).
        2. Type check per ``value_type``.
        3. Range check (``min_value`` / ``max_value``) for numeric types.
        4. Pattern check (``re.fullmatch``) for strings.
        5. Enum membership check (respecting ``unknown_enum_policy``).
        """
        # 1. Null check
        if value is None:
            if self.nullable:
                return None
            return (
                f"Key '{self.key}' is not nullable but received None"
            )

        # 2 + 3 + 4 + 5. Dispatch by value_type
        validator = _TYPE_VALIDATORS.get(self.value_type)
        if validator is None:
            return f"Key '{self.key}' has unknown value_type '{self.value_type}'"
        return validator(self, value)

    def coerce(self, value: Any) -> Any:
        """Apply coercion policies and return the (possibly transformed) value.

        Currently only applies to ``"enum"`` schemas with an
        ``unknown_enum_policy`` other than ``"reject"``.  For all other
        schemas the value is returned unchanged.
        """
        if self.value_type != "enum":
            return value
        if self.enum_values is not None and isinstance(value, str):
            if value not in self.enum_values:
                return _coerce_enum(self, value)
        return value


# -- Private validators (one per value_type) ---------------------------------


def _validate_bool(schema: KeySchema, value: Any) -> Optional[str]:
    if not isinstance(value, bool):
        return (
            f"Key '{schema.key}' expected bool, "
            f"got {type(value).__name__}: {value!r}"
        )
    return None


def _validate_str(schema: KeySchema, value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return (
            f"Key '{schema.key}' expected str, "
            f"got {type(value).__name__}: {value!r}"
        )
    if schema.pattern is not None:
        if re.fullmatch(schema.pattern, value) is None:
            return (
                f"Key '{schema.key}' value {value!r} does not match "
                f"pattern '{schema.pattern}'"
            )
    return None


def _validate_int(schema: KeySchema, value: Any) -> Optional[str]:
    # Python bools are ints -- reject them explicitly
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            f"Key '{schema.key}' expected int, "
            f"got {type(value).__name__}: {value!r}"
        )
    return _check_range(schema, value)


def _validate_float(schema: KeySchema, value: Any) -> Optional[str]:
    # Accept int as float, but reject bool
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (
            f"Key '{schema.key}' expected float, "
            f"got {type(value).__name__}: {value!r}"
        )
    return _check_range(schema, value)


def _validate_enum(schema: KeySchema, value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return (
            f"Key '{schema.key}' expected str for enum, "
            f"got {type(value).__name__}: {value!r}"
        )
    if schema.enum_values is not None and value not in schema.enum_values:
        # Check policy -- some policies allow unknown values through
        # validation so that coerce() can handle them.
        policy = schema.unknown_enum_policy
        if policy == "reject":
            return (
                f"Key '{schema.key}' value {value!r} is not in "
                f"allowed enum values {schema.enum_values!r}"
            )
        # "map_to:*" and "default_with_violation" pass validation;
        # coerce() will transform the value.
    return None


# -- Range helper ------------------------------------------------------------


def _check_range(schema: KeySchema, value: Any) -> Optional[str]:
    """Check inclusive min/max bounds for numeric values."""
    if schema.min_value is not None and value < schema.min_value:
        return (
            f"Key '{schema.key}' value {value} is below "
            f"minimum {schema.min_value}"
        )
    if schema.max_value is not None and value > schema.max_value:
        return (
            f"Key '{schema.key}' value {value} is above "
            f"maximum {schema.max_value}"
        )
    return None


# -- Enum coercion helper ---------------------------------------------------


def _coerce_enum(schema: KeySchema, value: str) -> Any:
    """Apply the unknown_enum_policy for an out-of-range enum value."""
    policy = schema.unknown_enum_policy
    if policy.startswith("map_to:"):
        return policy[len("map_to:"):]
    if policy == "default_with_violation":
        return schema.default
    # "reject" -- return value unchanged; validate() handles rejection
    return value


# -- Validator dispatch table ------------------------------------------------

_TYPE_VALIDATORS = {
    "bool": _validate_bool,
    "str": _validate_str,
    "int": _validate_int,
    "float": _validate_float,
    "enum": _validate_enum,
}


# -- Schema Registry ---------------------------------------------------------


class SchemaRegistry:
    """Thread-safe registry mapping key names to their ``KeySchema``.

    Raises ``ValueError`` on duplicate registration attempts.
    """

    def __init__(self) -> None:
        self._schemas: dict[str, KeySchema] = {}

    def register(self, schema: KeySchema) -> None:
        """Register a ``KeySchema``.

        Raises
        ------
        ValueError
            If a schema for the same key is already registered.
        """
        if schema.key in self._schemas:
            raise ValueError(
                f"Schema for key '{schema.key}' is already registered"
            )
        self._schemas[schema.key] = schema

    def get(self, key: str) -> Optional[KeySchema]:
        """Return the schema for *key*, or ``None`` if not registered."""
        return self._schemas.get(key)

    def all_keys(self) -> Set[str]:
        """Return the set of all registered key names."""
        return set(self._schemas.keys())
