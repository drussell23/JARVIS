from __future__ import annotations

import asyncio
import random
from typing import List, Optional

from aiohttp import ClientSession, WSMsgType, web


class HostileProxy:
    """WS proxy that degrades the link between a client and the real
    upstream server. Deterministic (seeded)."""

    def __init__(
        self,
        upstream_url: str,
        *,
        latency_s: float = 0.0,
        jitter_s: float = 0.0,
        reorder_window: int = 1,
        drop_after: Optional[int] = None,
        max_hold_s: float = 0.1,
        seed: int = 1234,
    ) -> None:
        self._upstream = upstream_url
        self._latency = latency_s
        self._jitter = jitter_s
        self._reorder_window = max(1, reorder_window)
        self._drop_after = drop_after
        # Max-hold for the reorder buffer. A real reorder/jitter buffer
        # shuffles frames within a window but releases a trailing partial
        # window after a bounded idle, rather than withholding it until the
        # connection closes. Without this a persistent (never-dropped)
        # connection whose replay tail is shorter than reorder_window would
        # hold the last frames FOREVER -- a phantom "drop the tail" fault
        # distinct from the four intended faults (latency, jitter, windowed
        # shuffle, mid-stream drop-close). It is deterministic: the idle
        # timeout (default 0.1s) is far larger than the live publish cadence
        # and sub-ms replay bursts, so it never splits a window mid-stream --
        # it only releases the trailing residual once the upstream is quiet.
        self._max_hold = max_hold_s
        self._rng = random.Random(seed)

    def register(self, app: web.Application, path: str) -> None:
        app.router.add_get(path, self._handle)

    async def _delay(self) -> None:
        d = self._latency + (self._rng.uniform(0, self._jitter) if self._jitter else 0.0)
        if d > 0:
            await asyncio.sleep(d)

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        downstream = web.WebSocketResponse(heartbeat=None)
        await downstream.prepare(request)
        session = ClientSession()
        delivered = 0
        buf: List[bytes] = []
        try:
            async with session.ws_connect(self._upstream, heartbeat=None) as upstream:
                async def c2u() -> None:
                    async for msg in downstream:
                        if msg.type in (WSMsgType.BINARY, WSMsgType.TEXT):
                            await upstream.send_bytes(
                                msg.data if isinstance(msg.data, bytes) else msg.data.encode()
                            )
                        else:
                            break

                async def _flush() -> bool:
                    """Shuffle + deliver the current buffer. Returns False
                    iff the drop fault fired (connection closed)."""
                    nonlocal delivered
                    self._rng.shuffle(buf)
                    while buf:
                        if self._drop_after is not None and delivered >= self._drop_after:
                            await downstream.close()
                            return False
                        frame = buf.pop(0)
                        await downstream.send_bytes(frame)
                        delivered += 1
                    return True

                async def u2c() -> None:
                    while True:
                        try:
                            msg = await asyncio.wait_for(
                                upstream.receive(), timeout=self._max_hold
                            )
                        except asyncio.TimeoutError:
                            # Upstream idle: release the trailing partial window
                            # (bounded max-hold) so it is never withheld forever.
                            if buf and not await _flush():
                                return
                            continue
                        if msg.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
                            break
                        data = msg.data if isinstance(msg.data, bytes) else msg.data.encode()
                        await self._delay()
                        buf.append(data)
                        if len(buf) >= self._reorder_window and not await _flush():
                            return

                await asyncio.gather(c2u(), u2c(), return_exceptions=True)
                # Flush any remaining buffered frames unless we already dropped.
                for frame in buf:
                    if self._drop_after is not None and delivered >= self._drop_after:
                        break
                    await downstream.send_bytes(frame)
                    delivered += 1
        finally:
            await session.close()
            if not downstream.closed:
                await downstream.close()
        return downstream
