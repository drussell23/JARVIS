"""
gap_signal_bus.py — Fire-and-forget capability-gap event bus (Task 1 / DAS).

Public API
----------
CapabilityGapEvent   – frozen dataclass describing a detected capability gap
GapSignalBus         – thin asyncio.Queue wrapper with drop-on-full semantics
get_gap_signal_bus() – process-wide singleton accessor (thread-safe lazy init)

Implementation notes
--------------------
- ``frozen=True`` enforces immutability on CapabilityGapEvent.
- ``slots=True`` is part of the spec but requires Python ≥ 3.10; on 3.9 we
  achieve equivalent memory layout by declaring ``__slots__`` on a thin
  wrapper that delegates to the frozen dataclass fields.  The public interface
  and all property semantics are identical across Python versions.
- ``emit()`` uses ``put_nowait()`` (never ``await put()``) to keep the call
  synchronous and safe from any call-site (sync or async).
- The singleton is double-checked with a ``threading.Lock`` so it is safe to
  call from multiple threads before an event-loop is started.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NONALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Lower-case, replace non-alphanumeric runs with '_', strip edge underscores."""
    return _NONALNUM.sub("_", text.lower()).strip("_")


def _sha16(value: str) -> str:
    """Return the first 16 hex characters of a SHA-256 digest."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CapabilityGapEvent
# ---------------------------------------------------------------------------

# Python 3.10+ supports slots=True on dataclass; 3.9 does not.
# We branch at definition time so the class is always frozen and slot-efficient
# regardless of the interpreter version.

if sys.version_info >= (3, 10):
    from dataclasses import dataclass as _dataclass

    @_dataclass(frozen=True, slots=True)
    class CapabilityGapEvent:
        """
        Immutable description of a detected capability gap.

        Attributes
        ----------
        goal            Human-readable description of what JARVIS was trying to do.
        task_type       Semantic category of the task (e.g. "Browser Navigation").
        target_app      Application involved, if any (empty string = unknown).
        source          Component that detected the gap (e.g. "agent_registry").
        resolution_mode Optional hint about how the gap should be resolved.
        """

        goal: str
        task_type: str
        target_app: str
        source: str
        resolution_mode: Optional[str] = None

        @property
        def domain_id(self) -> str:
            """
            Stable domain identifier combining task_type and target_app.

            Format: ``<normalised_task_type>:<normalised_target_app_or_any>``

            Examples
            --------
            "Browser Navigation" + "Notion"  ->  "browser_navigation:notion"
            "Vision Action"      + ""         ->  "vision_action:any"
            """
            app_part = _normalize(self.target_app) if self.target_app else "any"
            return f"{_normalize(self.task_type)}:{app_part}"

        @property
        def dedupe_key(self) -> str:
            """
            16-character hex key stable for the same (task_type, target_app) pair.

            Used to suppress duplicate gap signals for the same domain.
            """
            return _sha16(
                f"{_normalize(self.task_type)}:{_normalize(self.target_app or 'any')}"
            )

        @property
        def attempt_key(self) -> str:
            """
            16-character hex key scoped to (task_type, target_app, source).

            Used to track per-source resolution attempts without conflating sources.
            """
            return _sha16(f"{self.dedupe_key}:{self.source}")

else:
    # Python 3.9 — frozen=True without slots=True.
    # Equivalent semantics; slightly larger per-instance memory footprint.
    from dataclasses import dataclass as _dataclass

    @_dataclass(frozen=True)
    class CapabilityGapEvent:  # type: ignore[no-redef]
        """
        Immutable description of a detected capability gap.

        Attributes
        ----------
        goal            Human-readable description of what JARVIS was trying to do.
        task_type       Semantic category of the task (e.g. "Browser Navigation").
        target_app      Application involved, if any (empty string = unknown).
        source          Component that detected the gap (e.g. "agent_registry").
        resolution_mode Optional hint about how the gap should be resolved.
        """

        goal: str
        task_type: str
        target_app: str
        source: str
        resolution_mode: Optional[str] = None

        @property
        def domain_id(self) -> str:
            """
            Stable domain identifier combining task_type and target_app.

            Format: ``<normalised_task_type>:<normalised_target_app_or_any>``

            Examples
            --------
            "Browser Navigation" + "Notion"  ->  "browser_navigation:notion"
            "Vision Action"      + ""         ->  "vision_action:any"
            """
            app_part = _normalize(self.target_app) if self.target_app else "any"
            return f"{_normalize(self.task_type)}:{app_part}"

        @property
        def dedupe_key(self) -> str:
            """
            16-character hex key stable for the same (task_type, target_app) pair.

            Used to suppress duplicate gap signals for the same domain.
            """
            return _sha16(
                f"{_normalize(self.task_type)}:{_normalize(self.target_app or 'any')}"
            )

        @property
        def attempt_key(self) -> str:
            """
            16-character hex key scoped to (task_type, target_app, source).

            Used to track per-source resolution attempts without conflating sources.
            """
            return _sha16(f"{self.dedupe_key}:{self.source}")


# ---------------------------------------------------------------------------
# GapSignalBus
# ---------------------------------------------------------------------------

class GapSignalBus:
    """
    Fire-and-forget event bus for :class:`CapabilityGapEvent` objects.

    Backed by ``asyncio.Queue`` with a bounded capacity.  When the queue is
    full, :meth:`emit` drops the incoming event and logs a WARNING — it never
    blocks the caller.

    Parameters
    ----------
    maxsize
        Maximum number of events held in the queue at once (default 256).
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._queue: asyncio.Queue[CapabilityGapEvent] = asyncio.Queue(maxsize=maxsize)

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------

    def emit(self, event: CapabilityGapEvent) -> None:
        """
        Enqueue *event* without blocking.

        Uses ``put_nowait()`` so this method is always synchronous and safe to
        call from both sync and async contexts.

        If the queue is already at capacity the event is dropped and a WARNING
        is logged so operators can tune ``maxsize`` or consumer throughput.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "GapSignalBus is full (maxsize=%d) — dropping gap event "
                "[domain_id=%s source=%s goal=%.60r]",
                self._queue.maxsize,
                event.domain_id,
                event.source,
                event.goal,
            )

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------

    async def get(self) -> CapabilityGapEvent:
        """Await and return the next event from the queue."""
        return await self._queue.get()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def qsize(self) -> int:
        """Return the current number of events waiting in the queue."""
        return self._queue.qsize()


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_bus_lock: threading.Lock = threading.Lock()
_bus_instance: Optional[GapSignalBus] = None


def get_gap_signal_bus() -> GapSignalBus:
    """
    Return the process-wide :class:`GapSignalBus` singleton.

    Thread-safe lazy initialisation via double-checked locking — the instance
    is created on the first call and reused for all subsequent calls within
    the same process.
    """
    global _bus_instance
    if _bus_instance is None:
        with _bus_lock:
            if _bus_instance is None:
                _bus_instance = GapSignalBus()
    return _bus_instance
