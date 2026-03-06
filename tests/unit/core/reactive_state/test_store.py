"""Tests for ReactiveStateStore -- CAS, epoch fencing, ownership, schema, replay.

Covers the full write pipeline (schema validation, coercion, ownership check,
epoch fencing, CAS, journal append, watcher notification) plus read, watch,
global revision, defaults initialization, and journal replay on reopen.

18 tests across 6 test classes.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import StateEntry, WriteStatus


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> ReactiveStateStore:
    s = ReactiveStateStore(
        journal_path=tmp_path / "journal.db",
        epoch=1,
        session_id="test-session-1",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
    )
    s.open()
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


# ── TestStoreWrite ────────────────────────────────────────────────────


class TestStoreWrite:
    """Write pipeline: schema, coercion, ownership, epoch, CAS, journal."""

    def test_first_write_succeeds(self, store: ReactiveStateStore) -> None:
        """First write with expected_version=0 produces version 1."""
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        assert result.entry is not None
        assert result.entry.version == 1
        assert result.entry.value is True
        assert result.entry.key == "gcp.offload_active"
        assert result.entry.epoch == 1
        assert result.entry.writer == "gcp_controller"
        assert result.entry.origin == "explicit"

    def test_cas_succeeds_with_correct_version(
        self, store: ReactiveStateStore
    ) -> None:
        """CAS succeeds when expected_version matches current version."""
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        result = store.write(
            key="gcp.offload_active",
            value=False,
            expected_version=1,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        assert result.entry is not None
        assert result.entry.version == 2
        assert result.entry.value is False

    def test_cas_fails_with_wrong_version(
        self, store: ReactiveStateStore
    ) -> None:
        """CAS fails when expected_version != current version."""
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        result = store.write(
            key="gcp.offload_active",
            value=False,
            expected_version=0,  # Wrong: current is 1
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.VERSION_CONFLICT
        assert result.rejection is not None
        assert result.entry is None

    def test_ownership_rejected(self, store: ReactiveStateStore) -> None:
        """Write by wrong writer for key's domain is rejected."""
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="memory_assessor",  # Wrong: gcp.* owned by gcp_controller
        )
        assert result.status == WriteStatus.OWNERSHIP_REJECTED
        assert result.rejection is not None

    def test_schema_invalid_type(self, store: ReactiveStateStore) -> None:
        """Writing a string to a bool key is rejected."""
        result = store.write(
            key="gcp.offload_active",
            value="not_a_bool",
            expected_version=0,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.SCHEMA_INVALID
        assert result.rejection is not None

    def test_schema_invalid_enum_with_default_with_violation(
        self, store: ReactiveStateStore
    ) -> None:
        """Enum with default_with_violation policy coerces unknown value to default."""
        # memory.tier has unknown_enum_policy="default_with_violation"
        result = store.write(
            key="memory.tier",
            value="totally_bogus_tier",
            expected_version=0,
            writer="memory_assessor",
        )
        assert result.status == WriteStatus.OK
        assert result.entry is not None
        # Should be coerced to the default value ("unknown")
        assert result.entry.value == "unknown"

    def test_schema_invalid_range(self, store: ReactiveStateStore) -> None:
        """Port 0 is rejected when min_value=1."""
        result = store.write(
            key="gcp.node_port",
            value=0,
            expected_version=0,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.SCHEMA_INVALID
        assert result.rejection is not None

    def test_epoch_stale_rejected(self, store: ReactiveStateStore) -> None:
        """Writer with epoch < store epoch is rejected."""
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
            writer_epoch=0,  # Store epoch is 1 → stale
        )
        assert result.status == WriteStatus.EPOCH_STALE
        assert result.rejection is not None


# ── TestStoreRead ─────────────────────────────────────────────────────


class TestStoreRead:
    """Thread-safe reads from in-memory entries."""

    def test_read_nonexistent_returns_none(
        self, store: ReactiveStateStore
    ) -> None:
        """Reading a key that has never been written returns None."""
        assert store.read("gcp.offload_active") is None

    def test_read_after_write(self, store: ReactiveStateStore) -> None:
        """Reading after a successful write returns the correct entry."""
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        entry = store.read("gcp.offload_active")
        assert entry is not None
        assert entry.value is True
        assert entry.version == 1

    def test_read_many_returns_only_existing(
        self, store: ReactiveStateStore
    ) -> None:
        """read_many returns only keys that exist (no None values)."""
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        result = store.read_many(
            ["gcp.offload_active", "gcp.node_booting", "nonexistent.key"]
        )
        assert "gcp.offload_active" in result
        assert "gcp.node_booting" not in result
        assert "nonexistent.key" not in result
        assert len(result) == 1

    def test_read_returns_latest_after_multiple_writes(
        self, store: ReactiveStateStore
    ) -> None:
        """Multiple writes to same key: read returns the latest."""
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        store.write(
            key="gcp.offload_active",
            value=False,
            expected_version=1,
            writer="gcp_controller",
        )
        entry = store.read("gcp.offload_active")
        assert entry is not None
        assert entry.value is False
        assert entry.version == 2


# ── TestStoreWatch ────────────────────────────────────────────────────


class TestStoreWatch:
    """Watcher notification on write success/failure."""

    def test_watcher_notified_on_successful_write(
        self, store: ReactiveStateStore
    ) -> None:
        """Watcher callback fires on successful write."""
        events, cb = _collect_notifications()
        store.watch("gcp.*", cb)

        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        assert len(events) == 1
        old, new = events[0]
        assert old is None  # First write → no old entry
        assert new.value is True

    def test_watcher_not_notified_on_failed_write(
        self, store: ReactiveStateStore
    ) -> None:
        """Watcher callback does NOT fire when write is rejected."""
        events, cb = _collect_notifications()
        store.watch("gcp.*", cb)

        # This will fail: wrong writer
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="memory_assessor",
        )
        assert len(events) == 0

    def test_unwatch_stops_notifications(
        self, store: ReactiveStateStore
    ) -> None:
        """After unwatch, callback no longer fires."""
        events, cb = _collect_notifications()
        watch_id = store.watch("gcp.*", cb)

        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        assert len(events) == 1

        store.unwatch(watch_id)

        store.write(
            key="gcp.offload_active",
            value=False,
            expected_version=1,
            writer="gcp_controller",
        )
        assert len(events) == 1  # No new notification


