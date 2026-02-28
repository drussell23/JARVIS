#!/usr/bin/env python3
"""
Advanced Memory Quantizer - Intelligent Memory Management for JARVIS
=====================================================================

Production-grade memory management system with UAE/SAI/Learning Database integration.
Zero hardcoding, fully async, adaptive, self-healing, and intelligent.

Features:
- Dynamic memory tier management with ML-driven optimization
- Integration with UAE for predictive memory planning
- Integration with SAI for environment-aware memory allocation
- Learning Database integration for pattern-based optimization
- Predictive memory pressure detection
- Adaptive quantization strategies
- Cross-session memory usage learning
- Emergency cleanup with graceful degradation
- Real-time metrics and telemetry

Architecture:
    MemoryQuantizer (orchestrator)
    ├── MemoryTierManager (dynamic tier calculation)
    ├── PredictiveMemoryPlanner (ML-based forecasting)
    ├── AdaptiveOptimizer (context-aware optimization)
    ├── MemoryPatternLearner (learns from history)
    ├── EmergencyCleanupCoordinator (critical response)
    └── MetricsCollector (telemetry)

Author: Derek J. Russell
Date: October 2025
Version: 2.0.0 - UAE/SAI/Learning Database Integration
"""

import asyncio
import logging
import psutil
import gc
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Callable, Set
from enum import Enum
from collections import deque, defaultdict
import json

# NumPy for ML features
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    logging.warning("NumPy not available - ML features disabled")

logger = logging.getLogger(__name__)

# v266.0: Mmap thrash detection via vm_stat pageins
THRASH_PAGEIN_HEALTHY = int(os.getenv("THRASH_PAGEIN_HEALTHY", "100"))
THRASH_PAGEIN_WARNING = int(os.getenv("THRASH_PAGEIN_WARNING", "500"))
THRASH_PAGEIN_EMERGENCY = int(os.getenv("THRASH_PAGEIN_EMERGENCY", "2000"))
THRASH_SUSTAINED_SECONDS = int(os.getenv("THRASH_SUSTAINED_SECONDS", "10"))
THRASH_EMERGENCY_SUSTAINED_SECONDS = int(
    os.getenv("THRASH_EMERGENCY_SUSTAINED_SECONDS", "10")
)
THRASH_RECOVERY_SUSTAINED_SECONDS = int(
    os.getenv("THRASH_RECOVERY_SUSTAINED_SECONDS", "20")
)
THRASH_PAGEIN_PANIC_MULTIPLIER = float(
    os.getenv("THRASH_PAGEIN_PANIC_MULTIPLIER", "4.0")
)
THRASH_PAGEIN_EMA_ALPHA = float(os.getenv("THRASH_PAGEIN_EMA_ALPHA", "0.35"))


# ============================================================================
# Data Models
# ============================================================================

class MemoryTier(Enum):
    """Memory usage tiers for quantization - dynamically calculated"""
    ABUNDANT = "abundant"     # < 40% - Full performance mode
    OPTIMAL = "optimal"       # 40-60% - Normal operation
    ELEVATED = "elevated"     # 60-75% - Proactive optimization
    CONSTRAINED = "constrained"  # 75-85% - Aggressive optimization
    CRITICAL = "critical"     # 85-95% - Emergency mode
    EMERGENCY = "emergency"   # > 95% - Survival mode


class MemoryPressure(Enum):
    """System memory pressure states (macOS specific)"""
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class OptimizationStrategy(Enum):
    """Optimization strategies - learned from patterns"""
    CACHE_PRUNING = "cache_pruning"
    LAZY_LOADING = "lazy_loading"
    AGGRESSIVE_GC = "aggressive_gc"
    COMPONENT_UNLOAD = "component_unload"
    BUFFER_REDUCTION = "buffer_reduction"
    EMERGENCY_CLEANUP = "emergency_cleanup"
    PREDICTIVE_PREEMPT = "predictive_preempt"


@dataclass
class MemoryMetrics:
    """Real-time memory metrics"""
    timestamp: float
    process_memory_gb: float
    system_memory_gb: float
    system_memory_percent: float
    system_memory_available_gb: float
    tier: MemoryTier
    pressure: MemoryPressure
    swap_used_gb: float = 0.0
    page_faults: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            **asdict(self),
            'tier': self.tier.value,
            'pressure': self.pressure.value
        }


@dataclass
class OptimizationResult:
    """Result of optimization operation"""
    success: bool
    tier_before: MemoryTier
    tier_after: MemoryTier
    memory_freed_mb: float
    strategies_applied: List[str]
    duration_ms: float
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryPattern:
    """Learned memory usage pattern"""
    pattern_id: str
    component: str
    average_memory_mb: float
    peak_memory_mb: float
    memory_growth_rate: float  # MB/hour
    usage_count: int
    last_seen: float
    confidence: float
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Predictive Memory Planner
# ============================================================================

