# Disease 8 Cure: Reactive State Propagation — Wave 2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add UMF `state.changed` event emission after journal commit, a durable `last_published_revision` cursor, and a background reconciler that replays unpublished entries on crash recovery.

**Architecture:** A `StateEventEmitter` bridges the synchronous store write pipeline and the async UMF `DeliveryEngine`. After each successful journal append (step 7), the store calls the emitter with the new `JournalEntry`. The emitter constructs a `UmfMessage`, publishes it via a caller-provided async callback, and advances a durable cursor in the journal's SQLite DB. A `PublishReconciler` runs as a background async task that detects gaps between the cursor and the journal's `latest_revision`, replaying unpublished entries. Idempotency keys (`state.{epoch}.{global_revision}`) prevent duplicate processing downstream.

**Tech Stack:** Python 3.9+, stdlib only in the reactive_state package (dataclasses, typing, threading, sqlite3, asyncio). UMF types imported from `backend.core.umf.types` — this is the first cross-package import in the reactive_state package, scoped to the event emitter module only.

**Design doc:** `docs/plans/2026-03-05-reactive-state-propagation-design.md` — Section 6 (UMF Event Integration), Appendix A.9 (Publisher Recovery Edge Cases).

**Wave 0+1 code (already built and tagged `disease8-wave1`):**
- `backend/core/reactive_state/types.py` — StateEntry, WriteResult, WriteStatus, WriteRejection, JournalEntry
- `backend/core/reactive_state/schemas.py` — KeySchema, SchemaRegistry
- `backend/core/reactive_state/ownership.py` — OwnershipRule, OwnershipRegistry
- `backend/core/reactive_state/journal.py` — AppendOnlyJournal (SQLite WAL)
- `backend/core/reactive_state/watchers.py` — WatcherManager
- `backend/core/reactive_state/manifest.py` — OWNERSHIP_RULES, KEY_SCHEMAS, CONSISTENCY_GROUPS, builders
- `backend/core/reactive_state/store.py` — ReactiveStateStore (8-step write pipeline with policy + audit + rejection counters)
- `backend/core/reactive_state/policy.py` — PolicyEngine, 3 invariant rules
- `backend/core/reactive_state/audit.py` — AuditLog, post_replay_invariant_audit

**UMF infrastructure (existing):**
- `backend/core/umf/types.py` — UmfMessage, Stream, Kind, MessageSource, MessageTarget, Priority
- `backend/core/umf/delivery_engine.py` — DeliveryEngine (async pub/sub with dedup ledger)
- `backend/core/umf/dedup_ledger.py` — SqliteDedupLedger (idempotency guard)

---

## Task 1: Publish cursor — durable `last_published_revision` in journal DB

**Files:**
- Modify: `backend/core/reactive_state/journal.py`
- Test: `tests/unit/core/reactive_state/test_publish_cursor.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_publish_cursor.py
"""Tests for durable publish cursor in the journal DB."""
from __future__ import annotations

import pytest

from backend.core.reactive_state.journal import AppendOnlyJournal


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


class TestPublishCursor:
    def test_initial_cursor_is_zero(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            assert j.get_publish_cursor() == 0
        finally:
            j.close()

    def test_advance_cursor(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            j.advance_publish_cursor(2)
            assert j.get_publish_cursor() == 2
        finally:
            j.close()

    def test_cursor_monotonic_rejects_backward(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            j.advance_publish_cursor(2)
            # Attempting to move backward raises ValueError
            with pytest.raises(ValueError, match="monotonic"):
                j.advance_publish_cursor(1)
        finally:
            j.close()

    def test_cursor_persists_across_reopen(self, tmp_path) -> None:
        db = tmp_path / "j.db"
        j = AppendOnlyJournal(db)
        j.open()
        _append_sample(j, key="a", version=1)
        j.advance_publish_cursor(1)
        j.close()

        j2 = AppendOnlyJournal(db)
        j2.open()
        try:
            assert j2.get_publish_cursor() == 1
        finally:
            j2.close()

    def test_unpublished_entries(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)  # rev 1
            _append_sample(j, key="b", version=1)  # rev 2
            _append_sample(j, key="c", version=1)  # rev 3

            j.advance_publish_cursor(1)
            unpublished = j.read_unpublished()
            assert len(unpublished) == 2
            assert unpublished[0].global_revision == 2
            assert unpublished[1].global_revision == 3
        finally:
            j.close()

    def test_unpublished_entries_when_all_published(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            j.advance_publish_cursor(1)
            assert j.read_unpublished() == []
        finally:
            j.close()

    def test_unpublished_entries_when_none_published(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            unpublished = j.read_unpublished()
            assert len(unpublished) == 2
        finally:
            j.close()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_publish_cursor.py -v`
