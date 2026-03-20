"""backend/core/ouroboros/consciousness/prophecy_engine.py

ProphecyEngine — Heuristic Failure-Risk Predictor
===================================================

Analyses a proposed changeset (list of modified file paths + optional diff
summary) and produces a ProphecyReport: per-file risk scores, a ranked list
of predicted test failures, an overall risk level, and a confidence score.

Design:
    - Heuristic-only in v1: MemoryEngine reputation data + Oracle dependency
      graph edge count + a fixed baseline contribution.  Confidence is capped
      at 0.6 until a J-Prime LLM enhancement pass is added (future work).
    - asyncio.Lock prevents duplicate concurrent analyses from racing.
    - DreamEngine and ProphecyEngine share no mutable state so they can run
      simultaneously without coordination (TC34).
    - on_high_risk_callback fires when risk_level is "high" or "critical".
      It is called inside the lock so callers must not re-enter analyze_change.
    - Oracle is optional.  When unavailable, dependency scores default to 0.
    - All errors from Oracle.get_file_neighborhood are caught; the engine
      degrades gracefully rather than crashing (TC10-style guard).

Risk scoring formula per file (TC15):
    score = (1 - success_rate) * 0.3
          + min(fragility_score, 1.0) * 0.3
          + min(dependent_count / 20.0, 1.0) * 0.2
          + 0.1   # baseline

Risk level thresholds:
    < 0.3  -> "low"
    < 0.6  -> "medium"
    < 0.8  -> "high"
    >= 0.8 -> "critical"

Confidence cap (TC16):
    Without J-Prime: max 0.6.
    With J-Prime (future): up to 1.0.

Concurrency (TC34):
    Single asyncio.Lock guards analyze_change; get_risk_scores is read-only.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.consciousness.types import (
    PredictedFailure,
    ProphecyReport,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RISK_WEIGHT_FAILURE_RATE: float = 0.3
_RISK_WEIGHT_FRAGILITY: float = 0.3
_RISK_WEIGHT_DEPENDENTS: float = 0.2
_RISK_BASELINE: float = 0.1

_DEPENDENTS_NORMALISER: float = 20.0  # 20 dependents -> max dependency score

_CONFIDENCE_CAP_HEURISTIC: float = 0.6   # no J-Prime
_CONFIDENCE_CAP_JPRIME: float = 1.0      # reserved for future LLM pass

_RISK_LEVEL_THRESHOLDS: Tuple[Tuple[float, str], ...] = (
    (0.8, "critical"),
    (0.6, "high"),
    (0.3, "medium"),
    (0.0, "low"),
)

_HIGH_RISK_LEVELS = frozenset({"high", "critical"})


# ---------------------------------------------------------------------------
# ProphecyEngine
# ---------------------------------------------------------------------------


class ProphecyEngine:
    """Heuristic failure-risk predictor for proposed changesets.

    Parameters
    ----------
    memory_engine:
        Any object with ``get_file_reputation(file_path: str) -> FileReputation``.
        Typically a MemoryEngine instance.
    oracle:
        Optional.  Any object with
        ``get_file_neighborhood(paths: List[Path]) -> neighborhood``
        where ``neighborhood`` has an ``edges`` attribute (list/sequence).
    on_high_risk_callback:
        Optional async or sync callable invoked with the ProphecyReport when
        risk_level is "high" or "critical".  Called inside the analysis lock.
    """

    def __init__(
        self,
        memory_engine: Any,
        oracle: Any = None,
        on_high_risk_callback: Optional[Callable] = None,
    ) -> None:
        self._memory = memory_engine
        self._oracle = oracle
        self._on_high_risk = on_high_risk_callback
        self._lock = asyncio.Lock()
        # Cache from the most recent analyze_change call: file_path -> score
        self._last_risk_scores: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle (no-op — ready immediately)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """No-op lifecycle hook.  ProphecyEngine is ready immediately."""
        logger.debug("ProphecyEngine: started (heuristic-only mode, confidence cap %.1f)", _CONFIDENCE_CAP_HEURISTIC)

    async def stop(self) -> None:
        """No-op lifecycle hook."""
        logger.debug("ProphecyEngine: stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_change(
        self,
        files_changed: List[str],
        diff_summary: str = "",
    ) -> ProphecyReport:
        """Analyse a proposed changeset and return a ProphecyReport.

        Parameters
        ----------
        files_changed:
            Repo-relative paths of files modified by the proposed change.
        diff_summary:
            Optional free-text summary of the diff (reserved for future
            J-Prime LLM pass; not used by heuristic scoring in v1).

        Returns
        -------
        ProphecyReport
            Always returned even for empty changesets (risk_level="low").
        """
        async with self._lock:
            return await self._run_analysis(files_changed, diff_summary)

    def get_risk_scores(self) -> Dict[str, float]:
        """Return cached per-file risk scores from the last analyze_change call.

        Returns an empty dict if analyze_change has never been called.
        The returned dict is a snapshot — mutations do not affect internal state.
        """
        return dict(self._last_risk_scores)

    # ------------------------------------------------------------------
    # Internal: core analysis pipeline
    # ------------------------------------------------------------------

    async def _run_analysis(
        self,
        files_changed: List[str],
        diff_summary: str,
    ) -> ProphecyReport:
        """Run heuristic analysis.  Called under self._lock."""
        if not files_changed:
            report = ProphecyReport(
                change_id=_make_change_id(files_changed, diff_summary),
                risk_level="low",
                predicted_failures=(),
                confidence=_CONFIDENCE_CAP_HEURISTIC,
                reasoning="No files changed — no risk predicted.",
                recommended_tests=(),
            )
            self._last_risk_scores = {}
            return report

        # Score each changed file
        risk_scores: Dict[str, float] = {}
        for file_path in files_changed:
            risk_scores[file_path] = self._heuristic_risk(file_path)

        self._last_risk_scores = dict(risk_scores)

        # Aggregate: use the maximum file score as the changeset score
        max_score = max(risk_scores.values())
        risk_level = _score_to_level(max_score)

        # Build predicted failures from files above a low threshold
        predicted_failures = self._build_predicted_failures(risk_scores)

        # Collect recommended tests (files whose names contain "test")
        recommended_tests = _infer_recommended_tests(files_changed, predicted_failures)

        # Confidence is capped at heuristic level (no J-Prime)
        # Use average score as confidence proxy, capped at the cap
        avg_score = sum(risk_scores.values()) / len(risk_scores)
        confidence = min(avg_score, _CONFIDENCE_CAP_HEURISTIC)
        # Ensure minimum meaningful confidence when there ARE files
        confidence = max(confidence, 0.2)

        reasoning = _build_reasoning(files_changed, risk_scores, risk_level, diff_summary)

        report = ProphecyReport(
            change_id=_make_change_id(files_changed, diff_summary),
            risk_level=risk_level,
            predicted_failures=tuple(predicted_failures),
            confidence=confidence,
            reasoning=reasoning,
            recommended_tests=tuple(recommended_tests),
        )

        logger.info(
            "ProphecyEngine: analysed %d file(s) -> risk=%s confidence=%.2f",
            len(files_changed),
            risk_level,
            confidence,
        )

        # Fire callback for high/critical risk (TC20)
        if risk_level in _HIGH_RISK_LEVELS and self._on_high_risk is not None:
            try:
                result = self._on_high_risk(report)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("ProphecyEngine: on_high_risk_callback raised: %s", exc)

        return report

    # ------------------------------------------------------------------
    # Internal: heuristic scoring
    # ------------------------------------------------------------------

    def _heuristic_risk(self, file_path: str) -> float:
        """Compute a 0.0–1.0 heuristic risk score for a single file (TC15).

        Formula:
            score  = (1 - success_rate)   * 0.3   # historical failure rate
                   + min(fragility, 1.0)  * 0.3   # fragility
                   + min(deps / 20, 1.0)  * 0.2   # dependency fan-out
                   + 0.1                           # baseline
        """
        score = 0.0

        # --- Memory reputation contribution ---
        rep = None
        try:
            rep = self._memory.get_file_reputation(file_path)
        except Exception as exc:
            logger.debug("ProphecyEngine: memory lookup failed for %s: %s", file_path, exc)

        if rep is not None:
            score += (1.0 - rep.success_rate) * _RISK_WEIGHT_FAILURE_RATE
            score += min(rep.fragility_score, 1.0) * _RISK_WEIGHT_FRAGILITY

        # --- Oracle dependency contribution ---
        if self._oracle is not None and hasattr(self._oracle, "get_file_neighborhood"):
            try:
                neighborhood = self._oracle.get_file_neighborhood([Path(file_path)])
                dependents = len(getattr(neighborhood, "edges", []))
                score += min(dependents / _DEPENDENTS_NORMALISER, 1.0) * _RISK_WEIGHT_DEPENDENTS
            except Exception as exc:
                logger.debug(
                    "ProphecyEngine: oracle neighborhood failed for %s: %s", file_path, exc
                )

        # --- Baseline ---
        score += _RISK_BASELINE

        return min(score, 1.0)

    # ------------------------------------------------------------------
    # Internal: predicted failures
    # ------------------------------------------------------------------

    def _build_predicted_failures(
        self, risk_scores: Dict[str, float]
    ) -> List[PredictedFailure]:
        """Build PredictedFailure entries for files with elevated risk.

        Files with score < 0.3 are below the "low" threshold and are not
        individually predicted to fail; they only affect aggregate risk.
        Sorted by probability descending.
        """
        failures: List[PredictedFailure] = []
        for file_path, score in risk_scores.items():
            if score < 0.3:
                continue
            # Derive a plausible test path
            test_file = _derive_test_path(file_path)
            reason = _describe_risk_reason(score)
            evidence = _build_evidence_text(file_path, score)
            failures.append(
                PredictedFailure(
                    test_file=test_file,
                    probability=round(min(score, 1.0), 4),
                    reason=reason,
                    evidence=evidence,
                )
            )
        failures.sort(key=lambda f: f.probability, reverse=True)
        return failures


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions — no side effects)
# ---------------------------------------------------------------------------


def _score_to_level(score: float) -> str:
    """Map a 0–1 risk score to a categorical risk level."""
    for threshold, level in _RISK_LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return "low"


def _make_change_id(files_changed: List[str], diff_summary: str) -> str:
    """Derive a short, deterministic change identifier."""
    import hashlib
    payload = "|".join(sorted(files_changed)) + ":" + diff_summary[:128]
    return "chg-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def _derive_test_path(file_path: str) -> str:
    """Heuristically derive the most likely test path for a source file.

    Examples:
        backend/core/foo.py         -> tests/test_foo.py
        backend/core/bar/baz.py     -> tests/test_baz.py
        tests/test_already.py       -> tests/test_already.py
    """
    p = Path(file_path)
    if "test" in p.name.lower():
        return file_path
    stem = p.stem
    return f"tests/test_{stem}.py"


def _describe_risk_reason(score: float) -> str:
    """Human-readable reason string matching the risk level."""
    level = _score_to_level(score)
    if level == "critical":
        return "Critical risk: high historical failure rate and fragility"
    if level == "high":
        return "High risk: elevated fragility or many dependents"
    if level == "medium":
        return "Medium risk: moderate historical instability"
    return "Low risk: historically stable file"


def _build_evidence_text(file_path: str, score: float) -> str:
    """Compact evidence string for a predicted failure."""
    return f"heuristic_score={score:.3f} file={file_path}"


def _infer_recommended_tests(
    files_changed: List[str],
    predicted_failures: List[PredictedFailure],
) -> List[str]:
    """Union of predicted test paths and directly changed test files."""
    tests: List[str] = []
    seen = set()
    for f in predicted_failures:
        if f.test_file not in seen:
            seen.add(f.test_file)
            tests.append(f.test_file)
    for f in files_changed:
        if "test" in Path(f).name.lower() and f not in seen:
            seen.add(f)
            tests.append(f)
    return tests


def _build_reasoning(
    files_changed: List[str],
    risk_scores: Dict[str, float],
    risk_level: str,
    diff_summary: str,
) -> str:
    """Narrative reasoning summary for the ProphecyReport."""
    n = len(files_changed)
    max_score = max(risk_scores.values()) if risk_scores else 0.0
    top_file = max(risk_scores, key=lambda k: risk_scores[k]) if risk_scores else "n/a"
    parts = [
        f"Analysed {n} file(s) using heuristic scoring (v1, no J-Prime).",
        f"Overall risk: {risk_level} (max score {max_score:.3f} on '{top_file}').",
    ]
    if diff_summary:
        parts.append(f"Diff context: {diff_summary[:200]}")
    parts.append(
        "Confidence capped at 0.60 — heuristic-only analysis without LLM enhancement."
    )
    return " ".join(parts)
