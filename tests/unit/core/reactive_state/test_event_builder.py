"""Tests for state event builder -- UmfMessage construction from JournalEntry."""
from __future__ import annotations

from backend.core.reactive_state.event_emitter import build_state_changed_event
from backend.core.reactive_state.types import JournalEntry
from backend.core.umf.types import Kind, Stream


def _journal_entry(
    *,
    global_revision: int = 1,
    key: str = "gcp.offload_active",
    value: object = True,
    previous_value: object = False,
    version: int = 2,
    epoch: int = 3,
    writer: str = "gcp_controller",
    writer_session_id: str = "sess-abc-123",
    origin: str = "explicit",
    consistency_group: str | None = "gcp_readiness",
    timestamp_unix_ms: int = 1700000000000,
    checksum: str = "abc123",
) -> JournalEntry:
    return JournalEntry(
        global_revision=global_revision,
        key=key,
        value=value,
        previous_value=previous_value,
        version=version,
        epoch=epoch,
        writer=writer,
        writer_session_id=writer_session_id,
        origin=origin,
        consistency_group=consistency_group,
        timestamp_unix_ms=timestamp_unix_ms,
        checksum=checksum,
    )


class TestBuildStateChangedEvent:
    def test_stream_is_event(self) -> None:
        msg = build_state_changed_event(
            _journal_entry(), instance_id="inst-1", session_id="sess-1"
        )
        assert msg.stream == Stream.event

    def test_kind_is_event(self) -> None:
        msg = build_state_changed_event(
            _journal_entry(), instance_id="inst-1", session_id="sess-1"
        )
        assert msg.kind == Kind.event

    def test_source_fields(self) -> None:
        msg = build_state_changed_event(
            _journal_entry(), instance_id="inst-1", session_id="sess-1"
        )
        assert msg.source.repo == "jarvis"
        assert msg.source.component == "reactive_state_store"
        assert msg.source.instance_id == "inst-1"
        assert msg.source.session_id == "sess-1"

    def test_target_is_broadcast(self) -> None:
        msg = build_state_changed_event(
            _journal_entry(), instance_id="inst-1", session_id="sess-1"
        )
        assert msg.target.repo == "broadcast"
        assert msg.target.component == "*"

    def test_idempotency_key_format(self) -> None:
        je = _journal_entry(epoch=3, global_revision=142)
        msg = build_state_changed_event(je, instance_id="i", session_id="s")
        assert msg.idempotency_key == "state.3.142"

    def test_payload_contains_all_fields(self) -> None:
        je = _journal_entry(
            key="gcp.offload_active",
            value=True,
            previous_value=False,
            version=2,
            epoch=3,
            global_revision=142,
            writer="gcp_controller",
            writer_session_id="sess-abc-123",
            origin="explicit",
            consistency_group="gcp_readiness",
        )
        msg = build_state_changed_event(je, instance_id="i", session_id="s")
        p = msg.payload
        assert p["event_type"] == "state.changed"
        assert p["event_schema_version"] == 1
        assert p["key"] == "gcp.offload_active"
        assert p["value"] is True
        assert p["previous_value"] is False
        assert p["version"] == 2
        assert p["epoch"] == 3
        assert p["global_revision"] == 142
        assert p["writer"] == "gcp_controller"
        assert p["writer_session_id"] == "sess-abc-123"
        assert p["origin"] == "explicit"
        assert p["consistency_group"] == "gcp_readiness"

    def test_routing_partition_key_is_key(self) -> None:
        je = _journal_entry(key="memory.tier")
        msg = build_state_changed_event(je, instance_id="i", session_id="s")
        assert msg.routing_partition_key == "memory.tier"

    def test_null_consistency_group(self) -> None:
        je = _journal_entry(consistency_group=None)
        msg = build_state_changed_event(je, instance_id="i", session_id="s")
        assert msg.payload["consistency_group"] is None
