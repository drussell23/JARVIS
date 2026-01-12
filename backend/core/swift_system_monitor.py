#!/usr/bin/env python3
"""
Advanced Swift System Monitor - Intelligent System Monitoring for JARVIS
=========================================================================

Production-grade system monitoring with UAE/SAI/Learning Database integration.
Zero hardcoding, fully async, adaptive, self-healing, and predictive.

Features:
- High-performance Swift acceleration (with Python fallback)
- Integration with UAE for predictive system planning
- Integration with SAI for environment-aware monitoring
- Learning Database integration for pattern-based insights
- Predictive system health forecasting
- Adaptive monitoring intervals based on load
- Cross-session system pattern learning
- Anomaly detection and alerting
- Real-time metrics and telemetry
- Resource correlation analysis

Architecture:
    SwiftSystemMonitor (orchestrator)
    ├── PerformanceMonitor (Swift/Python hybrid)
    ├── PredictiveHealthAnalyzer (ML-based forecasting)
    ├── AdaptiveIntervalManager (dynamic sampling)
    ├── SystemPatternLearner (learns from history)
    ├── AnomalyDetector (outlier detection)
    └── ResourceCorrelator (cross-metric analysis)

Author: Derek J. Russell
Date: October 2025
Version: 2.0.0 - UAE/SAI/Learning Database Integration
"""

import asyncio
import logging
import psutil
import time
import os
import sys
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

# Try to import Swift performance bridge
SWIFT_AVAILABLE = False
try:
    from backend.swift_bridge.performance_bridge import (
        get_system_monitor as get_swift_monitor,
        SWIFT_PERFORMANCE_AVAILABLE,
        SystemMetrics as SwiftSystemMetrics
    )
    SWIFT_AVAILABLE = SWIFT_PERFORMANCE_AVAILABLE
except Exception as e:
    logging.debug(f"Swift performance bridge not available: {e}")

logger = logging.getLogger(__name__)


# ============================================================================
# Data Models
# ============================================================================

class SystemHealth(Enum):
    """System health states"""
    EXCELLENT = "excellent"    # < 30% CPU, < 60% mem
    GOOD = "good"             # < 50% CPU, < 75% mem
    FAIR = "fair"             # < 70% CPU, < 85% mem
    DEGRADED = "degraded"     # < 85% CPU, < 90% mem
    CRITICAL = "critical"     # > 85% CPU or > 90% mem
    EMERGENCY = "emergency"   # > 95% CPU or > 95% mem


class ResourceType(Enum):
    """Types of system resources"""
    CPU = "cpu"
    MEMORY = "memory"
    DISK = "disk"
    NETWORK = "network"
    GPU = "gpu"


class AnomalyType(Enum):
    """Types of detected anomalies"""
    CPU_SPIKE = "cpu_spike"
    MEMORY_LEAK = "memory_leak"
    DISK_SATURATION = "disk_saturation"
    NETWORK_ANOMALY = "network_anomaly"
    PROCESS_THRASHING = "process_thrashing"
    THERMAL_ISSUE = "thermal_issue"


@dataclass
class SystemMetrics:
    """Comprehensive system metrics"""
    timestamp: float
    cpu_usage_percent: float
    memory_used_mb: int
    memory_available_mb: int
    memory_total_mb: int
    memory_pressure: str
    disk_usage_percent: float = 0.0
    disk_read_mb: float = 0.0
    disk_write_mb: float = 0.0
    network_sent_mb: float = 0.0
    network_recv_mb: float = 0.0
    process_count: int = 0
    thread_count: int = 0
    swap_used_mb: int = 0
    cpu_temperature: float = 0.0
    health: SystemHealth = SystemHealth.GOOD
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            **asdict(self),
            'health': self.health.value
        }


@dataclass
class HealthPrediction:
    """Predicted system health"""
    predicted_health: SystemHealth
    confidence: float
    horizon_minutes: int
    contributing_factors: List[str]
    recommended_actions: List[str]
    timestamp: float


@dataclass
class SystemAnomaly:
    """Detected system anomaly"""
    anomaly_type: AnomalyType
    severity: float  # 0.0-1.0
    resource_type: ResourceType
    detected_at: float
    current_value: float
    expected_value: float
    deviation: float
    description: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemPattern:
    """Learned system usage pattern"""
    pattern_id: str
    pattern_type: str  # 'temporal', 'contextual', 'workload'
    time_of_day: Optional[int] = None  # Hour 0-23
    day_of_week: Optional[int] = None  # 0-6
    average_cpu: float = 0.0
    average_memory_mb: float = 0.0
    peak_cpu: float = 0.0
    peak_memory_mb: float = 0.0
    occurrence_count: int = 0
    confidence: float = 0.0
    last_seen: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Predictive Health Analyzer
