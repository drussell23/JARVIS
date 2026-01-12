"""
ContinuousImprovementEngine v100.0 - Self-Improving Learning Loop
=================================================================

Advanced continuous improvement system that enables JARVIS to:
1. Track performance metrics over time across all components
2. Generate and test improvement hypotheses
3. Run A/B experiments with automatic rollback
4. Measure improvement velocity and ROI
5. Learn from both successes and failures
6. Self-modify behavior parameters safely

This bridges the gap between basic RLHF and true autonomous learning.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │               ContinuousImprovementEngine                        │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  MetricsCollector                                          │ │
    │  │  - Performance tracking across domains                     │ │
    │  │  - Time-series analysis                                    │ │
    │  │  - Anomaly detection in metrics                            │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  HypothesisGenerator                                       │ │
    │  │  - Identify improvement opportunities                      │ │
    │  │  - Generate testable hypotheses                            │ │
    │  │  - Prioritize by expected impact                           │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  ExperimentRunner                                          │ │
    │  │  - A/B testing framework                                   │ │
    │  │  - Statistical significance testing                        │ │
    │  │  - Automatic rollback on regression                        │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  ParameterOptimizer                                        │ │
    │  │  - Safe parameter modification                             │ │
    │  │  - Constraint enforcement                                  │ │
    │  │  - Gradient-free optimization                              │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  ImprovementTracker                                        │ │
    │  │  - Velocity measurement                                    │ │
    │  │  - ROI calculation                                         │ │
    │  │  - Learning curve analysis                                 │ │
    │  └────────────────────────────────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import statistics
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)

import numpy as np

from backend.core.async_safety import LazyAsyncLock

# Environment-driven configuration
IMPROVEMENT_DATA_DIR = Path(os.getenv(
    "IMPROVEMENT_DATA_DIR",
    str(Path.home() / ".jarvis" / "continuous_improvement")
))
EXPERIMENT_MIN_SAMPLES = int(os.getenv("EXPERIMENT_MIN_SAMPLES", "30"))
EXPERIMENT_MAX_DURATION_HOURS = int(os.getenv("EXPERIMENT_MAX_DURATION_HOURS", "168"))
STATISTICAL_SIGNIFICANCE_THRESHOLD = float(os.getenv("SIGNIFICANCE_THRESHOLD", "0.05"))
IMPROVEMENT_VELOCITY_WINDOW_DAYS = int(os.getenv("IMPROVEMENT_VELOCITY_WINDOW_DAYS", "7"))
AUTO_ROLLBACK_REGRESSION_THRESHOLD = float(os.getenv("AUTO_ROLLBACK_THRESHOLD", "0.1"))
MAX_CONCURRENT_EXPERIMENTS = int(os.getenv("MAX_CONCURRENT_EXPERIMENTS", "3"))
HYPOTHESIS_GENERATION_INTERVAL = int(os.getenv("HYPOTHESIS_GEN_INTERVAL_SECONDS", "3600"))


class MetricType(Enum):
    """Types of metrics to track."""
    ACCURACY = "accuracy"
    LATENCY = "latency"
    CONFIDENCE = "confidence"
    SUCCESS_RATE = "success_rate"
    USER_SATISFACTION = "user_satisfaction"
    ERROR_RATE = "error_rate"
    THROUGHPUT = "throughput"
    RESOURCE_USAGE = "resource_usage"


class ExperimentStatus(Enum):
    """Status of an experiment."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HypothesisType(Enum):
    """Types of improvement hypotheses."""
    PARAMETER_TUNE = "parameter_tune"
    ALGORITHM_CHANGE = "algorithm_change"
    FEATURE_ADDITION = "feature_addition"
    THRESHOLD_ADJUSTMENT = "threshold_adjustment"
    CACHING_STRATEGY = "caching_strategy"
    MODEL_SWAP = "model_swap"


class ImprovementPriority(Enum):
    """Priority levels for improvements."""
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4
    EXPERIMENTAL = 5


