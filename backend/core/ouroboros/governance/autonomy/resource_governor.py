"""Resource Governor — yields CPU/memory to user traffic when contention detected."""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("Ouroboros.ResourceGovernor")


@dataclass(frozen=True)
class ResourceGovernor:
    """Checks system resources and yields to user traffic when overloaded.

    Fail-open: if psutil fails or is unavailable, returns False (don't yield).
    """

    preempt_on_cpu_above: float = 80.0
    preempt_on_memory_above: float = 85.0

    async def should_yield(self) -> bool:
        """True if iteration should pause to let user traffic through."""
        try:
            import psutil

            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory().percent
            if cpu > self.preempt_on_cpu_above:
                logger.debug(
                    "ResourceGovernor: CPU %.1f%% > %.1f%%, yielding",
                    cpu,
                    self.preempt_on_cpu_above,
                )
                return True
            if mem > self.preempt_on_memory_above:
                logger.debug(
                    "ResourceGovernor: Memory %.1f%% > %.1f%%, yielding",
                    mem,
                    self.preempt_on_memory_above,
                )
                return True
            return False
        except Exception as exc:
            logger.debug(
                "ResourceGovernor: psutil error (%s), fail-open (not yielding)", exc
            )
            return False
