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

import logging
from typing import Any, Awaitable, Callable, Dict

from backend.core.reactive_state.journal import AppendOnlyJournal
from backend.core.reactive_state.types import JournalEntry
from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Priority,
    Stream,
    UmfMessage,
)

# -- Constants ----------------------------------------------------------------

STATE_EVENT_SCHEMA_VERSION: int = 1


# -- Event builder ------------------------------------------------------------


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


# -- Emitter -----------------------------------------------------------------

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


# -- Reconciler --------------------------------------------------------------


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
