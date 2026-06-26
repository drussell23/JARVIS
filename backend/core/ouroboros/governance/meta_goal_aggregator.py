"""Adaptive Meta-Goal Aggregator -- bundle N disjoint single-file ops into a
fan-outable Meta-Goal DAG (the Omni-Soak #3 ``single_file_op`` fix).

THE BUG (Omni-Soak #3)
----------------------
When N independent single-file ops pool in the dispatch queue (e.g. 3
isolated chaos failures), each is dispatched as a SEPARATE
``single_file_op`` -> :func:`~parallel_dispatch.is_fanout_eligible`
returns ``allowed=False reason=single_file_op`` -> NO fan-out, ever. The
swarm only fans out ONE goal that decomposes into a multi-unit DAG. So 3
parallelizable bug-fixes queue serially while the swarm starves.

THE FIX (this module)
---------------------
Watch the pooled single-file ops (reusing the signal-coalescing window
notion). When ``count(single_file_ops) >= JARVIS_META_GOAL_MIN_OPS`` within
the window, ask the existing zero-trust
:mod:`~backend.core.ouroboros.governance.collision_matrix` which ops are
pairwise-DISJOINT, then bundle the largest disjoint set into ONE Meta-Goal
whose :class:`~backend.core.ouroboros.governance.autonomy.subagent_types.ExecutionGraph`
is built by the EXISTING :func:`~parallel_dispatch.build_execution_graph`
(one node per bundled op). The Meta-Goal then takes the proven fan-out path
through :func:`is_fanout_eligible` (with ``force=True`` so a genuine
>=2-disjoint-unit Meta-Goal authoritatively engages fan-out without flipping
the standalone WAVE3 flags) -> ``allowed=True n=N``.

Reuse-first -- this module is *upstream intelligence*; the proven executor is
untouched. It reuses, never duplicates:

- :func:`parallel_dispatch.is_fanout_eligible` (+ ``force``) -- the Meta-Goal
  fan-out eligibility decision (replaces the ``SINGLE_FILE_OP`` reject).
- :func:`parallel_dispatch.build_execution_graph` -- the ONE node-per-op DAG.
- :func:`collision_matrix.partition_parallel_safe` -- which ops are disjoint.
- :func:`dag_composer.compose_fanout_result` (over the SUCCESSFUL subset) --
  the sha256 zero-loss union for partial (Poison-Pill) recomposition.
- :func:`intake_dlq.append_dlq` -- the Cryo-DLQ for failed unit(s).
- :class:`memory_pressure_gate.MemoryPressureGate` -- live worker capacity for
  the dynamic resource-aware batching ceiling.

Constraints honoured
---------------------
- **Dynamic resource-aware batching**: chunk size = ``min(max_concurrent_
  workers, gate_allowed, max_units_cap)`` from LIVE capacity, never a
  hardcoded N. 50 ops -> multiple capacity-sized Meta-Goals, never one
  mega-DAG (OOM / API rate-limit guard).
- **Fail-CLOSED on the collision invariant**: coupled ops are never bundled
  together (the collision matrix is authoritative; indeterminate coupling
  is a collision under zero-trust).
- **Partial-tolerant on unit failure (Poison-Pill)**: when a Meta-Goal
  bundles 3 and 2 succeed + 1 fails, the 2 successful disjoint patches are
  composed into ONE PR and the failed unit is routed to the Cryo-DLQ for a
  standalone retry. Good cognition is never scrapped for one bad sibling.
- **Failover-aware**: a single node's mid-flight DW collapse routes that unit
  to J-Prime via the existing ``provider_override`` Cryo-DLQ handoff and
  resumes only that node; siblings keep crunching.
- **Granular telemetry**: every emitted line tags BOTH the parent
  ``meta_goal_id`` AND the origin ``single_file_op`` id.
- **Gated default-OFF (byte-identical)**: master
  ``JARVIS_META_GOAL_AGGREGATOR_ENABLED`` (default ``false``). OFF -> the
  aggregator never bundles; pooled ops are returned as-is and the legacy
  single-file dispatch path is byte-identical.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionState,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.collision_matrix import (
    partition_parallel_safe,
)
from backend.core.ouroboros.governance.dag_composer import (
    ComposeFailure,
    ComposedCandidate,
    compose_fanout_result,
)
from backend.core.ouroboros.governance.intake_dlq import append_dlq
from backend.core.ouroboros.governance.memory_pressure_gate import (
    MemoryPressureGate,
    get_default_gate,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    CandidateFile,
    FanoutEligibility,
    build_execution_graph,
    is_fanout_eligible,
    parallel_dispatch_max_units,
)
from backend.core.ouroboros.governance.posture import Posture

logger = logging.getLogger("Ouroboros.MetaGoalAggregator")


# ---------------------------------------------------------------------------
# Env flags (default-OFF master; threshold env; no hardcoded batch size)
# ---------------------------------------------------------------------------

META_GOAL_FLAG = "JARVIS_META_GOAL_AGGREGATOR_ENABLED"
_MIN_OPS_FLAG = "JARVIS_META_GOAL_MIN_OPS"
_WINDOW_FLAG = "JARVIS_META_GOAL_COALESCE_WINDOW_S"
_MAX_WORKERS_FLAG = "JARVIS_MAX_CONCURRENT_WORKERS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    if v < minimum:
        return minimum
    return v


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if v <= 0.0:
        return default
    return v


def meta_goal_aggregator_enabled() -> bool:
    """Master flag -- ``JARVIS_META_GOAL_AGGREGATOR_ENABLED`` (default ``false``).

    When ``false`` (graduation default), :meth:`MetaGoalAggregator.offer`
    still pools ops, but :meth:`MetaGoalAggregator.drain_ready_bundles`
    NEVER returns a bundle -- ops fall through to the legacy single-file
    dispatch path, byte-identical to pre-aggregator behaviour.
    """
    return _env_bool(META_GOAL_FLAG, False)


def meta_goal_min_ops(default: int = 2) -> int:
    """Min pooled single-file ops before a Meta-Goal forms (env, default 2)."""
    return _env_int(_MIN_OPS_FLAG, default, minimum=2)


def meta_goal_coalesce_window_s(default: float = 30.0) -> float:
    """Coalescing window seconds (env; mirrors JARVIS_COALESCE_WINDOW_S default).

    Ops offered within this window of each other are eligible to bundle
    together. Default 30s matches the orchestrator's signal-coalescing window.
    """
    return _env_float(_WINDOW_FLAG, default)


def max_concurrent_workers(default: int = 5) -> int:
    """Live worker-capacity ceiling -- ``JARVIS_MAX_CONCURRENT_WORKERS``.

    The dynamic batch ceiling is ``min(this, gate_allowed, max_units_cap)``;
    this is the worker-pool dimension of that strictest-wins minimum.
    """
    return _env_int(_MAX_WORKERS_FLAG, default, minimum=1)


# ---------------------------------------------------------------------------
# Pooled op + bundle records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PooledOp:
    """A single-file op pooled in the dispatch window awaiting aggregation.

    Mirrors the shape a chaos-failure (or any single-file) op carries into
    the queue WITHOUT importing the orchestrator's op-context type (authority
    -import ban). ``offered_at`` is stamped at :meth:`MetaGoalAggregator.offer`
    so the coalescing window can age ops out.
    """

    op_id: str
    file_path: str
    full_content: str = ""
    rationale: str = ""
    repo: str = "jarvis"
    offered_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.op_id or not str(self.op_id).strip():
            raise ValueError("PooledOp.op_id must be non-empty")
        if not self.file_path or not str(self.file_path).strip():
            raise ValueError("PooledOp.file_path must be non-empty")


@dataclass(frozen=True)
class MetaGoalBundle:
    """One fan-outable Meta-Goal: a graph + the eligibility that authorized it.

    Attributes
    ----------
    meta_goal_id:
        Deterministic id derived from the bundled op-ids.
    graph:
        The :class:`ExecutionGraph` (one node per bundled op) built by the
        EXISTING :func:`build_execution_graph`.
    eligibility:
        The :class:`FanoutEligibility` (``allowed=True n_allowed=N``) that
        replaced the per-op ``SINGLE_FILE_OP`` reject.
    unit_to_op:
        ``unit_id -> origin single_file_op id`` -- the granular-telemetry +
        partial-recomposition trace.
    ops:
        The :class:`PooledOp` records bundled into this Meta-Goal.
    """

    meta_goal_id: str
    graph: ExecutionGraph
    eligibility: FanoutEligibility
    unit_to_op: Dict[str, str]
    ops: Tuple[PooledOp, ...]


@dataclass(frozen=True)
class PartialRecomposition:
    """Outcome of :meth:`MetaGoalAggregator.partial_recompose` (Poison-Pill).

    Attributes
    ----------
    composed:
        The :class:`ComposedCandidate` over the SUCCESSFUL disjoint subset
        (ONE PR), or ``None`` when zero units succeeded (nothing to commit).
    dlq_op_ids:
        Origin single_file_op ids routed back to the Cryo-DLQ for standalone
        retry (the failed siblings).
    composed_op_ids:
        Origin single_file_op ids whose patches made it into the composed PR.
    """

    composed: Optional[ComposedCandidate]
    dlq_op_ids: Tuple[str, ...]
    composed_op_ids: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _meta_goal_id_for(op_ids: Sequence[str]) -> str:
    digest = hashlib.sha256(
        "\x1f".join(sorted(op_ids)).encode("utf-8")
    ).hexdigest()
    return f"mg-{digest[:12]}"


class MetaGoalAggregator:
    """Pools single-file ops and bundles disjoint ones into Meta-Goal DAGs.

    The aggregator is the upstream intelligence that turns the Omni-Soak #3
    ``single_file_op`` reject into a fan-out feast. It owns no executor; it
    only decides *what to bundle* and *how big each batch may be* given live
    capacity, then hands a :class:`MetaGoalBundle` to the proven fan-out path.

    Thread-safety: callers drive ``offer`` / ``drain_ready_bundles`` from a
    single asyncio loop (the dispatch coalescing tick). No internal locking;
    the operations are synchronous + fast (pure partition + graph build).
    """

    def __init__(
        self,
        *,
        gate: Optional[MemoryPressureGate] = None,
        posture_fn: Optional[
            Callable[[], Tuple[Optional[Posture], Optional[float]]]
        ] = None,
        oracle: Any = None,
        max_concurrent_workers: Optional[int] = None,
        repo: str = "jarvis",
    ) -> None:
        self._gate = gate if gate is not None else get_default_gate()
        self._posture_fn = posture_fn
        self._oracle = oracle
        self._repo = repo
        # None -> read from env at decision time (live capacity). An explicit
        # int (tests / operator override) pins the worker dimension.
        self._max_workers_override = max_concurrent_workers
        self._pool: List[PooledOp] = []

    # -- intake -------------------------------------------------------------

    def offer(self, op: PooledOp) -> None:
        """Pool a single-file op for possible Meta-Goal aggregation.

        Idempotent on ``op_id`` -- re-offering the same op replaces the
        prior entry (keeps the latest content/rationale). Never raises on a
        well-formed :class:`PooledOp`.
        """
        self._pool = [p for p in self._pool if p.op_id != op.op_id]
        self._pool.append(op)

    def pending_ops(self) -> Tuple[PooledOp, ...]:
        """The currently-pooled ops (legacy single-file path consumes these)."""
        return tuple(self._pool)

    def drop_ops(self, op_ids: Sequence[str]) -> int:
        """Remove the given op-ids from the pool; return how many were dropped.

        Pool bookkeeping (NOT aggregation logic) -- the live wiring calls this
        when an un-bundled op has aged out of the coalescing window and been
        flushed to the legacy single-file dispatch, so it does not linger in
        the pool forever. Mirrors the drain-path drop on the same ``_pool``
        list. Idempotent -- dropping an absent id is a no-op.
        """
        drop = {str(o) for o in op_ids}
        if not drop:
            return 0
        before = len(self._pool)
        self._pool = [p for p in self._pool if p.op_id not in drop]
        return before - len(self._pool)

    # -- capacity -----------------------------------------------------------

    def _effective_worker_cap(self) -> int:
        if self._max_workers_override is not None:
            return max(1, int(self._max_workers_override))
        return max_concurrent_workers()

    def _dynamic_batch_ceiling(self, n_disjoint: int) -> int:
        """STRICTEST-wins live ceiling on a single Meta-Goal's degree.

        ``min(worker_cap, gate_allowed, max_units_cap, n_disjoint)`` -- never
        a hardcoded constant. A flood of 50 ops therefore chunks into many
        capacity-sized Meta-Goals instead of one OOM mega-DAG.
        """
        worker_cap = self._effective_worker_cap()
        gate_allowed = self._gate.can_fanout(n_disjoint).n_allowed
        max_units = parallel_dispatch_max_units()
        ceiling = min(worker_cap, gate_allowed, max_units, n_disjoint)
        return max(1, int(ceiling))

    # -- bundling -----------------------------------------------------------

    def _window_ops(self, now: float, window_s: float) -> List[PooledOp]:
        """Ops still inside the coalescing window (reuse-first window notion)."""
        return [p for p in self._pool if (now - p.offered_at) <= window_s]

    def _op_to_unit_spec(self, op: PooledOp) -> WorkUnitSpec:
        """One node per bundled single-file op (deterministic unit_id)."""
        digest = hashlib.sha256(
            f"{op.op_id}\x1f{op.file_path}".encode("utf-8")
        ).hexdigest()
        return WorkUnitSpec(
            unit_id=f"unit-{digest[:12]}",
            repo=op.repo or self._repo,
            goal=op.rationale or f"fix {op.file_path}",
            target_files=(op.file_path,),
            owned_paths=(op.file_path,),
        )

    async def prewarm_window(self) -> int:
        """Self-Warming Oracle JIT seam -- async-warm the Oracle for the ops in
        the coalescing window BEFORE the (sync) disjointness partition.

        The bug this closes (Omni-Soak v5): ``drain_ready_bundles`` is SYNC and
        runs the zero-trust collision partition against ``self._oracle``. On a
        fresh node the Oracle has NO data for the pooled files, so every
        cross-file pair is INDETERMINATE -> COLLIDE and nothing bundles
        ("aged out ... no disjoint sibling found"). ``prewarm_collision_files``
        (built, but never AWAITED from the live bundle path) JIT-indexes those
        files so the subsequent ``_coupled_files`` probe finds them indexed ->
        DISJOINT -> the disjoint set BUNDLES.

        REUSE-only: warms the SAME window ``drain_ready_bundles`` will partition,
        via the SAME injected ``self._oracle`` handle and the existing
        ``prewarm_collision_files`` (which itself reuses ``ensure_file_indexed``).
        No new Oracle is constructed; no new logic.

        Gating: a no-op (returns 0, byte-identical) when the Meta-Goal master is
        OFF, when ``JARVIS_ORACLE_SELF_WARMING_ENABLED`` is OFF (the
        ``prewarm_collision_files`` gate), or when there is no Oracle. Fully
        fail-soft -- a warm error is swallowed and the drain falls through to
        the existing COLD behaviour (COLLIDE), never crashing the drain.

        Returns the count of files warmed-attempted (0 on any no-op / error).
        """
        if not meta_goal_aggregator_enabled():
            return 0
        try:
            from backend.core.ouroboros.governance.collision_matrix import (
                prewarm_collision_files,
                self_warming_enabled,
            )

            if not self_warming_enabled() or self._oracle is None:
                return 0
            now = time.monotonic()
            window = self._window_ops(now, meta_goal_coalesce_window_s())
            if len(window) < meta_goal_min_ops():
                return 0
            # De-duped file set the partition will probe -- same notion as the
            # drain (each pooled op owns a single target file).
            files: List[str] = []
            seen: set = set()
            for op in window:
                fp = getattr(op, "file_path", None)
                if fp and fp not in seen:
                    seen.add(fp)
                    files.append(fp)
            if not files:
                return 0
            logger.info(
                "[MetaGoal] pre-warming oracle for %d files before "
                "disjointness check (self-warming JIT)",
                len(files),
            )
            return await prewarm_collision_files(self._oracle, files)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- warm is advisory; never sink the drain
            logger.debug(
                "[MetaGoal] prewarm_window failed (fail-soft -> cold partition)",
                exc_info=True,
            )
            return 0

    def drain_ready_bundles(self) -> List[MetaGoalBundle]:
        """Form + return all ready Meta-Goal bundles; drain bundled ops.

        Master-OFF -> always ``[]`` (legacy single-file dispatch; pooled ops
        stay retrievable via :meth:`pending_ops`).

        Otherwise:

        1. Take the ops inside the coalescing window. If fewer than
           ``JARVIS_META_GOAL_MIN_OPS``, nothing is ready yet.
        2. Partition them via the zero-trust collision matrix
           (:func:`partition_parallel_safe`) -- coupled ops are NEVER
           co-grouped (fail-CLOSED collision invariant).
        3. For each disjoint group, CHUNK it into capacity-sized Meta-Goals
           via :meth:`_dynamic_batch_ceiling` (no OOM mega-DAG).
        4. For each chunk of >= 2 units, run :func:`is_fanout_eligible`
           (``force=True``) on the chunk degree, then
           :func:`build_execution_graph`. ``allowed=True n=chunk`` -> a
           :class:`MetaGoalBundle`.

        Bundled ops are removed from the pool; un-bundled ops (singletons,
        coupled remainders, sub-min windows) stay pooled for the next tick or
        the legacy single-file path.
        """
        if not meta_goal_aggregator_enabled():
            return []

        now = time.monotonic()
        window_s = meta_goal_coalesce_window_s()
        window = self._window_ops(now, window_s)
        if len(window) < meta_goal_min_ops():
            return []

        # Map unit_id -> PooledOp so we can recover origin ops + content.
        op_by_path = {op.file_path: op for op in window}
        specs = [self._op_to_unit_spec(op) for op in window]
        spec_by_unit = {s.unit_id: s for s in specs}

        # Zero-trust disjoint partition -- coupled ops are never co-grouped.
        parallel_groups, _sequential = partition_parallel_safe(
            specs, oracle=self._oracle
        )

        bundles: List[MetaGoalBundle] = []
        bundled_op_ids: set = set()

        for group in parallel_groups:
            # Deterministic order within the group.
            group_sorted = sorted(group, key=lambda u: u.unit_id)
            # CHUNK by live capacity -- never a single mega-DAG.
            ceiling = self._dynamic_batch_ceiling(len(group_sorted))
            if ceiling < 2:
                continue  # capacity collapsed to serial; leave ops pooled
            for start in range(0, len(group_sorted), ceiling):
                chunk = group_sorted[start : start + ceiling]
                if len(chunk) < 2:
                    # A trailing singleton chunk is serial-equivalent; leave
                    # it pooled for the next tick / legacy single-file path.
                    continue
                bundle = self._build_bundle(chunk, spec_by_unit, op_by_path)
                if bundle is None:
                    continue
                bundles.append(bundle)
                bundled_op_ids.update(bundle.unit_to_op.values())

        # Drain only the ops that actually got bundled.
        if bundled_op_ids:
            self._pool = [p for p in self._pool if p.op_id not in bundled_op_ids]

        return bundles

    def _build_bundle(
        self,
        chunk: Sequence[WorkUnitSpec],
        spec_by_unit: Dict[str, WorkUnitSpec],
        op_by_path: Dict[str, PooledOp],
    ) -> Optional[MetaGoalBundle]:
        """Build ONE Meta-Goal from a >=2 disjoint chunk (reuse fan-out path)."""
        ops = [op_by_path[s.target_files[0]] for s in chunk]
        op_ids = [o.op_id for o in ops]
        meta_goal_id = _meta_goal_id_for(op_ids)

        # The run-#3 fix: force=True so a genuine >=2-disjoint-unit Meta-Goal
        # authoritatively engages the proven fan-out path WITHOUT flipping the
        # standalone WAVE3_PARALLEL_DISPATCH flags. All real clamps (memory
        # CRITICAL, posture, max_units) remain authoritative.
        eligibility = is_fanout_eligible(
            op_id=meta_goal_id,
            n_candidate_files=len(chunk),
            gate=self._gate,
            posture_fn=self._posture_fn,
            emit_log=True,
            force=True,
        )
        if not eligibility.allowed or eligibility.n_allowed < 2:
            logger.info(
                "[MetaGoal] meta=%s NOT-eligible n_requested=%d "
                "n_allowed=%d reason=%s -> ops stay single-file",
                meta_goal_id,
                len(chunk),
                eligibility.n_allowed,
                eligibility.reason_code.value,
            )
            return None

        # If eligibility clamped below the chunk size (posture/memory), trim
        # to the allowed degree; the trimmed ops stay pooled (not bundled).
        n = eligibility.n_allowed
        chunk = list(chunk)[:n]
        ops = ops[:n]
        op_ids = op_ids[:n]
        # Recompute meta-goal id + eligibility for the trimmed set so the
        # graph degree matches eligibility.n_allowed exactly.
        if len(chunk) != n or n != eligibility.n_requested:
            meta_goal_id = _meta_goal_id_for(op_ids)
            eligibility = is_fanout_eligible(
                op_id=meta_goal_id,
                n_candidate_files=len(chunk),
                gate=self._gate,
                posture_fn=self._posture_fn,
                emit_log=False,
                force=True,
            )
            if not eligibility.allowed or eligibility.n_allowed < 2:
                return None

        candidate_files = [
            CandidateFile(
                file_path=op_by_path[s.target_files[0]].file_path,
                full_content=op_by_path[s.target_files[0]].full_content,
                rationale=op_by_path[s.target_files[0]].rationale,
            )
            for s in chunk
        ]
        # Reuse the EXISTING graph builder -- one node per bundled op.
        graph = build_execution_graph(
            op_id=meta_goal_id,
            repo=self._repo,
            candidate_files=candidate_files,
            eligibility=eligibility,
        )

        # unit_id -> origin op id, keyed by the graph's actual units (the
        # graph builder derives its OWN unit_ids from meta_goal_id + path).
        path_to_op = {op_by_path[s.target_files[0]].file_path: op_by_path[s.target_files[0]].op_id for s in chunk}
        unit_to_op = {
            u.unit_id: path_to_op[u.target_files[0]] for u in graph.units
        }

        logger.info(
            "[MetaGoal] meta=%s formed n_units=%d files=%s graph=%s "
            "(run-#3 single_file_op fix: %d disjoint single-file ops -> 1 fan-out)",
            meta_goal_id,
            len(graph.units),
            [u.target_files[0] for u in graph.units],
            graph.graph_id,
            len(graph.units),
        )

        return MetaGoalBundle(
            meta_goal_id=meta_goal_id,
            graph=graph,
            eligibility=eligibility,
            unit_to_op=unit_to_op,
            ops=tuple(ops),
        )

    # -- failover-aware per-node resume -------------------------------------

    def build_failover_override(
        self, bundle: MetaGoalBundle, unit_id: str
    ) -> Dict[str, Any]:
        """Per-unit J-Prime failover handoff descriptor (the existing vehicle).

        A single node's mid-flight DW collapse routes ONLY that unit to
        J-Prime via the existing ``provider_override`` Cryo-DLQ handoff +
        scheduler resume; siblings keep crunching. This builds the override
        envelope (the same shape the failover lifecycle consumes), tagged with
        the meta+unit lineage so the resumed node re-joins THIS Meta-Goal's
        recomposition (returns paused->resumed, never FAILED, so partial-
        recomp still gets it).

        We do NOT add a new failover engine -- we reuse the per-unit override
        + scheduler batch resume already proven in the failover lifecycle.
        """
        origin_op = bundle.unit_to_op.get(unit_id, "")
        return {
            "provider_override": "gcp-jprime",
            "unit_id": unit_id,
            "op_id": origin_op,
            "meta_goal_id": bundle.meta_goal_id,
            "reason": "dw_collapse_node_failover",
        }

    # -- partial (Poison-Pill) recomposition --------------------------------

    def partial_recompose(
        self, bundle: MetaGoalBundle, state: GraphExecutionState
    ) -> PartialRecomposition:
        """Poison-Pill: commit the successful patches; Cryo-DLQ the failures.

        If a Meta-Goal bundles N and some units FAIL (even after J-Prime
        failover), do NOT scrap the DAG. Extract the SUCCESSFUL disjoint
        patches -> compose them via the EXISTING
        :func:`dag_composer.compose_fanout_result` (sha256 zero-loss union
        over the successful subset) -> ONE PR; route the FAILED unit(s) back
        to the Cryo-DLQ (:func:`intake_dlq.append_dlq`) for a future
        standalone retry. Good cognition is never thrown away for one bad
        sibling.

        This is a NEW *partial* mode -- the original single-decompose path
        keeps its fail-CLOSED behaviour; the Meta-Goal path uses partial. The
        composer's union+sha256 logic is REUSED over the successful subset (a
        synthetic single-success ExecutionGraph), never rewritten.
        """
        results = dict(getattr(state, "results", {}) or {})
        succeeded: List[WorkUnitSpec] = []
        failed_units: List[str] = []
        for unit in bundle.graph.units:
            r = results.get(unit.unit_id)
            if r is not None and getattr(r, "status", None) == WorkUnitState.COMPLETED:
                succeeded.append(unit)
            else:
                failed_units.append(unit.unit_id)

        composed: Optional[ComposedCandidate] = None
        composed_op_ids: List[str] = []

        if succeeded:
            # Build a synthetic graph over ONLY the successful units so the
            # existing composer's count/sha/no-overwrite proof passes over the
            # subset (the full graph would trip UNIT_NOT_SUCCESS, by design).
            sub_graph = self._subset_graph(bundle.graph, succeeded)
            sub_results = {u.unit_id: results[u.unit_id] for u in succeeded}
            result = compose_fanout_result(sub_graph, sub_results)
            if isinstance(result, ComposeFailure):
                logger.warning(
                    "[MetaGoal] meta=%s partial compose FAILED reason=%s "
                    "detail=%s -> successful subset not committed this pass",
                    bundle.meta_goal_id,
                    result.reason.value,
                    result.detail,
                )
            else:
                composed = result
                composed_op_ids = [
                    bundle.unit_to_op[u.unit_id] for u in succeeded
                ]
                logger.info(
                    "[MetaGoal] meta=%s partial-recompose committed n_files=%d "
                    "ops=%s (Poison-Pill: %d/%d succeeded -> ONE PR, DAG not scrapped)",
                    bundle.meta_goal_id,
                    composed.n_files,
                    composed_op_ids,
                    len(succeeded),
                    len(bundle.graph.units),
                )

        # Route failed siblings to the Cryo-DLQ for standalone retry.
        dlq_op_ids: List[str] = []
        for unit_id in failed_units:
            origin_op = bundle.unit_to_op.get(unit_id, "")
            unit = bundle.graph.unit_map.get(unit_id)
            envelope = {
                "op_id": origin_op,
                "meta_goal_id": bundle.meta_goal_id,
                "unit_id": unit_id,
                "file_path": (unit.target_files[0] if unit else ""),
                "goal": (unit.goal if unit else ""),
            }
            try:
                append_dlq(
                    envelope,
                    reason=(
                        f"meta_goal_partial_failure:poison_pill:"
                        f"meta={bundle.meta_goal_id}"
                    ),
                )
                dlq_op_ids.append(origin_op)
                logger.info(
                    "[MetaGoal] meta=%s unit=%s op=%s file=%s status=failed "
                    "-> Cryo-DLQ (standalone retry)",
                    bundle.meta_goal_id,
                    unit_id,
                    origin_op,
                    (unit.target_files[0] if unit else ""),
                )
            except Exception:  # noqa: BLE001 -- DLQ is fail-soft, never crash recomp
                logger.warning(
                    "[MetaGoal] meta=%s DLQ append failed for unit=%s op=%s",
                    bundle.meta_goal_id,
                    unit_id,
                    origin_op,
                    exc_info=True,
                )

        return PartialRecomposition(
            composed=composed,
            dlq_op_ids=tuple(dlq_op_ids),
            composed_op_ids=tuple(composed_op_ids),
        )

    def _subset_graph(
        self, graph: ExecutionGraph, units: Sequence[WorkUnitSpec]
    ) -> ExecutionGraph:
        """A synthetic ExecutionGraph over a disjoint SUCCESSFUL subset.

        Reuses the same graph type so the composer is fed its native input.
        Dependency ids are stripped to the subset (the units are disjoint by
        the collision-matrix invariant, so they are independent anyway).
        """
        subset_ids = {u.unit_id for u in units}
        rescoped = tuple(
            WorkUnitSpec(
                unit_id=u.unit_id,
                repo=u.repo,
                goal=u.goal,
                target_files=u.target_files,
                dependency_ids=tuple(
                    d for d in u.dependency_ids if d in subset_ids
                ),
                owned_paths=u.owned_paths,
                max_attempts=u.max_attempts,
                timeout_s=u.timeout_s,
            )
            for u in units
        )
        return ExecutionGraph(
            graph_id=f"{graph.graph_id}-partial",
            op_id=graph.op_id,
            planner_id=graph.planner_id,
            schema_version=graph.schema_version,
            units=rescoped,
            concurrency_limit=max(1, len(rescoped)),
        )

    # -- telemetry ----------------------------------------------------------

    def telemetry_lines(self, bundle: MetaGoalBundle) -> List[str]:
        """Per-unit ``[MetaGoal]`` lines tagging meta_goal_id + origin op-id.

        Every worker's output + the existing ``[L3Telemetry]`` lines should
        carry BOTH the parent ``meta_goal_id`` AND the origin
        ``single_file_op`` id, so the fan-out + recomposition matrix is fully
        observable. This returns those lines (and emits them) for the caller
        to thread into the scheduler's per-unit telemetry.
        """
        lines: List[str] = []
        for unit in bundle.graph.units:
            origin_op = bundle.unit_to_op.get(unit.unit_id, "")
            line = (
                f"[MetaGoal] meta={bundle.meta_goal_id} "
                f"unit={origin_op} "
                f"sub_unit={unit.unit_id} "
                f"file={unit.target_files[0]} "
                f"status=dispatched"
            )
            lines.append(line)
            logger.info(line)
        return lines


__all__ = [
    "META_GOAL_FLAG",
    "MetaGoalAggregator",
    "MetaGoalBundle",
    "PartialRecomposition",
    "PooledOp",
    "append_dlq",  # re-exported for monkeypatch seam in tests
    "max_concurrent_workers",
    "meta_goal_aggregator_enabled",
    "meta_goal_coalesce_window_s",
    "meta_goal_min_ops",
]