# ============================================================================

class PredictiveHealthAnalyzer:
    """ML-driven system health forecasting"""

    def __init__(self, history_size: int = 500):
        self.history_size = history_size
        self.metrics_history: deque = deque(maxlen=history_size)

    def add_metrics(self, metrics: SystemMetrics):
        """Add metrics to history"""
        self.metrics_history.append(metrics)

    def predict_health(self, horizon_minutes: int = 10) -> HealthPrediction:
        """Predict system health in the future"""
        if len(self.metrics_history) < 20 or not NUMPY_AVAILABLE:
            return HealthPrediction(
                predicted_health=SystemHealth.GOOD,
                confidence=0.0,
                horizon_minutes=horizon_minutes,
                contributing_factors=[],
                recommended_actions=[],
                timestamp=time.time()
            )

        # Extract recent metrics
        recent = list(self.metrics_history)[-100:]
        cpu_usage = np.array([m.cpu_usage_percent for m in recent])
        mem_usage = np.array([m.memory_used_mb / m.memory_total_mb * 100 for m in recent])

        # Predict CPU trend
        x = np.arange(len(cpu_usage))
        cpu_coeffs = np.polyfit(x, cpu_usage, 1)
        cpu_slope = cpu_coeffs[0]

        # Predict memory trend
        mem_coeffs = np.polyfit(x, mem_usage, 1)
        mem_slope = mem_coeffs[0]

        # Project forward
        samples_ahead = horizon_minutes * 6  # Assuming 10s interval
        predicted_cpu = cpu_usage[-1] + (cpu_slope * samples_ahead)
        predicted_mem = mem_usage[-1] + (mem_slope * samples_ahead)

        # Determine health
        predicted_health = self._calculate_health(predicted_cpu, predicted_mem)

        # Calculate confidence based on variance
        cpu_var = np.var(cpu_usage)
        mem_var = np.var(mem_usage)
        confidence = max(0.0, min(1.0, 1.0 - (cpu_var + mem_var) / 200))

        # Identify contributing factors
        factors = []
        if cpu_slope > 1.0:
            factors.append(f"CPU trending up ({cpu_slope:.1f}%/min)")
        if mem_slope > 0.5:
            factors.append(f"Memory trending up ({mem_slope:.1f}%/min)")
        if predicted_cpu > 80:
            factors.append(f"Projected CPU: {predicted_cpu:.1f}%")
        if predicted_mem > 85:
            factors.append(f"Projected Memory: {predicted_mem:.1f}%")

        # Recommend actions
        actions = []
        if predicted_health in [SystemHealth.DEGRADED, SystemHealth.CRITICAL]:
            if predicted_cpu > 80:
                actions.append("Consider optimizing CPU-intensive tasks")
            if predicted_mem > 85:
                actions.append("Run memory optimization")
            actions.append("Monitor for resource-heavy processes")

        return HealthPrediction(
            predicted_health=predicted_health,
            confidence=confidence,
            horizon_minutes=horizon_minutes,
            contributing_factors=factors,
            recommended_actions=actions,
            timestamp=time.time()
        )

    @staticmethod
    def _calculate_health(cpu_percent: float, mem_percent: float) -> SystemHealth:
        """
        Calculate health from metrics (macOS-aware)

        macOS memory philosophy:
        - 50-70% memory is NORMAL and healthy (file cache working)
        - 70-85% is GOOD (aggressive caching, system optimizing)
        - >85% is when we start caring (if swap is also high)
        - CPU thresholds remain similar to Linux

        We weight CPU more heavily than memory for macOS.
        """
        # Emergency: CPU critical OR memory >95%
        if cpu_percent >= 95 or mem_percent >= 95:
            return SystemHealth.EMERGENCY

        # Critical: CPU >85% OR memory >92% (macOS-adjusted)
        if cpu_percent >= 85 or mem_percent >= 92:
            return SystemHealth.CRITICAL

        # Degraded: CPU >70% OR memory >88% (macOS-adjusted)
        if cpu_percent >= 70 or mem_percent >= 88:
            return SystemHealth.DEGRADED

        # Fair: CPU >50% OR memory >82% (macOS-adjusted)
        if cpu_percent >= 50 or mem_percent >= 82:
            return SystemHealth.FAIR

        # Good: CPU >30% OR memory >70% (macOS-adjusted - this is NORMAL!)
        if cpu_percent >= 30 or mem_percent >= 60:
            return SystemHealth.GOOD

        # Excellent: Both resources low (rare on macOS due to caching)
        return SystemHealth.EXCELLENT


