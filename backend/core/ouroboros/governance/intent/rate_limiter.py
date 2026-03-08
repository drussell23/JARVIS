"""
Rate Limiter for Intent Engine
===============================

Governs the throughput of autonomous operations in JARVIS's Intent Engine
(Layer 1 of self-development).  Before any detected signal is submitted to
the governed pipeline, the :class:`RateLimiter` enforces:

1. **Per-file cooldown** -- prevents the same file from being modified
   repeatedly within a short window (default 10 min).
2. **Per-signal cooldown** -- prevents the same signal key from triggering
   back-to-back operations (default 5 min).
3. **Hourly cap** -- limits total operations per hour (default 5).
4. **Daily cap** -- limits total operations per 24-hour window (default 20).

All internal timestamps use ``time.monotonic()`` to remain immune to
wall-clock adjustments.

Configuration is read from environment variables via
:meth:`RateLimiterConfig.from_env`, making it easy to tune in different
deployment environments without code changes.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SECONDS_PER_HOUR = 3600.0
_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class RateLimiterConfig:
    """Immutable configuration for :class:`RateLimiter`.

    Parameters
    ----------
    max_ops_per_hour:
        Maximum number of operations allowed within a rolling 1-hour window.
    max_ops_per_day:
        Maximum number of operations allowed within a rolling 24-hour window.
    per_file_cooldown_s:
        Minimum seconds between operations targeting the same file.
    per_signal_cooldown_s:
        Minimum seconds between operations triggered by the same signal key.
    """

    max_ops_per_hour: int = 5
    max_ops_per_day: int = 20
    per_file_cooldown_s: float = 600.0
    per_signal_cooldown_s: float = 300.0

    @classmethod
    def from_env(cls) -> RateLimiterConfig:
        """Build a config from environment variables, falling back to defaults.

        Environment variables
        ---------------------
        JARVIS_INTENT_MAX_OPS_HOUR : int
        JARVIS_INTENT_MAX_OPS_DAY  : int
        JARVIS_INTENT_FILE_COOLDOWN_S : float
        JARVIS_INTENT_SIGNAL_COOLDOWN_S : float
        """
        defaults = cls()
        return cls(
            max_ops_per_hour=int(
                os.environ.get("JARVIS_INTENT_MAX_OPS_HOUR", defaults.max_ops_per_hour)
            ),
            max_ops_per_day=int(
                os.environ.get("JARVIS_INTENT_MAX_OPS_DAY", defaults.max_ops_per_day)
            ),
            per_file_cooldown_s=float(
                os.environ.get(
                    "JARVIS_INTENT_FILE_COOLDOWN_S", defaults.per_file_cooldown_s
                )
            ),
            per_signal_cooldown_s=float(
                os.environ.get(
                    "JARVIS_INTENT_SIGNAL_COOLDOWN_S", defaults.per_signal_cooldown_s
                )
            ),
        )


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Enforces throughput limits for autonomous intent operations.

    Checks are performed in the following order (first failure wins):

    1. Per-file cooldown
    2. Per-signal cooldown
    3. Hourly operation cap
    4. Daily operation cap

    All timestamps are based on ``time.monotonic()`` for clock-skew safety.

    Parameters
    ----------
    config:
        Optional configuration.  Defaults to :class:`RateLimiterConfig` with
        default values.
    """

    def __init__(self, config: Optional[RateLimiterConfig] = None) -> None:
        self._config = config or RateLimiterConfig()
        self._file_timestamps: Dict[str, float] = {}
        self._signal_timestamps: Dict[str, float] = {}
        self._op_timestamps: List[float] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self, file_path: str, signal_key: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Check whether an operation is allowed under current rate limits.

        Parameters
        ----------
        file_path:
            The target file for the proposed operation.
        signal_key:
            Optional signal dedup key.  When provided, the per-signal
            cooldown is enforced.

        Returns
        -------
        (allowed, reason_code):
            ``(True, "")`` if the operation is permitted, or
            ``(False, reason_code)`` with one of:

            - ``"rate_limit:file_cooldown"``
            - ``"rate_limit:signal_cooldown"``
            - ``"rate_limit:hourly_cap"``
            - ``"rate_limit:daily_cap"``
        """
        now = time.monotonic()

        # 1. Per-file cooldown
        last_file_ts = self._file_timestamps.get(file_path)
        if last_file_ts is not None:
            if (now - last_file_ts) < self._config.per_file_cooldown_s:
                return False, "rate_limit:file_cooldown"

        # 2. Per-signal cooldown
        if signal_key is not None:
            last_signal_ts = self._signal_timestamps.get(signal_key)
            if last_signal_ts is not None:
                if (now - last_signal_ts) < self._config.per_signal_cooldown_s:
                    return False, "rate_limit:signal_cooldown"

        # 3. Hourly cap
        hourly_cutoff = now - _SECONDS_PER_HOUR
        hourly_count = sum(1 for ts in self._op_timestamps if ts > hourly_cutoff)
        if hourly_count >= self._config.max_ops_per_hour:
            return False, "rate_limit:hourly_cap"

        # 4. Daily cap
        daily_cutoff = now - _SECONDS_PER_DAY
        daily_count = sum(1 for ts in self._op_timestamps if ts > daily_cutoff)
        if daily_count >= self._config.max_ops_per_day:
            return False, "rate_limit:daily_cap"

        return True, ""

    def record(
        self, file_path: str, signal_key: Optional[str] = None
    ) -> None:
        """Record that an operation was executed.

        Updates file and signal timestamps and appends to the global
        operation timestamp list.  Prunes entries older than 24 hours.

        Parameters
        ----------
        file_path:
            The target file of the completed operation.
        signal_key:
            Optional signal dedup key associated with the operation.
        """
        now = time.monotonic()

        # Update per-file timestamp
        self._file_timestamps[file_path] = now

        # Update per-signal timestamp
        if signal_key is not None:
            self._signal_timestamps[signal_key] = now

        # Append to global op timeline
        self._op_timestamps.append(now)

        # Prune timestamps older than 24h
        daily_cutoff = now - _SECONDS_PER_DAY
        self._op_timestamps = [
            ts for ts in self._op_timestamps if ts > daily_cutoff
        ]
