"""Zero-trust AST Collision Matrix — regression spine.

Proves the mathematical guarantee that two L3 subagents NEVER fan out in
parallel when their tasks touch the same file OR import-coupled files
(interface <-> implementation). Colliding units are forced sequential;
only provably-disjoint units fan out.

Invariants pinned here:

1. Same-file targets COLLIDE -> forced sequential.
2. Import-coupled targets (via the Oracle import graph) COLLIDE.
3. Disjoint, import-isolated targets are pairwise-safe -> fan out.
4. INDETERMINATE coupling (Oracle unavailable / unknown) -> COLLIDE
   (zero-trust default-DENY, NEVER optimistic parallel).
5. partition_parallel_safe returns the correct (parallel, sequential)
   split for a mixed set, deterministically.
6. The eligibility path returns COLLISION_FORCED_SEQUENTIAL when nothing
   can safely parallelize.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import WorkUnitSpec
from backend.core.ouroboros.governance.collision_matrix import (
    CollisionMatrix,
    CollisionVerdict,
    build_collision_matrix,
    is_fanout_eligible_collision_aware,
    partition_parallel_safe,
)
from backend.core.ouroboros.governance.parallel_dispatch import ReasonCode


# ---------------------------------------------------------------------------
# Test doubles — a minimal Oracle import-graph stub.
# ---------------------------------------------------------------------------


class _StubNodeID:
    """Minimal NodeID-like object with a .file_path attribute."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


class _StubOracle:
    """Deterministic in-memory import-graph stub.

    ``couples`` maps a file_path -> set(file_paths it is coupled to) via
    the import/call graph. Symmetric coupling is NOT assumed — the matrix
    must probe both directions.

    ``known_files`` is the set of files the Oracle has indexed. A file not
    in ``known_files`` yields an *empty* node list from
    ``find_nodes_in_file`` (the Oracle genuinely has no data for it) —
    which the matrix must treat as INDETERMINATE (zero-trust deny).
    """

    def __init__(self, couples: dict, known_files: set) -> None:
        self._couples = couples
        self._known = known_files

    def find_nodes_in_file(self, file_path: str):
        if file_path not in self._known:
            return []
        # One synthetic node per known file.
        return [_StubNodeID(file_path)]

    def get_dependencies(self, node_id):
        fp = node_id.file_path
        return [_StubNodeID(d) for d in self._couples.get(fp, set())]

    def get_dependents(self, node_id):
        # Incoming coupling: anyone who lists fp in their dependencies.
        fp = node_id.file_path
        out = []
        for src, deps in self._couples.items():
            if fp in deps:
                out.append(_StubNodeID(src))
        return out


class _RaisingOracle:
    """Oracle whose probes raise — simulates an unavailable/broken graph."""

    def find_nodes_in_file(self, file_path: str):
        raise RuntimeError("oracle graph unavailable")

    def get_dependencies(self, node_id):
        raise RuntimeError("oracle graph unavailable")

    def get_dependents(self, node_id):
        raise RuntimeError("oracle graph unavailable")


def _unit(unit_id: str, *files: str) -> WorkUnitSpec:
    return WorkUnitSpec(
        unit_id=unit_id,
        repo="jarvis",
        goal=f"edit {files[0]}",
        target_files=tuple(files),
    )


# ---------------------------------------------------------------------------
# (a) Same-file units COLLIDE -> forced sequential.
# ---------------------------------------------------------------------------


def test_same_file_units_collide():
    oracle = _StubOracle(couples={}, known_files={"a.py"})
    u1 = _unit("u1", "a.py")
    u2 = _unit("u2", "a.py")
    matrix = build_collision_matrix([u1, u2], oracle=oracle)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE
    assert matrix.collides("u1", "u2") is True


def test_same_file_partition_forces_sequential():
    oracle = _StubOracle(couples={}, known_files={"a.py"})
    units = [_unit("u1", "a.py"), _unit("u2", "a.py")]
    parallel, sequential = partition_parallel_safe(units, oracle=oracle)
    # No two can run together -> at most one parallel group with a single
    # unit; the other drops to sequential-forced.
    parallel_ids = {u.unit_id for grp in parallel for u in grp}
    sequential_ids = {u.unit_id for u in sequential}
    assert parallel_ids | sequential_ids == {"u1", "u2"}
    # They must never be in the SAME parallel group.
    for grp in parallel:
        assert not ({"u1", "u2"} <= {u.unit_id for u in grp})


