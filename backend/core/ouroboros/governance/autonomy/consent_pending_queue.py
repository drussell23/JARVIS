"""A graph node waiting on a human is not a graph that has stopped.

`capability_router` already refuses to hold the LLM's turn open while an
operator decides — it yields SUSPENDED in 0.0 ms. That fixes ONE task. It does
not fix the graph: a work unit whose tool call suspended is still, from the
scheduler's point of view, a unit that was handed out and never came back. With
a bounded worker pool, enough of those and the whole swarm idles behind one
unanswered prompt while unrelated, non-dependent branches sit ready.

WHY THIS IS SIX LINES AND NOT A SCHEDULER
-------------------------------------------
`SubagentScheduler._compute_ready_units` already answers "what may run now":

    for unit in graph.units:
        if unit.unit_id in completed | failed | cancelled | running: continue
        if all(dep in completed for dep in unit.dependency_ids): ready.append(...)

A parked unit is none of those four. It is not running (nothing is executing),
not completed, not failed, not cancelled — so today it silently re-enters
`ready` and gets dispatched again, re-asking the operator on every pass.

So the fix is a FIFTH state in that filter, not a second traversal. The
existing topological logic is correct and stays untouched; it simply gains a
word for "handed to a human". Everything else — dependency ordering, the
concurrency limit, wave progression — keeps working because none of it needed
to change.

A parked unit's DEPENDENTS stay blocked, and that is deliberate: they depend on
work that has not happened. Only genuinely independent branches proceed, which
is the whole and only claim being made.

BOUNDED, AND HONEST WHEN IT GIVES UP
--------------------------------------
Parking is not free — a unit parked forever is a leak that looks like patience.
Entries carry the same TTL discipline as the router's consent, and an expired
park RELEASES the unit rather than deleting it, so the graph resolves the unit
normally (its tool call will re-ask, or fail closed) instead of the node
vanishing from a DAG that still lists it.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Ouroboros.ConsentPendingQueue")

CONSENT_QUEUE_SCHEMA_VERSION: str = "consent_pending_queue.v1"


def queue_enabled() -> bool:
    """Master gate. Default TRUE — failure-path-only: it changes nothing until
    a unit actually suspends on consent. NEVER raises."""
    return (os.environ.get("JARVIS_CONSENT_QUEUE_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def park_ttl_s() -> float:
    """How long a unit may sit parked. Clamped. NEVER raises.

    Nothing blocks for this. It bounds how long a graph will hold a slot open
    for an operator who may never answer.
    """
    try:
        v = float(os.environ.get("JARVIS_CONSENT_PARK_TTL_S", "900"))
    except (TypeError, ValueError):
        v = 900.0
    return max(30.0, min(v, 24 * 3600.0))


def max_parked() -> int:
    """Ring bound per graph. NEVER raises."""
    try:
        return max(1, int(os.environ.get("JARVIS_CONSENT_MAX_PARKED", "32")))
    except (TypeError, ValueError):
        return 32


@dataclass
class ParkedUnit:
    """One work unit awaiting operator consent."""

    unit_id: str
    graph_id: str
    request_id: str = ""
    capability: str = ""
    parked_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return (time.time() - self.parked_at) > park_ttl_s()

    def age_s(self) -> float:
        return max(0.0, time.time() - self.parked_at)


class ConsentPendingQueue:
    """Units parked on consent, per graph. Every method NEVER raises."""

    def __init__(self) -> None:
        self._parked: Dict[str, Dict[str, ParkedUnit]] = {}
        self._stats: Dict[str, int] = {"parked": 0, "released": 0,
                                       "expired": 0, "rejected": 0}

    def park(self, graph_id: str, unit_id: str, *, request_id: str = "",
             capability: str = "") -> bool:
        """Park a unit. Returns False if the graph is at its bound.

        Refusing at the bound rather than evicting: dropping the OLDEST parked
        unit would silently un-park something an operator is still looking at.
        A refusal leaves the unit schedulable, which is the safe direction —
        it will re-ask.
        """
        if not queue_enabled():
            return False
        try:
            bucket = self._parked.setdefault(str(graph_id), {})
            if unit_id in bucket:
                return True                      # idempotent re-park
            if len(bucket) >= max_parked():
                self._stats["rejected"] += 1
                logger.warning(
                    "[ConsentQueue] graph %s at the parking bound (%d) — "
                    "unit %s stays schedulable and will re-ask",
                    graph_id, max_parked(), unit_id)
                return False
            bucket[unit_id] = ParkedUnit(
                unit_id=str(unit_id), graph_id=str(graph_id),
                request_id=str(request_id), capability=str(capability))
            self._stats["parked"] += 1
            logger.info("[ConsentQueue] unit %s parked on consent for '%s' — "
                        "graph %s continues on independent branches",
                        unit_id, capability or "?", graph_id)
            return True
        except Exception:  # noqa: BLE001
            return False

    def release(self, graph_id: str, unit_id: str) -> Optional[ParkedUnit]:
        """Un-park a resolved unit so the scheduler may pick it up. NEVER raises."""
        try:
            bucket = self._parked.get(str(graph_id))
            if not bucket:
                return None
            unit = bucket.pop(str(unit_id), None)
            if unit is not None:
                self._stats["released"] += 1
            if not bucket:
                self._parked.pop(str(graph_id), None)
            return unit
        except Exception:  # noqa: BLE001
            return None

    def parked_ids(self, graph_id: str) -> Set[str]:
        """The fifth state for `_compute_ready_units`. NEVER raises.

        Sweeps expirations on read — an expired park RELEASES rather than
        lingering, so a unit whose operator never answered rejoins the graph
        instead of disappearing from a DAG that still lists it.
        """
        try:
            bucket = self._parked.get(str(graph_id))
            if not bucket:
                return set()
            for uid, unit in list(bucket.items()):
                if unit.expired():
                    bucket.pop(uid, None)
                    self._stats["expired"] += 1
                    logger.info(
                        "[ConsentQueue] unit %s park EXPIRED after %.0fs — "
                        "returning it to the graph", uid, unit.age_s())
            if not bucket:
                self._parked.pop(str(graph_id), None)
                return set()
            return set(bucket)
        except Exception:  # noqa: BLE001
            return set()

    def snapshot(self, graph_id: str = "") -> List[Dict[str, Any]]:
        """Transport-safe view for a surface. NEVER raises."""
        try:
            out: List[Dict[str, Any]] = []
            buckets = ([self._parked.get(str(graph_id), {})] if graph_id
                       else list(self._parked.values()))
            for bucket in buckets:
                for u in bucket.values():
                    out.append({"unit_id": u.unit_id, "graph_id": u.graph_id,
                                "capability": u.capability,
                                "request_id": u.request_id,
                                "age_s": round(u.age_s(), 1)})
            return out
        except Exception:  # noqa: BLE001
            return []

    def clear(self, graph_id: str = "") -> None:
        """Drop parks for a finished graph. NEVER raises."""
        try:
            if graph_id:
                self._parked.pop(str(graph_id), None)
            else:
                self._parked.clear()
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> Dict[str, Any]:
        try:
            return {"schema_version": CONSENT_QUEUE_SCHEMA_VERSION,
                    "enabled": queue_enabled(),
                    "graphs": len(self._parked),
                    "pending": sum(len(b) for b in self._parked.values()),
                    **self._stats}
        except Exception:  # noqa: BLE001
            return {"schema_version": CONSENT_QUEUE_SCHEMA_VERSION}


_QUEUE: Optional[ConsentPendingQueue] = None


def get_consent_queue() -> ConsentPendingQueue:
    """Process-wide queue. NEVER raises."""
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = ConsentPendingQueue()
    return _QUEUE


def reset_consent_queue() -> None:
    """Testing seam. NEVER raises."""
    global _QUEUE
    _QUEUE = None
