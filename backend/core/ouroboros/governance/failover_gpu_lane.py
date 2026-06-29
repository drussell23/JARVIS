"""failover_gpu_lane.py -- the Elastic Multi-Tier Fleet's GPU Escalation Lane.

During a SUSTAINED DW outage the 7B CPU survival node is the always-on baseline.
This lane manages a SECOND, elastic GPU node that is provisioned on demand for a
high-priority / token-overflowing op and reaped the instant its work drains:

    IDLE --request(needs GPU)--> PROVISIONING --ready--> ACTIVE
    ACTIVE --in-flight drains to 0--> REAPING --> IDLE

Routing is the return value of :meth:`request`: a GPU endpoint string means "send
this op to the 32B node"; ``None`` means "the 7B survival node handles it". The
lane never double-provisions (a single node serves all concurrent GPU ops) and
reaps to STOP BILLING the moment the last GPU op completes.

Boundaries (provision/reap/ready) are injected -> zero GCP coupling, fully
testable. Cost-safe: escalation requires the quality-tier master gate ON (via
``resolve_tier_for_op``) AND a confirmed sustained outage. Fail-soft throughout.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Awaitable, Callable, Optional, Set

from backend.core.ouroboros.governance.failover_tier import resolve_tier_for_op

logger = logging.getLogger(__name__)


class GpuLaneState(str, enum.Enum):
    IDLE = "idle"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    REAPING = "reaping"


class GpuEscalationLane:
    """Elastic GPU sub-fleet manager. One instance per failover controller."""

    def __init__(
        self,
        *,
        provision_fn: Callable[[], Awaitable[Optional[str]]],
        reap_fn: Callable[[], Awaitable[None]],
        ready_fn: Optional[Callable[[str], Awaitable[bool]]] = None,
        outage_confirmed_fn: Optional[Callable[[], bool]] = None,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provision_fn = provision_fn
        self._reap_fn = reap_fn
        self._ready_fn = ready_fn
        self._outage_confirmed_fn = outage_confirmed_fn
        self._clock = clock_fn
        self._state = GpuLaneState.IDLE
        self._endpoint: Optional[str] = None
        self._inflight: Set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def is_gpu_active(self) -> bool:
        return self._state == GpuLaneState.ACTIVE

    def gpu_inflight_count(self) -> int:
        return len(self._inflight)

    @property
    def endpoint(self) -> Optional[str]:
        return self._endpoint if self._state == GpuLaneState.ACTIVE else None

    # ------------------------------------------------------------------
    # Demand-driven escalation
    # ------------------------------------------------------------------
    async def request(
        self,
        op_id: str,
        *,
        urgency: str = "",
        complexity: str = "",
        estimated_tokens: int = 0,
    ) -> Optional[str]:
        """Route one op. Returns the GPU endpoint if this op is escalated to the
        32B node (provisioning it on first demand), or ``None`` to route the op to
        the 7B survival node. NEVER raises -> ``None`` (safe fallback to 7B)."""
        try:
            tier = resolve_tier_for_op(
                urgency=urgency, complexity=complexity, estimated_tokens=estimated_tokens,
            )
            if not tier.is_gpu:
                return None  # 7B survival node handles it
            # Sustained-outage gate: never spin a GPU for a transient blip.
            if self._outage_confirmed_fn is not None and not self._outage_confirmed_fn():
                return None
            async with self._lock:
                if self._state == GpuLaneState.IDLE:
                    self._state = GpuLaneState.PROVISIONING
                    ep = await self._provision_fn()
                    if not ep:
                        self._state = GpuLaneState.IDLE
                        logger.warning("[GpuLane] provision returned no endpoint -> 7B")
                        return None
                    if self._ready_fn is not None and not await self._ready_fn(ep):
                        logger.warning("[GpuLane] node never became ready -> reap + 7B")
                        await self._reap_locked()
                        return None
                    self._endpoint = ep
                    self._state = GpuLaneState.ACTIVE
                    logger.info("[GpuLane] GPU node ACTIVE endpoint=%s", ep)
                if self._state != GpuLaneState.ACTIVE:
                    return None  # mid-transition -> 7B this round
                self._inflight.add(op_id)
                return self._endpoint
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GpuLane] request fail-soft err=%r -> 7B", exc)
            return None

    async def complete(self, op_id: str) -> None:
        """Mark a GPU op done. When the LAST GPU op drains, reap the node to stop
        billing. NEVER raises."""
        try:
            async with self._lock:
                self._inflight.discard(op_id)
                if not self._inflight and self._state == GpuLaneState.ACTIVE:
                    self._state = GpuLaneState.REAPING
                    logger.info("[GpuLane] GPU in-flight drained to 0 -> reaping")
                    await self._reap_locked()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GpuLane] complete fail-soft err=%r", exc)

    async def drain_and_reap(self) -> None:
        """Force-reap the GPU node regardless of in-flight (handback / shutdown).
        NEVER raises."""
        try:
            async with self._lock:
                if self._state in (GpuLaneState.ACTIVE, GpuLaneState.PROVISIONING):
                    self._state = GpuLaneState.REAPING
                    await self._reap_locked()
                self._inflight.clear()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GpuLane] drain_and_reap fail-soft err=%r", exc)

    # ------------------------------------------------------------------
    async def _reap_locked(self) -> None:
        """Reap the GPU node (caller holds the lock). Always returns to IDLE."""
        try:
            await self._reap_fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[GpuLane] reap_fn err=%r (node may orphan)", exc)
        finally:
            self._endpoint = None
            self._state = GpuLaneState.IDLE


__all__ = ["GpuEscalationLane", "GpuLaneState"]
