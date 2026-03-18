"""Unified boot epoch — single source of truth for cross-process epoch fencing.

P0-5: Epoch Fencing Bridge.

Multiple subsystems previously maintained independent epoch counters
(VerdictAuthority._current_epoch, CommProtocol heartbeat epoch,
HeartbeatWriter JSON epoch, MemoryBudgetBroker.epoch). This module
provides the single authoritative source so that stale-epoch rejection
is consistent across all process boundaries.

Usage pattern
-------------
* The supervisor calls ``advance_epoch()`` once at startup (and again on
  each graceful restart).
* Downstream consumers call ``get_epoch()`` to validate incoming verdicts.
* ``BOOT_ID`` is a stable UUID hex for the lifetime of this process,
  useful as a correlation ID in cross-repo event envelopes.

Thread-safety
-------------
``advance_epoch()`` is serialised via a ``threading.Lock``.  All other
operations are lock-free reads of immutable / monotonically-increasing values.
"""

from __future__ import annotations

import threading
import uuid

__all__ = [
    "BOOT_ID",
    "get_boot_id",
    "get_epoch",
    "advance_epoch",
]

# Stable process-lifetime identity — set once at module import.
BOOT_ID: str = uuid.uuid4().hex

_lock = threading.Lock()
_epoch: int = 0


def get_boot_id() -> str:
    """Return the immutable boot UUID for this process instance."""
    return BOOT_ID


def get_epoch() -> int:
    """Return the current epoch counter (0 until first ``advance_epoch`` call)."""
    return _epoch


def advance_epoch() -> int:
    """Increment the epoch counter and return the new value.

    Thread-safe.  The supervisor calls this once at startup; subsequent
    calls represent supervisor restarts within the same OS process.
    """
    global _epoch
    with _lock:
        _epoch += 1
        return _epoch
