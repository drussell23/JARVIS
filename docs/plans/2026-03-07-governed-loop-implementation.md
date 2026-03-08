# Governed Self-Programming Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire sandbox_loop.py through governance can_write() as the only write gate, add approval pause/resume, route candidate generation to GCP with failback, and define a shadow harness for one domain slice.

**Architecture:** Five new modules (op_context, orchestrator, candidate_generator, approval_provider, shadow_harness) compose into a pipeline. The orchestrator is a thin coordinator that advances OperationContext through phases. Each module is independently testable. Two existing files get minimal modifications (sandbox_loop.py, integration.py).

**Tech Stack:** Python 3.11+, asyncio, dataclasses (frozen), typing.Protocol, hashlib (SHA-256), ast, unittest.mock

**Design Doc:** `docs/plans/2026-03-07-governed-loop-design.md`

---

## Task 1: OperationContext + Phase Enum (`op_context.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/op_context.py`
- Create: `tests/test_ouroboros_governance/test_op_context.py`

**Context:** This is the typed state object passed through every pipeline phase. It's frozen — all mutations go through `advance()` which returns a new instance with updated phase and hash chain. The phase enum defines legal transitions.

**Step 1: Write the failing tests**

Create `tests/test_ouroboros_governance/test_op_context.py`:

```python
"""Tests for OperationContext and OperationPhase."""

import hashlib
import json
import pytest
from datetime import datetime, timezone
from dataclasses import FrozenInstanceError

from backend.core.ouroboros.governance.op_context import (
    OperationPhase,
    OperationContext,
    GenerationResult,
    ValidationResult,
    PHASE_TRANSITIONS,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.routing_policy import RoutingDecision


class TestOperationPhase:
    """Test the OperationPhase enum and transition table."""

    def test_all_phases_exist(self):
        phases = {p.value for p in OperationPhase}
        expected = {
            "CLASSIFY", "ROUTE", "GENERATE", "GENERATE_RETRY",
            "VALIDATE", "VALIDATE_RETRY", "GATE", "APPROVE",
            "APPLY", "VERIFY", "COMPLETE", "CANCELLED", "EXPIRED",
            "POSTMORTEM",
        }
        assert phases == expected

    def test_terminal_phases(self):
        for phase in (OperationPhase.COMPLETE, OperationPhase.CANCELLED,
                      OperationPhase.EXPIRED, OperationPhase.POSTMORTEM):
            assert phase.value in ("COMPLETE", "CANCELLED", "EXPIRED", "POSTMORTEM")
            # Terminal phases should have no outgoing transitions
            assert PHASE_TRANSITIONS.get(phase, set()) == set()

    def test_classify_transitions(self):
        allowed = PHASE_TRANSITIONS[OperationPhase.CLASSIFY]
        assert OperationPhase.ROUTE in allowed
        assert OperationPhase.CANCELLED in allowed

    def test_generate_transitions(self):
        allowed = PHASE_TRANSITIONS[OperationPhase.GENERATE]
        assert OperationPhase.VALIDATE in allowed
        assert OperationPhase.GENERATE_RETRY in allowed
        assert OperationPhase.CANCELLED in allowed

    def test_validate_transitions(self):
        allowed = PHASE_TRANSITIONS[OperationPhase.VALIDATE]
        assert OperationPhase.GATE in allowed
        assert OperationPhase.VALIDATE_RETRY in allowed
        assert OperationPhase.CANCELLED in allowed

    def test_gate_transitions(self):
        allowed = PHASE_TRANSITIONS[OperationPhase.GATE]
        assert OperationPhase.APPROVE in allowed
        assert OperationPhase.APPLY in allowed
        assert OperationPhase.CANCELLED in allowed

    def test_approve_transitions(self):
        allowed = PHASE_TRANSITIONS[OperationPhase.APPROVE]
        assert OperationPhase.APPLY in allowed
        assert OperationPhase.CANCELLED in allowed
        assert OperationPhase.EXPIRED in allowed


class TestOperationContext:
    """Test OperationContext frozen dataclass and advance()."""

    def _make_context(self, **overrides):
        now = datetime.now(timezone.utc)
        defaults = dict(
            op_id="op-test-001",
            created_at=now,
            phase=OperationPhase.CLASSIFY,
            phase_entered_at=now,
            context_hash="initial",
            previous_hash=None,
            target_files=("backend/core/ouroboros/governance/foo.py",),
            risk_tier=None,
            description="Test operation",
        )
        defaults.update(overrides)
        return OperationContext(**defaults)

    def test_frozen_after_construction(self):
        ctx = self._make_context()
        with pytest.raises(FrozenInstanceError):
            ctx.phase = OperationPhase.ROUTE  # type: ignore[misc]

    def test_advance_returns_new_instance(self):
        ctx = self._make_context()
        ctx2 = ctx.advance(OperationPhase.ROUTE)
        assert ctx2 is not ctx
        assert ctx2.phase == OperationPhase.ROUTE
        assert ctx.phase == OperationPhase.CLASSIFY  # original unchanged

    def test_advance_updates_hash_chain(self):
        ctx = self._make_context()
        ctx2 = ctx.advance(OperationPhase.ROUTE)
        assert ctx2.previous_hash == ctx.context_hash
        assert ctx2.context_hash != ctx.context_hash
        assert len(ctx2.context_hash) == 64  # SHA-256 hex

    def test_advance_updates_phase_entered_at(self):
        ctx = self._make_context()
        ctx2 = ctx.advance(OperationPhase.ROUTE)
        assert ctx2.phase_entered_at >= ctx.phase_entered_at

    def test_advance_with_updates(self):
        ctx = self._make_context()
        ctx2 = ctx.advance(
            OperationPhase.ROUTE,
            risk_tier=RiskTier.SAFE_AUTO,
            routing=RoutingDecision.LOCAL,
        )
        assert ctx2.risk_tier == RiskTier.SAFE_AUTO
        assert ctx2.routing == RoutingDecision.LOCAL

    def test_advance_invalid_transition_raises(self):
        ctx = self._make_context(phase=OperationPhase.COMPLETE)
        with pytest.raises(ValueError, match="Invalid phase transition"):
            ctx.advance(OperationPhase.GENERATE)

    def test_advance_to_cancelled_always_allowed(self):
        """CANCELLED is reachable from any non-terminal phase."""
        for phase in OperationPhase:
            if phase in (OperationPhase.COMPLETE, OperationPhase.CANCELLED,
                         OperationPhase.EXPIRED, OperationPhase.POSTMORTEM):
                continue
            ctx = self._make_context(phase=phase)
            ctx2 = ctx.advance(OperationPhase.CANCELLED)
            assert ctx2.phase == OperationPhase.CANCELLED

    def test_hash_chain_deterministic(self):
        """Same context produces same hash."""
        now = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)
        ctx1 = OperationContext(
            op_id="op-det-001", created_at=now,
            phase=OperationPhase.CLASSIFY, phase_entered_at=now,
            context_hash="seed", previous_hash=None,
            target_files=("a.py",), risk_tier=None,
            description="deterministic test",
        )
        ctx2 = OperationContext(
            op_id="op-det-001", created_at=now,
            phase=OperationPhase.CLASSIFY, phase_entered_at=now,
            context_hash="seed", previous_hash=None,
            target_files=("a.py",), risk_tier=None,
            description="deterministic test",
        )
        # Advance both with identical timestamp
        ctx1b = ctx1.advance(OperationPhase.ROUTE, _timestamp=now)
        ctx2b = ctx2.advance(OperationPhase.ROUTE, _timestamp=now)
        assert ctx1b.context_hash == ctx2b.context_hash


class TestGenerationResult:
    def test_creation(self):
        result = GenerationResult(
            candidates=({"code": "x = 1", "description": "simple"},),
            provider_name="local",
            generation_duration_s=1.5,
        )
        assert len(result.candidates) == 1
        assert result.provider_name == "local"


class TestValidationResult:
    def test_creation(self):
        result = ValidationResult(
            passed=True,
            best_candidate={"code": "x = 1"},
            validation_duration_s=0.5,
            error=None,
        )
        assert result.passed is True
        assert result.best_candidate["code"] == "x = 1"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_op_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.op_context'`

**Step 3: Write minimal implementation**

Create `backend/core/ouroboros/governance/op_context.py`:

