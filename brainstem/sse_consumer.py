"""SSE Consumer — connects to Vercel's per-device event stream.

Uses urllib (system SSL stack) instead of aiohttp for the HTTP connection,
because aiohttp's TLS handshake hangs on macOS when the brainstem is
launched as a subprocess from Xcode. The long-lived SSE stream is read
in a background thread via asyncio.to_thread().
"""
import asyncio
import json
import logging
import urllib.request
from typing import Any, Callable, Coroutine, Dict, Optional

from brainstem.auth import BrainstemAuth
from brainstem.config import BrainstemConfig

logger = logging.getLogger("jarvis.brainstem.sse")

_CONNECT_TIMEOUT_S = 15


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


class SSEConsumer:
    def __init__(
        self,
        config: BrainstemConfig,
        auth: BrainstemAuth,
        on_event: Optional[EventHandler] = None,
    ) -> None:
        self.config = config
        self.auth = auth
        self.on_event = on_event
        self._last_event_id: Optional[str] = None
        self._consecutive_failures: int = 0
        self._stop_flag = False

    async def run(self, shutdown: asyncio.Event) -> None:
        logger.info("[SSE] Consumer starting (target=%s)", self.config.vercel_url)
        try:
            while not shutdown.is_set():
                try:
                    await self._connect_and_consume(shutdown)
                    self._consecutive_failures = 0
                except asyncio.CancelledError:
                    break
                except Exception as e:
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
            self._stop_flag = True

    async def _connect_and_consume(self, shutdown: asyncio.Event) -> None:
        logger.info("[SSE] Requesting stream token from %s...", self.config.vercel_url)
        token = await self.auth.get_stream_token(None, self.config.vercel_url)
        url = f"{self.config.vercel_url}/api/stream/{self.config.device_id}?t={token}"
        logger.info("[SSE] Connecting to %s", url.split("?")[0])

        headers = {}
        if self._last_event_id:
            headers["Last-Event-ID"] = self._last_event_id

        # Read SSE stream in a background thread (urllib uses system SSL, no hangs).
        # Events are pushed to an asyncio queue for dispatch on the event loop.
        queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()

        def _stream_reader() -> None:
            """Blocking SSE reader — runs in thread pool."""
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT_S) as resp:
                    if resp.status == 401:
                        raise RuntimeError("401 Unauthorized")
                    logger.info("[SSE] Connected (status=%d)", resp.status)

                    buffer = ""
                    while not self._stop_flag:
                        try:
                            chunk = resp.read(4096)
                        except TimeoutError:
                            continue
                        if not chunk:
                            break  # connection closed
                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n\n" in buffer:
                            event_block, buffer = buffer.split("\n\n", 1)
                            parsed = parse_sse_block(event_block)
                            if parsed is not None:
                                asyncio.get_event_loop().call_soon_threadsafe(
                                    queue.put_nowait, parsed,
                                )
            except Exception as exc:
                logger.warning("[SSE] Stream reader error: %s: %s", type(exc).__name__, exc)
            finally:
                asyncio.get_event_loop().call_soon_threadsafe(queue.put_nowait, None)

        # Start reader thread
        reader_task = asyncio.get_event_loop().run_in_executor(None, _stream_reader)

        # Token refresh loop
        refresh_task = asyncio.create_task(self._token_refresh_loop(shutdown))

        try:
            while not shutdown.is_set():
                parsed = await asyncio.wait_for(queue.get(), timeout=30)
                if parsed is None:
                    # Reader thread ended — reconnect
                    logger.info("[SSE] Stream ended, will reconnect")
                    break

                if parsed.get("event_id"):
                    self._last_event_id = parsed["event_id"]
                if parsed["event_type"] == "heartbeat":
                    continue
                if self.on_event:
                    await self.on_event(parsed["event_type"], parsed["data"])
        except asyncio.TimeoutError:
            logger.warning("[SSE] No data for 30s — reconnecting")
        finally:
            self._stop_flag = True
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            try:
                await reader_task
            except Exception:
                pass
            self._stop_flag = False

    async def _token_refresh_loop(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            await asyncio.sleep(self.config.token_refresh_s)
            try:
                await self.auth.refresh_stream_token(None, self.config.vercel_url)
                logger.debug("[SSE] Token refreshed")
            except Exception as e:
                logger.warning("[SSE] Token refresh failed: %s", e)