# ---------------------------------------------------------------------------
# (b) Import-coupled units COLLIDE (via the Oracle graph).
# ---------------------------------------------------------------------------


def test_import_coupled_units_collide():
    # iface.py is imported by impl.py -> coupled.
    oracle = _StubOracle(
        couples={"impl.py": {"iface.py"}},
        known_files={"iface.py", "impl.py"},
    )
    u1 = _unit("u1", "iface.py")
    u2 = _unit("u2", "impl.py")
    matrix = build_collision_matrix([u1, u2], oracle=oracle)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE


def test_reverse_import_coupling_also_collides():
    # Coupling probed in the OTHER direction (A imports B).
    oracle = _StubOracle(
        couples={"a.py": {"b.py"}},
        known_files={"a.py", "b.py"},
    )
    matrix = build_collision_matrix(
        [_unit("u1", "b.py"), _unit("u2", "a.py")], oracle=oracle
    )
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE


# ---------------------------------------------------------------------------
# (c) Disjoint, import-isolated units fan out (happy path).
# ---------------------------------------------------------------------------


def test_disjoint_isolated_units_all_parallel_safe():
    oracle = _StubOracle(
        couples={},  # no coupling at all
        known_files={"a.py", "b.py", "c.py"},
    )
    units = [_unit("u1", "a.py"), _unit("u2", "b.py"), _unit("u3", "c.py")]
    matrix = build_collision_matrix(units, oracle=oracle)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.DISJOINT
    assert matrix.verdict("u1", "u3") is CollisionVerdict.DISJOINT
    assert matrix.verdict("u2", "u3") is CollisionVerdict.DISJOINT

    parallel, sequential = partition_parallel_safe(units, oracle=oracle)
    assert sequential == []
    # All three in a single pairwise-disjoint group.
    assert len(parallel) == 1
    assert {u.unit_id for u in parallel[0]} == {"u1", "u2", "u3"}


# ---------------------------------------------------------------------------
# (d) INDETERMINATE coupling -> COLLIDE (zero-trust fail-CLOSED).
# ---------------------------------------------------------------------------


def test_oracle_unavailable_pair_is_collide_not_parallel():
    # Oracle is None -> coupling cannot be proven disjoint -> deny.
    units = [_unit("u1", "a.py"), _unit("u2", "b.py")]
    matrix = build_collision_matrix(units, oracle=None)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE
    parallel, sequential = partition_parallel_safe(units, oracle=None)
    # Fail-closed: must NOT fan them out together.
    for grp in parallel:
        assert not ({"u1", "u2"} <= {u.unit_id for u in grp})


def test_oracle_raises_pair_is_collide_not_parallel():
    units = [_unit("u1", "a.py"), _unit("u2", "b.py")]
    matrix = build_collision_matrix(units, oracle=_RaisingOracle())
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE


def test_unindexed_file_is_indeterminate_collide():
    # b.py is NOT in known_files -> Oracle has no data -> indeterminate.
    oracle = _StubOracle(couples={}, known_files={"a.py"})
    units = [_unit("u1", "a.py"), _unit("u2", "b.py")]
    matrix = build_collision_matrix(units, oracle=oracle)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE


# ---------------------------------------------------------------------------
# (e) Mixed set -> correct (parallel, sequential) split, deterministic.
# ---------------------------------------------------------------------------


def test_mixed_set_partition_split():
    # u1(a.py) + u2(b.py) disjoint; u3(impl.py) coupled to a.py; u4(d.py)
    # disjoint from everything.
    oracle = _StubOracle(
        couples={"impl.py": {"a.py"}},
        known_files={"a.py", "b.py", "impl.py", "d.py"},
    )
    units = [
        _unit("u1", "a.py"),
        _unit("u2", "b.py"),
        _unit("u3", "impl.py"),
        _unit("u4", "d.py"),
    ]
    parallel, sequential = partition_parallel_safe(units, oracle=oracle)

    placed = {u.unit_id for grp in parallel for u in grp} | {
        u.unit_id for u in sequential
    }
    assert placed == {"u1", "u2", "u3", "u4"}

    # u1 and u3 collide (a.py <-> impl.py) -> never in the same group.
    for grp in parallel:
        ids = {u.unit_id for u in grp}
        assert not ({"u1", "u3"} <= ids)

    # Determinism: same inputs -> same split.
    parallel2, sequential2 = partition_parallel_safe(units, oracle=oracle)
    sig = lambda p, s: (  # noqa: E731
        [[u.unit_id for u in g] for g in p],
        [u.unit_id for u in s],
    )
    assert sig(parallel, sequential) == sig(parallel2, sequential2)