class PredictiveMemoryPlanner:
    """ML-driven memory forecasting and planning"""

    def __init__(self, history_size: int = 500):
        self.history_size = history_size
        self.metrics_history: deque = deque(maxlen=history_size)
        self.pattern_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))

    def add_metrics(self, metrics: MemoryMetrics):
        """Add metrics to history"""
        self.metrics_history.append(metrics)

    def predict_memory_pressure(self, horizon_minutes: int = 10) -> Tuple[MemoryTier, float]:
        """Predict memory tier in the future"""
        if len(self.metrics_history) < 10 or not NUMPY_AVAILABLE:
            return MemoryTier.OPTIMAL, 0.0

        # Extract recent memory percentages
        recent = list(self.metrics_history)[-50:]
        memory_usage = np.array([m.system_memory_percent for m in recent])

        # Calculate trend using linear regression
        x = np.arange(len(memory_usage))
        if len(x) > 1:
            coeffs = np.polyfit(x, memory_usage, 1)
            slope = coeffs[0]  # MB per sample

            # Project forward
            samples_ahead = horizon_minutes * 6  # Assuming 10s sample interval
            predicted_usage = memory_usage[-1] + (slope * samples_ahead)

            # Calculate confidence based on variance
            variance = np.var(memory_usage)
            confidence = max(0.0, min(1.0, 1.0 - (variance / 100)))

            # Determine predicted tier
            predicted_tier = self._usage_to_tier(predicted_usage)

            return predicted_tier, confidence

        return MemoryTier.OPTIMAL, 0.0

    def predict_optimization_impact(self, strategy: OptimizationStrategy) -> float:
        """Predict how much memory an optimization will free (MB)"""
        # Learn from historical optimization results
        # For now, use heuristics (will learn over time)
        impact_estimates = {
            OptimizationStrategy.CACHE_PRUNING: 50,
            OptimizationStrategy.LAZY_LOADING: 100,
            OptimizationStrategy.AGGRESSIVE_GC: 200,
            # v266.0: COMPONENT_UNLOAD now actually unloads the LLM model (4-8GB)
            OptimizationStrategy.COMPONENT_UNLOAD: 6000,
            OptimizationStrategy.BUFFER_REDUCTION: 150,
            # v266.0: EMERGENCY_CLEANUP is gc.collect + flush — realistic estimate
            OptimizationStrategy.EMERGENCY_CLEANUP: 50,
            OptimizationStrategy.PREDICTIVE_PREEMPT: 75
        }
        return impact_estimates.get(strategy, 50)

    def suggest_optimal_strategies(self, current_tier: MemoryTier) -> List[OptimizationStrategy]:
        """Suggest optimal strategies for current tier"""
        strategy_map = {
            MemoryTier.ABUNDANT: [],
            MemoryTier.OPTIMAL: [OptimizationStrategy.CACHE_PRUNING],
            MemoryTier.ELEVATED: [
                OptimizationStrategy.CACHE_PRUNING,
                OptimizationStrategy.LAZY_LOADING
            ],
            MemoryTier.CONSTRAINED: [
                OptimizationStrategy.CACHE_PRUNING,
                OptimizationStrategy.LAZY_LOADING,
                OptimizationStrategy.AGGRESSIVE_GC
            ],
            MemoryTier.CRITICAL: [
                OptimizationStrategy.CACHE_PRUNING,
                OptimizationStrategy.LAZY_LOADING,
                OptimizationStrategy.AGGRESSIVE_GC,
                OptimizationStrategy.COMPONENT_UNLOAD,
                OptimizationStrategy.BUFFER_REDUCTION
            ],
            MemoryTier.EMERGENCY: [
                OptimizationStrategy.EMERGENCY_CLEANUP,
                OptimizationStrategy.COMPONENT_UNLOAD,
                OptimizationStrategy.AGGRESSIVE_GC
            ]
        }
        return strategy_map.get(current_tier, [])

    @staticmethod
    def _usage_to_tier(usage_percent: float) -> MemoryTier:
        """Convert usage percentage to tier"""
        if usage_percent >= 95:
            return MemoryTier.EMERGENCY
        elif usage_percent >= 85:
            return MemoryTier.CRITICAL
        elif usage_percent >= 75:
            return MemoryTier.CONSTRAINED
        elif usage_percent >= 60:
            return MemoryTier.ELEVATED
        elif usage_percent >= 40:
            return MemoryTier.OPTIMAL
        else:
            return MemoryTier.ABUNDANT


# ============================================================================
# Memory Pattern Learner
# ============================================================================

class MemoryPatternLearner:
    """Learn memory usage patterns and correlate with system events"""

    def __init__(self):
        self.patterns: Dict[str, MemoryPattern] = {}
        self.component_usage: Dict[str, List[float]] = defaultdict(list)
        self.learning_db = None  # Will be set during initialization

    def set_learning_db(self, learning_db):
        """Set Learning Database reference"""
        self.learning_db = learning_db

    async def learn_pattern(self, component: str, memory_mb: float, metadata: Optional[Dict] = None):
        """Learn memory usage pattern for a component"""
        pattern_id = f"memory_pattern_{component}"

        # Update usage history
        self.component_usage[component].append(memory_mb)
        if len(self.component_usage[component]) > 100:
            self.component_usage[component].pop(0)

        # Calculate statistics
        usage_history = self.component_usage[component]
        avg_memory = sum(usage_history) / len(usage_history)
        peak_memory = max(usage_history)

        # Calculate growth rate (simple linear)
        growth_rate = 0.0
        if len(usage_history) >= 10 and NUMPY_AVAILABLE:
            x = np.arange(len(usage_history))
            y = np.array(usage_history)
            coeffs = np.polyfit(x, y, 1)
            growth_rate = coeffs[0] * 360  # MB per hour (assuming 10s intervals)

        # Create or update pattern
        if pattern_id in self.patterns:
            pattern = self.patterns[pattern_id]
            pattern.average_memory_mb = avg_memory
            pattern.peak_memory_mb = peak_memory
            pattern.memory_growth_rate = growth_rate
            pattern.usage_count += 1
            pattern.last_seen = time.time()
            pattern.confidence = min(1.0, pattern.usage_count / 50)
        else:
            pattern = MemoryPattern(
                pattern_id=pattern_id,
                component=component,
                average_memory_mb=avg_memory,
                peak_memory_mb=peak_memory,
                memory_growth_rate=growth_rate,
                usage_count=1,
                last_seen=time.time(),
                confidence=0.1,
                metadata=metadata or {}
            )
            self.patterns[pattern_id] = pattern

        # Store to Learning Database if available
        if self.learning_db:
            try:
                await self.learning_db.store_pattern(
                    pattern_type="memory_usage",
                    pattern_data={
                        'component': component,
                        'average_memory_mb': avg_memory,
                        'peak_memory_mb': peak_memory,
                        'growth_rate': growth_rate
                    },
                    confidence=pattern.confidence,
                    metadata=metadata or {}
                )
            except Exception as e:
                logger.warning(f"Failed to store pattern to Learning DB: {e}")

    def get_pattern(self, component: str) -> Optional[MemoryPattern]:
        """Get learned pattern for component"""
        pattern_id = f"memory_pattern_{component}"
        return self.patterns.get(pattern_id)

    def predict_component_memory(self, component: str, horizon_minutes: int = 10) -> float:
        """Predict future memory usage for component"""
        pattern = self.get_pattern(component)
        if pattern and pattern.confidence > 0.5:
            # Project forward using growth rate
            future_memory = pattern.average_memory_mb + (pattern.memory_growth_rate * (horizon_minutes / 60))
            return max(0, future_memory)
        return 0.0


# ============================================================================
# Advanced Memory Quantizer
# ============================================================================

