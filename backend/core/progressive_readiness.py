"""Progressive Readiness — authoritative boot state with parallel DAG tracking.

This is the SINGLE SOURCE OF TRUTH for system readiness. The frontend, voice
narrator, dashboard, and all routing decisions gate on this state.

The UI never lies: each DAG node reports its actual resolution status.
The tier advances ONLY when the required nodes have actually resolved.

Boot DAG (nodes can run in parallel where edges allow):

    clean_slate ──┬──> backend ──> intelligence ──> ACTIVE_LOCAL barrier
                  │                                      │
                  └──> resources ─────────────────────────┘
                                                         │
    gcp_vm_start (T=0) ─────────────> trinity ──> ACTIVE_FULL barrier
                                                         │
                                         governance ──> FULLY_OPERATIONAL barrier

Readiness Tiers (never regress):
    BOOTING           — startup in progress
    ACTIVE_LOCAL      — Backend + Intelligence resolved. Claude API fallback active.
    ACTIVE_FULL       — Trinity resolved. J-Prime + Reactor + Neural Mesh available.
    FULLY_OPERATIONAL — Governance + Dashboard resolved. Self-programming active.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class ReadinessTier(IntEnum):
    """Progressive readiness levels — ordered by capability."""
    BOOTING = 0
    ACTIVE_LOCAL = 1       # Claude API + WebSocket + Voice
    ACTIVE_FULL = 2        # + J-Prime + Reactor + Neural Mesh
    FULLY_OPERATIONAL = 3  # + Governance + Dashboard + Graduation


@dataclass
class DAGNode:
    """A single boot phase tracked in the parallel DAG."""
    name: str
    status: str = "pending"        # pending, running, resolved, failed, skipped
    started_at: float = 0.0
    resolved_at: float = 0.0
    error: str = ""
    detail: str = ""

    @property
    def elapsed_s(self) -> float:
        if self.resolved_at > 0 and self.started_at > 0:
            return self.resolved_at - self.started_at
        if self.started_at > 0:
            return time.monotonic() - self.started_at
        return 0.0


# Tier requirements: which DAG nodes must resolve for each tier
_TIER_REQUIREMENTS: Dict[ReadinessTier, Set[str]] = {
    ReadinessTier.ACTIVE_LOCAL: {"clean_slate", "backend", "intelligence"},
    ReadinessTier.ACTIVE_FULL: {"clean_slate", "backend", "intelligence", "trinity"},
    ReadinessTier.FULLY_OPERATIONAL: {
        "clean_slate", "backend", "intelligence", "trinity", "governance",
    },
}


class ProgressiveReadiness:
    """Authoritative boot state manager with parallel DAG tracking.

    The frontend subscribes to state changes via WebSocket. The UI progress
    bar and transitions are driven by actual DAG node resolution — no
    synthetic progress values.

    Thread-safe for reads. Mutations happen only from the main asyncio loop.
    """

    def __init__(self) -> None:
        self._tier = ReadinessTier.BOOTING
        self._tier_timestamps: Dict[ReadinessTier, float] = {}
        self._listeners: List[Callable] = []
        self._boot_start = time.monotonic()
        self._nodes: Dict[str, DAGNode] = {}
        self._ws_broadcast: Optional[Callable] = None  # wired by supervisor

    # -- Properties -----------------------------------------------------------

    @property
    def tier(self) -> ReadinessTier:
        return self._tier

    @property
    def is_local_ready(self) -> bool:
        return self._tier >= ReadinessTier.ACTIVE_LOCAL

    @property
    def is_full_ready(self) -> bool:
        return self._tier >= ReadinessTier.ACTIVE_FULL

    @property
    def is_fully_operational(self) -> bool:
        return self._tier >= ReadinessTier.FULLY_OPERATIONAL

    def elapsed_since_boot(self) -> float:
        return time.monotonic() - self._boot_start

    # -- DAG Node Tracking (Directive 2: State Integrity) ---------------------

    def mark_running(self, node_name: str, detail: str = "") -> None:
        """Mark a DAG node as running (started but not resolved)."""
        node = self._nodes.get(node_name)
        if node is None:
            node = DAGNode(name=node_name)
            self._nodes[node_name] = node
        node.status = "running"
        node.started_at = time.monotonic()
        node.detail = detail
        logger.info("[Readiness] DAG node '%s' RUNNING %s", node_name, detail)
        self._emit_state_sync()

    async def mark_resolved(self, node_name: str, detail: str = "") -> None:
        """Mark a DAG node as resolved and check tier advancement."""
        node = self._nodes.get(node_name)
        if node is None:
            node = DAGNode(name=node_name)
            self._nodes[node_name] = node
        node.status = "resolved"
        node.resolved_at = time.monotonic()
        node.detail = detail
        elapsed = node.elapsed_s
        logger.info(
            "[Readiness] DAG node '%s' RESOLVED (%.1fs) %s",
            node_name, elapsed, detail,
        )
        self._emit_state_sync()

        # Check if any tier requirements are now satisfied
        await self._evaluate_tier_advancement()

    async def mark_failed(self, node_name: str, error: str = "") -> None:
        """Mark a DAG node as failed."""
        node = self._nodes.get(node_name)
        if node is None:
            node = DAGNode(name=node_name)
            self._nodes[node_name] = node
        node.status = "failed"
        node.resolved_at = time.monotonic()
        node.error = error
        logger.warning("[Readiness] DAG node '%s' FAILED: %s", node_name, error)
        self._emit_state_sync()

        # Even failed nodes can unlock tiers (graceful degradation)
        await self._evaluate_tier_advancement()

    def mark_skipped(self, node_name: str, reason: str = "") -> None:
        """Mark a DAG node as skipped (counts as resolved for tier purposes)."""
        node = self._nodes.get(node_name)
        if node is None:
            node = DAGNode(name=node_name)
            self._nodes[node_name] = node
        node.status = "skipped"
        node.resolved_at = time.monotonic()
        node.detail = reason
        self._emit_state_sync()

    # -- Tier Advancement (Directive 2: Synchronization Barrier) ---------------

    async def _evaluate_tier_advancement(self) -> None:
        """Check if DAG resolution satisfies a higher tier's requirements.

        STRICT: Tier advances ONLY when ALL required nodes have actually
        resolved (status in {'resolved', 'failed', 'skipped'}).
        The UI never lies.
        """
        resolved_names = {
            name for name, node in self._nodes.items()
            if node.status in ("resolved", "failed", "skipped")
        }

        # Check tiers in order (can skip intermediate if requirements met)
        for candidate_tier in (
            ReadinessTier.ACTIVE_LOCAL,
            ReadinessTier.ACTIVE_FULL,
            ReadinessTier.FULLY_OPERATIONAL,
        ):
            if candidate_tier <= self._tier:
                continue  # Already at or past this tier
            required = _TIER_REQUIREMENTS.get(candidate_tier, set())
            if required.issubset(resolved_names):
                await self._advance_to(candidate_tier)

    async def _advance_to(self, new_tier: ReadinessTier) -> None:
        """Advance to a confirmed tier (requirements verified)."""
        old = self._tier
        self._tier = new_tier
        self._tier_timestamps[new_tier] = time.monotonic()
        elapsed = self.elapsed_since_boot()

        logger.info(
            "[Readiness] TIER ADVANCE: %s -> %s (%.1fs into boot)",
            old.name, new_tier.name, elapsed,
        )

        self._emit_state_sync()

        # Notify listeners (voice narrator, dashboard, etc.)
        for listener in self._listeners:
            try:
                result = listener(old, new_tier)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug("[Readiness] Listener error: %s", exc)

    # -- Listeners (Directive 3: Live UI Transparency) ------------------------

    def on_tier_change(self, callback: Callable) -> None:
        """Register a listener for tier transitions."""
        self._listeners.append(callback)

    def set_ws_broadcast(self, broadcast_fn: Callable) -> None:
        """Wire the WebSocket broadcast function for frontend state sync."""
        self._ws_broadcast = broadcast_fn

    def _emit_state_sync(self) -> None:
        """Broadcast current state to frontend via WebSocket (non-blocking)."""
        if self._ws_broadcast is None:
            return
        try:
            state = self.snapshot()
            self._ws_broadcast({
                "type": "readiness_state",
                "tier": state["tier"],
                "tier_value": state["tier_value"],
                "elapsed_s": state["elapsed_s"],
                "nodes": state["nodes"],
            })
        except Exception:
            pass  # Broadcasting is best-effort during boot

    # -- State Snapshot -------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return the full readiness state for health endpoints / frontend."""
        return {
            "tier": self._tier.name,
            "tier_value": int(self._tier),
            "elapsed_s": round(self.elapsed_since_boot(), 1),
            "tier_timestamps": {
                t.name: round(ts - self._boot_start, 1)
                for t, ts in self._tier_timestamps.items()
            },
            "nodes": {
                name: {
                    "status": node.status,
                    "elapsed_s": round(node.elapsed_s, 1),
                    "detail": node.detail,
                    "error": node.error,
                }
                for name, node in self._nodes.items()
            },
            "pending": [
                name for name, node in self._nodes.items()
                if node.status in ("pending", "running")
            ],
            "resolved": [
                name for name, node in self._nodes.items()
                if node.status in ("resolved", "skipped")
            ],
        }

    def estimated_time_to_full(self) -> Optional[float]:
        """Estimate seconds until ACTIVE_FULL based on Trinity resolution."""
        if self._tier >= ReadinessTier.ACTIVE_FULL:
            return 0.0
        trinity = self._nodes.get("trinity")
        if trinity and trinity.status == "running":
            # Typical golden image GCP VM: ~90-120s
            elapsed = trinity.elapsed_s
            return max(0, 120.0 - elapsed)
        return 120.0  # conservative default

    def health(self) -> Dict[str, Any]:
        return self.snapshot()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[ProgressiveReadiness] = None


def get_readiness() -> ProgressiveReadiness:
    """Get the singleton ProgressiveReadiness instance."""
    global _instance
    if _instance is None:
        _instance = ProgressiveReadiness()
    return _instance
