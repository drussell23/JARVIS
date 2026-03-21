"""PrimeMetricsPoller — fetches J-Prime queue metrics for LittlesLawVerifier.

Polls J-Prime's /v1/metrics endpoint at a configurable interval and feeds
queue_depth + avg_processing_latency_ms into the "prime" LittlesLawVerifier
so ProactiveDrive can include Prime's idle state in its Little's Law check.

Design:
    - Async background task, started/stopped by ProactiveDriveService
    - Uses persistent aiohttp session for connection reuse
    - Circuit breaker: after 5 failures, backs off to avoid hammering dead endpoint
    - All errors logged at DEBUG — never crashes the caller
    - Configurable via JARVIS_PRIME_URL env var (already set for PrimeClient)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PrimeMetricsPoller:
    """Polls J-Prime /v1/metrics and feeds LittlesLawVerifier for "prime" repo.

    Parameters
    ----------
    verifier:
        The "prime" LittlesLawVerifier instance from ProactiveDriveService.
    endpoint:
        Full URL to J-Prime metrics endpoint. Defaults from JARVIS_PRIME_URL.
    poll_interval_s:
        Seconds between polls. Default 15.
    max_failures:
        Circuit breaker threshold. Default 5.
    """

    def __init__(
        self,
        verifier: Any,
        endpoint: Optional[str] = None,
        poll_interval_s: float = 15.0,
        max_failures: int = 5,
    ) -> None:
        _base = endpoint or os.environ.get("JARVIS_PRIME_URL", "")
        self._endpoint = f"{_base.rstrip('/')}/v1/metrics" if _base else ""
        self._verifier = verifier
        self._poll_interval = poll_interval_s
        self._max_failures = max_failures
        self._consecutive_failures = 0
        self._circuit_open = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[Any] = None

    @property
    def is_enabled(self) -> bool:
        return bool(self._endpoint)

    async def start(self) -> None:
        if not self._endpoint:
            logger.info("[PrimeMetrics] No JARVIS_PRIME_URL — poller disabled")
            return
        self._task = asyncio.create_task(self._loop(), name="prime_metrics_poller")
        logger.info("[PrimeMetrics] Started: endpoint=%s interval=%ss", self._endpoint, self._poll_interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _loop(self) -> None:
        import aiohttp

        while True:
            await asyncio.sleep(self._poll_interval)

            if self._circuit_open:
                # Back off: try once every 60s when circuit is open
                await asyncio.sleep(45.0)
                self._circuit_open = False
                self._consecutive_failures = 0

            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=5.0),
                    )

                async with self._session.get(self._endpoint) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.json()

                # Feed verifier
                queue_depth = int(data.get("queue_depth", 0))
                latency_ms = float(data.get("avg_processing_latency_ms", 0.0))
                self._verifier.record(depth=queue_depth, processing_latency_ms=latency_ms)
                self._consecutive_failures = 0

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_failures:
                    self._circuit_open = True
                    logger.warning(
                        "[PrimeMetrics] Circuit breaker OPEN after %d failures: %s",
                        self._consecutive_failures, exc,
                    )
                else:
                    logger.debug("[PrimeMetrics] Poll failed (%d/%d): %s",
                                 self._consecutive_failures, self._max_failures, exc)