# ============================================================================
# Anomaly Detector
# ============================================================================

class AnomalyDetector:
    """Detect system anomalies using statistical methods"""

    def __init__(self, sensitivity: float = 2.0):
        self.sensitivity = sensitivity  # Standard deviations for anomaly
        self.baseline_metrics: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))

    def add_baseline(self, metrics: SystemMetrics):
        """Add metrics to baseline"""
        self.baseline_metrics['cpu'].append(metrics.cpu_usage_percent)
        self.baseline_metrics['memory'].append(metrics.memory_used_mb)
        self.baseline_metrics['disk_read'].append(metrics.disk_read_mb)
        self.baseline_metrics['disk_write'].append(metrics.disk_write_mb)

    def detect_anomalies(self, metrics: SystemMetrics) -> List[SystemAnomaly]:
        """Detect anomalies in current metrics"""
        anomalies = []

        if not NUMPY_AVAILABLE or len(self.baseline_metrics['cpu']) < 20:
            return anomalies

        # CPU spike detection
        cpu_baseline = np.array(list(self.baseline_metrics['cpu']))
        cpu_mean = np.mean(cpu_baseline)
        cpu_std = np.std(cpu_baseline)
        cpu_threshold = cpu_mean + (self.sensitivity * cpu_std)

        if metrics.cpu_usage_percent > cpu_threshold:
            anomalies.append(SystemAnomaly(
                anomaly_type=AnomalyType.CPU_SPIKE,
                severity=min(1.0, (metrics.cpu_usage_percent - cpu_threshold) / 50),
                resource_type=ResourceType.CPU,
                detected_at=time.time(),
                current_value=metrics.cpu_usage_percent,
                expected_value=cpu_mean,
                deviation=(metrics.cpu_usage_percent - cpu_mean) / cpu_std,
                description=f"CPU spike: {metrics.cpu_usage_percent:.1f}% (expected {cpu_mean:.1f}%)",
                metadata={'threshold': cpu_threshold}
            ))

        # Memory leak detection (sustained growth)
        mem_baseline = np.array(list(self.baseline_metrics['memory']))
        if len(mem_baseline) >= 50:
            x = np.arange(len(mem_baseline))
            coeffs = np.polyfit(x, mem_baseline, 1)
            mem_slope = coeffs[0]

            # If memory growing > 10MB per sample
            if mem_slope > 10:
                anomalies.append(SystemAnomaly(
                    anomaly_type=AnomalyType.MEMORY_LEAK,
                    severity=min(1.0, mem_slope / 50),
                    resource_type=ResourceType.MEMORY,
                    detected_at=time.time(),
                    current_value=metrics.memory_used_mb,
                    expected_value=mem_baseline[0],
                    deviation=mem_slope,
                    description=f"Possible memory leak: growing {mem_slope:.1f}MB per sample",
                    metadata={'growth_rate': mem_slope}
                ))

        return anomalies


# ============================================================================
# System Pattern Learner
# ============================================================================

