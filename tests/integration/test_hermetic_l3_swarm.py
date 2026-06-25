"""Hermetic L3 Concurrency Validator -- the $0 in-memory L3 fan-out proof rig.

The L3 analog of ``test_hermetic_a1_matrix.py``. Where the A1 matrix proves the
SENSE->DETECT->DISPATCH leg locally, this rig proves the downstream
DISPATCH->FAN-OUT->RECOMPOSE leg: a collision-cleared set of import-isolated
chaos targets fans out into N PARALLEL git-worktree subagents, each runs a
simulated DoubleWord inference, the per-unit patches recompose into ONE
multi-file candidate, and a worker failure fails CLOSED to the legacy serial
path. All local, async, event-driven, in milliseconds, for $0 (NO GCP, NO real
DW calls -- only the generation is stubbed; the orchestration is REAL).

What is REAL here (not mocked):

* A real ``git init`` fixture repo carrying 3 import-isolated pure-leaf
  functions + their 3 GREEN pytests, committed.
* The real ``DecomposableChaosInjector`` (``acquire_isolated_targets(3)`` /
  ``do_inject_decomposable``) -- the same external saboteur the live-fire chaos
  harness uses -- acquires the 3 mutually-isolated targets and plants the 3 RED
  mutations against the FIXTURE repo.
* The real zero-trust ``CollisionMatrix`` (``build_collision_matrix`` /
  ``partition_parallel_safe``) -- proves the 3 disjoint targets partition into
  exactly ONE parallel group (the matrix is the gate that authorizes fan-out).
* The real ``SubagentScheduler`` + real ``GenerationSubagentExecutor`` + real
  ``WorktreeManager`` -- the live scheduler drives the wave: real ``git worktree
  add`` per unit, real concurrency batching, real worktree reap. Only the
  generator inside the executor is a stub.
* The real ``MemoryPressureGate`` consultation inside the scheduler (the RAM
  backpressure cap the demo respects to protect the local host).
* The real ``compose_fanout_result`` (DAGComposer) -- recomposes the terminal
  scheduler results into ONE multi-file candidate of the exact shape the
  orchestrator's multi-file APPLY path consumes.
* The real ``[L3Telemetry]`` emits (per-unit worktree lifespan + simulated DW
  cost + graph-aggregate).

The ONLY bypassed boundary is the DoubleWord cloud edge -- a STUB generator
that returns a real candidate (a fix for the unit's own file) + a ``cost_usd``
+ blocks on a per-unit ``asyncio.Event`` the test fires, so concurrency is
DETERMINISTIC and event-driven, never sleep-based. Each worker captures the
exact ``time.monotonic()`` (ms) it starts and finishes its simulated inference,
so concurrency is PROVEN by OVERLAP of captured timestamps, not assumed.

ACCEPTANCE BAR: this file GREEN ==
  (a) the 3 collision-cleared targets partition into ONE parallel group,
  (b) 3 worktrees are created concurrently (overlapping captured windows) and
      ALL reaped (none leaked),
  (c) the fan-out is event-driven (no sync sleeps) and the concurrency cap is
      respected (RAM protection),
  (d) ``[L3Telemetry]`` captures each worktree's lifespan + simulated DW cost +
      a graph-aggregate,
  (e) the DAGComposer recomposes EXACTLY 3 file-patches into ONE multi-file
      candidate (zero dropped), and a simulated worker FAILURE ->
      ``ComposeFailure`` -> legacy serial (no partial compose).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

# --- REAL L3 chassis (driven, not mocked) ---------------------------------
from backend.core.ouroboros.governance.collision_matrix import (
    CollisionVerdict,
    build_collision_matrix,
    partition_parallel_safe,
)
from backend.core.ouroboros.governance.dag_composer import (
    ComposeFailureReason,
    compose_fanout_result,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision,
    PressureLevel,
    reset_default_gate,
)
from backend.core.ouroboros.governance.worktree_manager import WorktreeManager
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
    ExecutionGraphStore,
)
from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
    GenerationSubagentExecutor,
    SubagentScheduler,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    WorkUnitSpec,
    WorkUnitState,
)

# --- The real external saboteur (standalone script; load by path) ---------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import chaos_injector_ast as ci  # noqa: E402


# ===========================================================================
# Fixture repo -- real git init, 3 import-isolated green leaves, 3 RED chaos
# ===========================================================================


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# Three import-isolated pure-leaf functions. Each lives in its OWN module and
# imports NOTHING from the others -> the zero-trust collision matrix proves
# them pairwise DISJOINT -> they fan out as a single parallel group.
_N_TARGETS = 3


def _leaf_src(i: int) -> str:
    # ``a + b + i`` is a pure leaf; the chaos injector flips ``+`` -> ``-``,
    # turning the leaf's own green assertion RED while keeping it pure.
    return f"def compute{i}(a, b):\n    return a + b + {i}\n"


def _leaf_test_src(i: int) -> str:
    return (
        f"from src.calc{i} import compute{i}\n"
        "\n"
        "\n"
        f"def test_compute{i}():\n"
        f"    assert compute{i}(1, 2) == {3 + i}\n"
    )


@pytest.fixture
def l3_chaos_repo(tmp_path: Path) -> Iterator[dict]:
    """A real committed git repo: 3 import-isolated GREEN leaves, then the real
    DecomposableChaosInjector plants 3 RED mutations (one per file).

    Yields a dict carrying the repo ``root``, the absolute ``targets`` paths,
    and the acquired ``ChaosTarget`` list (so the test asserts on real injector
    output, not fabricated paths).
    """
    root = tmp_path / "l3_fixture"
    src_dir = root / "src"
    tests_dir = root / "tests"
    src_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    # ``conftest.py`` at root so ``from src.calcN import ...`` resolves.
    (root / "conftest.py").write_text("")
    (src_dir / "__init__.py").write_text("")

    for i in range(_N_TARGETS):
        (src_dir / f"calc{i}.py").write_text(_leaf_src(i))
        (tests_dir / f"test_calc{i}.py").write_text(_leaf_test_src(i))

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "hermetic-l3@matrix.local")
    _git(root, "config", "user.name", "Hermetic L3 Matrix")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "green isolated baseline")

    # Drive the REAL injector against the fixture. JARVIS_CHAOS_TARGET_DIRS
    # points it at the fixture's src/ (the only bypass: directing the saboteur
    # at the fixture instead of backend/). Readiness probe off -- no live bus.
    saved_dirs = os.environ.get("JARVIS_CHAOS_TARGET_DIRS")
    saved_probe = os.environ.get("JARVIS_CHAOS_READINESS_PROBE_ENABLED")
    os.environ["JARVIS_CHAOS_TARGET_DIRS"] = str(src_dir)
    os.environ["JARVIS_CHAOS_READINESS_PROBE_ENABLED"] = "false"
    try:
        cfg = ci.InjectConfig(
            repo_root=str(root), test_timeout_s=30.0, verify_green=True,
        )
        targets = ci.acquire_isolated_targets(cfg, n=_N_TARGETS)
        assert len(targets) == _N_TARGETS, (
            f"injector acquired {len(targets)}/{_N_TARGETS} isolated targets "
            "(honest-fewer, NOT fabricated) -- fixture should yield exactly 3"
        )
        rc = ci.do_inject_decomposable(cfg, n=_N_TARGETS, require_exact=True)
        assert rc == 0, f"decomposable inject failed rc={rc}"
    finally:
        if saved_dirs is None:
            os.environ.pop("JARVIS_CHAOS_TARGET_DIRS", None)
        else:
            os.environ["JARVIS_CHAOS_TARGET_DIRS"] = saved_dirs
        if saved_probe is None:
            os.environ.pop("JARVIS_CHAOS_READINESS_PROBE_ENABLED", None)
        else:
            os.environ["JARVIS_CHAOS_READINESS_PROBE_ENABLED"] = saved_probe

    # Commit the chaos so each worktree branches from a RED HEAD (the worktree
    # is a fresh checkout of the committed tree; uncommitted chaos would not
    # propagate to ``git worktree add``).
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chaos: 3 isolated RED leaves")

    target_paths = [str(t.target_file) for t in targets]
    yield {
        "root": root,
        "targets": target_paths,
        "chaos_targets": targets,
        "rel_targets": [
            str(Path(p).relative_to(root)).replace(os.sep, "/")
            for p in target_paths
        ],
    }


# ===========================================================================
# Omni-flag arming (in-process) + gate isolation
# ===========================================================================

_OMNI_FLAGS = {
    "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED": "true",
    "JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE": "true",
    "JARVIS_WAVE3_DAG_COMPOSE_ENABLED": "true",
    "JARVIS_SWARM_ORCHESTRATOR_ENABLED": "true",
    "JARVIS_L3_TELEMETRY_ENABLED": "true",
}


@pytest.fixture(autouse=True)
def _omni_flags_and_gate_isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, val in _OMNI_FLAGS.items():
        monkeypatch.setenv(name, val)
    # The default MemoryPressureGate is a process singleton -- reset before and
    # after so a stub probe injected by one test can never leak into another.
    reset_default_gate()
    try:
        yield
    finally:
        reset_default_gate()


# ===========================================================================
# Simulated DoubleWord inference -- a STUB generator, event-driven (NO sleeps)
# ===========================================================================


class _StubGeneration:
    """Minimal generation result mirroring GenerationResult's consumed shape."""

    def __init__(self, *, candidates: tuple, cost_usd: float) -> None:
        self.is_noop = False
        self.candidates = candidates
        self.cost_usd = cost_usd


