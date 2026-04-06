# RSI Convergence Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 6 mathematically grounded RSI convergence improvements for Ouroboros, giving the self-development pipeline a unified score function, convergence monitoring, adaptive graduation, oracle pre-scoring, transition probability tracking, and vindication reflection.

**Architecture:** All 6 components are purely deterministic (no LLM calls). They measure and govern the existing agentic pipeline. The CompositeScoreFunction is the foundation — everything else depends on it. See `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md` for the full theoretical specification.

**Tech Stack:** Python 3.11+, dataclasses, math/statistics stdlib, asyncio, pytest, existing Ouroboros persistence patterns (JSON in `~/.jarvis/ouroboros/`)

---

## File Structure

### New Files

| File | Responsibility |
|---|---|
| `backend/core/ouroboros/governance/composite_score.py` | Composite score function: 5 sub-scores, weighted sum, persistence |
| `backend/core/ouroboros/governance/convergence_tracker.py` | Convergence monitoring: trend detection, logarithmic fit, plateau/oscillation detection |
| `backend/core/ouroboros/governance/oracle_prescorer.py` | Fast approximate quality gate using TheOracle graph signals |
| `backend/core/ouroboros/governance/transition_tracker.py` | Empirical P(success \| technique, domain, complexity) tracking |
| `backend/core/ouroboros/governance/vindication_reflector.py` | Forward-looking coupling/blast-radius/entropy trajectory analysis |
| `tests/test_ouroboros_governance/test_composite_score.py` | Tests for composite score |
| `tests/test_ouroboros_governance/test_convergence_tracker.py` | Tests for convergence tracker |
| `tests/test_ouroboros_governance/test_adaptive_graduation.py` | Tests for adaptive graduation threshold |
| `tests/test_ouroboros_governance/test_oracle_prescorer.py` | Tests for oracle pre-scorer |
| `tests/test_ouroboros_governance/test_transition_tracker.py` | Tests for transition probability tracker |
| `tests/test_ouroboros_governance/test_vindication_reflector.py` | Tests for vindication reflector |

### Modified Files

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/graduation_orchestrator.py` | Replace static threshold with adaptive Bayesian threshold in `EphemeralUsageTracker` |
| `backend/core/ouroboros/governance/self_evolution.py` | Export `CodeMetricsAnalyzer` and `CodeMetricsReport` for composite score consumption |
| `backend/core/ouroboros/governance/orchestrator.py` | Wire in composite score (VERIFY phase), convergence tracker (COMPLETE), oracle pre-scorer (GENERATE), vindication reflector (GATE), transition tracker (outcome publishing) |
| `backend/core/ouroboros/governance/ledger.py` | Add new `OperationState` variants: `SCORE_COMPUTED`, `CONVERGENCE_CHECKED`, `PRE_SCORED`, `VINDICATION_CHECKED` |

---

## Task 1: Composite Score Function — Data Structures & Core Logic

**Files:**
- Create: `backend/core/ouroboros/governance/composite_score.py`
- Test: `tests/test_ouroboros_governance/test_composite_score.py`

- [ ] **Step 1: Write the failing test for CompositeScore dataclass and sigmoid helper**

```python
"""Tests for CompositeScoreFunction — Wang-consistent RSI quality metric."""
from __future__ import annotations

import math
import pytest
import time


def test_sigmoid_at_zero():
    from backend.core.ouroboros.governance.composite_score import _sigmoid
    assert _sigmoid(0.0) == pytest.approx(0.5, abs=1e-9)


def test_sigmoid_positive():
    from backend.core.ouroboros.governance.composite_score import _sigmoid
    result = _sigmoid(5.0)
    assert 0.99 < result < 1.0


def test_sigmoid_negative():
    from backend.core.ouroboros.governance.composite_score import _sigmoid
    result = _sigmoid(-5.0)
    assert 0.0 < result < 0.01


def test_sigmoid_monotonic():
    from backend.core.ouroboros.governance.composite_score import _sigmoid
    values = [_sigmoid(x) for x in range(-10, 11)]
    for i in range(len(values) - 1):
        assert values[i] <= values[i + 1]


def test_composite_score_creation():
    from backend.core.ouroboros.governance.composite_score import CompositeScore
    score = CompositeScore(
        test_delta=0.1,
        coverage_delta=0.2,
        complexity_delta=0.3,
        lint_delta=0.4,
        blast_radius=0.5,
        composite=0.25,
        op_id="op-test-001",
        timestamp=time.time(),
    )
    assert score.composite == 0.25
    assert score.op_id == "op-test-001"


def test_composite_score_is_frozen():
    from backend.core.ouroboros.governance.composite_score import CompositeScore
    score = CompositeScore(
        test_delta=0.0, coverage_delta=0.0, complexity_delta=0.0,
        lint_delta=0.0, blast_radius=0.0, composite=0.0,
        op_id="op-test-002", timestamp=time.time(),
    )
    with pytest.raises(AttributeError):
        score.composite = 0.5  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_composite_score.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.composite_score'`

- [ ] **Step 3: Write minimal implementation for dataclass and sigmoid**

```python
"""
Composite Score Function — Wang-consistent RSI quality metric for Ouroboros.

Maps Wenyi Wang's RSI score function concept onto the Ouroboros pipeline.
Combines 5 deterministic quality signals into a single scalar score where
lower = better (closer to optimal), consistent with Wang's convention.

Boundary Principle: 100% deterministic. No LLM calls. No heuristics.
The score measures the agentic pipeline's output; it is not itself agentic.

See: docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md Section 5
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_SELF_EVOLUTION_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
    )
)

# Default weights (sum to 1.0) — overridable via OUROBOROS_RSI_SCORE_WEIGHTS
_DEFAULT_WEIGHTS = (0.40, 0.20, 0.15, 0.10, 0.15)


def _sigmoid(x: float) -> float:
    """Sigmoid normalization: maps (-inf, inf) to (0, 1). sigmoid(0) = 0.5."""
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp value to [lo, hi]."""
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class CompositeScore:
    """Wang-consistent composite quality score for a patch/operation.

    Lower composite = better (closer to optimal). Range [0, 1].
    """
    test_delta: float
    coverage_delta: float
    complexity_delta: float
    lint_delta: float
    blast_radius: float
    composite: float
    op_id: str
    timestamp: float
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_composite_score.py -v --timeout=15 -x`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/composite_score.py tests/test_ouroboros_governance/test_composite_score.py
git commit -m "feat(rsi): add CompositeScore dataclass and sigmoid helper"
```

---

## Task 2: Composite Score Function — Computation Engine

**Files:**
- Modify: `backend/core/ouroboros/governance/composite_score.py`
- Test: `tests/test_ouroboros_governance/test_composite_score.py`

- [ ] **Step 1: Write the failing test for score computation**

Append to `tests/test_ouroboros_governance/test_composite_score.py`:

```python
def test_compute_score_perfect_patch():
    """A patch that improves everything should score near 0."""
    from backend.core.ouroboros.governance.composite_score import CompositeScoreFunction
    csf = CompositeScoreFunction()
    score = csf.compute(
        op_id="op-perfect-001",
        test_pass_rate_before=0.80, test_pass_rate_after=1.0,
        coverage_before=0.60, coverage_after=0.80,
        complexity_before=10.0, complexity_after=8.0,
        lint_before=5, lint_after=2,
        blast_radius_total=3,
    )
    assert score.composite < 0.3
    assert score.test_delta < 0.3
    assert score.coverage_delta < 0.3


def test_compute_score_terrible_patch():
    """A patch that degrades everything should score near 1."""
    from backend.core.ouroboros.governance.composite_score import CompositeScoreFunction
    csf = CompositeScoreFunction()
    score = csf.compute(
        op_id="op-terrible-001",
        test_pass_rate_before=1.0, test_pass_rate_after=0.50,
        coverage_before=0.90, coverage_after=0.60,
        complexity_before=5.0, complexity_after=20.0,
        lint_before=0, lint_after=10,
        blast_radius_total=40,
    )
    assert score.composite > 0.6
    assert score.test_delta > 0.4


def test_compute_score_neutral_patch():
    """A patch that changes nothing should score around 0.5."""
    from backend.core.ouroboros.governance.composite_score import CompositeScoreFunction
    csf = CompositeScoreFunction()
    score = csf.compute(
        op_id="op-neutral-001",
        test_pass_rate_before=0.90, test_pass_rate_after=0.90,
        coverage_before=0.80, coverage_after=0.80,
        complexity_before=10.0, complexity_after=10.0,
        lint_before=3, lint_after=3,
        blast_radius_total=0,
    )
    assert 0.35 < score.composite < 0.65


def test_custom_weights():
    """Custom weights should change the composite result."""
    from backend.core.ouroboros.governance.composite_score import CompositeScoreFunction
    csf_default = CompositeScoreFunction()
    csf_test_heavy = CompositeScoreFunction(weights=(0.90, 0.025, 0.025, 0.025, 0.025))
    score_default = csf_default.compute(
        op_id="op-w1", test_pass_rate_before=0.5, test_pass_rate_after=1.0,
        coverage_before=0.5, coverage_after=0.5,
        complexity_before=10.0, complexity_after=10.0,
        lint_before=0, lint_after=0, blast_radius_total=0,
    )
    score_heavy = csf_test_heavy.compute(
        op_id="op-w2", test_pass_rate_before=0.5, test_pass_rate_after=1.0,
        coverage_before=0.5, coverage_after=0.5,
        complexity_before=10.0, complexity_after=10.0,
        lint_before=0, lint_after=0, blast_radius_total=0,
    )
    # Test-heavy weighting should produce a lower (better) score when tests improve
    assert score_heavy.composite < score_default.composite