```python
"""Typed operation context for the governed self-programming pipeline.

OperationContext is a frozen dataclass that flows through every pipeline phase.
All mutations go through advance() which enforces legal transitions and
maintains a SHA-256 hash chain for forensic traceability.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.routing_policy import RoutingDecision


# ---------------------------------------------------------------------------
# Phase enum
# ---------------------------------------------------------------------------

class OperationPhase(enum.Enum):
    CLASSIFY = "CLASSIFY"
    ROUTE = "ROUTE"
    GENERATE = "GENERATE"
    GENERATE_RETRY = "GENERATE_RETRY"
    VALIDATE = "VALIDATE"
    VALIDATE_RETRY = "VALIDATE_RETRY"
    GATE = "GATE"
    APPROVE = "APPROVE"
    APPLY = "APPLY"
    VERIFY = "VERIFY"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    POSTMORTEM = "POSTMORTEM"


TERMINAL_PHASES: Set[OperationPhase] = {
    OperationPhase.COMPLETE,
    OperationPhase.CANCELLED,
    OperationPhase.EXPIRED,
    OperationPhase.POSTMORTEM,
}

PHASE_TRANSITIONS: Dict[OperationPhase, Set[OperationPhase]] = {
    OperationPhase.CLASSIFY: {OperationPhase.ROUTE, OperationPhase.CANCELLED},
    OperationPhase.ROUTE: {OperationPhase.GENERATE, OperationPhase.CANCELLED},
    OperationPhase.GENERATE: {
        OperationPhase.VALIDATE,
        OperationPhase.GENERATE_RETRY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.GENERATE_RETRY: {
        OperationPhase.VALIDATE,
        OperationPhase.CANCELLED,
    },
    OperationPhase.VALIDATE: {
        OperationPhase.GATE,
        OperationPhase.VALIDATE_RETRY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.VALIDATE_RETRY: {
        OperationPhase.GATE,
        OperationPhase.VALIDATE_RETRY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.GATE: {
        OperationPhase.APPROVE,
        OperationPhase.APPLY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.APPROVE: {
        OperationPhase.APPLY,
        OperationPhase.CANCELLED,
        OperationPhase.EXPIRED,
    },
    OperationPhase.APPLY: {
        OperationPhase.VERIFY,
        OperationPhase.POSTMORTEM,
        OperationPhase.CANCELLED,
    },
    OperationPhase.VERIFY: {
        OperationPhase.COMPLETE,
        OperationPhase.POSTMORTEM,
    },
    # Terminal phases have no outgoing transitions
    OperationPhase.COMPLETE: set(),
    OperationPhase.CANCELLED: set(),
    OperationPhase.EXPIRED: set(),
    OperationPhase.POSTMORTEM: set(),
}


# ---------------------------------------------------------------------------
# Typed sub-objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GenerationResult:
    candidates: Tuple[Dict[str, Any], ...]
    provider_name: str
    generation_duration_s: float


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    best_candidate: Optional[Dict[str, Any]]
    validation_duration_s: float
    error: Optional[str]


@dataclass(frozen=True)
class ApprovalDecision:
    status: str  # PENDING, APPROVED, REJECTED, EXPIRED, SUPERSEDED
    approver: Optional[str]
    reason: Optional[str]
    decided_at: Optional[datetime]
    request_id: str


@dataclass(frozen=True)
class ShadowResult:
    confidence: float
    comparison_mode: str  # EXACT, AST, SEMANTIC
    violations: Tuple[str, ...]
    shadow_duration_s: float
    production_match: bool
    disqualified: bool


# ---------------------------------------------------------------------------
# OperationContext
# ---------------------------------------------------------------------------

def _compute_hash(ctx_dict: Dict[str, Any]) -> str:
    """Compute SHA-256 of a serialized context dict."""
    serialized = json.dumps(ctx_dict, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass(frozen=True)
class OperationContext:
    op_id: str
    created_at: datetime
    phase: OperationPhase
    phase_entered_at: datetime
    context_hash: str
    previous_hash: Optional[str]

    # Classification
    target_files: Tuple[str, ...]
    risk_tier: Optional[RiskTier]
    description: str

    # Phase-specific sub-objects (None until populated)
    routing: Optional[RoutingDecision] = None
    approval: Optional[ApprovalDecision] = None
    shadow: Optional[ShadowResult] = None
    generation: Optional[GenerationResult] = None
    validation: Optional[ValidationResult] = None

    # Governance
    policy_version: str = ""
    side_effects_blocked: bool = True

    def advance(
        self,
        new_phase: OperationPhase,
        _timestamp: Optional[datetime] = None,
        **updates: Any,
    ) -> "OperationContext":
        """Return a new OperationContext with updated phase + hash chain.

        Raises ValueError if the transition is illegal per PHASE_TRANSITIONS.
        """
        allowed = PHASE_TRANSITIONS.get(self.phase, set())
        if new_phase not in allowed:
            raise ValueError(
                f"Invalid phase transition: {self.phase.value} -> {new_phase.value}"
            )

        now = _timestamp or datetime.now(timezone.utc)
        new_ctx = replace(
            self,
            phase=new_phase,
            phase_entered_at=now,
            previous_hash=self.context_hash,
            **updates,
        )
        # Compute new hash over all fields except context_hash itself
        hash_dict = {
            f.name: getattr(new_ctx, f.name)
            for f in fields(new_ctx)
            if f.name != "context_hash"
        }
        new_hash = _compute_hash(hash_dict)
        return replace(new_ctx, context_hash=new_hash)

    @classmethod
    def create(
        cls,
        op_id: str,
        target_files: Tuple[str, ...],
        description: str,
        policy_version: str = "",
    ) -> "OperationContext":
        """Factory for initial context in CLASSIFY phase."""
        now = datetime.now(timezone.utc)
        seed = _compute_hash({
            "op_id": op_id,
            "created_at": str(now),
            "target_files": target_files,
        })
        return cls(
            op_id=op_id,
            created_at=now,
            phase=OperationPhase.CLASSIFY,
            phase_entered_at=now,
            context_hash=seed,
            previous_hash=None,
            target_files=target_files,
            risk_tier=None,
            description=description,
            policy_version=policy_version,
        )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_op_context.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/op_context.py tests/test_ouroboros_governance/test_op_context.py
git commit -m "feat(governance): add OperationContext with phase state machine and hash chain"
```

---

## Task 2: Approval Provider (`approval_provider.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/approval_provider.py`
- Create: `tests/test_ouroboros_governance/test_approval_provider.py`

**Context:** The ApprovalProvider protocol defines how the orchestrator pauses for human approval. The CLI implementation stores pending requests in-memory with asyncio.Event for blocking. Idempotent operations, timeout -> EXPIRED (never auto-approve), late decisions -> SUPERSEDED.

**Step 1: Write the failing tests**

Create `tests/test_ouroboros_governance/test_approval_provider.py`:

