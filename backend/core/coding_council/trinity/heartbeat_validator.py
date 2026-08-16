"""
v115.0: Heartbeat Validator - Enterprise Edition
=================================================

Robust heartbeat validation with:
- Gap #2: Staleness detection
- Gap #3: PID validation
- Component health tracking
- Automatic dead component cleanup
- Health score calculation
- v108.0: Startup grace period awareness
- v108.0: Component-specific timeout profiles
- v108.0: Integration with TrinityOrchestrationConfig
- v115.0: Permanent removal tracking (prevents dead component loop)
- v115.0: Heartbeat file cleanup on removal
- v115.0: Reduced log noise (only log significant transitions)

CRITICAL v108.0 FIX: All thresholds now come from TrinityOrchestrationConfig
to prevent mismatched configuration values causing cascading failures.

CRITICAL v115.0 FIX: Dead components are permanently removed and their
heartbeat files are deleted to prevent "removed every cycle" log spam.

Author: JARVIS v115.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

# v115.0: Use fixed logger name to avoid duplicate loggers when imported
# as "backend.core.coding_council.trinity.heartbeat_validator" vs
# "core.coding_council.trinity.heartbeat_validator"
logger = logging.getLogger("jarvis.trinity.heartbeat_validator")

# Shared-validator lifecycle (single monitor loop per process).
_shared_validator_lock = threading.Lock()
_shared_validators: Dict[str, "HeartbeatValidator"] = {}
_shared_validator_refcounts: Dict[str, int] = {}


def _shared_key_for_dir(heartbeat_dir: Optional[Path]) -> str:
    """Normalize heartbeat dir into a stable registry key."""
    effective_dir = heartbeat_dir or (Path.home() / ".jarvis" / "trinity" / "heartbeats")
    try:
        return str(effective_dir.expanduser().resolve())
    except Exception:
        return str(effective_dir.expanduser())


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


def _env_float(key: str, default: float) -> float:
    """Get float from environment variable with default."""
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


class HeartbeatValidator:
    """
    Validates heartbeats with PID verification and staleness detection.

    Features:
    - PID validation (ensures process is actually running)
    - Staleness detection (heartbeat age threshold)
    - Health score calculation
    - Automatic cleanup of dead components
    - Event callbacks for status changes

    v108.0 Enhancements:
    - Startup grace period awareness - components NOT marked dead during startup
    - Component-specific timeout profiles from TrinityOrchestrationConfig
    - Dynamic threshold adjustment based on component type
    - All thresholds configurable via environment variables (fallback)
    """

    # v108.0: Default thresholds - actual values come from TrinityOrchestrationConfig
    # These are FALLBACK values only when orchestration config is not available
    STALE_THRESHOLD_SECONDS = _env_float("HEARTBEAT_STALE_THRESHOLD", 45.0)  # Increased from 30.0
    DEAD_THRESHOLD_SECONDS = _env_float("HEARTBEAT_DEAD_THRESHOLD", 180.0)   # Increased from 120.0
    HEALTH_DECAY_RATE = _env_float("HEARTBEAT_HEALTH_DECAY_RATE", 0.1)
    MONITOR_INTERVAL_SECONDS = _env_float("HEARTBEAT_MONITOR_INTERVAL", 5.0)
    CLEANUP_MULTIPLIER = _env_float("HEARTBEAT_CLEANUP_MULTIPLIER", 5.0)  # Dead threshold * this = cleanup

    def __init__(self, heartbeat_dir: Optional[Path] = None):
        self.heartbeat_dir = heartbeat_dir or Path.home() / ".jarvis" / "trinity" / "heartbeats"
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)

        # v93.0: Cross-repo heartbeat directories for Trinity synchronization
        self._cross_repo_dirs: List[Path] = [
            self.heartbeat_dir,                                    # Primary: local heartbeats
            Path.home() / ".jarvis" / "cross_repo",               # Cross-repo shared directory
            Path.home() / ".jarvis" / "trinity" / "components",   # Legacy compatibility
        ]

        # Ensure cross-repo directory exists
        cross_repo_dir = Path.home() / ".jarvis" / "cross_repo"
        cross_repo_dir.mkdir(parents=True, exist_ok=True)

        self._components: Dict[str, ComponentHealth] = {}
        self._callbacks: List[Callable[[str, HeartbeatStatus, HeartbeatStatus], Coroutine]] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    # v93.0: Enable cross-repo synchronization by default
        self._cross_repo_sync_enabled = True

        # v108.0: Track component startup times for grace period awareness
        self._component_startup_times: Dict[str, float] = {}

        # v108.0: Cache for component-specific thresholds
        self._threshold_cache: Dict[str, Dict[str, float]] = {}

        # ═══════════════════════════════════════════════════════════════════
        # v113.0: GLOBAL STARTUP GRACE PERIOD
        # ═══════════════════════════════════════════════════════════════════
        # This is the FIX for cross-repo staleness during startup.
        # If the supervisor process just started, ALL components get grace period
        # even if record_component_startup() wasn't called for them.
        # ═══════════════════════════════════════════════════════════════════
        self._global_startup_time = time.time()
        self._global_startup_grace_seconds = _env_float(
            "TRINITY_GLOBAL_STARTUP_GRACE_PERIOD", 120.0  # 2 minutes grace for all
        )

        # ═══════════════════════════════════════════════════════════════════
        # v115.0: PERMANENT REMOVAL TRACKING
        # ═══════════════════════════════════════════════════════════════════
        # Prevents the "dead component removed every cycle" log spam.
        # When a component is marked dead and removed, we:
        # 1. Delete its heartbeat files from all directories
        # 2. Add its component_id to this set
        # 3. Skip loading heartbeats for IDs in this set
        # This persists until the validator is restarted (intentional - restart resets)
        # ═══════════════════════════════════════════════════════════════════
        self._permanently_removed: set[str] = set()

        # v115.0: Track already-logged state transitions to reduce noise
        # Key: component_id, Value: (old_status, new_status, timestamp)
        self._logged_transitions: Dict[str, tuple] = {}

    def get_stale_threshold(self, component_type: str) -> float:
        """
        v108.0: Get component-specific stale threshold.

        This integrates with TrinityOrchestrationConfig for consistent values.
        Falls back to class defaults if orchestration config is unavailable.

        Args:
            component_type: Component type string (jarvis_body, jarvis_prime, etc.)

        Returns:
            Stale threshold in seconds
        """
        # Check cache first
        if component_type in self._threshold_cache:
            return self._threshold_cache[component_type].get(
                "stale", self.STALE_THRESHOLD_SECONDS
            )

        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
            )
            orch_config = get_orchestration_config()
            profile = orch_config.get_profile_by_name(component_type)

            # Cache the thresholds
            self._threshold_cache[component_type] = {
                "stale": profile.heartbeat_stale,
                "dead": profile.effective_dead_threshold,
                "grace_period": profile.startup_grace_period,
            }

            return profile.heartbeat_stale

        except ImportError:
            logger.debug(
                f"[HeartbeatValidator] TrinityOrchestrationConfig not available, "
                f"using default stale threshold for {component_type}"
            )
            return self.STALE_THRESHOLD_SECONDS

    def get_dead_threshold(self, component_type: str) -> float:
        """
        v108.0: Get component-specific dead threshold.

        Critical: Dead threshold must ALWAYS be >= startup timeout to prevent
        components being marked dead while still initializing.
        """
        if component_type in self._threshold_cache:
            return self._threshold_cache[component_type].get(
                "dead", self.DEAD_THRESHOLD_SECONDS
            )

        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
            )
            orch_config = get_orchestration_config()
            profile = orch_config.get_profile_by_name(component_type)

            # Cache the thresholds
            self._threshold_cache[component_type] = {
                "stale": profile.heartbeat_stale,
                "dead": profile.effective_dead_threshold,
                "grace_period": profile.startup_grace_period,
            }

            return profile.effective_dead_threshold

        except ImportError:
            return self.DEAD_THRESHOLD_SECONDS

    def record_component_startup(self, component_id: str, component_type: str) -> None:
        """
        v108.0: Record when a component starts for grace period tracking.

        This should be called when a component process is spawned.
        """
        self._component_startup_times[component_id] = time.time()
        logger.debug(
            f"[HeartbeatValidator] Recorded startup time for {component_id} ({component_type})"
        )

    def is_in_startup_grace_period(self, component_id: str, component_type: str) -> bool:
        """
        v113.0: Check if a component is still in its startup grace period.

        During startup, components should NOT be marked as dead/stale
        even if heartbeats are missing.

        v113.0 CRITICAL FIX: Now uses GLOBAL startup grace period as fallback
        for cross-repo components that weren't registered via record_component_startup().
        This fixes the reactor_core staleness issue during Trinity startup.

        Args:
            component_id: Unique component ID
            component_type: Component type (jarvis_body, jarvis_prime, etc.)

        Returns:
            True if component is still in grace period
        """
        # ═══════════════════════════════════════════════════════════════════
        # v113.0: CHECK GLOBAL GRACE PERIOD FIRST (affects ALL components)
        # ═══════════════════════════════════════════════════════════════════
        # This is the FIX for cross-repo staleness during startup.
        # During system startup, ALL components get a grace period, even if
        # record_component_startup() was never called for them.
        # ═══════════════════════════════════════════════════════════════════
        global_elapsed = time.time() - self._global_startup_time
        if global_elapsed < self._global_startup_grace_seconds:
            logger.debug(
                f"[HeartbeatValidator] {component_id} in GLOBAL startup grace period "
                f"({global_elapsed:.1f}s < {self._global_startup_grace_seconds}s)"
            )
            return True

        # ═══════════════════════════════════════════════════════════════════
        # v108.0: Per-component grace period (for components that were registered)
        # ═══════════════════════════════════════════════════════════════════
        startup_time = self._component_startup_times.get(component_id)

        if startup_time is None:
            # No recorded startup time and global grace expired
            return False

        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
            )
            orch_config = get_orchestration_config()
            profile = orch_config.get_profile_by_name(component_type)

            elapsed = time.time() - startup_time
            in_grace = elapsed < profile.startup_grace_period

            if in_grace:
                logger.debug(
                    f"[HeartbeatValidator] {component_id} in per-component grace period "
                    f"({elapsed:.1f}s < {profile.startup_grace_period}s)"
                )

            return in_grace

        except ImportError:
            # Fallback: use 2x dead threshold as grace period
            grace_period = self.DEAD_THRESHOLD_SECONDS * 2
            elapsed = time.time() - startup_time
            return elapsed < grace_period

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

    def off_status_change(self, callback: Callable[[str, HeartbeatStatus, HeartbeatStatus], Coroutine]) -> None:
        """Remove a previously registered status-change callback."""
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    def on_staleness(self, callback: Callable[[str, float], Coroutine]) -> Callable:
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

                        # v114.0: Handle both async and sync callbacks properly
                        if asyncio.iscoroutinefunction(callback):
                            await callback(component_id, last_seen)
                        else:
                            result = callback(component_id, last_seen)
                            # Handle case where non-async function returns a coroutine
                            if asyncio.iscoroutine(result):
                                await result
                    except Exception as e:
                        logger.error(f"[HeartbeatValidator] Staleness callback error: {e}")

        self._callbacks.append(staleness_wrapper)
        logger.debug("[HeartbeatValidator] Staleness callback registered")
        return staleness_wrapper

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

        v108.0: Now uses component-specific thresholds and startup grace period awareness.
        v111.2: In-process components (same PID) are always considered healthy.

        Gap #2: Staleness detection
        """
        # ═══════════════════════════════════════════════════════════════════
        # v111.2: IN-PROCESS COMPONENT DETECTION (Unified Monolith Mode)
        # ═══════════════════════════════════════════════════════════════════
        # If a component's PID matches our current PID, it's running in-process
        # with the supervisor and is inherently alive. No heartbeat file needed.
        #
        # This prevents the HeartbeatValidator from marking in-process components
        # (like jarvis_body in unified monolith mode) as "dead" and triggering
        # recovery cascades that shut down the backend.
        # ═══════════════════════════════════════════════════════════════════
        current_pid = os.getpid()
        if health.pid == current_pid and health.host == os.uname().nodename:
            # In-process component - always healthy (same process = same fate)
            if health.status != HeartbeatStatus.HEALTHY:
                old_status = health.status
                health.status = HeartbeatStatus.HEALTHY
                health.health_score = 1.0
                health.last_heartbeat = time.time()  # Update to prevent staleness
                # Only log transition once to avoid spam
                if old_status != HeartbeatStatus.HEALTHY:
                    logger.debug(
                        f"[HeartbeatValidator] {health.component_id} is in-process "
                        f"(PID={current_pid}) - always healthy in unified monolith mode"
                    )
            return HeartbeatStatus.HEALTHY

        age = time.time() - health.last_heartbeat

        # v108.0: Get component-specific thresholds
        stale_threshold = self.get_stale_threshold(health.component_type)
        dead_threshold = self.get_dead_threshold(health.component_type)

        # Check staleness thresholds
        if age > dead_threshold:
            # v108.0: CRITICAL FIX - Check startup grace period before marking dead
            if self.is_in_startup_grace_period(health.component_id, health.component_type):
                # Component is still starting - don't mark as dead
                new_status = HeartbeatStatus.STALE  # Use stale instead of dead during startup
                logger.debug(
                    f"[HeartbeatValidator] {health.component_id} would be DEAD "
                    f"(age={age:.1f}s > {dead_threshold}s) but in startup grace period, "
                    f"marking as STALE instead"
                )
            else:
                new_status = HeartbeatStatus.DEAD
        elif age > stale_threshold:
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

        # Update health score (v108.0: use component-specific dead threshold)
        health.health_score = self._calculate_health_score(health, age, dead_threshold)

        return new_status

    def _calculate_health_score(
        self,
        health: ComponentHealth,
        age: float,
        dead_threshold: Optional[float] = None,
    ) -> float:
        """
        Calculate health score (0.0 to 1.0).

        v108.0: Now accepts component-specific dead_threshold parameter.

        Based on:
        - Heartbeat age
        - Consecutive failures
        - Response metrics
        """
        score = 1.0

        # v108.0: Use component-specific threshold if provided
        effective_dead_threshold = dead_threshold or self.DEAD_THRESHOLD_SECONDS

        # Age penalty
        if age > 0:
            age_factor = max(0, 1.0 - (age / effective_dead_threshold))
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
        """
        v93.0: Write heartbeat to file for cross-process visibility.

        Enhanced to write to both local and cross-repo directories for
        Trinity-wide synchronization.
        """
        heartbeat_data = json.dumps(heartbeat.to_dict(), indent=2)

        # Write to primary heartbeat directory
        try:
            filepath = self.heartbeat_dir / f"{heartbeat.component_id}.json"
            tmp_path = filepath.with_suffix(".tmp")
            tmp_path.write_text(heartbeat_data)
            tmp_path.rename(filepath)
        except Exception as e:
            logger.warning(f"[HeartbeatValidator] Failed to write heartbeat to primary: {e}")

        # v93.0: Also write to cross-repo directory for Trinity synchronization
        if self._cross_repo_sync_enabled:
            try:
                cross_repo_dir = Path.home() / ".jarvis" / "cross_repo"
                cross_repo_dir.mkdir(parents=True, exist_ok=True)
                filepath = cross_repo_dir / f"{heartbeat.component_id}.json"
                tmp_path = filepath.with_suffix(".tmp")
                tmp_path.write_text(heartbeat_data)
                tmp_path.rename(filepath)
            except Exception as e:
                logger.debug(f"[HeartbeatValidator] Failed to write heartbeat to cross-repo: {e}")

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
        """
        v115.0: Load and REFRESH all heartbeats from multiple directories.

        CRITICAL FIX v114.0: Now ALWAYS refreshes timestamp from files, even for
        existing components. This fixes the root cause of false staleness warnings
        where in-memory timestamps became stale while files were being updated.

        CRITICAL FIX v115.0: Skips permanently removed components to prevent the
        "dead component removed every cycle" log spam. Components in _permanently_removed
        are not reloaded until the validator is restarted.

        Loads from all cross-repo directories for Trinity-wide visibility:
        - Primary heartbeat directory
        - Cross-repo shared directory
        - Legacy components directory

        Priority: Files in earlier directories take precedence if timestamps are equal.
        However, NEWER timestamps from any directory always win.
        """
        # Track which components we've processed (with their timestamps)
        processed_components: Dict[str, float] = {}

        for heartbeat_dir in self._cross_repo_dirs:
            if not heartbeat_dir.exists():
                continue

            try:
                for filepath in heartbeat_dir.glob("*.json"):
                    component_id = filepath.stem

                    # ═══════════════════════════════════════════════════════════
                    # v115.0: Skip permanently removed components
                    # This prevents the "dead component re-appearing every cycle" issue
                    # ═══════════════════════════════════════════════════════════
                    if component_id in self._permanently_removed:
                        continue

                    try:
                        # ALWAYS read the file to check timestamp
                        data = json.loads(filepath.read_text())
                        file_timestamp = data.get("timestamp", 0.0)

                        # Skip if we already processed a NEWER heartbeat for this component
                        if component_id in processed_components:
                            if processed_components[component_id] >= file_timestamp:
                                continue

                        # v114.0 CRITICAL FIX: Update existing components if file is newer
                        if component_id in self._components:
                            existing_timestamp = self._components[component_id].last_heartbeat
                            if file_timestamp > existing_timestamp:
                                # File has newer timestamp - refresh the in-memory state
                                heartbeat = Heartbeat.from_dict(data)
                                self._components[component_id].last_heartbeat = heartbeat.timestamp
                                self._components[component_id].pid = heartbeat.pid
                                self._components[component_id].host = heartbeat.host
                                self._components[component_id].metrics = heartbeat.metrics
                                # If it was stale/dead, reset to unknown for re-evaluation
                                if self._components[component_id].status in (
                                    HeartbeatStatus.STALE,
                                    HeartbeatStatus.DEAD,
                                    HeartbeatStatus.ZOMBIE,
                                ):
                                    self._components[component_id].status = HeartbeatStatus.UNKNOWN
                                    self._components[component_id].health_score = 0.5
                                logger.debug(
                                    f"[HeartbeatValidator] Refreshed {component_id} timestamp: "
                                    f"{existing_timestamp:.1f} -> {file_timestamp:.1f}"
                                )
                        else:
                            # New component - load it
                            await self._load_heartbeat_from_path(filepath)

                        processed_components[component_id] = file_timestamp

                    except json.JSONDecodeError:
                        logger.debug(f"[HeartbeatValidator] Invalid JSON in {filepath}")
                    except Exception as file_err:
                        logger.debug(f"[HeartbeatValidator] Error reading {filepath}: {file_err}")

            except Exception as e:
                logger.debug(f"[HeartbeatValidator] Failed to load heartbeats from {heartbeat_dir}: {e}")

    async def _load_heartbeat_from_path(self, filepath: Path) -> None:
        """
        v93.0: Load heartbeat from a specific file path.

        Enhanced version of _load_heartbeat that accepts a full path.
        """
        try:
            if filepath.exists():
                # `exists()` + `read_text()` + `json.loads()` per heartbeat
                # file, every monitor tick. StallSampler caught this on the
                # wedged main loop: a handful of small reads is nothing until
                # the disk is busy, and then the loop wears the latency of
                # every one of them. The parse rides along because splitting
                # it would leave the JSON decode — CPU over the file — on the
                # loop for no gain.
                try:
                    from backend.core.async_offload import call_off_loop
                    data = await call_off_loop(
                        lambda: json.loads(filepath.read_text()))
                except Exception:  # noqa: BLE001 — fail-open to inline
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

                self._components[heartbeat.component_id] = health
                logger.debug(
                    f"[HeartbeatValidator] Loaded heartbeat for {heartbeat.component_id} "
                    f"from {filepath.parent.name}"
                )
        except Exception as e:
            logger.debug(f"[HeartbeatValidator] Failed to load heartbeat from {filepath}: {e}")

    async def _notify_status_change(
        self,
        component_id: str,
        old_status: HeartbeatStatus,
        new_status: HeartbeatStatus
    ) -> None:
        """
        v115.0: Notify callbacks of status change with reduced log noise.

        Only logs significant transitions to reduce spam:
        - Transitions TO dead/zombie/stale (problems starting)
        - Transitions FROM dead/zombie (recovery)
        - First time a component is seen (unknown -> anything)

        Normal healthy state maintenance is logged at DEBUG level.
        """
        # ═══════════════════════════════════════════════════════════════════
        # v115.0: SMART LOGGING - Reduce "unknown -> healthy" spam
        # ═══════════════════════════════════════════════════════════════════
        is_significant = (
            # Transitions TO problematic states (always important)
            new_status in (HeartbeatStatus.DEAD, HeartbeatStatus.ZOMBIE, HeartbeatStatus.STALE)
            # Recovery from problematic states (important)
            or old_status in (HeartbeatStatus.DEAD, HeartbeatStatus.ZOMBIE)
            # First time seeing a component go healthy (informative, but not spammy)
            or (old_status == HeartbeatStatus.UNKNOWN and new_status == HeartbeatStatus.HEALTHY
                and component_id not in self._logged_transitions)
        )

        # Track this transition
        self._logged_transitions[component_id] = (old_status, new_status, time.time())

        if is_significant:
            logger.info(f"[HeartbeatValidator] {component_id}: {old_status.value} -> {new_status.value}")
        else:
            # Log routine transitions at DEBUG level
            logger.debug(f"[HeartbeatValidator] {component_id}: {old_status.value} -> {new_status.value}")

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
                    # v170.0: Skip components already marked for permanent removal
                    if component_id in self._permanently_removed:
                        self._components.pop(component_id, None)
                        continue

                    health = self._components.get(component_id)
                    if not health:
                        continue

                    await self._evaluate_status(health)

                    # Clean up dead components after extended period
                    if health.status == HeartbeatStatus.DEAD:
                        age = time.time() - health.last_heartbeat
                        # v93.0: Use configurable cleanup multiplier
                        cleanup_threshold = self.DEAD_THRESHOLD_SECONDS * self.CLEANUP_MULTIPLIER
                        if age > cleanup_threshold:
                            # ═══════════════════════════════════════════════════════
                            # v170.0: PERMANENT REMOVAL WITH FILE CLEANUP
                            # This fixes the "dead component removed every cycle" bug
                            # ═══════════════════════════════════════════════════════
                            await self._permanently_remove_component(component_id)

                # v93.0: Use configurable monitor interval
                await asyncio.sleep(self.MONITOR_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HeartbeatValidator] Monitor error: {e}")
                await asyncio.sleep(1)

    async def _permanently_remove_component(self, component_id: str) -> None:
        """
        v170.0: Permanently remove a dead component and clean up its heartbeat files.

        This prevents the "dead component removed every cycle" log spam by:
        1. Checking if ALREADY permanently removed (prevents duplicate logs)
        2. Deleting heartbeat files from all directories
        3. Adding to _permanently_removed set
        4. Removing from _components dict

        The component will not be reloaded until the validator is restarted.
        """
        # v170.0: CRITICAL FIX - Skip if already permanently removed
        # This prevents duplicate "Permanently removed dead component" log spam
        if component_id in self._permanently_removed:
            # Already removed - just ensure it's not in _components
            self._components.pop(component_id, None)
            return

        # Delete from in-memory tracking
        if component_id in self._components:
            del self._components[component_id]

        # Add to permanent removal set (prevents future loading)
        self._permanently_removed.add(component_id)

        # Delete heartbeat files from all directories
        files_deleted = 0
        for heartbeat_dir in self._cross_repo_dirs:
            try:
                filepath = heartbeat_dir / f"{component_id}.json"
                if filepath.exists():
                    filepath.unlink()
                    files_deleted += 1
            except Exception as e:
                logger.debug(f"[HeartbeatValidator] Could not delete {filepath}: {e}")

        # Log removal only once (this is the only place this log should appear)
        logger.info(
            f"[HeartbeatValidator] Permanently removed dead component: {component_id} "
            f"(deleted {files_deleted} heartbeat file(s))"
        )

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


