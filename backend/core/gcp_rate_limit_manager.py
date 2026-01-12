#!/usr/bin/env python3
"""
GCP Rate Limit & Quota Manager v1.0
====================================

Centralized, intelligent rate limit and quota management for all GCP services.
Prevents API throttling, reduces errors, and optimizes resource usage.

Features:
- Token bucket rate limiting for all GCP APIs
- Sliding window request tracking
- Pre-flight rate limit checks before API calls
- Quota caching with intelligent refresh
- Circuit breaker integration
- Async/await throughout
- Dynamic configuration from environment
- Comprehensive metrics and observability
- No hardcoding - all limits configurable

GCP Rate Limits (Default - can be overridden):
┌─────────────────────────────────────┬────────────────────┬─────────────────────┐
│ API Service                         │ Read Rate          │ Write Rate          │
├─────────────────────────────────────┼────────────────────┼─────────────────────┤
│ Compute Engine                      │ 20 req/sec         │ 5 req/sec           │
│ Cloud SQL Admin                     │ 180 req/100sec     │ 60 req/100sec       │
│ Cloud Run Admin                     │ 60 req/min         │ 30 req/min          │
│ Cloud Storage                       │ 5000 req/sec       │ 1000 req/sec        │
│ IAM                                 │ 120 req/min        │ 60 req/min          │
│ Cloud Logging                       │ 60 req/sec         │ 60 req/sec          │
└─────────────────────────────────────┴────────────────────┴─────────────────────┘

Architecture:
┌─────────────────────────────────────────────────────────────────────────────┐
│                     GCP Rate Limit Manager v1.0                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │ Token Bucket     │  │ Sliding Window   │  │ Quota Cache      │          │
│  │ Rate Limiter     │  │ Request Tracker  │  │ Manager          │          │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘          │
│           └─────────────────────┼─────────────────────┘                     │
│                                 ▼                                           │
│           ┌─────────────────────────────────────────┐                       │
│           │         Rate Limit Coordinator          │                       │
│           │  • Pre-flight checks                    │                       │
│           │  • Backpressure signaling              │                       │
│           │  • Adaptive throttling                  │                       │
│           │  • Retry scheduling                     │                       │
│           └─────────────────────────────────────────┘                       │
└─────────────────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, TypeVar

from backend.core.async_safety import LazyAsyncLock, get_shutdown_event

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# GCP SERVICE DEFINITIONS
# =============================================================================

class GCPService(Enum):
    """GCP services that have rate limits."""
    COMPUTE_ENGINE = "compute"
    CLOUD_SQL = "sqladmin"
    CLOUD_RUN = "run"
    CLOUD_STORAGE = "storage"
    IAM = "iam"
    CLOUD_LOGGING = "logging"
    CLOUD_FUNCTIONS = "cloudfunctions"
    PUBSUB = "pubsub"
    SECRET_MANAGER = "secretmanager"


class OperationType(Enum):
    """Types of API operations."""
    READ = "read"
    WRITE = "write"
    LIST = "list"
    DELETE = "delete"


@dataclass
class RateLimitConfig:
    """
    Rate limit configuration for a GCP service.
    All rates are requests per window_seconds.
    """
    service: GCPService
    read_limit: int  # Max read requests per window
    write_limit: int  # Max write requests per window
    list_limit: int  # Max list requests per window
    delete_limit: int  # Max delete requests per window
    window_seconds: float  # Time window for rate calculation
    burst_multiplier: float = 1.5  # Allow burst up to this multiplier
    cooldown_seconds: float = 60.0  # Cooldown after hitting limit
    
    @classmethod
    def from_env(cls, service: GCPService) -> "RateLimitConfig":
        """Load rate limit config from environment variables."""
        prefix = f"GCP_{service.value.upper()}_"
        
        # Default rate limits based on GCP documentation
        defaults = {
            GCPService.COMPUTE_ENGINE: (20, 5, 20, 5, 1.0),
            GCPService.CLOUD_SQL: (180, 60, 180, 30, 100.0),
            GCPService.CLOUD_RUN: (60, 30, 60, 30, 60.0),
            GCPService.CLOUD_STORAGE: (5000, 1000, 5000, 500, 1.0),
            GCPService.IAM: (120, 60, 120, 30, 60.0),
            GCPService.CLOUD_LOGGING: (60, 60, 60, 60, 1.0),
            GCPService.CLOUD_FUNCTIONS: (40, 20, 40, 20, 60.0),
            GCPService.PUBSUB: (100, 100, 100, 50, 1.0),
            GCPService.SECRET_MANAGER: (60, 30, 60, 30, 60.0),
        }
        
        default_read, default_write, default_list, default_delete, default_window = defaults.get(
            service, (60, 30, 60, 30, 60.0)
        )
        
        return cls(
            service=service,
            read_limit=int(os.getenv(f"{prefix}READ_LIMIT", default_read)),
            write_limit=int(os.getenv(f"{prefix}WRITE_LIMIT", default_write)),
            list_limit=int(os.getenv(f"{prefix}LIST_LIMIT", default_list)),
            delete_limit=int(os.getenv(f"{prefix}DELETE_LIMIT", default_delete)),
            window_seconds=float(os.getenv(f"{prefix}WINDOW_SECONDS", default_window)),
            burst_multiplier=float(os.getenv(f"{prefix}BURST_MULTIPLIER", 1.5)),
            cooldown_seconds=float(os.getenv(f"{prefix}COOLDOWN_SECONDS", 60.0)),
        )


# =============================================================================
# TOKEN BUCKET IMPLEMENTATION
# =============================================================================

@dataclass
class TokenBucket:
    """
    Token bucket rate limiter with async support.
    
    Allows bursting up to capacity, then rate-limits to refill_rate.
    """
    capacity: float  # Maximum tokens
    refill_rate: float  # Tokens added per second
    tokens: float = field(init=False)
    last_refill: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    
    def __post_init__(self):
        self.tokens = self.capacity
    
    async def acquire(self, tokens: float = 1.0, timeout: float = 30.0) -> bool:
        """
        Acquire tokens from the bucket with shutdown protection.

        Args:
            tokens: Number of tokens to acquire
            timeout: Maximum time to wait for tokens

        Returns:
            True if tokens acquired, False if timeout or shutdown
        """
        start_time = time.time()
        shutdown_event = get_shutdown_event()

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.debug("Token bucket acquire cancelled due to shutdown")
                return False

            async with self._lock:
                # Refill tokens based on elapsed time
                now = time.time()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                self.last_refill = now

                # Check if we have enough tokens
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

                # Calculate wait time for enough tokens
                tokens_needed = tokens - self.tokens
                wait_time = tokens_needed / self.refill_rate

            # Check timeout
            if time.time() - start_time + wait_time > timeout:
                return False

            # Wait for tokens to refill
            await asyncio.sleep(min(wait_time, 0.1))
    
    async def try_acquire(self, tokens: float = 1.0) -> bool:
        """
        Try to acquire tokens without waiting.
        
        Returns:
            True if tokens available and acquired, False otherwise
        """
        async with self._lock:
            # Refill tokens
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False
    
    @property
    def available_tokens(self) -> float:
        """Get current available tokens (may be stale)."""
        return self.tokens
    
    @property
    def utilization(self) -> float:
        """Get utilization as percentage (0-100)."""
        return ((self.capacity - self.tokens) / self.capacity) * 100 if self.capacity > 0 else 100


# =============================================================================
# SLIDING WINDOW REQUEST TRACKER
# =============================================================================

@dataclass
class SlidingWindowTracker:
    """
    Sliding window request tracker for precise rate limiting.
    
    Tracks exact timestamps of requests within the window.
    """
    window_seconds: float
    max_requests: int
    requests: Deque[float] = field(default_factory=deque)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    
    async def record_request(self) -> bool:
        """
        Record a request and check if within limit.
        
        Returns:
            True if request is allowed, False if rate limit exceeded
        """
        async with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            
            # Remove old requests outside the window
            while self.requests and self.requests[0] < cutoff:
                self.requests.popleft()
            
            # Check if we can add a new request
            if len(self.requests) >= self.max_requests:
                return False
            
            self.requests.append(now)
            return True
    
    async def can_make_request(self) -> Tuple[bool, float]:
        """
        Check if a request can be made without recording it.
        
        Returns:
            (can_make, wait_time_seconds)
        """
        async with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            
            # Remove old requests
            while self.requests and self.requests[0] < cutoff:
                self.requests.popleft()
            
            if len(self.requests) < self.max_requests:
                return True, 0.0
            
            # Calculate when the oldest request will expire
            oldest = self.requests[0]
            wait_time = oldest + self.window_seconds - now
            return False, max(0.0, wait_time)
    
    @property
    def current_rate(self) -> float:
        """Get current request rate (requests per window)."""
        return len(self.requests)
    
    @property
    def utilization(self) -> float:
        """Get utilization as percentage (0-100)."""
        return (len(self.requests) / self.max_requests) * 100 if self.max_requests > 0 else 100


# =============================================================================
# SERVICE RATE LIMITER
# =============================================================================

@dataclass
class ServiceRateLimiter:
    """
    Rate limiter for a specific GCP service.
    Combines token bucket (for bursting) and sliding window (for precise limits).
    """
    config: RateLimitConfig
    
    # Token buckets for each operation type
    read_bucket: TokenBucket = field(init=False)
    write_bucket: TokenBucket = field(init=False)
    list_bucket: TokenBucket = field(init=False)
    delete_bucket: TokenBucket = field(init=False)
    
    # Sliding window trackers
    read_tracker: SlidingWindowTracker = field(init=False)
    write_tracker: SlidingWindowTracker = field(init=False)
    list_tracker: SlidingWindowTracker = field(init=False)
    delete_tracker: SlidingWindowTracker = field(init=False)
    
    # Cooldown tracking
    cooldown_until: float = 0.0
    
    # Statistics
    stats: Dict[str, int] = field(default_factory=lambda: {
        "read_requests": 0,
        "write_requests": 0,
        "list_requests": 0,
        "delete_requests": 0,
        "rate_limited": 0,
        "cooldowns": 0,
    })
    
    def __post_init__(self):
        # Initialize token buckets with burst capacity
        burst = self.config.burst_multiplier
        window = self.config.window_seconds
        
        self.read_bucket = TokenBucket(
            capacity=self.config.read_limit * burst,
            refill_rate=self.config.read_limit / window
        )
        self.write_bucket = TokenBucket(
            capacity=self.config.write_limit * burst,
            refill_rate=self.config.write_limit / window
        )
        self.list_bucket = TokenBucket(
            capacity=self.config.list_limit * burst,
            refill_rate=self.config.list_limit / window
        )
        self.delete_bucket = TokenBucket(
            capacity=self.config.delete_limit * burst,
            refill_rate=self.config.delete_limit / window
        )
        
        # Initialize sliding window trackers
        self.read_tracker = SlidingWindowTracker(window, self.config.read_limit)
        self.write_tracker = SlidingWindowTracker(window, self.config.write_limit)
        self.list_tracker = SlidingWindowTracker(window, self.config.list_limit)
        self.delete_tracker = SlidingWindowTracker(window, self.config.delete_limit)
    
    def _get_bucket_and_tracker(
        self, op_type: OperationType
    ) -> Tuple[TokenBucket, SlidingWindowTracker]:
        """Get the bucket and tracker for an operation type."""
        if op_type == OperationType.READ:
            return self.read_bucket, self.read_tracker
        elif op_type == OperationType.WRITE:
            return self.write_bucket, self.write_tracker
        elif op_type == OperationType.LIST:
            return self.list_bucket, self.list_tracker
        else:  # DELETE
            return self.delete_bucket, self.delete_tracker
    
    async def can_make_request(self, op_type: OperationType) -> Tuple[bool, float, str]:
        """
        Check if a request can be made.
        
        Returns:
            (can_make, wait_time_seconds, reason)
        """
        # Check cooldown
        if time.time() < self.cooldown_until:
            wait = self.cooldown_until - time.time()
            return False, wait, f"Cooldown active ({wait:.1f}s remaining)"
        
        bucket, tracker = self._get_bucket_and_tracker(op_type)
        
        # Check sliding window first (hard limit)
        can_window, window_wait = await tracker.can_make_request()
        if not can_window:
            return False, window_wait, f"Sliding window limit ({tracker.current_rate}/{tracker.max_requests})"
        
        # Check token bucket (allows bursting)
        can_bucket = await bucket.try_acquire()
        if not can_bucket:
            # Calculate wait time based on refill rate
            wait = 1.0 / bucket.refill_rate
            return False, wait, f"Token bucket depleted ({bucket.available_tokens:.1f}/{bucket.capacity:.0f})"
        
        # Request allowed (bucket already consumed a token)
        return True, 0.0, "OK"
    
    async def acquire(
        self, op_type: OperationType, timeout: float = 30.0
    ) -> Tuple[bool, str]:
        """
        Acquire permission to make a request, waiting if necessary.
        
        Returns:
            (acquired, reason)
        """
        start = time.time()
        
        while time.time() - start < timeout:
            can_make, wait_time, reason = await self.can_make_request(op_type)
            
            if can_make:
                # Record the request in the sliding window
                _, tracker = self._get_bucket_and_tracker(op_type)
                await tracker.record_request()
                
                # Update stats
                stat_key = f"{op_type.value}_requests"
                self.stats[stat_key] = self.stats.get(stat_key, 0) + 1
                
                return True, reason
            
            if wait_time > timeout - (time.time() - start):
                # Would exceed timeout
                self.stats["rate_limited"] = self.stats.get("rate_limited", 0) + 1
                return False, f"Would exceed timeout ({reason})"
            
            # Wait and retry
            await asyncio.sleep(min(wait_time, 1.0))
        
        self.stats["rate_limited"] = self.stats.get("rate_limited", 0) + 1
        return False, "Timeout waiting for rate limit"
    
    def enter_cooldown(self, reason: str = ""):
        """Enter cooldown mode (e.g., after receiving 429 from API)."""
        self.cooldown_until = time.time() + self.config.cooldown_seconds
        self.stats["cooldowns"] = self.stats.get("cooldowns", 0) + 1
        logger.warning(
            f"⏳ {self.config.service.value} entering {self.config.cooldown_seconds}s cooldown: {reason}"
        )
    
    def get_status(self) -> Dict[str, Any]:
        """Get current rate limiter status."""
        return {
            "service": self.config.service.value,
            "in_cooldown": time.time() < self.cooldown_until,
            "cooldown_remaining": max(0, self.cooldown_until - time.time()),
            "read": {
                "bucket_tokens": self.read_bucket.available_tokens,
                "bucket_capacity": self.read_bucket.capacity,
                "window_requests": self.read_tracker.current_rate,
                "window_limit": self.read_tracker.max_requests,
                "utilization": self.read_tracker.utilization,
            },
            "write": {
                "bucket_tokens": self.write_bucket.available_tokens,
                "bucket_capacity": self.write_bucket.capacity,
                "window_requests": self.write_tracker.current_rate,
                "window_limit": self.write_tracker.max_requests,
                "utilization": self.write_tracker.utilization,
            },
            "stats": self.stats,
        }


# =============================================================================
# QUOTA MANAGER
# =============================================================================

@dataclass
class QuotaMetric:
    """A single quota metric with caching."""
    metric_name: str
    limit: float
    usage: float
    region: Optional[str] = None
    last_updated: float = field(default_factory=time.time)
    cache_ttl_seconds: float = 60.0
    
    @property
    def available(self) -> float:
        return max(0, self.limit - self.usage)
    
    @property
    def is_exceeded(self) -> bool:
        return self.usage >= self.limit
    
    @property
    def utilization_percent(self) -> float:
        return (self.usage / self.limit * 100) if self.limit > 0 else 100
    
    @property
    def is_stale(self) -> bool:
        return time.time() - self.last_updated > self.cache_ttl_seconds


class QuotaManager:
    """
    Manages GCP quota information with caching and pre-flight checks.
    """
    
    def __init__(self):
        self._quota_cache: Dict[str, QuotaMetric] = {}
        self._cache_lock = asyncio.Lock()
        self._project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", ""))
        self._regions_client = None
        self._initialized = False
        self._init_lock = asyncio.Lock()
        
        # Quota check cooldown (avoid hammering the API)
        self._last_quota_check: Dict[str, float] = {}
        self._quota_check_interval = 30.0  # Minimum seconds between checks
        
        # Stats
        self.stats = {
            "quota_checks": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "quota_exceeded_detections": 0,
        }
    
    async def initialize(self):
        """Initialize the quota manager with GCP clients."""
        if self._initialized:
            return
        
        async with self._init_lock:
            if self._initialized:
                return
            
            try:
                from google.cloud import compute_v1
                self._regions_client = await asyncio.to_thread(compute_v1.RegionsClient)
                self._initialized = True
                logger.info("✅ QuotaManager initialized with GCP Regions client")
            except ImportError:
                logger.warning("⚠️ google-cloud-compute not installed, quota checking limited")
            except Exception as e:
                logger.warning(f"⚠️ QuotaManager initialization failed: {e}")
    
    async def get_quota(
        self, metric_name: str, region: Optional[str] = None, force_refresh: bool = False
    ) -> Optional[QuotaMetric]:
        """
        Get a quota metric, using cache when available.
        
        Args:
            metric_name: Name of the quota metric (e.g., "CPUS_ALL_REGIONS")
            region: Optional region for regional quotas
            force_refresh: Force refresh from API even if cached
            
        Returns:
            QuotaMetric or None if not found
        """
        cache_key = f"{metric_name}:{region or 'global'}"
        
        # Check cache first
        async with self._cache_lock:
            if cache_key in self._quota_cache and not force_refresh:
                cached = self._quota_cache[cache_key]
                if not cached.is_stale:
                    self.stats["cache_hits"] += 1
                    return cached
        
        # Rate limit quota checks
        now = time.time()
        last_check = self._last_quota_check.get(cache_key, 0)
        if now - last_check < self._quota_check_interval and not force_refresh:
            # Return stale cache if available
            async with self._cache_lock:
                if cache_key in self._quota_cache:
                    return self._quota_cache[cache_key]
            return None
        
        self._last_quota_check[cache_key] = now
        self.stats["cache_misses"] += 1
        self.stats["quota_checks"] += 1
        
        # Fetch from API
        if not self._initialized:
            await self.initialize()
        
        if not self._regions_client or not self._project_id:
            return None
        
        try:
            target_region = region or os.getenv("GCP_REGION", "us-central1")
            
            region_info = await asyncio.to_thread(
                self._regions_client.get,
                project=self._project_id,
                region=target_region
            )
            
            for quota in region_info.quotas:
                if quota.metric == metric_name:
                    quota_metric = QuotaMetric(
                        metric_name=metric_name,
                        limit=quota.limit,
                        usage=quota.usage,
                        region=target_region,
                    )
                    
                    async with self._cache_lock:
                        self._quota_cache[cache_key] = quota_metric
                    
                    if quota_metric.is_exceeded:
                        self.stats["quota_exceeded_detections"] += 1
                    
                    return quota_metric
            
            return None
            
        except Exception as e:
            logger.debug(f"Could not fetch quota {metric_name}: {e}")
            return None
    
    async def check_quotas_for_vm_creation(self) -> Tuple[bool, List[QuotaMetric], str]:
        """
        Check all quotas required for VM creation.
        
        Returns:
            (can_create, blocking_quotas, message)
        """
        required_quotas = ["CPUS_ALL_REGIONS", "IN_USE_ADDRESSES", "INSTANCES"]
        
        blocking = []
        warnings = []
        
        # Check quotas in parallel
        tasks = [self.get_quota(q) for q in required_quotas]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"Quota check error for {required_quotas[i]}: {result}")
                continue
            if result is None:
                continue
            
            if result.is_exceeded:
                blocking.append(result)
            elif result.utilization_percent > 80:
                warnings.append(result)
        
        if blocking:
            blocked_names = ", ".join(q.metric_name for q in blocking)
            return False, blocking, f"Quotas exceeded: {blocked_names}"
        
        if warnings:
            warn_names = ", ".join(f"{q.metric_name} ({q.utilization_percent:.0f}%)" for q in warnings)
            return True, warnings, f"High quota utilization: {warn_names}"
        
        return True, [], "All quotas OK"
    
    def get_status(self) -> Dict[str, Any]:
        """Get quota manager status."""
        return {
            "initialized": self._initialized,
            "project_id": self._project_id[:10] + "..." if self._project_id else None,
            "cached_quotas": len(self._quota_cache),
            "stats": self.stats,
        }


# =============================================================================
# CENTRAL RATE LIMIT MANAGER (SINGLETON)
# =============================================================================

class GCPRateLimitManager:
    """
    Central manager for all GCP rate limits and quotas.
    
    Provides a unified interface for rate limiting across all GCP services.
    Singleton pattern ensures consistent state across the application.
    """
    
    _instance: Optional["GCPRateLimitManager"] = None
    _lock = asyncio.Lock()
    
    def __new__(cls) -> "GCPRateLimitManager":
        if cls._instance is None:
            # Note: Actual initialization happens in __init__
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # Initialize rate limiters for each service
        self._rate_limiters: Dict[GCPService, ServiceRateLimiter] = {}
        
        for service in GCPService:
            config = RateLimitConfig.from_env(service)
            self._rate_limiters[service] = ServiceRateLimiter(config=config)
        
        # Initialize quota manager
        self._quota_manager = QuotaManager()
        
        # Global stats
        self._stats = {
            "total_requests": 0,
            "rate_limited_requests": 0,
            "quota_blocked_requests": 0,
            "api_429_responses": 0,
        }
        
        self._initialized = True
        logger.info("✅ GCPRateLimitManager initialized")
    
    async def initialize(self):
        """Async initialization for quota manager."""
        await self._quota_manager.initialize()
    
    # =========================================================================
    # PRE-FLIGHT CHECKS
    # =========================================================================
    
    async def can_make_request(
        self, service: GCPService, op_type: OperationType
    ) -> Tuple[bool, float, str]:
        """
        Check if a request can be made to a GCP service.
        
        Args:
            service: The GCP service
            op_type: Type of operation (READ, WRITE, LIST, DELETE)
            
        Returns:
            (can_make, wait_time_seconds, reason)
        """
        limiter = self._rate_limiters.get(service)
        if not limiter:
            return True, 0.0, "No rate limiter configured"
        
        return await limiter.can_make_request(op_type)
    
    async def acquire(
        self,
        service: GCPService,
        op_type: OperationType,
        timeout: float = 30.0
    ) -> Tuple[bool, str]:
        """
        Acquire permission to make a request, waiting if necessary.
        
        Args:
            service: The GCP service
            op_type: Type of operation
            timeout: Maximum time to wait
            
        Returns:
            (acquired, reason)
        """
        self._stats["total_requests"] += 1
        
        limiter = self._rate_limiters.get(service)
        if not limiter:
            return True, "No rate limiter configured"
        
        acquired, reason = await limiter.acquire(op_type, timeout)
        
        if not acquired:
            self._stats["rate_limited_requests"] += 1
        
        return acquired, reason
    
    async def check_quota_for_vm(self) -> Tuple[bool, List[QuotaMetric], str]:
        """
        Check if quotas allow VM creation.
        
        Returns:
            (can_create, blocking_quotas, message)
        """
        can_create, quotas, message = await self._quota_manager.check_quotas_for_vm_creation()
        
        if not can_create:
            self._stats["quota_blocked_requests"] += 1
        
        return can_create, quotas, message
    
    # =========================================================================
    # ERROR HANDLING
    # =========================================================================
    
    def handle_429_response(self, service: GCPService, retry_after: Optional[float] = None):
        """
        Handle a 429 (Too Many Requests) response from GCP API.
        
        Args:
            service: The service that returned 429
            retry_after: Optional Retry-After header value in seconds
        """
        self._stats["api_429_responses"] += 1
        
        limiter = self._rate_limiters.get(service)
        if limiter:
            cooldown = retry_after or limiter.config.cooldown_seconds
            limiter.cooldown_until = time.time() + cooldown
            limiter.stats["cooldowns"] += 1
            logger.warning(
                f"🚫 {service.value} API returned 429 - entering {cooldown:.0f}s cooldown"
            )
    
    def handle_quota_exceeded(self, service: GCPService, quota_name: str):
        """
        Handle a quota exceeded error from GCP API.
        
        Args:
            service: The service that hit quota
            quota_name: Name of the exceeded quota
        """
        self._stats["quota_blocked_requests"] += 1
        
        limiter = self._rate_limiters.get(service)
        if limiter:
            # Longer cooldown for quota errors (they won't resolve quickly)
            limiter.cooldown_until = time.time() + 300  # 5 minutes
            limiter.enter_cooldown(f"Quota exceeded: {quota_name}")
    
    # =========================================================================
    # DECORATORS
    # =========================================================================
    
    def rate_limited(
        self, service: GCPService, op_type: OperationType, timeout: float = 30.0
    ):
        """
        Decorator to rate limit a function.
        
        Usage:
            @rate_limit_manager.rate_limited(GCPService.COMPUTE_ENGINE, OperationType.WRITE)
            async def create_vm(...):
                ...
        """
        def decorator(func: Callable) -> Callable:
            async def wrapper(*args, **kwargs):
                acquired, reason = await self.acquire(service, op_type, timeout)
                if not acquired:
                    raise RateLimitExceededError(
                        f"Rate limit exceeded for {service.value}.{op_type.value}: {reason}"
                    )
                return await func(*args, **kwargs)
            return wrapper
        return decorator
    
    # =========================================================================
    # STATUS & METRICS
    # =========================================================================
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive status of all rate limiters."""
        return {
            "global_stats": self._stats,
            "services": {
                service.value: limiter.get_status()
                for service, limiter in self._rate_limiters.items()
            },
            "quota_manager": self._quota_manager.get_status(),
        }
    
    def get_service_status(self, service: GCPService) -> Dict[str, Any]:
        """Get status for a specific service."""
        limiter = self._rate_limiters.get(service)
        if limiter:
            return limiter.get_status()
        return {"error": f"No rate limiter for {service.value}"}