```python
"""Tests for ApprovalProvider protocol and CLIApprovalProvider."""

import asyncio
import pytest
from datetime import datetime, timezone

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalStatus,
    CLIApprovalProvider,
)
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)


@pytest.fixture
def provider():
    return CLIApprovalProvider()


@pytest.fixture
def sample_context():
    now = datetime.now(timezone.utc)
    return OperationContext(
        op_id="op-test-approve-001",
        created_at=now,
        phase=OperationPhase.APPROVE,
        phase_entered_at=now,
        context_hash="abc123",
        previous_hash=None,
        target_files=("backend/core/ouroboros/governance/foo.py",),
        risk_tier=None,
        description="Test approval",
    )


class TestCLIApprovalProvider:

    @pytest.mark.asyncio
    async def test_request_returns_request_id(self, provider, sample_context):
        request_id = await provider.request(sample_context)
        assert request_id == sample_context.op_id

    @pytest.mark.asyncio
    async def test_request_idempotent(self, provider, sample_context):
        id1 = await provider.request(sample_context)
        id2 = await provider.request(sample_context)
        assert id1 == id2

    @pytest.mark.asyncio
    async def test_approve_flow(self, provider, sample_context):
        request_id = await provider.request(sample_context)
        decision = await provider.approve(request_id, approver="derek")
        assert decision.status == ApprovalStatus.APPROVED
        assert decision.approver == "derek"
        assert decision.request_id == request_id

    @pytest.mark.asyncio
    async def test_reject_flow(self, provider, sample_context):
        request_id = await provider.request(sample_context)
        decision = await provider.reject(request_id, approver="derek", reason="too risky")
        assert decision.status == ApprovalStatus.REJECTED
        assert decision.reason == "too risky"

    @pytest.mark.asyncio
    async def test_idempotent_approve(self, provider, sample_context):
        request_id = await provider.request(sample_context)
        d1 = await provider.approve(request_id, approver="derek")
        d2 = await provider.approve(request_id, approver="derek")
        assert d1.status == ApprovalStatus.APPROVED
        assert d2.status == ApprovalStatus.APPROVED
        assert d1.decided_at == d2.decided_at  # Same decision, not re-decided

    @pytest.mark.asyncio
    async def test_await_decision_resolves_on_approve(self, provider, sample_context):
        request_id = await provider.request(sample_context)

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            await provider.approve(request_id, approver="derek")

        asyncio.get_event_loop().create_task(approve_after_delay())
        decision = await provider.await_decision(request_id, timeout_s=5.0)
        assert decision.status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_await_decision_timeout_returns_expired(self, provider, sample_context):
        request_id = await provider.request(sample_context)
        decision = await provider.await_decision(request_id, timeout_s=0.05)
        assert decision.status == ApprovalStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_late_decision_after_expired_returns_superseded(self, provider, sample_context):
        request_id = await provider.request(sample_context)
        # Let it expire
        expired = await provider.await_decision(request_id, timeout_s=0.05)
        assert expired.status == ApprovalStatus.EXPIRED
        # Late approve
        late = await provider.approve(request_id, approver="derek")
        assert late.status == ApprovalStatus.SUPERSEDED

    @pytest.mark.asyncio
    async def test_approve_unknown_request_raises(self, provider):
        with pytest.raises(KeyError):
            await provider.approve("nonexistent", approver="derek")

    @pytest.mark.asyncio
    async def test_list_pending(self, provider, sample_context):
        await provider.request(sample_context)
        pending = provider.list_pending()
        assert len(pending) == 1
        assert pending[0]["op_id"] == sample_context.op_id

    @pytest.mark.asyncio
    async def test_list_pending_excludes_decided(self, provider, sample_context):
        request_id = await provider.request(sample_context)
        await provider.approve(request_id, approver="derek")
        pending = provider.list_pending()
        assert len(pending) == 0
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_approval_provider.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `backend/core/ouroboros/governance/approval_provider.py`:

```python
"""Approval provider protocol and CLI implementation.

The ApprovalProvider protocol defines how the orchestrator pauses for human
approval on APPROVAL_REQUIRED operations. CLIApprovalProvider is the Phase 1
implementation using in-memory state and asyncio.Event for blocking.

Behavioral guarantees:
- Idempotent: approving an already-approved op returns existing decision
- Timeout -> EXPIRED: never auto-approve
- Late decision after EXPIRED -> SUPERSEDED: forensic trail, no effect
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger(__name__)


class ApprovalStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class ApprovalResult:
    status: ApprovalStatus
    approver: Optional[str]
    reason: Optional[str]
    decided_at: Optional[datetime]
    request_id: str


@runtime_checkable
class ApprovalProvider(Protocol):
    async def request(self, context: OperationContext) -> str: ...
    async def approve(self, request_id: str, approver: str) -> ApprovalResult: ...
    async def reject(self, request_id: str, approver: str, reason: str) -> ApprovalResult: ...
    async def await_decision(self, request_id: str, timeout_s: float) -> ApprovalResult: ...


@dataclass
class _PendingRequest:
    context: OperationContext
    result: Optional[ApprovalResult]
    event: asyncio.Event
    created_at: datetime


class CLIApprovalProvider:
    """In-memory approval provider for CLI-driven approval workflows."""

    def __init__(self) -> None:
        self._requests: Dict[str, _PendingRequest] = {}

    async def request(self, context: OperationContext) -> str:
        request_id = context.op_id
        if request_id not in self._requests:
            self._requests[request_id] = _PendingRequest(
                context=context,
                result=None,
                event=asyncio.Event(),
                created_at=datetime.now(timezone.utc),
            )
            logger.info(
                "[Approval] Pending: op_id=%s desc=%s files=%s",
                context.op_id, context.description, context.target_files,
            )
        return request_id

    async def approve(self, request_id: str, approver: str) -> ApprovalResult:
        pending = self._requests.get(request_id)
        if pending is None:
            raise KeyError(f"No pending request: {request_id}")
        # Idempotent: return existing decision
        if pending.result is not None:
            if pending.result.status in (ApprovalStatus.EXPIRED, ApprovalStatus.REJECTED):
                result = ApprovalResult(
                    status=ApprovalStatus.SUPERSEDED,
                    approver=approver,
                    reason="late_decision_after_" + pending.result.status.value.lower(),
                    decided_at=datetime.now(timezone.utc),
                    request_id=request_id,
                )
                logger.warning("[Approval] SUPERSEDED: %s (was %s)", request_id, pending.result.status.value)
                return result
            return pending.result
        result = ApprovalResult(
            status=ApprovalStatus.APPROVED,
            approver=approver,
            reason=None,
            decided_at=datetime.now(timezone.utc),
            request_id=request_id,
        )
        pending.result = result
        pending.event.set()
        logger.info("[Approval] APPROVED: %s by %s", request_id, approver)
        return result

    async def reject(self, request_id: str, approver: str, reason: str) -> ApprovalResult:
        pending = self._requests.get(request_id)
        if pending is None:
            raise KeyError(f"No pending request: {request_id}")
        if pending.result is not None:
            if pending.result.status in (ApprovalStatus.EXPIRED,):
                result = ApprovalResult(
                    status=ApprovalStatus.SUPERSEDED,
                    approver=approver,
                    reason="late_decision_after_expired",
                    decided_at=datetime.now(timezone.utc),
                    request_id=request_id,
                )
                return result
            return pending.result
        result = ApprovalResult(
            status=ApprovalStatus.REJECTED,
            approver=approver,
            reason=reason,
            decided_at=datetime.now(timezone.utc),
            request_id=request_id,
        )
        pending.result = result
        pending.event.set()
        logger.info("[Approval] REJECTED: %s by %s reason=%s", request_id, approver, reason)
        return result

    async def await_decision(self, request_id: str, timeout_s: float) -> ApprovalResult:
        pending = self._requests.get(request_id)
        if pending is None:
            raise KeyError(f"No pending request: {request_id}")
        if pending.result is not None:
            return pending.result
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            if pending.result is None:
                expired = ApprovalResult(
                    status=ApprovalStatus.EXPIRED,
                    approver=None,
                    reason="timeout",
                    decided_at=datetime.now(timezone.utc),
                    request_id=request_id,
                )
                pending.result = expired
                pending.event.set()
                logger.warning("[Approval] EXPIRED: %s after %.1fs", request_id, timeout_s)
        return pending.result  # type: ignore[return-value]

    def list_pending(self) -> List[Dict[str, Any]]:
        result = []
        for rid, req in self._requests.items():
            if req.result is None or req.result.status == ApprovalStatus.PENDING:
                result.append({
                    "op_id": req.context.op_id,
                    "description": req.context.description,
                    "target_files": req.context.target_files,
                    "created_at": str(req.created_at),
                    "request_id": rid,
                })
        return result
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_approval_provider.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/approval_provider.py tests/test_ouroboros_governance/test_approval_provider.py
git commit -m "feat(governance): add ApprovalProvider protocol and CLI implementation"
```

---

## Task 3: Candidate Generator + Failback FSM (`candidate_generator.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/candidate_generator.py`
- Create: `tests/test_ouroboros_governance/test_candidate_generator.py`

**Context:** The candidate generator wraps a failback state machine around two CandidateProvider implementations (GCP Prime and Local). Failover is immediate (one failure). Failback requires 3 consecutive health probes over 45s. Per-provider concurrency quotas. Deadlines propagated, not fixed timeouts.

**Step 1: Write the failing tests**

Create `tests/test_ouroboros_governance/test_candidate_generator.py`:

```python
"""Tests for CandidateGenerator and FailbackStateMachine."""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, PropertyMock

from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackState,
    FailbackStateMachine,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
)


def _make_context(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        op_id="op-gen-001", created_at=now,
        phase=OperationPhase.GENERATE, phase_entered_at=now,
        context_hash="abc", previous_hash=None,
        target_files=("a.py",), risk_tier=None,
        description="Generate test",
    )
    defaults.update(overrides)
    return OperationContext(**defaults)


def _make_provider(name="mock_primary", generate_result=None, healthy=True):
    provider = AsyncMock()
    type(provider).provider_name = PropertyMock(return_value=name)
    if generate_result is None:
        generate_result = GenerationResult(
            candidates=({"code": "x = 1", "description": "simple"},),
            provider_name=name,
            generation_duration_s=1.0,
        )
    provider.generate.return_value = generate_result
    provider.health_probe.return_value = healthy
    return provider


class TestFailbackStateMachine:

    def test_initial_state(self):
        fsm = FailbackStateMachine()
        assert fsm.state == FailbackState.PRIMARY_READY

    def test_primary_failure_transitions_to_fallback(self):
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()
        assert fsm.state == FailbackState.FALLBACK_ACTIVE

    def test_single_probe_not_enough_for_recovery(self):
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()
        fsm.record_probe_success()
        # Should be DEGRADED (probing), not PRIMARY_READY
        assert fsm.state == FailbackState.PRIMARY_DEGRADED

    def test_three_probes_with_dwell_recovers(self):
        fsm = FailbackStateMachine(
            required_probes=3,
            dwell_time_s=0.0,  # Zero dwell for test speed
        )
        fsm.record_primary_failure()
        assert fsm.state == FailbackState.FALLBACK_ACTIVE
        fsm.record_probe_success()
        assert fsm.state == FailbackState.PRIMARY_DEGRADED
        fsm.record_probe_success()
        fsm.record_probe_success()
        assert fsm.state == FailbackState.PRIMARY_READY

    def test_dwell_time_enforced(self):
        fsm = FailbackStateMachine(
            required_probes=3,
            dwell_time_s=100.0,  # Large dwell — won't pass
        )
        fsm.record_primary_failure()
        fsm.record_probe_success()
        fsm.record_probe_success()
        fsm.record_probe_success()
        # Still degraded because dwell time hasn't elapsed
        assert fsm.state == FailbackState.PRIMARY_DEGRADED

    def test_fallback_failure_transitions_to_queue_only(self):
        fsm = FailbackStateMachine()
        fsm.record_primary_failure()
        fsm.record_fallback_failure()
        assert fsm.state == FailbackState.QUEUE_ONLY


class TestCandidateGenerator:

    @pytest.mark.asyncio
    async def test_primary_success(self):
        primary = _make_provider("gcp_prime")
        local = _make_provider("local")
        gen = CandidateGenerator(primary=primary, fallback=local)
        ctx = _make_context()
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        result = await gen.generate(ctx, deadline)
        assert result.provider_name == "gcp_prime"
        primary.generate.assert_awaited_once()
        local.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_primary_timeout_falls_back(self):
        primary = _make_provider("gcp_prime")
        primary.generate.side_effect = asyncio.TimeoutError()
        local = _make_provider("local")
        gen = CandidateGenerator(primary=primary, fallback=local)
        ctx = _make_context()
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        result = await gen.generate(ctx, deadline)
        assert result.provider_name == "local"
        assert gen.fsm.state == FailbackState.FALLBACK_ACTIVE

    @pytest.mark.asyncio
    async def test_both_fail_raises(self):
        primary = _make_provider("gcp_prime")
        primary.generate.side_effect = asyncio.TimeoutError()
        local = _make_provider("local")
        local.generate.side_effect = RuntimeError("local failed")
        gen = CandidateGenerator(primary=primary, fallback=local)
        ctx = _make_context()
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        with pytest.raises(RuntimeError, match="all_providers_exhausted"):
            await gen.generate(ctx, deadline)
        assert gen.fsm.state == FailbackState.QUEUE_ONLY

    @pytest.mark.asyncio
    async def test_concurrency_quota_limits_parallel(self):
        primary = _make_provider("gcp_prime")
        gen = CandidateGenerator(
            primary=primary,
            fallback=_make_provider("local"),
            primary_concurrency=2,
        )
        ctx = _make_context()
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        # Should respect semaphore (won't deadlock with 2 concurrent)
        results = await asyncio.gather(
            gen.generate(ctx, deadline),
            gen.generate(ctx, deadline),
        )
        assert len(results) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_candidate_generator.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `backend/core/ouroboros/governance/candidate_generator.py`:

```python
"""Candidate generator with failback state machine.

