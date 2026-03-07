# Phase A: Formal Contracts Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build 4 typed contracts (DecisionEnvelope, PolicyGate, ActionCommitLedger, BehavioralHealthMonitor) + 2 thin adapter protocols that bridge reasoning output to committed actions with full traceability, idempotency, and anomaly detection.

**Architecture:** Core contracts live in `backend/core/contracts/` (system-wide protocols + value types). Triage-specific implementations live in `backend/autonomy/contracts/`. All value types are frozen dataclasses. Ledger uses SQLite WAL. All thresholds are env-var configurable.

**Tech Stack:** Python 3.9 stdlib only (dataclasses, enum, sqlite3, asyncio, hashlib, uuid, typing). No new dependencies.

---

### Task 1: Create package structure and DecisionEnvelope enums

**Files:**
- Create: `backend/core/contracts/__init__.py`
- Create: `backend/core/contracts/decision_envelope.py`
- Create: `backend/autonomy/contracts/__init__.py`
- Test: `tests/unit/core/contracts/test_decision_envelope.py`
- Create: `tests/unit/core/contracts/__init__.py`

**Step 1: Write the failing tests**

Create `tests/unit/core/contracts/__init__.py` (empty) and `tests/unit/core/contracts/test_decision_envelope.py`:

```python
"""Tests for DecisionEnvelope, enums, and IdempotencyKey."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestDecisionType:
    def test_all_members_exist(self):
        from core.contracts.decision_envelope import DecisionType
        assert DecisionType.EXTRACTION == "extraction"
        assert DecisionType.SCORING == "scoring"
        assert DecisionType.POLICY == "policy"
        assert DecisionType.ACTION == "action"

    def test_is_str_enum(self):
        from core.contracts.decision_envelope import DecisionType
        assert isinstance(DecisionType.EXTRACTION, str)


class TestDecisionSource:
    def test_all_members_exist(self):
        from core.contracts.decision_envelope import DecisionSource
        assert DecisionSource.JPRIME_V1 == "jprime_v1"
        assert DecisionSource.JPRIME_DEGRADED == "jprime_degraded_fallback"
        assert DecisionSource.HEURISTIC == "heuristic"
        assert DecisionSource.CLOUD_CLAUDE == "cloud_claude"
        assert DecisionSource.LOCAL_PRIME == "local_prime"
        assert DecisionSource.ADAPTIVE == "adaptive"


class TestOriginComponent:
    def test_all_members_exist(self):
        from core.contracts.decision_envelope import OriginComponent
        assert OriginComponent.EMAIL_TRIAGE_RUNNER == "email_triage.runner"
        assert OriginComponent.EMAIL_TRIAGE_EXTRACTION == "email_triage.extraction"
        assert OriginComponent.EMAIL_TRIAGE_SCORING == "email_triage.scoring"
        assert OriginComponent.EMAIL_TRIAGE_POLICY == "email_triage.policy"
        assert OriginComponent.EMAIL_TRIAGE_LABELER == "email_triage.labels"
        assert OriginComponent.EMAIL_TRIAGE_NOTIFIER == "email_triage.notifications"


class TestDecisionEnvelope:
    def _make_envelope(self, **overrides):
        from core.contracts.decision_envelope import (
            DecisionEnvelope, DecisionType, DecisionSource, OriginComponent,
        )
        defaults = dict(
            envelope_id="env-1",
            trace_id="trace-1",
            parent_envelope_id=None,
            decision_type=DecisionType.EXTRACTION,
            source=DecisionSource.HEURISTIC,
            origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
            payload={"key": "value"},
            confidence=0.95,
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
            causal_seq=1,
            config_version="v1",
        )
        defaults.update(overrides)
        return DecisionEnvelope(**defaults)

    def test_frozen(self):
        env = self._make_envelope()
        with pytest.raises(AttributeError):
            env.confidence = 0.5  # type: ignore[misc]

    def test_dual_timestamps(self):
        before_epoch = time.time()
        before_mono = time.monotonic()
        env = self._make_envelope(
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
        assert env.created_at_epoch >= before_epoch
        assert env.created_at_monotonic >= before_mono

    def test_schema_version_defaults(self):
        env = self._make_envelope()
        assert env.schema_version == 1
        assert env.producer_version == "1.0.0"
        assert env.compat_min_version == 1

    def test_metadata_default_empty(self):
        env = self._make_envelope()
        assert env.metadata == {}

    def test_causal_chaining(self):
        parent = self._make_envelope(envelope_id="parent-1", causal_seq=1)
        child = self._make_envelope(
            envelope_id="child-1",
            parent_envelope_id="parent-1",
            causal_seq=2,
        )
        assert child.parent_envelope_id == parent.envelope_id
        assert child.causal_seq > parent.causal_seq

    def test_typed_enums_not_strings(self):
        from core.contracts.decision_envelope import DecisionType
        env = self._make_envelope()
        assert isinstance(env.decision_type, DecisionType)
        # But also works as string comparison
        assert env.decision_type == "extraction"


class TestIdempotencyKey:
    def test_deterministic(self):
        from core.contracts.decision_envelope import IdempotencyKey, DecisionType
        k1 = IdempotencyKey.build(DecisionType.ACTION, "msg-1", "apply_label", "v1")
        k2 = IdempotencyKey.build(DecisionType.ACTION, "msg-1", "apply_label", "v1")
        assert k1.key == k2.key

    def test_different_inputs_different_keys(self):
        from core.contracts.decision_envelope import IdempotencyKey, DecisionType
        k1 = IdempotencyKey.build(DecisionType.ACTION, "msg-1", "apply_label", "v1")
        k2 = IdempotencyKey.build(DecisionType.ACTION, "msg-2", "apply_label", "v1")
        assert k1.key != k2.key

    def test_key_length(self):
        from core.contracts.decision_envelope import IdempotencyKey, DecisionType
        k = IdempotencyKey.build(DecisionType.SCORING, "msg-1", "score", "v1")
        assert len(k.key) == 32

    def test_frozen(self):
        from core.contracts.decision_envelope import IdempotencyKey, DecisionType
        k = IdempotencyKey.build(DecisionType.ACTION, "msg-1", "apply_label", "v1")
        with pytest.raises(AttributeError):
            k.key = "tampered"  # type: ignore[misc]
```

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_decision_envelope.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.contracts'`

**Step 3: Write the implementation**

Create `backend/core/contracts/__init__.py`:
```python
"""Core contracts for the JARVIS autonomous pipeline."""
```

