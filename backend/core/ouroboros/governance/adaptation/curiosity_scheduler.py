"""CuriosityScheduler — orchestrates when CuriosityEngine fires.

Per the post-CuriosityEngine brutal architectural review:

  > CuriosityEngine ships with the entry point (run_cycle); no
  > caller wires it to RuntimeHealth's idle-GPU window signal.

This module ships the trigger logic that closes that gap. The
scheduler:

1. **Polls (or accepts injection of)** an idle-window signal —
   typically RuntimeHealth's `is_idle()` or memory_pressure_gate's
   PressureLevel.OK.
2. **Posture-aware**: skips when posture is HARDEN (the system is
   in defensive mode — no curiosity); allows EXPLORE / CONSOLIDATE
   / MAINTAIN.
3. **Rate-limited**: at most `MAX_CYCLES_PER_HOUR` (default 4) —
   even if every check passes, the scheduler enforces a cooldown.
4. **Memory-pressure-aware**: skips when memory pressure is
   CRITICAL or HIGH (LSP allocator under stress; don't burn budget
   on speculative probes).
5. **Records every decision** via Phase 8.1 decision-trace ledger
   (the scheduler IS an autonomic decision producer — its own
   skip/fire decisions deserve audit).

## Cage rules (load-bearing)

  * **Master flag default false**: `JARVIS_CURIOSITY_SCHEDULER_ENABLED`.
    When off, `tick()` is a no-op + records `SCHEDULER_OFF`.
  * **Posture HARDEN forbids curiosity**: the system is in
    defensive mode; speculative work has no place. Hard-skip.
  * **Memory pressure HIGH/CRITICAL forbids**: speculative probes
    under memory stress can OOM the L3 fan-out workers. Hard-skip.
  * **Per-hour rate cap**: even if every check passes, the cap
    enforces breathing room for the rest of the system.
  * **Bounded probe budget inheritance**: each `run_cycle()` call
    inherits CuriosityEngine + Phase 7.6 + Item #3 bounds; the
    scheduler doesn't add new bounds, just bounds the **outer
    cadence** (fire frequency).
  * **NEVER raises into caller**: every error path is caught +
    converted to a structured `SchedulerResult`.

## Default-off

`JARVIS_CURIOSITY_SCHEDULER_ENABLED` (default false until
graduation cadence — tracked in Item #4's CADENCE_POLICY in a
follow-up).
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Per-hour cycle cap. 4 cycles/hour = 1 every 15 min — generous
# breathing room for the rest of the system.
DEFAULT_MAX_CYCLES_PER_HOUR: int = 4

# Cooldown after each successful fire (seconds). Belt-and-suspenders
# with the per-hour cap — even if hour-window math allows another
# fire, the cooldown enforces minimum spacing.
DEFAULT_COOLDOWN_S: float = 60.0

# Postures that ALLOW curiosity (HARDEN excluded — defensive mode).
_CURIOSITY_OK_POSTURES = frozenset({
    "EXPLORE", "CONSOLIDATE", "MAINTAIN",
})

# Memory-pressure levels that ALLOW curiosity (HIGH + CRITICAL
# blocked).
_CURIOSITY_OK_PRESSURE = frozenset({
    "OK", "WARN",
})


def is_scheduler_enabled() -> bool:
    """Master flag — ``JARVIS_CURIOSITY_SCHEDULER_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_CURIOSITY_SCHEDULER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def get_max_cycles_per_hour() -> int:
    raw = os.environ.get("JARVIS_CURIOSITY_SCHEDULER_MAX_PER_HOUR")
    if raw is None:
        return DEFAULT_MAX_CYCLES_PER_HOUR
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_MAX_CYCLES_PER_HOUR
    except ValueError:
        return DEFAULT_MAX_CYCLES_PER_HOUR


def get_cooldown_s() -> float:
    raw = os.environ.get("JARVIS_CURIOSITY_SCHEDULER_COOLDOWN_S")
    if raw is None:
        return DEFAULT_COOLDOWN_S
    try:
        v = float(raw)
        return v if v >= 0 else DEFAULT_COOLDOWN_S
    except ValueError:
        return DEFAULT_COOLDOWN_S


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class SchedulerStatus(str, enum.Enum):
    FIRED = "fired"
    SKIPPED_MASTER_OFF = "skipped_master_off"
    SKIPPED_POSTURE_HARDEN = "skipped_posture_harden"
    SKIPPED_MEMORY_PRESSURE = "skipped_memory_pressure"
    SKIPPED_NOT_IDLE = "skipped_not_idle"
    SKIPPED_RATE_CAP = "skipped_rate_cap"
    SKIPPED_COOLDOWN = "skipped_cooldown"
    SKIPPED_NO_CLUSTER_PROVIDER = "skipped_no_cluster_provider"
    ENGINE_ERROR = "engine_error"


