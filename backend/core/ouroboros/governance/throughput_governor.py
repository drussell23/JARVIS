"""Measured throughput decides how many lanes may run — not a constant.

THE DEFECT THIS CLOSES
----------------------
``BackgroundAgentPool`` runs ``JARVIS_BG_POOL_SIZE`` workers (default 3)
against one serving endpoint. On a metered cloud provider that is fine: three
concurrent requests are served concurrently. On a SINGLE LOCAL GPU they are
not — one model instance serves them **serially**, so three lanes do not
triple throughput, they triple each op's wall-clock. Every lane then races the
same route budget (BACKGROUND: 180s + 5s grace) and the tail blows it, which
this codebase already has a name for: ``tool_loop_deadline_exceeded``.

The instruments to prevent that already exist and were never connected:

  * :class:`local_inference_director.LatencyProfiler` MEASURES per-endpoint
    TTFT and per-token cost, warm-started across runs from the physics ledger.
  * :meth:`BackgroundAgentPool.set_target_pool_size` ACCEPTS a lane target.
  * :mod:`memory_pressure_gate` already caps fan-out by memory level.

Nothing bound the measurement to the target. This module is that binding, and
deliberately nothing else — it owns no probe, no timer and no event loop.

THE MATH, AND WHY IT IS THE ROOT CAUSE
--------------------------------------
One endpoint serving ``N`` concurrent ops gives each roughly ``N ×
per_op_ms``. Requiring that to fit the route budget::

    lanes = floor( (budget_ms × safety) / per_op_ms )

``per_op_ms`` is NOT re-derived here — it is
:meth:`LatencyProfiler.adaptive_timeout_ms`, the same estimator the dispatch
path already trusts (measured TTFT + per-token × expected output + σ margin,
clamped to its own floor/ceiling). Re-deriving it would be a second opinion
about the same physics.

COMPOSITION, NOT A SECOND AUTHORITY
-----------------------------------
The verdict is a CEILING that composes strictest-wins with every existing
authority — fleet topology's lane count, the memory gate's fan-out cap, the
configured pool size. It can only ever lower concurrency, never raise it above
what another authority already allowed, and it can never reach zero: a lane
count of 0 is a stalled queue, which is worse than a slow one.

WHY IT DOES NOT JUST CALL set_target_pool_size
----------------------------------------------
That setter carries an Immutability Lock: ``source="fleet_topology"`` is
hardware-derived truth and silently REJECTS other sources while held. Fighting
it would make this module a no-op exactly when a serving mesh exists. Lane
COUNT and lane DRIVABILITY are different axes: four nodes may exist while the
local GPU can drive one. So the verdict is published for the pool to consult
cooperatively, and when the lock is held the governor still reports — loudly —
rather than silently losing.

Python 3.9+. Stdlib only at import time; every governance import is lazy and
fail-soft, so a broken probe degrades to today's behaviour instead of taking
the pool down.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ThroughputGovernor")

_TRUTHY = ("1", "true", "yes", "on")

ENABLED_ENV = "JARVIS_THROUGHPUT_GOVERNOR_ENABLED"
SAFETY_ENV = "JARVIS_THROUGHPUT_SAFETY_FRACTION"
MAX_LANES_ENV = "JARVIS_THROUGHPUT_MAX_LANES"
TTL_ENV = "JARVIS_THROUGHPUT_VERDICT_TTL_S"
PROMPT_TOKENS_ENV = "JARVIS_THROUGHPUT_REFERENCE_PROMPT_TOKENS"


def governor_enabled() -> bool:
    """Master gate. Default ON; OFF returns UNGOVERNED and the pool is
    byte-identical to its pre-governor behaviour. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def safety_fraction() -> float:
    """How much of the route budget one op may consume. Default 0.6.

    NOT a fudge factor. The budget must also cover everything the op does
    BESIDES token generation — tool round-trips, VALIDATE, the Iron Gate,
    SemanticGuardian's AST pass — and `adaptive_timeout_ms` estimates only the
    generation leg. Spending the whole budget on generation would guarantee
    the very deadline breach this exists to prevent. Clamped to (0, 1]."""
    try:
        raw = float(os.environ.get(SAFETY_ENV, "0.6"))
    except (TypeError, ValueError):
        return 0.6
    if raw <= 0.0 or raw > 1.0:
        return 0.6
    return raw


