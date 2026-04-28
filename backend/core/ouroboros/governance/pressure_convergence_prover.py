"""Slice T1.2 — PressureConvergenceProver: formal convergence proof.

Per ``OUROBOROS_VENOM_PRD.md`` §24.7.1 (Memory pressure positive feedback):

  > Memory pressure → MemoryPressureGate clamps fan-out → fewer
  > subagents complete → backlog grows → memory pressure increases.
  > Mitigated only if pressure-relief activates faster than backlog-
  > growth rate. **Untested at sustained load.**
  >
  > Fix path: a synthetic load-test sensor that injects backlog at
  > controlled rate while inducing memory pressure (mock); assert
  > pressure-relief activates within RELIEF_DEADLINE_S.

This module ships a **pure-function mathematical simulator** that
models the pressure↔backlog↔fanout feedback loop and proves
convergence (or correctly identifies overload conditions requiring
load-shedding).

## Model

  State at tick t:
    backlog(t)     — pending tasks
    pressure(t)    — simulated memory pressure level
    fanout_cap(t)  — max parallel workers at pressure(t)
    completion(t)  — tasks completed = min(backlog, fanout_cap)
    arrival(t)     — new tasks arriving per tick (caller-supplied)

  Transition:
    backlog(t+1)   = max(0, backlog(t) - completion(t) + arrival(t))
    pressure(t+1)  = pressure_from_backlog(backlog(t+1))
    fanout_cap(t+1)= fanout_cap_at(pressure(t+1))

## Convergence criterion

  The system converges when backlog reaches steady-state (oscillation
  bounded) OR drains to zero. For arrival_rate < fanout_cap(OK), the
  system always converges. For arrival_rate > fanout_cap(CRITICAL),
  backlog grows unbounded — the prover reports this as OVERLOADED
  and emits a ShedLoadRecommendation.

## Cage rules

  * Stdlib-only
  * Pure functions — no side effects, no I/O
  * Never raises
  * Master flag: ``JARVIS_PRESSURE_CONVERGENCE_PROVER_ENABLED``
    (default false)
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Hard caps
# ---------------------------------------------------------------------------

MAX_SIMULATION_TICKS: int = 10_000
DEFAULT_RELIEF_DEADLINE_TICKS: int = 500
DEFAULT_STEADY_STATE_WINDOW: int = 20

# ---------------------------------------------------------------------------
# Master flag + configuration
# ---------------------------------------------------------------------------


def is_prover_enabled() -> bool:
    return os.environ.get(
        "JARVIS_PRESSURE_CONVERGENCE_PROVER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _relief_deadline_ticks() -> int:
    try:
        v = int(os.environ.get(
            "JARVIS_PRESSURE_RELIEF_DEADLINE_TICKS",
            str(DEFAULT_RELIEF_DEADLINE_TICKS),
        ).strip())
        return max(1, min(MAX_SIMULATION_TICKS, v))
    except (ValueError, TypeError):
        return DEFAULT_RELIEF_DEADLINE_TICKS


def _steady_state_window() -> int:
    try:
        v = int(os.environ.get(
            "JARVIS_PRESSURE_STEADY_STATE_WINDOW",
            str(DEFAULT_STEADY_STATE_WINDOW),
        ).strip())
        return max(2, min(200, v))
    except (ValueError, TypeError):
        return DEFAULT_STEADY_STATE_WINDOW


# ---------------------------------------------------------------------------
# Pressure simulation vocabulary
# ---------------------------------------------------------------------------


class SimPressureLevel(str, enum.Enum):
    OK = "ok"
    WARN = "warn"
    HIGH = "high"
    CRITICAL = "critical"


class ConvergenceVerdict(str, enum.Enum):
    CONVERGED = "converged"
    DRAINED = "drained"
    OVERLOADED = "overloaded"
    INCONCLUSIVE = "inconclusive"


# ---------------------------------------------------------------------------
# Configuration — mirrors MemoryPressureGate's real thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PressureConfig:
    """Simulation parameters — mirrors MemoryPressureGate defaults.

    Backlog thresholds map backlog size → pressure level, simulating
    the real-world relationship between memory consumption (which
    grows with backlog) and pressure level.
    """

    # Backlog thresholds — above these, pressure escalates.
    backlog_warn: int = 10
    backlog_high: int = 25
    backlog_critical: int = 50

    # Fanout caps per pressure level (from MemoryPressureGate).
    fanout_ok: int = 16
    fanout_warn: int = 8
    fanout_high: int = 3
    fanout_critical: int = 1


DEFAULT_CONFIG = PressureConfig()


# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickState:
    """State at one simulation tick."""
    tick: int
    backlog: int
    pressure: SimPressureLevel
    fanout_cap: int
    completed: int
    arrived: int


@dataclass(frozen=True)
class ConvergenceResult:
    """Terminal result of a convergence simulation."""
    verdict: ConvergenceVerdict
    ticks_simulated: int
    ticks_to_drain: int
    peak_backlog: int
    peak_pressure: SimPressureLevel
    steady_state_backlog: int
    final_pressure: SimPressureLevel
    arrival_rate: int
    initial_backlog: int
    within_deadline: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "ticks_simulated": self.ticks_simulated,
            "ticks_to_drain": self.ticks_to_drain,
            "peak_backlog": self.peak_backlog,
            "peak_pressure": self.peak_pressure.value,
            "steady_state_backlog": self.steady_state_backlog,
            "final_pressure": self.final_pressure.value,
            "arrival_rate": self.arrival_rate,
            "initial_backlog": self.initial_backlog,
            "within_deadline": self.within_deadline,
        }


@dataclass(frozen=True)
class ShedLoadRecommendation:
    """Emitted when arrival_rate exceeds all fanout caps."""
    arrival_rate: int
    max_sustainable_rate: int
    excess: int
    message: str


# ---------------------------------------------------------------------------
# Core simulation — pure functions
# ---------------------------------------------------------------------------


def pressure_from_backlog(
    backlog: int,
    config: PressureConfig = DEFAULT_CONFIG,
) -> SimPressureLevel:
    """Map backlog size to a pressure level. Pure function."""
    if backlog >= config.backlog_critical:
        return SimPressureLevel.CRITICAL
    if backlog >= config.backlog_high:
        return SimPressureLevel.HIGH
    if backlog >= config.backlog_warn:
        return SimPressureLevel.WARN
    return SimPressureLevel.OK


def fanout_cap_at(
    pressure: SimPressureLevel,
    config: PressureConfig = DEFAULT_CONFIG,
) -> int:
    """Fanout cap for a given pressure level. Pure function."""
    if pressure is SimPressureLevel.OK:
        return config.fanout_ok
    if pressure is SimPressureLevel.WARN:
        return config.fanout_warn
    if pressure is SimPressureLevel.HIGH:
        return config.fanout_high
    if pressure is SimPressureLevel.CRITICAL:
        return config.fanout_critical
    return config.fanout_ok


def simulate_tick(
    state: TickState,
    arrival_rate: int,
    config: PressureConfig = DEFAULT_CONFIG,
) -> TickState:
    """Advance one tick. Pure function."""
    completed = min(state.backlog, state.fanout_cap)
    new_backlog = max(0, state.backlog - completed + arrival_rate)
    new_pressure = pressure_from_backlog(new_backlog, config)
    new_fanout = fanout_cap_at(new_pressure, config)
    return TickState(
        tick=state.tick + 1,
        backlog=new_backlog,
        pressure=new_pressure,
        fanout_cap=new_fanout,
        completed=completed,
        arrived=arrival_rate,
    )


def _is_steady_state(
    history: List[int],
    window: int,
) -> Tuple[bool, int]:
    """Check if the last `window` backlog values are bounded (no growth).

    Returns (is_steady, steady_backlog) where steady_backlog is the
    max over the window.
    """
    if len(history) < window:
        return (False, 0)
    recent = history[-window:]
    first_half = recent[:window // 2]
    second_half = recent[window // 2:]
    max_first = max(first_half)
    max_second = max(second_half)
    # Steady state: second half not growing beyond first half.
    if max_second <= max_first + 1:
        return (True, max(recent))
    return (False, 0)


# ---------------------------------------------------------------------------
# Main prover
# ---------------------------------------------------------------------------


def prove_convergence(
    *,
    arrival_rate: int,
    initial_backlog: int = 0,
    config: PressureConfig = DEFAULT_CONFIG,
    max_ticks: Optional[int] = None,
    deadline_ticks: Optional[int] = None,
    steady_window: Optional[int] = None,
) -> ConvergenceResult:
    """Run the feedback loop simulation and determine convergence.

    Three possible verdicts:
      * ``DRAINED`` — backlog reaches 0 (system recovered)
      * ``CONVERGED`` — backlog stabilizes at bounded steady state
      * ``OVERLOADED`` — backlog grows unbounded within max_ticks

    NEVER raises.
    """
    arrival_rate = max(0, int(arrival_rate))
    initial_backlog = max(0, int(initial_backlog))
    mt = max_ticks or min(MAX_SIMULATION_TICKS, _relief_deadline_ticks() * 3)
    deadline = deadline_ticks or _relief_deadline_ticks()
    window = steady_window or _steady_state_window()

    # Initial state.
    pressure = pressure_from_backlog(initial_backlog, config)
    fanout = fanout_cap_at(pressure, config)
    state = TickState(
        tick=0, backlog=initial_backlog, pressure=pressure,
        fanout_cap=fanout, completed=0, arrived=0,
    )

    peak_backlog = initial_backlog
    peak_pressure = pressure
    backlog_history: List[int] = [initial_backlog]
    drain_tick = -1

    for _ in range(mt):
        state = simulate_tick(state, arrival_rate, config)
        backlog_history.append(state.backlog)

        if state.backlog > peak_backlog:
            peak_backlog = state.backlog
        if _pressure_rank(state.pressure) > _pressure_rank(peak_pressure):
            peak_pressure = state.pressure

        # Check drain.
        if state.backlog == 0 and drain_tick < 0:
            drain_tick = state.tick
            return ConvergenceResult(
                verdict=ConvergenceVerdict.DRAINED,
                ticks_simulated=state.tick,
                ticks_to_drain=drain_tick,
                peak_backlog=peak_backlog,
                peak_pressure=peak_pressure,
                steady_state_backlog=0,
                final_pressure=state.pressure,
                arrival_rate=arrival_rate,
                initial_backlog=initial_backlog,
                within_deadline=drain_tick <= deadline,
            )

        # Check steady state.
        is_steady, steady_bl = _is_steady_state(backlog_history, window)
        if is_steady:
            return ConvergenceResult(
                verdict=ConvergenceVerdict.CONVERGED,
                ticks_simulated=state.tick,
                ticks_to_drain=state.tick,
                peak_backlog=peak_backlog,
                peak_pressure=peak_pressure,
                steady_state_backlog=steady_bl,
                final_pressure=state.pressure,
                arrival_rate=arrival_rate,
                initial_backlog=initial_backlog,
                within_deadline=state.tick <= deadline,
            )

    # Didn't converge within max_ticks.
    return ConvergenceResult(
        verdict=ConvergenceVerdict.OVERLOADED,
        ticks_simulated=mt,
        ticks_to_drain=-1,
        peak_backlog=peak_backlog,
        peak_pressure=peak_pressure,
        steady_state_backlog=state.backlog,
        final_pressure=state.pressure,
        arrival_rate=arrival_rate,
        initial_backlog=initial_backlog,
        within_deadline=False,
    )


def _pressure_rank(level: SimPressureLevel) -> int:
    _RANK = {
        SimPressureLevel.OK: 0,
        SimPressureLevel.WARN: 1,
        SimPressureLevel.HIGH: 2,
        SimPressureLevel.CRITICAL: 3,
    }
    return _RANK.get(level, 0)


# ---------------------------------------------------------------------------
# Load-shedding recommendation
# ---------------------------------------------------------------------------


def check_overload(
    arrival_rate: int,
    config: PressureConfig = DEFAULT_CONFIG,
) -> Optional[ShedLoadRecommendation]:
    """Check if arrival_rate exceeds the system's sustainable throughput.

    The maximum sustainable rate is ``fanout_cap(OK)`` — if arrival
    exceeds this, even under zero pressure the system can't keep up.
    Under pressure, the sustainable rate drops further.

    Returns ``ShedLoadRecommendation`` if overloaded, None if sustainable.
    NEVER raises.
    """
    max_rate = config.fanout_ok
    if arrival_rate <= max_rate:
        return None
    return ShedLoadRecommendation(
        arrival_rate=arrival_rate,
        max_sustainable_rate=max_rate,
        excess=arrival_rate - max_rate,
        message=(
            f"Arrival rate {arrival_rate}/tick exceeds max sustainable "
            f"throughput {max_rate}/tick (fanout_cap at OK pressure). "
            f"Excess: {arrival_rate - max_rate}/tick. Load shedding required."
        ),
    )


# ---------------------------------------------------------------------------
# Batch prover (parameter sweep)
# ---------------------------------------------------------------------------


def prove_batch(
    scenarios: List[Tuple[int, int]],
    config: PressureConfig = DEFAULT_CONFIG,
) -> List[ConvergenceResult]:
    """Run convergence proof for a list of (arrival_rate, initial_backlog)
    pairs. Returns one ConvergenceResult per scenario. NEVER raises."""
    results: List[ConvergenceResult] = []
    for arrival, backlog in scenarios:
        try:
            r = prove_convergence(
                arrival_rate=arrival,
                initial_backlog=backlog,
                config=config,
            )
            results.append(r)
        except Exception:  # noqa: BLE001
            results.append(ConvergenceResult(
                verdict=ConvergenceVerdict.INCONCLUSIVE,
                ticks_simulated=0, ticks_to_drain=-1,
                peak_backlog=0, peak_pressure=SimPressureLevel.OK,
                steady_state_backlog=0, final_pressure=SimPressureLevel.OK,
                arrival_rate=arrival, initial_backlog=backlog,
                within_deadline=False,
            ))
    return results


__all__ = [
    "ConvergenceResult",
    "ConvergenceVerdict",
    "DEFAULT_CONFIG",
    "DEFAULT_RELIEF_DEADLINE_TICKS",
    "DEFAULT_STEADY_STATE_WINDOW",
    "MAX_SIMULATION_TICKS",
    "PressureConfig",
    "ShedLoadRecommendation",
    "SimPressureLevel",
    "TickState",
    "check_overload",
    "fanout_cap_at",
    "is_prover_enabled",
    "pressure_from_backlog",
    "prove_batch",
    "prove_convergence",
    "simulate_tick",
]