Create `backend/core/contracts/decision_envelope.py`:
```python
"""DecisionEnvelope — typed wrapper for autonomous reasoning outputs.

Every stage in the autonomous pipeline (extraction, scoring, policy, action)
wraps its output in a DecisionEnvelope. Envelopes chain causally via
parent_envelope_id and causal_seq (LamportClock ticks).

All identity fields use typed enums, not free strings.
Dual timestamps (epoch + monotonic) separate human display from execution semantics.
Schema versioning enables forward/backward compatibility across repos.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Enums (strict, not free strings)
# ---------------------------------------------------------------------------

class DecisionType(str, Enum):
    """What stage of the pipeline produced this decision."""
    EXTRACTION = "extraction"
    SCORING = "scoring"
    POLICY = "policy"
    ACTION = "action"


class DecisionSource(str, Enum):
    """Which reasoning backend produced this decision."""
    JPRIME_V1 = "jprime_v1"
    JPRIME_DEGRADED = "jprime_degraded_fallback"
    HEURISTIC = "heuristic"
    CLOUD_CLAUDE = "cloud_claude"
    LOCAL_PRIME = "local_prime"
    ADAPTIVE = "adaptive"


class OriginComponent(str, Enum):
    """Which module in the codebase produced this decision."""
    EMAIL_TRIAGE_RUNNER = "email_triage.runner"
    EMAIL_TRIAGE_EXTRACTION = "email_triage.extraction"
    EMAIL_TRIAGE_SCORING = "email_triage.scoring"
    EMAIL_TRIAGE_POLICY = "email_triage.policy"
    EMAIL_TRIAGE_LABELER = "email_triage.labels"
    EMAIL_TRIAGE_NOTIFIER = "email_triage.notifications"


# ---------------------------------------------------------------------------
# DecisionEnvelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionEnvelope:
    """Typed wrapper for any autonomous pipeline decision.

    Frozen for immutability. Chain envelopes via parent_envelope_id.
    Use EnvelopeFactory.create() for convenience.
    """
    envelope_id: str
    trace_id: str
    parent_envelope_id: Optional[str]
    decision_type: DecisionType
    source: DecisionSource
    origin_component: OriginComponent

    payload: Dict[str, Any]
    confidence: float

    created_at_epoch: float
    created_at_monotonic: float

    causal_seq: int

    config_version: str
    schema_version: int = 1
    producer_version: str = "1.0.0"
    compat_min_version: int = 1

    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# IdempotencyKey
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdempotencyKey:
    """Canonical deterministic idempotency key.

    Built from decision_type + target_id + action + config_version.
    Same inputs always produce the same key. Shared contract across repos.
    """
    key: str

    @classmethod
    def build(
        cls,
        decision_type: DecisionType,
        target_id: str,
        action: str,
        config_version: str,
    ) -> IdempotencyKey:
        raw = f"{decision_type.value}:{target_id}:{action}:{config_version}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return cls(key=digest)


# ---------------------------------------------------------------------------
# EnvelopeFactory (convenience)
# ---------------------------------------------------------------------------

class EnvelopeFactory:
    """Creates DecisionEnvelopes with auto-generated IDs and timestamps.

    Accepts an optional LamportClock for causal sequencing. If none is
    provided, causal_seq defaults to 0.
    """

    def __init__(self, clock=None):
        self._clock = clock

    def create(
        self,
        trace_id: str,
        decision_type: DecisionType,
        source: DecisionSource,
        origin_component: OriginComponent,
        payload: Dict[str, Any],
        confidence: float,
        config_version: str,
        parent_envelope_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DecisionEnvelope:
        seq = self._clock.tick() if self._clock is not None else 0
        return DecisionEnvelope(
            envelope_id=str(uuid.uuid4()),
            trace_id=trace_id,
            parent_envelope_id=parent_envelope_id,
            decision_type=decision_type,
            source=source,
            origin_component=origin_component,
            payload=payload,
            confidence=confidence,
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
            causal_seq=seq,
            config_version=config_version,
            metadata=metadata or {},
        )
```

Create `backend/autonomy/contracts/__init__.py`:
```python
"""Autonomy-specific contract implementations."""
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_decision_envelope.py -v`
Expected: All 13 tests PASS

**Step 5: Commit**

```bash
git add backend/core/contracts/ backend/autonomy/contracts/__init__.py tests/unit/core/contracts/
git commit -m "feat(contracts): add DecisionEnvelope with typed enums and IdempotencyKey builder"
```

---

### Task 2: PolicyGate protocol and PolicyVerdict

**Files:**
- Create: `backend/core/contracts/policy_gate.py`
- Test: `tests/unit/core/contracts/test_policy_gate.py`

**Step 1: Write the failing tests**

Create `tests/unit/core/contracts/test_policy_gate.py`:

```python
"""Tests for PolicyGate protocol and PolicyVerdict."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestVerdictAction:
    def test_all_members_exist(self):
        from core.contracts.policy_gate import VerdictAction
        assert VerdictAction.ALLOW == "allow"
        assert VerdictAction.DENY == "deny"
        assert VerdictAction.DEFER == "defer"

    def test_is_str_enum(self):
        from core.contracts.policy_gate import VerdictAction
        assert isinstance(VerdictAction.ALLOW, str)


class TestPolicyVerdict:
    def test_frozen(self):
        from core.contracts.policy_gate import PolicyVerdict, VerdictAction
        v = PolicyVerdict(
            allowed=True,
            action=VerdictAction.ALLOW,
            reason="budget available",
            conditions=("not_quiet_hours",),
            envelope_id="env-1",
            gate_name="triage_policy",
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
        with pytest.raises(AttributeError):
            v.allowed = False  # type: ignore[misc]

    def test_deny_verdict(self):
        from core.contracts.policy_gate import PolicyVerdict, VerdictAction
        v = PolicyVerdict(
            allowed=False,
            action=VerdictAction.DENY,
            reason="quiet hours",
            conditions=(),
            envelope_id="env-1",
            gate_name="triage_policy",
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
        assert not v.allowed
        assert v.action == VerdictAction.DENY

    def test_defer_verdict(self):
        from core.contracts.policy_gate import PolicyVerdict, VerdictAction
        v = PolicyVerdict(
            allowed=False,
            action=VerdictAction.DEFER,
            reason="budget exhausted, queue for summary",
            conditions=("summary_window_open",),
            envelope_id="env-1",
            gate_name="triage_policy",
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
        assert not v.allowed
        assert v.action == VerdictAction.DEFER
        assert "summary_window_open" in v.conditions


class TestPolicyGateProtocol:
    @pytest.mark.asyncio
    async def test_protocol_conformance(self):
        """A class implementing evaluate() should satisfy PolicyGate."""
        from core.contracts.policy_gate import PolicyGate, PolicyVerdict, VerdictAction
        from core.contracts.decision_envelope import (
            DecisionEnvelope, DecisionType, DecisionSource, OriginComponent,
        )

        class MockGate:
            async def evaluate(self, envelope, context):
                return PolicyVerdict(
                    allowed=True,
                    action=VerdictAction.ALLOW,
                    reason="test",
                    conditions=(),
                    envelope_id=envelope.envelope_id,
                    gate_name="mock",
                    created_at_epoch=time.time(),
                    created_at_monotonic=time.monotonic(),
                )

        gate = MockGate()
        assert isinstance(gate, PolicyGate)

        env = DecisionEnvelope(
            envelope_id="env-1", trace_id="t-1", parent_envelope_id=None,
            decision_type=DecisionType.SCORING, source=DecisionSource.HEURISTIC,
            origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
            payload={"score": 85}, confidence=1.0,
            created_at_epoch=time.time(), created_at_monotonic=time.monotonic(),
            causal_seq=1, config_version="v1",
        )
        verdict = await gate.evaluate(env, {})
        assert verdict.allowed is True

    @pytest.mark.asyncio
    async def test_non_conforming_class_fails(self):
        from core.contracts.policy_gate import PolicyGate

        class NotAGate:
            def wrong_method(self):
                pass

        assert not isinstance(NotAGate(), PolicyGate)
```

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_policy_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.contracts.policy_gate'`

