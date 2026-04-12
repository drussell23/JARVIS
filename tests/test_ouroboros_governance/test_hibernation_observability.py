"""Tests for HIBERNATION_MODE step 7 — observability hooks.

Covers the ``GovernedLoopService._build_hibernation_observability_hooks``
factory: the pair of async hooks that emit structured CommProtocol
messages around every hibernation transition so SerpentFlow renders a
proactive_alert panel, LogTransport writes debug.log, and any dashboard
transport picks up the event feed.

Tests avoid building a full governance stack: a minimal "stack stub"
exposing ``.comm`` is enough to exercise the hook contract, and a
``LogTransport`` captures every emitted :class:`CommMessage` for
assertions. End-to-end tests also wire the hooks into a real
:class:`SupervisorOuroborosController` so we can prove the
enter_hibernation/wake_from_hibernation path fires them, and that
emergency_stop from HIBERNATION triggers the wake observability.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopService,
)
from backend.core.ouroboros.governance.supervisor_controller import (
    AutonomyMode,
    SupervisorOuroborosController,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stack_with_comm() -> tuple[SimpleNamespace, LogTransport]:
    """Build a minimal stack exposing ``.comm`` with a LogTransport.

    Returns ``(stack, transport)`` so tests can make assertions about
    the messages captured by the transport.
    """
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    stack = SimpleNamespace(comm=comm)
    return stack, transport


def _payload_by_type(
    messages: List[CommMessage], msg_type: MessageType,
) -> List[dict]:
    return [m.payload for m in messages if m.msg_type is msg_type]


# ---------------------------------------------------------------------------
# Factory contract
# ---------------------------------------------------------------------------


class TestFactoryContract:
    def test_factory_returns_two_async_callables(self):
        stack, _ = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        assert callable(on_hibernate)
        assert callable(on_wake)
        assert asyncio.iscoroutinefunction(on_hibernate)
        assert asyncio.iscoroutinefunction(on_wake)

    def test_factory_independent_closures_per_call(self):
        """Each factory invocation yields a fresh cycle counter — two
        independent services must not share hibernation cycle state."""
        stack_a, transport_a = _make_stack_with_comm()
        stack_b, transport_b = _make_stack_with_comm()
        on_hib_a, on_wake_a = (
            GovernedLoopService._build_hibernation_observability_hooks(stack_a)
        )
        on_hib_b, on_wake_b = (
            GovernedLoopService._build_hibernation_observability_hooks(stack_b)
        )

        async def _run() -> None:
            await on_hib_a(reason="a1")
            await on_hib_a(reason="a2")
            await on_wake_a(reason="a-back")
            await on_hib_b(reason="b1")
            await on_wake_b(reason="b-back")

        asyncio.get_event_loop().run_until_complete(_run())

        # A produced cycles 001 and 002 under independent op_ids.
        a_ops = sorted({m.op_id for m in transport_a.messages})
        assert len(a_ops) == 2
        assert all(op.startswith("hibernation-") for op in a_ops)
        # B produced exactly one cycle (001).
        b_ops = sorted({m.op_id for m in transport_b.messages})
        assert len(b_ops) == 1
        assert b_ops[0].startswith("hibernation-001-")


# ---------------------------------------------------------------------------
# Enter-side semantics
# ---------------------------------------------------------------------------


class TestHibernateHook:
    @pytest.mark.asyncio
    async def test_emits_heartbeat_then_decision(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, _on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="dw down")

        msgs = transport.messages
        types = [m.msg_type for m in msgs]
        assert types == [MessageType.HEARTBEAT, MessageType.DECISION]

    @pytest.mark.asyncio
    async def test_heartbeat_carries_proactive_alert(self):
        """SerpentTransport only renders a Panel when
        ``proactive_alert=True`` — the payload MUST set that flag plus
        title/body/severity so the battle-test CLI has everything it
        needs to display the alert."""
        stack, transport = _make_stack_with_comm()
        on_hibernate, _ = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="provider outage")

        heartbeats = _payload_by_type(transport.messages, MessageType.HEARTBEAT)
        assert len(heartbeats) == 1
        p = heartbeats[0]
        assert p["proactive_alert"] is True
        assert p["alert_title"] == "HIBERNATING"
        assert "provider outage" in p["alert_body"]
        assert p["alert_severity"] == "critical"
        assert p["alert_source"] == "provider_exhaustion"
        assert p["phase"] == "hibernation_enter"
        assert p["progress_pct"] == 0.0
        assert p["hibernation_cycle"] == 1

    @pytest.mark.asyncio
    async def test_decision_carries_structured_outcome(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, _ = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="claude 429")

        decisions = _payload_by_type(transport.messages, MessageType.DECISION)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "hibernation_entered"
        assert d["reason_code"] == "provider_exhaustion"
        assert d["diff_summary"] == "claude 429"

    @pytest.mark.asyncio
    async def test_cycle_counter_increments_across_enters(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="first")
        await on_wake(reason="back")
        await on_hibernate(reason="second")
        await on_wake(reason="back")

        cycles = [
            m.payload["hibernation_cycle"]
            for m in transport.messages
            if "hibernation_cycle" in m.payload
        ]
        # Enter(1), Wake(1), Enter(2), Wake(2) — 4 messages total.
        assert cycles == [1, 1, 2, 2]

    @pytest.mark.asyncio
    async def test_empty_reason_defaults_to_unspecified(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, _ = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="")

        heartbeats = _payload_by_type(transport.messages, MessageType.HEARTBEAT)
        assert "unspecified" in heartbeats[0]["alert_body"]


# ---------------------------------------------------------------------------
# Wake-side semantics
# ---------------------------------------------------------------------------


class TestWakeHook:
    @pytest.mark.asyncio
    async def test_wake_emits_heartbeat_decision_postmortem(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="outage")
        await on_wake(reason="healthy")

        # Sequence: enter HEARTBEAT, enter DECISION, wake HEARTBEAT,
        # wake DECISION, wake POSTMORTEM.
        types = [m.msg_type for m in transport.messages]
        assert types == [
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.POSTMORTEM,
        ]

    @pytest.mark.asyncio
    async def test_wake_shares_op_id_with_enter(self):
        """Enter + wake form a single logical operation in the
        CommProtocol seq space so causal parent links stay intact."""
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="down")
        await on_wake(reason="back")

        op_ids = {m.op_id for m in transport.messages}
        assert len(op_ids) == 1  # single shared op_id
        # And seq numbers monotonically increase within that op.
        seqs = [m.seq for m in transport.messages]
        assert seqs == sorted(seqs)
        assert seqs[0] == 1

    @pytest.mark.asyncio
    async def test_wake_without_prior_enter_synthesizes_op_id(self):
        """If wake fires first (e.g. tests that exercise wake hook in
        isolation, or an emergency_stop racing the enter), the hook
        must still produce a well-formed message sequence."""
        stack, transport = _make_stack_with_comm()
        _, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_wake(reason="orphan wake")

        assert len(transport.messages) == 3  # HEARTBEAT, DECISION, POSTMORTEM
        op_id = transport.messages[0].op_id
        assert op_id.startswith("hibernation-wake-")
        assert all(m.op_id == op_id for m in transport.messages)

    @pytest.mark.asyncio
    async def test_wake_clears_current_op_id(self):
        """Second enter after a wake must allocate a fresh op_id, not
        reuse the stale previous one."""
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="cycle1")
        await on_wake(reason="back1")
        await on_hibernate(reason="cycle2")

        op_ids = {m.op_id for m in transport.messages}
        assert len(op_ids) == 2  # distinct cycles

    @pytest.mark.asyncio
    async def test_wake_heartbeat_carries_recovered_alert(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="down")
        await on_wake(reason="dw healthy again")

        # Second heartbeat is the wake one.
        heartbeats = _payload_by_type(transport.messages, MessageType.HEARTBEAT)
        assert len(heartbeats) == 2
        wake = heartbeats[1]
        assert wake["proactive_alert"] is True
        assert wake["alert_title"] == "RECOVERED"
        assert wake["alert_severity"] == "info"
        assert wake["alert_source"] == "provider_recovery"
        assert "dw healthy again" in wake["alert_body"]
        assert wake["phase"] == "hibernation_wake"
        assert wake["progress_pct"] == 100.0

    @pytest.mark.asyncio
    async def test_wake_postmortem_points_to_resume(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="down")
        await on_wake(reason="healthy")

        postmortems = _payload_by_type(
            transport.messages, MessageType.POSTMORTEM,
        )
        assert len(postmortems) == 1
        pm = postmortems[0]
        assert pm["root_cause"] == "healthy"
        assert pm["failed_phase"] is None
        assert pm["next_safe_action"] == "resume_governed_loop"


# ---------------------------------------------------------------------------
# Fault tolerance
# ---------------------------------------------------------------------------


class TestFaultTolerance:
    @pytest.mark.asyncio
    async def test_none_stack_is_silent_noop(self):
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(None)
        )
        # Both hooks must return without raising.
        await on_hibernate(reason="x")
        await on_wake(reason="x")

    @pytest.mark.asyncio
    async def test_stack_without_comm_is_noop(self):
        stack = SimpleNamespace(comm=None)
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="x")
        await on_wake(reason="x")

    @pytest.mark.asyncio
    async def test_raising_transport_does_not_propagate(self):
        """A transport that raises must not break the hook — CommProtocol
        already fault-isolates transports, and the hook adds a broad
        try/except on top."""

        class _RaisingTransport:
            async def send(self, msg: Any) -> None:
                raise RuntimeError("transport boom")

        comm = CommProtocol(transports=[_RaisingTransport()])
        stack = SimpleNamespace(comm=comm)
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="down")
        await on_wake(reason="back")
        # No assertion needed — surviving both calls is the proof.

    @pytest.mark.asyncio
    async def test_raising_comm_method_does_not_propagate(self):
        class _ExplodingComm:
            async def emit_heartbeat(self, **kw: Any) -> None:
                raise RuntimeError("heartbeat broke")

            async def emit_decision(self, **kw: Any) -> None:
                raise RuntimeError("decision broke")

            async def emit_postmortem(self, **kw: Any) -> None:
                raise RuntimeError("postmortem broke")

        stack = SimpleNamespace(comm=_ExplodingComm())
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )
        await on_hibernate(reason="down")
        await on_wake(reason="back")


# ---------------------------------------------------------------------------
# End-to-end: real controller + hook registration
# ---------------------------------------------------------------------------


class TestEndToEndWithController:
    @pytest.mark.asyncio
    async def test_enter_hibernation_fires_observability(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )

        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=on_hibernate,
            on_wake=on_wake,
            name="test.observability",
        )
        await ctrl.start()
        await ctrl.enter_hibernation("substrate failure")

        types = [m.msg_type for m in transport.messages]
        assert types == [MessageType.HEARTBEAT, MessageType.DECISION]
        assert ctrl.mode is AutonomyMode.HIBERNATION

    @pytest.mark.asyncio
    async def test_full_enter_wake_cycle_fires_five_messages(self):
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )

        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=on_hibernate,
            on_wake=on_wake,
            name="test.observability",
        )
        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        await ctrl.wake_from_hibernation(reason="healthy again")

        types = [m.msg_type for m in transport.messages]
        assert types == [
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.POSTMORTEM,
        ]
        # All share the same op_id.
        assert len({m.op_id for m in transport.messages}) == 1

    @pytest.mark.asyncio
    async def test_emergency_stop_from_hibernation_fires_wake_obs(self):
        """Emergency stop supersedes HIBERNATION and must still deliver
        the wake observability so the transport stream stays well-formed
        instead of stranding a dangling enter."""
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )

        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=on_hibernate,
            on_wake=on_wake,
            name="test.observability",
        )
        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        await ctrl.emergency_stop("operator halt")

        types = [m.msg_type for m in transport.messages]
        # enter -> HEARTBEAT, DECISION
        # emergency_stop from hibernation -> wake hooks fire
        #   -> HEARTBEAT, DECISION, POSTMORTEM
        assert types == [
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.POSTMORTEM,
        ]
        assert ctrl.mode is AutonomyMode.EMERGENCY_STOP

    @pytest.mark.asyncio
    async def test_repeat_hibernation_rejected_does_not_double_emit(self):
        """Second enter_hibernation while already hibernating is a
        no-op for the controller, so the observability hook must not
        fire a second time either."""
        stack, transport = _make_stack_with_comm()
        on_hibernate, on_wake = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )

        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=on_hibernate,
            on_wake=on_wake,
            name="test.observability",
        )
        await ctrl.start()
        await ctrl.enter_hibernation("first")
        await ctrl.enter_hibernation("second")  # no-op: already hibernating

        heartbeats = [
            m for m in transport.messages if m.msg_type is MessageType.HEARTBEAT
        ]
        assert len(heartbeats) == 1

    @pytest.mark.asyncio
    async def test_obs_hook_runs_alongside_bridge_hook(self):
        """Step 6.5 bridge hooks and step 7 observability hooks coexist
        — the controller fires them in registration order and neither
        interferes with the other."""
        stack, transport = _make_stack_with_comm()
        on_hibernate_obs, on_wake_obs = (
            GovernedLoopService._build_hibernation_observability_hooks(stack)
        )

        bridge_calls: List[str] = []

        def _bridge_hibernate(*, reason: str) -> None:
            bridge_calls.append(f"hibernate:{reason}")

        def _bridge_wake(*, reason: str) -> None:
            bridge_calls.append(f"wake:{reason}")

        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=_bridge_hibernate,
            on_wake=_bridge_wake,
            name="bridge",
        )
        ctrl.register_hibernation_hooks(
            on_hibernate=on_hibernate_obs,
            on_wake=on_wake_obs,
            name="observability",
        )
        await ctrl.start()
        await ctrl.enter_hibernation("dw down")
        await ctrl.wake_from_hibernation(reason="dw back")

        assert bridge_calls == ["hibernate:dw down", "wake:dw back"]
        assert len(transport.messages) == 5  # full enter + wake sequence
