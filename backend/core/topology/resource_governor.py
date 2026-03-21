"""PIDController and ResourceGovernor for Sentinel CPU throttling."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PIDController:
    """Proportional-Integral-Derivative controller for resource throttling.

    Controls the Sentinel's concurrency level by measuring actual CPU
    utilization against a target. Anti-windup clamp prevents integral runaway.
    """
    target_cpu_fraction: float = 0.40
    Kp: float = 0.5
    Ki: float = 0.1
    Kd: float = 0.05
    min_concurrency: int = 1
    max_concurrency: int = 8

    _integral: float = field(default=0.0, init=False, repr=False)
    _prev_error: float = field(default=0.0, init=False, repr=False)
    _prev_time: float = field(default_factory=time.monotonic, init=False, repr=False)

    def update(self, measured_cpu_fraction: float) -> int:
        """Given current CPU utilization, returns the adjusted concurrency level."""
        now = time.monotonic()
        dt = max(now - self._prev_time, 0.001)
        error = self.target_cpu_fraction - measured_cpu_fraction

        self._integral += error * dt
        self._integral = max(-10.0, min(10.0, self._integral))

        derivative = (error - self._prev_error) / dt
        u = self.Kp * error + self.Ki * self._integral + self.Kd * derivative

        self._prev_error = error
        self._prev_time = now

        baseline = (self.min_concurrency + self.max_concurrency) // 2
        new_concurrency = baseline + int(round(u * baseline))
        return max(self.min_concurrency, min(self.max_concurrency, new_concurrency))


class ResourceGovernor:
    """Wraps the PID controller with a live measurement loop.

    Runs as a background asyncio task while the Sentinel is active.
    """

    # During the first BURST_WINDOW_S seconds, sample at BURST_INTERVAL_S
    # to catch pathological processes that allocate gigabytes in <1 second.
    # After the burst window, relax to the configured poll_interval.
    BURST_INTERVAL_S = 1.0
    BURST_WINDOW_S = 30.0

    def __init__(
        self,
        controller: PIDController,
        sentinel_semaphore: asyncio.Semaphore,
        poll_interval: float = 5.0,
    ) -> None:
        self._pid = controller
        self._sem = sentinel_semaphore
        self._poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._started_at: float = 0.0

    async def start(self) -> None:
        self._started_at = time.monotonic()
        self._task = asyncio.create_task(self._loop(), name="resource_governor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        import psutil
        while True:
            # Adaptive interval: fast sampling during burst window, relaxed after
            elapsed = time.monotonic() - self._started_at
            interval = self.BURST_INTERVAL_S if elapsed < self.BURST_WINDOW_S else self._poll_interval
            await asyncio.sleep(interval)
            cpu = psutil.cpu_percent(interval=None) / 100.0
            self._pid.update(cpu)
