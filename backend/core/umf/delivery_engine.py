"""UMF Delivery Engine -- pub/sub routing with dedup and contract validation.

Core message bus that validates, deduplicates, and dispatches UMF messages
to registered subscribers based on stream affinity.

Pipeline per ``publish()`` call
-------------------------------
1. **Contract gate** -- schema, TTL, deadline, capability hash.
2. **Dedup ledger** -- reserve idempotency key (SQLite WAL).
3. **Fan-out dispatch** -- invoke every handler registered for the stream.
4. **Commit** -- mark dedup entry as delivered.

Design rules
------------
* Stdlib + sibling UMF modules only.
* Handlers may be sync or async callables.
* All public methods are ``async``.
* Thread-safe subscriber registration via ``asyncio.Lock``.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.core.umf.contract_gate import validate_message
from backend.core.umf.dedup_ledger import SqliteDedupLedger
from backend.core.umf.types import ReserveResult, UmfMessage

# ── Types ─────────────────────────────────────────────────────────────

Handler = Callable[[UmfMessage], Any]


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a ``DeliveryEngine.publish()`` call.

    ``delivered`` is True when the message passed all gates and was
    dispatched to at least zero handlers (an empty subscriber list
    is still a successful delivery -- the message was accepted).
    ``reject_reason`` explains why delivery was refused, or None on success.
    """

    delivered: bool
    message_id: str
    reject_reason: Optional[str] = None


# ── Subscription record ──────────────────────────────────────────────


@dataclass
class _Subscription:
    """Internal record binding a handler to a stream."""

    sub_id: str
    stream: str
    handler: Handler


# ── DeliveryEngine ────────────────────────────────────────────────────


class DeliveryEngine:
    """Core UMF pub/sub router with contract validation and dedup.

    Parameters
    ----------
    dedup_db_path:
        Filesystem path for the SQLite dedup ledger database.
    expected_capability_hash:
        If provided, passed to the contract gate for capability
        compatibility checks on every inbound message.
    """

    def __init__(
        self,
        dedup_db_path: Path,
        expected_capability_hash: Optional[str] = None,
    ) -> None:
        self._ledger = SqliteDedupLedger(dedup_db_path)
        self._expected_capability_hash = expected_capability_hash
        self._subscribers: Dict[str, List[_Subscription]] = {}
        self._sub_lock = asyncio.Lock()
        self._running = False
        self._stats: Dict[str, int] = {
            "published": 0,
            "rejected": 0,
            "dispatched": 0,
        }

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the delivery engine and its dedup ledger."""
        await self._ledger.start()
        self._running = True

    async def stop(self) -> None:
        """Stop the delivery engine and its dedup ledger."""
        self._running = False
        await self._ledger.stop()

    # ── publish ───────────────────────────────────────────────────────

    async def publish(self, msg: UmfMessage) -> PublishResult:
        """Validate, dedup, and dispatch a UMF message.

        Returns
        -------
        PublishResult
            Always populated -- never raises on invalid messages.
        """
        # 1. Contract gate validation
        validation = validate_message(
            msg,
            expected_capability_hash=self._expected_capability_hash,
        )
        if not validation.accepted:
            self._stats["rejected"] += 1
            return PublishResult(
                delivered=False,
                message_id=msg.message_id,
                reject_reason=validation.reject_reason,
            )

        # 2. Dedup reservation
        reserve_result = await self._ledger.reserve(
            idempotency_key=msg.idempotency_key,
            message_id=msg.message_id,
            ttl_ms=msg.routing_ttl_ms,
        )
        if reserve_result is not ReserveResult.reserved:
            self._stats["rejected"] += 1
            return PublishResult(
                delivered=False,
                message_id=msg.message_id,
                reject_reason="dedup_duplicate",
            )

        # 3. Fan-out dispatch to stream subscribers
        stream_key = msg.stream.value if hasattr(msg.stream, "value") else str(msg.stream)
        handlers = self._subscribers.get(stream_key, [])
        for sub in handlers:
            if inspect.iscoroutinefunction(sub.handler):
                await sub.handler(msg)
            else:
                sub.handler(msg)
            self._stats["dispatched"] += 1

        # 4. Commit dedup entry
        await self._ledger.commit(msg.message_id, effect_hash="")

        self._stats["published"] += 1
        return PublishResult(
            delivered=True,
            message_id=msg.message_id,
        )

    # ── subscribe ─────────────────────────────────────────────────────

    async def subscribe(self, stream: str, handler: Handler) -> str:
        """Register a handler for messages on a given stream.

        Parameters
        ----------
        stream:
            The stream name (or ``Stream`` enum member) to subscribe to.
        handler:
            Callable that accepts a ``UmfMessage``.  May be sync or async.

        Returns
        -------
        str
            Unique subscription ID that can be used for future unsubscribe.
        """
        stream_key = stream.value if hasattr(stream, "value") else str(stream)
        sub_id = uuid.uuid4().hex
        sub = _Subscription(sub_id=sub_id, stream=stream_key, handler=handler)

        async with self._sub_lock:
            if stream_key not in self._subscribers:
                self._subscribers[stream_key] = []
            self._subscribers[stream_key].append(sub)

        return sub_id

    # ── health ────────────────────────────────────────────────────────

    async def health(self) -> Dict[str, Any]:
        """Return engine health status and cumulative stats.

        Returns
        -------
        dict
            Keys: ``running`` (bool), ``stats`` (dict of counters).
        """
        return {
            "running": self._running,
            "stats": dict(self._stats),
        }
