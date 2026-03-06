"""Tests for the KeySchema validation layer and SchemaRegistry.

Covers type validation, nullable handling, enum coercion policies,
range constraints, regex patterns, version defaults, and registry
operations.
"""
from __future__ import annotations

import pytest

from backend.core.reactive_state.schemas import (
    KeySchema,
    SchemaRegistry,
    SchemaValidationError,
)


# -- Helpers ----------------------------------------------------------------


def _bool_schema(**overrides) -> KeySchema:
    defaults = dict(
        key="gcp.vm_ready",
        value_type="bool",
        nullable=False,
        default=False,
        description="Whether the GCP VM is ready.",
    )
    defaults.update(overrides)
    return KeySchema(**defaults)


def _enum_schema(**overrides) -> KeySchema:
    defaults = dict(
        key="prime.tier",
        value_type="enum",
        nullable=False,
        default="CLAUDE",
        description="Active inference tier.",
        enum_values=("PRIME_API", "PRIME_LOCAL", "CLAUDE"),
    )
    defaults.update(overrides)
    return KeySchema(**defaults)


def _int_schema(**overrides) -> KeySchema:
    defaults = dict(
        key="audio.sample_rate",
        value_type="int",
        nullable=False,
        default=16000,
        description="Audio sample rate in Hz.",
    )
    defaults.update(overrides)
    return KeySchema(**defaults)


def _float_schema(**overrides) -> KeySchema:
    defaults = dict(
        key="voice.confidence",
        value_type="float",
        nullable=False,
        default=0.0,
        description="Voice biometric confidence score.",
    )
    defaults.update(overrides)
    return KeySchema(**defaults)


def _str_schema(**overrides) -> KeySchema:
    defaults = dict(
        key="prime.endpoint",
        value_type="str",
        nullable=False,
        default="",
        description="Active prime endpoint URL.",
    )
    defaults.update(overrides)
    return KeySchema(**defaults)


# -- TestBoolSchema ---------------------------------------------------------


class TestBoolSchema:
    """Bool schema accepts only Python bool values."""

    def test_bool_schema_accepts_bool(self):
        schema = _bool_schema()
        assert schema.validate(True) is None
        assert schema.validate(False) is None

    def test_bool_schema_rejects_string(self):
        schema = _bool_schema()
        error = schema.validate("true")
        assert error is not None
        assert "bool" in error.lower()


# -- TestEnumSchema ---------------------------------------------------------


class TestEnumSchema:
    """Enum schema validates membership and applies coercion policies."""

    def test_enum_accepts_valid_value(self):
        schema = _enum_schema()
        assert schema.validate("PRIME_API") is None
        assert schema.validate("CLAUDE") is None

    def test_enum_rejects_invalid_value_with_reject_policy(self):
        schema = _enum_schema(unknown_enum_policy="reject")
        error = schema.validate("NONEXISTENT_TIER")
        assert error is not None
        assert "NONEXISTENT_TIER" in error

    def test_enum_map_to_policy(self):
        schema = _enum_schema(unknown_enum_policy="map_to:CLAUDE")
        # validate passes because coercion will handle it
        assert schema.validate("UNKNOWN_VALUE") is None
        # coerce maps the unknown value
        assert schema.coerce("UNKNOWN_VALUE") == "CLAUDE"


# -- TestNullable -----------------------------------------------------------


class TestNullable:
    """Nullable schemas accept None; non-nullable reject it."""

    def test_nullable_accepts_none(self):
        schema = _str_schema(nullable=True)
        assert schema.validate(None) is None

    def test_non_nullable_rejects_none(self):
        schema = _str_schema(nullable=False)
        error = schema.validate(None)
        assert error is not None
        assert "null" in error.lower() or "none" in error.lower()


# -- TestIntSchema ----------------------------------------------------------


class TestIntSchema:
    """Int schema validates type and range constraints."""

    def test_int_with_min_max_range(self):
        schema = _int_schema(min_value=8000, max_value=48000)
        assert schema.validate(16000) is None
        assert schema.validate(8000) is None   # inclusive
        assert schema.validate(48000) is None  # inclusive

        error_low = schema.validate(4000)
        assert error_low is not None
        assert "8000" in error_low

        error_high = schema.validate(96000)
        assert error_high is not None
        assert "48000" in error_high


# -- TestFloatSchema --------------------------------------------------------


class TestFloatSchema:
    """Float schema validates type and range, accepts ints as floats."""

    def test_float_with_min(self):
        schema = _float_schema(min_value=0.0)
        assert schema.validate(0.85) is None
        assert schema.validate(0.0) is None    # inclusive
        assert schema.validate(0) is None      # int accepted as float

        error = schema.validate(-0.1)
        assert error is not None
        assert "0.0" in error


# -- TestStringSchema -------------------------------------------------------


class TestStringSchema:
    """String schema validates type and optional regex pattern."""

    def test_string_with_regex_pattern(self):
        schema = _str_schema(pattern=r"https?://\S+")
        assert schema.validate("https://example.com") is None

        error = schema.validate("not-a-url")
        assert error is not None
        assert "pattern" in error.lower()


# -- TestSchemaVersion ------------------------------------------------------


class TestSchemaVersion:
    """KeySchema defaults to schema_version=1."""

    def test_schema_version_defaults_to_one(self):
        schema = _bool_schema()
        assert schema.schema_version == 1


# -- TestSchemaRegistry -----------------------------------------------------


class TestSchemaRegistry:
    """SchemaRegistry stores and retrieves KeySchema instances."""

    def test_register_and_get(self):
        registry = SchemaRegistry()
        schema = _bool_schema()
        registry.register(schema)
        assert registry.get("gcp.vm_ready") is schema

    def test_get_unknown_returns_none(self):
        registry = SchemaRegistry()
        assert registry.get("nonexistent.key") is None

    def test_duplicate_registration_raises(self):
        registry = SchemaRegistry()
        schema = _bool_schema()
        registry.register(schema)
        with pytest.raises(ValueError):
            registry.register(schema)

    def test_all_keys(self):
        registry = SchemaRegistry()
        registry.register(_bool_schema(key="a.b"))
        registry.register(_int_schema(key="c.d"))
        registry.register(_str_schema(key="e.f"))
        assert registry.all_keys() == {"a.b", "c.d", "e.f"}
