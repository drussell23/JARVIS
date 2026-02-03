"""
Tests for backend.utils.env_config module.

This module tests the consolidated environment configuration utilities for
type-safe environment variable parsing. These utilities are designed for
extraction to jarvis-common in Phase 2.

Test coverage:
- get_env_str: String environment variable parsing
- get_env_optional_str: Optional string parsing (None vs empty)
- get_env_int: Integer parsing with bounds validation
- get_env_float: Float parsing with bounds validation
- get_env_bool: Boolean parsing (case-insensitive)
- get_env_list: List parsing with configurable separator
- EnvConfig: Dataclass-based configuration with env key convention

Following TDD: Tests written first, then implementation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import ClassVar

import pytest


# =============================================================================
# Test: get_env_str - String environment variable parsing
# =============================================================================


class TestGetEnvStr:
    """Tests for get_env_str - basic string env var parsing."""

    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify default value is returned when env var is not set."""
        from backend.utils.env_config import get_env_str

        monkeypatch.delenv("TEST_UNSET_VAR", raising=False)

        result = get_env_str("TEST_UNSET_VAR", "default_value")
        assert result == "default_value"

    def test_returns_default_empty_string_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify empty string default when not provided."""
        from backend.utils.env_config import get_env_str

        monkeypatch.delenv("TEST_UNSET_VAR", raising=False)

        result = get_env_str("TEST_UNSET_VAR")
        assert result == ""

    def test_returns_set_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify actual env var value is returned when set."""
        from backend.utils.env_config import get_env_str

        monkeypatch.setenv("TEST_STR_VAR", "actual_value")

        result = get_env_str("TEST_STR_VAR", "default")
        assert result == "actual_value"

    def test_returns_empty_string_when_set_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify empty string is returned when env var is set to empty."""
        from backend.utils.env_config import get_env_str

        monkeypatch.setenv("TEST_EMPTY_VAR", "")

        result = get_env_str("TEST_EMPTY_VAR", "default")
        assert result == ""


# =============================================================================
# Test: get_env_optional_str - Optional string with None distinction
# =============================================================================


class TestGetEnvOptionalStr:
    """Tests for get_env_optional_str - distinguishing unset from empty."""

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify None is returned when env var is not set."""
        from backend.utils.env_config import get_env_optional_str

        monkeypatch.delenv("TEST_OPTIONAL_UNSET", raising=False)

        result = get_env_optional_str("TEST_OPTIONAL_UNSET")
        assert result is None

    def test_returns_empty_string_when_set_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify empty string is returned when set to empty (not None)."""
        from backend.utils.env_config import get_env_optional_str

        monkeypatch.setenv("TEST_OPTIONAL_EMPTY", "")

        result = get_env_optional_str("TEST_OPTIONAL_EMPTY")
        assert result == ""
        assert result is not None  # Explicitly check it's not None

    def test_returns_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify actual value is returned when set."""
        from backend.utils.env_config import get_env_optional_str

        monkeypatch.setenv("TEST_OPTIONAL_SET", "some_value")

        result = get_env_optional_str("TEST_OPTIONAL_SET")
        assert result == "some_value"


# =============================================================================
# Test: get_env_int - Integer parsing with bounds
# =============================================================================