class _SimulatedDWGenerator:
    """A per-worker simulated DoubleWord inference.

    * Returns a real candidate -- a *fix* for the unit's own target file (flips
      the chaos ``-`` back to ``+``) so the patch is genuine, file-scoped, and
      recomposable.
    * Reports a ``cost_usd`` so [L3Telemetry] threads a real DW cost.
    * BLOCKS on a per-unit ``asyncio.Event`` the test fires -- the inference
      "finishes" only when the test resolves that unit's gate. This makes
      concurrency deterministic + event-driven (no sleep is the sync mechanism).
    * Captures ``time.monotonic()`` at start (gate await begins) and finish
      (gate released) so the test PROVES overlap from real timestamps.
    """

    def __init__(
        self,
        *,
        fix_by_relpath: Dict[str, str],
        cost_by_unit: Dict[str, float],
        gate_by_unit: Dict[str, asyncio.Event],
        started_at: Dict[str, float],
        finished_at: Dict[str, float],
        active_now: List[str],
        max_active_seen: List[int],
        fail_units: Optional[set] = None,
    ) -> None:
        self._fix_by_relpath = fix_by_relpath
        self._cost_by_unit = cost_by_unit
        self._gate_by_unit = gate_by_unit
        self._started_at = started_at
        self._finished_at = finished_at
        self._active_now = active_now
        self._max_active_seen = max_active_seen
        self._fail_units = fail_units or set()

    @staticmethod
    def _unit_id_from_ctx(ctx: Any) -> str:
        # op_id is "<graph.op_id>:<unit_id>" (set by the executor).
        op_id = str(getattr(ctx, "op_id", ""))
        return op_id.rsplit(":", 1)[-1] if ":" in op_id else op_id

    @staticmethod
    def _relpath_from_ctx(ctx: Any) -> str:
        tf = getattr(ctx, "target_files", ()) or ()
        return str(tf[0]) if tf else ""

    async def generate(self, ctx: Any, deadline: Any) -> Any:
        unit_id = self._unit_id_from_ctx(ctx)
        relpath = self._relpath_from_ctx(ctx)

        # --- enter the simulated inference window (timestamped) ------------
        self._started_at[unit_id] = time.monotonic()
        self._active_now.append(unit_id)
        self._max_active_seen[0] = max(self._max_active_seen[0], len(self._active_now))

        gate = self._gate_by_unit.get(unit_id)
        try:
            if gate is not None:
                # Event-driven barrier: block until the test fires this unit's
                # gate. THIS is the synchronization mechanism -- not a sleep.
                await asyncio.wait_for(gate.wait(), timeout=30.0)
        finally:
            self._finished_at[unit_id] = time.monotonic()
            try:
                self._active_now.remove(unit_id)
            except ValueError:
                pass

        if unit_id in self._fail_units:
            # Simulate a worker whose generation produced nothing usable ->
            # no candidate survives validation -> the unit terminates FAILED.
            return _StubGeneration(candidates=(), cost_usd=0.0)

        fix = self._fix_by_relpath.get(relpath, "")
        candidate = {"file_path": relpath, "full_content": fix}
        return _StubGeneration(
            candidates=(candidate,),
            cost_usd=self._cost_by_unit.get(unit_id, 0.0),
        )