def test_weights_must_be_length_5():
    from backend.core.ouroboros.governance.composite_score import CompositeScoreFunction
    with pytest.raises(ValueError, match="exactly 5 weights"):
        CompositeScoreFunction(weights=(0.5, 0.5))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_composite_score.py::test_compute_score_perfect_patch -v --timeout=15 -x`
Expected: FAIL with `ImportError` (CompositeScoreFunction not defined)

- [ ] **Step 3: Write the CompositeScoreFunction class**

Add to `backend/core/ouroboros/governance/composite_score.py` after the `CompositeScore` dataclass:

```python
class CompositeScoreFunction:
    """Computes Wang-consistent composite quality scores for Ouroboros operations.

    Each sub-score is normalized to [0, 1] where lower = better.
    The composite is a weighted sum of sub-scores, also in [0, 1].

    Deterministic. No model calls. Thread-safe (stateless computation).
    """

    def __init__(
        self,
        weights: tuple[float, ...] = _DEFAULT_WEIGHTS,
        persistence_dir: Path = _PERSISTENCE_DIR,
    ) -> None:
        if len(weights) != 5:
            raise ValueError(f"CompositeScoreFunction requires exactly 5 weights, got {len(weights)}")
        total = sum(weights)
        self._weights = tuple(w / total for w in weights)  # Normalize to sum=1
        self._persistence_dir = persistence_dir

    def compute(
        self,
        op_id: str,
        test_pass_rate_before: float,
        test_pass_rate_after: float,
        coverage_before: float,
        coverage_after: float,
        complexity_before: float,
        complexity_after: float,
        lint_before: int,
        lint_after: int,
        blast_radius_total: int,
    ) -> CompositeScore:
        """Compute composite score from before/after quality signals.

        All inputs are raw values; normalization happens internally.
        Returns a frozen CompositeScore with all sub-scores and the weighted composite.
        """
        # Sub-score 1: Test delta — improvement in pass rate
        # 1.0 - (after - before) => if tests improve, score decreases (better)
        s_test = _clamp(1.0 - (test_pass_rate_after - test_pass_rate_before))

        # Sub-score 2: Coverage delta — improvement in coverage
        s_cov = _clamp(1.0 - (coverage_after - coverage_before))

        # Sub-score 3: Complexity delta — sigmoid of complexity change
        # If complexity decreases, sigmoid(negative) < 0.5 (better)
        s_cx = _sigmoid(complexity_after - complexity_before)

        # Sub-score 4: Lint delta — sigmoid of lint issue change
        s_lint = _sigmoid(float(lint_after - lint_before))

        # Sub-score 5: Blast radius — linear normalization against cap of 50
        s_br = _clamp(blast_radius_total / 50.0)

        w = self._weights
        composite = (
            w[0] * s_test
            + w[1] * s_cov
            + w[2] * s_cx
            + w[3] * s_lint
            + w[4] * s_br
        )

        return CompositeScore(
            test_delta=round(s_test, 4),
            coverage_delta=round(s_cov, 4),
            complexity_delta=round(s_cx, 4),
            lint_delta=round(s_lint, 4),
            blast_radius=round(s_br, 4),
            composite=round(composite, 4),
            op_id=op_id,
            timestamp=time.time(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_composite_score.py -v --timeout=15 -x`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/composite_score.py tests/test_ouroboros_governance/test_composite_score.py
git commit -m "feat(rsi): add CompositeScoreFunction computation engine"
```

---

## Task 3: Composite Score Function — Persistence & History

**Files:**
- Modify: `backend/core/ouroboros/governance/composite_score.py`
- Test: `tests/test_ouroboros_governance/test_composite_score.py`

- [ ] **Step 1: Write the failing test for persistence**

Append to `tests/test_ouroboros_governance/test_composite_score.py`:

```python
def test_score_history_persists(tmp_path):
    from backend.core.ouroboros.governance.composite_score import (
        CompositeScoreFunction, ScoreHistory,
    )
    history = ScoreHistory(persistence_dir=tmp_path)
    csf = CompositeScoreFunction()
    score1 = csf.compute(
        op_id="op-h1", test_pass_rate_before=0.8, test_pass_rate_after=0.9,
        coverage_before=0.7, coverage_after=0.75,
        complexity_before=10, complexity_after=9,
        lint_before=3, lint_after=2, blast_radius_total=5,
    )
    score2 = csf.compute(
        op_id="op-h2", test_pass_rate_before=0.9, test_pass_rate_after=0.95,
        coverage_before=0.75, coverage_after=0.80,
        complexity_before=9, complexity_after=8,
        lint_before=2, lint_after=1, blast_radius_total=3,
    )
    history.record(score1)
    history.record(score2)

    # Reload from disk
    history2 = ScoreHistory(persistence_dir=tmp_path)
    scores = history2.get_recent(10)
    assert len(scores) == 2
    assert scores[0].op_id == "op-h1"
    assert scores[1].op_id == "op-h2"


def test_score_history_get_recent_limits(tmp_path):
    from backend.core.ouroboros.governance.composite_score import (
        CompositeScoreFunction, ScoreHistory,
    )
    history = ScoreHistory(persistence_dir=tmp_path)
    csf = CompositeScoreFunction()
    for i in range(10):
        score = csf.compute(
            op_id=f"op-limit-{i}", test_pass_rate_before=0.8, test_pass_rate_after=0.9,
            coverage_before=0.7, coverage_after=0.75,
            complexity_before=10, complexity_after=9,
            lint_before=3, lint_after=2, blast_radius_total=5,
        )
        history.record(score)

    recent_5 = history.get_recent(5)
    assert len(recent_5) == 5
    assert recent_5[0].op_id == "op-limit-5"
    assert recent_5[4].op_id == "op-limit-9"


def test_score_history_empty(tmp_path):
    from backend.core.ouroboros.governance.composite_score import ScoreHistory
    history = ScoreHistory(persistence_dir=tmp_path)
    assert history.get_recent(10) == []


def test_score_history_composites_only(tmp_path):
    from backend.core.ouroboros.governance.composite_score import (
        CompositeScoreFunction, ScoreHistory,
    )
    history = ScoreHistory(persistence_dir=tmp_path)
    csf = CompositeScoreFunction()
    for i in range(5):
        score = csf.compute(
            op_id=f"op-co-{i}", test_pass_rate_before=0.8, test_pass_rate_after=0.85 + i * 0.03,
            coverage_before=0.7, coverage_after=0.75,
            complexity_before=10, complexity_after=9,
            lint_before=3, lint_after=2, blast_radius_total=5,
        )
        history.record(score)
    composites = history.get_composite_values()
    assert len(composites) == 5
    assert all(isinstance(v, float) for v in composites)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_composite_score.py::test_score_history_persists -v --timeout=15 -x`
Expected: FAIL with `ImportError` (ScoreHistory not defined)

- [ ] **Step 3: Write the ScoreHistory class**

Add to `backend/core/ouroboros/governance/composite_score.py`:

```python
class ScoreHistory:
    """Append-only history of composite scores. Persisted as JSONL."""

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._path = persistence_dir / "composite_scores.jsonl"
        self._scores: List[CompositeScore] = []
        self._load()

    def record(self, score: CompositeScore) -> None:
        """Append a score to history and persist."""
        self._scores.append(score)
        self._append(score)

    def get_recent(self, n: int) -> List[CompositeScore]:
        """Return the last n scores in chronological order."""
        return self._scores[-n:] if n < len(self._scores) else list(self._scores)

    def get_composite_values(self) -> List[float]:
        """Return just the composite float values, chronological order."""
        return [s.composite for s in self._scores]

    def _append(self, score: CompositeScore) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(json.dumps(asdict(score)) + "\n")
        except Exception:
            logger.debug("ScoreHistory: failed to persist score", exc_info=True)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    self._scores.append(CompositeScore(**d))
        except Exception:
            logger.debug("ScoreHistory: failed to load scores", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_composite_score.py -v --timeout=15 -x`
Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/composite_score.py tests/test_ouroboros_governance/test_composite_score.py
git commit -m "feat(rsi): add ScoreHistory persistence for composite scores"
```

---

## Task 4: Convergence Tracker — Core Detection

**Files:**
- Create: `backend/core/ouroboros/governance/convergence_tracker.py`
- Test: `tests/test_ouroboros_governance/test_convergence_tracker.py`

- [ ] **Step 1: Write the failing test for convergence detection**

```python
"""Tests for ConvergenceTracker — RSI convergence monitoring."""
from __future__ import annotations

import pytest
import time


def test_insufficient_data():
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceState,
    )
    tracker = ConvergenceTracker()
    report = tracker.analyze([0.5, 0.4, 0.3])
    assert report.state == ConvergenceState.INSUFFICIENT_DATA


def test_improving_trend():
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceState,
    )
    tracker = ConvergenceTracker()
    # Steadily decreasing scores = improving
    scores = [0.9 - i * 0.04 for i in range(20)]
    report = tracker.analyze(scores)
    assert report.state in (ConvergenceState.IMPROVING, ConvergenceState.LOGARITHMIC)
    assert report.slope < 0


def test_degrading_trend():
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceState,
    )
    tracker = ConvergenceTracker()
    # Steadily increasing scores = degrading
    scores = [0.1 + i * 0.04 for i in range(20)]
    report = tracker.analyze(scores)
    assert report.state == ConvergenceState.DEGRADING
    assert report.slope > 0


def test_plateaued():
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceState,
    )
    tracker = ConvergenceTracker()
    # Nearly flat scores = plateau
    scores = [0.5 + (i % 2) * 0.005 for i in range(20)]
    report = tracker.analyze(scores)
    assert report.state == ConvergenceState.PLATEAUED


def test_oscillating():
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceState,
    )
    tracker = ConvergenceTracker()
    # Alternating up and down = oscillating
    scores = [0.5 + (0.15 if i % 2 == 0 else -0.15) for i in range(20)]
    report = tracker.analyze(scores)
    assert report.state == ConvergenceState.OSCILLATING
    assert report.oscillation_ratio > 0.5


