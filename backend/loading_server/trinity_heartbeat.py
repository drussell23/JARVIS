"""
Trinity Heartbeat Reader for JARVIS Loading Server v212.0
==========================================================

Direct Trinity heartbeat file monitoring for component health.

Features:
- File watcher with efficient monitoring
- Heartbeat age validation (< 30s = healthy)
- Automatic staleness detection
- Cached reads with TTL
- Support for all Trinity components
- Async-safe file operations

Usage:
    from backend.loading_server.trinity_heartbeat import TrinityHeartbeatReader

    reader = TrinityHeartbeatReader()
    heartbeat = await reader.read_component_heartbeat("jarvis_body")
    all_heartbeats = await reader.get_all_heartbeats()

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LoadingServer.Heartbeat")


# Default Trinity components
DEFAULT_COMPONENTS = [
    "jarvis_body",
    "jarvis_prime",
    "reactor_core",
    "coding_council",
    "agentic_watchdog",
    "memory_consolidator",
]


@dataclass
class HeartbeatData:
    """Parsed heartbeat data."""

    component: str
    timestamp: float
    age_seconds: float
    is_healthy: bool
    is_stale: bool
    status: str
    data: Dict[str, Any]

    @classmethod
    def from_dict(cls, component: str, data: Dict[str, Any]) -> "HeartbeatData":
        """Create from dictionary."""
        now = time.time()
        timestamp = data.get("timestamp", 0)
        age = now - timestamp

        return cls(
            component=component,
            timestamp=timestamp,
            age_seconds=age,
            is_healthy=age < 30.0,
            is_stale=age >= 30.0,
            status=data.get("status", "unknown"),
            data=data,
        )


@dataclass
class TrinityHeartbeatReader:
    """
    Direct Trinity heartbeat file monitoring.

    Reads heartbeat files from ~/.jarvis/trinity/components/ to track:
    - jarvis_body.json
    - jarvis_prime.json
    - reactor_core.json
    - coding_council.json
    - agentic_watchdog.json

    Features:
    - File watcher with efficient monitoring
    - Heartbeat age validation (< 30s = healthy)
    - Automatic staleness detection
    - Cached reads with configurable TTL
    """

    jarvis_home: Path = field(default_factory=lambda: Path.home() / ".jarvis")
    cache_ttl: float = 2.0  # Cache heartbeats for 2 seconds
    stale_threshold: float = 30.0  # Heartbeats older than 30s are stale

    # Internal state
    _last_read: Dict[str, float] = field(init=False, default_factory=dict)
    _cache: Dict[str, HeartbeatData] = field(init=False, default_factory=dict)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    @property
    def heartbeat_dir(self) -> Path:
        """Get heartbeat directory path."""
        return self.jarvis_home / "trinity" / "components"

    def _get_heartbeat_path(self, component: str) -> Path:
        """Get heartbeat file path for a component."""
        return self.heartbeat_dir / f"{component}.json"

    async def read_component_heartbeat(
        self, component: str, force_refresh: bool = False
    ) -> Optional[HeartbeatData]:
        """
        Read heartbeat file for a component with caching.

        Args:
            component: Component name (e.g., "jarvis_body")
            force_refresh: If True, bypass cache

        Returns:
            HeartbeatData if file exists and is valid, None otherwise
        """
        now = time.time()

        # Check cache first (unless force refresh)
        if not force_refresh and component in self._cache:
            cache_age = now - self._last_read.get(component, 0)
            if cache_age < self.cache_ttl:
                return self._cache[component]

        # Read from disk
        heartbeat_path = self._get_heartbeat_path(component)

        if not heartbeat_path.exists():
            logger.debug(f"[Trinity] No heartbeat file for {component}")
            return None

        async with self._lock:
            try:
                # Read file asynchronously
                content = await asyncio.to_thread(heartbeat_path.read_text)
                data = json.loads(content)

                # Create heartbeat data
                heartbeat = HeartbeatData.from_dict(component, data)

                # Validate freshness
                if heartbeat.is_stale:
                    logger.debug(
                        f"[Trinity] {component} heartbeat stale (age={heartbeat.age_seconds:.1f}s)"
                    )

                # Update cache
                self._cache[component] = heartbeat
                self._last_read[component] = now

                return heartbeat

            except json.JSONDecodeError as e:
                logger.debug(f"[Trinity] Invalid JSON in {component} heartbeat: {e}")
                return None
            except IOError as e:
                logger.debug(f"[Trinity] Failed to read {component} heartbeat: {e}")
                return None

    async def get_all_heartbeats(
        self, components: Optional[List[str]] = None
    ) -> Dict[str, Optional[HeartbeatData]]:
        """
        Get heartbeats for all Trinity components.

        Args:
            components: List of component names, or None for defaults

        Returns:
            Dict mapping component name to HeartbeatData (or None if unavailable)
        """
        if components is None:
            components = DEFAULT_COMPONENTS

        # Read all heartbeats in parallel
        tasks = [self.read_component_heartbeat(c) for c in components]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        heartbeats: Dict[str, Optional[HeartbeatData]] = {}
        for i, component in enumerate(components):
            result = results[i]
            if isinstance(result, Exception):
                logger.debug(f"[Trinity] Error reading {component}: {result}")
                heartbeats[component] = None
            elif isinstance(result, HeartbeatData):
                heartbeats[component] = result
            else:
                heartbeats[component] = None

        return heartbeats

    async def get_healthy_components(self) -> List[str]:
        """
        Get list of healthy components.

        Returns:
            List of component names that have fresh heartbeats
        """
        heartbeats = await self.get_all_heartbeats()
        return [
            name
            for name, hb in heartbeats.items()
            if hb is not None and hb.is_healthy
        ]

    async def get_unhealthy_components(self) -> List[str]:
        """
        Get list of unhealthy/stale components.

        Returns:
            List of component names with stale or missing heartbeats
        """
        heartbeats = await self.get_all_heartbeats()
        return [
            name
            for name, hb in heartbeats.items()
            if hb is None or hb.is_stale
        ]

    async def get_component_status(self, component: str) -> str:
        """
        Get status string for a component.

        Args:
            component: Component name

        Returns:
            Status string: "healthy", "degraded", "stale", or "unknown"
        """
        heartbeat = await self.read_component_heartbeat(component)

        if heartbeat is None:
            return "unknown"
        elif heartbeat.age_seconds < 10:
            return "healthy"
        elif heartbeat.age_seconds < 30:
            return "degraded"
        else:
            return "stale"

    async def wait_for_heartbeat(
        self,
        component: str,
        timeout: float = 30.0,
        check_interval: float = 0.5,
    ) -> Optional[HeartbeatData]:
        """
        Wait for a component's heartbeat to appear.

        Useful during startup when waiting for components to come online.

        Args:
            component: Component name to wait for
            timeout: Maximum time to wait
            check_interval: Time between checks

        Returns:
            HeartbeatData when heartbeat appears, None on timeout
        """
        start = time.time()

        while time.time() - start < timeout:
            heartbeat = await self.read_component_heartbeat(component, force_refresh=True)
            if heartbeat is not None and heartbeat.is_healthy:
                return heartbeat
            await asyncio.sleep(check_interval)

        return None

    async def get_health_summary(self) -> Dict[str, Any]:
        """
        Get comprehensive health summary across all components.

        Returns:
            Dict with component health details, counts, and recommendations
        """
        heartbeats = await self.get_all_heartbeats()

        healthy = []
        degraded = []
        stale = []
        unknown = []

        for name, hb in heartbeats.items():
            if hb is None:
                unknown.append(name)
            elif hb.age_seconds < 10:
                healthy.append(name)
            elif hb.age_seconds < 30:
                degraded.append(name)
            else:
                stale.append(name)

        total = len(heartbeats)
        healthy_count = len(healthy)

        # Calculate overall health percentage
        health_pct = (healthy_count / total * 100) if total > 0 else 0

        return {
            "overall_health_percent": round(health_pct, 1),
            "total_components": total,
            "healthy_count": healthy_count,
            "degraded_count": len(degraded),
            "stale_count": len(stale),
            "unknown_count": len(unknown),
            "healthy": healthy,
            "degraded": degraded,
            "stale": stale,
            "unknown": unknown,
            "components": {
                name: {
                    "status": (
                        "healthy" if hb and hb.age_seconds < 10
                        else "degraded" if hb and hb.age_seconds < 30
                        else "stale" if hb
                        else "unknown"
                    ),
                    "age_seconds": round(hb.age_seconds, 1) if hb else None,
                    "last_status": hb.status if hb else None,
                }
                for name, hb in heartbeats.items()
            },
        }

    def clear_cache(self) -> None:
        """Clear the heartbeat cache."""
        self._cache.clear()
        self._last_read.clear()

    async def write_heartbeat(
        self,
        component: str,
        status: str = "healthy",
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Write a heartbeat file for a component.

        Useful for components that want to report their own status.

        Args:
            component: Component name
            status: Status string
            extra_data: Additional data to include

        Returns:
            True if write successful
        """
        heartbeat_path = self._get_heartbeat_path(component)
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "component": component,
            "timestamp": time.time(),
            "status": status,
            **(extra_data or {}),
        }

        try:
            content = json.dumps(data, indent=2)
            await asyncio.to_thread(heartbeat_path.write_text, content)
            return True
        except Exception as e:
            logger.warning(f"[Trinity] Failed to write heartbeat for {component}: {e}")
            return False
