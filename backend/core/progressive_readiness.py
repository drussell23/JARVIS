"""Progressive Readiness — adaptive, event-driven boot state management.

Instead of blocking the entire supervisor for 8-10 minutes while GCP Spot VMs
provision, JARVIS comes online in ~30s with local capabilities (Claude API,
WebSocket, voice) and progressively unlocks advanced tiers as cloud
infrastructure comes alive.

Readiness Tiers:
    BOOTING         — startup in progress, no user interaction
    ACTIVE_LOCAL    — Backend + Intelligence ready, Claude API fallback active.
                      User CAN issue QUERY commands. ACTION commands that need
                      Neural Mesh / J-Prime get a "warming up" response.
    ACTIVE_FULL     — Trinity connected (J-Prime + Reactor online).
                      Full Neural Mesh, Vision, browser automation available.
    FULLY_OPERATIONAL — Governance, agents, dashboard all online.
                      Graduation, proactive drive, self-programming active.

Usage:
    from backend.core.progressive_readiness import (
        get_readiness,
        ReadinessTier,
    )

    tier = get_readiness().tier
    if tier >= ReadinessTier.ACTIVE_LOCAL:
        # Claude API is available
    if tier >= ReadinessTier.ACTIVE_FULL:
        # J-Prime Neural Mesh is available
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ReadinessTier(IntEnum):
    """Progressive readiness levels — ordered by capability."""
    BOOTING = 0
    ACTIVE_LOCAL = 1       # Claude API + WebSocket + Voice
    ACTIVE_FULL = 2        # + J-Prime + Reactor + Neural Mesh
    FULLY_OPERATIONAL = 3  # + Governance + Dashboard + Graduation


class ProgressiveReadiness:
    """Singleton state manager for progressive boot readiness.

    Event-driven: components call ``advance()`` when they come online.
    Listeners receive callbacks on tier transitions (for narrator, dashboard, etc.).
    """

    def __init__(self) -> None:
        self._tier = ReadinessTier.BOOTING
        self._tier_timestamps: Dict[ReadinessTier, float] = {}
        self._listeners: List[Callable] = []
        self._boot_start = time.monotonic()
        self._details: Dict[str, str] = {}  # component → status

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

    def on_tier_change(self, callback: Callable) -> None:
        """Register a listener for tier transitions."""
        self._listeners.append(callback)

    async def advance(self, new_tier: ReadinessTier, reason: str = "") -> None:
        """Advance to a higher readiness tier (never regresses)."""
        if new_tier <= self._tier:
            return  # Never regress

        old = self._tier
        self._tier = new_tier
        self._tier_timestamps[new_tier] = time.monotonic()
        elapsed = self.elapsed_since_boot()

        logger.info(
            "[ProgressiveReadiness] %s -> %s (%.1fs into boot) %s",
            old.name, new_tier.name, elapsed, reason,
        )

        # Notify listeners (async-safe)
        for listener in self._listeners:
            try:
                result = listener(old, new_tier, reason)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug("[ProgressiveReadiness] Listener error: %s", exc)

    def set_detail(self, component: str, status: str) -> None:
        """Set a component's status detail (for dashboard/health)."""
        self._details[component] = status

    def health(self) -> Dict[str, Any]:
        """Return health snapshot."""
        return {
            "tier": self._tier.name,
            "tier_value": int(self._tier),
            "elapsed_s": self.elapsed_since_boot(),
            "tier_timestamps": {
                t.name: ts - self._boot_start
                for t, ts in self._tier_timestamps.items()
            },
            "details": dict(self._details),
        }

    def estimated_time_to_full(self) -> Optional[float]:
        """Estimate seconds until ACTIVE_FULL based on typical GCP boot time."""
        if self._tier >= ReadinessTier.ACTIVE_FULL:
            return 0.0
        # Typical GCP Spot VM with golden image: ~90s from ACTIVE_LOCAL
        elapsed_since_local = 0.0
        local_ts = self._tier_timestamps.get(ReadinessTier.ACTIVE_LOCAL)
        if local_ts:
            elapsed_since_local = time.monotonic() - local_ts
        typical_gcp_boot = 120.0  # conservative estimate
        remaining = max(0, typical_gcp_boot - elapsed_since_local)
        return remaining


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