# ===========================================================================
# Minimal event sink (the scheduler's emitter is best-effort + async)
# ===========================================================================


class _RecordingEventEmitter:
    """Captures EventEnvelopes the scheduler emits (proves phase transitions)."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    async def emit(self, envelope: Any) -> None:
        self.events.append(envelope)


# ===========================================================================
# Builders
# ===========================================================================


def _build_units(rel_targets: List[str]) -> Tuple[WorkUnitSpec, ...]:
    """One work unit per isolated chaos file -- no dependencies (parallel)."""
    return tuple(
        WorkUnitSpec(
            unit_id=f"u{i}",
            repo="jarvis",
            goal=f"repair {rel}",
            target_files=(rel,),
            owned_paths=(rel,),
        )
        for i, rel in enumerate(sorted(rel_targets))
    )


def _build_graph(units: Tuple[WorkUnitSpec, ...], *, concurrency_limit: int) -> ExecutionGraph:
    return ExecutionGraph(
        graph_id="l3-hermetic-graph",
        op_id="op-l3-hermetic",
        planner_id="hermetic-l3",
        schema_version="2d.1",
        units=units,
        concurrency_limit=concurrency_limit,
    )


def _fix_content(rel: str) -> str:
    """The 'fix' a simulated DW worker returns for its file: the green source
    (flip the chaos ``-`` back to ``+``). Derived from the file basename so the
    fix is genuine + file-scoped (NOT fabricated boilerplate)."""
    stem = Path(rel).stem  # calcN
    i = int(stem.replace("calc", ""))
    return _leaf_src(i)


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float, interval: float = 0.01
) -> None:
    """Await until *pred()* is truthy or *timeout* elapses (fully async)."""
    async def _spin() -> None:
        while not pred():
            await asyncio.sleep(interval)
    try:
        await asyncio.wait_for(_spin(), timeout=timeout)
    except asyncio.TimeoutError:
        pass  # caller asserts on the predicate -> clear failure surface


def _windows_overlap(
    started: Dict[str, float], finished: Dict[str, float], unit_ids: List[str]
) -> bool:
    """True iff EVERY pair of unit execution windows [start, finish] overlaps --
    the mathematical proof of concurrency (computed from captured timestamps,
    never assumed)."""
    for a in unit_ids:
        for b in unit_ids:
            if a >= b:
                continue
            # Overlap iff start_a <= finish_b AND start_b <= finish_a.
            if not (started[a] <= finished[b] and started[b] <= finished[a]):
                return False
    return True


# ===========================================================================
# (A) COLLISION MATRIX -- 3 disjoint targets clear as ONE parallel group
# ===========================================================================


@pytest.mark.asyncio
async def test_collision_matrix_clears_three_as_one_parallel_group(
    l3_chaos_repo: dict,
) -> None:
    """The zero-trust collision matrix proves the 3 import-isolated targets are
    pairwise DISJOINT and partition into exactly ONE parallel group (no Oracle
    needed -- disjointness comes from the disjoint ``target_files`` sets)."""
    units = _build_units(l3_chaos_repo["rel_targets"])
    assert len(units) == _N_TARGETS

    # oracle=None: distinct files never set-overlap -> DISJOINT pairwise even
    # under zero-trust (the import-coupling probe is only consulted when files
    # could otherwise be coupled; distinct disjoint files short-circuit to
    # DISJOINT via no set-overlap + no coupling edges, here proven via the
    # injected-disjoint fixture). Provide a coupling-free oracle stub so the
    # matrix resolves coupling deterministically rather than zero-trust-denying.
    class _NoCouplingOracle:
        def find_nodes_in_file(self, file_path: str) -> list:
            # Each file has exactly one indexed node with no neighbours ->
            # resolved + uncoupled (the real injector PROVED import-isolation).
            return [type("N", (), {"file_path": file_path})()]

        def get_dependencies(self, node: Any) -> list:
            return []

        def get_dependents(self, node: Any) -> list:
            return []

    oracle = _NoCouplingOracle()
    matrix = build_collision_matrix(units, oracle=oracle)
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            assert matrix.verdict(units[i].unit_id, units[j].unit_id) is (
                CollisionVerdict.DISJOINT
            ), f"{units[i].unit_id} vs {units[j].unit_id} not proven disjoint"

    parallel_groups, sequential = partition_parallel_safe(units, oracle=oracle)
    assert len(parallel_groups) == 1, (
        f"expected ONE parallel group, got {len(parallel_groups)}"
    )
    assert len(parallel_groups[0]) == _N_TARGETS, (
        "the one parallel group must hold all 3 disjoint units"
    )
    assert sequential == [], "no unit should be forced serial -- all 3 disjoint"


# ===========================================================================
# (B+C+D) FAN-OUT -- 3 concurrent worktree subagents, event-driven, telemetry
# ===========================================================================


@pytest.mark.asyncio
async def test_three_concurrent_worktree_subagents_event_driven(
    l3_chaos_repo: dict,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drive the REAL scheduler: 3 isolated units fan out into 3 concurrent git
    worktrees (overlap proven by captured timestamps), the concurrency cap is
    respected (RAM protection), all 3 worktrees are reaped, and [L3Telemetry]
    captures each worktree's lifespan + simulated DW cost + a graph-aggregate.
    """
    root: Path = l3_chaos_repo["root"]
    rel_targets: List[str] = l3_chaos_repo["rel_targets"]
    units = _build_units(rel_targets)
    # concurrency_limit = 3 so all three may run in one wave; the
    # MemoryPressureGate is the dynamic cap the scheduler additionally honors.
    graph = _build_graph(units, concurrency_limit=_N_TARGETS)

    # --- Simulated DW wiring: per-unit gate + timestamp capture -----------
    gate_by_unit = {u.unit_id: asyncio.Event() for u in units}
    started_at: Dict[str, float] = {}
    finished_at: Dict[str, float] = {}
    active_now: List[str] = []
    max_active_seen = [0]
    cost_by_unit = {u.unit_id: 0.001 * (idx + 1) for idx, u in enumerate(units)}
    fix_by_relpath = {rel: _fix_content(rel) for rel in rel_targets}

    generator = _SimulatedDWGenerator(
        fix_by_relpath=fix_by_relpath,
        cost_by_unit=cost_by_unit,
        gate_by_unit=gate_by_unit,
        started_at=started_at,
        finished_at=finished_at,
        active_now=active_now,
        max_active_seen=max_active_seen,
    )

    # --- REAL chassis: store + bus + emitter + executor + worktree mgr ----
    store = ExecutionGraphStore(state_dir=tmp_path / "graph_state")
    command_bus = CommandBus()
    emitter = _RecordingEventEmitter()
    worktree_base = tmp_path / "worktrees"
    worktree_mgr = WorktreeManager(repo_root=root, worktree_base=worktree_base)
    executor = GenerationSubagentExecutor(
        generator=generator,
        validation_runner=None,  # candidate is a .py we don't need to re-run
        repo_roots={"jarvis": root},
        worktree_manager=worktree_mgr,
    )
    scheduler = SubagentScheduler(
        store=store,
        command_bus=command_bus,
        event_emitter=emitter,
        executor=executor,
        max_concurrent_graphs=1,
    )

    import logging

    caplog.set_level(logging.INFO, logger="Ouroboros.SubagentScheduler")

    await scheduler.start()
    try:
        submitted = await scheduler.submit(graph)
        assert submitted, "scheduler refused the graph"

        # Wait (event-driven) until ALL 3 workers have ENTERED their simulated
        # inference -- i.e. all 3 worktrees are live simultaneously. This is the
        # concurrency proof window: if the scheduler serialized them, only 1
        # would ever be active and this wait would time out.
        await _wait_until(
            lambda: len(active_now) >= _N_TARGETS, timeout=20.0,
        )
        assert len(active_now) >= _N_TARGETS, (
            f"expected {_N_TARGETS} workers concurrently active, "
            f"only {len(active_now)} entered -- fan-out serialized"
        )
        # The worktree dirs exist NOW, while all 3 are mid-inference.
        live_worktrees = sorted(p.name for p in worktree_base.iterdir()) if (
            worktree_base.exists()
        ) else []
        assert len(live_worktrees) >= _N_TARGETS, (
            f"expected {_N_TARGETS} live worktrees during fan-out, "
            f"saw {live_worktrees}"
        )

        # Now release every worker's simulated DW inference (event-driven).
        for ev in gate_by_unit.values():
            ev.set()

        terminal = await scheduler.wait_for_graph(graph.graph_id, timeout_s=25.0)
    finally:
        await scheduler.stop()

    # --- (B) all units COMPLETED + concurrency proven by overlap ----------
    assert terminal.phase is GraphExecutionPhase.COMPLETED, (
        f"graph not COMPLETED: phase={terminal.phase} err={terminal.last_error}"
    )
    assert len(terminal.results) == _N_TARGETS
    assert all(
        r.status is WorkUnitState.COMPLETED for r in terminal.results.values()
    ), "every fan-out unit must COMPLETE"

    unit_ids = [u.unit_id for u in units]
    assert set(started_at) == set(unit_ids) and set(finished_at) == set(unit_ids)
    assert _windows_overlap(started_at, finished_at, unit_ids), (
        "execution windows do not all overlap -- concurrency NOT proven "
        f"(started={started_at} finished={finished_at})"
    )
    # The peak simultaneously-active count reached the full fan-out degree.
    assert max_active_seen[0] == _N_TARGETS, (
        f"peak concurrency {max_active_seen[0]} != {_N_TARGETS} (RAM-bound or serialized)"
    )

    # --- (C) concurrency cap respected (RAM protection) -------------------
    # Peak concurrency never exceeded the scheduler's concurrency_limit (and,
    # transitively, the MemoryPressureGate cap consulted inside the scheduler).
    assert max_active_seen[0] <= graph.concurrency_limit, (
        f"peak {max_active_seen[0]} exceeded concurrency cap {graph.concurrency_limit}"
    )

    # --- (B) all 3 worktrees reaped (none leaked) -------------------------
    await _wait_until(
        lambda: (not worktree_base.exists())
        or not any(worktree_base.iterdir()),
        timeout=10.0,
    )
    leaked = sorted(p.name for p in worktree_base.iterdir()) if (
        worktree_base.exists()
    ) else []
    assert leaked == [], f"worktrees leaked (not reaped): {leaked}"

    # --- (D) telemetry: per-unit lifespan + DW cost + graph-aggregate -----
    per_unit_lines = [
        rec.message for rec in caplog.records
        if "[L3Telemetry] unit=" in rec.getMessage()
    ]
    assert len(per_unit_lines) >= _N_TARGETS, (
        f"expected >={_N_TARGETS} per-unit [L3Telemetry] lines, got {len(per_unit_lines)}"
    )
    # Each unit result carries a real worktree lifespan + the simulated DW cost.
    for uid, res in terminal.results.items():
        assert res.worktree_lifespan_s is not None and res.worktree_lifespan_s >= 0.0, (
            f"unit {uid} missing worktree lifespan telemetry"
        )
        assert res.dw_cost_usd == cost_by_unit[uid], (
            f"unit {uid} DW cost {res.dw_cost_usd} != injected {cost_by_unit[uid]}"
        )
    agg_lines = [
        rec.getMessage() for rec in caplog.records
        if "[L3Telemetry] graph=" in rec.getMessage()
    ]
    assert agg_lines, "no graph-aggregate [L3Telemetry] line emitted"
    expected_total = round(sum(cost_by_unit.values()), 6)
    assert any(
        f"total_dw_cost_usd={expected_total!r}" in line for line in agg_lines
    ), f"graph-aggregate DW cost not {expected_total} in {agg_lines}"