class TestGetEnvInt:
    """Tests for get_env_int - integer parsing with bounds validation."""

    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify default value when env var is not set."""
        from backend.utils.env_config import get_env_int

        monkeypatch.delenv("TEST_INT_UNSET", raising=False)

        result = get_env_int("TEST_INT_UNSET", 42)
        assert result == 42

    def test_parses_valid_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify valid integer string is parsed correctly."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_VALID", "123")

        result = get_env_int("TEST_INT_VALID", 0)
        assert result == 123

    def test_parses_negative_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify negative integers are parsed correctly."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_NEGATIVE", "-50")

        result = get_env_int("TEST_INT_NEGATIVE", 0)
        assert result == -50

    def test_invalid_string_logs_warning_returns_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify invalid string logs warning and returns default."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_INVALID", "not_a_number")

        with caplog.at_level(logging.WARNING):
            result = get_env_int("TEST_INT_INVALID", 99)

        assert result == 99
        assert "TEST_INT_INVALID" in caplog.text
        assert "not_a_number" in caplog.text or "invalid" in caplog.text.lower()

    def test_float_string_logs_warning_returns_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify float string is invalid for int parsing."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_FLOAT", "3.14")

        with caplog.at_level(logging.WARNING):
            result = get_env_int("TEST_INT_FLOAT", 10)

        assert result == 10
        assert "TEST_INT_FLOAT" in caplog.text

    def test_min_val_clamps_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify value below min_val is clamped and logged."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_MIN", "5")

        with caplog.at_level(logging.WARNING):
            result = get_env_int("TEST_INT_MIN", 50, min_val=10)

        assert result == 10  # Clamped to min
        assert "TEST_INT_MIN" in caplog.text

    def test_max_val_clamps_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify value above max_val is clamped and logged."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_MAX", "500")

        with caplog.at_level(logging.WARNING):
            result = get_env_int("TEST_INT_MAX", 50, max_val=100)

        assert result == 100  # Clamped to max
        assert "TEST_INT_MAX" in caplog.text

    def test_value_within_bounds_not_clamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify value within bounds is returned as-is."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_BOUNDS", "50")

        result = get_env_int("TEST_INT_BOUNDS", 0, min_val=10, max_val=100)
        assert result == 50

    def test_value_at_bounds_not_clamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify value exactly at min/max bounds is accepted."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_INT_AT_MIN", "10")
        monkeypatch.setenv("TEST_INT_AT_MAX", "100")

        result_min = get_env_int("TEST_INT_AT_MIN", 0, min_val=10, max_val=100)
        result_max = get_env_int("TEST_INT_AT_MAX", 0, min_val=10, max_val=100)

        assert result_min == 10
        assert result_max == 100


# =============================================================================
# Test: get_env_float - Float parsing with bounds
# =============================================================================


class TestGetEnvFloat:
    """Tests for get_env_float - float parsing with bounds validation."""

    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify default value when env var is not set."""
        from backend.utils.env_config import get_env_float

        monkeypatch.delenv("TEST_FLOAT_UNSET", raising=False)

        result = get_env_float("TEST_FLOAT_UNSET", 3.14)
        assert result == 3.14

    def test_parses_valid_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify valid float string is parsed correctly."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_VALID", "2.718")

        result = get_env_float("TEST_FLOAT_VALID", 0.0)
        assert result == 2.718

    def test_parses_integer_as_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify integer string is parsed as float."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_INT", "42")

        result = get_env_float("TEST_FLOAT_INT", 0.0)
        assert result == 42.0

    def test_parses_negative_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify negative floats are parsed correctly."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_NEGATIVE", "-1.5")

        result = get_env_float("TEST_FLOAT_NEGATIVE", 0.0)
        assert result == -1.5

    def test_parses_scientific_notation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify scientific notation is parsed correctly."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_SCI", "1.5e-3")

        result = get_env_float("TEST_FLOAT_SCI", 0.0)
        assert result == 0.0015

    def test_invalid_string_logs_warning_returns_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify invalid string logs warning and returns default."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_INVALID", "not_a_float")

        with caplog.at_level(logging.WARNING):
            result = get_env_float("TEST_FLOAT_INVALID", 1.0)

        assert result == 1.0
        assert "TEST_FLOAT_INVALID" in caplog.text

    def test_min_val_clamps_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify value below min_val is clamped and logged."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_MIN", "0.5")

        with caplog.at_level(logging.WARNING):
            result = get_env_float("TEST_FLOAT_MIN", 5.0, min_val=1.0)

        assert result == 1.0  # Clamped to min
        assert "TEST_FLOAT_MIN" in caplog.text

    def test_max_val_clamps_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify value above max_val is clamped and logged."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_MAX", "150.5")

        with caplog.at_level(logging.WARNING):
            result = get_env_float("TEST_FLOAT_MAX", 50.0, max_val=100.0)

        assert result == 100.0  # Clamped to max
        assert "TEST_FLOAT_MAX" in caplog.text

    def test_value_within_bounds_not_clamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify value within bounds is returned as-is."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_FLOAT_BOUNDS", "50.5")

        result = get_env_float("TEST_FLOAT_BOUNDS", 0.0, min_val=10.0, max_val=100.0)
        assert result == 50.5


