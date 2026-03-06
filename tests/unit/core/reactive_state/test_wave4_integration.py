"""Wave 4 integration -- env bridge active-mode end-to-end."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
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
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger


@pytest.fixture()
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


class TestActiveModeWriteThrough:
    """Store write -> watcher -> mirror_to_env -> env updated -> get_subprocess_env."""

    def test_watcher_driven_env_mirror(self, tmp_journal: Path) -> None:
        """Write to store, watcher calls mirror_to_env, env var updated."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.ACTIVE,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-e2e-1",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            # Register watcher that mirrors to env
            def on_change(_old: Optional[StateEntry], new: StateEntry) -> None:
                bridge.mirror_to_env(new)

            store.watch("*", on_change)

            with mock.patch.dict(os.environ, {}, clear=False):
                # Write gcp.offload_active=True
                entry = store.read("gcp.offload_active")
                result = store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=entry.version,
                    writer="gcp_controller",
                )
                assert result.status == WriteStatus.OK
                # Watcher should have mirrored the value
                assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

                # Write gcp.node_ip="10.0.0.5"
                ip_entry = store.read("gcp.node_ip")
                result2 = store.write(
                    key="gcp.node_ip",
                    value="10.0.0.5",
                    expected_version=ip_entry.version,
                    writer="gcp_controller",
                )
                assert result2.status == WriteStatus.OK
                assert os.environ["JARVIS_INVINCIBLE_NODE_IP"] == "10.0.0.5"
        finally:
            store.close()

    def test_subprocess_env_reflects_store_writes(self, tmp_journal: Path) -> None:
        """After store writes + mirror, get_subprocess_env returns updated values."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.ACTIVE,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-e2e-2",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            # Write a value
            entry = store.read("memory.available_gb")
            store.write(
                key="memory.available_gb",
                value=15.5,
                expected_version=entry.version,
                writer="memory_assessor",
            )

            env = bridge.get_subprocess_env(store)
            assert env["JARVIS_HEAVY_ADMISSION_AVAILABLE_GB"] == "15.5"
        finally:
            store.close()


class TestFullModeLifecycle:
    """legacy -> shadow -> active with mode-appropriate behavior at each stage."""

    def test_lifecycle_legacy_shadow_active(self, tmp_journal: Path) -> None:
        """Full mode lifecycle with correct behavior at each stage."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.LEGACY,
            parity_logger=parity,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-lifecycle",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            entry = store.read("gcp.offload_active")

            # -- LEGACY: shadow_compare is no-op, mirror_to_env is no-op --
            with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
                bridge.shadow_compare(entry, global_revision=1)
                assert parity.total_comparisons == 0
                assert bridge.mirror_to_env(entry) is False

            # -- Transition to SHADOW --
            bridge.transition_to(BridgeMode.SHADOW)

            # SHADOW: shadow_compare records, mirror_to_env is no-op
            with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
                bridge.shadow_compare(entry, global_revision=2)
                assert parity.total_comparisons == 1
                assert bridge.mirror_to_env(entry) is False

            # -- Transition to ACTIVE --
            bridge.transition_to(BridgeMode.ACTIVE)

            # ACTIVE: shadow_compare is no-op, mirror_to_env writes
            with mock.patch.dict(os.environ, {}, clear=False):
                bridge.shadow_compare(entry, global_revision=3)
                assert parity.total_comparisons == 1  # unchanged!

                result = bridge.mirror_to_env(entry)
                assert result is True
                assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"
        finally:
            store.close()

    def test_domain_kill_switch_with_active_mode(self, tmp_journal: Path) -> None:
        """Per-domain kill switch in active mode: only active domains mirror."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()

        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp"},
            clear=False,
        ):
            bridge = EnvBridge(
                schema_registry=schema_reg,
                initial_mode=BridgeMode.ACTIVE,
            )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-domain",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            # Register watcher that mirrors to env
            def on_change(_old: Optional[StateEntry], new: StateEntry) -> None:
                bridge.mirror_to_env(new)

            store.watch("*", on_change)

            with mock.patch.dict(os.environ, {}, clear=False):
                # Write gcp key (active domain) -> should mirror
                gcp_entry = store.read("gcp.offload_active")
                store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=gcp_entry.version,
                    writer="gcp_controller",
                )
                assert os.environ.get("JARVIS_GCP_OFFLOAD_ACTIVE") == "true"

                # Write memory key (inactive domain) -> should NOT mirror
                mem_entry = store.read("memory.can_spawn_heavy")
                store.write(
                    key="memory.can_spawn_heavy",
                    value=True,
                    expected_version=mem_entry.version,
                    writer="memory_assessor",
                )
                assert "JARVIS_CAN_SPAWN_HEAVY" not in os.environ
        finally:
            store.close()
