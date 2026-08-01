"""The swarm cannot deadlock on dependencies — asserted, not assumed.

Written after a request to build an `EligibilityGraphArbiter`: an ephemeral
DAG of inter-agent eligibility requirements, DFS cycle detection, and
deterministic cycle severance by criticality. Investigating first showed the
premise does not hold in this codebase, in three separate ways — so what is
pinned here is the immunity that actually exists rather than a new mechanism
guarding an impossible state.

**1. Eligibility has no graph.** ``swarm_eligible(unit)`` takes ONE unit and
reads three of its own attributes. It never consults another unit, so there is
no edge, no cycle, and nothing to sever. The bug it replaced was circular in
the DEFINITIONAL sense — "a unit is eligible to be shaped into a swarm worker
iff it is already shaped as one", P ⟺ P — not a dependency cycle.

**2. Dependency cycles are impossible by construction.**
``ExecutionGraph.__post_init__`` calls ``_validate_unit_dag``, which runs
Kahn's algorithm and RAISES. A cyclic graph never becomes an object, so a
runtime severance pass would sit downstream of a state that cannot reach it.

**3. Severance would be worse than the failure it prevents.** Dropping a
declared dependency to break a cycle means running a unit before its
prerequisite completed — its inputs do not exist yet. Refusing the graph is
the correct answer; a swarm that always converges on SOME agent by discarding
constraints converges on the wrong one.

What the request was right about is the PROPERTY: the scheduler must never
hang. Three layers deliver it, and the middle one had no test at all:

  * cycles      — rejected at construction (covered elsewhere for the
                  planner's builder, not for the scheduler's own entry);
  * fail-fast   — a failed unit terminates its graph immediately, so a
                  dependent whose prerequisite died is never left spinning;
  * starvation  — if a ready set is ever emptied, the loop STOPS with
                  ``no_schedulable_ready_units`` (``_finish_graph`` then
                  ``return``) rather than waiting. Its precondition is covered
                  here; the branch is defensive and unreachable by design —
                  see `TestSchedulerTerminatesOnUnschedulableGraphs`;
  * epistemic   — ``EpistemicDeadlockBreaker`` kills two agents talking past
                  each other (covered in test_swarm_invoker.py case (d)).

Every async assertion here is wrapped in ``asyncio.wait_for``. A regression
that reintroduced a hang must FAIL this suite, not hang it — a test for
deadlock immunity that can itself deadlock is worth nothing.
"""
from __future__ import annotations

import asyncio

import pytest

# DRY — the scheduler fixtures already exist and are exercised by the
# scheduler's own suite. A second `_FakeExecutor` would be a second thing to
# keep in step with `WorkUnitResult`, and the first to drift.
from tests.governance.autonomy.test_subagent_scheduler import (  # noqa: F401
    _FakeExecutor,
)

#: Every await gets a ceiling. Generous enough that a slow machine does not
#: flake, tight enough that a genuine hang fails in seconds.
_NO_HANG_S = 10.0


def _unit(unit_id: str, *, deps=(), repo: str = "jarvis"):
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        WorkUnitSpec,
    )
    return WorkUnitSpec(
        unit_id=unit_id,
        repo=repo,
        goal=f"work {unit_id}",
        target_files=(f"{repo}/{unit_id}.py",),
        owned_paths=(f"{repo}/{unit_id}.py",),
        dependency_ids=tuple(deps),
    )


def _graph(units, *, graph_id="graph-cycle", concurrency_limit=2):
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
    )
    return ExecutionGraph(
        graph_id=graph_id,
        op_id="op-cycle",
        planner_id="planner-v1",
        schema_version="2d.1",
        concurrency_limit=concurrency_limit,
        units=tuple(units),
    )