async def acquire_shared_heartbeat_validator(
    heartbeat_dir: Optional[Path] = None
) -> HeartbeatValidator:
    """
    Acquire a process-wide shared HeartbeatValidator instance.

    This prevents multiple monitor loops from scanning the same heartbeat files
    and emitting duplicate transitions in one process.
    """
    key = _shared_key_for_dir(heartbeat_dir)
    should_start = False
    with _shared_validator_lock:
        if key not in _shared_validators:
            _shared_validators[key] = HeartbeatValidator(heartbeat_dir=heartbeat_dir)
            _shared_validator_refcounts[key] = 0
            should_start = True

        _shared_validator_refcounts[key] = _shared_validator_refcounts.get(key, 0) + 1
        validator = _shared_validators[key]
        refcount = _shared_validator_refcounts[key]

    if validator is None:
        raise RuntimeError("Failed to create shared HeartbeatValidator")

    if should_start:
        try:
            await validator.start()
        except Exception:
            with _shared_validator_lock:
                if _shared_validators.get(key) is validator:
                    _shared_validators.pop(key, None)
                _shared_validator_refcounts[key] = max(0, _shared_validator_refcounts.get(key, 1) - 1)
                if _shared_validator_refcounts.get(key, 0) == 0:
                    _shared_validator_refcounts.pop(key, None)
            raise

    logger.debug("[HeartbeatValidator] Shared instance acquired (key=%s, refs=%d)", key, refcount)
    return validator


