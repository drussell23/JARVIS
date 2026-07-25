"""Zero-copy audio broadcast tap — one mic, many observers, STT never pays.

The constraint
--------------
macOS CoreAudio will not hand out a second handle on a device that is already
captured: a UI-owned stream would fail with ``Resource busy`` / device
unavailable. So the visualizer cannot open its own mic — it has to observe the
stream the STT pipeline already owns.

But the STT pipeline is the *primary*. Transcription latency and correctness must
not degrade because a decorative waveform is downstream. Three properties make
that guarantee structural rather than aspirational:

1. **Zero copy.** The tap stores a read-only *view* of the caller's buffer
   (``ndarray.view()`` with ``writeable=False``, an O(1) header op) — never a
   ``copy()``. On a 48kHz stream a per-chunk copy is pure waste for a value that
   is about to be reduced to one float.

2. **Never blocks.** ``offer()`` writes into a **single-slot latest-wins**
   mailbox under a non-blocking lock and returns. No queue to fill, no
   ``put(timeout=...)`` to stall on, no unbounded growth. If the consumer is
   slow, old chunks are simply overwritten — for an amplitude monitor the newest
   sample is the only interesting one, so dropping is correct behaviour, not
   degradation.

3. **Fault-isolated.** Every observer call is individually wrapped. A consumer
   that raises, hangs its own task, or is garbage is dropped on the floor; the
   capture path sees ``offer()`` return and carries on. RMS itself happens on
   the CONSUMER side (``drain``), so no arithmetic at all runs in the capture
   thread.

A deliberate trade worth stating: because the view is zero-copy, a source that
*reuses* its capture buffer could be mid-overwrite when the consumer reduces it,
yielding one slightly-wrong amplitude frame. For a visualizer that is strictly
preferable to copying every chunk; for anything that needed sample-exact audio
this tap would be the wrong tool, and the docstring says so on purpose.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

#: An observer receives ``(read_only_view, sample_rate)`` and must return fast.
Observer = Callable[[Any, int], None]


def _read_only_view(chunk: Any) -> Any:
    """An O(1) immutable view of ``chunk``. Falls back to the object itself when
    it is not a numpy array (or is already immutable). Never copies, never
    raises."""
    try:
        view = chunk.view()
        view.flags.writeable = False
        return view
    except Exception:  # noqa: BLE001 — not an ndarray, or already read-only
        try:
            return memoryview(chunk)
        except Exception:  # noqa: BLE001
            return chunk


class AudioBroadcastTap:
    """Single-slot latest-wins fan-out from the capture thread to observers."""

    def __init__(self, *, sample_rate: int = 16000) -> None:
        self._observers: List[Observer] = []
        self._slot: Optional[Any] = None
        self._sample_rate = int(sample_rate)
        self._lock = threading.Lock()
        # Observability — proves the tap is live and how much it shed.
        self.offered = 0
        self.dropped = 0
        self.drained = 0
        self.observer_faults = 0

    # -- registration ---------------------------------------------------

    def subscribe(self, observer: Observer) -> Callable[[], None]:
        """Register an observer; returns an unsubscribe thunk (same contract as
        ``ReactiveTheme.register_invalidate`` — one idiom for "attach a
        callback")."""
        with self._lock:
            self._observers.append(observer)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._observers.remove(observer)
                except ValueError:
                    pass

        return _unsubscribe

    @property
    def observer_count(self) -> int:
        with self._lock:
            return len(self._observers)

    # -- capture side (HOT PATH — must stay O(1) and non-blocking) -------

    def offer(self, chunk: Any, *, sample_rate: Optional[int] = None) -> bool:
        """Called FROM THE CAPTURE THREAD with a freshly read PCM chunk.

        Stores a read-only view in the latest-wins slot and returns immediately.
        Returns True iff something was stored. NEVER raises, NEVER blocks, and
        performs no arithmetic on the samples."""
        try:
            view = _read_only_view(chunk)
            # acquire(False): if the consumer happens to hold the lock this
            # instant, we SKIP rather than wait. The capture thread must never
            # be parked by a visualizer.
            if not self._lock.acquire(False):
                self.dropped += 1
                return False
            try:
                if self._slot is not None:
                    self.dropped += 1      # overwriting an undrained chunk
                self._slot = view
                if sample_rate:
                    self._sample_rate = int(sample_rate)
                self.offered += 1
                return True
            finally:
                self._lock.release()
        except Exception:  # noqa: BLE001 — the tap can NEVER break capture
            return False

    # -- consumer side --------------------------------------------------

    def take(self) -> Optional[Any]:
        """Atomically remove and return the pending chunk, or None."""
        with self._lock:
            chunk, self._slot = self._slot, None
        return chunk

    def drain(self) -> bool:
        """Hand the pending chunk to every observer. Returns True iff a chunk
        was dispatched. Runs on the CONSUMER (UI/async) side — this is where RMS
        ends up happening, safely away from capture.

        Each observer is isolated: one that raises is counted and skipped, and
        the others still receive the chunk."""
        chunk = self.take()
        if chunk is None:
            return False
        with self._lock:
            observers = list(self._observers)
        self.drained += 1
        for obs in observers:
            try:
                obs(chunk, self._sample_rate)
            except Exception:  # noqa: BLE001 — a bad observer never poisons the rest
                self.observer_faults += 1
                logger.debug("[AudioTap] observer raised", exc_info=True)
        return True

    def stats(self) -> dict:
        return {
            "offered": self.offered, "dropped": self.dropped,
            "drained": self.drained, "observer_faults": self.observer_faults,
            "observers": self.observer_count,
        }


_DEFAULT_TAP: Optional[AudioBroadcastTap] = None
_TAP_LOCK = threading.Lock()


def get_default_tap() -> AudioBroadcastTap:
    """Process-wide tap. Lazy so importing this module costs nothing and so the
    capture seam can reference it without an init ordering dependency."""
    global _DEFAULT_TAP
    with _TAP_LOCK:
        if _DEFAULT_TAP is None:
            _DEFAULT_TAP = AudioBroadcastTap()
        return _DEFAULT_TAP


def reset_default_tap() -> None:
    """Test seam — drop the singleton so suites cannot bleed into each other."""
    global _DEFAULT_TAP
    with _TAP_LOCK:
        _DEFAULT_TAP = None


def offer_to_default_tap(chunk: Any, *, sample_rate: Optional[int] = None) -> bool:
    """Convenience for the capture seam: fire-and-forget into the singleton.

    Deliberately does NOT construct the tap when none exists yet — a capture
    path with no visualizer attached should stay byte-identical, doing nothing at
    all rather than allocating a mailbox nobody drains."""
    try:
        with _TAP_LOCK:
            tap = _DEFAULT_TAP
        if tap is None or tap.observer_count == 0:
            return False
        return tap.offer(chunk, sample_rate=sample_rate)
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "AudioBroadcastTap",
    "Observer",
    "get_default_tap",
    "offer_to_default_tap",
    "reset_default_tap",
]