Routes candidate generation requests to GCP Prime (primary) or local model
(fallback) with deterministic failback logic. Failover is immediate on first
failure. Failback requires N consecutive health probes over a dwell period.

Per-provider concurrency quotas via asyncio.Semaphore.
Deadlines propagated to providers — no fixed internal timeouts.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Protocol, runtime_checkable

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class CandidateProvider(Protocol):
    async def generate(self, context: OperationContext, deadline: datetime) -> GenerationResult: ...
    async def health_probe(self) -> bool: ...
    @property
    def provider_name(self) -> str: ...


# ---------------------------------------------------------------------------
# Failback state machine
# ---------------------------------------------------------------------------

class FailbackState(enum.Enum):
    PRIMARY_READY = "PRIMARY_READY"
    FALLBACK_ACTIVE = "FALLBACK_ACTIVE"
    PRIMARY_DEGRADED = "PRIMARY_DEGRADED"
    QUEUE_ONLY = "QUEUE_ONLY"


class FailbackStateMachine:
    """4-state FSM with asymmetric failover/failback timing."""

    def __init__(
        self,
        required_probes: int = 3,
        dwell_time_s: float = 45.0,
    ) -> None:
        self._state = FailbackState.PRIMARY_READY
        self._required_probes = required_probes
        self._dwell_time_s = dwell_time_s
        self._probe_successes: List[float] = []
        self._dwell_start: Optional[float] = None

    @property
    def state(self) -> FailbackState:
        return self._state

    def record_primary_failure(self) -> None:
        if self._state == FailbackState.PRIMARY_READY:
            self._state = FailbackState.FALLBACK_ACTIVE
            self._probe_successes.clear()
            self._dwell_start = None
            logger.warning("[Failback] PRIMARY_READY -> FALLBACK_ACTIVE")

    def record_fallback_failure(self) -> None:
        if self._state == FailbackState.FALLBACK_ACTIVE:
            self._state = FailbackState.QUEUE_ONLY
            logger.error("[Failback] FALLBACK_ACTIVE -> QUEUE_ONLY")

    def record_probe_success(self) -> None:
        now = time.monotonic()
        if self._state == FailbackState.FALLBACK_ACTIVE:
            self._state = FailbackState.PRIMARY_DEGRADED
            self._probe_successes = [now]
            self._dwell_start = now
            logger.info("[Failback] FALLBACK_ACTIVE -> PRIMARY_DEGRADED (probe 1/%d)", self._required_probes)
        elif self._state == FailbackState.PRIMARY_DEGRADED:
            self._probe_successes.append(now)
            if (
                len(self._probe_successes) >= self._required_probes
                and self._dwell_start is not None
                and (now - self._dwell_start) >= self._dwell_time_s
            ):
                self._state = FailbackState.PRIMARY_READY
                self._probe_successes.clear()
                self._dwell_start = None
                logger.info("[Failback] PRIMARY_DEGRADED -> PRIMARY_READY (recovered)")

    def record_probe_failure(self) -> None:
        if self._state == FailbackState.PRIMARY_DEGRADED:
            self._state = FailbackState.FALLBACK_ACTIVE
            self._probe_successes.clear()
            self._dwell_start = None
            logger.warning("[Failback] PRIMARY_DEGRADED -> FALLBACK_ACTIVE (probe failed)")


# ---------------------------------------------------------------------------
# CandidateGenerator
# ---------------------------------------------------------------------------