@dataclass
class MetricDataPoint:
    """A single metric measurement."""
    timestamp: float = field(default_factory=time.time)
    metric_type: MetricType = MetricType.ACCURACY
    value: float = 0.0
    domain: str = "general"
    context: Dict[str, Any] = field(default_factory=dict)
    experiment_id: Optional[str] = None  # If part of an experiment


@dataclass
class MetricSeries:
    """Time series of a specific metric."""
    metric_type: MetricType
    domain: str
    data_points: deque = field(default_factory=lambda: deque(maxlen=10000))

    def add(self, value: float, context: Optional[Dict[str, Any]] = None) -> None:
        """Add a data point."""
        self.data_points.append(MetricDataPoint(
            metric_type=self.metric_type,
            value=value,
            domain=self.domain,
            context=context or {},
        ))

    def get_recent(self, n: int = 100) -> List[MetricDataPoint]:
        """Get most recent n data points."""
        return list(self.data_points)[-n:]

    def get_in_window(self, seconds: float) -> List[MetricDataPoint]:
        """Get data points in the last N seconds."""
        cutoff = time.time() - seconds
        return [dp for dp in self.data_points if dp.timestamp >= cutoff]

    def mean(self, n: Optional[int] = None) -> float:
        """Calculate mean of recent values."""
        data = self.get_recent(n) if n else list(self.data_points)
        if not data:
            return 0.0
        return statistics.mean(dp.value for dp in data)

    def std(self, n: Optional[int] = None) -> float:
        """Calculate standard deviation."""
        data = self.get_recent(n) if n else list(self.data_points)
        if len(data) < 2:
            return 0.0
        return statistics.stdev(dp.value for dp in data)

    def trend(self, window: int = 50) -> float:
        """Calculate trend (positive = improving)."""
        data = self.get_recent(window)
        if len(data) < 2:
            return 0.0

        # Simple linear regression slope
        n = len(data)
        x_mean = (n - 1) / 2
        y_mean = statistics.mean(dp.value for dp in data)

        numerator = sum((i - x_mean) * (data[i].value - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 0.0

        return numerator / denominator


@dataclass
class Hypothesis:
    """An improvement hypothesis to test."""
    hypothesis_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)

    # Description
    hypothesis_type: HypothesisType = HypothesisType.PARAMETER_TUNE
    description: str = ""
    rationale: str = ""

    # Target
    target_domain: str = "general"
    target_metric: MetricType = MetricType.ACCURACY
    target_component: str = ""

    # Expected outcome
    expected_improvement: float = 0.0  # Percentage improvement expected
    confidence_in_hypothesis: float = 0.5

    # Change specification
    change_spec: Dict[str, Any] = field(default_factory=dict)
    rollback_spec: Dict[str, Any] = field(default_factory=dict)

    # Priority and constraints
    priority: ImprovementPriority = ImprovementPriority.MEDIUM
    max_regression_allowed: float = 0.05  # 5% regression tolerance
    min_samples_required: int = EXPERIMENT_MIN_SAMPLES

    # Status
    tested: bool = False
    experiment_id: Optional[str] = None
    result: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "hypothesis_id": self.hypothesis_id,
            "created_at": self.created_at,
            "type": self.hypothesis_type.value,
            "description": self.description,
            "rationale": self.rationale,
            "target_domain": self.target_domain,
            "target_metric": self.target_metric.value,
            "expected_improvement": self.expected_improvement,
            "priority": self.priority.value,
            "tested": self.tested,
            "result": self.result,
        }


@dataclass
class ExperimentVariant:
    """A variant in an A/B experiment."""
    variant_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "control"
    is_control: bool = True
    parameters: Dict[str, Any] = field(default_factory=dict)

    # Results
    sample_count: int = 0
    metric_values: List[float] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0

    def mean(self) -> float:
        """Get mean metric value."""
        if not self.metric_values:
            return 0.0
        return statistics.mean(self.metric_values)

    def std(self) -> float:
        """Get standard deviation."""
        if len(self.metric_values) < 2:
            return 0.0
        return statistics.stdev(self.metric_values)

    def success_rate(self) -> float:
        """Get success rate."""
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.success_count / total


