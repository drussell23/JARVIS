"""
Reactor-Core API Client - The "Ignition Key"
==============================================

This module provides the critical connection between JARVIS and Reactor-Core,
enabling programmatic training triggers and unified learning coordination.

Features:
- Async HTTP client using aiohttp
- Health monitoring with auto-reconnection
- Training trigger with priority and scheduling
- Pipeline status monitoring
- Experience log streaming
- Cross-repo event integration
- Robust error handling (never crashes JARVIS)

Architecture:
    ┌────────────────────────────────────────────────────────────────┐
    │                     JARVIS-AI-Agent                            │
    │  ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐  │
    │  │   Agentic    │ → │  Reactor-Core    │ → │   Training    │  │
    │  │   Runner     │   │  Client          │   │   Trigger     │  │
    │  └──────────────┘   └────────┬─────────┘   └───────────────┘  │
    │                              │                                 │
    │                    ┌─────────▼─────────┐                       │
    │                    │  Cross-Repo       │                       │
    │                    │  Event Bridge     │                       │
    │                    └───────────────────┘                       │
    └────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                     Reactor-Core                               │
    │  ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐  │
    │  │   Training   │ ← │  Night Shift     │ ← │   API         │  │
    │  │   Pipeline   │   │  Orchestrator    │   │   Endpoints   │  │
    │  └──────────────┘   └──────────────────┘   └───────────────┘  │
    └────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0 (Ignition Key)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class ReactorCoreConfig:
    """Configuration for Reactor-Core client."""

    # API Configuration
    api_url: str = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_API_URL", "http://localhost:8090")
    )
    api_timeout: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_TIMEOUT", "30.0"))
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_API_KEY", "")
    )

    # Connection Settings
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_MAX_RETRIES", "3"))
    )
    retry_delay: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_RETRY_DELAY", "1.0"))
    )
    connection_pool_size: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_POOL_SIZE", "10"))
    )

    # Health Check Settings - v2.0 Adaptive Health System
    health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_HEALTH_INTERVAL", "60.0"))
    )
    health_check_timeout: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_HEALTH_TIMEOUT", "10.0"))  # v2.0: Increased from 5s
    )
    # v2.0: Adaptive timeout range
    health_check_timeout_min: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_HEALTH_TIMEOUT_MIN", "5.0"))
    )
    health_check_timeout_max: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_HEALTH_TIMEOUT_MAX", "30.0"))
    )
    # v2.0: Startup grace period
    startup_grace_period: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_STARTUP_GRACE", "120.0"))
    )
    # v2.0: Hysteresis thresholds
    recovery_threshold: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_RECOVERY_THRESHOLD", "3"))
    )
    offline_threshold: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_OFFLINE_THRESHOLD", "3"))
    )

    # Training Trigger Settings
    auto_trigger_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_AUTO_TRIGGER", "true").lower() == "true"
    )
    experience_threshold: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_EXP_THRESHOLD", "100"))
    )
    min_trigger_interval_hours: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_MIN_INTERVAL_HOURS", "6.0"))
    )

    # Cross-Repo Bridge
    bridge_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_BRIDGE_ENABLED", "true").lower() == "true"
    )


class TrainingPriority(str, Enum):
    """Priority levels for training triggers."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class PipelineStage(str, Enum):
    """Pipeline stages for status monitoring."""
    IDLE = "idle"
    SCOUTING = "scouting"
    INGESTING = "ingesting"
    FORMATTING = "formatting"
    DISTILLING = "distilling"
    TRAINING = "training"
    EVALUATING = "evaluating"
    QUANTIZING = "quantizing"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TrainingJob:
    """Represents a training job."""
    job_id: str
    status: str
    stage: PipelineStage
    progress: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    experience_count: int = 0
    priority: TrainingPriority = TrainingPriority.NORMAL
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage.value,
            "progress": self.progress,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "experience_count": self.experience_count,
            "priority": self.priority.value,
            "error": self.error,
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrainingJob":
        return cls(
            job_id=data["job_id"],
            status=data["status"],
            stage=PipelineStage(data.get("stage", "idle")),
            progress=data.get("progress", 0.0),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            experience_count=data.get("experience_count", 0),
            priority=TrainingPriority(data.get("priority", "normal")),
            error=data.get("error"),
            metrics=data.get("metrics", {}),
        )


