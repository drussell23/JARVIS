"""F1 Slice 1 — IntakePriorityQueue primitive.

Urgency-priority heap with reserved-slot starvation guard, per-envelope
deadlines with priority-inversion emergency pop, queue-depth telemetry,
and back-pressure signals.

Why this exists
---------------
Wave 3 (6) Slice 5b graduation S1 (``bt-2026-04-24-062608``, 2026-04-24)
surfaced the ``live_reachability=blocked_by_intake_starvation`` gap.
F2 envelope stamping fired correctly on live traffic — the forced-reach
seed was stamped ``urgency=critical`` + ``routing_override=standard``
and handed to ``router.ingest(envelope)`` which returned ``"enqueued"``
— but the op never reached ``UrgencyRouter.classify()``. BG sensors
(DocStaleness / TodoScanner) burst-emitted ~12 envelopes at session
boot, crowding the class-partitioned FIFO queue head. F2's priority-0.5
clause cannot fire on an op that never reaches the router.

F2 fixed the routing DECISION. F1 fixes the routing REACHABILITY:
``urgency=critical`` must be a dequeue-priority signal, not just a label
on the envelope.

Scope boundaries
----------------
F1 changes routing ORDER, not DECISIONS. Authority invariant: this module
has zero imports of orchestrator / policy / iron_gate / risk_tier /
change_engine / candidate_generator / gate / semantic_guardian.

Slice 1 is primitive + tests only. ``UnifiedIntakeRouter`` is not wired
in Slice 1 — Slice 2 wires it behind the same master flag
(``JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED``, default off). Byte-
identical to pre-F1 dispatch order when master flag is off.

Semantics (binding)
-------------------
1. **Heap ordering**: ``(urgency_rank, enqueue_monotonic_ts, sequence)``.
   ``urgency_rank`` maps ``critical=0, high=1, normal=2, low=3``. heapq
   pops smallest first → critical dequeued before high before normal
   before low. FIFO within equal urgency via monotonic timestamp +
   sequence counter (determinism under same-tick ingestion).

2. **Reserved-slot starvation guard**: of every ``N`` sequential
   dequeues, at least ``M`` must be urgency >= ``normal`` IF any such
   envelope is in queue. Prevents pathological "infinite low-urgency
   burst after a normal entry starves it." Defaults: ``N=5``, ``M=1``.

3. **Per-envelope deadline**: each envelope carries
   ``must_be_routed_by_monotonic``. On dequeue, if any envelope is past
   its deadline, it pops out-of-order with ``dequeue_mode=priority_inversion``
   telemetry. This is §7 Authority Override in kernel form: deterministic
   core seizes attention from agentic queue order. Defaults:
   ``critical=5s, high=30s, normal=300s, low=inf``.

4. **Back-pressure**: when queue depth exceeds threshold (default 200),
   ingest refuses new normal+low envelopes with ``retry_after_s``. Critical
   is always admitted (by definition it shouldn't starve).

5. **Telemetry sink**: optional ``Callable[[str, Dict], None]`` invoked
   on every enqueue / dequeue / priority_inversion / backpressure event.
   Tests inject a list-appending sink. Never fails the queue.

Thread safety
-------------
Not thread-safe. Caller must own synchronization. In production, the
``UnifiedIntakeRouter`` already holds a lock on its queue operations —
Slice 2 wires this primitive inside that existing lock scope.
"""
from __future__ import annotations

import heapq
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Urgency ranking — lower rank = higher priority (heapq pops smallest first)
# ---------------------------------------------------------------------------

URGENCY_RANK: Dict[str, int] = {
    "critical": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}

# Per-urgency default deadline in seconds. ``critical`` must dequeue within
# this window or priority_inversion fires. Callers can override at enqueue.
# ``low`` has no deadline (float inf) — low-urgency envelopes never force-pop.
_DEFAULT_DEADLINES_S: Dict[str, float] = {
    "critical": 5.0,
    "high": 30.0,
    "normal": 300.0,
    "low": float("inf"),
}


# ---------------------------------------------------------------------------
# Env-tunable knobs — re-read at call time so operator env changes take
# effect without restart (same convention as F2 / F3 / governor knobs).
# ---------------------------------------------------------------------------


