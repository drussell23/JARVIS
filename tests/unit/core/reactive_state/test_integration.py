"""Integration smoke tests for the reactive state package.

Verifies that the public API (package-level imports) works end-to-end:
full lifecycle (defaults, read, watch, CAS write, conflict) and
multi-writer ownership isolation.

Disease 8, Wave 0, Task 8.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.core.reactive_state import (
    ReactiveStateStore,
    StateEntry,
    WriteStatus,
    build_ownership_registry,
    build_schema_registry,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> ReactiveStateStore:
    s = ReactiveStateStore(
        journal_path=tmp_path / "integration.db",
        epoch=1,
        session_id="integration-test",
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


# ── Tests ─────────────────────────────────────────────────────────────


class TestFullLifecycle:
    """End-to-end lifecycle: defaults, read, watch, CAS write, conflict."""

    def test_full_lifecycle(self, store: ReactiveStateStore) -> None:
        # 1. Initialize defaults
        store.initialize_defaults()

        # 2. Read a default value and verify it
        entry = store.read("gcp.offload_active")
        assert entry is not None
        assert entry.value is False
        assert entry.origin == "default"

        # 3. Watch "gcp.*" for changes
        events, cb = _collect_notifications()
        watch_id = store.watch("gcp.*", cb)

        # 4. Write gcp.offload_active=True with correct CAS
        expected_version = entry.version
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=expected_version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        assert result.entry is not None
        assert result.entry.value is True
        assert result.entry.version == expected_version + 1

        # 5. Verify watcher received notification
        assert len(events) == 1
        old_entry, new_entry = events[0]
        assert old_entry is not None
        assert old_entry.value is False
        assert new_entry.value is True
        assert new_entry.key == "gcp.offload_active"

        # 6. Attempt CAS conflict (write with stale version)
        stale_version = expected_version  # this is now stale
        conflict_result = store.write(
            key="gcp.offload_active",
            value=False,
            expected_version=stale_version,
            writer="gcp_controller",
        )
        assert conflict_result.status == WriteStatus.VERSION_CONFLICT

        # 7. Verify global_revision = number_of_defaults + 1 explicit write
        num_defaults = len(build_schema_registry().all_keys())
        assert store.global_revision() == num_defaults + 1

        # Cleanup
        store.unwatch(watch_id)


class TestMultiWriterIsolation:
    """Ownership enforcement: writers can only write their own keys."""

    def test_multi_writer_isolation(self, store: ReactiveStateStore) -> None:
        store.initialize_defaults()

        # 1. supervisor cannot write gcp.offload_active
        entry = store.read("gcp.offload_active")
        assert entry is not None
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=entry.version,
            writer="supervisor",
        )
        assert result.status == WriteStatus.OWNERSHIP_REJECTED

        # 2. gcp_controller cannot write lifecycle.startup_complete
        entry = store.read("lifecycle.startup_complete")
        assert entry is not None
        result = store.write(
            key="lifecycle.startup_complete",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OWNERSHIP_REJECTED

        # 3. gcp_controller CAN write gcp.offload_active
        entry = store.read("gcp.offload_active")
        assert entry is not None
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK

        # 4. supervisor CAN write lifecycle.startup_complete
        entry = store.read("lifecycle.startup_complete")
        assert entry is not None
        result = store.write(
            key="lifecycle.startup_complete",
            value=True,
            expected_version=entry.version,
            writer="supervisor",
        )
        assert result.status == WriteStatus.OK
