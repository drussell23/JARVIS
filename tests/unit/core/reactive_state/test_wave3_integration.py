"""Wave 3 integration -- env bridge shadow-mode end-to-end."""
from __future__ import annotations

import os
import time
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
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger


@pytest.fixture()
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


class TestShadowModeEndToEnd:
    def test_write_triggers_shadow_compare(self, tmp_journal: Path) -> None:
        """Write to store, manually call shadow_compare, verify parity counter."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w3-t1",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
                entry = store.read("gcp.offload_active")
                result = store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=entry.version,
                    writer="gcp_controller",
                )
                assert result.status == WriteStatus.OK

                bridge.shadow_compare(result.entry, store.global_revision())

            assert parity.total_comparisons == 1
            assert parity.mismatches == 0
        finally:
            store.close()

    def test_shadow_watcher_integration(self, tmp_journal: Path) -> None:
        """Register a watcher that calls shadow_compare on every write."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        parity = ShadowParityLogger(min_comparisons=2)
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w3-t2",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            # Register a watcher on "*" that forwards to shadow_compare
            def on_change(old: StateEntry | None, new: StateEntry) -> None:
                bridge.shadow_compare(new, store.global_revision())

            store.watch("*", on_change)

            with mock.patch.dict(
                os.environ,
                {
                    "JARVIS_GCP_OFFLOAD_ACTIVE": "true",
                    "JARVIS_MEMORY_AVAILABLE_GB": "7.5",
                },
            ):
                # Write gcp.offload_active=True
                offload = store.read("gcp.offload_active")
                r1 = store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=offload.version,
                    writer="gcp_controller",
                )
                assert r1.status == WriteStatus.OK

                # Write memory.available_gb=7.5
                mem = store.read("memory.available_gb")
                r2 = store.write(
                    key="memory.available_gb",
                    value=7.5,
                    expected_version=mem.version,
                    writer="memory_assessor",
                )
                assert r2.status == WriteStatus.OK

            assert parity.total_comparisons == 2
            assert parity.mismatches == 0
            assert parity.is_promotion_ready() is True
        finally:
            store.close()

    def test_mode_lifecycle_legacy_to_shadow(self) -> None:
        """In LEGACY mode shadow_compare is a no-op; after transition to SHADOW it records."""
        schema_reg = build_schema_registry()
        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.LEGACY,
            parity_logger=parity,
        )

        # Create a StateEntry manually
        entry = StateEntry(
            key="gcp.offload_active",
            value=True,
            version=1,
            epoch=1,
            writer="gcp_controller",
            origin="explicit",
            updated_at_mono=time.monotonic(),
            updated_at_unix_ms=int(time.time() * 1000),
        )

        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
            # In LEGACY mode, shadow_compare is a no-op
            bridge.shadow_compare(entry, global_revision=1)
            assert parity.total_comparisons == 0

            # Transition to SHADOW
            bridge.transition_to(BridgeMode.SHADOW)

            # Now shadow_compare records
            bridge.shadow_compare(entry, global_revision=2)
            assert parity.total_comparisons == 1
