"""
v77.0: Heartbeat Validator - Gaps #2-3
=======================================

Robust heartbeat validation with:
- Gap #2: Staleness detection
- Gap #3: PID validation
- Component health tracking
- Automatic dead component cleanup
- Health score calculation

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


class HeartbeatStatus(Enum):
    """Status of a heartbeat."""
    HEALTHY = "healthy"      # Recent heartbeat, valid PID
    STALE = "stale"          # Heartbeat older than threshold
    DEAD = "dead"            # Process not running
    UNKNOWN = "unknown"      # No heartbeat received
    ZOMBIE = "zombie"        # PID exists but not responding


@dataclass
class ComponentHealth:
    """Health status of a Trinity component."""
    component_id: str
    component_type: str  # jarvis_body, j_prime, reactor_core, coding_council
    status: HeartbeatStatus
    last_heartbeat: float = 0.0
    pid: Optional[int] = None
    host: str = "localhost"
    metrics: Dict[str, Any] = field(default_factory=dict)
    health_score: float = 1.0  # 0.0 to 1.0
    consecutive_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_id": self.component_id,
            "component_type": self.component_type,
            "status": self.status.value,
            "last_heartbeat": self.last_heartbeat,
            "pid": self.pid,
            "host": self.host,
            "metrics": self.metrics,
            "health_score": self.health_score,
            "consecutive_failures": self.consecutive_failures,
            "age_seconds": time.time() - self.last_heartbeat if self.last_heartbeat else 0,
        }


@dataclass
class Heartbeat:
    """A heartbeat message from a component."""
    component_id: str
    component_type: str
    timestamp: float = field(default_factory=time.time)
    pid: int = field(default_factory=os.getpid)
    host: str = field(default_factory=lambda: os.uname().nodename)
    version: str = "77.0"
    status: str = "running"
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_id": self.component_id,
            "component_type": self.component_type,
            "timestamp": self.timestamp,
            "pid": self.pid,
            "host": self.host,
            "version": self.version,
            "status": self.status,
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Heartbeat":
        return cls(
            component_id=data.get("component_id", ""),
            component_type=data.get("component_type", ""),
            timestamp=data.get("timestamp", time.time()),
            pid=data.get("pid", 0),
            host=data.get("host", ""),
            version=data.get("version", ""),
            status=data.get("status", ""),
            metrics=data.get("metrics", {}),
        )


class HeartbeatValidator:
    """
    Validates heartbeats with PID verification and staleness detection.

    Features:
    - PID validation (ensures process is actually running)
    - Staleness detection (heartbeat age threshold)
    - Health score calculation
    - Automatic cleanup of dead components
    - Event callbacks for status changes
    """

    # Thresholds
    STALE_THRESHOLD_SECONDS = 30.0  # Mark as stale after 30s
    DEAD_THRESHOLD_SECONDS = 120.0  # Mark as dead after 2 minutes
    HEALTH_DECAY_RATE = 0.1  # Health score decay per missed heartbeat

    def __init__(self, heartbeat_dir: Optional[Path] = None):
        self.heartbeat_dir = heartbeat_dir or Path.home() / ".jarvis" / "trinity" / "heartbeats"
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)
        self._components: Dict[str, ComponentHealth] = {}
        self._callbacks: List[Callable[[str, HeartbeatStatus, HeartbeatStatus], Coroutine]] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the heartbeat monitor."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("[HeartbeatValidator] Started")

    async def stop(self) -> None:
        """Stop the heartbeat monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[HeartbeatValidator] Stopped")

    async def register_heartbeat(self, heartbeat: Heartbeat) -> bool:
        """
        Register a heartbeat from a component.

        Validates PID and updates component health.
        """
        component_id = heartbeat.component_id

        # Validate PID (Gap #3)
        if heartbeat.host == os.uname().nodename:
            if not self._validate_pid(heartbeat.pid):
                logger.warning(f"[HeartbeatValidator] Invalid PID {heartbeat.pid} for {component_id}")
                return False

        # Get or create component health
        old_status = HeartbeatStatus.UNKNOWN
        if component_id in self._components:
            old_status = self._components[component_id].status

        # Update component health
        health = ComponentHealth(
            component_id=component_id,
            component_type=heartbeat.component_type,
            status=HeartbeatStatus.HEALTHY,
            last_heartbeat=heartbeat.timestamp,
            pid=heartbeat.pid,
            host=heartbeat.host,
            metrics=heartbeat.metrics,
            health_score=1.0,
            consecutive_failures=0,
        )

        self._components[component_id] = health

        # Write to file for cross-process visibility
        await self._write_heartbeat(heartbeat)

        # Notify if status changed
        if old_status != HeartbeatStatus.HEALTHY:
            await self._notify_status_change(component_id, old_status, HeartbeatStatus.HEALTHY)

        return True

    async def get_component_health(self, component_id: str) -> Optional[ComponentHealth]:
        """Get health status for a specific component."""
        return self._components.get(component_id)

    async def get_all_health(self) -> Dict[str, ComponentHealth]:
        """Get health status for all known components."""
        return self._components.copy()

    async def check_component(self, component_id: str) -> HeartbeatStatus:
        """Check current status of a component."""
        if component_id not in self._components:
            # Try to load from file
            await self._load_heartbeat(component_id)

        if component_id not in self._components:
            return HeartbeatStatus.UNKNOWN

        health = self._components[component_id]
        return await self._evaluate_status(health)

    def on_status_change(self, callback: Callable[[str, HeartbeatStatus, HeartbeatStatus], Coroutine]) -> None:
        """Register callback for status changes."""
        self._callbacks.append(callback)

    def on_staleness(self, callback: Callable[[str, float], Coroutine]) -> None:
        """
        v93.0: Register callback for when a component becomes stale or dead.

        This is a convenience method that wraps on_status_change to specifically
        handle staleness events. The callback receives component_id and last_seen timestamp.

        Args:
            callback: Async function that takes (component_id: str, last_seen: float)
        """
        # Store reference to self for closure
        validator = self

        async def staleness_wrapper(
            component_id: str,
            old_status: HeartbeatStatus,
            new_status: HeartbeatStatus
        ) -> None:
            # Only trigger if transitioning TO stale or dead
            if new_status in (HeartbeatStatus.STALE, HeartbeatStatus.DEAD, HeartbeatStatus.ZOMBIE):
                if old_status == HeartbeatStatus.HEALTHY:
                    try:
                        # Get last_seen from component health
                        last_seen = 0.0
                        if component_id in validator._components:
                            last_seen = validator._components[component_id].last_heartbeat

                        if asyncio.iscoroutinefunction(callback):
                            await callback(component_id, last_seen)
                        else:
                            callback(component_id, last_seen)
                    except Exception as e:
                        logger.error(f"[HeartbeatValidator] Staleness callback error: {e}")

        self._callbacks.append(staleness_wrapper)
        logger.debug("[HeartbeatValidator] Staleness callback registered")

    async def get_healthy_components(self, component_type: Optional[str] = None) -> List[ComponentHealth]:
        """Get list of healthy components."""
        healthy = []
        for health in self._components.values():
            if component_type and health.component_type != component_type:
                continue
            if health.status == HeartbeatStatus.HEALTHY:
                healthy.append(health)
        return healthy

    def _validate_pid(self, pid: int) -> bool:
        """
        Validate that a PID corresponds to a running process.

        Gap #3: PID Validation
        """
        if pid <= 0:
            return False

        try:
            # Check if process exists
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    async def _evaluate_status(self, health: ComponentHealth) -> HeartbeatStatus:
        """
        Evaluate current status of a component.

        Gap #2: Staleness detection
        """
        age = time.time() - health.last_heartbeat

        # Check staleness thresholds
        if age > self.DEAD_THRESHOLD_SECONDS:
            new_status = HeartbeatStatus.DEAD
        elif age > self.STALE_THRESHOLD_SECONDS:
            new_status = HeartbeatStatus.STALE
        else:
            new_status = HeartbeatStatus.HEALTHY

        # If marked healthy, verify PID
        if new_status == HeartbeatStatus.HEALTHY:
            if health.host == os.uname().nodename and health.pid:
                if not self._validate_pid(health.pid):
                    new_status = HeartbeatStatus.ZOMBIE

        # Update status if changed
        if health.status != new_status:
            old_status = health.status
            health.status = new_status
            await self._notify_status_change(health.component_id, old_status, new_status)

        # Update health score
        health.health_score = self._calculate_health_score(health, age)

        return new_status

    def _calculate_health_score(self, health: ComponentHealth, age: float) -> float:
        """
        Calculate health score (0.0 to 1.0).

        Based on:
        - Heartbeat age
        - Consecutive failures
        - Response metrics
        """
        score = 1.0

        # Age penalty
        if age > 0:
            age_factor = max(0, 1.0 - (age / self.DEAD_THRESHOLD_SECONDS))
            score *= age_factor

        # Failure penalty
        if health.consecutive_failures > 0:
            failure_factor = max(0.1, 1.0 - (health.consecutive_failures * self.HEALTH_DECAY_RATE))
            score *= failure_factor

        # Status penalty
        if health.status == HeartbeatStatus.STALE:
            score *= 0.5
        elif health.status == HeartbeatStatus.ZOMBIE:
            score *= 0.1
        elif health.status == HeartbeatStatus.DEAD:
            score = 0.0

        return round(score, 3)

    async def _write_heartbeat(self, heartbeat: Heartbeat) -> None:
        """Write heartbeat to file for cross-process visibility."""
        try:
            filepath = self.heartbeat_dir / f"{heartbeat.component_id}.json"
            tmp_path = filepath.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(heartbeat.to_dict(), indent=2))
            tmp_path.rename(filepath)
        except Exception as e:
            logger.warning(f"[HeartbeatValidator] Failed to write heartbeat: {e}")

    async def _load_heartbeat(self, component_id: str) -> None:
        """Load heartbeat from file."""
        try:
            filepath = self.heartbeat_dir / f"{component_id}.json"
            if filepath.exists():
                data = json.loads(filepath.read_text())
                heartbeat = Heartbeat.from_dict(data)

                health = ComponentHealth(
                    component_id=heartbeat.component_id,
                    component_type=heartbeat.component_type,
                    status=HeartbeatStatus.UNKNOWN,
                    last_heartbeat=heartbeat.timestamp,
                    pid=heartbeat.pid,
                    host=heartbeat.host,
                    metrics=heartbeat.metrics,
                )

                self._components[component_id] = health
        except Exception as e:
            logger.debug(f"[HeartbeatValidator] Failed to load heartbeat: {e}")

    async def _load_all_heartbeats(self) -> None:
        """Load all heartbeats from directory."""
        try:
            for filepath in self.heartbeat_dir.glob("*.json"):
                component_id = filepath.stem
                if component_id not in self._components:
                    await self._load_heartbeat(component_id)
        except Exception as e:
            logger.warning(f"[HeartbeatValidator] Failed to load heartbeats: {e}")

    async def _notify_status_change(
        self,
        component_id: str,
        old_status: HeartbeatStatus,
        new_status: HeartbeatStatus
    ) -> None:
        """Notify callbacks of status change."""
        logger.info(f"[HeartbeatValidator] {component_id}: {old_status.value} -> {new_status.value}")

        for callback in self._callbacks:
            try:
                await callback(component_id, old_status, new_status)
            except Exception as e:
                logger.error(f"[HeartbeatValidator] Callback error: {e}")

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while self._running:
            try:
                # Load any new heartbeats from files
                await self._load_all_heartbeats()

                # Evaluate all components
                for component_id in list(self._components.keys()):
                    health = self._components[component_id]
                    await self._evaluate_status(health)

                    # Clean up dead components after extended period
                    if health.status == HeartbeatStatus.DEAD:
                        age = time.time() - health.last_heartbeat
                        if age > self.DEAD_THRESHOLD_SECONDS * 5:  # 10 minutes
                            del self._components[component_id]
                            logger.info(f"[HeartbeatValidator] Removed dead component: {component_id}")

                await asyncio.sleep(5)  # Check every 5 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HeartbeatValidator] Monitor error: {e}")
                await asyncio.sleep(1)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of all component health."""
        healthy = sum(1 for h in self._components.values() if h.status == HeartbeatStatus.HEALTHY)
        stale = sum(1 for h in self._components.values() if h.status == HeartbeatStatus.STALE)
        dead = sum(1 for h in self._components.values() if h.status == HeartbeatStatus.DEAD)
        zombie = sum(1 for h in self._components.values() if h.status == HeartbeatStatus.ZOMBIE)

        return {
            "total": len(self._components),
            "healthy": healthy,
            "stale": stale,
            "dead": dead,
            "zombie": zombie,
            "by_type": self._group_by_type(),
        }

    def _group_by_type(self) -> Dict[str, Dict[str, int]]:
        """Group component counts by type."""
        by_type: Dict[str, Dict[str, int]] = {}

        for health in self._components.values():
            if health.component_type not in by_type:
                by_type[health.component_type] = {"total": 0, "healthy": 0}

            by_type[health.component_type]["total"] += 1
            if health.status == HeartbeatStatus.HEALTHY:
                by_type[health.component_type]["healthy"] += 1

        return by_type