def _intake_priority_scheduler_enabled() -> bool:
    """Master flag for F1. Default OFF through Slice 3 graduation cadence."""
    raw = os.environ.get(
        "JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _reserved_dequeue_n() -> int:
    """Reserved-slot window: of every N dequeues, at least M must be >=normal."""
    raw = os.environ.get("JARVIS_INTAKE_RESERVED_N", "").strip()
    try:
        return max(1, int(raw)) if raw else 5
    except ValueError:
        return 5


def _reserved_dequeue_m() -> int:
    """Reserved-slot minimum: how many >=normal dequeues per window."""
    raw = os.environ.get("JARVIS_INTAKE_RESERVED_M", "").strip()
    try:
        return max(0, int(raw)) if raw else 1
    except ValueError:
        return 1


def _back_pressure_threshold() -> int:
    """Queue-depth cap above which BG sources get retry_after_s."""
    raw = os.environ.get("JARVIS_INTAKE_BACKPRESSURE_THRESHOLD", "").strip()
    try:
        return max(1, int(raw)) if raw else 200
    except ValueError:
        return 200


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(order=True)
class _HeapEntry:
    """heapq-compatible entry.

    Ordered by ``(urgency_rank, enqueue_monotonic, sequence)`` — lowest
    triple wins. Non-comparable fields (``envelope``, ``deadline_monotonic``,
    ``source``) are excluded from order via ``compare=False`` so heapq
    never tries to compare envelope objects on tie.
    """
    urgency_rank: int
    enqueue_monotonic: float
    sequence: int
    envelope: Any = field(compare=False)
    deadline_monotonic: float = field(compare=False, default=float("inf"))
    source: str = field(compare=False, default="")


@dataclass(frozen=True)
class EnqueueResult:
    """Result of attempting to enqueue an envelope."""
    accepted: bool
    reason: str = ""
    retry_after_s: float = 0.0


@dataclass(frozen=True)
class DequeueDecision:
    """Structured record of a dequeue — what was popped and why."""
    envelope: Any
    urgency: str
    source: str
    waited_s: float
    dequeue_mode: str  # "priority" | "reserved_slot" | "priority_inversion"
    starved_budget_pct: float  # 0.0-100.0


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


class IntakePriorityQueue:
    """Urgency-priority heap with starvation guard + deadline inversion.

    Not thread-safe — caller owns synchronization.

    Parameters
    ----------
    reserved_n, reserved_m:
        Override ``JARVIS_INTAKE_RESERVED_N`` / ``_M`` at construction.
        Useful for tests.
    back_pressure_threshold:
        Override ``JARVIS_INTAKE_BACKPRESSURE_THRESHOLD`` at construction.
    deadlines_s:
        Per-urgency deadline overrides. Missing keys use ``_DEFAULT_DEADLINES_S``.
    telemetry_sink:
        Optional callable ``(event_type, payload) -> None`` invoked on every
        enqueue / dequeue / priority_inversion / backpressure event.
        Exceptions from the sink are suppressed at DEBUG.
    clock:
        Optional monotonic-clock override for deterministic tests.
    """

    def __init__(
        self,
        *,
        reserved_n: Optional[int] = None,
        reserved_m: Optional[int] = None,
        back_pressure_threshold: Optional[int] = None,
        deadlines_s: Optional[Dict[str, float]] = None,
        telemetry_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._heap: List[_HeapEntry] = []
        self._sequence: int = 0
        self._reserved_n: int = (
            reserved_n if reserved_n is not None else _reserved_dequeue_n()
        )
        self._reserved_m: int = (
            reserved_m if reserved_m is not None else _reserved_dequeue_m()
        )
        self._back_pressure_threshold: int = (
            back_pressure_threshold
            if back_pressure_threshold is not None
            else _back_pressure_threshold()
        )
        self._deadlines_s: Dict[str, float] = (
            dict(deadlines_s) if deadlines_s is not None else dict(_DEFAULT_DEADLINES_S)
        )
        self._telemetry: Optional[Callable[[str, Dict[str, Any]], None]] = telemetry_sink
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        # Sliding window of last N dequeues: 1 if urgency >=normal, 0 if low.
        self._recent_dequeues: List[int] = []

    def __len__(self) -> int:
        return len(self._heap)

    def is_empty(self) -> bool:
        return not self._heap

    def enqueue(
        self,
        envelope: Any,
        *,
        urgency: Optional[str] = None,
        source: Optional[str] = None,
        deadline_s: Optional[float] = None,
    ) -> EnqueueResult:
        """Attempt to add envelope to queue.

        Returns ``EnqueueResult(accepted=False)`` if back-pressure is active.
        ``urgency`` / ``source`` extracted from envelope attributes if not
        provided — accepts both ``IntentEnvelope`` instances and duck-typed
        stubs.
        """
        _urgency = urgency if urgency is not None else getattr(envelope, "urgency", "normal")
        _source = source if source is not None else getattr(envelope, "source", "")
        rank = URGENCY_RANK.get(_urgency, URGENCY_RANK["normal"])
        now = self._clock()

        # Back-pressure: when queue overflows, refuse non-critical ingestion.
        # Critical is always admitted by design — a starved critical envelope
        # is the exact failure mode F1 exists to prevent.
        if len(self._heap) >= self._back_pressure_threshold and rank > URGENCY_RANK["critical"]:
            result = EnqueueResult(
                accepted=False,
                reason="queue_full",
                retry_after_s=min(
                    5.0,
                    max(0.1, self._deadlines_s.get(_urgency, 300.0) / 4.0),
                ),
            )
            self._emit("backpressure_applied", {
                "source": _source,
                "urgency": _urgency,
                "reason": "queue_full",
                "retry_after_s": result.retry_after_s,
                "queue_depth_total": len(self._heap),
            })
            return result

        # Compute deadline. None → use per-urgency default. Explicit override
        # (including float('inf') for "no deadline") wins.
        if deadline_s is None:
            deadline_s = self._deadlines_s.get(_urgency, 300.0)
        deadline_monotonic = (
            now + deadline_s if deadline_s != float("inf") else float("inf")
        )

        self._sequence += 1
        entry = _HeapEntry(
            urgency_rank=rank,
            enqueue_monotonic=now,
            sequence=self._sequence,
            envelope=envelope,
            deadline_monotonic=deadline_monotonic,
            source=_source,
        )
        heapq.heappush(self._heap, entry)

        self._emit("enqueue", {
            "urgency": _urgency,
            "source": _source,
            "deadline_s": deadline_s,
            "queue_depth_total": len(self._heap),
            "depths": self.snapshot_depths(),
        })
        return EnqueueResult(accepted=True)

    def dequeue(self) -> Optional[DequeueDecision]:
        """Pop the next envelope per priority + starvation + deadline policy.

        Returns ``None`` if the queue is empty.

        Policy order:
        1. Deadline inversion — any envelope past its deadline pops first
           regardless of urgency rank.
        2. Reserved-slot starvation guard — if window shows fewer than M
           of N recent dequeues were >=normal, and a >=normal envelope is
           waiting, force-pop it.
        3. Normal priority — heapq pop of the lowest (rank, ts, seq) tuple.
        """
        if not self._heap:
            return None
        now = self._clock()

        # Step 1: deadline inversion.
        deadlined_idx = self._find_deadlined_index(now)
        if deadlined_idx is not None:
            entry = self._pop_index(deadlined_idx)
            return self._finalize_decision(entry, now, "priority_inversion")

        # Step 2: reserved-slot starvation guard.
        if self._needs_reserved_slot_pop():
            reserved_idx = self._find_high_urgency_index()
            if reserved_idx is not None:
                entry = self._pop_index(reserved_idx)
                return self._finalize_decision(entry, now, "reserved_slot")

        # Step 3: normal priority pop (heap top).
        entry = heapq.heappop(self._heap)
        return self._finalize_decision(entry, now, "priority")

    def snapshot_depths(self) -> Dict[str, int]:
        """Return count per urgency class. Always returns all 4 keys."""
        depths = {u: 0 for u in URGENCY_RANK}
        for entry in self._heap:
            u = self._rank_to_urgency(entry.urgency_rank)
            depths[u] = depths.get(u, 0) + 1
        return depths

    def oldest_wait_s(self, urgency: Optional[str] = None) -> float:
        """Return wait-time of oldest envelope (optionally filtered by urgency).

        Zero if empty or no matching envelope.
        """
        if not self._heap:
            return 0.0
        now = self._clock()
        rank_filter = URGENCY_RANK.get(urgency) if urgency else None
        oldest_ts: Optional[float] = None
        for entry in self._heap:
            if rank_filter is not None and entry.urgency_rank != rank_filter:
                continue
            if oldest_ts is None or entry.enqueue_monotonic < oldest_ts:
                oldest_ts = entry.enqueue_monotonic
        return (now - oldest_ts) if oldest_ts is not None else 0.0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _finalize_decision(
        self, entry: _HeapEntry, now: float, mode: str,
    ) -> DequeueDecision:
        waited = now - entry.enqueue_monotonic
        urgency = self._rank_to_urgency(entry.urgency_rank)
        decision = DequeueDecision(
            envelope=entry.envelope,
            urgency=urgency,
            source=entry.source,
            waited_s=waited,
            dequeue_mode=mode,
            starved_budget_pct=self._starved_budget_pct(),
        )
        self._record_dequeue(entry.urgency_rank)
        if mode == "priority_inversion":
            deadline_s = (
                entry.deadline_monotonic - entry.enqueue_monotonic
                if entry.deadline_monotonic != float("inf")
                else None
            )
            self._emit("priority_inversion", {
                "urgency": urgency,
                "source": entry.source,
                "waited_s": waited,
                "deadline_s": deadline_s,
            })
        self._emit("dequeue", {
            "urgency": urgency,
            "source": entry.source,
            "waited_s": waited,
            "dequeue_mode": mode,
            "starved_budget_pct": decision.starved_budget_pct,
            "queue_depth_total": len(self._heap),
        })
        return decision

    def _find_deadlined_index(self, now: float) -> Optional[int]:
        """Return index of any envelope past its deadline, or None."""
        for i, entry in enumerate(self._heap):
            if entry.deadline_monotonic != float("inf") and now >= entry.deadline_monotonic:
                return i
        return None

    def _find_high_urgency_index(self) -> Optional[int]:
        """Find index of highest-priority envelope with urgency >= normal."""
        best_idx: Optional[int] = None
        best_rank = URGENCY_RANK["normal"] + 1  # any match must be strictly < this
        for i, entry in enumerate(self._heap):
            if entry.urgency_rank <= URGENCY_RANK["normal"] and entry.urgency_rank < best_rank:
                best_rank = entry.urgency_rank
                best_idx = i
        return best_idx

    def _pop_index(self, idx: int) -> _HeapEntry:
        """Remove and return entry at index. O(n) rebuild — acceptable for
        bounded queue depths (<200 typical)."""
        entry = self._heap[idx]
        self._heap[idx] = self._heap[-1]
        self._heap.pop()
        if self._heap:
            heapq.heapify(self._heap)
        return entry

    def _needs_reserved_slot_pop(self) -> bool:
        """True if the recent dequeue window shows fewer than M normal+ pops.

        The window only enforces once it's full (``len >= N``) — during
        warmup we just follow priority order.
        """
        if self._reserved_m <= 0:
            return False
        window = self._recent_dequeues[-self._reserved_n:]
        if len(window) < self._reserved_n:
            return False
        normal_or_better = sum(window)
        return normal_or_better < self._reserved_m

    def _record_dequeue(self, rank: int) -> None:
        """Append 1 if rank >= normal (urgency is critical/high/normal),
        0 if low. Cap window memory to 4×N."""
        self._recent_dequeues.append(1 if rank <= URGENCY_RANK["normal"] else 0)
        cap = self._reserved_n * 4
        if len(self._recent_dequeues) > cap:
            self._recent_dequeues = self._recent_dequeues[-self._reserved_n * 2:]

    def _starved_budget_pct(self) -> float:
        """Percent of recent window that was low-urgency. 0-100."""
        window = self._recent_dequeues[-self._reserved_n:]
        if not window:
            return 0.0
        low_count = len(window) - sum(window)
        return 100.0 * low_count / len(window)

    def _rank_to_urgency(self, rank: int) -> str:
        for u, r in URGENCY_RANK.items():
            if r == rank:
                return u
        return "normal"

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Invoke telemetry sink. Never raises — suppresses at DEBUG."""
        if self._telemetry is None:
            return
        try:
            self._telemetry(event_type, payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[IntakePriorityQueue] telemetry sink raised (suppressed): %r", exc,
            )