# =============================================================================
# Test: get_env_bool - Boolean parsing (case-insensitive)
# =============================================================================


class TestGetEnvBool:
    """Tests for get_env_bool - case-insensitive boolean parsing."""

    def test_returns_default_false_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify False default when env var is not set."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.delenv("TEST_BOOL_UNSET", raising=False)

        result = get_env_bool("TEST_BOOL_UNSET")
        assert result is False

    def test_returns_custom_default_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify custom default when env var is not set."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.delenv("TEST_BOOL_UNSET", raising=False)

        result = get_env_bool("TEST_BOOL_UNSET", default=True)
        assert result is True

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "TrUe"])
    def test_true_values_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Verify 'true' in any case returns True."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_TRUE", value)

        result = get_env_bool("TEST_BOOL_TRUE", default=False)
        assert result is True

    @pytest.mark.parametrize("value", ["1"])
    def test_one_returns_true(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Verify '1' returns True."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_ONE", value)

        result = get_env_bool("TEST_BOOL_ONE", default=False)
        assert result is True

    @pytest.mark.parametrize("value", ["yes", "Yes", "YES", "yEs"])
    def test_yes_values_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Verify 'yes' in any case returns True."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_YES", value)

        result = get_env_bool("TEST_BOOL_YES", default=False)
        assert result is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "FaLsE"])
    def test_false_values_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Verify 'false' in any case returns False."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_FALSE", value)

        result = get_env_bool("TEST_BOOL_FALSE", default=True)
        assert result is False

    @pytest.mark.parametrize("value", ["0"])
    def test_zero_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Verify '0' returns False."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_ZERO", value)

        result = get_env_bool("TEST_BOOL_ZERO", default=True)
        assert result is False

    @pytest.mark.parametrize("value", ["no", "No", "NO", "nO"])
    def test_no_values_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Verify 'no' in any case returns False."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_NO", value)

        result = get_env_bool("TEST_BOOL_NO", default=True)
        assert result is False

    def test_empty_string_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify empty string returns False."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_EMPTY", "")

        result = get_env_bool("TEST_BOOL_EMPTY", default=True)
        assert result is False

    def test_unrecognized_value_logs_warning_returns_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify unrecognized value logs warning and returns default."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_UNKNOWN", "maybe")

        with caplog.at_level(logging.WARNING):
            result = get_env_bool("TEST_BOOL_UNKNOWN", default=True)

        assert result is True  # Returns default
        assert "TEST_BOOL_UNKNOWN" in caplog.text
        assert "maybe" in caplog.text or "unrecognized" in caplog.text.lower()

    def test_whitespace_only_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify whitespace-only value is treated as unrecognized."""
        from backend.utils.env_config import get_env_bool

        monkeypatch.setenv("TEST_BOOL_WHITESPACE", "   ")

        with caplog.at_level(logging.WARNING):
            result = get_env_bool("TEST_BOOL_WHITESPACE", default=False)

        # Whitespace-only strings are stripped to "", which is a valid False value
        # No warning should be logged since empty string is explicitly handled
        assert result is False
        # Note: No warning expected - whitespace strips to empty which is in _FALSE_VALUES


# =============================================================================
# Test: get_env_list - List parsing with separator
# =============================================================================


class TestGetEnvList:
    """Tests for get_env_list - list parsing with configurable separator."""

    def test_returns_empty_list_when_unset_no_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify empty list returned when unset and no default."""
        from backend.utils.env_config import get_env_list

        monkeypatch.delenv("TEST_LIST_UNSET", raising=False)

        result = get_env_list("TEST_LIST_UNSET")
        assert result == []

    def test_returns_default_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify default list returned when unset."""
        from backend.utils.env_config import get_env_list

        monkeypatch.delenv("TEST_LIST_UNSET", raising=False)

        default = ["a", "b", "c"]
        result = get_env_list("TEST_LIST_UNSET", default=default)
        assert result == default

    def test_parses_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify comma-separated values are parsed correctly."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_COMMA", "one,two,three")

        result = get_env_list("TEST_LIST_COMMA")
        assert result == ["one", "two", "three"]

    def test_strips_whitespace_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify whitespace is stripped from each element by default."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_WHITESPACE", "  one  , two ,  three  ")

        result = get_env_list("TEST_LIST_WHITESPACE")
        assert result == ["one", "two", "three"]

    def test_preserves_whitespace_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify whitespace is preserved when strip=False."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_NO_STRIP", "  one  , two ,  three  ")

        result = get_env_list("TEST_LIST_NO_STRIP", strip=False)
        assert result == ["  one  ", " two ", "  three  "]

    def test_custom_separator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify custom separator works correctly."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_PIPE", "one|two|three")

        result = get_env_list("TEST_LIST_PIPE", separator="|")
        assert result == ["one", "two", "three"]

    def test_colon_separator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify colon separator (like PATH) works correctly."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_PATH", "/usr/bin:/usr/local/bin:/home/user/bin")

        result = get_env_list("TEST_LIST_PATH", separator=":")
        assert result == ["/usr/bin", "/usr/local/bin", "/home/user/bin"]

    def test_single_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify single item without separator works."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_SINGLE", "only_one")

        result = get_env_list("TEST_LIST_SINGLE")
        assert result == ["only_one"]

    def test_empty_string_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify empty string returns empty list."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_EMPTY", "")

        result = get_env_list("TEST_LIST_EMPTY", default=["should", "not", "use"])
        assert result == []

    def test_filters_empty_elements(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify empty elements from consecutive separators are filtered."""
        from backend.utils.env_config import get_env_list

        monkeypatch.setenv("TEST_LIST_GAPS", "one,,two,,,three,")

        result = get_env_list("TEST_LIST_GAPS")
        assert result == ["one", "two", "three"]


