"""
Helicone-Style Cost Tracking for Voice Authentication

Enterprise-grade cost tracking and optimization for voice authentication
operations. Provides insights into per-operation costs, caching savings,
and optimization opportunities.

Features:
- Per-operation cost tracking
- Same-voice semantic caching
- Daily/monthly cost reports
- Cache hit optimization
- Cost anomaly detection
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from dataclasses import dataclass, field
from collections import defaultdict

from backend.core.async_safety import LazyAsyncLock

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

class CostConfig:
    """Environment-driven cost tracking configuration."""

    @staticmethod
    def is_enabled() -> bool:
        """Check if cost tracking is enabled."""
        return os.getenv("HELICONE_COST_TRACKING_ENABLED", "true").lower() == "true"

    @staticmethod
    def get_cache_ttl_seconds() -> int:
        """Voice cache TTL in seconds."""
        return int(os.getenv("HELICONE_VOICE_CACHE_TTL", "1800"))

    @staticmethod
    def get_cache_similarity_threshold() -> float:
        """Similarity threshold for cache hits."""
        return float(os.getenv("HELICONE_CACHE_SIMILARITY_THRESHOLD", "0.98"))

    @staticmethod
    def get_embedding_cost() -> float:
        """Cost per embedding extraction."""
        return float(os.getenv("VOICE_COST_EMBEDDING", "0.002"))

    @staticmethod
    def get_transcription_cost_per_second() -> float:
        """Cost per second of transcription."""
        return float(os.getenv("VOICE_COST_TRANSCRIPTION_PER_SEC", "0.006"))

    @staticmethod
    def get_llm_reasoning_cost() -> float:
        """Cost per LLM reasoning call."""
        return float(os.getenv("VOICE_COST_LLM_REASONING", "0.01"))

    @staticmethod
    def get_anti_spoofing_cost() -> float:
        """Cost per anti-spoofing analysis."""
        return float(os.getenv("VOICE_COST_ANTI_SPOOFING", "0.003"))

    @staticmethod
    def get_storage_cost_per_gb_month() -> float:
        """Storage cost per GB per month."""
        return float(os.getenv("VOICE_COST_STORAGE_PER_GB", "0.02"))

    @staticmethod
    def get_api_call_cost() -> float:
        """Cost per external API call."""
        return float(os.getenv("VOICE_COST_API_CALL", "0.001"))

    @staticmethod
    def get_max_cache_size() -> int:
        """Maximum cache entries."""
        return int(os.getenv("VOICE_CACHE_MAX_SIZE", "10000"))


# =============================================================================
# OPERATION TYPES AND COSTS
# =============================================================================

class OperationType(str, Enum):
    """Types of voice authentication operations."""

    EMBEDDING_EXTRACTION = "embedding_extraction"
    SPEAKER_VERIFICATION = "speaker_verification"
    TRANSCRIPTION = "transcription"
    ANTI_SPOOFING = "anti_spoofing"
    LLM_REASONING = "llm_reasoning"
    BEHAVIORAL_ANALYSIS = "behavioral_analysis"
    CHROMADB_QUERY = "chromadb_query"
    CHROMADB_STORE = "chromadb_store"
    EXTERNAL_API = "external_api"
    CACHE_HIT = "cache_hit"

    @property
    def base_cost(self) -> float:
        """Get base cost for this operation type."""
        costs = {
            self.EMBEDDING_EXTRACTION: CostConfig.get_embedding_cost(),
            self.SPEAKER_VERIFICATION: CostConfig.get_embedding_cost(),
            self.TRANSCRIPTION: CostConfig.get_transcription_cost_per_second() * 2,  # Avg 2s
            self.ANTI_SPOOFING: CostConfig.get_anti_spoofing_cost(),
            self.LLM_REASONING: CostConfig.get_llm_reasoning_cost(),
            self.BEHAVIORAL_ANALYSIS: 0.001,
            self.CHROMADB_QUERY: 0.0005,
            self.CHROMADB_STORE: 0.0003,
            self.EXTERNAL_API: CostConfig.get_api_call_cost(),
            self.CACHE_HIT: 0.0001,  # Minimal cost for cache retrieval
        }
        return costs.get(self, 0.001)


@dataclass
class OperationCost:
    """Record of a single operation's cost."""

    operation_id: str
    operation_type: OperationType
    timestamp: datetime
    cost: float
    duration_ms: float

    # Context
    session_id: str = ""
    user_id: str = ""

    # Cache info
    was_cached: bool = False
    cache_key: str = ""
    savings: float = 0.0

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type.value,
            "timestamp": self.timestamp.isoformat(),
            "cost": self.cost,
            "duration_ms": self.duration_ms,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "was_cached": self.was_cached,
            "savings": self.savings,
        }