Expected: FAIL — `AttributeError: 'AppendOnlyJournal' object has no attribute 'get_publish_cursor'`

**Step 3: Implement publish cursor in journal.py**

Add these SQL constants after the existing ones:

```python
_CREATE_CURSOR_TABLE = """\
CREATE TABLE IF NOT EXISTS publish_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_published  INTEGER NOT NULL DEFAULT 0
)
"""

_UPSERT_CURSOR = """\
INSERT INTO publish_cursor (id, last_published) VALUES (1, ?)
ON CONFLICT(id) DO UPDATE SET last_published = excluded.last_published
"""

_SELECT_CURSOR = """\
SELECT last_published FROM publish_cursor WHERE id = 1
"""

_SELECT_UNPUBLISHED = """\
SELECT global_revision, key, value, previous_value, version, epoch,
       writer, writer_session_id, origin, consistency_group,
       timestamp_unix_ms, checksum
FROM state_journal
WHERE global_revision > ?
ORDER BY global_revision
"""
```

In `open()`, after creating the journal table and indexes, add:
```python
self._conn.execute(_CREATE_CURSOR_TABLE)
```

Add three new public methods to `AppendOnlyJournal`:

```python
def get_publish_cursor(self) -> int:
    """Return the last published revision, or 0 if none published yet."""
    assert self._conn is not None, "Journal not opened"
    cur = self._conn.execute(_SELECT_CURSOR)
    row = cur.fetchone()
    return row[0] if row is not None else 0

def advance_publish_cursor(self, revision: int) -> None:
    """Advance the publish cursor to *revision*.

    Raises ``ValueError`` if *revision* is less than the current cursor
    (cursor is monotonic-only).
    """
    assert self._conn is not None, "Journal not opened"
    with self._lock:
        current = self.get_publish_cursor()
        if revision < current:
            raise ValueError(
                f"Publish cursor is monotonic: cannot move from {current} to {revision}"
            )
        self._conn.execute(_UPSERT_CURSOR, (revision,))
        self._conn.commit()

def read_unpublished(self) -> List[JournalEntry]:
    """Return all journal entries with revision > publish cursor."""
    assert self._conn is not None, "Journal not opened"
    cursor = self.get_publish_cursor()
    cur = self._conn.execute(_SELECT_UNPUBLISHED, (cursor,))
    return [self._row_to_entry(row) for row in cur.fetchall()]
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_publish_cursor.py -v`
Expected: All 7 tests PASS

**Step 5: Run full suite to check for regressions**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All 140 tests PASS (133 existing + 7 new)

**Step 6: Commit**

```bash
git add backend/core/reactive_state/journal.py tests/unit/core/reactive_state/test_publish_cursor.py
git commit -m "feat(disease8): add durable publish cursor to journal DB (Wave 2, Task 1)"
```

---

## Task 2: State event builder — construct UmfMessage from JournalEntry

**Files:**
- Create: `backend/core/reactive_state/event_emitter.py`
- Test: `tests/unit/core/reactive_state/test_event_builder.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_event_builder.py
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
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_event_builder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.reactive_state.event_emitter'`

**Step 3: Implement the event builder**

