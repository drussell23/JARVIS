"""Watcher / subscription system with bounded-queue backpressure.

Provides a ``WatcherManager`` that lets components subscribe to
state-key mutations via glob patterns.  Callbacks are dispatched
synchronously; async dispatch with real queue backpressure is
reserved for a future wave.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only (plus sibling types).
* Notify iterates watchers **outside** the lock to avoid deadlocks.
* A raising callback is logged and swallowed -- no poisoning of peers.
* ``fnmatch.fnmatch`` for glob matching (from stdlib).
"""
from __future__ import annotations

import fnmatch
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from backend.core.reactive_state.types import StateEntry

logger = logging.getLogger(__name__)

# ── Type aliases ──────────────────────────────────────────────────────

WatcherCallback = Callable[[Optional[StateEntry], StateEntry], None]

# ── Internal spec ─────────────────────────────────────────────────────


@dataclass
class _WatchSpec:
    """Internal record tracking a single subscription."""

    watch_id: str
    key_pattern: str
    callback: WatcherCallback
    max_queue_size: int
    overflow_policy: str  # "drop_oldest" | "drop_newest" | "block_bounded"
    drop_count: int = field(default=0)


# ── WatcherManager ────────────────────────────────────────────────────


class WatcherManager:
    """Manages key-pattern subscriptions and dispatches change notifications.

    Thread-safe.  Callbacks are invoked synchronously in the caller's
    thread.  Queue-based async dispatch (with real backpressure) will be
    layered on top in a later wave.

    Parameters
    ----------
    (none -- stateless constructor)

    Usage
    -----
    >>> mgr = WatcherManager()
    >>> wid = mgr.subscribe("gcp.*", lambda old, new: print(new.key))
    >>> mgr.notify("gcp.vm_ready", None, new_entry)
    gcp.vm_ready
    >>> mgr.unsubscribe(wid)
    True
    """

    def __init__(self) -> None:
        self._watchers: Dict[str, _WatchSpec] = {}
        self._lock = threading.Lock()
        self._total_drops: int = 0

    # ── Public API ────────────────────────────────────────────────────

    def subscribe(
        self,
        key_pattern: str,
        callback: WatcherCallback,
        max_queue_size: int = 100,
        overflow_policy: str = "drop_oldest",
    ) -> str:
        """Register *callback* for keys matching *key_pattern* (fnmatch glob).

        Returns a unique ``watch_id`` that can be passed to
        :meth:`unsubscribe` to remove this subscription.
        """
        watch_id = uuid.uuid4().hex
        spec = _WatchSpec(
            watch_id=watch_id,
            key_pattern=key_pattern,
            callback=callback,
            max_queue_size=max_queue_size,
            overflow_policy=overflow_policy,
        )
        with self._lock:
            self._watchers[watch_id] = spec
        return watch_id

    def unsubscribe(self, watch_id: str) -> bool:
        """Remove the watcher identified by *watch_id*.

        Returns ``True`` if the watcher existed and was removed,
        ``False`` otherwise.
        """
        with self._lock:
            return self._watchers.pop(watch_id, None) is not None

    def notify(
        self,
        key: str,
        old_entry: Optional[StateEntry],
        new_entry: StateEntry,
    ) -> None:
        """Dispatch a change notification to all watchers whose pattern matches *key*.

        Watchers are iterated **outside** the lock (snapshot copy) to
        avoid holding the lock during potentially slow callbacks.
        A raising callback is caught, logged, and swallowed so that
        other watchers continue to receive the notification.
        """
        # Snapshot under lock
        with self._lock:
            specs: List[_WatchSpec] = list(self._watchers.values())

        # Dispatch outside lock
        for spec in specs:
            if not self._matches(spec.key_pattern, key):
                continue
            try:
                spec.callback(old_entry, new_entry)
            except Exception:
                logger.exception(
                    "Watcher %s (pattern=%r) raised on key %r -- swallowed",
                    spec.watch_id,
                    spec.key_pattern,
                    key,
                )

    def total_drops(self) -> int:
        """Return the cumulative number of notifications dropped across all watchers."""
        return self._total_drops

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _matches(pattern: str, key: str) -> bool:
        """Return ``True`` if *key* matches the fnmatch *pattern*."""
        return fnmatch.fnmatch(key, pattern)