@dataclass
class Experiment:
    """An A/B experiment."""
    experiment_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)

    # Hypothesis
    hypothesis_id: str = ""
    description: str = ""

    # Variants
    control: ExperimentVariant = field(default_factory=lambda: ExperimentVariant(name="control", is_control=True))
    treatment: ExperimentVariant = field(default_factory=lambda: ExperimentVariant(name="treatment", is_control=False))

    # Target
    target_metric: MetricType = MetricType.ACCURACY
    target_domain: str = "general"

    # Status
    status: ExperimentStatus = ExperimentStatus.PENDING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Results
    winner: Optional[str] = None  # "control" or "treatment"
    statistical_significance: float = 0.0
    effect_size: float = 0.0
    confidence_interval: Tuple[float, float] = (0.0, 0.0)

    # Configuration
    min_samples: int = EXPERIMENT_MIN_SAMPLES
    max_duration_hours: int = EXPERIMENT_MAX_DURATION_HOURS
    traffic_split: float = 0.5  # 50% to treatment

    def is_ready_for_analysis(self) -> bool:
        """Check if enough samples collected."""
        return (
            self.control.sample_count >= self.min_samples and
            self.treatment.sample_count >= self.min_samples
        )

    def duration_hours(self) -> float:
        """Get experiment duration in hours."""
        if not self.started_at:
            return 0.0
        end = self.completed_at or time.time()
        return (end - self.started_at) / 3600

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "description": self.description,
            "status": self.status.value,
            "target_metric": self.target_metric.value,
            "control_mean": self.control.mean(),
            "treatment_mean": self.treatment.mean(),
            "control_samples": self.control.sample_count,
            "treatment_samples": self.treatment.sample_count,
            "winner": self.winner,
            "statistical_significance": self.statistical_significance,
            "effect_size": self.effect_size,
        }


@dataclass
class ImprovementResult:
    """Result of an improvement effort."""
    improvement_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    hypothesis_id: str = ""
    experiment_id: Optional[str] = None

    # Outcome
    successful: bool = False
    improvement_percentage: float = 0.0
    affected_metric: MetricType = MetricType.ACCURACY
    domain: str = "general"

    # Details
    before_value: float = 0.0
    after_value: float = 0.0
    samples_analyzed: int = 0

    # Applied changes
    changes_applied: Dict[str, Any] = field(default_factory=dict)
    was_rolled_back: bool = False


class MetricsCollector:
    """Collects and manages performance metrics."""

    def __init__(self):
        self.logger = logging.getLogger("MetricsCollector")
        self._series: Dict[str, MetricSeries] = {}
        self._lock = asyncio.Lock()

    def _get_key(self, metric_type: MetricType, domain: str) -> str:
        """Get series key."""
        return f"{domain}:{metric_type.value}"

    async def record(
        self,
        metric_type: MetricType,
        value: float,
        domain: str = "general",
        context: Optional[Dict[str, Any]] = None,
        experiment_id: Optional[str] = None
    ) -> None:
        """Record a metric value."""
        async with self._lock:
            key = self._get_key(metric_type, domain)

            if key not in self._series:
                self._series[key] = MetricSeries(
                    metric_type=metric_type,
                    domain=domain,
                )

            dp = MetricDataPoint(
                metric_type=metric_type,
                value=value,
                domain=domain,
                context=context or {},
                experiment_id=experiment_id,
            )
            self._series[key].data_points.append(dp)

    async def get_series(
        self,
        metric_type: MetricType,
        domain: str = "general"
    ) -> Optional[MetricSeries]:
        """Get a metric series."""
        async with self._lock:
            key = self._get_key(metric_type, domain)
            return self._series.get(key)

    async def get_current_value(
        self,
        metric_type: MetricType,
        domain: str = "general",
        window: int = 100
    ) -> float:
        """Get current metric value (mean of recent samples)."""
        series = await self.get_series(metric_type, domain)
        if series:
            return series.mean(window)
        return 0.0

    async def detect_anomalies(
        self,
        metric_type: MetricType,
        domain: str = "general",
        threshold_std: float = 2.5
    ) -> List[MetricDataPoint]:
        """Detect anomalous values."""
        series = await self.get_series(metric_type, domain)
        if not series or len(series.data_points) < 10:
            return []

        mean = series.mean()
        std = series.std()

        if std == 0:
            return []

        anomalies = [
            dp for dp in series.get_recent(100)
            if abs(dp.value - mean) > threshold_std * std
        ]

        return anomalies

    async def get_all_domains(self) -> List[str]:
        """Get all tracked domains."""
        async with self._lock:
            domains = set()
            for key in self._series:
                domain = key.split(":")[0]
                domains.add(domain)
            return list(domains)


