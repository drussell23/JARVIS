"""Is the model lane free for background work?

Background model work -- dreaming, intent discovery -- shares the one local
model with the pipeline, and the local engine serves ONE request at a time.
Anything that occupies the slot while an op is generating makes that op wait
the full length of the background call. Until 2026-10-09 the DreamEngine was
handed stubs that said "always idle, never yield"; it was harmless only
because it could not run at all.

This module answers the question from the two facts that decide it, both
already maintained by the organism:

* the governed loop's ACTIVE OPS -- an op holds the lane across all of its
  phases, including the gaps between its model calls;
* the inference gateway's IN-FLIGHT ledger for the local endpoint -- every
  bracketed generation from any path (rt_gate calls included).

``last_activity_s`` is exact, not sampled: the active-op set stamps the
moment it last changed (:class:`StampedOpSet`), so an op that began and
ended between two polls still resets the quiet period.

It is the ``activity_monitor`` + ``resource_governor`` the DreamEngine was
designed to take, and the gate any other background lane user consults.
NEVER raises; anything unknowable reads as BUSY -- background work must
fail toward not taking the slot.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger("Ouroboros.LaneIdleness")


class StampedOpSet(set):
    """A ``set`` that records when its membership last changed.

    Every mutation path ``set`` offers is covered, so no call site has to
    remember to stamp (the governed loop mutates its op set in four places)."""

    def __init__(self, items: Iterable[Any] = (), *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(items)
        self._clock = clock
        self.changed_at = clock()

    def _stamp(self) -> None:
        self.changed_at = self._clock()

    def add(self, item: Any) -> None:
        super().add(item)
        self._stamp()

    def discard(self, item: Any) -> None:
        super().discard(item)
        self._stamp()

    def remove(self, item: Any) -> None:
        super().remove(item)
        self._stamp()

    def pop(self) -> Any:
        item = super().pop()
        self._stamp()
        return item

    def clear(self) -> None:
        super().clear()
        self._stamp()

    def update(self, *others: Iterable[Any]) -> None:
        super().update(*others)
        self._stamp()

    def difference_update(self, *others: Iterable[Any]) -> None:
        super().difference_update(*others)
        self._stamp()

    def intersection_update(self, *others: Iterable[Any]) -> None:
        super().intersection_update(*others)
        self._stamp()

    def symmetric_difference_update(self, other: Iterable[Any]) -> None:
        super().symmetric_difference_update(other)
        self._stamp()

    def __ior__(self, other: Iterable[Any]) -> "StampedOpSet":
        self.update(other)
        return self

    def __isub__(self, other: Iterable[Any]) -> "StampedOpSet":
        self.difference_update(other)
        return self

    def __iand__(self, other: Iterable[Any]) -> "StampedOpSet":
        self.intersection_update(other)
        return self

    def __ixor__(self, other: Iterable[Any]) -> "StampedOpSet":
        self.symmetric_difference_update(other)
        return self


def _local_base_url() -> Optional[str]:
    try:
        from backend.core.ouroboros.governance.local_inference_director import (  # noqa: PLC0415
            LocalConfig, local_prime_enabled,
        )
        return LocalConfig.from_env().base_url if local_prime_enabled() else None
    except Exception:  # noqa: BLE001
        return None


def local_lane_inflight() -> int:
    """Generations occupying the local lane right now. Unknowable -> 1."""
    try:
        count = 0
        base_url = _local_base_url()
        if base_url:
            from backend.core.ouroboros.governance.inference_gateway import (  # noqa: PLC0415
                get_default_gateway,
            )
            count += int(get_default_gateway().inflight(base_url))
        from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
            get_jprime_inflight_count,
        )
        return count + int(get_jprime_inflight_count())
    except Exception:  # noqa: BLE001
        return 1


class LaneIdleness:
    """``activity_monitor`` + ``resource_governor`` over the live organism.

    ``active_ops`` is the governed loop's op set (a :class:`StampedOpSet`;
    a plain set still works, measuring idleness from this object's own
    observations). ``inflight`` defaults to :func:`local_lane_inflight`."""

    def __init__(
        self,
        active_ops: Any,
        *,
        inflight: Callable[[], int] = local_lane_inflight,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ops = active_ops
        self._inflight = inflight
        self._clock = clock
        self._last_busy = clock()

    def busy(self, *, holding: int = 0) -> bool:
        """Whether the lane is wanted by anyone but the caller, who holds
        ``holding`` of the in-flight generations itself (a background call
        is bracketed in the same ledger it consults)."""
        try:
            busy = bool(len(self._ops)) or int(self._inflight()) - max(0, holding) > 0
        except Exception:  # noqa: BLE001
            busy = True
        if busy:
            self._last_busy = self._clock()
        return busy

    async def wanted_by_others(self, holding: int) -> bool:
        """For a holder deciding whether to give the lane back mid-call."""
        return self.busy(holding=holding)

    def last_activity_s(self) -> float:
        """Seconds the lane has been quiet. 0 while busy."""
        if self.busy():
            return 0.0
        changed = getattr(self._ops, "changed_at", None)
        last = max(self._last_busy, changed) if isinstance(changed, (int, float)) else self._last_busy
        return max(0.0, self._clock() - last)

    async def should_yield(self) -> bool:
        return self.busy()


_ENV_YIELD_POLL_S = "JARVIS_LANE_YIELD_POLL_S"
_DEFAULT_YIELD_POLL_S = 1.0


def yield_poll_s() -> float:
    try:
        value = float(os.environ.get(_ENV_YIELD_POLL_S, "") or _DEFAULT_YIELD_POLL_S)
    except ValueError:
        return _DEFAULT_YIELD_POLL_S
    return value if value > 0 else _DEFAULT_YIELD_POLL_S


class Preempted(Exception):
    """The lane was wanted by live work; the background call was given back."""


async def run_unless_wanted(call: Awaitable[Any], governor: Any, *, holding: int = 1) -> Any:
    """Await ``call`` unless live work wants the lane first; then cancel it
    and raise :class:`Preempted`.

    Admission gates only decide whether background work STARTS; an op that
    arrives mid-call would otherwise wait out the whole call on a lane that
    serves one request at a time. Cancelling releases the slot: the gate's
    brackets decrement in-flight in ``finally`` and closing the request lets
    the engine abandon the generation. ``holding`` is how many in-flight
    generations the caller itself owns (its own call is in the ledger it
    reads). A governor without ``wanted_by_others`` is asked
    ``should_yield()``; anything unknowable reads as wanted. A cancellation
    of the CALLER propagates; the call is cancelled with it."""
    task = asyncio.ensure_future(call)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=yield_poll_s())
            if done:
                return task.result()
            try:
                probe = getattr(governor, "wanted_by_others", None)
                wanted = await (probe(holding) if probe is not None else governor.should_yield())
            except Exception:  # noqa: BLE001
                wanted = True
            if wanted:
                raise Preempted()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 -- the cancel we asked for
                pass


_default_lane: Optional[LaneIdleness] = None


def set_default_lane(lane: Optional[LaneIdleness]) -> None:
    """Registered by the governed loop over its live op set."""
    global _default_lane
    _default_lane = lane


def default_lane() -> LaneIdleness:
    """The organism's lane. Before (or without) a governed loop: in-flight
    generations only, with no op set to consult."""
    return _default_lane if _default_lane is not None else LaneIdleness(())


__all__ = [
    "LaneIdleness", "Preempted", "StampedOpSet", "default_lane",
    "local_lane_inflight", "run_unless_wanted", "set_default_lane",
]
