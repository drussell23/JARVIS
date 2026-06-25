"""Wave 3 (6) -- Proactive zero-trust AST Collision Matrix for L3 fan-out.

This module is the *proactive, pre-submit* complement to the scheduler's
*reactive* runtime guard
(:meth:`~backend.core.ouroboros.governance.autonomy.subagent_scheduler.SubagentScheduler._select_ready_batch`,
which defers a ready unit when its ``effective_owned_paths`` overlap a
unit already running). The reactive guard catches collisions silently at
execution time; this matrix catches them *before* the
:class:`ExecutionGraph` is even built, so a forced-serial decision is
observable and fail-fast instead of an invisible runtime serialization.

It does NOT remove or duplicate the reactive guard -- it shares the same
path-overlap notion (units own ``target_files``) but evaluates eligibility
pre-submit and additionally forbids parallelism across *import-coupled*
files (interface <-> implementation) using the real Oracle import/call
graph.

Zero-trust default-DENY mandate
-------------------------------
Two units are allowed to fan out in parallel ONLY when their coupling can
be *proven disjoint*. Any of the following yields ``COLLIDE`` (force
sequential), never optimistic parallel:

1. Direct ``target_files`` set-overlap (same file).
2. Import-coupling via the Oracle graph (A imports B, B imports A, or a
   shared interface<->impl edge in either direction).
3. **Indeterminate coupling** -- Oracle is ``None``, the Oracle raises, or
   the Oracle genuinely has no indexed data for one of the files. Unknown
   coupling is treated as a collision; we never optimistically parallelize
   an unknown.

Reuse-first
-----------
- No new import parser. Coupling is read from the existing Oracle graph
  (``find_nodes_in_file`` / ``get_dependencies`` / ``get_dependents``,
  which back the IMPORTS / IMPORTS_FROM / CALLS edges).
- No new unit type. Units are :class:`WorkUnitSpec`; coupling is keyed on
  the existing ``target_files`` tuple.
- The eligibility entry point reuses :func:`is_fanout_eligible` from
  :mod:`parallel_dispatch` for the master-flag / posture / memory chain,
  and emits the already-defined
  :data:`~parallel_dispatch.ReasonCode.COLLISION_FORCED_SEQUENTIAL`.

Determinism
-----------
:func:`partition_parallel_safe` uses a deterministic greedy
maximal-pairwise-disjoint partition over units sorted by ``unit_id``
(documented inline). Same inputs -> same split.

Master-flag gating
------------------
The matrix only matters when fan-out is on. When the WAVE3 master flag is
off, :func:`is_fanout_eligible_collision_aware` short-circuits to the
underlying :func:`is_fanout_eligible` (``MASTER_OFF``) WITHOUT building the
collision matrix -- production is byte-identical to the pre-matrix path.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    WorkUnitSpec,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    FanoutEligibility,
    ReasonCode,
    is_fanout_eligible,
    parallel_dispatch_enabled,
)

logger = logging.getLogger("Ouroboros.CollisionMatrix")


# ---------------------------------------------------------------------------
# Verdict vocabulary
# ---------------------------------------------------------------------------


class CollisionVerdict(str, enum.Enum):
    """Pairwise verdict between two work units.

    ``COLLIDE`` -- the two units MUST NOT run in parallel (same file,
    import-coupled, or indeterminate coupling under zero-trust).

    ``DISJOINT`` -- the two units were *proven* to touch neither the same
    file nor any import-coupled file; they are safe to fan out together.

    There is intentionally no ``UNKNOWN`` value: under the zero-trust
    mandate, anything not provably disjoint is a ``COLLIDE``.
    """

    COLLIDE = "collide"
    DISJOINT = "disjoint"


# ---------------------------------------------------------------------------
# Oracle coupling probe (reuse-first: reads the existing import/call graph)
# ---------------------------------------------------------------------------


def _files_of(unit: WorkUnitSpec) -> Tuple[str, ...]:
    """Files a unit owns -- the existing ``target_files`` tuple.

    Mirrors the reactive guard's path notion (units own their target
    files). We deliberately key on ``target_files`` rather than
    ``effective_owned_paths`` because coupling is computed from the files
    the model will actually edit; an empty tuple is impossible
    (``WorkUnitSpec.__post_init__`` rejects it).
    """
    return tuple(unit.target_files)


def _node_file(node: Any) -> Optional[str]:
    """Extract a ``file_path`` from an Oracle node-ish object, defensively."""
    fp = getattr(node, "file_path", None)
    if isinstance(fp, str) and fp:
        return fp
    return None


def _coupled_files(oracle: Any, file_path: str) -> Optional[set]:
    """Return the set of files import/call-coupled to ``file_path``.

    Reuses the Oracle graph exclusively -- no new parser:

    - ``find_nodes_in_file(file_path)`` -> the nodes Oracle has indexed for
      this file. An EMPTY list means the Oracle genuinely has no data for
      the file (unindexed) -> indeterminate.
    - For each such node, ``get_dependencies`` (outgoing IMPORTS/CALLS) and
      ``get_dependents`` (incoming) give the coupled nodes; their
      ``file_path`` attributes are the coupled files.

    Returns
    -------
    Optional[set]
        The set of coupled file paths (excluding ``file_path`` itself) on
        success. ``None`` on INDETERMINATE -- Oracle is missing, raised, or
        has no indexed nodes for this file. ``None`` is the zero-trust
        signal: callers MUST treat it as a collision.
    """
    if oracle is None:
        return None
    try:
        nodes = oracle.find_nodes_in_file(file_path)
    except Exception:  # noqa: BLE001 -- broken/unavailable graph -> indeterminate
        logger.debug(
            "[CollisionMatrix] find_nodes_in_file raised for %s -> indeterminate",
            file_path,
            exc_info=True,
        )
        return None
    if not nodes:
        # Oracle has no data for this file -> cannot prove disjoint.
        return None

    coupled: set = set()
    for node in nodes:
        for probe in ("get_dependencies", "get_dependents"):
            fn = getattr(oracle, probe, None)
            if fn is None:
                continue
            try:
                neighbours = fn(node)
            except Exception:  # noqa: BLE001 -- partial graph -> indeterminate
                logger.debug(
                    "[CollisionMatrix] %s raised for %s -> indeterminate",
                    probe,
                    file_path,
                    exc_info=True,
                )
                return None
            for nb in neighbours or ():
                nf = _node_file(nb)
                if nf and nf != file_path:
                    coupled.add(nf)
    return coupled


# ---------------------------------------------------------------------------
# Pairwise verdict
# ---------------------------------------------------------------------------


def _pair_verdict(
    files_a: Sequence[str],
    files_b: Sequence[str],
    oracle: Any,
    cache: Dict[str, Optional[set]],
) -> CollisionVerdict:
    """Decide COLLIDE / DISJOINT for two units' file sets (zero-trust).

    Order of evaluation (first trip wins -> COLLIDE):

    (a) Direct same-file overlap of ``target_files``.
    (b) Import-coupling: for any file in A, is any file in B in A's coupled
        set (or vice versa)? Probed in BOTH directions because the Oracle
        graph is directional.
    (c) Indeterminate: if coupling for ANY file on either side could not be
        resolved (``_coupled_files`` returned ``None``), default-DENY.

    Returns ``DISJOINT`` only when every probe resolved AND no overlap or
    coupling edge was found.
    """
    set_a = set(files_a)
    set_b = set(files_b)

    # (a) Direct file overlap -- same path notion as the reactive guard.
    if set_a & set_b:
        return CollisionVerdict.COLLIDE

    # (b)+(c) Import-coupling with zero-trust on indeterminate resolution.
    # Resolve coupled sets for every involved file once (cached).
    for fp in set_a | set_b:
        if fp not in cache:
            cache[fp] = _coupled_files(oracle, fp)

    indeterminate = False
    for fp in set_a:
        coupled = cache[fp]
        if coupled is None:
            indeterminate = True
            continue
        if coupled & set_b:
            return CollisionVerdict.COLLIDE
    for fp in set_b:
        coupled = cache[fp]
        if coupled is None:
            indeterminate = True
            continue
        if coupled & set_a:
            return CollisionVerdict.COLLIDE

    # (c) Zero-trust: any unresolved coupling -> COLLIDE, never optimistic.
    if indeterminate:
        return CollisionVerdict.COLLIDE

    return CollisionVerdict.DISJOINT


# ---------------------------------------------------------------------------
# CollisionMatrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollisionMatrix:
    """Immutable pairwise verdict table over a set of work units.

    Built once via :func:`build_collision_matrix`; queried with
    :meth:`verdict` / :meth:`collides`. Symmetric: ``verdict(a, b) ==
    verdict(b, a)``. A unit never collides with itself in the pairwise
    sense (``collides(x, x) is False``).
    """

    unit_ids: Tuple[str, ...]
    _verdicts: Dict[Tuple[str, str], CollisionVerdict] = field(
        default_factory=dict
    )

    @staticmethod
    def _key(a: str, b: str) -> Tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def verdict(self, a: str, b: str) -> CollisionVerdict:
        """Symmetric pairwise verdict. A unit vs. itself is DISJOINT."""
        if a == b:
            return CollisionVerdict.DISJOINT
        return self._verdicts.get(self._key(a, b), CollisionVerdict.COLLIDE)

    def collides(self, a: str, b: str) -> bool:
        """``True`` iff the pair must not run in parallel (and ``a != b``)."""
        if a == b:
            return False
        return self.verdict(a, b) is CollisionVerdict.COLLIDE


def build_collision_matrix(
    units: Sequence[WorkUnitSpec],
    *,
    oracle: Any = None,
) -> CollisionMatrix:
    """Build the pairwise collision matrix for ``units`` (zero-trust).

    Parameters
    ----------
    units:
        The candidate work units. Each carries ``target_files``.
    oracle:
        The Oracle import/call graph (dependency-injected for tests). When
        ``None``, the default global Oracle is used; if THAT cannot be
        obtained, every cross-file coupling is INDETERMINATE -> COLLIDE.

    Returns
    -------
    CollisionMatrix
        Pairwise verdicts for every distinct unit pair. Bounded:
        ``O(U^2)`` pairs over ``O(F)`` distinct files; per-file coupling is
        resolved once and cached.
    """
    resolved_oracle = oracle
    if resolved_oracle is None:
        resolved_oracle = _maybe_default_oracle()

    unit_ids = tuple(u.unit_id for u in units)
    verdicts: Dict[Tuple[str, str], CollisionVerdict] = {}
    # Per-build coupling cache: file_path -> coupled set (or None).
    cache: Dict[str, Optional[set]] = {}

    files = {u.unit_id: _files_of(u) for u in units}
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            a = units[i].unit_id
            b = units[j].unit_id
            v = _pair_verdict(files[a], files[b], resolved_oracle, cache)
            verdicts[CollisionMatrix._key(a, b)] = v

    return CollisionMatrix(unit_ids=unit_ids, _verdicts=verdicts)


def _maybe_default_oracle() -> Any:
    """Best-effort fetch of the global Oracle; ``None`` on any failure.

    A ``None`` oracle drives the zero-trust path (every cross-file pair
    INDETERMINATE -> COLLIDE), which is the correct fail-closed behaviour.
    """
    try:
        from backend.core.ouroboros.oracle import get_oracle

        return get_oracle()
    except Exception:  # noqa: BLE001 -- Oracle absent -> zero-trust deny
        logger.debug(
            "[CollisionMatrix] default Oracle unavailable -> zero-trust deny",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Deterministic maximal-pairwise-disjoint partition
# ---------------------------------------------------------------------------


def partition_parallel_safe(
    units: Sequence[WorkUnitSpec],
    *,
    oracle: Any = None,
    matrix: Optional[CollisionMatrix] = None,
) -> Tuple[List[List[WorkUnitSpec]], List[WorkUnitSpec]]:
    """Partition units into parallel-safe groups + a sequential remainder.

    Algorithm (deterministic greedy clique packing)
    -----------------------------------------------
    1. Sort units by ``unit_id`` (stable, deterministic).
    2. For the first unsplaced unit, open a new parallel group seeded with
       it, then greedily admit each later unit that is ``DISJOINT`` with
       EVERY unit already in the group (a clique in the disjoint graph).
    3. Repeat over the remaining unplaced units until all are placed.
    4. Any group of size >= 2 is a real fan-out group; any singleton group
       is serial-equivalent and is moved to the ``sequential_forced`` list
       (a fan-out of 1 is meaningless overhead and would otherwise hide a
       forced-serial unit).

    This greedy clique packing is a heuristic for maximum disjoint
    partitioning (the exact problem is graph-colouring-hard); it is
    deterministic, fail-closed (a unit only joins a group when *proven*
    disjoint from all members), and never co-groups a colliding pair.

    Returns
    -------
    (parallel_groups, sequential_forced)
        ``parallel_groups`` -- list of groups, each a list of >= 2 units
        that are pairwise DISJOINT and may fan out together.
        ``sequential_forced`` -- units that could not join any >= 2 group;
        they run one at a time on the serial path.
    """
    ordered = sorted(units, key=lambda u: u.unit_id)
    if matrix is None:
        matrix = build_collision_matrix(ordered, oracle=oracle)

    placed: set = set()
    groups: List[List[WorkUnitSpec]] = []

    for seed in ordered:
        if seed.unit_id in placed:
            continue
        group = [seed]
        placed.add(seed.unit_id)
        for cand in ordered:
            if cand.unit_id in placed:
                continue
            # Admit only when DISJOINT with every current group member.
            if all(
                matrix.verdict(member.unit_id, cand.unit_id)
                is CollisionVerdict.DISJOINT
                for member in group
            ):
                group.append(cand)
                placed.add(cand.unit_id)
        groups.append(group)

    parallel_groups: List[List[WorkUnitSpec]] = []
    sequential_forced: List[WorkUnitSpec] = []
    for group in groups:
        if len(group) >= 2:
            parallel_groups.append(group)
        else:
            sequential_forced.extend(group)

    return parallel_groups, sequential_forced


# ---------------------------------------------------------------------------
# Collision-aware eligibility decision (the seam wired pre-submit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollisionAwareEligibility:
    """Eligibility decision augmented with the collision partition.

    Wraps the underlying :class:`FanoutEligibility` (master-flag / posture
    / memory chain) and adds the proactive collision partition.

    Attributes
    ----------
    allowed:
        ``True`` iff a >= 2-unit pairwise-disjoint fan-out group survives
        both the collision partition AND the underlying eligibility chain.
    reason_code:
        ``COLLISION_FORCED_SEQUENTIAL`` when the collision matrix collapses
        fan-out below 2 units; otherwise the underlying chain's reason.
    parallel_units:
        The flat list of units offered for parallel fan-out (the largest
        disjoint group, capped by the underlying ``n_allowed``). ``<= 1``
        unit means serial-equivalent.
    sequential_units:
        Units forced onto the serial path -- colliders plus any units
        trimmed by the underlying fan-out cap.
    base_eligibility:
        The underlying :class:`FanoutEligibility` record, for telemetry.
    """

    allowed: bool
    reason_code: ReasonCode
    parallel_units: Tuple[WorkUnitSpec, ...]
    sequential_units: Tuple[WorkUnitSpec, ...]
    base_eligibility: Optional[FanoutEligibility] = None


def is_fanout_eligible_collision_aware(
    *,
    op_id: str,
    units: Sequence[WorkUnitSpec],
    oracle: Any = None,
    gate: Any = None,
    posture_fn: Any = None,
    emit_log: bool = True,
) -> CollisionAwareEligibility:
    """Proactive, pre-submit collision-aware fan-out eligibility.

    This is the seam intended to run BEFORE ``build_execution_graph`` /
    ``scheduler.submit`` (e.g. from ``phase_dispatcher`` where
    ``enforce_evaluate_fanout`` is called). It:

    1. Short-circuits to the underlying :func:`is_fanout_eligible` when the
       WAVE3 master flag is off -> ``MASTER_OFF``, collision matrix NOT
       built (production byte-identical to the pre-matrix path).
    2. Builds the zero-trust :class:`CollisionMatrix` and partitions the
       candidate units into pairwise-disjoint fan-out groups + a forced
       sequential remainder.
    3. Picks the largest disjoint group as the fan-out set, then runs the
       underlying eligibility chain (posture / memory / max_units) on that
       group's size to compute the effective fan-out degree.
    4. If fewer than 2 units survive (nothing provably parallelizes, or the
       chain clamps to 1), returns ``allowed=False`` with
       ``COLLISION_FORCED_SEQUENTIAL`` -- the previously-dormant reason
       code now has its first producer.

    The remainder (colliders + any units trimmed by the underlying cap)
    falls onto the serial path, complementing -- not duplicating -- the
    scheduler's reactive ``owned_paths`` deferral.
    """
    unit_list = list(units)

    # 1. Master flag off -> byte-identical passthrough, no matrix built.
    if not parallel_dispatch_enabled():
        base = is_fanout_eligible(
            op_id=op_id,
            n_candidate_files=len(unit_list),
            gate=gate,
            posture_fn=posture_fn,
            emit_log=emit_log,
        )
        return CollisionAwareEligibility(
            allowed=False,
            reason_code=base.reason_code,
            parallel_units=(),
            sequential_units=tuple(unit_list),
            base_eligibility=base,
        )

    # 2. Build the zero-trust collision matrix + partition.
    matrix = build_collision_matrix(unit_list, oracle=oracle)
    parallel_groups, sequential_forced = partition_parallel_safe(
        unit_list, oracle=oracle, matrix=matrix
    )

    # 3. Pick the largest disjoint group (deterministic tie-break: the
    #    group whose sorted unit_ids come first).
    best_group: List[WorkUnitSpec] = []
    for group in parallel_groups:
        if len(group) > len(best_group) or (
            len(group) == len(best_group)
            and _group_sig(group) < _group_sig(best_group)
        ):
            best_group = group

    # Run the underlying chain on the disjoint group's degree.
    base = is_fanout_eligible(
        op_id=op_id,
        n_candidate_files=len(best_group),
        gate=gate,
        posture_fn=posture_fn,
        emit_log=emit_log,
    )

    # 4. Compose the final split. The underlying chain may clamp below the
    #    disjoint group size (posture / memory / max_units) -- trim the
    #    fan-out set and push the trimmed units to sequential.
    n_allowed = max(0, int(base.n_allowed))
    # Deterministic order within the chosen group.
    chosen_sorted = sorted(best_group, key=lambda u: u.unit_id)
    if base.allowed and n_allowed >= 2:
        parallel_units = tuple(chosen_sorted[:n_allowed])
        trimmed = tuple(chosen_sorted[n_allowed:])
    else:
        # Nothing parallelizes -- whole group drops to serial.
        parallel_units = ()
        trimmed = tuple(chosen_sorted)

    sequential_units = tuple(
        sorted(
            list(sequential_forced) + list(trimmed),
            key=lambda u: u.unit_id,
        )
    )

    if len(parallel_units) >= 2:
        allowed = True
        reason_code = base.reason_code
    else:
        # Collision matrix (or the chain on the disjoint subset) collapsed
        # fan-out below 2 -> the dormant reason code fires here.
        allowed = False
        reason_code = ReasonCode.COLLISION_FORCED_SEQUENTIAL

    if emit_log:
        logger.info(
            "[CollisionMatrix] op=%s units=%d parallel=%d sequential=%d "
            "groups=%d reason=%s",
            op_id[:16],
            len(unit_list),
            len(parallel_units),
            len(sequential_units),
            len(parallel_groups),
            reason_code.value,
        )

    return CollisionAwareEligibility(
        allowed=allowed,
        reason_code=reason_code,
        parallel_units=parallel_units,
        sequential_units=sequential_units,
        base_eligibility=base,
    )


def _group_sig(group: Sequence[WorkUnitSpec]) -> Tuple[str, ...]:
    """Deterministic signature of a group (sorted unit_ids)."""
    return tuple(sorted(u.unit_id for u in group))