class HypothesisGenerator:
    """Generates improvement hypotheses from metrics."""

    def __init__(self, metrics: MetricsCollector):
        self.metrics = metrics
        self.logger = logging.getLogger("HypothesisGenerator")
        self._generated_hypotheses: deque = deque(maxlen=100)

    async def generate_hypotheses(
        self,
        domains: Optional[List[str]] = None
    ) -> List[Hypothesis]:
        """Generate improvement hypotheses."""
        hypotheses = []

        if domains is None:
            domains = await self.metrics.get_all_domains()

        for domain in domains:
            # Generate hypotheses for each metric type
            for metric_type in MetricType:
                series = await self.metrics.get_series(metric_type, domain)
                if series and len(series.data_points) >= 20:
                    domain_hypotheses = await self._generate_for_series(series)
                    hypotheses.extend(domain_hypotheses)

        # Prioritize and deduplicate
        hypotheses = self._prioritize(hypotheses)
        self._generated_hypotheses.extend(hypotheses)

        return hypotheses

    async def _generate_for_series(self, series: MetricSeries) -> List[Hypothesis]:
        """Generate hypotheses for a metric series."""
        hypotheses = []

        mean = series.mean()
        std = series.std()
        trend = series.trend()

        # Hypothesis 1: If metric is declining, try to reverse
        if trend < -0.01:  # Negative trend
            hypotheses.append(Hypothesis(
                hypothesis_type=HypothesisType.PARAMETER_TUNE,
                description=f"Reverse declining {series.metric_type.value} in {series.domain}",
                rationale=f"Metric shows negative trend of {trend:.4f}",
                target_domain=series.domain,
                target_metric=series.metric_type,
                expected_improvement=abs(trend) * 100,
                priority=ImprovementPriority.HIGH if abs(trend) > 0.05 else ImprovementPriority.MEDIUM,
            ))

        # Hypothesis 2: If high variance, try to stabilize
        cv = std / mean if mean > 0 else 0  # Coefficient of variation
        if cv > 0.3:
            hypotheses.append(Hypothesis(
                hypothesis_type=HypothesisType.THRESHOLD_ADJUSTMENT,
                description=f"Stabilize high variance in {series.metric_type.value}",
                rationale=f"High coefficient of variation: {cv:.2f}",
                target_domain=series.domain,
                target_metric=series.metric_type,
                expected_improvement=cv * 20,  # Expect to reduce variance
                priority=ImprovementPriority.MEDIUM,
            ))

        # Hypothesis 3: If below benchmark, try to improve
        benchmarks = {
            MetricType.ACCURACY: 0.85,
            MetricType.SUCCESS_RATE: 0.90,
            MetricType.CONFIDENCE: 0.80,
            MetricType.ERROR_RATE: 0.05,  # Lower is better
        }

        benchmark = benchmarks.get(series.metric_type)
        if benchmark:
            is_below = mean < benchmark if series.metric_type != MetricType.ERROR_RATE else mean > benchmark

            if is_below:
                gap = abs(mean - benchmark)
                hypotheses.append(Hypothesis(
                    hypothesis_type=HypothesisType.ALGORITHM_CHANGE,
                    description=f"Improve {series.metric_type.value} to reach benchmark",
                    rationale=f"Current: {mean:.2%}, Benchmark: {benchmark:.2%}, Gap: {gap:.2%}",
                    target_domain=series.domain,
                    target_metric=series.metric_type,
                    expected_improvement=gap * 100,
                    priority=ImprovementPriority.HIGH if gap > 0.1 else ImprovementPriority.MEDIUM,
                ))

        return hypotheses

    def _prioritize(self, hypotheses: List[Hypothesis]) -> List[Hypothesis]:
        """Prioritize hypotheses by expected impact and confidence."""
        # Score each hypothesis
        for h in hypotheses:
            h._score = (
                h.expected_improvement *
                h.confidence_in_hypothesis *
                (6 - h.priority.value)  # Higher priority = higher multiplier
            )

        # Sort by score
        hypotheses.sort(key=lambda h: h._score, reverse=True)

        return hypotheses[:10]  # Return top 10


