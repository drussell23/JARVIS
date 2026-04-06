"""TransitionProbabilityTracker — tracks which self-evolution techniques succeed.

Based on Wang's Markov transition matrix concept: maintains outcome statistics
at three granularities (technique:domain:complexity, technique:domain,
technique-only) and uses Laplace smoothing to avoid zero/one probabilities.

The fallback hierarchy handles sparse data gracefully:
  full key (>=5 obs) -> partial key (>=5 obs) -> technique only (>=5 obs) -> 0.5
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Minimum observations required before a counter level is trusted
_MIN_OBS = 5

# Global prior when no level has enough data
_GLOBAL_PRIOR = 0.5


@dataclass
class TechniqueOutcome:
    """Result of applying a single self-evolution technique to an operation."""

    technique: str
    domain: str
    complexity: str
    success: bool
    composite_score: float
    op_id: str
    timestamp: float = field(default_factory=time.time)


class TransitionProbabilityTracker:
    """Tracks per-technique success rates with three-level Laplace smoothing.

    Counter structure (each value is ``{"success": int, "total": int}``):
      _full    — keyed by  ``technique:domain:complexity``
      _partial — keyed by  ``technique:domain``
      _technique — keyed by  ``technique``

    Usage::
        tracker = TransitionProbabilityTracker()
        tracker.record(outcome)
        p = tracker.get_probability("module_mutation", "code", "medium")
        ranked = tracker.rank_techniques("code", "medium")
    """

    _FILENAME = "transition_probabilities.json"

    def __init__(
        self,
        persistence_dir: Optional[Path] = None,
    ) -> None:
        default_dir = Path(
            os.environ.get(
                "JARVIS_SELF_EVOLUTION_DIR",
                str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
            )
        )
        self._dir: Path = persistence_dir if persistence_dir is not None else default_dir
        self._path: Path = self._dir / self._FILENAME

        # Three-level counters: key -> {"success": int, "total": int}
        self._full: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        self._partial: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        self._technique: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, outcome: TechniqueOutcome) -> None:
        """Record one outcome and persist immediately."""
        full_key = f"{outcome.technique}:{outcome.domain}:{outcome.complexity}"
        partial_key = f"{outcome.technique}:{outcome.domain}"
        technique_key = outcome.technique

        for counter, key in (
            (self._full, full_key),
            (self._partial, partial_key),
            (self._technique, technique_key),
        ):
            counter[key]["total"] += 1
            if outcome.success:
                counter[key]["success"] += 1

        self._persist()

    def get_probability(self, technique: str, domain: str, complexity: str) -> float:
        """Return Laplace-smoothed P(success) with fallback hierarchy.

        Fallback order (each level requires >= 5 observations):
          1. full key  (technique:domain:complexity)
          2. partial key (technique:domain)
          3. technique key (technique)
          4. global prior 0.5
        """
        full_key = f"{technique}:{domain}:{complexity}"
        partial_key = f"{technique}:{domain}"
        technique_key = technique

        for counter, key in (
            (self._full, full_key),
            (self._partial, partial_key),
            (self._technique, technique_key),
        ):
            entry = counter.get(key)
            if entry is not None and entry["total"] >= _MIN_OBS:
                return self._laplace(entry["success"], entry["total"])

        return _GLOBAL_PRIOR

    def rank_techniques(self, domain: str, complexity: str) -> List[Tuple[str, float]]:
        """Return all known techniques sorted by P(success) descending.

        Only techniques that have at least one observation at *any* level for
        the given (domain, complexity) context are included.
        """
        # Collect all techniques that have any record for this domain+complexity.
        # We look at the partial key to find candidates, then score them.
        candidates: List[str] = []

        # Check full keys that match the domain:complexity suffix
        suffix = f":{domain}:{complexity}"
        for key in self._full:
            if key.endswith(suffix):
                tech = key[: -len(suffix)]
                if tech not in candidates:
                    candidates.append(tech)

        if not candidates:
            return []

        scored = [
            (tech, self.get_probability(tech, domain, complexity))
            for tech in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Serialize all counters to JSON (silent fail on I/O errors)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data = {
                "full": dict(self._full),
                "partial": dict(self._partial),
                "technique": dict(self._technique),
            }
            self._path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("[TransitionProbabilityTracker] Persist failed: %s", exc)

    def _load(self) -> None:
        """Deserialize counters from JSON (silent fail on missing/corrupt file)."""
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text())
            for key, entry in data.get("full", {}).items():
                self._full[key] = entry
            for key, entry in data.get("partial", {}).items():
                self._partial[key] = entry
            for key, entry in data.get("technique", {}).items():
                self._technique[key] = entry
        except Exception as exc:
            logger.warning("[TransitionProbabilityTracker] Load failed: %s — starting fresh", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _laplace(successes: int, total: int) -> float:
        """Laplace-smoothed probability: (1 + successes) / (2 + total)."""
        return (1 + successes) / (2 + total)
