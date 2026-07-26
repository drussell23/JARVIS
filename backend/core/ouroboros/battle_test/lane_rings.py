"""Per-lane history, and the thread boundary that would otherwise lose it.

Two pieces that only make sense together:

``ContextAwareThreadPool``
    ``ContextVar`` crosses ``await`` for free and is copied into new tasks,
    so the lane tag survives the whole async call graph. It does NOT cross
    every thread boundary. Measured on this interpreter:

        asyncio.to_thread        -> propagates
        loop.run_in_executor     -> LOST
        ThreadPoolExecutor.submit-> LOST

    ``to_thread`` already copies the context, so wrapping those call sites
    would be machinery with nothing to do. The other two are the real hole:
    a worker that hands its heavy compute or disk I/O to an executor orphans
    everything that function emits, and the output arrives untagged — which
    looks exactly like the organism speaking as itself.

``LaneRegistry``
    Bounded per-lane rings. D3 lets the operator focus a lane and expects its
    history to be there; a lane that only exists while something is printing
    would show an empty pane. Bounded because a swarm worker can emit for
    minutes and the daemon is long-lived.
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.LaneRings")

_TRUTHY = ("1", "true", "yes", "on")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


def lane_ring_size() -> int:
    """Lines retained per lane. Enough to explain what a worker has been
    doing without becoming a second transcript."""
    return _env_int("JARVIS_LANE_RING_SIZE", 100, 1, 10000)


def max_lanes() -> int:
    """Ceiling on tracked lanes. A runaway spawner must not turn the ring
    registry into an unbounded map keyed by worker id."""
    return _env_int("JARVIS_LANE_MAX", 64, 1, 4096)


# ---------------------------------------------------------------------------
# The thread boundary
# ---------------------------------------------------------------------------


class ContextAwareThreadPool:
    """A ``ThreadPoolExecutor`` that carries the caller's context across.

    ``copy_context()`` is taken at SUBMIT time on the calling thread, then
    ``ctx.run(fn, ...)`` executes the callable inside that copy on the worker
    thread. A copy, not a reference: the worker must not be able to mutate
    the submitter's context, and two workers submitted from the same lane
    must not share mutable state.

    Drop-in for the executor argument of ``loop.run_in_executor``, and usable
    directly via ``submit``.
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        *,
        thread_name_prefix: str = "ov-lane",
    ) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix,
        )

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        """Submit *fn*, preserving the submitting context. NEVER raises
        beyond what the pool itself raises."""
        ctx = contextvars.copy_context()
        return self._pool.submit(ctx.run, fn, *args, **kwargs)

    def map(self, fn: Callable[..., Any], *iterables: Any, **kw: Any) -> Any:
        ctx = contextvars.copy_context()
        return self._pool.map(
            lambda *a: ctx.run(fn, *a), *iterables, **kw,
        )

    def shutdown(self, wait: bool = True, **kw: Any) -> None:
        try:
            self._pool.shutdown(wait=wait, **kw)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "ContextAwareThreadPool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Per-lane history
# ---------------------------------------------------------------------------


@dataclass
class LaneLine:
    text: str
    ts: float
    severity: str = "INFO"


def tombstone_ttl_s() -> float:
    """How long a finished lane's history outlives the worker that wrote it.

    THE GHOST-PANE DEFENSE. The deck is painted from a snapshot; the operator
    reads it, reaches for an arrow key, and presses Enter — and in that human
    interval the worker can finish. Destroying the lane on completion makes
    that a race the operator loses at random, and no amount of `except
    KeyError` around the lookup fixes it: catching the miss just turns a crash
    into an empty pane, which is the same lie told politely.

    The answer is retention. A finished lane becomes read-only rather than
    absent, so the selection that was valid when the operator saw it is still
    valid when they act on it — and its final output, which is usually the
    interesting part, is exactly what they get."""
    try:
        return max(1.0, min(3600.0, float(
            os.environ.get("JARVIS_LANE_TOMBSTONE_TTL_S", "60") or 60,
        )))
    except (TypeError, ValueError):
        return 60.0