class ExperimentRunner:
    """Runs A/B experiments."""

    def __init__(self, metrics: MetricsCollector):
        self.metrics = metrics
        self.logger = logging.getLogger("ExperimentRunner")

        self._active_experiments: Dict[str, Experiment] = {}
        self._completed_experiments: deque = deque(maxlen=100)
        self._lock = asyncio.Lock()

    async def create_experiment(
        self,
        hypothesis: Hypothesis,
        control_params: Dict[str, Any],
        treatment_params: Dict[str, Any]
    ) -> Experiment:
        """Create a new experiment."""
        experiment = Experiment(
            hypothesis_id=hypothesis.hypothesis_id,
            description=hypothesis.description,
            target_metric=hypothesis.target_metric,
            target_domain=hypothesis.target_domain,
            min_samples=hypothesis.min_samples_required,
        )

        experiment.control.parameters = control_params
        experiment.treatment.parameters = treatment_params

        async with self._lock:
            self._active_experiments[experiment.experiment_id] = experiment

        hypothesis.experiment_id = experiment.experiment_id
        hypothesis.tested = True

        return experiment

    async def start_experiment(self, experiment_id: str) -> bool:
        """Start an experiment."""
        async with self._lock:
            experiment = self._active_experiments.get(experiment_id)
            if not experiment:
                return False

            experiment.status = ExperimentStatus.RUNNING
            experiment.started_at = time.time()

        self.logger.info(f"Started experiment {experiment_id}")
        return True

    async def record_sample(
        self,
        experiment_id: str,
        is_treatment: bool,
        metric_value: float,
        success: bool = True
    ) -> None:
        """Record a sample for an experiment."""
        async with self._lock:
            experiment = self._active_experiments.get(experiment_id)
            if not experiment or experiment.status != ExperimentStatus.RUNNING:
                return

            variant = experiment.treatment if is_treatment else experiment.control
            variant.sample_count += 1
            variant.metric_values.append(metric_value)
            if success:
                variant.success_count += 1
            else:
                variant.failure_count += 1

    async def get_variant(self, experiment_id: str) -> Tuple[bool, Dict[str, Any]]:
        """Get which variant to use for a request."""
        async with self._lock:
            experiment = self._active_experiments.get(experiment_id)
            if not experiment or experiment.status != ExperimentStatus.RUNNING:
                return False, {}

            # Random assignment based on traffic split
            is_treatment = random.random() < experiment.traffic_split
            variant = experiment.treatment if is_treatment else experiment.control

            return is_treatment, variant.parameters

    async def analyze_experiment(self, experiment_id: str) -> Optional[Experiment]:
        """Analyze experiment results."""
        async with self._lock:
            experiment = self._active_experiments.get(experiment_id)
            if not experiment:
                return None

            if not experiment.is_ready_for_analysis():
                return experiment

            # Calculate effect size (Cohen's d)
            control_mean = experiment.control.mean()
            treatment_mean = experiment.treatment.mean()
            pooled_std = math.sqrt(
                (experiment.control.std() ** 2 + experiment.treatment.std() ** 2) / 2
            )

            if pooled_std > 0:
                experiment.effect_size = (treatment_mean - control_mean) / pooled_std
            else:
                experiment.effect_size = 0

            # Simple t-test approximation
            n1 = experiment.control.sample_count
            n2 = experiment.treatment.sample_count
            se = math.sqrt(experiment.control.std() ** 2 / n1 + experiment.treatment.std() ** 2 / n2)

            if se > 0:
                t_stat = abs(treatment_mean - control_mean) / se
                # Approximate p-value (simplified)
                df = n1 + n2 - 2
                p_value = 2 * (1 - self._t_cdf(t_stat, df))
                experiment.statistical_significance = 1 - p_value
            else:
                experiment.statistical_significance = 0

            # Determine winner
            if experiment.statistical_significance > (1 - STATISTICAL_SIGNIFICANCE_THRESHOLD):
                if treatment_mean > control_mean:
                    experiment.winner = "treatment"
                else:
                    experiment.winner = "control"

            return experiment

    def _t_cdf(self, t: float, df: int) -> float:
        """Approximate t-distribution CDF."""
        # Simplified approximation
        x = df / (df + t * t)
        return 1 - 0.5 * x ** (df / 2)

    async def complete_experiment(
        self,
        experiment_id: str,
        apply_winner: bool = True
    ) -> Optional[ImprovementResult]:
        """Complete an experiment and optionally apply the winner."""
        experiment = await self.analyze_experiment(experiment_id)
        if not experiment:
            return None

        async with self._lock:
            experiment.status = ExperimentStatus.COMPLETED
            experiment.completed_at = time.time()

            # Move to completed
            self._completed_experiments.append(experiment)
            del self._active_experiments[experiment_id]

        result = ImprovementResult(
            hypothesis_id=experiment.hypothesis_id,
            experiment_id=experiment_id,
            successful=experiment.winner == "treatment",
            improvement_percentage=experiment.effect_size * 100,
            affected_metric=experiment.target_metric,
            domain=experiment.target_domain,
            before_value=experiment.control.mean(),
            after_value=experiment.treatment.mean(),
            samples_analyzed=experiment.control.sample_count + experiment.treatment.sample_count,
        )

        if apply_winner and experiment.winner == "treatment":
            result.changes_applied = experiment.treatment.parameters

        return result

    async def rollback_experiment(self, experiment_id: str) -> bool:
        """Rollback an experiment."""
        async with self._lock:
            experiment = self._active_experiments.get(experiment_id)
            if not experiment:
                return False

            experiment.status = ExperimentStatus.ROLLED_BACK
            experiment.completed_at = time.time()

            self._completed_experiments.append(experiment)
            del self._active_experiments[experiment_id]

        self.logger.warning(f"Rolled back experiment {experiment_id}")
        return True


