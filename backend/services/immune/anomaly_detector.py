"""AnomalyDetector — ML-based anomaly detection service.

Extracted from unified_supervisor.py (lines 43377-43626).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.services.immune._base import (
    CapabilityContract,
    ServiceHealthReport,
    SystemKernelConfig,
    SystemService,
)


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------

@dataclass
class AnomalyScore:
    """Score for an anomaly detection."""
    score: float  # 0.0 = normal, 1.0 = highly anomalous
    category: str
    features: Dict[str, float]
    threshold: float
    is_anomaly: bool
    timestamp: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# AnomalyDetector
# ---------------------------------------------------------------------------

class AnomalyDetector(SystemService):
    """
    Machine learning-based anomaly detection system.

    Detects unusual patterns in system behavior, access patterns,
    and data flows using statistical and ML-based methods.

    Features:
    - Statistical anomaly detection (z-score, IQR)
    - Time-series anomaly detection
    - Behavioral baseline learning
    - Multi-dimensional anomaly scoring
    - Adaptive thresholds
    """

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._baselines: Dict[str, Dict[str, Any]] = {}
        self._history: Dict[str, deque] = {}
        self._history_size: int = 10000
        self._anomaly_log: deque = deque(maxlen=50000)
        self._detection_handlers: List[Callable] = []
        self._thresholds: Dict[str, float] = {
            "access": 0.85,
            "performance": 0.90,
            "security": 0.75,
            "data": 0.80,
        }
        self._logger = logging.getLogger("AnomalyDetector")
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize anomaly detector."""
        try:
            self._initialized = True
            self._logger.info("Anomaly detector initialized")
            return True
        except Exception as e:
            self._logger.error(f"Failed to initialize anomaly detector: {e}")
            return False

    async def record_observation(
        self,
        category: str,
        features: Dict[str, float],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[AnomalyScore]:
        """
        Record an observation and check for anomalies.

        Args:
            category: Category of observation (access, performance, etc.)
            features: Feature vector (name -> value)
            metadata: Additional context

        Returns:
            AnomalyScore if anomaly detected, None otherwise
        """
        async with self._lock:
            # Initialize history for category if needed
            if category not in self._history:
                self._history[category] = deque(maxlen=self._history_size)

            # Record observation
            observation = {
                "timestamp": datetime.now(),
                "features": features,
                "metadata": metadata or {},
            }
            self._history[category].append(observation)

            # Update baseline
            self._update_baseline(category, features)

            # Calculate anomaly score
            score = self._calculate_anomaly_score(category, features)

            if score.is_anomaly:
                self._anomaly_log.append({
                    "timestamp": datetime.now().isoformat(),
                    "category": category,
                    "score": score.score,
                    "features": features,
                    "metadata": metadata,
                })

                # Trigger handlers
                for handler in self._detection_handlers:
                    try:
                        await handler(score, metadata)
                    except Exception as e:
                        self._logger.error(f"Anomaly handler error: {e}")

                return score

            return None

    def _update_baseline(self, category: str, features: Dict[str, float]) -> None:
        """Update baseline statistics for a category."""
        if category not in self._baselines:
            self._baselines[category] = {}

        baseline = self._baselines[category]

        for name, value in features.items():
            if name not in baseline:
                baseline[name] = {
                    "count": 0,
                    "sum": 0.0,
                    "sum_sq": 0.0,
                    "min": float("inf"),
                    "max": float("-inf"),
                }

            stats = baseline[name]
            stats["count"] += 1
            stats["sum"] += value
            stats["sum_sq"] += value * value
            stats["min"] = min(stats["min"], value)
            stats["max"] = max(stats["max"], value)

    def _calculate_anomaly_score(
        self,
        category: str,
        features: Dict[str, float],
    ) -> AnomalyScore:
        """Calculate anomaly score using statistical methods."""
        baseline = self._baselines.get(category, {})
        threshold = self._thresholds.get(category, 0.85)

        if not baseline:
            return AnomalyScore(
                score=0.0,
                category=category,
                features=features,
                threshold=threshold,
                is_anomaly=False,
            )

        feature_scores: Dict[str, float] = {}

        for name, value in features.items():
            if name not in baseline:
                feature_scores[name] = 0.0
                continue

            stats = baseline[name]
            count = stats["count"]

            if count < 10:
                # Not enough data for statistical analysis
                feature_scores[name] = 0.0
                continue

            # Calculate mean and std dev
            mean = stats["sum"] / count
            variance = (stats["sum_sq"] / count) - (mean * mean)
            std_dev = max(variance ** 0.5, 0.001)  # Avoid division by zero

            # Calculate z-score
            z_score = abs(value - mean) / std_dev

            # Convert to 0-1 score using sigmoid-like function
            feature_scores[name] = min(1.0, z_score / 3.0)  # 3 std devs = 1.0

        # Aggregate feature scores
        if feature_scores:
            max_score = max(feature_scores.values())
            avg_score = sum(feature_scores.values()) / len(feature_scores)
            # Weight towards max score but consider average
            overall_score = 0.7 * max_score + 0.3 * avg_score
        else:
            overall_score = 0.0

        return AnomalyScore(
            score=overall_score,
            category=category,
            features=feature_scores,
            threshold=threshold,
            is_anomaly=overall_score >= threshold,
        )

    def register_handler(
        self,
        handler: Callable[[AnomalyScore, Optional[Dict[str, Any]]], Awaitable[None]],
    ) -> None:
        """Register a handler for anomaly detection."""
        self._detection_handlers.append(handler)

    def set_threshold(self, category: str, threshold: float) -> None:
        """Set detection threshold for a category."""
        self._thresholds[category] = max(0.0, min(1.0, threshold))

    def get_baseline(self, category: str) -> Optional[Dict[str, Any]]:
        """Get baseline statistics for a category."""
        return self._baselines.get(category)

    def get_recent_anomalies(
        self,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get recent anomaly detections."""
        entries = list(self._anomaly_log)

        if category:
            entries = [e for e in entries if e.get("category") == category]

        return entries[-limit:]

    # -- SystemService ABC --------------------------------------------------
    async def health_check(self) -> Tuple[bool, str]:
        anomalies = len(self._anomaly_log)
        baselines = len(self._baselines)
        return (True, f"AnomalyDetector: {baselines} baselines, {anomalies} anomalies logged")

    async def cleanup(self) -> None:
        self._anomaly_log.clear()
        self._history.clear()

    async def start(self) -> bool:
        if not self._initialized:
            await self.initialize()
        return True

    async def health(self) -> ServiceHealthReport:
        return ServiceHealthReport(
            alive=True,
            ready=self._initialized,
            message=f"AnomalyDetector: initialized={self._initialized}, baselines={len(self._baselines)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        return True

    async def stop(self) -> None:
        await self.cleanup()

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="AnomalyDetector",
            version="1.0.0",
            inputs=["telemetry.metric"],
            outputs=["anomaly.detected"],
            side_effects=["writes_anomaly_scores"],
        )

    def activation_triggers(self) -> List[str]:
        return []  # always_on
