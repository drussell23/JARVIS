"""Tests for EnvBridge.get_subprocess_env() method."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from backend.core.reactive_state import (
    BridgeMode,
    EnvBridge,
    ReactiveStateStore,
    WriteStatus,
)
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def schema_registry():
    return build_schema_registry()


@pytest.fixture()
def ownership_registry():
    return build_ownership_registry()


@pytest.fixture()
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def store(tmp_journal, ownership_registry, schema_registry):
    """Open a store, initialize defaults, yield, then close."""
    s = ReactiveStateStore(
        journal_path=tmp_journal,
        epoch=1,
        session_id="w4-subprocess",
        ownership_registry=ownership_registry,
        schema_registry=schema_registry,
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


# -- TestGetSubprocessEnv ---------------------------------------------------


class TestGetSubprocessEnv:
    """EnvBridge.get_subprocess_env() builds env dict from store snapshot."""

    def test_overlays_mapped_keys_from_store(self, schema_registry, store) -> None:
        """Store values for active-domain keys are overlaid onto os.environ copy."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)

        # Write a value to the store
        entry = store.read("gcp.offload_active")
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK

        env = bridge.get_subprocess_env(store)
        assert env["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

    def test_preserves_unmapped_env_vars(self, schema_registry, store) -> None:
        """Env vars NOT in ENV_KEY_MAPPINGS are preserved from os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)

        with mock.patch.dict(os.environ, {"MY_CUSTOM_VAR": "hello"}, clear=False):
            env = bridge.get_subprocess_env(store)
            assert env["MY_CUSTOM_VAR"] == "hello"

    def test_all_values_are_strings(self, schema_registry, store) -> None:
        """Every value in the returned dict is a string."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        env = bridge.get_subprocess_env(store)
        for key, value in env.items():
            assert isinstance(value, str), f"{key}={value!r} is not a string"

    def test_defaults_overlaid_on_fresh_store(self, schema_registry, store) -> None:
        """Even default values from initialize_defaults() are overlaid."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        env = bridge.get_subprocess_env(store)
        # gcp.offload_active default is False -> "false"
        assert env["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"
        # gcp.node_port default is 8000 -> "8000"
        assert env["JARVIS_INVINCIBLE_NODE_PORT"] == "8000"

    def test_respects_domain_kill_switch(self, schema_registry, store) -> None:
        """Keys in inactive domains are NOT overlaid from store."""
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp",
                "JARVIS_CAN_SPAWN_HEAVY": "original",
            },
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            env = bridge.get_subprocess_env(store)
            # gcp domain is active -> overlaid from store
            assert env["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"
            # memory domain is NOT active -> preserved from os.environ
            assert env["JARVIS_CAN_SPAWN_HEAVY"] == "original"

    def test_returns_plain_copy_in_legacy_mode(self, schema_registry, store) -> None:
        """In LEGACY mode, returns os.environ.copy() with no overlay."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.LEGACY)
        with mock.patch.dict(os.environ, {"MY_VAR": "42"}, clear=False):
            env = bridge.get_subprocess_env(store)
            assert env["MY_VAR"] == "42"

    def test_returns_plain_copy_in_shadow_mode(self, schema_registry, store) -> None:
        """In SHADOW mode, returns os.environ.copy() with no overlay."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.SHADOW)
        env = bridge.get_subprocess_env(store)
        assert isinstance(env, dict)

    def test_returns_independent_copy(self, schema_registry, store) -> None:
        """Mutating the returned dict does not affect os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        env = bridge.get_subprocess_env(store)
        env["SHOULD_NOT_LEAK"] = "yes"
        assert "SHOULD_NOT_LEAK" not in os.environ
