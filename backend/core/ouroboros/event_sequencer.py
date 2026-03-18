"""GlobalEventSequencer — P1-5 cross-op monotonic event sequence numbers.

Provides a single process-wide, thread-safe monotonically increasing
counter for tagging governance pipeline events with a global sequence
number.  This enables:

* Total causal ordering of events across multi-repo state changes.
* Audit trail reconstruction without wall-clock ambiguity.
* Detection of dropped / duplicated events in the governance bus.

Usage
-----
    from backend.core.ouroboros.event_sequencer import next_seq

    global_seq = next_seq()   # returns 1, 2, 3, …

The counter starts at 1 and never resets within a process lifetime.
It is intentionally *not* reset on governance epoch changes — the
combination of ``(BOOT_ID, global_seq)`` is the stable event identity.
"""

from __future__ import annotations

import threading
from typing import Optional

__all__ = [
    "GlobalEventSequencer",
    "next_seq",
    "current_seq",
    "get_global_event_sequencer",
]


class GlobalEventSequencer:
    """Thread-safe monotonic counter for governance events.

    Parameters
    ----------
    start:
        Initial value; the first call to ``next()`` returns ``start``.
    """

    def __init__(self, start: int = 1) -> None:
        self._lock = threading.Lock()
        self._counter: int = start - 1  # next() pre-increments

    def next(self) -> int:
        """Return the next sequence number (thread-safe, monotonic)."""
        with self._lock:
            self._counter += 1
            return self._counter

    def current(self) -> int:
        """Return the most recently issued sequence number (0 if none)."""
        with self._lock:
            return self._counter

    def reset(self, value: int = 1) -> None:
        """Reset the counter.

        This should only be called in tests — production code must never
        reset the sequencer because doing so breaks causal ordering.
        """
        with self._lock:
            self._counter = value - 1


# ---------------------------------------------------------------------------
# Module-level singleton + helpers
# ---------------------------------------------------------------------------

_g_sequencer: Optional[GlobalEventSequencer] = None
_g_singleton_lock = threading.Lock()


def get_global_event_sequencer() -> GlobalEventSequencer:
    """Return (lazily creating) the process-wide GlobalEventSequencer."""
    global _g_sequencer
    if _g_sequencer is None:
        with _g_singleton_lock:
            if _g_sequencer is None:
                _g_sequencer = GlobalEventSequencer(start=1)
    return _g_sequencer


def next_seq() -> int:
    """Return the next global governance event sequence number."""
    return get_global_event_sequencer().next()


def current_seq() -> int:
    """Return the last issued global governance event sequence number."""
    return get_global_event_sequencer().current()