class ParameterOptimizer:
    """Optimizes system parameters safely."""

    def __init__(self):
        self.logger = logging.getLogger("ParameterOptimizer")

        # Parameter constraints (min, max, step)
        self._constraints: Dict[str, Tuple[float, float, float]] = {}

        # Current values
        self._current_values: Dict[str, float] = {}

        # History
        self._optimization_history: deque = deque(maxlen=500)

    def register_parameter(
        self,
        name: str,
        min_value: float,
        max_value: float,
        step: float,
        initial_value: float
    ) -> None:
        """Register a parameter for optimization."""
        self._constraints[name] = (min_value, max_value, step)
        self._current_values[name] = initial_value

    def get_current_value(self, name: str) -> Optional[float]:
        """Get current parameter value."""
        return self._current_values.get(name)

    async def suggest_change(
        self,
        name: str,
        metric_series: MetricSeries,
        direction: Literal["increase", "decrease", "explore"] = "explore"
    ) -> Optional[float]:
        """Suggest a parameter change."""
        if name not in self._constraints:
            return None

        min_val, max_val, step = self._constraints[name]
        current = self._current_values.get(name, (min_val + max_val) / 2)

        if direction == "increase":
            new_value = min(max_val, current + step)
        elif direction == "decrease":
            new_value = max(min_val, current - step)
        else:
            # Explore: random perturbation
            perturbation = random.uniform(-2 * step, 2 * step)
            new_value = max(min_val, min(max_val, current + perturbation))

        return new_value

    async def apply_change(self, name: str, new_value: float) -> bool:
        """Apply a parameter change."""
        if name not in self._constraints:
            return False

        min_val, max_val, _ = self._constraints[name]
        if not (min_val <= new_value <= max_val):
            return False

        old_value = self._current_values.get(name)
        self._current_values[name] = new_value

        self._optimization_history.append({
            "timestamp": time.time(),
            "parameter": name,
            "old_value": old_value,
            "new_value": new_value,
        })

        self.logger.info(f"Parameter {name} changed: {old_value} -> {new_value}")
        return True

    async def revert_last_change(self, name: str) -> bool:
        """Revert the last change to a parameter."""
        for entry in reversed(self._optimization_history):
            if entry["parameter"] == name and entry["old_value"] is not None:
                self._current_values[name] = entry["old_value"]
                self.logger.info(f"Reverted {name} to {entry['old_value']}")
                return True
        return False