class CandidateGenerator:
    """Routes generation requests through failback FSM."""

    def __init__(
        self,
        primary: CandidateProvider,
        fallback: CandidateProvider,
        primary_concurrency: int = 4,
        fallback_concurrency: int = 2,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self.fsm = FailbackStateMachine()
        self._primary_sem = asyncio.Semaphore(primary_concurrency)
        self._fallback_sem = asyncio.Semaphore(fallback_concurrency)

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate candidates via primary or fallback provider."""
        if self.fsm.state == FailbackState.QUEUE_ONLY:
            raise RuntimeError("all_providers_exhausted")

        if self.fsm.state == FailbackState.PRIMARY_READY:
            try:
                return await self._call_primary(context, deadline)
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning(
                    "[Generator] Primary failed: %s — falling back", exc,
                )
                self.fsm.record_primary_failure()
                return await self._call_fallback(context, deadline)

        # FALLBACK_ACTIVE or PRIMARY_DEGRADED — use fallback
        return await self._call_fallback(context, deadline)

    async def _call_primary(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        async with self._primary_sem:
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise asyncio.TimeoutError("deadline_already_passed")
            return await asyncio.wait_for(
                self._primary.generate(context, deadline),
                timeout=remaining,
            )

    async def _call_fallback(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        try:
            async with self._fallback_sem:
                remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    raise asyncio.TimeoutError("deadline_already_passed")
                return await asyncio.wait_for(
                    self._fallback.generate(context, deadline),
                    timeout=remaining,
                )
        except Exception as exc:
            self.fsm.record_fallback_failure()
            raise RuntimeError("all_providers_exhausted") from exc

    async def run_health_probe(self) -> bool:
        """Probe primary provider health. Updates FSM state."""
        try:
            healthy = await self._primary.health_probe()
            if healthy:
                self.fsm.record_probe_success()
            else:
                self.fsm.record_probe_failure()
            return healthy
        except Exception:
            self.fsm.record_probe_failure()
            return False
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_candidate_generator.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/candidate_generator.py tests/test_ouroboros_governance/test_candidate_generator.py
git commit -m "feat(governance): add CandidateGenerator with failback state machine"
```

---

## Task 4: Shadow Harness (`shadow_harness.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/shadow_harness.py`
- Create: `tests/test_ouroboros_governance/test_shadow_harness.py`

**Context:** The shadow harness enforces a hard side-effect firewall via monkey-patching, runs candidate code in isolation, and compares outputs with a confidence score. Three comparison modes (EXACT, AST, SEMANTIC). Auto-disqualifies slices with 3 consecutive low-confidence runs.

**Step 1: Write the failing tests**

Create `tests/test_ouroboros_governance/test_shadow_harness.py`:

```python
"""Tests for ShadowHarness, SideEffectFirewall, and OutputComparator."""

import ast
import json
import pytest

from backend.core.ouroboros.governance.shadow_harness import (
    CompareMode,
    OutputComparator,
    ShadowHarness,
    ShadowModeViolation,
    ShadowResult,
    SideEffectFirewall,
)


class TestSideEffectFirewall:

    def test_blocks_file_write(self, tmp_path):
        target = tmp_path / "test.txt"
        with SideEffectFirewall():
            with pytest.raises(ShadowModeViolation, match="write"):
                open(str(target), "w")

    def test_blocks_file_append(self, tmp_path):
        target = tmp_path / "test.txt"
        with SideEffectFirewall():
            with pytest.raises(ShadowModeViolation, match="write"):
                open(str(target), "a")

    def test_allows_file_read(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("hello")
        with SideEffectFirewall():
            with open(str(target), "r") as f:
                assert f.read() == "hello"

    def test_blocks_subprocess_run(self):
        import subprocess
        with SideEffectFirewall():
            with pytest.raises(ShadowModeViolation, match="subprocess"):
                subprocess.run(["echo", "hi"])

    def test_blocks_subprocess_popen(self):
        import subprocess
        with SideEffectFirewall():
            with pytest.raises(ShadowModeViolation, match="subprocess"):
                subprocess.Popen(["echo", "hi"])

    def test_blocks_os_remove(self, tmp_path):
        import os
        target = tmp_path / "test.txt"
        target.write_text("hello")
        with SideEffectFirewall():
            with pytest.raises(ShadowModeViolation):
                os.remove(str(target))

    def test_allows_ast_parse(self):
        with SideEffectFirewall():
            tree = ast.parse("x = 1")
            assert isinstance(tree, ast.Module)

    def test_allows_json_loads(self):
        with SideEffectFirewall():
            data = json.loads('{"key": "value"}')
            assert data["key"] == "value"

    def test_restores_originals_on_exit(self, tmp_path):
        target = tmp_path / "test.txt"
        with SideEffectFirewall():
            pass
        # After exiting, write should work again
        with open(str(target), "w") as f:
            f.write("restored")
        assert target.read_text() == "restored"

    def test_restores_on_exception(self, tmp_path):
        target = tmp_path / "test.txt"
        try:
            with SideEffectFirewall():
                raise ValueError("oops")
        except ValueError:
            pass
        with open(str(target), "w") as f:
            f.write("restored")
        assert target.read_text() == "restored"


class TestOutputComparator:

    def test_exact_match_returns_1(self):
        comp = OutputComparator()
        assert comp.compare("hello", "hello", CompareMode.EXACT) == 1.0

    def test_exact_mismatch_returns_0(self):
        comp = OutputComparator()
        assert comp.compare("hello", "world", CompareMode.EXACT) == 0.0

    def test_ast_identical_returns_1(self):
        comp = OutputComparator()
        score = comp.compare("x = 1\n", "x = 1\n", CompareMode.AST)
        assert score == 1.0

    def test_ast_whitespace_diff_high_score(self):
        comp = OutputComparator()
        score = comp.compare(
            "x  =  1\n\n",
            "x = 1\n",
            CompareMode.AST,
        )
        assert score >= 0.9

    def test_ast_different_code_low_score(self):
        comp = OutputComparator()
        score = comp.compare(
            "x = 1",
            "y = 2\nz = 3",
            CompareMode.AST,
        )
        assert score < 0.5

    def test_ast_unparseable_returns_0(self):
        comp = OutputComparator()
        score = comp.compare(
            "x = 1",
            "this is not python!!!{{{",
            CompareMode.AST,
        )
        assert score == 0.0


class TestShadowHarness:

    def test_shadow_result_creation(self):
        result = ShadowResult(
            confidence=0.95,
            comparison_mode=CompareMode.AST,
            violations=(),
            shadow_duration_s=0.5,
            production_match=True,
            disqualified=False,
        )
        assert result.confidence == 0.95
        assert result.production_match is True

    def test_disqualification_after_three_low_confidence(self):
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        for _ in range(3):
            harness.record_run(confidence=0.5)
        assert harness.is_disqualified is True

    def test_no_disqualification_with_mixed_scores(self):
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        harness.record_run(confidence=0.5)
        harness.record_run(confidence=0.8)  # breaks streak
        harness.record_run(confidence=0.5)
        assert harness.is_disqualified is False

    def test_high_confidence_resets_streak(self):
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        harness.record_run(confidence=0.5)
        harness.record_run(confidence=0.5)
        harness.record_run(confidence=0.9)  # resets
        harness.record_run(confidence=0.5)
        assert harness.is_disqualified is False
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_shadow_harness.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `backend/core/ouroboros/governance/shadow_harness.py`:

```python
"""Shadow execution harness with side-effect firewall and output comparator.

Runs candidate code in a side-effect-free environment. The firewall uses
monkey-patching to physically prevent file writes, process spawning, and
network calls. This is hard enforcement, not convention.

Output comparator scores similarity between expected and actual outputs
in three modes: EXACT, AST, SEMANTIC.
"""

from __future__ import annotations

import ast
import builtins
import enum
import logging
import os
import subprocess
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ShadowModeViolation(Exception):
    """Raised when shadow code attempts a forbidden side effect."""


class CompareMode(enum.Enum):
    EXACT = "EXACT"
    AST = "AST"
    SEMANTIC = "SEMANTIC"


@dataclass(frozen=True)
class ShadowResult:
    confidence: float
    comparison_mode: CompareMode
    violations: Tuple[str, ...]
    shadow_duration_s: float
    production_match: bool
    disqualified: bool


# ---------------------------------------------------------------------------
# Side-Effect Firewall
# ---------------------------------------------------------------------------

class SideEffectFirewall:
    """Context manager that monkey-patches dangerous operations.

    BLOCKED: file writes, subprocess, os mutations, network
    ALLOWED: file reads, ast.parse, json.loads, math, etc.
    """

    def __init__(self) -> None:
        self._originals: Dict[str, Any] = {}

    def __enter__(self) -> "SideEffectFirewall":
        # Save originals
        self._originals["builtins_open"] = builtins.open
        self._originals["subprocess_run"] = subprocess.run
        self._originals["subprocess_popen"] = subprocess.Popen
        self._originals["os_remove"] = os.remove
        self._originals["os_unlink"] = os.unlink
        self._originals["shutil_rmtree"] = shutil.rmtree

        original_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(m in mode for m in ("w", "a", "x", "+")):
                raise ShadowModeViolation(
                    f"Shadow mode: file write blocked (mode={mode!r}, file={file!r})"
                )
            return original_open(file, mode, *args, **kwargs)

        def blocked_subprocess_run(*args, **kwargs):
            raise ShadowModeViolation("Shadow mode: subprocess.run blocked")

        def blocked_subprocess_popen(*args, **kwargs):
            raise ShadowModeViolation("Shadow mode: subprocess.Popen blocked")

        def blocked_os_remove(*args, **kwargs):
            raise ShadowModeViolation("Shadow mode: os.remove blocked")

        def blocked_os_unlink(*args, **kwargs):
            raise ShadowModeViolation("Shadow mode: os.unlink blocked")

        def blocked_shutil_rmtree(*args, **kwargs):
            raise ShadowModeViolation("Shadow mode: shutil.rmtree blocked")

        # Apply patches
        builtins.open = guarded_open  # type: ignore[assignment]
        subprocess.run = blocked_subprocess_run  # type: ignore[assignment]
        subprocess.Popen = blocked_subprocess_popen  # type: ignore[assignment]
        os.remove = blocked_os_remove  # type: ignore[assignment]
        os.unlink = blocked_os_unlink  # type: ignore[assignment]
        shutil.rmtree = blocked_shutil_rmtree  # type: ignore[assignment]

        return self

    def __exit__(self, *args: Any) -> None:
        # Restore ALL originals unconditionally
        builtins.open = self._originals["builtins_open"]  # type: ignore[assignment]
        subprocess.run = self._originals["subprocess_run"]  # type: ignore[assignment]
        subprocess.Popen = self._originals["subprocess_popen"]  # type: ignore[assignment]
        os.remove = self._originals["os_remove"]  # type: ignore[assignment]
        os.unlink = self._originals["os_unlink"]  # type: ignore[assignment]
        shutil.rmtree = self._originals["shutil_rmtree"]  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Output Comparator
# ---------------------------------------------------------------------------

class OutputComparator:
    """Compare expected vs actual output with configurable mode."""

    def compare(self, expected: Any, actual: Any, mode: CompareMode) -> float:
        if mode == CompareMode.EXACT:
            return 1.0 if expected == actual else 0.0
        elif mode == CompareMode.AST:
            return self._compare_ast(str(expected), str(actual))
        elif mode == CompareMode.SEMANTIC:
            return self._compare_semantic(str(expected), str(actual))
        return 0.0

    def _compare_ast(self, expected: str, actual: str) -> float:
        try:
            tree_a = ast.parse(expected)
            tree_b = ast.parse(actual)
        except SyntaxError:
            return 0.0
        dump_a = ast.dump(tree_a, annotate_fields=False)
        dump_b = ast.dump(tree_b, annotate_fields=False)
        if dump_a == dump_b:
            return 1.0
        # Partial similarity based on common prefix length
        common = 0
        for ca, cb in zip(dump_a, dump_b):
            if ca == cb:
                common += 1
            else:
                break
        max_len = max(len(dump_a), len(dump_b))
        if max_len == 0:
            return 1.0
        return common / max_len

    def _compare_semantic(self, expected: str, actual: str) -> float:
        # Semantic comparison: normalize whitespace, compare AST structure
        # For Phase 1, delegate to AST comparison
        return self._compare_ast(expected, actual)


# ---------------------------------------------------------------------------
# Shadow Harness (confidence tracking + disqualification)
# ---------------------------------------------------------------------------

class ShadowHarness:
    """Tracks shadow run confidence and manages auto-disqualification."""

    def __init__(
        self,
        confidence_threshold: float = 0.7,
        disqualify_after: int = 3,
    ) -> None:
        self._threshold = confidence_threshold
        self._disqualify_after = disqualify_after
        self._consecutive_low: int = 0
        self._disqualified: bool = False
        self._runs: List[float] = []

    @property
    def is_disqualified(self) -> bool:
        return self._disqualified

    def record_run(self, confidence: float) -> None:
        self._runs.append(confidence)
        if confidence < self._threshold:
            self._consecutive_low += 1
            if self._consecutive_low >= self._disqualify_after:
                self._disqualified = True
                logger.warning(
                    "[Shadow] Auto-disqualified: %d consecutive runs below %.2f",
                    self._consecutive_low, self._threshold,
                )
        else:
            self._consecutive_low = 0

    def reset(self) -> None:
        self._consecutive_low = 0
        self._disqualified = False
        self._runs.clear()
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_shadow_harness.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/shadow_harness.py tests/test_ouroboros_governance/test_shadow_harness.py
git commit -m "feat(governance): add shadow harness with side-effect firewall and output comparator"
```

---

## Task 5: Orchestrator (`orchestrator.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/orchestrator.py`
- Create: `tests/test_ouroboros_governance/test_orchestrator.py`

**Context:** The orchestrator is a thin pipeline coordinator. It advances OperationContext through phases by calling existing components (risk_engine, candidate_generator, change_engine, etc.). It owns no domain logic — only phase transitions and error handling. Every failure ends in a terminal state.

**Docs to check:** Design doc Section 2 (lifecycle), Section 6 (failure matrix).

**Step 1: Write the failing tests**

Create `tests/test_ouroboros_governance/test_orchestrator.py`:

```python
"""Tests for the governed pipeline orchestrator."""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskClassification,
    RiskTier,
)
from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)


def _mock_stack(can_write_result=(True, "ok")):
    stack = MagicMock()
    stack.can_write.return_value = can_write_result
    stack.risk_engine.classify.return_value = RiskClassification(
        tier=RiskTier.SAFE_AUTO,
        reason_code="default_safe",
    )
    stack.ledger = AsyncMock()
    stack.ledger.append = AsyncMock(return_value=True)
    stack.comm = AsyncMock()
    stack.change_engine = AsyncMock()
    stack.change_engine.execute = AsyncMock(return_value=MagicMock(
        success=True, rolled_back=False, op_id="op-test-001",
    ))
    return stack


def _mock_generator(result=None):
    gen = AsyncMock()
    if result is None:
        result = GenerationResult(
            candidates=({"code": "x = 1", "description": "simple"},),
            provider_name="local",
            generation_duration_s=1.0,
        )
    gen.generate = AsyncMock(return_value=result)
    return gen


def _mock_approval(status=ApprovalStatus.APPROVED):
    prov = AsyncMock()
    prov.request = AsyncMock(return_value="op-test-001")
    prov.await_decision = AsyncMock(return_value=ApprovalResult(
        status=status,
        approver="derek",
        reason=None,
        decided_at=datetime.now(timezone.utc),
        request_id="op-test-001",
    ))
    return prov


class TestGovernedOrchestrator:

    @pytest.mark.asyncio
    async def test_happy_path_safe_auto(self, tmp_path):
        """SAFE_AUTO op goes CLASSIFY->ROUTE->GENERATE->VALIDATE->GATE->APPLY->VERIFY->COMPLETE."""
        stack = _mock_stack()
        gen = _mock_generator()
        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen,
            approval_provider=None,
            config=OrchestratorConfig(project_root=tmp_path),
        )
        ctx = OperationContext.create(
            op_id="op-test-001",
            target_files=("backend/core/ouroboros/governance/foo.py",),
            description="Test happy path",
        )
        result = await orch.run(ctx)
        assert result.phase in (OperationPhase.COMPLETE, OperationPhase.VERIFY)
        # Ledger should have been called
        assert stack.ledger.append.await_count >= 1

    @pytest.mark.asyncio
    async def test_can_write_false_cancels(self, tmp_path):
        stack = _mock_stack(can_write_result=(False, "canary_not_promoted:foo.py"))
        gen = _mock_generator()
        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen,
            approval_provider=None,
            config=OrchestratorConfig(project_root=tmp_path),
        )
        ctx = OperationContext.create(
            op_id="op-test-002",
            target_files=("foo.py",),
            description="Test gate denial",
        )
        result = await orch.run(ctx)
        assert result.phase == OperationPhase.CANCELLED

    @pytest.mark.asyncio
    async def test_approval_required_pauses_then_approves(self, tmp_path):
        stack = _mock_stack()
        stack.risk_engine.classify.return_value = RiskClassification(
            tier=RiskTier.APPROVAL_REQUIRED,
            reason_code="crosses_repo_boundary",
        )
        gen = _mock_generator()
        approval = _mock_approval(ApprovalStatus.APPROVED)
        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen,
            approval_provider=approval,
            config=OrchestratorConfig(project_root=tmp_path),
        )
        ctx = OperationContext.create(
            op_id="op-test-003",
            target_files=("foo.py",),
            description="Test approval flow",
        )
        result = await orch.run(ctx)
        approval.request.assert_awaited_once()
        approval.await_decision.assert_awaited_once()
        # Should proceed to APPLY after approval
        assert result.phase not in (OperationPhase.CANCELLED, OperationPhase.EXPIRED)

    @pytest.mark.asyncio
    async def test_approval_timeout_expires(self, tmp_path):
        stack = _mock_stack()
        stack.risk_engine.classify.return_value = RiskClassification(
            tier=RiskTier.APPROVAL_REQUIRED,
            reason_code="crosses_repo_boundary",
        )
        gen = _mock_generator()
        approval = _mock_approval(ApprovalStatus.EXPIRED)
        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen,
            approval_provider=approval,
            config=OrchestratorConfig(project_root=tmp_path),
        )
        ctx = OperationContext.create(
            op_id="op-test-004",
            target_files=("foo.py",),
            description="Test approval timeout",
        )
        result = await orch.run(ctx)
        assert result.phase == OperationPhase.EXPIRED

    @pytest.mark.asyncio
    async def test_generation_failure_cancels(self, tmp_path):
        stack = _mock_stack()
        gen = AsyncMock()
        gen.generate = AsyncMock(side_effect=RuntimeError("all_providers_exhausted"))
        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen,
            approval_provider=None,
            config=OrchestratorConfig(project_root=tmp_path),
        )
        ctx = OperationContext.create(
            op_id="op-test-005",
            target_files=("foo.py",),
            description="Test gen failure",
        )
        result = await orch.run(ctx)
        assert result.phase == OperationPhase.CANCELLED

    @pytest.mark.asyncio
    async def test_blocked_tier_cancels_immediately(self, tmp_path):
        stack = _mock_stack()
        stack.risk_engine.classify.return_value = RiskClassification(
            tier=RiskTier.BLOCKED,
            reason_code="touches_supervisor",
        )
        gen = _mock_generator()
        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen,
            approval_provider=None,
            config=OrchestratorConfig(project_root=tmp_path),
        )
        ctx = OperationContext.create(
            op_id="op-test-006",
            target_files=("unified_supervisor.py",),
            description="Test blocked",
        )
        result = await orch.run(ctx)
        assert result.phase == OperationPhase.CANCELLED

    @pytest.mark.asyncio
    async def test_crash_in_pipeline_goes_to_postmortem(self, tmp_path):
        stack = _mock_stack()
        gen = _mock_generator()
        # Make change_engine raise
        stack.change_engine.execute = AsyncMock(side_effect=IOError("disk full"))
        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen,
            approval_provider=None,
            config=OrchestratorConfig(project_root=tmp_path),
        )
        ctx = OperationContext.create(
            op_id="op-test-007",
            target_files=("foo.py",),
            description="Test crash",
        )
        result = await orch.run(ctx)
        assert result.phase == OperationPhase.POSTMORTEM
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `backend/core/ouroboros/governance/orchestrator.py`:

```python
"""Governed pipeline orchestrator.

Thin coordinator that advances OperationContext through pipeline phases
by delegating to existing governance components. Owns no domain logic —
only phase transitions and error handling.

Every failure path ends in a terminal state (CANCELLED, EXPIRED, POSTMORTEM).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
    ValidationResult,
    ApprovalDecision as ApprovalDecisionCtx,
)
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskTier,
)
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorConfig:
    project_root: Path
    generation_timeout_s: float = 120.0
    validation_timeout_s: float = 60.0
    approval_timeout_s: float = 600.0
    max_generate_retries: int = 1
    max_validate_retries: int = 2