def test_logarithmic_convergence():
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceState,
    )
    import math
    tracker = ConvergenceTracker()
    # S = -0.1 * ln(t) + 0.8 => decreasing logarithmically
    scores = [-0.1 * math.log(t + 1) + 0.8 for t in range(20)]
    report = tracker.analyze(scores)
    assert report.state == ConvergenceState.LOGARITHMIC
    assert report.r_squared_log > 0.7


def test_convergence_report_fields():
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceReport,
    )
    tracker = ConvergenceTracker()
    scores = [0.8 - i * 0.03 for i in range(20)]
    report = tracker.analyze(scores)
    assert isinstance(report, ConvergenceReport)
    assert report.window_size == 20
    assert report.scores_analyzed == 20
    assert isinstance(report.recommendation, str)
    assert report.timestamp > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_convergence_tracker.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the ConvergenceTracker implementation**

```python
"""
Convergence Tracker — RSI convergence monitoring for Ouroboros.

Detects whether the Ouroboros pipeline is converging (improving),
plateauing, oscillating, or degrading by analyzing composite score
history. Triggers corrective actions when convergence stalls.

Based on Wang (2018) logarithmic convergence prediction:
healthy RSI systems show O(log n) steps to reach near-optimal.

Boundary Principle: 100% deterministic. Statistics only, no LLM calls.

See: docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md Section 6
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)

_WINDOW_SIZE = int(os.environ.get("OUROBOROS_CONVERGENCE_WINDOW", "20"))
_EPSILON = float(os.environ.get("OUROBOROS_CONVERGENCE_EPSILON", "0.01"))
_PLATEAU_K = 5
_PLATEAU_DELTA = 0.02
_OSCILLATION_THRESHOLD = 0.6
_MIN_DATA_POINTS = 5
_LOG_FIT_THRESHOLD = 0.7


class ConvergenceState(str, Enum):
    IMPROVING = "improving"
    LOGARITHMIC = "logarithmic"
    PLATEAUED = "plateaued"
    OSCILLATING = "oscillating"
    DEGRADING = "degrading"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class ConvergenceReport:
    """Result of convergence analysis over a window of composite scores."""
    state: ConvergenceState
    window_size: int
    slope: float
    r_squared_log: float
    oscillation_ratio: float
    plateau_stddev: float
    scores_analyzed: int
    recommendation: str
    timestamp: float


def _linear_regression_slope(values: List[float]) -> float:
    """Compute slope of linear regression y = m*x + b over indices 0..n-1."""
    n = len(values)
    if n < 2:
        return 0.0
    sx = sum(range(n))
    sy = sum(values)
    sxy = sum(i * v for i, v in enumerate(values))
    sxx = sum(i * i for i in range(n))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _log_fit_r_squared(values: List[float]) -> float:
    """R-squared of fit S = a * ln(t+1) + b. Returns (r_squared, a)."""
    n = len(values)
    if n < 3:
        return 0.0
    log_t = [math.log(i + 1) for i in range(n)]
    mean_log = sum(log_t) / n
    mean_s = sum(values) / n
    ss_tot = sum((v - mean_s) ** 2 for v in values)
    if ss_tot < 1e-12:
        return 1.0  # All values identical — trivially perfect fit
    cov = sum((log_t[i] - mean_log) * (values[i] - mean_s) for i in range(n))
    var_log = sum((lt - mean_log) ** 2 for lt in log_t)
    if var_log < 1e-12:
        return 0.0
    a = cov / var_log
    b = mean_s - a * mean_log
    ss_res = sum((values[i] - (a * log_t[i] + b)) ** 2 for i in range(n))
    r_sq = 1.0 - ss_res / ss_tot
    # Only count as logarithmic if a < 0 (decreasing)
    if a >= 0:
        return 0.0
    return max(0.0, r_sq)


def _oscillation_ratio(values: List[float]) -> float:
    """Fraction of consecutive differences that alternate sign."""
    if len(values) < 3:
        return 0.0
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    sign_changes = sum(
        1 for i in range(len(diffs) - 1)
        if diffs[i] * diffs[i + 1] < 0
    )
    return sign_changes / max(1, len(diffs) - 1)


def _stddev(values: List[float]) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


_RECOMMENDATIONS = {
    ConvergenceState.IMPROVING: "Pipeline is converging. Continue current strategy.",
    ConvergenceState.LOGARITHMIC: "Pipeline matches Wang's O(log n) prediction. Healthy convergence confirmed.",
    ConvergenceState.PLATEAUED: "Pipeline has plateaued. Consider triggering Dynamic Re-Planning (technique #5).",
    ConvergenceState.OSCILLATING: "Pipeline is oscillating. Tighten negative constraints and narrow generation scope.",
    ConvergenceState.DEGRADING: "Pipeline is degrading. Consider pausing autonomous operations and investigating.",
    ConvergenceState.INSUFFICIENT_DATA: "Not enough data for convergence analysis. Continue collecting scores.",
}


class ConvergenceTracker:
    """Analyzes composite score history to detect convergence state."""

    def analyze(self, scores: List[float]) -> ConvergenceReport:
        """Analyze a list of composite scores (chronological, lower=better).

        Returns a ConvergenceReport with the detected state and recommendation.
        """
        n = len(scores)
        if n < _MIN_DATA_POINTS:
            return ConvergenceReport(
                state=ConvergenceState.INSUFFICIENT_DATA,
                window_size=n, slope=0.0, r_squared_log=0.0,
                oscillation_ratio=0.0, plateau_stddev=0.0,
                scores_analyzed=n,
                recommendation=_RECOMMENDATIONS[ConvergenceState.INSUFFICIENT_DATA],
                timestamp=time.time(),
            )

        window = scores[-_WINDOW_SIZE:] if n > _WINDOW_SIZE else scores
        slope = _linear_regression_slope(window)
        r_sq_log = _log_fit_r_squared(window)
        osc = _oscillation_ratio(window)
        tail_std = _stddev(window[-_PLATEAU_K:]) if len(window) >= _PLATEAU_K else _stddev(window)

        # Classification priority: logarithmic > oscillating > plateau > improving/degrading
        if r_sq_log > _LOG_FIT_THRESHOLD and slope < -_EPSILON:
            state = ConvergenceState.LOGARITHMIC
        elif osc > _OSCILLATION_THRESHOLD:
            state = ConvergenceState.OSCILLATING
        elif tail_std < _PLATEAU_DELTA and abs(slope) < _EPSILON:
            state = ConvergenceState.PLATEAUED
        elif slope < -_EPSILON:
            state = ConvergenceState.IMPROVING
        elif slope > _EPSILON:
            state = ConvergenceState.DEGRADING
        else:
            state = ConvergenceState.PLATEAUED

        return ConvergenceReport(
            state=state,
            window_size=len(window),
            slope=round(slope, 6),
            r_squared_log=round(r_sq_log, 4),
            oscillation_ratio=round(osc, 4),
            plateau_stddev=round(tail_std, 6),
            scores_analyzed=n,
            recommendation=_RECOMMENDATIONS[state],
            timestamp=time.time(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_convergence_tracker.py -v --timeout=15 -x`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/convergence_tracker.py tests/test_ouroboros_governance/test_convergence_tracker.py
git commit -m "feat(rsi): add ConvergenceTracker with trend and logarithmic fit detection"
```

---

## Task 5: Adaptive Graduation Threshold

**Files:**
- Modify: `backend/core/ouroboros/governance/graduation_orchestrator.py:119-157`
- Test: `tests/test_ouroboros_governance/test_adaptive_graduation.py`

- [ ] **Step 1: Write the failing test for adaptive threshold computation**

```python
"""Tests for Adaptive Graduation Threshold — Bayesian estimation."""
from __future__ import annotations

import math
import pytest


def test_all_successes_low_threshold():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold,
    )
    # 3 successes, 0 failures, all different goals => threshold should be low
    result = compute_adaptive_threshold(successes=3, failures=0, unique_goals=3, total_uses=3)
    assert result.threshold == 3  # ceil(2.0 / 0.80) = 3
    assert result.p_success > 0.7


def test_mixed_results_higher_threshold():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold,
    )
    # 2 successes, 1 failure => needs more evidence
    result = compute_adaptive_threshold(successes=2, failures=1, unique_goals=3, total_uses=3)
    assert result.threshold >= 4


def test_low_success_rate_much_higher_threshold():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold,
    )
    # 1 success, 2 failures => very high threshold
    result = compute_adaptive_threshold(successes=1, failures=2, unique_goals=3, total_uses=3)
    assert result.threshold >= 5


def test_diversity_bonus_same_goal():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold,
    )
    # 3 successes but all same goal => lower diversity => higher threshold
    result = compute_adaptive_threshold(successes=3, failures=0, unique_goals=1, total_uses=3)
    assert result.threshold >= 3
    assert result.diversity < 0.5


def test_diversity_bonus_diverse_goals():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold,
    )
    # 3 successes, all different goals => full diversity
    result = compute_adaptive_threshold(successes=3, failures=0, unique_goals=3, total_uses=3)
    assert result.diversity >= 0.9


def test_minimum_threshold_enforced():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold,
    )
    # Even perfect results can't go below MIN_THRESHOLD
    result = compute_adaptive_threshold(successes=100, failures=0, unique_goals=100, total_uses=100)
    assert result.threshold >= 2


def test_zero_uses_returns_high_threshold():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold,
    )
    result = compute_adaptive_threshold(successes=0, failures=0, unique_goals=0, total_uses=0)
    assert result.threshold >= 4  # Very conservative with no data