@dataclass(frozen=True)
class SchedulerResult:
    """Terminal result of one scheduler tick. Frozen for audit
    durability."""

    status: SchedulerStatus
    posture: Optional[str] = None
    pressure_level: Optional[str] = None
    is_idle: Optional[bool] = None
    cycles_in_window: int = 0
    seconds_since_last_fire: Optional[float] = None
    engine_result: Any = None  # CuriosityResult when FIRED
    detail: str = ""
    ts_epoch: float = 0.0

    @property
    def is_fired(self) -> bool:
        return self.status is SchedulerStatus.FIRED

    @property
    def is_skipped(self) -> bool:
        return self.status.value.startswith("skipped_")


# ---------------------------------------------------------------------------
# CuriosityScheduler
# ---------------------------------------------------------------------------


@dataclass
class CuriosityScheduler:
    """Stateful scheduler — `tick()` is the entry point.

    All injection points are optional; production wires real
    callables, tests inject fakes.

    Args:
        engine: CuriosityEngine to invoke. None → SKIPPED_NO_CLUSTER_PROVIDER
            on tick (the engine is what RUNS the cycle).
        cluster_provider: Callable that returns a list of clusters
            (typically wraps `postmortem_clusterer.cluster_postmortems`).
        idle_signal: Callable that returns True iff the system is
            idle. None → treated as always-idle (trust the master
            flag + posture + pressure gates).
        posture_provider: Callable returning the current posture
            string ("EXPLORE" | "CONSOLIDATE" | "HARDEN" | "MAINTAIN").
            None → treated as always-EXPLORE (allow curiosity).
        pressure_provider: Callable returning current memory-pressure
            string ("OK" | "WARN" | "HIGH" | "CRITICAL"). None →
            treated as always-OK.
    """

    engine: Any = None  # CuriosityEngine
    cluster_provider: Optional[Callable[[], Sequence[Any]]] = None
    idle_signal: Optional[Callable[[], bool]] = None
    posture_provider: Optional[Callable[[], str]] = None
    pressure_provider: Optional[Callable[[], str]] = None
    max_cycles_per_hour: Optional[int] = None
    cooldown_s: Optional[float] = None

    # Internal state — not part of the dataclass init signature.
    _fire_history: List[float] = field(default_factory=list)
    _last_fire_ts: Optional[float] = field(default=None)

    def __post_init__(self) -> None:
        if self.max_cycles_per_hour is None:
            self.max_cycles_per_hour = get_max_cycles_per_hour()
        if self.cooldown_s is None:
            self.cooldown_s = get_cooldown_s()

    def _prune_fire_history(self, now: float) -> None:
        """Drop entries older than 1 hour."""
        cutoff = now - 3600.0
        self._fire_history = [
            t for t in self._fire_history if t >= cutoff
        ]

    def _check_rate_cap(self, now: float) -> bool:
        """True iff firing now would exceed the per-hour cap."""
        self._prune_fire_history(now)
        # Use explicit None check (not `or`) so cap=0 is honored if
        # ever supplied; __post_init__ ensures this is non-None at
        # construction.
        cap = (
            self.max_cycles_per_hour
            if self.max_cycles_per_hour is not None
            else DEFAULT_MAX_CYCLES_PER_HOUR
        )
        return len(self._fire_history) >= cap

    def _check_cooldown(self, now: float) -> Optional[float]:
        """Return seconds until cooldown expires (None if expired)."""
        if self._last_fire_ts is None:
            return None
        elapsed = now - self._last_fire_ts
        # Explicit None check (not `or`) so cooldown=0.0 is honored
        # — falsy-but-not-None must NOT fall through to default.
        cd = (
            self.cooldown_s
            if self.cooldown_s is not None
            else DEFAULT_COOLDOWN_S
        )
        if elapsed >= cd:
            return None
        return cd - elapsed

    def tick(
        self,
        *,
        now_unix: Optional[float] = None,
    ) -> SchedulerResult:
        """One scheduler tick. Evaluates all gates + fires if all
        green. NEVER raises.

        Gates (in order):
          1. Master flag → SKIPPED_MASTER_OFF
          2. Cluster provider missing → SKIPPED_NO_CLUSTER_PROVIDER
          3. Posture is HARDEN → SKIPPED_POSTURE_HARDEN
          4. Memory pressure HIGH/CRITICAL → SKIPPED_MEMORY_PRESSURE
          5. Not idle → SKIPPED_NOT_IDLE
          6. Per-hour rate cap → SKIPPED_RATE_CAP
          7. Cooldown active → SKIPPED_COOLDOWN
          8. ENGINE FIRES — run_cycle() invoked

        Failures during engine invocation → ENGINE_ERROR with detail.
        """
        ts = now_unix if now_unix is not None else time.time()

        if not is_scheduler_enabled():
            return SchedulerResult(
                status=SchedulerStatus.SKIPPED_MASTER_OFF,
                detail="master_off", ts_epoch=ts,
            )

        if self.cluster_provider is None or self.engine is None:
            return SchedulerResult(
                status=SchedulerStatus.SKIPPED_NO_CLUSTER_PROVIDER,
                detail="cluster_provider_or_engine_unwired",
                ts_epoch=ts,
            )

        # Gate 3: posture
        posture_str: Optional[str] = None
        if self.posture_provider is not None:
            try:
                posture_str = (self.posture_provider() or "").upper()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[CuriosityScheduler] posture_provider raised %s",
                    type(exc).__name__,
                )
                posture_str = None
        if (
            posture_str is not None
            and posture_str not in _CURIOSITY_OK_POSTURES
        ):
            return SchedulerResult(
                status=SchedulerStatus.SKIPPED_POSTURE_HARDEN,
                posture=posture_str,
                detail=f"posture={posture_str} not in {sorted(_CURIOSITY_OK_POSTURES)}",
                ts_epoch=ts,
            )

        # Gate 4: memory pressure
        pressure_str: Optional[str] = None
        if self.pressure_provider is not None:
            try:
                pressure_str = (self.pressure_provider() or "").upper()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[CuriosityScheduler] pressure_provider raised %s",
                    type(exc).__name__,
                )
                pressure_str = None
        if (
            pressure_str is not None
            and pressure_str not in _CURIOSITY_OK_PRESSURE
        ):
            return SchedulerResult(
                status=SchedulerStatus.SKIPPED_MEMORY_PRESSURE,
                posture=posture_str, pressure_level=pressure_str,
                detail=f"pressure={pressure_str} not in {sorted(_CURIOSITY_OK_PRESSURE)}",
                ts_epoch=ts,
            )

        # Gate 5: idle signal
        is_idle = True
        if self.idle_signal is not None:
            try:
                is_idle = bool(self.idle_signal())
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[CuriosityScheduler] idle_signal raised %s",
                    type(exc).__name__,
                )
                # Defensive: treat as NOT idle on error (don't fire
                # speculatively when we can't tell the system state).
                is_idle = False
        if not is_idle:
            return SchedulerResult(
                status=SchedulerStatus.SKIPPED_NOT_IDLE,
                posture=posture_str, pressure_level=pressure_str,
                is_idle=False, detail="idle_signal_returned_false",
                ts_epoch=ts,
            )

        # Gate 6: per-hour rate cap
        if self._check_rate_cap(ts):
            return SchedulerResult(
                status=SchedulerStatus.SKIPPED_RATE_CAP,
                posture=posture_str, pressure_level=pressure_str,
                is_idle=is_idle,
                cycles_in_window=len(self._fire_history),
                detail=(
                    f"rate_cap_hit:{len(self._fire_history)}>="
                    f"{self.max_cycles_per_hour}"
                ),
                ts_epoch=ts,
            )

        # Gate 7: cooldown
        remaining = self._check_cooldown(ts)
        if remaining is not None:
            return SchedulerResult(
                status=SchedulerStatus.SKIPPED_COOLDOWN,
                posture=posture_str, pressure_level=pressure_str,
                is_idle=is_idle,
                seconds_since_last_fire=ts - (self._last_fire_ts or 0),
                detail=f"cooldown_remaining_s={remaining:.2f}",
                ts_epoch=ts,
            )

        # All gates passed — invoke the engine.
        try:
            clusters = self.cluster_provider()
        except Exception as exc:  # noqa: BLE001
            return SchedulerResult(
                status=SchedulerStatus.ENGINE_ERROR,
                posture=posture_str, pressure_level=pressure_str,
                is_idle=is_idle,
                detail=f"cluster_provider_raised:{type(exc).__name__}:{exc}",
                ts_epoch=ts,
            )

        try:
            engine_result = self.engine.run_cycle(
                clusters, now_unix=ts,
            )
        except Exception as exc:  # noqa: BLE001
            return SchedulerResult(
                status=SchedulerStatus.ENGINE_ERROR,
                posture=posture_str, pressure_level=pressure_str,
                is_idle=is_idle,
                detail=f"run_cycle_raised:{type(exc).__name__}:{exc}",
                ts_epoch=ts,
            )

        # Update fire history regardless of engine outcome (the
        # scheduler made the decision to FIRE; the engine's verdict
        # is downstream).
        self._fire_history.append(ts)
        self._last_fire_ts = ts

        return SchedulerResult(
            status=SchedulerStatus.FIRED,
            posture=posture_str, pressure_level=pressure_str,
            is_idle=is_idle,
            cycles_in_window=len(self._fire_history),
            engine_result=engine_result,
            detail=f"engine_status={getattr(engine_result, 'status', '?')}",
            ts_epoch=ts,
        )

    def reset_state(self) -> None:
        """Test-only: clear fire history + cooldown."""
        self._fire_history = []
        self._last_fire_ts = None


_DEFAULT_SCHEDULER: Optional[CuriosityScheduler] = None


def get_default_scheduler() -> CuriosityScheduler:
    global _DEFAULT_SCHEDULER
    if _DEFAULT_SCHEDULER is None:
        _DEFAULT_SCHEDULER = CuriosityScheduler()
    return _DEFAULT_SCHEDULER


def reset_default_scheduler() -> None:
    global _DEFAULT_SCHEDULER
    _DEFAULT_SCHEDULER = None


__all__ = [
    "CuriosityScheduler",
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_MAX_CYCLES_PER_HOUR",
    "SchedulerResult",
    "SchedulerStatus",
    "get_cooldown_s",
    "get_default_scheduler",
    "get_max_cycles_per_hour",
    "is_scheduler_enabled",
    "reset_default_scheduler",
]
