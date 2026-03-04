"""
Cost-Aware Cloud SQL Proxy Lifecycle.

In solo developer mode, starts the Cloud SQL proxy on-demand (not via launchd)
and stops it after a configurable idle timeout. Wraps ProxyLifecycleController
to add cost-sensitive behavior.

Saves ~$9/month by avoiding persistent proxy uptime when not needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class CostAwareProxyLifecycle:
    """On-demand Cloud SQL proxy lifecycle for cost-sensitive modes.

    When JARVIS_SOLO_DEVELOPER_MODE=true (default), proxy starts only when
    DB access is requested and stops after idle timeout.
    """

    def __init__(self):
        self._solo_mode = os.getenv("JARVIS_SOLO_DEVELOPER_MODE", "true").lower() == "true"
        self._idle_timeout_s = float(os.getenv("JARVIS_CLOUD_SQL_IDLE_TIMEOUT_S", "120"))
        self._last_access_time: float = 0.0
        self._proxy_running = False
        self._idle_monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._inner_controller = None  # Lazy-init ProxyLifecycleController

    def _get_inner(self):
        """Lazy-init the inner ProxyLifecycleController."""
        if self._inner_controller is None:
            from backend.core.proxy.lifecycle_controller import ProxyLifecycleController
            self._inner_controller = ProxyLifecycleController()
            # Disable launchd in cost-aware mode to prevent auto-restart
            if self._solo_mode:
                self._inner_controller._use_launchd = False
        return self._inner_controller

    async def ensure_proxy_running(self) -> bool:
        """Start proxy if not running. Records access time for idle tracking.

        Returns True if proxy is ready.
        """
        async with self._lock:
            self._last_access_time = time.monotonic()

            if self._proxy_running:
                return True

            # Budget check before starting (fail-closed by default)
            #
            # Policy: JARVIS_PROXY_BUDGET_BYPASS=true is the explicit break-glass
            # override. Without it, if the cost tracker is unavailable or errors,
            # proxy start is BLOCKED to enforce single budget authority.
            _budget_bypass = os.getenv("JARVIS_PROXY_BUDGET_BYPASS", "false").lower() == "true"
            try:
                from backend.core.cost_tracker import get_cost_tracker
                ct = get_cost_tracker()
                allowed, reason = await ct.can_spend(
                    ct.config.cloud_sql_hourly_cost, "cloud_sql"
                )
                if not allowed:
                    logger.warning("[CostAwareProxy] Budget gate blocked proxy start: %s", reason)
                    return False
            except Exception as budget_err:
                if _budget_bypass:
                    logger.warning(
                        "[CostAwareProxy] Budget check failed (%s) — "
                        "JARVIS_PROXY_BUDGET_BYPASS=true, allowing start",
                        budget_err,
                    )
                else:
                    logger.error(
                        "[CostAwareProxy] Budget check failed (%s) — "
                        "proxy start BLOCKED (set JARVIS_PROXY_BUDGET_BYPASS=true to override)",
                        budget_err,
                    )
                    return False

            controller = self._get_inner()
            success = await controller.start()
            if success:
                self._proxy_running = True
                logger.info("[CostAwareProxy] Proxy started on-demand")
                # Start idle monitor
                if self._idle_monitor_task is None or self._idle_monitor_task.done():
                    self._idle_monitor_task = asyncio.create_task(self._idle_monitor())
            return success

    async def stop_proxy(self) -> bool:
        """Stop the proxy and cancel idle monitor."""
        async with self._lock:
            if not self._proxy_running:
                return True

            controller = self._get_inner()
            success = await controller.stop()
            self._proxy_running = False
            logger.info("[CostAwareProxy] Proxy stopped")
            return success

    async def _idle_monitor(self):
        """Background task that stops proxy after idle timeout."""
        try:
            while self._proxy_running:
                await asyncio.sleep(30)  # Check every 30s
                idle_s = time.monotonic() - self._last_access_time
                if idle_s >= self._idle_timeout_s:
                    logger.info(
                        "[CostAwareProxy] Proxy idle for %.0fs (limit %.0fs) — stopping",
                        idle_s, self._idle_timeout_s,
                    )
                    await self.stop_proxy()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[CostAwareProxy] Idle monitor error: %s", e)

    def record_access(self):
        """Record a DB access to reset idle timer."""
        self._last_access_time = time.monotonic()

    @property
    def is_running(self) -> bool:
        return self._proxy_running

    @property
    def is_cost_aware_mode(self) -> bool:
        return self._solo_mode


# Module-level singleton
_instance: Optional[CostAwareProxyLifecycle] = None


def get_cost_aware_proxy() -> CostAwareProxyLifecycle:
    """Get or create the cost-aware proxy singleton."""
    global _instance
    if _instance is None:
        _instance = CostAwareProxyLifecycle()
    return _instance