@dataclass
class CostReport:
    """Aggregated cost report."""

    period_start: datetime
    period_end: datetime
    total_operations: int = 0
    total_cost: float = 0.0
    total_savings: float = 0.0

    # Breakdown by type
    cost_by_type: Dict[str, float] = field(default_factory=dict)
    count_by_type: Dict[str, int] = field(default_factory=dict)

    # Cache metrics
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit_rate: float = 0.0

    # Per-user breakdown
    cost_by_user: Dict[str, float] = field(default_factory=dict)

    # Trends
    daily_costs: Dict[str, float] = field(default_factory=dict)
    hourly_distribution: Dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "total_operations": self.total_operations,
            "total_cost": self.total_cost,
            "total_savings": self.total_savings,
            "cost_by_type": self.cost_by_type,
            "count_by_type": self.count_by_type,
            "cache_metrics": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate": self.cache_hit_rate,
            },
            "cost_by_user": self.cost_by_user,
            "daily_costs": self.daily_costs,
        }


# =============================================================================
# VOICE CACHE MANAGER
# =============================================================================

class VoiceCacheManager:
    """
    Semantic cache for voice authentication results.

    Caches voice verification results based on embedding similarity,
    reducing redundant processing for repeated authentication attempts.
    """

    def __init__(
        self,
        max_size: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
        similarity_threshold: Optional[float] = None,
    ):
        """
        Initialize the cache manager.

        Args:
            max_size: Maximum cache entries
            ttl_seconds: Cache TTL
            similarity_threshold: Similarity threshold for cache hits
        """
        self.max_size = max_size or CostConfig.get_max_cache_size()
        self.ttl_seconds = ttl_seconds or CostConfig.get_cache_ttl_seconds()
        self.similarity_threshold = (
            similarity_threshold or CostConfig.get_cache_similarity_threshold()
        )

        # Cache storage: user_id -> [(embedding, result, timestamp), ...]
        self._cache: Dict[str, List[Tuple[List[float], Dict[str, Any], float]]] = {}
        self._lock = asyncio.Lock()

        # Statistics
        self._hits = 0
        self._misses = 0
        self._evictions = 0

        logger.info(
            f"VoiceCacheManager initialized "
            f"(max_size={self.max_size}, ttl={self.ttl_seconds}s)"
        )

    async def get(
        self,
        user_id: str,
        embedding: List[float],
    ) -> Optional[Dict[str, Any]]:
        """
        Get cached result for an embedding.

        Args:
            user_id: User identifier
            embedding: Voice embedding to look up

        Returns:
            Cached result if found, None otherwise
        """
        async with self._lock:
            if user_id not in self._cache:
                self._misses += 1
                return None

            current_time = time.time()
            user_cache = self._cache[user_id]

            # Find best match
            best_match = None
            best_similarity = 0.0
            valid_entries = []

            for cached_embedding, result, timestamp in user_cache:
                # Check TTL
                if current_time - timestamp > self.ttl_seconds:
                    continue

                valid_entries.append((cached_embedding, result, timestamp))

                # Calculate similarity
                similarity = self._cosine_similarity(embedding, cached_embedding)

                if similarity > best_similarity and similarity >= self.similarity_threshold:
                    best_similarity = similarity
                    best_match = result

            # Update cache with only valid entries
            self._cache[user_id] = valid_entries

            if best_match:
                self._hits += 1
                logger.debug(
                    f"Cache hit for user {user_id} "
                    f"(similarity={best_similarity:.4f})"
                )
                return best_match
            else:
                self._misses += 1
                return None

    async def set(
        self,
        user_id: str,
        embedding: List[float],
        result: Dict[str, Any],
    ) -> None:
        """
        Cache a result for an embedding.

        Args:
            user_id: User identifier
            embedding: Voice embedding
            result: Result to cache
        """
        async with self._lock:
            if user_id not in self._cache:
                self._cache[user_id] = []

            # Add entry
            self._cache[user_id].append((embedding, result, time.time()))

            # Enforce max size per user (keep most recent)
            max_per_user = self.max_size // 100  # Reasonable per-user limit
            if len(self._cache[user_id]) > max_per_user:
                self._cache[user_id] = self._cache[user_id][-max_per_user:]
                self._evictions += 1

            # Enforce total max size
            total_entries = sum(len(v) for v in self._cache.values())
            if total_entries > self.max_size:
                await self._evict_oldest()

    async def _evict_oldest(self) -> None:
        """Evict oldest entries to stay within size limit."""
        # Find oldest entries across all users
        all_entries = []
        for user_id, entries in self._cache.items():
            for i, (embedding, result, timestamp) in enumerate(entries):
                all_entries.append((user_id, i, timestamp))

        # Sort by timestamp (oldest first)
        all_entries.sort(key=lambda x: x[2])

        # Remove oldest 10%
        to_remove = len(all_entries) // 10
        for user_id, idx, _ in all_entries[:to_remove]:
            if user_id in self._cache and idx < len(self._cache[user_id]):
                self._cache[user_id].pop(idx)
                self._evictions += 1

        # Clean up empty user caches
        empty_users = [u for u, e in self._cache.items() if not e]
        for user_id in empty_users:
            del self._cache[user_id]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if len(a) != len(b):
            return 0.0

        a_arr = np.array(a)
        b_arr = np.array(b)

        dot_product = np.dot(a_arr, b_arr)
        norm_a = np.linalg.norm(a_arr)
        norm_b = np.linalg.norm(b_arr)

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return float(dot_product / (norm_a * norm_b))

    async def clear_user(self, user_id: str) -> int:
        """Clear cache for a specific user."""
        async with self._lock:
            if user_id in self._cache:
                count = len(self._cache[user_id])
                del self._cache[user_id]
                return count
            return 0

    async def clear_all(self) -> int:
        """Clear entire cache."""
        async with self._lock:
            count = sum(len(v) for v in self._cache.values())
            self._cache.clear()
            return count

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / total if total > 0 else 0.0,
            "evictions": self._evictions,
            "total_entries": sum(len(v) for v in self._cache.values()),
            "unique_users": len(self._cache),
        }


