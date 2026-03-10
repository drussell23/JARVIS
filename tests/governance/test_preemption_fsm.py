import pytest
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

from backend.core.ouroboros.governance.contracts.fsm_contract import (
    LoopState,
    LoopEvent,
    ReasonCode,
    RetryBudget,
    IdempotencyEnvelope,
    TransitionInput,
    TransitionDecision,
    LoopRuntimeContext,
    FsmEngine,
    FsmExecutor,
)


# -----------------------------------------------------------------------------
# Contract Lock: immutable state/event sets
# -----------------------------------------------------------------------------

def test_loop_state_enum_is_frozen() -> None:
    assert {s.value for s in LoopState} == {
        "RUNNING",
        "SUSPENDED_PREEMPTED",
        "REHYDRATING",
        "RESUMED",
        "FAILED_PERMANENT",
    }


def test_loop_event_enum_is_frozen() -> None:
    assert {e.value for e in LoopEvent} == {
        "EV_GENERATE_START",
        "EV_GENERATE_SUCCESS",
        "EV_GENERATE_TIMEOUT",
        "EV_CONNECTION_LOSS",
        "EV_SPOT_TERMINATED",
        "EV_REHYDRATE_STARTED",
        "EV_REHYDRATE_HEALTHY",
        "EV_REHYDRATE_FAILED",
        "EV_RETRY_BUDGET_EXHAUSTED",
        "EV_ABORT_POLICY_VIOLATION",
        "EV_CANCELLED",
    }


# -----------------------------------------------------------------------------
# Test doubles (replace with real implementation imports once wired)
# -----------------------------------------------------------------------------

class FakeLedger:
    def __init__(self) -> None:
        self.checkpoints = set()
        self.calls: list[tuple[str, int, str, str]] = []

    async def checkpoint_exists(self, *, op_id: str, checkpoint_seq: int) -> bool:
        return (op_id, checkpoint_seq) in self.checkpoints

    async def append_checkpoint(
        self,
        *,
        op_id: str,
        checkpoint_seq: int,
        state: LoopState,
        event: LoopEvent,
        reason_code: Optional[str],
        payload: Mapping[str, Any],
    ) -> None:
        self.calls.append(("append_checkpoint", checkpoint_seq, state.value, event.value))
        self.checkpoints.add((op_id, checkpoint_seq))


class FakeTelemetry:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def emit_transition(self, decision: TransitionDecision, payload: Mapping[str, Any]) -> None:
        self.calls.append(f"{decision.from_state.value}->{decision.to_state.value}:{decision.event.value}")


class FakeEngine(FsmEngine):
    """
    Minimal deterministic map for contract tests.
    Replace with real engine once implemented.
    """
    def decide(self, ctx: LoopRuntimeContext, ti: TransitionInput, budget: RetryBudget) -> TransitionDecision:
        cs, ev = ti.current_state, ti.event

        if cs == LoopState.RUNNING and ev in {
            LoopEvent.EV_GENERATE_TIMEOUT,
            LoopEvent.EV_CONNECTION_LOSS,
            LoopEvent.EV_SPOT_TERMINATED,
        }:
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.SUSPENDED_PREEMPTED,
                event=ev,
                reason_code=ReasonCode.FSM_SUSPENDED_PREEMPTED,
                retry_index=ctx.retry_index,
                backoff_ms=0,
                terminal=False,
                actions=["append_ledger_checkpoint", "emit_telemetry"],
            )

        if cs == LoopState.SUSPENDED_PREEMPTED and ev == LoopEvent.EV_REHYDRATE_STARTED:
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.REHYDRATING,
                event=ev,
                reason_code=None,
                retry_index=ctx.retry_index,
                backoff_ms=0,
                terminal=False,
                actions=["append_ledger_checkpoint", "emit_telemetry"],
            )

        if cs == LoopState.REHYDRATING and ev == LoopEvent.EV_REHYDRATE_HEALTHY:
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.RESUMED,
                event=ev,
                reason_code=None,
                retry_index=ctx.retry_index,
                backoff_ms=0,
                terminal=False,
                actions=["append_ledger_checkpoint", "emit_telemetry"],
            )

        if ev in {LoopEvent.EV_RETRY_BUDGET_EXHAUSTED, LoopEvent.EV_ABORT_POLICY_VIOLATION}:
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.FAILED_PERMANENT,
                event=ev,
                reason_code=ReasonCode.FSM_REHYDRATE_BUDGET_EXHAUSTED
                if ev == LoopEvent.EV_RETRY_BUDGET_EXHAUSTED
                else ReasonCode.CONTRACT_CAPABILITY_MISMATCH,
                retry_index=ctx.retry_index,
                backoff_ms=0,
                terminal=True,
                actions=["append_ledger_checkpoint", "emit_telemetry"],
            )

        return TransitionDecision(
            from_state=cs,
            to_state=cs,
            event=ev,
            reason_code=None,
            retry_index=ctx.retry_index,
            backoff_ms=0,
            terminal=False,
            actions=["append_ledger_checkpoint"],
        )


