"""Tests for PolicyEngine integration in ReactiveStateStore write pipeline.

Verifies that the policy engine (step 5) correctly gates writes between the
CAS check and journal append.  Covers rejection of cross-key invariant
violations, pass-through when no engine is configured, and side-effect
isolation (revision not bumped, watchers not notified on rejection).

7 tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import StateEntry, WriteStatus


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store_with_policy(tmp_path: Path) -> ReactiveStateStore:
    s = ReactiveStateStore(
        journal_path=tmp_path / "policy.db",
        epoch=1,
        session_id="policy-test",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


@pytest.fixture
def store_no_policy(tmp_path: Path) -> ReactiveStateStore:
    """Store created WITHOUT a policy engine (None default)."""
    s = ReactiveStateStore(
        journal_path=tmp_path / "nopolicy.db",
        epoch=1,
        session_id="nopolicy-test",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


# ── Helpers ───────────────────────────────────────────────────────────


def _collect_notifications() -> Tuple[
    List[Tuple[Optional[StateEntry], StateEntry]],
    "callable",
]:
    """Return (events_list, callback) for capturing watcher notifications."""
    events: List[Tuple[Optional[StateEntry], StateEntry]] = []

    def _cb(old: Optional[StateEntry], new: StateEntry) -> None:
        events.append((old, new))

    return events, _cb


# ── Tests ─────────────────────────────────────────────────────────────


class TestStorePolicyEnforcement:
    """Policy engine enforcement inside the store write pipeline."""

    def test_offload_rejected_without_ip(
        self, store_with_policy: ReactiveStateStore
    ) -> None:
        """gcp.offload_active=True is rejected when gcp.node_ip is empty (default)."""
        # Defaults have been initialized: gcp.node_ip="" (empty string default)
        entry = store_with_policy.read("gcp.node_ip")
        assert entry is not None
        assert entry.value == ""

        result = store_with_policy.write(
            key="gcp.offload_active",
            value=True,
            expected_version=1,  # version 1 from initialize_defaults
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.POLICY_REJECTED
        assert result.rejection is not None
        assert result.entry is None

    def test_offload_allowed_with_ip(
        self, store_with_policy: ReactiveStateStore
    ) -> None:
        """gcp.offload_active=True succeeds when gcp.node_ip is set."""
        # Set IP first
        ip_entry = store_with_policy.read("gcp.node_ip")
        assert ip_entry is not None
        store_with_policy.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=ip_entry.version,
            writer="gcp_controller",
        )

        # Now offload should be allowed (port has default 8000 from schema)
        offload_entry = store_with_policy.read("gcp.offload_active")
        assert offload_entry is not None
        result = store_with_policy.write(
            key="gcp.offload_active",
            value=True,
            expected_version=offload_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        assert result.entry is not None
        assert result.entry.value is True

    def test_hollow_rejected_without_offload(
        self, store_with_policy: ReactiveStateStore
    ) -> None:
        """hollow.client_active=True is rejected when gcp.offload_active is False."""
        # Defaults: gcp.offload_active=False
        offload_entry = store_with_policy.read("gcp.offload_active")
        assert offload_entry is not None
        assert offload_entry.value is False

        hollow_entry = store_with_policy.read("hollow.client_active")
        assert hollow_entry is not None
        result = store_with_policy.write(
            key="hollow.client_active",
            value=True,
            expected_version=hollow_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.POLICY_REJECTED
        assert result.rejection is not None
        assert result.entry is None

    def test_hollow_allowed_with_offload(
        self, store_with_policy: ReactiveStateStore
    ) -> None:
        """hollow.client_active=True succeeds when offload is active (IP+port+offload set)."""
        # Set IP
        ip_entry = store_with_policy.read("gcp.node_ip")
        assert ip_entry is not None
        store_with_policy.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=ip_entry.version,
            writer="gcp_controller",
        )

        # Activate offload (port already has default 8000)
        offload_entry = store_with_policy.read("gcp.offload_active")
        assert offload_entry is not None
        store_with_policy.write(
            key="gcp.offload_active",
            value=True,
            expected_version=offload_entry.version,
            writer="gcp_controller",
        )

        # Now hollow should be allowed
        hollow_entry = store_with_policy.read("hollow.client_active")
        assert hollow_entry is not None
        result = store_with_policy.write(
            key="hollow.client_active",
            value=True,
            expected_version=hollow_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        assert result.entry is not None
        assert result.entry.value is True

    def test_no_policy_engine_skips_validation(
        self, store_no_policy: ReactiveStateStore
    ) -> None:
        """Without a policy engine, offload=True succeeds even without IP."""
        # Defaults: gcp.node_ip="" (empty), but no policy engine to enforce
        ip_entry = store_no_policy.read("gcp.node_ip")
        assert ip_entry is not None
        assert ip_entry.value == ""

        offload_entry = store_no_policy.read("gcp.offload_active")
        assert offload_entry is not None
        result = store_no_policy.write(
            key="gcp.offload_active",
            value=True,
            expected_version=offload_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        assert result.entry is not None
        assert result.entry.value is True

    def test_policy_rejected_does_not_increment_revision(
        self, store_with_policy: ReactiveStateStore
    ) -> None:
        """A policy-rejected write must not increment the global revision."""
        rev_before = store_with_policy.global_revision()

        # This should be rejected (offload=True with empty IP)
        offload_entry = store_with_policy.read("gcp.offload_active")
        assert offload_entry is not None
        result = store_with_policy.write(
            key="gcp.offload_active",
            value=True,
            expected_version=offload_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.POLICY_REJECTED

        rev_after = store_with_policy.global_revision()
        assert rev_after == rev_before

    def test_watcher_not_notified_on_policy_rejection(
        self, store_with_policy: ReactiveStateStore
    ) -> None:
        """Watchers must NOT be notified when a write is policy-rejected."""
        events, cb = _collect_notifications()
        store_with_policy.watch("gcp.*", cb)

        # Count existing notifications from initialize_defaults
        baseline = len(events)

        # Attempt a policy-rejected write
        offload_entry = store_with_policy.read("gcp.offload_active")
        assert offload_entry is not None
        result = store_with_policy.write(
            key="gcp.offload_active",
            value=True,
            expected_version=offload_entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.POLICY_REJECTED

        # No new notifications should have been dispatched
        assert len(events) == baseline