# =============================================================================
# COST TRACKER
# =============================================================================

class VoiceAuthCostTracker:
    """
    Comprehensive cost tracking for voice authentication.

    Tracks costs per operation, provides reports, and
    optimizes through caching.

    Usage:
        tracker = await get_cost_tracker()

        # Track an operation
        with tracker.track_operation(OperationType.EMBEDDING_EXTRACTION) as op:
            # Do embedding extraction
            pass

        # Get cost report
        report = await tracker.get_daily_report()
    """

    def __init__(self):
        """Initialize the cost tracker."""
        self._enabled = CostConfig.is_enabled()
        self._cache = VoiceCacheManager()

        # Cost history
        self._operations: List[OperationCost] = []
        self._max_history = 100000

        # Aggregated metrics
        self._total_cost = 0.0
        self._total_savings = 0.0
        self._daily_costs: Dict[str, float] = {}

        # Statistics
        self._stats = {
            "total_operations": 0,
            "total_cost": 0.0,
            "total_savings": 0.0,
            "avg_cost_per_auth": 0.0,
        }

        self._lock = asyncio.Lock()

        logger.info(f"VoiceAuthCostTracker initialized (enabled={self._enabled})")

    async def track_operation(
        self,
        operation_type: OperationType,
        session_id: str = "",
        user_id: str = "",
        duration_ms: float = 0.0,
        was_cached: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OperationCost:
        """
        Track a single operation.

        Args:
            operation_type: Type of operation
            session_id: Session identifier
            user_id: User identifier
            duration_ms: Operation duration
            was_cached: Whether result was from cache
            metadata: Additional metadata

        Returns:
            OperationCost record
        """
        if not self._enabled:
            return OperationCost(
                operation_id="disabled",
                operation_type=operation_type,
                timestamp=datetime.now(timezone.utc),
                cost=0.0,
                duration_ms=duration_ms,
            )

        # Calculate cost
        base_cost = operation_type.base_cost

        if was_cached:
            actual_cost = OperationType.CACHE_HIT.base_cost
            savings = base_cost - actual_cost
        else:
            actual_cost = base_cost
            savings = 0.0

        # Create operation record
        operation_id = hashlib.sha256(
            f"{operation_type.value}:{time.time()}:{id(self)}".encode()
        ).hexdigest()[:12]

        operation = OperationCost(
            operation_id=operation_id,
            operation_type=operation_type,
            timestamp=datetime.now(timezone.utc),
            cost=actual_cost,
            duration_ms=duration_ms,
            session_id=session_id,
            user_id=user_id,
            was_cached=was_cached,
            savings=savings,
            metadata=metadata or {},
        )

        async with self._lock:
            # Store operation
            self._operations.append(operation)

            # Limit history
            if len(self._operations) > self._max_history:
                self._operations = self._operations[-self._max_history:]

            # Update aggregates
            self._total_cost += actual_cost
            self._total_savings += savings

            # Update daily costs
            day_key = operation.timestamp.strftime("%Y-%m-%d")
            self._daily_costs[day_key] = self._daily_costs.get(day_key, 0.0) + actual_cost

            # Update stats
            self._stats["total_operations"] += 1
            self._stats["total_cost"] = self._total_cost
            self._stats["total_savings"] = self._total_savings

        return operation

    async def track_authentication(
        self,
        session_id: str,
        user_id: str,
        operations: List[Tuple[OperationType, float, bool]],
    ) -> float:
        """
        Track all operations in an authentication attempt.

        Args:
            session_id: Session identifier
            user_id: User identifier
            operations: List of (type, duration_ms, was_cached) tuples

        Returns:
            Total cost
        """
        total_cost = 0.0

        for op_type, duration, cached in operations:
            op = await self.track_operation(
                operation_type=op_type,
                session_id=session_id,
                user_id=user_id,
                duration_ms=duration,
                was_cached=cached,
            )
            total_cost += op.cost

        return total_cost

    async def check_and_update_cache(
        self,
        user_id: str,
        embedding: List[float],
        result: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check cache and optionally update it.

        Args:
            user_id: User identifier
            embedding: Voice embedding
            result: Result to cache (if provided)

        Returns:
            Tuple of (was_cached, cached_result)
        """
        # Check cache first
        cached = await self._cache.get(user_id, embedding)

        if cached:
            return True, cached

        # Cache miss - store result if provided
        if result:
            await self._cache.set(user_id, embedding, result)

        return False, None

    async def get_daily_report(
        self,
        date: Optional[datetime] = None,
    ) -> CostReport:
        """
        Get cost report for a specific day.

        Args:
            date: Date to report (default: today)

        Returns:
            CostReport for the day
        """
        if date is None:
            date = datetime.now(timezone.utc)

        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        return await self._generate_report(day_start, day_end)

    async def get_monthly_report(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> CostReport:
        """
        Get cost report for a specific month.

        Args:
            year: Year (default: current)
            month: Month (default: current)

        Returns:
            CostReport for the month
        """
        now = datetime.now(timezone.utc)
        year = year or now.year
        month = month or now.month

        month_start = datetime(year, month, 1, tzinfo=timezone.utc)

        if month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            month_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

        return await self._generate_report(month_start, month_end)

    async def _generate_report(
        self,
        start: datetime,
        end: datetime,
    ) -> CostReport:
        """Generate a cost report for a time period."""
        async with self._lock:
            # Filter operations
            relevant = [
                op for op in self._operations
                if start <= op.timestamp < end
            ]

        report = CostReport(
            period_start=start,
            period_end=end,
            total_operations=len(relevant),
        )

        if not relevant:
            return report

        # Calculate metrics
        report.total_cost = sum(op.cost for op in relevant)
        report.total_savings = sum(op.savings for op in relevant)

        # Breakdown by type
        for op in relevant:
            type_key = op.operation_type.value
            report.cost_by_type[type_key] = report.cost_by_type.get(type_key, 0.0) + op.cost
            report.count_by_type[type_key] = report.count_by_type.get(type_key, 0) + 1

        # Cache metrics
        report.cache_hits = sum(1 for op in relevant if op.was_cached)
        report.cache_misses = len(relevant) - report.cache_hits
        report.cache_hit_rate = (
            report.cache_hits / len(relevant) if relevant else 0.0
        )

        # Per-user costs
        for op in relevant:
            if op.user_id:
                report.cost_by_user[op.user_id] = (
                    report.cost_by_user.get(op.user_id, 0.0) + op.cost
                )

        # Daily breakdown
        for op in relevant:
            day_key = op.timestamp.strftime("%Y-%m-%d")
            report.daily_costs[day_key] = (
                report.daily_costs.get(day_key, 0.0) + op.cost
            )

        # Hourly distribution
        for op in relevant:
            hour = op.timestamp.hour
            report.hourly_distribution[hour] = (
                report.hourly_distribution.get(hour, 0.0) + op.cost
            )

        return report

    async def get_optimization_suggestions(self) -> List[Dict[str, Any]]:
        """
        Get cost optimization suggestions.

        Returns:
            List of suggestions with potential savings
        """
        suggestions = []

        # Get recent data
        report = await self.get_daily_report()
        cache_stats = self._cache.get_stats()

        # Check cache hit rate
        if cache_stats["hit_rate"] < 0.3:
            suggestions.append({
                "type": "cache_optimization",
                "title": "Low cache hit rate",
                "description": (
                    f"Cache hit rate is {cache_stats['hit_rate']:.1%}. "
                    "Consider increasing cache TTL or lowering similarity threshold."
                ),
                "potential_savings": report.total_cost * 0.3,
                "priority": "high",
            })

        # Check expensive operations
        if report.cost_by_type.get("llm_reasoning", 0) > report.total_cost * 0.4:
            suggestions.append({
                "type": "operation_optimization",
                "title": "High LLM reasoning costs",
                "description": (
                    "LLM reasoning accounts for over 40% of costs. "
                    "Consider increasing confidence thresholds for fast-path authentication."
                ),
                "potential_savings": report.cost_by_type.get("llm_reasoning", 0) * 0.3,
                "priority": "medium",
            })

        # Check per-user concentration
        if report.cost_by_user:
            top_user_cost = max(report.cost_by_user.values())
            if top_user_cost > report.total_cost * 0.5:
                suggestions.append({
                    "type": "usage_pattern",
                    "title": "High per-user concentration",
                    "description": (
                        "A single user accounts for over 50% of costs. "
                        "Consider implementing per-user rate limiting or quotas."
                    ),
                    "potential_savings": top_user_cost * 0.2,
                    "priority": "low",
                })

        return suggestions

    def get_stats(self) -> Dict[str, Any]:
        """Get tracker statistics."""
        return {
            **self._stats,
            "enabled": self._enabled,
            "cache": self._cache.get_stats(),
            "operations_stored": len(self._operations),
        }

    async def close(self) -> None:
        """Close the cost tracker."""
        await self._cache.clear_all()


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================

_tracker_instance: Optional[VoiceAuthCostTracker] = None
_tracker_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_cost_tracker() -> VoiceAuthCostTracker:
    """Get or create the cost tracker."""
    global _tracker_instance

    async with _tracker_lock:
        if _tracker_instance is None:
            _tracker_instance = VoiceAuthCostTracker()
        return _tracker_instance


def create_cost_tracker() -> VoiceAuthCostTracker:
    """Create a new cost tracker instance."""
    return VoiceAuthCostTracker()


__all__ = [
    "VoiceAuthCostTracker",
    "OperationCost",
    "OperationType",
    "CostReport",
    "CostConfig",
    "VoiceCacheManager",
    "get_cost_tracker",
    "create_cost_tracker",
]
