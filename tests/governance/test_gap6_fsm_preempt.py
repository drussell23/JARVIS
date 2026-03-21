"""Verify EV_PREEMPT is in LoopEvent and triggers RUNNING -> SUSPENDED_PREEMPTED."""
import pytest
from datetime import datetime, timezone
from backend.core.ouroboros.governance.contracts.fsm_contract import (
    LoopEvent, LoopState, LoopRuntimeContext, RetryBudget,
)
from backend.core.ouroboros.governance.preemption_fsm import (
    PreemptionFsmEngine, build_transition_input,
)


def test_ev_preempt_exists_in_loop_event():
    assert hasattr(LoopEvent, "EV_PREEMPT"), "EV_PREEMPT must be added to LoopEvent"
    assert LoopEvent.EV_PREEMPT.value == "EV_PREEMPT"


def test_running_ev_preempt_transitions_to_suspended_preempted():
    engine = PreemptionFsmEngine()
    ctx = LoopRuntimeContext(op_id="test-op-1")
    budget = RetryBudget()

    ti = build_transition_input(
        op_id="test-op-1",
        phase="GENERATE",
        event=LoopEvent.EV_PREEMPT,
        ctx=ctx,
        checkpoint_seq=1,
        metadata={"source": "user_signal_bus"},
    )
    decision = engine.decide(ctx, ti, budget)

    assert decision.from_state == LoopState.RUNNING
    assert decision.to_state == LoopState.SUSPENDED_PREEMPTED
    assert not decision.terminal


def test_suspended_ev_preempt_is_noop():
    """EV_PREEMPT on already-suspended op does nothing (unhandled = noop)."""
    engine = PreemptionFsmEngine()
    ctx = LoopRuntimeContext(op_id="test-op-2", state=LoopState.SUSPENDED_PREEMPTED)
    budget = RetryBudget()

    ti = build_transition_input(
        op_id="test-op-2",
        phase="GENERATE",
        event=LoopEvent.EV_PREEMPT,
        ctx=ctx,
        checkpoint_seq=1,
        metadata={},
    )
    decision = engine.decide(ctx, ti, budget)

    # Unhandled event in SUSPENDED_PREEMPTED -> same state, not terminal
    assert decision.to_state == LoopState.SUSPENDED_PREEMPTED
    assert not decision.terminal


def test_rehydrating_ev_preempt_transitions_to_failed_permanent():
    engine = PreemptionFsmEngine()
    ctx = LoopRuntimeContext(op_id="test-op-3", state=LoopState.REHYDRATING)
    budget = RetryBudget()
    ti = build_transition_input(
        op_id="test-op-3",
        phase="GENERATE",
        event=LoopEvent.EV_PREEMPT,
        ctx=ctx,
        checkpoint_seq=1,
        metadata={},
    )
    decision = engine.decide(ctx, ti, budget)
    assert decision.to_state == LoopState.FAILED_PERMANENT
    assert decision.terminal


def test_resumed_ev_preempt_transitions_to_failed_permanent():
    engine = PreemptionFsmEngine()
    ctx = LoopRuntimeContext(op_id="test-op-4", state=LoopState.RESUMED)
    budget = RetryBudget()
    ti = build_transition_input(
        op_id="test-op-4",
        phase="GENERATE",
        event=LoopEvent.EV_PREEMPT,
        ctx=ctx,
        checkpoint_seq=1,
        metadata={},
    )
    decision = engine.decide(ctx, ti, budget)
    assert decision.to_state == LoopState.FAILED_PERMANENT
    assert decision.terminal