**Step 3: Write the implementation**

Create `backend/core/contracts/policy_gate.py`:

```python
"""PolicyGate — async protocol for gating autonomous actions.

Every proposed action must pass through a PolicyGate before execution.
The gate evaluates a DecisionEnvelope against runtime context and returns
a PolicyVerdict (ALLOW / DENY / DEFER).

The protocol is async because real policy checks may need to hit
stores, quotas, or lease managers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Protocol, Tuple, runtime_checkable

from core.contracts.decision_envelope import DecisionEnvelope


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class VerdictAction(str, Enum):
    """What the gate decided."""
    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"


# ---------------------------------------------------------------------------
# PolicyVerdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyVerdict:
    """Immutable result of a PolicyGate evaluation."""
    allowed: bool
    action: VerdictAction
    reason: str
    conditions: Tuple[str, ...]
    envelope_id: str
    gate_name: str
    created_at_epoch: float
    created_at_monotonic: float


# ---------------------------------------------------------------------------
# PolicyGate Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class PolicyGate(Protocol):
    """Async gate that decides whether a proposed action should execute."""

    async def evaluate(
        self, envelope: DecisionEnvelope, context: Dict[str, Any]
    ) -> PolicyVerdict: ...
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_policy_gate.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add backend/core/contracts/policy_gate.py tests/unit/core/contracts/test_policy_gate.py
git commit -m "feat(contracts): add async PolicyGate protocol and PolicyVerdict"
```

---

### Task 3: ActionCommitLedger with SQLite WAL state machine

**Files:**
- Create: `backend/core/contracts/action_commit_ledger.py`
- Test: `tests/unit/core/contracts/test_action_commit_ledger.py`

**Step 1: Write the failing tests**

Create `tests/unit/core/contracts/test_action_commit_ledger.py`:

```python
"""Tests for ActionCommitLedger state machine and SQLite WAL backend."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest

from core.contracts.decision_envelope import (
    DecisionEnvelope, DecisionType, DecisionSource, IdempotencyKey,
    OriginComponent,
)


def _make_envelope(envelope_id="env-1", trace_id="trace-1", decision_type=DecisionType.ACTION):
    return DecisionEnvelope(
        envelope_id=envelope_id, trace_id=trace_id, parent_envelope_id=None,
        decision_type=decision_type, source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_RUNNER,
        payload={"message_id": "msg-1"}, confidence=0.9,
        created_at_epoch=time.time(), created_at_monotonic=time.monotonic(),
        causal_seq=1, config_version="v1",
    )


def _make_idem_key(target_id="msg-1"):
    return IdempotencyKey.build(DecisionType.ACTION, target_id, "apply_label", "v1")


@pytest.fixture
async def ledger(tmp_path):
    from core.contracts.action_commit_ledger import ActionCommitLedger
    db_path = tmp_path / "test_ledger.db"
    lg = ActionCommitLedger(db_path)
    await lg.start()
    yield lg
    await lg.stop()


class TestReserveCommitAbort:
    @pytest.mark.asyncio
    async def test_reserve_returns_commit_id(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        commit_id = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        assert isinstance(commit_id, str)
        assert len(commit_id) > 0

    @pytest.mark.asyncio
    async def test_commit_transitions_state(self, ledger):
        from core.contracts.action_commit_ledger import CommitState
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.commit(cid, outcome="success")
        records = await ledger.query(since_epoch=0)
        assert len(records) == 1
        assert records[0].state == CommitState.COMMITTED
        assert records[0].outcome == "success"

    @pytest.mark.asyncio
    async def test_abort_transitions_state(self, ledger):
        from core.contracts.action_commit_ledger import CommitState
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.abort(cid, reason="label_api_failed")
        records = await ledger.query(since_epoch=0)
        assert len(records) == 1
        assert records[0].state == CommitState.ABORTED
        assert records[0].abort_reason == "label_api_failed"

    @pytest.mark.asyncio
    async def test_commit_already_committed_raises(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.commit(cid, outcome="success")
        with pytest.raises(ValueError, match="not in RESERVED state"):
            await ledger.commit(cid, outcome="success")

    @pytest.mark.asyncio
    async def test_abort_already_committed_raises(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.commit(cid, outcome="success")
        with pytest.raises(ValueError, match="not in RESERVED state"):
            await ledger.abort(cid, reason="too late")


class TestDuplicateDetection:
    @pytest.mark.asyncio
    async def test_is_duplicate_after_commit(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.commit(cid, outcome="success")
        assert await ledger.is_duplicate(idem) is True

    @pytest.mark.asyncio
    async def test_not_duplicate_when_only_reserved(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        # Reserved but not committed is NOT a duplicate
        assert await ledger.is_duplicate(idem) is False

    @pytest.mark.asyncio
    async def test_not_duplicate_after_abort(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.abort(cid, reason="test")
        assert await ledger.is_duplicate(idem) is False


class TestLeaseExpiry:
    @pytest.mark.asyncio
    async def test_expire_stale_transitions(self, ledger):
        from core.contracts.action_commit_ledger import CommitState
        env = _make_envelope()
        idem = _make_idem_key()
        # Reserve with a lease that expires immediately
        await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=0.0,  # expires instantly
        )
        count = await ledger.expire_stale()
        assert count == 1
        records = await ledger.query(since_epoch=0)
        assert records[0].state == CommitState.EXPIRED


class TestPreExecInvariants:
    @pytest.mark.asyncio
    async def test_valid_invariants_pass(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        ok, reason = await ledger.check_pre_exec_invariants(cid, current_fencing_token=1)
        assert ok is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_wrong_fencing_token_fails(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        ok, reason = await ledger.check_pre_exec_invariants(cid, current_fencing_token=99)
        assert ok is False
        assert "fencing" in reason.lower()

    @pytest.mark.asyncio
    async def test_expired_lease_fails(self, ledger):
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=0.0,  # already expired
        )
        ok, reason = await ledger.check_pre_exec_invariants(cid, current_fencing_token=1)
        assert ok is False
        assert "expired" in reason.lower()

    @pytest.mark.asyncio
    async def test_already_committed_duplicate_fails(self, ledger):
        env1 = _make_envelope(envelope_id="env-1")
        idem = _make_idem_key()
        cid1 = await ledger.reserve(
            envelope=env1, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.commit(cid1, outcome="success")

        # New reserve with same idempotency key (retry scenario)
        env2 = _make_envelope(envelope_id="env-2")
        idem2 = _make_idem_key()  # same key
        cid2 = await ledger.reserve(
            envelope=env2, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="runner-1", session_id="cycle-2",
            idempotency_key=idem2, lease_duration_s=30.0,
        )
        ok, reason = await ledger.check_pre_exec_invariants(cid2, current_fencing_token=1)
        assert ok is False
        assert "duplicate" in reason.lower()


class TestQueryFiltering:
    @pytest.mark.asyncio
    async def test_query_by_decision_type(self, ledger):
        env1 = _make_envelope(envelope_id="env-1", decision_type=DecisionType.ACTION)
        env2 = _make_envelope(envelope_id="env-2", decision_type=DecisionType.SCORING)
        idem1 = IdempotencyKey.build(DecisionType.ACTION, "msg-1", "label", "v1")
        idem2 = IdempotencyKey.build(DecisionType.SCORING, "msg-2", "score", "v1")
        await ledger.reserve(
            envelope=env1, action="label", target_id="msg-1",
            fencing_token=1, lock_owner="r1", session_id="c1",
            idempotency_key=idem1, lease_duration_s=30.0,
        )
        await ledger.reserve(
            envelope=env2, action="score", target_id="msg-2",
            fencing_token=1, lock_owner="r1", session_id="c1",
            idempotency_key=idem2, lease_duration_s=30.0,
        )
        results = await ledger.query(since_epoch=0, decision_type=DecisionType.ACTION)
        assert len(results) == 1
        assert results[0].decision_type == DecisionType.ACTION

    @pytest.mark.asyncio
    async def test_query_by_state(self, ledger):
        from core.contracts.action_commit_ledger import CommitState
        env = _make_envelope()
        idem = _make_idem_key()
        cid = await ledger.reserve(
            envelope=env, action="apply_label", target_id="msg-1",
            fencing_token=1, lock_owner="r1", session_id="c1",
            idempotency_key=idem, lease_duration_s=30.0,
        )
        await ledger.commit(cid, outcome="success")
        results = await ledger.query(since_epoch=0, state=CommitState.COMMITTED)
        assert len(results) == 1
        results = await ledger.query(since_epoch=0, state=CommitState.RESERVED)
        assert len(results) == 0
```

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_action_commit_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.contracts.action_commit_ledger'`