async def _scheduler(tmp_path, executor):
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import (
        EventEmitter,
    )

    scheduler = SubagentScheduler(
        store=ExecutionGraphStore(tmp_path),
        command_bus=CommandBus(maxsize=100),
        event_emitter=EventEmitter(),
        executor=executor,
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    return scheduler


# ---------------------------------------------------------------------------
# 1. the requested scenario — A requires B, B requires C, C requires A
# ---------------------------------------------------------------------------


class TestCycleIsImpossibleNotSevered:
    def test_three_way_cycle_is_refused_at_construction(self):
        """A → B → C → A never becomes an ExecutionGraph.

        This is where a runtime arbiter would have been asked to sever an
        edge. It cannot be reached: the object does not exist by then.
        """
        with pytest.raises(ValueError) as excinfo:
            _graph([
                _unit("A", deps=("C",)),
                _unit("B", deps=("A",)),
                _unit("C", deps=("B",)),
            ])
        message = str(excinfo.value)
        assert "cycle" in message.lower()
        # The cyclic set is NAMED. "a cycle exists" is not actionable; the
        # operator needs to know which units are in it.
        for unit_id in ("A", "B", "C"):
            assert unit_id in message, message

    def test_a_two_way_cycle_is_refused_too(self):
        """The smallest cycle, in case the walk special-cases length."""
        with pytest.raises(ValueError, match="cycle"):
            _graph([_unit("A", deps=("B",)), _unit("B", deps=("A",))])

    def test_a_self_dependency_is_refused(self):
        """A unit depending on itself — a cycle of length one, and the shape a
        naive visited-set walk misses because it never leaves the node."""
        with pytest.raises(ValueError, match="cycle"):
            _graph([_unit("A", deps=("A",))])

    def test_a_dangling_dependency_is_refused_before_the_cycle_walk(self):
        """A dependency on a unit that does not exist is rejected on its own
        terms. Left to the topological walk it would present as "cycle", and
        an operator would go looking for a loop that is not there."""
        with pytest.raises(ValueError) as excinfo:
            _graph([_unit("A", deps=("GHOST",))])
        assert "unknown dependency" in str(excinfo.value).lower()

    def test_a_legitimate_chain_is_accepted(self):
        """The guard must reject cycles, not dependencies. A → B → C is the
        same edge count as the cycle above and is perfectly valid."""
        graph = _graph([
            _unit("A"),
            _unit("B", deps=("A",)),
            _unit("C", deps=("B",)),
        ])
        assert len(graph.units) == 3

    def test_a_diamond_is_accepted(self):
        """Two paths reconverging is not a cycle. A walk that marks visited
        globally instead of per-path calls this one a cycle."""
        graph = _graph([
            _unit("A"),
            _unit("B", deps=("A",)),
            _unit("C", deps=("A",)),
            _unit("D", deps=("B", "C")),
        ])
        assert len(graph.units) == 4


# ---------------------------------------------------------------------------
# 2. eligibility — the predicate that was circular, and what it is now
# ---------------------------------------------------------------------------


class TestEligibilityIsAPerUnitPredicate:
    def test_it_consults_no_other_unit(self):
        """The structural claim behind "there is no cycle to detect".

        `swarm_eligible` takes ONE unit. If it ever grew an inter-unit
        requirement this test fails, and THAT is the moment a cycle becomes
        possible and an arbiter becomes worth building.
        """
        import inspect
        from backend.core.ouroboros.governance.autonomy.swarm_invoker import (
            swarm_eligible,
        )
        params = list(inspect.signature(swarm_eligible).parameters)
        assert params == ["unit"], params
        source = inspect.getsource(swarm_eligible)
        assert "dependency_ids" not in source, (
            "eligibility now reads inter-unit dependencies — a cycle is "
            "reachable and this suite's premise no longer holds"
        )

    def test_the_old_circularity_would_have_blocked_everything(self):
        """The bug was P ⟺ P: eligible iff already-shaped, while the
        invoker's job is to DO the shaping. Pinned as an explicit statement so
        the fix is not quietly reverted to "check the marking"."""
        from backend.core.ouroboros.governance.autonomy.swarm_invoker import (
            swarm_eligible,
        )
        unshaped = _unit("U")
        assert not getattr(unshaped, "is_swarm_worker", False)
        # Under the old predicate this was False and the swarm never ran.
        assert swarm_eligible(unshaped) is True

    def test_eligibility_is_currently_TOTAL_for_constructible_units(self):
        """A finding, recorded rather than silently relied upon.

        `swarm_eligible` tests for a non-empty `target_files` and a non-empty
        `goal` — but `WorkUnitSpec.__post_init__` ALREADY requires both, so
        neither branch can be False for a real unit. The predicate went from
        never-true (the tautology) to always-true.

        That is not a safety hole: the gates that actually decide are
        `is_orchestrator_enabled()` and `is_graph_parallelizable(graph)`, and
        the checks still earn their place against duck-typed inputs. But
        eligibility is currently carrying no information, and someone reading
        the name would assume it filters. Pinned so the vacuity is visible and
        deliberate — if a real eligibility rule is ever wanted, this test is
        where the intent gets stated.
        """
        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            WorkUnitSpec,
        )
        from backend.core.ouroboros.governance.autonomy.swarm_invoker import (
            swarm_eligible,
        )
        for missing in ("goal", "target_files"):
            kwargs = dict(unit_id="V", repo="r", goal="g",
                          target_files=("r/v.py",))
            kwargs[missing] = "" if missing == "goal" else ()
            with pytest.raises(ValueError):
                WorkUnitSpec(**kwargs)   # cannot even be built
        assert swarm_eligible(_unit("W")) is True


