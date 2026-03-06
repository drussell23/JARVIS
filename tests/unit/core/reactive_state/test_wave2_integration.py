"""Wave 2 integration -- event emission + reconciler end-to-end."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.reactive_state import (
    ReactiveStateStore,
    WriteStatus,
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.event_emitter import (
    PublishReconciler,
    StateEventEmitter,
)
from backend.core.reactive_state.journal import AppendOnlyJournal
from backend.core.umf.types import UmfMessage


class TestWave2Integration:
    def test_write_publish_reconcile_lifecycle(self, tmp_path: Path) -> None:
        """Full lifecycle: write -> event published -> cursor advances -> reconcile is no-op."""
        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg)
            return True

        s = ReactiveStateStore(
            journal_path=tmp_path / "w2.db",
            epoch=1,
            session_id="w2-int",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            event_emitter_factory=lambda journal: StateEventEmitter(
                journal=journal,
                publish_fn=fake_publish,
                instance_id="w2-inst",
                session_id="w2-int",
            ),
        )
        s.open()
        s.initialize_defaults()

        # Write a value
        ip = s.read("gcp.node_ip")
        r = s.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=ip.version,
            writer="gcp_controller",
        )
        assert r.status == WriteStatus.OK

        # Event was published
        ip_events = [
            e for e in published
            if e.payload["key"] == "gcp.node_ip" and e.payload["value"] == "10.0.0.1"
        ]
        assert len(ip_events) == 1
        assert ip_events[0].idempotency_key.startswith("state.1.")

        # Cursor advanced
        cursor = s._journal.get_publish_cursor()
        assert cursor > 0

        # Reconciler finds nothing to do
        reconciler = PublishReconciler(
            journal=s._journal,
            emitter=s._event_emitter,
        )
        count = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile_once()
        )
        assert count == 0

        s.close()

    def test_crash_recovery_reconcile(self, tmp_path: Path) -> None:
        """Simulate crash: write without publish -> reconciler catches up."""
        db_path = tmp_path / "crash.db"

        # Phase 1: Write to store WITHOUT event emitter (simulating crash before publish)
        s1 = ReactiveStateStore(
            journal_path=db_path,
            epoch=1,
            session_id="s1",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
        )
        s1.open()
        s1.initialize_defaults()
        ip = s1.read("gcp.node_ip")
        s1.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=ip.version,
            writer="gcp_controller",
        )
        s1.close()

        # Phase 2: "Restart" with event emitter -- reconciler catches up
        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg)
            return True

        s2 = ReactiveStateStore(
            journal_path=db_path,
            epoch=2,
            session_id="s2",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            event_emitter_factory=lambda journal: StateEventEmitter(
                journal=journal,
                publish_fn=fake_publish,
                instance_id="s2-inst",
                session_id="s2",
            ),
        )
        s2.open()

        # Cursor should be 0 (nothing was published in phase 1)
        assert s2._journal.get_publish_cursor() == 0

        # Reconciler replays all unpublished entries
        reconciler = PublishReconciler(
            journal=s2._journal,
            emitter=s2._event_emitter,
        )
        count = asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile_once()
        )

        # All default writes + the explicit IP write should be reconciled
        assert count > 0
        assert s2._journal.get_publish_cursor() == s2._journal.latest_revision()

        # Verify the IP write event is in published
        ip_events = [
            e for e in published
            if e.payload["key"] == "gcp.node_ip" and e.payload["value"] == "10.0.0.1"
        ]
        assert len(ip_events) == 1

        s2.close()

    def test_idempotency_key_prevents_duplicates(self, tmp_path: Path) -> None:
        """Same entry published twice has same idempotency key."""
        from backend.core.reactive_state.event_emitter import build_state_changed_event
        from backend.core.reactive_state.types import JournalEntry

        je = JournalEntry(
            global_revision=42,
            key="gcp.offload_active",
            value=True,
            previous_value=False,
            version=2,
            epoch=3,
            writer="gcp_controller",
            writer_session_id="sess-1",
            origin="explicit",
            consistency_group="gcp_readiness",
            timestamp_unix_ms=1700000000000,
            checksum="abc",
        )

        msg1 = build_state_changed_event(je, instance_id="i1", session_id="s1")
        msg2 = build_state_changed_event(je, instance_id="i1", session_id="s1")

        # Same idempotency key
        assert msg1.idempotency_key == msg2.idempotency_key == "state.3.42"

        # Different message IDs (UUID)
        assert msg1.message_id != msg2.message_id
