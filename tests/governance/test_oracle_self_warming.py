"""Autonomous Self-Warming Oracle -- regression spine.

Proves the cold-boot fix for the Meta-Goal Aggregator's
``aged out of coalescing window -> legacy single-file dispatch (no disjoint
sibling found)`` blocker. On a fresh node the Oracle has NO indexed data, so
``CollisionMatrix._coupled_files`` returns indeterminate -> COLLIDE
(zero-trust) -> the Aggregator can never prove N chaos targets disjoint -> no
fan-out. This spine pins:

(a) cold-boot JIT: an empty Oracle, given 3 import-disjoint files, JIT-indexes
    them on a ``_coupled_files`` MISS and the CollisionMatrix returns DISJOINT
    -> a >=2-unit fan-out bundle (``allowed=true n=3``) -- THE FIX.
(b) gate INTACT: genuinely import-coupled files STILL COLLIDE after the JIT
    (self-warming UPGRADES the gate, never bypasses it).
(c) async dedup: 3 concurrent ``ensure_file_indexed`` for the SAME file invoke
    the underlying ``_index_file`` exactly ONCE (no thundering herd).
(d) cycle + depth: A imports B imports A terminates via ``visited`` and the
    hard ``JARVIS_ORACLE_JIT_MAX_DEPTH`` cap; no infinite recursion / crash.
(e) crypto invalidation: a pre-warm payload whose sha256 matches the live file
    warms with NO JIT; a sha256 MISMATCH discards the payload + falls back to
    the JIT; a missing payload falls back to the JIT.
(f) state-aware window: an op whose disjointness proof is in-flight is NOT
    aged out; the pause is hard-bounded by ``JARVIS_META_GOAL_PROOF_MAX_WAIT_S``
    and fail-closes (release the hold) on a hung proof.
(g) OFF byte-identical: master flag off -> ``_coupled_files`` miss ->
    indeterminate -> COLLIDE, NO JIT, NO payload, NO window-pause.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import collision_matrix as cm
from backend.core.ouroboros.governance.collision_matrix import (
    CollisionVerdict,
    build_collision_matrix,
    partition_parallel_safe,
    prewarm_collision_files,
    self_warming_enabled,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import WorkUnitSpec


# ---------------------------------------------------------------------------
# Self-warming Oracle test double -- reuses the JIT contract, not the parser.
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


class _WarmingOracle:
    """A fake Oracle that mimics the real ``ensure_file_indexed`` JIT.

    The "disk truth" is two dicts injected at construction:

    - ``known``: file_path -> bool whether the file is *indexable* (a parse
      error file is absent / False).
    - ``couples``: file_path -> set(import-coupled file_paths). One-hop
      neighbourhood the JIT would index.

    The Oracle starts COLD (``_file_index`` empty). ``ensure_file_indexed``
    populates ``_file_index`` for the target + its one-hop neighbours by
    calling the (counted) ``_index_file`` reuse-point. Concurrent calls for
    the same path share ONE in-flight future (the dedup contract).
    """

    def __init__(self, known, couples, *, parse_fail=None) -> None:
        self._known = dict(known)
        self._couples = {k: set(v) for k, v in couples.items()}
        self._parse_fail = set(parse_fail or ())
        self._file_index = {}  # warmed state
        self._index_calls = {}  # file_path -> count (dedup assertion)
        self._in_flight_indexes = {}
        self._index_delay = 0.0  # let tests force overlap

    # -- the reuse point the JIT drives (analogue of TheOracle._index_file) --
    async def _index_file_one(self, file_path: str) -> bool:
        self._index_calls[file_path] = self._index_calls.get(file_path, 0) + 1
        if self._index_delay:
            await asyncio.sleep(self._index_delay)
        if file_path in self._parse_fail:
            return False
        if not self._known.get(file_path, False):
            return False
        self._file_index[file_path] = {f"{file_path}::node"}
        return True

    # -- the JIT (Component 1) ----------------------------------------------
    async def ensure_file_indexed(self, file_path, *, _visited=None, _depth=0):
        return await cm._oracle_ensure_file_indexed(
            self, file_path, _visited=_visited, _depth=_depth,
        )

    def _jit_one_hop_neighbours(self, file_path):
        return set(self._couples.get(file_path, set()))

    async def _jit_index_single(self, file_path) -> bool:
        # dedup-aware single-file index (the JIT calls THIS, which guards the
        # one-call-per-file invariant via _in_flight_indexes).
        existing = self._in_flight_indexes.get(file_path)
        if existing is not None:
            return await existing
        fut = asyncio.ensure_future(self._index_file_one(file_path))
        self._in_flight_indexes[file_path] = fut
        try:
            return await fut
        finally:
            self._in_flight_indexes.pop(file_path, None)

    # -- the sync collision-matrix probe surface ----------------------------
    def find_nodes_in_file(self, file_path):
        return [_Node(file_path) for _ in self._file_index.get(file_path, ())]

    def get_dependencies(self, node):
        return [_Node(c) for c in self._couples.get(node.file_path, set())]

    def get_dependents(self, node):
        out = []
        for src, deps in self._couples.items():
            if node.file_path in deps:
                out.append(_Node(src))
        return out


def _unit(unit_id, *files):
    return WorkUnitSpec(
        unit_id=unit_id,
        repo="jarvis",
        goal=f"edit {files[0]}",
        target_files=tuple(files),
    )


@pytest.fixture(autouse=True)
def _enable_self_warming(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_ORACLE_JIT_MAX_DEPTH", "2")
    yield


# ---------------------------------------------------------------------------
# (a) Cold-boot JIT -> DISJOINT -> bundle allowed=true n=3 (THE FIX).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_boot_jit_proves_disjoint_then_bundles_n3(monkeypatch):
    monkeypatch.setenv("JARVIS_META_GOAL_AGGREGATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_META_GOAL_MIN_OPS", "2")
    # Three genuinely import-disjoint files; the Oracle starts COLD.
    oracle = _WarmingOracle(
        known={"a.py": True, "b.py": True, "c.py": True},
        couples={"a.py": set(), "b.py": set(), "c.py": set()},
    )
    files = ["a.py", "b.py", "c.py"]
    # Cold: with no warming the matrix would COLLIDE on every pair.
    assert oracle._file_index == {}

    # Async pre-warm (the seam the aggregator runs before the sync partition).
    await prewarm_collision_files(oracle, files)
    # JIT populated the index for the 3 cold targets.
    assert set(oracle._file_index) >= set(files)

    units = [_unit("u1", "a.py"), _unit("u2", "b.py"), _unit("u3", "c.py")]
    matrix = build_collision_matrix(units, oracle=oracle)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.DISJOINT
    assert matrix.verdict("u1", "u3") is CollisionVerdict.DISJOINT
    assert matrix.verdict("u2", "u3") is CollisionVerdict.DISJOINT

    parallel, sequential = partition_parallel_safe(units, oracle=oracle, matrix=matrix)
    # All 3 disjoint -> ONE fan-out group of 3 (the bundle), nothing forced serial.
    assert len(parallel) == 1
    assert len(parallel[0]) == 3
    assert sequential == []


# ---------------------------------------------------------------------------
# (b) Coupled files STILL COLLIDE after the JIT (gate intact, not bypassed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coupled_files_still_collide_after_jit():
    # a.py imports b.py -> genuinely coupled even once both are indexed.
    oracle = _WarmingOracle(
        known={"a.py": True, "b.py": True},
        couples={"a.py": {"b.py"}, "b.py": set()},
    )
    await prewarm_collision_files(oracle, ["a.py", "b.py"])
    assert "a.py" in oracle._file_index and "b.py" in oracle._file_index

    units = [_unit("u1", "a.py"), _unit("u2", "b.py")]
    matrix = build_collision_matrix(units, oracle=oracle)
    # The JIT warmed the index, but the real import edge keeps them COLLIDING.
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE


@pytest.mark.asyncio
async def test_parse_error_file_collides_after_jit():
    # b.py cannot be indexed (parse error) -> still indeterminate -> COLLIDE.
    oracle = _WarmingOracle(
        known={"a.py": True, "b.py": True},
        couples={"a.py": set(), "b.py": set()},
        parse_fail={"b.py"},
    )
    await prewarm_collision_files(oracle, ["a.py", "b.py"])
    units = [_unit("u1", "a.py"), _unit("u2", "b.py")]
    matrix = build_collision_matrix(units, oracle=oracle)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE


# ---------------------------------------------------------------------------
# (c) Async dedup: concurrent ensure_file_indexed(same) -> ONE _index_file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_dedup_index_file_invoked_once():
    oracle = _WarmingOracle(
        known={"utils.py": True},
        couples={"utils.py": set()},
    )
    oracle._index_delay = 0.02  # force the three calls to overlap

    results = await asyncio.gather(
        oracle.ensure_file_indexed("utils.py"),
        oracle.ensure_file_indexed("utils.py"),
        oracle.ensure_file_indexed("utils.py"),
    )
    assert all(results)
    # Exactly ONE underlying parse despite three concurrent callers.
    assert oracle._index_calls["utils.py"] == 1
    # In-flight map cleaned in finally.
    assert oracle._in_flight_indexes == {}


# ---------------------------------------------------------------------------
# (d) Cycle terminates via visited + hard depth cap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circular_import_terminates_via_visited(monkeypatch):
    # A imports B imports A -> the JIT must not recurse forever.
    oracle = _WarmingOracle(
        known={"a.py": True, "b.py": True},
        couples={"a.py": {"b.py"}, "b.py": {"a.py"}},
    )
    ok = await asyncio.wait_for(oracle.ensure_file_indexed("a.py"), timeout=2.0)
    assert ok is True
    # Both reachable files were indexed exactly once (visited breaks the cycle).
    assert oracle._index_calls.get("a.py") == 1
    assert oracle._index_calls.get("b.py") == 1


@pytest.mark.asyncio
async def test_depth_cap_bounds_the_walk(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_JIT_MAX_DEPTH", "1")
    # a -> b -> c chain; depth 1 indexes a + its 1-hop (b), NOT c.
    oracle = _WarmingOracle(
        known={"a.py": True, "b.py": True, "c.py": True},
        couples={"a.py": {"b.py"}, "b.py": {"c.py"}, "c.py": set()},
    )
    await oracle.ensure_file_indexed("a.py")
    assert "a.py" in oracle._file_index
    assert "b.py" in oracle._file_index  # 1-hop neighbour
    assert "c.py" not in oracle._file_index  # beyond the depth cap


# ---------------------------------------------------------------------------
# (e) Crypto invalidation of the pre-warm payload.
# ---------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_prewarm_payload_sha_match_warms_without_jit(tmp_path, monkeypatch):
    from backend.core.ouroboros import oracle as oracle_mod

    f = tmp_path / "tgt.py"
    f.write_text("x = 1\n")
    payload = {
        "schema_version": 1,
        "targets": [
            {"file_path": str(f), "sha256": _sha256_of(f), "coupled": []},
        ],
    }
    payload_path = tmp_path / "oracle_prewarm.json"
    payload_path.write_text(json.dumps(payload))

    o = oracle_mod.TheOracle()
    n = o.ingest_prewarm_payload(str(payload_path))
    assert n == 1
    # Index warmed from the payload -> find_nodes_in_file is now non-empty.
    assert o.find_nodes_in_file(str(f))


@pytest.mark.asyncio
async def test_prewarm_payload_sha_mismatch_discards(tmp_path, monkeypatch):
    from backend.core.ouroboros import oracle as oracle_mod

    f = tmp_path / "tgt.py"
    f.write_text("x = 1\n")
    payload = {
        "schema_version": 1,
        "targets": [
            {"file_path": str(f), "sha256": "deadbeef" * 8, "coupled": []},
        ],
    }
    payload_path = tmp_path / "oracle_prewarm.json"
    payload_path.write_text(json.dumps(payload))

    o = oracle_mod.TheOracle()
    n = o.ingest_prewarm_payload(str(payload_path))
    # Stale payload (sha mismatch) -> DISCARDED entirely, nothing warmed.
    assert n == 0
    assert not o.find_nodes_in_file(str(f))


@pytest.mark.asyncio
async def test_missing_payload_falls_back_gracefully(tmp_path):
    from backend.core.ouroboros import oracle as oracle_mod

    o = oracle_mod.TheOracle()
    n = o.ingest_prewarm_payload(str(tmp_path / "does_not_exist.json"))
    assert n == 0  # missing -> graceful no-op, no raise


# ---------------------------------------------------------------------------
# (f) State-aware coalescing window + absolute anti-zombie ceiling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proof_in_flight_holds_window(monkeypatch):
    from backend.core.ouroboros.governance import meta_goal_wiring as wiring
    from backend.core.ouroboros.governance.meta_goal_aggregator import (
        MetaGoalAggregator,
        PooledOp,
    )
    import time as _time

    monkeypatch.setenv("JARVIS_META_GOAL_AGGREGATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_META_GOAL_COALESCE_WINDOW_S", "0.0")  # everything aged
    monkeypatch.setenv("JARVIS_META_GOAL_PROOF_MAX_WAIT_S", "15.0")

    class _Host:
        pass

    host = _Host()
    agg = MetaGoalAggregator()
    host._meta_goal_aggregator = agg
    host._bg_pool = _CountingPool()
    host._meta_goal_pending_ctx = {}

    # Back-date offered_at so the op is genuinely past the (0s) window.
    op = PooledOp(
        op_id="op-1", file_path="a.py", rationale="fix",
        offered_at=_time.monotonic() - 100.0,
    )
    agg.offer(op)
    host._meta_goal_pending_ctx["op-1"] = _Ctx("op-1")
    # Mark a proof in-flight for this op -> the window pauses, NOT aged out.
    wiring.mark_proof_in_flight(host, "op-1")

    await wiring._flush_aged_ops(host, agg)
    # Held: NOT flushed to the legacy single-file pool while the proof builds.
    assert host._bg_pool.submitted == []
    assert "op-1" in {p.op_id for p in agg.pending_ops()}


@pytest.mark.asyncio
async def test_proof_exceeds_ceiling_fail_closes(monkeypatch):
    from backend.core.ouroboros.governance import meta_goal_wiring as wiring
    from backend.core.ouroboros.governance.meta_goal_aggregator import (
        MetaGoalAggregator,
        PooledOp,
    )
    import time as _time

    monkeypatch.setenv("JARVIS_META_GOAL_AGGREGATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_META_GOAL_COALESCE_WINDOW_S", "0.0")
    monkeypatch.setenv("JARVIS_META_GOAL_PROOF_MAX_WAIT_S", "0.0")  # ceiling already passed

    class _Host:
        pass

    host = _Host()
    agg = MetaGoalAggregator()
    host._meta_goal_aggregator = agg
    host._bg_pool = _CountingPool()
    host._meta_goal_pending_ctx = {}

    op = PooledOp(
        op_id="op-1", file_path="a.py", rationale="fix",
        offered_at=_time.monotonic() - 100.0,
    )
    agg.offer(op)
    host._meta_goal_pending_ctx["op-1"] = _Ctx("op-1")
    wiring.mark_proof_in_flight(host, "op-1")

    await wiring._flush_aged_ops(host, agg)
    # Ceiling passed -> fail-closed: the hold is released, op flushed to legacy.
    assert host._bg_pool.submitted == ["op-1"]


# ---------------------------------------------------------------------------
# (g) OFF byte-identical: miss -> COLLIDE, no JIT, no payload, no pause.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_byte_identical_no_jit(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "false")
    assert self_warming_enabled() is False
    oracle = _WarmingOracle(
        known={"a.py": True, "b.py": True},
        couples={"a.py": set(), "b.py": set()},
    )
    # No pre-warm should run; even if called, OFF -> no JIT.
    await prewarm_collision_files(oracle, ["a.py", "b.py"])
    assert oracle._index_calls == {}  # JIT never fired

    units = [_unit("u1", "a.py"), _unit("u2", "b.py")]
    matrix = build_collision_matrix(units, oracle=oracle)
    # Cold + OFF -> indeterminate -> COLLIDE (byte-identical pre-feature).
    assert matrix.verdict("u1", "u2") is CollisionVerdict.COLLIDE


# ---------------------------------------------------------------------------
# Test doubles for the window tests.
# ---------------------------------------------------------------------------


class _CountingPool:
    def __init__(self):
        self.submitted = []

    async def submit(self, ctx):
        self.submitted.append(ctx.op_id)


class _Ctx:
    def __init__(self, op_id):
        self.op_id = op_id
        self.target_files = ("a.py",)
        self.goal = "fix"
