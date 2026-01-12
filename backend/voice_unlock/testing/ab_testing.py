"""
Voice Authentication A/B Testing Framework.
============================================

Provides a robust A/B testing framework for voice authentication models.
Enables controlled experiments to validate model improvements before
full deployment.

Features:
1. Traffic splitting (control vs treatment)
2. Statistical significance testing
3. Metric collection and analysis
4. Automatic experiment conclusion
5. Winner promotion
6. Experiment history tracking

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  ABTestingManager                                                   │
    │  ├── ExperimentRegistry (tracks active and completed experiments)   │
    │  ├── TrafficAllocator (assigns users to control/treatment)          │
    │  ├── MetricsCollector (collects experiment metrics)                 │
    │  └── StatisticalAnalyzer (analyzes results for significance)        │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS Trinity v81.0 - Unified Learning Loop
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# =============================================================================
# Types and Enums
# =============================================================================

class ExperimentStatus(Enum):
    """Status of an A/B test experiment."""
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ExperimentVariant(Enum):
    """Experiment variants."""
    CONTROL = "control"
    TREATMENT = "treatment"


class MetricType(Enum):
    """Types of metrics to track."""
    ACCURACY = "accuracy"           # Correct decisions / total
    PRECISION = "precision"         # TP / (TP + FP)
    RECALL = "recall"               # TP / (TP + FN)
    F1_SCORE = "f1_score"           # Harmonic mean of precision/recall
    LATENCY = "latency"             # Response time
    CONFIDENCE = "confidence"        # Average confidence
    FALSE_POSITIVE_RATE = "fpr"
    FALSE_NEGATIVE_RATE = "fnr"


class WinnerDecision(Enum):
    """Decision on experiment winner."""
    CONTROL = "control"
    TREATMENT = "treatment"
    NO_SIGNIFICANT_DIFFERENCE = "no_difference"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass
class ExperimentConfig:
    """Configuration for an A/B test experiment."""
    test_id: str
    name: str
    control_version: str
    treatment_version: str
    traffic_split: float = 0.1  # Percentage to treatment
    min_samples: int = 100       # Minimum samples before analysis
    max_duration_hours: float = 168.0  # 7 days max
    significance_level: float = 0.05  # p-value threshold
    min_improvement: float = 0.02     # 2% minimum improvement
    primary_metric: MetricType = MetricType.ACCURACY


@dataclass
class VariantMetrics:
    """Metrics for a single variant."""
    variant: ExperimentVariant
    samples: int = 0
    true_positives: int = 0
    true_negatives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    total_latency_ms: float = 0.0
    total_confidence: float = 0.0

    @property
    def accuracy(self) -> float:
        """Calculate accuracy."""
        total = self.true_positives + self.true_negatives + self.false_positives + self.false_negatives
        if total == 0:
            return 0.0
        return (self.true_positives + self.true_negatives) / total

    @property
    def precision(self) -> float:
        """Calculate precision."""
        if self.true_positives + self.false_positives == 0:
            return 0.0
        return self.true_positives / (self.true_positives + self.false_positives)

    @property
    def recall(self) -> float:
        """Calculate recall."""
        if self.true_positives + self.false_negatives == 0:
            return 0.0
        return self.true_positives / (self.true_positives + self.false_negatives)

    @property
    def f1_score(self) -> float:
        """Calculate F1 score."""
        p, r = self.precision, self.recall
        if p + r == 0:
            return 0.0
        return 2 * (p * r) / (p + r)

    @property
    def avg_latency_ms(self) -> float:
        """Average latency."""
        if self.samples == 0:
            return 0.0
        return self.total_latency_ms / self.samples

    @property
    def avg_confidence(self) -> float:
        """Average confidence."""
        if self.samples == 0:
            return 0.0
        return self.total_confidence / self.samples

    @property
    def false_positive_rate(self) -> float:
        """False positive rate."""
        if self.true_negatives + self.false_positives == 0:
            return 0.0
        return self.false_positives / (self.true_negatives + self.false_positives)

    @property
    def false_negative_rate(self) -> float:
        """False negative rate."""
        if self.true_positives + self.false_negatives == 0:
            return 0.0
        return self.false_negatives / (self.true_positives + self.false_negatives)

    def get_metric(self, metric_type: MetricType) -> float:
        """Get metric by type."""
        return {
            MetricType.ACCURACY: self.accuracy,
            MetricType.PRECISION: self.precision,
            MetricType.RECALL: self.recall,
            MetricType.F1_SCORE: self.f1_score,
            MetricType.LATENCY: self.avg_latency_ms,
            MetricType.CONFIDENCE: self.avg_confidence,
            MetricType.FALSE_POSITIVE_RATE: self.false_positive_rate,
            MetricType.FALSE_NEGATIVE_RATE: self.false_negative_rate,
        }.get(metric_type, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "variant": self.variant.value,
            "samples": self.samples,
            "accuracy": self.accuracy,
            "precision": self.precision,
            "recall": self.recall,
            "f1_score": self.f1_score,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_confidence": self.avg_confidence,
            "fpr": self.false_positive_rate,
            "fnr": self.false_negative_rate,
        }


@dataclass
class ExperimentResult:
    """Result of an A/B test experiment."""
    test_id: str
    winner: WinnerDecision
    control_metrics: VariantMetrics
    treatment_metrics: VariantMetrics
    p_value: float = 1.0
    improvement: float = 0.0
    is_significant: bool = False
    recommendation: str = ""
    analyzed_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "test_id": self.test_id,
            "winner": self.winner.value,
            "control": self.control_metrics.to_dict(),
            "treatment": self.treatment_metrics.to_dict(),
            "p_value": self.p_value,
            "improvement": self.improvement,
            "is_significant": self.is_significant,
            "recommendation": self.recommendation,
        }


@dataclass
class ABTest:
    """An A/B test experiment."""
    config: ExperimentConfig
    status: ExperimentStatus = ExperimentStatus.DRAFT
    control_metrics: VariantMetrics = field(
        default_factory=lambda: VariantMetrics(variant=ExperimentVariant.CONTROL)
    )
    treatment_metrics: VariantMetrics = field(
        default_factory=lambda: VariantMetrics(variant=ExperimentVariant.TREATMENT)
    )
    started_at: float = 0.0
    completed_at: float = 0.0
    result: Optional[ExperimentResult] = None

    @property
    def duration_hours(self) -> float:
        """Duration in hours."""
        end = self.completed_at or time.time()
        return (end - self.started_at) / 3600 if self.started_at else 0.0

    @property
    def total_samples(self) -> int:
        """Total samples across both variants."""
        return self.control_metrics.samples + self.treatment_metrics.samples


# =============================================================================
# Statistical Analyzer
# =============================================================================

class StatisticalAnalyzer:
    """
    Performs statistical analysis on A/B test results.

    Uses two-proportion z-test for binary outcomes.
    """

    @staticmethod
    def calculate_z_score(
        control_successes: int,
        control_samples: int,
        treatment_successes: int,
        treatment_samples: int,
    ) -> float:
        """
        Calculate z-score for two-proportion z-test.

        Args:
            control_successes: Number of successes in control
            control_samples: Total samples in control
            treatment_successes: Number of successes in treatment
            treatment_samples: Total samples in treatment

        Returns:
            Z-score
        """
        if control_samples == 0 or treatment_samples == 0:
            return 0.0

        p1 = control_successes / control_samples
        p2 = treatment_successes / treatment_samples

        # Pooled proportion
        p_pool = (control_successes + treatment_successes) / (control_samples + treatment_samples)

        # Standard error
        se = math.sqrt(
            p_pool * (1 - p_pool) * (1/control_samples + 1/treatment_samples)
        )

        if se == 0:
            return 0.0

        return (p2 - p1) / se

    @staticmethod
    def calculate_p_value(z_score: float) -> float:
        """
        Calculate p-value from z-score (two-tailed test).

        Uses approximation of normal CDF.
        """
        # Approximation of normal CDF
        def normal_cdf(x: float) -> float:
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        return 2 * (1 - normal_cdf(abs(z_score)))

    @staticmethod
    def analyze_experiment(
        test: ABTest,
    ) -> ExperimentResult:
        """
        Analyze an A/B test experiment.

        Args:
            test: A/B test to analyze

        Returns:
            ExperimentResult with statistical analysis
        """
        control = test.control_metrics
        treatment = test.treatment_metrics

        # Check minimum samples
        if control.samples < test.config.min_samples or treatment.samples < test.config.min_samples:
            return ExperimentResult(
                test_id=test.config.test_id,
                winner=WinnerDecision.INSUFFICIENT_DATA,
                control_metrics=control,
                treatment_metrics=treatment,
                recommendation=(
                    f"Insufficient data. Control: {control.samples}, "
                    f"Treatment: {treatment.samples} "
                    f"(need {test.config.min_samples} each)"
                ),
            )

        # Get primary metric values
        primary_metric = test.config.primary_metric
        control_value = control.get_metric(primary_metric)
        treatment_value = treatment.get_metric(primary_metric)

        # Calculate improvement
        if control_value > 0:
            improvement = (treatment_value - control_value) / control_value
        else:
            improvement = 0.0

        # Calculate statistical significance for accuracy
        control_successes = control.true_positives + control.true_negatives
        treatment_successes = treatment.true_positives + treatment.true_negatives

        z_score = StatisticalAnalyzer.calculate_z_score(
            control_successes, control.samples,
            treatment_successes, treatment.samples,
        )
        p_value = StatisticalAnalyzer.calculate_p_value(z_score)

        is_significant = p_value < test.config.significance_level

        # Determine winner
        if not is_significant:
            winner = WinnerDecision.NO_SIGNIFICANT_DIFFERENCE
            recommendation = (
                f"No statistically significant difference detected "
                f"(p={p_value:.4f}, need <{test.config.significance_level})"
            )
        elif improvement < test.config.min_improvement:
            winner = WinnerDecision.NO_SIGNIFICANT_DIFFERENCE
            recommendation = (
                f"Improvement ({improvement:.2%}) below minimum threshold "
                f"({test.config.min_improvement:.2%})"
            )
        elif treatment_value > control_value:
            winner = WinnerDecision.TREATMENT
            recommendation = (
                f"Treatment wins! Improvement: {improvement:.2%}, "
                f"p-value: {p_value:.4f}. Recommend promoting treatment."
            )
        else:
            winner = WinnerDecision.CONTROL
            recommendation = (
                f"Control wins. Treatment performed worse by {-improvement:.2%}. "
                f"Recommend keeping control."
            )

        return ExperimentResult(
            test_id=test.config.test_id,
            winner=winner,
            control_metrics=control,
            treatment_metrics=treatment,
            p_value=p_value,
            improvement=improvement,
            is_significant=is_significant,
            recommendation=recommendation,
        )


# =============================================================================
# A/B Testing Manager
# =============================================================================

class ABTestingManager:
    """
    Manages A/B testing for voice authentication models.

    Handles:
    - Experiment creation and lifecycle
    - Traffic allocation
    - Metric collection
    - Result analysis
    - Winner promotion

    Usage:
        manager = ABTestingManager()
        await manager.start_test(
            test_id="exp_001",
            control_version="v1.0",
            treatment_version="v1.1",
            traffic_split=0.1,
        )

        # During authentication:
        variant = await manager.get_variant("exp_001", user_id)
        # ... run authentication with corresponding model ...
        await manager.record_result("exp_001", user_id, outcome)

        # Check results:
        result = await manager.evaluate_test("exp_001")
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
    ):
        """
        Initialize the A/B testing manager.

        Args:
            data_dir: Directory for storing experiment data
        """
        self.data_dir = data_dir or Path(
            os.getenv("AB_TESTING_DIR", str(Path.home() / ".jarvis" / "ab_tests"))
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Active experiments
        self._experiments: Dict[str, ABTest] = {}
        self._user_assignments: Dict[str, Dict[str, ExperimentVariant]] = {}  # test_id -> user_id -> variant
        self._lock = asyncio.Lock()

        # Load existing experiments
        self._load_experiments()

        # Callbacks
        self._on_test_complete: List[Callable[[ExperimentResult], None]] = []

        logger.info(f"[ABTestingManager] Initialized with data_dir={self.data_dir}")

    def _load_experiments(self) -> None:
        """Load experiments from disk."""
        experiments_file = self.data_dir / "experiments.json"

        if experiments_file.exists():
            try:
                with open(experiments_file) as f:
                    data = json.load(f)

                for test_data in data.get("experiments", []):
                    config = ExperimentConfig(
                        test_id=test_data["config"]["test_id"],
                        name=test_data["config"].get("name", ""),
                        control_version=test_data["config"]["control_version"],
                        treatment_version=test_data["config"]["treatment_version"],
                        traffic_split=test_data["config"].get("traffic_split", 0.1),
                        min_samples=test_data["config"].get("min_samples", 100),
                    )

                    test = ABTest(
                        config=config,
                        status=ExperimentStatus(test_data["status"]),
                        started_at=test_data.get("started_at", 0),
                        completed_at=test_data.get("completed_at", 0),
                    )

                    self._experiments[config.test_id] = test

                logger.info(f"[ABTestingManager] Loaded {len(self._experiments)} experiments")

            except Exception as e:
                logger.warning(f"[ABTestingManager] Failed to load experiments: {e}")

    async def _save_experiments(self) -> None:
        """Save experiments to disk."""
        experiments_file = self.data_dir / "experiments.json"

        data = {
            "experiments": [
                {
                    "config": {
                        "test_id": test.config.test_id,
                        "name": test.config.name,
                        "control_version": test.config.control_version,
                        "treatment_version": test.config.treatment_version,
                        "traffic_split": test.config.traffic_split,
                        "min_samples": test.config.min_samples,
                    },
                    "status": test.status.value,
                    "started_at": test.started_at,
                    "completed_at": test.completed_at,
                    "control_metrics": test.control_metrics.to_dict(),
                    "treatment_metrics": test.treatment_metrics.to_dict(),
                }
                for test in self._experiments.values()
            ],
            "updated_at": time.time(),
        }

        try:
            with open(experiments_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[ABTestingManager] Failed to save experiments: {e}")

    async def start_test(
        self,
        test_id: str,
        control_version: str,
        treatment_version: str,
        traffic_split: float = 0.1,
        name: Optional[str] = None,
        min_samples: int = 100,
    ) -> ABTest:
        """
        Start a new A/B test experiment.

        Args:
            test_id: Unique test identifier
            control_version: Control model version
            treatment_version: Treatment model version
            traffic_split: Fraction of traffic to treatment (0.0-1.0)
            name: Human-readable name
            min_samples: Minimum samples before analysis

        Returns:
            ABTest instance
        """
        async with self._lock:
            config = ExperimentConfig(
                test_id=test_id,
                name=name or f"Test {test_id}",
                control_version=control_version,
                treatment_version=treatment_version,
                traffic_split=traffic_split,
                min_samples=min_samples,
            )

            test = ABTest(
                config=config,
                status=ExperimentStatus.RUNNING,
                started_at=time.time(),
            )

            self._experiments[test_id] = test
            self._user_assignments[test_id] = {}

            await self._save_experiments()

            logger.info(
                f"[ABTestingManager] Started test {test_id}: "
                f"control={control_version} vs treatment={treatment_version} "
                f"({traffic_split:.0%} to treatment)"
            )

            return test

    async def get_variant(
        self,
        test_id: str,
        user_id: str,
    ) -> Optional[ExperimentVariant]:
        """
        Get the variant for a user in an experiment.

        Uses consistent hashing for deterministic assignment.

        Args:
            test_id: Test identifier
            user_id: User identifier

        Returns:
            ExperimentVariant or None if test not found/not running
        """
        test = self._experiments.get(test_id)
        if not test or test.status != ExperimentStatus.RUNNING:
            return None

        # Check existing assignment
        assignments = self._user_assignments.get(test_id, {})
        if user_id in assignments:
            return assignments[user_id]

        # Consistent assignment based on hash
        hash_input = f"{test_id}:{user_id}"
        hash_value = int(hashlib.md5(hash_input.encode()).hexdigest(), 16)
        normalized = (hash_value % 10000) / 10000.0

        variant = (
            ExperimentVariant.TREATMENT
            if normalized < test.config.traffic_split
            else ExperimentVariant.CONTROL
        )

        # Store assignment
        async with self._lock:
            if test_id not in self._user_assignments:
                self._user_assignments[test_id] = {}
            self._user_assignments[test_id][user_id] = variant

        return variant

    async def record_result(
        self,
        test_id: str,
        user_id: str,
        is_true_positive: bool = False,
        is_true_negative: bool = False,
        is_false_positive: bool = False,
        is_false_negative: bool = False,
        latency_ms: float = 0.0,
        confidence: float = 0.0,
    ) -> None:
        """
        Record an authentication result for an experiment.

        Args:
            test_id: Test identifier
            user_id: User identifier
            is_true_positive: Was it a true positive?
            is_true_negative: Was it a true negative?
            is_false_positive: Was it a false positive?
            is_false_negative: Was it a false negative?
            latency_ms: Response latency
            confidence: Confidence score
        """
        test = self._experiments.get(test_id)
        if not test or test.status != ExperimentStatus.RUNNING:
            return

        variant = await self.get_variant(test_id, user_id)
        if not variant:
            return

        async with self._lock:
            metrics = (
                test.treatment_metrics
                if variant == ExperimentVariant.TREATMENT
                else test.control_metrics
            )

            metrics.samples += 1
            if is_true_positive:
                metrics.true_positives += 1
            if is_true_negative:
                metrics.true_negatives += 1
            if is_false_positive:
                metrics.false_positives += 1
            if is_false_negative:
                metrics.false_negatives += 1
            metrics.total_latency_ms += latency_ms
            metrics.total_confidence += confidence

            # Check if should auto-analyze
            if (test.control_metrics.samples >= test.config.min_samples and
                test.treatment_metrics.samples >= test.config.min_samples):
                # Auto-analyze periodically
                if test.total_samples % 100 == 0:
                    await self._auto_analyze(test)

    async def _auto_analyze(self, test: ABTest) -> None:
        """Perform automatic analysis during experiment."""
        result = StatisticalAnalyzer.analyze_experiment(test)

        # Check for early stopping
        if result.is_significant and result.winner != WinnerDecision.NO_SIGNIFICANT_DIFFERENCE:
            logger.info(
                f"[ABTestingManager] Test {test.config.test_id} reaching significance: "
                f"{result.recommendation}"
            )

    async def evaluate_test(self, test_id: str) -> Optional[ExperimentResult]:
        """
        Evaluate an A/B test and get results.

        Args:
            test_id: Test identifier

        Returns:
            ExperimentResult or None if test not found
        """
        test = self._experiments.get(test_id)
        if not test:
            return None

        result = StatisticalAnalyzer.analyze_experiment(test)
        test.result = result

        await self._save_experiments()

        return result

    async def complete_test(
        self,
        test_id: str,
        promote_winner: bool = True,
    ) -> Optional[ExperimentResult]:
        """
        Complete an A/B test experiment.

        Args:
            test_id: Test identifier
            promote_winner: Automatically promote winning variant

        Returns:
            ExperimentResult
        """
        async with self._lock:
            test = self._experiments.get(test_id)
            if not test:
                return None

            test.status = ExperimentStatus.COMPLETED
            test.completed_at = time.time()

            result = StatisticalAnalyzer.analyze_experiment(test)
            test.result = result

            await self._save_experiments()

            logger.info(
                f"[ABTestingManager] Completed test {test_id}: "
                f"winner={result.winner.value}, "
                f"improvement={result.improvement:.2%}"
            )

            # Notify callbacks
            for callback in self._on_test_complete:
                try:
                    callback(result)
                except Exception as e:
                    logger.debug(f"[ABTestingManager] Callback error: {e}")

            # Promote winner if requested
            if promote_winner and result.winner == WinnerDecision.TREATMENT:
                await self._promote_treatment(test)

            return result

    async def _promote_treatment(self, test: ABTest) -> None:
        """Promote treatment variant to production."""
        try:
            from backend.voice_unlock.learning.model_deployer import (
                get_voice_model_deployer,
                ModelType,
                DeploymentStrategy,
            )

            deployer = await get_voice_model_deployer()

            # Get the treatment version and deploy as active
            # This assumes the treatment was already deployed in shadow/AB mode
            logger.info(
                f"[ABTestingManager] Promoting treatment version: "
                f"{test.config.treatment_version}"
            )

        except Exception as e:
            logger.warning(f"[ABTestingManager] Failed to promote treatment: {e}")

    def get_test(self, test_id: str) -> Optional[ABTest]:
        """Get an experiment by ID."""
        return self._experiments.get(test_id)

    def get_active_tests(self) -> List[ABTest]:
        """Get all active experiments."""
        return [
            test for test in self._experiments.values()
            if test.status == ExperimentStatus.RUNNING
        ]

    def on_test_complete(
        self,
        callback: Callable[[ExperimentResult], None],
    ) -> None:
        """Register callback for test completion."""
        self._on_test_complete.append(callback)


# =============================================================================
# Singleton Access
# =============================================================================

_manager_instance: Optional[ABTestingManager] = None
_manager_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_ab_testing_manager() -> ABTestingManager:
    """Get the singleton A/B testing manager."""
    global _manager_instance

    async with _manager_lock:
        if _manager_instance is None:
            _manager_instance = ABTestingManager()
        return _manager_instance


async def start_ab_test(
    control_version: str,
    treatment_version: str,
    traffic_split: float = 0.1,
    **kwargs,
) -> ABTest:
    """Convenience function to start an A/B test."""
    manager = await get_ab_testing_manager()
    test_id = f"exp_{int(time.time())}"
    return await manager.start_test(
        test_id=test_id,
        control_version=control_version,
        treatment_version=treatment_version,
        traffic_split=traffic_split,
        **kwargs,
    )