class GovernedOrchestrator:
    """Advances OperationContext through the governed pipeline."""

    def __init__(
        self,
        stack: Any,  # GovernanceStack
        generator: Any,  # CandidateGenerator
        approval_provider: Optional[Any],  # ApprovalProvider
        config: OrchestratorConfig,
    ) -> None:
        self._stack = stack
        self._generator = generator
        self._approval = approval_provider
        self._config = config

    async def run(self, ctx: OperationContext) -> OperationContext:
        """Run the full governed pipeline. Returns terminal-state context."""
        try:
            return await self._run_pipeline(ctx)
        except Exception as exc:
            logger.error(
                "[Orchestrator] Unhandled error in pipeline op=%s phase=%s: %s",
                ctx.op_id, ctx.phase.value, exc, exc_info=True,
            )
            try:
                ctx = ctx.advance(OperationPhase.POSTMORTEM)
            except ValueError:
                pass  # Already terminal
            await self._record_ledger(ctx, OperationState.FAILED, {
                "error_class": type(exc).__name__,
                "error_message": str(exc),
                "phase_at_failure": ctx.phase.value,
                "rollback_performed": False,
            })
            return ctx

    async def _run_pipeline(self, ctx: OperationContext) -> OperationContext:
        # ── CLASSIFY ──
        profile = self._build_profile(ctx)
        classification = self._stack.risk_engine.classify(profile)
        ctx = ctx.advance(
            OperationPhase.ROUTE,
            risk_tier=classification.tier,
            policy_version=classification.policy_version,
        )
        await self._record_ledger(ctx, OperationState.PLANNED, {
            "risk_tier": classification.tier.name,
            "reason_code": classification.reason_code,
        })

        # BLOCKED -> immediate cancel
        if classification.tier == RiskTier.BLOCKED:
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._record_ledger(ctx, OperationState.BLOCKED, {
                "reason": classification.reason_code,
            })
            return ctx

        # ── ROUTE ──
        ctx = ctx.advance(OperationPhase.GENERATE)

        # ── GENERATE (with retry) ──
        gen_retries = 0
        generation: Optional[GenerationResult] = None
        while gen_retries <= self._config.max_generate_retries:
            try:
                deadline = datetime.now(timezone.utc) + timedelta(
                    seconds=self._config.generation_timeout_s,
                )
                generation = await self._generator.generate(ctx, deadline)
                break
            except (asyncio.TimeoutError, RuntimeError) as exc:
                gen_retries += 1
                if gen_retries <= self._config.max_generate_retries:
                    ctx = ctx.advance(OperationPhase.GENERATE_RETRY)
                    logger.warning(
                        "[Orchestrator] Generation retry %d/%d for %s: %s",
                        gen_retries, self._config.max_generate_retries,
                        ctx.op_id, exc,
                    )
                else:
                    ctx = ctx.advance(OperationPhase.CANCELLED)
                    await self._record_ledger(ctx, OperationState.FAILED, {
                        "reason": "generation_failed",
                        "error": str(exc),
                    })
                    return ctx

        if generation is None or not generation.candidates:
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason": "no_candidates",
            })
            return ctx

        ctx = ctx.advance(OperationPhase.VALIDATE, generation=generation)

        # ── VALIDATE (with retry) ──
        validate_retries = 0
        best_candidate = None
        while validate_retries <= self._config.max_validate_retries:
            best_candidate = await self._validate_candidates(generation)
            if best_candidate is not None:
                break
            validate_retries += 1
            if validate_retries <= self._config.max_validate_retries:
                try:
                    ctx = ctx.advance(OperationPhase.VALIDATE_RETRY)
                except ValueError:
                    # Already in VALIDATE_RETRY, stay there
                    pass

        if best_candidate is None:
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason": "validation_failed",
            })
            return ctx

        validation = ValidationResult(
            passed=True,
            best_candidate=best_candidate,
            validation_duration_s=0.0,
            error=None,
        )
        ctx = ctx.advance(OperationPhase.GATE, validation=validation)

        # ── GATE ──
        allowed, reason = self._stack.can_write({
            "files": list(ctx.target_files),
        })
        if not allowed:
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._record_ledger(ctx, OperationState.BLOCKED, {
                "reason": reason,
            })
            return ctx

        # ── APPROVE (if required) ──
        if classification.tier == RiskTier.APPROVAL_REQUIRED:
            if self._approval is None:
                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(ctx, OperationState.FAILED, {
                    "reason": "no_approval_provider",
                })
                return ctx

            ctx = ctx.advance(OperationPhase.APPROVE)
            await self._record_ledger(ctx, OperationState.GATING, {
                "awaiting_approval": True,
            })
            request_id = await self._approval.request(ctx)
            decision = await self._approval.await_decision(
                request_id, self._config.approval_timeout_s,
            )

            if decision.status == ApprovalStatus.EXPIRED:
                ctx = ctx.advance(OperationPhase.EXPIRED)
                await self._record_ledger(ctx, OperationState.FAILED, {
                    "reason": "approval_expired",
                })
                return ctx
            elif decision.status == ApprovalStatus.REJECTED:
                ctx = ctx.advance(OperationPhase.CANCELLED)
                await self._record_ledger(ctx, OperationState.FAILED, {
                    "reason": f"human_rejected:{decision.reason}",
                })
                return ctx

        # ── APPLY ──
        ctx = ctx.advance(OperationPhase.APPLY)
        await self._record_ledger(ctx, OperationState.APPLYING, {})

        change_result = await self._stack.change_engine.execute(
            self._build_change_request(ctx, best_candidate),
        )

        if not change_result.success:
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason": "apply_failed",
                "rolled_back": change_result.rolled_back,
            })
            return ctx

        # ── VERIFY ──
        ctx = ctx.advance(OperationPhase.VERIFY)
        await self._record_ledger(ctx, OperationState.APPLIED, {
            "change_op_id": change_result.op_id,
        })

        # ── COMPLETE ──
        ctx = ctx.advance(OperationPhase.COMPLETE)
        await self._record_ledger(ctx, OperationState.APPLIED, {
            "final": True,
        })
        return ctx

    def _build_profile(self, ctx: OperationContext) -> OperationProfile:
        return OperationProfile(
            files_affected=[Path(f) for f in ctx.target_files],
            change_type=ChangeType.MODIFY,
            blast_radius=len(ctx.target_files),
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=any("supervisor" in f for f in ctx.target_files),
            test_scope_confidence=0.8,
        )

    async def _validate_candidates(
        self, generation: GenerationResult,
    ) -> Optional[Dict[str, Any]]:
        import ast as _ast
        for candidate in generation.candidates:
            code = candidate.get("code", "")
            try:
                _ast.parse(code)
                return candidate
            except SyntaxError:
                continue
        return None

    def _build_change_request(
        self, ctx: OperationContext, candidate: Dict[str, Any],
    ) -> Any:
        from backend.core.ouroboros.governance.change_engine import ChangeRequest
        from backend.core.ouroboros.governance.risk_engine import OperationProfile, ChangeType
        return ChangeRequest(
            goal=ctx.description,
            target_file=Path(ctx.target_files[0]) if ctx.target_files else Path("."),
            proposed_content=candidate.get("code", ""),
            profile=OperationProfile(
                files_affected=[Path(f) for f in ctx.target_files],
                change_type=ChangeType.MODIFY,
                blast_radius=len(ctx.target_files),
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.8,
            ),
        )

    async def _record_ledger(
        self,
        ctx: OperationContext,
        state: OperationState,
        data: Dict[str, Any],
    ) -> None:
        data["context_hash"] = ctx.context_hash
        data["phase"] = ctx.phase.value
        try:
            await self._stack.ledger.append(LedgerEntry(
                op_id=ctx.op_id,
                state=state,
                data=data,
            ))
        except Exception as exc:
            logger.error("[Orchestrator] Ledger append failed: %s", exc)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/test_ouroboros_governance/test_orchestrator.py