# ---------------------------------------------------------------------------
# 3. the untested guard — starvation terminates, it does not hang
# ---------------------------------------------------------------------------


class TestSchedulerTerminatesOnUnschedulableGraphs:
    """A dependency that can never be satisfied must not wedge the loop.

    SCOPE, stated precisely because the first draft of this docstring was
    wrong. It claimed to cover ``no_schedulable_ready_units``. Running the
    scenario and reading the terminal state showed
    ``last_error='boom'`` — the graph fails FAST on the unit's own failure and
    never reaches the starvation branch, so u2 is never even considered.

    The assertions below are sound and the property they pin is the one that
    matters — the graph terminates, the loop stays live, and the dependent
    unit does not run. But the mechanism is fail-fast, not the starvation
    guard, and saying otherwise would have been a test passing for a reason
    its own documentation got wrong.

    ``no_schedulable_ready_units`` has no coverage, and a second pass says
    that is because it is DEFENSIVE, not because it was overlooked. The guess
    written here first — "reaching it needs `_select_ready_batch` or the
    MemoryPressureGate to empty a non-empty ready set" — is wrong on both
    counts:

      * `_select_ready_batch` selects the FIRST ready unit unconditionally
        (``owned_paths`` starts empty, so the conflict test cannot fire on it).
        A non-empty ready set always yields a non-empty batch.
      * the gate cannot clamp to zero. ``warn/high/critical_fanout_cap`` are
        each ``_env_int(..., minimum=1)``, so even
        ``JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP=0`` resolves to 1, and
        ``can_fanout`` documents ``n_allowed`` as "0 only when n_requested=0"
        — which the ``if selected:`` guard upstream already excludes.

    So ``not selected`` implies ``ready`` is empty; and since any failed unit
    terminates its graph immediately, an empty ready set with live units left
    is a state the rest of the design prevents. The branch guards an
    inconsistent scheduler, which is worth keeping and is not worth a test
    that would have to fabricate the inconsistency to reach it.

    ``test_starvation_is_computed_correctly`` covers the precondition
    directly, which is the part that can be asserted honestly.
    """

    def test_starvation_is_computed_correctly(self, tmp_path):
        """`_compute_ready_units` yields NOTHING when a dependency failed.

        The precondition of the starvation guard, tested where it can be
        tested honestly: `all(dep in completed)` is false for a dep sitting in
        `failed`, so the dependent is never ready — and it is not terminal
        either. That combination is what a hang would be made of; the layers
        above are what stop it becoming one.
        """
        from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
            SubagentScheduler,
        )
        graph = _graph([_unit("u1"), _unit("u2", deps=("u1",))],
                       graph_id="graph-compute")

        class _State:
            completed_units = ()
            failed_units = ("u1",)
            cancelled_units = ()
            running_units = ()

        ready = SubagentScheduler._compute_ready_units(
            None, graph, _State(),  # type: ignore[arg-type]
        )
        assert ready == [], (
            "u2 became ready despite its dependency failing — running it "
            "would be the dropped constraint that cycle severance performs "
            "deliberately"
        )

    def test_the_memory_gate_cannot_clamp_fanout_to_zero(self, monkeypatch):
        """The starvation-deadlock hypothesis, closed at its source.

        "Memory pressure clamps concurrency to 0 and the swarm starves
        forever" is the natural reading of a gate that reduces fan-out. It
        cannot happen: all three caps are ``_env_int(..., minimum=1)``, so at
        least one unit always proceeds and the graph always advances. Asserted
        against a HOSTILE configuration — an operator explicitly setting every
        cap to zero or negative — because that is the only way the value could
        arrive, and the floor is the entire defence.
        """
        from backend.core.ouroboros.governance import memory_pressure_gate as g

        for var in ("JARVIS_MEMORY_PRESSURE_WARN_FANOUT_CAP",
                    "JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP",
                    "JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP"):
            monkeypatch.setenv(var, "0")
        assert g.warn_fanout_cap() >= 1
        assert g.high_fanout_cap() >= 1
        assert g.critical_fanout_cap() >= 1

        for var in ("JARVIS_MEMORY_PRESSURE_WARN_FANOUT_CAP",
                    "JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP",
                    "JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP"):
            monkeypatch.setenv(var, "-5")
        assert min(g.warn_fanout_cap(), g.high_fanout_cap(),
                   g.critical_fanout_cap()) >= 1

    def test_a_non_empty_ready_set_always_yields_a_batch(self):
        """The other half of "``not selected`` implies ``ready`` is empty".

        `_select_ready_batch` cannot starve a ready set: the first unit meets
        an empty ``owned_paths``, so the conflict test cannot exclude it.
        Asserted with units that ALL collide on one path — the worst case for
        the conflict rule, and still exactly one gets through.
        """
        from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
            SubagentScheduler,
        )
        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            ExecutionGraph, WorkUnitSpec,
        )
        collide = tuple(
            WorkUnitSpec(unit_id=uid, repo="jarvis", goal=f"g {uid}",
                         target_files=("jarvis/shared.py",),
                         owned_paths=("jarvis/shared.py",))
            for uid in ("u1", "u2", "u3")
        )
        graph = ExecutionGraph(
            graph_id="graph-collide", op_id="op", planner_id="p",
            schema_version="2d.1", concurrency_limit=3, units=collide,
        )
        selected, deferred = SubagentScheduler._select_ready_batch(
            None, graph, ["u1", "u2", "u3"],  # type: ignore[arg-type]
        )
        assert len(selected) == 1, (selected, deferred)
        assert sorted(deferred) == ["u2", "u3"], deferred

    @pytest.mark.asyncio
    async def test_a_failed_dependency_does_not_wedge_the_graph(self, tmp_path):
        """u2 depends on u1; u1 FAILS.

        u2's dependency will never be in `completed`, so it can never become
        ready. The graph must reach a terminal phase rather than spin.

        In practice it terminates by FAIL-FAST — ``last_error='boom'``, u1's
        own failure — before starvation is ever evaluated. Verified by reading
        the terminal state, not assumed.

        Wrapped in `wait_for`: if this ever hangs, the test FAILS.
        """
        executor = _FakeExecutor(fail_unit="u1")
        scheduler = await _scheduler(tmp_path, executor)
        try:
            graph = _graph(
                [_unit("u1"), _unit("u2", deps=("u1",))],
                graph_id="graph-starve",
            )
            accepted = await asyncio.wait_for(
                scheduler.submit(graph), timeout=_NO_HANG_S)
            assert accepted is True

            state = await asyncio.wait_for(
                scheduler.wait_for_graph(graph.graph_id, timeout_s=_NO_HANG_S),
                timeout=_NO_HANG_S,
            )
            # Terminal — the specific phase matters less than the fact that it
            # STOPPED. A graph parked in RUNNING forever is the bug.
            assert state.phase.value in ("failed", "completed"), state.phase
            assert "u2" not in executor.started, (
                "u2 ran despite its dependency failing — a dropped constraint "
                "is exactly what cycle severance would have done deliberately"
            )
        finally:
            await asyncio.wait_for(scheduler.stop(), timeout=_NO_HANG_S)

    @pytest.mark.asyncio
    async def test_the_event_loop_stays_responsive_throughout(self, tmp_path):
        """Liveness, not just termination.

        A scheduler that finishes but blocks the loop while doing it is still
        a hang from every other coroutine's point of view — and the whole
        organism is `asyncio`. A ticker running alongside must keep getting
        scheduled.
        """
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        executor = _FakeExecutor(fail_unit="u1")
        scheduler = await _scheduler(tmp_path, executor)
        ticker = asyncio.ensure_future(_ticker())
        try:
            graph = _graph(
                [_unit("u1"), _unit("u2", deps=("u1",))],
                graph_id="graph-live",
            )
            await asyncio.wait_for(scheduler.submit(graph), timeout=_NO_HANG_S)
            await asyncio.wait_for(
                scheduler.wait_for_graph(graph.graph_id, timeout_s=_NO_HANG_S),
                timeout=_NO_HANG_S,
            )
            assert ticks > 0, "the event loop never yielded to another task"
        finally:
            ticker.cancel()
            await asyncio.wait_for(scheduler.stop(), timeout=_NO_HANG_S)

    @pytest.mark.asyncio
    async def test_a_healthy_dependency_chain_still_completes(self, tmp_path):
        """The guard must not fire on graphs that CAN progress. A starvation
        check that trips early would turn every ordered graph into a failure,
        which is the same outage wearing the opposite sign."""
        executor = _FakeExecutor()
        scheduler = await _scheduler(tmp_path, executor)
        try:
            graph = _graph(
                [_unit("u1"), _unit("u2", deps=("u1",))],
                graph_id="graph-healthy",
            )
            await asyncio.wait_for(scheduler.submit(graph), timeout=_NO_HANG_S)
            state = await asyncio.wait_for(
                scheduler.wait_for_graph(graph.graph_id, timeout_s=_NO_HANG_S),
                timeout=_NO_HANG_S,
            )
            assert state.phase.value == "completed", state.phase
            assert executor.started == ["u1", "u2"], executor.started
        finally:
            await asyncio.wait_for(scheduler.stop(), timeout=_NO_HANG_S)