class ImprovementTracker:
    """Tracks improvement velocity and ROI."""

    def __init__(self):
        self.logger = logging.getLogger("ImprovementTracker")
        self._improvements: deque = deque(maxlen=1000)

    def record_improvement(self, result: ImprovementResult) -> None:
        """Record an improvement result."""
        self._improvements.append(result)

    def get_velocity(
        self,
        window_days: int = IMPROVEMENT_VELOCITY_WINDOW_DAYS
    ) -> Dict[str, float]:
        """Get improvement velocity metrics."""
        cutoff = time.time() - (window_days * 86400)

        recent = [i for i in self._improvements if i.timestamp >= cutoff]

        if not recent:
            return {
                "improvements_per_day": 0.0,
                "success_rate": 0.0,
                "avg_improvement": 0.0,
            }

        successful = [i for i in recent if i.successful]

        return {
            "improvements_per_day": len(recent) / window_days,
            "success_rate": len(successful) / len(recent) if recent else 0,
            "avg_improvement": statistics.mean(
                i.improvement_percentage for i in successful
            ) if successful else 0,
            "total_improvements": len(successful),
            "total_attempts": len(recent),
        }

    def get_learning_curve(
        self,
        metric_type: MetricType,
        domain: str = "general"
    ) -> List[Tuple[float, float]]:
        """Get learning curve (timestamp, cumulative improvement)."""
        relevant = [
            i for i in self._improvements
            if i.affected_metric == metric_type and i.domain == domain and i.successful
        ]

        if not relevant:
            return []

        # Sort by timestamp
        relevant.sort(key=lambda x: x.timestamp)

        # Calculate cumulative improvement
        curve = []
        cumulative = 0.0
        for i in relevant:
            cumulative += i.improvement_percentage
            curve.append((i.timestamp, cumulative))

        return curve