@dataclass
class LaneState:
    lane: str
    lines: Deque[LaneLine]
    first_seen: float
    last_seen: float
    total: int = 0          # lifetime count, including lines the ring dropped
    label: str = ""
    #: When the worker finished. None while it is still running.
    died_at: Optional[float] = None

    @property
    def tombstoned(self) -> bool:
        return self.died_at is not None


class LaneRegistry:
    """Bounded rings, one per lane, plus enough metadata for the deck.

    Thread-safe: emissions arrive from the event loop AND from executor
    threads (that is the entire point of the pool above), so the registry is
    written from more than one thread.
    """

    def __init__(self, *, ring: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ring = ring
        self._clock = clock
        self._lanes: Dict[str, LaneState] = {}
        self._lock = threading.Lock()
        self.evicted_lanes = 0
        #: Called with a lane id when it stops existing — TTL expiry or
        #: eviction. The client FSM cannot infer this: a lane simply missing
        #: from the next heartbeat is indistinguishable from a slow frame, so
        #: an operator focused on it would sit in a pane that never updates
        #: and never explains itself. The daemon knows the moment it happens
        #: and says so.
        self._reap_sinks: List[Callable[[str], None]] = []
        #: Reaps collected under the lock, announced after it is released.
        self._pending_reaps: List[str] = []

    def on_reap(self, sink: Callable[[str], None]) -> Callable[[], None]:
        """Subscribe to lane disappearance. Returns an unsubscribe callable."""
        self._reap_sinks.append(sink)

        def _off() -> None:
            try:
                self._reap_sinks.remove(sink)
            except ValueError:
                pass
        return _off

    def _drain_reaps(self) -> None:
        """Announce reaps collected under the lock. Call AFTER releasing it.

        The two-phase shape is deliberate: eviction and TTL expiry both run
        inside the critical section, and a subscriber that queries the
        registry from its callback would deadlock if notified there."""
        with self._lock:
            pending, self._pending_reaps = self._pending_reaps, []
        for lane in pending:
            self._announce_reap(lane)

    def _announce_reap(self, lane: str) -> None:
        """Fan out one reap. Called WITHOUT the lock held — a sink that
        reaches back into the registry must not deadlock. NEVER raises."""
        for sink in list(self._reap_sinks):
            try:
                sink(lane)
            except Exception:  # noqa: BLE001
                logger.debug("[LaneRings] reap sink degraded", exc_info=True)

    def record(
        self, lane: str, text: str, *, severity: str = "INFO",
        label: str = "",
    ) -> None:
        """Append one line to *lane*'s ring. NEVER raises — this sits on the
        emission path and must not be able to break rendering."""
        try:
            lane = str(lane or "").strip()
            text = str(text or "")
            if not lane or not text:
                return
            now = self._clock()
            size = self._ring if self._ring is not None else lane_ring_size()
            with self._lock:
                st = self._lanes.get(lane)
                if st is None:
                    self._reap_if_needed(now)
                    st = LaneState(
                        lane=lane, lines=deque(maxlen=size),
                        first_seen=now, last_seen=now,
                    )
                    self._lanes[lane] = st
                if label:
                    st.label = label
                st.lines.append(LaneLine(text=text, ts=now, severity=severity))
                st.last_seen = now
                st.total += 1
            self._drain_reaps()
        except Exception:  # noqa: BLE001
            pass

    def mark_dead(self, lane: str) -> bool:
        """The worker finished. Retain its history as a tombstone.

        Idempotent, and deliberately NOT a delete: see :func:`tombstone_ttl_s`.
        A lane that was never seen is not resurrected — marking the death of
        something that never lived would put an empty pane in the deck."""
        try:
            with self._lock:
                st = self._lanes.get(str(lane))
                if st is None:
                    return False
                if st.died_at is None:
                    st.died_at = self._clock()
                return True
        except Exception:  # noqa: BLE001
            return False

    def is_tombstoned(self, lane: str) -> bool:
        with self._lock:
            st = self._lanes.get(str(lane))
            return bool(st and st.tombstoned)

    def _expire_tombstones(self, now: float) -> List[str]:
        """Drop tombstones past their TTL. Called with the lock held.

        Returns the reaped ids rather than announcing them, because the
        caller holds the lock and a sink that reaches back in would deadlock.

        Only tombstones expire on time. A live lane that has simply been quiet
        for a while is still a worker the operator may want to look at, so
        silence is not death."""
        reaped: List[str] = []
        for k, st in list(self._lanes.items()):
            if st.died_at is not None and now - st.died_at > tombstone_ttl_s():
                del self._lanes[k]
                reaped.append(k)
        return reaped

    def _reap_if_needed(self, now: float) -> None:
        """Evict the least-recently-active lane when at the ceiling.

        Called with the lock held. Recency rather than size: a finished
        worker's history stops being interesting long before a busy one's,
        and the operator focuses what is happening now."""
        reaped = self._expire_tombstones(now)
        limit = max_lanes()
        if len(self._lanes) >= limit:
            # Prefer a tombstone as the victim: a finished lane's history is
            # worth less than a running worker's, whatever the timestamps say.
            pool = [s for s in self._lanes.values() if s.tombstoned] or \
                   list(self._lanes.values())
            victim = min(pool, key=lambda s: s.last_seen)
            self._lanes.pop(victim.lane, None)
            self.evicted_lanes += 1
            reaped.append(victim.lane)
        self._pending_reaps.extend(reaped)

    # -- read side --------------------------------------------------------

    def lanes(self) -> List[str]:
        with self._lock:
            return sorted(
                self._lanes, key=lambda k: -self._lanes[k].last_seen,
            )

    def history(self, lane: str, limit: Optional[int] = None) -> List[LaneLine]:
        """Hydration for D3: the pristine backlog of one lane, oldest first."""
        with self._lock:
            st = self._lanes.get(str(lane))
            if st is None:
                return []
            lines = list(st.lines)
        return lines[-limit:] if limit else lines

    def summary(self) -> List[Dict[str, Any]]:
        """What the selectable deck lists — live lanes first, then tombstones.

        A dict rather than a tuple because this crosses the IPC bridge into
        the client's selection list, and a positional payload would have to be
        re-agreed at both ends every time a field is added."""
        with self._lock:
            now = self._clock()
            self._pending_reaps.extend(self._expire_tombstones(now))
            rows = sorted(
                self._lanes.values(),
                key=lambda s: (s.tombstoned, -s.last_seen),
            )
            out = [
                {
                    "lane": s.lane,
                    "lines": len(s.lines),
                    "last_seen": s.last_seen,
                    "label": s.label,
                    "tombstoned": s.tombstoned,
                    "age_s": round(now - s.last_seen, 1),
                }
                for s in rows
            ]
        # Announced here, outside the lock: summary() is polled ~1Hz by the
        # heartbeat, which makes it the reliable place a TTL expiry becomes
        # observable even if nothing else touches the registry.
        self._drain_reaps()
        return out

    def dropped(self, lane: str) -> int:
        """Lines this lane emitted that the ring no longer holds. Reported
        rather than hidden — a truncated pane must say so."""
        with self._lock:
            st = self._lanes.get(str(lane))
            return max(0, st.total - len(st.lines)) if st else 0

    def clear(self) -> None:
        with self._lock:
            self._lanes.clear()


_REGISTRY: Optional[LaneRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def get_lane_registry() -> LaneRegistry:
    """Process-wide registry. NEVER raises."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = LaneRegistry()
        return _REGISTRY


def reset_lane_registry() -> None:
    """Teardown seam for tests."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


__all__ = [
    "ContextAwareThreadPool",
    "LaneLine",
    "LaneRegistry",
    "LaneState",
    "get_lane_registry",
    "lane_ring_size",
    "max_lanes",
    "reset_lane_registry",
]