# ===========================================================================
# (E) DAG COMPOSE -- exactly 3 patches -> ONE candidate (zero-loss)
# ===========================================================================


@pytest.mark.asyncio
async def test_dag_composer_recomposes_three_patches_into_one_candidate(
    l3_chaos_repo: dict,
    tmp_path: Path,
) -> None:
    """The terminal fan-out results recompose into EXACTLY ONE multi-file
    candidate carrying all 3 disjoint per-file patches (zero dropped)."""
    root: Path = l3_chaos_repo["root"]
    rel_targets: List[str] = l3_chaos_repo["rel_targets"]
    units = _build_units(rel_targets)
    graph = _build_graph(units, concurrency_limit=_N_TARGETS)

    terminal = await _run_fanout_to_terminal(
        graph, units, rel_targets, root, tmp_path,
    )
    assert terminal.phase is GraphExecutionPhase.COMPLETED

    composed = compose_fanout_result(graph, terminal.results)
    assert composed.is_failure is False, (
        f"compose failed: {getattr(composed, 'reason', None)}"
    )
    assert composed.n_files == _N_TARGETS, (
        f"composed {composed.n_files} files, expected {_N_TARGETS} (patch dropped!)"
    )
    # Exactly the 3 disjoint target files, no duplicates, no omissions.
    assert set(composed.file_paths) == set(rel_targets)
    assert len(composed.candidate["files"]) == _N_TARGETS
    # The composed candidate is the orchestrator multi-file shape.
    assert "file_path" in composed.candidate and "full_content" in composed.candidate
    assert composed.candidate["composed_by"]  # lineage stamped