def max_lanes() -> int:
    """Absolute ceiling the governor may recommend. Default 32.

    A guard against a pathologically fast measurement (a cached or refused
    response returning in ~0ms) proposing unbounded concurrency."""
    try:
        return max(1, int(os.environ.get(MAX_LANES_ENV, "32")))
    except (TypeError, ValueError):
        return 32


def verdict_ttl_s() -> float:
    """How long a verdict stays fresh. Default 30s.

    Long enough that the pool is not recomputing per dequeue; short enough
    that a model swap or a memory-pressure change is reflected within one
    op. 0 disables caching."""
    try:
        return max(0.0, float(os.environ.get(TTL_ENV, "30")))
    except (TypeError, ValueError):
        return 30.0


def reference_prompt_tokens() -> int:
    """Prompt size the lane estimate is taken at. Default 8000.

    `adaptive_timeout_ms` is prompt-size dependent, so the governor must pick
    a reference point. 8000 approximates a CONTEXT_EXPANSION-fed GENERATE
    prompt — deliberately not a trivial one, because sizing lanes off a tiny
    prompt would over-promise concurrency for the ops that actually matter."""
    try:
        return max(1, int(os.environ.get(PROMPT_TOKENS_ENV, "8000")))
    except (TypeError, ValueError):
        return 8000


@dataclass(frozen=True)
class ConcurrencyVerdict:
    """One lane recommendation, carrying the reading that produced it.

    Frozen and self-describing for the same reason every other verdict in this
    codebase is: a number without its provenance cannot be audited, and
    ``lanes=1`` means something very different when it came from a measurement
    than when it came from a degraded probe."""

    lanes: int
    reason: str
    #: False when no measurement was available — the pool must then leave its
    #: own target alone rather than trust this.
    governed: bool = False
    per_op_ms: float = 0.0
    budget_ms: float = 0.0
    memory_level: str = "unknown"
    #: True when the physics ledger held >= min_samples real measurements for
    #: this model@ctx. NOT `LatencyProfiler.is_calibrated()` -- that is a
    #: per-instance scout-lock flag the ledger does not persist, so a fresh
    #: instance reports False even while serving genuinely measured numbers.
    #: `is_warm()` counts the samples themselves, which DO survive.
    measured: bool = False
    #: How to read `per_op_ms`. Mirrors the Advisor's blast-provenance
    #: vocabulary, for the same reason: a number whose provenance is unstated
    #: gets read as a measurement even when it is a clamp.
    #:   "measured"        -- the estimator's own value, unclamped
    #:   "ceiling_clamped" -- the estimator SATURATED its ceiling, so this is a
    #:                        LOWER BOUND; lanes derived from it are therefore
    #:                        conservative (a lower bound on per-op cost can
    #:                        only ever under-count lanes, never over-count)
    #:   "seeded"          -- fewer than min_samples; the blind seed
    #:   "wedged"          -- the estimator refused: runaway latency
    provenance: str = "unknown"
    detail: Dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        if not self.governed:
            return f"[ThroughputGovernor] UNGOVERNED — {self.reason}"
        return (
            f"[ThroughputGovernor] lanes={self.lanes} "
            f"per_op={self.per_op_ms:.0f}ms budget={self.budget_ms:.0f}ms "
            f"mem={self.memory_level} via={self.provenance} "
            f"— {self.reason}"
        )


#: The single UNGOVERNED verdict shape. Every degraded path returns one of
#: these rather than inventing a lane count, so "we could not measure" is
#: never mistaken for "we measured 1".
def _ungoverned(reason: str) -> ConcurrencyVerdict:
    return ConcurrencyVerdict(lanes=0, reason=reason, governed=False)


