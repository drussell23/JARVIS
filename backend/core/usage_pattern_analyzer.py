"""Usage Pattern Analyzer — learns VM usage patterns from cost_tracking.db.

Reads historical vm_sessions data to build:
- Hourly usage histograms (when VMs are typically needed)
- Average session durations
- Daily session counts
- False alarm rates (VMs created but never used)

Feeds into intelligent_gcp_optimizer.py time-of-day factor and
gcp_vm_manager.py golden image ROI calculations.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Module-level singleton
_instance: Optional["UsagePatternAnalyzer"] = None


class UsagePatternAnalyzer:
    """Learns VM usage patterns from historical cost_tracking.db data.

    Provides:
    - get_hourly_histogram(): 24-bucket usage probability by hour (UTC)
    - get_avg_daily_sessions(): mean sessions per day over lookback window
    - get_avg_session_duration_hours(): mean VM runtime
    - get_avg_daily_vm_hours(): total VM hours per day
    - get_false_alarm_rate(): fraction of VMs with zero component usage
    - get_time_of_day_factor(hour): learned replacement for hardcoded heuristic
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path.home() / ".jarvis" / "learning" / "cost_tracking.db"
        self._db_path = db_path
        self._lookback_days = int(os.getenv("JARVIS_USAGE_LOOKBACK_DAYS", "30"))

        # Cached results (refreshed periodically)
        self._cache: Dict[str, Any] = {}
        self._cache_ttl_s = float(os.getenv("JARVIS_USAGE_CACHE_TTL_S", "600"))
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()

    async def _ensure_fresh(self) -> None:
        """Refresh cache if stale."""
        if time.monotonic() - self._last_refresh < self._cache_ttl_s:
            return
        async with self._lock:
            # Double-check after acquiring lock
            if time.monotonic() - self._last_refresh < self._cache_ttl_s:
                return
            await self._refresh_cache()

    async def _refresh_cache(self) -> None:
        """Query cost_tracking.db and rebuild all cached metrics."""
        try:
            import aiosqlite
        except ImportError:
            logger.debug("[UsagePattern] aiosqlite not available")
            return

        if not self._db_path.exists():
            logger.debug("[UsagePattern] DB not found at %s", self._db_path)
            return

        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._lookback_days)).isoformat()

        try:
            async with aiosqlite.connect(str(self._db_path), timeout=10.0) as db:
                # 1. Fetch all sessions in lookback window
                cursor = await db.execute(
                    """
                    SELECT created_at, deleted_at, runtime_hours, components, metadata
                    FROM vm_sessions
                    WHERE created_at >= ?
                    ORDER BY created_at
                    """,
                    (cutoff,),
                )
                rows = await cursor.fetchall()

            if not rows:
                self._cache = self._empty_cache()
                self._last_refresh = time.monotonic()
                return

            # 2. Build hourly histogram
            hourly_counts = [0] * 24
            total_sessions = len(rows)
            total_runtime_hours = 0.0
            false_alarms = 0
            session_dates: set = set()

            for row in rows:
                created_at_str, deleted_at_str, runtime_hours, components, metadata = row

                # Parse hour from created_at
                try:
                    dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    hourly_counts[dt.hour] += 1
                    session_dates.add(dt.date())
                except (ValueError, AttributeError):
                    pass

                # Accumulate runtime
                if runtime_hours and runtime_hours > 0:
                    total_runtime_hours += runtime_hours

                # Detect false alarms (VM created but no components used)
                if components in (None, "", "[]", "null"):
                    false_alarms += 1

            # 3. Compute metrics
            num_days = max(len(session_dates), 1)
            avg_daily_sessions = total_sessions / num_days
            avg_session_duration = total_runtime_hours / max(total_sessions, 1)
            avg_daily_vm_hours = total_runtime_hours / num_days
            false_alarm_rate = false_alarms / max(total_sessions, 1)

            # Normalize histogram to probabilities
            max_count = max(hourly_counts) if any(hourly_counts) else 1
            hourly_probs = [c / max_count for c in hourly_counts]

            self._cache = {
                "hourly_histogram": hourly_probs,
                "hourly_counts": hourly_counts,
                "avg_daily_sessions": avg_daily_sessions,
                "avg_session_duration_hours": avg_session_duration,
                "avg_daily_vm_hours": avg_daily_vm_hours,
                "false_alarm_rate": false_alarm_rate,
                "total_sessions": total_sessions,
                "num_days": num_days,
                "lookback_days": self._lookback_days,
            }
            self._last_refresh = time.monotonic()

            logger.info(
                "[UsagePattern] Refreshed: %d sessions over %d days, "
                "avg %.1f sessions/day, avg %.2fh duration, "
                "false alarm rate %.1f%%",
                total_sessions, num_days,
                avg_daily_sessions, avg_session_duration,
                false_alarm_rate * 100,
            )

        except Exception as e:
            logger.warning("[UsagePattern] Cache refresh failed: %s", e)

    def _empty_cache(self) -> Dict[str, Any]:
        """Return empty/default cache when no data is available."""
        return {
            "hourly_histogram": [0.5] * 24,  # Uniform prior
            "hourly_counts": [0] * 24,
            "avg_daily_sessions": 2.0,  # Conservative default
            "avg_session_duration_hours": 1.0,
            "avg_daily_vm_hours": 2.0,
            "false_alarm_rate": 0.0,
            "total_sessions": 0,
            "num_days": 0,
            "lookback_days": self._lookback_days,
        }

    async def get_hourly_histogram(self) -> List[float]:
        """Return 24-bucket normalized usage probability by hour (UTC)."""
        await self._ensure_fresh()
        return self._cache.get("hourly_histogram", [0.5] * 24)

    async def get_avg_daily_sessions(self) -> float:
        """Return average number of VM sessions per day."""
        await self._ensure_fresh()
        return self._cache.get("avg_daily_sessions", 2.0)

    async def get_avg_session_duration_hours(self) -> float:
        """Return average VM session duration in hours."""
        await self._ensure_fresh()
        return self._cache.get("avg_session_duration_hours", 1.0)

    async def get_avg_daily_vm_hours(self) -> float:
        """Return average total VM hours per day."""
        await self._ensure_fresh()
        return self._cache.get("avg_daily_vm_hours", 2.0)

    async def get_false_alarm_rate(self) -> float:
        """Return fraction of VMs created but never used (0.0-1.0)."""
        await self._ensure_fresh()
        return self._cache.get("false_alarm_rate", 0.0)

    async def get_time_of_day_factor(self, hour: Optional[int] = None) -> float:
        """Return learned time-of-day scaling factor (0.0-1.0).

        Replaces hardcoded heuristic in intelligent_gcp_optimizer.py.
        Higher values = more likely to need a VM at this hour.

        Args:
            hour: UTC hour (0-23). Defaults to current hour.
        """
        if hour is None:
            hour = datetime.now(timezone.utc).hour

        histogram = await self.get_hourly_histogram()
        if 0 <= hour < 24:
            return histogram[hour]
        return 0.5  # Fallback for invalid hour

    async def get_dynamic_idle_timeout_minutes(self) -> float:
        """Calculate a dynamic idle timeout based on usage patterns.

        If sessions are frequent and short, use shorter timeout.
        If sessions are infrequent and long, use longer timeout.
        Falls back to env var default.
        """
        default_timeout = float(os.getenv("GCP_IDLE_TIMEOUT_MINUTES", "5"))

        await self._ensure_fresh()
        sessions_per_day = self._cache.get("avg_daily_sessions", 0)
        avg_duration = self._cache.get("avg_session_duration_hours", 0)

        if sessions_per_day < 1 or avg_duration <= 0:
            return default_timeout

        # Heuristic: timeout = avg gap between sessions, clamped 3-15 min
        avg_gap_hours = 24.0 / max(sessions_per_day, 1) - avg_duration
        avg_gap_minutes = max(avg_gap_hours * 60, 0)

        # Use 1/4 of average gap, clamped to [3, 15] minutes
        dynamic_timeout = max(3.0, min(15.0, avg_gap_minutes / 4.0))

        return dynamic_timeout

    async def get_stats(self) -> Dict[str, Any]:
        """Return all cached stats for telemetry/MCP exposure."""
        await self._ensure_fresh()
        return dict(self._cache) if self._cache else self._empty_cache()


def get_usage_pattern_analyzer() -> UsagePatternAnalyzer:
    """Get or create the usage pattern analyzer singleton."""
    global _instance
    if _instance is None:
        _instance = UsagePatternAnalyzer()
    return _instance