class SystemPatternLearner:
    """Learn system usage patterns and correlate with time/context"""

    def __init__(self):
        self.patterns: Dict[str, SystemPattern] = {}
        self.learning_db = None

    def set_learning_db(self, learning_db):
        """Set Learning Database reference"""
        self.learning_db = learning_db

    async def learn_pattern(self, metrics: SystemMetrics, context: Optional[Dict] = None):
        """Learn system usage pattern"""
        # Temporal pattern (time of day + day of week)
        dt = datetime.fromtimestamp(metrics.timestamp)
        hour = dt.hour
        day_of_week = dt.weekday()

        pattern_id = f"temporal_{day_of_week}_{hour}"

        if pattern_id in self.patterns:
            pattern = self.patterns[pattern_id]
            # Update running averages
            n = pattern.occurrence_count
            pattern.average_cpu = (pattern.average_cpu * n + metrics.cpu_usage_percent) / (n + 1)
            pattern.average_memory_mb = (pattern.average_memory_mb * n + metrics.memory_used_mb) / (n + 1)
            pattern.peak_cpu = max(pattern.peak_cpu, metrics.cpu_usage_percent)
            pattern.peak_memory_mb = max(pattern.peak_memory_mb, metrics.memory_used_mb)
            pattern.occurrence_count += 1
            pattern.last_seen = time.time()
            pattern.confidence = min(1.0, pattern.occurrence_count / 100)
        else:
            pattern = SystemPattern(
                pattern_id=pattern_id,
                pattern_type='temporal',
                time_of_day=hour,
                day_of_week=day_of_week,
                average_cpu=metrics.cpu_usage_percent,
                average_memory_mb=metrics.memory_used_mb,
                peak_cpu=metrics.cpu_usage_percent,
                peak_memory_mb=metrics.memory_used_mb,
                occurrence_count=1,
                confidence=0.01,
                last_seen=time.time(),
                metadata={'context': context or {}}
            )
            self.patterns[pattern_id] = pattern

        # Store to Learning Database
        if self.learning_db:
            try:
                await self.learning_db.store_pattern(
                    pattern_type="system_usage",
                    pattern_data={
                        'time_of_day': hour,
                        'day_of_week': day_of_week,
                        'average_cpu': pattern.average_cpu,
                        'average_memory_mb': pattern.average_memory_mb,
                        'peak_cpu': pattern.peak_cpu,
                        'peak_memory_mb': pattern.peak_memory_mb
                    },
                    confidence=pattern.confidence,
                    metadata=context or {}
                )
            except Exception as e:
                logger.warning(f"Failed to store pattern to Learning DB: {e}")

    def get_expected_metrics(self, timestamp: Optional[float] = None) -> Optional[Tuple[float, float]]:
        """Get expected CPU and memory for current time"""
        dt = datetime.fromtimestamp(timestamp or time.time())
        hour = dt.hour
        day_of_week = dt.weekday()
        pattern_id = f"temporal_{day_of_week}_{hour}"

        pattern = self.patterns.get(pattern_id)
        if pattern and pattern.confidence > 0.5:
            return (pattern.average_cpu, pattern.average_memory_mb)

        return None


# ============================================================================
# Adaptive Interval Manager
# ============================================================================

class AdaptiveIntervalManager:
    """Dynamically adjust monitoring interval based on system state"""

    def __init__(
        self,
        min_interval: float = 5.0,
        max_interval: float = 30.0,
        default_interval: float = 10.0
    ):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.current_interval = default_interval

    def calculate_interval(self, metrics: SystemMetrics, health: SystemHealth) -> float:
        """Calculate optimal monitoring interval"""
        # Critical systems: monitor frequently
        if health in [SystemHealth.CRITICAL, SystemHealth.EMERGENCY]:
            return self.min_interval

        # Degraded: monitor more frequently
        if health == SystemHealth.DEGRADED:
            return self.min_interval * 1.5

        # Fair: normal monitoring
        if health == SystemHealth.FAIR:
            return self.min_interval * 2

        # Good/Excellent: can relax monitoring
        if health == SystemHealth.GOOD:
            return self.min_interval * 2.5

        # Excellent: minimal monitoring
        return self.max_interval


# ============================================================================
# Advanced Swift System Monitor
# ============================================================================

