"""Tests for PublishReconciler -- background catch-up for unpublished entries."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.reactive_state.event_emitter import (
    PublishReconciler,
    StateEventEmitter,
)
from backend.core.reactive_state.journal import AppendOnlyJournal
from backend.core.umf.types import UmfMessage


def _append_sample(
    journal: AppendOnlyJournal,
    *,
    key: str = "gcp.vm_ready",
    value: object = True,
    previous_value: object = None,
    version: int = 1,
    epoch: int = 0,
    writer: str = "supervisor",
    writer_session_id: str = "sess-abc-123",
    origin: str = "explicit",
    consistency_group: str | None = None,
):
    return journal.append(
        key=key,
        value=value,
        previous_value=previous_value,
        version=version,
        epoch=epoch,
        writer=writer,
        writer_session_id=writer_session_id,
        origin=origin,
        consistency_group=consistency_group,
    )


@pytest.fixture
def journal(tmp_path: Path) -> AppendOnlyJournal:
    j = AppendOnlyJournal(tmp_path / "reconciler.db")
    j.open()
    yield j
    j.close()


class TestPublishReconciler:
    def test_reconcile_publishes_unpublished_entries(self, journal) -> None:
        """Reconciler catches up entries written before it started."""
        _append_sample(journal, key="a", version=1)
        _append_sample(journal, key="b", version=1)
        _append_sample(journal, key="c", version=1)

        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg.payload["key"])
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        reconciler = PublishReconciler(journal=journal, emitter=emitter)
        asyncio.get_event_loop().run_until_complete(reconciler.reconcile_once())

        assert published == ["a", "b", "c"]
        assert journal.get_publish_cursor() == 3

    def test_reconcile_skips_already_published(self, journal) -> None:
        """If cursor is at 2, only entries after 2 are published."""
        _append_sample(journal, key="a", version=1)
        _append_sample(journal, key="b", version=1)
        _append_sample(journal, key="c", version=1)
        journal.advance_publish_cursor(2)

        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg.payload["key"])
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        reconciler = PublishReconciler(journal=journal, emitter=emitter)
        asyncio.get_event_loop().run_until_complete(reconciler.reconcile_once())

        assert published == ["c"]
        assert journal.get_publish_cursor() == 3

    def test_reconcile_noop_when_all_published(self, journal) -> None:
        """No-op if everything is already published."""
        _append_sample(journal, key="a", version=1)
        journal.advance_publish_cursor(1)

        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg)
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        reconciler = PublishReconciler(journal=journal, emitter=emitter)
        asyncio.get_event_loop().run_until_complete(reconciler.reconcile_once())

        assert published == []

    def test_reconcile_stops_on_first_failure(self, journal) -> None:
        """If publish fails mid-batch, cursor advances to last success."""
        _append_sample(journal, key="a", version=1)
        _append_sample(journal, key="b", version=1)
        _append_sample(journal, key="c", version=1)

        call_count = 0

        async def fail_on_second(msg: UmfMessage) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return False
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fail_on_second,
            instance_id="i1", session_id="s1",
        )
        reconciler = PublishReconciler(journal=journal, emitter=emitter)
        asyncio.get_event_loop().run_until_complete(reconciler.reconcile_once())

        # First succeeded (cursor=1), second failed (cursor stays at 1)
        assert journal.get_publish_cursor() == 1

    def test_reconcile_stats(self, journal) -> None:
        _append_sample(journal, key="a", version=1)
        _append_sample(journal, key="b", version=1)

        async def fake_publish(msg: UmfMessage) -> bool:
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        reconciler = PublishReconciler(journal=journal, emitter=emitter)
        asyncio.get_event_loop().run_until_complete(reconciler.reconcile_once())

        stats = reconciler.stats()
        assert stats["reconciled"] == 2
        assert stats["reconcile_runs"] == 1

    def test_reconcile_empty_journal(self, journal) -> None:
        """No crash on empty journal."""
        async def fake_publish(msg: UmfMessage) -> bool:
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        reconciler = PublishReconciler(journal=journal, emitter=emitter)
        asyncio.get_event_loop().run_until_complete(reconciler.reconcile_once())
        assert journal.get_publish_cursor() == 0
