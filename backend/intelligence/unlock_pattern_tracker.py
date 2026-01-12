#!/usr/bin/env python3
"""
Unlock Pattern Tracker for JARVIS Voice Authentication
=======================================================

Tracks behavioral patterns for temporal authentication intelligence:
- Typical unlock times (morning, evening, night)
- Time since last lock/unlock
- Unlock frequency patterns
- Day-of-week and hour-of-day distributions
- Anomaly detection for unusual unlock times

Enables behavioral verification like:
- "It's 7:15 AM (typical unlock time ✓)"
- "Always unlocks within 30 min of waking ✓"
- "Unusual unlock time - 3 AM (typically unlocks at 7 AM)"

Author: Derek J. Russell
Version: 1.0.0
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta, time
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict, deque
import numpy as np

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================
class UnlockPatternConfig:
    """Dynamic configuration for unlock pattern tracker."""

    def __init__(self):
        # Pattern detection thresholds
        self.min_samples_for_pattern = int(os.getenv('UNLOCK_MIN_SAMPLES', '10'))  # Need 10 unlocks min
        self.typical_time_window_minutes = int(os.getenv('UNLOCK_TIME_WINDOW', '30'))  # ±30 min is "typical"
        self.anomaly_std_threshold = float(os.getenv('UNLOCK_ANOMALY_STD', '2.5'))  # 2.5 std devs = anomaly

        # Confidence scoring
        self.high_confidence_threshold = float(os.getenv('UNLOCK_HIGH_CONF', '0.90'))
        self.medium_confidence_threshold = float(os.getenv('UNLOCK_MEDIUM_CONF', '0.75'))

        # Time-of-day buckets (hours)
        self.time_buckets = {
            'early_morning': (5, 8),    # 5-8 AM
            'morning': (8, 12),          # 8 AM-12 PM
            'afternoon': (12, 17),       # 12-5 PM
            'evening': (17, 21),         # 5-9 PM
            'night': (21, 24),           # 9 PM-12 AM
            'late_night': (0, 5),        # 12-5 AM
        }

        # History limits
        self.max_unlock_history = int(os.getenv('UNLOCK_MAX_HISTORY', '500'))  # Keep last 500 unlocks
        self.pattern_decay_days = int(os.getenv('UNLOCK_DECAY_DAYS', '90'))  # Patterns decay after 90 days

        # Database path
        self.db_path = Path(os.getenv(
            'UNLOCK_PATTERN_DB',
            os.path.expanduser('~/.jarvis/unlock_patterns.json')
        ))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_config = UnlockPatternConfig()


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class UnlockEvent:
    """Single unlock event."""
    timestamp: datetime
    hour: int
    day_of_week: int  # 0=Monday, 6=Sunday
    success: bool
    confidence: float
    duration_since_last_unlock_minutes: Optional[int] = None
    location_type: str = "unknown"  # home, office, other
    network_ssid_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'timestamp': self.timestamp.isoformat(),
            'hour': self.hour,
            'day_of_week': self.day_of_week,
            'success': self.success,
            'confidence': self.confidence,
            'duration_since_last_unlock_minutes': self.duration_since_last_unlock_minutes,
            'location_type': self.location_type,
            'network_ssid_hash': self.network_ssid_hash
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'UnlockEvent':
        """Create from dictionary."""
        return UnlockEvent(
            timestamp=datetime.fromisoformat(data['timestamp']),
            hour=data['hour'],
            day_of_week=data['day_of_week'],
            success=data['success'],
            confidence=data['confidence'],
            duration_since_last_unlock_minutes=data.get('duration_since_last_unlock_minutes'),
            location_type=data.get('location_type', 'unknown'),
            network_ssid_hash=data.get('network_ssid_hash')
        )


@dataclass
class UnlockPattern:
    """Learned unlock pattern statistics."""
    typical_hours: List[int] = field(default_factory=list)  # Most common hours (e.g., [7, 8, 18, 19])
    typical_days: List[int] = field(default_factory=list)  # Most common days (0=Mon, 6=Sun)
    hour_distribution: Dict[int, int] = field(default_factory=dict)  # Hour -> count
    day_distribution: Dict[int, int] = field(default_factory=dict)  # Day -> count
    avg_time_between_unlocks_minutes: float = 0.0
    std_time_between_unlocks_minutes: float = 0.0
    earliest_typical_hour: int = 6
    latest_typical_hour: int = 23
    weekday_vs_weekend_ratio: float = 1.0  # >1 = more weekday unlocks

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'typical_hours': self.typical_hours,
            'typical_days': self.typical_days,
            'hour_distribution': {str(k): v for k, v in self.hour_distribution.items()},
            'day_distribution': {str(k): v for k, v in self.day_distribution.items()},
            'avg_time_between_unlocks_minutes': self.avg_time_between_unlocks_minutes,
            'std_time_between_unlocks_minutes': self.std_time_between_unlocks_minutes,
            'earliest_typical_hour': self.earliest_typical_hour,
            'latest_typical_hour': self.latest_typical_hour,
            'weekday_vs_weekend_ratio': self.weekday_vs_weekend_ratio
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'UnlockPattern':
        """Create from dictionary."""
        return UnlockPattern(
            typical_hours=data.get('typical_hours', []),
            typical_days=data.get('typical_days', []),
            hour_distribution={int(k): v for k, v in data.get('hour_distribution', {}).items()},
            day_distribution={int(k): v for k, v in data.get('day_distribution', {}).items()},
            avg_time_between_unlocks_minutes=data.get('avg_time_between_unlocks_minutes', 0.0),
            std_time_between_unlocks_minutes=data.get('std_time_between_unlocks_minutes', 0.0),
            earliest_typical_hour=data.get('earliest_typical_hour', 6),
            latest_typical_hour=data.get('latest_typical_hour', 23),
            weekday_vs_weekend_ratio=data.get('weekday_vs_weekend_ratio', 1.0)
        )


@dataclass
class UnlockContext:
    """Current unlock context with behavioral analysis."""
    is_typical_time: bool
    is_typical_day: bool
    is_within_typical_range: bool
    confidence: float
    reasoning: str
    time_since_last_unlock_minutes: Optional[int] = None
    anomaly_score: float = 0.0  # 0 = normal, 1.0 = highly anomalous
    pattern_match_score: float = 0.0  # 0-1 how well this matches learned patterns

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'is_typical_time': self.is_typical_time,
            'is_typical_day': self.is_typical_day,
            'is_within_typical_range': self.is_within_typical_range,
            'confidence': self.confidence,
            'reasoning': self.reasoning,
            'time_since_last_unlock_minutes': self.time_since_last_unlock_minutes,
            'anomaly_score': self.anomaly_score,
            'pattern_match_score': self.pattern_match_score
        }


# =============================================================================
# UNLOCK PATTERN TRACKER
# =============================================================================
class UnlockPatternTracker:
    """
    Tracks and analyzes behavioral unlock patterns over time.

    Features:
    - Time-of-day and day-of-week pattern recognition
    - Anomaly detection for unusual unlock times
    - Confidence scoring based on behavioral consistency
    - Temporal pattern prediction
    """

    def __init__(self):
        self.config = _config
        self._history: deque = deque(maxlen=self.config.max_unlock_history)
        self._pattern: Optional[UnlockPattern] = None
        self._last_unlock_time: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self):
        """Initialize tracker and load historical data."""
        if self._initialized:
            return

        async with self._lock:
            try:
                await self._load_history()
                await self._compute_patterns()
                self._initialized = True
                logger.info(f"🕐 [UNLOCK-TRACKER] Initialized with {len(self._history)} historical unlocks")
            except Exception as e:
                logger.error(f"🕐 [UNLOCK-TRACKER] Initialization failed: {e}")
                self._initialized = True

    async def _load_history(self):
        """Load unlock history from disk."""
        if not self.config.db_path.exists():
            logger.debug("🕐 [UNLOCK-TRACKER] No existing history database")
            return

        try:
            with open(self.config.db_path, 'r') as f:
                data = json.load(f)

            for event_data in data.get('history', []):
                event = UnlockEvent.from_dict(event_data)
                self._history.append(event)

            if 'pattern' in data:
                self._pattern = UnlockPattern.from_dict(data['pattern'])

            if 'last_unlock_time' in data:
                self._last_unlock_time = datetime.fromisoformat(data['last_unlock_time'])

            logger.info(f"🕐 [UNLOCK-TRACKER] Loaded {len(self._history)} unlock events")

        except Exception as e:
            logger.error(f"🕐 [UNLOCK-TRACKER] Failed to load history: {e}")

    async def _save_history(self):
        """Save unlock history to disk."""
        try:
            data = {
                'history': [event.to_dict() for event in list(self._history)[-100:]],  # Save last 100
                'pattern': self._pattern.to_dict() if self._pattern else None,
                'last_unlock_time': self._last_unlock_time.isoformat() if self._last_unlock_time else None,
                'last_updated': datetime.now().isoformat()
            }

            with open(self.config.db_path, 'w') as f:
                json.dump(data, f, indent=2)

            logger.debug(f"🕐 [UNLOCK-TRACKER] Saved unlock history")

        except Exception as e:
            logger.error(f"🕐 [UNLOCK-TRACKER] Failed to save history: {e}")

    async def _compute_patterns(self):
        """Compute unlock patterns from historical data."""
        if len(self._history) < self.config.min_samples_for_pattern:
            logger.debug(f"🕐 [UNLOCK-TRACKER] Insufficient data for pattern computation ({len(self._history)} < {self.config.min_samples_for_pattern})")
            return

        # Filter successful unlocks only
        successful_unlocks = [e for e in self._history if e.success]

        if len(successful_unlocks) < self.config.min_samples_for_pattern:
            return

        # Hour distribution
        hour_dist = defaultdict(int)
        for event in successful_unlocks:
            hour_dist[event.hour] += 1

        # Day distribution
        day_dist = defaultdict(int)
        for event in successful_unlocks:
            day_dist[event.day_of_week] += 1

        # Find typical hours (top 30% by frequency)
        sorted_hours = sorted(hour_dist.items(), key=lambda x: x[1], reverse=True)
        top_count = max(1, int(len(sorted_hours) * 0.3))
        typical_hours = [hour for hour, _ in sorted_hours[:top_count]]

        # Find typical days
        sorted_days = sorted(day_dist.items(), key=lambda x: x[1], reverse=True)
        top_day_count = max(1, int(len(sorted_days) * 0.5))
        typical_days = [day for day, _ in sorted_days[:top_day_count]]

        # Calculate time between unlocks
        time_diffs = []
        sorted_events = sorted(successful_unlocks, key=lambda e: e.timestamp)
        for i in range(1, len(sorted_events)):
            diff = (sorted_events[i].timestamp - sorted_events[i-1].timestamp).total_seconds() / 60
            if 0 < diff < 1440:  # Ignore > 24 hours
                time_diffs.append(diff)

        avg_time_between = np.mean(time_diffs) if time_diffs else 0.0
        std_time_between = np.std(time_diffs) if len(time_diffs) > 1 else 0.0

        # Earliest and latest typical hours
        if typical_hours:
            earliest = min(typical_hours)
            latest = max(typical_hours)
        else:
            earliest, latest = 6, 23

        # Weekday vs weekend ratio
        weekday_count = sum(1 for e in successful_unlocks if e.day_of_week < 5)
        weekend_count = sum(1 for e in successful_unlocks if e.day_of_week >= 5)
        weekday_weekend_ratio = (weekday_count / max(1, weekend_count)) if weekend_count > 0 else 1.0

        self._pattern = UnlockPattern(
            typical_hours=typical_hours,
            typical_days=typical_days,
            hour_distribution=dict(hour_dist),
            day_distribution=dict(day_dist),
            avg_time_between_unlocks_minutes=avg_time_between,
            std_time_between_unlocks_minutes=std_time_between,
            earliest_typical_hour=earliest,
            latest_typical_hour=latest,
            weekday_vs_weekend_ratio=weekday_weekend_ratio
        )

        logger.debug(
            f"🕐 [UNLOCK-TRACKER] Computed pattern: typical_hours={typical_hours}, "
            f"avg_interval={avg_time_between:.1f}min"
        )

    async def record_unlock(
        self,
        success: bool,
        confidence: float,
        location_type: str = "unknown",
        network_ssid_hash: Optional[str] = None
    ):
        """
        Record an unlock attempt.

        Args:
            success: Whether unlock was successful
            confidence: Voice confidence score
            location_type: Location classification
            network_ssid_hash: Network SSID hash (if available)
        """
        if not self._initialized:
            await self.initialize()

        now = datetime.now()

        # Calculate time since last unlock
        time_since_last = None
        if self._last_unlock_time:
            time_since_last = int((now - self._last_unlock_time).total_seconds() / 60)

        event = UnlockEvent(
            timestamp=now,
            hour=now.hour,
            day_of_week=now.weekday(),
            success=success,
            confidence=confidence,
            duration_since_last_unlock_minutes=time_since_last,
            location_type=location_type,
            network_ssid_hash=network_ssid_hash
        )

        self._history.append(event)

        if success:
            self._last_unlock_time = now

        # Recompute patterns every 10 unlocks
        if len(self._history) % 10 == 0:
            await self._compute_patterns()
            await self._save_history()

        logger.debug(f"🕐 [UNLOCK-TRACKER] Recorded {'successful' if success else 'failed'} unlock at {now.hour}:{now.minute:02d}")

    async def get_unlock_context(self, current_time: Optional[datetime] = None) -> UnlockContext:
        """
        Get behavioral context for current unlock attempt.

        Args:
            current_time: Time to analyze (defaults to now)

        Returns:
            UnlockContext with behavioral analysis
        """
        if not self._initialized:
            await self.initialize()

        current_time = current_time or datetime.now()
        current_hour = current_time.hour
        current_day = current_time.weekday()

        # If no pattern yet, return neutral context
        if not self._pattern or len(self._history) < self.config.min_samples_for_pattern:
            return UnlockContext(
                is_typical_time=True,
                is_typical_day=True,
                is_within_typical_range=True,
                confidence=0.5,
                reasoning="Insufficient unlock history - building behavioral profile",
                time_since_last_unlock_minutes=self._get_time_since_last_unlock(current_time)
            )

        # Check if typical hour
        is_typical_time = current_hour in self._pattern.typical_hours

        # Check if typical day
        is_typical_day = current_day in self._pattern.typical_days

        # Check if within typical range
        is_within_range = (
            self._pattern.earliest_typical_hour <= current_hour <= self._pattern.latest_typical_hour
        )

        # Calculate anomaly score (how unusual is this time?)
        anomaly_score = self._calculate_anomaly_score(current_hour, current_day)

        # Calculate pattern match score
        hour_freq = self._pattern.hour_distribution.get(current_hour, 0)
        max_hour_freq = max(self._pattern.hour_distribution.values()) if self._pattern.hour_distribution else 1
        pattern_match = hour_freq / max_hour_freq if max_hour_freq > 0 else 0.0

        # Calculate confidence
        if is_typical_time and is_typical_day:
            confidence = self.config.high_confidence_threshold
            reasoning = f"Typical unlock time: {current_hour}:00 matches historical pattern"
        elif is_typical_time or is_typical_day:
            confidence = self.config.medium_confidence_threshold
            reasoning = f"Partially typical: {'time' if is_typical_time else 'day'} matches pattern"
        elif is_within_range:
            confidence = 0.60
            reasoning = f"Within typical range ({self._pattern.earliest_typical_hour}-{self._pattern.latest_typical_hour})"
        else:
            confidence = 0.40
            reasoning = f"Unusual unlock time: {current_hour}:00 (typically {self._pattern.typical_hours})"

        # Adjust confidence based on anomaly score
        confidence *= (1.0 - anomaly_score * 0.3)  # Reduce up to 30% for anomalies

        return UnlockContext(
            is_typical_time=is_typical_time,
            is_typical_day=is_typical_day,
            is_within_typical_range=is_within_range,
            confidence=confidence,
            reasoning=reasoning,
            time_since_last_unlock_minutes=self._get_time_since_last_unlock(current_time),
            anomaly_score=anomaly_score,
            pattern_match_score=pattern_match
        )

    def _calculate_anomaly_score(self, hour: int, day: int) -> float:
        """Calculate anomaly score (0 = normal, 1.0 = highly anomalous)."""
        if not self._pattern:
            return 0.0

        # Hour-based anomaly
        hour_freq = self._pattern.hour_distribution.get(hour, 0)
        total_unlocks = sum(self._pattern.hour_distribution.values())
        hour_prob = hour_freq / total_unlocks if total_unlocks > 0 else 0.0

        # Day-based anomaly
        day_freq = self._pattern.day_distribution.get(day, 0)
        total_days = sum(self._pattern.day_distribution.values())
        day_prob = day_freq / total_days if total_days > 0 else 0.0

        # Combined anomaly (low probability = high anomaly)
        combined_prob = (hour_prob + day_prob) / 2
        anomaly = 1.0 - combined_prob

        return min(1.0, anomaly)

    def _get_time_since_last_unlock(self, current_time: datetime) -> Optional[int]:
        """Get minutes since last unlock."""
        if not self._last_unlock_time:
            return None

        return int((current_time - self._last_unlock_time).total_seconds() / 60)


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================
_tracker: Optional[UnlockPatternTracker] = None
_tracker_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_unlock_pattern_tracker() -> UnlockPatternTracker:
    """Get or create the global unlock pattern tracker."""
    global _tracker

    if _tracker is None:
        async with _tracker_lock:
            if _tracker is None:
                _tracker = UnlockPatternTracker()
                await _tracker.initialize()

    return _tracker


# Alias for backwards compatibility
get_pattern_tracker = get_unlock_pattern_tracker