def test_adaptive_threshold_result_fields():
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        compute_adaptive_threshold, AdaptiveThresholdResult,
    )
    result = compute_adaptive_threshold(successes=5, failures=1, unique_goals=4, total_uses=6)
    assert isinstance(result, AdaptiveThresholdResult)
    assert isinstance(result.threshold, int)
    assert 0.0 <= result.p_success <= 1.0
    assert 0.0 <= result.diversity <= 1.0
    assert 0.0 <= result.effective_p <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_adaptive_graduation.py -v --timeout=15 -x`
Expected: FAIL with `ImportError` (compute_adaptive_threshold not defined)

- [ ] **Step 3: Add AdaptiveThresholdResult and compute_adaptive_threshold**

In `backend/core/ouroboros/governance/graduation_orchestrator.py`, add after the `_STOP_WORDS` definition (after line 48):

```python
_ADAPTIVE_MIN_THRESHOLD = int(os.environ.get("OUROBOROS_ADAPTIVE_GRAD_MIN", "2"))
_ADAPTIVE_CONFIDENCE = float(os.environ.get("OUROBOROS_ADAPTIVE_GRAD_CONFIDENCE", "2.0"))


@dataclass(frozen=True)
class AdaptiveThresholdResult:
    """Result of adaptive graduation threshold computation."""
    threshold: int
    p_success: float     # Beta posterior mean
    diversity: float     # Goal diversity ratio [0, 1]
    effective_p: float   # p_success adjusted by diversity


def compute_adaptive_threshold(
    successes: int,
    failures: int,
    unique_goals: int,
    total_uses: int,
) -> AdaptiveThresholdResult:
    """Compute adaptive graduation threshold using Bayesian estimation.

    Uses Beta(1+s, 1+f) posterior for success probability,
    adjusted by goal diversity. Returns at least _ADAPTIVE_MIN_THRESHOLD.

    Based on Wang's RSI score-as-expected-steps concept: tools closer
    to "proven" need fewer additional observations.
    """
    import math

    # Beta posterior mean: P(success) = (1 + s) / (2 + s + f)
    p_success = (1 + successes) / (2 + successes + failures)

    # Diversity: fraction of unique goals out of total uses
    if total_uses > 0:
        diversity = min(1.0, unique_goals / total_uses)
    else:
        diversity = 0.0

    # Effective probability: adjusted by diversity (0.5 + 0.5 * diversity)
    effective_p = p_success * (0.5 + 0.5 * diversity)

    # Threshold: ceil(confidence / effective_p), floored at minimum
    if effective_p > 0:
        threshold = max(_ADAPTIVE_MIN_THRESHOLD, math.ceil(_ADAPTIVE_CONFIDENCE / effective_p))
    else:
        threshold = max(_ADAPTIVE_MIN_THRESHOLD, math.ceil(_ADAPTIVE_CONFIDENCE / 0.1))

    return AdaptiveThresholdResult(
        threshold=threshold,
        p_success=round(p_success, 4),
        diversity=round(diversity, 4),
        effective_p=round(effective_p, 4),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_adaptive_graduation.py -v --timeout=15 -x`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/graduation_orchestrator.py tests/test_ouroboros_governance/test_adaptive_graduation.py
git commit -m "feat(rsi): add Bayesian adaptive graduation threshold"
```

---

## Task 6: Wire Adaptive Threshold into EphemeralUsageTracker

**Files:**
- Modify: `backend/core/ouroboros/governance/graduation_orchestrator.py:137-157`
- Test: `tests/test_ouroboros_governance/test_adaptive_graduation.py`

- [ ] **Step 1: Write the failing test for tracker integration**

Append to `tests/test_ouroboros_governance/test_adaptive_graduation.py`:

```python
@pytest.mark.asyncio
async def test_tracker_uses_adaptive_threshold(tmp_path):
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        EphemeralUsageTracker,
    )
    tracker = EphemeralUsageTracker(
        persistence_path=tmp_path / "usage.json",
        graduation_threshold=3,  # Static fallback, but adaptive should take over
    )
    # Record 3 successes for the same goal — low diversity means threshold > 3
    for _ in range(3):
        result = await tracker.record_usage(
            goal="calculate prime numbers",
            code_hash="abc123",
            outcome="success",
            elapsed_s=1.0,
        )
    # With same goal repeated, diversity is low, so threshold should be > 3
    # This means 3 successes is NOT enough — result should be None
    assert result is None


@pytest.mark.asyncio
async def test_tracker_fires_with_diverse_goals(tmp_path):
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        EphemeralUsageTracker,
    )
    tracker = EphemeralUsageTracker(
        persistence_path=tmp_path / "usage.json",
        graduation_threshold=3,
    )
    # Record 3 successes for different goals — high diversity
    goals = ["analyze images for objects", "detect edges in photographs", "classify visual scenes"]
    results = []
    for goal in goals:
        result = await tracker.record_usage(
            goal=goal,
            code_hash="abc123",
            outcome="success",
            elapsed_s=1.0,
        )
        results.append(result)
    # With diverse goals and 100% success, adaptive threshold should be 3
    # So the 3rd success should fire
    assert results[-1] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_adaptive_graduation.py::test_tracker_uses_adaptive_threshold -v --timeout=15 -x`
Expected: FAIL (the current static threshold fires at 3 regardless of diversity)

- [ ] **Step 3: Modify EphemeralUsageTracker.record_usage to use adaptive threshold**

In `backend/core/ouroboros/governance/graduation_orchestrator.py`, replace the threshold check block inside `record_usage` (approximately lines 148-157). The current code is:

```python
            success_count = sum(1 for r in self._data[gcid] if r.execution_outcome == "success")
            if success_count >= self._threshold and gcid not in self._threshold_fired:
                self._threshold_fired.add(gcid)
                return gcid
```

Replace with:

```python
            records = self._data[gcid]
            success_count = sum(1 for r in records if r.execution_outcome == "success")
            failure_count = len(records) - success_count
            unique_goals = len({r.goal_hash for r in records})
            total_uses = len(records)

            adaptive = compute_adaptive_threshold(
                successes=success_count,
                failures=failure_count,
                unique_goals=unique_goals,
                total_uses=total_uses,
            )
            if success_count >= adaptive.threshold and gcid not in self._threshold_fired:
                self._threshold_fired.add(gcid)
                logger.info(
                    "[AdaptiveGraduation] Threshold met for %s: %d/%d "
                    "(p=%.2f, diversity=%.2f, threshold=%d)",
                    gcid, success_count, total_uses,
                    adaptive.p_success, adaptive.diversity, adaptive.threshold,
                )
                return gcid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_adaptive_graduation.py -v --timeout=15 -x`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/graduation_orchestrator.py tests/test_ouroboros_governance/test_adaptive_graduation.py
git commit -m "feat(rsi): wire adaptive threshold into EphemeralUsageTracker"
```

---

## Task 7: Transition Probability Tracker

**Files:**
- Create: `backend/core/ouroboros/governance/transition_tracker.py`
- Test: `tests/test_ouroboros_governance/test_transition_tracker.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for TransitionProbabilityTracker — empirical technique success rates."""
from __future__ import annotations

import pytest


def test_record_and_query(tmp_path):
    from backend.core.ouroboros.governance.transition_tracker import (
        TransitionProbabilityTracker, TechniqueOutcome,
    )
    tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
    tracker.record(TechniqueOutcome(
        technique="module_mutation", domain="governance", complexity="heavy_code",
        success=True, composite_score=0.3, op_id="op-1", timestamp=1.0,
    ))
    tracker.record(TechniqueOutcome(
        technique="module_mutation", domain="governance", complexity="heavy_code",
        success=True, composite_score=0.25, op_id="op-2", timestamp=2.0,
    ))
    tracker.record(TechniqueOutcome(
        technique="module_mutation", domain="governance", complexity="heavy_code",
        success=False, composite_score=0.8, op_id="op-3", timestamp=3.0,
    ))
    p = tracker.get_probability("module_mutation", "governance", "heavy_code")
    # Laplace: (1 + 2) / (2 + 3) = 0.6
    assert p == pytest.approx(0.6, abs=0.01)


def test_fallback_to_technique_domain(tmp_path):
    from backend.core.ouroboros.governance.transition_tracker import (
        TransitionProbabilityTracker, TechniqueOutcome,
    )
    tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
    # Record 10 outcomes at (tech, domain) level but only 2 at full key
    for i in range(10):
        tracker.record(TechniqueOutcome(
            technique="prompt_adaptation", domain="backend", complexity="light",
            success=(i < 8), composite_score=0.3, op_id=f"op-{i}", timestamp=float(i),
        ))
    # Query with a different complexity — should fall back to (tech, domain)
    p = tracker.get_probability("prompt_adaptation", "backend", "complex")
    # (tech, domain) has 8 successes out of 10: (1+8)/(2+10) = 0.75
    assert p == pytest.approx(0.75, abs=0.01)


def test_fallback_to_global_prior(tmp_path):
    from backend.core.ouroboros.governance.transition_tracker import (
        TransitionProbabilityTracker,
    )
    tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
    # No data at all — should return global prior 0.5
    p = tracker.get_probability("unknown_technique", "unknown_domain", "complex")
    assert p == pytest.approx(0.5, abs=0.01)


def test_rank_techniques(tmp_path):
    from backend.core.ouroboros.governance.transition_tracker import (
        TransitionProbabilityTracker, TechniqueOutcome,
    )
    tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
    # module_mutation: 4/5 success
    for i in range(5):
        tracker.record(TechniqueOutcome(
            technique="module_mutation", domain="backend", complexity="heavy_code",
            success=(i < 4), composite_score=0.3, op_id=f"mm-{i}", timestamp=float(i),
        ))
    # negative_constraints: 2/5 success
    for i in range(5):
        tracker.record(TechniqueOutcome(
            technique="negative_constraints", domain="backend", complexity="heavy_code",
            success=(i < 2), composite_score=0.5, op_id=f"nc-{i}", timestamp=float(i),
        ))
    ranked = tracker.rank_techniques(domain="backend", complexity="heavy_code")
    assert len(ranked) >= 2
    assert ranked[0][0] == "module_mutation"
    assert ranked[0][1] > ranked[1][1]


def test_persistence(tmp_path):
    from backend.core.ouroboros.governance.transition_tracker import (
        TransitionProbabilityTracker, TechniqueOutcome,
    )
    tracker1 = TransitionProbabilityTracker(persistence_dir=tmp_path)
    tracker1.record(TechniqueOutcome(
        technique="metrics_feedback", domain="vision", complexity="light",
        success=True, composite_score=0.2, op_id="op-p1", timestamp=1.0,
    ))
    # Reload
    tracker2 = TransitionProbabilityTracker(persistence_dir=tmp_path)
    p = tracker2.get_probability("metrics_feedback", "vision", "light")
    assert p > 0.5  # At least one success recorded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_transition_tracker.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the TransitionProbabilityTracker implementation**

```python
"""
Transition Probability Tracker — empirical technique success rates for Ouroboros.

Tracks P(success | technique, domain, complexity) from historical outcomes.
Uses Laplace-smoothed frequencies with a fallback hierarchy:
  1. Full key (technique, domain, complexity) — if >= 5 observations
  2. Partial key (technique, domain) — if >= 5 observations
  3. Technique only — always available after first use
  4. Global prior 0.5 — before any data

Based on Wang's Markov transition matrix concept: each "technique" is a
program that generates patches with domain-specific success probabilities.

Boundary Principle: 100% deterministic. Frequency counting only.

See: docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md Section 9
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_SELF_EVOLUTION_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
    )
)
_MIN_OBSERVATIONS = 5
_GLOBAL_PRIOR = 0.5


