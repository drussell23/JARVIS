"""Phase 4 P3 Slice 1 — Cognitive metrics wrapper (un-strands the two RSI modules).

Per OUROBOROS_VENOM_PRD.md §9 Phase 4 P3:

  > Phase 4 — Cognitive Metrics. The oracle_prescorer + vindication_reflector
  > modules already exist + are tested per Phase 0 audit, but are currently
  > STRANDED — only test files import them. This slice wires them into a
  > singleton wrapper with an audit ledger + operator REPL surface so the
  > pipeline (Slice 2) can consume them as advisory signals.

The two underlying modules implement complementary halves of the Wang RSI
convergence framework:

* **OraclePreScorer** — pre-APPLY scoring of a candidate (target_files,
  complexity, has_tests). Returns ``PreScoreResult`` with a
  ``pre_score`` ∈ [0,1] + a 3-tier ``gate`` (FAST_TRACK / NORMAL / WARN).
  Higher score = higher inherent risk. The orchestrator's existing
  Iron Gate / risk_tier_floor stack remains authoritative; this is
  observational input that future cognitive metrics can weight.
* **VindicationReflector** — post-APPLY forward-looking score
  (vindication_score ∈ [-1, +1]) measuring whether a patch improves
  future tractability of the codebase (negative coupling/blast/entropy
  delta = positive vindication).

Both modules already accept any oracle satisfying the
``compute_blast_radius / get_dependencies / get_dependents`` shape;
``oracle.CodebaseKnowledgeGraph`` is the production implementation.

This wrapper:

  1. Provides default-singleton accessors so the orchestrator (Slice 2)
     and the REPL can share one CognitiveMetricsService per process.
  2. Records every emitted ``PreScoreResult`` / ``VindicationResult``
     to a JSONL audit ledger at
     ``.jarvis/cognitive_metrics.jsonl`` (schema_version
     ``cognitive_metrics.1``).
  3. Computes simple aggregate ``stats()`` (counts + means) for
     observability surfaces (REPL, future summary.json wiring).
  4. Stays **authority-free**: no orchestrator / policy / iron_gate /
     risk_tier / change_engine / candidate_generator / gate /
     semantic_guardian imports.

Default-off behind ``JARVIS_COGNITIVE_METRICS_ENABLED`` until the
graduation slice flips it. When off, ``score_pre_apply`` and
``reflect_post_apply`` return the underlying module's neutral fallback
without touching the ledger — byte-for-byte pre-Slice-1 behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.oracle_prescorer import (
    OraclePreScorer,
    PreScoreResult,
)
from backend.core.ouroboros.governance.vindication_reflector import (
    VindicationReflector,
    VindicationResult,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")

# Schema version frozen for the JSONL on-disk format. Future bumps need
# additive migration semantics + this constant + the source-grep pin
# updated together.
COGNITIVE_METRICS_SCHEMA_VERSION: str = "cognitive_metrics.1"

DEFAULT_LEDGER_FILENAME: str = "cognitive_metrics.jsonl"


def is_enabled() -> bool:
    """Master flag — ``JARVIS_COGNITIVE_METRICS_ENABLED`` (default ``false``).

    Slice 1 ships default-off. Slice 2 graduation flips it after the
    orchestrator integration lands + the comprehensive pin suite +
    in-process live-fire smoke + reachability supplement complete."""
    return os.environ.get(
        "JARVIS_COGNITIVE_METRICS_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Ledger record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CognitiveMetricRecord:
    """One ledger row — a snapshot of either a pre-score or a vindication
    reflection event tied to an op_id."""

    schema_version: str
    op_id: str
    kind: str              # "pre_score" | "vindication"
    target_files: Tuple[str, ...]
    pre_score: Optional[float] = None
    pre_score_gate: Optional[str] = None
    vindication_score: Optional[float] = None
    vindication_advisory: Optional[str] = None
    timestamp_unix: float = 0.0
    # Compact subsignal block — keeps the ledger row self-contained
    # without requiring a second lookup against the underlying result.
    subsignals: Optional[Dict[str, float]] = None

    def to_ledger_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["target_files"] = list(self.target_files)
        return d


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CognitiveMetricsService:
    """Wrap OraclePreScorer + VindicationReflector under a single service
    with shared oracle + audit ledger.

    Parameters
    ----------
    oracle:
        Object satisfying the prescorer/reflector oracle interface
        (``compute_blast_radius`` / ``get_dependencies`` / ``get_dependents``).
        ``oracle.CodebaseKnowledgeGraph`` is the production implementation.
    project_root:
        Repo root (used to locate the JSONL ledger directory).
    ledger_path:
        Optional explicit override.
    """

    def __init__(
        self,
        oracle: Any,
        project_root: Path,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self._oracle = oracle
        self._root = Path(project_root).resolve()
        self._ledger_path = (
            Path(ledger_path).resolve()
            if ledger_path is not None
            else self._root / ".jarvis" / DEFAULT_LEDGER_FILENAME
        )
        self._prescorer = OraclePreScorer(oracle)
        self._reflector = VindicationReflector(oracle)
        self._lock = threading.Lock()

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    # ---- public API ----

    def score_pre_apply(
        self,
        op_id: str,
        target_files: List[str],
        max_complexity: int = 0,
        has_tests: bool = True,
    ) -> PreScoreResult:
        """Compute pre-score for a candidate. Always returns a
        ``PreScoreResult`` (neutral on any failure). When master flag
        is on, also persists a ``CognitiveMetricRecord`` to the ledger."""
        result = self._prescorer.score(
            target_files=target_files,
            max_complexity=max_complexity,
            has_tests=has_tests,
        )
        if is_enabled():
            self._persist(self._record_from_pre_score(
                op_id=op_id,
                target_files=tuple(target_files),
                result=result,
            ))
            logger.info(
                "[CognitiveMetrics] op=%s pre_score=%.3f gate=%s "
                "files=%d (Phase 4 P3)",
                op_id, result.pre_score, result.gate, len(target_files),
            )
        return result

    def reflect_post_apply(
        self,
        op_id: str,
        target_files: List[str],
        coupling_after: float,
        blast_radius_after: float,
        complexity_after: float,
        complexity_before: float,
    ) -> VindicationResult:
        """Compute vindication score for a completed patch. Always returns
        a ``VindicationResult`` (neutral on any failure). When master flag
        is on, also persists a ``CognitiveMetricRecord``."""
        result = self._reflector.reflect(
            target_files=target_files,
            coupling_after=coupling_after,
            blast_radius_after=blast_radius_after,
            complexity_after=complexity_after,
            complexity_before=complexity_before,
        )
        if is_enabled():
            self._persist(self._record_from_vindication(
                op_id=op_id,
                target_files=tuple(target_files),
                result=result,
            ))
            logger.info(
                "[CognitiveMetrics] op=%s vindication_score=%.3f "
                "advisory=%s files=%d (Phase 4 P3)",
                op_id, result.vindication_score, result.advisory,
                len(target_files),
            )
        return result

    def load_records(self) -> List[CognitiveMetricRecord]:
        """Read all ledger rows. Tolerates malformed lines + missing file."""
        if not self._ledger_path.exists():
            return []
        try:
            text = self._ledger_path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[CognitiveMetricRecord] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            kind = str(d.get("kind", "")).strip()
            if kind not in ("pre_score", "vindication"):
                continue
            out.append(self._record_from_dict(d))
        return out

    def stats(self) -> Dict[str, Any]:
        """Aggregate stats for observability surfaces.

        Returns a dict with:
          * ``total`` — total ledger rows
          * ``pre_score_count`` / ``vindication_count``
          * ``mean_pre_score`` (or None when no rows)
          * ``mean_vindication_score`` (or None)
          * ``gate_counts`` — {FAST_TRACK, NORMAL, WARN}
          * ``advisory_counts`` — {vindicating, neutral, concerning, warning}
        """
        records = self.load_records()
        pre_scores = [
            r.pre_score for r in records
            if r.kind == "pre_score" and r.pre_score is not None
        ]
        vinds = [
            r.vindication_score for r in records
            if r.kind == "vindication" and r.vindication_score is not None
        ]
        gate_counts: Dict[str, int] = {}
        for r in records:
            if r.kind == "pre_score" and r.pre_score_gate:
                gate_counts[r.pre_score_gate] = (
                    gate_counts.get(r.pre_score_gate, 0) + 1
                )
        advisory_counts: Dict[str, int] = {}
        for r in records:
            if r.kind == "vindication" and r.vindication_advisory:
                advisory_counts[r.vindication_advisory] = (
                    advisory_counts.get(r.vindication_advisory, 0) + 1
                )
        return {
            "total": len(records),
            "pre_score_count": len(pre_scores),
            "vindication_count": len(vinds),
            "mean_pre_score": (
                sum(pre_scores) / len(pre_scores) if pre_scores else None
            ),
            "mean_vindication_score": (
                sum(vinds) / len(vinds) if vinds else None
            ),
            "gate_counts": gate_counts,
            "advisory_counts": advisory_counts,
        }

    # ---- internals ----

    def _persist(self, record: CognitiveMetricRecord) -> bool:
        """Best-effort JSONL append. Never raises."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_ledger_dict()) + "\n")
            return True
        except OSError:
            logger.debug(
                "[CognitiveMetrics] persist failed: %s", self._ledger_path,
                exc_info=True,
            )
            return False

    @staticmethod
    def _record_from_pre_score(
        op_id: str,
        target_files: Tuple[str, ...],
        result: PreScoreResult,
    ) -> CognitiveMetricRecord:
        return CognitiveMetricRecord(
            schema_version=COGNITIVE_METRICS_SCHEMA_VERSION,
            op_id=op_id,
            kind="pre_score",
            target_files=target_files,
            pre_score=result.pre_score,
            pre_score_gate=result.gate,
            timestamp_unix=time.time(),
            subsignals={
                "blast_radius": result.blast_radius_signal,
                "coupling": result.coupling_signal,
                "complexity": result.complexity_signal,
                "test_coverage": result.test_coverage_signal,
                "locality": result.locality_signal,
            },
        )

    @staticmethod
    def _record_from_vindication(
        op_id: str,
        target_files: Tuple[str, ...],
        result: VindicationResult,
    ) -> CognitiveMetricRecord:
        return CognitiveMetricRecord(
            schema_version=COGNITIVE_METRICS_SCHEMA_VERSION,
            op_id=op_id,
            kind="vindication",
            target_files=target_files,
            vindication_score=result.vindication_score,
            vindication_advisory=result.advisory,
            timestamp_unix=time.time(),
            subsignals={
                "coupling_delta": result.coupling_delta,
                "blast_radius_delta": result.blast_radius_delta,
                "entropy_delta": result.entropy_delta,
            },
        )

    @staticmethod
    def _record_from_dict(d: Dict[str, Any]) -> CognitiveMetricRecord:
        return CognitiveMetricRecord(
            schema_version=str(
                d.get("schema_version", COGNITIVE_METRICS_SCHEMA_VERSION)
            ),
            op_id=str(d.get("op_id", "")),
            kind=str(d.get("kind", "")),
            target_files=tuple(d.get("target_files", []) or []),
            pre_score=(
                float(d["pre_score"])
                if d.get("pre_score") is not None else None
            ),
            pre_score_gate=(
                str(d.get("pre_score_gate"))
                if d.get("pre_score_gate") is not None else None
            ),
            vindication_score=(
                float(d["vindication_score"])
                if d.get("vindication_score") is not None else None
            ),
            vindication_advisory=(
                str(d.get("vindication_advisory"))
                if d.get("vindication_advisory") is not None else None
            ),
            timestamp_unix=float(d.get("timestamp_unix", 0.0) or 0.0),
            subsignals=d.get("subsignals") if isinstance(
                d.get("subsignals"), dict
            ) else None,
        )