# =============================================================================
# EXCEPTIONS
# =============================================================================

class RateLimitExceededError(Exception):
    """Raised when rate limit is exceeded and cannot wait."""
    pass


class QuotaExceededError(Exception):
    """Raised when GCP quota is exceeded."""
    pass


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_manager_instance: Optional[GCPRateLimitManager] = None
_manager_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_rate_limit_manager() -> GCPRateLimitManager:
    """
    Get the singleton GCPRateLimitManager instance.
    
    Usage:
        manager = await get_rate_limit_manager()
        
        # Check before making request
        can_make, wait_time, reason = await manager.can_make_request(
            GCPService.COMPUTE_ENGINE, OperationType.WRITE
        )
        
        # Or acquire with waiting
        acquired, reason = await manager.acquire(
            GCPService.CLOUD_SQL, OperationType.READ
        )
    """
    global _manager_instance
    
    if _manager_instance is None:
        async with _manager_lock:
            if _manager_instance is None:
                _manager_instance = GCPRateLimitManager()
                await _manager_instance.initialize()
    
    return _manager_instance


def get_rate_limit_manager_sync() -> GCPRateLimitManager:
    """
    Synchronous version for non-async contexts.
    Note: Quota manager won't be initialized without async call.
    """
    global _manager_instance
    
    if _manager_instance is None:
        _manager_instance = GCPRateLimitManager()
    
    return _manager_instance