class ContinuousImprovementEngine:
    """
    Main engine for continuous improvement.

    Provides closed-loop learning and A/B testing for JARVIS.
    """

    def __init__(self):
        self.logger = logging.getLogger("ContinuousImprovementEngine")

        # Initialize components
        self.metrics = MetricsCollector()
        self.hypothesis_generator = HypothesisGenerator(self.metrics)
        self.experiment_runner = ExperimentRunner(self.metrics)
        self.parameter_optimizer = ParameterOptimizer()
        self.improvement_tracker = ImprovementTracker()

        # State
        self._running = False
        self._hypothesis_task: Optional[asyncio.Task] = None
        self._experiment_monitor_task: Optional[asyncio.Task] = None

        # Ensure data directory exists
        IMPROVEMENT_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the improvement engine."""
        if self._running:
            return

        self._running = True
        self.logger.info("ContinuousImprovementEngine starting...")

        # Load historical data
        await self._load_data()

        # Start background tasks
        self._hypothesis_task = asyncio.create_task(self._hypothesis_generation_loop())
        self._experiment_monitor_task = asyncio.create_task(self._experiment_monitor_loop())

        self.logger.info("ContinuousImprovementEngine started")

    async def stop(self) -> None:
        """Stop the improvement engine."""
        self._running = False

        for task in [self._hypothesis_task, self._experiment_monitor_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._save_data()
        self.logger.info("ContinuousImprovementEngine stopped")

    async def record_metric(
        self,
        metric_type: MetricType,
        value: float,
        domain: str = "general",
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record a performance metric."""
        await self.metrics.record(metric_type, value, domain, context)

    async def create_and_run_experiment(
        self,
        hypothesis: Hypothesis,
        control_params: Dict[str, Any],
        treatment_params: Dict[str, Any]
    ) -> Experiment:
        """Create and start an experiment."""
        experiment = await self.experiment_runner.create_experiment(
            hypothesis, control_params, treatment_params
        )
        await self.experiment_runner.start_experiment(experiment.experiment_id)
        return experiment

    async def get_improvement_velocity(self) -> Dict[str, float]:
        """Get current improvement velocity."""
        return self.improvement_tracker.get_velocity()

    async def generate_hypotheses(self) -> List[Hypothesis]:
        """Generate improvement hypotheses."""
        return await self.hypothesis_generator.generate_hypotheses()

    async def _hypothesis_generation_loop(self) -> None:
        """Background loop for generating hypotheses."""
        while self._running:
            try:
                await asyncio.sleep(HYPOTHESIS_GENERATION_INTERVAL)

                hypotheses = await self.generate_hypotheses()
                if hypotheses:
                    self.logger.info(f"Generated {len(hypotheses)} improvement hypotheses")
                    for h in hypotheses[:3]:
                        self.logger.info(f"  - {h.description} (expected: {h.expected_improvement:.1f}%)")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Hypothesis generation error: {e}")
                await asyncio.sleep(60)

    async def _experiment_monitor_loop(self) -> None:
        """Monitor running experiments."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute

                for exp_id in list(self.experiment_runner._active_experiments.keys()):
                    experiment = self.experiment_runner._active_experiments.get(exp_id)
                    if not experiment:
                        continue

                    # Check duration limit
                    if experiment.duration_hours() > experiment.max_duration_hours:
                        self.logger.warning(f"Experiment {exp_id} exceeded max duration, completing")
                        result = await self.experiment_runner.complete_experiment(exp_id)
                        if result:
                            self.improvement_tracker.record_improvement(result)

                    # Check for significant regression (auto-rollback)
                    elif experiment.is_ready_for_analysis():
                        control_mean = experiment.control.mean()
                        treatment_mean = experiment.treatment.mean()

                        if control_mean > 0:
                            regression = (control_mean - treatment_mean) / control_mean
                            if regression > AUTO_ROLLBACK_REGRESSION_THRESHOLD:
                                self.logger.warning(
                                    f"Experiment {exp_id} showing {regression:.1%} regression, rolling back"
                                )
                                await self.experiment_runner.rollback_experiment(exp_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Experiment monitor error: {e}")
                await asyncio.sleep(60)

    async def _load_data(self) -> None:
        """Load historical data."""
        # Load improvements
        improvements_file = IMPROVEMENT_DATA_DIR / "improvements.json"
        if improvements_file.exists():
            try:
                with open(improvements_file) as f:
                    data = json.load(f)
                self.logger.info(f"Loaded {len(data)} historical improvements")
            except Exception as e:
                self.logger.warning(f"Failed to load improvements: {e}")

    async def _save_data(self) -> None:
        """Save data to disk."""
        improvements_file = IMPROVEMENT_DATA_DIR / "improvements.json"

        try:
            data = [
                {
                    "improvement_id": i.improvement_id,
                    "timestamp": i.timestamp,
                    "successful": i.successful,
                    "improvement_percentage": i.improvement_percentage,
                    "metric": i.affected_metric.value,
                    "domain": i.domain,
                }
                for i in self.improvement_tracker._improvements
            ]

            with open(improvements_file, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            self.logger.error(f"Failed to save improvements: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        return {
            "running": self._running,
            "active_experiments": len(self.experiment_runner._active_experiments),
            "completed_experiments": len(self.experiment_runner._completed_experiments),
            "improvement_velocity": self.improvement_tracker.get_velocity(),
        }


# Global instance
_improvement_engine: Optional[ContinuousImprovementEngine] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_improvement_engine() -> ContinuousImprovementEngine:
    """Get the global ContinuousImprovementEngine instance."""
    global _improvement_engine

    async with _lock:
        if _improvement_engine is None:
            _improvement_engine = ContinuousImprovementEngine()
            await _improvement_engine.start()

        return _improvement_engine