@dataclass
class TechniqueOutcome:
    """One recorded outcome of applying a self-evolution technique."""
    technique: str
    domain: str
    complexity: str
    success: bool
    composite_score: float
    op_id: str
    timestamp: float = field(default_factory=time.time)


class TransitionProbabilityTracker:
    """Tracks empirical success probabilities for self-evolution techniques.

    Maintains counters at three granularity levels:
    - Full: (technique, domain, complexity)
    - Partial: (technique, domain)
    - Technique: (technique,)

    Queries fall back through the hierarchy when data is sparse.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        # Counters: key -> {"success": int, "total": int}
        self._full: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        self._partial: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        self._technique: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        self._load()

    def record(self, outcome: TechniqueOutcome) -> None:
        """Record one technique outcome at all granularity levels."""
        full_key = f"{outcome.technique}:{outcome.domain}:{outcome.complexity}"
        partial_key = f"{outcome.technique}:{outcome.domain}"
        tech_key = outcome.technique

        for key, store in [
            (full_key, self._full),
            (partial_key, self._partial),
            (tech_key, self._technique),
        ]:
            store[key]["total"] += 1
            if outcome.success:
                store[key]["success"] += 1

        self._persist()

    def get_probability(self, technique: str, domain: str, complexity: str) -> float:
        """Get P(success) with fallback hierarchy. Laplace-smoothed."""
        full_key = f"{technique}:{domain}:{complexity}"
        if full_key in self._full and self._full[full_key]["total"] >= _MIN_OBSERVATIONS:
            c = self._full[full_key]
            return (1 + c["success"]) / (2 + c["total"])

        partial_key = f"{technique}:{domain}"
        if partial_key in self._partial and self._partial[partial_key]["total"] >= _MIN_OBSERVATIONS:
            c = self._partial[partial_key]
            return (1 + c["success"]) / (2 + c["total"])

        tech_key = technique
        if tech_key in self._technique:
            c = self._technique[tech_key]
            return (1 + c["success"]) / (2 + c["total"])

        return _GLOBAL_PRIOR

    def rank_techniques(
        self, domain: str, complexity: str,
    ) -> List[Tuple[str, float]]:
        """Rank all known techniques by P(success) for given domain+complexity.

        Returns list of (technique_name, probability) sorted descending.
        """
        techniques = set(self._technique.keys())
        ranked = []
        for tech in techniques:
            p = self.get_probability(tech, domain, complexity)
            ranked.append((tech, round(p, 4)))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "transition_probabilities.json"
            data = {
                "full": dict(self._full),
                "partial": dict(self._partial),
                "technique": dict(self._technique),
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.debug("TransitionTracker: persist failed", exc_info=True)

    def _load(self) -> None:
        path = self._persistence_dir / "transition_probabilities.json"
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for key, val in data.get("full", {}).items():
                self._full[key] = val
            for key, val in data.get("partial", {}).items():
                self._partial[key] = val
            for key, val in data.get("technique", {}).items():
                self._technique[key] = val
        except Exception:
            logger.debug("TransitionTracker: load failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_transition_tracker.py -v --timeout=15 -x`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/transition_tracker.py tests/test_ouroboros_governance/test_transition_tracker.py
git commit -m "feat(rsi): add TransitionProbabilityTracker with fallback hierarchy"
```

---

## Task 8: Oracle Pre-Scorer

**Files:**
- Create: `backend/core/ouroboros/governance/oracle_prescorer.py`
- Test: `tests/test_ouroboros_governance/test_oracle_prescorer.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for OraclePreScorer — fast approximate quality gate."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass


@dataclass
class MockBlastRadius:
    total_affected: int
    risk_level: str


@dataclass
class MockMetrics:
    max_complexity: int


def _make_mock_oracle(blast_total=5, risk="low", deps=3, dependents=2):
    oracle = MagicMock()
    oracle.compute_blast_radius.return_value = MockBlastRadius(
        total_affected=blast_total, risk_level=risk,
    )
    oracle.get_dependencies.return_value = [MagicMock()] * deps
    oracle.get_dependents.return_value = [MagicMock()] * dependents
    return oracle


def test_low_risk_candidate():
    from backend.core.ouroboros.governance.oracle_prescorer import (
        OraclePreScorer, PreScoreResult,
    )
    oracle = _make_mock_oracle(blast_total=2, risk="low", deps=1, dependents=1)
    scorer = OraclePreScorer(oracle=oracle)
    result = scorer.score(
        target_files=["backend/core/simple.py"],
        max_complexity=5,
        has_tests=True,
    )
    assert isinstance(result, PreScoreResult)
    assert result.pre_score < 0.3
    assert result.gate == "FAST_TRACK"


def test_high_risk_candidate():
    from backend.core.ouroboros.governance.oracle_prescorer import (
        OraclePreScorer,
    )
    oracle = _make_mock_oracle(blast_total=40, risk="critical", deps=15, dependents=10)
    scorer = OraclePreScorer(oracle=oracle)
    result = scorer.score(
        target_files=["backend/core/critical_module.py"],
        max_complexity=25,
        has_tests=False,
    )
    assert result.pre_score >= 0.7
    assert result.gate == "WARN"


def test_medium_risk_candidate():
    from backend.core.ouroboros.governance.oracle_prescorer import (
        OraclePreScorer,
    )
    oracle = _make_mock_oracle(blast_total=15, risk="medium", deps=8, dependents=5)
    scorer = OraclePreScorer(oracle=oracle)
    result = scorer.score(
        target_files=["backend/core/module.py"],
        max_complexity=12,
        has_tests=True,
    )
    assert 0.3 <= result.pre_score < 0.7
    assert result.gate == "NORMAL"


def test_oracle_failure_returns_neutral(tmp_path):
    from backend.core.ouroboros.governance.oracle_prescorer import (
        OraclePreScorer,
    )
    oracle = MagicMock()
    oracle.compute_blast_radius.side_effect = Exception("Oracle offline")
    scorer = OraclePreScorer(oracle=oracle)
    result = scorer.score(
        target_files=["backend/core/module.py"],
        max_complexity=10,
        has_tests=True,
    )
    # On oracle failure, return neutral score — never block
    assert 0.3 <= result.pre_score <= 0.7
    assert result.gate == "NORMAL"


def test_multiple_files_uses_worst():
    from backend.core.ouroboros.governance.oracle_prescorer import (
        OraclePreScorer,
    )
    oracle = MagicMock()
    # First file: low risk. Second file: high risk.
    blast_results = [
        MockBlastRadius(total_affected=2, risk_level="low"),
        MockBlastRadius(total_affected=30, risk_level="high"),
    ]
    oracle.compute_blast_radius.side_effect = blast_results
    oracle.get_dependencies.return_value = [MagicMock()] * 10
    oracle.get_dependents.return_value = [MagicMock()] * 10
    scorer = OraclePreScorer(oracle=oracle)
    result = scorer.score(
        target_files=["a.py", "b.py"],
        max_complexity=10,
        has_tests=True,
    )
    # Should use worst blast radius (30/50 = 0.6 for that signal)
    assert result.blast_radius_signal > 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_oracle_prescorer.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the OraclePreScorer implementation**

```python
"""
Oracle Pre-Scorer — fast approximate quality gate for Ouroboros candidates.

Uses TheOracle's graph signals (blast radius, coupling, structure) to
produce a quick quality estimate BEFORE full validation. This avoids
wasting validation cycles on obviously problematic candidates.

Inspired by Wang's "oracle score function" suggestion (Section 5,
Discussion and Future Works): a score that evaluates without processing
all programs.

Boundary Principle: 100% deterministic. Graph queries only, no LLM calls.
The pre-score NEVER blocks candidates — it only prioritizes and warns.

See: docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md Section 8
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)

_RISK_MAP = {"low": 0.0, "medium": 0.3, "high": 0.7, "critical": 1.0}

