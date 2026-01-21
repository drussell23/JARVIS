"""
Cross-Repo Cost Synchronization v2.0 - Production Hardened
===========================================================

Enables unified cost tracking across JARVIS, JARVIS Prime, and Reactor Core repos.

HARDENED VERSION (v2.0) with:
- ResilientRedisClient for auto-reconnecting Redis connection
- AtomicFileOps for safe file-based fallback
- Auto-recovery from Redis disconnections with subscription restore
- Comprehensive health monitoring and metrics

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    Redis Pub/Sub Hub                             │
    │  Channel: jarvis:cost:cross_repo                                │
    └────────────────────────────────┬────────────────────────────────┘
           │                         │                         │
           ▼                         ▼                         ▼
    ┌─────────────┐          ┌─────────────┐          ┌─────────────┐
    │   JARVIS    │          │JARVIS Prime │          │Reactor Core │
    │ Cost Report │          │ Cost Report │          │ Cost Report │
    └─────────────┘          └─────────────┘          └─────────────┘

Features:
- Real-time cost synchronization via Redis Pub/Sub
- Unified budget enforcement across all repos
- Cost aggregation and reporting
- Per-repo cost attribution
- Budget alerts that consider total system cost
- File-based fallback when Redis unavailable

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

# Import resilience utilities
try:
    from backend.core.resilience.redis_reconnector import (
        ResilientRedisClient, RedisConnectionConfig, RedisNotAvailableError
    )
    from backend.core.resilience.atomic_file_ops import AtomicFileOps, AtomicFileConfig
    RESILIENCE_AVAILABLE = True
except ImportError:
    RESILIENCE_AVAILABLE = False
    ResilientRedisClient = None
    AtomicFileOps = None

logger = logging.getLogger("CrossRepoCostSync")

# =============================================================================
# Configuration
# =============================================================================

# Redis channels for cross-repo cost sync
REDIS_CHANNEL_CROSS_REPO_COST = "jarvis:cost:cross_repo"
REDIS_CHANNEL_CROSS_REPO_BUDGET = "jarvis:cost:cross_repo_budget"

# Redis keys for shared state
REDIS_KEY_CROSS_REPO_COSTS = "jarvis:cost:cross_repo:totals"
REDIS_KEY_REPO_COSTS_PREFIX = "jarvis:cost:repo:"
REDIS_KEY_UNIFIED_BUDGET = "jarvis:cost:unified_budget"

# File-based fallback directory
CROSS_REPO_COST_DIR = Path(os.getenv(
    "CROSS_REPO_COST_DIR",
    str(Path.home() / ".jarvis" / "cross_repo" / "costs")
))

# Configuration from environment
UNIFIED_DAILY_BUDGET = float(os.getenv("JARVIS_UNIFIED_DAILY_BUDGET", "5.00"))
COST_SYNC_INTERVAL = float(os.getenv("JARVIS_COST_SYNC_INTERVAL", "30.0"))
COST_REPORT_TTL = 120  # Seconds before a repo's cost report is considered stale


@dataclass
class RepoCountReport:
    """Cost report from a single repo."""
    repo_name: str
    instance_id: str
    timestamp: float = field(default_factory=time.time)

    # Cost breakdown
    daily_cost: float = 0.0
    weekly_cost: float = 0.0
    monthly_cost: float = 0.0

    # API costs
    api_calls_count: int = 0
    api_cost_usd: float = 0.0

    # Compute costs (VMs, Cloud Run, etc.)
    compute_cost_usd: float = 0.0
    active_vms: int = 0
    vm_runtime_hours: float = 0.0

    # Model inference costs
    inference_requests: int = 0
    inference_cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    # Local savings (running locally instead of cloud)
    local_inference_count: int = 0
    estimated_cloud_savings: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepoCountReport":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def total_cost(self) -> float:
        """Calculate total cost for this repo."""
        return self.api_cost_usd + self.compute_cost_usd + self.inference_cost_usd


@dataclass
class UnifiedCostState:
    """Aggregated cost state across all repos."""
    timestamp: float = field(default_factory=time.time)
    repos: Dict[str, RepoCountReport] = field(default_factory=dict)

    # Unified totals
    total_daily_cost: float = 0.0
    total_weekly_cost: float = 0.0
    total_monthly_cost: float = 0.0

    # Budget tracking
    daily_budget: float = UNIFIED_DAILY_BUDGET
    budget_used_percent: float = 0.0
    budget_exceeded: bool = False

    def update_totals(self) -> None:
        """Recalculate totals from all repo reports."""
        now = time.time()
        self.total_daily_cost = 0.0

        for repo_name, report in list(self.repos.items()):
            # Skip stale reports
            if now - report.timestamp > COST_REPORT_TTL:
                continue

            self.total_daily_cost += report.daily_cost
            self.total_weekly_cost += report.weekly_cost
            self.total_monthly_cost += report.monthly_cost

        # Update budget status
        if self.daily_budget > 0:
            self.budget_used_percent = (self.total_daily_cost / self.daily_budget) * 100
            self.budget_exceeded = self.total_daily_cost >= self.daily_budget

        self.timestamp = now

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "repos": {k: v.to_dict() for k, v in self.repos.items()},
            "total_daily_cost": self.total_daily_cost,
            "total_weekly_cost": self.total_weekly_cost,
            "total_monthly_cost": self.total_monthly_cost,
            "daily_budget": self.daily_budget,
            "budget_used_percent": self.budget_used_percent,
            "budget_exceeded": self.budget_exceeded,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UnifiedCostState":
        repos = {
            k: RepoCountReport.from_dict(v)
            for k, v in data.get("repos", {}).items()
        }
        return cls(
            timestamp=data.get("timestamp", time.time()),
            repos=repos,
            total_daily_cost=data.get("total_daily_cost", 0.0),
            total_weekly_cost=data.get("total_weekly_cost", 0.0),
            total_monthly_cost=data.get("total_monthly_cost", 0.0),
            daily_budget=data.get("daily_budget", UNIFIED_DAILY_BUDGET),
            budget_used_percent=data.get("budget_used_percent", 0.0),
            budget_exceeded=data.get("budget_exceeded", False),
        )


# =============================================================================
# Cross-Repo Cost Sync
# =============================================================================

class CrossRepoCostSync:
    """
    Synchronizes cost tracking across all JARVIS repos.

    This enables:
    - Unified budget enforcement (single budget for all repos)
    - Cross-repo cost visibility
    - Coordinated cost alerts
    - Per-repo cost attribution

    HARDENED Features (v2.0):
    - ResilientRedisClient for auto-reconnecting Redis with subscription restore
    - AtomicFileOps for safe file-based fallback
    - Auto-recovery from Redis disconnections
    - Comprehensive health monitoring
    """

    def __init__(
        self,
        repo_name: str,
        instance_id: Optional[str] = None,
    ):
        self.repo_name = repo_name
        self.instance_id = instance_id or f"{repo_name}-{os.getpid()}-{int(time.time())}"
        self.logger = logging.getLogger(f"CrossRepoCostSync.{repo_name}")

        # State
        self._unified_state = UnifiedCostState()
        self._local_report = RepoCountReport(
            repo_name=repo_name,
            instance_id=self.instance_id,
        )

        # ===== RESILIENCE COMPONENTS (v2.0) =====
        self._use_resilience = RESILIENCE_AVAILABLE

        # Resilient Redis client (instead of raw aioredis)
        self._resilient_redis: Optional[ResilientRedisClient] = None
        self._redis: Optional[Any] = None  # Fallback for non-resilient mode
        self._redis_available = False

        # Atomic file operations
        self._file_ops: Optional[AtomicFileOps] = None
        if RESILIENCE_AVAILABLE:
            self._file_ops = AtomicFileOps(AtomicFileConfig(
                max_retries=3,
                verify_checksum=False,
            ))

        # Pub/Sub (managed by ResilientRedisClient in resilient mode)
        self._pubsub: Optional[Any] = None
        self._pubsub_task: Optional[asyncio.Task] = None

        # Sync task
        self._sync_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._running = False

        # Error tracking
        self._consecutive_errors = 0
        self._last_error: Optional[Exception] = None

        # Callbacks
        self._budget_alert_callbacks: Set[Callable] = set()
        self._cost_update_callbacks: Set[Callable] = set()

        # File-based fallback
        CROSS_REPO_COST_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> bool:
        """Start the cross-repo cost sync."""
        if self._running:
            return True

        self._running = True
        self.logger.info(f"CrossRepoCostSync starting for {self.repo_name}...")

        # Initialize Redis
        await self._initialize_redis()

        # Start sync loop
        self._sync_task = asyncio.create_task(
            self._sync_loop(),
            name=f"cross_repo_cost_sync_{self.repo_name}",
        )

        self.logger.info(
            f"CrossRepoCostSync ready (Redis: {self._redis_available}, "
            f"budget: ${UNIFIED_DAILY_BUDGET:.2f}/day)"
        )
        return True

    async def stop(self) -> None:
        """Stop the cross-repo cost sync."""
        self._running = False

        # Cancel tasks
        for task in [self._sync_task, self._pubsub_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Cleanup Redis
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
            except Exception:
                pass

        if self._redis:
            try:
                await self._redis.aclose()  # v93.14
            except Exception:
                pass

        self.logger.info(f"CrossRepoCostSync stopped for {self.repo_name}")

    async def _initialize_redis(self) -> bool:
        """Initialize Redis connection with resilience support."""
        # Get Redis config
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis_password = os.getenv("REDIS_PASSWORD")
        redis_db = int(os.getenv("REDIS_DB", "0"))

        if redis_password:
            redis_url = f"redis://:{redis_password}@{redis_host}:{redis_port}/{redis_db}"
        else:
            redis_url = f"redis://{redis_host}:{redis_port}/{redis_db}"

        # Try resilient Redis first
        if self._use_resilience and RESILIENCE_AVAILABLE:
            try:
                config = RedisConnectionConfig(
                    url=redis_url,
                    db=redis_db,
                    password=redis_password,
                    auto_reconnect=True,
                    initial_reconnect_delay=1.0,
                    max_reconnect_delay=30.0,
                    health_check_interval=15.0,
                )

                self._resilient_redis = ResilientRedisClient(
                    config=config,
                    on_connect=self._on_redis_connect,
                    on_disconnect=self._on_redis_disconnect,
                )

                if await self._resilient_redis.connect():
                    self._redis_available = True
                    self.logger.info(
                        f"[Resilient] Redis connected: {redis_host}:{redis_port}"
                    )

                    # Start Pub/Sub with auto-reconnection
                    await self._start_resilient_pubsub()

                    return True

            except Exception as e:
                self.logger.warning(f"Resilient Redis failed: {e}")

        # Fallback to basic Redis
        try:
            try:
                import redis.asyncio as aioredis
            except ImportError:
                try:
                    import aioredis
                except ImportError:
                    self.logger.warning("Redis not available - using file-based sync")
                    return False

            self._redis = await aioredis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=5.0,
            )

            await self._redis.ping()
            self._redis_available = True
            self.logger.info(f"[Fallback] Redis connected: {redis_host}:{redis_port}")

            # Start Pub/Sub
            await self._start_pubsub()

            return True

        except Exception as e:
            self.logger.warning(f"Redis connection failed: {e} - using file-based sync")
            self._redis_available = False
            return False

    async def _on_redis_connect(self) -> None:
        """Callback when Redis reconnects."""
        self.logger.info("[Resilient] Redis reconnected - restoring subscriptions")
        self._redis_available = True
        self._consecutive_errors = 0

    async def _on_redis_disconnect(self, error: Exception) -> None:
        """Callback when Redis disconnects."""
        self.logger.warning(f"[Resilient] Redis disconnected: {error}")
        self._redis_available = False
        self._consecutive_errors += 1
        self._last_error = error

    async def _start_resilient_pubsub(self) -> None:
        """Start Pub/Sub with ResilientRedisClient."""
        if not self._resilient_redis:
            return

        try:
            # Subscribe with handler
            await self._resilient_redis.subscribe(
                REDIS_CHANNEL_CROSS_REPO_COST,
                self._handle_pubsub_message,
            )
            await self._resilient_redis.subscribe(
                REDIS_CHANNEL_CROSS_REPO_BUDGET,
                self._handle_pubsub_message,
            )

            self.logger.debug("[Resilient] Pub/Sub subscriptions established")

        except Exception as e:
            self.logger.warning(f"[Resilient] Failed to start Pub/Sub: {e}")

    async def _handle_pubsub_message(self, channel: str, data: bytes) -> None:
        """Handle Pub/Sub message from resilient client."""
        try:
            message_data = json.loads(data.decode() if isinstance(data, bytes) else data)

            if channel == REDIS_CHANNEL_CROSS_REPO_COST:
                await self._handle_cost_update(message_data)
            elif channel == REDIS_CHANNEL_CROSS_REPO_BUDGET:
                await self._handle_budget_alert(message_data)

        except json.JSONDecodeError:
            pass
        except Exception as e:
            self.logger.error(f"Pub/Sub message handling error: {e}")

    async def _start_pubsub(self) -> None:
        """Start Redis Pub/Sub listener."""
        if not self._redis:
            return

        try:
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(
                REDIS_CHANNEL_CROSS_REPO_COST,
                REDIS_CHANNEL_CROSS_REPO_BUDGET,
            )

            self._pubsub_task = asyncio.create_task(
                self._pubsub_loop(),
                name=f"cross_repo_cost_pubsub_{self.repo_name}",
            )

            self.logger.debug("Pub/Sub listener started")

        except Exception as e:
            self.logger.warning(f"Failed to start Pub/Sub: {e}")

    async def _pubsub_loop(self) -> None:
        """Listen for cross-repo cost updates."""
        try:
            async for message in self._pubsub.listen():
                if message["type"] != "message":
                    continue

                channel = message["channel"]
                try:
                    data = json.loads(message["data"])

                    if channel == REDIS_CHANNEL_CROSS_REPO_COST:
                        await self._handle_cost_update(data)
                    elif channel == REDIS_CHANNEL_CROSS_REPO_BUDGET:
                        await self._handle_budget_alert(data)

                except json.JSONDecodeError:
                    continue

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"Pub/Sub error: {e}")

    async def _handle_cost_update(self, data: Dict[str, Any]) -> None:
        """Handle cost update from another repo."""
        repo_name = data.get("repo_name")
        if not repo_name or repo_name == self.repo_name:
            return

        try:
            report = RepoCountReport.from_dict(data)
            self._unified_state.repos[repo_name] = report
            self._unified_state.update_totals()

            self.logger.debug(
                f"Received cost update from {repo_name}: ${report.daily_cost:.4f}"
            )

            # Notify callbacks
            for callback in self._cost_update_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(self._unified_state)
                    else:
                        callback(self._unified_state)
                except Exception as e:
                    self.logger.debug(f"Callback error: {e}")

        except Exception as e:
            self.logger.warning(f"Failed to process cost update: {e}")

    async def _handle_budget_alert(self, data: Dict[str, Any]) -> None:
        """Handle budget alert from cross-repo."""
        alert_type = data.get("alert_type")

        self.logger.warning(
            f"Cross-repo budget alert: {alert_type} - "
            f"Total: ${data.get('total_cost', 0):.4f} / ${data.get('budget', 0):.2f}"
        )

        # Notify callbacks
        for callback in self._budget_alert_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                self.logger.debug(f"Budget alert callback error: {e}")

    async def _sync_loop(self) -> None:
        """Periodic sync loop."""
        while self._running:
            try:
                # Publish our cost report
                await self._publish_cost_report()

                # Read other repos (file-based fallback)
                if not self._redis_available:
                    await self._read_file_based_reports()

                # Check budget
                await self._check_unified_budget()

                await asyncio.sleep(COST_SYNC_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync loop error: {e}")
                await asyncio.sleep(5.0)

    async def _publish_cost_report(self) -> None:
        """Publish our cost report to other repos."""
        self._local_report.timestamp = time.time()
        report_data = self._local_report.to_dict()

        if self._redis_available:
            try:
                # Publish via Pub/Sub
                await self._redis.publish(
                    REDIS_CHANNEL_CROSS_REPO_COST,
                    json.dumps(report_data),
                )

                # Also store in hash for persistent access
                await self._redis.hset(
                    REDIS_KEY_CROSS_REPO_COSTS,
                    self.repo_name,
                    json.dumps(report_data),
                )

            except Exception as e:
                self.logger.warning(f"Redis publish failed: {e}")

        # Always write file for fallback
        report_file = CROSS_REPO_COST_DIR / f"{self.repo_name}.json"
        try:
            tmp_file = report_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(report_data, indent=2))
            tmp_file.replace(report_file)
        except Exception as e:
            self.logger.warning(f"File write failed: {e}")

    async def _read_file_based_reports(self) -> None:
        """Read cost reports from files (fallback when Redis unavailable)."""
        now = time.time()

        for report_file in CROSS_REPO_COST_DIR.glob("*.json"):
            if report_file.stem == self.repo_name:
                continue

            try:
                data = json.loads(report_file.read_text())
                report = RepoCountReport.from_dict(data)

                # Check if stale
                if now - report.timestamp > COST_REPORT_TTL:
                    continue

                self._unified_state.repos[report.repo_name] = report

            except Exception as e:
                self.logger.debug(f"Failed to read {report_file}: {e}")

        self._unified_state.update_totals()

    async def _check_unified_budget(self) -> None:
        """Check if unified budget is exceeded."""
        self._unified_state.update_totals()

        if self._unified_state.budget_exceeded:
            alert_data = {
                "alert_type": "budget_exceeded",
                "total_cost": self._unified_state.total_daily_cost,
                "budget": self._unified_state.daily_budget,
                "repos": {
                    k: v.daily_cost for k, v in self._unified_state.repos.items()
                },
                "timestamp": time.time(),
            }

            if self._redis_available:
                try:
                    await self._redis.publish(
                        REDIS_CHANNEL_CROSS_REPO_BUDGET,
                        json.dumps(alert_data),
                    )
                except Exception:
                    pass

            await self._handle_budget_alert(alert_data)

        elif self._unified_state.budget_used_percent >= 80:
            # Warning at 80%
            self.logger.warning(
                f"Cross-repo budget at {self._unified_state.budget_used_percent:.1f}%: "
                f"${self._unified_state.total_daily_cost:.4f} / ${self._unified_state.daily_budget:.2f}"
            )

    # =========================================================================
    # Public API
    # =========================================================================

    def update_local_cost(
        self,
        api_cost: float = 0.0,
        compute_cost: float = 0.0,
        inference_cost: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        local_inference_count: int = 0,
    ) -> None:
        """
        Update local repo's cost report.

        Call this whenever costs are incurred in this repo.
        """
        self._local_report.api_cost_usd += api_cost
        self._local_report.compute_cost_usd += compute_cost
        self._local_report.inference_cost_usd += inference_cost
        self._local_report.tokens_in += tokens_in
        self._local_report.tokens_out += tokens_out
        self._local_report.local_inference_count += local_inference_count

        # Update daily cost
        self._local_report.daily_cost = self._local_report.total_cost()

        # Update unified state
        self._unified_state.repos[self.repo_name] = self._local_report
        self._unified_state.update_totals()

    def record_api_call(self, cost: float) -> None:
        """Record an API call cost."""
        self._local_report.api_calls_count += 1
        self._local_report.api_cost_usd += cost
        self._local_report.daily_cost = self._local_report.total_cost()

    def record_inference(
        self,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        is_local: bool = False,
    ) -> None:
        """Record an inference request."""
        self._local_report.inference_requests += 1
        self._local_report.tokens_in += tokens_in
        self._local_report.tokens_out += tokens_out
        self._local_report.inference_cost_usd += cost

        if is_local:
            self._local_report.local_inference_count += 1
            # Estimate cloud savings (based on Claude API pricing)
            cloud_input = (tokens_in / 1000) * 0.008
            cloud_output = (tokens_out / 1000) * 0.024
            self._local_report.estimated_cloud_savings += cloud_input + cloud_output

        self._local_report.daily_cost = self._local_report.total_cost()

    def record_vm_usage(self, runtime_hours: float, cost: float) -> None:
        """Record VM compute cost."""
        self._local_report.vm_runtime_hours += runtime_hours
        self._local_report.compute_cost_usd += cost
        self._local_report.daily_cost = self._local_report.total_cost()

    def can_incur_cost(self, amount: float) -> bool:
        """
        Check if a cost can be incurred without exceeding budget.

        Use this BEFORE making expensive API calls or starting VMs.
        """
        self._unified_state.update_totals()
        projected = self._unified_state.total_daily_cost + amount
        return projected < self._unified_state.daily_budget

    def get_unified_state(self) -> UnifiedCostState:
        """Get current unified cost state."""
        self._unified_state.update_totals()
        return self._unified_state

    def get_local_report(self) -> RepoCountReport:
        """Get this repo's cost report."""
        return self._local_report

    def get_remaining_budget(self) -> float:
        """Get remaining daily budget."""
        self._unified_state.update_totals()
        return max(0, self._unified_state.daily_budget - self._unified_state.total_daily_cost)

    def register_budget_alert_callback(self, callback: Callable) -> None:
        """Register callback for budget alerts."""
        self._budget_alert_callbacks.add(callback)

    def register_cost_update_callback(self, callback: Callable) -> None:
        """Register callback for cost updates from other repos."""
        self._cost_update_callbacks.add(callback)

    # v2.1: Alias methods for compatibility
    def on_budget_alert(self, callback: Callable) -> None:
        """
        Alias for register_budget_alert_callback.

        v2.1: Added for backward compatibility with supervisor code.
        """
        self.register_budget_alert_callback(callback)

    def on_cost_update(self, callback: Callable) -> None:
        """
        Alias for register_cost_update_callback.

        v2.1: Added for backward compatibility with supervisor code.
        """
        self.register_cost_update_callback(callback)

    def get_metrics(self) -> Dict[str, Any]:
        """Get sync metrics."""
        return {
            "repo_name": self.repo_name,
            "redis_available": self._redis_available,
            "running": self._running,
            "unified_state": self._unified_state.to_dict(),
            "local_report": self._local_report.to_dict(),
            "remaining_budget": self.get_remaining_budget(),
        }


# =============================================================================
# Global Instance Management
# =============================================================================

_cost_sync: Optional[CrossRepoCostSync] = None
_cost_sync_lock: Optional[asyncio.Lock] = None


def _get_cost_sync_lock() -> asyncio.Lock:
    """Get or create the cost sync lock."""
    global _cost_sync_lock
    if _cost_sync_lock is None:
        _cost_sync_lock = asyncio.Lock()
    return _cost_sync_lock


async def get_cross_repo_cost_sync(repo_name: str = "jarvis") -> CrossRepoCostSync:
    """Get the global CrossRepoCostSync instance."""
    global _cost_sync

    lock = _get_cost_sync_lock()
    async with lock:
        if _cost_sync is None:
            _cost_sync = CrossRepoCostSync(repo_name)
            await _cost_sync.start()

        return _cost_sync


async def shutdown_cross_repo_cost_sync() -> None:
    """Shutdown the global CrossRepoCostSync."""
    global _cost_sync

    if _cost_sync:
        await _cost_sync.stop()
        _cost_sync = None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "CrossRepoCostSync",
    "RepoCountReport",
    "UnifiedCostState",
    "get_cross_repo_cost_sync",
    "shutdown_cross_repo_cost_sync",
]