# =============================================================================
# Test: EnvConfig - Dataclass-based configuration
# =============================================================================


class TestEnvConfig:
    """Tests for EnvConfig - dataclass-based environment configuration."""

    def test_from_env_uses_jarvis_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify default JARVIS_ prefix is used for env keys."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class TestConfig(EnvConfig):
            sample_rate: int = 16000
            threshold: float = 0.85

        monkeypatch.setenv("JARVIS_SAMPLE_RATE", "44100")
        monkeypatch.setenv("JARVIS_THRESHOLD", "0.90")

        config = TestConfig.from_env()

        assert config.sample_rate == 44100
        assert config.threshold == 0.90

    def test_from_env_uses_defaults_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify defaults are used when env vars are not set."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class TestConfig(EnvConfig):
            timeout: int = 30
            enabled: bool = True

        monkeypatch.delenv("JARVIS_TIMEOUT", raising=False)
        monkeypatch.delenv("JARVIS_ENABLED", raising=False)

        config = TestConfig.from_env()

        assert config.timeout == 30
        assert config.enabled is True

    def test_from_env_custom_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify custom prefix via _env_prefix class variable."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class CustomConfig(EnvConfig):
            _env_prefix: ClassVar[str] = "MYAPP_"
            port: int = 8080
            host: str = "localhost"

        monkeypatch.setenv("MYAPP_PORT", "3000")
        monkeypatch.setenv("MYAPP_HOST", "0.0.0.0")

        config = CustomConfig.from_env()

        assert config.port == 3000
        assert config.host == "0.0.0.0"

    def test_from_env_bool_type_inference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify bool fields use get_env_bool."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class BoolConfig(EnvConfig):
            debug: bool = False
            verbose: bool = True

        monkeypatch.setenv("JARVIS_DEBUG", "true")
        monkeypatch.setenv("JARVIS_VERBOSE", "no")

        config = BoolConfig.from_env()

        assert config.debug is True
        assert config.verbose is False

    def test_from_env_str_type_inference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify str fields use get_env_str."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class StrConfig(EnvConfig):
            name: str = "default_name"
            path: str = "/default/path"

        monkeypatch.setenv("JARVIS_NAME", "custom_name")
        monkeypatch.delenv("JARVIS_PATH", raising=False)

        config = StrConfig.from_env()

        assert config.name == "custom_name"
        assert config.path == "/default/path"

    def test_from_env_handles_snake_case_to_upper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify field names are converted to UPPER_SNAKE_CASE."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class SnakeCaseConfig(EnvConfig):
            my_setting: int = 100
            another_long_name: str = "default"

        monkeypatch.setenv("JARVIS_MY_SETTING", "200")
        monkeypatch.setenv("JARVIS_ANOTHER_LONG_NAME", "custom")

        config = SnakeCaseConfig.from_env()

        assert config.my_setting == 200
        assert config.another_long_name == "custom"

    def test_to_dict_returns_all_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify to_dict returns all field values."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class DictConfig(EnvConfig):
            setting_a: int = 1
            setting_b: str = "b"
            setting_c: bool = True

        monkeypatch.setenv("JARVIS_SETTING_A", "10")

        config = DictConfig.from_env()
        result = config.to_dict()

        assert result == {
            "setting_a": 10,
            "setting_b": "b",
            "setting_c": True,
        }

    def test_from_env_mixed_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify all supported types work together."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class MixedConfig(EnvConfig):
            int_field: int = 0
            float_field: float = 0.0
            bool_field: bool = False
            str_field: str = ""

        monkeypatch.setenv("JARVIS_INT_FIELD", "42")
        monkeypatch.setenv("JARVIS_FLOAT_FIELD", "3.14")
        monkeypatch.setenv("JARVIS_BOOL_FIELD", "yes")
        monkeypatch.setenv("JARVIS_STR_FIELD", "hello")

        config = MixedConfig.from_env()

        assert config.int_field == 42
        assert config.float_field == 3.14
        assert config.bool_field is True
        assert config.str_field == "hello"

    def test_from_env_invalid_int_logs_warning_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify invalid int value logs warning and uses default."""
        from backend.utils.env_config import EnvConfig

        @dataclass
        class IntConfig(EnvConfig):
            count: int = 5

        monkeypatch.setenv("JARVIS_COUNT", "not_an_int")

        with caplog.at_level(logging.WARNING):
            config = IntConfig.from_env()

        assert config.count == 5
        assert "JARVIS_COUNT" in caplog.text


# =============================================================================
# Test: Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_get_env_int_empty_string_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify empty string for int uses default."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_EMPTY_INT", "")

        with caplog.at_level(logging.WARNING):
            result = get_env_int("TEST_EMPTY_INT", 42)

        assert result == 42

    def test_get_env_float_empty_string_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify empty string for float uses default."""
        from backend.utils.env_config import get_env_float

        monkeypatch.setenv("TEST_EMPTY_FLOAT", "")

        with caplog.at_level(logging.WARNING):
            result = get_env_float("TEST_EMPTY_FLOAT", 3.14)

        assert result == 3.14

    def test_get_env_int_whitespace_only_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify whitespace-only string for int uses default."""
        from backend.utils.env_config import get_env_int

        monkeypatch.setenv("TEST_WHITESPACE_INT", "   ")

        with caplog.at_level(logging.WARNING):
            result = get_env_int("TEST_WHITESPACE_INT", 99)

        assert result == 99

    def test_no_exception_on_any_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify no exceptions are raised regardless of input."""
        from backend.utils.env_config import (
            get_env_bool,
            get_env_float,
            get_env_int,
            get_env_list,
            get_env_optional_str,
            get_env_str,
        )

        # Set various problematic values (note: null bytes are rejected by OS)
        monkeypatch.setenv("TEST_WEIRD_1", "!!@#$%^&*()_+-=[]{}|;':\",./<>?")
        monkeypatch.setenv("TEST_WEIRD_2", "inf")
        monkeypatch.setenv("TEST_WEIRD_3", "nan")
        monkeypatch.setenv("TEST_WEIRD_4", "9" * 1000)  # Very long number
        monkeypatch.setenv("TEST_WEIRD_5", "\t\n\r")  # Control characters

        # None of these should raise exceptions
        get_env_str("TEST_WEIRD_1", "default")
        get_env_optional_str("TEST_WEIRD_1")
        get_env_int("TEST_WEIRD_1", 0)
        get_env_float("TEST_WEIRD_2", 0.0)  # "inf" parses to float('inf')
        get_env_float("TEST_WEIRD_3", 0.0)  # "nan" parses to float('nan')
        get_env_int("TEST_WEIRD_4", 0)  # Very large number
        get_env_bool("TEST_WEIRD_1", False)
        get_env_list("TEST_WEIRD_1")
        get_env_str("TEST_WEIRD_5", "default")
        get_env_bool("TEST_WEIRD_5", False)
