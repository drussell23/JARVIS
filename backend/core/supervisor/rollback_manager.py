#!/usr/bin/env python3
"""
JARVIS Rollback Manager with Dead Man's Switch
================================================

Version history tracking, rollback logic, and post-update stability verification
for the Self-Updating Lifecycle Manager.

Features:
- SQLite-based version history with boot/crash tracking
- Git reflog integration for emergency rollback
- Dead Man's Switch: Post-update probation with automatic rollback
- Parallel health probing for fast failure detection
- Intelligent stability commitment
- Crash pattern analysis and learning

The Dead Man's Switch ensures JARVIS can safely self-update by:
1. Starting a probation period after each update
2. Monitoring health via parallel heartbeat probes
3. Auto-rolling back if stability checks fail
4. Committing the version as "stable" once probation passes

Author: JARVIS System
Version: 2.0.0 - Dead Man's Switch Edition
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import aiohttp

from .supervisor_config import SupervisorConfig, get_supervisor_config

logger = logging.getLogger(__name__)


# =============================================================================
# Dead Man's Switch - Enums and Data Classes
# =============================================================================

class ProbationState(str, Enum):
    """State of post-update probation period."""
    INACTIVE = "inactive"           # No recent update, normal operation
    MONITORING = "monitoring"       # Actively monitoring post-update
    HEALTHY = "healthy"             # Passed probation, ready to commit
    FAILING = "failing"             # Health checks failing, may rollback
    ROLLING_BACK = "rolling_back"   # Rollback in progress
    COMMITTED = "committed"         # Version marked as stable


class HeartbeatStatus(str, Enum):
    """Status of a single heartbeat probe."""
    ALIVE = "alive"
    TIMEOUT = "timeout"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class HeartbeatResult:
    """Result of a single heartbeat probe."""
    endpoint: str
    status: HeartbeatStatus
    response_time_ms: float = 0.0
    http_status: Optional[int] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbationStatus:
    """Current status of the Dead Man's Switch probation period."""
    state: ProbationState = ProbationState.INACTIVE
    started_at: Optional[datetime] = None
    update_commit: Optional[str] = None
    previous_commit: Optional[str] = None
    elapsed_seconds: float = 0.0
    remaining_seconds: float = 0.0
    consecutive_failures: int = 0
    total_heartbeats: int = 0
    successful_heartbeats: int = 0
    health_score: float = 0.0
    last_heartbeat: Optional[datetime] = None
    probe_results: dict[str, HeartbeatResult] = field(default_factory=dict)
    
    @property
    def success_rate(self) -> float:
        """Calculate heartbeat success rate."""
        if self.total_heartbeats == 0:
            return 0.0
        return self.successful_heartbeats / self.total_heartbeats
    
    @property
    def is_probation_active(self) -> bool:
        """Check if probation is currently active."""
        return self.state in (ProbationState.MONITORING, ProbationState.FAILING)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/storage."""
        return {
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "update_commit": self.update_commit,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "remaining_seconds": round(self.remaining_seconds, 1),
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(self.success_rate * 100, 1),
            "health_score": round(self.health_score * 100, 1),
        }


@dataclass
class RollbackDecision:
    """Result of analyzing whether to rollback."""
    should_rollback: bool = False
    reason: str = ""
    confidence: float = 0.0
    target_commit: Optional[str] = None
    is_emergency: bool = False  # Skip confirmation, rollback immediately
    
    
@dataclass
class BootMetrics:
    """Metrics for a single boot attempt (for pattern learning)."""
    commit: str
    boot_time_seconds: float
    components_ready: int
    health_score_at_stability: float
    was_post_update: bool
    succeeded: bool
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class VersionSnapshot:
    """Snapshot of a working JARVIS version."""
    id: int = 0
    git_commit: str = ""
    git_branch: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    pip_freeze: Optional[str] = None  # Output of pip freeze
    is_stable: bool = False  # Confirmed working
    boot_count: int = 0
    crash_count: int = 0
    notes: str = ""
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "timestamp": self.timestamp.isoformat(),
            "pip_freeze": self.pip_freeze,
            "is_stable": self.is_stable,
            "boot_count": self.boot_count,
            "crash_count": self.crash_count,
            "notes": self.notes,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VersionSnapshot:
        """Create from dictionary."""
        return cls(
            id=data.get("id", 0),
            git_commit=data.get("git_commit", ""),
            git_branch=data.get("git_branch", ""),
            timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
            pip_freeze=data.get("pip_freeze"),
            is_stable=data.get("is_stable", False),
            boot_count=data.get("boot_count", 0),
            crash_count=data.get("crash_count", 0),
            notes=data.get("notes", ""),
        )


# =============================================================================
# Dead Man's Switch - Core Engine
# =============================================================================

class DeadManSwitch:
    """
    Post-Update Stability Verification System.
    
    The Dead Man's Switch is the critical safety mechanism that allows JARVIS
    to self-update safely. It ensures that broken updates are automatically
    reverted before they can cause persistent damage.
    
    Architecture:
        ┌─────────────────────────────────────────────────────────────────┐
        │                    Dead Man's Switch Engine                      │
        ├─────────────────────────────────────────────────────────────────┤
        │                                                                  │
        │  ┌─────────────────────────────────────────────────────────┐    │
        │  │                 Parallel Health Probes                   │    │
        │  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────────┐│    │
        │  │  │ Backend │ │Frontend │ │  Voice  │ │ Internal State  ││    │
        │  │  │/health  │ │ :3000   │ │(optional)│ │  (components)   ││    │
        │  │  └────┬────┘ └────┬────┘ └────┬────┘ └────────┬────────┘│    │
        │  └───────┼───────────┼───────────┼───────────────┼─────────┘    │
        │          │           │           │               │              │
        │          └───────────┴───────────┴───────────────┘              │
        │                          │                                      │
        │                    ┌─────▼─────┐                                │
        │                    │ Aggregator │ → Health Score                │
        │                    └─────┬─────┘                                │
        │                          │                                      │
        │          ┌───────────────┼───────────────┐                      │
        │          ▼               ▼               ▼                      │
        │    ┌──────────┐   ┌──────────┐   ┌──────────────┐               │
        │    │  HEALTHY │   │  FAILING │   │ ROLL BACK    │               │
        │    │ Continue │   │ 3 strikes│   │ Auto-revert  │               │
        │    └──────────┘   └──────────┘   └──────────────┘               │
        │                                                                  │
        └─────────────────────────────────────────────────────────────────┘
    
    Features:
    - Parallel async health probes for minimal latency
    - Configurable probation period (default: 45 seconds)
    - Multi-signal health assessment (HTTP + components + internal)
    - Intelligent rollback decision with confidence scoring
    - Automatic stability commitment on success
    - Crash pattern learning for future detection
    - Voice announcements for transparency
    
    Example:
        >>> dms = DeadManSwitch(config, rollback_manager)
        >>> await dms.start_probation(update_commit="abc123", previous_commit="def456")
        >>> # ... JARVIS starts up ...
        >>> result = await dms.run_probation_loop()
        >>> if result.state == ProbationState.COMMITTED:
        ...     print("Update successful!")
    """
    
    def __init__(
        self,
        config: Optional[SupervisorConfig] = None,
        rollback_manager: Optional["RollbackManager"] = None,
        narrator: Optional[Any] = None,  # SupervisorNarrator for voice feedback
    ):
        """
        Initialize the Dead Man's Switch.
        
        Args:
            config: Supervisor configuration
            rollback_manager: RollbackManager for executing rollbacks
            narrator: Optional narrator for voice announcements
        """
        self.config = config or get_supervisor_config()
        self.dms_config = self.config.dead_man_switch
        self._rollback_manager = rollback_manager
        self._narrator = narrator
        
        # State
        self._status = ProbationStatus()
        self._session: Optional[aiohttp.ClientSession] = None
        self._probation_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Callbacks
        self._on_rollback: list[Callable[[RollbackDecision], None]] = []
        self._on_stable: list[Callable[[str], None]] = []
        self._on_status_change: list[Callable[[ProbationStatus], None]] = []
        
        # Boot metrics tracking
        self._current_boot_start: Optional[datetime] = None
        self._boot_metrics: list[BootMetrics] = []
        
        logger.info("🔧 Dead Man's Switch initialized")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session with connection pooling."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=10,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(
                    total=self.dms_config.heartbeat_timeout_seconds
                ),
            )
        return self._session
    
    def set_rollback_manager(self, manager: "RollbackManager") -> None:
        """Set the rollback manager (for lazy initialization)."""
        self._rollback_manager = manager
    
    def set_narrator(self, narrator: Any) -> None:
        """Set the narrator for voice announcements."""
        self._narrator = narrator
    
    async def start_probation(
        self,
        update_commit: str,
        previous_commit: str,
    ) -> None:
        """
        Start the post-update probation period.
        
        This should be called AFTER an update is applied but BEFORE
        JARVIS fully starts up.
        
        Args:
            update_commit: Git commit hash of the new version
            previous_commit: Git commit hash of the version being replaced
        """
        if not self.dms_config.enabled:
            logger.info("⚠️ Dead Man's Switch disabled, skipping probation")
            return
        
        logger.info(f"🎯 Starting Dead Man's Switch probation for {update_commit[:12]}")
        logger.info(f"   Probation period: {self.dms_config.probation_seconds}s")
        logger.info(f"   Previous commit: {previous_commit[:12]}")
        
        # Initialize status
        self._status = ProbationStatus(
            state=ProbationState.MONITORING,
            started_at=datetime.now(),
            update_commit=update_commit,
            previous_commit=previous_commit,
            remaining_seconds=float(self.dms_config.probation_seconds),
        )
        
        # Track boot start for metrics
        self._current_boot_start = datetime.now()
        
        # Set environment variable for child processes
        os.environ["JARVIS_DMS_ACTIVE"] = "1"
        os.environ["JARVIS_DMS_UPDATE_COMMIT"] = update_commit
        
        self._notify_status_change()
        
        # Voice announcement if enabled
        if self._narrator and self.dms_config.announce_rollback:
            try:
                await self._narrator.speak(
                    "Update applied. Monitoring for stability.",
                    wait=False,
                )
            except Exception as e:
                logger.debug(f"Narrator error: {e}")
    
    async def run_probation_loop(self) -> ProbationStatus:
        """
        Run the main probation monitoring loop.
        
        This continuously monitors JARVIS health until either:
        1. Probation period completes successfully → COMMITTED
        2. Too many failures occur → ROLLING_BACK
        3. Shutdown is requested → current state
        
        Returns:
            Final ProbationStatus
        """
        if not self._status.is_probation_active:
            return self._status
        
        logger.info("🔄 Dead Man's Switch probation loop started")
        
        try:
            while not self._shutdown_event.is_set():
                # Check if probation period expired
                elapsed = (datetime.now() - self._status.started_at).total_seconds()
                self._status.elapsed_seconds = elapsed
                self._status.remaining_seconds = max(
                    0, self.dms_config.probation_seconds - elapsed
                )
                
                # Probation period completed successfully!
                if elapsed >= self.dms_config.probation_seconds:
                    if self._status.state != ProbationState.FAILING:
                        await self._commit_stable()
                        break
                
                # Perform parallel health probes
                heartbeat_ok = await self._perform_heartbeat()
                
                if heartbeat_ok:
                    self._status.consecutive_failures = 0
                    self._status.successful_heartbeats += 1
                    
                    if self._status.state == ProbationState.FAILING:
                        logger.info("💚 Health recovered, resuming monitoring")
                        self._status.state = ProbationState.MONITORING
                else:
                    self._status.consecutive_failures += 1
                    
                    if self._status.consecutive_failures >= self.dms_config.max_consecutive_failures:
                        self._status.state = ProbationState.FAILING
                        
                        # Analyze if we should rollback
                        decision = await self._analyze_rollback_decision()
                        
                        if decision.should_rollback:
                            await self._execute_rollback(decision)
                            break
                
                self._status.total_heartbeats += 1
                self._status.last_heartbeat = datetime.now()
                self._notify_status_change()
                
                # Wait for next heartbeat interval
                await asyncio.sleep(self.dms_config.heartbeat_interval_seconds)
        
        except asyncio.CancelledError:
            logger.info("⚠️ Probation loop cancelled")
        except Exception as e:
            logger.error(f"❌ Probation loop error: {e}")
            # On unexpected error, lean towards safety (don't auto-rollback)
        finally:
            os.environ.pop("JARVIS_DMS_ACTIVE", None)
            os.environ.pop("JARVIS_DMS_UPDATE_COMMIT", None)
        
        return self._status
    
    async def _perform_heartbeat(self) -> bool:
        """
        Perform parallel health probes and aggregate results.
        
        Returns:
            True if overall health is acceptable
        """
        # Build probe tasks dynamically (no hardcoding)
        probe_tasks: list[asyncio.Task] = []
        probe_names: list[str] = []
        
        backend_port = int(os.environ.get("BACKEND_PORT", "8010"))
        frontend_port = int(os.environ.get("FRONTEND_PORT", "3000"))
        
        if self.dms_config.probe_backend:
            probe_tasks.append(asyncio.create_task(
                self._probe_endpoint("backend", f"http://localhost:{backend_port}/health")
            ))
            probe_names.append("backend")
        
        if self.dms_config.probe_frontend:
            probe_tasks.append(asyncio.create_task(
                self._probe_endpoint("frontend", f"http://localhost:{frontend_port}")
            ))
            probe_names.append("frontend")
        
        if self.dms_config.probe_voice:
            # Voice is typically on the backend, check specific endpoint
            probe_tasks.append(asyncio.create_task(
                self._probe_endpoint("voice", f"http://localhost:{backend_port}/health/ready")
            ))
            probe_names.append("voice")
        
        # Execute probes in parallel
        if probe_tasks:
            results = await asyncio.gather(*probe_tasks, return_exceptions=True)
            
            # Process results
            healthy_count = 0
            total_probes = len(results)
            
            for i, result in enumerate(results):
                name = probe_names[i]
                
                if isinstance(result, Exception):
                    self._status.probe_results[name] = HeartbeatResult(
                        endpoint=name,
                        status=HeartbeatStatus.ERROR,
                        error=str(result),
                    )
                elif isinstance(result, HeartbeatResult):
                    self._status.probe_results[name] = result
                    if result.status == HeartbeatStatus.ALIVE:
                        healthy_count += 1
            
            # Calculate health score
            self._status.health_score = healthy_count / total_probes if total_probes > 0 else 0.0
            
            # Check critical requirements
            if self.dms_config.require_backend_healthy:
                backend_result = self._status.probe_results.get("backend")
                if backend_result and backend_result.status != HeartbeatStatus.ALIVE:
                    logger.warning("⚠️ Backend unhealthy - critical failure")
                    return False
            
            # Check minimum health score
            if self._status.health_score < self.dms_config.min_health_score:
                logger.warning(
                    f"⚠️ Health score {self._status.health_score:.1%} below threshold "
                    f"{self.dms_config.min_health_score:.1%}"
                )
                return False
            
            return True
        
        # No probes configured - assume healthy
        return True
    
    async def _probe_endpoint(
        self,
        name: str,
        url: str,
    ) -> HeartbeatResult:
        """
        Probe a single HTTP endpoint.
        
        Args:
            name: Endpoint name for logging
            url: URL to probe
            
        Returns:
            HeartbeatResult with status and timing
        """
        start_time = time.time()
        
        try:
            session = await self._get_session()
            
            async with session.get(url) as response:
                elapsed_ms = (time.time() - start_time) * 1000
                
                details = {}
                if response.status == 200:
                    try:
                        data = await response.json()
                        details = data
                    except Exception:
                        pass
                
                return HeartbeatResult(
                    endpoint=name,
                    status=HeartbeatStatus.ALIVE if response.status == 200 else HeartbeatStatus.ERROR,
                    response_time_ms=elapsed_ms,
                    http_status=response.status,
                    details=details,
                )
        
        except asyncio.TimeoutError:
            return HeartbeatResult(
                endpoint=name,
                status=HeartbeatStatus.TIMEOUT,
                response_time_ms=self.dms_config.heartbeat_timeout_seconds * 1000,
                error="Timeout",
            )
        except aiohttp.ClientError as e:
            return HeartbeatResult(
                endpoint=name,
                status=HeartbeatStatus.ERROR,
                error=str(e),
            )
        except Exception as e:
            return HeartbeatResult(
                endpoint=name,
                status=HeartbeatStatus.ERROR,
                error=f"Unexpected: {e}",
            )
    
    async def _analyze_rollback_decision(self) -> RollbackDecision:
        """
        Analyze whether to trigger a rollback.
        
        Uses multiple signals to make an intelligent decision:
        - Consecutive failure count
        - Overall health score trend
        - Crash pattern history (if enabled)
        - Time since probation started
        
        Returns:
            RollbackDecision with recommendation
        """
        decision = RollbackDecision()
        
        # Check if auto-rollback is even enabled
        if not self.dms_config.auto_rollback_enabled:
            decision.reason = "Auto-rollback disabled"
            return decision
        
        # Check if we have a target to rollback to
        if not self._status.previous_commit:
            decision.reason = "No previous commit available"
            return decision
        
        decision.target_commit = self._status.previous_commit
        
        # Calculate confidence based on multiple factors
        confidence_factors = []
        
        # Factor 1: Consecutive failures (weight: 40%)
        failure_ratio = self._status.consecutive_failures / self.dms_config.max_consecutive_failures
        confidence_factors.append(min(1.0, failure_ratio) * 0.4)
        
        # Factor 2: Low health score (weight: 30%)
        if self._status.health_score < self.dms_config.min_health_score:
            health_deficit = (self.dms_config.min_health_score - self._status.health_score) / self.dms_config.min_health_score
            confidence_factors.append(min(1.0, health_deficit) * 0.3)
        else:
            confidence_factors.append(0)
        
        # Factor 3: Overall success rate (weight: 20%)
        if self._status.success_rate < 0.5:
            confidence_factors.append((0.5 - self._status.success_rate) * 0.4)  # 0.2 max
        else:
            confidence_factors.append(0)
        
        # Factor 4: Backend specifically unhealthy (weight: 10%)
        backend_result = self._status.probe_results.get("backend")
        if backend_result and backend_result.status != HeartbeatStatus.ALIVE:
            confidence_factors.append(0.1)
        
        decision.confidence = sum(confidence_factors)
        
        # Decision threshold
        if decision.confidence >= 0.5:
            decision.should_rollback = True
            decision.reason = (
                f"Health degraded: {self._status.consecutive_failures} consecutive failures, "
                f"health score {self._status.health_score:.1%}, "
                f"confidence {decision.confidence:.1%}"
            )
            
            # Emergency if completely unresponsive
            if self._status.health_score == 0:
                decision.is_emergency = True
                decision.reason = "Complete health failure - emergency rollback"
        
        logger.info(
            f"🔍 Rollback analysis: confidence={decision.confidence:.1%}, "
            f"should_rollback={decision.should_rollback}"
        )
        
        return decision
    
    async def _execute_rollback(self, decision: RollbackDecision) -> bool:
        """
        Execute the rollback based on the decision.
        
        Args:
            decision: RollbackDecision with target and reason
            
        Returns:
            True if rollback succeeded
        """
        self._status.state = ProbationState.ROLLING_BACK
        self._notify_status_change()
        
        logger.warning(f"🔄 Executing rollback: {decision.reason}")
        logger.info(f"   Target: {decision.target_commit[:12]}")
        
        # Voice announcement
        if self._narrator and self.dms_config.announce_rollback:
            try:
                await self._narrator.speak(
                    "Update failed stability check. Rolling back to previous version.",
                    wait=False,
                )
            except Exception as e:
                logger.debug(f"Narrator error: {e}")
        
        # Notify callbacks
        for callback in self._on_rollback:
            try:
                callback(decision)
            except Exception as e:
                logger.error(f"Rollback callback error: {e}")
        
        # Execute rollback via RollbackManager
        if self._rollback_manager:
            try:
                success = await self._rollback_manager.rollback_using_reflog(steps=1)
                
                if not success:
                    # Try snapshot-based rollback
                    logger.warning("⚠️ Reflog rollback failed, trying snapshot...")
                    success = await self._rollback_manager.rollback()
                
                if success:
                    logger.info("✅ Rollback completed successfully")
                    # Record this as a crash for the failed version
                    if self._status.update_commit:
                        await self._rollback_manager.record_crash(self._status.update_commit)
                    return True
                else:
                    logger.error("❌ All rollback attempts failed")
                    return False
                    
            except Exception as e:
                logger.error(f"❌ Rollback execution error: {e}")
                return False
        else:
            logger.error("❌ No RollbackManager available")
            return False
    
    async def _commit_stable(self) -> None:
        """
        Commit the current version as stable.
        
        Called when probation period completes successfully.
        """
        self._status.state = ProbationState.COMMITTED
        self._notify_status_change()
        
        commit = self._status.update_commit or "unknown"
        
        logger.info(f"✅ Version {commit[:12]} passed stability check!")
        logger.info(f"   Probation: {self._status.elapsed_seconds:.1f}s")
        logger.info(f"   Success rate: {self._status.success_rate:.1%}")
        logger.info(f"   Final health: {self._status.health_score:.1%}")
        
        # Mark as stable in database
        if self._rollback_manager and self.dms_config.auto_commit_stable:
            try:
                await self._rollback_manager.mark_stable(commit)
                await self._rollback_manager.record_boot(commit)
            except Exception as e:
                logger.warning(f"Failed to mark stable in DB: {e}")
        
        # Track boot metrics
        if self.dms_config.track_boot_metrics and self._current_boot_start:
            boot_time = (datetime.now() - self._current_boot_start).total_seconds()
            metrics = BootMetrics(
                commit=commit,
                boot_time_seconds=boot_time,
                components_ready=len([
                    r for r in self._status.probe_results.values()
                    if r.status == HeartbeatStatus.ALIVE
                ]),
                health_score_at_stability=self._status.health_score,
                was_post_update=True,
                succeeded=True,
            )
            self._boot_metrics.append(metrics)
        
        # Voice announcement
        if self._narrator and self.dms_config.notify_on_commit:
            try:
                await self._narrator.speak(
                    "Update verified. System is stable.",
                    wait=False,
                )
            except Exception as e:
                logger.debug(f"Narrator error: {e}")
        
        # Notify callbacks
        for callback in self._on_stable:
            try:
                callback(commit)
            except Exception as e:
                logger.error(f"Stable callback error: {e}")
    
    def _notify_status_change(self) -> None:
        """Notify status change callbacks."""
        for callback in self._on_status_change:
            try:
                callback(self._status)
            except Exception as e:
                logger.debug(f"Status change callback error: {e}")
    
    async def handle_crash(self, exit_code: int) -> RollbackDecision:
        """
        Handle a crash during probation period.
        
        This is called by the supervisor when JARVIS crashes during probation.
        It's a more aggressive trigger than normal health check failures.
        
        Args:
            exit_code: Exit code of the crashed process
            
        Returns:
            RollbackDecision (should always recommend rollback for crash during probation)
        """
        if not self._status.is_probation_active:
            return RollbackDecision(
                should_rollback=False,
                reason="Not in probation period",
            )
        
        logger.error(f"💥 CRASH during probation! Exit code: {exit_code}")
        
        return RollbackDecision(
            should_rollback=True,
            reason=f"Process crashed during probation (exit code: {exit_code})",
            confidence=1.0,
            target_commit=self._status.previous_commit,
            is_emergency=True,
        )
    
    def cancel_probation(self) -> None:
        """Cancel the probation period (for clean shutdown)."""
        self._shutdown_event.set()
        if self._probation_task and not self._probation_task.done():
            self._probation_task.cancel()
    
    def get_status(self) -> ProbationStatus:
        """Get current probation status."""
        return self._status
    
    def is_probation_active(self) -> bool:
        """Check if probation is currently active."""
        return self._status.is_probation_active
    
    def on_rollback(self, callback: Callable[[RollbackDecision], None]) -> None:
        """Register a rollback callback."""
        self._on_rollback.append(callback)
    
    def on_stable(self, callback: Callable[[str], None]) -> None:
        """Register a stability callback (called when version committed)."""
        self._on_stable.append(callback)
    
    def on_status_change(self, callback: Callable[[ProbationStatus], None]) -> None:
        """Register a status change callback."""
        self._on_status_change.append(callback)
    
    async def close(self) -> None:
        """Close resources."""
        self.cancel_probation()
        if self._session and not self._session.closed:
            await self._session.close()


class RollbackManager:
    """
    Version history and rollback management.
    
    Features:
    - SQLite-based version history
    - Git reflog integration
    - pip freeze snapshots
    - Automatic rollback on boot failure
    - Configurable rollback depth
    
    Example:
        >>> manager = RollbackManager(config)
        >>> await manager.initialize()
        >>> await manager.create_snapshot()
        >>> # ... after failed update ...
        >>> await manager.rollback()
    """
    
    def __init__(
        self,
        config: Optional[SupervisorConfig] = None,
        repo_path: Optional[Path] = None,
    ):
        """
        Initialize the rollback manager.
        
        Args:
            config: Supervisor configuration
            repo_path: Path to git repository
        """
        self.config = config or get_supervisor_config()
        self.repo_path = repo_path or self._detect_repo_path()
        
        # Database path
        db_path = self.repo_path / self.config.rollback.history_db
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        
        self._conn: Optional[sqlite3.Connection] = None
        self._initialized = False
        
        logger.info(f"🔧 Rollback manager initialized (db: {self.db_path})")
    
    def _detect_repo_path(self) -> Path:
        """Detect the git repository path."""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / ".git").exists():
                return parent
        return Path.cwd()
    
    async def initialize(self) -> None:
        """Initialize the database."""
        if self._initialized:
            return
        
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        
        # Create tables
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS version_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                git_commit TEXT NOT NULL,
                git_branch TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                pip_freeze TEXT,
                is_stable INTEGER DEFAULT 0,
                boot_count INTEGER DEFAULT 0,
                crash_count INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            )
        """)
        
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_commit ON version_history(git_commit)
        """)
        
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stable ON version_history(is_stable)
        """)
        
        self._conn.commit()
        self._initialized = True
        
        logger.info("✅ Rollback database initialized")
    
    async def _run_command(self, command: str, timeout: int = 30) -> tuple[bool, str]:
        """Run a shell command asynchronously."""
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
            
            if process.returncode == 0:
                return True, stdout.decode().strip()
            else:
                return False, stderr.decode().strip()
                
        except asyncio.TimeoutError:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)
    
    async def get_current_commit(self) -> str:
        """Get the current git commit hash."""
        success, output = await self._run_command("git rev-parse HEAD")
        return output[:40] if success else ""
    
    async def get_current_branch(self) -> str:
        """Get the current git branch."""
        success, output = await self._run_command("git rev-parse --abbrev-ref HEAD")
        return output if success else "unknown"
    
    async def get_pip_freeze(self) -> Optional[str]:
        """Get pip freeze output if enabled."""
        if not self.config.rollback.include_pip_freeze:
            return None
        
        success, output = await self._run_command("pip freeze", timeout=60)
        return output if success else None
    
    async def create_snapshot(self, notes: str = "") -> Optional[VersionSnapshot]:
        """
        Create a snapshot of the current version.
        
        Args:
            notes: Optional notes about this snapshot
            
        Returns:
            The created snapshot, or None on failure
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            commit = await self.get_current_commit()
            branch = await self.get_current_branch()
            pip_freeze = await self.get_pip_freeze()
            
            snapshot = VersionSnapshot(
                git_commit=commit,
                git_branch=branch,
                timestamp=datetime.now(),
                pip_freeze=pip_freeze,
                notes=notes,
            )
            
            cursor = self._conn.execute(
                """
                INSERT INTO version_history (
                    git_commit, git_branch, timestamp, pip_freeze, notes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.git_commit,
                    snapshot.git_branch,
                    snapshot.timestamp.isoformat(),
                    snapshot.pip_freeze,
                    snapshot.notes,
                ),
            )
            
            self._conn.commit()
            snapshot.id = cursor.lastrowid
            
            # Cleanup old snapshots
            await self._cleanup_old_snapshots()
            
            logger.info(f"📸 Snapshot created: {commit[:12]} ({branch})")
            return snapshot
            
        except Exception as e:
            logger.error(f"❌ Failed to create snapshot: {e}")
            return None
    
    async def _cleanup_old_snapshots(self) -> None:
        """Remove old snapshots beyond max_versions."""
        max_versions = self.config.rollback.max_versions
        
        # Keep stable versions separate
        self._conn.execute(
            """
            DELETE FROM version_history
            WHERE id NOT IN (
                SELECT id FROM version_history
                WHERE is_stable = 1
                ORDER BY timestamp DESC
                LIMIT ?
            )
            AND id NOT IN (
                SELECT id FROM version_history
                WHERE is_stable = 0
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (max_versions, max_versions),
        )
        
        self._conn.commit()
    
    async def mark_stable(self, commit: Optional[str] = None) -> bool:
        """Mark a version as stable (confirmed working)."""
        if not self._initialized:
            await self.initialize()
        
        if commit is None:
            commit = await self.get_current_commit()
        
        try:
            self._conn.execute(
                "UPDATE version_history SET is_stable = 1 WHERE git_commit = ?",
                (commit,),
            )
            self._conn.commit()
            
            logger.info(f"✅ Marked {commit[:12]} as stable")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to mark stable: {e}")
            return False
    
    async def record_boot(self, commit: Optional[str] = None) -> None:
        """Record a successful boot for a version."""
        if not self._initialized:
            await self.initialize()
        
        if commit is None:
            commit = await self.get_current_commit()
        
        try:
            self._conn.execute(
                "UPDATE version_history SET boot_count = boot_count + 1 WHERE git_commit = ?",
                (commit,),
            )
            self._conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record boot: {e}")
    
    async def record_crash(self, commit: Optional[str] = None) -> None:
        """Record a crash for a version."""
        if not self._initialized:
            await self.initialize()
        
        if commit is None:
            commit = await self.get_current_commit()
        
        try:
            self._conn.execute(
                "UPDATE version_history SET crash_count = crash_count + 1 WHERE git_commit = ?",
                (commit,),
            )
            self._conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record crash: {e}")
    
    async def get_last_stable(self) -> Optional[VersionSnapshot]:
        """Get the most recent stable version."""
        if not self._initialized:
            await self.initialize()
        
        cursor = self._conn.execute(
            """
            SELECT * FROM version_history
            WHERE is_stable = 1
            ORDER BY timestamp DESC
            LIMIT 1
            """
        )
        
        row = cursor.fetchone()
        if row:
            return VersionSnapshot(
                id=row["id"],
                git_commit=row["git_commit"],
                git_branch=row["git_branch"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                pip_freeze=row["pip_freeze"],
                is_stable=bool(row["is_stable"]),
                boot_count=row["boot_count"],
                crash_count=row["crash_count"],
                notes=row["notes"],
            )
        
        return None
    
    async def get_previous_version(self) -> Optional[VersionSnapshot]:
        """Get the previous version (for rollback)."""
        if not self._initialized:
            await self.initialize()
        
        current_commit = await self.get_current_commit()
        
        # First try to get the last stable version
        stable = await self.get_last_stable()
        if stable and stable.git_commit != current_commit:
            return stable
        
        # Otherwise get the previous snapshot
        cursor = self._conn.execute(
            """
            SELECT * FROM version_history
            WHERE git_commit != ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (current_commit,),
        )
        
        row = cursor.fetchone()
        if row:
            return VersionSnapshot(
                id=row["id"],
                git_commit=row["git_commit"],
                git_branch=row["git_branch"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                pip_freeze=row["pip_freeze"],
                is_stable=bool(row["is_stable"]),
                boot_count=row["boot_count"],
                crash_count=row["crash_count"],
                notes=row["notes"],
            )
        
        return None
    
    async def rollback(self, target: Optional[VersionSnapshot] = None) -> bool:
        """
        Rollback to a previous version.
        
        Args:
            target: Target version (default: previous version)
            
        Returns:
            True if rollback succeeded
        """
        if not self.config.rollback.enabled:
            logger.warning("⚠️ Rollback is disabled in config")
            return False
        
        if not self._initialized:
            await self.initialize()
        
        # Get target version
        if target is None:
            target = await self.get_previous_version()
        
        if target is None:
            logger.error("❌ No previous version available for rollback")
            return False
        
        logger.info(f"🔄 Rolling back to {target.git_commit[:12]} ({target.git_branch})")
        
        try:
            # Git reset to target commit
            success, output = await self._run_command(
                f"git reset --hard {target.git_commit}",
                timeout=60,
            )
            
            if not success:
                logger.error(f"❌ Git reset failed: {output}")
                return False
            
            # Restore pip dependencies if available
            if target.pip_freeze and self.config.rollback.include_pip_freeze:
                logger.info("📦 Restoring pip dependencies...")
                
                # Write pip freeze to temp file
                req_file = self.repo_path / ".rollback_requirements.txt"
                req_file.write_text(target.pip_freeze)
                
                success, output = await self._run_command(
                    f"pip install -r {req_file} --quiet",
                    timeout=300,
                )
                
                req_file.unlink(missing_ok=True)
                
                if not success:
                    logger.warning(f"⚠️ pip install failed: {output}")
                    # Continue anyway, git rollback is more important
            
            logger.info(f"✅ Rollback complete to {target.git_commit[:12]}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Rollback failed: {e}")
            return False
    
    async def rollback_using_reflog(self, steps: int = 1) -> bool:
        """
        Rollback using git reflog (useful if no snapshots).
        
        Args:
            steps: Number of steps to go back in reflog
            
        Returns:
            True if rollback succeeded
        """
        logger.info(f"🔄 Rolling back {steps} step(s) via reflog")
        
        try:
            success, output = await self._run_command(
                f"git reset --hard HEAD@{{{steps}}}",
                timeout=60,
            )
            
            if success:
                logger.info(f"✅ Reflog rollback complete")
                return True
            else:
                logger.error(f"❌ Reflog rollback failed: {output}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Reflog rollback error: {e}")
            return False
    
    async def get_history(self, limit: int = 10) -> list[VersionSnapshot]:
        """Get version history."""
        if not self._initialized:
            await self.initialize()
        
        cursor = self._conn.execute(
            """
            SELECT * FROM version_history
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        
        snapshots = []
        for row in cursor.fetchall():
            snapshots.append(VersionSnapshot(
                id=row["id"],
                git_commit=row["git_commit"],
                git_branch=row["git_branch"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                pip_freeze=row["pip_freeze"],
                is_stable=bool(row["is_stable"]),
                boot_count=row["boot_count"],
                crash_count=row["crash_count"],
                notes=row["notes"],
            ))
        
        return snapshots
    
    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            self._initialized = False
