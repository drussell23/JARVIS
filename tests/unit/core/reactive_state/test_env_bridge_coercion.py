"""Tests for BridgeMode enum and canonical coercion functions.

Covers forward-only mode transitions, string enum membership,
and all five canonical coercion helpers (bool, int, float, str, enum).
"""
from __future__ import annotations

import pytest

from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    canonical_bool,
    canonical_enum,
    canonical_float,
    canonical_int,
    canonical_str,
)


# ── BridgeMode ────────────────────────────────────────────────────────


class TestBridgeMode:
    """BridgeMode is a str enum with forward-only transitions."""

    def test_has_three_modes(self) -> None:
        assert len(BridgeMode) == 3

    def test_values_are_strings(self) -> None:
        for mode in BridgeMode:
            assert isinstance(mode, str)
            assert isinstance(mode.value, str)

    def test_from_string_valid(self) -> None:
        assert BridgeMode("legacy") is BridgeMode.LEGACY
        assert BridgeMode("shadow") is BridgeMode.SHADOW
        assert BridgeMode("active") is BridgeMode.ACTIVE

    def test_from_string_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            BridgeMode("bogus")

    def test_can_transition_forward_only(self) -> None:
        # Valid forward transitions
        assert BridgeMode.LEGACY.can_transition_to(BridgeMode.SHADOW) is True
        assert BridgeMode.SHADOW.can_transition_to(BridgeMode.ACTIVE) is True

        # No self-transitions
        assert BridgeMode.LEGACY.can_transition_to(BridgeMode.LEGACY) is False
        assert BridgeMode.SHADOW.can_transition_to(BridgeMode.SHADOW) is False
        assert BridgeMode.ACTIVE.can_transition_to(BridgeMode.ACTIVE) is False

        # No reverse transitions
        assert BridgeMode.SHADOW.can_transition_to(BridgeMode.LEGACY) is False
        assert BridgeMode.ACTIVE.can_transition_to(BridgeMode.SHADOW) is False
        assert BridgeMode.ACTIVE.can_transition_to(BridgeMode.LEGACY) is False

        # No skip transitions
        assert BridgeMode.LEGACY.can_transition_to(BridgeMode.ACTIVE) is False


# ── canonical_bool ────────────────────────────────────────────────────


class TestCanonicalBool:
    """canonical_bool coerces env-style strings to Optional[bool]."""

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"])
    def test_truthy_strings(self, value: str) -> None:
        assert canonical_bool(value) is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "No", "NO", ""])
    def test_falsy_strings(self, value: str) -> None:
        assert canonical_bool(value) is False

    def test_none_returns_none(self) -> None:
        assert canonical_bool(None) is None

    def test_bool_passthrough(self) -> None:
        assert canonical_bool(True) is True
        assert canonical_bool(False) is False


# ── canonical_int ─────────────────────────────────────────────────────


class TestCanonicalInt:
    """canonical_int coerces env-style strings to Optional[int]."""

    def test_string_to_int(self) -> None:
        assert canonical_int("42") == 42
        assert canonical_int("-7") == -7

    def test_already_int(self) -> None:
        assert canonical_int(99) == 99

    def test_none_returns_none(self) -> None:
        assert canonical_int(None) is None

    def test_empty_returns_none(self) -> None:
        assert canonical_int("") is None

    def test_non_numeric_returns_none(self) -> None:
        assert canonical_int("abc") is None
        assert canonical_int("3.14") is None


# ── canonical_float ───────────────────────────────────────────────────


class TestCanonicalFloat:
    """canonical_float coerces env-style strings to Optional[float]."""

    def test_string_to_float(self) -> None:
        assert canonical_float("3.14") == pytest.approx(3.14)

    def test_int_string_to_float(self) -> None:
        assert canonical_float("42") == pytest.approx(42.0)

    def test_already_float(self) -> None:
        assert canonical_float(2.718) == pytest.approx(2.718)

    def test_already_int_coerces(self) -> None:
        result = canonical_float(7)
        assert isinstance(result, float)
        assert result == pytest.approx(7.0)

    def test_none_returns_none(self) -> None:
        assert canonical_float(None) is None

    def test_empty_returns_none(self) -> None:
        assert canonical_float("") is None


# ── canonical_str ─────────────────────────────────────────────────────


class TestCanonicalStr:
    """canonical_str coerces values to Optional[str]."""

    def test_passthrough(self) -> None:
        assert canonical_str("hello") == "hello"

    def test_none_returns_none(self) -> None:
        assert canonical_str(None) is None

    def test_non_string_coerces(self) -> None:
        assert canonical_str(42) == "42"
        assert canonical_str(True) == "True"


# ── canonical_enum ────────────────────────────────────────────────────


class TestCanonicalEnum:
    """canonical_enum strips whitespace, preserves case, handles None."""

    def test_strips_whitespace(self) -> None:
        assert canonical_enum("  active  ") == "active"

    def test_case_sensitive(self) -> None:
        assert canonical_enum("Active") == "Active"
        assert canonical_enum("ACTIVE") == "ACTIVE"
        assert canonical_enum("active") == "active"

    def test_none_returns_none(self) -> None:
        assert canonical_enum(None) is None

    def test_passthrough(self) -> None:
        assert canonical_enum("shadow") == "shadow"
