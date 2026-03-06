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
