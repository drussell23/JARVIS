# backend/core/ouroboros/governance/prewarm_director.py
"""Predictive model pre-warming -- pay the PCIe transfer before it is on the
critical path.

THE PROBLEM
-----------
A model swap is ~20GB across PCIe. ``InferenceGateway.ensure_model_resident``
already moves that cost OUT of the op's generation clock, which stops it being
misattributed as model slowness -- but the op still WAITS for it. The transfer
is serial with the work either way; only the blame moved.

The information needed to start it earlier already exists and is thrown away.
When a sensor ingests an envelope the router ACCEPTS, that acceptance is a
statement that work of a known shape is queued. The tier that work will use is
derivable from its urgency and complexity by the same function the dispatcher
uses. So the swap can begin the moment work is ENQUEUED rather than the moment
it is DEQUEUED, and the transfer overlaps the queue wait instead of following
it.

WHAT MAKES THIS SAFE RATHER THAN A RACE
---------------------------------------
Pre-warming is speculative eviction on a device with one set of weights. Three
properties, none optional:

  * **It is advisory.** ``ensure_model_resident(advisory=True)`` refuses to swap
    while any generation is in flight on that host. Weights in use are never
    evicted, so the worst outcome of a wrong prediction is a wasted probe.
  * **It queues, never barges.** It goes through the SAME per-endpoint swap
    mutex as a real dispatch, so it serializes with real work by construction
    rather than by timing.
  * **It is bounded and droppable.** Rate-limited by the same
    :class:`TokenBucket` the sensors use, single-flighted per endpoint,
    debounced per model, and wrapped in a hard wall. A pre-warm that cannot run
    cheaply does not run.

Being wrong is CHEAP BY DESIGN and that is the whole argument for doing it
speculatively: a correct prediction hides ~20GB of latency, an incorrect one
costs one HTTP probe and a bucket token.

WHAT THIS IS NOT
----------------
Not a scheduler, not a queue, and not a second opinion about which model an op
should use -- it asks ``failover_tier.resolve_tier`` exactly like the dispatch
path does, so a pre-warm can never warm a model the dispatcher would not have
chosen. Not a polling loop: there is no timer here. It is edge-triggered by
sensor emissions and idle otherwise.

Python 3.9+. Gated by ``JARVIS_PREWARM_ENABLED`` (default OFF -> no task is ever
created, byte-identical).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional, Set

from .intake.sensors.emission_control import TokenBucket, env_flag, env_num

logger = logging.getLogger("Ouroboros.PrewarmDirector")

ENABLED_ENV = "JARVIS_PREWARM_ENABLED"
CAPACITY_ENV = "JARVIS_PREWARM_BUCKET_CAPACITY"
REFILL_ENV = "JARVIS_PREWARM_REFILL_PER_S"
DEBOUNCE_ENV = "JARVIS_PREWARM_DEBOUNCE_S"
WALL_ENV = "JARVIS_PREWARM_WALL_S"

#: Outcomes, returned by `hint` for observability. A pre-warm is best-effort, so
#: every refusal is a NAMED state rather than an exception or a silent return.
OUTCOME_DISABLED = "disabled"
OUTCOME_NO_LOOP = "no_running_loop"
OUTCOME_THROTTLED = "throttled"
OUTCOME_DEBOUNCED = "debounced"
OUTCOME_SINGLE_FLIGHT = "single_flight"
OUTCOME_UNRESOLVED = "unresolved_target"
OUTCOME_SCHEDULED = "scheduled"


def prewarm_enabled() -> bool:
    """Master gate. Default OFF -- and OFF means no task is created at all, not
    a task that returns early. NEVER raises."""
    return env_flag(ENABLED_ENV, "0")


def bucket_capacity() -> float:
    """Burst size. Small on purpose: pre-warming is speculative, and a burst
    large enough to chain several swaps would spend more PCIe bandwidth on
    guesses than on work. Clamped, live-read. NEVER raises."""
    return env_num(CAPACITY_ENV, 2.0, 0.0, 32.0)


def bucket_refill_per_s() -> float:
    """Sustained rate. Default one token per ~5 minutes, i.e. pre-warming is a
    rare event tied to queue transitions, not a background activity. Clamped,
    live-read. NEVER raises."""
    return env_num(REFILL_ENV, 1.0 / 300.0, 0.0, 10.0)


def debounce_s() -> float:
    """How long the same (endpoint, model) stays un-repeatable. Default 300s.

    Guards the shape a miner scan actually produces: one cycle emits many
    envelopes of the SAME strategy within seconds, and every one of them
    predicts the same tier. Without this they would be N identical pre-warms,
    of which the first is useful and the rest are pure probe traffic. NEVER
    raises."""
    return env_num(DEBOUNCE_ENV, 300.0, 0.0, 86_400.0)


def wall_s() -> float:
    """Hard ceiling on one pre-warm attempt, INCLUDING time spent waiting for
    the swap mutex. Default 600s.

    Required, not defensive: the mutex is fair to real dispatches, so under
    sustained load an advisory waiter could sit behind them indefinitely. A
    pre-warm that has been waiting ten minutes has already lost its reason to
    exist -- the work it was predicting has long since dequeued. NEVER raises."""
    return env_num(WALL_ENV, 600.0, 5.0, 3_600.0)


class PrewarmDirector:
    """Edge-triggered, non-blocking speculative residency.

    Not a singleton by construction -- :func:`get_default_director` provides the
    process-wide one and tests build their own.
    """

    def __init__(
        self,
        *,
        gateway: "Optional[Any]" = None,
        bucket: "Optional[TokenBucket]" = None,
    ) -> None:
        self._lock = threading.Lock()
        self._gateway = gateway
        self._bucket = bucket or TokenBucket(bucket_capacity, bucket_refill_per_s)
        #: (endpoint, model) -> monotonic time of the last ATTEMPT. Records the
        #: attempt, not the success: a failed pre-warm that is immediately
        #: retried is the failure mode debouncing exists to prevent.
        self._last_attempt: Dict[str, float] = {}
        #: Endpoints with a pre-warm in flight. Bounds concurrency to one swap
        #: per device -- two speculative swaps racing on one GPU is strictly
        #: worse than none.
        self._in_flight: Set[str] = set()
        #: Strong refs to scheduled tasks. asyncio only holds weak references,
        #: so a fire-and-forget task can be garbage collected mid-await and
        #: vanish without running.
        self._tasks: Set["asyncio.Task"] = set()
        self._stats: Dict[str, int] = {}

    # -- resolution --------------------------------------------------------

    def _get_gateway(self) -> "Optional[Any]":
        """The gateway to warm against. Lazy so importing this module never
        constructs one. NEVER raises."""
        if self._gateway is not None:
            return self._gateway
        try:
            from .inference_gateway import get_default_gateway  # noqa: PLC0415
            return get_default_gateway()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _predicted_model(urgency: str, complexity: str) -> str:
        """The model the DISPATCHER would pick for this shape of work.

        Delegates to ``failover_tier.resolve_tier`` rather than reimplementing
        the urgency/complexity rules. That is not merely DRY: a private copy
        could drift and start warming a model the dispatcher would never
        request, which is worse than not warming at all -- it would evict the
        right weights to install the wrong ones. NEVER raises."""
        try:
            from .failover_tier import resolve_tier  # noqa: PLC0415
            tier = resolve_tier(urgency=urgency or "", complexity=complexity or "")
            return (getattr(tier, "model_label", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _resolve_target(self, model: str, route: Optional[str]) -> "Optional[Any]":
        """A :class:`GatewayTarget` for *model* on the endpoint the gateway
        would route to. Reuses ``target_for`` for endpoint + health resolution
        and substitutes only the model, so a pre-warm can never aim at a host
        the router considers unhealthy. NEVER raises."""
        gw = self._get_gateway()
        if gw is None:
            return None
        try:
            import dataclasses  # noqa: PLC0415
            base = gw.target_for(route=route)
            if base is None:
                return None
            if not model or model == getattr(base, "model_name", None):
                return base
            return dataclasses.replace(base, model_name=model)
        except Exception:  # noqa: BLE001
            return None

    # -- the emission seam -------------------------------------------------

    def _bump(self, outcome: str) -> None:
        try:
            with self._lock:
                self._stats[outcome] = self._stats.get(outcome, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    def hint(
        self,
        *,
        urgency: str = "",
        complexity: str = "",
        route: Optional[str] = None,
        source: str = "",
    ) -> str:
        """Signal that work of this shape has been ENQUEUED. NON-BLOCKING.

        Returns a named outcome and NEVER raises, NEVER awaits and NEVER
        performs I/O on the caller's stack. The caller is a sensor on the
        intake hot path: it must be able to call this without knowing whether a
        gateway, an event loop or a GPU exists.

        Every refusal path is checked BEFORE a task is created, so the common
        case (gate off, or throttled) allocates nothing."""
        try:
            if not prewarm_enabled():
                return OUTCOME_DISABLED

            # A loop is required to schedule onto. Sensors run under asyncio,
            # but this may also be reached from a sync test or a worker thread,
            # where the correct answer is to decline rather than to block.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._bump(OUTCOME_NO_LOOP)
                return OUTCOME_NO_LOOP

            model = self._predicted_model(urgency, complexity)
            if not model:
                self._bump(OUTCOME_UNRESOLVED)
                return OUTCOME_UNRESOLVED

            target = self._resolve_target(model, route)
            if target is None:
                self._bump(OUTCOME_UNRESOLVED)
                return OUTCOME_UNRESOLVED

            endpoint = getattr(target, "base_url", "") or ""
            key = endpoint + "|" + model
            now = time.monotonic()

            with self._lock:
                if endpoint in self._in_flight:
                    outcome = OUTCOME_SINGLE_FLIGHT
                else:
                    last = self._last_attempt.get(key)
                    if last is not None and (now - last) < debounce_s():
                        outcome = OUTCOME_DEBOUNCED
                    else:
                        outcome = ""
            if outcome:
                self._bump(outcome)
                return outcome

            # Throttle LAST among the cheap checks: a token spent on a hint that
            # would have been debounced anyway is a token the next genuinely
            # novel prediction does not get.
            if not self._bucket.take():
                self._bump(OUTCOME_THROTTLED)
                return OUTCOME_THROTTLED

            with self._lock:
                self._last_attempt[key] = now
                self._in_flight.add(endpoint)

            task = loop.create_task(self._prewarm(target, endpoint, source))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            self._bump(OUTCOME_SCHEDULED)
            return OUTCOME_SCHEDULED
        except Exception:  # noqa: BLE001 -- a hint must never break an emission
            return OUTCOME_UNRESOLVED

    async def _prewarm(self, target: "Any", endpoint: str, source: str) -> None:
        """The background attempt. Owns its own wall and swallows everything.

        Nothing awaits this and nothing consumes its result, so an exception
        escaping here would surface only as an "exception was never retrieved"
        warning at interpreter shutdown -- the least useful place to learn about
        it. It is therefore logged where it happens and never propagated."""
        gw = self._get_gateway()
        try:
            if gw is None:
                return
            started = time.monotonic()
            report = await asyncio.wait_for(
                gw.ensure_model_resident(target, advisory=True),
                timeout=wall_s(),
            )
            elapsed = time.monotonic() - started
            reason = (report or {}).get("reason", "")
            if (report or {}).get("swapped"):
                logger.info(
                    "[PrewarmDirector] pre-warmed %s on %s in %.1fs "
                    "(predicted from source=%s) — this transfer is no longer on "
                    "the op's critical path",
                    getattr(target, "model_name", "?"), endpoint, elapsed,
                    source or "?",
                )
            else:
                logger.debug(
                    "[PrewarmDirector] no swap for %s on %s (%.1fs): %s",
                    getattr(target, "model_name", "?"), endpoint, elapsed, reason,
                )
        except asyncio.TimeoutError:
            # Expected under sustained load: the mutex is fair to real work.
            logger.debug(
                "[PrewarmDirector] pre-warm for %s abandoned at the %.0fs wall — "
                "real dispatches held the device; the prediction has expired",
                getattr(target, "model_name", "?"), wall_s(),
            )
        except asyncio.CancelledError:  # noqa: PERF203 -- shutdown, not an error
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[PrewarmDirector] pre-warm degraded for %s: %s: %s",
                getattr(target, "model_name", "?"), type(exc).__name__, exc,
            )
        finally:
            try:
                with self._lock:
                    self._in_flight.discard(endpoint)
            except Exception:  # noqa: BLE001
                pass

    # -- observability -----------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Counts per outcome + live state. Read-only. NEVER raises."""
        try:
            with self._lock:
                return {
                    "enabled": prewarm_enabled(),
                    "outcomes": dict(self._stats),
                    "in_flight": sorted(self._in_flight),
                    "tracked_keys": len(self._last_attempt),
                    "tokens": round(float(self._bucket.tokens), 3),
                    "capacity": bucket_capacity(),
                    "refill_per_s": bucket_refill_per_s(),
                    "debounce_s": debounce_s(),
                    "wall_s": wall_s(),
                }
        except Exception:  # noqa: BLE001
            return {"enabled": False, "outcomes": {}}

    async def aclose(self) -> None:
        """Cancel any in-flight pre-warms. Used on shutdown so a speculative
        swap cannot outlive the loop that owns it. NEVER raises."""
        try:
            tasks = list(self._tasks)
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:  # noqa: BLE001
            pass


_DEFAULT: "Optional[PrewarmDirector]" = None
_DEFAULT_LOCK = threading.Lock()


def get_default_director() -> "PrewarmDirector":
    """Process-wide director. NEVER raises."""
    global _DEFAULT  # noqa: PLW0603
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = PrewarmDirector()
        return _DEFAULT


def hint(
    *,
    urgency: str = "",
    complexity: str = "",
    route: Optional[str] = None,
    source: str = "",
) -> str:
    """Module-level convenience over the default director. NON-BLOCKING,
    NEVER raises. This is the ONE call a sensor needs to make."""
    try:
        return get_default_director().hint(
            urgency=urgency, complexity=complexity, route=route, source=source)
    except Exception:  # noqa: BLE001
        return OUTCOME_UNRESOLVED


def reset_for_tests() -> None:
    """Drop the process-wide director. Tests only."""
    global _DEFAULT  # noqa: PLW0603
    with _DEFAULT_LOCK:
        _DEFAULT = None
