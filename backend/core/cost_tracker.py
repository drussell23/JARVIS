#!/usr/bin/env python3
"""
Advanced Cost Tracking System for JARVIS Hybrid Cloud Intelligence v3.0
========================================================================

Fully async, dynamic, configuration-driven cost tracking with no hardcoding.
Tracks GCP VM costs, runtime hours, and cost optimization metrics.
Stores data in learning database for historical analysis and alerts.

v3.0 Features (Redis Integration):
- 🔴 Redis Pub/Sub for real-time WebSocket streaming (replaces polling!)
- 💾 Redis caching for frequently accessed cost metrics
- 🔄 Cross-instance cost synchronization
- 📡 Push-based notifications (no more polling!)
- 🌐 GCP Cloud Memorystore integration (or local Redis)

v2.0 Features (Triple-Lock Integration):
- 🛡️ Hard budget enforcement (blocks VM creation when exceeded)
- 📊 Intelligent cost forecasting with ML (simple time-series)
- 🔒 Integration with shutdown_hook.py for guaranteed cleanup
- ⏰ Alignment with 3-hour max-run-duration safety limit
- 🚨 Solo developer mode with aggressive cost protection
- 📈 Real-time cost streaming via WebSocket events

Triple-Lock Safety System Integration:
1. Platform-Level (GCP max-run-duration) - VMs auto-delete after 3 hours
2. VM-Side (startup script self-destruct) - VM shuts down if backend dies  
3. Local Cleanup (shutdown_hook.py) - Cleanup on shutdown + cost tracking

Redis Benefits for WebSocket:
- Polling: Client asks "any updates?" every N seconds (wasteful)
- Pub/Sub: Server pushes updates INSTANTLY when they happen (efficient)
- Result: Lower latency, less network traffic, real-time updates

Cost Protection for Solo Developers:
- Default daily budget: $1.00 (configurable via COST_ALERT_DAILY)
- Hard budget mode: Blocks VM creation when budget exceeded
- Immediate alerts when costs exceed 50% of daily budget
- Proactive forecasting warns before budget is hit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Set

from backend.core.async_safety import TimeoutConfig, get_shutdown_event

logger = logging.getLogger(__name__)

# Redis channel names for Pub/Sub
REDIS_CHANNEL_COST_UPDATES = "jarvis:cost:updates"
REDIS_CHANNEL_VM_EVENTS = "jarvis:cost:vm_events"
REDIS_CHANNEL_ALERTS = "jarvis:cost:alerts"
REDIS_CHANNEL_BUDGET = "jarvis:cost:budget"

# Redis cache keys
REDIS_KEY_DAILY_COST = "jarvis:cost:daily:{date}"
REDIS_KEY_ACTIVE_VMS = "jarvis:cost:active_vms"
REDIS_KEY_BUDGET_STATUS = "jarvis:cost:budget_status"
REDIS_KEY_METRICS = "jarvis:cost:metrics"
REDIS_CACHE_TTL = 60  # Cache TTL in seconds


# ============================================================================
# ENUMS AND TYPES
# ============================================================================


class AlertLevel(Enum):
    """Alert severity levels"""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class TriggerReason(Enum):
    """VM creation trigger reasons"""

    HIGH_RAM = "HIGH_RAM"
    PROACTIVE = "PROACTIVE"
    MANUAL = "MANUAL"
    EMERGENCY = "EMERGENCY"
    SCHEDULED = "SCHEDULED"


# ============================================================================
# CONFIGURATION CLASS
# ============================================================================


@dataclass
class CostTrackerConfig:
    """
    Dynamic configuration for cost tracking - no hardcoding.
    
    v2.0: Enhanced with solo developer mode and stricter defaults.
    All values configurable via environment variables.
    """

    # Database configuration
    db_path: Optional[Path] = None
    db_connection_pool_size: int = 5
    db_timeout: int = 30

    # GCP configuration (loaded from env)
    gcp_project_id: str = field(default_factory=lambda: os.getenv("GCP_PROJECT_ID", ""))
    gcp_region: str = field(default_factory=lambda: os.getenv("GCP_REGION", "us-central1"))
    gcp_zone: str = field(default_factory=lambda: os.getenv("GCP_ZONE", "us-central1-a"))

    # VM pricing (dynamically loadable from GCP API or config)
    spot_vm_hourly_cost: float = field(
        default_factory=lambda: float(os.getenv("SPOT_VM_HOURLY_COST", "0.029"))
    )
    regular_vm_hourly_cost: float = field(
        default_factory=lambda: float(os.getenv("REGULAR_VM_HOURLY_COST", "0.120"))
    )
    vm_instance_type: str = field(default_factory=lambda: os.getenv("GCP_VM_TYPE", "e2-highmem-4"))

    # =========================================================================
    # SOLO DEVELOPER MODE - Aggressive cost protection
    # =========================================================================
    # When enabled, provides stricter budget enforcement and more frequent alerts
    solo_developer_mode: bool = field(
        default_factory=lambda: os.getenv("JARVIS_SOLO_DEVELOPER_MODE", "true").lower() == "true"
    )
    
    # Hard budget enforcement - BLOCKS VM creation when exceeded (not just alerts)
    hard_budget_enforcement: bool = field(
        default_factory=lambda: os.getenv("JARVIS_HARD_BUDGET_ENFORCEMENT", "true").lower() == "true"
    )
    
    # Alert when this percentage of daily budget is reached (default: 50%)
    budget_warning_percent: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_BUDGET_WARNING_PERCENT", "50"))
    )
    
    # Alert thresholds (configurable via env)
    # v2.0: Lower defaults for solo developer protection
    alert_threshold_daily: float = field(
        default_factory=lambda: float(os.getenv("COST_ALERT_DAILY", "1.00"))
    )
    alert_threshold_weekly: float = field(
        default_factory=lambda: float(os.getenv("COST_ALERT_WEEKLY", "5.00"))
    )
    alert_threshold_monthly: float = field(
        default_factory=lambda: float(os.getenv("COST_ALERT_MONTHLY", "20.00"))
    )

    # =========================================================================
    # TRIPLE-LOCK INTEGRATION - Aligned with GCP safety limits
    # =========================================================================
    # v2.0: Aligned with max-run-duration of 3 hours
    max_vm_lifetime_hours: float = field(
        default_factory=lambda: float(os.getenv("MAX_VM_LIFETIME_HOURS", "3.0"))
    )
    max_local_ram_percent: float = field(
        default_factory=lambda: float(os.getenv("MAX_LOCAL_RAM_PERCENT", "85"))
    )
    min_gcp_routing_ratio: float = field(
        default_factory=lambda: float(os.getenv("MIN_GCP_ROUTING_RATIO", "0.1"))
    )

    # Cleanup configuration
    # v2.0: Aligned with Triple-Lock 3-hour limit (was 6 hours)
    orphaned_vm_max_age_hours: int = field(
        default_factory=lambda: int(os.getenv("ORPHANED_VM_MAX_AGE_HOURS", "3"))
    )
    cleanup_check_interval_hours: int = field(
        default_factory=lambda: int(os.getenv("CLEANUP_CHECK_INTERVAL_HOURS", "1"))
    )

    # Alert notification configuration
    alert_email: Optional[str] = field(default_factory=lambda: os.getenv("JARVIS_ALERT_EMAIL"))
    enable_desktop_notifications: bool = field(
        default_factory=lambda: os.getenv("ENABLE_DESKTOP_NOTIFICATIONS", "true").lower() == "true"
    )
    enable_email_alerts: bool = field(
        default_factory=lambda: os.getenv("ENABLE_EMAIL_ALERTS", "false").lower() == "true"
    )

    # Advanced features
    enable_auto_cleanup: bool = field(
        default_factory=lambda: os.getenv("ENABLE_AUTO_CLEANUP", "true").lower() == "true"
    )
    enable_cost_forecasting: bool = field(
        default_factory=lambda: os.getenv("ENABLE_COST_FORECASTING", "true").lower() == "true"
    )
    
    # Cost forecasting window (days of history to use)
    forecast_history_days: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_FORECAST_HISTORY_DAYS", "7"))
    )

    # =========================================================================
    # REDIS CONFIGURATION - Real-time streaming & caching
    # =========================================================================
    # Redis enables:
    # - Pub/Sub for instant WebSocket updates (no polling!)
    # - Caching for fast metric retrieval
    # - Cross-instance synchronization
    
    enable_redis: bool = field(
        default_factory=lambda: os.getenv("JARVIS_COST_REDIS_ENABLED", "true").lower() == "true"
    )
    
    # Redis connection - supports both local and GCP Cloud Memorystore
    redis_host: str = field(
        default_factory=lambda: os.getenv("REDIS_HOST", "localhost")
    )
    redis_port: int = field(
        default_factory=lambda: int(os.getenv("REDIS_PORT", "6379"))
    )
    redis_password: Optional[str] = field(
        default_factory=lambda: os.getenv("REDIS_PASSWORD")
    )
    redis_db: int = field(
        default_factory=lambda: int(os.getenv("REDIS_DB", "0"))
    )
    
    # Redis cache settings
    redis_cache_ttl: int = field(
        default_factory=lambda: int(os.getenv("REDIS_CACHE_TTL", "60"))
    )
    
    # Pub/Sub settings
    redis_pubsub_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_REDIS_PUBSUB", "true").lower() == "true"
    )

    def __post_init__(self):
        """Initialize paths and validate configuration"""
        if self.db_path is None:
            self.db_path = Path.home() / ".jarvis" / "learning" / "cost_tracking.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Log solo developer mode status
        if self.solo_developer_mode:
            logger.info("🛡️ Solo Developer Mode: ENABLED (stricter cost protection)")
            logger.info(f"   Daily budget: ${self.alert_threshold_daily:.2f}")
            logger.info(f"   Hard enforcement: {self.hard_budget_enforcement}")
            logger.info(f"   Warning at: {self.budget_warning_percent}% of budget")
        
        # Log Redis configuration
        if self.enable_redis:
            logger.info("🔴 Redis Integration: ENABLED")
            logger.info(f"   Host: {self.redis_host}:{self.redis_port}")
            logger.info(f"   Pub/Sub: {self.redis_pubsub_enabled}")
            logger.info(f"   Cache TTL: {self.redis_cache_ttl}s")
        else:
            logger.info("🔴 Redis Integration: DISABLED (using local-only mode)")


# ============================================================================
# DATA MODELS
# ============================================================================


@dataclass
class VMSession:
    """Represents a single GCP VM session with enhanced tracking"""

    instance_id: str
    created_at: datetime
    deleted_at: Optional[datetime] = None
    runtime_hours: float = 0.0
    estimated_cost: float = 0.0
    actual_cost: Optional[float] = None  # From GCP billing API if available
    components: List[str] = field(default_factory=list)
    trigger_reason: str = TriggerReason.HIGH_RAM.value
    is_orphaned: bool = False
    vm_type: str = "e2-highmem-4"
    region: str = "us-central1"
    zone: str = "us-central1-a"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def calculate_cost(self, hourly_rate: float) -> float:
        """Calculate cost based on runtime and hourly rate"""
        if self.deleted_at:
            runtime = (self.deleted_at - self.created_at).total_seconds() / 3600
        else:
            runtime = (datetime.utcnow() - self.created_at).total_seconds() / 3600

        self.runtime_hours = runtime
        self.estimated_cost = runtime * hourly_rate
        return self.estimated_cost


@dataclass
class CostMetrics:
    """Enhanced cost and performance metrics"""

    total_vms_created: int = 0
    total_runtime_hours: float = 0.0
    total_estimated_cost: float = 0.0
    total_actual_cost: float = 0.0  # From GCP billing API
    orphaned_vms_count: int = 0
    orphaned_vms_cost: float = 0.0
    average_vm_lifetime_hours: float = 0.0
    median_vm_lifetime_hours: float = 0.0
    local_requests: int = 0
    gcp_requests: int = 0
    gcp_routing_ratio: float = 0.0
    cost_savings_vs_regular: float = 0.0
    cost_efficiency_score: float = 0.0  # 0-100 score
    period_start: datetime = field(default_factory=datetime.utcnow)
    period_end: datetime = field(default_factory=datetime.utcnow)


# ============================================================================
# ADVANCED COST TRACKER CLASS
# ============================================================================


class CostTracker:
    """
    Advanced async cost tracking system with dynamic configuration.

    v3.0 Features:
    - 🔴 Redis Pub/Sub for real-time WebSocket streaming
    - 💾 Redis caching for fast metric access
    - 🔄 Cross-instance synchronization
    - Fully async database operations
    - Dynamic configuration via environment variables
    - No hardcoded values
    - Alert callbacks and notifications
    - Auto-cleanup of orphaned VMs
    - Cost forecasting
    - GCP billing API integration (optional)
    - WebSocket event streaming (now via Redis!)
    """

    def __init__(self, config: Optional[CostTrackerConfig] = None):
        """
        Initialize advanced cost tracker with Redis support.

        Args:
            config: Optional configuration object
        """
        self.config = config or CostTrackerConfig()
        self.active_sessions: Dict[str, VMSession] = {}
        self.metrics = CostMetrics()
        self._alert_callbacks: List[Callable] = []
        self._cleanup_task: Optional[asyncio.Task] = None
        self._db_lock = asyncio.Lock()
        
        # Redis components (v3.0)
        self._redis: Optional[Any] = None  # aioredis/redis.asyncio client
        self._redis_pubsub: Optional[Any] = None
        self._pubsub_task: Optional[asyncio.Task] = None
        self._redis_available = False
        self._websocket_subscribers: Set[Callable] = set()

        logger.info(f"💰 Advanced CostTracker v3.0 initialized")
        logger.info(f"   DB: {self.config.db_path}")
        logger.info(f"   VM Type: {self.config.vm_instance_type}")
        logger.info(f"   Region: {self.config.gcp_region}")
        logger.info(f"   Spot Rate: ${self.config.spot_vm_hourly_cost:.4f}/hr")

    async def initialize(self):
        """Initialize cost tracking system with Redis"""
        await self.initialize_database()
        
        # Initialize Redis connection (v3.0)
        if self.config.enable_redis:
            await self._initialize_redis()

        if self.config.enable_auto_cleanup:
            self._cleanup_task = asyncio.create_task(self._auto_cleanup_loop())
            logger.info(
                f"✅ Auto-cleanup enabled (every {self.config.cleanup_check_interval_hours}h)"
            )

    async def shutdown(self):
        """
        Gracefully shutdown cost tracker with Redis cleanup.

        v93.6: Enhanced with proper task termination and timeout handling.
        """
        logger.info("💰 CostTracker shutdown starting...")

        # v93.6: Signal cleanup loop to stop
        if hasattr(self, '_cleanup_running'):
            self._cleanup_running = False

        # v93.6: Set global shutdown event if available
        try:
            from backend.core.async_safety import set_shutdown_event
            set_shutdown_event()
        except ImportError:
            pass

        # Cancel cleanup task with timeout
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                # Give task 5 seconds to shutdown gracefully
                await asyncio.wait_for(
                    asyncio.shield(self._cleanup_task),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("⚠️ Cleanup task didn't stop in time, forcing...")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Cleanup task termination error (non-critical): {e}")

        # Shutdown Redis (v3.0)
        await self._shutdown_redis()

        logger.info("💰 CostTracker shutdown complete")

    # =========================================================================
    # v3.0: REDIS INTEGRATION - Real-time Streaming & Caching
    # =========================================================================

    async def _initialize_redis(self) -> bool:
        """
        Initialize Redis connection for Pub/Sub and caching.
        
        Supports both:
        - Local Redis (docker run -p 6379:6379 redis:alpine)
        - GCP Cloud Memorystore for Redis
        
        Returns:
            bool: True if Redis is available
        """
        try:
            # Try redis.asyncio (redis-py 4.2+) first, then aioredis
            try:
                import redis.asyncio as aioredis
            except ImportError:
                try:
                    import aioredis
                except ImportError:
                    logger.warning("🔴 Redis libraries not installed. Install with: pip install redis")
                    logger.info("   Falling back to local-only mode (no real-time streaming)")
                    self._redis_available = False
                    return False
            
            # Build connection URL
            if self.config.redis_password:
                redis_url = f"redis://:{self.config.redis_password}@{self.config.redis_host}:{self.config.redis_port}/{self.config.redis_db}"
            else:
                redis_url = f"redis://{self.config.redis_host}:{self.config.redis_port}/{self.config.redis_db}"
            
            # Connect to Redis
            self._redis = await aioredis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
            )
            
            # Test connection
            await self._redis.ping()
            
            self._redis_available = True
            logger.info(f"🔴 Redis connected: {self.config.redis_host}:{self.config.redis_port}")
            
            # Start Pub/Sub listener if enabled
            if self.config.redis_pubsub_enabled:
                await self._start_pubsub_listener()
            
            # Sync active sessions to Redis
            await self._sync_active_sessions_to_redis()
            
            return True
            
        except Exception as e:
            logger.warning(f"🔴 Redis connection failed: {e}")
            logger.info("   Falling back to local-only mode (no real-time streaming)")
            logger.info("   To enable Redis: docker run -d -p 6379:6379 redis:alpine")
            self._redis_available = False
            return False

    async def _shutdown_redis(self):
        """Gracefully shutdown Redis connections"""
        try:
            # Cancel Pub/Sub task
            if self._pubsub_task:
                self._pubsub_task.cancel()
                try:
                    await self._pubsub_task
                except asyncio.CancelledError:
                    pass
            
            # Close Pub/Sub
            if self._redis_pubsub:
                await self._redis_pubsub.unsubscribe()
                await self._redis_pubsub.close()
            
            # Close Redis connection
            if self._redis:
                await self._redis.close()
                
            logger.info("🔴 Redis connections closed")
            
        except Exception as e:
            logger.debug(f"Redis shutdown error (non-critical): {e}")

    async def _start_pubsub_listener(self):
        """Start background task to listen for Pub/Sub messages"""
        try:
            self._redis_pubsub = self._redis.pubsub()
            
            # Subscribe to cost-related channels
            await self._redis_pubsub.subscribe(
                REDIS_CHANNEL_COST_UPDATES,
                REDIS_CHANNEL_VM_EVENTS,
                REDIS_CHANNEL_ALERTS,
                REDIS_CHANNEL_BUDGET,
            )
            
            # Start listener task
            self._pubsub_task = asyncio.create_task(self._pubsub_listener_loop())
            
            logger.info("🔴 Redis Pub/Sub listener started")
            logger.info(f"   Channels: {REDIS_CHANNEL_COST_UPDATES}, {REDIS_CHANNEL_VM_EVENTS}")
            
        except Exception as e:
            logger.error(f"Failed to start Pub/Sub listener: {e}")

    async def _pubsub_listener_loop(self):
        """
        Background loop to receive Pub/Sub messages.
        
        This enables cross-instance communication and real-time updates.
        Messages received here are forwarded to WebSocket subscribers.
        """
        try:
            async for message in self._redis_pubsub.listen():
                if message["type"] == "message":
                    channel = message["channel"]
                    data = message["data"]
                    
                    try:
                        # Parse JSON data
                        if isinstance(data, str):
                            parsed_data = json.loads(data)
                        else:
                            parsed_data = data
                        
                        # Forward to WebSocket subscribers
                        await self._broadcast_to_websockets(channel, parsed_data)
                        
                        # Trigger local callbacks
                        await self._notify_event(f"redis:{channel}", parsed_data)
                        
                    except json.JSONDecodeError:
                        logger.debug(f"Non-JSON message on {channel}: {data}")
                        
        except asyncio.CancelledError:
            logger.debug("Pub/Sub listener cancelled")
        except Exception as e:
            logger.error(f"Pub/Sub listener error: {e}")

    async def _publish_event(self, channel: str, data: Dict[str, Any]):
        """
        Publish event to Redis Pub/Sub channel.
        
        This is the key method for real-time WebSocket updates!
        When you publish here, ALL connected clients receive the update instantly.
        
        Args:
            channel: Redis channel name
            data: Event data to publish
        """
        if not self._redis_available:
            # Fallback: just trigger local callbacks
            await self._notify_event(channel, data)
            return
        
        try:
            # Add metadata
            data["timestamp"] = datetime.utcnow().isoformat()
            data["source"] = "cost_tracker"
            
            # Publish to Redis
            await self._redis.publish(channel, json.dumps(data))
            
            logger.debug(f"📡 Published to {channel}: {data.get('event_type', 'unknown')}")
            
        except Exception as e:
            logger.error(f"Failed to publish to Redis: {e}")
            # Fallback to local callbacks
            await self._notify_event(channel, data)

    async def _broadcast_to_websockets(self, channel: str, data: Dict[str, Any]):
        """
        Broadcast message to all registered WebSocket subscribers.
        
        This is how Redis Pub/Sub replaces polling:
        1. Event happens (VM created, cost updated, etc.)
        2. Event published to Redis
        3. This method receives it via Pub/Sub
        4. Instantly pushes to all connected WebSocket clients
        
        No polling needed - updates are pushed in real-time!
        """
        if not self._websocket_subscribers:
            return
        
        message = {
            "channel": channel,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        # Notify all subscribers in parallel
        tasks = []
        for subscriber in list(self._websocket_subscribers):
            try:
                if asyncio.iscoroutinefunction(subscriber):
                    tasks.append(subscriber(message))
                else:
                    subscriber(message)
            except Exception as e:
                logger.debug(f"WebSocket subscriber error: {e}")
                # Remove dead subscribers
                self._websocket_subscribers.discard(subscriber)
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def register_websocket_subscriber(self, callback: Callable):
        """
        Register a WebSocket connection for real-time updates.
        
        Usage:
            async def ws_handler(message):
                await websocket.send_json(message)
            
            cost_tracker.register_websocket_subscriber(ws_handler)
        
        Args:
            callback: Async function to receive messages
        """
        self._websocket_subscribers.add(callback)
        logger.debug(f"WebSocket subscriber registered (total: {len(self._websocket_subscribers)})")

    def unregister_websocket_subscriber(self, callback: Callable):
        """Unregister a WebSocket connection"""
        self._websocket_subscribers.discard(callback)
        logger.debug(f"WebSocket subscriber removed (total: {len(self._websocket_subscribers)})")

    async def _sync_active_sessions_to_redis(self):
        """Sync active VM sessions to Redis for cross-instance visibility"""
        if not self._redis_available:
            return
        
        try:
            # Store active sessions in Redis hash
            if self.active_sessions:
                session_data = {
                    vm_id: json.dumps({
                        "instance_id": s.instance_id,
                        "created_at": s.created_at.isoformat(),
                        "vm_type": s.vm_type,
                        "region": s.region,
                        "zone": s.zone,
                    })
                    for vm_id, s in self.active_sessions.items()
                }
                await self._redis.hset(REDIS_KEY_ACTIVE_VMS, mapping=session_data)
            
            logger.debug(f"Synced {len(self.active_sessions)} active sessions to Redis")
            
        except Exception as e:
            logger.debug(f"Failed to sync sessions to Redis: {e}")

    # =========================================================================
    # v3.0: REDIS CACHING - Fast Metric Access
    # =========================================================================

    async def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get value from Redis cache"""
        if not self._redis_available:
            return None
        
        try:
            data = await self._redis.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.debug(f"Cache get error: {e}")
        
        return None

    async def _cache_set(self, key: str, value: Dict[str, Any], ttl: Optional[int] = None):
        """Set value in Redis cache with TTL"""
        if not self._redis_available:
            return
        
        try:
            ttl = ttl or self.config.redis_cache_ttl
            await self._redis.setex(key, ttl, json.dumps(value))
        except Exception as e:
            logger.debug(f"Cache set error: {e}")

    async def _cache_delete(self, key: str):
        """Delete key from Redis cache"""
        if not self._redis_available:
            return
        
        try:
            await self._redis.delete(key)
        except Exception as e:
            logger.debug(f"Cache delete error: {e}")

    async def initialize_database(self):
        """Create database tables with enhanced schema"""
        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(
                    self.config.db_path, timeout=self.config.db_timeout
                ) as db:
                    # Enhanced VM Sessions table
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS vm_sessions (
                            instance_id TEXT PRIMARY KEY,
                            created_at TEXT NOT NULL,
                            deleted_at TEXT,
                            runtime_hours REAL DEFAULT 0.0,
                            estimated_cost REAL DEFAULT 0.0,
                            actual_cost REAL,
                            components TEXT,
                            trigger_reason TEXT,
                            is_orphaned INTEGER DEFAULT 0,
                            vm_type TEXT,
                            region TEXT,
                            zone TEXT,
                            metadata TEXT
                        )
                    """
                    )

                    # Cost configuration history table
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS cost_config_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            config_key TEXT NOT NULL,
                            old_value TEXT,
                            new_value TEXT,
                            reason TEXT
                        )
                    """
                    )

                    # Enhanced routing metrics table
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS routing_metrics (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            local_ram_percent REAL,
                            routing_decision TEXT,
                            components_shifted TEXT,
                            local_request INTEGER DEFAULT 1,
                            decision_latency_ms REAL,
                            success INTEGER DEFAULT 1
                        )
                    """
                    )

                    # Alerts history table
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS alert_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            alert_level TEXT NOT NULL,
                            alert_type TEXT NOT NULL,
                            message TEXT NOT NULL,
                            details TEXT,
                            acknowledged INTEGER DEFAULT 0
                        )
                    """
                    )

                    # Cost forecasts table
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS cost_forecasts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            generated_at TEXT NOT NULL,
                            forecast_period TEXT NOT NULL,
                            forecast_start TEXT NOT NULL,
                            forecast_end TEXT NOT NULL,
                            predicted_cost REAL NOT NULL,
                            confidence_score REAL,
                            actual_cost REAL,
                            accuracy_score REAL
                        )
                    """
                    )

                    await db.commit()

                    # Run migrations for existing databases
                    await self._migrate_database(db)

                    logger.info("✅ Advanced cost tracking database initialized")

        except Exception as e:
            logger.error(f"Failed to initialize cost tracking database: {e}")
            raise

    async def _migrate_database(self, db):
        """Migrate existing database schema to add missing columns"""
        try:
            # Check if actual_cost column exists in vm_sessions
            cursor = await db.execute("PRAGMA table_info(vm_sessions)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]

            if "actual_cost" not in column_names:
                logger.info("🔧 Migrating database: adding actual_cost column")
                await db.execute("ALTER TABLE vm_sessions ADD COLUMN actual_cost REAL")
                await db.commit()
                logger.info("✅ Migration complete: actual_cost column added")
        except Exception as e:
            logger.warning(f"Database migration check failed (non-critical): {e}")

    async def record_vm_created(
        self,
        instance_id: str,
        components: List[str],
        trigger_reason: str = TriggerReason.HIGH_RAM.value,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Record VM creation with enhanced tracking"""
        session = VMSession(
            instance_id=instance_id,
            created_at=datetime.utcnow(),
            components=components,
            trigger_reason=trigger_reason,
            vm_type=self.config.vm_instance_type,
            region=self.config.gcp_region,
            zone=self.config.gcp_zone,
            metadata=metadata or {},
        )

        self.active_sessions[instance_id] = session
        self.metrics.total_vms_created += 1

        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    await db.execute(
                        """
                        INSERT OR REPLACE INTO vm_sessions
                        (instance_id, created_at, components, trigger_reason, vm_type, region, zone, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            instance_id,
                            session.created_at.isoformat(),
                            json.dumps(components),
                            trigger_reason,
                            session.vm_type,
                            session.region,
                            session.zone,
                            json.dumps(metadata or {}),
                        ),
                    )
                    await db.commit()

            logger.info(
                f"💰 VM created: {instance_id} (trigger: {trigger_reason}, type: {session.vm_type})"
            )

            # v3.0: Publish to Redis for real-time WebSocket updates
            await self._publish_event(REDIS_CHANNEL_VM_EVENTS, {
                "event_type": "vm_created",
                "instance_id": instance_id,
                "trigger_reason": trigger_reason,
                "vm_type": session.vm_type,
                "region": session.region,
                "zone": session.zone,
                "active_vms": len(self.active_sessions),
            })
            
            # Update Redis cache
            await self._sync_active_sessions_to_redis()
            await self._cache_delete(REDIS_KEY_BUDGET_STATUS)  # Invalidate budget cache

            # Trigger alert callbacks (for backward compatibility)
            await self._notify_event("vm_created", {"instance_id": instance_id, "session": session})

        except Exception as e:
            logger.error(f"Failed to record VM creation: {e}")

    async def record_vm_deleted(
        self, instance_id: str, was_orphaned: bool = False, actual_cost: Optional[float] = None
    ):
        """Record VM deletion with cost calculation"""
        session = self.active_sessions.get(instance_id)

        if not session:
            logger.warning(f"VM {instance_id} not found in active sessions - querying database")
            session = await self._load_session_from_db(instance_id)

        if not session:
            logger.error(f"VM {instance_id} not found anywhere - creating placeholder")
            session = VMSession(
                instance_id=instance_id,
                created_at=datetime.utcnow() - timedelta(hours=1),
                is_orphaned=was_orphaned,
                vm_type=self.config.vm_instance_type,
                region=self.config.gcp_region,
                zone=self.config.gcp_zone,
            )

        session.deleted_at = datetime.utcnow()
        session.is_orphaned = was_orphaned
        session.actual_cost = actual_cost
        cost = session.calculate_cost(self.config.spot_vm_hourly_cost)

        # Update metrics
        self.metrics.total_runtime_hours += session.runtime_hours
        self.metrics.total_estimated_cost += cost
        if actual_cost:
            self.metrics.total_actual_cost += actual_cost

        if was_orphaned:
            self.metrics.orphaned_vms_count += 1
            self.metrics.orphaned_vms_cost += cost

        # Calculate average VM lifetime
        if self.metrics.total_vms_created > 0:
            self.metrics.average_vm_lifetime_hours = (
                self.metrics.total_runtime_hours / self.metrics.total_vms_created
            )

        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    await db.execute(
                        """
                        UPDATE vm_sessions
                        SET deleted_at = ?, runtime_hours = ?, estimated_cost = ?,
                            actual_cost = ?, is_orphaned = ?
                        WHERE instance_id = ?
                    """,
                        (
                            session.deleted_at.isoformat(),
                            session.runtime_hours,
                            session.estimated_cost,
                            actual_cost,
                            1 if was_orphaned else 0,
                            instance_id,
                        ),
                    )
                    await db.commit()

            logger.info(
                f"💰 VM deleted: {instance_id} "
                f"(runtime: {session.runtime_hours:.2f}h, cost: ${cost:.4f}, "
                f"orphaned: {was_orphaned})"
            )

            # Remove from active sessions
            if instance_id in self.active_sessions:
                del self.active_sessions[instance_id]

            # v3.0: Publish to Redis for real-time WebSocket updates
            await self._publish_event(REDIS_CHANNEL_VM_EVENTS, {
                "event_type": "vm_deleted",
                "instance_id": instance_id,
                "runtime_hours": round(session.runtime_hours, 2),
                "cost": round(cost, 4),
                "was_orphaned": was_orphaned,
                "active_vms": len(self.active_sessions),
            })
            
            # Publish cost update
            await self._publish_event(REDIS_CHANNEL_COST_UPDATES, {
                "event_type": "cost_update",
                "total_runtime_hours": round(self.metrics.total_runtime_hours, 2),
                "total_estimated_cost": round(self.metrics.total_estimated_cost, 4),
                "active_vms": len(self.active_sessions),
            })
            
            # Update Redis cache
            await self._sync_active_sessions_to_redis()
            await self._cache_delete(REDIS_KEY_BUDGET_STATUS)  # Invalidate budget cache

            # Check for cost alerts
            await self._check_cost_alerts()

            # Trigger alert callbacks (for backward compatibility)
            await self._notify_event("vm_deleted", {"instance_id": instance_id, "session": session})

        except Exception as e:
            logger.error(f"Failed to record VM deletion: {e}")

    # ============================================================================
    # ALIAS METHODS FOR COMPATIBILITY (used by gcp_vm_manager.py)
    # ============================================================================

    async def record_vm_creation(
        self,
        instance_id: str,
        vm_type: Optional[str] = None,
        region: Optional[str] = None,
        zone: Optional[str] = None,
        components: Optional[List[str]] = None,
        trigger_reason: str = TriggerReason.HIGH_RAM.value,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,  # Accept any additional parameters for forward compatibility
    ):
        """
        Alias for record_vm_created with extended parameters.
        
        This method provides compatibility with gcp_vm_manager.py which calls
        record_vm_creation() instead of record_vm_created().
        
        Args:
            instance_id: Unique VM instance identifier
            vm_type: VM machine type (e.g., e2-highmem-4)
            region: GCP region (e.g., us-central1)
            zone: GCP zone (e.g., us-central1-a)
            components: List of components running on this VM
            trigger_reason: Why the VM was created
            metadata: Additional metadata dict
            **kwargs: Additional parameters for forward compatibility
        """
        # Build enhanced metadata with VM details
        enhanced_metadata = metadata.copy() if metadata else {}
        if vm_type:
            enhanced_metadata["vm_type"] = vm_type
        if region:
            enhanced_metadata["region"] = region
        if zone:
            enhanced_metadata["zone"] = zone
        
        # Add any extra kwargs to metadata
        for key, value in kwargs.items():
            if value is not None:
                enhanced_metadata[key] = value
        
        # Temporarily update config if specific region/zone provided
        original_vm_type = self.config.vm_instance_type
        original_region = self.config.gcp_region
        original_zone = self.config.gcp_zone
        
        try:
            if vm_type:
                self.config.vm_instance_type = vm_type
            if region:
                self.config.gcp_region = region
            if zone:
                self.config.gcp_zone = zone
                
            await self.record_vm_created(
                instance_id=instance_id,
                components=components or [],
                trigger_reason=trigger_reason,
                metadata=enhanced_metadata,
            )
        finally:
            # Restore original config
            self.config.vm_instance_type = original_vm_type
            self.config.gcp_region = original_region
            self.config.gcp_zone = original_zone

    async def record_vm_termination(
        self,
        instance_id: str,
        reason: str = "",
        total_cost: Optional[float] = None,
        was_orphaned: bool = False,
        **kwargs,  # Accept any additional parameters for forward compatibility
    ):
        """
        Alias for record_vm_deleted with extended parameters.
        
        This method provides compatibility with gcp_vm_manager.py which calls
        record_vm_termination() instead of record_vm_deleted().
        
        Args:
            instance_id: Unique VM instance identifier
            reason: Reason for termination (stored in logs)
            total_cost: Total cost incurred by this VM
            was_orphaned: Whether this was an orphaned VM cleanup
            **kwargs: Additional parameters for forward compatibility
        """
        logger.info(f"💰 VM termination recorded: {instance_id} (reason: {reason})")
        
        await self.record_vm_deleted(
            instance_id=instance_id,
            was_orphaned=was_orphaned,
            actual_cost=total_cost,
        )

    async def _load_session_from_db(self, instance_id: str) -> Optional[VMSession]:
        """Load VM session from database"""
        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    async with db.execute(
                        """
                        SELECT created_at, components, trigger_reason, vm_type, region, zone, metadata
                        FROM vm_sessions
                        WHERE instance_id = ?
                    """,
                        (instance_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            return VMSession(
                                instance_id=instance_id,
                                created_at=datetime.fromisoformat(row[0]),
                                components=json.loads(row[1]) if row[1] else [],
                                trigger_reason=row[2] or TriggerReason.HIGH_RAM.value,
                                vm_type=row[3] or self.config.vm_instance_type,
                                region=row[4] or self.config.gcp_region,
                                zone=row[5] or self.config.gcp_zone,
                                metadata=json.loads(row[6]) if row[6] else {},
                            )
        except Exception as e:
            logger.error(f"Failed to load session from DB: {e}")

        return None

    async def cleanup_orphaned_vms(self) -> Dict[str, Any]:
        """
        Automatically cleanup orphaned VMs from GCP.
        Integrated version of cleanup script.
        """
        results = {
            "checked_at": datetime.utcnow().isoformat(),
            "orphaned_vms_found": 0,
            "orphaned_vms_deleted": 0,
            "errors": [],
            "vms": [],
        }

        try:
            # List all jarvis VMs from GCP
            cmd = [
                "gcloud",
                "compute",
                "instances",
                "list",
                f"--project={self.config.gcp_project_id}",
                "--filter=name~'jarvis-auto-.*'",
                "--format=json",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = f"gcloud list failed: {stderr.decode()}"
                logger.error(error_msg)
                results["errors"].append(error_msg)
                return results

            vms = json.loads(stdout.decode()) if stdout else []

            for vm in vms:
                instance_id = vm.get("name")
                creation_time = vm.get("creationTimestamp")

                if not instance_id or not creation_time:
                    continue

                # Parse creation time
                created_at = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
                age_hours = (datetime.now(created_at.tzinfo) - created_at).total_seconds() / 3600

                results["vms"].append(
                    {
                        "instance_id": instance_id,
                        "age_hours": round(age_hours, 2),
                        "status": vm.get("status"),
                    }
                )

                if age_hours >= self.config.orphaned_vm_max_age_hours:
                    results["orphaned_vms_found"] += 1

                    logger.warning(f"🗑️  Orphaned VM found: {instance_id} (age: {age_hours:.1f}h)")

                    # Delete the VM
                    delete_cmd = [
                        "gcloud",
                        "compute",
                        "instances",
                        "delete",
                        instance_id,
                        f"--project={self.config.gcp_project_id}",
                        f"--zone={self.config.gcp_zone}",
                        "--quiet",
                    ]

                    delete_process = await asyncio.create_subprocess_exec(
                        *delete_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    await delete_process.communicate()

                    if delete_process.returncode == 0:
                        results["orphaned_vms_deleted"] += 1

                        # Record deletion in database
                        await self.record_vm_deleted(instance_id, was_orphaned=True)

                        logger.info(f"✅ Deleted orphaned VM: {instance_id}")

                        # Send notification
                        if self.config.enable_desktop_notifications:
                            await self._send_desktop_notification(
                                f"Deleted orphaned VM: {instance_id}"
                            )
                    else:
                        error_msg = f"Failed to delete {instance_id}"
                        results["errors"].append(error_msg)
                        logger.error(error_msg)

            # Log summary
            logger.info(
                f"🧹 Cleanup complete: {results['orphaned_vms_deleted']}/{results['orphaned_vms_found']} orphaned VMs deleted"
            )

            return results

        except Exception as e:
            error_msg = f"Cleanup failed: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            return results

    async def _auto_cleanup_loop(self):
        """
        Background task for automatic cleanup with timeout protection.

        v93.6: Enhanced with short sleep intervals (1s) to check shutdown state
        frequently, preventing "Task was destroyed but it is pending" errors.
        """
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("COST_CLEANUP_MAX_ITERATIONS", "0")) or None
        iteration = 0
        self._cleanup_running = True  # v93.6: Track running state

        while self._cleanup_running:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("Auto-cleanup loop stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"Auto-cleanup loop reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                # v93.6: Use short sleep intervals (1s) to check shutdown state frequently
                # This prevents the "Task was destroyed but it is pending" error
                total_sleep_seconds = self.config.cleanup_check_interval_hours * 3600
                sleep_remaining = total_sleep_seconds

                while sleep_remaining > 0 and self._cleanup_running:
                    # Check shutdown event frequently
                    if shutdown_event.is_set():
                        logger.info("Auto-cleanup loop: shutdown event detected during sleep")
                        self._cleanup_running = False
                        return

                    # Sleep in 1-second intervals
                    await asyncio.sleep(min(1.0, sleep_remaining))
                    sleep_remaining -= 1.0

                # Exit if we were signaled to stop
                if not self._cleanup_running:
                    break

                logger.info("🔄 Running scheduled orphaned VM cleanup...")
                # Add timeout protection for cleanup operation
                results = await asyncio.wait_for(
                    self.cleanup_orphaned_vms(),
                    timeout=TimeoutConfig.VM_OPERATION
                )

                if results["orphaned_vms_deleted"] > 0:
                    await self._log_alert(
                        AlertLevel.WARNING,
                        "orphaned_vms_cleanup",
                        f"Cleaned up {results['orphaned_vms_deleted']} orphaned VMs",
                        results,
                    )

            except asyncio.TimeoutError:
                logger.warning(f"Auto-cleanup timed out after {TimeoutConfig.VM_OPERATION}s")
            except asyncio.CancelledError:
                logger.info("Auto-cleanup loop cancelled (graceful shutdown)")
                self._cleanup_running = False
                return
            except Exception as e:
                if not self._cleanup_running:
                    break
                logger.error(f"Auto-cleanup loop error: {e}")

        # v93.6: Clean exit logging
        logger.info("Auto-cleanup loop stopped")

    async def record_routing_decision(
        self,
        local_ram_percent: float,
        decision: str,
        components: Optional[List[str]] = None,
        routed_to_gcp: bool = False,
        decision_latency_ms: Optional[float] = None,
        success: bool = True,
    ):
        """Record routing decision with enhanced metrics"""
        if routed_to_gcp:
            self.metrics.gcp_requests += 1
        else:
            self.metrics.local_requests += 1

        total_requests = self.metrics.local_requests + self.metrics.gcp_requests
        if total_requests > 0:
            self.metrics.gcp_routing_ratio = self.metrics.gcp_requests / total_requests

        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    await db.execute(
                        """
                        INSERT INTO routing_metrics
                        (timestamp, local_ram_percent, routing_decision, components_shifted,
                         local_request, decision_latency_ms, success)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            datetime.utcnow().isoformat(),
                            local_ram_percent,
                            decision,
                            json.dumps(components or []),
                            0 if routed_to_gcp else 1,
                            decision_latency_ms,
                            1 if success else 0,
                        ),
                    )
                    await db.commit()

        except Exception as e:
            logger.error(f"Failed to record routing decision: {e}")

    async def get_cost_summary(self, period: str = "all") -> Dict[str, Any]:
        """Get cost summary with enhanced metrics"""
        now = datetime.utcnow()
        period_start = {
            "day": now - timedelta(days=1),
            "week": now - timedelta(weeks=1),
            "month": now - timedelta(days=30),
            "all": datetime(2000, 1, 1),
        }.get(period, datetime(2000, 1, 1))

        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    async with db.execute(
                        """
                        SELECT
                            COUNT(*) as total_vms,
                            SUM(runtime_hours) as total_hours,
                            SUM(estimated_cost) as total_cost,
                            SUM(actual_cost) as total_actual,
                            SUM(CASE WHEN is_orphaned = 1 THEN 1 ELSE 0 END) as orphaned_vms,
                            SUM(CASE WHEN is_orphaned = 1 THEN estimated_cost ELSE 0 END) as orphaned_cost,
                            AVG(runtime_hours) as avg_lifetime,
                            MAX(runtime_hours) as max_lifetime,
                            MIN(runtime_hours) as min_lifetime
                        FROM vm_sessions
                        WHERE created_at >= ?
                    """,
                        (period_start.isoformat(),),
                    ) as cursor:
                        row = await cursor.fetchone()

                        if row:
                            total_cost = row[2] or 0.0
                            total_hours = row[1] or 0.0
                            total_actual = row[3] or 0.0

                            regular_cost = total_hours * self.config.regular_vm_hourly_cost
                            savings = regular_cost - total_cost

                            # Calculate cost efficiency score
                            if total_actual > 0:
                                efficiency = ((total_actual - total_cost) / total_actual) * 100
                            else:
                                efficiency = 0.0

                            return {
                                "period": period,
                                "period_start": period_start.isoformat(),
                                "period_end": now.isoformat(),
                                "total_vms_created": row[0] or 0,
                                "total_runtime_hours": round(total_hours, 2),
                                "total_estimated_cost": round(total_cost, 4),
                                "total_actual_cost": round(total_actual, 4),
                                "orphaned_vms_count": row[4] or 0,
                                "orphaned_vms_cost": round(row[5] or 0.0, 4),
                                "average_vm_lifetime_hours": round(row[6] or 0.0, 2),
                                "max_vm_lifetime_hours": round(row[7] or 0.0, 2),
                                "min_vm_lifetime_hours": round(row[8] or 0.0, 2),
                                "cost_savings_vs_regular": round(savings, 4),
                                "savings_percentage": round(
                                    (savings / regular_cost * 100) if regular_cost > 0 else 0, 1
                                ),
                                "cost_efficiency_score": round(efficiency, 2),
                                "vm_type": self.config.vm_instance_type,
                                "spot_rate": self.config.spot_vm_hourly_cost,
                                "regular_rate": self.config.regular_vm_hourly_cost,
                            }

            return {}

        except Exception as e:
            logger.error(f"Failed to get cost summary: {e}")
            return {}

    async def get_daily_cost(self) -> float:
        """
        Get today's total estimated cost.

        Returns:
            float: Total estimated cost for the current day
        """
        summary = await self.get_cost_summary("day")
        return summary.get("total_estimated_cost", 0.0)

    async def get_routing_metrics(self, period: str = "day") -> Dict[str, Any]:
        """Get routing metrics with enhanced analytics"""
        now = datetime.utcnow()
        period_start = {
            "day": now - timedelta(days=1),
            "week": now - timedelta(weeks=1),
            "month": now - timedelta(days=30),
            "all": datetime(2000, 1, 1),
        }.get(period, now - timedelta(days=1))

        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    async with db.execute(
                        """
                        SELECT
                            COUNT(*) as total_requests,
                            SUM(local_request) as local_requests,
                            SUM(CASE WHEN local_request = 0 THEN 1 ELSE 0 END) as gcp_requests,
                            AVG(local_ram_percent) as avg_ram,
                            MAX(local_ram_percent) as max_ram,
                            AVG(decision_latency_ms) as avg_latency,
                            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful_decisions
                        FROM routing_metrics
                        WHERE timestamp >= ?
                    """,
                        (period_start.isoformat(),),
                    ) as cursor:
                        row = await cursor.fetchone()

                        if row and row[0]:
                            total = row[0]
                            local = row[1] or 0
                            gcp = row[2] or 0
                            successful = row[6] or 0

                            return {
                                "period": period,
                                "total_requests": total,
                                "local_requests": local,
                                "gcp_requests": gcp,
                                "successful_decisions": successful,
                                "success_rate": round(
                                    (successful / total * 100) if total > 0 else 0, 2
                                ),
                                "gcp_routing_ratio": round(gcp / total if total > 0 else 0, 3),
                                "average_local_ram_percent": round(row[3] or 0, 1),
                                "max_local_ram_percent": round(row[4] or 0, 1),
                                "average_decision_latency_ms": round(row[5] or 0, 2),
                            }

            return {
                "period": period,
                "total_requests": 0,
                "local_requests": 0,
                "gcp_requests": 0,
                "successful_decisions": 0,
                "success_rate": 0.0,
                "gcp_routing_ratio": 0.0,
                "average_local_ram_percent": 0.0,
                "max_local_ram_percent": 0.0,
                "average_decision_latency_ms": 0.0,
            }

        except Exception as e:
            logger.error(f"Failed to get routing metrics: {e}")
            return {}

    async def _check_cost_alerts(self):
        """Check cost thresholds and trigger alerts"""
        checks = [
            ("day", self.config.alert_threshold_daily, "daily"),
            ("week", self.config.alert_threshold_weekly, "weekly"),
            ("month", self.config.alert_threshold_monthly, "monthly"),
        ]

        for period, threshold, label in checks:
            summary = await self.get_cost_summary(period)
            actual_cost = summary.get("total_estimated_cost", 0)

            if actual_cost > threshold:
                await self._log_alert(
                    AlertLevel.WARNING,
                    f"cost_threshold_{label}",
                    f"{label.capitalize()} cost ${actual_cost:.2f} exceeds threshold ${threshold:.2f}",
                    summary,
                )

        # Check performance thresholds
        avg_lifetime = self.metrics.average_vm_lifetime_hours
        if avg_lifetime > self.config.max_vm_lifetime_hours:
            await self._log_alert(
                AlertLevel.WARNING,
                "vm_lifetime_threshold",
                f"Average VM lifetime {avg_lifetime:.2f}h exceeds threshold {self.config.max_vm_lifetime_hours}h",
                {"average_vm_lifetime_hours": avg_lifetime},
            )

    async def _log_alert(
        self, level: AlertLevel, alert_type: str, message: str, details: Optional[Dict] = None
    ):
        """Log alert to database and trigger notifications"""
        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    await db.execute(
                        """
                        INSERT INTO alert_history (timestamp, alert_level, alert_type, message, details)
                        VALUES (?, ?, ?, ?, ?)
                    """,
                        (
                            datetime.utcnow().isoformat(),
                            level.value,
                            alert_type,
                            message,
                            json.dumps(details or {}),
                        ),
                    )
                    await db.commit()

            # Log to console
            log_func = {
                AlertLevel.INFO: logger.info,
                AlertLevel.WARNING: logger.warning,
                AlertLevel.CRITICAL: logger.critical,
                AlertLevel.EMERGENCY: logger.critical,
            }.get(level, logger.warning)

            log_func(f"💰 ALERT [{level.value.upper()}]: {message}")

            # v3.0: Publish alert to Redis for real-time WebSocket updates
            await self._publish_event(REDIS_CHANNEL_ALERTS, {
                "event_type": "alert",
                "level": level.value,
                "alert_type": alert_type,
                "message": message,
                "details": details,
            })

            # Send notifications
            if self.config.enable_desktop_notifications:
                await self._send_desktop_notification(f"{level.value.upper()}: {message}")

            # Send email alerts for medium/high severity
            if level in [AlertLevel.MEDIUM, AlertLevel.HIGH, AlertLevel.CRITICAL, AlertLevel.EMERGENCY]:
                await self._send_email_alert(
                    subject=f"{level.value.upper()} Alert: {alert_type}", message=message
                )

            # Trigger callbacks (for backward compatibility)
            await self._notify_event(
                "alert", {"level": level.value, "type": alert_type, "message": message}
            )

        except Exception as e:
            logger.error(f"Failed to log alert: {e}")

    async def _send_desktop_notification(self, message: str):
        """Send desktop notification (macOS/Linux)"""
        try:
            if os.path.exists("/usr/bin/osascript"):  # macOS
                cmd = [
                    "osascript",
                    "-e",
                    f'display notification "{message}" with title "JARVIS Cost Alert" sound name "Purr"',
                ]
                await asyncio.create_subprocess_exec(*cmd)
        except Exception as e:
            logger.debug(f"Desktop notification failed: {e}")

    async def _send_email_alert(self, subject: str, message: str):
        """Send email alert notification (async)"""
        if not self.config.enable_email_alerts or not self.config.alert_email:
            return

        try:
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            import aiosmtplib

            # Email configuration from environment
            smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
            smtp_port = int(os.getenv("SMTP_PORT", "587"))
            smtp_user = os.getenv("SMTP_USER", self.config.alert_email)
            smtp_password = os.getenv("SMTP_PASSWORD", "")

            if not smtp_password:
                logger.debug("SMTP password not configured - skipping email")
                return

            # Create email message
            msg = MIMEMultipart("alternative")
            msg["From"] = smtp_user
            msg["To"] = self.config.alert_email
            msg["Subject"] = f"🤖 JARVIS: {subject}"

            # HTML email content
            html = f"""
            <html>
              <head></head>
              <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h2 style="color: #2196F3;">🤖 JARVIS Cost Alert</h2>
                <div style="background-color: #f5f5f5; padding: 15px; border-radius: 5px;">
                  <h3>{subject}</h3>
                  <p style="white-space: pre-wrap;">{message}</p>
                </div>
                <hr style="margin: 20px 0;">
                <p style="color: #666; font-size: 12px;">
                  This is an automated alert from JARVIS Hybrid Cloud Intelligence.<br>
                  <a href="http://localhost:8010/hybrid/status">View Cost Dashboard</a>
                </p>
              </body>
            </html>
            """

            # Plain text fallback
            text = f"JARVIS Cost Alert\n\n{subject}\n\n{message}"

            msg.attach(MIMEText(text, "plain"))
            msg.attach(MIMEText(html, "html"))

            # Send email asynchronously
            await aiosmtplib.send(
                msg,
                hostname=smtp_server,
                port=smtp_port,
                username=smtp_user,
                password=smtp_password,
                start_tls=True,
            )

            logger.info(f"📧 Email alert sent to {self.config.alert_email}")

        except ImportError:
            logger.warning("aiosmtplib not installed - install via: pip install aiosmtplib")
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")

    async def get_orphaned_vms_report(self) -> Dict[str, Any]:
        """Get comprehensive orphaned VMs report"""
        try:
            import aiosqlite

            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    async with db.execute(
                        """
                        SELECT instance_id, created_at, deleted_at, runtime_hours,
                               estimated_cost, vm_type, region, zone
                        FROM vm_sessions
                        WHERE is_orphaned = 1
                        ORDER BY created_at DESC
                        LIMIT 100
                    """
                    ) as cursor:
                        rows = await cursor.fetchall()

                        orphaned_vms = []
                        for row in rows:
                            orphaned_vms.append(
                                {
                                    "instance_id": row[0],
                                    "created_at": row[1],
                                    "deleted_at": row[2],
                                    "runtime_hours": round(row[3], 2),
                                    "estimated_cost": round(row[4], 4),
                                    "vm_type": row[5],
                                    "region": row[6],
                                    "zone": row[7],
                                }
                            )

                        return {
                            "total_orphaned_vms": len(orphaned_vms),
                            "total_orphaned_cost": sum(vm["estimated_cost"] for vm in orphaned_vms),
                            "orphaned_vms": orphaned_vms,
                            "max_age_hours": self.config.orphaned_vm_max_age_hours,
                        }

        except Exception as e:
            logger.error(f"Failed to get orphaned VMs report: {e}")
            return {
                "total_orphaned_vms": 0,
                "total_orphaned_cost": 0,
                "orphaned_vms": [],
                "max_age_hours": self.config.orphaned_vm_max_age_hours,
            }

    def register_alert_callback(self, callback: Callable):
        """Register callback for alert events"""
        self._alert_callbacks.append(callback)

    async def _notify_event(self, event_type: str, data: Dict[str, Any]):
        """Notify registered callbacks of events"""
        for callback in self._alert_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event_type, data)
                else:
                    callback(event_type, data)
            except Exception as e:
                logger.error(f"Alert callback failed: {e}")

    # =========================================================================
    # v2.0: BUDGET ENFORCEMENT (Solo Developer Protection)
    # =========================================================================

    async def can_create_vm(self) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Check if VM creation is allowed based on budget constraints.
        
        This is the key method for hard budget enforcement.
        Called by gcp_vm_manager before creating any VM.
        
        Returns:
            Tuple of (allowed, reason, details)
        """
        details = {
            "daily_budget": self.config.alert_threshold_daily,
            "daily_spent": 0.0,
            "daily_remaining": self.config.alert_threshold_daily,
            "budget_percent_used": 0.0,
            "hard_enforcement": self.config.hard_budget_enforcement,
            "solo_developer_mode": self.config.solo_developer_mode,
            "active_vms": len(self.active_sessions),
        }
        
        try:
            daily_summary = await self.get_cost_summary("day")
            daily_spent = daily_summary.get("total_estimated_cost", 0.0)
            
            # Include cost of currently running VMs
            for session in self.active_sessions.values():
                runtime_cost = session.calculate_cost(self.config.spot_vm_hourly_cost)
                daily_spent += runtime_cost
            
            details["daily_spent"] = round(daily_spent, 4)
            details["daily_remaining"] = round(self.config.alert_threshold_daily - daily_spent, 4)
            details["budget_percent_used"] = round(
                (daily_spent / self.config.alert_threshold_daily * 100) 
                if self.config.alert_threshold_daily > 0 else 0, 1
            )
            
            # Check budget warning threshold
            if details["budget_percent_used"] >= self.config.budget_warning_percent:
                await self._log_alert(
                    AlertLevel.WARNING,
                    "budget_warning",
                    f"Daily budget {details['budget_percent_used']:.0f}% used (${daily_spent:.2f}/${self.config.alert_threshold_daily:.2f})",
                    details,
                )
            
            # Check hard budget limit
            if daily_spent >= self.config.alert_threshold_daily:
                if self.config.hard_budget_enforcement:
                    await self._log_alert(
                        AlertLevel.CRITICAL,
                        "budget_exceeded",
                        f"🚫 DAILY BUDGET EXCEEDED! ${daily_spent:.2f} >= ${self.config.alert_threshold_daily:.2f}",
                        details,
                    )
                    return False, f"Daily budget exceeded: ${daily_spent:.2f}", details
                else:
                    # Soft enforcement - just warn
                    await self._log_alert(
                        AlertLevel.HIGH,
                        "budget_exceeded_soft",
                        f"⚠️ Daily budget exceeded (soft mode): ${daily_spent:.2f}",
                        details,
                    )
            
            # Check forecast (will we exceed budget soon?)
            if self.config.enable_cost_forecasting:
                forecast = await self.forecast_daily_cost()
                if forecast.get("predicted_cost", 0) > self.config.alert_threshold_daily:
                    details["forecast"] = forecast
                    await self._log_alert(
                        AlertLevel.WARNING,
                        "budget_forecast_warning",
                        f"📈 Forecast: Today's cost likely to reach ${forecast['predicted_cost']:.2f}",
                        details,
                    )
            
            return True, "Budget OK", details
            
        except Exception as e:
            logger.error(f"Budget check failed: {e}")
            # On error, allow VM creation (don't block due to tracking issues)
            return True, f"Budget check error (allowing): {e}", details

    async def forecast_daily_cost(self) -> Dict[str, Any]:
        """
        Simple cost forecasting using recent trends.
        
        Uses weighted average of recent daily costs to predict today's total.
        More recent days have higher weight.
        
        Returns:
            Dict with prediction details
        """
        try:
            import aiosqlite
            
            # Get historical daily costs
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=self.config.forecast_history_days)
            
            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    async with db.execute(
                        """
                        SELECT 
                            DATE(created_at) as day,
                            SUM(estimated_cost) as daily_cost,
                            COUNT(*) as vm_count
                        FROM vm_sessions
                        WHERE created_at >= ?
                        GROUP BY DATE(created_at)
                        ORDER BY day DESC
                        """,
                        (start_date.isoformat(),),
                    ) as cursor:
                        rows = await cursor.fetchall()
            
            if not rows:
                return {
                    "predicted_cost": 0.0,
                    "confidence": 0.0,
                    "method": "no_data",
                    "history_days": 0,
                }
            
            # Extract daily costs
            daily_costs = [row[1] or 0.0 for row in rows]
            
            if len(daily_costs) < 2:
                return {
                    "predicted_cost": daily_costs[0] if daily_costs else 0.0,
                    "confidence": 0.3,
                    "method": "single_day",
                    "history_days": len(daily_costs),
                }
            
            # Weighted average (recent days have higher weight)
            weights = list(range(len(daily_costs), 0, -1))
            weighted_avg = sum(c * w for c, w in zip(daily_costs, weights)) / sum(weights)
            
            # Calculate trend (is cost increasing or decreasing?)
            if len(daily_costs) >= 3:
                recent_avg = statistics.mean(daily_costs[:3])
                older_avg = statistics.mean(daily_costs[3:]) if len(daily_costs) > 3 else daily_costs[-1]
                trend = (recent_avg - older_avg) / older_avg if older_avg > 0 else 0
            else:
                trend = 0
            
            # Add today's current spend
            today_summary = await self.get_cost_summary("day")
            today_so_far = today_summary.get("total_estimated_cost", 0.0)
            
            # Hours left in day
            now = datetime.utcnow()
            hours_elapsed = now.hour + now.minute / 60
            hours_remaining = 24 - hours_elapsed
            
            # Projected additional cost based on hourly rate
            if hours_elapsed > 0:
                hourly_rate = today_so_far / hours_elapsed
                projected_addition = hourly_rate * hours_remaining
            else:
                projected_addition = weighted_avg  # Use historical if no data today
            
            predicted_cost = today_so_far + projected_addition
            
            # Apply trend adjustment
            if trend > 0:
                predicted_cost *= (1 + min(trend, 0.5))  # Cap trend at 50%
            
            # Confidence based on data amount and variability
            if len(daily_costs) >= 7:
                confidence = 0.8
            elif len(daily_costs) >= 3:
                confidence = 0.6
            else:
                confidence = 0.4
            
            # Reduce confidence if high variability
            if len(daily_costs) >= 2:
                cv = statistics.stdev(daily_costs) / statistics.mean(daily_costs) if statistics.mean(daily_costs) > 0 else 0
                confidence *= max(0.5, 1 - cv)
            
            return {
                "predicted_cost": round(predicted_cost, 4),
                "today_so_far": round(today_so_far, 4),
                "projected_addition": round(projected_addition, 4),
                "weighted_avg_daily": round(weighted_avg, 4),
                "trend": round(trend, 2),
                "confidence": round(confidence, 2),
                "method": "weighted_trend",
                "history_days": len(daily_costs),
                "hours_remaining": round(hours_remaining, 1),
            }
            
        except Exception as e:
            logger.error(f"Cost forecasting failed: {e}")
            return {
                "predicted_cost": 0.0,
                "confidence": 0.0,
                "method": "error",
                "error": str(e),
            }

    async def get_budget_status(self) -> Dict[str, Any]:
        """
        Get comprehensive budget status for dashboard/API.
        
        v3.0: Uses Redis caching for fast access.
        
        Returns:
            Dict with all budget-related metrics
        """
        # v3.0: Try cache first
        cached = await self._cache_get(REDIS_KEY_BUDGET_STATUS)
        if cached:
            # Add real-time active VM data (not cached)
            active_vm_cost = 0.0
            for session in self.active_sessions.values():
                active_vm_cost += session.calculate_cost(self.config.spot_vm_hourly_cost)
            cached["active_vms"]["count"] = len(self.active_sessions)
            cached["active_vms"]["current_cost"] = round(active_vm_cost, 4)
            return cached
        
        daily = await self.get_cost_summary("day")
        weekly = await self.get_cost_summary("week")
        monthly = await self.get_cost_summary("month")
        forecast = await self.forecast_daily_cost() if self.config.enable_cost_forecasting else {}
        
        # Calculate active VM costs
        active_vm_cost = 0.0
        for session in self.active_sessions.values():
            active_vm_cost += session.calculate_cost(self.config.spot_vm_hourly_cost)
        
        result = {
            "mode": "solo_developer" if self.config.solo_developer_mode else "team",
            "hard_enforcement": self.config.hard_budget_enforcement,
            "budgets": {
                "daily": {
                    "limit": self.config.alert_threshold_daily,
                    "spent": daily.get("total_estimated_cost", 0.0),
                    "remaining": max(0, self.config.alert_threshold_daily - daily.get("total_estimated_cost", 0.0)),
                    "percent_used": round(
                        daily.get("total_estimated_cost", 0.0) / self.config.alert_threshold_daily * 100
                        if self.config.alert_threshold_daily > 0 else 0, 1
                    ),
                },
                "weekly": {
                    "limit": self.config.alert_threshold_weekly,
                    "spent": weekly.get("total_estimated_cost", 0.0),
                    "remaining": max(0, self.config.alert_threshold_weekly - weekly.get("total_estimated_cost", 0.0)),
                },
                "monthly": {
                    "limit": self.config.alert_threshold_monthly,
                    "spent": monthly.get("total_estimated_cost", 0.0),
                    "remaining": max(0, self.config.alert_threshold_monthly - monthly.get("total_estimated_cost", 0.0)),
                },
            },
            "active_vms": {
                "count": len(self.active_sessions),
                "current_cost": round(active_vm_cost, 4),
                "instances": [
                    {
                        "id": s.instance_id,
                        "runtime_hours": round(s.runtime_hours, 2),
                        "cost": round(s.calculate_cost(self.config.spot_vm_hourly_cost), 4),
                    }
                    for s in self.active_sessions.values()
                ],
            },
            "forecast": forecast,
            "triple_lock_status": {
                "max_vm_lifetime_hours": self.config.max_vm_lifetime_hours,
                "orphaned_vm_max_age_hours": self.config.orphaned_vm_max_age_hours,
                "cleanup_interval_hours": self.config.cleanup_check_interval_hours,
            },
            "redis_status": {
                "available": self._redis_available,
                "pubsub_enabled": self.config.redis_pubsub_enabled,
                "websocket_subscribers": len(self._websocket_subscribers),
            },
            "generated_at": datetime.utcnow().isoformat(),
        }
        
        # v3.0: Cache the result
        await self._cache_set(REDIS_KEY_BUDGET_STATUS, result, ttl=30)  # 30 second cache
        
        return result

    # =========================================================================
    # v2.0: SHUTDOWN HOOK INTEGRATION
    # =========================================================================

    async def record_shutdown_cleanup(
        self,
        cleanup_result: Dict[str, Any],
        reason: str = "shutdown_hook",
    ) -> None:
        """
        Record cleanup from shutdown hook for cost tracking.
        
        Called by shutdown_hook.py after cleaning up VMs.
        
        Args:
            cleanup_result: Result dict from cleanup_remote_resources()
            reason: Reason for cleanup
        """
        vms_cleaned = cleanup_result.get("vms_cleaned", 0)
        method = cleanup_result.get("method", "unknown")
        
        if vms_cleaned == 0:
            logger.debug(f"💰 Shutdown cleanup recorded: 0 VMs (method: {method})")
            return
        
        logger.info(f"💰 Recording shutdown cleanup: {vms_cleaned} VM(s) via {method}")
        
        # Mark any remaining active sessions as terminated
        for instance_id in list(self.active_sessions.keys()):
            await self.record_vm_deleted(
                instance_id=instance_id,
                was_orphaned=True,
                actual_cost=None,
            )
        
        # Log the cleanup event
        await self._log_alert(
            AlertLevel.INFO,
            "shutdown_cleanup",
            f"Shutdown hook cleaned {vms_cleaned} VM(s) via {method}",
            cleanup_result,
        )


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_cost_tracker_instance: Optional[CostTracker] = None


def get_cost_tracker(config: Optional[CostTrackerConfig] = None) -> CostTracker:
    """Get or create global cost tracker instance"""
    global _cost_tracker_instance

    if _cost_tracker_instance is None:
        _cost_tracker_instance = CostTracker(config)

    return _cost_tracker_instance


async def initialize_cost_tracking(config: Optional[CostTrackerConfig] = None):
    """Initialize cost tracking system"""
    tracker = get_cost_tracker(config)
    await tracker.initialize()
    logger.info("💰 Advanced cost tracking system initialized")
