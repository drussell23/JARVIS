"""Tests for EnvBridge active-mode env mirror writes with loop prevention."""
from __future__ import annotations

import os
import time
from typing import Any
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import BridgeMode, EnvBridge
from backend.core.reactive_state.manifest import build_schema_registry
from backend.core.reactive_state.types import StateEntry


# -- Helpers ----------------------------------------------------------------


def _make_entry(key: str, value: Any, version: int = 1) -> StateEntry:
    """Create a minimal ``StateEntry`` for testing."""
    return StateEntry(
        key=key,
        value=value,
        version=version,
        epoch=1,
        writer="test",
        origin="explicit",
        updated_at_mono=time.monotonic(),
        updated_at_unix_ms=int(time.time() * 1000),
    )


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def schema_registry():
    return build_schema_registry()


# -- TestMirrorToEnv --------------------------------------------------------


class TestMirrorToEnv:
    """EnvBridge.mirror_to_env() writes store values to os.environ in ACTIVE mode."""

    def test_mirrors_bool_to_env(self, schema_registry) -> None:
        """Bool value True -> 'true' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

    def test_mirrors_int_to_env(self, schema_registry) -> None:
        """Int value 9090 -> '9090' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.node_port", 9090)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_INVINCIBLE_NODE_PORT"] == "9090"

    def test_mirrors_float_to_env(self, schema_registry) -> None:
        """Float value 7.5 -> '7.5' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("memory.available_gb", 7.5)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_HEAVY_ADMISSION_AVAILABLE_GB"] == "7.5"

    def test_mirrors_str_to_env(self, schema_registry) -> None:
        """Str value -> same string in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.node_ip", "10.0.0.42")
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_INVINCIBLE_NODE_IP"] == "10.0.0.42"

    def test_mirrors_enum_to_env(self, schema_registry) -> None:
        """Enum value -> string in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("lifecycle.effective_mode", "cloud_first")
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_STARTUP_EFFECTIVE_MODE"] == "cloud_first"

    def test_mirrors_none_to_empty_string(self, schema_registry) -> None:
        """None value -> '' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("prime.early_pid", None)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_PRIME_EARLY_PID"] == ""

    def test_noop_in_legacy_mode(self, schema_registry) -> None:
        """mirror_to_env returns False and does nothing in LEGACY mode."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.LEGACY)
        entry = _make_entry("gcp.offload_active", True)
        result = bridge.mirror_to_env(entry)
        assert result is False

    def test_noop_in_shadow_mode(self, schema_registry) -> None:
        """mirror_to_env returns False and does nothing in SHADOW mode."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.SHADOW)
        entry = _make_entry("gcp.offload_active", True)
        result = bridge.mirror_to_env(entry)
        assert result is False

    def test_unmapped_key_returns_false(self, schema_registry) -> None:
        """Keys not in ENV_KEY_MAPPINGS are silently skipped."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("unknown.key.not.mapped", "value")
        result = bridge.mirror_to_env(entry)
        assert result is False


# -- TestVersionGuard -------------------------------------------------------


class TestVersionGuard:
    """Version guard prevents re-mirroring the same version (loop prevention A.7)."""

    def test_skips_same_version(self, schema_registry) -> None:
        """Mirroring the same version twice -> second call returns False."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.offload_active", True, version=1)
        with mock.patch.dict(os.environ, {}, clear=False):
            assert bridge.mirror_to_env(entry) is True
            assert bridge.mirror_to_env(entry) is False

    def test_allows_new_version(self, schema_registry) -> None:
        """Different version -> mirrors again and updates env value."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry_v1 = _make_entry("gcp.offload_active", True, version=1)
        entry_v2 = _make_entry("gcp.offload_active", False, version=2)
        with mock.patch.dict(os.environ, {}, clear=False):
            assert bridge.mirror_to_env(entry_v1) is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"
            assert bridge.mirror_to_env(entry_v2) is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"

    def test_independent_keys_have_independent_guards(self, schema_registry) -> None:
        """Version guard tracks per-key, not globally."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry_a = _make_entry("gcp.offload_active", True, version=1)
        entry_b = _make_entry("gcp.node_port", 9090, version=1)
        with mock.patch.dict(os.environ, {}, clear=False):
            assert bridge.mirror_to_env(entry_a) is True
            assert bridge.mirror_to_env(entry_b) is True
            # Re-mirror same versions -> both skip
            assert bridge.mirror_to_env(entry_a) is False
            assert bridge.mirror_to_env(entry_b) is False


# -- TestShadowCompareActiveNoop -------------------------------------------


class TestShadowCompareActiveNoop:
    """shadow_compare is a no-op in ACTIVE mode (store is authoritative)."""

    def test_shadow_compare_noop_in_active_mode(self, schema_registry) -> None:
        """In ACTIVE mode, shadow_compare does not record any comparisons."""
        from backend.core.umf.shadow_parity import ShadowParityLogger

        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.ACTIVE,
            parity_logger=parity,
        )
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}, clear=False):
            bridge.shadow_compare(entry, global_revision=1)
        assert parity.total_comparisons == 0
