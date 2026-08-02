"""One node waiting on a human must not idle the swarm.

`capability_router` already refuses to hold the LLM's turn open — SUSPENDED in
0.0 ms. That frees one TASK. It does not free the GRAPH: a work unit whose tool
call suspended is, to the scheduler, a unit that was dispatched and never came
back. With a bounded pool, enough of those and everything idles behind one
unanswered prompt while unrelated branches sit ready.

The mandated scenario is `test_node_B_runs_while_node_A_awaits_consent`:
Node A needs consent, Node B is parallel and read-only, and B must execute
without waiting for A.

WHY THE FIX IS A WORD AND NOT A SCHEDULER
-------------------------------------------
`_compute_ready_units` already answers "what may run now" by excluding
`completed | failed | cancelled | running`. A parked unit is none of those four,
so before this it silently re-entered `ready` every pass and re-asked the
operator forever. The repair is a FIFTH state in that filter — the topological
logic was already right, it only lacked a word for "handed to a human".

`test_a_dependent_of_a_parked_unit_stays_blocked` is the other half and matters
as much: context-switching must not mean running work that depends on the thing
nobody has approved.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.autonomy import consent_pending_queue as cq
from backend.core.ouroboros.governance.autonomy.consent_pending_queue import (
    ConsentPendingQueue,
    get_consent_queue,
    reset_consent_queue,
)


@pytest.fixture(autouse=True)
def _fresh():
    reset_consent_queue()
    yield
    reset_consent_queue()


class _Unit:
    """Shaped like `WorkUnitSpec` — only the two fields readiness reads."""

    def __init__(self, unit_id: str, deps: Tuple[str, ...] = ()) -> None:
        self.unit_id = unit_id
        self.dependency_ids = deps


class _Graph:
    def __init__(self, units: List[_Unit], graph_id: str = "g-1") -> None:
        self.graph_id = graph_id
        self.units = tuple(units)


class _State:
    def __init__(self, **kw: Any) -> None:
        self.completed_units = kw.get("completed", ())
        self.failed_units = kw.get("failed", ())
        self.cancelled_units = kw.get("cancelled", ())
        self.running_units = kw.get("running", ())


def _ready(graph: _Graph, state: Optional[_State] = None) -> List[str]:
    """Drive the REAL readiness computation, not a copy of it."""
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )
    sched = SubagentScheduler.__new__(SubagentScheduler)
    return SubagentScheduler._compute_ready_units(sched, graph, state)


class TestTheMandatedScenario:
    def test_node_B_runs_while_node_A_awaits_consent(self):
        """A needs consent, B is parallel and read-only. B must not wait."""
        graph = _Graph([_Unit("A"), _Unit("B")])

        # Both start ready.
        assert _ready(graph) == ["A", "B"]

        # A's tool call suspended on `lock_screen`; the orchestrator parks it.
        assert get_consent_queue().park("g-1", "A", request_id="req-1",
                                        capability="lock_screen") is True

        # THE assertion: B is still schedulable, A is not re-offered.
        assert _ready(graph) == ["B"], (
            "the graph stalled behind an unanswered prompt")

    def test_the_operator_answers_and_A_rejoins(self):
        graph = _Graph([_Unit("A"), _Unit("B")])
        get_consent_queue().park("g-1", "A", capability="lock_screen")
        assert _ready(graph) == ["B"]

        # Resolved out of band — B may have finished by now.
        get_consent_queue().release("g-1", "A")
        assert _ready(graph, _State(completed=("B",))) == ["A"]

    def test_a_dependent_of_a_parked_unit_stays_blocked(self):
        """Context-switching must not run work that depends on the thing
        nobody has approved."""
        graph = _Graph([_Unit("A"), _Unit("B"), _Unit("C", deps=("A",))])
        get_consent_queue().park("g-1", "A", capability="lock_screen")
        ready = _ready(graph)
        assert "B" in ready
        assert "C" not in ready, "a dependent of an unapproved unit ran"
        assert "A" not in ready

    def test_without_parking_the_unit_is_re_offered_forever(self):
        """The defect this closes. A suspended unit is not running, completed,
        failed or cancelled — so without the fifth state it comes straight
        back around and re-asks."""
        graph = _Graph([_Unit("A"), _Unit("B")])
        for _ in range(3):
            assert "A" in _ready(graph)      # no park: A keeps returning

    def test_many_parked_units_do_not_block_the_rest(self):
        units = [_Unit(f"n{i}") for i in range(10)]
        graph = _Graph(units)
        for i in range(0, 10, 2):
            get_consent_queue().park("g-1", f"n{i}", capability="lock_screen")
        ready = _ready(graph)
        assert ready == ["n1", "n3", "n5", "n7", "n9"]


class TestParkingIsBounded:
    def test_an_expired_park_returns_the_unit_to_the_graph(self, monkeypatch):
        """A unit parked forever is a leak that looks like patience."""
        monkeypatch.setenv("JARVIS_CONSENT_PARK_TTL_S", "30")
        q = get_consent_queue()
        q.park("g-1", "A", capability="lock_screen")
        assert q.parked_ids("g-1") == {"A"}
        q._parked["g-1"]["A"].parked_at = time.time() - 10_000
        assert q.parked_ids("g-1") == set(), "an expired park still held a slot"
        assert q.stats()["expired"] >= 1

    def test_at_the_bound_it_refuses_rather_than_evicting(self, monkeypatch):
        """Evicting the OLDEST would silently un-park something an operator is
        still looking at. Refusing leaves the unit schedulable — it re-asks."""
        monkeypatch.setenv("JARVIS_CONSENT_MAX_PARKED", "2")
        q = ConsentPendingQueue()
        assert q.park("g", "a") and q.park("g", "b")
        assert q.park("g", "c") is False
        assert q.parked_ids("g") == {"a", "b"}
        assert q.stats()["rejected"] == 1

    def test_parking_is_idempotent(self):
        q = get_consent_queue()
        assert q.park("g", "A") and q.park("g", "A")
        assert q.parked_ids("g") == {"A"}

    def test_graphs_do_not_leak_into_each_other(self):
        q = get_consent_queue()
        q.park("g-1", "A")
        q.park("g-2", "A")
        assert q.parked_ids("g-1") == {"A"} and q.parked_ids("g-2") == {"A"}
        q.release("g-1", "A")
        assert q.parked_ids("g-1") == set() and q.parked_ids("g-2") == {"A"}

    def test_clearing_a_finished_graph(self):
        q = get_consent_queue()
        q.park("g-1", "A")
        q.clear("g-1")
        assert q.parked_ids("g-1") == set()


class TestItFailsOpen:
    def test_a_broken_queue_leaves_the_scheduler_as_it_was(self, monkeypatch):
        """A consent queue that cannot answer must not stall a graph it can no
        longer reason about."""
        import backend.core.ouroboros.governance.autonomy.subagent_scheduler as sch
        monkeypatch.setattr(
            sch, "_parked_units",
            lambda g: (_ for _ in ()).throw(RuntimeError("queue down")),
            raising=False)
        graph = _Graph([_Unit("A"), _Unit("B")])
        with pytest.raises(RuntimeError):
            sch._parked_units(graph)          # the stub really raises…
        # …and the real helper swallows it, so readiness is unaffected.
        monkeypatch.undo()
        assert _ready(graph) == ["A", "B"]

    def test_the_master_switch_disables_parking(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONSENT_QUEUE_ENABLED", "0")
        q = ConsentPendingQueue()
        assert q.park("g", "A") is False
        assert q.parked_ids("g") == set()

    def test_releasing_an_unparked_unit_never_raises(self):
        assert get_consent_queue().release("nope", "nope") is None

    @pytest.mark.parametrize("bad", [None, 42, object()])
    def test_hostile_ids_never_raise(self, bad):
        q = ConsentPendingQueue()
        q.park(bad, bad)          # type: ignore[arg-type]
        assert isinstance(q.parked_ids(bad), set)   # type: ignore[arg-type]


class TestObservability:
    def test_a_surface_can_see_who_is_waiting(self):
        q = get_consent_queue()
        q.park("g-1", "A", request_id="req-9", capability="lock_screen")
        snap = q.snapshot("g-1")
        assert snap and snap[0]["unit_id"] == "A"
        assert snap[0]["capability"] == "lock_screen"
        assert snap[0]["request_id"] == "req-9"
        assert snap[0]["age_s"] >= 0

    def test_stats_count_the_boundary(self):
        q = get_consent_queue()
        q.park("g", "A", capability="lock_screen")
        assert q.stats()["pending"] == 1 and q.stats()["parked"] == 1
        q.release("g", "A")
        assert q.stats()["pending"] == 0 and q.stats()["released"] == 1


class TestItComposesWithTheRouter:
    @pytest.mark.asyncio
    async def test_a_suspended_call_is_what_gets_parked(self):
        """End to end: the router suspends, the queue parks THAT request, and
        the graph moves on."""
        from backend.system_control.capability_registry import (
            CapabilityRegistry, capability,
        )
        from backend.system_control.capability_router import (
            CapabilityRouter, Outcome,
        )

        class _Ctl:
            @capability(mutates=True)
            async def lock_screen(self) -> tuple:
                """Lock."""
                return (True, "locked")

        class _Prov:
            async def request(self, ctx: Any) -> str:
                return "req-42"

        ctl = _Ctl()
        router = CapabilityRouter(registry=CapabilityRegistry(ctl).hydrate(),
                                  provider=_Prov(), target=ctl)
        out = await router.route("lock_screen")
        assert out.outcome == Outcome.SUSPENDED.value

        get_consent_queue().park("g-1", "A", request_id=out.request_id,
                                 capability=out.capability)
        graph = _Graph([_Unit("A"), _Unit("B")])
        assert _ready(graph) == ["B"]
        assert get_consent_queue().snapshot("g-1")[0]["request_id"] == "req-42"