# ── TestStoreGlobalRevision ───────────────────────────────────────────


class TestStoreGlobalRevision:
    """Global revision tracking via journal."""

    def test_revision_increments_on_success(
        self, store: ReactiveStateStore
    ) -> None:
        """Successful writes increment the global revision."""
        assert store.global_revision() == 0
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        assert store.global_revision() == 1
        store.write(
            key="gcp.offload_active",
            value=False,
            expected_version=1,
            writer="gcp_controller",
        )
        assert store.global_revision() == 2

    def test_revision_does_not_increment_on_failure(
        self, store: ReactiveStateStore
    ) -> None:
        """Failed writes do NOT increment the global revision."""
        assert store.global_revision() == 0
        # Wrong writer → rejected
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="memory_assessor",
        )
        assert store.global_revision() == 0


# ── TestStoreDefaults ─────────────────────────────────────────────────


class TestStoreDefaults:
    """initialize_defaults populates schema-declared keys."""

    def test_initialize_defaults_populates_all_keys(
        self, store: ReactiveStateStore
    ) -> None:
        """All schema-declared keys are populated with defaults."""
        store.initialize_defaults()

        snapshot = store.snapshot()
        all_keys = store._schemas.all_keys()
        for key in all_keys:
            assert key in snapshot, f"Key {key!r} missing from snapshot"
            schema = store._schemas.get(key)
            assert schema is not None
            assert snapshot[key].value == schema.default
            assert snapshot[key].origin == "default"

    def test_initialize_defaults_does_not_overwrite_existing(
        self, store: ReactiveStateStore
    ) -> None:
        """Existing values are NOT overwritten by initialize_defaults."""
        store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        store.initialize_defaults()

        entry = store.read("gcp.offload_active")
        assert entry is not None
        assert entry.value is True  # NOT the default (False)
        assert entry.version == 1  # NOT overwritten


# ── TestStoreReplay ───────────────────────────────────────────────────


class TestStoreReplay:
    """Journal replay restores in-memory state on reopen."""

    def test_replay_on_reopen(self, tmp_path: Path) -> None:
        """Write, close, reopen with new epoch — state is replayed correctly."""
        journal_path = tmp_path / "journal.db"

        # First session
        s1 = ReactiveStateStore(
            journal_path=journal_path,
            epoch=1,
            session_id="session-1",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
        )
        s1.open()
        s1.write(
            key="gcp.offload_active",
            value=True,
            expected_version=0,
            writer="gcp_controller",
        )
        s1.write(
            key="lifecycle.startup_complete",
            value=True,
            expected_version=0,
            writer="supervisor",
        )
        rev_before_close = s1.global_revision()
        s1.close()

        # Second session with new epoch
        s2 = ReactiveStateStore(
            journal_path=journal_path,
            epoch=2,
            session_id="session-2",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
        )
        s2.open()
        try:
            # State should be replayed
            entry_gcp = s2.read("gcp.offload_active")
            assert entry_gcp is not None
            assert entry_gcp.value is True
            assert entry_gcp.version == 1

            entry_lc = s2.read("lifecycle.startup_complete")
            assert entry_lc is not None
            assert entry_lc.value is True

            # Global revision continues from where it left off
            assert s2.global_revision() == rev_before_close

            # New writes with new epoch work
            result = s2.write(
                key="gcp.offload_active",
                value=False,
                expected_version=1,
                writer="gcp_controller",
            )
            assert result.status == WriteStatus.OK
            assert result.entry is not None
            assert result.entry.version == 2
            assert result.entry.epoch == 2
            assert s2.global_revision() == rev_before_close + 1
        finally:
            s2.close()
