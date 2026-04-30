# governance/contracts/fsm_contract.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence
from abc import ABC, abstractmethod
from datetime import datetime, timezone


# ---------------------------
# Immutable FSM Definitions
# ---------------------------

class LoopState(str, Enum):
    RUNNING = "RUNNING"
    SUSPENDED_PREEMPTED = "SUSPENDED_PREEMPTED"
    REHYDRATING = "REHYDRATING"
    RESUMED = "RESUMED"
    FAILED_PERMANENT = "FAILED_PERMANENT"


class LoopEvent(str, Enum):
    EV_GENERATE_START = "EV_GENERATE_START"
    EV_GENERATE_SUCCESS = "EV_GENERATE_SUCCESS"
    EV_GENERATE_TIMEOUT = "EV_GENERATE_TIMEOUT"
    EV_CONNECTION_LOSS = "EV_CONNECTION_LOSS"
    EV_SPOT_TERMINATED = "EV_SPOT_TERMINATED"
    EV_REHYDRATE_STARTED = "EV_REHYDRATE_STARTED"
    EV_REHYDRATE_HEALTHY = "EV_REHYDRATE_HEALTHY"
    EV_REHYDRATE_FAILED = "EV_REHYDRATE_FAILED"
    EV_RETRY_BUDGET_EXHAUSTED = "EV_RETRY_BUDGET_EXHAUSTED"
    EV_ABORT_POLICY_VIOLATION = "EV_ABORT_POLICY_VIOLATION"
    EV_CANCELLED = "EV_CANCELLED"
    EV_PREEMPT = "EV_PREEMPT"          # user-initiated preemption (voice/CLI stop)


class ReasonCode(str, Enum):
    CONTRACT_SCHEMA_INVALID = "CONTRACT_SCHEMA_INVALID"
    CONTRACT_VERSION_INCOMPATIBLE = "CONTRACT_VERSION_INCOMPATIBLE"
    CONTRACT_REQUIRED_BRAIN_MISSING = "CONTRACT_REQUIRED_BRAIN_MISSING"
    CONTRACT_CAPABILITY_MISMATCH = "CONTRACT_CAPABILITY_MISMATCH"
    RUNTIME_INVENTORY_STALE = "RUNTIME_INVENTORY_STALE"
    FSM_SUSPENDED_PREEMPTED = "FSM_SUSPENDED_PREEMPTED"
    FSM_REHYDRATE_BUDGET_EXHAUSTED = "FSM_REHYDRATE_BUDGET_EXHAUSTED"


@dataclass(frozen=True)
class RetryBudget:
    max_rehydrate_attempts: int = 8
    max_total_suspend_seconds: int = 1800
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 120.0
    full_jitter: bool = True


@dataclass(frozen=True)
class IdempotencyEnvelope:
    op_id: str
    phase: str
    retry_index: int
    attempt_id: str                  # deterministic hash(op_id, phase, retry_index)
    idempotency_key: str             # required for side-effects
    checkpoint_seq: int              # monotonic, durable


@dataclass(frozen=True)
class TransitionInput:
    now_utc: datetime
    current_state: LoopState
    event: LoopEvent
    envelope: IdempotencyEnvelope
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionDecision:
    from_state: LoopState
    to_state: LoopState
    event: LoopEvent
    reason_code: Optional[ReasonCode]
    retry_index: int
    backoff_ms: int
    terminal: bool
    actions: Sequence[str]  # e.g. ["append_ledger_checkpoint", "emit_telemetry", ...]


@dataclass
class LoopRuntimeContext:
    op_id: str
    state: LoopState = LoopState.RUNNING
    retry_index: int = 0
    first_suspend_at_utc: Optional[datetime] = None
    last_transition_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_reason_code: Optional[ReasonCode] = None
    # Mid-flight activity timestamp — updated when an op is making progress
    # WITHOUT a phase transition (e.g. streaming tokens during GENERATE).
    # The ActivityMonitor uses ``max(last_transition_at_utc,
    # last_activity_at_utc)`` as the freshness signal so a long-running
    # GENERATE that's actively producing tokens is not mis-classified as
    # stale. Phase transitions implicitly bump this too (any progress is
    # progress). Defaults to construction time so a brand-new ctx is fresh.
    last_activity_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Ledger(Protocol):
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
        ...

    async def checkpoint_exists(self, *, op_id: str, checkpoint_seq: int) -> bool:
        ...


class TelemetrySink(Protocol):
    async def emit_transition(self, decision: TransitionDecision, payload: Mapping[str, Any]) -> None:
        ...


class FsmEngine(ABC):
    """
    Immutable transition contract.
    Implementations MUST preserve state/event definitions and invariants.
    """

    @abstractmethod
    def decide(self, ctx: LoopRuntimeContext, ti: TransitionInput, budget: RetryBudget) -> TransitionDecision:
        """
        Pure transition function:
        - no side effects
        - deterministic decision
        """
        raise NotImplementedError


class FsmExecutor(ABC):
    """
    Side-effect executor around FsmEngine:
    - durable checkpoint first
    - then side effects
    """

    @abstractmethod
    async def apply(self, ctx: LoopRuntimeContext, ti: TransitionInput) -> TransitionDecision:
        raise NotImplementedError


# Invariants (must remain true):
# 1) No state transition without durable ledger append.
# 2) Duplicate (op_id, checkpoint_seq) is no-op.
# 3) Side effects require idempotency_key and must be replay-safe.
# 4) GovernedLoop cannot perform infra lifecycle mutation (authority boundary).