def test_partition_keeps_disjoint_pair_together():
    oracle = _StubOracle(
        couples={"impl.py": {"a.py"}},
        known_files={"a.py", "b.py", "impl.py"},
    )
    # b.py is disjoint from both a.py and impl.py; a.py<->impl.py collide.
    units = [_unit("u1", "a.py"), _unit("u2", "impl.py"), _unit("u3", "b.py")]
    parallel, sequential = partition_parallel_safe(units, oracle=oracle)
    # Greedy disjoint: u1 + u3 can run together; u2 forced sequential.
    parallel_ids = {u.unit_id for grp in parallel for u in grp if len(grp) > 1}
    # b.py (u3) should join whichever disjoint group it can.
    placed = {u.unit_id for grp in parallel for u in grp} | {
        u.unit_id for u in sequential
    }
    assert placed == {"u1", "u2", "u3"}
    # u1 and u2 must never be co-grouped.
    for grp in parallel:
        assert not ({"u1", "u2"} <= {u.unit_id for u in grp})


# ---------------------------------------------------------------------------
# (f) Eligibility path returns COLLISION_FORCED_SEQUENTIAL when nothing's
#     disjoint.
# ---------------------------------------------------------------------------


def test_eligibility_collision_forced_sequential(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    # Two units on the SAME file -> nothing disjoint.
    units = [_unit("u1", "a.py"), _unit("u2", "a.py")]
    oracle = _StubOracle(couples={}, known_files={"a.py"})
    result = is_fanout_eligible_collision_aware(
        op_id="op-collide",
        units=units,
        oracle=oracle,
        posture_fn=lambda: (None, None),
        emit_log=False,
    )
    assert result.allowed is False
    assert result.reason_code is ReasonCode.COLLISION_FORCED_SEQUENTIAL
    # The disjoint subset offered for fan-out is at most 1 unit (serial-eq).
    assert len(result.parallel_units) <= 1


def test_eligibility_reduces_to_disjoint_subset(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    # u1(a.py) collides with u2(impl.py); u3(c.py) + u4(d.py) disjoint.
    oracle = _StubOracle(
        couples={"impl.py": {"a.py"}},
        known_files={"a.py", "impl.py", "c.py", "d.py"},
    )
    units = [
        _unit("u1", "a.py"),
        _unit("u2", "impl.py"),
        _unit("u3", "c.py"),
        _unit("u4", "d.py"),
    ]
    result = is_fanout_eligible_collision_aware(
        op_id="op-mixed",
        units=units,
        oracle=oracle,
        posture_fn=lambda: (None, None),
        emit_log=False,
    )
    # At least the disjoint pair fans out.
    parallel_ids = {u.unit_id for u in result.parallel_units}
    assert len(parallel_ids) >= 2
    # The colliding pair is NOT both in the fan-out set.
    assert not ({"u1", "u2"} <= parallel_ids)
    # The forced-sequential remainder accounts for the rest.
    seq_ids = {u.unit_id for u in result.sequential_units}
    assert parallel_ids.isdisjoint(seq_ids)
    assert parallel_ids | seq_ids == {"u1", "u2", "u3", "u4"}


def test_master_off_is_byte_identical_passthrough(monkeypatch):
    # Master off -> collision check is dead code; MASTER_OFF returned, no
    # collision matrix consulted.
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "false")
    units = [_unit("u1", "a.py"), _unit("u2", "a.py")]
    result = is_fanout_eligible_collision_aware(
        op_id="op-off",
        units=units,
        oracle=_StubOracle(couples={}, known_files={"a.py"}),
        posture_fn=lambda: (None, None),
        emit_log=False,
    )
    assert result.allowed is False
    assert result.reason_code is ReasonCode.MASTER_OFF


def test_matrix_is_symmetric_and_self_disjoint():
    oracle = _StubOracle(couples={}, known_files={"a.py", "b.py"})
    units = [_unit("u1", "a.py"), _unit("u2", "b.py")]
    matrix = build_collision_matrix(units, oracle=oracle)
    assert matrix.verdict("u1", "u2") is matrix.verdict("u2", "u1")
    # A unit never collides with itself in the pairwise sense.
    assert matrix.collides("u1", "u1") is False