@pytest.mark.asyncio
async def test_worker_failure_fails_closed_to_serial_no_partial(
    l3_chaos_repo: dict,
    tmp_path: Path,
) -> None:
    """A simulated worker FAILURE -> the DAGComposer fails CLOSED
    (ComposeFailure) -> legacy serial path. NO partial compose, no silent
    patch loss."""
    root: Path = l3_chaos_repo["root"]
    rel_targets: List[str] = l3_chaos_repo["rel_targets"]
    units = _build_units(rel_targets)
    graph = _build_graph(units, concurrency_limit=_N_TARGETS)

    # Fail exactly one worker (its simulated DW returns no usable candidate ->
    # the unit terminates FAILED).
    fail_unit = units[1].unit_id
    terminal = await _run_fanout_to_terminal(
        graph, units, rel_targets, root, tmp_path, fail_units={fail_unit},
    )
    # The graph itself terminates FAILED (a unit failed) -- never COMPLETED.
    assert terminal.phase is not GraphExecutionPhase.COMPLETED

    composed = compose_fanout_result(graph, terminal.results)
    assert composed.is_failure is True, "compose must fail CLOSED on a failed unit"
    assert composed.reason is ComposeFailureReason.UNIT_NOT_SUCCESS, (
        f"expected UNIT_NOT_SUCCESS fail-closed, got {composed.reason}"
    )
    assert composed.offending_unit_id == fail_unit
    # Fail-closed contract: NO ComposedCandidate, no partial union -> the caller
    # falls back to the legacy serial walk (proven here by the absence of a
    # composed candidate, not by a half-built one).
    assert not hasattr(composed, "n_files")


