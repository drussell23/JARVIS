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


@dataclass
class LaneState:
    lane: str
    lines: Deque[LaneLine]
    first_seen: float
    last_seen: float
    total: int = 0          # lifetime count, including lines the ring dropped
    label: str = ""


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
        except Exception:  # noqa: BLE001
            pass

    def _reap_if_needed(self, now: float) -> None:
        """Evict the least-recently-active lane when at the ceiling.

        Called with the lock held. Recency rather than size: a finished
        worker's history stops being interesting long before a busy one's,
        and the operator focuses what is happening now."""
        limit = max_lanes()
        if len(self._lanes) < limit:
            return
        victim = min(self._lanes.values(), key=lambda s: s.last_seen)
        self._lanes.pop(victim.lane, None)
        self.evicted_lanes += 1

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

    def summary(self) -> List[Tuple[str, int, float, str]]:
        """``(lane, retained, last_seen, label)`` — what the deck lists."""
        with self._lock:
            return [
                (s.lane, len(s.lines), s.last_seen, s.label)
                for s in sorted(
                    self._lanes.values(), key=lambda s: -s.last_seen,
                )
            ]

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
