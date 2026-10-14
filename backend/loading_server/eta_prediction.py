"""
Predictive ETA Calculator for JARVIS Loading Server v212.0
===========================================================

ML-based ETA prediction using historical startup data.

Features:
- Linear regression for stage duration prediction
- Exponential moving average (EMA) for progress rate smoothing
- Historical startup data analysis with SQLite persistence
- Adaptive learning from each startup
- Confidence intervals for predictions
- Multi-method prediction (EMA + historical average)

Usage:
    from backend.loading_server.eta_prediction import PredictiveETACalculator

    calculator = PredictiveETACalculator()
    calculator.start_session("session-123")
    calculator.update_progress(50.0)
    eta, confidence = calculator.predict_eta(50.0)
    print(f"ETA: {eta}s (confidence: {confidence})")

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("LoadingServer.ETA")


@dataclass
class ComponentTiming:
    """Historical timing data for a component."""

    name: str
    durations: List[float] = field(default_factory=list)
    max_samples: int = 100

    def add_duration(self, duration: float) -> None:
        """Add a duration sample."""
        self.durations.append(duration)
        if len(self.durations) > self.max_samples:
            self.durations = self.durations[-self.max_samples:]

    @property
    def mean_duration(self) -> float:
        """Get mean duration."""
        return statistics.mean(self.durations) if self.durations else 0.0

    @property
    def std_duration(self) -> float:
        """Get standard deviation."""
        return statistics.stdev(self.durations) if len(self.durations) > 1 else 0.0

    @property
    def predicted_duration(self) -> float:
        """Get predicted duration with 95th percentile buffer."""
        if not self.durations:
            return 5.0  # Default 5 seconds
        return self.mean_duration + (1.645 * self.std_duration)


@dataclass
class PredictiveETACalculator:
    """
    ML-based ETA prediction using historical startup data.

    Uses multiple prediction methods:
    1. Exponential Moving Average (EMA) for real-time progress rate
    2. Historical average for stage-based prediction
    3. Linear regression on recent progress points

    The final ETA is a weighted combination of these methods,
    with higher weight given to methods with more data.

    Attributes:
        db_path: Path to SQLite database for historical data
        ema_alpha: Smoothing factor for EMA (0.3 = 30% weight to new data)
        json_history_path: Path to JSON file for component timing cache
    """

    db_path: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "loading_server" / "eta.db"
    )
    json_history_path: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "cache" / "eta_history.json"
    )
    ema_alpha: float = 0.3
    # Re-entrant: the public methods compose (finish_session records the
    # "total" stage through record_stage_completion while holding it), and a
    # plain Lock deadlocked the loading server's loop at shutdown.
    _lock: threading.RLock = field(init=False, default_factory=threading.RLock)

    # Current session tracking
    _session_id: Optional[str] = field(init=False, default=None)
    _session_start: Optional[float] = field(init=False, default=None)
    _last_progress: float = field(init=False, default=0.0)
    _last_update_time: Optional[float] = field(init=False, default=None)
    _progress_rate_ema: Optional[float] = field(init=False, default=None)
    _progress_history: List[Tuple[float, float]] = field(
        init=False, default_factory=list
    )
    _component_timings: Dict[str, ComponentTiming] = field(
        init=False, default_factory=dict
    )

    def __post_init__(self):
        """Initialize database and load history."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_history()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with proper settings."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        """Initialize SQLite database for historical data."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS startup_history (
                        session_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        duration REAL NOT NULL,
                        final_progress REAL NOT NULL,
                        PRIMARY KEY (session_id, stage)
                    )
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_stage_duration
                    ON startup_history(stage, duration)
                """)

                conn.commit()

    def _load_history(self) -> None:
        """Load historical timing data from JSON cache."""
        try:
            if self.json_history_path.exists():
                with open(self.json_history_path) as f:
                    data = json.load(f)
                    for name, durations in data.get("components", {}).items():
                        self._component_timings[name] = ComponentTiming(
                            name=name, durations=durations
                        )
                    logger.debug(
                        f"[ETA] Loaded history for {len(self._component_timings)} components"
                    )
        except Exception as e:
            logger.debug(f"[ETA] Could not load history: {e}")

    def _save_history(self) -> None:
        """Save timing history to JSON cache for future predictions."""
        try:
            self.json_history_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "components": {
                    name: timing.durations
                    for name, timing in self._component_timings.items()
                },
                "updated_at": time.time(),
            }
            with open(self.json_history_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"[ETA] Could not save history: {e}")

    def start_session(self, session_id: str) -> None:
        """
        Start tracking a new startup session.

        Args:
            session_id: Unique identifier for this session
        """
        with self._lock:
            self._session_id = session_id
            self._session_start = time.monotonic()
            self._last_progress = 0.0
            self._last_update_time = self._session_start
            self._progress_rate_ema = None
            self._progress_history.clear()

    def update_progress(self, current_progress: float) -> None:
        """
        Update current progress and calculate rate.

        This method:
        1. Records the progress point with timestamp
        2. Calculates instantaneous progress rate
        3. Updates the EMA of progress rate

        Args:
            current_progress: Current progress percentage (0-100)
        """
        now = time.monotonic()

        with self._lock:
            if self._last_update_time is None:
                self._last_update_time = now
                self._last_progress = current_progress
                return

            # Calculate instantaneous progress rate (progress per second)
            elapsed = now - self._last_update_time
            if elapsed > 0:
                progress_delta = current_progress - self._last_progress
                instant_rate = progress_delta / elapsed

                # Update EMA
                if self._progress_rate_ema is None:
                    self._progress_rate_ema = instant_rate
                else:
                    self._progress_rate_ema = (
                        self.ema_alpha * instant_rate
                        + (1 - self.ema_alpha) * self._progress_rate_ema
                    )

            # Record progress point (keep last 100)
            self._progress_history.append((now, current_progress))
            if len(self._progress_history) > 100:
                self._progress_history = self._progress_history[-100:]

            self._last_progress = current_progress
            self._last_update_time = now

    def predict_eta(
        self, current_progress: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Predict estimated time to completion.

        Uses multiple methods:
        1. EMA-based prediction (real-time progress rate)
        2. Historical average (from database)
        3. Linear extrapolation from recent progress points

        Returns:
            (eta_seconds, confidence) - ETA in seconds and confidence (0-1)
            Returns (None, None) if insufficient data
        """
        # Need at least some progress to predict
        if current_progress <= 0:
            return (None, None)

        if current_progress >= 100:
            return (0.0, 1.0)

        # Update with current progress
        self.update_progress(current_progress)

        with self._lock:
            # Method 1: EMA-based prediction (real-time rate)
            eta_ema = None
            if self._progress_rate_ema and self._progress_rate_ema > 0:
                remaining_progress = 100.0 - current_progress
                eta_ema = remaining_progress / self._progress_rate_ema

            # Method 2: Historical average (from database)
            eta_historical = self._predict_from_historical()

            # Method 3: Linear extrapolation from recent points
            eta_linear = self._predict_from_linear()

            # Combine predictions (weighted average)
            estimates: List[Tuple[float, float]] = []

            if eta_ema is not None and eta_ema > 0 and eta_ema < 600:
                # More weight to EMA if we have good data
                weight = 0.5 if current_progress > 20 else 0.3
                estimates.append((eta_ema, weight))

            if eta_historical is not None and eta_historical > 0 and eta_historical < 600:
                weight = 0.3
                estimates.append((eta_historical, weight))

            if eta_linear is not None and eta_linear > 0 and eta_linear < 600:
                # Linear gets higher weight with more data points
                weight = 0.4 if len(self._progress_history) > 10 else 0.2
                estimates.append((eta_linear, weight))

            if not estimates:
                return (None, None)

            # Weighted average
            total_weight = sum(w for _, w in estimates)
            eta = sum(e * w for e, w in estimates) / total_weight

            # Calculate confidence based on:
            # - Number of data points
            # - Agreement between methods
            # - Progress level
            confidence = self._calculate_confidence(estimates, current_progress)

            # Clamp to reasonable bounds (0.1s to 600s)
            eta = max(0.1, min(eta, 600.0))

            return (round(eta, 1), round(confidence, 2))

    def _predict_from_historical(self) -> Optional[float]:
        """Predict ETA based on historical startup times."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT AVG(duration) as avg_duration
                    FROM startup_history
                    WHERE stage = 'total'
                    ORDER BY start_time DESC
                    LIMIT 10
                """)

                row = cursor.fetchone()
                if row and row[0]:
                    avg_total_duration = row[0]

                    # Estimate remaining time based on average total
                    if self._session_start:
                        elapsed = time.monotonic() - self._session_start
                        remaining = max(0, avg_total_duration - elapsed)
                        return remaining
        except Exception as e:
            logger.debug(f"[ETA] Historical prediction error: {e}")

        return None

    def _predict_from_linear(self) -> Optional[float]:
        """Predict ETA using linear extrapolation from recent progress points."""
        if len(self._progress_history) < 3:
            return None

        # Use last 10 points for linear regression
        recent = self._progress_history[-10:]

        if len(recent) < 3:
            return None

        # Simple linear regression
        t0, p0 = recent[0]
        t1, p1 = recent[-1]

        if t1 <= t0 or p1 <= p0:
            return None

        # Progress rate from linear fit
        rate = (p1 - p0) / (t1 - t0)

        if rate <= 0:
            return None

        # Time to reach 100%
        remaining_progress = 100.0 - p1
        eta = remaining_progress / rate

        return eta

    def _calculate_confidence(
        self, estimates: List[Tuple[float, float]], progress: float
    ) -> float:
        """
        Calculate confidence score for ETA prediction.

        Based on:
        - Number of estimation methods available
        - Agreement between methods
        - Current progress level
        """
        if not estimates:
            return 0.0

        # Base confidence from number of methods
        base_confidence = min(0.3 + (len(estimates) * 0.2), 0.7)

        # Adjust for progress level (more progress = more confidence)
        progress_factor = min(progress / 100.0, 1.0)
        confidence = base_confidence + (progress_factor * 0.2)

        # Adjust for agreement between methods
        if len(estimates) > 1:
            etas = [e for e, _ in estimates]
            mean_eta = statistics.mean(etas)
            if mean_eta > 0:
                # Coefficient of variation
                cv = statistics.stdev(etas) / mean_eta if len(etas) > 1 else 0
                # Lower CV = better agreement = higher confidence
                agreement_factor = max(0, 1 - cv)
                confidence *= (0.7 + 0.3 * agreement_factor)

        return min(confidence, 0.95)

    def record_stage_completion(
        self,
        session_id: str,
        stage: str,
        start_time: float,
        end_time: float,
        final_progress: float,
    ) -> None:
        """
        Record completed stage for future predictions.

        Args:
            session_id: Session identifier
            stage: Stage name (e.g., "backend", "frontend", "total")
            start_time: Stage start timestamp (monotonic)
            end_time: Stage end timestamp (monotonic)
            final_progress: Progress at stage completion
        """
        duration = end_time - start_time

        with self._lock:
            try:
                with self._get_connection() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO startup_history
                        (session_id, stage, start_time, end_time, duration, final_progress)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (session_id, stage, start_time, end_time, duration, final_progress),
                    )
                    conn.commit()
            except Exception as e:
                logger.debug(f"[ETA] Could not record stage: {e}")

            # Also update component timing
            if stage not in self._component_timings:
                self._component_timings[stage] = ComponentTiming(name=stage)
            self._component_timings[stage].add_duration(duration)

    def record_component_complete(self, name: str, duration: float) -> None:
        """
        Record component completion time for future predictions.

        Args:
            name: Component name
            duration: Time taken in seconds
        """
        with self._lock:
            if name not in self._component_timings:
                self._component_timings[name] = ComponentTiming(name=name)
            self._component_timings[name].add_duration(duration)

    def finish_session(self) -> None:
        """
        Finish tracking and save history.

        Call this when startup completes to persist learning data.
        """
        with self._lock:
            # Record total session duration
            if self._session_id and self._session_start:
                total_duration = time.monotonic() - self._session_start
                self.record_stage_completion(
                    self._session_id,
                    "total",
                    self._session_start,
                    time.monotonic(),
                    100.0,
                )
                logger.debug(
                    f"[ETA] Session {self._session_id} completed in {total_duration:.1f}s"
                )

            # Save component timings
            self._save_history()

            # Reset session state
            self._session_id = None
            self._session_start = None
            self._progress_history.clear()

    def get_predicted_eta(self) -> Dict[str, Any]:
        """
        Get predicted ETA in dictionary format (compatible with existing API).

        Returns:
            Dict with eta_seconds, confidence, method, elapsed_seconds, progress_rate
        """
        if self._session_start is None:
            return {"eta_seconds": None, "confidence": 0.0, "method": "none"}

        elapsed = time.monotonic() - self._session_start

        if self._last_progress >= 100:
            return {"eta_seconds": 0, "confidence": 1.0, "method": "complete"}

        eta, confidence = self.predict_eta(self._last_progress)

        if eta is None:
            return {"eta_seconds": 60, "confidence": 0.2, "method": "fallback"}

        return {
            "eta_seconds": eta,
            "confidence": confidence,
            "method": "weighted_ensemble",
            "elapsed_seconds": round(elapsed, 1),
            "progress_rate": round(
                self._progress_rate_ema if self._progress_rate_ema else 0, 2
            ),
        }

    def get_startup_analytics(self) -> Dict[str, Any]:
        """
        Get comprehensive startup performance analytics.

        Returns:
            Analytics summary with:
            - Historical startup times (last 20 startups)
            - Average, min, max durations
            - Stage-level breakdown
            - Trend analysis
        """
        with self._lock:
            try:
                with self._get_connection() as conn:
                    # Get overall statistics
                    cursor = conn.execute("""
                        SELECT
                            COUNT(DISTINCT session_id) as total_startups,
                            AVG(duration) as avg_duration,
                            MIN(duration) as min_duration,
                            MAX(duration) as max_duration
                        FROM startup_history
                        WHERE stage = 'total'
                    """)
                    stats = cursor.fetchone()

                    # Get recent startups (last 20)
                    cursor = conn.execute("""
                        SELECT
                            session_id,
                            start_time,
                            duration,
                            final_progress
                        FROM startup_history
                        WHERE stage = 'total'
                        ORDER BY start_time DESC
                        LIMIT 20
                    """)
                    recent_startups = [
                        {
                            "session_id": row[0],
                            "start_time": row[1],
                            "duration": row[2],
                            "final_progress": row[3],
                        }
                        for row in cursor.fetchall()
                    ]

                    # Calculate trend
                    cursor = conn.execute("""
                        SELECT AVG(duration)
                        FROM (
                            SELECT duration FROM startup_history
                            WHERE stage = 'total'
                            ORDER BY start_time DESC LIMIT 5
                        )
                    """)
                    recent_avg = cursor.fetchone()[0] or 0

                    cursor = conn.execute("""
                        SELECT AVG(duration)
                        FROM (
                            SELECT duration FROM startup_history
                            WHERE stage = 'total'
                            ORDER BY start_time DESC LIMIT 20 OFFSET 5
                        )
                    """)
                    older_avg = cursor.fetchone()[0] or 0

                    trend = None
                    if older_avg > 0:
                        trend = ((recent_avg - older_avg) / older_avg) * 100

                    return {
                        "total_startups": stats[0] if stats else 0,
                        "average_duration": round(stats[1], 2) if stats and stats[1] else None,
                        "min_duration": round(stats[2], 2) if stats and stats[2] else None,
                        "max_duration": round(stats[3], 2) if stats and stats[3] else None,
                        "recent_startups": recent_startups,
                        "trend_percentage": round(trend, 1) if trend else None,
                        "trend_direction": (
                            "improving" if trend and trend < 0
                            else "degrading" if trend and trend > 0
                            else "stable"
                        ),
                        "component_timings": {
                            name: {
                                "mean": round(timing.mean_duration, 2),
                                "std": round(timing.std_duration, 2),
                                "predicted": round(timing.predicted_duration, 2),
                                "samples": len(timing.durations),
                            }
                            for name, timing in self._component_timings.items()
                        },
                    }
            except Exception as e:
                logger.debug(f"[ETA] Analytics error: {e}")
                return {
                    "error": str(e),
                    "total_startups": 0,
                }