# ===========================================================================
# (C) RAM backpressure -- the MemoryPressureGate cap is honored
# ===========================================================================


@pytest.mark.asyncio
async def test_memory_pressure_gate_clamps_fanout(
    l3_chaos_repo: dict,
    tmp_path: Path,
) -> None:
    """Under simulated HIGH memory pressure the scheduler clamps the fan-out to
    the gate's per-level cap (RAM protection) -- proven by peak concurrency
    never exceeding the cap, while ALL units still complete (overflow replayed,
    zero work loss)."""
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg

    root: Path = l3_chaos_repo["root"]
    rel_targets: List[str] = l3_chaos_repo["rel_targets"]
    units = _build_units(rel_targets)
    graph = _build_graph(units, concurrency_limit=_N_TARGETS)

    # Inject a HIGH-pressure probe (free_pct in [10,20) -> HIGH -> cap 3, but
    # we tighten the HIGH cap to 1 so the clamp is observable on a 3-unit graph).
    high_probe = mpg.MemoryProbe(
        free_pct=15.0, total_bytes=16 * 1024**3,
        available_bytes=int(0.15 * 16 * 1024**3), source="stub",
    )
    gate = mpg.MemoryPressureGate(probe_fn=lambda: high_probe)
    mpg._default_gate = gate  # the scheduler pulls get_default_gate()
    os.environ["JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP"] = "1"
    try:
        decision: FanoutDecision = gate.can_fanout(_N_TARGETS)
        assert decision.level is PressureLevel.HIGH
        assert decision.n_allowed == 1, "HIGH cap should clamp 3 -> 1"

        terminal = await _run_fanout_to_terminal(
            graph, units, rel_targets, root, tmp_path,
            track_peak=True,
        )
    finally:
        os.environ.pop("JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP", None)

    # All units still COMPLETED -- overflow was deferred + replayed, not lost.
    assert terminal.phase is GraphExecutionPhase.COMPLETED
    assert len(terminal.results) == _N_TARGETS
    # Peak concurrency was clamped to the gate cap (1), NOT the graph limit (3).
    assert _PEAK_HOLDER[0] == 1, (
        f"peak concurrency {_PEAK_HOLDER[0]} != gate cap 1 (RAM clamp not honored)"
    )