async def release_shared_heartbeat_validator(
    validator: Optional[HeartbeatValidator] = None
) -> None:
    """Release a shared validator lease; stops monitor when last lease is released."""
    key = _shared_key_for_dir(None)
    to_stop: Optional[HeartbeatValidator] = None
    with _shared_validator_lock:
        if validator is not None:
            key = ""
            for candidate_key, candidate_validator in _shared_validators.items():
                if candidate_validator is validator:
                    key = candidate_key
                    break
            if not key:
                logger.debug("[HeartbeatValidator] Ignoring release for non-shared validator instance")
                return

        if key not in _shared_validators:
            return

        if _shared_validator_refcounts.get(key, 0) > 0:
            _shared_validator_refcounts[key] -= 1

        refcount = _shared_validator_refcounts.get(key, 0)
        if refcount == 0:
            to_stop = _shared_validators.pop(key)
            _shared_validator_refcounts.pop(key, None)

    if to_stop is not None:
        await to_stop.stop()

    logger.debug("[HeartbeatValidator] Shared instance released (key=%s, refs=%d)", key, refcount)


def get_shared_heartbeat_validator_refcount(heartbeat_dir: Optional[Path] = None) -> int:
    """Return current lease count for shared HeartbeatValidator instances."""
    key = _shared_key_for_dir(heartbeat_dir) if heartbeat_dir is not None else None
    with _shared_validator_lock:
        if key is None:
            return sum(_shared_validator_refcounts.values())
        return _shared_validator_refcounts.get(key, 0)
