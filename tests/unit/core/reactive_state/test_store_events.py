"""Tests for event emitter integration in ReactiveStateStore."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.reactive_state.event_emitter import StateEventEmitter
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import WriteStatus
from backend.core.umf.types import UmfMessage


@pytest.fixture
def published_events():
    return []


@pytest.fixture
def store_with_emitter(tmp_path: Path, published_events):
    async def fake_publish(msg: UmfMessage) -> bool:
        published_events.append(msg)
        return True

    s = ReactiveStateStore(
        journal_path=tmp_path / "events.db",
        epoch=1,
        session_id="event-test",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        event_emitter_factory=lambda journal: StateEventEmitter(
            journal=journal,
            publish_fn=fake_publish,
            instance_id="test-instance",
            session_id="event-test",
        ),
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


class TestStoreEventEmission:
    def test_successful_write_publishes_event(
        self, store_with_emitter, published_events
    ) -> None:
        store = store_with_emitter
        entry = store.read("gcp.node_ip")
        result = store.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK

        # Filter for our explicit write (not defaults)
        ip_events = [
            e for e in published_events
            if e.payload["key"] == "gcp.node_ip" and e.payload["value"] == "10.0.0.1"
        ]
        assert len(ip_events) == 1
        assert ip_events[0].payload["event_type"] == "state.changed"

    def test_rejected_write_does_not_publish(
        self, store_with_emitter, published_events
    ) -> None:
        store = store_with_emitter
        count_before = len(published_events)
        store.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=0,
            writer="wrong_writer",
        )
        assert len(published_events) == count_before

    def test_event_idempotency_key_format(
        self, store_with_emitter, published_events
    ) -> None:
        store = store_with_emitter
        entry = store.read("gcp.node_ip")
        store.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=entry.version,
            writer="gcp_controller",
        )
        ip_events = [
            e for e in published_events
            if e.payload["key"] == "gcp.node_ip" and e.payload["value"] == "10.0.0.1"
        ]
        assert len(ip_events) == 1
        assert ip_events[0].idempotency_key.startswith("state.1.")

    def test_no_emitter_works_fine(self, tmp_path) -> None:
        """Store without event_emitter_factory should work as before."""
        s = ReactiveStateStore(
            journal_path=tmp_path / "no_emitter.db",
            epoch=1,
            session_id="no-emitter",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
        )
        s.open()
        s.initialize_defaults()
        entry = s.read("gcp.node_ip")
        result = s.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK
        s.close()

    def test_publish_cursor_advances_with_writes(
        self, store_with_emitter
    ) -> None:
        store = store_with_emitter
        entry = store.read("gcp.node_ip")
        store.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=entry.version,
            writer="gcp_controller",
        )
        cursor = store._journal.get_publish_cursor()
        assert cursor > 0
