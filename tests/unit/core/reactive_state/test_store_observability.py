"""Tests for rejection observability counters in ReactiveStateStore.

Verifies that the store increments per-(key, reason) counters on every
write rejection, enabling downstream dashboards and alerting without
parsing logs.

6 tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import WriteStatus


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> ReactiveStateStore:
    s = ReactiveStateStore(
        journal_path=tmp_path / "obs.db",
        epoch=1,
        session_id="obs-test",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


# -- Tests -------------------------------------------------------------------


class TestRejectionCounters:
    """Per-(key, reason) rejection counters for observability."""

    def test_initial_counters_empty(self, store: ReactiveStateStore) -> None:
        """rejection_stats() returns empty dict before any rejections."""
        assert store.rejection_stats() == {}

    def test_ownership_rejection_counted(self, store: ReactiveStateStore) -> None:
        """An ownership-rejected write increments the counter for that key+reason."""
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=1,
            writer="supervisor",  # wrong owner for gcp.*
        )
        stats = store.rejection_stats()
        assert stats[("gcp.offload_active", "OWNERSHIP_REJECTED")] == 1

    def test_multiple_rejections_accumulated(self, store: ReactiveStateStore) -> None:
        """Repeated rejections accumulate in the same counter bucket."""
        for _ in range(3):
            store.write(
                key="gcp.offload_active",
                value=True,
                expected_version=1,
                writer="supervisor",
            )
        stats = store.rejection_stats()
        assert stats[("gcp.offload_active", "OWNERSHIP_REJECTED")] == 3

    def test_different_reasons_tracked_separately(
        self, store: ReactiveStateStore
    ) -> None:
        """Ownership and schema rejections on the same key produce separate entries."""
        # Ownership rejection
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=1,
            writer="supervisor",
        )
        # Schema rejection (string to a bool key)
        store.write(
            key="gcp.offload_active",
            value="not_a_bool",
            expected_version=1,
            writer="gcp_controller",
        )
        stats = store.rejection_stats()
        assert stats[("gcp.offload_active", "OWNERSHIP_REJECTED")] == 1
        assert stats[("gcp.offload_active", "SCHEMA_INVALID")] == 1

    def test_policy_rejection_counted(self, store: ReactiveStateStore) -> None:
        """A policy-rejected write increments the counter for that key+reason."""
        # gcp.offload_active=True without setting gcp.node_ip triggers policy rejection
        offload = store.read("gcp.offload_active")
        assert offload is not None
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=offload.version,
            writer="gcp_controller",
        )
        stats = store.rejection_stats()
        assert stats[("gcp.offload_active", "POLICY_REJECTED")] == 1

    def test_successful_writes_not_counted(self, store: ReactiveStateStore) -> None:
        """Successful writes do NOT appear in rejection stats."""
        ip_entry = store.read("gcp.node_ip")
        assert ip_entry is not None
        result = store.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=ip_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        stats = store.rejection_stats()
        # No entries for gcp.node_ip should exist
        for (key, _reason), _count in stats.items():
            assert key != "gcp.node_ip"