```python
# backend/core/reactive_state/event_emitter.py
"""State event emitter -- bridges reactive state store and UMF transport.

Constructs ``UmfMessage`` envelopes from ``JournalEntry`` records and
provides a publish-with-cursor-advance helper for the store's write
pipeline.

Design rules
------------
* Imports ``backend.core.umf.types`` for the UMF envelope types -- this
  is the **only** cross-package import in the reactive_state package.
* All other imports are stdlib or sibling modules.
* The event builder is a pure function (no side effects).
* Publish cursor advancement is delegated to the journal.
"""
from __future__ import annotations

from backend.core.reactive_state.types import JournalEntry
from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Priority,
    Stream,
    UmfMessage,
)

# ── Constants ──────────────────────────────────────────────────────────

STATE_EVENT_SCHEMA_VERSION: int = 1


# ── Event builder ──────────────────────────────────────────────────────


def build_state_changed_event(
    entry: JournalEntry,
    *,
    instance_id: str,
    session_id: str,
) -> UmfMessage:
    """Build a ``state.changed`` UMF event from a journal entry.

    Parameters
    ----------
    entry:
        The journal entry representing the committed state change.
    instance_id:
        Instance identifier for the source field.
    session_id:
        Session identifier for the source field.

    Returns
    -------
    UmfMessage
        Fully populated event message ready for publish.
    """
    return UmfMessage(
        stream=Stream.event,
        kind=Kind.event,
        source=MessageSource(
            repo="jarvis",
            component="reactive_state_store",
            instance_id=instance_id,
            session_id=session_id,
        ),
        target=MessageTarget(repo="broadcast", component="*"),
        payload={
            "event_type": "state.changed",
            "event_schema_version": STATE_EVENT_SCHEMA_VERSION,
            "key": entry.key,
            "value": entry.value,
            "previous_value": entry.previous_value,
            "version": entry.version,
            "epoch": entry.epoch,
            "global_revision": entry.global_revision,
            "writer": entry.writer,
            "writer_session_id": entry.writer_session_id,
            "origin": entry.origin,
            "consistency_group": entry.consistency_group,
        },
        idempotency_key=f"state.{entry.epoch}.{entry.global_revision}",
        routing_partition_key=entry.key,
        routing_priority=Priority.normal,
    )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_event_builder.py -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add backend/core/reactive_state/event_emitter.py tests/unit/core/reactive_state/test_event_builder.py
git commit -m "feat(disease8): add state.changed event builder from JournalEntry (Wave 2, Task 2)"
```

---

## Task 3: StateEventEmitter — publish + cursor advance helper

**Files:**
- Modify: `backend/core/reactive_state/event_emitter.py`
- Test: `tests/unit/core/reactive_state/test_event_emitter.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_event_emitter.py
"""Tests for StateEventEmitter -- publish + cursor advance."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.core.reactive_state.event_emitter import StateEventEmitter
from backend.core.reactive_state.journal import AppendOnlyJournal
from backend.core.reactive_state.types import JournalEntry
from backend.core.umf.types import UmfMessage


def _journal_entry(
    *,
    global_revision: int = 1,
    key: str = "gcp.offload_active",
    value: object = True,
    previous_value: object = False,
    version: int = 1,
    epoch: int = 1,
    writer: str = "gcp_controller",
    writer_session_id: str = "sess-1",
    origin: str = "explicit",
    consistency_group: str | None = None,
    timestamp_unix_ms: int = 1700000000000,
    checksum: str = "abc",
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


@pytest.fixture
def journal(tmp_path: Path) -> AppendOnlyJournal:
    j = AppendOnlyJournal(tmp_path / "emitter.db")
    j.open()
    yield j
    j.close()


class TestStateEventEmitter:
    def test_publish_calls_callback_and_advances_cursor(self, journal) -> None:
        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg)
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))

        assert len(published) == 1
        assert published[0].payload["key"] == "gcp.offload_active"
        assert journal.get_publish_cursor() == 1

    def test_publish_failure_does_not_advance_cursor(self, journal) -> None:
        async def failing_publish(msg: UmfMessage) -> bool:
            return False

        emitter = StateEventEmitter(
            journal=journal, publish_fn=failing_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))
        assert journal.get_publish_cursor() == 0

    def test_publish_exception_does_not_advance_cursor(self, journal) -> None:
        async def exploding_publish(msg: UmfMessage) -> bool:
            raise RuntimeError("network down")

        emitter = StateEventEmitter(
            journal=journal, publish_fn=exploding_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))
        # Cursor stays at 0, no exception propagated
        assert journal.get_publish_cursor() == 0

    def test_multiple_publishes_advance_cursor_monotonically(self, journal) -> None:
        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg)
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        for rev in [1, 2, 3]:
            je = _journal_entry(global_revision=rev, key=f"k{rev}", version=rev)
            asyncio.get_event_loop().run_until_complete(emitter.publish(je))

        assert len(published) == 3
        assert journal.get_publish_cursor() == 3

    def test_publish_stats(self, journal) -> None:
        async def fake_publish(msg: UmfMessage) -> bool:
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))

        stats = emitter.stats()
        assert stats["published"] == 1
        assert stats["failed"] == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_event_emitter.py -v`