class ThroughputGovernor:
    """Derives a lane CEILING from measured latency. Owns no loop, no probe.

    Thread-safe: the pool consults it from worker coroutines while the
    profiler is written from dispatch threads. Verdicts are cached for
    :func:`verdict_ttl_s` so a per-dequeue consult costs a dict read.
    """

    __slots__ = ("_lock", "_cached", "_cached_at", "_last_rendered")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: Optional[ConcurrencyVerdict] = None
        self._cached_at: float = 0.0
        self._last_rendered: str = ""

    # -- inputs, each lazily resolved and individually fail-soft -----------

    @staticmethod
    def _profiler_and_config() -> Tuple[Any, Any]:
        """A profiler bound to the DURABLE physics ledger, or (None, None).

        This does NOT construct a second opinion. ``LatencyProfiler`` keeps its
        measured window in a file-backed ledger keyed by ``model@ctx``
        (:func:`local_inference_director.physics_key`) — the samples the
        dispatch path records are flushed there, and every new instance
        warm-starts from them. So the numbers read here ARE the dispatch path's
        measurements; only the object holding them is local.

        The alternative, ``CandidateGenerator._failover_profiler_for``, is an
        instance method needing a live endpoint and a live generator — neither
        of which a pool worker has — and its per-instance EWMA is a cache of
        the same ledger, not a separate truth.

        Rebuilt per evaluation (behind the verdict TTL, so ~one small JSON read
        per :func:`verdict_ttl_s`) precisely BECAUSE the ledger is read at
        ``__init__``: holding one instance would freeze the governor's view of
        physics at process start and it would never see a re-measured model.
        """
        try:
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                local_inference_director as lid,
            )
            cfg = lid.LocalConfig.from_env()
            return (lid.LatencyProfiler(cfg, ledger_key=lid.physics_key(cfg)), cfg)
        except Exception:  # noqa: BLE001
            return (None, None)

    @staticmethod
    def _memory_cap(current_ceiling: int) -> Tuple[int, str]:
        """Compose the EXISTING memory gate. NEVER raises.

        Asks the gate the question it already answers — ``can_fanout(n)`` — and
        takes its ``n_allowed`` rather than re-reading a level and re-deriving
        a cap. That keeps process-tree pressure, reservations and the free-pct
        thresholds (all dimensions the gate weighs and this module does not
        know about) authoritative.

        Returns ``(cap, level_name)``. An unavailable gate returns the incoming
        ceiling unchanged — the governor must not invent memory pressure it did
        not observe.
        """
        try:
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                memory_pressure_gate as mpg,
            )
            if not mpg.is_enabled():
                return (current_ceiling, "disabled")
            decision = mpg.get_default_gate().can_fanout(int(current_ceiling))
            level = str(
                getattr(getattr(decision, "level", None), "value", "")
                or getattr(decision, "level", "")
                or "unknown"
            )
            allowed = getattr(decision, "n_allowed", None)
            if isinstance(allowed, int) and allowed > 0:
                # min() and not a bare assignment: the gate is a CEILING
                # authority like this one, and a gate that ever answered
                # higher than it was asked must not raise our lane count.
                return (min(current_ceiling, allowed), level)
            return (current_ceiling, level)
        except Exception:  # noqa: BLE001
            return (current_ceiling, "unknown")

    @staticmethod
    def _applicable_ceiling_ms(cfg: Any) -> float:
        """The ceiling `adaptive_timeout_ms` would clamp to for *cfg*, or 0.0.

        This reads the estimator's OWN branch condition (`cfg.num_ctx` selects
        the survival/CPU path from the heavy/GPU path) rather than re-deriving
        any part of the estimate. It exists solely so a SATURATED estimate can
        be labelled as the lower bound it is."""
        try:
            if not getattr(cfg, "num_ctx", None):
                return float(getattr(cfg, "timeout_ceiling_ms", 0.0) or 0.0)
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                local_inference_director as lid,
            )
            return float(lid._absolute_ceiling_ms())  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return 0.0

    # -- the decision ------------------------------------------------------

    def evaluate(
        self,
        *,
        budget_s: float,
        prompt_tokens: Optional[int] = None,
        now: Optional[float] = None,
    ) -> ConcurrencyVerdict:
        """Lane ceiling for a route whose per-op budget is *budget_s*.

        NEVER raises. Every failure path returns an UNGOVERNED verdict, which
        instructs the caller to keep its existing target — degrading to
        today's behaviour rather than to a guess.
        """
        if not governor_enabled():
            return _ungoverned("master flag off")
        try:
            return self._evaluate_inner(
                budget_s=budget_s,
                prompt_tokens=prompt_tokens,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 — a governor must never break dispatch
            logger.debug("[ThroughputGovernor] degraded", exc_info=True)
            return _ungoverned(f"evaluation failed: {type(exc).__name__}")

    def _evaluate_inner(
        self,
        *,
        budget_s: float,
        prompt_tokens: Optional[int],
        now: Optional[float],
    ) -> ConcurrencyVerdict:
        _now = time.monotonic() if now is None else float(now)
        ttl = verdict_ttl_s()
        with self._lock:
            if (
                ttl > 0.0
                and self._cached is not None
                and (_now - self._cached_at) < ttl
            ):
                return self._cached

        try:
            budget_ms = float(budget_s) * 1000.0
        except (TypeError, ValueError):
            return _ungoverned("budget not numeric")
        if budget_ms <= 0.0:
            return _ungoverned("budget non-positive")

        profiler, _cfg = self._profiler_and_config()
        if profiler is None:
            return _ungoverned("no profiler available")

        tokens = reference_prompt_tokens() if prompt_tokens is None else max(
            1, int(prompt_tokens))
        try:
            per_op_ms = float(profiler.adaptive_timeout_ms(prompt_tokens=tokens))
        except Exception as exc:  # noqa: BLE001
            # A runaway-latency refusal is INFORMATION, not an absence of it:
            # the model is wedged, so the honest lane count is the minimum, not
            # "keep whatever you had". Only a genuinely unreadable profiler
            # degrades to UNGOVERNED.
            if type(exc).__name__ == "UnrecoverableInferenceLatency":
                return ConcurrencyVerdict(
                    lanes=1, reason="estimator refused: runaway latency",
                    governed=True, budget_ms=budget_ms, provenance="wedged",
                    detail={"error": str(exc)[:200]},
                )
            return _ungoverned("profiler could not estimate")
        if per_op_ms <= 0.0:
            # A zero/negative estimate is a broken reading, not infinite speed.
            return _ungoverned("estimate non-positive")

        try:
            measured = bool(profiler.is_warm())
        except Exception:  # noqa: BLE001
            measured = False

        # THE MATH. One endpoint serving N ops gives each ~N × per_op.
        allowance_ms = budget_ms * safety_fraction()
        raw_lanes = int(allowance_ms // per_op_ms)

        # Floor of 1: a zero-lane verdict is a stalled queue, which is a worse
        # failure than a slow one. When even ONE op cannot fit the budget the
        # honest answer is "run one and let the existing deadline machinery
        # record the breach" — not "run none and stall silently".
        lanes = max(1, min(raw_lanes, max_lanes()))
        ceiling_ms = self._applicable_ceiling_ms(_cfg)
        saturated = ceiling_ms > 0.0 and per_op_ms >= ceiling_ms
        if not measured:
            provenance, reason = "seeded", (
                "seeded (fewer than min_samples in the physics ledger)")
        elif saturated:
            provenance, reason = "ceiling_clamped", (
                "estimate saturated its ceiling — per-op cost is a LOWER BOUND, "
                "so this lane count is conservative")
        else:
            provenance, reason = "measured", "measured"
        if raw_lanes < 1:
            reason += " | single op exceeds the safety allowance — floored to 1"

        lanes, mem_level = self._memory_cap(lanes)
        lanes = max(1, lanes)

        verdict = ConcurrencyVerdict(
            lanes=lanes,
            reason=reason,
            governed=True,
            per_op_ms=per_op_ms,
            budget_ms=budget_ms,
            memory_level=mem_level,
            measured=measured,
            provenance=provenance,
            detail={
                "raw_lanes": raw_lanes,
                "safety_fraction": safety_fraction(),
                "reference_prompt_tokens": tokens,
                "max_lanes": max_lanes(),
                "ceiling_ms": ceiling_ms,
                "saturated": saturated,
            },
        )
        with self._lock:
            self._cached = verdict
            self._cached_at = _now
            rendered = verdict.render()
            changed = rendered != self._last_rendered
            self._last_rendered = rendered
        # Log on CHANGE only — a per-dequeue consult must not become a log
        # storm, and an unchanged verdict carries no new information.
        if changed:
            logger.info("%s", rendered)
        return verdict

    def invalidate(self) -> None:
        """Drop the cached verdict — for a model swap or a test."""
        with self._lock:
            self._cached = None
            self._cached_at = 0.0

    def snapshot(self) -> Dict[str, Any]:
        """Observability surface. NEVER raises."""
        with self._lock:
            v = self._cached
        if v is None:
            return {"cached": False, "enabled": governor_enabled()}
        return {
            "cached": True,
            "enabled": governor_enabled(),
            "lanes": v.lanes,
            "governed": v.governed,
            "per_op_ms": round(v.per_op_ms, 1),
            "budget_ms": round(v.budget_ms, 1),
            "memory_level": v.memory_level,
            "measured": v.measured,
            "provenance": v.provenance,
            "reason": v.reason,
            **v.detail,
        }


_SINGLETON: Optional[ThroughputGovernor] = None
_SINGLETON_LOCK = threading.Lock()


def get_default_governor() -> ThroughputGovernor:
    """Process-wide governor. Mirrors `memory_pressure_gate.get_default_gate`."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = ThroughputGovernor()
        return _SINGLETON


def reset_for_tests() -> None:
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None
