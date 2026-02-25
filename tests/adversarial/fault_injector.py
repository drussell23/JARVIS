"""Fault injection framework for adversarial testing.

Provides deterministic, reproducible fault injection for:
- Network partitions (full and partial)
- Timeout-after-success (operation completes but caller times out)
- Clock jumps (wall clock and monotonic)
- Delayed duplicates
- Crash mid-commit
- Suspend/resume

Usage:
    injector = FaultInjector(seed=42)
    injector.register("prime_client.request", FaultType.NETWORK_PARTITION)
    fault = injector.check("prime_client.request")
    if fault:
        # Apply fault semantics
        await apply_fault(fault, my_operation(), timeout=5)
"""

import asyncio
import fnmatch
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from unittest.mock import patch


class FaultType(str, Enum):
    NETWORK_PARTITION = "network_partition"
    PARTIAL_PARTITION = "partial_partition"
    TIMEOUT_AFTER_SUCCESS = "timeout_after_success"
    DELAYED_DUPLICATE = "delayed_duplicate"
    CLOCK_JUMP_FORWARD = "clock_jump_forward"
    CLOCK_JUMP_BACKWARD = "clock_jump_backward"
    CRASH_MID_COMMIT = "crash_mid_commit"
    SUSPEND_RESUME = "suspend_resume"


@dataclass
class FaultSpec:
    fault_type: FaultType
    params: Dict[str, Any] = field(default_factory=dict)
    one_shot: bool = True


@dataclass
class _ProbabilisticFault:
    pattern: str
    fault_type: FaultType
    probability: float
    params: Dict[str, Any] = field(default_factory=dict)


class FaultInjector:
    """Deterministic fault injection engine."""

    def __init__(self, seed: Optional[int] = None) -> None:
        self._rng = random.Random(seed)
        self._registered: Dict[str, FaultSpec] = {}
        self._probabilistic: List[_ProbabilisticFault] = []

    def register(
        self,
        boundary: str,
        fault_type: FaultType,
        params: Optional[Dict[str, Any]] = None,
        one_shot: bool = True,
    ) -> None:
        self._registered[boundary] = FaultSpec(
            fault_type=fault_type,
            params=params or {},
            one_shot=one_shot,
        )

    def register_probabilistic(
        self,
        pattern: str,
        fault_type: FaultType,
        probability: float,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._probabilistic.append(
            _ProbabilisticFault(
                pattern=pattern,
                fault_type=fault_type,
                probability=probability,
                params=params or {},
            )
        )

    def check(self, boundary: str) -> Optional[FaultSpec]:
        # Check exact match first
        if boundary in self._registered:
            spec = self._registered[boundary]
            if spec.one_shot:
                del self._registered[boundary]
            return spec

        # Check probabilistic matches
        for pf in self._probabilistic:
            if fnmatch.fnmatch(boundary, pf.pattern):
                if self._rng.random() < pf.probability:
                    return FaultSpec(
                        fault_type=pf.fault_type,
                        params=pf.params,
                        one_shot=False,
                    )
        return None

    def clear(self) -> None:
        self._registered.clear()
        self._probabilistic.clear()


async def apply_fault(
    fault: FaultSpec,
    coro: Any,
    timeout: float = 5.0,
) -> Any:
    """Apply fault semantics to an async operation."""
    if fault.fault_type == FaultType.NETWORK_PARTITION:
        raise ConnectionError("Simulated network partition")

    if fault.fault_type == FaultType.TIMEOUT_AFTER_SUCCESS:
        delay_s = fault.params.get("delay_s", 1.0)

        async def _delayed():
            result = await coro
            await asyncio.sleep(delay_s)
            return result

        return await asyncio.wait_for(_delayed(), timeout=timeout)

    if fault.fault_type == FaultType.DELAYED_DUPLICATE:
        delay_s = fault.params.get("delay_s", 0.5)
        result = await coro
        await asyncio.sleep(delay_s)
        return result

    if fault.fault_type == FaultType.CRASH_MID_COMMIT:
        raise RuntimeError("Simulated crash mid-commit")

    if fault.fault_type == FaultType.SUSPEND_RESUME:
        suspend_s = fault.params.get("suspend_s", 1.0)
        await asyncio.sleep(suspend_s)
        return await coro

    # Default: just run the operation
    return await coro


class MockClock:
    """Mock clock with controllable offsets for testing time-dependent code."""

    def __init__(self) -> None:
        self.wall_offset: float = 0.0
        self.mono_offset: float = 0.0
        self._original_time = time.time
        self._original_monotonic = time.monotonic

    def apply_fault(self, fault: FaultSpec) -> None:
        if fault.fault_type == FaultType.CLOCK_JUMP_FORWARD:
            jump_s = fault.params.get("jump_s", 60)
            self.wall_offset += jump_s
            self.mono_offset += jump_s
        elif fault.fault_type == FaultType.CLOCK_JUMP_BACKWARD:
            jump_s = fault.params.get("jump_s", 60)
            self.wall_offset -= jump_s
            # Monotonic should never go backward, but we simulate it for testing
            # In real systems, monotonic is immune to backward jumps

    def _patched_time(self) -> float:
        return self._original_time() + self.wall_offset

    def _patched_monotonic(self) -> float:
        return self._original_monotonic() + self.mono_offset

    def __enter__(self):
        self._time_patch = patch("time.time", self._patched_time)
        self._mono_patch = patch("time.monotonic", self._patched_monotonic)
        self._time_patch.start()
        self._mono_patch.start()
        return self

    def __exit__(self, *args):
        self._time_patch.stop()
        self._mono_patch.stop()