Expected: FAIL — `ImportError: cannot import name 'StateEventEmitter'`

**Step 3: Add StateEventEmitter to event_emitter.py**

Append to the bottom of `backend/core/reactive_state/event_emitter.py`:

```python
import logging
from typing import Any, Awaitable, Callable, Dict

from backend.core.reactive_state.journal import AppendOnlyJournal

logger = logging.getLogger(__name__)

# Type alias: async publish function that returns True on success
PublishFn = Callable[[UmfMessage], Awaitable[bool]]


class StateEventEmitter:
    """Bridges the synchronous store write pipeline and async UMF publish.

    After each successful journal append, the store calls
    ``emitter.publish(journal_entry)`` which:
    1. Builds a ``state.changed`` UMF message.
    2. Invokes the async ``publish_fn`` callback.
    3. On success, advances the durable publish cursor.
    4. On failure, logs and leaves cursor unchanged (reconciler fills gaps).

    Parameters
    ----------
    journal:
        The journal instance (for cursor advancement).
    publish_fn:
        Async callable that publishes a UmfMessage. Returns True on success.
    instance_id:
        Instance identifier for the UMF source field.
    session_id:
        Session identifier for the UMF source field.
    """

    def __init__(
        self,
        *,
        journal: AppendOnlyJournal,
        publish_fn: PublishFn,
        instance_id: str,
        session_id: str,
    ) -> None:
        self._journal = journal
        self._publish_fn = publish_fn
        self._instance_id = instance_id
        self._session_id = session_id
        self._published_count: int = 0
        self._failed_count: int = 0

    async def publish(self, entry: JournalEntry) -> bool:
        """Build and publish a state.changed event, advancing cursor on success.

        Returns True if publish succeeded, False otherwise.
        Never raises -- failures are logged and counted.
        """
        msg = build_state_changed_event(
            entry,
            instance_id=self._instance_id,
            session_id=self._session_id,
        )
        try:
            ok = await self._publish_fn(msg)
        except Exception:
            logger.exception(
                "Failed to publish state.changed for revision %d key=%r",
                entry.global_revision,
                entry.key,
            )
            self._failed_count += 1
            return False

        if ok:
            self._journal.advance_publish_cursor(entry.global_revision)
            self._published_count += 1
            return True
        else:
            logger.warning(
                "Publish returned False for revision %d key=%r",
                entry.global_revision,
                entry.key,
            )
            self._failed_count += 1
            return False

    def stats(self) -> Dict[str, int]:
        """Return publish statistics."""
        return {
            "published": self._published_count,
            "failed": self._failed_count,
        }
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_event_emitter.py -v`
Expected: All 5 tests PASS