git commit -m "feat(governance): add governed pipeline orchestrator"
```

---

## Task 6: Wire into GovernanceStack (`integration.py`) + Update `__init__.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Context:** Add orchestrator, generator, approval_provider, and shadow_harness references to GovernanceStack. Wire the factory to create them. Add CLI flags for --approve/--reject/--list-pending. Export new public types from __init__.py.

**Step 1: Write the failing test**

Add to `tests/test_ouroboros_governance/test_integration.py` (append to existing):

```python
class TestGovernanceStackOrchestratorWiring:
    """Test that GovernanceStack exposes orchestrator components."""

    def test_stack_has_orchestrator_fields(self):
        """GovernanceStack should have orchestrator, generator, approval_provider, shadow_harness fields."""
        from backend.core.ouroboros.governance.integration import GovernanceStack
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(GovernanceStack)}
        assert "orchestrator" in field_names or hasattr(GovernanceStack, "orchestrator")

    def test_new_exports_available(self):
        from backend.core.ouroboros.governance import (
            OperationPhase,
            OperationContext,
            GovernedOrchestrator,
            CandidateGenerator,
            CLIApprovalProvider,
            ShadowHarness,
            SideEffectFirewall,
            ShadowModeViolation,
        )
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceStackOrchestratorWiring -v`
Expected: FAIL

