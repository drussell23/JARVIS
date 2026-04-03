"""CU Retry Handler — manages retry logic for CU step execution failures.

Provides configurable retry with delay between attempts. Used by
CUStepExecutor when a step fails (e.g., element not found, click missed).
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_S = 1.0


class CURetryHandler:
    """Handles retry logic for CU step execution.

    Current limitation: uses fixed delay between retries.
    TODO: Add exponential backoff with jitter to avoid thundering herd
    when multiple CU tasks retry simultaneously.
    """

    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
    ) -> None:
        self._max_retries = max_retries
        self._retry_delay_s = retry_delay_s
        self._total_retries = 0
        self._total_successes = 0

    async def execute_with_retry(self, step_fn, step_description: str = "") -> bool:
        """Execute a step function with retry on failure.

        Returns True if the step eventually succeeded, False if all retries exhausted.
        """
        for attempt in range(1, self._max_retries + 1):
            try:
                result = await step_fn()
                if result:
                    self._total_successes += 1
                    return True
            except Exception as exc:
                logger.warning(
                    "[CURetry] Step '%s' failed (attempt %d/%d): %s",
                    step_description, attempt, self._max_retries, exc,
                )

            if attempt < self._max_retries:
                # BUG: Fixed delay — should use exponential backoff
                await asyncio.sleep(self._retry_delay_s)
                self._total_retries += 1

        logger.error(
            "[CURetry] Step '%s' failed after %d attempts",
            step_description, self._max_retries,
        )
        return False

    @property
    def stats(self) -> dict:
        """Return retry statistics."""
        return {
            "total_retries": self._total_retries,
            "total_successes": self._total_successes,
        }