**Step 5: Run full suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add backend/core/reactive_state/event_emitter.py tests/unit/core/reactive_state/test_event_emitter.py
git commit -m "feat(disease8): add StateEventEmitter with publish + cursor advance (Wave 2, Task 3)"
```

---

## Task 4: PublishReconciler — background async loop for crash recovery

**Files:**
- Modify: `backend/core/reactive_state/event_emitter.py`
- Test: `tests/unit/core/reactive_state/test_reconciler.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_reconciler.py
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
        _append_sample(journal, key="a", version=1)  # rev 1
        _append_sample(journal, key="b", version=1)  # rev 2
        _append_sample(journal, key="c", version=1)  # rev 3

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
        _append_sample(journal, key="a", version=1)  # rev 1
        _append_sample(journal, key="b", version=1)  # rev 2
        _append_sample(journal, key="c", version=1)  # rev 3
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
        _append_sample(journal, key="a", version=1)  # rev 1
        _append_sample(journal, key="b", version=1)  # rev 2
        _append_sample(journal, key="c", version=1)  # rev 3

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
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_reconciler.py -v`
Expected: FAIL — `ImportError: cannot import name 'PublishReconciler'`

**Step 3: Add PublishReconciler to event_emitter.py**

Append to the bottom of `backend/core/reactive_state/event_emitter.py`:

```python
class PublishReconciler:
    """Background reconciler that replays unpublished journal entries.

    On crash recovery, the publish cursor may lag behind the journal's
    latest revision.  The reconciler reads ``journal.read_unpublished()``
    and publishes each entry via the ``StateEventEmitter``, stopping
    on first failure so the cursor reflects exactly what was delivered.

    Parameters
    ----------
    journal:
        The journal instance (for reading unpublished entries).
    emitter:
        The event emitter (for publishing and cursor advancement).
    """

    def __init__(
        self,
        *,
        journal: AppendOnlyJournal,
        emitter: StateEventEmitter,
    ) -> None:
        self._journal = journal
        self._emitter = emitter
        self._reconciled_count: int = 0
        self._reconcile_runs: int = 0

    async def reconcile_once(self) -> int:
        """Publish all unpublished entries, stopping on first failure.

        Returns the number of entries successfully published in this run.
        """
        unpublished = self._journal.read_unpublished()
        published_this_run = 0

        for entry in unpublished:
            ok = await self._emitter.publish(entry)
            if not ok:
                logger.warning(
                    "Reconciler stopping at revision %d after publish failure",
                    entry.global_revision,
                )
                break
            published_this_run += 1

        self._reconciled_count += published_this_run
        self._reconcile_runs += 1
        return published_this_run

    def stats(self) -> Dict[str, int]:
        """Return reconciler statistics."""
        return {
            "reconciled": self._reconciled_count,
            "reconcile_runs": self._reconcile_runs,
        }
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_reconciler.py -v`
Expected: All 6 tests PASS

**Step 5: Run full suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add backend/core/reactive_state/event_emitter.py tests/unit/core/reactive_state/test_reconciler.py
git commit -m "feat(disease8): add PublishReconciler for crash recovery replay (Wave 2, Task 4)"
```

---

## Task 5: Wire event emitter into store write pipeline

**Files:**
- Modify: `backend/core/reactive_state/store.py`
- Test: `tests/unit/core/reactive_state/test_store_events.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_store_events.py
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
        # Count events before
        count_before = len(published_events)
        # Wrong writer -- ownership rejected
        store.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=0,
            writer="wrong_writer",
        )
        # No new events
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
        # Format: state.{epoch}.{global_revision}
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
        # Cursor should have advanced
        cursor = store._journal.get_publish_cursor()
        assert cursor > 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_events.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'event_emitter_factory'`

**Step 3: Modify store.py to accept and use event emitter**

Add import at top of `store.py`:
```python
import asyncio
from backend.core.reactive_state.event_emitter import StateEventEmitter
```

Add to `__init__` signature and body:
```python
def __init__(
    self,
    *,
    journal_path: Path,
    epoch: int,
    session_id: str,
    ownership_registry: OwnershipRegistry,
    schema_registry: SchemaRegistry,
    policy_engine: Optional[PolicyEngine] = None,
    audit_log: Optional[AuditLog] = None,
    event_emitter_factory: Optional[Callable[[AppendOnlyJournal], StateEventEmitter]] = None,
) -> None:
    # ... existing init ...
    self._event_emitter_factory = event_emitter_factory
    self._event_emitter: Optional[StateEventEmitter] = None
```

Add type import:
```python
from typing import Any, Callable, Dict, List, Optional
```

In `open()`, after `self._replay()`:
```python
def open(self) -> None:
    self._journal.open()
    self._replay()
    if self._event_emitter_factory is not None:
        self._event_emitter = self._event_emitter_factory(self._journal)
```

In the `write()` method, after step 8 (watcher notification), add step 9:
```python
        # Step 8: Notify watchers OUTSIDE the lock.
        if notify_new is not None:
            self._watchers.notify(key, notify_old, notify_new)

        # Step 9: Publish state.changed event (if emitter configured).
        if notify_new is not None and self._event_emitter is not None:
            self._publish_event(journal_entry)
```

We need to capture the `JournalEntry` from step 7. Modify step 7 to save it:
```python
            # Step 7: Journal append + in-memory update
            # ... existing code ...
            journal_entry = self._journal.append(...)  # capture return value
```

Note: currently the return value of `self._journal.append()` is not captured. Change the existing `self._journal.append(...)` call to `journal_entry = self._journal.append(...)`.

Also initialize `journal_entry` to `None` before the lock block:
```python
        journal_entry: Optional[JournalEntry] = None
```