**Step 3: Write the implementation**

Create `backend/core/contracts/action_commit_ledger.py`:

```python
"""ActionCommitLedger — durable append-only record of committed actions.

Every action that passes a PolicyGate gets recorded here BEFORE execution.
After execution, the outcome is recorded. This gives:
- Full audit trail
- Idempotency (check before re-executing)
- Crash recovery (replay uncommitted)
- Behavioral health input (pattern detection)

State machine: RESERVED -> COMMITTED | ABORTED | EXPIRED
Transitions are atomic (SQLite transaction).

Storage: SQLite WAL (matches DedupLedger and TriageStateStore patterns).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.contracts.decision_envelope import DecisionEnvelope, DecisionType, IdempotencyKey


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CommitState(str, Enum):
    """Explicit state machine for commit records."""
    RESERVED = "reserved"
    COMMITTED = "committed"
    ABORTED = "aborted"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# CommitRecord
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommitRecord:
    """Immutable record of a committed (or pending) action."""
    commit_id: str
    idempotency_key: str
    envelope_id: str
    trace_id: str
    decision_type: DecisionType
    action: str
    target_id: str

    fencing_token: int
    lock_owner: str
    session_id: str
    expires_at_monotonic: float

    state: CommitState
    reserved_at_epoch: float
    committed_at_epoch: Optional[float]
    outcome: Optional[str]
    abort_reason: Optional[str]
    metadata: Dict[str, Any]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS action_commits (
    commit_id           TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL,
    envelope_id         TEXT NOT NULL,
    trace_id            TEXT NOT NULL,
    decision_type       TEXT NOT NULL,
    action              TEXT NOT NULL,
    target_id           TEXT NOT NULL,
    fencing_token       INTEGER NOT NULL,
    lock_owner          TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    expires_at_monotonic REAL NOT NULL,
    state               TEXT NOT NULL DEFAULT 'reserved',
    reserved_at_epoch   REAL NOT NULL,
    committed_at_epoch  REAL,
    outcome             TEXT,
    abort_reason        TEXT,
    metadata            TEXT NOT NULL DEFAULT '{}'
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ac_idempotency ON action_commits (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS idx_ac_state ON action_commits (state)",
    "CREATE INDEX IF NOT EXISTS idx_ac_trace ON action_commits (trace_id)",
]

_INSERT = """\
INSERT INTO action_commits
    (commit_id, idempotency_key, envelope_id, trace_id, decision_type,
     action, target_id, fencing_token, lock_owner, session_id,
     expires_at_monotonic, state, reserved_at_epoch, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
"""

_UPDATE_COMMIT = """\
UPDATE action_commits
SET state = 'committed', committed_at_epoch = ?, outcome = ?, metadata = ?
WHERE commit_id = ? AND state = 'reserved'
"""

_UPDATE_ABORT = """\
UPDATE action_commits
SET state = 'aborted', abort_reason = ?
WHERE commit_id = ? AND state = 'reserved'
"""

_UPDATE_EXPIRE = """\
UPDATE action_commits
SET state = 'expired'
WHERE state = 'reserved' AND expires_at_monotonic <= ?
"""

_SELECT_BY_ID = """\
SELECT commit_id, idempotency_key, envelope_id, trace_id, decision_type,
       action, target_id, fencing_token, lock_owner, session_id,
       expires_at_monotonic, state, reserved_at_epoch, committed_at_epoch,
       outcome, abort_reason, metadata
FROM action_commits WHERE commit_id = ?
"""

_SELECT_COMMITTED_BY_KEY = """\
SELECT 1 FROM action_commits
WHERE idempotency_key = ? AND state = 'committed' LIMIT 1
"""

_SELECT_QUERY = """\
SELECT commit_id, idempotency_key, envelope_id, trace_id, decision_type,
       action, target_id, fencing_token, lock_owner, session_id,
       expires_at_monotonic, state, reserved_at_epoch, committed_at_epoch,
       outcome, abort_reason, metadata
FROM action_commits
WHERE reserved_at_epoch >= ?
"""


# ---------------------------------------------------------------------------
# ActionCommitLedger
# ---------------------------------------------------------------------------

class ActionCommitLedger:
    """Durable append-only ledger backed by SQLite WAL."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    async def start(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_TABLE)
        for idx in _CREATE_INDEXES:
            self._conn.execute(idx)
        self._conn.commit()

    async def stop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _row_to_record(self, row) -> CommitRecord:
        return CommitRecord(
            commit_id=row[0],
            idempotency_key=row[1],
            envelope_id=row[2],
            trace_id=row[3],
            decision_type=DecisionType(row[4]),
            action=row[5],
            target_id=row[6],
            fencing_token=row[7],
            lock_owner=row[8],
            session_id=row[9],
            expires_at_monotonic=row[10],
            state=CommitState(row[11]),
            reserved_at_epoch=row[12],
            committed_at_epoch=row[13],
            outcome=row[14],
            abort_reason=row[15],
            metadata=json.loads(row[16]) if row[16] else {},
        )

    async def reserve(
        self,
        envelope: DecisionEnvelope,
        action: str,
        target_id: str,
        fencing_token: int,
        lock_owner: str,
        session_id: str,
        idempotency_key: IdempotencyKey,
        lease_duration_s: float,
    ) -> str:
        assert self._conn is not None, "Ledger not started"
        commit_id = str(uuid.uuid4())
        now_epoch = time.time()
        now_mono = time.monotonic()
        expires = now_mono + lease_duration_s

        async with self._lock:
            self._conn.execute(_INSERT, (
                commit_id, idempotency_key.key, envelope.envelope_id,
                envelope.trace_id, envelope.decision_type.value,
                action, target_id, fencing_token, lock_owner, session_id,
                expires, now_epoch, json.dumps(envelope.metadata),
            ))
            self._conn.commit()
        return commit_id

    async def commit(
        self, commit_id: str, outcome: str, metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        assert self._conn is not None, "Ledger not started"
        now = time.time()
        meta_json = json.dumps(metadata or {})
        async with self._lock:
            cur = self._conn.execute(_UPDATE_COMMIT, (now, outcome, meta_json, commit_id))
            self._conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"Commit {commit_id} not in RESERVED state")

    async def abort(self, commit_id: str, reason: str) -> None:
        assert self._conn is not None, "Ledger not started"
        async with self._lock:
            cur = self._conn.execute(_UPDATE_ABORT, (reason, commit_id))
            self._conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"Commit {commit_id} not in RESERVED state")

    async def expire_stale(self) -> int:
        assert self._conn is not None, "Ledger not started"
        now = time.monotonic()
        async with self._lock:
            cur = self._conn.execute(_UPDATE_EXPIRE, (now,))
            self._conn.commit()
            return cur.rowcount

    async def is_duplicate(self, idempotency_key: IdempotencyKey) -> bool:
        assert self._conn is not None, "Ledger not started"
        cur = self._conn.execute(_SELECT_COMMITTED_BY_KEY, (idempotency_key.key,))
        return cur.fetchone() is not None

    async def check_pre_exec_invariants(
        self, commit_id: str, current_fencing_token: int,
    ) -> Tuple[bool, Optional[str]]:
        assert self._conn is not None, "Ledger not started"
        cur = self._conn.execute(_SELECT_BY_ID, (commit_id,))
        row = cur.fetchone()
        if row is None:
            return False, f"commit_id {commit_id} not found"
        record = self._row_to_record(row)

        if record.fencing_token != current_fencing_token:
            return False, (
                f"Fencing token mismatch: record={record.fencing_token}, "
                f"current={current_fencing_token}"
            )

        if time.monotonic() >= record.expires_at_monotonic:
            return False, f"Lease expired at {record.expires_at_monotonic}"

        # Check if same idempotency key already committed (by another record)
        dup_cur = self._conn.execute(
            "SELECT 1 FROM action_commits "
            "WHERE idempotency_key = ? AND state = 'committed' AND commit_id != ? LIMIT 1",
            (record.idempotency_key, commit_id),
        )
        if dup_cur.fetchone() is not None:
            return False, f"Duplicate: idempotency_key {record.idempotency_key} already committed"

        return True, None

    async def query(
        self,
        *,
        since_epoch: float = 0,
        decision_type: Optional[DecisionType] = None,
        state: Optional[CommitState] = None,
    ) -> List[CommitRecord]:
        assert self._conn is not None, "Ledger not started"
        sql = _SELECT_QUERY
        params: list = [since_epoch]
        if decision_type is not None:
            sql += " AND decision_type = ?"
            params.append(decision_type.value)
        if state is not None:
            sql += " AND state = ?"
            params.append(state.value)
        sql += " ORDER BY reserved_at_epoch ASC"
        cur = self._conn.execute(sql, params)
        return [self._row_to_record(row) for row in cur.fetchall()]
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_action_commit_ledger.py -v`
Expected: All 14 tests PASS

