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
    """Master flag — ``JARVIS_COGNITIVE_METRICS_ENABLED`` (default ``true``).

    GRADUATED 2026-04-26 (Slice 2). Default: **``true``** post-graduation.
    Layered evidence on the graduation PR:
      * Slice 1 — wrapper + REPL + ledger (43 tests, authority-pinned)
      * Slice 2 — orchestrator integration (boot-time singleton wiring +
        CONTEXT_EXPANSION pre-score call site) + comprehensive pin suite
        + in-process live-fire smoke + reachability supplement
      * Underlying RSI modules — 131/131 tests green per Phase 0 audit

    Hot-revert: ``export JARVIS_COGNITIVE_METRICS_ENABLED=false`` →
    helper short-circuits, no ledger writes, behavior byte-for-byte
    identical to pre-graduation. Both wrapped methods continue to
    return their underlying module's neutral fallback even when the
    flag is on but the oracle fails — fail-safe by construction."""
    return os.environ.get(
        "JARVIS_COGNITIVE_METRICS_ENABLED", "true",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Ledger record
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Oracle snapshot — used by the post-APPLY vindication call site to
# capture before-values at CONTEXT_EXPANSION (next to the pre-score call)
# and read them back at APPLY-success time.
# ---------------------------------------------------------------------------


# Hard cap on the in-process snapshot cache so a long-running session
# can't accumulate unbounded snapshots from ops that never reach APPLY.
# Pinned by tests so behaviour stays reviewable.
_SNAPSHOT_CACHE_MAX: int = 256


@dataclass(frozen=True)
class OracleSnapshot:
    """Pre-APPLY oracle state for a target_files set.

    Captured at CONTEXT_EXPANSION (alongside ``score_pre_apply``) and
    consumed by ``reflect_post_apply`` to compute before/after deltas.

    Attributes
    ----------
    coupling_total:
        Sum of ``len(get_dependencies) + len(get_dependents)`` across
        all target_files. Float so the reflector's division math is
        consistent.
    blast_max:
        Max ``compute_blast_radius(file).total_affected`` across target
        files. Float for consistency.
    complexity_estimate:
        Conservative complexity proxy (number of target files for v1 —
        future slices can wire a real cyclomatic complexity probe).
        Captured before-values lets the reflector compute entropy_delta
        even when the post-state probe is unavailable.
    """

    coupling_total: float
    blast_max: float
    complexity_estimate: float


def snapshot_oracle_state(
    oracle: Any, target_files: List[str],
) -> Optional[OracleSnapshot]:
    """Best-effort oracle state snapshot for a target_files set.

    Returns ``None`` on any oracle failure — caller treats absence as
    "no before-snapshot available" and falls back to the reflector's
    neutral path. Never raises.
    """
    if not target_files or oracle is None:
        return None
    try:
        coupling_total = 0.0
        blast_max = 0.0
        for f in target_files:
            try:
                deps = oracle.get_dependencies(f)
                dependents = oracle.get_dependents(f)
                coupling_total += float(len(deps) + len(dependents))
            except Exception:
                # Per-file failure: skip; partial snapshot is better
                # than none for the average-case math.
                continue
            try:
                br = oracle.compute_blast_radius(f)
                blast_max = max(blast_max, float(br.total_affected))
            except Exception:
                continue
        return OracleSnapshot(
            coupling_total=coupling_total,
            blast_max=blast_max,
            complexity_estimate=float(len(target_files)),
        )
    except Exception:
        return None


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
        # Per-process cache: op_id → OracleSnapshot captured at
        # CONTEXT_EXPANSION (next to score_pre_apply). Read by
        # reflect_post_apply at APPLY-success to compute before/after
        # deltas. Bounded by _SNAPSHOT_CACHE_MAX (FIFO eviction) so a
        # long-running session can't accumulate snapshots from ops
        # that never reached APPLY.
        self._pre_apply_snapshots: Dict[str, OracleSnapshot] = {}

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
        is on, also persists a ``CognitiveMetricRecord`` to the ledger
        AND captures an ``OracleSnapshot`` keyed on ``op_id`` for the
        post-APPLY vindication call site to read back."""
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
            # Capture the before-snapshot for the post-APPLY call site.
            # Best-effort: snapshot_oracle_state returns None on oracle
            # failure; the post-APPLY helper falls back to neutral.
            snap = snapshot_oracle_state(self._oracle, target_files)
            if snap is not None:
                self._cache_snapshot(op_id, snap)
            logger.info(
                "[CognitiveMetrics] op=%s pre_score=%.3f gate=%s "
                "files=%d (Phase 4 P3)",
                op_id, result.pre_score, result.gate, len(target_files),
            )
        return result

    def get_pre_apply_snapshot(self, op_id: str) -> Optional[OracleSnapshot]:
        """Return the cached pre-APPLY snapshot for an op_id, or None
        if no snapshot was captured (e.g. flag was off at CONTEXT_EXPANSION
        time, or the op never went through score_pre_apply)."""
        with self._lock:
            return self._pre_apply_snapshots.get(op_id)

    def pop_pre_apply_snapshot(self, op_id: str) -> Optional[OracleSnapshot]:
        """Same as ``get_pre_apply_snapshot`` but ALSO evicts the entry
        from the cache. Use this from the post-APPLY call site so the
        cache stays bounded over a long session."""
        with self._lock:
            return self._pre_apply_snapshots.pop(op_id, None)

    def _cache_snapshot(self, op_id: str, snap: OracleSnapshot) -> None:
        """FIFO-bounded snapshot cache (oldest evicted at the cap)."""
        with self._lock:
            if len(self._pre_apply_snapshots) >= _SNAPSHOT_CACHE_MAX:
                # Evict the oldest entry (insertion order) — Python dicts
                # preserve insertion order so popitem(last=False) on
                # OrderedDict semantics is the natural FIFO.
                try:
                    oldest = next(iter(self._pre_apply_snapshots))
                    self._pre_apply_snapshots.pop(oldest, None)
                except StopIteration:
                    pass
            self._pre_apply_snapshots[op_id] = snap

    def auto_reflect_post_apply(
        self,
        op_id: str,
        target_files: List[str],
    ) -> Optional[VindicationResult]:
        """High-level post-APPLY entry point used by the orchestrator
        helper. Resolves before/after snapshots automatically:

        * BEFORE — pulled from the snapshot cache via
          ``pop_pre_apply_snapshot(op_id)``. Returns ``None`` (caller
          short-circuits) when no snapshot was captured (master flag
          was off at CONTEXT_EXPANSION OR the op skipped pre-score).
        * AFTER — fresh ``snapshot_oracle_state(target_files)``. Returns
          ``None`` (caller short-circuits) when the live oracle is
          unavailable.

        Returns the underlying ``VindicationResult`` (which itself
        carries the neutral fallback contract). Caller treats ``None``
        return as "couldn't reflect this op" — never blocks the FSM.
        """
        if not target_files:
            return None
        before = self.pop_pre_apply_snapshot(op_id)
        if before is None:
            return None
        after = snapshot_oracle_state(self._oracle, target_files)
        if after is None:
            return None
        return self.reflect_post_apply(
            op_id=op_id,
            target_files=target_files,
            coupling_after=after.coupling_total,
            blast_radius_after=after.blast_max,
            complexity_after=after.complexity_estimate,
            complexity_before=before.complexity_estimate,
        )

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
    "OracleSnapshot",
    "get_default_service",
    "is_enabled",
    "reset_default_service",
    "set_default_service",
    "snapshot_oracle_state",
]
