"""Tests for EnvKeyMapping dataclass and ENV_KEY_MAPPINGS table.

Covers frozen dataclass properties, coercion callable contracts,
table completeness against KEY_SCHEMAS, uniqueness invariants,
naming conventions, and round-trip coercion for every supported type.
"""
from __future__ import annotations

import re
from dataclasses import FrozenInstanceError

import pytest

from backend.core.reactive_state.env_bridge import ENV_KEY_MAPPINGS, EnvKeyMapping
from backend.core.reactive_state.manifest import KEY_SCHEMAS


# ── EnvKeyMapping dataclass ──────────────────────────────────────────


class TestEnvKeyMapping:
    """EnvKeyMapping is a frozen dataclass with the right shape."""

    def test_is_frozen(self) -> None:
        mapping = ENV_KEY_MAPPINGS[0]
        with pytest.raises(FrozenInstanceError):
            mapping.env_var = "SHOULD_FAIL"  # type: ignore[misc]

    def test_has_required_fields(self) -> None:
        mapping = ENV_KEY_MAPPINGS[0]
        assert hasattr(mapping, "env_var")
        assert hasattr(mapping, "state_key")
        assert hasattr(mapping, "coerce_to_env")
        assert hasattr(mapping, "coerce_from_env")
        assert hasattr(mapping, "sensitive")

    def test_coerce_functions_are_callable(self) -> None:
        for mapping in ENV_KEY_MAPPINGS:
            assert callable(mapping.coerce_to_env), (
                f"{mapping.state_key}: coerce_to_env is not callable"
            )
            assert callable(mapping.coerce_from_env), (
                f"{mapping.state_key}: coerce_from_env is not callable"
            )

    def test_sensitive_defaults_to_false(self) -> None:
        mapping = EnvKeyMapping(
            env_var="JARVIS_TEST",
            state_key="test.key",
            coerce_to_env=str,
            coerce_from_env=str,
        )
        assert mapping.sensitive is False


# ── ENV_KEY_MAPPINGS table ───────────────────────────────────────────


class TestEnvKeyMappingsTable:
    """ENV_KEY_MAPPINGS covers the full manifest with no collisions."""

    def test_covers_all_manifest_keys(self) -> None:
        """Every key in KEY_SCHEMAS has a corresponding mapping."""
        schema_keys = {ks.key for ks in KEY_SCHEMAS}
        mapping_keys = {m.state_key for m in ENV_KEY_MAPPINGS}
        assert mapping_keys == schema_keys

    def test_no_duplicate_env_vars(self) -> None:
        env_vars = [m.env_var for m in ENV_KEY_MAPPINGS]
        assert len(env_vars) == len(set(env_vars)), (
            f"Duplicate env vars: {[v for v in env_vars if env_vars.count(v) > 1]}"
        )

    def test_no_duplicate_state_keys(self) -> None:
        state_keys = [m.state_key for m in ENV_KEY_MAPPINGS]
        assert len(state_keys) == len(set(state_keys)), (
            f"Duplicate state keys: {[k for k in state_keys if state_keys.count(k) > 1]}"
        )

    def test_env_var_naming_convention(self) -> None:
        """All env vars start with JARVIS_ and are UPPER_SNAKE_CASE."""
        upper_snake = re.compile(r"^JARVIS_[A-Z][A-Z0-9_]*$")
        for mapping in ENV_KEY_MAPPINGS:
            assert upper_snake.match(mapping.env_var), (
                f"{mapping.env_var!r} does not match JARVIS_UPPER_SNAKE_CASE"
            )

    def test_is_tuple(self) -> None:
        assert isinstance(ENV_KEY_MAPPINGS, tuple)


# ── Coercion round-trips ────────────────────────────────────────────


class TestCoercionRoundTrip:
    """to_env -> from_env produces the original value (or equivalent)."""

    def _find_mapping(self, state_key: str) -> EnvKeyMapping:
        for m in ENV_KEY_MAPPINGS:
            if m.state_key == state_key:
                return m
        raise KeyError(f"No mapping for {state_key!r}")

    def test_bool_roundtrip(self) -> None:
        m = self._find_mapping("lifecycle.startup_complete")
        for val in (True, False):
            env_str = m.coerce_to_env(val)
            assert isinstance(env_str, str)
            result = m.coerce_from_env(env_str)
            assert result is val

    def test_int_roundtrip(self) -> None:
        m = self._find_mapping("gcp.node_port")
        for val in (8000, 1, 65535):
            env_str = m.coerce_to_env(val)
            assert isinstance(env_str, str)
            result = m.coerce_from_env(env_str)
            assert result == val
            assert isinstance(result, int)

    def test_float_roundtrip(self) -> None:
        m = self._find_mapping("memory.available_gb")
        for val in (0.0, 3.14, 15.5):
            env_str = m.coerce_to_env(val)
            assert isinstance(env_str, str)
            result = m.coerce_from_env(env_str)
            assert result == pytest.approx(val)

    def test_str_roundtrip(self) -> None:
        m = self._find_mapping("gcp.node_ip")
        for val in ("10.0.0.1", "", "192.168.1.100"):
            env_str = m.coerce_to_env(val)
            assert isinstance(env_str, str)
            result = m.coerce_from_env(env_str)
            assert result == val

    def test_enum_roundtrip(self) -> None:
        m = self._find_mapping("lifecycle.effective_mode")
        for val in ("local_full", "cloud_first", "minimal"):
            env_str = m.coerce_to_env(val)
            assert isinstance(env_str, str)
            result = m.coerce_from_env(env_str)
            assert result == val

    def test_nullable_int_roundtrip_none(self) -> None:
        m = self._find_mapping("prime.early_pid")
        # None -> "" -> None
        env_str = m.coerce_to_env(None)
        assert env_str == ""
        result = m.coerce_from_env(env_str)
        assert result is None

    def test_nullable_int_roundtrip_value(self) -> None:
        m = self._find_mapping("prime.early_pid")
        env_str = m.coerce_to_env(12345)
        assert env_str == "12345"
        result = m.coerce_from_env(env_str)
        assert result == 12345

    def test_none_to_env_produces_empty_string(self) -> None:
        """All to_env helpers produce '' for None."""
        m_bool = self._find_mapping("lifecycle.startup_complete")
        m_int = self._find_mapping("gcp.node_port")
        m_float = self._find_mapping("memory.available_gb")
        m_str = self._find_mapping("gcp.node_ip")
        m_enum = self._find_mapping("lifecycle.effective_mode")

        assert m_bool.coerce_to_env(None) == ""
        assert m_int.coerce_to_env(None) == ""
        assert m_float.coerce_to_env(None) == ""
        assert m_str.coerce_to_env(None) == ""
        assert m_enum.coerce_to_env(None) == ""
