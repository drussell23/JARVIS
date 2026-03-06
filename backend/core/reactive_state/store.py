"""Reactive state store -- versioned, journaled, ownership-aware key-value store.

Wires together the journal, schema registry, ownership registry, and watcher
manager into a single thread-safe store with a CAS + epoch-fenced write
pipeline.

Write pipeline (steps 1-7 under lock, step 8 outside lock)
-----------------------------------------------------------
1. Schema validation (if schema exists for key).
2. Apply coercion (e.g. ``map_to`` for unknown enums).
3. Ownership check (writer must own key's domain).
4. Epoch fencing (writer_epoch must be >= store epoch).
5. CAS check (expected_version must match current version; 0 = new key).
6. Policy validation (if policy engine configured).
7. Journal append + in-memory update.
8. Notify watchers **outside** the lock (deadlock avoidance).

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only (plus sibling modules).
* ``threading.Lock`` serializes writes; reads are lock-protected but cheap.
* Watcher notification is always outside the lock.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.reactive_state.audit import AuditLog, post_replay_invariant_audit
from backend.core.reactive_state.journal import AppendOnlyJournal
from backend.core.reactive_state.ownership import OwnershipRegistry
from backend.core.reactive_state.policy import PolicyEngine
from backend.core.reactive_state.schemas import SchemaRegistry
from backend.core.reactive_state.types import (
    StateEntry,
    WriteRejection,
    WriteResult,
    WriteStatus,
)
from backend.core.reactive_state.watchers import WatcherManager

logger = logging.getLogger(__name__)


class ReactiveStateStore:
    """Thread-safe reactive state store with CAS, epoch fencing, and ownership.

    Parameters
    ----------
    journal_path:
        Filesystem path for the SQLite journal database.
    epoch:
        Store-wide epoch; bumped on ownership / topology changes.
    session_id:
        Session-scoped identifier for this store instance.
    ownership_registry:
        Registry enforcing writer-domain ownership of key prefixes.
    schema_registry:
        Registry providing per-key type, constraint, and default schemas.
    """

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
    ) -> None:
        self._journal = AppendOnlyJournal(journal_path)
        self._epoch = epoch
        self._session_id = session_id
        self._ownership = ownership_registry
        self._schemas = schema_registry
        self._policy_engine = policy_engine
        self._audit_log = audit_log
        self._entries: Dict[str, StateEntry] = {}
        self._rejection_counters: Counter = Counter()
        self._watchers = WatcherManager()
        self._lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def open(self) -> None:
        """Open journal, replay existing entries to rebuild in-memory state."""
        self._journal.open()
        self._replay()

    def close(self) -> None:
        """Close the journal and release resources."""
        self._journal.close()

    @property
    def audit_log(self) -> Optional[AuditLog]:
        """Return the audit log instance, or ``None`` if not configured."""
        return self._audit_log

    # ── Write ─────────────────────────────────────────────────────────

    def write(
        self,
        *,
        key: str,
        value: Any,
        expected_version: int,
        writer: str,
        writer_epoch: Optional[int] = None,
        origin: str = "explicit",
        consistency_group: Optional[str] = None,
    ) -> WriteResult:
        """Execute the full write pipeline.

        Steps 1-7 run under ``self._lock``.  Step 8 (watcher notification)
        runs outside the lock to avoid deadlock.

        Parameters
        ----------
        key:
            Dotted key name.
        value:
            New value (JSON-serializable).
        expected_version:
            CAS guard.  ``0`` means "key must not exist yet".
        writer:
            Logical writer identity (must own the key's domain).
        writer_epoch:
            Writer's epoch.  Defaults to ``self._epoch`` if ``None``.
        origin:
            How the value was produced (``"explicit"``, ``"default"``,
            ``"derived"``).
        consistency_group:
            Optional group tag for multi-key atomic writes.

        Returns
        -------
        WriteResult
            ``WriteStatus.OK`` with the new ``StateEntry``, or a failure
            status with a ``WriteRejection`` diagnostic.
        """
        # Notification state -- populated under lock, dispatched outside.
        notify_old: Optional[StateEntry] = None
        notify_new: Optional[StateEntry] = None

        with self._lock:
            # Step 1: Schema validation
            schema = self._schemas.get(key)
            if schema is not None:
                error = schema.validate(value)
                if error is not None:
                    return self._reject(
                        key, writer, WriteStatus.SCHEMA_INVALID, expected_version
                    )

                # Step 2: Coercion
                value = schema.coerce(value)

            # Step 3: Ownership check
            if not self._ownership.check_ownership(key, writer):
                return self._reject(
                    key, writer, WriteStatus.OWNERSHIP_REJECTED, expected_version
                )

            # Step 4: Epoch fencing
            effective_epoch = writer_epoch if writer_epoch is not None else self._epoch
            if effective_epoch < self._epoch:
                return self._reject(
                    key, writer, WriteStatus.EPOCH_STALE, expected_version
                )

            # Step 5: CAS check
            current_entry = self._entries.get(key)
            current_version = current_entry.version if current_entry is not None else 0
            if expected_version != current_version:
                return self._reject(
                    key, writer, WriteStatus.VERSION_CONFLICT, expected_version
                )

            # Step 6: Policy validation (if engine configured)
            if self._policy_engine is not None:
                snapshot = dict(self._entries)  # read-only copy
                policy_result = self._policy_engine.evaluate(key, value, snapshot)
                if not policy_result.allowed:
                    return self._reject(
                        key, writer, WriteStatus.POLICY_REJECTED, expected_version
                    )

            # Step 7: Journal append + in-memory update
            new_version = current_version + 1
            previous_value = current_entry.value if current_entry is not None else None
            now_mono = time.monotonic()
            now_unix_ms = int(time.time() * 1000)

            self._journal.append(
                key=key,
                value=value,
                previous_value=previous_value,
                version=new_version,
                epoch=self._epoch,
                writer=writer,
                writer_session_id=self._session_id,
                origin=origin,
                consistency_group=consistency_group,
            )

            new_entry = StateEntry(
                key=key,
                value=value,
                version=new_version,
                epoch=self._epoch,
                writer=writer,
                origin=origin,
                updated_at_mono=now_mono,
                updated_at_unix_ms=now_unix_ms,
            )
            self._entries[key] = new_entry

            # Prepare notification data (dispatched outside lock).
            notify_old = current_entry
            notify_new = new_entry

        # Step 8: Notify watchers OUTSIDE the lock.
        if notify_new is not None:
            self._watchers.notify(key, notify_old, notify_new)

        return WriteResult(status=WriteStatus.OK, entry=notify_new)

    # ── Read ──────────────────────────────────────────────────────────

    def read(self, key: str) -> Optional[StateEntry]:
        """Thread-safe read from in-memory entries.

        Returns ``None`` if the key has never been written.
        """
        with self._lock:
            return self._entries.get(key)

    def read_many(self, keys: List[str]) -> Dict[str, StateEntry]:
        """Thread-safe batch read.  Returns only keys that exist."""
        with self._lock:
            return {
                k: self._entries[k]
                for k in keys
                if k in self._entries
            }

    # ── Watch ─────────────────────────────────────────────────────────

    def watch(
        self,
        key_pattern: str,
        callback: Any,
        max_queue_size: int = 100,
        overflow_policy: str = "drop_oldest",
    ) -> str:
        """Delegate to WatcherManager."""
        return self._watchers.subscribe(
            key_pattern,
            callback,
            max_queue_size=max_queue_size,
            overflow_policy=overflow_policy,
        )

    def unwatch(self, watch_id: str) -> bool:
        """Delegate to WatcherManager."""
        return self._watchers.unsubscribe(watch_id)

    # ── Queries ───────────────────────────────────────────────────────

    def global_revision(self) -> int:
        """Return the journal's latest revision."""
        return self._journal.latest_revision()

    def snapshot(self) -> Dict[str, StateEntry]:
        """Return a copy of all entries."""
        with self._lock:
            return dict(self._entries)

    def rejection_stats(self) -> Dict[tuple, int]:
        """Return rejection counts as {(key, reason_value): count}."""
        return dict(self._rejection_counters)

    # ── Defaults ──────────────────────────────────────────────────────

    def initialize_defaults(self) -> None:
        """Populate all schema-declared keys with their defaults if not already set.

        For each key in the schema registry, if the key is not already
        present in ``_entries``, write the schema's default value with
        ``expected_version=0`` and ``origin='default'``.  The writer is
        set to the key's declared owner.
        """
        for key in sorted(self._schemas.all_keys()):
            if key in self._entries:
                continue
            schema = self._schemas.get(key)
            if schema is None:
                continue  # pragma: no cover -- defensive
            owner = self._ownership.resolve_owner(key)
            if owner is None:
                continue  # pragma: no cover -- defensive
            self.write(
                key=key,
                value=schema.default,
                expected_version=0,
                writer=owner,
                origin="default",
            )

    # ── Internal ──────────────────────────────────────────────────────

    def _replay(self) -> None:
        """Rebuild in-memory state from journal.  Called by ``open()``.

        Reads all journal entries and reconstructs the latest ``StateEntry``
        for each key.  Replayed entries get ``updated_at_mono=0.0`` since
        the original monotonic timestamps are meaningless after restart.

        After replay, if an ``AuditLog`` is configured and entries were
        replayed, runs ``post_replay_invariant_audit`` and records any
        findings into the audit log.
        """
        entries = self._journal.read_since(1)
        for je in entries:
            self._entries[je.key] = StateEntry(
                key=je.key,
                value=je.value,
                version=je.version,
                epoch=je.epoch,
                writer=je.writer,
                origin=je.origin,
                updated_at_mono=0.0,
                updated_at_unix_ms=je.timestamp_unix_ms,
            )

        # Post-replay invariant audit
        if self._audit_log is not None and entries:
            findings = post_replay_invariant_audit(
                dict(self._entries), self._journal.latest_revision()
            )
            for finding in findings:
                self._audit_log.record_finding(finding)

    def _reject(
        self,
        key: str,
        writer: str,
        reason: WriteStatus,
        attempted_version: int,
    ) -> WriteResult:
        """Build a ``WriteResult`` with a ``WriteRejection``.  Logs at debug level."""
        self._rejection_counters[(key, reason.value)] += 1
        current_entry = self._entries.get(key)
        current_version = current_entry.version if current_entry is not None else 0

        rejection = WriteRejection(
            key=key,
            writer=writer,
            writer_session_id=self._session_id,
            reason=reason,
            epoch=self._epoch,
            attempted_version=attempted_version,
            current_version=current_version,
            global_revision_at_reject=self._journal.latest_revision(),
            timestamp_mono=time.monotonic(),
        )
        logger.debug(
            "Write rejected: key=%r writer=%r reason=%s attempted_v=%d current_v=%d",
            key,
            writer,
            reason.value,
            attempted_version,
            current_version,
        )
        return WriteResult(status=reason, rejection=rejection)
