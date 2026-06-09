"""Slice 188 Phase 4 — singleflight (zero redundant vendor calls).

When multiple sub-agents concurrently request the SAME generation, they must all await ONE
underlying network race, not fire N identical DW calls. Maps a cryptographic hash of the payload
to an in-flight ``asyncio.Future``; the first caller does the work, the rest await its result.
Inspired by Go's ``singleflight``. NEVER leaks a failed future (cleared in ``finally``).
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Awaitable, Callable, Dict, Optional


def payload_key(*parts: Any) -> str:
    """Deterministic content-address of a request payload (sha256 of the joined parts)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


class Singleflight:
    """Deduplicate concurrent identical async calls. ``do(key, factory)`` runs ``factory`` once per
    in-flight key; concurrent callers with the same key await the same result. Off by default at
    the call site — enable per integration. NEVER raises out of bookkeeping."""

    def __init__(self) -> None:
        self._inflight: Dict[str, "asyncio.Future[Any]"] = {}
        self._lock = asyncio.Lock()

    def inflight_count(self) -> int:
        return len(self._inflight)

    async def do(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        # fast path: an existing in-flight future for this key → await it (shared result)
        async with self._lock:
            fut: Optional["asyncio.Future[Any]"] = self._inflight.get(key)
            is_leader = fut is None
            if is_leader:
                loop = asyncio.get_event_loop()
                fut = loop.create_future()
                self._inflight[key] = fut

        if not is_leader:
            # follower: await the leader's result WITHOUT cancelling it if I'm cancelled
            return await asyncio.shield(fut)

        # leader: do the work, publish the result, always clear the registry
        try:
            result = await factory()
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:  # noqa: BLE001 — propagate to followers then re-raise
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            async with self._lock:
                if self._inflight.get(key) is fut:
                    del self._inflight[key]