**Step 5: Commit**

```bash
git add backend/core/contracts/action_commit_ledger.py tests/unit/core/contracts/test_action_commit_ledger.py
git commit -m "feat(contracts): add ActionCommitLedger with SQLite WAL state machine"
```

---

### Task 4: BehavioralHealthMonitor with anomaly detection

**Files:**
- Create: `backend/autonomy/contracts/behavioral_health.py`
- Test: `tests/unit/autonomy/contracts/test_behavioral_health.py`
- Create: `tests/unit/autonomy/contracts/__init__.py`

**Step 1: Write the failing tests**

Create `tests/unit/autonomy/contracts/__init__.py` (empty) and `tests/unit/autonomy/contracts/test_behavioral_health.py`:

```python
"""Tests for BehavioralHealthMonitor anomaly detection."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest

from core.contracts.decision_envelope import (
    DecisionEnvelope, DecisionType, DecisionSource, OriginComponent,
)


def _make_envelope(confidence=0.9):
    return DecisionEnvelope(
        envelope_id="env-1", trace_id="trace-1", parent_envelope_id=None,
        decision_type=DecisionType.EXTRACTION, source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
        payload={}, confidence=confidence,
        created_at_epoch=time.time(), created_at_monotonic=time.monotonic(),
        causal_seq=1, config_version="v1",
    )


def _make_report(emails_processed=5, tier_counts=None, errors=None):
    """Build a minimal TriageCycleReport-like object for testing."""
    from autonomy.email_triage.schemas import TriageCycleReport
    return TriageCycleReport(
        cycle_id="cycle-1", started_at=time.time(), completed_at=time.time(),
        emails_fetched=emails_processed, emails_processed=emails_processed,
        tier_counts=tier_counts or {1: 1, 2: 1, 3: 2, 4: 1},
        notifications_sent=1, notifications_suppressed=0,
        errors=errors or [],
    )


class TestHealthyBaseline:
    def test_initial_health_is_healthy(self):
        from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
        monitor = BehavioralHealthMonitor()
        report = monitor.check_health()
        assert report.healthy is True
        assert len(report.anomalies) == 0

    def test_normal_cycles_stay_healthy(self):
        from autonomy.contracts.behavioral_health import (
            BehavioralHealthMonitor, ThrottleRecommendation,
        )
        monitor = BehavioralHealthMonitor()
        for _ in range(5):
            monitor.record_cycle(_make_report(), [_make_envelope()])
        report = monitor.check_health()
        assert report.healthy is True
        assert report.recommendation == ThrottleRecommendation.NONE


class TestRateAnomaly:
    def test_rate_spike_detected(self):
        from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
        monitor = BehavioralHealthMonitor()
        # Build baseline of 5 emails/cycle
        for _ in range(5):
            monitor.record_cycle(_make_report(emails_processed=5), [_make_envelope()])
        # Sudden spike to 50 emails
        monitor.record_cycle(_make_report(emails_processed=50), [_make_envelope()])
        report = monitor.check_health()
        assert not report.healthy
        assert any("rate" in a.lower() for a in report.anomalies)


class TestErrorRateAnomaly:
    def test_error_spike_detected(self):
        from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
        monitor = BehavioralHealthMonitor()
        # Baseline: no errors
        for _ in range(5):
            monitor.record_cycle(_make_report(errors=[]), [_make_envelope()])
        # Spike: many errors
        monitor.record_cycle(
            _make_report(emails_processed=10, errors=["err"] * 8),
            [_make_envelope()],
        )
        report = monitor.check_health()
        assert not report.healthy
        assert any("error" in a.lower() for a in report.anomalies)


class TestConfidenceDegradation:
    def test_declining_confidence_detected(self):
        from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
        monitor = BehavioralHealthMonitor()
        # Confidence declining over 5 cycles
        for conf in [0.95, 0.85, 0.75, 0.65, 0.55]:
            monitor.record_cycle(_make_report(), [_make_envelope(confidence=conf)])
        report = monitor.check_health()
        assert not report.healthy
        assert any("confidence" in a.lower() for a in report.anomalies)


class TestThrottleRecommendations:
    def test_reduce_batch_recommendation(self):
        from autonomy.contracts.behavioral_health import (
            BehavioralHealthMonitor, ThrottleRecommendation,
        )
        monitor = BehavioralHealthMonitor()
        # Moderate anomaly: high error rate
        for _ in range(5):
            monitor.record_cycle(_make_report(errors=[]), [_make_envelope()])
        monitor.record_cycle(
            _make_report(emails_processed=10, errors=["err"] * 6),
            [_make_envelope()],
        )
        rec, reason = monitor.should_throttle()
        assert rec in (
            ThrottleRecommendation.REDUCE_BATCH,
            ThrottleRecommendation.PAUSE_CYCLE,
            ThrottleRecommendation.CIRCUIT_BREAK,
        )
        assert reason is not None

    def test_no_throttle_when_healthy(self):
        from autonomy.contracts.behavioral_health import (
            BehavioralHealthMonitor, ThrottleRecommendation,
        )
        monitor = BehavioralHealthMonitor()
        for _ in range(5):
            monitor.record_cycle(_make_report(), [_make_envelope()])
        rec, reason = monitor.should_throttle()
        assert rec == ThrottleRecommendation.NONE
        assert reason is None


class TestSlidingWindow:
    def test_old_cycles_drop_out(self):
        from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
        monitor = BehavioralHealthMonitor(window_size=3)
        # 3 bad cycles, then 3 good cycles
        for _ in range(3):
            monitor.record_cycle(
                _make_report(emails_processed=10, errors=["err"] * 8),
                [_make_envelope()],
            )
        for _ in range(3):
            monitor.record_cycle(_make_report(errors=[]), [_make_envelope()])
        report = monitor.check_health()
        # Bad cycles should have fallen out of window
        assert report.healthy is True
```

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/autonomy/contracts/test_behavioral_health.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autonomy.contracts.behavioral_health'`

