"""Tests for EnvBridge class -- construction, bootstrap, and mode transitions.

Covers bootstrap mode resolution from ``JARVIS_STATE_BRIDGE_MODE``,
explicit ``initial_mode`` override, forward-only mode transitions,
and lookup helpers for ``EnvKeyMapping`` entries.
"""
from __future__ import annotations

from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    EnvBridge,
)
from backend.core.reactive_state.manifest import build_schema_registry


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def schema_registry():
    """Build a fresh schema registry for each test."""
    return build_schema_registry()


# ── Bootstrap Resolution ─────────────────────────────────────────────


class TestBootstrapResolution:
    """EnvBridge reads JARVIS_STATE_BRIDGE_MODE from os.environ at init."""

    def test_defaults_to_legacy_when_absent(self, schema_registry) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            bridge = EnvBridge(schema_registry=schema_registry)
        assert bridge.mode is BridgeMode.LEGACY

    def test_reads_shadow_from_env(self, schema_registry) -> None:
        with mock.patch.dict(
            "os.environ", {"JARVIS_STATE_BRIDGE_MODE": "shadow"}, clear=True,
        ):
            bridge = EnvBridge(schema_registry=schema_registry)
        assert bridge.mode is BridgeMode.SHADOW

    def test_reads_active_from_env(self, schema_registry) -> None:
        with mock.patch.dict(
            "os.environ", {"JARVIS_STATE_BRIDGE_MODE": "active"}, clear=True,
        ):
            bridge = EnvBridge(schema_registry=schema_registry)
        assert bridge.mode is BridgeMode.ACTIVE

    def test_invalid_value_defaults_to_legacy(self, schema_registry) -> None:
        with mock.patch.dict(
            "os.environ", {"JARVIS_STATE_BRIDGE_MODE": "turbo"}, clear=True,
        ):
            bridge = EnvBridge(schema_registry=schema_registry)
        assert bridge.mode is BridgeMode.LEGACY

    def test_explicit_mode_overrides_env(self, schema_registry) -> None:
        with mock.patch.dict(
            "os.environ", {"JARVIS_STATE_BRIDGE_MODE": "legacy"}, clear=True,
        ):
            bridge = EnvBridge(
                schema_registry=schema_registry,
                initial_mode=BridgeMode.SHADOW,
            )
        assert bridge.mode is BridgeMode.SHADOW


# ── Mode Transitions ─────────────────────────────────────────────────


class TestModeTransitions:
    """EnvBridge enforces forward-only mode transitions."""

    def test_legacy_to_shadow(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.LEGACY,
        )
        bridge.transition_to(BridgeMode.SHADOW)
        assert bridge.mode is BridgeMode.SHADOW

    def test_shadow_to_active(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
        )
        bridge.transition_to(BridgeMode.ACTIVE)
        assert bridge.mode is BridgeMode.ACTIVE

    def test_skip_not_allowed(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.LEGACY,
        )
        with pytest.raises(ValueError, match="Cannot transition"):
            bridge.transition_to(BridgeMode.ACTIVE)

    def test_reverse_not_allowed(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
        )
        with pytest.raises(ValueError, match="Cannot transition"):
            bridge.transition_to(BridgeMode.LEGACY)

    def test_self_transition_not_allowed(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.LEGACY,
        )
        with pytest.raises(ValueError, match="Cannot transition"):
            bridge.transition_to(BridgeMode.LEGACY)


# ── Bridge Lookups ───────────────────────────────────────────────────


class TestBridgeLookups:
    """EnvBridge provides O(1) lookups by state_key and env_var."""

    def test_lookup_by_state_key(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.LEGACY,
        )
        mapping = bridge.get_mapping_by_state_key("gcp.offload_active")
        assert mapping is not None
        assert mapping.env_var == "JARVIS_GCP_OFFLOAD_ACTIVE"

    def test_lookup_by_state_key_missing(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.LEGACY,
        )
        mapping = bridge.get_mapping_by_state_key("nonexistent.key")
        assert mapping is None

    def test_lookup_by_env_var(self, schema_registry) -> None:
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.LEGACY,
        )
        mapping = bridge.get_mapping_by_env_var("JARVIS_INVINCIBLE_NODE_PORT")
        assert mapping is not None
        assert mapping.state_key == "gcp.node_port"
