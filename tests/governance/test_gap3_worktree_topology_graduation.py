"""Gap #3 Slice 5 — Worktree Topology View graduation regression suite.

Closes the cage end-to-end. Verifies:

  §1   Two master flags graduated default-true (with hot-revert)
  §2   FlagRegistry seeds installed (2 new flags)
  §3   shipped_code_invariants pins (3 new) registered + clean
  §4   EventChannelServer constructor accepts scheduler + WM kwargs
  §5   IDEObservabilityRouter receives kwargs from EventChannelServer
  §6   End-to-end: scheduler emits → bridge translates → SSE fires
  §7   GLS hoists worktree_manager as instance attribute
  §8   GLS install_default_bridge wiring present in source
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.event_emitter import (
    EventEmitter,
)
from backend.core.ouroboros.governance.event_channel import (
    EventChannelServer,
)
from backend.core.ouroboros.governance.flag_registry_seed import (
    SEED_SPECS,
)
from backend.core.ouroboros.governance.ide_observability import (
    IDEObservabilityRouter,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED,
    EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    list_shipped_code_invariants,
    validate_invariant,
)
from backend.core.ouroboros.governance.verification.worktree_topology import (
    worktree_topology_enabled,
)
from backend.core.ouroboros.governance.verification.worktree_topology_sse_bridge import (
    WorktreeTopologySSEBridge,
    install_default_bridge,
    worktree_topology_sse_enabled,
)


# ============================================================================
# §1 — Two master flags graduated default-true
# ============================================================================


class TestMasterFlagGraduation:
    def test_substrate_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", raising=False,
        )
        assert worktree_topology_enabled() is True

    def test_sse_bridge_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", raising=False,
        )
        assert worktree_topology_sse_enabled() is True

    def test_substrate_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", "false",
        )
        assert worktree_topology_enabled() is False

    def test_sse_bridge_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "false",
        )
        assert worktree_topology_sse_enabled() is False


# ============================================================================
# §2 — FlagRegistry seeds installed
# ============================================================================


class TestFlagRegistrySeeds:
    @pytest.fixture(scope="class")
    def seed_names(self):
        return {s.name for s in SEED_SPECS}

    def test_substrate_master_seeded(self, seed_names):
        assert "JARVIS_WORKTREE_TOPOLOGY_ENABLED" in seed_names

    def test_sse_bridge_master_seeded(self, seed_names):
        assert "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED" in seed_names

    def test_both_masters_default_true_in_seeds(self):
        masters = {
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED",
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED",
        }
        for spec in SEED_SPECS:
            if spec.name in masters:
                assert spec.default is True, (
                    f"{spec.name} graduation requires default=True "
                    f"in FlagRegistry seed (got {spec.default!r})"
                )


# ============================================================================
# §3 — shipped_code_invariants pins (3 new) registered + clean
# ============================================================================


class TestShippedCodeInvariants:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", "true",
        )

    def test_three_gap3_pins_registered(self):
        names = {
            inv.invariant_name
            for inv in list_shipped_code_invariants()
        }
        expected = {
            "gap3_worktree_topology_substrate",
            "gap3_worktree_topology_sse_bridge",
            "gap3_ide_observability_worktrees_routes",
        }
        missing = expected - names
        assert not missing, f"missing Gap #3 pins: {missing}"

    def test_substrate_pin_clean(self):
        for inv in list_shipped_code_invariants():
            if inv.invariant_name == (
                "gap3_worktree_topology_substrate"
            ):
                violations = validate_invariant(inv)
                assert not violations, [
                    v.detail for v in violations
                ]

    def test_bridge_pin_clean(self):
        for inv in list_shipped_code_invariants():
            if inv.invariant_name == (
                "gap3_worktree_topology_sse_bridge"
            ):
                violations = validate_invariant(inv)
                assert not violations, [
                    v.detail for v in violations
                ]

    def test_routes_pin_clean(self):
        for inv in list_shipped_code_invariants():
            if inv.invariant_name == (
                "gap3_ide_observability_worktrees_routes"
            ):
                violations = validate_invariant(inv)
                assert not violations, [
                    v.detail for v in violations
                ]


# ============================================================================
# §4 — EventChannelServer constructor accepts scheduler + WM kwargs
# ============================================================================


class TestEventChannelServerKwargs:
    def test_constructor_accepts_scheduler_kwarg(self):
        # No real router needed — the constructor only stores
        # references; we never call .start().
        srv = EventChannelServer(
            router=None, scheduler="stub-scheduler",
        )
        assert srv._scheduler == "stub-scheduler"

    def test_constructor_accepts_worktree_manager_kwarg(self):
        srv = EventChannelServer(
            router=None, worktree_manager="stub-wm",
        )
        assert srv._worktree_manager == "stub-wm"

    def test_constructor_defaults_to_none(self):
        srv = EventChannelServer(router=None)
        assert srv._scheduler is None
        assert srv._worktree_manager is None


# ============================================================================
# §5 — IDEObservabilityRouter receives kwargs (substring proof in source)
# ============================================================================


class TestEventChannelToRouterWiring:
    def test_event_channel_passes_kwargs_to_router(self):
        """Source-level pin: when ide_observability is mounted,
        EventChannelServer must pass scheduler + worktree_manager."""
        src = Path(
            "backend/core/ouroboros/governance/event_channel.py"
        ).read_text()
        # The mount block must reference both kwargs
        assert "IDEObservabilityRouter(" in src
        assert "scheduler=self._scheduler" in src
        assert "worktree_manager=self._worktree_manager" in src


# ============================================================================
# §6 — End-to-end: scheduler emits → bridge → SSE
# ============================================================================


class TestCageCloseEndToEnd:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "true",
        )

    def test_graph_event_propagates_to_sse(self):
        async def main():
            emitter = EventEmitter()
            broker = StreamEventBroker()
            install_default_bridge(emitter, broker=broker)
            env = EventEnvelope(
                source_layer="L1",
                event_type=EventType.EXECUTION_GRAPH_STATE_CHANGED,
                payload={
                    "graph_id": "g1", "phase": "running",
                    "ready_units": [], "running_units": ["a"],
                    "completed_units": [], "failed_units": [],
                    "cancelled_units": [], "last_error": "",
                },
                op_id="op-graduated",
            )
            await emitter.emit(env)
            return list(broker._history)

        history = asyncio.run(main())
        types = [e.event_type for e in history]
        assert EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED in types

    def test_unit_event_propagates_to_sse(self):
        async def main():
            emitter = EventEmitter()
            broker = StreamEventBroker()
            install_default_bridge(emitter, broker=broker)
            env = EventEnvelope(
                source_layer="L1",
                event_type=EventType.WORK_UNIT_STATE_CHANGED,
                payload={
                    "graph_id": "g1", "unit_id": "a",
                    "repo": "primary", "status": "completed",
                    "barrier_id": "", "owned_paths": ["f.py"],
                },
                op_id="op-graduated",
            )
            await emitter.emit(env)
            return list(broker._history)

        history = asyncio.run(main())
        types = [e.event_type for e in history]
        assert EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED in types

    def test_install_default_bridge_returns_bridge_instance(self):
        # Master-on path must return a real bridge (vs None for
        # master-off — see Slice 3 tests).
        emitter = EventEmitter()
        broker = StreamEventBroker()
        bridge = install_default_bridge(emitter, broker=broker)
        assert isinstance(bridge, WorktreeTopologySSEBridge)

    def test_router_with_scheduler_returns_topology(self):
        """Router with a wired scheduler returns the topology
        (vs 503 when unwired). Confirms the Slice 5 wiring is
        end-to-end functional."""
        from aiohttp.test_utils import make_mocked_request
        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            ExecutionGraph, GraphExecutionPhase, GraphExecutionState,
            WorkUnitSpec,
        )

        class _StubScheduler:
            def __init__(self, graphs):
                self._graphs = graphs

        def _make_state():
            unit = WorkUnitSpec(
                unit_id="u1", repo="primary",
                goal="goal-u1", target_files=("f.py",),
            )
            graph = ExecutionGraph(
                graph_id="g1", op_id="op-1",
                planner_id="t", schema_version="1.0",
                units=(unit,), concurrency_limit=4,
            )
            return GraphExecutionState(
                graph=graph, phase=GraphExecutionPhase.RUNNING,
                running_units=("u1",),
            )

        scheduler = _StubScheduler({"g1": _make_state()})
        router = IDEObservabilityRouter(scheduler=scheduler)

        async def main():
            req = make_mocked_request("GET", "/observability/worktrees")
            req._transport_peername = ("127.0.0.1", 0)
            return await router._handle_worktrees_list(req)

        # Need observability master on (default-true post-graduation)
        resp = asyncio.run(main())
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["topology"]["outcome"] == "ok"
        assert body["topology"]["summary"]["total_units"] == 1


# ============================================================================
# §7 — GLS hoists worktree_manager as instance attribute
# ============================================================================


class TestGLSWorktreeManagerHoist:
    def test_worktree_manager_attr_initialized_to_none(self):
        """GovernedLoopService.__init__ must initialize
        ``self._worktree_manager = None`` so cross-block wiring
        is safe even when L3 isolation is disabled."""
        src = Path(
            "backend/core/ouroboros/governance/governed_loop_service.py"
        ).read_text()
        assert "self._worktree_manager: Optional[Any] = None" in src

    def test_worktree_manager_assigned_in_l3_block(self):
        """When L3 isolation is enabled, the local _wt_manager
        must be assigned to self._worktree_manager so it's in
        scope for the EventChannelServer construction below."""
        src = Path(
            "backend/core/ouroboros/governance/governed_loop_service.py"
        ).read_text()
        assert "self._worktree_manager = _wt_manager" in src


# ============================================================================
# §8 — GLS install_default_bridge wiring present
# ============================================================================


class TestGLSBridgeInstall:
    def test_bridge_install_called_after_scheduler(self):
        """GLS must call install_default_bridge with the
        EventEmitter after the SubagentScheduler is constructed."""
        src = Path(
            "backend/core/ouroboros/governance/governed_loop_service.py"
        ).read_text()
        assert "install_default_bridge as _install_topology_bridge" in src
        assert "_install_topology_bridge(self._event_emitter)" in src

    def test_event_channel_constructed_with_scheduler_and_wm(self):
        """GLS must pass scheduler + worktree_manager when
        constructing EventChannelServer so the IDE worktree
        topology routes have refs."""
        src = Path(
            "backend/core/ouroboros/governance/governed_loop_service.py"
        ).read_text()
        # Find the EventChannelServer construction site
        assert "EventChannelServer(" in src
        assert "scheduler=self._subagent_scheduler" in src
        # _worktree_manager passed (matches the kwarg name on
        # EventChannelServer.__init__)
        assert "worktree_manager=self._worktree_manager" in src