# ---------------------------------------------------------------------------
# Default-singleton accessor (mirrors the PostmortemRecallService /
# SelfGoalFormationEngine / HypothesisLedger pattern)
# ---------------------------------------------------------------------------


_default_service: Optional[CognitiveMetricsService] = None
_default_lock = threading.Lock()


def get_default_service(
    oracle: Optional[Any] = None,
    project_root: Optional[Path] = None,
) -> Optional[CognitiveMetricsService]:
    """Return the process-wide ``CognitiveMetricsService``.

    Lazily constructs on first call. Returns ``None`` when:
      * the master flag is off (operator hot-revert), OR
      * the singleton is uninitialised AND no ``oracle`` was supplied
        on this call.

    Slice 2 will wire boot-time singleton creation in the orchestrator,
    so callers in the production path will get a working instance once
    the master flag is on."""
    if not is_enabled():
        return None
    global _default_service
    with _default_lock:
        if _default_service is None:
            if oracle is None:
                return None
            root = Path(project_root) if project_root else Path.cwd()
            _default_service = CognitiveMetricsService(
                oracle=oracle, project_root=root,
            )
    return _default_service


def set_default_service(service: Optional[CognitiveMetricsService]) -> None:
    """Operator-controlled singleton injection. Used by orchestrator boot
    in Slice 2 + by tests to install a stub service."""
    global _default_service
    with _default_lock:
        _default_service = service


def reset_default_service() -> None:
    """Reset the singleton — for tests + config reload."""
    global _default_service
    with _default_lock:
        _default_service = None


__all__ = [
    "COGNITIVE_METRICS_SCHEMA_VERSION",
    "DEFAULT_LEDGER_FILENAME",
    "CognitiveMetricRecord",
    "CognitiveMetricsService",
    "get_default_service",
    "is_enabled",
    "reset_default_service",
    "set_default_service",
]
