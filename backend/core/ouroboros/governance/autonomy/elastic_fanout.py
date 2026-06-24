"""elastic_fanout — §3.1 Elastic Adaptive Fan-Out for the Sovereign Swarm.

Beyond the governed base concurrency floor (3), the SwarmOrchestrator is
elastically adaptive, gated on the EXISTING ``MemoryPressureGate`` (psutil
-> /proc/meminfo -> vm_stat cascade — we REUSE it, never reimplement the
probe). Before authorizing a spawn beyond the floor, it consults live host
memory pressure:

  * pressure < ``JARVIS_SWARM_BURST_PRESSURE`` (0.65) -> permit BURST up to
    ``JARVIS_SWARM_MAX_CONCURRENCY``;
  * 0.65 <= pressure <= ``JARVIS_SWARM_BACKPRESSURE`` (0.80) -> HOLD at the
    current level (no burst, no shrink);
  * pressure > 0.80 -> FREEZE instantiation; pending workers are held in a
    FIFO queue (no drop, no loss), drained as pressure recedes.

**Fail-CLOSED:** an unreadable / not-ok pressure probe is treated as
> 0.80 (FREEZE), NEVER as < 0.65 (BURST). The swarm never bursts blind.

This operates WITHIN the global governance ceiling (the SensorGovernor op
cap + the MemoryPressureGate's own ``can_fanout`` cap still apply at the
scheduler) — the elasticity only chooses a concurrency level inside it.
"""
from __future__ import annotations

import collections
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knobs (no hardcoded thresholds / concurrency)
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def base_floor() -> int:
    """The governed base concurrency floor (always permitted)."""
    return max(1, _env_int("JARVIS_SWARM_BASE_FLOOR", 3))


def max_concurrency() -> int:
    return max(base_floor(), _env_int("JARVIS_SWARM_MAX_CONCURRENCY", 8))


def burst_pressure() -> float:
    return _env_float("JARVIS_SWARM_BURST_PRESSURE", 0.65)


def backpressure() -> float:
    return _env_float("JARVIS_SWARM_BACKPRESSURE", 0.80)


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


class FanoutAction(str, Enum):
    BURST = "burst"
    HOLD = "hold"
    FREEZE = "freeze"


@dataclass(frozen=True)
class ElasticFanoutDecision:
    """A fan-out decision the orchestrator/scheduler honors."""

    action: FanoutAction
    permitted_concurrency: int
    used_pressure: float
    free_pct: float
    probe_source: str
    probe_ok: bool
    reason: str

    @property
    def may_spawn_beyond_floor(self) -> bool:
        return self.action is FanoutAction.BURST


# ---------------------------------------------------------------------------
# Pressure read (REUSES MemoryPressureGate.probe — no reimplementation)
# ---------------------------------------------------------------------------


def _read_used_pressure(gate: Any) -> "tuple[float, float, str, bool]":
    """Return (used_pressure_fraction, free_pct, source, ok).

    Fail-CLOSED: any exception, a not-ok probe, or a probe missing
    ``free_pct`` -> (1.0, 0.0, source, False) which the caller maps to
    FREEZE. We never invent a healthy reading from a bad probe.
    """
    try:
        probe = gate.probe()
    except Exception:  # noqa: BLE001
        logger.debug("[ElasticFanout] probe raised -> FREEZE (fail-closed)",
                     exc_info=True)
        return (1.0, 0.0, "probe_error", False)

    ok = bool(getattr(probe, "ok", False))
    source = str(getattr(probe, "source", "unknown"))
    if not ok:
        return (1.0, 0.0, source, False)

    free_pct = getattr(probe, "free_pct", None)
    try:
        free_pct = float(free_pct)
    except (TypeError, ValueError):
        return (1.0, 0.0, source, False)

    # free_pct is a percentage 0..100; used pressure is the complement as a
    # 0..1 fraction. Clamp defensively.
    free_pct = max(0.0, min(100.0, free_pct))
    used = max(0.0, min(1.0, (100.0 - free_pct) / 100.0))
    return (used, free_pct, source, True)