class FakeExecutor(FsmExecutor):
    """
    Verifies durable-ledger-first ordering and duplicate checkpoint no-op.
    """
    def __init__(self, engine: FsmEngine, ledger: FakeLedger, telemetry: FakeTelemetry) -> None:
        self.engine = engine
        self.ledger = ledger
        self.telemetry = telemetry
        self.side_effect_log: list[str] = []

    async def apply(self, ctx: LoopRuntimeContext, ti: TransitionInput) -> TransitionDecision:
        decision = self.engine.decide(ctx, ti, RetryBudget())

        # Invariant: duplicate checkpoint is no-op
        if await self.ledger.checkpoint_exists(op_id=ti.envelope.op_id, checkpoint_seq=ti.envelope.checkpoint_seq):
            self.side_effect_log.append("duplicate_noop")
            return decision

        # Invariant: durable ledger append before side effects
        await self.ledger.append_checkpoint(
            op_id=ti.envelope.op_id,
            checkpoint_seq=ti.envelope.checkpoint_seq,
            state=decision.to_state,
            event=decision.event,
            reason_code=decision.reason_code.value if decision.reason_code else None,
            payload={},
        )
        self.side_effect_log.append("after_ledger_append")

        if "emit_telemetry" in decision.actions:
            await self.telemetry.emit_transition(decision, {})
            self.side_effect_log.append("telemetry_emitted")

        return decision


@pytest.fixture
def base_input() -> TransitionInput:
    return TransitionInput(
        now_utc=datetime.now(timezone.utc),
        current_state=LoopState.RUNNING,
        event=LoopEvent.EV_CONNECTION_LOSS,
        envelope=IdempotencyEnvelope(
            op_id="op-123",
            phase="GENERATE",
            retry_index=0,
            attempt_id="a1",
            idempotency_key="idem-123",
            checkpoint_seq=1,
        ),
        metadata={},
    )


# -----------------------------------------------------------------------------
# FSM transition matrix tests
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "state,event,expected",
    [
        (LoopState.RUNNING, LoopEvent.EV_CONNECTION_LOSS, LoopState.SUSPENDED_PREEMPTED),
        (LoopState.RUNNING, LoopEvent.EV_GENERATE_TIMEOUT, LoopState.SUSPENDED_PREEMPTED),
        (LoopState.RUNNING, LoopEvent.EV_SPOT_TERMINATED, LoopState.SUSPENDED_PREEMPTED),
        (LoopState.SUSPENDED_PREEMPTED, LoopEvent.EV_REHYDRATE_STARTED, LoopState.REHYDRATING),
        (LoopState.REHYDRATING, LoopEvent.EV_REHYDRATE_HEALTHY, LoopState.RESUMED),
        (LoopState.REHYDRATING, LoopEvent.EV_RETRY_BUDGET_EXHAUSTED, LoopState.FAILED_PERMANENT),
    ],
)
def test_transition_matrix_minimal(state: LoopState, event: LoopEvent, expected: LoopState) -> None:
    engine = FakeEngine()
    ctx = LoopRuntimeContext(op_id="op-123", state=state)
    ti = TransitionInput(
        now_utc=datetime.now(timezone.utc),
        current_state=state,
        event=event,
        envelope=IdempotencyEnvelope(
            op_id="op-123",
            phase="GENERATE",
            retry_index=0,
            attempt_id="a1",
            idempotency_key="idem-123",
            checkpoint_seq=1,
        ),
        metadata={},
    )
    decision = engine.decide(ctx, ti, RetryBudget())
    assert decision.to_state == expected


@pytest.mark.asyncio
async def test_ledger_append_occurs_before_side_effects(base_input: TransitionInput) -> None:
    engine = FakeEngine()
    ledger = FakeLedger()
    telemetry = FakeTelemetry()
    executor = FakeExecutor(engine, ledger, telemetry)
    ctx = LoopRuntimeContext(op_id="op-123", state=LoopState.RUNNING)

    decision = await executor.apply(ctx, base_input)

    assert decision.to_state == LoopState.SUSPENDED_PREEMPTED
    assert ledger.calls, "expected durable checkpoint append"
    assert executor.side_effect_log[0] == "after_ledger_append"


@pytest.mark.asyncio
async def test_duplicate_checkpoint_is_noop(base_input: TransitionInput) -> None:
    engine = FakeEngine()
    ledger = FakeLedger()
    telemetry = FakeTelemetry()
    executor = FakeExecutor(engine, ledger, telemetry)
    ctx = LoopRuntimeContext(op_id="op-123", state=LoopState.RUNNING)

    # First apply writes checkpoint
    await executor.apply(ctx, base_input)

    # Second apply with same checkpoint_seq should no-op
    await executor.apply(ctx, base_input)
    assert executor.side_effect_log[-1] == "duplicate_noop"
