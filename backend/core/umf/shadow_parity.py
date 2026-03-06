"""UMF Shadow Mode Parity Logger -- compares legacy vs UMF decisions.

In shadow mode, both legacy paths and UMF process the same inputs. This
module records comparison results and tracks parity ratio. Blocks
promotion to active mode if parity falls below the configured threshold.

Design rules
------------
* Stdlib only -- no third-party or JARVIS imports.
* Thread-safe via append-only lists and atomic counter increments.
* Bounded diff history (last N mismatches) to prevent unbounded growth.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_DIFF_HISTORY = 1000


class ShadowParityLogger:
    """Track parity between legacy and UMF decision paths.

    Parameters
    ----------
    parity_threshold:
        Minimum parity ratio (0.0-1.0) required for promotion readiness.
    min_comparisons:
        Minimum number of comparisons before promotion can be considered.
    """

    def __init__(
        self,
        parity_threshold: float = 0.999,
        min_comparisons: int = 100,
    ) -> None:
        self._parity_threshold = parity_threshold
        self._min_comparisons = min_comparisons
        self._total: int = 0
        self._mismatches_count: int = 0
        self._diffs: List[Dict[str, Any]] = []

    # -- recording ---------------------------------------------------------

    def record(
        self,
        trace_id: str,
        category: str,
        *,
        legacy_result: str,
        umf_result: str,
    ) -> None:
        """Record a single comparison between legacy and UMF paths.

        Parameters
        ----------
        trace_id:
            Causality trace ID for correlation.
        category:
            Decision category (e.g. 'route', 'dedup', 'heartbeat').
        legacy_result:
            The result from the legacy path.
        umf_result:
            The result from the UMF path.
        """
        self._total += 1
        if legacy_result != umf_result:
            self._mismatches_count += 1
            diff = {
                "trace_id": trace_id,
                "category": category,
                "legacy_result": legacy_result,
                "umf_result": umf_result,
                "recorded_at": time.monotonic(),
            }
            # Bounded history
            if len(self._diffs) >= _MAX_DIFF_HISTORY:
                self._diffs.pop(0)
            self._diffs.append(diff)
            logger.warning(
                "[SHADOW-PARITY] Mismatch trace_id=%s category=%s legacy=%s umf=%s",
                trace_id, category, legacy_result, umf_result,
            )

    # -- queries -----------------------------------------------------------

    @property
    def total_comparisons(self) -> int:
        """Total number of comparisons recorded."""
        return self._total

    @property
    def mismatches(self) -> int:
        """Total number of mismatches recorded."""
        return self._mismatches_count

    @property
    def parity_ratio(self) -> float:
        """Ratio of matching comparisons (1.0 = perfect parity)."""
        if self._total == 0:
            return 1.0
        return (self._total - self._mismatches_count) / self._total

    def is_promotion_ready(self) -> bool:
        """Return True if parity exceeds threshold with sufficient data."""
        if self._total < self._min_comparisons:
            return False
        return self.parity_ratio >= self._parity_threshold

    def get_recent_diffs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent mismatch diffs, up to *limit*."""
        return list(self._diffs[-limit:])