**Step 3: Write the implementation**

Create `backend/autonomy/contracts/behavioral_health.py`:

```python
"""BehavioralHealthMonitor — anomaly detection on autonomous behavior.

Monitors sliding window of triage cycle metrics for:
- Rate spikes (actions per cycle > threshold * rolling mean)
- Error rate spikes (error ratio > threshold * rolling mean)
- Confidence degradation (mean confidence trending down)
- Tier distribution shifts (not yet: deferred to Phase B)

Returns typed recommendations. Does NOT mutate control directly.
The supervisor/runtime decides whether to apply throttling.

All thresholds are env-var configurable.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

from core.contracts.decision_envelope import DecisionEnvelope

# Lazy import to avoid circular — TriageCycleReport is only used for type
try:
    from autonomy.email_triage.schemas import TriageCycleReport
except ImportError:
    TriageCycleReport = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Enums & Value Types
# ---------------------------------------------------------------------------

class ThrottleRecommendation(str, Enum):
    NONE = "none"
    REDUCE_BATCH = "reduce_batch"
    PAUSE_CYCLE = "pause_cycle"
    CIRCUIT_BREAK = "circuit_break"


@dataclass(frozen=True)
class BehavioralHealthReport:
    healthy: bool
    anomalies: Tuple[str, ...]
    recommendation: ThrottleRecommendation
    recommended_max_emails: Optional[int]
    confidence: float
    window_cycles: int
    metrics: Dict[str, float]


# ---------------------------------------------------------------------------
# Per-Cycle Snapshot (internal)
# ---------------------------------------------------------------------------

@dataclass
class _CycleSnapshot:
    emails_processed: int
    error_count: int
    error_ratio: float
    mean_confidence: float
    tier_counts: Dict[int, int]


# ---------------------------------------------------------------------------
# BehavioralHealthMonitor
# ---------------------------------------------------------------------------

class BehavioralHealthMonitor:
    """Sliding-window anomaly detection for autonomous triage behavior."""

    def __init__(self, window_size: int = 0) -> None:
        self._window_size = window_size or _env_int(
            "BEHAVIORAL_HEALTH_WINDOW_SIZE", 10
        )
        self._snapshots: Deque[_CycleSnapshot] = deque(maxlen=self._window_size)

        # Thresholds (env-var configurable)
        self._rate_spike_factor = _env_float("BEHAVIORAL_HEALTH_RATE_SPIKE_FACTOR", 3.0)
        self._error_spike_factor = _env_float("BEHAVIORAL_HEALTH_ERROR_SPIKE_FACTOR", 2.0)
        self._confidence_slope_threshold = _env_float(
            "BEHAVIORAL_HEALTH_CONFIDENCE_SLOPE", -0.05
        )
        self._min_cycles_for_detection = _env_int(
            "BEHAVIORAL_HEALTH_MIN_CYCLES", 3
        )

    def record_cycle(
        self,
        report: Any,  # TriageCycleReport
        envelopes: List[DecisionEnvelope],
    ) -> None:
        processed = getattr(report, "emails_processed", 0)
        errors = getattr(report, "errors", [])
        error_count = len(errors)
        error_ratio = error_count / max(processed, 1)
        tier_counts = dict(getattr(report, "tier_counts", {}))

        confidences = [e.confidence for e in envelopes] if envelopes else [1.0]
        mean_conf = sum(confidences) / len(confidences)

        self._snapshots.append(_CycleSnapshot(
            emails_processed=processed,
            error_count=error_count,
            error_ratio=error_ratio,
            mean_confidence=mean_conf,
            tier_counts=tier_counts,
        ))

    def check_health(self) -> BehavioralHealthReport:
        anomalies: List[str] = []

        if len(self._snapshots) < self._min_cycles_for_detection:
            return BehavioralHealthReport(
                healthy=True, anomalies=(), recommendation=ThrottleRecommendation.NONE,
                recommended_max_emails=None, confidence=1.0,
                window_cycles=len(self._snapshots), metrics={},
            )

        snapshots = list(self._snapshots)

        # Rate spike detection
        rates = [s.emails_processed for s in snapshots]
        if len(rates) >= 2:
            baseline_mean = sum(rates[:-1]) / len(rates[:-1])
            latest = rates[-1]
            if baseline_mean > 0 and latest > self._rate_spike_factor * baseline_mean:
                anomalies.append(
                    f"Rate spike: {latest} emails vs baseline mean {baseline_mean:.1f}"
                )

        # Error rate spike detection
        error_ratios = [s.error_ratio for s in snapshots]
        if len(error_ratios) >= 2:
            baseline_mean = sum(error_ratios[:-1]) / len(error_ratios[:-1])
            latest = error_ratios[-1]
            if latest > 0.3 and (baseline_mean == 0 or latest > self._error_spike_factor * max(baseline_mean, 0.05)):
                anomalies.append(
                    f"Error rate spike: {latest:.0%} vs baseline {baseline_mean:.0%}"
                )

        # Confidence degradation detection
        confidences = [s.mean_confidence for s in snapshots]
        if len(confidences) >= self._min_cycles_for_detection:
            n = len(confidences)
            x_mean = (n - 1) / 2.0
            y_mean = sum(confidences) / n
            numerator = sum((i - x_mean) * (c - y_mean) for i, c in enumerate(confidences))
            denominator = sum((i - x_mean) ** 2 for i in range(n))
            if denominator > 0:
                slope = numerator / denominator
                if slope < self._confidence_slope_threshold:
                    anomalies.append(
                        f"Confidence degradation: slope={slope:.3f}/cycle"
                    )

        healthy = len(anomalies) == 0
        recommendation, rec_max = self._compute_recommendation(anomalies, snapshots)

        return BehavioralHealthReport(
            healthy=healthy,
            anomalies=tuple(anomalies),
            recommendation=recommendation,
            recommended_max_emails=rec_max,
            confidence=1.0 - min(len(anomalies) * 0.3, 0.9),
            window_cycles=len(snapshots),
            metrics={
                "mean_error_ratio": sum(s.error_ratio for s in snapshots) / len(snapshots),
                "mean_emails": sum(s.emails_processed for s in snapshots) / len(snapshots),
                "latest_confidence": snapshots[-1].mean_confidence if snapshots else 1.0,
            },
        )

    def should_throttle(self) -> Tuple[ThrottleRecommendation, Optional[str]]:
        report = self.check_health()
        if report.recommendation == ThrottleRecommendation.NONE:
            return ThrottleRecommendation.NONE, None
        reasons = "; ".join(report.anomalies)
        return report.recommendation, reasons

    def _compute_recommendation(
        self,
        anomalies: List[str],
        snapshots: List[_CycleSnapshot],
    ) -> Tuple[ThrottleRecommendation, Optional[int]]:
        if not anomalies:
            return ThrottleRecommendation.NONE, None
        if len(anomalies) >= 3:
            return ThrottleRecommendation.CIRCUIT_BREAK, 0
        if any("error" in a.lower() for a in anomalies):
            mean_emails = sum(s.emails_processed for s in snapshots) / len(snapshots)
            return ThrottleRecommendation.REDUCE_BATCH, max(1, int(mean_emails * 0.5))
        if any("rate" in a.lower() for a in anomalies):
            mean_emails = sum(s.emails_processed for s in snapshots) / len(snapshots)
            return ThrottleRecommendation.REDUCE_BATCH, max(1, int(mean_emails))
        return ThrottleRecommendation.PAUSE_CYCLE, None
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/autonomy/contracts/test_behavioral_health.py -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add backend/autonomy/contracts/behavioral_health.py tests/unit/autonomy/contracts/
git commit -m "feat(contracts): add BehavioralHealthMonitor with sliding-window anomaly detection"
```

