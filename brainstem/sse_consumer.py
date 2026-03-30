"""SSE Consumer — connects to Vercel's per-device event stream."""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional

import aiohttp

from brainstem.auth import BrainstemAuth
from brainstem.config import BrainstemConfig

logger = logging.getLogger("jarvis.brainstem.sse")


def parse_sse_block(block: str) -> Optional[Dict[str, Any]]:
    if not block or not block.strip():
        return None
    event_type: Optional[str] = None
    event_id: Optional[str] = None
    data_lines: list[str] = []
    for line in block.split("\n"):
        if line.startswith("event:"):
            event_type = line[6:]
        elif line.startswith("id:"):
            event_id = line[3:]
        elif line.startswith("data:"):
            data_lines.append(line[5:])
    if not event_type:
        return None
    data_str = "\n".join(data_lines)
    try:
        data = json.loads(data_str) if data_str else {}
    except json.JSONDecodeError:
        logger.debug("[SSE] Malformed JSON: %s", data_str[:100])
        return {"event_type": event_type, "event_id": event_id, "data": {}}
    return {"event_type": event_type, "event_id": event_id, "data": data}


EventHandler = Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]


@dataclass
class SSEConsumer:
    config: BrainstemConfig
    auth: BrainstemAuth
    on_event: Optional[EventHandler] = None
    _last_event_id: Optional[str] = field(default=None, init=False)
    _session: Optional[aiohttp.ClientSession] = field(default=None, init=False)
    _consecutive_failures: int = field(default=0, init=False)

    async def run(self, shutdown: asyncio.Event) -> None:
        logger.info("[SSE] Consumer starting (target=%s)", self.config.vercel_url)
        self._session = aiohttp.ClientSession()
        try:
            while not shutdown.is_set():
                try:
                    await self._connect_and_consume(shutdown)
                    self._consecutive_failures = 0
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    # Catch ALL exceptions (including RuntimeError from failed token requests,
                    # aiohttp.ClientError, TimeoutError, etc.) so the consumer always retries
                    # and errors are always logged rather than silently swallowed.
                    self._consecutive_failures += 1
                    backoff = min(
                        self.config.reconnect_backoff_base * (2 ** self._consecutive_failures),
                        self.config.reconnect_backoff_max,
                    )
                    logger.warning(
                        "[SSE] Connection error (%s: %s), reconnecting in %.0fs...",
                        type(e).__name__, e, backoff,
                    )
                    await asyncio.sleep(backoff)
        finally:
            await self._session.close()

    async def _connect_and_consume(self, shutdown: asyncio.Event) -> None:
        assert self._session is not None
        logger.info("[SSE] Requesting stream token from %s...", self.config.vercel_url)
        token = await self.auth.get_stream_token(self._session, self.config.vercel_url)
        url = f"{self.config.vercel_url}/api/stream/{self.config.device_id}?t={token}"
        headers: Dict[str, str] = {}
        if self._last_event_id:
            headers["Last-Event-ID"] = self._last_event_id
        logger.info("[SSE] Connecting to %s", url.split("?")[0])
        connect_timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=None)
        async with self._session.get(url, headers=headers, timeout=connect_timeout) as resp:
            if resp.status == 401:
                raise aiohttp.ClientError("401 Unauthorized")
            resp.raise_for_status()
            logger.info("[SSE] Connected")
            self._consecutive_failures = 0
            refresh_task = asyncio.create_task(self._token_refresh_loop(shutdown))
            try:
                buffer = ""
                async for chunk_bytes in resp.content.iter_any():
                    if shutdown.is_set():
                        break
                    chunk = chunk_bytes.decode("utf-8")
                    buffer += chunk
                    while "\n\n" in buffer:
                        event_block, buffer = buffer.split("\n\n", 1)
                        parsed = parse_sse_block(event_block)
                        if parsed is None:
                            continue
                        if parsed.get("event_id"):
                            self._last_event_id = parsed["event_id"]
                        if parsed["event_type"] == "heartbeat":
                            continue
                        if self.on_event:
                            await self.on_event(parsed["event_type"], parsed["data"])
            finally:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

    async def _token_refresh_loop(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            await asyncio.sleep(self.config.token_refresh_s)
            try:
                assert self._session is not None
                await self.auth.refresh_stream_token(self._session, self.config.vercel_url)
                logger.debug("[SSE] Token refreshed")
            except Exception as e:
                logger.warning("[SSE] Token refresh failed: %s", e)
