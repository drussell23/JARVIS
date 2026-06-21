"""
Parked Op Store — Stage 1.6 substrate
======================================

In-memory registry of operations that have released their BG worker
slot while awaiting provider I/O.  Single source of truth for "who is
parked, what are they waiting on, what was the result."

Lifecycle
---------

::

    park(op_id, attempt_seq, descriptor)  ──►  token
              │                                 │
              │                                 ▼
              │                          ParkedOpRecord
              │                          (asyncio.Event)
              │                                 │
              │                                 ▼
        out-of-pool task                  complete(token, result)
        fulfils descriptor       ────────►       │
                                                 ▼
                          await result_for(token)  unblocks
                                                 │
                                                 ▼
                                        BG pool resubmits ctx
                                          with resumed=True

Design invariants
-----------------

1. **Single-flight by (op_id, attempt_seq)** — a second ``park`` call
   with the same key returns the existing token (idempotent), so a
   GENERATE retry that re-parks does not double-dispatch.
2. **No authority** — the store does not decide whether to park; it
   only tracks state for callers that have already decided.  It does
   not invoke the orchestrator, the pool, or any provider.  AST-pinned.
3. **Bounded** — size capped by
   ``JARVIS_BG_PARK_STORE_MAX_SIZE`` (default 64).  When at capacity, the
   oldest record (by park_started_at) is evicted with its Event flipped
   to a sentinel ``ParkedOpResult(status="evicted", ...)`` so any
   awaiter unblocks cleanly rather than hanging.
4. **TTL-prunable** — records older than ``JARVIS_BG_PARK_TTL_S``
   (default 1800s) are reaped on every ``park``/``complete`` call and
   on explicit ``prune_stale()``.  Reaped records flip their Event to
   ``status="ttl_expired"``.
5. **Async-safe** — single ``asyncio.Lock`` guards the registry dict;
   ``Event.set()`` is performed inside the lock so awaiters never see a
   torn ``(status, record)`` view.  Hot path is short — no blocking I/O
   under the lock.
6. **§33.5 lossless roundtrip** — ``ParkedOpResult`` is a frozen
   dataclass with ``to_dict``/``from_dict``, matching the discipline
   used across the SWE-Bench-Pro arc.  Slice 1 does not yet persist to
   disk; the roundtrip primitives are in place for Slice 4 if durable
   continuation becomes necessary.

Master flag
-----------
``JARVIS_BG_PARK_ENABLED`` (default ``false`` per §33.1).  When off,
the substrate is byte-identical at runtime — there are no callers in
this slice.  Slice 2 will introduce the GENERATE-runner caller behind
this same flag.

Composition
-----------
* Reads ``OperationState.PARKED_GENERATE`` from
  ``backend.core.ouroboros.governance.ledger`` to keep the enum as the
  single source of truth for the park state name.
* Module-level singleton via ``get_default_store`` / ``reset_default_store``
  mirrors the pattern used by ``get_default_broker`` (B.2.0.5),
  ``get_default_store`` (Phase D), and ``get_default_semaphore``
  (_process_singletons).  Same shape — operators can monkey-patch via
  the public ``reset`` for test isolation.

This module imports only stdlib + the ledger enum.  Authority-free.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.park_signal import ParkDescriptor

logger = logging.getLogger("Ouroboros.OpParkStore")


# ---------------------------------------------------------------------------
# Env knobs — read at call time so FlagRegistry / test monkey-patching wins
# ---------------------------------------------------------------------------


_ENV_MASTER = "JARVIS_BG_PARK_ENABLED"
_ENV_TTL_S = "JARVIS_BG_PARK_TTL_S"
_ENV_MAX_SIZE = "JARVIS_BG_PARK_STORE_MAX_SIZE"

_DEFAULT_TTL_S = 1800.0  # 30 min — longer than the longest legitimate
                          # provider round-trip (DW BACKGROUND ~10 min) but
                          # short enough to reap dead resume continuations
                          # before they wedge the store.
_DEFAULT_MAX_SIZE = 64    # Bounded — empirical: BG_POOL_SIZE * 4 covers
                          # all realistic concurrent-park scenarios with
                          # plenty of headroom; eviction is observable.


def park_enabled() -> bool:
    """Return True iff the master flag is on. Read at call time."""
    return os.environ.get(_ENV_MASTER, "false").strip().lower() in {
        "true", "1", "yes", "on",
    }


# ---------------------------------------------------------------------------
# Park-policy — deterministic route-aware decision, no LLM, ~1µs
# ---------------------------------------------------------------------------


_ENV_PARK_ROUTES = "JARVIS_BG_PARK_ROUTES"

# Default policy: park only on routes whose generation budget is long
# enough that holding a slot during provider I/O dominates BG-pool
# utilization.  IMMEDIATE / STANDARD are operator-visible fast paths
# (60–120s budgets) — parking them just adds bookkeeping overhead.
# SPECULATIVE is fire-and-forget — no human is waiting, no slot to
# protect.  BACKGROUND (180s+ budget) and COMPLEX (180s+ planning + tool
# rounds) are the routes where slot release matters.  Operator
# override via JARVIS_BG_PARK_ROUTES (CSV of route names).
_DEFAULT_PARK_ROUTES = frozenset({"background", "complex"})

# Superset of routes that can dispatch via the DW batch API (Slice 36 route gate
# is standard/complex; background is batch-native). An ASYNC_BATCH_PAYLOAD op on
# any of these must detach. Distinct from _DEFAULT_PARK_ROUTES (throughput tuning)
# because the batch-strangle wedge proved 'standard' diff-codegen needs it too.
_BATCH_CAPABLE_ROUTES = frozenset({"standard", "complex", "background"})


def _resolved_park_routes() -> frozenset:
    """Resolve the set of routes eligible for parking. Read at call time.

    Empty / missing env → default.  Malformed entries (whitespace, case)
    are normalized.  Unknown route names are silently retained (they
    simply never match) so operators can experiment without breaking
    the resolver.
    """
    raw = os.environ.get(_ENV_PARK_ROUTES, "").strip()
    if not raw:
        return _DEFAULT_PARK_ROUTES
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return frozenset(parts) if parts else _DEFAULT_PARK_ROUTES


def should_park_for_route(
    provider_route: str,
    *,
    queue_pressure: bool,
    is_resumed: bool = False,
    async_batch_payload: bool = False,
) -> bool:
    """Single source of truth for "should this op park before its
    provider await?"  Pure function — no I/O, no side effects.

    Decision table
    --------------

    +--------------+---------------+-----------+----------+
    | master flag  | provider_route| queue_p.  | result   |
    +==============+===============+===========+==========+
    | off          | *             | *         | False    |
    | on           | not eligible  | *         | False    |
    | on           | eligible      | False     | False    |
    | on           | eligible      | True      | True     |
    | on           | *             | * (resumed) | False  |
    +--------------+---------------+-----------+----------+

    Parameters
    ----------
    provider_route:
        The route stamped by UrgencyRouter at ROUTE phase (e.g.
        ``"background"``, ``"complex"``, ``"standard"``, etc.).
        Compared case-insensitively against the eligible-route set.
    queue_pressure:
        ``True`` iff the BG pool has at least one queued op waiting
        for a slot.  Without pressure, parking the current op just
        adds bookkeeping with no throughput benefit — the slot would
        idle anyway.  The caller (BG pool wiring at Slice 2b)
        computes this from ``pool._queue.qsize() > 0`` or
        equivalent.
    is_resumed:
        ``True`` iff this dispatch is a resume-after-park.  Resumed
        dispatches MUST NOT re-park (would loop indefinitely); they
        materialize from the store and proceed.

    Returns
    -------
    bool
        ``True`` iff the op should park its GENERATE provider await.

    Why a pure function
    -------------------
    The policy is a deterministic compose of (1) the master flag, (2)
    a CSV-driven set of eligible routes, and (3) the caller's queue-
    pressure observation.  Keeping it pure makes the spine trivial to
    pin (parametrized table) and lets future arcs add more inputs
    without touching the BG pool's hot path.
    """
    if not park_enabled():
        return False
    if is_resumed:
        return False
    # Sovereign Transport Profiler Matrix (2026-06-20) — ACTIVE DETACHMENT.
    # An ASYNC_BATCH_PAYLOAD op MUST park regardless of queue pressure: its
    # provider call is a minutes-long async batch poll, so holding the worker slot
    # for it is pure starvation (the v-180012 fleet-wide wedge), not a throughput
    # nicety. Detach it unconditionally → free the slot → resume on batch
    # completion. The route gate here is the SUPERSET of routes that actually
    # dispatch via batch (standard/complex/background) — NOT the throughput-tuned
    # _resolved_park_routes (which omits 'standard'); the live wedge proved
    # standard-route diff-codegen is exactly where batch-only models land.
    # IMMEDIATE/SPECULATIVE never force-batch (Slice 36 route gate) so the tag is
    # never set there, but the gate is explicit for defense-in-depth.
    if async_batch_payload:
        return (provider_route or "").strip().lower() in _BATCH_CAPABLE_ROUTES
    if not queue_pressure:
        return False
    eligible = _resolved_park_routes()
    return (provider_route or "").strip().lower() in eligible


def _ttl_s() -> float:
    """Resolved TTL in seconds. Invalid values fall back to default."""
    raw = os.environ.get(_ENV_TTL_S, "")
    if not raw:
        return _DEFAULT_TTL_S
    try:
        v = float(raw)
        return v if v > 0 else _DEFAULT_TTL_S
    except (TypeError, ValueError):
        return _DEFAULT_TTL_S


def _max_size() -> int:
    """Resolved max-size in records. Invalid values fall back to default."""
    raw = os.environ.get(_ENV_MAX_SIZE, "")
    if not raw:
        return _DEFAULT_MAX_SIZE
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_MAX_SIZE
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SIZE


# ---------------------------------------------------------------------------
# Closed result-status taxonomy (operator §33.1 discipline)
# ---------------------------------------------------------------------------


_VALID_STATUSES = frozenset({
    "pending",     # park admitted, awaiting complete()
    "completed",   # complete() fired with a result payload
    "cancelled",   # cancel() fired before complete()
    "ttl_expired", # TTL reaper fired before complete()
    "evicted",     # LRU eviction fired before complete()
})


# ---------------------------------------------------------------------------
# Frozen result + record dataclasses (§33.5 lossless roundtrip)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParkedOpResult:
    """Outcome of a parked op's resume continuation.

    ``status="completed"`` carries the resume payload (provider
    response); other statuses carry ``payload={}`` and an explanatory
    ``reason`` string for observability.
    """

    status: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"ParkedOpResult.status must be one of {sorted(_VALID_STATUSES)!r}, "
                f"got {self.status!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "payload": dict(self.payload),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ParkedOpResult":
        return cls(
            status=str(d["status"]),
            payload=dict(d.get("payload", {})),
            reason=str(d.get("reason", "")),
        )


@dataclass
class _ParkedRecord:
    """Mutable internal record — not part of the public surface.

    The event flips exactly once per record lifetime (one of the four
    terminal statuses).  Subsequent ``complete``/``cancel`` calls on
    the same token are no-ops (idempotent).
    """

    op_id: str
    attempt_seq: int
    token: str
    descriptor: ParkDescriptor
    park_started_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[ParkedOpResult] = None


# ---------------------------------------------------------------------------
# ParkedOpStore
# ---------------------------------------------------------------------------


class ParkedOpStore:
    """Bounded in-memory registry of parked ops.

    Thread-model: single-event-loop, async-safe via one
    :class:`asyncio.Lock`.  All public methods are coroutines.

    The store does not own the BG pool, the orchestrator, or the
    ledger — it is a passive data structure with single-flight admission
    and terminal-flip-once invariants.  Callers compose it.
    """

    def __init__(self) -> None:
        self._records: Dict[str, _ParkedRecord] = {}
        # Sequence counter — itertools.count is monotonic and lock-free
        # for read-after-increment; used only for log correlation, not
        # for token identity (token derives from op_id + attempt_seq).
        self._seq = itertools.count(1)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @staticmethod
    def make_token(op_id: str, attempt_seq: int) -> str:
        """Single source of truth for token shape.

        Tokens are deterministic in ``(op_id, attempt_seq)`` — the
        single-flight key.  Public so callers (and tests) can compute
        the same token without holding a reference to the store.
        """
        if not op_id:
            raise ValueError("op_id must be non-empty")
        if attempt_seq < 1:
            raise ValueError("attempt_seq must be >= 1")
        return f"{op_id}::attempt-{attempt_seq}"

    async def park(
        self,
        op_id: str,
        attempt_seq: int,
        descriptor: ParkDescriptor,
    ) -> Tuple[str, bool]:
        """Admit a park, or return the existing token if already parked.

        Returns
        -------
        (token, fresh): tuple
            ``token`` is the single-flight key.  ``fresh`` is True iff
            this call admitted a new record; False iff an existing
            record was returned (idempotent).

        Raises
        ------
        RuntimeError
            If the master flag is off.  Callers MUST gate their
            park-emit on :func:`park_enabled` before calling.
        """
        if not park_enabled():
            raise RuntimeError(
                "ParkedOpStore.park called with master flag off; "
                "callers must gate on park_enabled() first"
            )
        token = self.make_token(op_id, attempt_seq)
        async with self._lock:
            self._prune_stale_locked()
            existing = self._records.get(token)
            if existing is not None:
                logger.debug(
                    "park idempotent: token=%s already admitted (seq=%d)",
                    token, next(self._seq),
                )
                return token, False
            self._evict_oldest_if_full_locked()
            record = _ParkedRecord(
                op_id=op_id,
                attempt_seq=attempt_seq,
                token=token,
                descriptor=descriptor,
                park_started_at=time.monotonic(),
            )
            self._records[token] = record
            logger.info(
                "park admitted: op_id=%s attempt=%d token=%s kind=%s seq=%d "
                "store_size=%d (state=%s)",
                op_id, attempt_seq, token, descriptor.kind, next(self._seq),
                len(self._records), OperationState.PARKED_GENERATE.value,
            )
            return token, True

    async def complete(
        self,
        token: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Fire the resume Event with a ``completed`` result.

        Returns
        -------
        bool
            True iff this call flipped the record; False iff already
            terminal (idempotent — subsequent calls are no-ops).
        """
        result = ParkedOpResult(
            status="completed",
            payload=payload or {},
        )
        return await self._terminal_flip(token, result)

    async def cancel(self, token: str, reason: str = "") -> bool:
        """Fire the resume Event with a ``cancelled`` result."""
        result = ParkedOpResult(
            status="cancelled",
            reason=reason or "cancelled",
        )
        return await self._terminal_flip(token, result)

    async def result_for(self, token: str) -> Optional[ParkedOpResult]:
        """Await the resume Event and return the terminal result.

        Returns
        -------
        Optional[ParkedOpResult]
            The terminal result (one of the four non-pending statuses).
            Returns ``None`` if the token was never admitted (caller
            mis-ordering — log and let caller decide).
        """
        async with self._lock:
            record = self._records.get(token)
            if record is None:
                logger.warning(
                    "result_for: token=%s not admitted (caller mis-ordered)",
                    token,
                )
                return None
            event = record.event
        # Await the Event OUTSIDE the lock — the lock guards registry
        # mutation, not the wait itself.  Otherwise complete() would
        # deadlock trying to acquire the lock to flip the Event.
        await event.wait()
        async with self._lock:
            # Re-read under lock to defeat any race between Event.set()
            # and result assignment in _terminal_flip.
            record = self._records.get(token)
            if record is None or record.result is None:
                return None
            return record.result

    async def prune_stale(self) -> int:
        """Reap records past TTL.  Returns count reaped.

        Idempotent — pruning an already-empty store returns 0.
        """
        async with self._lock:
            return self._prune_stale_locked()

    async def is_parked(self, token: str) -> bool:
        """Return True iff token is admitted and not yet terminal."""
        async with self._lock:
            record = self._records.get(token)
            return record is not None and record.result is None

    async def size(self) -> int:
        """Return the current store size (admitted records)."""
        async with self._lock:
            return len(self._records)

    async def reset(self) -> None:
        """Drop all records.  Used by tests and harness shutdown.

        Any awaiter on a dropped record receives ``status="cancelled"``
        with reason=``"store_reset"`` so no coroutine hangs.
        """
        async with self._lock:
            for record in list(self._records.values()):
                if record.result is None:
                    record.result = ParkedOpResult(
                        status="cancelled",
                        reason="store_reset",
                    )
                    record.event.set()
            self._records.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _terminal_flip(
        self,
        token: str,
        result: ParkedOpResult,
    ) -> bool:
        """Atomic terminal-flip: lock → assign result → set Event."""
        async with self._lock:
            record = self._records.get(token)
            if record is None:
                logger.warning(
                    "terminal_flip: token=%s not admitted "
                    "(complete/cancel before park, or after eviction)",
                    token,
                )
                return False
            if record.result is not None:
                logger.debug(
                    "terminal_flip idempotent: token=%s already %s",
                    token, record.result.status,
                )
                return False
            record.result = result
            record.event.set()
            logger.info(
                "terminal_flip: op_id=%s token=%s status=%s reason=%r",
                record.op_id, token, result.status, result.reason,
            )
            return True

    def _prune_stale_locked(self) -> int:
        """Reap records past TTL.  MUST be called with self._lock held."""
        ttl = _ttl_s()
        now = time.monotonic()
        stale_tokens = [
            tok for tok, rec in self._records.items()
            if rec.result is None and (now - rec.park_started_at) > ttl
        ]
        for tok in stale_tokens:
            rec = self._records[tok]
            rec.result = ParkedOpResult(
                status="ttl_expired",
                reason=f"age={now - rec.park_started_at:.1f}s>ttl={ttl:.0f}s",
            )
            rec.event.set()
            logger.warning(
                "ttl_expired: op_id=%s token=%s age=%.1fs ttl=%.0fs",
                rec.op_id, tok, now - rec.park_started_at, ttl,
            )
            del self._records[tok]
        return len(stale_tokens)

    def _evict_oldest_if_full_locked(self) -> None:
        """LRU-by-park-time eviction.  MUST be called with lock held."""
        cap = _max_size()
        if len(self._records) < cap:
            return
        # Pick the oldest non-terminal record by park_started_at.
        # If everything is terminal (no awaiter), drop the oldest by
        # the same key — they're all reapable.
        victim_token = min(
            self._records.keys(),
            key=lambda t: self._records[t].park_started_at,
        )
        victim = self._records.pop(victim_token)
        if victim.result is None:
            victim.result = ParkedOpResult(
                status="evicted",
                reason=f"store_at_capacity={cap}",
            )
            victim.event.set()
        logger.warning(
            "lru_evict: op_id=%s token=%s reason=store_at_capacity=%d",
            victim.op_id, victim_token, cap,
        )


# ---------------------------------------------------------------------------
# Module-level singleton — mirrors get_default_broker / get_default_store
# ---------------------------------------------------------------------------


_default_store: Optional[ParkedOpStore] = None


def get_default_store() -> ParkedOpStore:
    """Return the process-wide ParkedOpStore singleton.

    Lazy-initialized on first access.  Same pattern as
    ``get_default_broker`` (B.2.0.5) and ``get_default_store`` (Phase D).
    """
    global _default_store
    if _default_store is None:
        _default_store = ParkedOpStore()
    return _default_store


def reset_default_store() -> None:
    """Drop the singleton — used by tests and harness shutdown.

    Note: this does NOT call ``ParkedOpStore.reset()`` on the dropped
    instance.  Callers that need to unblock awaiters should call
    ``await get_default_store().reset()`` BEFORE this function.
    """
    global _default_store
    _default_store = None
