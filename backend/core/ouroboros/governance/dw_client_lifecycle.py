"""Slice 39 — Transport-pool lifecycle coordinator.

Single-owner flush policy: decides WHEN to call
``provider.force_session_reset()`` (Task 3).  Composes the provider
method; contains no connector logic of its own.

Guards:
* Master env switch ``JARVIS_DW_TRANSPORT_FLUSH_ENABLED`` (default true).
* Cooldown window ``JARVIS_DW_TRANSPORT_FLUSH_COOLDOWN_S`` (default 60 s)
  prevents a storm of transport failures from thrashing the aiohttp pool.

``flush_transport_pool`` NEVER raises — provider exceptions are caught,
logged, and surfaced as a False return so callers can proceed without
defensive wrapping.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

logger = logging.getLogger("Ouroboros.ClientLifecycle")


# ---------------------------------------------------------------------------
# Stdlib-only env helpers (mirror preflight_probe.py convention)
# ---------------------------------------------------------------------------

def _envb(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# ClientLifecycleManager
# ---------------------------------------------------------------------------

class ClientLifecycleManager:
    """Decides when to flush the aiohttp transport pool.

    Parameters
    ----------
    now_fn:
        Monotonic clock callable.  Injectable for deterministic tests.
    cooldown_s:
        Minimum seconds between successive flushes.  When *None* the value
        is read from ``JARVIS_DW_TRANSPORT_FLUSH_COOLDOWN_S`` (default 60 s).
    """

    def __init__(
        self,
        *,
        now_fn: Callable[[], float] = time.monotonic,
        cooldown_s: Optional[float] = None,
    ) -> None:
        self._now = now_fn
        self._cooldown_s: float = (
            cooldown_s
            if cooldown_s is not None
            else _envf("JARVIS_DW_TRANSPORT_FLUSH_COOLDOWN_S", 60.0)
        )
        self._last_flush_at: Optional[float] = None

    async def flush_transport_pool(self, provider, *, reason: str) -> bool:
        """Flush the provider's transport pool if guards allow.

        Returns True when ``provider.force_session_reset()`` was called
        successfully, False in all other cases (disabled, cooldown, error).
        """
        if not _envb("JARVIS_DW_TRANSPORT_FLUSH_ENABLED", True):
            logger.info("flush skipped: disabled by env")
            return False

        now = self._now()
        if (
            self._last_flush_at is not None
            and (now - self._last_flush_at) < self._cooldown_s
        ):
            logger.info(
                "flush suppressed by cooldown (%.1fs < %.1fs) reason=%s",
                now - self._last_flush_at,
                self._cooldown_s,
                reason,
            )
            return False

        try:
            await provider.force_session_reset()
        except Exception:
            logger.warning(
                "flush_transport_pool: force_session_reset raised; reason=%s",
                reason,
                exc_info=True,
            )
            return False

        self._last_flush_at = now
        logger.warning("transport pool HARD-FLUSHED reason=%s", reason)
        return True