# =============================================================================
# INTELLIGENT ORCHESTRATOR INTEGRATION
# =============================================================================

async def get_intelligent_rate_manager():
    """
    Get the unified intelligent rate orchestrator.
    
    This is the recommended way to access rate limiting in JARVIS v2.0+.
    It provides ML-powered forecasting and adaptive throttling.
    
    Usage:
        orchestrator = await get_intelligent_rate_manager()
        
        # Use with decorator
        @orchestrator.rate_limited(ServiceType.GCP_CLOUD_SQL)
        async def query_db():
            ...
        
        # Or context manager
        async with orchestrator.rate_limit_context(ServiceType.CLAUDE_API):
            response = await claude.messages.create(...)
    """
    try:
        from core.intelligent_rate_orchestrator import get_rate_orchestrator
        return await get_rate_orchestrator()
    except ImportError:
        try:
            from backend.core.intelligent_rate_orchestrator import get_rate_orchestrator
            return await get_rate_orchestrator()
        except ImportError:
            logger.warning("Intelligent rate orchestrator not available, using basic manager")
            return await get_rate_limit_manager()


# =============================================================================
# CONVENIENCE EXPORTS
# =============================================================================

__all__ = [
    # Enums
    'GCPService',
    'OperationType',
    # Classes
    'GCPRateLimitManager',
    'ServiceRateLimiter',
    'TokenBucket',
    'SlidingWindowTracker',
    'QuotaManager',
    'QuotaMetric',
    'RateLimitConfig',
    # Exceptions
    'RateLimitExceededError',
    'QuotaExceededError',
    # Functions
    'get_rate_limit_manager',
    'get_rate_limit_manager_sync',
    'get_intelligent_rate_manager',
]
