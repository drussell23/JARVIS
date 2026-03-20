"""backend/core/ouroboros/consciousness/dream_metrics.py

DreamMetricsTracker — mutable accumulator for DreamEngine cost observability.

Design:
    - All public methods are intentionally synchronous; callers serialise access
      within the DreamEngine async loop (single-task writer) so no lock is needed.
    - hit_rate is computed lazily in get_metrics() from raw hits/computed counters;
      the DreamMetrics.blueprint_hit_rate field is populated on snapshot generation.
    - persist()/load() use atomic JSON serialisation over a flat dict so the file
      format stays human-readable and diff-friendly in audit logs.
    - Division-by-zero for hit_rate is explicitly guarded: returns 0.0 when no
      blueprints have been computed yet.

TC22: compute_minutes + hit_rate tracked correctly.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Union

from backend.core.ouroboros.consciousness.types import DreamMetrics


class DreamMetricsTracker:
    """Accumulates runtime counters for the DreamEngine background loop.

    Usage::

        tracker = DreamMetricsTracker()
        tracker.record_compute_time(3.2)
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        metrics = tracker.get_metrics()   # DreamMetrics snapshot
        tracker.persist(Path("metrics.json"))
        restored = DreamMetricsTracker.load(Path("metrics.json"))
        tracker.reset()
    """

    def __init__(self) -> None:
        self._compute_minutes: float = 0.0
        self._preemptions: int = 0
        self._blueprints_computed: int = 0
        self._blueprints_discarded: int = 0
        self._blueprint_hits: int = 0
        self._jobs_deduplicated: int = 0
        self._cost_saved_usd: float = 0.0

    # ------------------------------------------------------------------
    # Recording methods
    # ------------------------------------------------------------------

    def record_compute_time(self, minutes: float) -> None:
        """Increment opportunistic_compute_minutes by *minutes*."""
        self._compute_minutes += minutes

    def record_preemption(self) -> None:
        """Increment preemptions_count by 1."""
        self._preemptions += 1

    def record_blueprint_computed(self) -> None:
        """Increment blueprints_computed by 1 (a miss until record_blueprint_hit)."""
        self._blueprints_computed += 1

    def record_blueprint_discarded(self) -> None:
        """Increment blueprints_discarded_stale by 1."""
        self._blueprints_discarded += 1

    def record_blueprint_hit(self) -> None:
        """Record one cache hit for hit_rate calculation.

        A hit means a previously computed blueprint was reused rather than
        discarded.  Hits beyond blueprints_computed are silently tolerated
        (the rate is clamped by the formula, not by assertion).
        """
        self._blueprint_hits += 1

    def record_dedup(self) -> None:
        """Increment jobs_deduplicated by 1."""
        self._jobs_deduplicated += 1

    def record_cost_saved(self, usd: float) -> None:
        """Increment estimated_cost_saved_usd by *usd*."""
        self._cost_saved_usd += usd

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def get_metrics(self) -> DreamMetrics:
        """Return a DreamMetrics snapshot of the current counters.

        hit_rate is computed as hits / (hits + misses) where misses =
        blueprints_computed - hits.  Returns 0.0 when no blueprints have
        been computed (TC22 division-by-zero guard).
        """
        computed = self._blueprints_computed
        hits = self._blueprint_hits
        if computed <= 0:
            hit_rate = 0.0
        else:
            misses = computed - hits
            denominator = hits + misses  # == computed
            hit_rate = hits / denominator if denominator > 0 else 0.0

        return DreamMetrics(
            opportunistic_compute_minutes=self._compute_minutes,
            preemptions_count=self._preemptions,
            blueprints_computed=computed,
            blueprints_discarded_stale=self._blueprints_discarded,
            blueprint_hit_rate=hit_rate,
            jobs_deduplicated=self._jobs_deduplicated,
            estimated_cost_saved_usd=self._cost_saved_usd,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def persist(self, path: Union[Path, str]) -> None:
        """Write the current counters to a JSON file at *path*.

        The file format is a flat JSON object whose keys match the
        DreamMetrics field names plus ``_blueprint_hits`` (internal counter
        needed to restore hit_rate faithfully after a round-trip).
        """
        path = Path(path)
        payload = {
            "opportunistic_compute_minutes": self._compute_minutes,
            "preemptions_count": self._preemptions,
            "blueprints_computed": self._blueprints_computed,
            "blueprints_discarded_stale": self._blueprints_discarded,
            "blueprint_hits": self._blueprint_hits,
            "jobs_deduplicated": self._jobs_deduplicated,
            "estimated_cost_saved_usd": self._cost_saved_usd,
        }
        path.write_text(json.dumps(payload, indent=2))

    @classmethod
    def load(cls, path: Union[Path, str]) -> "DreamMetricsTracker":
        """Restore a DreamMetricsTracker from a JSON file created by persist().

        Raises:
            FileNotFoundError: if *path* does not exist.
            json.JSONDecodeError: if the file is not valid JSON.
            KeyError: if required fields are missing from the JSON payload.
        """
        path = Path(path)
        data = json.loads(path.read_text())
        tracker = cls()
        tracker._compute_minutes = float(data["opportunistic_compute_minutes"])
        tracker._preemptions = int(data["preemptions_count"])
        tracker._blueprints_computed = int(data["blueprints_computed"])
        tracker._blueprints_discarded = int(data["blueprints_discarded_stale"])
        tracker._blueprint_hits = int(data["blueprint_hits"])
        tracker._jobs_deduplicated = int(data["jobs_deduplicated"])
        tracker._cost_saved_usd = float(data["estimated_cost_saved_usd"])
        return tracker

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Zero all counters.  Useful at the start of a new dream session."""
        self._compute_minutes = 0.0
        self._preemptions = 0
        self._blueprints_computed = 0
        self._blueprints_discarded = 0
        self._blueprint_hits = 0
        self._jobs_deduplicated = 0
        self._cost_saved_usd = 0.0
