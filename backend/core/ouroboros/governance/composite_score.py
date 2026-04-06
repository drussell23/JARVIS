# backend/core/ouroboros/governance/composite_score.py
"""
Composite Score Function — RSI Convergence Framework
======================================================

Computes a single [0, 1] quality score for each code-change operation.
Lower score = closer to optimal (consistent with Wang's RSI formulation).

Components
----------
- `_sigmoid` / `_clamp`  — math helpers
- `CompositeScore`        — frozen dataclass carrying per-dimension scores
- `CompositeScoreFunction`— computation engine with configurable weights
- `ScoreHistory`          — JSONL-backed persistence for trend analysis

Default weights (overridable via ``OUROBOROS_RSI_SCORE_WEIGHTS`` env var)::

    test_delta=0.40, coverage_delta=0.20, complexity_delta=0.15,
    lint_delta=0.10, blast_radius=0.15
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level defaults
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS: Tuple[float, ...] = (0.40, 0.20, 0.15, 0.10, 0.15)

_PERSISTENCE_DIR = Path(
    os.environ.get(
        "OUROBOROS_RSI_SCORE_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
    )
)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _sigmoid(x: float) -> float:
    """Standard logistic sigmoid: maps (-inf, +inf) -> (0, 1), sigmoid(0) = 0.5."""
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# CompositeScore dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositeScore:
    """Frozen record of per-dimension scores and the weighted composite.

    All per-dimension values are in [0, 1].
    Lower composite = better patch.
    """

    test_delta: float
    coverage_delta: float
    complexity_delta: float
    lint_delta: float
    blast_radius: float
    composite: float
    op_id: str
    timestamp: float


# ---------------------------------------------------------------------------
# CompositeScoreFunction
# ---------------------------------------------------------------------------


class CompositeScoreFunction:
    """Compute composite RSI quality scores for code-change operations.

    Parameters
    ----------
    weights:
        5-tuple of floats, one per sub-score dimension.  They are
        normalized to sum to 1.0 internally.  Must have exactly 5 elements.
    persistence_dir:
        Directory used by the associated :class:`ScoreHistory` instance.
        Defaults to ``OUROBOROS_RSI_SCORE_DIR`` env var or
        ``~/.jarvis/ouroboros/evolution/``.
    """

    def __init__(
        self,
        weights: Optional[Tuple[float, ...]] = None,
        persistence_dir: Optional[Path] = None,
    ) -> None:
        raw = self._resolve_weights(weights)
        if len(raw) != 5:
            raise ValueError(
                f"weights must have exactly 5 elements, got {len(raw)}"
            )
        total = sum(raw)
        self._weights = tuple(w / total for w in raw)
        self._history = ScoreHistory(
            persistence_dir=persistence_dir or _PERSISTENCE_DIR
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(
        self,
        op_id: str,
        *,
        test_pass_rate_before: float,
        test_pass_rate_after: float,
        coverage_before: float,
        coverage_after: float,
        complexity_before: float,
        complexity_after: float,
        lint_violations_before: int,
        lint_violations_after: int,
        blast_radius_total: int,
    ) -> CompositeScore:
        """Compute a :class:`CompositeScore` from before/after quality signals.

        Sub-score conventions (all in [0, 1], lower = better):

        * **test_delta**      = ``1 - (after - before)`` clamped to [0, 1].
          Improvement in pass rate reduces score.
        * **coverage_delta**  = ``1 - (after - before) / 100`` clamped.
          Improvement in coverage (percentage points) reduces score.
        * **complexity_delta** = ``sigmoid(after - before)``.
          Rising complexity => score > 0.5; falling => score < 0.5.
        * **lint_delta**      = ``sigmoid(after - before)``.
          More violations => score > 0.5; fewer => score < 0.5.
        * **blast_radius**    = ``total / 50`` clamped to [0, 1].
          More affected symbols => higher risk.
        """
        w = self._weights

        # 1. test_delta: lower is better when pass rate improves
        td = _clamp(1.0 - (test_pass_rate_after - test_pass_rate_before), 0.0, 1.0)

        # 2. coverage_delta: coverage is in 0-100 percentage points
        cd = _clamp(1.0 - (coverage_after - coverage_before) / 100.0, 0.0, 1.0)

        # 3. complexity_delta: rising complexity => score > 0.5
        xd = _sigmoid(complexity_after - complexity_before)

        # 4. lint_delta: more violations => score > 0.5
        ld = _sigmoid(float(lint_violations_after - lint_violations_before))

        # 5. blast_radius: normalised to 50 affected symbols
        br = _clamp(blast_radius_total / 50.0, 0.0, 1.0)

        composite = (
            w[0] * td
            + w[1] * cd
            + w[2] * xd
            + w[3] * ld
            + w[4] * br
        )

        score = CompositeScore(
            test_delta=td,
            coverage_delta=cd,
            complexity_delta=xd,
            lint_delta=ld,
            blast_radius=br,
            composite=composite,
            op_id=op_id,
            timestamp=time.time(),
        )
        self._history.record(score)
        return score

    @property
    def history(self) -> "ScoreHistory":
        return self._history

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_weights(
        weights: Optional[Tuple[float, ...]]
    ) -> Tuple[float, ...]:
        """Return the effective weights tuple.

        Priority: explicit argument > env var > module default.
        """
        if weights is not None:
            return tuple(weights)
        env_val = os.environ.get("OUROBOROS_RSI_SCORE_WEIGHTS")
        if env_val:
            try:
                parsed = tuple(float(x.strip()) for x in env_val.split(","))
                return parsed
            except ValueError:
                logger.warning(
                    "CompositeScoreFunction: could not parse OUROBOROS_RSI_SCORE_WEIGHTS=%r",
                    env_val,
                )
        return _DEFAULT_WEIGHTS


# ---------------------------------------------------------------------------
# ScoreHistory
# ---------------------------------------------------------------------------


class ScoreHistory:
    """Append-only, JSONL-backed store for :class:`CompositeScore` records.

    Parameters
    ----------
    persistence_dir:
        Directory in which ``composite_scores.jsonl`` is stored.
    """

    _FILENAME = "composite_scores.jsonl"

    def __init__(self, persistence_dir: Path) -> None:
        self._dir = persistence_dir
        self._path = self._dir / self._FILENAME
        self._scores: List[CompositeScore] = []
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, score: CompositeScore) -> None:
        """Append *score* to in-memory list and persist to disk."""
        self._scores.append(score)
        self._append(score)

    def get_recent(self, n: int) -> List[CompositeScore]:
        """Return the last *n* scores in chronological (oldest-first) order."""
        return list(self._scores[-n:]) if n > 0 else []

    def get_composite_values(self) -> List[float]:
        """Return all composite values as a plain list of floats."""
        return [s.composite for s in self._scores]

    # ------------------------------------------------------------------
    # Internal persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load existing JSONL records from disk; silently skip errors."""
        try:
            if not self._path.exists():
                return
            for line in self._path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self._scores.append(
                        CompositeScore(
                            test_delta=float(data["test_delta"]),
                            coverage_delta=float(data["coverage_delta"]),
                            complexity_delta=float(data["complexity_delta"]),
                            lint_delta=float(data["lint_delta"]),
                            blast_radius=float(data["blast_radius"]),
                            composite=float(data["composite"]),
                            op_id=str(data["op_id"]),
                            timestamp=float(data["timestamp"]),
                        )
                    )
                except Exception as exc:
                    logger.debug(
                        "ScoreHistory: skipping corrupt JSONL line: %s — %s",
                        line[:80],
                        exc,
                    )
        except Exception as exc:
            logger.warning("ScoreHistory: _load failed: %s — starting empty", exc)

    def _append(self, score: CompositeScore) -> None:
        """Append a single score record to the JSONL file; silently fail on errors."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(score)) + "\n")
        except Exception as exc:
            logger.warning("ScoreHistory: _append failed: %s", exc)