def decide_fanout(
    *,
    gate: Any,
    current_concurrency: int,
    n_pending: int,
) -> ElasticFanoutDecision:
    """Decide whether the swarm may burst, hold, or freeze.

    Parameters
    ----------
    gate:
        A ``MemoryPressureGate`` (or anything exposing ``probe()`` ->
        object with ``free_pct``/``ok``/``source``). REUSED, not
        reimplemented.
    current_concurrency:
        The concurrency already authorized for this graph.
    n_pending:
        How many additional workers want to spawn.
    """
    floor = base_floor()
    ceiling = max_concurrency()
    cur = max(0, int(current_concurrency))

    used, free_pct, source, ok = _read_used_pressure(gate)

    # Up to the floor is ALWAYS permitted regardless of pressure read — the
    # base governed concurrency is the contract. Elasticity only governs
    # spawns BEYOND the floor.
    floor_target = min(ceiling, max(floor, cur, min(floor, cur + n_pending)))

    if not ok:
        # Fail-CLOSED: unreadable probe -> FREEZE at >floor, never burst.
        permitted = min(max(cur, floor), ceiling)
        return ElasticFanoutDecision(
            action=FanoutAction.FREEZE,
            permitted_concurrency=permitted,
            used_pressure=used,
            free_pct=free_pct,
            probe_source=source,
            probe_ok=False,
            reason="unreadable pressure probe -> FREEZE (fail-closed)",
        )

    if used > backpressure():
        permitted = min(max(cur, floor), ceiling)
        return ElasticFanoutDecision(
            action=FanoutAction.FREEZE,
            permitted_concurrency=permitted,
            used_pressure=used,
            free_pct=free_pct,
            probe_source=source,
            probe_ok=True,
            reason="pressure {0:.2f} > backpressure {1:.2f} -> FREEZE".format(
                used, backpressure()),
        )

    if used >= burst_pressure():
        # 65%..80% -> hold at current level (but never below the floor).
        permitted = min(ceiling, max(cur, floor))
        return ElasticFanoutDecision(
            action=FanoutAction.HOLD,
            permitted_concurrency=permitted,
            used_pressure=used,
            free_pct=free_pct,
            probe_source=source,
            probe_ok=True,
            reason="pressure {0:.2f} in [burst,backpressure] -> HOLD".format(used),
        )

    # < 65% -> burst up to ceiling, bounded by demand.
    desired = max(floor, cur + max(0, int(n_pending)))
    permitted = min(ceiling, desired)
    permitted = max(permitted, floor_target)
    return ElasticFanoutDecision(
        action=FanoutAction.BURST,
        permitted_concurrency=permitted,
        used_pressure=used,
        free_pct=free_pct,
        probe_source=source,
        probe_ok=True,
        reason="pressure {0:.2f} < burst {1:.2f} -> BURST to {2}".format(
            used, burst_pressure(), permitted),
    )


# ---------------------------------------------------------------------------
# FIFO hold queue — pending workers held (never dropped) under backpressure
# ---------------------------------------------------------------------------


@dataclass
class PendingFanoutQueue:
    """FIFO queue of pending worker ids held under FREEZE.

    No drop, no loss — workers are dequeued in arrival order as pressure
    recedes. This is the structural guarantee behind §3.1's "hold pending
    workers in a FIFO queue (no drop)".
    """

    _queue: Deque[str] = field(default_factory=collections.deque)

    def enqueue(self, worker_id: str) -> None:
        self._queue.append(str(worker_id))

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def pending_ids(self) -> List[str]:
        return list(self._queue)

    def drain(self, n: int) -> List[str]:
        """Pop up to ``n`` worker ids in FIFO order. Never raises on empty."""
        out: List[str] = []
        for _ in range(max(0, int(n))):
            if not self._queue:
                break
            out.append(self._queue.popleft())
        return out

    def admit(
        self,
        *,
        gate: Any,
        current_concurrency: int,
    ) -> "tuple[List[str], ElasticFanoutDecision]":
        """Admit as many queued workers as the live decision permits.

        Returns the admitted worker ids (FIFO) plus the decision that
        authorized them. On FREEZE / HOLD with no headroom, returns an
        empty admit list and leaves the queue intact (no drop).
        """
        decision = decide_fanout(
            gate=gate,
            current_concurrency=current_concurrency,
            n_pending=len(self._queue),
        )
        if decision.action is not FanoutAction.BURST:
            return ([], decision)
        headroom = max(0, decision.permitted_concurrency - max(0, int(current_concurrency)))
        admitted = self.drain(headroom)
        return (admitted, decision)