---

### Task 5: Thin adapter protocols (ReasoningProvider + ActionExecutor)

**Files:**
- Create: `backend/autonomy/contracts/reasoning_provider.py`
- Create: `backend/autonomy/contracts/action_executor.py`
- Test: `tests/unit/autonomy/contracts/test_adapter_protocols.py`

**Step 1: Write the failing tests**

Create `tests/unit/autonomy/contracts/test_adapter_protocols.py`:

```python
"""Tests for ReasoningProvider and ActionExecutor thin adapter protocols."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest

from core.contracts.decision_envelope import (
    DecisionEnvelope, DecisionType, DecisionSource, OriginComponent,
)
from core.contracts.policy_gate import PolicyVerdict, VerdictAction


def _make_envelope():
    return DecisionEnvelope(
        envelope_id="env-1", trace_id="t-1", parent_envelope_id=None,
        decision_type=DecisionType.EXTRACTION, source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
        payload={}, confidence=0.9,
        created_at_epoch=time.time(), created_at_monotonic=time.monotonic(),
        causal_seq=1, config_version="v1",
    )


def _make_verdict():
    return PolicyVerdict(
        allowed=True, action=VerdictAction.ALLOW, reason="test",
        conditions=(), envelope_id="env-1", gate_name="test",
        created_at_epoch=time.time(), created_at_monotonic=time.monotonic(),
    )


class TestReasoningProviderProtocol:
    @pytest.mark.asyncio
    async def test_conforming_class_passes(self):
        from autonomy.contracts.reasoning_provider import ReasoningProvider

        class MockProvider:
            async def reason(self, prompt, context, deadline=None):
                return _make_envelope()

            @property
            def provider_name(self):
                return DecisionSource.HEURISTIC

        provider = MockProvider()
        assert isinstance(provider, ReasoningProvider)
        result = await provider.reason("test", {})
        assert isinstance(result, DecisionEnvelope)

    def test_non_conforming_class_fails(self):
        from autonomy.contracts.reasoning_provider import ReasoningProvider

        class NotAProvider:
            pass

        assert not isinstance(NotAProvider(), ReasoningProvider)


class TestActionExecutorProtocol:
    @pytest.mark.asyncio
    async def test_conforming_class_passes(self):
        from autonomy.contracts.action_executor import ActionExecutor, ActionOutcome

        class MockExecutor:
            async def execute(self, envelope, verdict, commit_id):
                return ActionOutcome.SUCCESS

        executor = MockExecutor()
        assert isinstance(executor, ActionExecutor)
        result = await executor.execute(_make_envelope(), _make_verdict(), "cid-1")
        assert result == ActionOutcome.SUCCESS

    def test_non_conforming_class_fails(self):
        from autonomy.contracts.action_executor import ActionExecutor

        class NotAnExecutor:
            pass

        assert not isinstance(NotAnExecutor(), ActionExecutor)

    def test_action_outcome_members(self):
        from autonomy.contracts.action_executor import ActionOutcome
        assert ActionOutcome.SUCCESS == "success"
        assert ActionOutcome.PARTIAL == "partial"
        assert ActionOutcome.FAILED == "failed"
        assert ActionOutcome.SKIPPED == "skipped"
```

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/autonomy/contracts/test_adapter_protocols.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementations**

