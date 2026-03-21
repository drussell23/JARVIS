"""RemoteHTTPTransport — sends CommProtocol messages to J-Prime over HTTP.

Bridges local governance events (INTENT/PLAN/HEARTBEAT/DECISION/POSTMORTEM)
to the J-Prime cloud instance so both JARVIS and Prime have full operation
visibility. Follows the same async send(msg) interface as LogTransport.

Design:
    - Uses aiohttp persistent session for connection reuse
    - Fire-and-forget with timeout — never blocks the local pipeline
    - Circuit breaker: after N consecutive failures, stops attempting
      until a health check succeeds (prevents latency from dead endpoint)
    - All errors swallowed — remote transport failure never crashes local ops
    - Configurable via env vars, no hardcoding
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RemoteHTTPTransport:
    """CommProtocol transport that forwards messages to J-Prime via HTTP POST.

    Parameters
    ----------
    endpoint:
        Full URL of the J-Prime comm receiver (e.g., http://136.113.252.164:8000/v1/comm).
        Defaults to JARVIS_PRIME_COMM_ENDPOINT env var.
    timeout_s:
        Per-request timeout. Defaults to 5 seconds.
    max_consecutive_failures:
        Circuit breaker threshold. After this many failures, transport
        stops sending until reset. Defaults to 5.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        timeout_s: float = 5.0,
        max_consecutive_failures: int = 5,
    ) -> None:
        self._endpoint = endpoint or os.environ.get(
            "JARVIS_PRIME_COMM_ENDPOINT",
            "",  # empty = disabled
        )
        self._timeout_s = timeout_s
        self._max_failures = max_consecutive_failures
        self._consecutive_failures = 0
        self._circuit_open = False
        self._session: Optional[Any] = None  # aiohttp.ClientSession

    @property
    def is_enabled(self) -> bool:
        return bool(self._endpoint)

    @property
    def is_circuit_open(self) -> bool:
        return self._circuit_open

    async def send(self, msg: Any) -> None:
        """Forward a CommMessage to J-Prime. Never raises."""
        if not self._endpoint:
            return  # disabled — no endpoint configured

        if self._circuit_open:
            return  # circuit breaker tripped — skip silently

        try:
            payload = self._serialize(msg)
            await self._post(payload)
            self._consecutive_failures = 0  # reset on success
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures:
                self._circuit_open = True
                logger.warning(
                    "[RemoteHTTPTransport] Circuit breaker OPEN after %d failures: %s",
                    self._consecutive_failures, exc,
                )
            else:
                logger.debug(
                    "[RemoteHTTPTransport] Send failed (%d/%d): %s",
                    self._consecutive_failures, self._max_failures, exc,
                )

    async def reset_circuit(self) -> None:
        """Reset the circuit breaker. Called when J-Prime health check passes."""
        self._circuit_open = False
        self._consecutive_failures = 0
        logger.info("[RemoteHTTPTransport] Circuit breaker RESET")

    async def close(self) -> None:
        """Close the persistent aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _serialize(self, msg: Any) -> str:
        """Serialize CommMessage to JSON string."""
        try:
            return json.dumps(asdict(msg), default=str)
        except (TypeError, AttributeError):
            # Fallback: convert to dict manually
            return json.dumps({
                "msg_type": str(getattr(msg, "msg_type", "")),
                "op_id": str(getattr(msg, "op_id", "")),
                "seq": getattr(msg, "seq", 0),
                "payload": getattr(msg, "payload", {}),
                "timestamp": getattr(msg, "timestamp", time.time()),
            }, default=str)

    async def _post(self, payload: str) -> None:
        """HTTP POST with timeout. Creates session on first use."""
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            )

        async with self._session.post(
            self._endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(
                    f"J-Prime comm endpoint returned {resp.status}: {text[:200]}"
                )