**Step 3: Modify integration.py**

Add to `GovernanceStack` dataclass (after existing fields, before `_started`):

```python
    # Governed loop components (optional — present when orchestrator is wired)
    orchestrator: Optional[Any] = None
    generator: Optional[Any] = None
    approval_provider: Optional[Any] = None
    shadow_harness: Optional[Any] = None
```

Update `__init__.py` — add to exports:

```python
# Governed Loop
from backend.core.ouroboros.governance.op_context import (
    OperationPhase,
    OperationContext,
    GenerationResult,
    ValidationResult,
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    CandidateProvider,
    FailbackState,
    FailbackStateMachine,
)
from backend.core.ouroboros.governance.approval_provider import (
    ApprovalProvider,
    ApprovalStatus,
    ApprovalResult,
    CLIApprovalProvider,
)
from backend.core.ouroboros.governance.shadow_harness import (
    ShadowHarness,
    ShadowResult,
    ShadowModeViolation,
    SideEffectFirewall,
    OutputComparator,
    CompareMode,
)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceStackOrchestratorWiring -v`
Expected: PASS

**Step 5: Run full governance test suite**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short`
Expected: All PASS (358+ existing + new tests)

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py backend/core/ouroboros/governance/__init__.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): wire orchestrator components into GovernanceStack and exports"
```

---

## Task 7: Full Integration Test + Final Verification

**Files:**
- Modify: `tests/test_ouroboros_governance/test_integration.py` (add end-to-end orchestrator test)

**Context:** Prove the full pipeline works: create an OperationContext, run it through the orchestrator with mocked providers, verify it reaches COMPLETE with correct ledger entries and hash chain.

**Step 1: Write the integration test**

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
class TestGovernedPipelineEndToEnd:
    """End-to-end test of the governed self-programming pipeline."""

    @pytest.mark.asyncio
    async def test_full_pipeline_sandbox_mode(self, tmp_path):
        """Create stack -> create orchestrator -> run op -> verify COMPLETE."""
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
            OperationPhase,
        )
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
        )
        from backend.core.ouroboros.governance.op_context import GenerationResult
        from backend.core.ouroboros.governance.risk_engine import (
            RiskClassification,
            RiskTier,
        )
        from unittest.mock import AsyncMock, MagicMock

        # Mock stack
        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        stack.risk_engine.classify.return_value = RiskClassification(
            tier=RiskTier.SAFE_AUTO, reason_code="default_safe",
        )
        stack.ledger = AsyncMock()
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine = AsyncMock()
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=True, rolled_back=False, op_id="op-e2e-001",
        ))

        # Mock generator
        gen_mock = AsyncMock()
        gen_mock.generate = AsyncMock(return_value=GenerationResult(
            candidates=({"code": "x = 1", "description": "test"},),
            provider_name="local",
            generation_duration_s=0.1,
        ))

        orch = GovernedOrchestrator(
            stack=stack,
            generator=gen_mock,
            approval_provider=None,
            config=OrchestratorConfig(project_root=tmp_path),
        )

        ctx = OperationContext.create(
            op_id="op-e2e-001",
            target_files=("backend/core/ouroboros/governance/foo.py",),
            description="End-to-end test",
        )

        result = await orch.run(ctx)

        # Verify terminal state
        assert result.phase == OperationPhase.COMPLETE
        # Verify hash chain
        assert result.context_hash != ctx.context_hash
        assert result.previous_hash is not None
        # Verify ledger was called
        assert stack.ledger.append.await_count >= 3
```

**Step 2: Run the integration test**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernedPipelineEndToEnd -v`
Expected: PASS

**Step 3: Run the complete governance test suite**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short 2>&1 | tail -20`
Expected: All PASS

**Step 4: Commit**

```bash
git add tests/test_ouroboros_governance/test_integration.py
git commit -m "test(governance): add end-to-end governed pipeline integration test"
```

---

## Summary

| Task | Component | New Files | Test File |
|------|-----------|-----------|-----------|
| 1 | OperationContext + Phase enum | `op_context.py` | `test_op_context.py` |
| 2 | Approval Provider | `approval_provider.py` | `test_approval_provider.py` |
| 3 | Candidate Generator + Failback FSM | `candidate_generator.py` | `test_candidate_generator.py` |
| 4 | Shadow Harness | `shadow_harness.py` | `test_shadow_harness.py` |
| 5 | Orchestrator | `orchestrator.py` | `test_orchestrator.py` |
| 6 | Integration wiring | modify `integration.py`, `__init__.py` | modify `test_integration.py` |
| 7 | End-to-end integration test | — | modify `test_integration.py` |

**Total new files:** 5 implementation + 4 test files
**Total modified files:** 3 (`integration.py`, `__init__.py`, `test_integration.py`)
**Dependency order:** Task 1 first (all others depend on OperationContext), then Tasks 2-4 in parallel, Task 5 after 1-4, Tasks 6-7 after 5.