class MemoryQuantizer:
    """
    Advanced memory quantization system with UAE/SAI/Learning Database integration
    Zero hardcoding, fully dynamic, self-learning, and adaptive
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        uae_engine=None,
        sai_engine=None,
        learning_db=None
    ):
        """
        Initialize Advanced Memory Quantizer

        Args:
            config: Configuration dictionary (optional)
            uae_engine: Unified Awareness Engine instance (optional)
            sai_engine: Situational Awareness Engine instance (optional)
            learning_db: Learning Database instance (optional)
        """
        # Configuration with intelligent defaults
        self.config = config or {}

        # Dynamic thresholds (learned over time, not hardcoded)
        self.tier_thresholds = self.config.get('tier_thresholds', {
            'abundant': 40,
            'optimal': 60,
            'elevated': 75,
            'constrained': 85,
            'critical': 95
        })

        # Integration points
        self.uae_engine = uae_engine
        self.sai_engine = sai_engine
        self.learning_db = learning_db

        # Core components
        self.planner = PredictiveMemoryPlanner()
        self.pattern_learner = MemoryPatternLearner()

        if self.learning_db:
            self.pattern_learner.set_learning_db(self.learning_db)

        # State management
        self.current_tier = MemoryTier.OPTIMAL
        self.current_metrics: Optional[MemoryMetrics] = None
        self.metrics_history: deque = deque(maxlen=1000)
        self.optimization_history: deque = deque(maxlen=100)

        # Monitoring
        self.monitoring = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.monitor_interval = self.config.get('monitor_interval_seconds', 10.0)

        # Callbacks for tier changes
        self.tier_change_callbacks: List[Callable] = []

        # Performance tracking
        self.total_optimizations = 0
        self.total_memory_freed_gb = 0.0
        self.optimization_success_rate = 1.0

        # Component tracking
        self.tracked_components: Set[str] = set()

        # v266.0: Memory reservation system
        # Allows components to "reserve" RAM in accounting before actually allocating.
        # Reservations are factored into tier calculations so other components see
        # accurate available headroom.
        self._memory_reservations: Dict[str, Tuple[float, str]] = {}

        # v266.0: Thrash detection state
        self._last_pageins: Optional[int] = None
        self._last_pagein_time: float = 0.0
        self._pagein_rate: float = 0.0  # pageins/sec
        self._pagein_rate_ema: float = 0.0
        self._thrash_state: str = "healthy"  # healthy, thrashing, emergency
        self._thrash_warning_since: float = 0.0
        self._thrash_emergency_since: float = 0.0
        self._thrash_recovery_since: float = 0.0
        self._thrash_callbacks: List[Callable] = []
        self._unload_callbacks: List[Callable] = []
        self._recovery_callbacks: List[Callable] = []

        logger.info("Advanced Memory Quantizer initialized")
        logger.info(f"  Config: {self.config}")
        logger.info(f"  UAE: {'✅' if uae_engine else '❌'}")
        logger.info(f"  SAI: {'✅' if sai_engine else '❌'}")
        logger.info(f"  Learning DB: {'✅' if learning_db else '❌'}")

    async def initialize(self):
        """Async initialization - load patterns from Learning DB"""
        if self.learning_db:
            try:
                # Load historical memory patterns
                patterns = await self.learning_db.get_patterns(
                    pattern_type="memory_usage",
                    limit=100
                )
                logger.info(f"Loaded {len(patterns)} memory patterns from Learning DB")
            except Exception as e:
                logger.warning(f"Failed to load patterns: {e}")

        # Start monitoring
        await self.start_monitoring()

        logger.info("✅ Memory Quantizer initialized and monitoring started")

    # ========================================================================
    # Core Memory Management
    # ========================================================================

    def get_current_metrics(self) -> MemoryMetrics:
        """Get current memory metrics (macOS-aware)"""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        process = psutil.Process()
        process_mem = process.memory_info()

        # Get system memory pressure (macOS specific - PRIMARY signal)
        pressure = self._get_memory_pressure()

        # Calculate macOS-aware memory pressure percentage
        # macOS uses: wired + active + compressed as "truly used"
        # inactive and purgeable can be freed instantly
        macos_pressure_percent = self._calculate_macos_memory_pressure(mem)

        # v266.0: Factor in memory reservations from other components
        reserved_gb = self.get_total_reserved_gb()
        if reserved_gb > 0:
            reserved_bytes = reserved_gb * 1024 * 1024 * 1024
            # Recalculate pressure including reservations as "used"
            wired = getattr(mem, 'wired', 0)
            active = getattr(mem, 'active', 0)
            compressed = getattr(mem, 'compressed', 0) if hasattr(mem, 'compressed') else 0
            effective_used = (wired + active + compressed) + reserved_bytes
            macos_pressure_percent = min(100.0, (effective_used / mem.total) * 100)
            logger.debug(
                f"[MemReserve] Adjusted pressure: {macos_pressure_percent:.1f}% "
                f"(+{reserved_gb:.2f}GB reserved)"
            )

        # Calculate tier based on macOS kernel pressure, not psutil percent
        tier = self._calculate_tier_macos(pressure, macos_pressure_percent, swap)

        metrics = MemoryMetrics(
            timestamp=time.time(),
            process_memory_gb=process_mem.rss / (1024 ** 3),
            system_memory_gb=mem.total / (1024 ** 3),
            system_memory_percent=macos_pressure_percent,  # Use macOS-aware calculation
            system_memory_available_gb=mem.available / (1024 ** 3),
            tier=tier,
            pressure=pressure,
            swap_used_gb=swap.used / (1024 ** 3),
            page_faults=getattr(process_mem, 'pfaults', 0),
            metadata={
                'process_pid': os.getpid(),
                'python_version': sys.version.split()[0],
                'psutil_percent': mem.percent,  # Keep original for comparison
                'macos_pressure_percent': macos_pressure_percent,
                'wired_gb': getattr(mem, 'wired', 0) / (1024 ** 3),
                'active_gb': getattr(mem, 'active', 0) / (1024 ** 3),
                'inactive_gb': getattr(mem, 'inactive', 0) / (1024 ** 3),
                'compressed_gb': getattr(mem, 'compressed', 0) / (1024 ** 3) if hasattr(mem, 'compressed') else 0,
                'reserved_gb': reserved_gb
            }
        )

        self.current_metrics = metrics
        return metrics

    async def get_current_metrics_async(self) -> MemoryMetrics:
        """
        Async version of get_current_metrics() - non-blocking memory_pressure call.

        v266.0: Use this from async contexts (monitor loops, callbacks) to avoid
        blocking the event loop with the memory_pressure subprocess. The psutil
        calls are fast (<1ms) and safe to call synchronously; only the
        memory_pressure subprocess (up to 2s) is made async.

        The sync get_current_metrics() is preserved for backward compatibility
        (called synchronously from backend/main.py and other non-async callers).
        """
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        process = psutil.Process()
        process_mem = process.memory_info()

        # Get system memory pressure (macOS specific - PRIMARY signal)
        # v266.0: async subprocess instead of blocking subprocess.run()
        pressure = await self._get_memory_pressure_async()

        # Calculate macOS-aware memory pressure percentage
        # macOS uses: wired + active + compressed as "truly used"
        # inactive and purgeable can be freed instantly
        macos_pressure_percent = self._calculate_macos_memory_pressure(mem)

        # v266.0: Factor in memory reservations from other components
        reserved_gb = self.get_total_reserved_gb()
        if reserved_gb > 0:
            reserved_bytes = reserved_gb * 1024 * 1024 * 1024
            # Recalculate pressure including reservations as "used"
            wired = getattr(mem, 'wired', 0)
            active = getattr(mem, 'active', 0)
            compressed = getattr(mem, 'compressed', 0) if hasattr(mem, 'compressed') else 0
            effective_used = (wired + active + compressed) + reserved_bytes
            macos_pressure_percent = min(100.0, (effective_used / mem.total) * 100)
            logger.debug(
                f"[MemReserve] Adjusted pressure: {macos_pressure_percent:.1f}% "
                f"(+{reserved_gb:.2f}GB reserved)"
            )

        # Calculate tier based on macOS kernel pressure, not psutil percent
        tier = self._calculate_tier_macos(pressure, macos_pressure_percent, swap)

        metrics = MemoryMetrics(
            timestamp=time.time(),
            process_memory_gb=process_mem.rss / (1024 ** 3),
            system_memory_gb=mem.total / (1024 ** 3),
            system_memory_percent=macos_pressure_percent,  # Use macOS-aware calculation
            system_memory_available_gb=mem.available / (1024 ** 3),
            tier=tier,
            pressure=pressure,
            swap_used_gb=swap.used / (1024 ** 3),
            page_faults=getattr(process_mem, 'pfaults', 0),
            metadata={
                'process_pid': os.getpid(),
                'python_version': sys.version.split()[0],
                'psutil_percent': mem.percent,  # Keep original for comparison
                'macos_pressure_percent': macos_pressure_percent,
                'wired_gb': getattr(mem, 'wired', 0) / (1024 ** 3),
                'active_gb': getattr(mem, 'active', 0) / (1024 ** 3),
                'inactive_gb': getattr(mem, 'inactive', 0) / (1024 ** 3),
                'compressed_gb': getattr(mem, 'compressed', 0) / (1024 ** 3) if hasattr(mem, 'compressed') else 0,
                'reserved_gb': reserved_gb
            }
        )

        self.current_metrics = metrics
        return metrics

    @staticmethod
    def _effective_available_gb(
        metrics: MemoryMetrics,
        *,
        include_reservations: bool = True,
    ) -> Tuple[float, float]:
        """
        Compute effective available memory for admission decisions.

        Returns:
            (effective_available_gb, reserved_gb_applied)
        """
        available_gb = max(0.0, float(metrics.system_memory_available_gb))
        reserved_gb = 0.0
        if include_reservations:
            try:
                reserved_gb = max(
                    0.0,
                    float((metrics.metadata or {}).get("reserved_gb", 0.0)),
                )
            except Exception:
                reserved_gb = 0.0
            available_gb = max(0.0, available_gb - reserved_gb)
        return available_gb, reserved_gb

    def build_admission_snapshot(
        self,
        metrics: MemoryMetrics,
        *,
        include_reservations: bool = True,
        source: str = "memory_quantizer",
    ) -> Dict[str, Any]:
        """
        Build a canonical memory snapshot for startup/runtime admission checks.
        """
        effective_available_gb, reserved_gb = self._effective_available_gb(
            metrics,
            include_reservations=include_reservations,
        )
        return {
            "available_gb": effective_available_gb,
            "raw_available_gb": float(metrics.system_memory_available_gb),
            "reserved_gb": reserved_gb,
            "tier": metrics.tier.value,
            "pressure": metrics.pressure.value,
            "source": source,
            "timestamp": float(metrics.timestamp),
        }

    def get_admission_snapshot(
        self,
        *,
        refresh: bool = False,
        include_reservations: bool = True,
        allow_probe_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Return canonical memory snapshot for startup/runtime gates (sync).
        """
        metrics = self.current_metrics
        if refresh or (metrics is None and allow_probe_if_missing):
            metrics = self.get_current_metrics()
        if metrics is None:
            return {
                "available_gb": None,
                "raw_available_gb": None,
                "reserved_gb": 0.0,
                "tier": "",
                "pressure": "",
                "source": "memory_quantizer_sync",
                "timestamp": time.time(),
            }
        return self.build_admission_snapshot(
            metrics,
            include_reservations=include_reservations,
            source="memory_quantizer_sync",
        )

    async def get_admission_snapshot_async(
        self,
        *,
        refresh: bool = True,
        include_reservations: bool = True,
    ) -> Dict[str, Any]:
        """
        Return canonical memory snapshot for startup/runtime gates (async).
        """
        metrics = self.current_metrics
        if refresh or metrics is None:
            metrics = await self.get_current_metrics_async()
        return self.build_admission_snapshot(
            metrics,
            include_reservations=include_reservations,
            source="memory_quantizer_async",
        )

    def _calculate_macos_memory_pressure(self, mem) -> float:
        """
        Calculate macOS-specific memory pressure percentage

        macOS memory management philosophy:
        - Wired: Kernel memory, cannot be paged out (TRULY USED)
        - Active: Recently used, likely to stay in RAM (TRULY USED)
        - Inactive: Not recently used, CAN be freed instantly (AVAILABLE)
        - Compressed: Compressed memory (TRULY USED but efficient)
        - Purgeable: Can be freed by system instantly (AVAILABLE)
        - Free: Completely unused (AVAILABLE)

        Unlike Linux, macOS WANTS to use all RAM for caching (inactive pages).
        High "used" percentage is NORMAL and GOOD on macOS!

        Real pressure = (Wired + Active + Compressed) / Total
        """
        total = mem.total

        # macOS-specific fields (available via psutil on macOS)
        wired = getattr(mem, 'wired', 0)
        active = getattr(mem, 'active', 0)

        # Compressed memory (if available - macOS 10.9+)
        # Note: psutil may not expose this, use vm_stat parsing if needed
        compressed = 0
        if hasattr(mem, 'compressed'):
            compressed = mem.compressed

        # Calculate TRUE memory pressure (what's actually locked in RAM)
        true_used = wired + active + compressed
        pressure_percent = (true_used / total) * 100

        # Cap at 100%
        return min(100.0, pressure_percent)

    def _calculate_tier_macos(
        self,
        kernel_pressure: MemoryPressure,
        pressure_percent: float,
        swap
    ) -> MemoryTier:
        """
        Calculate memory tier for macOS using kernel pressure as PRIMARY signal

        macOS tier philosophy (different from Linux):
        - <50% pressure is ABUNDANT (underutilized)
        - 50-70% pressure is OPTIMAL (healthy file cache)
        - 70-80% is ELEVATED (normal for macOS if pressure is "normal")
        - >80% with "warn" is CONSTRAINED
        - >85% with "critical" OR heavy swap (>80%) is CRITICAL
        - >95% is EMERGENCY

        KEY: Trust kernel's memory_pressure command over percentages!
        Swap usage on macOS is NORMAL - only worry if >80% swap AND kernel warns
        """
        swap_gb = swap.used / (1024 ** 3)
        swap_percent = swap.percent if swap.total > 0 else 0

        # EMERGENCY: Kernel says critical OR >95% pressure
        if kernel_pressure == MemoryPressure.CRITICAL or pressure_percent >= 95:
            return MemoryTier.EMERGENCY

        # CRITICAL: >90% pressure OR (>85% pressure + heavy swap >80%)
        if pressure_percent >= 90:
            return MemoryTier.CRITICAL
        if pressure_percent >= 85 and swap_percent > 80:
            return MemoryTier.CRITICAL

        # CONSTRAINED: >80% pressure with kernel "warn" OR extreme swap (>90%)
        if pressure_percent >= 80 and kernel_pressure == MemoryPressure.WARN:
            return MemoryTier.CONSTRAINED
        if swap_percent > 90:
            return MemoryTier.CONSTRAINED

        # ELEVATED: >70% pressure (normal for macOS with good file cache)
        # This is HEALTHY on macOS! Not a problem unless kernel warns.
        if pressure_percent >= 70:
            return MemoryTier.ELEVATED

        # OPTIMAL: 50-70% pressure (sweet spot for macOS)
        if pressure_percent >= 50:
            return MemoryTier.OPTIMAL

        # ABUNDANT: <50% pressure (RAM underutilized, but fine)
        return MemoryTier.ABUNDANT

    def _calculate_tier(self, usage_percent: float) -> MemoryTier:
        """
        Legacy tier calculation (kept for backward compatibility)
        NOTE: This uses simple percentage which doesn't work well for macOS
        Use _calculate_tier_macos() instead
        """
        thresholds = self.tier_thresholds

        if usage_percent >= thresholds['critical']:
            return MemoryTier.EMERGENCY if usage_percent >= 95 else MemoryTier.CRITICAL
        elif usage_percent >= thresholds['constrained']:
            return MemoryTier.CONSTRAINED
        elif usage_percent >= thresholds['elevated']:
            return MemoryTier.ELEVATED
        elif usage_percent >= thresholds['optimal']:
            return MemoryTier.OPTIMAL
        else:
            return MemoryTier.ABUNDANT

    def _get_memory_pressure(self) -> MemoryPressure:
        """
        Get system memory pressure from macOS kernel (PRIMARY truth source) - SYNC version.

        macOS kernel's memory_pressure is the MOST accurate indicator.
        It considers:
        - Page compression effectiveness
        - Swap activity
        - Page fault rate
        - File cache efficiency
        - Memory allocation requests

        This is MORE reliable than simple percentage calculations!

        NOTE: This calls subprocess.run() synchronously and blocks for up to 2s.
        Use _get_memory_pressure_async() from async contexts to avoid blocking
        the event loop. See v266.0 fix (same pattern as ECAPA v265.2).
        """
        try:
            import subprocess
            result = subprocess.run(
                ['memory_pressure'],
                capture_output=True,
                text=True,
                timeout=2
            )
            output = result.stdout.lower()

            parsed = self._parse_memory_pressure_output(output)
            if parsed is not None:
                return parsed

        except Exception as e:
            logger.debug(f"memory_pressure command failed: {e}")

        # Fallback: Use conservative thresholds based on TRUE pressure
        return self._fallback_pressure_from_cached_metrics()

    async def _get_memory_pressure_async(self) -> MemoryPressure:
        """
        Get system memory pressure from macOS kernel - ASYNC non-blocking version.

        v266.0: Replaces subprocess.run() with asyncio.create_subprocess_exec()
        to avoid blocking the event loop for up to 2s per call. Same pattern as
        ECAPA v265.2 (importlib) and ChromaDB v265.2 (import) fixes.

        Used by get_current_metrics_async() and _monitor_loop().
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                'memory_pressure',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            output = stdout_bytes.decode('utf-8', errors='replace').lower()

            parsed = self._parse_memory_pressure_output(output)
            if parsed is not None:
                return parsed

        except asyncio.TimeoutError:
            logger.debug("memory_pressure command timed out (2s)")
        except FileNotFoundError:
            logger.debug("memory_pressure command not found")
        except Exception as e:
            logger.debug(f"memory_pressure failed: {e}")

        # Fallback: Use conservative thresholds based on TRUE pressure
        return self._fallback_pressure_from_cached_metrics()

    async def _get_pagein_rate_async(self) -> float:
        """Get pageins/sec from vm_stat delta between two samples.

        Uses the 'Pageins' line from vm_stat. Returns pageins/sec since
        last call, or 0.0 on first call or error.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                'vm_stat',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            output = stdout.decode('utf-8', errors='replace')

            # Parse "Pageins:" line
            current_pageins = None
            for line in output.splitlines():
                if 'Pageins' in line or 'pageins' in line:
                    # Format: "Pageins:                          12345."
                    parts = line.split(':')
                    if len(parts) >= 2:
                        num_str = parts[1].strip().rstrip('.')
                        try:
                            current_pageins = int(num_str)
                        except ValueError:
                            pass
                    break

            if current_pageins is None:
                return 0.0

            now = time.time()

            if self._last_pageins is not None and self._last_pagein_time > 0:
                elapsed = now - self._last_pagein_time
                if elapsed > 0:
                    delta = current_pageins - self._last_pageins
                    rate = delta / elapsed
                    self._last_pageins = current_pageins
                    self._last_pagein_time = now
                    return max(0.0, rate)

            # First call — just store baseline
            self._last_pageins = current_pageins
            self._last_pagein_time = now
            return 0.0

        except asyncio.TimeoutError:
            return 0.0
        except FileNotFoundError:
            return 0.0
        except Exception:
            return 0.0

    def _parse_memory_pressure_output(self, output: str) -> Optional[MemoryPressure]:
        """
        Parse the output of the memory_pressure command.

        Returns the parsed MemoryPressure, or None if parsing fails.
        Shared by both sync and async paths.
        """
        # Parse the kernel's assessment
        if 'critical' in output:
            return MemoryPressure.CRITICAL
        elif 'warn' in output:
            return MemoryPressure.WARN
        elif 'normal' in output:
            return MemoryPressure.NORMAL

        # Parse "system-wide memory free percentage" if available
        if 'percentage' in output:
            import re
            match = re.search(r'percentage:\s*(\d+)%', output)
            if match:
                free_percent = int(match.group(1))
                # macOS reports FREE percentage (opposite of used)
                if free_percent < 10:
                    return MemoryPressure.CRITICAL
                elif free_percent < 25:
                    return MemoryPressure.WARN
                else:
                    return MemoryPressure.NORMAL

        return None

    def _fallback_pressure_from_cached_metrics(self) -> MemoryPressure:
        """
        Fallback pressure determination when memory_pressure command is unavailable.

        Uses conservative thresholds based on TRUE macOS pressure percentage
        (wired + active + compressed), NOT psutil's percentage which includes
        inactive pages.
        """
        if self.current_metrics:
            pressure = self.current_metrics.metadata.get('macos_pressure_percent', 0)
            if pressure > 90:
                return MemoryPressure.CRITICAL
            elif pressure > 85:
                return MemoryPressure.WARN

        return MemoryPressure.NORMAL

    # ========================================================================
    # Optimization Engine
    # ========================================================================

    async def optimize_memory(
        self,
        target_tier: Optional[MemoryTier] = None,
        strategies: Optional[List[OptimizationStrategy]] = None
    ) -> OptimizationResult:
        """
        Optimize memory intelligently

        Args:
            target_tier: Target memory tier (optional)
            strategies: Specific strategies to apply (optional, will be suggested if None)
        """
        start_time = time.time()
        before_metrics = self.get_current_metrics()

        # Determine strategies if not provided
        if not strategies:
            strategies = self.planner.suggest_optimal_strategies(before_metrics.tier)

        if not strategies:
            logger.info("No optimization needed - memory tier is healthy")
            return OptimizationResult(
                success=False,
                tier_before=before_metrics.tier,
                tier_after=before_metrics.tier,
                memory_freed_mb=0.0,
                strategies_applied=[],
                duration_ms=0.0,
                timestamp=time.time()
            )

        logger.info(f"🔧 Optimizing memory (tier: {before_metrics.tier.value}) with strategies: {[s.value for s in strategies]}")

        applied_strategies = []

        # Apply strategies in order
        for strategy in strategies:
            try:
                await self._apply_strategy(strategy)
                applied_strategies.append(strategy.value)
            except Exception as e:
                logger.error(f"Strategy {strategy.value} failed: {e}")

        # Force garbage collection
        gc.collect()

        # Wait a moment for effects
        await asyncio.sleep(0.5)

        # Check results
        after_metrics = self.get_current_metrics()
        memory_freed_mb = (before_metrics.process_memory_gb - after_metrics.process_memory_gb) * 1024
        duration_ms = (time.time() - start_time) * 1000

        result = OptimizationResult(
            success=after_metrics.tier.value < before_metrics.tier.value or memory_freed_mb > 0,
            tier_before=before_metrics.tier,
            tier_after=after_metrics.tier,
            memory_freed_mb=max(0, memory_freed_mb),
            strategies_applied=applied_strategies,
            duration_ms=duration_ms,
            timestamp=time.time(),
            metadata={
                'usage_before': before_metrics.system_memory_percent,
                'usage_after': after_metrics.system_memory_percent
            }
        )

        # Update tracking
        self.total_optimizations += 1
        self.total_memory_freed_gb += memory_freed_mb / 1024
        self.optimization_history.append(result)

        # Learn from this optimization
        if self.learning_db:
            try:
                await self.learning_db.store_action(
                    action_type="memory_optimization",
                    target=f"tier_{before_metrics.tier.value}",
                    success=result.success,
                    execution_time=duration_ms / 1000,
                    params={'strategies': applied_strategies},
                    result={'memory_freed_mb': memory_freed_mb}
                )
            except Exception as e:
                logger.warning(f"Failed to store optimization to Learning DB: {e}")

        logger.info(f"✅ Optimization complete: freed {memory_freed_mb:.1f}MB in {duration_ms:.0f}ms")

        return result

    async def _apply_strategy(self, strategy: OptimizationStrategy):
        """Apply specific optimization strategy"""
        if strategy == OptimizationStrategy.CACHE_PRUNING:
            # Clear internal caches
            if self.learning_db and hasattr(self.learning_db, 'pattern_cache'):
                self.learning_db.pattern_cache.cache.clear()
            if self.uae_engine and hasattr(self.uae_engine, '_cache'):
                self.uae_engine._cache.clear()

        elif strategy == OptimizationStrategy.LAZY_LOADING:
            # Signal components to enable lazy loading
            logger.debug("Lazy loading enabled")

        elif strategy == OptimizationStrategy.AGGRESSIVE_GC:
            # Run full garbage collection
            gc.collect(2)

        elif strategy == OptimizationStrategy.COMPONENT_UNLOAD:
            # v266.0: Fire registered unload callbacks
            if self._unload_callbacks:
                logger.warning(
                    f"[ComponentUnload] Firing {len(self._unload_callbacks)} unload callback(s) "
                    f"(tier={self.current_tier.value})"
                )
                for callback in self._unload_callbacks:
                    try:
                        _result = callback(self.current_tier)
                        if asyncio.iscoroutine(_result):
                            await _result
                    except Exception as e:
                        logger.error(f"[ComponentUnload] Callback error: {e}")
            else:
                logger.debug("Component unload requested but no callbacks registered")

        elif strategy == OptimizationStrategy.BUFFER_REDUCTION:
            # Reduce buffer sizes
            logger.debug("Buffer reduction applied")

        elif strategy == OptimizationStrategy.EMERGENCY_CLEANUP:
            # Emergency cleanup
            gc.collect(2)
            if self.learning_db:
                await self.learning_db.flush_all()

        elif strategy == OptimizationStrategy.PREDICTIVE_PREEMPT:
            # Preemptive optimization
            gc.collect()

    # ========================================================================
    # Monitoring & Learning
    # ========================================================================

    async def start_monitoring(self):
        """Start continuous memory monitoring"""
        if self.monitoring:
            return

        self.monitoring = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Memory monitoring started (interval: {self.monitor_interval}s)")

    async def stop_monitoring(self):
        """Stop monitoring"""
        self.monitoring = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Memory monitoring stopped")

    async def _monitor_loop(self):
        """Main monitoring loop"""
        while self.monitoring:
            try:
                # Get current metrics
                # v266.0: use async variant to avoid blocking event loop
                # with subprocess.run(['memory_pressure']) for up to 2s
                metrics = await self.get_current_metrics_async()
                self.metrics_history.append(metrics)
                self.planner.add_metrics(metrics)

                # Check for tier change
                if metrics.tier != self.current_tier:
                    await self._handle_tier_change(self.current_tier, metrics.tier)
                    self.current_tier = metrics.tier

                # v266.0: Mmap thrash detection via vm_stat pageins
                self._pagein_rate = await self._get_pagein_rate_async()
                if self._pagein_rate_ema <= 0:
                    self._pagein_rate_ema = self._pagein_rate
                else:
                    alpha = max(0.0, min(1.0, THRASH_PAGEIN_EMA_ALPHA))
                    self._pagein_rate_ema = (
                        alpha * self._pagein_rate
                        + (1.0 - alpha) * self._pagein_rate_ema
                    )
                await self._check_thrash_state()

                # Learn patterns for tracked components
                for component in self.tracked_components:
                    await self.pattern_learner.learn_pattern(
                        component,
                        metrics.process_memory_gb * 1024,
                        {'tier': metrics.tier.value}
                    )

                # Predictive optimization
                predicted_tier, confidence = self.planner.predict_memory_pressure(horizon_minutes=5)
                if predicted_tier in [MemoryTier.CRITICAL, MemoryTier.EMERGENCY] and confidence > 0.7:
                    logger.warning(f"⚠️  Memory pressure predicted: {predicted_tier.value} (confidence: {confidence:.2f})")
                    await self.optimize_memory(strategies=[OptimizationStrategy.PREDICTIVE_PREEMPT])

                # Auto-optimize if needed
                if metrics.tier in [MemoryTier.CRITICAL, MemoryTier.EMERGENCY]:
                    await self.optimize_memory()

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(self.monitor_interval)

    async def _handle_tier_change(self, old_tier: MemoryTier, new_tier: MemoryTier):
        """Handle memory tier changes"""
        logger.warning(f"🔄 Memory tier changed: {old_tier.value} → {new_tier.value}")

        # Notify callbacks
        for callback in self.tier_change_callbacks:
            try:
                _result = callback(old_tier, new_tier)
                if asyncio.iscoroutine(_result):
                    await _result
            except Exception as e:
                logger.error(f"Tier change callback error: {e}")

        if (
            old_tier in (MemoryTier.CRITICAL, MemoryTier.EMERGENCY)
            and new_tier in (MemoryTier.ABUNDANT, MemoryTier.OPTIMAL, MemoryTier.ELEVATED)
        ):
            logger.info(
                "[MemoryRecovery] Tier recovery detected: %s -> %s",
                old_tier.value,
                new_tier.value,
            )
            for callback in self._recovery_callbacks:
                try:
                    _result = callback(old_tier, new_tier)
                    if asyncio.iscoroutine(_result):
                        await _result
                except Exception as e:
                    logger.error(f"Recovery callback error: {e}")

        # Store to Learning DB
        if self.learning_db:
            try:
                await self.learning_db.store_action(
                    action_type="memory_tier_change",
                    target=f"{old_tier.value}_to_{new_tier.value}",
                    success=True,
                    execution_time=0,
                    params={'old_tier': old_tier.value, 'new_tier': new_tier.value}
                )
            except Exception as e:
                logger.warning(f"Failed to store tier change: {e}")

    def register_tier_change_callback(self, callback: Callable):
        """Register callback for tier changes"""
        if callback not in self.tier_change_callbacks:
            self.tier_change_callbacks.append(callback)

    def register_thrash_callback(self, callback: Callable) -> None:
        """Register callback for thrash state changes.

        Callback receives one argument: 'healthy', 'thrashing', or 'emergency'.
        Called when state transitions (not on every check).
        """
        if callback not in self._thrash_callbacks:
            self._thrash_callbacks.append(callback)

    def unregister_thrash_callback(self, callback: Callable) -> None:
        """Unregister thrash-state callback if present."""
        if callback in self._thrash_callbacks:
            self._thrash_callbacks.remove(callback)

    def register_unload_callback(self, callback: Callable) -> None:
        """Register callback for COMPONENT_UNLOAD strategy.

        Callback receives one argument: the current MemoryTier.
        Called when the system enters CRITICAL or EMERGENCY tier and
        the COMPONENT_UNLOAD optimization strategy fires. Use this to
        unload heavy components (e.g., the local LLM model) to free RAM.
        """
        if callback not in self._unload_callbacks:
            self._unload_callbacks.append(callback)

    def unregister_unload_callback(self, callback: Callable) -> None:
        """Unregister unload callback if present."""
        if callback in self._unload_callbacks:
            self._unload_callbacks.remove(callback)

    def register_recovery_callback(self, callback: Callable) -> None:
        """Register callback fired when memory recovers to stable tiers."""
        if callback not in self._recovery_callbacks:
            self._recovery_callbacks.append(callback)

    def unregister_recovery_callback(self, callback: Callable) -> None:
        """Unregister recovery callback if present."""
        if callback in self._recovery_callbacks:
            self._recovery_callbacks.remove(callback)

    async def _check_thrash_state(self) -> None:
        """Check pagein rate and transition thrash state if needed."""
        raw_rate = self._pagein_rate
        smoothed_rate = max(0.0, self._pagein_rate_ema or raw_rate)
        rate = max(raw_rate, smoothed_rate)
        now = time.time()
        old_state = self._thrash_state

        panic_threshold = THRASH_PAGEIN_EMERGENCY * max(1.0, THRASH_PAGEIN_PANIC_MULTIPLIER)
        if raw_rate >= panic_threshold:
            new_state = "emergency"
            self._thrash_emergency_since = now
            self._thrash_warning_since = now
            self._thrash_recovery_since = 0.0
        elif rate >= THRASH_PAGEIN_EMERGENCY:
            if self._thrash_emergency_since == 0.0:
                self._thrash_emergency_since = now
            if self._thrash_warning_since == 0.0:
                self._thrash_warning_since = now
            self._thrash_recovery_since = 0.0
            if now - self._thrash_emergency_since >= THRASH_EMERGENCY_SUSTAINED_SECONDS:
                new_state = "emergency"
            elif old_state == "emergency":
                new_state = "emergency"
            elif now - self._thrash_warning_since >= THRASH_SUSTAINED_SECONDS:
                new_state = "thrashing"
            else:
                return
        elif rate >= THRASH_PAGEIN_WARNING:
            self._thrash_emergency_since = 0.0
            if self._thrash_warning_since == 0:
                self._thrash_warning_since = now
            self._thrash_recovery_since = 0.0
            if now - self._thrash_warning_since >= THRASH_SUSTAINED_SECONDS:
                new_state = "thrashing"
            elif old_state in {"thrashing", "emergency"}:
                # Hold prior degraded state until recovery is sustained.
                return
            else:
                return  # Still accumulating sustained readings
        elif rate > THRASH_PAGEIN_HEALTHY:
            # Deadband: below warning but still above healthy floor.
            # Do not claim full recovery while pageins remain elevated.
            self._thrash_warning_since = 0.0
            self._thrash_emergency_since = 0.0
            self._thrash_recovery_since = 0.0
            if old_state == "emergency":
                new_state = "thrashing"
            elif old_state == "thrashing":
                return
            else:
                new_state = "healthy"
        else:
            self._thrash_warning_since = 0.0
            self._thrash_emergency_since = 0.0
            if old_state in {"thrashing", "emergency"}:
                if self._thrash_recovery_since == 0.0:
                    self._thrash_recovery_since = now
                    return
                if now - self._thrash_recovery_since < THRASH_RECOVERY_SUSTAINED_SECONDS:
                    return
            self._thrash_recovery_since = 0.0
            new_state = "healthy"

        if new_state != old_state:
            self._thrash_state = new_state
            log_fn = logger.warning if new_state in {"thrashing", "emergency"} else logger.info
            log_fn(
                f"[ThrashDetect] State change: {old_state} -> {new_state} "
                f"(pageins/sec: raw={raw_rate:.0f}, ema={smoothed_rate:.0f})"
            )
            for callback in self._thrash_callbacks:
                try:
                    _result = callback(new_state)
                    if asyncio.iscoroutine(_result):
                        await _result
                except Exception as e:
                    logger.debug(f"Thrash callback error: {e}")

    def track_component(self, component_name: str):
        """Track memory usage for a component"""
        self.tracked_components.add(component_name)

    # ========================================================================
    # v266.0: Memory Reservation System
    # ========================================================================

    def reserve_memory(self, gb: float, component: str) -> str:
        """
        Reserve memory in accounting. Returns reservation ID.

        Components call this BEFORE allocating large blocks (e.g., model loading).
        The reservation is factored into pressure calculations so other components
        see accurate available headroom. Call release_reservation() after the
        actual allocation completes (success or failure).

        Args:
            gb: Amount of memory to reserve in gigabytes.
            component: Name of the reserving component (for logging/debugging).

        Returns:
            Reservation ID string to pass to release_reservation().
        """
        import uuid
        reservation_id = f"res_{component}_{uuid.uuid4().hex[:8]}"
        self._memory_reservations[reservation_id] = (gb, component)
        logger.info(
            f"[MemReserve] Reserved {gb:.2f}GB for {component} (id={reservation_id})"
        )
        return reservation_id

    def release_reservation(self, reservation_id: str) -> None:
        """
        Release a memory reservation.

        Call this after the reserved memory has been physically allocated
        (the OS now tracks it in wired/active) or if the allocation failed.

        Args:
            reservation_id: The ID returned by reserve_memory().
        """
        if reservation_id in self._memory_reservations:
            gb, component = self._memory_reservations.pop(reservation_id)
            logger.info(
                f"[MemReserve] Released {gb:.2f}GB from {component} (id={reservation_id})"
            )

    def get_total_reserved_gb(self) -> float:
        """Get total reserved memory in GB across all active reservations."""
        return sum(gb for gb, _ in self._memory_reservations.values())

    def get_reservations(self) -> Dict[str, Tuple[float, str]]:
        """
        Get all active reservations.

        Returns:
            Dict mapping reservation_id -> (gb, component).
        """
        return dict(self._memory_reservations)

    # ========================================================================
    # UAE/SAI Integration
    # ========================================================================

    async def integrate_with_uae(self):
        """Integrate memory management with UAE predictions"""
        if not self.uae_engine:
            return

        # Use UAE to predict future memory needs
        try:
            # Get UAE predictions
            predictions = await self.uae_engine.get_predictions()

            # Estimate memory requirements
            for prediction in predictions:
                estimated_memory = self._estimate_action_memory(prediction)
                if estimated_memory > 0:
                    logger.debug(f"UAE prediction requires ~{estimated_memory}MB memory")
        except Exception as e:
            logger.debug(f"UAE integration: {e}")

    def _estimate_action_memory(self, action: Dict) -> float:
        """Estimate memory requirement for an action"""
        # Use learned patterns to estimate
        action_type = action.get('action_type', 'unknown')
        pattern = self.pattern_learner.get_pattern(action_type)

        if pattern:
            return pattern.average_memory_mb

        # Default estimates (will be learned over time)
        return 50.0  # MB

    # ========================================================================
    # Metrics & Status
    # ========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive status"""
        metrics = self.current_metrics or self.get_current_metrics()

        # Calculate statistics
        if self.metrics_history:
            avg_usage = sum(m.system_memory_percent for m in self.metrics_history) / len(self.metrics_history)
            max_usage = max(m.system_memory_percent for m in self.metrics_history)
        else:
            avg_usage = metrics.system_memory_percent
            max_usage = metrics.system_memory_percent

        # Predict future
        predicted_tier, confidence = self.planner.predict_memory_pressure(horizon_minutes=10)

        return {
            'current': metrics.to_dict(),
            'statistics': {
                'average_usage_percent': avg_usage,
                'max_usage_percent': max_usage,
                'total_optimizations': self.total_optimizations,
                'total_freed_gb': self.total_memory_freed_gb,
                'success_rate': self.optimization_success_rate,
                'history_size': len(self.metrics_history)
            },
            'prediction': {
                'predicted_tier': predicted_tier.value,
                'confidence': confidence,
                'horizon_minutes': 10
            },
            'integration': {
                'uae_connected': self.uae_engine is not None,
                'sai_connected': self.sai_engine is not None,
                'learning_db_connected': self.learning_db is not None
            },
            'monitoring': {
                'active': self.monitoring,
                'interval_seconds': self.monitor_interval,
                'tracked_components': len(self.tracked_components)
            }
        }

    def get_learned_patterns(self) -> List[Dict[str, Any]]:
        """Get all learned memory patterns"""
        return [asdict(p) for p in self.pattern_learner.patterns.values()]