Add the `_publish_event` helper:
```python
    def _publish_event(self, entry: JournalEntry) -> None:
        """Best-effort publish of a state.changed event.

        Runs the async publish in the current event loop if available,
        otherwise creates a new one. Failures are logged, not raised.
        """
        assert self._event_emitter is not None
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._event_emitter.publish(entry))
            else:
                loop.run_until_complete(self._event_emitter.publish(entry))
        except RuntimeError:
            # No event loop available -- create one
            asyncio.run(self._event_emitter.publish(entry))
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_store_events.py -v`
Expected: All 5 tests PASS

**Step 5: Run full suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add backend/core/reactive_state/store.py tests/unit/core/reactive_state/test_store_events.py
git commit -m "feat(disease8): wire StateEventEmitter into store write pipeline as step 9 (Wave 2, Task 5)"
```

---

## Task 6: Update package exports and Wave 2 integration test

**Files:**
- Modify: `backend/core/reactive_state/__init__.py`
- Test: `tests/unit/core/reactive_state/test_wave2_integration.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_wave2_integration.py
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
            # No event_emitter_factory -- events not published
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
```

**Step 2: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_wave2_integration.py -v`
Expected: All 3 tests PASS (if Tasks 1-5 are complete)

**Step 3: Update __init__.py exports**

```python
# backend/core/reactive_state/__init__.py
"""Reactive State Propagation -- Disease 8 cure.

Replaces 23+ environment variables used for cross-component state
with a versioned, observable, typed, CAS-protected state store.
"""
from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.event_emitter import (
    PublishReconciler,
    StateEventEmitter,
    build_state_changed_event,
)
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import PolicyEngine, build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import (
    StateEntry,
    WriteResult,
    WriteStatus,
)

__all__ = [
    "AuditLog",
    "AuditSeverity",
    "PolicyEngine",
    "PublishReconciler",
    "ReactiveStateStore",
    "StateEntry",
    "StateEventEmitter",
    "WriteResult",
    "WriteStatus",
    "build_default_policy_engine",
    "build_ownership_registry",
    "build_schema_registry",
    "build_state_changed_event",
]
```

**Step 4: Run full suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/reactive_state/__init__.py tests/unit/core/reactive_state/test_wave2_integration.py
git commit -m "feat(disease8): update exports and add Wave 2 integration test (Wave 2, Task 6)"
```

---

## Summary

| Task | Module | Tests | What it builds |
|------|--------|-------|---------------|
| 1 | `journal.py` (modify) | 7 | Durable `last_published_revision` cursor, `read_unpublished()` |
| 2 | `event_emitter.py` (create) | 8 | `build_state_changed_event()` — UmfMessage from JournalEntry |
| 3 | `event_emitter.py` (modify) | 5 | `StateEventEmitter` — publish + cursor advance helper |
| 4 | `event_emitter.py` (modify) | 6 | `PublishReconciler` — background catch-up for unpublished entries |
| 5 | `store.py` (modify) | 5 | Wire event emitter as step 9 in write pipeline |
| 6 | `__init__.py` + integration | 3 | Updated exports, crash recovery + idempotency test |
| **Total** | **1 new + 2 modified** | **~34** | **Complete Wave 2** |

**What's ready after Wave 2:**
- `state.changed` UMF event emission after every successful journal commit
- Durable publish cursor (`last_published_revision`) in journal SQLite DB
- Idempotency keys (`state.{epoch}.{global_revision}`) for duplicate prevention
- Background `PublishReconciler` that fills gaps on crash recovery
- Cursor only advances monotonically — never moves backward
- Publish failure is non-fatal — journal is authoritative, reconciler fills gaps

**Wave 2 hard gates (acceptance criteria):**
- Publish happens only after durable journal commit (never before)
- `last_published_revision` cursor advances only on confirmed publish
- Reconciler replays from cursor position — no duplicates (idempotency key)
- Crash between journal commit and publish → reconciler catches up on restart
- Empty journal → no crash, no spurious events
- Store without emitter works identically to Wave 1 (backward compatible)

**What Wave 3 adds (next plan):**
- `EnvKeyMapping` table for all 23+ env vars
- Shadow comparisons with canonical coercion
- `JARVIS_STATE_BRIDGE_MODE=shadow` opt-in
- Parity calculation and soak window