class SwiftSystemMonitor:
    """
    Advanced system monitoring with UAE/SAI/Learning Database integration
    Zero hardcoding, fully dynamic, self-learning, and predictive
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        uae_engine=None,
        sai_engine=None,
        learning_db=None
    ):
        """
        Initialize Advanced System Monitor

        Args:
            config: Configuration dictionary (optional)
            uae_engine: Unified Awareness Engine instance (optional)
            sai_engine: Situational Awareness Engine instance (optional)
            learning_db: Learning Database instance (optional)
        """
        # Configuration with intelligent defaults
        self.config = config or {}

        # Integration points
        self.uae_engine = uae_engine
        self.sai_engine = sai_engine
        self.learning_db = learning_db

        # Core components
        self.health_analyzer = PredictiveHealthAnalyzer()
        self.anomaly_detector = AnomalyDetector(
            sensitivity=self.config.get('anomaly_sensitivity', 2.5)
        )
        self.pattern_learner = SystemPatternLearner()
        self.interval_manager = AdaptiveIntervalManager(
            min_interval=self.config.get('min_interval', 5.0),
            max_interval=self.config.get('max_interval', 30.0),
            default_interval=self.config.get('default_interval', 10.0)
        )

        if self.learning_db:
            self.pattern_learner.set_learning_db(self.learning_db)

        # Swift acceleration
        self._swift_monitor = None
        self.swift_enabled = False

        if SWIFT_AVAILABLE:
            try:
                self._swift_monitor = get_swift_monitor()
                self.swift_enabled = self._swift_monitor is not None
            except Exception as e:
                logger.debug(f"Swift monitor unavailable: {e}")

        # State management
        self.current_metrics: Optional[SystemMetrics] = None
        self.current_health = SystemHealth.GOOD
        self.metrics_history: deque = deque(maxlen=1000)
        self.anomaly_history: deque = deque(maxlen=100)
        self.health_history: deque = deque(maxlen=100)

        # Monitoring
        self.monitoring = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.current_interval = self.interval_manager.current_interval

        # Callbacks
        self.health_change_callbacks: List[Callable] = []
        self.anomaly_callbacks: List[Callable] = []
        self.metrics_callbacks: List[Callable] = []

        # Performance tracking
        self.total_samples = 0
        self.total_anomalies = 0
        self.uptime_start = time.time()

        # Disk I/O baseline (for delta calculation)
        self._last_disk_io = None
        self._last_network_io = None

        logger.info("Advanced Swift System Monitor initialized")
        logger.info(f"  Config: {self.config}")
        logger.info(f"  Swift Acceleration: {'✅' if self.swift_enabled else '❌'}")
        logger.info(f"  UAE: {'✅' if uae_engine else '❌'}")
        logger.info(f"  SAI: {'✅' if sai_engine else '❌'}")
        logger.info(f"  Learning DB: {'✅' if learning_db else '❌'}")

    async def initialize(self):
        """Async initialization - load patterns from Learning DB"""
        if self.learning_db:
            try:
                # Load historical system patterns
                patterns = await self.learning_db.get_patterns(
                    pattern_type="system_usage",
                    limit=200
                )
                logger.info(f"Loaded {len(patterns)} system patterns from Learning DB")
            except Exception as e:
                logger.warning(f"Failed to load patterns: {e}")

        # Start monitoring
        await self.start_monitoring()

        logger.info("✅ System Monitor initialized and monitoring started")

    # ========================================================================
    # Core Monitoring
    # ========================================================================

    def get_current_metrics(self) -> SystemMetrics:
        """Get current system metrics (Swift or Python)"""
        if self.swift_enabled and self._swift_monitor:
            try:
                return self._get_metrics_swift()
            except Exception as e:
                logger.debug(f"Swift metrics failed, falling back to Python: {e}")

        return self._get_metrics_python()

    def _get_metrics_swift(self) -> SystemMetrics:
        """Get metrics using Swift acceleration"""
        swift_metrics = self._swift_monitor.get_metrics()

        # Get additional Python-only metrics
        disk = psutil.disk_usage('/')
        disk_io = psutil.disk_io_counters()
        net_io = psutil.net_io_counters()

        # Calculate deltas
        disk_read_mb = 0.0
        disk_write_mb = 0.0
        net_sent_mb = 0.0
        net_recv_mb = 0.0

        if self._last_disk_io:
            disk_read_mb = (disk_io.read_bytes - self._last_disk_io.read_bytes) / (1024 ** 2)
            disk_write_mb = (disk_io.write_bytes - self._last_disk_io.write_bytes) / (1024 ** 2)

        if self._last_network_io:
            net_sent_mb = (net_io.bytes_sent - self._last_network_io.bytes_sent) / (1024 ** 2)
            net_recv_mb = (net_io.bytes_recv - self._last_network_io.bytes_recv) / (1024 ** 2)

        self._last_disk_io = disk_io
        self._last_network_io = net_io

        # Calculate health
        health = self.health_analyzer._calculate_health(
            swift_metrics.cpu_usage_percent,
            (swift_metrics.memory_used_mb / swift_metrics.memory_total_mb * 100)
        )

        metrics = SystemMetrics(
            timestamp=swift_metrics.timestamp,
            cpu_usage_percent=swift_metrics.cpu_usage_percent,
            memory_used_mb=swift_metrics.memory_used_mb,
            memory_available_mb=swift_metrics.memory_available_mb,
            memory_total_mb=swift_metrics.memory_total_mb,
            memory_pressure=swift_metrics.memory_pressure,
            disk_usage_percent=disk.percent,
            disk_read_mb=disk_read_mb,
            disk_write_mb=disk_write_mb,
            network_sent_mb=net_sent_mb,
            network_recv_mb=net_recv_mb,
            process_count=len(psutil.pids()),
            swap_used_mb=int(psutil.swap_memory().used / (1024 ** 2)),
            health=health,
            metadata={'acceleration': 'swift'}
        )

        self.current_metrics = metrics
        return metrics

    def _get_metrics_python(self) -> SystemMetrics:
        """Get metrics using Python psutil (fallback)"""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu = psutil.cpu_percent(interval=0.1)
        disk = psutil.disk_usage('/')
        disk_io = psutil.disk_io_counters()
        net_io = psutil.net_io_counters()

        # Calculate deltas
        disk_read_mb = 0.0
        disk_write_mb = 0.0
        net_sent_mb = 0.0
        net_recv_mb = 0.0

        if self._last_disk_io:
            disk_read_mb = (disk_io.read_bytes - self._last_disk_io.read_bytes) / (1024 ** 2)
            disk_write_mb = (disk_io.write_bytes - self._last_disk_io.write_bytes) / (1024 ** 2)

        if self._last_network_io:
            net_sent_mb = (net_io.bytes_sent - self._last_network_io.bytes_sent) / (1024 ** 2)
            net_recv_mb = (net_io.bytes_recv - self._last_network_io.bytes_recv) / (1024 ** 2)

        self._last_disk_io = disk_io
        self._last_network_io = net_io

        # Get memory pressure
        memory_pressure = self._get_memory_pressure()

        # Calculate health
        health = self.health_analyzer._calculate_health(cpu, mem.percent)

        metrics = SystemMetrics(
            timestamp=time.time(),
            cpu_usage_percent=cpu,
            memory_used_mb=int(mem.used / (1024 ** 2)),
            memory_available_mb=int(mem.available / (1024 ** 2)),
            memory_total_mb=int(mem.total / (1024 ** 2)),
            memory_pressure=memory_pressure,
            disk_usage_percent=disk.percent,
            disk_read_mb=disk_read_mb,
            disk_write_mb=disk_write_mb,
            network_sent_mb=net_sent_mb,
            network_recv_mb=net_recv_mb,
            process_count=len(psutil.pids()),
            thread_count=sum(p.num_threads() for p in psutil.process_iter(['num_threads']) if p.info.get('num_threads')),
            swap_used_mb=int(swap.used / (1024 ** 2)),
            health=health,
            metadata={'acceleration': 'python'}
        )

        self.current_metrics = metrics
        return metrics

    def _get_memory_pressure(self) -> str:
        """
        Get system memory pressure (macOS kernel - PRIMARY source)

        macOS kernel's memory_pressure is the MOST reliable indicator.
        It understands compression, swap efficiency, and page fault rates.

        DO NOT fallback to simple percentage - it's misleading on macOS!
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

            # Trust the kernel's assessment
            if 'critical' in output:
                return 'critical'
            elif 'warn' in output:
                return 'warn'
            elif 'normal' in output:
                return 'normal'

            # Parse free percentage if available
            if 'percentage' in output:
                import re
                match = re.search(r'percentage:\s*(\d+)%', output)
                if match:
                    free_percent = int(match.group(1))
                    # macOS reports FREE (not used)
                    if free_percent < 10:
                        return 'critical'
                    elif free_percent < 25:
                        return 'warn'
                    else:
                        return 'normal'

        except Exception as e:
            logger.debug(f"memory_pressure command failed: {e}")

        # Conservative fallback using TRUE pressure (wired + active)
        try:
            mem = psutil.virtual_memory()
            wired = getattr(mem, 'wired', 0)
            active = getattr(mem, 'active', 0)
            total = mem.total

            true_pressure = ((wired + active) / total) * 100

            if true_pressure > 90:
                return 'critical'
            elif true_pressure > 85:
                return 'warn'
            else:
                return 'normal'
        except Exception:
            pass

        return 'normal'

    # ========================================================================
    # Monitoring & Learning
    # ========================================================================

    async def start_monitoring(self):
        """Start continuous monitoring"""
        if self.monitoring:
            return

        self.monitoring = True
        self.uptime_start = time.time()
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"System monitoring started (interval: {self.current_interval}s)")

    async def stop_monitoring(self):
        """Stop monitoring"""
        self.monitoring = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("System monitoring stopped")

    async def _monitor_loop(self):
        """Main monitoring loop with adaptive interval"""
        while self.monitoring:
            try:
                # Get current metrics
                metrics = self.get_current_metrics()
                self.metrics_history.append(metrics)
                self.total_samples += 1

                # Add to analyzers
                self.health_analyzer.add_metrics(metrics)
                self.anomaly_detector.add_baseline(metrics)

                # Learn patterns
                await self.pattern_learner.learn_pattern(
                    metrics,
                    context={'uae_active': self.uae_engine is not None}
                )

                # Check for health changes
                if metrics.health != self.current_health:
                    await self._handle_health_change(self.current_health, metrics.health, metrics)
                    self.current_health = metrics.health

                # Detect anomalies
                anomalies = self.anomaly_detector.detect_anomalies(metrics)
                if anomalies:
                    await self._handle_anomalies(anomalies, metrics)

                # Notify metrics callbacks
                for callback in self.metrics_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(metrics)
                        else:
                            callback(metrics)
                    except Exception as e:
                        logger.error(f"Metrics callback error: {e}")

                # Adapt monitoring interval
                new_interval = self.interval_manager.calculate_interval(metrics, metrics.health)
                if new_interval != self.current_interval:
                    logger.debug(f"Adapted interval: {self.current_interval}s → {new_interval}s")
                    self.current_interval = new_interval

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(self.current_interval)

    async def _handle_health_change(
        self,
        old_health: SystemHealth,
        new_health: SystemHealth,
        metrics: SystemMetrics
    ):
        """Handle system health changes"""
        logger.warning(f"🏥 System health changed: {old_health.value} → {new_health.value}")

        self.health_history.append((time.time(), new_health))

        # Notify callbacks
        for callback in self.health_change_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(old_health, new_health, metrics)
                else:
                    callback(old_health, new_health, metrics)
            except Exception as e:
                logger.error(f"Health change callback error: {e}")

        # Store to Learning DB
        if self.learning_db:
            try:
                await self.learning_db.store_action(
                    action_type="system_health_change",
                    target=f"{old_health.value}_to_{new_health.value}",
                    success=True,
                    execution_time=0,
                    params={
                        'old_health': old_health.value,
                        'new_health': new_health.value,
                        'cpu': metrics.cpu_usage_percent,
                        'memory_percent': (metrics.memory_used_mb / metrics.memory_total_mb * 100)
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to store health change: {e}")

    async def _handle_anomalies(self, anomalies: List[SystemAnomaly], metrics: SystemMetrics):
        """Handle detected anomalies"""
        for anomaly in anomalies:
            logger.warning(f"🚨 Anomaly detected: {anomaly.description}")
            self.anomaly_history.append(anomaly)
            self.total_anomalies += 1

            # Notify callbacks
            for callback in self.anomaly_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(anomaly, metrics)
                    else:
                        callback(anomaly, metrics)
                except Exception as e:
                    logger.error(f"Anomaly callback error: {e}")

            # Store to Learning DB
            if self.learning_db:
                try:
                    await self.learning_db.store_action(
                        action_type="system_anomaly",
                        target=anomaly.anomaly_type.value,
                        success=True,
                        execution_time=0,
                        params={
                            'severity': anomaly.severity,
                            'resource': anomaly.resource_type.value,
                            'current_value': anomaly.current_value,
                            'expected_value': anomaly.expected_value,
                            'deviation': anomaly.deviation
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to store anomaly: {e}")

    # ========================================================================
    # Predictions & Analysis
    # ========================================================================

    def predict_health(self, horizon_minutes: int = 10) -> HealthPrediction:
        """Predict system health"""
        return self.health_analyzer.predict_health(horizon_minutes)

    def get_expected_metrics(self) -> Optional[Tuple[float, float]]:
        """Get expected CPU and memory for current time"""
        return self.pattern_learner.get_expected_metrics()

    # ========================================================================
    # Callbacks
    # ========================================================================

    def register_health_change_callback(self, callback: Callable):
        """Register callback for health changes"""
        self.health_change_callbacks.append(callback)

    def register_anomaly_callback(self, callback: Callable):
        """Register callback for anomalies"""
        self.anomaly_callbacks.append(callback)

    def register_metrics_callback(self, callback: Callable):
        """Register callback for metrics updates"""
        self.metrics_callbacks.append(callback)

    # ========================================================================
    # Status & Metrics
    # ========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive status"""
        metrics = self.current_metrics or self.get_current_metrics()

        # Calculate statistics
        if self.metrics_history:
            avg_cpu = sum(m.cpu_usage_percent for m in self.metrics_history) / len(self.metrics_history)
            max_cpu = max(m.cpu_usage_percent for m in self.metrics_history)
            avg_mem = sum(m.memory_used_mb for m in self.metrics_history) / len(self.metrics_history)
            max_mem = max(m.memory_used_mb for m in self.metrics_history)
        else:
            avg_cpu = metrics.cpu_usage_percent
            max_cpu = metrics.cpu_usage_percent
            avg_mem = metrics.memory_used_mb
            max_mem = metrics.memory_used_mb

        # Predict future
        prediction = self.predict_health(10)

        # Expected vs actual
        expected = self.get_expected_metrics()

        return {
            'current': metrics.to_dict(),
            'statistics': {
                'average_cpu_percent': avg_cpu,
                'max_cpu_percent': max_cpu,
                'average_memory_mb': avg_mem,
                'max_memory_mb': max_mem,
                'total_samples': self.total_samples,
                'total_anomalies': self.total_anomalies,
                'uptime_hours': (time.time() - self.uptime_start) / 3600,
                'history_size': len(self.metrics_history)
            },
            'prediction': {
                'predicted_health': prediction.predicted_health.value,
                'confidence': prediction.confidence,
                'horizon_minutes': prediction.horizon_minutes,
                'factors': prediction.contributing_factors,
                'actions': prediction.recommended_actions
            },
            'expected_vs_actual': {
                'has_expectations': expected is not None,
                'expected_cpu': expected[0] if expected else None,
                'expected_memory_mb': expected[1] if expected else None,
                'actual_cpu': metrics.cpu_usage_percent,
                'actual_memory_mb': metrics.memory_used_mb
            },
            'integration': {
                'swift_enabled': self.swift_enabled,
                'uae_connected': self.uae_engine is not None,
                'sai_connected': self.sai_engine is not None,
                'learning_db_connected': self.learning_db is not None
            },
            'monitoring': {
                'active': self.monitoring,
                'current_interval_seconds': self.current_interval,
                'health': self.current_health.value,
                'anomaly_count': len(self.anomaly_history)
            }
        }

    def get_learned_patterns(self) -> List[Dict[str, Any]]:
        """Get all learned system patterns"""
        return [asdict(p) for p in self.pattern_learner.patterns.values()]

    def get_recent_anomalies(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent anomalies"""
        recent = list(self.anomaly_history)[-limit:]
        return [asdict(a) for a in recent]


# ============================================================================
# Singleton & Factory
# ============================================================================

_swift_system_monitor_instance: Optional[SwiftSystemMonitor] = None


async def get_swift_system_monitor(
    config: Optional[Dict[str, Any]] = None,
    uae_engine=None,
    sai_engine=None,
    learning_db=None,
    force_new: bool = False
) -> SwiftSystemMonitor:
    """
    Get or create Swift System Monitor instance

    Args:
        config: Configuration dictionary
        uae_engine: UAE instance
        sai_engine: SAI instance
        learning_db: Learning Database instance
        force_new: Force creation of new instance
    """
    global _swift_system_monitor_instance

    if _swift_system_monitor_instance is None or force_new:
        _swift_system_monitor_instance = SwiftSystemMonitor(
            config=config,
            uae_engine=uae_engine,
            sai_engine=sai_engine,
            learning_db=learning_db
        )
        await _swift_system_monitor_instance.initialize()

    return _swift_system_monitor_instance


# ============================================================================
# CLI Testing
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    async def test():
        print("🔍 Advanced Swift System Monitor Test")
        print("=" * 70)

        # Create instance
        monitor = await get_swift_system_monitor()

        # Show current status
        status = monitor.get_status()
        print(f"\n📊 Current Status:")
        print(f"  Health: {status['current']['health']}")
        print(f"  CPU: {status['current']['cpu_usage_percent']:.1f}%")
        print(f"  Memory: {status['current']['memory_used_mb']}MB / {status['current']['memory_total_mb']}MB")
        print(f"  Memory Pressure: {status['current']['memory_pressure']}")
        print(f"  Acceleration: {status['current']['metadata']['acceleration']}")

        # Test prediction
        prediction = monitor.predict_health(10)
        print(f"\n🔮 Health Prediction (10 min):")
        print(f"  Predicted: {prediction.predicted_health.value}")
        print(f"  Confidence: {prediction.confidence:.2f}")
        if prediction.contributing_factors:
            print(f"  Factors: {', '.join(prediction.contributing_factors)}")
        if prediction.recommended_actions:
            print(f"  Actions: {', '.join(prediction.recommended_actions)}")

        # Monitor for a bit
        print(f"\n⏱️  Monitoring for 60 seconds...")
        await asyncio.sleep(60)

        # Show statistics
        status = monitor.get_status()
        print(f"\n📈 Statistics:")
        print(f"  Avg CPU: {status['statistics']['average_cpu_percent']:.1f}%")
        print(f"  Max CPU: {status['statistics']['max_cpu_percent']:.1f}%")
        print(f"  Samples: {status['statistics']['total_samples']}")
        print(f"  Anomalies: {status['statistics']['total_anomalies']}")
        print(f"  Uptime: {status['statistics']['uptime_hours']:.2f}h")

        await monitor.stop_monitoring()

    asyncio.run(test())