# =============================================================================
# Reactor-Core Client
# =============================================================================

class ReactorCoreClient:
    """
    Async client for Reactor-Core API.

    This is the "Ignition Key" that connects JARVIS to the training pipeline,
    enabling autonomous continuous learning.

    Usage:
        client = ReactorCoreClient()
        await client.initialize()

        if await client.health_check():
            job = await client.trigger_training(
                experience_count=150,
                priority=TrainingPriority.NORMAL,
            )
            print(f"Training triggered: {job.job_id}")

        await client.close()
    """

    def __init__(self, config: Optional[ReactorCoreConfig] = None):
        """
        Initialize Reactor-Core client.

        Args:
            config: Client configuration (uses defaults if not provided)
        """
        self.config = config or ReactorCoreConfig()
        self._session: Optional[Any] = None  # aiohttp.ClientSession
        self._initialized = False
        self._is_online = False
        self._last_health_check: Optional[datetime] = None
        self._last_trigger_time: Optional[datetime] = None
        self._health_check_task: Optional[asyncio.Task] = None

        # Metrics
        self._requests_made = 0
        self._requests_failed = 0
        self._training_triggers = 0
        self._training_completions = 0

        # Event callbacks
        self._on_training_started: List[Callable] = []
        self._on_training_completed: List[Callable] = []
        self._on_training_failed: List[Callable] = []
        self._on_connection_lost: List[Callable] = []
        self._on_connection_restored: List[Callable] = []

        # v2.0: Adaptive Health System State
        self._current_timeout: float = self.config.health_check_timeout
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._in_startup_grace: bool = True
        self._startup_time: Optional[datetime] = None
        self._last_failure_reason: Optional[str] = None
        self._health_check_interval_multiplier: float = 1.0

        # v2.1: Training readiness state (enriched health monitoring)
        self._training_ready: bool = False
        self._reactor_phase: str = "unknown"
        self._trinity_connected: bool = False
        self._active_job_id: Optional[str] = None

        # v2.1: Training circuit breaker - prevents repeated triggers after failures
        try:
            from backend.kernel.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
            training_cb_config = CircuitBreakerConfig(
                failure_threshold=int(os.getenv("REACTOR_CB_FAILURE_THRESHOLD", "3")),
                recovery_timeout_seconds=float(os.getenv("REACTOR_CB_RECOVERY_TIMEOUT", "3600")),
                half_open_max_requests=1,
                success_threshold=1,
                name="reactor_training",
            )
            self._training_circuit_breaker: Optional[Any] = CircuitBreaker(
                name="reactor_training", config=training_cb_config
            )
        except ImportError:
            logger.warning("[ReactorClient] CircuitBreaker not available, training CB disabled")
            self._training_circuit_breaker = None

    async def initialize(self) -> bool:
        """
        Initialize the client and establish connection.

        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        try:
            # Import aiohttp
            try:
                import aiohttp
            except ImportError:
                logger.error("[ReactorClient] aiohttp not installed. Install with: pip install aiohttp")
                return False

            # Create session with connection pooling
            connector = aiohttp.TCPConnector(
                limit=self.config.connection_pool_size,
                limit_per_host=self.config.connection_pool_size,
            )
            timeout = aiohttp.ClientTimeout(
                total=self.config.api_timeout,
                connect=10.0,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )

            # v2.0: Record startup time for grace period
            self._startup_time = datetime.now()
            self._in_startup_grace = True

            # Initial health check (don't fail init if Reactor-Core is down)
            self._is_online = await self.health_check()

            # Start background health monitor
            if self.config.health_check_interval > 0:
                self._health_check_task = asyncio.create_task(self._health_monitor_loop())

            self._initialized = True
            logger.info(f"[ReactorClient] Initialized (online={self._is_online}, url={self.config.api_url})")

            return True

        except Exception as e:
            logger.error(f"[ReactorClient] Initialization failed: {e}")
            return False

    async def close(self) -> None:
        """Close the client and cleanup resources."""
        logger.info("[ReactorClient] Shutting down...")

        # Cancel health check task
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Close session
        if self._session:
            await self._session.close()
            self._session = None

        self._initialized = False
        self._is_online = False

        logger.info("[ReactorClient] Shutdown complete")

    # =========================================================================
    # Health & Status
    # =========================================================================

    async def health_check(self) -> bool:
        """
        v2.0: Adaptive health check with intelligent timeout and grace period.

        Features:
        - Adaptive timeout that scales based on failures
        - Startup grace period (doesn't log warnings during warmup)
        - Hysteresis for going online/offline
        - Classifies failure reasons

        Returns:
            True if Reactor-Core is online and responding
        """
        if not self._session:
            self._last_failure_reason = "no_session"
            return False

        try:
            import aiohttp

            # v2.0: Use adaptive timeout
            adaptive_timeout = aiohttp.ClientTimeout(total=self._current_timeout)

            async with self._session.get(
                f"{self.config.api_url}/health",
                timeout=adaptive_timeout,
            ) as response:
                if response.status == 200:
                    # v2.1: Parse training readiness from response body
                    try:
                        health_data = await response.json()
                        prev_phase = self._reactor_phase
                        self._training_ready = health_data.get("training_ready", False)
                        self._reactor_phase = health_data.get("phase", "unknown")
                        self._trinity_connected = health_data.get("trinity_connected", False)
                        # Log phase transitions
                        if prev_phase != self._reactor_phase:
                            logger.info(
                                f"[ReactorClient] Reactor-Core phase: {prev_phase} -> {self._reactor_phase} "
                                f"(training_ready={self._training_ready})"
                            )
                    except Exception:
                        pass  # Don't fail health check if JSON parsing fails

                    self._last_health_check = datetime.now()
                    self._consecutive_failures = 0
                    self._consecutive_successes += 1
                    self._last_failure_reason = None

                    # v2.0: Gradually reduce timeout on success (with floor)
                    self._current_timeout = max(
                        self.config.health_check_timeout_min,
                        self._current_timeout * 0.9
                    )

                    # v2.0: Reduce interval multiplier on success
                    self._health_check_interval_multiplier = max(
                        1.0,
                        self._health_check_interval_multiplier * 0.8
                    )

                    # v2.0: Require multiple successes before going online (hysteresis)
                    was_offline = not self._is_online
                    if was_offline:
                        if self._consecutive_successes >= self.config.recovery_threshold:
                            self._is_online = True
                            logger.info(
                                f"[ReactorClient] Reactor-Core is now ONLINE "
                                f"(after {self._consecutive_successes} successful checks)"
                            )
                            await self._emit_event("connection_restored")
                        else:
                            logger.debug(
                                f"[ReactorClient] Recovery in progress "
                                f"({self._consecutive_successes}/{self.config.recovery_threshold})"
                            )
                    else:
                        self._is_online = True

                    return True
                else:
                    self._consecutive_failures += 1
                    self._consecutive_successes = 0
                    self._last_failure_reason = f"http_{response.status}"

                    # v2.0: Only go offline after threshold failures
                    if self._is_online and self._consecutive_failures >= self.config.offline_threshold:
                        if not self._is_in_startup_grace():
                            self._is_online = False
                            logger.warning(
                                f"[ReactorClient] Reactor-Core went OFFLINE: "
                                f"HTTP {response.status} ({self._consecutive_failures} failures)"
                            )
                            await self._emit_event("connection_lost", {"error": f"HTTP {response.status}"})

                    return False

        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_reason = "timeout"

            # v2.0: Increase timeout for next attempt (with ceiling)
            self._current_timeout = min(
                self.config.health_check_timeout_max,
                self._current_timeout * 1.5
            )

            # v2.0: Only log warnings outside startup grace
            if self._is_online and self._consecutive_failures >= self.config.offline_threshold:
                if not self._is_in_startup_grace():
                    self._is_online = False
                    logger.warning(
                        f"[ReactorClient] Reactor-Core went OFFLINE: "
                        f"timeout after {self._current_timeout:.1f}s "
                        f"({self._consecutive_failures} failures)"
                    )
                    await self._emit_event("connection_lost", {"error": "timeout"})
            elif not self._is_in_startup_grace() and self._consecutive_failures > 0:
                logger.debug(
                    f"[ReactorClient] Health check timeout "
                    f"(attempt {self._consecutive_failures}, next timeout: {self._current_timeout:.1f}s)"
                )

            return False

        except Exception as e:
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_reason = f"error:{type(e).__name__}"

            # v2.0: Only go offline after threshold failures
            if self._is_online and self._consecutive_failures >= self.config.offline_threshold:
                if not self._is_in_startup_grace():
                    self._is_online = False
                    logger.warning(f"[ReactorClient] Reactor-Core went OFFLINE: {e}")
                    await self._emit_event("connection_lost", {"error": str(e)})
            elif not self._is_in_startup_grace() and self._consecutive_failures > 0:
                logger.debug(f"[ReactorClient] Health check error: {e}")

            return False

    def _is_in_startup_grace(self) -> bool:
        """v2.0: Check if we're still in the startup grace period."""
        if not self._in_startup_grace:
            return False

        if self._startup_time is None:
            return True

        elapsed = (datetime.now() - self._startup_time).total_seconds()
        if elapsed >= self.config.startup_grace_period:
            self._in_startup_grace = False
            logger.debug(f"[ReactorClient] Startup grace period ended after {elapsed:.1f}s")
            return False

        return True

    @property
    def is_online(self) -> bool:
        """Check if Reactor-Core is currently online."""
        return self._is_online

    @property
    def is_training_ready(self) -> bool:
        """Whether Reactor-Core is online AND training subsystem is ready."""
        return self._is_online and self._training_ready

    @property
    def is_initialized(self) -> bool:
        """Check if client is initialized."""
        return self._initialized

    async def get_status(self) -> Dict[str, Any]:
        """
        Get Reactor-Core status including pipeline state.

        Returns:
            Status dictionary or empty dict if offline
        """
        if not self._is_online:
            return {}

        try:
            data = await self._request("GET", "/api/status")
            return data or {}
        except Exception as e:
            logger.warning(f"[ReactorClient] Failed to get status: {e}")
            return {}

    async def get_pipeline_state(self) -> Optional[Dict[str, Any]]:
        """
        Get current pipeline execution state.

        Returns:
            Pipeline state dictionary or None
        """
        if not self._is_online:
            return None

        try:
            data = await self._request("GET", "/api/v1/pipeline/state")
            return data
        except Exception as e:
            logger.warning(f"[ReactorClient] Failed to get pipeline state: {e}")
            return None

    # =========================================================================
    # Training Triggers
    # =========================================================================

    async def trigger_training(
        self,
        experience_count: int = 0,
        priority: TrainingPriority = TrainingPriority.NORMAL,
        sources: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[TrainingJob]:
        """
        Trigger a training run in Reactor-Core.

        Args:
            experience_count: Number of experiences to train on
            priority: Training priority level
            sources: Data sources to use (default: ["jarvis_experience", "scout"])
            metadata: Additional metadata for the training run
            force: Force trigger even if minimum interval hasn't passed

        Returns:
            TrainingJob if triggered successfully, None otherwise
        """
        if not self._is_online:
            logger.warning("[ReactorClient] Cannot trigger training - Reactor-Core is offline")
            return None

        # Check minimum trigger interval
        if not force and self._last_trigger_time:
            min_interval = timedelta(hours=self.config.min_trigger_interval_hours)
            if datetime.now() - self._last_trigger_time < min_interval:
                logger.info(
                    f"[ReactorClient] Skipping trigger - minimum interval not reached "
                    f"(last trigger: {self._last_trigger_time})"
                )
                return None

        try:
            payload = {
                "experience_count": experience_count,
                "priority": priority.value,
                "sources": sources or ["jarvis_experience", "scout"],
                "metadata": metadata or {},
                "triggered_by": "jarvis_agentic_runner",
                "trigger_time": datetime.now().isoformat(),
            }

            data = await self._request("POST", "/api/v1/train", json=payload)

            if data and "job_id" in data:
                job = TrainingJob(
                    job_id=data["job_id"],
                    status=data.get("status", "queued"),
                    stage=PipelineStage(data.get("stage", "idle")),
                    experience_count=experience_count,
                    priority=priority,
                )

                self._last_trigger_time = datetime.now()
                self._training_triggers += 1

                logger.info(
                    f"[ReactorClient] Training triggered: job_id={job.job_id}, "
                    f"experiences={experience_count}, priority={priority.value}"
                )

                # Emit event
                await self._emit_event("training_started", job.to_dict())

                # Write to cross-repo bridge
                await self._write_bridge_event("training_triggered", job.to_dict())

                return job
            else:
                logger.warning(f"[ReactorClient] Training trigger failed: {data}")
                return None

        except Exception as e:
            self._requests_failed += 1
            logger.error(f"[ReactorClient] Training trigger error: {e}")
            return None

    async def cancel_training(self, job_id: str) -> bool:
        """
        Cancel a running training job.

        Args:
            job_id: ID of the job to cancel

        Returns:
            True if cancelled successfully
        """
        if not self._is_online:
            return False

        try:
            data = await self._request("POST", f"/api/v1/training/cancel/{job_id}")
            return data.get("cancelled", False) if data else False
        except Exception as e:
            logger.error(f"[ReactorClient] Cancel training error: {e}")
            return False

    async def get_training_job(self, job_id: str) -> Optional[TrainingJob]:
        """
        Get status of a training job.

        Args:
            job_id: ID of the job to query

        Returns:
            TrainingJob if found, None otherwise
        """
        if not self._is_online:
            return None

        try:
            data = await self._request("GET", f"/api/v1/training/job/{job_id}")
            if data:
                return TrainingJob.from_dict(data)
            return None
        except Exception as e:
            logger.warning(f"[ReactorClient] Get job error: {e}")
            return None

    async def get_training_history(
        self,
        limit: int = 10,
        status_filter: Optional[str] = None,
    ) -> List[TrainingJob]:
        """
        Get training job history.

        Args:
            limit: Maximum number of jobs to return
            status_filter: Filter by status (completed, failed, etc.)

        Returns:
            List of TrainingJob objects
        """
        if not self._is_online:
            return []

        try:
            params = {"limit": limit}
            if status_filter:
                params["status"] = status_filter

            data = await self._request("GET", "/api/v1/training/history", params=params)
            if data and isinstance(data, list):
                return [TrainingJob.from_dict(job) for job in data]
            return []
        except Exception as e:
            logger.warning(f"[ReactorClient] Get history error: {e}")
            return []

    # =========================================================================
    # Experience Streaming
    # =========================================================================

    async def stream_experience(
        self,
        experience: Dict[str, Any],
    ) -> bool:
        """
        Stream a single experience log to Reactor-Core for future training.

        Args:
            experience: Experience data dictionary

        Returns:
            True if streamed successfully
        """
        if not self._is_online:
            return False

        try:
            payload = {
                "experience": experience,
                "timestamp": datetime.now().isoformat(),
                "source": "jarvis_agent",
            }

            data = await self._request("POST", "/api/v1/experiences/stream", json=payload)
            return data.get("accepted", False) if data else False

        except Exception as e:
            logger.warning(f"[ReactorClient] Experience stream error: {e}")
            return False

    async def get_experience_count(self) -> int:
        """
        Get count of pending experiences ready for training.

        Returns:
            Number of pending experiences
        """
        if not self._is_online:
            return 0

        try:
            data = await self._request("GET", "/api/v1/experiences/count")
            return data.get("count", 0) if data else 0
        except Exception as e:
            logger.warning(f"[ReactorClient] Get experience count error: {e}")
            return 0

    # =========================================================================
    # JARVIS Prime Hot-Swap Integration (Phase 2)
    # =========================================================================

    async def swap_jarvis_prime_model(
        self,
        model_path: str,
        version_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Trigger hot-swap on JARVIS Prime with a new model file.

        This is called after training completes to deploy the new model.

        Args:
            model_path: Absolute path to the .gguf model file
            version_id: Optional version identifier
            force: Force swap even if validation fails

        Returns:
            Swap result dictionary with success status and details
        """
        jarvis_prime_url = os.getenv("JARVIS_PRIME_URL", "http://localhost:8001")  # v192.2: Changed to 8001

        try:
            import aiohttp

            payload = {
                "model_path": model_path,
                "version_id": version_id,
                "force": force,
                "validate_before_swap": True,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{jarvis_prime_url}/model/swap",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120.0),  # Models can take time to load
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.info(
                            f"[ReactorClient] JARVIS Prime model swap SUCCESS: "
                            f"{result.get('old_version')} → {result.get('new_version')} "
                            f"({result.get('duration_seconds', 0):.2f}s)"
                        )
                        return result
                    else:
                        text = await response.text()
                        logger.error(
                            f"[ReactorClient] JARVIS Prime model swap FAILED: "
                            f"{response.status} - {text}"
                        )
                        return {
                            "success": False,
                            "error_message": f"HTTP {response.status}: {text}",
                        }

        except Exception as e:
            logger.error(f"[ReactorClient] JARVIS Prime model swap error: {e}")
            return {
                "success": False,
                "error_message": str(e),
            }

    async def get_jarvis_prime_status(self) -> Optional[Dict[str, Any]]:
        """
        Get JARVIS Prime model status.

        Returns:
            Status dictionary or None if unreachable
        """
        jarvis_prime_url = os.getenv("JARVIS_PRIME_URL", "http://localhost:8001")  # v192.2: Changed to 8001

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{jarvis_prime_url}/model/status",
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    return None

        except Exception as e:
            logger.warning(f"[ReactorClient] JARVIS Prime status check failed: {e}")
            return None

    async def check_jarvis_prime_health(self) -> bool:
        """
        Check if JARVIS Prime is healthy.

        Returns:
            True if JARVIS Prime is running and healthy
        """
        jarvis_prime_url = os.getenv("JARVIS_PRIME_URL", "http://localhost:8001")  # v192.2: Changed to 8001

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{jarvis_prime_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as response:
                    return response.status == 200

        except Exception:
            return False

    # =========================================================================
    # Scout Integration
    # =========================================================================

    async def add_learning_topic(
        self,
        topic: str,
        category: str = "general",
        priority: str = "normal",
        urls: Optional[List[str]] = None,
    ) -> bool:
        """
        Add a new learning topic for the Scout to explore.

        Args:
            topic: Topic description
            category: Topic category
            priority: Topic priority
            urls: Optional seed URLs

        Returns:
            True if added successfully
        """
        if not self._is_online:
            return False

        try:
            payload = {
                "topic": topic,
                "category": category,
                "priority": priority,
                "urls": urls or [],
                "added_by": "jarvis_agent",
            }

            data = await self._request("POST", "/api/scout/topics", json=payload)
            return data.get("added", False) if data else False

        except Exception as e:
            logger.warning(f"[ReactorClient] Add topic error: {e}")
            return False

    # =========================================================================
    # Metrics & Analytics
    # =========================================================================

    def get_metrics(self) -> Dict[str, Any]:
        """Get client metrics."""
        return {
            "initialized": self._initialized,
            "is_online": self._is_online,
            "api_url": self.config.api_url,
            "last_health_check": self._last_health_check.isoformat() if self._last_health_check else None,
            "last_trigger_time": self._last_trigger_time.isoformat() if self._last_trigger_time else None,
            "requests_made": self._requests_made,
            "requests_failed": self._requests_failed,
            "training_triggers": self._training_triggers,
            "training_completions": self._training_completions,
        }

    # =========================================================================
    # Event Callbacks
    # =========================================================================

    def on_training_started(self, callback: Callable) -> None:
        """Register callback for training started events."""
        self._on_training_started.append(callback)

    def on_training_completed(self, callback: Callable) -> None:
        """Register callback for training completed events."""
        self._on_training_completed.append(callback)

    def on_training_failed(self, callback: Callable) -> None:
        """Register callback for training failed events."""
        self._on_training_failed.append(callback)

    def on_connection_lost(self, callback: Callable) -> None:
        """Register callback for connection lost events."""
        self._on_connection_lost.append(callback)

    def on_connection_restored(self, callback: Callable) -> None:
        """Register callback for connection restored events."""
        self._on_connection_restored.append(callback)

    # =========================================================================
    # Private Methods
    # =========================================================================

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Make an HTTP request with retry logic.

        Args:
            method: HTTP method
            path: API path
            json: JSON body for POST requests
            params: Query parameters

        Returns:
            Response data or None
        """
        if not self._session:
            return None

        url = f"{self.config.api_url}{path}"
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        for attempt in range(self.config.max_retries):
            try:
                self._requests_made += 1

                async with self._session.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=headers,
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 404:
                        return None
                    else:
                        text = await response.text()
                        logger.warning(f"[ReactorClient] {method} {path}: {response.status} - {text}")

                        if response.status >= 500:
                            # Retry on server errors
                            await asyncio.sleep(self.config.retry_delay * (attempt + 1))
                            continue

                        return None

            except asyncio.TimeoutError:
                logger.warning(f"[ReactorClient] Request timeout: {method} {path}")
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay * (attempt + 1))
            except Exception as e:
                logger.warning(f"[ReactorClient] Request error: {method} {path}: {e}")
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay * (attempt + 1))

        self._requests_failed += 1
        return None

    async def _check_and_auto_trigger(self) -> None:
        """
        v2.1: Check experience count and auto-trigger training if threshold met.

        Called from health monitor loop after each successful health check.
        Guards:
        - Auto-trigger must be enabled
        - Reactor-Core must be training-ready (is_training_ready)
        - No active training job (_active_job_id is None)
        - Experience count must meet threshold
        - Minimum interval enforced by trigger_training()
        """
        # v2.1: Circuit breaker guard - skip if open after repeated failures
        if self._training_circuit_breaker and not await self._training_circuit_breaker.can_execute():
            return

        if not self.config.auto_trigger_enabled:
            return
        if not self.is_training_ready:
            return
        if self._active_job_id:
            return

        try:
            count = await self.get_experience_count()
            if count >= self.config.experience_threshold:
                logger.info(
                    f"[ReactorClient] Auto-trigger: {count} experiences "
                    f">= threshold {self.config.experience_threshold}"
                )
                job = await self.trigger_training(
                    experience_count=count,
                    priority=TrainingPriority.NORMAL,
                )
                if job:
                    self._active_job_id = job.job_id
                    logger.info(f"[ReactorClient] Auto-triggered job: {job.job_id}")
        except Exception as e:
            logger.warning(f"[ReactorClient] Auto-trigger check error: {e}")

    async def _poll_active_job(self) -> None:
        """
        v2.1: Poll the active training job for completion/failure.

        Called from health monitor loop. Updates _active_job_id state
        and emits events on job completion or failure.
        """
        if not self._active_job_id:
            return

        try:
            job = await self.get_training_job(self._active_job_id)
            if not job:
                return

            status = job.status

            if status == "completed":
                job_data = job.to_dict()
                logger.info(
                    f"[ReactorClient] Training job {self._active_job_id} completed: "
                    f"metrics={job.metrics}"
                )
                await self._emit_event("training_completed", job_data)
                await self._write_bridge_event("training_completed", job_data)
                # v2.1: Record success in circuit breaker
                if self._training_circuit_breaker:
                    await self._training_circuit_breaker.record_success()
                self._active_job_id = None

            elif status == "failed":
                job_data = job.to_dict()
                logger.error(
                    f"[ReactorClient] Training job {self._active_job_id} failed: {job.error}"
                )
                await self._emit_event("training_failed", job_data)
                await self._write_bridge_event("training_failed", job_data)
                # v2.1: Record failure in circuit breaker
                if self._training_circuit_breaker:
                    await self._training_circuit_breaker.record_failure(
                        f"Training job failed: {job.error}"
                    )
                self._active_job_id = None

            elif status == "running":
                stage = job.stage.value if hasattr(job.stage, 'value') else str(job.stage)
                logger.debug(
                    f"[ReactorClient] Training job {self._active_job_id}: {status}/{stage}"
                )

        except Exception as e:
            logger.warning(f"[ReactorClient] Job poll error: {e}")

    async def _health_monitor_loop(self) -> None:
        """
        v2.0: Adaptive background health monitoring loop.

        Features:
        - Adaptive interval that slows down during failures
        - Faster recovery when coming back online
        - Intelligent backoff to reduce load on failing services
        """
        while True:
            try:
                # v2.0: Use adaptive interval based on health state
                adaptive_interval = (
                    self.config.health_check_interval *
                    self._health_check_interval_multiplier
                )
                await asyncio.sleep(adaptive_interval)

                healthy = await self.health_check()

                if healthy:
                    # v2.1: Check experience count and auto-trigger training
                    await self._check_and_auto_trigger()

                # v2.1: Poll active training job
                await self._poll_active_job()

                if not healthy:
                    # v2.0: Increase interval on failures (back off)
                    self._health_check_interval_multiplier = min(
                        4.0,  # Max 4x the base interval
                        self._health_check_interval_multiplier * 1.2
                    )
                # Success handling is done in health_check

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[ReactorClient] Health monitor error: {e}")

    async def _emit_event(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Emit event to registered callbacks."""
        callbacks = {
            "training_started": self._on_training_started,
            "training_completed": self._on_training_completed,
            "training_failed": self._on_training_failed,
            "connection_lost": self._on_connection_lost,
            "connection_restored": self._on_connection_restored,
        }.get(event_type, [])

        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.warning(f"[ReactorClient] Event callback error: {e}")

    async def _write_bridge_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Write event to cross-repo bridge."""
        if not self.config.bridge_enabled:
            return

        try:
            bridge_dir = Path.home() / ".jarvis" / "cross_repo" / "events"
            bridge_dir.mkdir(parents=True, exist_ok=True)

            event = {
                "event_id": str(uuid.uuid4())[:8],
                "event_type": event_type,
                "source": "jarvis_agent",
                "timestamp": datetime.now().isoformat(),
                "payload": data,
            }

            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{event['event_id']}.json"
            filepath = bridge_dir / filename

            with open(filepath, "w") as f:
                import json as json_lib
                json_lib.dump(event, f, indent=2)

        except Exception as e:
            logger.warning(f"[ReactorClient] Bridge event write error: {e}")


# =============================================================================
# Global Instance & Helpers
# =============================================================================

_client_instance: Optional[ReactorCoreClient] = None


def get_reactor_client() -> Optional[ReactorCoreClient]:
    """Get the global Reactor-Core client instance."""
    return _client_instance


async def initialize_reactor_client(
    config: Optional[ReactorCoreConfig] = None,
) -> ReactorCoreClient:
    """
    Initialize the global Reactor-Core client.

    Args:
        config: Client configuration

    Returns:
        Initialized client
    """
    global _client_instance

    if _client_instance is None:
        _client_instance = ReactorCoreClient(config)
        await _client_instance.initialize()

    return _client_instance


async def shutdown_reactor_client() -> None:
    """Shutdown the global client."""
    global _client_instance

    if _client_instance:
        await _client_instance.close()
        _client_instance = None


async def check_and_trigger_training(
    experience_count: int,
    threshold: Optional[int] = None,
    priority: TrainingPriority = TrainingPriority.NORMAL,
    force: bool = False,
) -> Optional[TrainingJob]:
    """
    Check experience count and trigger training if threshold met.

    Args:
        experience_count: Current experience count
        threshold: Custom threshold (uses config default if not provided)
        priority: Training priority
        force: Force trigger regardless of threshold

    Returns:
        TrainingJob if triggered, None otherwise
    """
    client = get_reactor_client()
    if not client or not client.is_online:
        return None

    effective_threshold = threshold or client.config.experience_threshold

    if force or experience_count >= effective_threshold:
        return await client.trigger_training(
            experience_count=experience_count,
            priority=priority,
            force=force,
        )

    return None