# Weights for pre-score components (sum to 1.0)
_W_BLAST = 0.30
_W_COUPLING = 0.25
_W_COMPLEXITY = 0.20
_W_TEST_COVERAGE = 0.15
_W_LOCALITY = 0.10

_FAST_TRACK_THRESHOLD = 0.3
_WARN_THRESHOLD = 0.7


@dataclass(frozen=True)
class PreScoreResult:
    """Result of oracle pre-scoring for a candidate patch."""
    pre_score: float          # [0, 1], lower = more promising
    gate: str                 # "FAST_TRACK", "NORMAL", or "WARN"
    blast_radius_signal: float
    coupling_signal: float
    complexity_signal: float
    test_coverage_signal: float
    locality_signal: float


class OraclePreScorer:
    """Fast approximate quality gate using TheOracle graph signals.

    Call score() on a candidate's target files to get a quick estimate.
    The pre-score never blocks — it only classifies candidates into
    FAST_TRACK (promising), NORMAL, or WARN (concerning).
    """

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    def score(
        self,
        target_files: List[str],
        max_complexity: int = 0,
        has_tests: bool = True,
    ) -> PreScoreResult:
        """Compute pre-score from oracle graph signals.

        Args:
            target_files: Files the candidate modifies.
            max_complexity: Maximum cyclomatic complexity of modified functions.
            has_tests: Whether test files exist for the target files.

        Returns:
            PreScoreResult with score and gate classification.
        """
        try:
            return self._compute(target_files, max_complexity, has_tests)
        except Exception:
            logger.debug("OraclePreScorer: falling back to neutral", exc_info=True)
            return PreScoreResult(
                pre_score=0.5, gate="NORMAL",
                blast_radius_signal=0.5, coupling_signal=0.5,
                complexity_signal=0.5, test_coverage_signal=0.0,
                locality_signal=0.5,
            )

    def _compute(
        self,
        target_files: List[str],
        max_complexity: int,
        has_tests: bool,
    ) -> PreScoreResult:
        # Signal 1: Blast radius (worst across all target files)
        worst_blast = 0.0
        for f in target_files:
            try:
                br = self._oracle.compute_blast_radius(f)
                risk_score = _RISK_MAP.get(br.risk_level, 0.5)
                linear_score = min(1.0, br.total_affected / 50.0)
                worst_blast = max(worst_blast, max(risk_score, linear_score))
            except Exception:
                pass
        s_blast = worst_blast

        # Signal 2: Coupling (dependencies + dependents)
        total_coupling = 0
        for f in target_files:
            try:
                deps = len(self._oracle.get_dependencies(f))
                depts = len(self._oracle.get_dependents(f))
                total_coupling += deps + depts
            except Exception:
                pass
        s_coupling = min(1.0, total_coupling / 20.0)

        # Signal 3: Complexity
        s_complexity = min(1.0, max_complexity / 30.0)

        # Signal 4: Test coverage proximity
        s_test = 0.0 if has_tests else 1.0

        # Signal 5: Change locality (all same directory = good)
        if len(target_files) > 1:
            dirs = {"/".join(f.split("/")[:-1]) for f in target_files}
            s_locality = 1.0 - (1.0 / len(dirs))
        else:
            s_locality = 0.0

        pre_score = (
            _W_BLAST * s_blast
            + _W_COUPLING * s_coupling
            + _W_COMPLEXITY * s_complexity
            + _W_TEST_COVERAGE * s_test
            + _W_LOCALITY * s_locality
        )
        pre_score = round(min(1.0, max(0.0, pre_score)), 4)

        if pre_score < _FAST_TRACK_THRESHOLD:
            gate = "FAST_TRACK"
        elif pre_score >= _WARN_THRESHOLD:
            gate = "WARN"
        else:
            gate = "NORMAL"

        return PreScoreResult(
            pre_score=pre_score,
            gate=gate,
            blast_radius_signal=round(s_blast, 4),
            coupling_signal=round(s_coupling, 4),
            complexity_signal=round(s_complexity, 4),
            test_coverage_signal=round(s_test, 4),
            locality_signal=round(s_locality, 4),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_oracle_prescorer.py -v --timeout=15 -x`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/oracle_prescorer.py tests/test_ouroboros_governance/test_oracle_prescorer.py
git commit -m "feat(rsi): add OraclePreScorer with graph-based quality estimation"
```

---

## Task 9: Vindication Reflector

**Files:**
- Create: `backend/core/ouroboros/governance/vindication_reflector.py`
- Test: `tests/test_ouroboros_governance/test_vindication_reflector.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for VindicationReflector — forward-looking trajectory analysis."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass


@dataclass
class MockBlastRadius:
    total_affected: int


def _mock_oracle(deps_before=5, deps_after=3, br_before=10, br_after=8):
    oracle = MagicMock()
    oracle.get_dependencies.return_value = [MagicMock()] * deps_before
    oracle.get_dependents.return_value = [MagicMock()] * deps_before
    oracle.compute_blast_radius.return_value = MockBlastRadius(total_affected=br_before)
    return oracle


def test_improving_patch_positive_vindication():
    from backend.core.ouroboros.governance.vindication_reflector import (
        VindicationReflector, VindicationResult,
    )
    oracle = _mock_oracle(deps_before=10, br_before=15)
    reflector = VindicationReflector(oracle=oracle)
    result = reflector.reflect(
        target_files=["backend/core/module.py"],
        coupling_after=6,     # Reduced from 20 (10 deps + 10 dependents)
        blast_radius_after=8,  # Reduced from 15
        complexity_after=5.0,
        complexity_before=10.0,
    )
    assert isinstance(result, VindicationResult)
    assert result.vindication_score > 0  # Positive = improving trajectory
    assert result.advisory == "vindicating"


def test_degrading_patch_negative_vindication():
    from backend.core.ouroboros.governance.vindication_reflector import (
        VindicationReflector,
    )
    oracle = _mock_oracle(deps_before=5, br_before=5)
    reflector = VindicationReflector(oracle=oracle)
    result = reflector.reflect(
        target_files=["backend/core/module.py"],
        coupling_after=30,     # Increased from 10
        blast_radius_after=25, # Increased from 5
        complexity_after=20.0,
        complexity_before=5.0,
    )
    assert result.vindication_score < -0.2
    assert result.advisory in ("concerning", "warning")


def test_neutral_patch():
    from backend.core.ouroboros.governance.vindication_reflector import (
        VindicationReflector,
    )
    oracle = _mock_oracle(deps_before=5, br_before=10)
    reflector = VindicationReflector(oracle=oracle)
    result = reflector.reflect(
        target_files=["backend/core/module.py"],
        coupling_after=10,     # Same as before (5+5)
        blast_radius_after=10, # Same
        complexity_after=8.0,
        complexity_before=8.0,
    )
    assert -0.2 <= result.vindication_score <= 0.2
    assert result.advisory == "neutral"


def test_oracle_failure_returns_neutral():
    from backend.core.ouroboros.governance.vindication_reflector import (
        VindicationReflector,
    )
    oracle = MagicMock()
    oracle.get_dependencies.side_effect = Exception("Oracle offline")
    reflector = VindicationReflector(oracle=oracle)
    result = reflector.reflect(
        target_files=["backend/core/module.py"],
        coupling_after=10,
        blast_radius_after=10,
        complexity_after=8.0,
        complexity_before=8.0,
    )
    assert result.advisory == "neutral"


def test_vindication_result_fields():
    from backend.core.ouroboros.governance.vindication_reflector import (
        VindicationReflector, VindicationResult,
    )
    oracle = _mock_oracle()
    reflector = VindicationReflector(oracle=oracle)
    result = reflector.reflect(
        target_files=["a.py"],
        coupling_after=5, blast_radius_after=5,
        complexity_after=5.0, complexity_before=5.0,
    )
    assert hasattr(result, "vindication_score")
    assert hasattr(result, "coupling_delta")
    assert hasattr(result, "blast_radius_delta")
    assert hasattr(result, "entropy_delta")
    assert hasattr(result, "advisory")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ouroboros_governance/test_vindication_reflector.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the VindicationReflector implementation**

```python
"""
Vindication Reflector — forward-looking trajectory analysis for Ouroboros.

Answers the question: "Will this patch make future patches better or worse?"

Computes three trajectory signals:
  1. Coupling trajectory — will dependencies increase?
  2. Blast radius trajectory — will future changes be riskier?
  3. Entropy trajectory — is complexity growing or shrinking?

Based on Fallenstein & Soares (2015) Vingean reflection concept, cited
in Wang's RSI paper. A self-improving system should reason about whether
its modifications improve its capacity for future improvement.

Boundary Principle: 100% deterministic. Graph queries + arithmetic only.
The vindication score is advisory — it never blocks patches.

See: docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md Section 10
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)

_W_COUPLING = 0.40
_W_BLAST = 0.35
_W_ENTROPY = 0.25

_VINDICATING_THRESHOLD = 0.2
_CONCERNING_THRESHOLD = -0.2
_WARNING_THRESHOLD = -0.5


@dataclass(frozen=True)
class VindicationResult:
    """Result of vindication reflection for a candidate patch."""
    vindication_score: float   # [-1, 1], positive = improving evolvability
    coupling_delta: float      # Negative = reducing coupling (good)
    blast_radius_delta: float  # Negative = reducing blast radius (good)
    entropy_delta: float       # Negative = reducing complexity (good)
    advisory: str              # "vindicating", "neutral", "concerning", "warning"


class VindicationReflector:
    """Forward-looking analysis of a patch's impact on future improvement capacity.

    Uses TheOracle to compare before/after coupling, blast radius, and
    complexity. Produces an advisory score but never blocks patches.
    """

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    def reflect(
        self,
        target_files: List[str],
        coupling_after: int,
        blast_radius_after: int,
        complexity_after: float,
        complexity_before: float,
    ) -> VindicationResult:
        """Compute vindication score for a candidate patch.

        Args:
            target_files: Files the candidate modifies.
            coupling_after: Estimated total coupling after patch.
            blast_radius_after: Estimated blast radius after patch.
            complexity_after: Average complexity after patch.
            complexity_before: Average complexity before patch.

        Returns:
            VindicationResult with score and advisory classification.
        """
        try:
            return self._compute(
                target_files, coupling_after, blast_radius_after,
                complexity_after, complexity_before,
            )
        except Exception:
            logger.debug("VindicationReflector: falling back to neutral", exc_info=True)
            return VindicationResult(
                vindication_score=0.0,
                coupling_delta=0.0,
                blast_radius_delta=0.0,
                entropy_delta=0.0,
                advisory="neutral",
            )

    def _compute(
        self,
        target_files: List[str],
        coupling_after: int,
        blast_radius_after: int,
        complexity_after: float,
        complexity_before: float,
    ) -> VindicationResult:
        # Measure before-state from Oracle
        coupling_before = 0
        br_before = 0
        for f in target_files:
            try:
                deps = len(self._oracle.get_dependencies(f))
                depts = len(self._oracle.get_dependents(f))
                coupling_before += deps + depts
            except Exception:
                pass
            try:
                br = self._oracle.compute_blast_radius(f)
                br_before = max(br_before, br.total_affected)
            except Exception:
                pass

        # Compute deltas (negative = improvement)
        if coupling_before > 0:
            coupling_delta = (coupling_after - coupling_before) / coupling_before
        else:
            coupling_delta = 0.0 if coupling_after == 0 else 1.0

        if br_before > 0:
            br_delta = (blast_radius_after - br_before) / br_before
        else:
            br_delta = 0.0 if blast_radius_after == 0 else 1.0

        if complexity_before > 0:
            entropy_delta = (complexity_after - complexity_before) / complexity_before
        else:
            entropy_delta = 0.0 if complexity_after == 0 else 1.0

        # Clamp deltas to [-1, 1]
        coupling_delta = max(-1.0, min(1.0, coupling_delta))
        br_delta = max(-1.0, min(1.0, br_delta))
        entropy_delta = max(-1.0, min(1.0, entropy_delta))

        # Vindication score: negative of weighted deltas
        # (negative delta = improvement, so negating makes positive = good)
        v_score = -1.0 * (
            _W_COUPLING * coupling_delta
            + _W_BLAST * br_delta
            + _W_ENTROPY * entropy_delta
        )
        v_score = round(max(-1.0, min(1.0, v_score)), 4)

        # Advisory classification
        if v_score > _VINDICATING_THRESHOLD:
            advisory = "vindicating"
        elif v_score < _WARNING_THRESHOLD:
            advisory = "warning"
        elif v_score < _CONCERNING_THRESHOLD:
            advisory = "concerning"
        else:
            advisory = "neutral"

        return VindicationResult(
            vindication_score=v_score,
            coupling_delta=round(coupling_delta, 4),
            blast_radius_delta=round(br_delta, 4),
            entropy_delta=round(entropy_delta, 4),
            advisory=advisory,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ouroboros_governance/test_vindication_reflector.py -v --timeout=15 -x`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/vindication_reflector.py tests/test_ouroboros_governance/test_vindication_reflector.py
git commit -m "feat(rsi): add VindicationReflector with trajectory analysis"
```

---

## Task 10: Add New Ledger States

**Files:**
- Modify: `backend/core/ouroboros/governance/ledger.py:43-72`

- [ ] **Step 1: Read the current OperationState enum**

Run: `python -m pytest tests/test_ouroboros_governance/ -k "ledger" -v --timeout=15 --co` (collect-only to see existing tests)

- [ ] **Step 2: Add the 4 new states to OperationState enum**

In `backend/core/ouroboros/governance/ledger.py`, add after the existing states in the `OperationState` enum (after the last entry before the closing of the enum):

```python
    # RSI Convergence Framework states (v0.2.0)
    SCORE_COMPUTED = "score_computed"
    CONVERGENCE_CHECKED = "convergence_checked"
    PRE_SCORED = "pre_scored"
    VINDICATION_CHECKED = "vindication_checked"
```

- [ ] **Step 3: Run existing ledger tests to verify no regressions**

Run: `python -m pytest tests/ -k "ledger" -v --timeout=30`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add backend/core/ouroboros/governance/ledger.py
git commit -m "feat(rsi): add SCORE_COMPUTED, CONVERGENCE_CHECKED, PRE_SCORED, VINDICATION_CHECKED ledger states"
```

---

## Task 11: Wire Composite Score into Orchestrator VERIFY Phase

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (VERIFY phase, ~lines 1851-1919, and _publish_outcome ~lines 1973-2090)

- [ ] **Step 1: Read the current VERIFY phase and _publish_outcome method**

Read `backend/core/ouroboros/governance/orchestrator.py` lines 1851-1919 and 1973-2090 to understand exact insertion points.

- [ ] **Step 2: Import and instantiate CompositeScoreFunction in orchestrator**

At the top of the GovernedOrchestrator `__init__` method or in a lazy property, add:

```python
# RSI Convergence: Composite Score
try:
    from backend.core.ouroboros.governance.composite_score import (
        CompositeScoreFunction, ScoreHistory,
    )
    self._score_function = CompositeScoreFunction()
    self._score_history = ScoreHistory()
except ImportError:
    self._score_function = None
    self._score_history = None
```

- [ ] **Step 3: Compute and record composite score after VERIFY phase**

After the benchmark/verify logic completes (around line 1919), add:

```python
# RSI Convergence: Compute composite score
if self._score_function is not None:
    try:
        from backend.core.ouroboros.governance.composite_score import CompositeScore
        score = self._score_function.compute(
            op_id=ctx.op_id,
            test_pass_rate_before=getattr(ctx, "test_pass_rate_before", 0.0),
            test_pass_rate_after=1.0 if ctx.validation_passed else 0.0,
            coverage_before=getattr(ctx, "coverage_before", 0.0),
            coverage_after=getattr(ctx, "coverage_after", 0.0),
            complexity_before=getattr(ctx, "complexity_before", 0.0),
            complexity_after=getattr(ctx, "complexity_after", 0.0),
            lint_before=getattr(ctx, "lint_before", 0),
            lint_after=getattr(ctx, "lint_after", 0),
            blast_radius_total=getattr(ctx, "blast_radius_total", 0),
        )
        if self._score_history is not None:
            self._score_history.record(score)
        # Record in ledger
        if hasattr(ctx, "op_id"):
            await self._stack.ledger.append(LedgerEntry(
                op_id=ctx.op_id,
                state=OperationState.SCORE_COMPUTED,
                data={"composite": score.composite, "components": {
                    "test": score.test_delta, "coverage": score.coverage_delta,
                    "complexity": score.complexity_delta, "lint": score.lint_delta,
                    "blast_radius": score.blast_radius,
                }},
            ))
    except Exception:
        logger.debug("RSI score computation failed", exc_info=True)
```

- [ ] **Step 4: Wire convergence check into _publish_outcome**

In `_publish_outcome()`, after the existing self-evolution recordings, add:

```python
# RSI Convergence: Check convergence state
if self._score_history is not None:
    try:
        from backend.core.ouroboros.governance.convergence_tracker import ConvergenceTracker
        tracker = ConvergenceTracker()
        composites = self._score_history.get_composite_values()
        if len(composites) >= 5:
            report = tracker.analyze(composites)
            logger.info("[RSI Convergence] State: %s, slope: %.4f, recommendation: %s",
                        report.state.value, report.slope, report.recommendation)
    except Exception:
        logger.debug("RSI convergence check failed", exc_info=True)
```

- [ ] **Step 5: Run the full test suite to verify no regressions**

Run: `python -m pytest tests/test_ouroboros_governance/ -v --timeout=60 -x --maxfail=5`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(rsi): wire composite score and convergence tracker into orchestrator"
```

---

## Task 12: Wire Oracle Pre-Scorer and Vindication Reflector into Orchestrator

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (GENERATE phase ~line 914, GATE phase ~line 1545)

- [ ] **Step 1: Read the current GENERATE and GATE phases**

Read `backend/core/ouroboros/governance/orchestrator.py` around lines 914-1078 (GENERATE) and 1545-1637 (GATE) to find exact insertion points.

- [ ] **Step 2: Wire OraclePreScorer into pre-GENERATE**

After candidate generation but before validation, add pre-scoring:

```python
# RSI Convergence: Oracle Pre-Score
try:
    from backend.core.ouroboros.governance.oracle_prescorer import OraclePreScorer
    if hasattr(self, "_oracle") and self._oracle is not None:
        prescorer = OraclePreScorer(oracle=self._oracle)
        pre_result = prescorer.score(
            target_files=list(ctx.target_files or ()),
            max_complexity=getattr(ctx, "max_complexity", 0),
            has_tests=any("test" in f for f in (ctx.target_files or ())),
        )
        logger.info("[RSI PreScore] score=%.3f gate=%s for %s",
                    pre_result.pre_score, pre_result.gate, ctx.op_id)
        if hasattr(ctx, "op_id"):
            await self._stack.ledger.append(LedgerEntry(
                op_id=ctx.op_id,
                state=OperationState.PRE_SCORED,
                data={"pre_score": pre_result.pre_score, "gate": pre_result.gate},
            ))
except Exception:
    logger.debug("RSI pre-scoring failed", exc_info=True)
```

- [ ] **Step 3: Wire VindicationReflector into GATE phase**

In the GATE phase, after security review but before APPROVE decision, add:

```python
# RSI Convergence: Vindication Reflection
try:
    from backend.core.ouroboros.governance.vindication_reflector import VindicationReflector
    if hasattr(self, "_oracle") and self._oracle is not None:
        reflector = VindicationReflector(oracle=self._oracle)
        v_result = reflector.reflect(
            target_files=list(ctx.target_files or ()),
            coupling_after=getattr(ctx, "coupling_after", 0),
            blast_radius_after=getattr(ctx, "blast_radius_after", 0),
            complexity_after=getattr(ctx, "complexity_after", 0.0),
            complexity_before=getattr(ctx, "complexity_before", 0.0),
        )
        logger.info("[RSI Vindication] score=%.3f advisory=%s for %s",
                    v_result.vindication_score, v_result.advisory, ctx.op_id)
        if v_result.advisory in ("concerning", "warning"):
            await self._narrate_if_available(
                f"Vindication check: {v_result.advisory}. "
                f"This patch may {'significantly ' if v_result.advisory == 'warning' else ''}"
                f"increase coupling or complexity."
            )
        if hasattr(ctx, "op_id"):
            await self._stack.ledger.append(LedgerEntry(
                op_id=ctx.op_id,
                state=OperationState.VINDICATION_CHECKED,
                data={
                    "vindication_score": v_result.vindication_score,
                    "advisory": v_result.advisory,
                    "coupling_delta": v_result.coupling_delta,
                    "blast_radius_delta": v_result.blast_radius_delta,
                    "entropy_delta": v_result.entropy_delta,
                },
            ))
except Exception:
    logger.debug("RSI vindication reflection failed", exc_info=True)
```

- [ ] **Step 4: Wire TransitionProbabilityTracker into _publish_outcome**

In `_publish_outcome()`, after self-evolution recordings, add:

```python
# RSI Convergence: Record technique outcomes
try:
    from backend.core.ouroboros.governance.transition_tracker import (
        TransitionProbabilityTracker, TechniqueOutcome,
    )
    tracker = TransitionProbabilityTracker()
    techniques_used = getattr(ctx, "techniques_applied", [])
    domain = getattr(ctx, "domain", "unknown")
    complexity = getattr(ctx, "task_complexity", "unknown")
    composite = getattr(ctx, "composite_score", 0.5)
    for tech in techniques_used:
        tracker.record(TechniqueOutcome(
            technique=tech, domain=domain, complexity=complexity,
            success=(final_state == OperationState.APPLIED),
            composite_score=composite, op_id=ctx.op_id,
        ))
except Exception:
    logger.debug("RSI transition tracking failed", exc_info=True)
```

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/test_ouroboros_governance/ -v --timeout=60 -x --maxfail=5`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(rsi): wire oracle pre-scorer, vindication reflector, and transition tracker into pipeline"
```

---

## Task 13: Full Integration Test

**Files:**
- Create: `tests/test_ouroboros_governance/test_rsi_convergence_integration.py`

- [ ] **Step 1: Write integration test that exercises the full RSI pipeline**

```python
"""Integration test: full RSI convergence pipeline end-to-end."""
from __future__ import annotations

import math
import pytest
import time
from unittest.mock import MagicMock
from dataclasses import dataclass


@dataclass
class MockBlastRadius:
    total_affected: int
    risk_level: str


def _mock_oracle():
    oracle = MagicMock()
    oracle.compute_blast_radius.return_value = MockBlastRadius(total_affected=5, risk_level="low")
    oracle.get_dependencies.return_value = [MagicMock()] * 3
    oracle.get_dependents.return_value = [MagicMock()] * 2
    return oracle


def test_full_rsi_pipeline(tmp_path):
    """Simulate 20 operations and verify convergence detection."""
    from backend.core.ouroboros.governance.composite_score import (
        CompositeScoreFunction, ScoreHistory,
    )
    from backend.core.ouroboros.governance.convergence_tracker import (
        ConvergenceTracker, ConvergenceState,
    )
    from backend.core.ouroboros.governance.transition_tracker import (
        TransitionProbabilityTracker, TechniqueOutcome,
    )
    from backend.core.ouroboros.governance.oracle_prescorer import OraclePreScorer
    from backend.core.ouroboros.governance.vindication_reflector import VindicationReflector

    csf = CompositeScoreFunction()
    history = ScoreHistory(persistence_dir=tmp_path)
    tracker = ConvergenceTracker()
    trans = TransitionProbabilityTracker(persistence_dir=tmp_path)
    oracle = _mock_oracle()
    prescorer = OraclePreScorer(oracle=oracle)
    reflector = VindicationReflector(oracle=oracle)

    # Simulate 20 improving operations
    for i in range(20):
        # Quality improves over time
        test_after = min(1.0, 0.7 + i * 0.015)
        coverage_after = min(1.0, 0.5 + i * 0.025)
        complexity_after = max(3.0, 15.0 - i * 0.5)

        score = csf.compute(
            op_id=f"op-integration-{i}",
            test_pass_rate_before=0.7, test_pass_rate_after=test_after,
            coverage_before=0.5, coverage_after=coverage_after,
            complexity_before=15.0, complexity_after=complexity_after,
            lint_before=5, lint_after=max(0, 5 - i // 4),
            blast_radius_total=max(1, 10 - i // 2),
        )
        history.record(score)

        # Record technique outcome
        trans.record(TechniqueOutcome(
            technique="module_mutation", domain="backend", complexity="heavy_code",
            success=(i % 5 != 0), composite_score=score.composite,
            op_id=f"op-integration-{i}", timestamp=time.time(),
        ))

    # Verify convergence detected
    composites = history.get_composite_values()
    assert len(composites) == 20
    report = tracker.analyze(composites)
    assert report.state in (
        ConvergenceState.IMPROVING,
        ConvergenceState.LOGARITHMIC,
    )
    assert report.slope < 0  # Scores should be decreasing

    # Verify transition probabilities
    p = trans.get_probability("module_mutation", "backend", "heavy_code")
    assert p > 0.5  # 16/20 successes

    # Verify pre-scoring works
    pre = prescorer.score(
        target_files=["backend/core/module.py"],
        max_complexity=8, has_tests=True,
    )
    assert pre.gate in ("FAST_TRACK", "NORMAL", "WARN")

    # Verify vindication works
    v = reflector.reflect(
        target_files=["backend/core/module.py"],
        coupling_after=4, blast_radius_after=4,
        complexity_after=5.0, complexity_before=10.0,
    )
    assert v.vindication_score > 0  # Improving trajectory


@pytest.mark.asyncio
async def test_adaptive_graduation_with_diverse_goals(tmp_path):
    """Verify adaptive threshold fires correctly with diverse goals."""
    from backend.core.ouroboros.governance.graduation_orchestrator import (
        EphemeralUsageTracker, compute_adaptive_threshold,
    )
    tracker = EphemeralUsageTracker(persistence_path=tmp_path / "usage.json")

    # 5 diverse successes — should graduate
    goals = [
        "analyze network traffic patterns",
        "detect anomalous login attempts",
        "classify security events by severity",
        "extract IOCs from log entries",
        "correlate alerts across data sources",
    ]
    fired = None
    for goal in goals:
        result = await tracker.record_usage(
            goal=goal, code_hash="sec-tool-v1",
            outcome="success", elapsed_s=2.0,
        )
        if result is not None:
            fired = result

    assert fired is not None, "Should have graduated with 5 diverse successes"


def test_score_history_persistence_roundtrip(tmp_path):
    """Verify scores survive persistence roundtrip."""
    from backend.core.ouroboros.governance.composite_score import (
        CompositeScoreFunction, ScoreHistory,
    )
    csf = CompositeScoreFunction()
    h1 = ScoreHistory(persistence_dir=tmp_path)
    for i in range(5):
        h1.record(csf.compute(
            op_id=f"op-rt-{i}",
            test_pass_rate_before=0.8, test_pass_rate_after=0.9,
            coverage_before=0.7, coverage_after=0.75,
            complexity_before=10, complexity_after=9,
            lint_before=3, lint_after=2, blast_radius_total=5,
        ))

    h2 = ScoreHistory(persistence_dir=tmp_path)
    assert len(h2.get_recent(10)) == 5
    assert h2.get_composite_values() == h1.get_composite_values()
```

- [ ] **Step 2: Run integration test**

Run: `python -m pytest tests/test_ouroboros_governance/test_rsi_convergence_integration.py -v --timeout=30 -x`
Expected: All 3 tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_ouroboros_governance/test_rsi_convergence_integration.py
git commit -m "test(rsi): add full RSI convergence pipeline integration tests"
```

---

## Task 14: Run Full Test Suite and Final Verification

- [ ] **Step 1: Run ALL RSI convergence tests**

Run: `python -m pytest tests/test_ouroboros_governance/test_composite_score.py tests/test_ouroboros_governance/test_convergence_tracker.py tests/test_ouroboros_governance/test_adaptive_graduation.py tests/test_ouroboros_governance/test_oracle_prescorer.py tests/test_ouroboros_governance/test_transition_tracker.py tests/test_ouroboros_governance/test_vindication_reflector.py tests/test_ouroboros_governance/test_rsi_convergence_integration.py -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 2: Run broader governance tests to verify no regressions**

Run: `python -m pytest tests/test_ouroboros_governance/ -v --timeout=120 --maxfail=10`
Expected: No new failures

- [ ] **Step 3: Final commit with all files**

```bash
git add -A
git status
git commit -m "feat(rsi): complete RSI Convergence Framework — 6 improvements

Implements Wang's RSI formulation for Ouroboros:
1. CompositeScoreFunction — unified quality metric (5 sub-scores)
2. ConvergenceTracker — logarithmic fit, plateau/oscillation detection
3. AdaptiveGraduationThreshold — Bayesian estimation with diversity bonus
4. OraclePreScorer — fast graph-based quality gate
5. TransitionProbabilityTracker — empirical technique success rates
6. VindicationReflector — forward-looking trajectory analysis

All components are 100% deterministic (no LLM calls).
See docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md for theory."
```
