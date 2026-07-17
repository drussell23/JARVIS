"""Priority-aware admission gate for Aegis upstream forwards (QoS tiering).

# Why this exists (and what it deliberately is NOT)

The shape-aware read budget (forwarding._resolve_upstream_read_budget_s, 2026-07-17)
lifted the non-streaming upstream read ceiling to as much as 600s so a silent
reasoning generation is not false-502'd. That is correct, but it means a single
forward can now hold its resources for minutes. The volumetric question that
raises is: when many long-lived forwards are in flight at once, does a critical
event-loop request (a DreamEngine RT tier, a Claude fallback) get starved behind
bulk background-sensor traffic?

Two facts shape the answer:

  * There is NO shared connection pool in the proxy — forwarding opens a fresh
    ``aiohttp.ClientSession`` per request (forwarding.py). So there is no pool to
    add a queue *inside*; the only genuinely-unbounded resource is the COUNT of
    concurrent in-flight forwards (coroutines + sockets + FDs).
  * Op-level priority admission ALREADY exists upstream at BackgroundAgentPool
    (``asyncio.PriorityQueue``, route-priority ordered). This gate does NOT
    duplicate it — the pool decides which *ops* run; this decides which *forwards*
    proceed when the proxy's concurrency ceiling is saturated, and it also covers
    traffic that never touches the pool (DreamEngine complete_sync, Claude
    fallbacks, voice).

So this is an ADMISSION gate at the single forward chokepoint, not a connection
pooler. It bounds concurrent forwards (env, generous default) and, only when that
bound is saturated, admits waiters in ``X-JARVIS-QoS-Tier`` priority order via a
standard ``asyncio.PriorityQueue`` (DRY — no bespoke scheduler).

# Bulletproofing

  * FAIL-OPEN: this gate guards the credential daemon's hot path. Any internal
    error admits the request immediately — a QoS bug must never wedge provider
    traffic. A leaked slot is impossible: release is in a ``finally``.
  * Default limit is generous, so the gate is DORMANT (never blocks) under
    normal load; it only shapes traffic under genuine saturation. Proven by the
    forced-collision test, which sets the limit to 1.
  * Bounded: waiter queue is drained deterministically; no unbounded growth
    because in-flight is hard-capped and every admit has a matching release.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("aegis-daemon")


# ---------------------------------------------------------------------------
# Tier taxonomy — lower value = higher priority (asyncio.PriorityQueue is a
# min-heap, so CRITICAL(0) is dequeued before BULK(2)).
# ---------------------------------------------------------------------------


class QoSTier(IntEnum):
    """Closed QoS taxonomy for upstream forwards.

    CRITICAL — event-loop-blocking, latency-critical traffic: the DreamEngine RT
        tier and Claude fallbacks. Must not queue behind bulk work.
    STANDARD — normal interactive generation (the default when no header).
    BULK     — background-sensor / speculative traffic that can wait.
    """

    CRITICAL = 0
    STANDARD = 1
    BULK = 2


# Canonical header a client stamps to declare a forward's QoS tier. MUST
# byte-match the client-side constant (aegis_provider_bridge.QOS_TIER_HEADER_NAME);
# pinned by tests/aegis/test_qos_admission.py.
QOS_TIER_HEADER: str = "X-JARVIS-QoS-Tier"

_MAX_CONCURRENT_ENV_VAR: str = "JARVIS_AEGIS_MAX_CONCURRENT_FORWARDS"
_DEFAULT_MAX_CONCURRENT: int = 24
_ENABLED_ENV_VAR: str = "JARVIS_AEGIS_QOS_ADMISSION_ENABLED"

# Accepted spellings → tier. Case-insensitive; unknown → STANDARD (safe middle).
_TIER_ALIASES: Dict[str, QoSTier] = {
    "critical": QoSTier.CRITICAL, "0": QoSTier.CRITICAL,
    "high": QoSTier.CRITICAL, "immediate": QoSTier.CRITICAL,
    "standard": QoSTier.STANDARD, "1": QoSTier.STANDARD,
    "normal": QoSTier.STANDARD, "default": QoSTier.STANDARD,
    "bulk": QoSTier.BULK, "2": QoSTier.BULK,
    "background": QoSTier.BULK, "low": QoSTier.BULK,
    "speculative": QoSTier.BULK,
}


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def admission_enabled() -> bool:
    """``JARVIS_AEGIS_QOS_ADMISSION_ENABLED`` — default TRUE. When off, the gate
    is a pure pass-through (byte-identical to no gate). NEVER raises."""
    raw = os.environ.get(_ENABLED_ENV_VAR, "true")
    return _truthy(raw)


def _max_concurrent() -> int:
    """Concurrent-forward ceiling. Env-tunable; invalid / non-positive → 24.
    NEVER raises."""
    raw = os.environ.get(_MAX_CONCURRENT_ENV_VAR, "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (ValueError, TypeError):
            pass
    return _DEFAULT_MAX_CONCURRENT


def tier_from_header_value(value: Optional[str]) -> QoSTier:
    """Map a raw ``X-JARVIS-QoS-Tier`` value to a tier. Absent / unknown →
    STANDARD (the safe middle — never silently critical, never silently starved).
    NEVER raises."""
    if not value:
        return QoSTier.STANDARD
    try:
        return _TIER_ALIASES.get(value.strip().lower(), QoSTier.STANDARD)
    except Exception:  # noqa: BLE001
        return QoSTier.STANDARD


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class ForwardAdmissionGate:
    """Bounds concurrent forwards; admits waiters in QoS-priority order.

    A single ``asyncio.Lock`` guards the in-flight counter and the waiter
    ``PriorityQueue``. When a slot is free, admission is immediate. When
    saturated, the caller parks on a future ordered by ``(tier, seq)`` — a
    strict priority with FIFO tie-break inside a tier. Release wakes the
    highest-priority waiter.
    """

    def __init__(self, *, max_concurrent: Optional[int] = None) -> None:
        self._explicit_max = max_concurrent
        self._in_flight = 0
        # Heap of (tier_value, seq, future). PriorityQueue would also work but a
        # plain list + heapq under the lock avoids a second async primitive and
        # keeps wake-up strictly synchronous with release.
        self._waiters: List[Tuple[int, int, "asyncio.Future[None]"]] = []
        self._seq = itertools.count()
        self._lock = asyncio.Lock()

    def _limit(self) -> int:
        return self._explicit_max if self._explicit_max is not None else _max_concurrent()

    # -- observability ---------------------------------------------------

    def snapshot(self) -> Dict[str, int]:
        # Read-only; intentionally lock-free (best-effort telemetry).
        return {
            "in_flight": self._in_flight,
            "waiting": len(self._waiters),
            "limit": self._limit(),
        }

    # -- core admission --------------------------------------------------

    @asynccontextmanager
    async def admit(self, tier: QoSTier):
        """Admit a forward of *tier*, blocking in priority order only if the
        concurrency ceiling is saturated. Always releases the slot on exit.

        FAIL-OPEN: if admission bookkeeping raises for any reason, the forward
        proceeds un-gated rather than being blocked or dropped.
        """
        admitted = False
        try:
            await self._acquire(tier)
            admitted = True
        except Exception:  # noqa: BLE001 — never block provider traffic on a gate bug
            logger.debug("[QoSAdmission] acquire faulted — admitting fail-open", exc_info=True)
            admitted = False
        try:
            yield
        finally:
            if admitted:
                try:
                    await self._release()
                except Exception:  # noqa: BLE001
                    logger.debug("[QoSAdmission] release faulted", exc_info=True)

    async def _acquire(self, tier: QoSTier) -> None:
        import heapq
        async with self._lock:
            if self._in_flight < self._limit():
                self._in_flight += 1
                return
            # Saturated — park on a priority-ordered future.
            loop = asyncio.get_event_loop()
            fut: "asyncio.Future[None]" = loop.create_future()
            heapq.heappush(
                self._waiters, (int(tier), next(self._seq), fut),
            )
        # Await OUTSIDE the lock so releases can proceed.
        await fut

    async def _release(self) -> None:
        import heapq
        async with self._lock:
            if self._waiters:
                # Hand the just-freed slot directly to the highest-priority
                # waiter (in_flight stays constant — a transfer, not a dip).
                _tier, _seq, fut = heapq.heappop(self._waiters)
                if not fut.done():
                    fut.set_result(None)
                    return
            # No waiter took the slot → the forward count actually drops.
            self._in_flight = max(0, self._in_flight - 1)


# ---------------------------------------------------------------------------
# Process singleton
# ---------------------------------------------------------------------------

_default_gate: Optional[ForwardAdmissionGate] = None
_singleton_lock = asyncio.Lock()


def get_admission_gate() -> ForwardAdmissionGate:
    """Process-wide gate. Lazily constructed (binds no loop at import)."""
    global _default_gate
    if _default_gate is None:
        _default_gate = ForwardAdmissionGate()
    return _default_gate


def reset_gate_for_tests() -> None:
    global _default_gate
    _default_gate = None


__all__ = [
    "QoSTier",
    "QOS_TIER_HEADER",
    "ForwardAdmissionGate",
    "admission_enabled",
    "tier_from_header_value",
    "get_admission_gate",
    "reset_gate_for_tests",
]