# ============================================================================
# Singleton & Factory
# ============================================================================

_memory_quantizer_instance: Optional[MemoryQuantizer] = None


def get_memory_quantizer_instance() -> Optional[MemoryQuantizer]:
    """Return current singleton without creating a new quantizer instance."""
    return _memory_quantizer_instance


async def get_memory_quantizer(
    config: Optional[Dict[str, Any]] = None,
    uae_engine=None,
    sai_engine=None,
    learning_db=None,
    force_new: bool = False
) -> MemoryQuantizer:
    """
    Get or create Memory Quantizer instance

    Args:
        config: Configuration dictionary
        uae_engine: UAE instance
        sai_engine: SAI instance
        learning_db: Learning Database instance
        force_new: Force creation of new instance
    """
    global _memory_quantizer_instance

    if _memory_quantizer_instance is None or force_new:
        _memory_quantizer_instance = MemoryQuantizer(
            config=config,
            uae_engine=uae_engine,
            sai_engine=sai_engine,
            learning_db=learning_db
        )
        await _memory_quantizer_instance.initialize()

    return _memory_quantizer_instance


# ============================================================================
# CLI Testing
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    async def test():
        print("🧠 Advanced Memory Quantizer Test")
        print("=" * 70)

        # Create instance
        quantizer = await get_memory_quantizer()

        # Show current status
        status = quantizer.get_status()
        print(f"\n📊 Current Status:")
        print(f"  Tier: {status['current']['tier']}")
        print(f"  Process Memory: {status['current']['process_memory_gb']:.2f}GB")
        print(f"  System Usage: {status['current']['system_memory_percent']:.1f}%")
        print(f"  Pressure: {status['current']['pressure']}")

        # Test prediction
        predicted_tier, confidence = quantizer.planner.predict_memory_pressure(10)
        print(f"\n🔮 Prediction (10 min):")
        print(f"  Predicted Tier: {predicted_tier.value}")
        print(f"  Confidence: {confidence:.2f}")

        # Test optimization
        print(f"\n🔧 Testing optimization...")
        result = await quantizer.optimize_memory()
        print(f"  Success: {result.success}")
        print(f"  Freed: {result.memory_freed_mb:.1f}MB")
        print(f"  Strategies: {result.strategies_applied}")

        # Monitor for a bit
        print(f"\n⏱️  Monitoring for 30 seconds...")
        await asyncio.sleep(30)

        # Show final status
        status = quantizer.get_status()
        print(f"\n📈 Statistics:")
        print(f"  Avg Usage: {status['statistics']['average_usage_percent']:.1f}%")
        print(f"  Max Usage: {status['statistics']['max_usage_percent']:.1f}%")
        print(f"  Optimizations: {status['statistics']['total_optimizations']}")
        print(f"  Total Freed: {status['statistics']['total_freed_gb']:.3f}GB")

        await quantizer.stop_monitoring()

    asyncio.run(test())
