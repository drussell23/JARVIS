"""
Preemption FSM — Real Implementation
======================================

Implements the deterministic state machine for Ouroboros loop preemption,
suspension, and rehydration.

Public surface:
  - PreemptionFsmEngine   — pure transition function (FsmEngine subclass)
  - PreemptionFsmExecutor — side-effect executor    (FsmExecutor subclass)
  - build_transition_input — factory helper for constructing TransitionInput

Authority boundary: This module performs NO infra lifecycle mutations (no VM
start/stop, no HTTP calls).  Infrastructure rehydration is triggered by the
caller that holds the appropriate authority.

Invariants enforced (mirrors fsm_contract.py):
  1. No state transition without a durable ledger append.
  2. Duplicate (op_id, checkpoint_seq) is a no-op — idempotent by design.
  3. All side effects require idempotency_key and must be replay-safe.
  4. FAILED_PERMANENT is a sink state — any further event is a no-op that
     preserves the existing reason_code.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from typing import Any, Optional

from backend.core.ouroboros.governance.contracts.fsm_contract import (
    FsmEngine,
    FsmExecutor,
    IdempotencyEnvelope,
    LoopEvent,
    LoopRuntimeContext,
    LoopState,
    ReasonCode,
    RetryBudget,
    TransitionDecision,
    TransitionInput,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_STATE_CHANGE_ACTIONS: tuple[str, ...] = ("append_ledger_checkpoint", "emit_telemetry")
_NOOP_ACTIONS: tuple[str, ...] = ("append_ledger_checkpoint",)


def _compute_backoff_ms(retry_index: int, budget: RetryBudget) -> int:
    """
    Compute the back-off delay in milliseconds for the given retry attempt.

    Two modes (driven by RetryBudget.full_jitter):

    full_jitter=True  — Amazon-style full jitter:
        sleep = random(0, min(cap, base * 2^retry_index))

    full_jitter=False — deterministic capped exponential:
        sleep = min(cap, base * 2^retry_index)

    Both values are converted from seconds (budget fields) to milliseconds.
    Returns 0 when retry_index is 0 and in the no-jitter path (first attempt
    delay is still calculated; callers decide whether to apply it).
    """
    base_ms: float = budget.backoff_base_seconds * 1000.0
    cap_ms: float = budget.backoff_cap_seconds * 1000.0

    # Guard against absurdly large retry_index values causing overflow.
    exponent = min(retry_index, 63)
    ceiling = min(cap_ms, base_ms * (2 ** exponent))

    if budget.full_jitter:
        return int(random.uniform(0.0, ceiling))
    return int(ceiling)


def _budget_exhausted(
    ctx: LoopRuntimeContext,
    budget: RetryBudget,
    now_utc: datetime,
) -> bool:
    """
    Return True if either the per-attempt or total-suspend budget is depleted.

    Checks:
      1. retry_index >= max_rehydrate_attempts
      2. first_suspend_at_utc is set and total elapsed >= max_total_suspend_seconds
    """
    if ctx.retry_index >= budget.max_rehydrate_attempts:
        return True
    if ctx.first_suspend_at_utc is not None:
        elapsed = (now_utc - ctx.first_suspend_at_utc).total_seconds()
        if elapsed >= budget.max_total_suspend_seconds:
            return True
    return False


# ---------------------------------------------------------------------------
# PreemptionFsmEngine — pure, deterministic transition table
# ---------------------------------------------------------------------------


class PreemptionFsmEngine(FsmEngine):
    """
    Full deterministic FSM for Ouroboros loop preemption lifecycle.

    ``decide()`` is a pure function: no I/O, no awaits, no mutations.
    All branching is driven solely by (current_state × event × budget).

    Transition table (abbreviated; full details in module docstring):

        RUNNING           × EV_GENERATE_START       → RUNNING            (no-op)
        RUNNING           × EV_GENERATE_SUCCESS      → RUNNING            (no-op)
        RUNNING           × EV_GENERATE_TIMEOUT      → SUSPENDED_PREEMPTED
        RUNNING           × EV_CONNECTION_LOSS       → SUSPENDED_PREEMPTED
        RUNNING           × EV_SPOT_TERMINATED       → SUSPENDED_PREEMPTED
        RUNNING           × EV_ABORT_POLICY_VIOLATION→ FAILED_PERMANENT   (terminal)
        RUNNING           × EV_CANCELLED             → FAILED_PERMANENT   (terminal)
        SUSPENDED_PREEMPTED × EV_REHYDRATE_STARTED   → REHYDRATING
        SUSPENDED_PREEMPTED × EV_RETRY_BUDGET_EXHAUSTED → FAILED_PERMANENT (terminal)
        SUSPENDED_PREEMPTED × EV_CANCELLED           → FAILED_PERMANENT   (terminal)
        REHYDRATING       × EV_REHYDRATE_HEALTHY     → RESUMED
        REHYDRATING       × EV_REHYDRATE_FAILED      → SUSPENDED_PREEMPTED  [or budget-exhausted]
        REHYDRATING       × EV_RETRY_BUDGET_EXHAUSTED→ FAILED_PERMANENT   (terminal)
        REHYDRATING       × EV_CANCELLED             → FAILED_PERMANENT   (terminal)
        RESUMED           × EV_GENERATE_START        → RUNNING
        RESUMED           × EV_CANCELLED             → FAILED_PERMANENT   (terminal)
        FAILED_PERMANENT  × (any)                    → FAILED_PERMANENT   (no-op, preserve reason)
        (any)             × (unhandled)              → (same state)       (no-op)
    """

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def decide(
        self,
        ctx: LoopRuntimeContext,
        ti: TransitionInput,
        budget: RetryBudget,
    ) -> TransitionDecision:
        """
        Pure transition function.

        Parameters
        ----------
        ctx:
            Current runtime state snapshot (read-only inside this function).
        ti:
            Immutable transition input carrying event, envelope, and timestamp.
        budget:
            Retry policy parameters (max attempts, backoff, jitter, etc.).

        Returns
        -------
        TransitionDecision
            Fully populated decision record.  The caller (PreemptionFsmExecutor)
            is responsible for persisting and applying it.
        """
        cs = ti.current_state
        ev = ti.event
        now_utc = ti.now_utc

        # --- FAILED_PERMANENT is a sink — absorb any event, preserve reason ---
        if cs == LoopState.FAILED_PERMANENT:
            return self._noop(cs, ev, ctx.retry_index, ctx.last_reason_code)

        # --- RUNNING ---
        if cs == LoopState.RUNNING:
            return self._from_running(ctx, ti, budget, now_utc)

        # --- SUSPENDED_PREEMPTED ---
        if cs == LoopState.SUSPENDED_PREEMPTED:
            return self._from_suspended(ctx, ti, budget, now_utc)

        # --- REHYDRATING ---
        if cs == LoopState.REHYDRATING:
            return self._from_rehydrating(ctx, ti, budget, now_utc)

        # --- RESUMED ---
        if cs == LoopState.RESUMED:
            return self._from_resumed(ctx, ti, now_utc)

        # Catch-all: unrecognised state is a no-op
        return self._noop(cs, ev, ctx.retry_index, ctx.last_reason_code)

    # ------------------------------------------------------------------
    # Per-state helpers (all pure)
    # ------------------------------------------------------------------

    def _from_running(
        self,
        ctx: LoopRuntimeContext,
        ti: TransitionInput,
        budget: RetryBudget,
        now_utc: datetime,
    ) -> TransitionDecision:
        cs, ev = ti.current_state, ti.event

        # Self-transitions (no-ops)
        if ev in {LoopEvent.EV_GENERATE_START, LoopEvent.EV_GENERATE_SUCCESS}:
            return self._noop(cs, ev, ctx.retry_index)

        # Preemption events → suspend
        if ev in {
            LoopEvent.EV_GENERATE_TIMEOUT,
            LoopEvent.EV_CONNECTION_LOSS,
            LoopEvent.EV_SPOT_TERMINATED,
        }:
            backoff = _compute_backoff_ms(ctx.retry_index, budget)
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.SUSPENDED_PREEMPTED,
                event=ev,
                reason_code=ReasonCode.FSM_SUSPENDED_PREEMPTED,
                retry_index=ctx.retry_index,
                backoff_ms=backoff,
                terminal=False,
                actions=list(_STATE_CHANGE_ACTIONS),
            )

        # User-initiated preemption → suspend (allow rehydrate/resume later)
        if ev == LoopEvent.EV_PREEMPT:
            backoff = _compute_backoff_ms(ctx.retry_index, budget)
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.SUSPENDED_PREEMPTED,
                event=ev,
                reason_code=ReasonCode.FSM_SUSPENDED_PREEMPTED,
                retry_index=ctx.retry_index,
                backoff_ms=backoff,
                terminal=False,
                actions=list(_STATE_CHANGE_ACTIONS),
            )

        # Policy violation → permanent failure
        if ev == LoopEvent.EV_ABORT_POLICY_VIOLATION:
            return self._terminal(
                cs, ev, ctx.retry_index, ReasonCode.CONTRACT_CAPABILITY_MISMATCH
            )

        # Cancellation → permanent failure
        if ev == LoopEvent.EV_CANCELLED:
            return self._terminal(cs, ev, ctx.retry_index, reason_code=None)

        # Unhandled event — no-op
        return self._noop(cs, ev, ctx.retry_index)

    def _from_suspended(
        self,
        ctx: LoopRuntimeContext,
        ti: TransitionInput,
        budget: RetryBudget,
        now_utc: datetime,
    ) -> TransitionDecision:
        cs, ev = ti.current_state, ti.event

        if ev == LoopEvent.EV_REHYDRATE_STARTED:
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.REHYDRATING,
                event=ev,
                reason_code=None,
                retry_index=ctx.retry_index,
                backoff_ms=0,
                terminal=False,
                actions=list(_STATE_CHANGE_ACTIONS),
            )

        if ev == LoopEvent.EV_RETRY_BUDGET_EXHAUSTED:
            return self._terminal(
                cs, ev, ctx.retry_index, ReasonCode.FSM_REHYDRATE_BUDGET_EXHAUSTED
            )

        if ev == LoopEvent.EV_CANCELLED:
            return self._terminal(cs, ev, ctx.retry_index, reason_code=None)

        return self._noop(cs, ev, ctx.retry_index)

    def _from_rehydrating(
        self,
        ctx: LoopRuntimeContext,
        ti: TransitionInput,
        budget: RetryBudget,
        now_utc: datetime,
    ) -> TransitionDecision:
        cs, ev = ti.current_state, ti.event

        if ev == LoopEvent.EV_REHYDRATE_HEALTHY:
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.RESUMED,
                event=ev,
                reason_code=None,
                retry_index=ctx.retry_index,
                backoff_ms=0,
                terminal=False,
                actions=list(_STATE_CHANGE_ACTIONS),
            )

        if ev == LoopEvent.EV_REHYDRATE_FAILED:
            # Before re-suspending, check whether the budget is now exhausted.
            if _budget_exhausted(ctx, budget, now_utc):
                return self._terminal(
                    cs, ev, ctx.retry_index, ReasonCode.FSM_REHYDRATE_BUDGET_EXHAUSTED
                )
            next_retry = ctx.retry_index + 1
            backoff = _compute_backoff_ms(next_retry, budget)
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.SUSPENDED_PREEMPTED,
                event=ev,
                reason_code=ReasonCode.FSM_SUSPENDED_PREEMPTED,
                retry_index=next_retry,
                backoff_ms=backoff,
                terminal=False,
                actions=list(_STATE_CHANGE_ACTIONS),
            )

        if ev == LoopEvent.EV_RETRY_BUDGET_EXHAUSTED:
            return self._terminal(
                cs, ev, ctx.retry_index, ReasonCode.FSM_REHYDRATE_BUDGET_EXHAUSTED
            )

        if ev == LoopEvent.EV_CANCELLED:
            return self._terminal(cs, ev, ctx.retry_index, reason_code=None)

        if ev == LoopEvent.EV_PREEMPT:
            return self._terminal(cs, ev, ctx.retry_index, reason_code=None)

        return self._noop(cs, ev, ctx.retry_index)

    def _from_resumed(
        self,
        ctx: LoopRuntimeContext,
        ti: TransitionInput,
        now_utc: datetime,
    ) -> TransitionDecision:
        cs, ev = ti.current_state, ti.event

        if ev == LoopEvent.EV_GENERATE_START:
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.RUNNING,
                event=ev,
                reason_code=None,
                retry_index=ctx.retry_index,
                backoff_ms=0,
                terminal=False,
                actions=list(_STATE_CHANGE_ACTIONS),
            )

        if ev == LoopEvent.EV_CANCELLED:
            return self._terminal(cs, ev, ctx.retry_index, reason_code=None)

        if ev == LoopEvent.EV_PREEMPT:
            return self._terminal(cs, ev, ctx.retry_index, reason_code=None)

        return self._noop(cs, ev, ctx.retry_index)

    # ------------------------------------------------------------------
    # Shared decision factories
    # ------------------------------------------------------------------

    @staticmethod
    def _terminal(
        from_state: LoopState,
        event: LoopEvent,
        retry_index: int,
        reason_code: Optional[ReasonCode],
    ) -> TransitionDecision:
        return TransitionDecision(
            from_state=from_state,
            to_state=LoopState.FAILED_PERMANENT,
            event=event,
            reason_code=reason_code,
            retry_index=retry_index,
            backoff_ms=0,
            terminal=True,
            actions=list(_STATE_CHANGE_ACTIONS),
        )

    @staticmethod
    def _noop(
        state: LoopState,
        event: LoopEvent,
        retry_index: int,
        reason_code: Optional[ReasonCode] = None,
    ) -> TransitionDecision:
        return TransitionDecision(
            from_state=state,
            to_state=state,
            event=event,
            reason_code=reason_code,
            retry_index=retry_index,
            backoff_ms=0,
            terminal=False,
            actions=list(_NOOP_ACTIONS),
        )


# ---------------------------------------------------------------------------
# PreemptionFsmExecutor — durable-first side-effect executor
# ---------------------------------------------------------------------------


class PreemptionFsmExecutor(FsmExecutor):
    """
    Wraps PreemptionFsmEngine with durable-ledger-first side effects.

    Ordering contract (must never be violated):
      1. Compute pure decision  (no I/O)
      2. Idempotency guard      (checkpoint_exists check)
      3. Durable ledger append  (FIRST durable write)
      4. Telemetry emit         (best-effort, never blocks the transition)
      5. Mutate ctx             (in-memory state update)

    Authority boundary: this class MUST NOT perform infra lifecycle mutations
    (VM start/stop, HTTP calls, etc.).  Those belong to the orchestrator layer.
    """

    def __init__(
        self,
        engine: FsmEngine,
        ledger: Any,       # implements Ledger protocol
        telemetry: Any,    # implements TelemetrySink protocol (may be None)
        budget: RetryBudget = RetryBudget(),
    ) -> None:
        self._engine = engine
        self._ledger = ledger
        self._telemetry = telemetry
        self._budget = budget

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def apply(
        self,
        ctx: LoopRuntimeContext,
        ti: TransitionInput,
    ) -> TransitionDecision:
        """
        Apply a transition event to the runtime context.

        Steps:
          1. Pure decision (no side effects).
          2. Idempotency check — return immediately if already persisted.
          3. Durable ledger append.
          4. Telemetry emit (best-effort).
          5. Mutate ctx in-memory.

        Returns the TransitionDecision regardless of whether it was a
        duplicate (callers can inspect to_state and terminal flag).
        """
        # Step 1: pure, synchronous decision
        decision = self._engine.decide(ctx, ti, self._budget)

        # Step 2: idempotency guard — duplicate (op_id, checkpoint_seq) is no-op
        op_id = ti.envelope.op_id
        checkpoint_seq = ti.envelope.checkpoint_seq

        if await self._ledger.checkpoint_exists(
            op_id=op_id,
            checkpoint_seq=checkpoint_seq,
        ):
            return decision

        # Step 3: durable ledger append — MUST happen before any side effect
        await self._ledger.append_checkpoint(
            op_id=op_id,
            checkpoint_seq=checkpoint_seq,
            state=decision.to_state,
            event=decision.event,
            reason_code=decision.reason_code.value if decision.reason_code else None,
            payload={
                "retry_index": decision.retry_index,
                "backoff_ms": decision.backoff_ms,
            },
        )

        # Step 4: telemetry — best-effort, never allowed to block the transition
        if "emit_telemetry" in decision.actions and self._telemetry is not None:
            try:
                await self._telemetry.emit_transition(
                    decision,
                    {"op_id": op_id},
                )
            except Exception:  # noqa: BLE001
                # Telemetry failure must never propagate; transition already
                # durable in ledger at this point.
                pass

        # Step 5: mutate ctx (in-memory only)
        ctx.state = decision.to_state
        ctx.retry_index = decision.retry_index
        ctx.last_transition_at_utc = ti.now_utc
        # Phase transitions are also activity — bump the activity stamp so
        # ActivityMonitor sees a freshness signal even for ops that aren't
        # streaming. Stream-tick handlers will further update this between
        # transitions during long GENERATE streams.
        ctx.last_activity_at_utc = ti.now_utc
        ctx.last_reason_code = decision.reason_code

        if (
            decision.to_state == LoopState.SUSPENDED_PREEMPTED
            and ctx.first_suspend_at_utc is None
        ):
            ctx.first_suspend_at_utc = ti.now_utc

        return decision


# ---------------------------------------------------------------------------
# build_transition_input — factory helper
# ---------------------------------------------------------------------------


def build_transition_input(
    op_id: str,
    phase: str,
    event: LoopEvent,
    ctx: LoopRuntimeContext,
    checkpoint_seq: int,
    metadata: dict | None = None,
) -> TransitionInput:
    """
    Construct a TransitionInput with deterministic attempt_id and idempotency_key.

    Both identifiers are derived from stable inputs so that:
      - Replaying the same (op_id, phase, retry_index) produces the same attempt_id.
      - Replaying the same (op_id, checkpoint_seq) produces the same idempotency_key.
    This makes the entire transition pipeline safe to replay after a crash.

    Parameters
    ----------
    op_id:
        Stable operation identifier (e.g. "op-<uuidv7>-<origin>").
    phase:
        Current pipeline phase name (e.g. "GENERATE", "REHYDRATE").
    event:
        The LoopEvent triggering this transition.
    ctx:
        Current runtime context (used for retry_index and current state).
    checkpoint_seq:
        Monotonic checkpoint counter for this operation.
    metadata:
        Optional caller-supplied key/value payload attached to the envelope.

    Returns
    -------
    TransitionInput
        Fully populated, ready to pass to FsmExecutor.apply().
    """
    attempt_id = hashlib.sha256(
        f"{op_id}:{phase}:{ctx.retry_index}".encode()
    ).hexdigest()[:16]

    idempotency_key = hashlib.sha256(
        f"{op_id}:{checkpoint_seq}".encode()
    ).hexdigest()[:24]

    return TransitionInput(
        now_utc=datetime.now(timezone.utc),
        current_state=ctx.state,
        event=event,
        envelope=IdempotencyEnvelope(
            op_id=op_id,
            phase=phase,
            retry_index=ctx.retry_index,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            checkpoint_seq=checkpoint_seq,
        ),
        metadata=metadata or {},
    )