# ===========================================================================
# Shared fan-out driver (real scheduler) used by (E) + RAM tests
# ===========================================================================

# Module-level peak holder the RAM test reads (set per-run inside the driver).
_PEAK_HOLDER = [0]


async def _run_fanout_to_terminal(
    graph: ExecutionGraph,
    units: Tuple[WorkUnitSpec, ...],
    rel_targets: List[str],
    root: Path,
    tmp_path: Path,
    *,
    fail_units: Optional[set] = None,
    track_peak: bool = False,
) -> Any:
    """Drive the REAL scheduler to a terminal state. Workers auto-release as
    soon as they enter (event-driven via a pre-set gate) so the helper returns
    the terminal GraphExecutionState. Returns the terminal state."""
    gate_by_unit = {u.unit_id: asyncio.Event() for u in units}
    # Pre-set every gate: each worker proceeds the moment it enters (still
    # event-driven -- the Event is the barrier, never a sleep).
    for ev in gate_by_unit.values():
        ev.set()
    started_at: Dict[str, float] = {}
    finished_at: Dict[str, float] = {}
    active_now: List[str] = []
    max_active_seen = [0]
    cost_by_unit = {u.unit_id: 0.002 for u in units}
    fix_by_relpath = {rel: _fix_content(rel) for rel in rel_targets}

    generator = _SimulatedDWGenerator(
        fix_by_relpath=fix_by_relpath,
        cost_by_unit=cost_by_unit,
        gate_by_unit=gate_by_unit,
        started_at=started_at,
        finished_at=finished_at,
        active_now=active_now,
        max_active_seen=max_active_seen,
        fail_units=fail_units,
    )

    store = ExecutionGraphStore(state_dir=tmp_path / f"gs_{graph.graph_id}")
    command_bus = CommandBus()
    emitter = _RecordingEventEmitter()
    worktree_base = tmp_path / f"wt_{id(generator)}"
    worktree_mgr = WorktreeManager(repo_root=root, worktree_base=worktree_base)
    executor = GenerationSubagentExecutor(
        generator=generator,
        validation_runner=None,
        repo_roots={"jarvis": root},
        worktree_manager=worktree_mgr,
    )
    scheduler = SubagentScheduler(
        store=store,
        command_bus=command_bus,
        event_emitter=emitter,
        executor=executor,
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    try:
        assert await scheduler.submit(graph)
        terminal = await scheduler.wait_for_graph(graph.graph_id, timeout_s=25.0)
    finally:
        await scheduler.stop()
    if track_peak:
        _PEAK_HOLDER[0] = max_active_seen[0]
    return terminal