Create `backend/autonomy/contracts/reasoning_provider.py`:

```python
"""ReasoningProvider — thin protocol for AI reasoning backends.

Wraps any reasoning backend (PrimeRouter, Claude API, heuristic engine)
behind a uniform async interface that returns DecisionEnvelopes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from core.contracts.decision_envelope import DecisionEnvelope, DecisionSource


@runtime_checkable
class ReasoningProvider(Protocol):
    """Async reasoning provider that returns typed DecisionEnvelopes."""

    async def reason(
        self,
        prompt: str,
        context: Dict[str, Any],
        deadline: Optional[float] = None,
    ) -> DecisionEnvelope: ...

    @property
    def provider_name(self) -> DecisionSource: ...
```

Create `backend/autonomy/contracts/action_executor.py`:

```python
"""ActionExecutor — thin protocol for executing committed actions.

Wraps any action execution (apply_label, deliver_notification, etc.)
behind a uniform async interface that returns ActionOutcome.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from core.contracts.decision_envelope import DecisionEnvelope
from core.contracts.policy_gate import PolicyVerdict


class ActionOutcome(str, Enum):
    """Result of executing an action."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


@runtime_checkable
class ActionExecutor(Protocol):
    """Async action executor that returns typed outcomes."""

    async def execute(
        self,
        envelope: DecisionEnvelope,
        verdict: PolicyVerdict,
        commit_id: str,
    ) -> ActionOutcome: ...
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/autonomy/contracts/test_adapter_protocols.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add backend/autonomy/contracts/reasoning_provider.py backend/autonomy/contracts/action_executor.py tests/unit/autonomy/contracts/test_adapter_protocols.py
git commit -m "feat(contracts): add ReasoningProvider and ActionExecutor thin adapter protocols"
```

---

### Task 6: Update package __init__.py exports and run full test suite

**Files:**
- Modify: `backend/core/contracts/__init__.py`
- Modify: `backend/autonomy/contracts/__init__.py`

**Step 1: Update core contracts __init__.py**

```python
"""Core contracts for the JARVIS autonomous pipeline.

Public API:
- DecisionEnvelope, DecisionType, DecisionSource, OriginComponent
- IdempotencyKey, EnvelopeFactory
- PolicyGate, PolicyVerdict, VerdictAction
- ActionCommitLedger, CommitRecord, CommitState
"""

from core.contracts.decision_envelope import (
    DecisionEnvelope,
    DecisionSource,
    DecisionType,
    EnvelopeFactory,
    IdempotencyKey,
    OriginComponent,
)
from core.contracts.policy_gate import PolicyGate, PolicyVerdict, VerdictAction
from core.contracts.action_commit_ledger import (
    ActionCommitLedger,
    CommitRecord,
    CommitState,
)
```

**Step 2: Update autonomy contracts __init__.py**

```python
"""Autonomy-specific contract implementations.

Public API:
- BehavioralHealthMonitor, BehavioralHealthReport, ThrottleRecommendation
- ReasoningProvider
- ActionExecutor, ActionOutcome
"""

from autonomy.contracts.behavioral_health import (
    BehavioralHealthMonitor,
    BehavioralHealthReport,
    ThrottleRecommendation,
)
from autonomy.contracts.reasoning_provider import ReasoningProvider
from autonomy.contracts.action_executor import ActionExecutor, ActionOutcome
```

**Step 3: Run full Phase A test suite**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/ tests/unit/autonomy/contracts/ -v`
Expected: All tests PASS (should be ~45 tests total across 4 test files)

**Step 4: Run existing email triage tests to verify no regressions**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/ -q --tb=short -k "not test_disabled_flag_skips_entirely"`
Expected: 223 pass, 0 fail (same as before Phase A)

**Step 5: Commit**

```bash
git add backend/core/contracts/__init__.py backend/autonomy/contracts/__init__.py
git commit -m "feat(contracts): export all Phase A contracts from package __init__"
```

---

### Task 7: Final verification and summary commit

**Step 1: Run the complete test suite**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/ tests/unit/autonomy/contracts/ tests/unit/backend/email_triage/ -q --tb=short -k "not test_disabled_flag_skips_entirely"`
Expected: ~268 tests pass

**Step 2: Verify file structure**

Run: `find backend/core/contracts backend/autonomy/contracts -name '*.py' | sort`
Expected output:
```
backend/autonomy/contracts/__init__.py
backend/autonomy/contracts/action_executor.py
backend/autonomy/contracts/behavioral_health.py
backend/autonomy/contracts/reasoning_provider.py
backend/core/contracts/__init__.py
backend/core/contracts/action_commit_ledger.py
backend/core/contracts/decision_envelope.py
backend/core/contracts/policy_gate.py
```

**Step 3: Verify done criteria**

Check against design doc `docs/plans/2026-03-06-phase-a-formal-contracts-design.md`:

1. All 4 contracts typed and versioned (schema_version + compat_min_version) -- YES
2. Ledger state machine with atomic SQLite transitions -- YES
3. Async PolicyGate protocol + conformance test -- YES
4. DecisionEnvelope with dual clocks + provenance + typed enums -- YES
5. Canonical IdempotencyKey builder (deterministic, shared) -- YES
6. BehavioralHealthMonitor returns recommendations only -- YES
7. Unit tests for all specified scenarios -- YES
