"""Miner graph coalescer — collapse N independent file ops into one ExecutionGraph.

Manifesto §3 (asynchronous tendrils): multiple same-strategy refactor candidates
should execute as a single parallel DAG instead of N isolated ops — one signal,
one approval, one graph, many concurrent work units.

Design
------
- Default behavior (``JARVIS_MINER_GRAPH_COALESCE=true``): when the miner
  selects ≥ 2 candidates, build a single coalesced envelope with all target
  files + graph metadata, and emit a single ingest call instead of N.
- Optional behavior (``JARVIS_MINER_GRAPH_AUTO_SUBMIT=false`` default): also
  submit the graph directly to the ``SubagentScheduler`` to ignite the
  Phase 3b ``ExecutionGraphProgressTracker`` with a real multi-op workload.
  Off by default because it bypasses the pending_ack gate; enable for battle
  tests or when the scheduler's own approval policy covers safety.
- All inputs are ``_FileAnalysis`` records from the miner.  The coalescer
  does NOT care about ast, file contents, or any miner internals — it only
  needs ``file_path``, ``composite_score`` and a strategy label.
- The caller (miner sensor) still decides whether to use the coalescer or
  fall back to per-file envelopes via ``should_coalesce()``.

Env knobs
---------
* ``JARVIS_MINER_GRAPH_COALESCE``        master switch (default ``true``)
* ``JARVIS_MINER_GRAPH_MIN_UNITS``       min selection size to coalesce (default ``2``)
* ``JARVIS_MINER_GRAPH_MAX_UNITS``       max units per graph (default ``16``)
* ``JARVIS_MINER_GRAPH_CONCURRENCY``     default graph concurrency_limit (default ``4``)
* ``JARVIS_MINER_GRAPH_AUTO_SUBMIT``     submit to scheduler directly (default ``false``)
* ``JARVIS_MINER_GRAPH_UNIT_TIMEOUT_S``  per-unit timeout (default ``180``)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() not in ("false", "0", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


_SCHEMA_VERSION = "miner_graph.1"
_PLANNER_ID = "miner_graph_coalescer"


# ---------------------------------------------------------------------------
# Minimal "file analysis" protocol (duck-typed — any object with these
# attributes works, so we don't import from the miner module and create
# circular deps).
# ---------------------------------------------------------------------------


class _AnalysisLike(Protocol):
    file_path: str

    @property
    def composite_score(self) -> float: ...


# ---------------------------------------------------------------------------
# Scheduler protocol (duck-typed too — don't import SubagentScheduler here)
# ---------------------------------------------------------------------------


class _SchedulerLike(Protocol):
    async def submit(self, graph: ExecutionGraph) -> bool: ...


# ---------------------------------------------------------------------------
# Coalescer result bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoalescedBatch:
    """Result of a successful coalescing operation."""

    graph: ExecutionGraph
    envelope_evidence: Dict[str, Any]
    description: str
    target_files: Tuple[str, ...]
    confidence: float
    submitted_to_scheduler: bool


# ---------------------------------------------------------------------------
# Coalescer
# ---------------------------------------------------------------------------


class MinerGraphCoalescer:
    """Collapses N miner candidates into one ExecutionGraph + envelope."""

    def __init__(
        self,
        *,
        scheduler: Optional[_SchedulerLike] = None,
        repo: str = "jarvis",
        enabled: Optional[bool] = None,
        min_units: Optional[int] = None,
        max_units: Optional[int] = None,
        concurrency_limit: Optional[int] = None,
        unit_timeout_s: Optional[float] = None,
        auto_submit: Optional[bool] = None,
    ) -> None:
        self._scheduler = scheduler
        self._repo = repo
        self._enabled = (
            _env_bool("JARVIS_MINER_GRAPH_COALESCE", True) if enabled is None else bool(enabled)
        )
        self._min_units = max(2, _env_int("JARVIS_MINER_GRAPH_MIN_UNITS", 2) if min_units is None else int(min_units))
        self._max_units = max(
            self._min_units,
            _env_int("JARVIS_MINER_GRAPH_MAX_UNITS", 16) if max_units is None else int(max_units),
        )
        self._concurrency_limit = max(
            1,
            _env_int("JARVIS_MINER_GRAPH_CONCURRENCY", 4) if concurrency_limit is None else int(concurrency_limit),
        )
        self._unit_timeout_s = (
            _env_float("JARVIS_MINER_GRAPH_UNIT_TIMEOUT_S", 180.0)
            if unit_timeout_s is None
            else float(unit_timeout_s)
        )
        self._auto_submit = (
            _env_bool("JARVIS_MINER_GRAPH_AUTO_SUBMIT", False) if auto_submit is None else bool(auto_submit)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def should_coalesce(self, analyses: Sequence[_AnalysisLike]) -> bool:
        """Return True if the caller should use this coalescer for ``analyses``."""
        if not self._enabled:
            return False
        if len(analyses) < self._min_units:
            return False
        return True

    async def coalesce(
        self,
        analyses: Sequence[_AnalysisLike],
        *,
        strategy: str,
        sort_field: str = "composite_score",
        repo: Optional[str] = None,
    ) -> Optional[CoalescedBatch]:
        """Build a graph + envelope payload from ``analyses``.

        Returns ``None`` if coalescing is disabled or the selection is too
        small. When ``auto_submit`` is enabled and a scheduler is attached,
        also submits the graph to the scheduler (lighting up the Phase 3b
        ``ExecutionGraphProgressTracker``).
        """
        if not self.should_coalesce(analyses):
            return None

        target_repo = repo or self._repo
        # Cap unit count so graphs stay small + scheduler-friendly.
        capped = list(analyses[: self._max_units])

        units = self._build_units(capped, strategy=strategy, repo=target_repo)
        if not units:
            return None

        graph_id, op_id = self._mint_ids(strategy=strategy)
        try:
            graph = ExecutionGraph(
                graph_id=graph_id,
                op_id=op_id,
                planner_id=_PLANNER_ID,
                schema_version=_SCHEMA_VERSION,
                units=tuple(units),
                concurrency_limit=min(self._concurrency_limit, len(units)),
            )
        except ValueError as exc:
            logger.warning(
                "[MinerGraphCoalescer] graph construction failed (strategy=%s, units=%d): %s",
                strategy, len(units), exc,
            )
            return None

        target_files = tuple(u.target_files[0] for u in units)
        composite_scores = [float(getattr(a, "composite_score", 0.0) or 0.0) for a in capped]
        avg_conf = max(0.1, min(1.0, sum(composite_scores) / len(composite_scores))) if composite_scores else 0.1

        description = (
            f"Coalesced refactor of {len(units)} {strategy} candidate(s): "
            f"{target_files[0]} + {len(units) - 1} more"
        )

        evidence: Dict[str, Any] = {
            "coalesced_graph": True,
            "strategy": strategy,
            "sort_field": sort_field,
            "unit_count": len(units),
            "concurrency_limit": graph.concurrency_limit,
            "graph_id": graph.graph_id,
            "graph_op_id": graph.op_id,
            "plan_digest": graph.plan_digest,
            "unit_specs": [
                {
                    "unit_id": u.unit_id,
                    "target_file": u.target_files[0],
                    "goal": u.goal,
                }
                for u in units
            ],
            "composite_scores": [round(s, 4) for s in composite_scores],
            "planner_id": _PLANNER_ID,
            "schema_version": _SCHEMA_VERSION,
        }

        submitted = False
        if self._auto_submit and self._scheduler is not None:
            submitted = await self._submit_with_guard(graph)
            evidence["submitted_to_scheduler"] = submitted

        return CoalescedBatch(
            graph=graph,
            envelope_evidence=evidence,
            description=description,
            target_files=target_files,
            confidence=avg_conf,
            submitted_to_scheduler=submitted,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_units(
        self,
        analyses: Sequence[_AnalysisLike],
        *,
        strategy: str,
        repo: str,
    ) -> List[WorkUnitSpec]:
        """Build independent (no-dep) work units — one per file."""
        units: List[WorkUnitSpec] = []
        seen_ids: set[str] = set()
        for analysis in analyses:
            rel = getattr(analysis, "file_path", "") or ""
            if not rel:
                continue
            unit_id = self._unit_id_for(rel, strategy)
            if unit_id in seen_ids:
                continue
            seen_ids.add(unit_id)
            goal = (
                f"{strategy}: refactor {rel} to reduce "
                f"{strategy.replace('_', ' ')} (composite={getattr(analysis, 'composite_score', 0.0):.2f})"
            )
            try:
                units.append(
                    WorkUnitSpec(
                        unit_id=unit_id,
                        repo=repo,
                        goal=goal,
                        target_files=(rel,),
                        owned_paths=(rel,),
                        max_attempts=1,
                        timeout_s=self._unit_timeout_s,
                    )
                )
            except ValueError as exc:
                logger.debug(
                    "[MinerGraphCoalescer] skipping invalid unit for %s: %s",
                    rel, exc,
                )
        return units

    @staticmethod
    def _unit_id_for(rel: str, strategy: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_]+", "_", rel).strip("_")[:48] or "file"
        return f"u_{strategy}_{slug}_{hashlib.sha1(rel.encode()).hexdigest()[:8]}"

    @staticmethod
    def _mint_ids(*, strategy: str) -> Tuple[str, str]:
        # graph_id is deterministic-ish to make logs readable; op_id is UUID
        # so scheduler store won't collide across scans.
        ts = int(time.time())
        suffix = uuid.uuid4().hex[:8]
        graph_id = f"miner_{strategy}_{ts}_{suffix}"
        op_id = f"op-{uuid.uuid4()}"
        return graph_id, op_id

    async def _submit_with_guard(self, graph: ExecutionGraph) -> bool:
        if self._scheduler is None:
            return False
        try:
            accepted = await self._scheduler.submit(graph)
        except Exception:
            logger.exception(
                "[MinerGraphCoalescer] scheduler.submit raised for graph=%s",
                graph.graph_id,
            )
            return False
        if accepted:
            logger.info(
                "[MinerGraphCoalescer] submitted graph=%s (%d units, concurrency=%d) "
                "to SubagentScheduler",
                graph.graph_id, len(graph.units), graph.concurrency_limit,
            )
        else:
            logger.warning(
                "[MinerGraphCoalescer] scheduler rejected graph=%s "
                "(duplicate/backpressure/stopped)",
                graph.graph_id,
            )
        return accepted
